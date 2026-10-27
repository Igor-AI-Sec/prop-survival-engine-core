"""All plans and outcomes in this file are fictional; no firm rules or edge."""
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext
from fractions import Fraction
import json

import pytest

from prop_survival_engine.prop_rules.models import (
    AccountStatus, EndOfDaySettled, MarkToMarketUpdated, PnlSettled,
    RealizedPnlUpdated, SessionStarted, TargetReached,
)
from prop_survival_engine.prop_rules.plans import (
    DrawdownType as D, Fees, PayoutFloorEffect as F, PayoutRules, PlanStage,
    PropPlan, RetryFee, derive_plan_metrics,
)
from prop_survival_engine.prop_rules.plan_engine import PlanRuleEngine, plan_evaluator
from prop_survival_engine.risk.policy import ContractLossEstimate, RiskPolicyConfig
from prop_survival_engine.simulation.outcomes import SequenceOutcomeSource, TradeOutcome
from prop_survival_engine.simulation.reach_avoid import PathConfig, PathEnd, Scenario
from prop_survival_engine.simulation.sizing import FixedContracts


def fictional_plan(**changes):
    plan = PropPlan(Decimal(1000), Decimal(100), Decimal(200), D.EOD_TRAILING,
        False, Decimal(150), None, 0, 2, PlanStage.EVALUATION,
        Fees(Decimal(30), Decimal(10), Decimal(20), RetryFee.RESET),
        PayoutRules(False, 0, Decimal(10), Decimal(100), Decimal(500),
                    Decimal("0.8"), Decimal(20), F.UNCHANGED),
        True, True, True, True, True)
    return replace(plan, **changes)


def engine(**changes):
    value = PlanRuleEngine(fictional_plan(**changes))
    value.apply(SessionStarted("fictional-session-1"))
    return value


def settle(value):
    return value.apply(EndOfDaySettled(value.state.session_id))


def funded(**changes):
    p = fictional_plan(stage=PlanStage.FUNDED, daily_loss_limit=None, drawdown_type=D.STATIC)
    p = replace(p, payouts=replace(p.payouts, enabled=True))
    return PlanRuleEngine(replace(p, **changes))


@pytest.mark.parametrize("kind,pre,post", [(D.STATIC, 800, 800), (D.EOD_TRAILING, 800, 850),
                                         (D.INTRADAY_TRAILING, 850, 850)])
def test_drawdown_timing(kind, pre, post):
    value = engine(drawdown_type=kind)
    value.apply(PnlSettled(Decimal(50)))
    assert value.state.trailing_floor == Decimal(pre)
    settle(value)
    assert value.state.trailing_floor == Decimal(post)


@pytest.mark.parametrize("moves,kind,floor", [(False, D.INTRADAY_TRAILING, 800),
    (True, D.INTRADAY_TRAILING, 850), (False, D.EOD_TRAILING, 800), (True, D.EOD_TRAILING, 850)])
def test_unrealized_floor_basis(moves, kind, floor):
    value = engine(drawdown_type=kind, unrealized_moves_floor=moves)
    value.apply(MarkToMarketUpdated(Decimal(50)))
    if kind is D.EOD_TRAILING:
        assert value.state.trailing_floor == 800
        settle(value)
    assert value.state.trailing_floor == Decimal(floor)
    assert value.state.equity == 1050


def test_eod_losing_day_cannot_lower_floor():
    value = engine()
    value.apply(PnlSettled(Decimal(50))); settle(value)
    value.apply(SessionStarted("fictional-session-2"))
    value.apply(PnlSettled(Decimal(20))); settle(value)
    assert value.state.trailing_floor == 850


@pytest.mark.parametrize("pnl,status", [(Decimal("-149.99"), AccountStatus.ALIVE),
    (Decimal(-150), AccountStatus.BREACHED), (Decimal("-150.01"), AccountStatus.BREACHED)])
def test_daily_loss_boundary(pnl, status):
    value = engine()
    value.apply(PnlSettled(pnl))
    assert value.state.status is status


def test_daily_floor_resets_only_at_new_session():
    value = engine()
    value.apply(PnlSettled(Decimal(40))); settle(value)
    assert value.state.daily_floor == 850
    value.apply(SessionStarted("fictional-session-2"))
    assert value.state.daily_floor == 890


