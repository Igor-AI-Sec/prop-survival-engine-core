"""Fenced SQLite ownership and explicit inventory gates; no provider I/O."""
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
import json
from uuid import UUID, uuid4

from prop_survival_engine.core import events as E
from prop_survival_engine.core.models import Side
from prop_survival_engine.core.ownership import (
    OperationalOwner, OwnershipStatus, InventoryOrderState as I, InventoryClassification as C,
    PositionReconciliationEvidence, InventoryLiability, InventoryEvidence, InventoryOrder,
)
from prop_survival_engine.core.reservations import RiskExposure, RiskOwner
from prop_survival_engine.persistence.sqlite import SQLiteOmsJournal
from prop_survival_engine.risk.money import decimal_from_fraction
from prop_survival_engine.risk.reservations import SQLiteRiskLedger, ReservationResult, ReservationRecoveryRequired
from prop_survival_engine.risk.service import RiskAction, RiskDecision


class OwnershipUnavailable(RuntimeError):
    pass


class StaleOwner(RuntimeError):
    pass


class InventoryRejected(ReservationRecoveryRequired):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class OperationalRiskLedger(SQLiteRiskLedger):
    """Strict operational journal; legacy journals remain offline fixtures only.

    No automatic leases. A takeover is an explicit administrative CAS operation,
    never an inference from PID, heartbeat age, receipt time or wall-clock expiry.
    """
    operational_managed = True
    def __init__(self, path, *, account_id, config, environment_id, binding_version,
                 loss_bounds=None, owner_id=None, clock=None, max_snapshot_age=timedelta(seconds=30), fault=None):
        if not environment_id or not binding_version or max_snapshot_age <= timedelta(0):
            raise ValueError("Explicit inventory binding and positive freshness limit required")
        self._initializing = True
        self.owner_id, self.fencing_token = owner_id or uuid4(), None
        if type(self.owner_id) is not UUID:
            raise ValueError("owner_id must be a UUID")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.environment_id, self.binding_version = environment_id, binding_version
        self.max_snapshot_age = max_snapshot_age
        self.loss_bounds = dict(loss_bounds or {})
        if any(not key or type(v) is not Decimal or not v.is_finite() or v <= 0 for key, v in self.loss_bounds.items()):
            raise ValueError("Loss bounds must be explicit positive finite Decimal")
        super().__init__(path, account_id=account_id, config=config, fault=fault)
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS operational_owner(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operational_inventory(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operational_binding(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
        """)
        binding = encode(dict(account_id=account_id, environment_id=environment_id,
                              binding_version=binding_version,
                              loss_bounds={k: str(v) for k, v in self.loss_bounds.items()},
                              max_age_seconds=str(max_snapshot_age.total_seconds())))
        with super()._tx():
            self._connection.execute("INSERT OR IGNORE INTO operational_binding VALUES(1,?)", (binding,))
            if self._connection.execute("SELECT data FROM operational_binding").fetchone()[0] != binding:
                raise ValueError("Operational account/environment binding mismatch")
        self._initializing = False

    def _owner_data(self):
        row = self._connection.execute("SELECT data FROM operational_owner").fetchone()
        return json.loads(row[0]) if row else None

    def current_owner(self):
        with self._lock:
            p = self._owner_data()
            return None if p is None else OperationalOwner(UUID(p["owner_id"]), p["account_id"], p["fencing_token"],
                datetime.fromisoformat(p["acquired_at"]), datetime.fromisoformat(p["renewed_at"]), OwnershipStatus(p["status"]))

    def _validate_fence(self):
        p = self._owner_data()
        if p is not None:
            self.current_owner()  # validate stored types and identity structure
        if (p is None or p["status"] != "ACTIVE" or p["owner_id"] != str(self.owner_id)
                or p["fencing_token"] != self.fencing_token or p["account_id"] != self.account_id):
            raise StaleOwner("No active matching account fencing token")

    def validate_ownership(self):
        with super()._tx():
            self._validate_fence()

    @contextmanager
    def _tx(self):
        with super()._tx():
            if not self._initializing:
                self._validate_fence()
            yield

    def acquire_ownership(self, *, expected_previous_token=None):
        with super()._tx():
            old = self._owner_data()
            if old:
                self.current_owner()
            if expected_previous_token is not None:
                if type(expected_previous_token) is not int or old is None or old["fencing_token"] != expected_previous_token:
                    raise OwnershipUnavailable("Takeover requires exact previous fencing token")
            elif old and old["status"] == "ACTIVE":
                raise OwnershipUnavailable("Account already has an active owner; explicit takeover required")
            token = 1 if old is None else old["fencing_token"] + 1
            moment = self.clock()
            if moment.utcoffset() is None:
                raise ValueError("Ownership telemetry requires an aware clock")
            now = moment.astimezone(timezone.utc).isoformat()
            value = dict(owner_id=str(self.owner_id), account_id=self.account_id, fencing_token=token,
                         acquired_at=now, renewed_at=now, status="ACTIVE")
            self._connection.execute("INSERT OR REPLACE INTO operational_owner VALUES(1,?)", (encode(value),))
            self._connection.execute("UPDATE risk_account SET ready=0")
            inventory = self._inventory()
            if inventory:
                inventory["accepted"] = False
                self._save_inventory(inventory)
            SQLiteOmsJournal.append(self, E.OWNERSHIP_CHANGED, value)
            self._fail_at("ownership_before_commit")
        self.fencing_token = token
        self._fail_at("ownership_after_commit")
        return self.current_owner()

    def ensure_ownership(self):
        if self.fencing_token is None:
            self.acquire_ownership()
        else:
            self.validate_ownership()

    def renew_ownership(self):
        with self._tx():
            value = self._owner_data()
            moment = self.clock()
            if moment.utcoffset() is None:
                raise ValueError("Ownership telemetry requires an aware clock")
            value["renewed_at"] = moment.astimezone(timezone.utc).isoformat()
            self._connection.execute("UPDATE operational_owner SET data=?", (encode(value),))
            SQLiteOmsJournal.append(self, E.OWNERSHIP_CHANGED, value)
            self._fail_at("renewal_before_commit")
        return self.current_owner()

    def release_ownership(self):
        with self._tx():
            value = self._owner_data()
            value["status"] = "RELEASED"
            self._connection.execute("UPDATE operational_owner SET data=?", (encode(value),))
            self._connection.execute("UPDATE risk_account SET ready=0")
            SQLiteOmsJournal.append(self, E.OWNERSHIP_CHANGED, value)
            self._fail_at("release_before_commit")

    def risk_begin_recovery(self):
        if self._initializing:
            return  # connecting must not reset another owner's projections
        with self._tx():
            inventory = self._inventory()
            if inventory:
                inventory["accepted"] = False
                self._save_inventory(inventory)
        super().risk_begin_recovery()

    def append(self, event_type, payload):
        with self._lock:
            outer = not self._connection.in_transaction
            try:
                with self._tx():
                    payload = dict(payload, operational_fence=self.fencing_token, operational_owner=str(self.owner_id))
                    record = super().append(event_type, payload)
                    if event_type in {E.INVENTORY_OBSERVED, E.INVENTORY_POSITIONS_CONFIRMED}:
                        self._save_inventory(payload["inventory"])
                        self._fail_at("inventory_before_commit")
                    return record
            except StaleOwner:
                raise  # never let a stale actor gate/mutate its successor
            except BaseException:
                if outer:
                    with self._tx():
                        self._connection.execute("UPDATE risk_account SET ready=0")
                        current = self._inventory()
                        if current:
                            current["accepted"] = False
                            self._save_inventory(current)
                raise

    def _inventory(self):
        row = self._connection.execute("SELECT data FROM operational_inventory").fetchone()
        return json.loads(row[0]) if row else None

    def _save_inventory(self, value):
        self._connection.execute("INSERT OR REPLACE INTO operational_inventory VALUES(1,?)", (encode(value),))

    def inventory_report(self):
        with self._tx():
            return self._inventory()

    def _fresh(self, stamp):
        if stamp is None:
            return False
        time = datetime.fromisoformat(stamp)
        return time.utcoffset() is not None and timedelta(0) <= self.clock() - time <= self.max_snapshot_age

    def _known_positions(self):
        quantities = {}
        for r in self._rows():
            sign = 1 if r["side"] == "BUY" else -1
            quantities[r["instrument"]] = quantities.get(r["instrument"], 0) + sign * (r["filled"] - r["closed"])
        for r in self._external():
            if r["owner"] == RiskOwner.POSITION.value:
                quantities[r["instrument"]] = quantities.get(r["instrument"], 0) + (1 if r["side"] == "BUY" else -1) * r["quantity"]
        return {k: v for k, v in quantities.items() if v}

    def _provider_evidence_after(self, timestamp):
        # Provider timestamps establish contradictions; local receipt time
        # is not substituted for venue ordering or snapshot authority.
        for record in self.replay():
            p = record.payload
            stamps = [p.get("provider_timestamp")]
            if record.event_type == E.PLATFORM_EXECUTION:
                stamps.append(p.get("timestamp"))
            if p.get("cursor"):
                stamps.append(p["cursor"][0])
            if p.get("event"):
                stamps.append(p["event"].get("provider_timestamp"))
            if p.get("execution"):
                stamps.append(p["execution"].get("timestamp"))
            if any(datetime.fromisoformat(s) > timestamp for s in stamps if s):
                return True
        return False

    def prepare_inventory(self, platform, local_orders):
        try:
            return self._prepare_inventory(platform, local_orders)
        except Exception:
            with self._tx():
                current = self._inventory()
                if current:
                    current["accepted"] = False
                    self._save_inventory(current)
                self.append(E.RISK_GATED, {"reason": "inventory rejected"})
            raise

    def _prepare_inventory(self, platform, local_orders):
        """Validate before OMS may project snapshot state; retain unexplained risk."""
        with self._tx():
            evidence = platform.inventory
            old = self._inventory()
            if (type(evidence) is not InventoryEvidence or evidence.account_id != self.account_id
                    or evidence.environment_id != self.environment_id or evidence.binding_version != self.binding_version):
                self.append(E.RISK_GATED, {"reason": "inventory binding missing or mismatched"})
                raise InventoryRejected("Inventory account/environment identity mismatch")
            if type(evidence.orders) is not tuple or any(type(o) is not InventoryOrder for o in evidence.orders):
                raise InventoryRejected("Provider-neutral immutable order evidence required")
            if (any(type(value) is not bool or not value for value in
                    (evidence.orders_complete, evidence.positions_complete, evidence.executions_complete))
                    or platform.positions is None or type(evidence.provider_version) is not int
                    or evidence.provider_version < 0 or not self._fresh(evidence.provider_timestamp.isoformat() if evidence.provider_timestamp else None)):
                self.append(E.RISK_GATED, {"reason": "incomplete or stale inventory"})
                raise InventoryRejected("Complete inventory with fresh provider timestamp and version required")
            if any(type(k) is not str or not k or type(v) is not int for k, v in platform.positions.items()):
                raise InventoryRejected("Invalid inventory quantities")
            if self._provider_evidence_after(evidence.provider_timestamp):
                raise InventoryRejected("Snapshot predates known provider evidence")
            value = dict(account_id=evidence.account_id, environment_id=evidence.environment_id,
                binding_version=evidence.binding_version, timestamp=evidence.provider_timestamp.astimezone(timezone.utc).isoformat(),
                version=evidence.provider_version, positions=dict(platform.positions),
                orders=[dict(asdict(o), side=o.side.value, state=o.state.value) for o in evidence.orders],
                executions=[dict(execution_id=x.execution_id, provider_order_id=x.provider_order_id,
                    instrument=x.instrument, side=x.side, quantity=x.quantity, price=str(x.price),
                    timestamp=x.timestamp.isoformat(), our_order_id=x.our_order_id) for x in platform.executions])
            canonical = dict(value)
            unique = {}
            for item in value["orders"]:
                key = item["provider_order_id"]
                if key in unique and unique[key] != item:
                    raise InventoryRejected("Conflicting duplicate inventory order")
                unique[key] = item
            canonical["orders"] = [unique[k] for k in sorted(unique)]
            unique = {}
            for item in value["executions"]:
                key = (item["provider_order_id"], item["execution_id"])
                if key in unique and unique[key] != item:
                    raise InventoryRejected("Conflicting duplicate inventory execution")
                unique[key] = item
            canonical["executions"] = [unique[k] for k in sorted(unique)]
            digest = sha256(encode(canonical).encode()).hexdigest()
            if old and (value["version"] < old["version"] or value["timestamp"] < old["timestamp"]
                        or value["version"] == old["version"] and digest != old["digest"]):
                raise InventoryRejected("Regressing or conflicting provider inventory version")
            by_provider = {}
            for remote in platform.orders:
                if not remote.provider_order_id or remote.provider_order_id in by_provider and by_provider[remote.provider_order_id] != remote:
                    raise InventoryRejected("Ambiguous platform order identity")
                by_provider[remote.provider_order_id] = remote
            if set(by_provider) != {o.provider_order_id for o in evidence.orders}:
                raise InventoryRejected("Snapshot/evidence order coverage mismatch")
            held = dict(old["held"]) if old else {}
            classifications, known_remote, seen, external_live, proven_fills = [], [], {}, False, []
            for remote in evidence.orders:
                projection = by_provider[remote.provider_order_id]
                equivalent = {I.OPEN: "ACCEPTED", I.WORKING: "PARTIALLY_FILLED" if remote.filled_quantity else "ACCEPTED",
                              I.PENDING: "PENDING", I.UNKNOWN: "UNKNOWN", I.FILLED: "FILLED",
                              I.CANCELLED: "CANCELLED", I.REJECTED: "REJECTED"}
                if (projection.status.value != equivalent[remote.state]
                        or projection.filled_quantity != remote.filled_quantity
                        or projection.client_order_id != (remote.client_order_id or "")
                        or projection.our_order_id != remote.our_order_id):
                    raise InventoryRejected("Platform order and inventory evidence disagree")
                encoded = encode(value["orders"][len(classifications)])
                if remote.provider_order_id in seen:
                    if seen[remote.provider_order_id] != encoded:
                        raise InventoryRejected("Ambiguous duplicate inventory order")
                    classifications.append(C.DUPLICATE.value)
                    continue
                seen[remote.provider_order_id] = encoded
                candidates = [m for m in local_orders.values() if (
                    remote.provider_order_id == m.provider_order_id
                    or remote.client_order_id and remote.client_order_id == m.order.client_order_id
                    or remote.our_order_id and remote.our_order_id == str(m.order.order_id))]
                if (len(candidates) > 1 or candidates and candidates[0].provider_order_id is not None
                        and candidates[0].provider_order_id != remote.provider_order_id):
                    classifications.append(C.AMBIGUOUS.value)
                    held["ambiguous:" + remote.provider_order_id] = dict(instrument=remote.instrument,
                        side=remote.side.value, quantity=remote.quantity, remaining=remote.quantity - remote.filled_quantity,
                        filled=remote.filled_quantity, accounted_filled=0, loss=None, terminal=False)
                    external_live = True
                    continue
                if candidates:
                    m = candidates[0]
                    if (remote.instrument != m.order.instrument or remote.side != m.order.side
                            or remote.quantity != m.order.quantity
                            or remote.filled_quantity < m.filled_quantity
                            or remote.provider_order_id != by_provider[remote.provider_order_id].provider_order_id
                            or remote.filled_quantity != by_provider[remote.provider_order_id].filled_quantity
                            or remote.our_order_id and remote.our_order_id != str(m.order.order_id)
                            or remote.client_order_id and remote.client_order_id != m.order.client_order_id):
                        raise InventoryRejected("Ambiguous local inventory identity")
                    if remote.state in {I.PENDING, I.UNKNOWN}:
                        external_live = True
                    classifications.append(C.LOCAL.value)
                    known_remote.append(by_provider[remote.provider_order_id])
                    proven_fills.append((remote.instrument, remote.side, remote.filled_quantity - m.filled_quantity))
                    continue
                key = "order:" + remote.provider_order_id
                terminal = remote.state in {I.CANCELLED, I.REJECTED, I.FILLED}
                classifications.append(C.TERMINAL_HISTORY.value if terminal else C.EXTERNAL.value)
                if terminal and key not in held:
                    continue  # completed historical record does not prove an open position
                previous = held.get(key)
                if previous and (previous["instrument"] != remote.instrument or previous["side"] != remote.side.value
                                 or previous["quantity"] != remote.quantity
                                 or remote.filled_quantity < previous["filled"] + previous.get("accounted_filled", 0)
                                 or previous["terminal"] and (not terminal or previous.get("terminal_state") != remote.state.value)):
                    raise InventoryRejected("External order identity/volume changed ambiguously")
                loss = self.loss_bounds.get(remote.instrument)
                held[key] = dict(instrument=remote.instrument, side=remote.side.value, quantity=remote.quantity,
                    remaining=0 if terminal else remote.quantity - remote.filled_quantity,
                    filled=remote.filled_quantity - (previous.get("accounted_filled", 0) if previous else 0),
                    accounted_filled=previous.get("accounted_filled", 0) if previous else 0,
                    loss=str(loss) if loss is not None else None,
                    terminal=terminal, terminal_state=remote.state.value if terminal else None)
                external_live |= not terminal
            known = self._known_positions()
            for instrument, side, quantity in proven_fills:
                known[instrument] = known.get(instrument, 0) + (1 if side is Side.BUY else -1) * quantity
            known = {k: v for k, v in known.items() if v}
            provider = {k: v for k, v in platform.positions.items() if v}
            mismatches = {k: [known.get(k, 0), provider.get(k, 0)] for k in set(known) | set(provider)
                          if known.get(k, 0) != provider.get(k, 0)}
            confirmed = old.get("confirmed", {}) if old and old["digest"] == digest else {}
            # Position supplements are independent of order liability. Filled
            # external units already cover same-direction provider position units.
            supplements = {}
            prior_supplements = old["supplements"] if old else {}
            for instrument in set(provider) | set(prior_supplements):
                quantity = provider.get(instrument, 0)
                prior = prior_supplements.get(instrument)
                sign = (1 if quantity > 0 else -1) if quantity else (1 if prior["side"] == "BUY" else -1)
                side = "BUY" if sign > 0 else "SELL"
                if prior and prior["side"] != side:
                    raise InventoryRejected("External position direction changed without disposition evidence")
                old_filled = sum(r["filled"] for r in (old["held"].values() if old else ())
                    if r["instrument"] == instrument and r["side"] == side)
                new_filled = sum(r["filled"] for r in held.values()
                    if r["instrument"] == instrument and r["side"] == side)
                proven = sum(q for inst, s, q in proven_fills if inst == instrument and s.value == side)
                # Newly attributed fills transfer units out of the position
                # supplement, not a second copy of the same inventory risk.
                prior_units = (prior["quantity"] if prior else 0) + old_filled
                required_units = max(0, prior_units - proven, abs(quantity) - max(0, sign * known.get(instrument, 0)))
                q = max(0, required_units - new_filled)
                if q:
                    loss = self.loss_bounds.get(instrument)
                    supplements[instrument] = dict(quantity=q, side=side,
                                                     loss=str(loss) if loss is not None else None)
            accepted = not external_live and all(k in confirmed and
                [confirmed[k]["expected_local_quantity"], confirmed[k]["provider_quantity"]] == pair
                for k, pair in mismatches.items()) and not supplements
            # Missing previously observed external orders never releases liability.
            accepted &= all(r["terminal"] and not r["filled"] for r in held.values())
            value.update(digest=digest, held=held, supplements=supplements, confirmed=confirmed,
                         classifications=classifications, mismatches=mismatches, local_positions=known,
                         snapshot_cut=self._snapshot_cut, accepted=bool(accepted))
            self.append(E.INVENTORY_OBSERVED, {"inventory": value})
            self._fail_at("external_liability_before_commit")
            if not accepted:
                self.append(E.RISK_GATED, {"reason": "unreconciled inventory"})
            # Unknown/terminal-history evidence stays in the inventory journal,
            # never synthesizes an executable Order or enters legacy adoption.
            return replace(platform, orders=tuple(known_remote)), bool(accepted)

    def confirm_positions(self, evidence: PositionReconciliationEvidence):
        with self._tx():
            current = self._inventory()
            if (type(evidence) is not PositionReconciliationEvidence or not evidence.evidence_id or current is None
                    or current["digest"] != evidence.snapshot_digest or not self._fresh(current["timestamp"])
                    or [current["local_positions"].get(evidence.instrument, 0), current["positions"].get(evidence.instrument, 0)] !=
                       [evidence.expected_local_quantity, evidence.provider_quantity]):
                raise InventoryRejected("Position review does not match fresh persisted inventory")
            if (current["snapshot_cut"] is None or self._connection.execute(
                    "SELECT 1 FROM journal WHERE event_type=? AND sequence>? LIMIT 1",
                    (E.EXECUTION_EVENT_RECEIVED, current["snapshot_cut"])).fetchone()
                    or self._provider_evidence_after(datetime.fromisoformat(current["timestamp"]))):
                raise InventoryRejected("Inventory changed after the position snapshot")
            if evidence.instrument in current["confirmed"]:
                if current["confirmed"][evidence.instrument] != asdict(evidence):
                    raise InventoryRejected("Conflicting position review identity")
                return
            related = [r for r in current["held"].values() if r["instrument"] == evidence.instrument and r["filled"]]
            if any(not r["terminal"] or r["loss"] is None for r in related):
                raise InventoryRejected("Unresolved/unpriced external executions remain held")
            transferred = 0
            if related:
                sides = {r["side"] for r in related}
                provider = evidence.provider_quantity
                if len(sides) != 1 or provider and (next(iter(sides)) == "BUY") != (provider > 0):
                    raise InventoryRejected("Ambiguous external position disposition")
                quantity = min(sum(r["filled"] for r in related), abs(provider))
                if quantity:
                    self.register_exposure(RiskExposure("inventory-filled:" + evidence.snapshot_digest + ":" + evidence.instrument,
                        evidence.instrument, Side(next(iter(sides))), quantity,
                        max(Decimal(r["loss"]) for r in related)))
                    transferred = quantity
                for r in related:
                    r["accounted_filled"] += r["filled"]
                    r["filled"] = 0
            supplement = current["supplements"].get(evidence.instrument)
            if supplement:
                if supplement["loss"] is None:
                    raise InventoryRejected("Cannot reconcile unpriced external position")
                sign = 1 if supplement["side"] == "BUY" else -1
                q = max(0, sign * evidence.provider_quantity - max(0, sign * evidence.expected_local_quantity) - transferred)
                if q:
                    self.register_exposure(RiskExposure("inventory-position:" + evidence.snapshot_digest + ":" + evidence.instrument,
                        evidence.instrument, Side(supplement["side"]), q, Decimal(supplement["loss"])))
                del current["supplements"][evidence.instrument]
            current["confirmed"][evidence.instrument] = asdict(evidence)
            self.append(E.INVENTORY_POSITIONS_CONFIRMED, {"inventory": current})
            self._fail_at("position_confirmation_before_commit")

    def exposure_by_owner(self):
        with self._tx():
            totals = super().exposure_by_owner()
            inventory = self._inventory()
            if not inventory:
                return totals
            for r in inventory["held"].values():
                for owner, q in ((RiskOwner.UNCERTAIN, r["remaining"]), (RiskOwner.POSITION, r["filled"])):
                    if q:
                        totals[owner] = (Decimal("Infinity") if r["loss"] is None or not totals[owner].is_finite()
                            else decimal_from_fraction(Fraction(totals[owner]) + q * Fraction(Decimal(r["loss"]))))
            for r in inventory["supplements"].values():
                totals[RiskOwner.POSITION] = (Decimal("Infinity") if r["loss"] is None or not totals[RiskOwner.POSITION].is_finite()
                    else decimal_from_fraction(Fraction(totals[RiskOwner.POSITION]) + r["quantity"] * Fraction(Decimal(r["loss"]))))
            return totals

    def inventory_liabilities(self):
        with self._tx():
            inventory = self._inventory()
            if not inventory:
                return ()
            result = []
            for key, r in inventory["held"].items():
                for category, q in (("uncertain-order", r["remaining"]), ("position", r["filled"])):
                    if q:
                        result.append(InventoryLiability(key, r["instrument"], Side(r["side"]), q,
                            Decimal(r["loss"]) if r["loss"] is not None else None, category))
            for key, r in inventory["supplements"].items():
                result.append(InventoryLiability("position:" + key, key, Side(r["side"]), r["quantity"],
                    Decimal(r["loss"]) if r["loss"] is not None else None, "position"))
            return tuple(result)

    def reserve(self, intent, estimate):
        with self._tx():
            if not self._inventory_acceptable():
                totals = self.exposure_by_owner().values()
                used = (Decimal("Infinity") if any(not v.is_finite() for v in totals)
                        else decimal_from_fraction(sum((Fraction(v) for v in totals), Fraction(0))))
                return ReservationResult(RiskDecision(RiskAction.REJECT, None, "AUTHORITATIVE_INVENTORY_REQUIRED"),
                                         None, Decimal(0), used)
            return super().reserve(intent, estimate)

    def _inventory_acceptable(self):
        inventory = self._inventory()
        return bool(inventory and inventory["accepted"] and self._fresh(inventory["timestamp"]))

    def risk_ready(self):
        with self._tx():
            return self._inventory_acceptable() and super().risk_ready()

    def risk_finish_recovery(self):
        with self._tx():
            return self._inventory_acceptable() and super().risk_finish_recovery()

    def inventory_positions_accounted(self, positions):
        with self._tx():
            return self._inventory_acceptable() and self._inventory()["positions"] == positions
