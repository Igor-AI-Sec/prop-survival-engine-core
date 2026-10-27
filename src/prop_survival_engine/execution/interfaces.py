from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from prop_survival_engine.core.models import Order
from prop_survival_engine.core.ownership import InventoryEvidence


class SubmissionStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    UNCERTAIN = "UNCERTAIN"


class PlatformOrderStatus(str, Enum):
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class SubmissionResult:
    status: SubmissionStatus
    detail: str = ""
    provider_order_id: str | None = None
    child_provider_order_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlatformOrder:
    client_order_id: str
    status: PlatformOrderStatus
    filled_quantity: int = 0
    provider_order_id: str | None = None
    our_order_id: str | None = None
    child_provider_order_ids: tuple[str, ...] = ()
    provider_timestamp: datetime | None = None


@dataclass(frozen=True)
class PlatformExecution:
    """Provider-neutral execution evidence included in reconciliation snapshots."""

    execution_id: str
    provider_order_id: str
    instrument: str
    side: str
    quantity: int
    price: Decimal
    timestamp: datetime
    our_order_id: str | None = None


@dataclass(frozen=True)
class PlatformSnapshot:
    orders: tuple[PlatformOrder, ...] = ()
    positions: dict[str, int] | None = None
    executions: tuple[PlatformExecution, ...] = ()
    inventory: InventoryEvidence | None = None


@runtime_checkable
class ExecutionAdapter(Protocol):
    def submit(self, order: Order) -> SubmissionResult: ...
    def cancel(self, client_order_id: str) -> None: ...
    def snapshot(self) -> PlatformSnapshot: ...
