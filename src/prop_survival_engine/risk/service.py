from dataclasses import dataclass
from enum import Enum

from prop_survival_engine.core.models import OrderIntent


class RiskAction(str, Enum):
    ALLOW = "ALLOW"
    REDUCE = "REDUCE"
    REJECT = "REJECT"


@dataclass(frozen=True)
class RiskDecision:
    action: RiskAction
    quantity: int | None = None
    reason: str = ""


class BasicRiskEvaluator:
    """Minimal injected-limit evaluator, not a trading strategy."""

    def __init__(self, max_quantity: int) -> None:
        if type(max_quantity) is not int or max_quantity < 1:
            raise ValueError("max_quantity must be a positive integer")
        self.max_quantity = max_quantity

    def evaluate(self, intent: OrderIntent) -> RiskDecision:
        if not isinstance(intent, OrderIntent) or type(intent.quantity) is not int or intent.quantity <= 0:
            raise ValueError("intent quantity must be a positive integer")
        if intent.quantity <= self.max_quantity:
            return RiskDecision(RiskAction.ALLOW, intent.quantity)
        return RiskDecision(RiskAction.REDUCE, self.max_quantity, "quantity limit")

