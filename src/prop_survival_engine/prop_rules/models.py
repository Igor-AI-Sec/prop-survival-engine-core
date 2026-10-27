from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from datetime import datetime


class AccountStatus(str, Enum):
    ALIVE = "ALIVE"
    BREACHED = "BREACHED"
    PASSED = "PASSED"


@dataclass(frozen=True)
class PropRuleConfig:
    """Firm-neutral defaults; ``profit_target_gain`` is an amount above start."""

    starting_balance: Decimal
    trailing_drawdown: Decimal
    daily_loss_limit: Decimal
    profit_target_gain: Decimal

    def __post_init__(self) -> None:
        if self.starting_balance <= 0:
            raise ValueError("starting_balance must be positive")
        if self.trailing_drawdown <= 0:
            raise ValueError("trailing_drawdown must be positive")
        if self.daily_loss_limit <= 0:
            raise ValueError("daily_loss_limit must be positive")
        if self.profit_target_gain <= 0:
            raise ValueError("profit_target_gain must be positive")


@dataclass(frozen=True)
class AccountState:
    starting_balance: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    day_start_equity: Decimal
    eod_high_watermark: Decimal
    trailing_floor: Decimal
    daily_floor: Decimal
    target_equity: Decimal | Fraction
    session_id: str
    status: AccountStatus

    def __post_init__(self) -> None:
        expected_equity = self.starting_balance + self.realized_pnl + self.unrealized_pnl
        if self.equity != expected_equity:
            raise ValueError(
                "equity must equal starting_balance + realized_pnl + unrealized_pnl"
            )
        if not self.session_id:
            raise ValueError("session_id is required")


# Compatibility name retained for callers of the initial v0.1 foundation.
PropAccountState = AccountState


@dataclass(frozen=True)
class SessionStarted:
    session_id: str


@dataclass(frozen=True)
class MarkToMarketUpdated:
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class RealizedPnlUpdated:
    """Sets cumulative realized PnL since account start (not a delta)."""

    realized_pnl: Decimal


@dataclass(frozen=True)
class PnlSettled:
    """Atomically set cumulative realized and remaining unrealized PnL.

    Closing a marked position must not temporarily remove or double-count its
    PnL while transferring it between these two account components.
    """
    realized_pnl: Decimal
    unrealized_pnl: Decimal = Decimal("0")


@dataclass(frozen=True)
class EndOfDaySettled:
    session_id: str


@dataclass(frozen=True)
class ClockAdvanced:
    at: datetime


@dataclass(frozen=True)
class PositionInventoryUpdated:
    open_positions: int


@dataclass(frozen=True)
class DailyLossPaused:
    equity: Decimal
    daily_floor: Decimal


@dataclass(frozen=True)
class TradingRestrictionBreached:
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class TargetReached:
    equity: Decimal
    target: Decimal | Fraction


@dataclass(frozen=True)
class DailyLossBreached:
    equity: Decimal
    daily_floor: Decimal


@dataclass(frozen=True)
class TrailingDrawdownBreached:
    equity: Decimal
    trailing_floor: Decimal


RuleInputEvent = SessionStarted | MarkToMarketUpdated | RealizedPnlUpdated | PnlSettled | EndOfDaySettled | ClockAdvanced | PositionInventoryUpdated
RuleSignal = TargetReached | DailyLossBreached | TrailingDrawdownBreached | DailyLossPaused | TradingRestrictionBreached
RuleEvent = RuleInputEvent | RuleSignal
