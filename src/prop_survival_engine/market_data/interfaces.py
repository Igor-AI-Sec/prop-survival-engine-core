from typing import Callable, Protocol, runtime_checkable

from prop_survival_engine.core.models import MarketEvent


@runtime_checkable
class MarketDataAdapter(Protocol):
    def subscribe(self, instrument: str, callback: Callable[[MarketEvent], None]) -> None: ...
    def unsubscribe(self, instrument: str) -> None: ...
