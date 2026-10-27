"""Aggregate reservation admission over one account's SQLite/WAL OMS journal.

Every writer uses BEGIN IMMEDIATE, including distinct connections/processes.
Risk ownership projection and its source OMS record commit together. No provider
calls, vendor models, offsetting/netting credit or automatic uncertain release.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from fractions import Fraction
import json
from uuid import UUID, uuid4

from prop_survival_engine.core import events as E
from prop_survival_engine.core.models import Side
from prop_survival_engine.core.reservations import RiskReservation, ReservationStatus as S, RiskOwner as O, RiskExposure
from prop_survival_engine.oms.models import OmsState
from prop_survival_engine.persistence.sqlite import SQLiteOmsJournal
from prop_survival_engine.prop_rules.models import AccountState, AccountStatus
from .features import RiskContext, SessionState, ActiveLossFloors
from .money import decimal_from_fraction
from .policy import RiskPolicy, RiskPolicyConfig
from .service import RiskAction, RiskDecision


class ReservationRecoveryRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class ReservationResult:
    decision: RiskDecision
    reservation: RiskReservation | None
    available_budget: Decimal
    aggregate_loss: Decimal
    replayed: bool = False


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _context(value):
    state = dict(value["account"])
    for key in state:
        if key not in {"session_id", "status"}:
            field_value = state[key]
            if key == "target_equity" and type(field_value) is dict:
                if (set(field_value) != {"type", "numerator", "denominator"}
                        or field_value["type"] != "fraction"
                        or type(field_value["numerator"]) is not int or field_value["numerator"] <= 0
                        or type(field_value["denominator"]) is not int or field_value["denominator"] <= 0):
                    raise ValueError("invalid exact rational target")
                state[key] = Fraction(field_value["numerator"], field_value["denominator"])
            else:
                state[key] = Decimal(field_value)
    state["status"] = AccountStatus(state["status"])
    with localcontext() as ctx:
        ctx.prec = max(1000, sum(len(str(v)) for v in state.values()))
        account = AccountState(**state)
    return RiskContext(account, OmsState(value["oms_state"]), SessionState(value["session_state"]),
                       ActiveLossFloors(**value["active_floors"]))


def _encode_context(context):
    value = asdict(context)
    value["account"] = {k: (v.value if isinstance(v, AccountStatus) else str(v))
                        for k, v in value["account"].items()}
    if type(context.account.target_equity) is Fraction:
        target = context.account.target_equity
        value["account"]["target_equity"] = {
            "type": "fraction", "numerator": target.numerator, "denominator": target.denominator,
        }
    value["oms_state"] = context.oms_state.value
    value["session_state"] = context.session_state.value
    return value


def _intent(intent):
    return {"intent_id": str(intent.intent_id), "instrument": intent.instrument,
            "side": intent.side.value, "quantity": intent.quantity,
            "is_entry": intent.is_entry, "metadata": intent.metadata}


class SQLiteRiskLedger(SQLiteOmsJournal):
    """One account per OMS journal (existing Order has no account routing field).

    Construct all worker connections before startup reconciliation. Opening a
    new connection conservatively recovery-gates this account for every writer.
    The legacy journal remains usable offline, but cannot dispatch into this
    risk-enabled journal without a persisted matching reservation.
    """
    risk_managed = True

    def __init__(self, path, *, account_id: str, config: RiskPolicyConfig, fault=None):
        super().__init__(path)
        if not account_id:
            self.close()
            raise ValueError("account_id required")
        self.account_id, self.policy, self.fault = account_id, RiskPolicy(config), fault
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS risk_account
                (id INTEGER PRIMARY KEY CHECK(id=1), account_id TEXT, config TEXT,
                 context TEXT, revision INTEGER DEFAULT 0, ready INTEGER DEFAULT 0,
                 positions TEXT, complete INTEGER DEFAULT 0, external_seen INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS risk_reservations (intent_id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS risk_external (exposure_id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS risk_inbox (event_id TEXT PRIMARY KEY);
        """)
        self._connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS risk_unique_intent_decision ON journal "
                                 "(json_extract(payload, '$.intent_id')) WHERE event_type='intent_decided'")
        encoded = _json({k: str(v) for k, v in asdict(config).items()})
        try:
            with self._tx():
                self._connection.execute("INSERT OR IGNORE INTO risk_account(id,account_id,config) VALUES(1,?,?)",
                                         (account_id, encoded))
                row = self._connection.execute("SELECT account_id,config FROM risk_account").fetchone()
                if row != (account_id, encoded):
                    raise ValueError("Journal account/policy configuration mismatch")
            self.risk_begin_recovery()
        except BaseException:
            self.close()
            raise

    @contextmanager
    def _tx(self):
        with self._lock:
            if (not getattr(self, "operational_managed", False) and self._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='operational_owner' AND type='table'").fetchone()):
                raise ReservationRecoveryRequired("Operational journal requires an active fenced ledger")
            nested = self._connection.in_transaction
            if not nested:
                self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                if not nested:
                    self._connection.execute("COMMIT")
            except BaseException:
                if not nested:
                    self._connection.execute("ROLLBACK")
                raise

    def _fail_at(self, stage):
        if self.fault:
            self.fault(stage)

    def _account(self):
        row = self._connection.execute("SELECT context,revision,ready,positions,complete,external_seen FROM risk_account").fetchone()
        return row

    @property
    def context_revision(self):
        with self._lock:
            return self._account()[1]

    def risk_ready(self):
        with self._tx():
            return bool(self._account()[2]) and not self._connection.execute("SELECT 1 FROM risk_inbox LIMIT 1").fetchone()

    def publish_context(self, context: RiskContext, *, expected_revision: int):
        # Evaluate structural validity without inventing or changing prop rules.
        from .features import derive_ruin_distances
        derive_ruin_distances(context)
        with self._tx():
            prior = self._account()
            if type(expected_revision) is not int or prior[1] != expected_revision:
                raise ReservationRecoveryRequired("Stale account revision")
            if prior[0]:
                old = _context(json.loads(prior[0])).account.status
                if old is not AccountStatus.ALIVE and context.account.status is not old:
                    raise ReservationRecoveryRequired("Terminal account state cannot roll back")
            self.append(E.RISK_CONTEXT, {"context": _encode_context(context), "revision": expected_revision + 1})

    def register_exposure(self, exposure: RiskExposure):
        if type(exposure) is not RiskExposure:
            self.append(E.RISK_GATED, {"reason": "unpriced exposure"})
            raise ValueError("Explicit priced exposure required")
        value = asdict(exposure)
        value.update(side=exposure.side.value, owner=exposure.owner.value,
                     per_contract_worst_case_loss=str(exposure.per_contract_worst_case_loss))
        with self._tx():
            old = self._connection.execute("SELECT data FROM risk_external WHERE exposure_id=?", (exposure.exposure_id,)).fetchone()
            if old:
                if json.loads(old[0]) != value:
                    raise ReservationRecoveryRequired("Exposure identity cannot be repriced or released locally")
                return
            self.append(E.RISK_EXTERNAL, value)

    def _rows(self):
        return [json.loads(row[0]) for row in self._connection.execute("SELECT data FROM risk_reservations ORDER BY intent_id")]

    def _external(self):
        return [json.loads(row[0]) for row in self._connection.execute("SELECT data FROM risk_external ORDER BY exposure_id")]

    @staticmethod
    def _remaining(row):
        return 0 if row["state"] in {"CANCELLED", "REJECTED", "ABANDONED"} else row["quantity"] - row["filled"]

    def _reservation(self, row):
        remaining = self._remaining(row)
        status = (S.UNCERTAIN if row["state"] in {"UNCERTAIN", "UNKNOWN"} else
                  S.RELEASED if row["state"] in {"CANCELLED", "REJECTED", "ABANDONED"} else
                  S.CONSUMED if row["filled"] == row["quantity"] else
                  S.PARTIALLY_CONSUMED if row["filled"] else S.RESERVED)
        owner = (O.UNCERTAIN if status is S.UNCERTAIN else O.WORKING
                 if row["state"] in {"ACCEPTED", "PARTIALLY_FILLED"} else O.PENDING
                 if row["order_id"] else O.RESERVATION)
        loss = Decimal(row["loss"])
        return RiskReservation(UUID(row["reservation_id"]), UUID(row["intent_id"]), self.account_id,
            row["instrument"], Side(row["side"]), row["quantity"], loss,
            decimal_from_fraction(remaining * Fraction(loss)), datetime.fromisoformat(row["created_at"]),
            status, remaining, row["filled"] - row["closed"], owner,
            UUID(row["order_id"]) if row["order_id"] else None)

    def reservations(self):
        with self._lock:
            return tuple(self._reservation(row) for row in self._rows())

    def exposure_by_owner(self):
        with self._tx():
            totals = {owner: Fraction(0) for owner in O}
            for row in self._rows():
                reservation = self._reservation(row)
                totals[reservation.owner] += Fraction(reservation.total_reserved_loss)
                totals[O.POSITION] += reservation.position_quantity * Fraction(reservation.per_contract_worst_case_loss)
            for row in self._external():
                totals[O(row["owner"])] += row["quantity"] * Fraction(Decimal(row["per_contract_worst_case_loss"]))
            account = self._account()[0]
            if account:
                # Frozen entry-loss estimates must also cover giving back marked
                # gains. Negative unrealized PnL never grants release/netting credit.
                totals[O.POSITION] += max(Fraction(0), Fraction(_context(json.loads(account)).account.unrealized_pnl))
            return {owner: decimal_from_fraction(value) for owner, value in totals.items()}

    def _used(self):
        return sum((Fraction(value) for value in self.exposure_by_owner().values()), Fraction(0))

    def reserve(self, intent, estimate):
        fingerprint = _intent(intent)
        with self._tx():
            used = self._used()
            old = self._connection.execute("SELECT data FROM risk_reservations WHERE intent_id=?", (str(intent.intent_id),)).fetchone()
            if old:
                row = json.loads(old[0])
                if row["intent"] != fingerprint:
                    raise ReservationRecoveryRequired("Conflicting stable intent_id")
                return ReservationResult(RiskDecision(RiskAction(row["action"]), row["quantity"], row["reason"]),
                                         self._reservation(row), Decimal(0), decimal_from_fraction(used), True)
            account = self._account()
            if not account[0] or not self.risk_ready():
                return self._reject("RESERVATION_RECOVERY_REQUIRED", used)
            if not intent.is_entry:
                return self._reject("ENTRY_RESERVATIONS_ONLY", used)
            evaluated = self.policy.evaluate(intent, context=_context(json.loads(account[0])), estimate=estimate)
            if evaluated.action is RiskAction.REJECT:
                return ReservationResult(evaluated.decision, None, Decimal(0), decimal_from_fraction(used))
            available = Fraction(evaluated.risk_budget) - used
            cushion = Fraction(evaluated.metrics.effective_ruin_distance) - used
            loss = Fraction(evaluated.per_contract_worst_case_loss)
            ratio = cushion / loss
            strict_cap = (ratio.numerator - 1) // ratio.denominator
            quantity = max(0, min(evaluated.approved_contracts, available // loss, strict_cap))
            if not quantity:
                return self._reject("AGGREGATE_RISK_BUDGET", used, available)
            action = RiskAction.ALLOW if quantity == intent.quantity else RiskAction.REDUCE
            reason = evaluated.decision.reason if quantity == evaluated.approved_contracts else "AGGREGATE_RISK_BUDGET"
            self._fail_at("approval_before_persistence")
            row = dict(fingerprint, reservation_id=str(uuid4()), intent=fingerprint,
                quantity=int(quantity), loss=str(evaluated.per_contract_worst_case_loss), filled=0, closed=0,
                order_id=None, state="RESERVED", action=action.value, reason=reason,
                created_at=datetime.now(timezone.utc).isoformat())
            self.append(E.RISK_RESERVED, row)
            self._fail_at("reservation_before_commit")
            result = ReservationResult(RiskDecision(action, int(quantity), reason), self._reservation(row),
                decimal_from_fraction(max(Fraction(0), available)), decimal_from_fraction(used + quantity * loss))
        self._fail_at("reservation_after_commit")
        return result

    @staticmethod
    def _reject(reason, used, available=Fraction(0)):
        return ReservationResult(RiskDecision(RiskAction.REJECT, None, reason), None,
                                 decimal_from_fraction(max(Fraction(0), available)), decimal_from_fraction(used))

    def abandon_undispatched(self, intent_id):
        with self._tx():
            row = self._find(intent_id=str(intent_id))
            if row["order_id"]:
                raise ReservationRecoveryRequired("Bound/submitted risk cannot be abandoned locally")
            if row["state"] != "ABANDONED":
                self.append(E.RISK_ABANDONED, {"intent_id": str(intent_id)})

    def _find(self, *, intent_id=None, order_id=None):
        for row in self._rows():
            if (intent_id is not None and row["intent_id"] == intent_id
                    or order_id is not None and row["order_id"] == order_id):
                return row
        raise ReservationRecoveryRequired("Missing durable risk ownership")

    def _store(self, row):
        if (row["state"] not in {"RESERVED", "CREATED", "SUBMITTING", "ACCEPTED", "PARTIALLY_FILLED",
                                 "FILLED", "CANCELLED", "REJECTED", "UNKNOWN", "UNCERTAIN", "ABANDONED"}
                or type(row["filled"]) is not int or type(row["closed"]) is not int
                or not 0 <= row["closed"] <= row["filled"] <= row["quantity"]):
            raise ReservationRecoveryRequired("Invalid risk ownership state")
        if row["state"] == "FILLED" and row["filled"] != row["quantity"]:
            raise ReservationRecoveryRequired("Incomplete filled risk ownership")
        self._reservation(row)  # enforce exact money, identity and unit bounds
        self._connection.execute("INSERT OR REPLACE INTO risk_reservations VALUES(?,?)", (row["intent_id"], _json(row)))

    def validate_risk_admission(self, intent, decision):
        if decision.action is RiskAction.REJECT:
            return
        with self._tx():
            row = self._find(intent_id=str(intent.intent_id))
            if row["intent"] != _intent(intent) or decision != RiskDecision(RiskAction(row["action"]), row["quantity"], row["reason"]):
                raise ReservationRecoveryRequired("OMS decision does not match its reservation")
            self._dispatch_guard(row)

    def _dispatch_guard(self, row):
        if not self.risk_ready() or row["state"] != "RESERVED" or row["order_id"]:
            raise ReservationRecoveryRequired("Risk recovery/admission gate")
        context = _context(json.loads(self._account()[0]))
        # The original liability stays reserved if cushion/terminal state changes.
        from .policy import ContractLossEstimate
        from prop_survival_engine.core.models import OrderIntent
        base_loss = decimal_from_fraction(Fraction(Decimal(row["loss"])) - Fraction(self.policy.config.execution_loss_buffer))
        evaluation = self.policy.evaluate(OrderIntent(row["instrument"], Side(row["side"]), 1),
            context=context, estimate=ContractLossEstimate(base_loss))
        if (evaluation.action is RiskAction.REJECT or self._used() > Fraction(evaluation.risk_budget)
                or self._used() >= Fraction(evaluation.metrics.effective_ruin_distance)):
            raise ReservationRecoveryRequired("Existing liabilities no longer fit account cushion")

    def append(self, event_type, payload):
        with self._lock:
            outer = not self._connection.in_transaction
            try:
                with self._tx():
                    if (event_type == E.RECONCILIATION and payload.get("result") == "success"
                            and not self.risk_finish_recovery()):
                        raise ReservationRecoveryRequired("Risk changed before READY commit")
                    if event_type == E.ORDER_CREATED:
                        row = self._find(intent_id=payload["intent_id"])
                        self._dispatch_guard(row)
                        if any(row[name] != payload[name] for name in ("instrument", "side", "quantity")) or not payload["is_entry"]:
                            raise ReservationRecoveryRequired("Unreserved or mismatched executable order")
                    record = super().append(event_type, payload)
                    self._project(event_type, payload)
                    self._fail_at("journal_" + event_type + "_before_commit")
                    return record
            except BaseException:
                if outer:
                    # A failed ACK/event commit can leave mutable OMS memory
                    # ahead of durable state. Gate all writers until replay.
                    with self._tx():
                        self._connection.execute("UPDATE risk_account SET ready=0")
                raise

    def _project(self, kind, p):
        db = self._connection
        if kind == E.RISK_GATED:
            db.execute("UPDATE risk_account SET ready=0")
        elif kind == E.RISK_CONTEXT:
            if p["revision"] != self._account()[1] + 1:
                raise ReservationRecoveryRequired("Invalid risk account history")
            context = _context(p["context"])
            old = self._account()[0]
            if old:
                old_status = _context(json.loads(old)).account.status
                if old_status is not AccountStatus.ALIVE and context.account.status is not old_status:
                    raise ReservationRecoveryRequired("Terminal account history regression")
            db.execute("UPDATE risk_account SET context=?,revision=?", (_json(p["context"]), p["revision"]))
        elif kind == E.RISK_RESERVED:
            if db.execute("SELECT 1 FROM risk_reservations WHERE intent_id=?", (p["intent_id"],)).fetchone():
                raise ReservationRecoveryRequired("Duplicate reservation history")
            intent = p["intent"]
            if (p["filled"] != 0 or p["closed"] != 0 or p["order_id"] is not None
                    or p["state"] != "RESERVED" or not intent["is_entry"]
                    or type(intent["quantity"]) is not int or not 0 < p["quantity"] <= intent["quantity"]
                    or p["action"] not in {RiskAction.ALLOW.value, RiskAction.REDUCE.value}
                    or any(p[key] != intent[key] for key in ("intent_id", "instrument", "side"))):
                raise ReservationRecoveryRequired("Invalid initial reservation history")
            self._store(p)
        elif kind == E.RISK_EXTERNAL:
            RiskExposure(p["exposure_id"], p["instrument"], Side(p["side"]), p["quantity"],
                         Decimal(p["per_contract_worst_case_loss"]), O(p["owner"]))
            db.execute("INSERT INTO risk_external VALUES(?,?)", (p["exposure_id"], _json(p)))
            if p["owner"] == O.UNCERTAIN.value:
                db.execute("UPDATE risk_account SET ready=0")
        elif kind == E.ORDER_CREATED:
            row = self._find(intent_id=p["intent_id"])
            if row["order_id"] or any(row[n] != p[n] for n in ("quantity", "instrument", "side")):
                raise ReservationRecoveryRequired("Invalid order risk binding")
            row.update(order_id=p["order_id"], state="CREATED")
            self._store(row)
        elif kind in {E.SUBMISSION_STATE, E.PLATFORM_ORDER, E.EXECUTION_EVENT_DECISION}:
            oid = p.get("order_id", p.get("our_order_id"))
            if p.get("external"):
                db.execute("UPDATE risk_account SET external_seen=1,ready=0")
            elif oid:
                row = self._find(order_id=oid)
                filled = p.get("filled_quantity", row["filled"])
                state = p.get("state", p.get("status", row["state"]))
                if (type(filled) is not int or not row["filled"] <= filled <= row["quantity"]
                        or row["state"] in {"CANCELLED", "REJECTED", "FILLED"} and state != row["state"]
                        or state == "FILLED" and filled != row["quantity"]):
                    raise ReservationRecoveryRequired("Risk lifecycle/volume regression")
                row.update(filled=filled, state=state)
                self._store(row)
                if state in {"UNCERTAIN", "UNKNOWN"}:
                    db.execute("UPDATE risk_account SET ready=0")
            if kind == E.EXECUTION_EVENT_DECISION:
                db.execute("DELETE FROM risk_inbox WHERE event_id=?", (p["event_id"],))
                if p["outcome"] in {"gate", "deferred"}:
                    db.execute("UPDATE risk_account SET ready=0")
        elif kind == E.EXECUTION_EVENT_RECEIVED:
            db.execute("INSERT OR IGNORE INTO risk_inbox VALUES(?)", (p["event_id"],))
        elif kind == E.PLATFORM_POSITIONS:
            if (type(p.get("complete")) is not bool or type(p["positions"]) is not dict
                    or any(type(instrument) is not str or not instrument or type(quantity) is not int
                           for instrument, quantity in p["positions"].items())):
                raise ReservationRecoveryRequired("Invalid complete position evidence")
            db.execute("UPDATE risk_account SET positions=?,complete=?", (_json(p["positions"]), bool(p.get("complete"))))
        elif kind == E.RECONCILIATION:
            db.execute("UPDATE risk_account SET ready=?", (p["result"] == "success",))
        elif kind == E.RISK_ABANDONED:
            row = self._find(intent_id=p["intent_id"])
            if row["order_id"]:
                raise ReservationRecoveryRequired("Submitted reservation cannot be released locally")
            row["state"] = "ABANDONED"
            self._store(row)
        elif kind == E.RISK_POSITIONS_RECONCILED:
            for iid, closed in p["closed"].items():
                row = self._find(intent_id=iid)
                if not row["closed"] <= closed <= row["filled"]:
                    raise ReservationRecoveryRequired("Position disposition regression")
                row["closed"] = closed
                self._store(row)
            for key, quantity in p["external"].items():
                row = json.loads(db.execute("SELECT data FROM risk_external WHERE exposure_id=?", (key,)).fetchone()[0])
                if not 0 <= quantity <= row["quantity"]:
                    raise ReservationRecoveryRequired("External position disposition regression")
                row["quantity"] = quantity
                db.execute("UPDATE risk_external SET data=? WHERE exposure_id=?", (_json(row), key))

    def risk_begin_recovery(self):
        self._snapshot_cut = None
        # Commit the gate first: corrupt replay must not restore an old READY bit.
        with self._tx():
            self._connection.execute("UPDATE risk_account SET ready=0")
        with self._tx():
            db = self._connection
            db.execute("DELETE FROM risk_reservations")
            db.execute("DELETE FROM risk_external")
            db.execute("DELETE FROM risk_inbox")
            db.execute("UPDATE risk_account SET context=NULL,revision=0,ready=0,positions=NULL,complete=0,external_seen=0")
            records = super().replay()
            from prop_survival_engine.oms.replay_validation import validate_replay
            validate_replay(records)
            for record in records:
                self._project(record.event_type, record.payload)
            db.execute("UPDATE risk_account SET ready=0,complete=0,external_seen=0")

    def risk_snapshot_started(self):
        with self._lock:
            self._snapshot_cut = self._connection.execute("SELECT COALESCE(MAX(sequence),0) FROM journal").fetchone()[0]

    def risk_finish_recovery(self):
        """Conservative gross lots; only complete provider positions can close risk.

        Opposing lots are retained without netting credit. Ambiguous inventory
        dispositions, unpriced/orphan positions and unknown orders remain gated.
        """
        with self._tx():
            account = self._account()
            rows, external = self._rows(), self._external()
            if (self._snapshot_cut is None
                    or self._connection.execute("SELECT 1 FROM journal WHERE event_type=? AND sequence>? LIMIT 1",
                                                (E.EXECUTION_EVENT_RECEIVED, self._snapshot_cut)).fetchone()
                    or self._connection.execute("SELECT 1 FROM risk_inbox LIMIT 1").fetchone()
                    or not account[0] or not account[4] or account[5]
                    or any(r["state"] in {"UNKNOWN", "UNCERTAIN", "SUBMITTING"} for r in rows)
                    or any(r["owner"] == O.UNCERTAIN.value and r["quantity"] for r in external)):
                return False
            positions = json.loads(account[3])
            lots = {}
            for row in rows:
                if row["filled"] > row["closed"]:
                    lots.setdefault(row["instrument"], []).append(("reservation", row["intent_id"], row["side"], row["filled"] - row["closed"]))
            for row in external:
                if row["owner"] == O.POSITION.value and row["quantity"]:
                    lots.setdefault(row["instrument"], []).append(("external", row["exposure_id"], row["side"], row["quantity"]))
            closed, ext = {}, {}
            for instrument in set(lots) | set(positions):
                def loss_of(lot):
                    if lot[0] == "reservation":
                        return Fraction(Decimal(self._find(intent_id=lot[1])["loss"]))
                    return Fraction(Decimal(next(r["per_contract_worst_case_loss"] for r in external if r["exposure_id"] == lot[1])))
                # Snapshot net size cannot identify which lot was closed. Retain
                # the most expensive possible remaining inventory, never infer
                # that the high-risk lot disappeared from quantity alone.
                group = sorted(lots.get(instrument, []), key=lambda lot: (loss_of(lot), lot[0], lot[1]))
                remote = positions.get(instrument, 0)
                known = sum(q if side == "BUY" else -q for _, _, side, q in group)
                if remote == known:
                    continue
                sides = {side for _, _, side, _ in group}
                if (len(sides) != 1 or remote * known < 0 or abs(remote) > abs(known)):
                    return False
                remove = abs(known) - abs(remote)
                for kind, key, side, q in group:
                    removed = min(remove, q)
                    remove -= removed
                    if kind == "reservation":
                        closed[key] = self._find(intent_id=key)["closed"] + removed
                    else:
                        ext[key] = q - removed
            if closed or ext:
                self.append(E.RISK_POSITIONS_RECONCILED, {"closed": closed, "external": ext})
            return not self._connection.execute("SELECT 1 FROM risk_inbox LIMIT 1").fetchone()


class ReservationRiskEvaluator:
    """Stable evaluate(intent)->RiskDecision contract, with durable side effects."""
    def __init__(self, ledger: SQLiteRiskLedger, estimate_provider):
        self.ledger, self.estimate_provider = ledger, estimate_provider

    def evaluate_details(self, intent):
        return self.ledger.reserve(intent, self.estimate_provider(intent))

    def evaluate(self, intent):
        return self.evaluate_details(intent).decision
