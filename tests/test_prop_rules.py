import json
from decimal import Decimal

import pytest

from prop_survival_engine.prop_rules.engine import StatefulPropRuleEngine
from prop_survival_engine.prop_rules.models import (
    AccountStatus,
    DailyLossBreached,
    EndOfDaySettled,
    MarkToMarketUpdated,
    PropRuleConfig,
    RealizedPnlUpdated,
    SessionStarted,
    TargetReached,
    TrailingDrawdownBreached,
)


D = Decimal


def make_engine(*, daily_loss="80", trailing="100", target="500"):
    return StatefulPropRuleEngine(PropRuleConfig(
        starting_balance=D("1000"),
        trailing_drawdown=D(trailing),
        daily_loss_limit=D(daily_loss),
        profit_target_gain=D(target),
    ))


def set_equity(engine, equity):
    return engine.apply(MarkToMarketUpdated(D(equity) - engine.state.starting_balance
                                              - engine.state.realized_pnl))


@pytest.mark.parametrize(
    ("equity", "expected"),
    [("920.00", AccountStatus.BREACHED),
     ("920.01", AccountStatus.ALIVE),
     ("919.99", AccountStatus.BREACHED)],
)
def test_exact_and_adjacent_daily_floor_boundaries(equity, expected):
    engine = make_engine()
    signals = set_equity(engine, equity)
    assert engine.state.daily_floor == D("920")
    assert engine.state.status is expected
    assert any(isinstance(signal, DailyLossBreached) for signal in signals) == (
        expected is AccountStatus.BREACHED
    )


@pytest.mark.parametrize(
    ("equity", "expected"),
    [("900.00", AccountStatus.BREACHED),
     ("900.01", AccountStatus.ALIVE),
     ("899.99", AccountStatus.BREACHED)],
)
def test_exact_and_adjacent_trailing_floor_boundaries(equity, expected):
    engine = make_engine(daily_loss="200")
    signals = set_equity(engine, equity)
    assert engine.state.trailing_floor == D("900")
    assert engine.state.status is expected
    assert any(isinstance(signal, TrailingDrawdownBreached) for signal in signals) == (
        expected is AccountStatus.BREACHED
    )


def test_exact_profit_target_passes_and_emits_target_event():
    engine = make_engine()
    signals = set_equity(engine, "1500.00")
    assert engine.state.equity == engine.state.target_equity == D("1500")
    assert engine.state.status is AccountStatus.PASSED
    assert signals == (TargetReached(D("1500"), D("1500")),)


def test_eod_high_watermark_increases_only_on_settlement_event():
    engine = make_engine()
    engine.apply(RealizedPnlUpdated(D("150")))
    assert engine.state.equity == D("1150")
    assert engine.state.eod_high_watermark == D("1000")
    assert engine.state.trailing_floor == D("900")
    engine.apply(EndOfDaySettled("initial"))
    assert engine.state.eod_high_watermark == D("1150")
    assert engine.state.trailing_floor == D("1050")


def test_eod_high_watermark_does_not_fall_after_losing_day():
    engine = make_engine()
    engine.apply(RealizedPnlUpdated(D("200")))
    engine.apply(EndOfDaySettled("initial"))
    engine.apply(SessionStarted("day-2"))
    engine.apply(RealizedPnlUpdated(D("150")))
    engine.apply(EndOfDaySettled("day-2"))
    assert engine.state.equity == D("1150")
    assert engine.state.eod_high_watermark == D("1200")
    assert engine.state.trailing_floor == D("1100")


def test_daily_floor_resets_only_at_session_boundary():
    engine = make_engine()
    engine.apply(RealizedPnlUpdated(D("200")))
    engine.apply(EndOfDaySettled("initial"))
    assert engine.state.daily_floor == D("920")
    engine.apply(SessionStarted("day-2"))
    assert engine.state.day_start_equity == D("1200")
    assert engine.state.daily_floor == D("1120")
    with pytest.raises(ValueError, match="active session"):
        engine.apply(EndOfDaySettled("initial"))


def test_restart_before_eod_preserves_unsettled_high_watermark_then_settles():
    engine = make_engine()
    engine.apply(RealizedPnlUpdated(D("125")))
    restored = StatefulPropRuleEngine.from_snapshot(json.loads(json.dumps(engine.snapshot())))
    assert restored.state.equity == D("1125")
    assert restored.state.eod_high_watermark == D("1000")
    assert restored.state.trailing_floor == D("900")
    restored.apply(EndOfDaySettled("initial"))
    assert restored.state.eod_high_watermark == D("1125")
    assert restored.state.trailing_floor == D("1025")


def test_restart_after_eod_preserves_settled_floors():
    engine = make_engine()
    engine.apply(RealizedPnlUpdated(D("125")))
    engine.apply(EndOfDaySettled("initial"))
    restored = StatefulPropRuleEngine.from_snapshot(json.loads(json.dumps(engine.snapshot())))
    assert restored.state.eod_high_watermark == D("1125")
    assert restored.state.trailing_floor == D("1025")
    assert restored.state.daily_floor == D("920")


@pytest.mark.parametrize("terminal", [AccountStatus.BREACHED, AccountStatus.PASSED])
def test_terminal_status_persists_after_restart_and_never_returns_alive(terminal):
    engine = make_engine()
    if terminal is AccountStatus.BREACHED:
        set_equity(engine, "920")
    else:
        set_equity(engine, "1500")
    restored = StatefulPropRuleEngine.from_snapshot(json.loads(json.dumps(engine.snapshot())))
    assert restored.state.status is terminal
    restored.apply(SessionStarted("next-session"))
    restored.apply(RealizedPnlUpdated(D("0")))
    assert restored.state.status is terminal


def test_open_unrealized_pnl_contributes_to_equity_and_can_breach_floor():
    engine = make_engine()
    engine.apply(MarkToMarketUpdated(D("-80")))
    assert engine.state.unrealized_pnl == D("-80")
    assert engine.state.equity == D("920")
    assert engine.state.status is AccountStatus.BREACHED


def test_realized_and_unrealized_pnl_remain_consistent_with_equity():
    engine = make_engine()
    engine.apply(MarkToMarketUpdated(D("-12.34")))
    assert engine.state.equity == D("987.66")
    engine.apply(RealizedPnlUpdated(D("10.00")))
    assert engine.state.equity == D("997.66")
    assert engine.state.equity == (
        engine.state.starting_balance
        + engine.state.realized_pnl
        + engine.state.unrealized_pnl
    )


def test_duplicate_session_start_does_not_reset_daily_floor():
    engine = make_engine()
    engine.apply(RealizedPnlUpdated(D("200")))
    engine.apply(SessionStarted("day-2"))
    original_floor = engine.state.daily_floor
    engine.apply(RealizedPnlUpdated(D("-50")))
    engine.apply(SessionStarted("day-2"))
    assert engine.state.daily_floor == original_floor
    assert engine.state.daily_floor == D("1120")