def test_disabled_daily_floor_mirrors_only_drawdown_for_risk():
    value = engine(daily_loss_limit=None)
    value.apply(PnlSettled(Decimal(-180)))
    assert value.state.status is AccountStatus.ALIVE
    assert value.state.daily_floor == value.state.trailing_floor == 800


@pytest.mark.parametrize("touch,status", [(True, AccountStatus.BREACHED), (False, AccountStatus.ALIVE)])
def test_explicit_breach_touch(touch, status):
    value = engine(daily_loss_limit=None, breach_on_touch=touch)
    value.apply(PnlSettled(Decimal(-200)))
    assert value.state.status is status


def test_target_beyond_and_realized_only_supported_by_engine():
    value = engine(target_on_touch=False, target_uses_unrealized=False)
    value.apply(MarkToMarketUpdated(Decimal(200)))
    assert value.state.status is AccountStatus.ALIVE
    value.apply(PnlSettled(Decimal(100)))
    assert value.state.status is AccountStatus.ALIVE
    value.apply(PnlSettled(Decimal("100.01")))
    assert value.state.status is AccountStatus.PASSED


def test_minimum_days_count_completed_active_sessions_not_boundaries():
    value = engine(minimum_trading_days=2)
    value.apply(PnlSettled(Decimal(100)))
    assert value.state.status is AccountStatus.ALIVE
    settle(value); settle(value)
    assert value.trading_days == 1
    value.apply(SessionStarted("fictional-session-2")); settle(value)
    assert value.trading_days == 1 and value.state.status is AccountStatus.ALIVE
    value.apply(SessionStarted("fictional-session-3")); value.apply(PnlSettled(Decimal(100)))
    settle(value)
    assert value.trading_days == 2 and value.state.status is AccountStatus.PASSED


def test_consistency_exact_boundary_and_completed_days():
    value = engine(consistency_fraction=Decimal("0.5"), minimum_trading_days=2)
    value.apply(PnlSettled(Decimal(100))); settle(value)
    assert value.state.status is AccountStatus.ALIVE
    value.apply(SessionStarted("fictional-session-2"))
    value.apply(PnlSettled(Decimal(200))); settle(value)
    assert value.best_day == 100 and value.state.status is AccountStatus.PASSED


def test_consistency_uses_net_profit_after_losing_day():
    value = engine(consistency_fraction=Decimal("0.5"))
    value.apply(PnlSettled(Decimal(80))); settle(value)
    value.apply(SessionStarted("fictional-session-2"))
    value.apply(PnlSettled(Decimal(60))); settle(value)
    value.apply(SessionStarted("fictional-session-3"))
    value.apply(PnlSettled(Decimal(150)))
    assert value.state.status is AccountStatus.ALIVE  # best 90 > half total 150


@pytest.mark.parametrize("pnl", [Decimal(100), Decimal(-200)])
def test_terminal_states_cannot_mutate(pnl):
    value = engine(daily_loss_limit=None)
    value.apply(PnlSettled(pnl)); snapshot = value.snapshot()
    value.apply(PnlSettled(Decimal(0))); value.apply(SessionStarted("later")); settle(value)
    assert value.snapshot() == snapshot


def test_injected_terminal_signal_rejected():
    value = engine()
    with pytest.raises(ValueError): value.apply(TargetReached(Decimal(1100), Decimal(1100)))
    assert value.state.status is AccountStatus.ALIVE


def test_invalid_session_does_not_change_state():
    value = engine(); before = value.snapshot()
    with pytest.raises(ValueError): value.apply(SessionStarted("unsettled-next"))
    with pytest.raises(ValueError): value.apply(EndOfDaySettled("wrong"))
    assert value.snapshot() == before


@pytest.mark.parametrize("effect,floor", [(F.UNCHANGED, 800), (F.LOWER_BY_WITHDRAWAL, 750)])
def test_payout_effect_on_floor_and_cash_split(effect, floor):
    value = funded(payouts=replace(fictional_plan().payouts, enabled=True, floor_effect=effect))
    value.apply(SessionStarted("funded-fictional")); value.apply(PnlSettled(Decimal(100))); settle(value)
    result = value.withdraw(Decimal(50))
    assert result.trader_payment == 40 and result.retained_split == 10
    assert value.state.equity == 1050 and value.state.trailing_floor == floor
    assert value._profit() == 100 and value.withdrawals == 50


