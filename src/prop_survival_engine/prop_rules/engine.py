from dataclasses import asdict, replace
from decimal import Decimal
from typing import Any

from prop_survival_engine.prop_rules.models import (
    AccountState,
    AccountStatus,
    DailyLossBreached,
    EndOfDaySettled,
    MarkToMarketUpdated,
    PropRuleConfig,
    PnlSettled,
    RealizedPnlUpdated,
    RuleEvent,
    RuleInputEvent,
    RuleSignal,
    SessionStarted,
    TargetReached,
    TrailingDrawdownBreached,
)


class StatefulPropRuleEngine:
    """Deterministic, firm-neutral account state machine.

    This implements a default/mock policy, not a universal prop-firm rule set:

    * daily floor = day-start equity - configured daily loss limit
    * unrealized PnL contributes to equity and rule evaluation
    * touching either loss floor breaches; touching target equity passes
    * trailing floor ratchets only on ``EndOfDaySettled``
    * target equity = starting balance + configured profit-target gain

    Future firm-specific engines may override daily-floor calculation,
    unrealized-PnL inclusion, touch semantics, trailing-floor timing, and target
    semantics. No real firm policy is encoded here.
    """

    SNAPSHOT_VERSION = 2
    INITIAL_SESSION_ID = "initial"

    def __init__(self, config: PropRuleConfig) -> None:
        self.config = config
        start = config.starting_balance
        self._state = AccountState(
            starting_balance=start,
            equity=start,
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            day_start_equity=start,
            eod_high_watermark=start,
            trailing_floor=start - config.trailing_drawdown,
            daily_floor=self._calculate_daily_floor(start),
            target_equity=self._calculate_target_equity(start),
            session_id=self.INITIAL_SESSION_ID,
            status=AccountStatus.ALIVE,
        )
        self._event_history: list[RuleInputEvent] = []
        self._signal_history: list[RuleSignal] = []

    @property
    def state(self) -> AccountState:
        return self._state

    @property
    def event_history(self) -> tuple[RuleInputEvent, ...]:
        """Input events suitable for replay by an external durable event store."""
        return tuple(self._event_history)

    @property
    def signal_history(self) -> tuple[RuleSignal, ...]:
        return tuple(self._signal_history)

    def apply(self, event: RuleEvent) -> tuple[RuleSignal, ...]:
        """Apply one account event and return deterministic rule signals."""
        if isinstance(event, SessionStarted):
            if not event.session_id:
                raise ValueError("session_id is required")
            if event.session_id == self._state.session_id:
                return ()  # repeated boundary event is idempotent
            self._state = replace(
                self._state,
                session_id=event.session_id,
                day_start_equity=self._state.equity,
                daily_floor=self._calculate_daily_floor(self._state.equity),
            )
        elif isinstance(event, MarkToMarketUpdated):
            self._state = replace(
                self._state,
                unrealized_pnl=event.unrealized_pnl,
                equity=(self._state.starting_balance
                        + self._state.realized_pnl
                        + event.unrealized_pnl),
            )
        elif isinstance(event, PnlSettled):
            self._state = replace(
                self._state, realized_pnl=event.realized_pnl,
                unrealized_pnl=event.unrealized_pnl,
                equity=self._state.starting_balance + event.realized_pnl + event.unrealized_pnl,
            )
        elif isinstance(event, RealizedPnlUpdated):
            self._state = replace(
                self._state,
                realized_pnl=event.realized_pnl,
                equity=(self._state.starting_balance
                        + event.realized_pnl
                        + self._state.unrealized_pnl),
            )
        elif isinstance(event, EndOfDaySettled):
            if event.session_id != self._state.session_id:
                raise ValueError("EOD settlement session_id must match the active session")
            high, floor = self._settle_eod_trailing(self._state)
            self._state = replace(
                self._state,
                eod_high_watermark=high,
                trailing_floor=floor,
            )
        elif isinstance(event, TargetReached):
            if self._state.status is AccountStatus.ALIVE:
                self._state = replace(self._state, status=AccountStatus.PASSED)
        elif isinstance(event, (DailyLossBreached, TrailingDrawdownBreached)):
            if self._state.status is AccountStatus.ALIVE:
                self._state = replace(self._state, status=AccountStatus.BREACHED)
        else:  # pragma: no cover - protects future event additions
            raise TypeError(f"unsupported prop rule event: {type(event).__name__}")

        if isinstance(event, (SessionStarted, MarkToMarketUpdated, RealizedPnlUpdated, PnlSettled, EndOfDaySettled)):
            self._event_history.append(event)

        signals = self._evaluate_thresholds()
        self._signal_history.extend(signals)
        return tuple(signals)

    def _evaluate_thresholds(self) -> list[RuleSignal]:
        state = self._state
        if state.status is not AccountStatus.ALIVE:
            return []

        signals: list[RuleSignal] = []
        # Equality is intentionally treated as crossing for both loss floors.
        if self._is_daily_loss_breached(state.equity, state.daily_floor):
            signals.append(DailyLossBreached(state.equity, state.daily_floor))
        if self._is_trailing_drawdown_breached(state.equity, state.trailing_floor):
            signals.append(TrailingDrawdownBreached(state.equity, state.trailing_floor))
        if signals:
            self._state = replace(state, status=AccountStatus.BREACHED)
            return signals

        # If a target and loss boundary somehow coincide, loss breach wins.
        if self._is_target_reached(state.equity, state.target_equity):
            signal = TargetReached(state.equity, state.target_equity)
            self._state = replace(state, status=AccountStatus.PASSED)
            return [signal]
        return []

    def _calculate_daily_floor(self, day_start_equity: Decimal) -> Decimal:
        """Default/mock policy; firm-specific policies may override this."""
        return day_start_equity - self.config.daily_loss_limit

    def _calculate_target_equity(self, starting_balance: Decimal) -> Decimal:
        return starting_balance + self.config.profit_target_gain

    def _settle_eod_trailing(self, state: AccountState) -> tuple[Decimal, Decimal]:
        """Default EOD-only ratchet; firm-specific timing/formulas may override."""
        high = max(state.eod_high_watermark, state.equity)
        floor = max(state.trailing_floor, high - self.config.trailing_drawdown)
        return high, floor

    def _is_daily_loss_breached(self, equity: Decimal, daily_floor: Decimal) -> bool:
        """Default/mock breach-on-touch behavior; implementations may override."""
        return equity <= daily_floor

    def _is_trailing_drawdown_breached(self, equity: Decimal, trailing_floor: Decimal) -> bool:
        """Default/mock breach-on-touch behavior; implementations may override."""
        return equity <= trailing_floor

    def _is_target_reached(self, equity: Decimal, target_equity: Decimal) -> bool:
        """Default/mock target-touch behavior; implementations may override."""
        return equity >= target_equity

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable state snapshot for caller-managed storage."""
        return {
            "version": self.SNAPSHOT_VERSION,
            "config": {key: str(value) for key, value in asdict(self.config).items()},
            "state": {
                key: value.value if isinstance(value, AccountStatus) else str(value)
                for key, value in asdict(self._state).items()
            },
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> "StatefulPropRuleEngine":
        if snapshot.get("version") != cls.SNAPSHOT_VERSION:
            raise ValueError("unsupported prop rule snapshot version")
        config_data = snapshot["config"]
        engine = cls(PropRuleConfig(
            starting_balance=Decimal(config_data["starting_balance"]),
            trailing_drawdown=Decimal(config_data["trailing_drawdown"]),
            daily_loss_limit=Decimal(config_data["daily_loss_limit"]),
            profit_target_gain=Decimal(config_data["profit_target_gain"]),
        ))
        data = snapshot["state"]
        engine._state = AccountState(
            starting_balance=Decimal(data["starting_balance"]),
            equity=Decimal(data["equity"]),
            realized_pnl=Decimal(data["realized_pnl"]),
            unrealized_pnl=Decimal(data["unrealized_pnl"]),
            day_start_equity=Decimal(data["day_start_equity"]),
            eod_high_watermark=Decimal(data["eod_high_watermark"]),
            trailing_floor=Decimal(data["trailing_floor"]),
            daily_floor=Decimal(data["daily_floor"]),
            target_equity=Decimal(data["target_equity"]),
            session_id=data["session_id"],
            status=AccountStatus(data["status"]),
        )
        if engine._state.starting_balance != engine.config.starting_balance:
            raise ValueError("snapshot starting balance does not match its configuration")
        if engine._state.target_equity != (
            engine.config.starting_balance + engine.config.profit_target_gain
        ):
            raise ValueError("snapshot target equity does not match its configuration")
        if engine._state.daily_floor != engine._state.day_start_equity - engine.config.daily_loss_limit:
            raise ValueError("snapshot daily floor does not match its session state")
        if engine._state.trailing_floor > engine._state.eod_high_watermark:
            raise ValueError("snapshot trailing floor cannot exceed its EOD high-watermark")
        return engine
