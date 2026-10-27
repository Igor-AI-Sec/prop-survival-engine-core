from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from prop_survival_engine.core.models import Order


class OmsState(str, Enum):
    STARTING = "STARTING"
    RECOVERING = "RECOVERING"
    READY = "READY"


class OrderState(str, Enum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    UNCERTAIN = "UNCERTAIN"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass
class ManagedOrder:
    order: Order
    state: OrderState
    filled_quantity: int = 0
    provider_order_id: str | None = None
    child_provider_order_ids: tuple[str, ...] = ()


class DuplicateIntentConflict(ValueError):
    pass