def test_payout_ratchet_basis_moves_with_floor_shift_not_just_once():
    value = funded(drawdown_type=D.INTRADAY_TRAILING,
        payouts=replace(fictional_plan().payouts, enabled=True, floor_effect=F.LOWER_BY_WITHDRAWAL))
    value.apply(SessionStarted("first")); value.apply(PnlSettled(Decimal(100))); settle(value)
    value.withdraw(Decimal(50))
    value.apply(SessionStarted("second")); value.apply(PnlSettled(Decimal(50)))
    assert value.state.trailing_floor == 850


def test_payout_caps_lifetime_buffer_and_days():
    value = funded(payouts=replace(fictional_plan().payouts, enabled=True,
        minimum_trading_days=1, per_payout_cap=Decimal(40), lifetime_cap=Decimal(60)))
    value.apply(SessionStarted("one")); value.apply(PnlSettled(Decimal(100)))
    assert "MINIMUM_DAYS" in value.assess_payout().reasons
    settle(value)
    assert value.assess_payout().maximum_gross_withdrawal == 40
    value.withdraw(Decimal(40))
    assert value.assess_payout().maximum_gross_withdrawal == 20
    value.withdraw(Decimal(20))
    assert not value.assess_payout().eligible
    with pytest.raises(ValueError): value.withdraw(Decimal(1))


def test_payout_floor_touch_rejects_without_mutation():
    value = funded(drawdown_type=D.INTRADAY_TRAILING, unrealized_moves_floor=True)
    value.apply(SessionStarted("one")); value.apply(MarkToMarketUpdated(Decimal(250)))
    value.apply(PnlSettled(Decimal(100)))  # floor1050, equity1100
    before = value.snapshot()
    with pytest.raises(ValueError): value.withdraw(Decimal(50))
    assert value.snapshot() == before


def test_payout_no_open_unrealized_or_disabled_or_terminal():
    value = funded(); value.apply(MarkToMarketUpdated(Decimal(100)))
    assert "OPEN_UNREALIZED_PNL" in value.assess_payout().reasons
    assert "PAYOUT_DISABLED" in engine().assess_payout().reasons
    value.apply(PnlSettled(Decimal(-200)))
    assert "TERMINAL_ACCOUNT" in value.assess_payout().reasons


def test_exact_fee_metrics_no_assumed_probability():
    p = fictional_plan()
    m = derive_plan_metrics(p, gross_payout=Decimal(100))
    assert m.target_drawdown_ratio == Fraction(1, 2)
    assert m.cost_per_drawdown == Fraction(1, 4)
    assert m.payout_drawdown_ratio == Fraction(1, 2)
    assert m.break_even_gross_payout == Fraction(125, 2)
    assert m.expected_attempts is m.expected_fees_to_first_pass is None
    m = derive_plan_metrics(p, pass_probability=Decimal("0.25"))
    assert m.expected_attempts == 4 and m.expected_fees_to_first_pass == 80
    m = derive_plan_metrics(replace(p, fees=replace(p.fees, retry_fee=RetryFee.NEW_EVALUATION)),
                            pass_probability=Decimal("0.25"))
    assert m.expected_fees_to_first_pass == 140


def test_repeating_metric_ratios_are_exact_under_low_precision():
    with localcontext() as ctx:
        ctx.prec = 2
        m = derive_plan_metrics(fictional_plan(), pass_probability=Decimal("0.3"))
    assert m.expected_attempts == Fraction(10, 3)
    assert m.expected_fees_to_first_pass == Fraction(220, 3)


@pytest.mark.parametrize("prob", [Decimal(0), Decimal(-1), Decimal("1.01"), Decimal("NaN"), .5, True])
def test_invalid_supplied_probability_rejects(prob):
    with pytest.raises(ValueError): derive_plan_metrics(fictional_plan(), pass_probability=prob)


@pytest.mark.parametrize("change", [dict(starting_balance=1000.0), dict(maximum_drawdown=Decimal(0)),
    dict(profit_target_gain=Decimal("Infinity")), dict(drawdown_type="STATIC"),
    dict(drawdown_type=D.STATIC, unrealized_moves_floor=True), dict(daily_loss_limit=Decimal(0)),
    dict(consistency_fraction=Decimal("1.1")), dict(consistency_fraction=Decimal(0)),
    dict(minimum_trading_days=True), dict(maximum_position_size=0), dict(terminal_pass=False),
    dict(terminal_breach=False), dict(target_on_touch="yes"), dict(stage="FUNDED")])
def test_invalid_or_ambiguous_plan_rejects(change):
    with pytest.raises(ValueError): fictional_plan(**change)


