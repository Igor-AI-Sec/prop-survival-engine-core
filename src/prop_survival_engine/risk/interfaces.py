from typing import Protocol

from prop_survival_engine.core.models import OrderIntent
from prop_survival_engine.risk.service import RiskDecision


class RiskEvaluator(Protocol):
    def evaluate(self, intent: OrderIntent) -> RiskDecision: ...

