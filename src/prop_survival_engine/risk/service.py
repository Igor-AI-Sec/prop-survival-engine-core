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
        if max_quantity < 1:
            raise ValueError("max_quantity must be positive")
        self.max_quantity = max_quantity

    def evaluate(self, intent: OrderIntent) -> RiskDecision:
        if intent.quantity <= self.max_quantity:
            return RiskDecision(RiskAction.ALLOW, intent.quantity)
        return RiskDecision(RiskAction.REDUCE, self.max_quantity, "quantity limit")

