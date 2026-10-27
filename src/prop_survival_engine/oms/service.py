from uuid import UUID, uuid4
from datetime import datetime
from decimal import Decimal

from prop_survival_engine.core import events
from prop_survival_engine.core.models import Order, OrderIntent
from prop_survival_engine.execution.interfaces import (
    ExecutionAdapter, PlatformExecution, PlatformOrderStatus, SubmissionStatus,
)
from prop_survival_engine.oms.models import (
    DuplicateIntentConflict, ManagedOrder, OmsState, OrderState,
)
from prop_survival_engine.persistence.interfaces import OmsJournal
from prop_survival_engine.risk.service import RiskAction, RiskDecision
from prop_survival_engine.execution.events import event_from_payload, execution_payload
from prop_survival_engine.oms.event_ingestion import EventIngestion, serialized, inbox_serialized
from prop_survival_engine.oms.replay_validation import TERMINAL, validate_replay


class RecoveryRequired(RuntimeError):
    pass


class OmsService(EventIngestion):
    def __init__(self, journal: OmsJournal, execution: ExecutionAdapter) -> None:
        self.journal = journal
        self.execution = execution
        self.state = OmsState.STARTING
        self.orders: dict[UUID, ManagedOrder] = {}
        self.by_intent: dict[UUID, ManagedOrder | None] = {}
        self.decisions: dict[UUID, RiskDecision] = {}
        self.platform_positions: dict[str, int] = {}
        self.platform_executions: dict[str, PlatformExecution] = {}
        self._init_event_ingestion()
        bind_events = getattr(execution, "set_event_sink", None)
        if bind_events is not None:
            bind_events(self.accept_execution_event)

    @serialized
    def cancel(self, order_id: UUID) -> None:
        """Authorize cancellation durably; only platform facts release liability."""
        if self.state is not OmsState.READY:
            raise RecoveryRequired("cancel requires reconciled ownership/inventory")
        managed = self.orders[order_id]
        if managed.state in {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED}:
            return
        if managed.state not in {OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED}:
            raise RecoveryRequired("uncertain order requires reconciliation before cancellation")
        self.journal.append(events.EXECUTION_CANCEL_REQUESTED, {
            "order_id": str(order_id), "client_order_id": managed.order.client_order_id,
            "command_id": str(uuid4()),
        })
        try:
            self.execution.cancel(managed.order.client_order_id)
        except Exception:
            self.state = OmsState.RECOVERING
            raise RecoveryRequired("cancel outcome unresolved; risk remains exposed") from None
        self.drain_execution_events()

    @serialized
    def recover(self) -> None:
        ownership = getattr(self.journal, "ensure_ownership", None)
        if ownership:
            ownership()
        risk_ready = getattr(self.journal, "risk_ready", lambda: True)
        if self.state is OmsState.READY and risk_ready():
            self.drain_execution_events()
            if self.state is OmsState.READY and not self._event_gate and not self._receipt_failed:
                return
        self.state = OmsState.RECOVERING
        begin_risk_recovery = getattr(self.journal, "risk_begin_recovery", None)
        if begin_risk_recovery:
            begin_risk_recovery()
        self._replay_journal()
        self.drain_execution_events()
        snapshot_started = getattr(self.journal, "risk_snapshot_started", None)
        if snapshot_started:
            snapshot_started()
        platform = self.execution.snapshot()
        inventory = getattr(self.journal, "prepare_inventory", None)
        if inventory:
            platform, accepted = inventory(platform, self.orders)
            if not accepted:
                self.journal.append(events.RECONCILIATION, {"result": "gated"})
                self.state = OmsState.RECOVERING
                return
        positions = dict(platform.positions or {})
        position_payload = {"positions": positions}
        if getattr(self.journal, "risk_managed", False):
            position_payload["complete"] = platform.positions is not None
        self.journal.append(events.PLATFORM_POSITIONS, position_payload)
        self.platform_positions = positions
        for execution in platform.executions:
            key = _execution_key(execution)
            existing = self.platform_executions.get(key)
            if existing is not None and execution_payload(existing) != execution_payload(execution):
                raise RecoveryRequired("conflicting execution ID requires correction reconciliation")
            if key not in self.platform_executions:
                self.journal.append(events.PLATFORM_EXECUTION, _serialize_execution(execution))
                self.platform_executions[key] = execution
        platform_by_client_id = {o.client_order_id: o for o in platform.orders if o.client_order_id}
        platform_by_provider_id = {
            o.provider_order_id: o for o in platform.orders if o.provider_order_id
        }
        platform_by_our_order_id = {o.our_order_id: o for o in platform.orders if o.our_order_id}
        provider_by_our_order_id = {
            execution.our_order_id: execution.provider_order_id
            for execution in self.platform_executions.values()
            if execution.our_order_id
        }
        # Active/open orders, positions, and recent executions are all retained.
        # No single open-order snapshot's absence is interpreted as rejection.
        for managed in self.orders.values():
            managed.provider_order_id = managed.provider_order_id or provider_by_our_order_id.get(
                str(managed.order.order_id)
            )
            remote = platform_by_client_id.get(managed.order.client_order_id)
            if remote is None:
                remote = platform_by_our_order_id.get(str(managed.order.order_id))
            if remote is None and managed.provider_order_id:
                remote = platform_by_provider_id.get(managed.provider_order_id)
            if remote is None:
                filled = max(managed.filled_quantity,
                             _filled_from_executions(self.platform_executions.values(), managed.provider_order_id))
                if filled > managed.order.quantity or (managed.state in TERMINAL and filled != managed.filled_quantity):
                    raise RecoveryRequired("execution evidence contradicts terminal volume; reconcile correction")
                state = managed.state if managed.state in TERMINAL else (
                    OrderState.FILLED if filled == managed.order.quantity else OrderState.UNKNOWN)
                self._commit_platform_observation(managed, state, filled, managed.provider_order_id,
                                                  managed.child_provider_order_ids, None)
            else:
                filled = max(
                    remote.filled_quantity,
                    _filled_from_executions(self.platform_executions.values(), managed.provider_order_id),
                )
                prior = self._order_event_cursors.get(managed.provider_order_id)
                if filled < managed.filled_quantity or filled > managed.order.quantity:
                    raise RecoveryRequired("platform snapshot regresses known fills or exceeds order size; correction reconciliation required")
                if self._stream_online is not None:
                    if (prior is not None and remote.provider_timestamp is not None
                            and remote.provider_timestamp < prior[0]
                            and (_map_platform_state(remote.status) is not managed.state or filled != managed.filled_quantity)):
                        raise RecoveryRequired("platform snapshot predates known order events; reconcile again")
                state = _map_platform_state(remote.status)
                if state is OrderState.ACCEPTED and 0 < filled < managed.order.quantity:
                    state = OrderState.PARTIALLY_FILLED
                elif filled == managed.order.quantity:
                    state = OrderState.FILLED
                if managed.state in TERMINAL and (state is not managed.state or filled != managed.filled_quantity):
                    raise RecoveryRequired("platform snapshot contradicts terminal order; reconcile correction")
                self._commit_platform_observation(managed, state, filled,
                    remote.provider_order_id or managed.provider_order_id,
                    remote.child_provider_order_ids, remote.provider_timestamp)
        # Platform-only orders are recorded as platform observations; the OMS
        # has no original intent to synthesize an executable local order from.
        unknown_snapshot_order = False
        for remote in platform.orders:
            if not any(
                (remote.client_order_id and remote.client_order_id == o.order.client_order_id)
                or remote.our_order_id == str(o.order.order_id)
                or (remote.provider_order_id and remote.provider_order_id == o.provider_order_id)
                for o in self.orders.values()
            ):
                unknown_snapshot_order = True
                self.journal.append(events.PLATFORM_ORDER, {
                    "client_order_id": remote.client_order_id,
                    "our_order_id": remote.our_order_id,
                    "provider_order_id": remote.provider_order_id,
                    "child_provider_order_ids": list(remote.child_provider_order_ids),
                    "status": remote.status.value,
                    "filled_quantity": remote.filled_quantity,
                    "external": True,
                })
        # Fresh platform state resolves prior ambiguity; events accepted during
        # snapshot are applied afterwards and may independently gate recovery.
        self._event_gate = False
        self._receipt_failed = False
        self.drain_execution_events()
        with self._inbox_lock:
            unresolved_stream_orders = self._stream_online is not None and any(
                row.state in {OrderState.SUBMITTING, OrderState.UNCERTAIN, OrderState.UNKNOWN}
                for row in self.orders.values()
            )
            blocked = (self._event_gate or self._receipt_failed or self._pending_events
                       or self._stream_online is False or unresolved_stream_orders
                       or (self._stream_online is not None and (unknown_snapshot_order or self._orphan_position())))
            finish_risk_recovery = getattr(self.journal, "risk_finish_recovery", None)
            if finish_risk_recovery and not finish_risk_recovery():
                blocked = True
            self.journal.append(events.RECONCILIATION, {"result": "gated" if blocked else "success"})
            self.state = OmsState.RECOVERING if blocked else OmsState.READY

    def _commit_platform_observation(self, managed, state, filled, provider_id, children, timestamp):
        """Persist reconciliation outcomes before exposing them to memory."""
        self.journal.append(events.PLATFORM_ORDER, {
            "client_order_id": managed.order.client_order_id,
            "our_order_id": str(managed.order.order_id), "provider_order_id": provider_id,
            "child_provider_order_ids": list(children), "status": state.value,
            "filled_quantity": filled,
            "provider_timestamp": timestamp.isoformat() if timestamp else None,
        })
        managed.state, managed.filled_quantity = state, filled
        managed.provider_order_id, managed.child_provider_order_ids = provider_id, children
        if timestamp is not None and provider_id:
            prior = self._order_event_cursors.get(provider_id)
            if prior is None or timestamp >= prior[0]:
                self._order_event_cursors[provider_id] = (timestamp, None)

    def _orphan_position(self):
        """Conservative exposure provenance, not a position accounting engine.

        For the streamed PoC, nonzero exposure needs enough local same-direction
        fill evidence. A pre-existing account baseline is not implemented. Flat
        snapshots need not equal historical fills (closed positions can be flat).
        """
        accounted = getattr(self.journal, "inventory_positions_accounted", None)
        if accounted:
            return not accounted(self.platform_positions)
        for instrument, position in self.platform_positions.items():
            evidence = sum(row.filled_quantity for row in self.orders.values()
                           if row.order.instrument == instrument
                           and (row.order.side.value == "BUY") == (position > 0))
            if position and abs(position) > evidence:
                return True
        return False

    @serialized
    def process(self, intent: OrderIntent, decision: RiskDecision) -> ManagedOrder | None:
        self.drain_execution_events()
        managed, dispatch = self._admit_order(intent, decision)
        if not dispatch:
            return managed
        order = managed.order
        # No inbox lock is held while waiting on the provider: a callback may
        # commit a receipt before ACK. Admission was serialized against receipt
        # acceptance through the durable SUBMITTING record.
        try:
            result = self.execution.submit(order)
        except Exception:
            managed.state = OrderState.UNCERTAIN
            self.journal.append(events.SUBMISSION_STATE, {
                "order_id": str(order.order_id), "state": managed.state.value,
                # Provider exceptions may contain credentials, URLs or response bodies.
                "detail": "EXECUTION_SUBMISSION_EXCEPTION",
            })
            self.drain_execution_events()
            return managed
        managed.state = {
            SubmissionStatus.ACCEPTED: OrderState.ACCEPTED,
            SubmissionStatus.REJECTED: OrderState.REJECTED,
            SubmissionStatus.UNCERTAIN: OrderState.UNCERTAIN,
        }[result.status]
        managed.provider_order_id = result.provider_order_id
        managed.child_provider_order_ids = result.child_provider_order_ids
        self.journal.append(events.SUBMISSION_STATE, {
            "order_id": str(order.order_id), "state": managed.state.value,
            # Returned details are untrusted too: adapters may copy exception text.
            "detail": {
                SubmissionStatus.ACCEPTED: "EXECUTION_ACCEPTED",
                SubmissionStatus.REJECTED: "EXECUTION_REJECTED",
                SubmissionStatus.UNCERTAIN: "EXECUTION_UNCERTAIN",
            }[result.status],
            "provider_order_id": managed.provider_order_id,
            "child_provider_order_ids": list(managed.child_provider_order_ids),
        })
        self.drain_execution_events()
        return managed

    @inbox_serialized
    def _admit_order(self, intent: OrderIntent, decision: RiskDecision):
        if self._pending_events or self._receipt_failed:
            raise RecoveryRequired("pending execution receipts must be drained before admission")
        if self.state is not OmsState.READY:
            if intent.is_entry:
                raise RecoveryRequired("OMS is not READY; entry orders are blocked")
            raise RecoveryRequired("OMS is not READY")
        existing = self.by_intent.get(intent.intent_id, _NOT_FOUND)
        if existing is not _NOT_FOUND:
            prior_decision = self.decisions.get(intent.intent_id)
            if prior_decision and prior_decision != decision:
                raise DuplicateIntentConflict("intent_id was already processed with a different decision")
            return existing, False
        if intent.is_entry and any(
            managed.state in {OrderState.SUBMITTING, OrderState.UNCERTAIN, OrderState.UNKNOWN}
            for managed in self.orders.values()
        ):
            raise RecoveryRequired("an order has unresolved execution state; reconcile before new entries")
        prior_decision = self.decisions.get(intent.intent_id)
        if prior_decision is not None and prior_decision != decision:
            raise DuplicateIntentConflict("persisted intent decision differs")
        risk_admission = getattr(self.journal, "validate_risk_admission", None)
        if risk_admission:
            risk_admission(intent, decision)
        if decision.action is RiskAction.REJECT:
            self.journal.append(events.INTENT_DECIDED, {
                "intent_id": str(intent.intent_id), "action": decision.action.value,
                "reason": decision.reason,
            })
            self.decisions[intent.intent_id] = decision
            self.by_intent[intent.intent_id] = None
            return None, False
        quantity = decision.quantity if decision.quantity is not None else intent.quantity
        if quantity <= 0 or quantity > intent.quantity:
            raise ValueError("risk decision quantity must be between 1 and intent quantity")
        if prior_decision is None:
            self.journal.append(events.INTENT_DECIDED, {
                "intent_id": str(intent.intent_id), "action": decision.action.value,
                "quantity": quantity, "reason": decision.reason,
            })
        self.decisions[intent.intent_id] = decision
        order = Order(
            order_id=uuid4(), client_order_id=str(uuid4()), intent_id=intent.intent_id,
            instrument=intent.instrument, side=intent.side, quantity=quantity,
            is_entry=intent.is_entry,
        )
        # Durable unique intent-to-order row is committed before side effects.
        self.journal.append(events.ORDER_CREATED, _serialize_order(order))
        managed = ManagedOrder(order, OrderState.CREATED)
        self.orders[order.order_id] = managed
        self.by_intent[intent.intent_id] = managed
        managed.state = OrderState.SUBMITTING
        self.journal.append(events.SUBMISSION_STATE, {
            "order_id": str(order.order_id), "intent_id": str(intent.intent_id),
            "state": managed.state.value,
        })
        return managed, True

    @inbox_serialized
    def _replay_journal(self) -> None:
        try:
            records = self.journal.replay()
            validate_replay(records)
        except Exception:
            self.state = OmsState.RECOVERING
            self._receipt_failed = True
            raise RecoveryRequired("invalid or incomplete OMS journal; recovery remains gated") from None
        self.orders.clear()
        self.by_intent.clear()
        self.decisions.clear()
        self.platform_positions.clear()
        self.platform_executions.clear()
        self._pending_events.clear()
        self._received_events.clear()
        self._deferred_events.clear()
        self._order_event_cursors.clear()
        self._stream_online = None
        self._event_gate = False
        for record in records:
            payload = record.payload
            if record.event_type == events.INTENT_DECIDED:
                intent_id = UUID(payload["intent_id"])
                self.decisions[intent_id] = RiskDecision(
                    RiskAction(payload["action"]), payload.get("quantity"), payload.get("reason", "")
                )
                if self.decisions[intent_id].action is RiskAction.REJECT:
                    self.by_intent[intent_id] = None
            elif record.event_type == events.ORDER_CREATED:
                order = _deserialize_order(payload)
                managed = ManagedOrder(order, OrderState.CREATED)
                self.orders[order.order_id] = managed
                self.by_intent[order.intent_id] = managed
            elif record.event_type == events.SUBMISSION_STATE:
                managed = self.orders.get(UUID(payload["order_id"]))
                if managed:
                    managed.state = OrderState(payload["state"])
                    managed.provider_order_id = payload.get("provider_order_id") or managed.provider_order_id
                    managed.child_provider_order_ids = tuple(
                        payload.get("child_provider_order_ids", managed.child_provider_order_ids)
                    )
            elif record.event_type == events.PLATFORM_ORDER:
                if payload.get("external"):
                    continue
                our_order_id = payload.get("our_order_id")
                managed = self.orders.get(UUID(our_order_id)) if our_order_id else None
                if managed is None and payload.get("client_order_id"):
                    managed = next((
                        row for row in self.orders.values()
                        if row.order.client_order_id == payload["client_order_id"]
                    ), None)
                if managed is not None:
                    managed.provider_order_id = payload.get("provider_order_id") or managed.provider_order_id
                    managed.child_provider_order_ids = tuple(
                        payload.get("child_provider_order_ids", managed.child_provider_order_ids)
                    )
                    managed.state = OrderState(payload["status"])
                    managed.filled_quantity = payload["filled_quantity"]
                    if payload.get("provider_timestamp") and managed.provider_order_id:
                        timestamp = datetime.fromisoformat(payload["provider_timestamp"])
                        prior = self._order_event_cursors.get(managed.provider_order_id)
                        if prior is None or timestamp >= prior[0]:
                            self._order_event_cursors[managed.provider_order_id] = (timestamp, None)
            elif record.event_type == events.PLATFORM_EXECUTION:
                execution = _deserialize_execution(payload)
                self.platform_executions[_execution_key(execution)] = execution
            elif record.event_type == events.PLATFORM_POSITIONS:
                self.platform_positions = {str(k): int(v) for k, v in payload["positions"].items()}
            elif record.event_type == events.EXECUTION_EVENT_RECEIVED:
                event = event_from_payload(payload["event"])
                self._received_events[payload["event_id"]] = record.sequence
                self._pending_events[payload["event_id"]] = event
            elif record.event_type == events.EXECUTION_EVENT_DECISION:
                self._apply_event_decision(payload)
            elif record.event_type == events.RECONCILIATION:
                self._event_gate = payload["result"] != "success"


