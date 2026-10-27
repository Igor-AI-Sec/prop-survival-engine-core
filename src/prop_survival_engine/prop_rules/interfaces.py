from typing import Protocol

from prop_survival_engine.prop_rules.models import AccountState, RuleEvent, RuleSignal


class PropRuleEngine(Protocol):
    @property
    def state(self) -> AccountState: ...

    def apply(self, event: RuleEvent) -> tuple[RuleSignal, ...]: ...

    def snapshot(self) -> dict: ...
