from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from uuid import UUID, uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class OrderIntent:
    instrument: str
    side: Side
    quantity: int
    intent_id: UUID = field(default_factory=uuid4)
    is_entry: bool = True
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.instrument:
            raise ValueError("instrument is required")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("quantity must be a positive integer")


@dataclass(frozen=True)
class Order:
    """Executable OMS order; constructed by OMS after risk approval."""

    order_id: UUID
    client_order_id: str
    intent_id: UUID
    instrument: str
    side: Side
    quantity: int
    is_entry: bool = True


@dataclass(frozen=True)
class Fill:
    order_id: UUID
    quantity: int
    price: Decimal


@dataclass(frozen=True)
class MarketEvent:
    instrument: str
    kind: str
    timestamp: datetime
    price: Decimal
    quantity: int = 0


@dataclass(frozen=True)
class AccountSnapshot:
    equity: Decimal
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    timestamp: datetime = field(default_factory=utc_now)