_NOT_FOUND = object()


def _serialize_order(order: Order) -> dict:
    return {
        "order_id": str(order.order_id), "client_order_id": order.client_order_id,
        "intent_id": str(order.intent_id), "instrument": order.instrument,
        "side": order.side.value, "quantity": order.quantity, "is_entry": order.is_entry,
    }


def _deserialize_order(data: dict) -> Order:
    from prop_survival_engine.core.models import Side
    return Order(
        UUID(data["order_id"]), data["client_order_id"], UUID(data["intent_id"]),
        data["instrument"], Side(data["side"]), int(data["quantity"]), bool(data["is_entry"]),
    )


def _map_platform_state(status: PlatformOrderStatus) -> OrderState:
    return {
        PlatformOrderStatus.PENDING: OrderState.SUBMITTING,
        PlatformOrderStatus.UNKNOWN: OrderState.UNKNOWN,
        PlatformOrderStatus.ACCEPTED: OrderState.ACCEPTED,
        PlatformOrderStatus.PARTIALLY_FILLED: OrderState.PARTIALLY_FILLED,
        PlatformOrderStatus.FILLED: OrderState.FILLED,
        PlatformOrderStatus.CANCELLED: OrderState.CANCELLED,
        PlatformOrderStatus.REJECTED: OrderState.REJECTED,
    }[status]


