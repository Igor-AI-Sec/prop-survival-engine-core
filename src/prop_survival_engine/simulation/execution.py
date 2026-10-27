from prop_survival_engine.core.models import Order, Side
from prop_survival_engine.execution.interfaces import (
    ExecutionAdapter, PlatformOrder, PlatformOrderStatus, PlatformSnapshot,
    SubmissionResult, SubmissionStatus,
)
from prop_survival_engine.market_data.interfaces import MarketDataAdapter


class SimulatedExecutionAdapter:
    """In-process execution simulator; no external connections."""

    def __init__(self) -> None:
        self.orders: dict[str, PlatformOrder] = {}
        self.positions: dict[str, int] = {}

    def submit(self, order: Order) -> SubmissionResult:
        if order.client_order_id in self.orders:
            return SubmissionResult(SubmissionStatus.ACCEPTED, "idempotent replay")
        self.orders[order.client_order_id] = PlatformOrder(
            order.client_order_id, PlatformOrderStatus.ACCEPTED, 0
        )
        return SubmissionResult(SubmissionStatus.ACCEPTED)

    def cancel(self, client_order_id: str) -> None:
        current = self.orders.get(client_order_id)
        if current:
            self.orders[client_order_id] = PlatformOrder(
                client_order_id, PlatformOrderStatus.CANCELLED, current.filled_quantity
            )

    def snapshot(self) -> PlatformSnapshot:
        return PlatformSnapshot(tuple(self.orders.values()), dict(self.positions))

    def set_status(self, client_order_id: str, status: PlatformOrderStatus, filled_quantity: int = 0) -> None:
        self.orders[client_order_id] = PlatformOrder(client_order_id, status, filled_quantity)


class SimulatedMarketDataAdapter:
    def __init__(self) -> None:
        self.subscriptions: dict[str, object] = {}

    def subscribe(self, instrument: str, callback) -> None:
        self.subscriptions[instrument] = callback

    def unsubscribe(self, instrument: str) -> None:
        self.subscriptions.pop(instrument, None)

    def publish(self, event) -> None:
        callback = self.subscriptions.get(event.instrument)
        if callback:
            callback(event)


def adapter_contracts_hold() -> bool:
    """Runtime Protocol check helpers are exposed for downstream wiring."""
    return isinstance(SimulatedExecutionAdapter(), ExecutionAdapter) and isinstance(
        SimulatedMarketDataAdapter(), MarketDataAdapter
    )

