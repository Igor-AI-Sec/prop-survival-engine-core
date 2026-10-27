"""Distance-to-Ruin means account cushion to active breach boundaries."""

from dataclasses import dataclass, fields
from enum import Enum
from fractions import Fraction

from decimal import Decimal
from prop_survival_engine.oms.models import OmsState
from prop_survival_engine.prop_rules.models import AccountState, AccountStatus
from prop_survival_engine.risk.money import finite_decimal, subtract


class SessionState(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"
    PAUSED = "PAUSED"


@dataclass(frozen=True)
class ActiveLossFloors:
    """Caller-supplied rule applicability; default/mock engine uses both floors."""
    daily: bool = True
    trailing: bool = True

    def __post_init__(self):
        if type(self.daily) is not bool or type(self.trailing) is not bool:
            raise ValueError("active floor flags must be bool")


@dataclass(frozen=True)
class RiskContext:
    account: AccountState
    oms_state: OmsState
    # A session ID does not establish that entry admission is open.
    session_state: SessionState = SessionState.UNKNOWN
    active_floors: ActiveLossFloors = ActiveLossFloors()


@dataclass(frozen=True)
class RuinDistances:
    equity: Decimal
    distance_to_daily_floor: Decimal
    distance_to_trailing_floor: Decimal
    effective_ruin_distance: Decimal | None
    distance_to_target: Decimal | Fraction
    account_status: AccountStatus
    session_id: str
    session_state: SessionState
    unrealized_pnl: Decimal
    active_floors: ActiveLossFloors


def derive_ruin_distances(context: RiskContext) -> RuinDistances:
    """Read-only signed distances; no clamping, rule updates or status changes.

    Missing active boundaries are represented by None, not infinite capacity.
    Equity is supplied by the firm-neutral rule engine, including unrealized PnL
    according to that engine's policy. Risk does not count unrealized PnL again.
    """
    if (not isinstance(context, RiskContext) or not isinstance(context.account, AccountState)
            or not isinstance(context.active_floors, ActiveLossFloors)
            or not isinstance(context.session_state, SessionState)):
        raise ValueError("invalid risk context")
    state = context.account
    for field in fields(state):
        if field.name == "target_equity" and type(state.target_equity) is Fraction:
            if state.target_equity <= 0:
                raise ValueError("target must be positive")
            continue
        if field.name not in {"session_id", "status"} and not finite_decimal(getattr(state, field.name)):
            raise ValueError("account money must be finite Decimal values")
    if (not isinstance(state.status, AccountStatus) or not isinstance(state.session_id, str)
            or not state.session_id or state.starting_balance <= 0
            or Fraction(state.equity) != (Fraction(state.starting_balance)
                                        + Fraction(state.realized_pnl) + Fraction(state.unrealized_pnl))):
        raise ValueError("invalid account state")
    daily = subtract(state.equity, state.daily_floor)
    trailing = subtract(state.equity, state.trailing_floor)
    active = ([daily] if context.active_floors.daily else []) + ([trailing] if context.active_floors.trailing else [])
    return RuinDistances(state.equity, daily, trailing, min(active) if active else None,
                         (state.target_equity - Fraction(state.equity) if type(state.target_equity) is Fraction
                          else subtract(state.target_equity, state.equity)), state.status,
                         state.session_id, context.session_state, state.unrealized_pnl, context.active_floors)