def _execution_key(execution: PlatformExecution) -> str:
    return f"{execution.provider_order_id}:{execution.execution_id}"


def _filled_from_executions(executions, provider_order_id: str | None) -> int:
    if provider_order_id is None:
        return 0
    seen: set[str] = set()
    total = 0
    for execution in executions:
        if execution.provider_order_id != provider_order_id or execution.execution_id in seen:
            continue
        seen.add(execution.execution_id)
        total += execution.quantity
    return total


def _serialize_execution(execution: PlatformExecution) -> dict:
    return {
        "execution_id": execution.execution_id,
        "provider_order_id": execution.provider_order_id,
        "instrument": execution.instrument,
        "side": execution.side,
        "quantity": execution.quantity,
        "price": str(execution.price),
        "timestamp": execution.timestamp.isoformat(),
        "our_order_id": execution.our_order_id,
    }


def _deserialize_execution(data: dict) -> PlatformExecution:
    return PlatformExecution(
        execution_id=data["execution_id"],
        provider_order_id=data["provider_order_id"],
        instrument=data["instrument"],
        side=data["side"],
        quantity=int(data["quantity"]),
        price=Decimal(data["price"]),
        timestamp=datetime.fromisoformat(data["timestamp"]),
        our_order_id=data.get("our_order_id"),
    )
