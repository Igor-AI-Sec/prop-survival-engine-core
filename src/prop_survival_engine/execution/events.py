"""Provider-neutral execution observations and stable content fingerprints."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json

from prop_survival_engine.execution.interfaces import PlatformExecution


class ExecutionEventKind(str, Enum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UPDATED = "UPDATED"
    EXECUTION = "EXECUTION"
    UNKNOWN = "UNKNOWN"
    STREAM_CONNECTED = "STREAM_CONNECTED"
    STREAM_DISCONNECTED = "STREAM_DISCONNECTED"
    STREAM_STOPPED = "STREAM_STOPPED"


@dataclass(frozen=True)
class ExecutionEvent:
    kind: ExecutionEventKind
    provider_order_id: str | None = None
    provider_timestamp: datetime | None = None
    filled_quantity: int | None = None  # cumulative, never a fill delta
    execution: PlatformExecution | None = None  # unique execution delta
    order_quantity: int | None = None
    instrument: str | None = None
    side: str | None = None
    child_provider_order_ids: tuple[str, ...] = ()
    occurrence_id: str | None = None  # unique local identity for control/malformed events

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ExecutionEventKind):
            raise TypeError("execution event kind must be provider-neutral")
        if self.provider_timestamp is not None and self.provider_timestamp.utcoffset() is None:
            raise ValueError("provider timestamps must be timezone-aware")
        for value in (self.filled_quantity, self.order_quantity):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("event quantities must be whole nonnegative contracts")
        if self.execution is not None and not isinstance(self.execution, PlatformExecution):
            raise TypeError("execution evidence must be provider-neutral")

    @property
    def fingerprint(self) -> str:
        return sha256(json.dumps(event_payload(self), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def execution_payload(value: PlatformExecution) -> dict:
    # Local correlation can differ after restart; it is not execution identity.
    return dict(execution_id=value.execution_id, provider_order_id=value.provider_order_id,
                instrument=value.instrument, side=value.side, quantity=value.quantity,
                price=str(value.price.normalize()), timestamp=value.timestamp.astimezone(timezone.utc).isoformat())


def execution_from_payload(value: dict) -> PlatformExecution:
    return PlatformExecution(value["execution_id"], value["provider_order_id"], value["instrument"],
                             value["side"], value["quantity"], Decimal(value["price"]),
                             datetime.fromisoformat(value["timestamp"]))


def event_payload(value: ExecutionEvent) -> dict:
    return dict(kind=value.kind.value, provider_order_id=value.provider_order_id,
                provider_timestamp=value.provider_timestamp.astimezone(timezone.utc).isoformat() if value.provider_timestamp else None,
                filled_quantity=value.filled_quantity,
                execution=execution_payload(value.execution) if value.execution else None,
                order_quantity=value.order_quantity, instrument=value.instrument, side=value.side,
                child_provider_order_ids=list(value.child_provider_order_ids), occurrence_id=value.occurrence_id)


def event_from_payload(value: dict) -> ExecutionEvent:
    return ExecutionEvent(ExecutionEventKind(value["kind"]), value["provider_order_id"],
                          datetime.fromisoformat(value["provider_timestamp"]) if value["provider_timestamp"] else None,
                          value["filled_quantity"], execution_from_payload(value["execution"]) if value["execution"] else None,
                          value["order_quantity"], value["instrument"], value["side"],
                          tuple(value["child_provider_order_ids"]), value["occurrence_id"])
