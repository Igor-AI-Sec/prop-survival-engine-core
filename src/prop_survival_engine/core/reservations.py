"""Provider-neutral risk liabilities, always in the account currency."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from uuid import UUID

from .models import Side


class ReservationStatus(str, Enum):
    RESERVED = "RESERVED"
    PARTIALLY_CONSUMED = "PARTIALLY_CONSUMED"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"
    UNCERTAIN = "UNCERTAIN"


class RiskOwner(str, Enum):
    RESERVATION = "RESERVATION"
    PENDING = "PENDING"
    WORKING = "WORKING"
    POSITION = "POSITION"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class RiskReservation:
    reservation_id: UUID
    intent_id: UUID
    account_id: str
    instrument: str
    side: Side
    quantity: int
    per_contract_worst_case_loss: Decimal
    total_reserved_loss: Decimal
    created_at: datetime
    status: ReservationStatus
    remaining_quantity: int
    position_quantity: int
    owner: RiskOwner
    order_id: UUID | None = None

    def __post_init__(self):
        if (type(self.reservation_id) is not UUID or type(self.intent_id) is not UUID
                or not self.account_id or not self.instrument or type(self.side) is not Side
                or type(self.status) is not ReservationStatus or type(self.owner) is not RiskOwner
                or self.created_at.utcoffset() is None):
            raise ValueError("Invalid reservation identity")
        if (any(type(n) is not int or n < 0 for n in
                (self.quantity, self.remaining_quantity, self.position_quantity))
                or self.quantity == 0 or self.remaining_quantity + self.position_quantity > self.quantity):
            raise ValueError("Invalid reservation quantity")
        loss, total = self.per_contract_worst_case_loss, self.total_reserved_loss
        if (type(loss) is not Decimal or not loss.is_finite() or loss <= 0
                or type(total) is not Decimal or not total.is_finite()
                or Fraction(total) != self.remaining_quantity * Fraction(loss)):
            raise ValueError("Invalid exact reservation money")


@dataclass(frozen=True)
class RiskExposure:
    """Explicit conservative baseline not already owned by an OMS reservation.

    Unknown/unpriced exposure must gate admission, never be assigned zero loss.
    Entries are immutable; only authoritative reconciliation may reduce them.
    """
    exposure_id: str
    instrument: str
    side: Side
    quantity: int
    per_contract_worst_case_loss: Decimal
    owner: RiskOwner = RiskOwner.POSITION

    def __post_init__(self):
        if (not self.exposure_id or self.exposure_id.startswith("oms:") or not self.instrument
                or type(self.side) is not Side or type(self.owner) is not RiskOwner
                or type(self.quantity) is not int or self.quantity <= 0
                or type(self.per_contract_worst_case_loss) is not Decimal
                or not self.per_contract_worst_case_loss.is_finite()
                or self.per_contract_worst_case_loss <= 0):
            raise ValueError("Exposure requires identity, units and positive exact loss")
