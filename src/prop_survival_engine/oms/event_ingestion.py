"""Durable inbox + conservative OMS event reducer; no provider calls here."""

from functools import wraps
from threading import RLock
from time import monotonic_ns
from uuid import UUID

from prop_survival_engine.core import events
from prop_survival_engine.execution.events import (
    ExecutionEvent, ExecutionEventKind as Kind, event_payload, event_from_payload,
    execution_payload, execution_from_payload,
)
from prop_survival_engine.oms.models import OmsState, OrderState


def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._oms_lock:
            guard = getattr(self.journal, "validate_ownership", None)
            if guard and method.__name__ != "recover":
                guard()
            return method(self, *args, **kwargs)
    return call


def inbox_serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._inbox_lock:
            guard = getattr(self.journal, "validate_ownership", None)
            if guard:
                guard()
            return method(self, *args, **kwargs)
    return call


class EventIngestion:
    def _init_event_ingestion(self):
        self._oms_lock = RLock()
        self._inbox_lock = RLock()
        self._pending_events: dict[str, ExecutionEvent] = {}
        self._received_events: dict[str, int] = {}
        self._deferred_events: set[str] = set()
        self._order_event_cursors: dict[str, tuple] = {}
        self._event_gate = False
        self._receipt_failed = False
        self._stream_online: bool | None = None

    def accept_execution_event(self, event: ExecutionEvent) -> int:
        """Durable acceptance only. Never wait for OMS or invoke the adapter.

        A journal commit is the acceptance boundary. Process/drain/recover apply
        lifecycle decisions on the serialized OMS caller, not the provider loop.
        """
        if not isinstance(event, ExecutionEvent):
            raise TypeError("OMS accepts provider-neutral execution events only")
        guard = getattr(self.journal, "validate_ownership", None)
        if guard:
            guard()
        identity = event.fingerprint
        with self._inbox_lock:
            if identity in self._received_events:
                return self._received_events[identity]
            try:
                receipt = self.journal.append(events.EXECUTION_EVENT_RECEIVED, {
                    "event_id": identity, "event": event_payload(event),
                    "receipt_monotonic_ns": monotonic_ns(),
                })
            except Exception:
                self._receipt_failed = True
                self.state = OmsState.RECOVERING
                raise
            self._received_events[identity] = receipt.sequence
            self._pending_events[identity] = event
            if event.kind in {Kind.STREAM_CONNECTED, Kind.STREAM_DISCONNECTED, Kind.STREAM_STOPPED, Kind.UNKNOWN}:
                self._event_gate = True
                self.state = OmsState.RECOVERING
            return receipt.sequence

    @serialized
    def drain_execution_events(self) -> list[str]:
        """Apply all currently accepted events, journaling each decision first."""
        with self._inbox_lock:
            pending = tuple(self._pending_events.items())
        return [self._reduce_event(identity, event) for identity, event in pending]

    def _reduce_event(self, identity, event):
        if event.kind in {Kind.STREAM_CONNECTED, Kind.STREAM_DISCONNECTED, Kind.STREAM_STOPPED}:
            return self._event_decision(identity, "gate", stream_online=event.kind is Kind.STREAM_CONNECTED,
                                        reason="stream lifecycle requires reconciliation")
        candidates = [row for row in self.orders.values()
                      if row.provider_order_id is not None and row.provider_order_id == event.provider_order_id]
        if len(candidates) != 1:
            return self._event_decision(identity, "deferred", reason="unknown provider order mapping")
        managed = candidates[0]
        def gate(reason):
            terminal = managed.state in {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED}
            return self._event_decision(identity, "gate", managed=managed,
                                        state=managed.state if terminal else OrderState.UNKNOWN, reason=reason)
        if event.kind is Kind.UNKNOWN:
            return gate("unsupported or malformed provider event")
        if event.provider_timestamp is None:
            return gate("missing provider timestamp")
        if event.order_quantity is not None and event.order_quantity != managed.order.quantity:
            return gate("order size change needs OMS amendment/reconciliation")
        if event.instrument is not None and event.instrument != managed.order.instrument:
            return gate("instrument mismatch")
        if event.side is not None and event.side != managed.order.side.value:
            return gate("side mismatch")
        if event.child_provider_order_ids:
            return gate("bracket child lifecycle is not implemented")
        filled = managed.filled_quantity
        execution = event.execution
        if event.kind is Kind.EXECUTION:
            if execution is None or execution.provider_order_id != managed.provider_order_id:
                return gate("invalid execution identity")
            if (execution.instrument != managed.order.instrument or execution.side != managed.order.side.value
                    or type(execution.quantity) is not int or execution.quantity <= 0
                    or not execution.price.is_finite() or execution.timestamp.utcoffset() is None):
                return gate("invalid execution evidence")
            key = f"{execution.provider_order_id}:{execution.execution_id}"
            existing = self.platform_executions.get(key)
            if existing is not None:
                if execution_payload(existing) != execution_payload(execution):
                    return gate("conflicting execution ID")
                return self._event_decision(identity, "duplicate")
            total = sum(item.quantity for item in self.platform_executions.values()
                        if item.provider_order_id == managed.provider_order_id) + execution.quantity
            if managed.state in {OrderState.CANCELLED, OrderState.REJECTED, OrderState.FILLED}:
                if total > filled or managed.state is OrderState.REJECTED:
                    return gate("late execution changes terminal volume; reconcile")
                # Order channel may confirm cumulative fills before their trade
                # channel records arrive. Journal those IDs without adding twice.
                return self._event_decision(identity, "applied", managed=managed,
                                            execution=execution)
            filled = max(filled, total)  # cumulative updates and deltas are never added together
            if filled > managed.order.quantity:
                return gate("execution volume exceeds OMS order size")
            state = OrderState.FILLED if filled == managed.order.quantity else OrderState.PARTIALLY_FILLED
            return self._event_decision(identity, "applied", managed=managed, state=state,
                                        filled=filled, execution=execution)
        cursor = self._order_event_cursors.get(managed.provider_order_id)
        if cursor is not None:
            timestamp, prior_identity = cursor
            if event.provider_timestamp < timestamp:
                return self._event_decision(identity, "stale")
            if event.provider_timestamp == timestamp and identity != prior_identity:
                predicted = _event_state(event.kind, event.filled_quantity, managed.order.quantity)
                if (prior_identity is None and event.filled_quantity == managed.filled_quantity
                        and predicted is managed.state):
                    return self._event_decision(identity, "stale")
                return gate("equal timestamp conflict")
        if event.filled_quantity is None:
            return gate("missing cumulative fill quantity")
        if event.filled_quantity < filled:
            return gate("newer event decreases filled quantity")
        filled = event.filled_quantity
        if filled > managed.order.quantity:
            return gate("cumulative fill exceeds OMS order size")
        state = _event_state(event.kind, filled, managed.order.quantity)
        if state is None:
            return gate("unsupported lifecycle event")
        if state is OrderState.FILLED and filled != managed.order.quantity:
            return gate("full fill without complete volume")
        if state is OrderState.PARTIALLY_FILLED and not 0 < filled < managed.order.quantity:
            return gate("invalid partial fill volume")
        if managed.state in {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED} and state is not managed.state:
            return gate("terminal state regression")
        if state is OrderState.REJECTED and filled:
            return gate("rejected order with fills")
        return self._event_decision(identity, "applied", managed=managed, state=state, filled=filled,
                                    cursor=(event.provider_timestamp, identity))

    def _event_decision(self, identity, outcome, *, managed=None, state=None, filled=None,
                        execution=None, cursor=None, stream_online=None, reason=""):
        if outcome == "deferred" and identity in self._deferred_events:
            self._event_gate = True
            self.state = OmsState.RECOVERING
            return outcome
        payload = dict(event_id=identity, outcome=outcome, reason=reason)
        if managed is not None:
            payload.update(order_id=str(managed.order.order_id),
                           state=(state or managed.state).value,
                           filled_quantity=filled if filled is not None else managed.filled_quantity)
        if cursor is not None:
            payload["cursor"] = [cursor[0].isoformat(), cursor[1]]
            payload["provider_order_id"] = managed.provider_order_id
        if execution is not None:
            payload["execution"] = execution_payload(execution)
        if stream_online is not None:
            payload["stream_online"] = stream_online
        try:
            self.journal.append(events.EXECUTION_EVENT_DECISION, payload)
        except Exception:
            self._receipt_failed = True
            self.state = OmsState.RECOVERING
            raise
        self._apply_event_decision(payload)
        return outcome

    def _apply_event_decision(self, payload):
        identity, outcome = payload["event_id"], payload["outcome"]
        if "order_id" in payload:
            managed = self.orders[UUID(payload["order_id"])]
            managed.state = OrderState(payload["state"])
            managed.filled_quantity = payload["filled_quantity"]
        if "execution" in payload:
            execution = execution_from_payload(payload["execution"])
            self.platform_executions[f"{execution.provider_order_id}:{execution.execution_id}"] = execution
        if "cursor" in payload:
            from datetime import datetime
            self._order_event_cursors[payload["provider_order_id"]] = (datetime.fromisoformat(payload["cursor"][0]), payload["cursor"][1])
        if "stream_online" in payload:
            self._stream_online = payload["stream_online"]
        with self._inbox_lock:
            if outcome == "deferred":
                self._deferred_events.add(identity)
            else:
                self._pending_events.pop(identity, None)
                self._deferred_events.discard(identity)
            if outcome in {"gate", "deferred"}:
                self._event_gate = True
                self.state = OmsState.RECOVERING


def _event_state(kind, filled, quantity):
    state = {
        Kind.ACKNOWLEDGED: OrderState.ACCEPTED, Kind.UPDATED: OrderState.ACCEPTED,
        Kind.PARTIAL_FILL: OrderState.PARTIALLY_FILLED, Kind.FILLED: OrderState.FILLED,
        Kind.CANCELLED: OrderState.CANCELLED, Kind.REJECTED: OrderState.REJECTED,
    }.get(kind)
    if state is OrderState.ACCEPTED and filled:
        return OrderState.FILLED if filled == quantity else OrderState.PARTIALLY_FILLED
    return state
