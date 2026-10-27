"""Provider-neutral operational ownership and inventory evidence."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from .models import Side


class OwnershipStatus(str, Enum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"


@dataclass(frozen=True)
class OperationalOwner:
    owner_id: UUID
    account_id: str
    fencing_token: int
    acquired_at: datetime
    renewed_at: datetime
    status: OwnershipStatus

    def __post_init__(self):
        if (type(self.owner_id) is not UUID or not self.account_id
                or type(self.fencing_token) is not int or self.fencing_token <= 0
                or self.acquired_at.utcoffset() is None or self.renewed_at.utcoffset() is None
                or type(self.status) is not OwnershipStatus):
            raise ValueError("Invalid persistent ownership identity")


class InventoryOrderState(str, Enum):
    OPEN = "OPEN"
    WORKING = "WORKING"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class InventoryOrder:
    provider_order_id: str
    instrument: str
    side: Side
    quantity: int
    filled_quantity: int
    state: InventoryOrderState
    client_order_id: str | None = None
    our_order_id: str | None = None

    def __post_init__(self):
        if (not self.provider_order_id or not self.instrument or type(self.side) is not Side
                or type(self.quantity) is not int or self.quantity <= 0
                or type(self.filled_quantity) is not int or not 0 <= self.filled_quantity <= self.quantity
                or type(self.state) is not InventoryOrderState
                or self.state is InventoryOrderState.FILLED and self.filled_quantity != self.quantity):
            raise ValueError("Invalid inventory order")


@dataclass(frozen=True)
class InventoryEvidence:
    account_id: str
    environment_id: str
    binding_version: str
    provider_timestamp: datetime | None
    provider_version: int | None
    orders_complete: bool
    positions_complete: bool
    executions_complete: bool
    orders: tuple[InventoryOrder, ...] = ()


class InventoryClassification(str, Enum):
    LOCAL = "LOCAL"
    DUPLICATE = "DUPLICATE"
    EXTERNAL = "EXTERNAL"
    AMBIGUOUS = "AMBIGUOUS"
    TERMINAL_HISTORY = "TERMINAL_HISTORY"


@dataclass(frozen=True)
class PositionReconciliationEvidence:
    """Explicit operator-reviewed disposition bound to a persisted provider snapshot.

    This records review; it cannot make an incomplete or stale snapshot valid.
    It is not broker proof by itself. The bound inventory remains the authority.
    """
    snapshot_digest: str
    instrument: str
    expected_local_quantity: int
    provider_quantity: int
    evidence_id: str


@dataclass(frozen=True)
class InventoryLiability:
    key: str
    instrument: str
    side: Side
    quantity: int
    per_contract_loss: Decimal | None
    category: str