def test_plan_serialization_roundtrip_immutable_and_deterministic():
    p = fictional_plan()
    assert PropPlan.from_json(p.to_json()) == p
    assert PropPlan.from_json(p.to_json()).to_json() == p.to_json()
    with pytest.raises(FrozenInstanceError): p.maximum_drawdown = Decimal(10)


@pytest.mark.parametrize("mutation", ["unknown", "missing", "float", "version", "enum", "nested"])
def test_serialization_fail_closed(mutation):
    data = json.loads(fictional_plan().to_json())
    if mutation == "unknown": data["plan"]["firm"] = "fictional"
    if mutation == "missing": del data["plan"]["breach_on_touch"]
    if mutation == "float": data["plan"]["starting_balance"] = 1000.0
    if mutation == "version": data["version"] = True
    if mutation == "enum": data["plan"]["drawdown_type"] = "unknown"
    if mutation == "nested": data["plan"]["fees"]["discount"] = "1"
    with pytest.raises(ValueError): PropPlan.from_json(json.dumps(data))


def test_duplicate_serialization_fields_reject():
    text = fictional_plan().to_json().replace('"version":1', '"version":1,"version":1')
    with pytest.raises(ValueError): PropPlan.from_json(text)


@pytest.mark.parametrize("kind", list(D))
def test_unchanged_monte_carlo_deterministic_with_fresh_plan_engine(kind):
    p = fictional_plan(drawdown_type=kind)
    safety = RiskPolicyConfig(10, Decimal("0.5"), Decimal(1))
    evaluate = plan_evaluator(p, safety, ContractLossEstimate(Decimal(10)), PathConfig(2, 2))
    scenario = Scenario("fictional-base", SequenceOutcomeSource((TradeOutcome(Decimal(50)),), repeat=True))
    a = evaluate.evaluate(FixedContracts("fictional-size", 10), scenario, simulations=3, seed=16)
    b = evaluate.evaluate(FixedContracts("fictional-size", 10), scenario, simulations=3, seed=16)
    assert a == b and a.passes == 3 and a.maximum_contracts == 2
    assert a.paths[0].final_state.status is AccountStatus.PASSED


def test_mc_minimum_days_pending_target_is_censored_not_falsely_passed():
    evaluate = plan_evaluator(fictional_plan(minimum_trading_days=2), RiskPolicyConfig(1),
        ContractLossEstimate(Decimal(10)), PathConfig(3, 1))
    report = evaluate.evaluate(FixedContracts("fictional-one", 1),
        Scenario("fictional-base", SequenceOutcomeSource((TradeOutcome(Decimal(100)),), repeat=True)),
        simulations=2, seed=16)
    assert report.passes == 0 and report.censored == 2 and report.pass_probability == 0
    assert all(p.end is PathEnd.HORIZON and p.trades == 1 for p in report.paths)


@pytest.mark.parametrize("changes", [dict(stage=PlanStage.FUNDED), dict(breach_on_touch=False),
    dict(target_on_touch=False), dict(target_uses_unrealized=False)])
def test_mc_rejects_incompatible_semantics_instead_of_rewriting(changes):
    with pytest.raises(ValueError): plan_evaluator(fictional_plan(**changes), RiskPolicyConfig(1), ContractLossEstimate(Decimal(10)))


@pytest.mark.parametrize("bad", [1.0, Decimal("NaN"), Decimal("Infinity")])
def test_invalid_pnl_rejects_before_state_mutation(bad):
    value = engine(); before = value.snapshot()
    with pytest.raises(ValueError): value.apply(PnlSettled(bad))
    assert value.snapshot() == before


def test_default_engine_restoration_cannot_drop_plan_policies():
    with pytest.raises(ValueError): PlanRuleEngine.from_snapshot(engine().snapshot())


def test_engine_plan_cannot_be_swapped_after_initialization():
    value = engine()
    with pytest.raises(AttributeError): value.plan = fictional_plan()


def test_plan_money_updates_ignore_low_ambient_precision():
    with localcontext() as ctx:
        ctx.prec = 2
        value = engine()
        value.apply(PnlSettled(Decimal("0.123456")))
    assert value.state.equity == Decimal("1000.123456")


def test_unsupported_precision_rolls_back_all_day_state():
    value = engine(); before = value.snapshot()
    with pytest.raises(ArithmeticError): value.apply(PnlSettled(Decimal("1e-200")))
    assert value.snapshot() == before
