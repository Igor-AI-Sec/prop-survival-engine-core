"""Fictional rule/time fixtures only. Catalog evidence is read without mutation."""
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction
import json
from pathlib import Path

import pytest

from tests.test_prop_plans import fictional_plan
from tests.test_challenge_simulation import run, profile, CONTEXT, external, BASE, SAFETY
from prop_survival_engine.prop_rules.plans import (
    PropPlan, DrawdownType, DailyLossAction, ConsistencyMode,
)
from prop_survival_engine.prop_rules.plan_engine import PlanRuleEngine
from prop_survival_engine.prop_rules.models import (
    AccountStatus, SessionStarted, EndOfDaySettled, PnlSettled, MarkToMarketUpdated,
    PositionInventoryUpdated, ClockAdvanced, DailyLossPaused, TrailingDrawdownBreached,
    TradingRestrictionBreached,
)
from prop_survival_engine.prop_rules.calendar import (
    TradingCalendar, SessionWindow, BlackoutWindow, HoldingAction, SimulationTimeline,
)
from prop_survival_engine.prop_rules.provenance import PlanConfiguration, PlanOrigin, VerificationStatus
from prop_survival_engine.prop_rules.operations import reviewed_challenge_semantics_report
from prop_survival_engine.prop_rules.catalog import load_catalog, Operation
from prop_survival_engine.risk.features import SessionState, derive_ruin_distances
from prop_survival_engine.risk.policy import RiskPolicy, ContractLossEstimate
from prop_survival_engine.core.models import OrderIntent, Side
from prop_survival_engine.simulation.challenge import ChallengePlan, experiment_matrix

D = Decimal
T = datetime(2026, 10, 7, 9, tzinfo=timezone.utc)


def engine(**changes):
    p = fictional_plan(**changes)
    e = PlanRuleEngine(p)
    if p.calendar_json is not None:
        e.apply(PositionInventoryUpdated(0))  # Explicit fictional fresh-flat evidence.
        e.apply(ClockAdvanced(T))
    e.apply(SessionStarted("fictional day 1"))
    return e


def next_day(e, name="fictional day 2"):
    e.apply(EndOfDaySettled(e.state.session_id))
    e.apply(SessionStarted(name))


def calendar(action=HoldingAction.REQUIRE_FLAT, blackouts=(), weekends=False):
    return TradingCalendar((SessionWindow(T, T + timedelta(hours=1), T + timedelta(minutes=55)),),
        blackouts, True, weekends, action)


def named(**changes):
    return ChallengePlan("explicitly fictional richer challenge", PlanConfiguration(fictional_plan(
        maximum_position_size=100, **changes), PlanOrigin.FICTIONAL))


@pytest.mark.parametrize("kind", [DrawdownType.EOD_TRAILING, DrawdownType.INTRADAY_TRAILING])
@pytest.mark.parametrize("cap", ["800", "1000", "1050"])
def test_capped_floor_monotone_never_exceeds_lock(kind, cap):
    e = engine(drawdown_type=kind, trailing_floor_cap=D(cap), profit_target_gain=D(10000), daily_loss_limit=None)
    previous = e.state.trailing_floor
    for index, profit in enumerate([50, 100, 200, 300, 500, 450, 600]):
        e.apply(PnlSettled(D(profit)))
        e.apply(EndOfDaySettled(e.state.session_id))
        assert previous <= e.state.trailing_floor <= D(cap)
        previous = e.state.trailing_floor
        e.apply(SessionStarted("fictional cap day " + str(index)))
    assert e.state.trailing_floor == D(cap)


@pytest.mark.parametrize("cap", [D(799), D(-1), D("NaN"), 1000.0])
def test_invalid_cap_rejects(cap):
    with pytest.raises(ValueError): fictional_plan(trailing_floor_cap=cap)


def test_static_cannot_gain_trailing_cap():
    with pytest.raises(ValueError): fictional_plan(drawdown_type=DrawdownType.STATIC, trailing_floor_cap=D(1000))


@pytest.mark.parametrize("pnl,paused", [("-49.99", False), ("-50", True), ("-50.01", True)])
def test_soft_dll_boundaries_pause_not_failure(pnl, paused):
    e = engine(daily_loss_limit=D(50), daily_loss_action=DailyLossAction.SOFT_PAUSE)
    signals = e.apply(PnlSettled(D(pnl)))
    assert e.state.status is AccountStatus.ALIVE and e.paused is paused
    assert any(type(s) is DailyLossPaused for s in signals) is paused
    assert e.risk_context().session_state is (SessionState.PAUSED if paused else SessionState.OPEN)
    assert e.risk_context().active_floors.daily is False


def test_soft_pause_latches_and_next_valid_session_resets():
    e = engine(daily_loss_limit=D(50), daily_loss_action=DailyLossAction.SOFT_PAUSE)
    e.apply(PnlSettled(D(-50)))
    e.apply(PnlSettled(D(-10)))  # Existing exposure can recover; new entry still blocked.
    assert e.paused and not e.eligibility().can_open
    approval = RiskPolicy(SAFETY).evaluate(OrderIntent("MES", Side.BUY, 1), context=e.risk_context(), estimate=ContractLossEstimate(D(1)))
    assert approval.approved_contracts == 0
    with pytest.raises(ValueError): e.before_trade()
    e.apply(SessionStarted(e.state.session_id))
    assert e.paused
    with pytest.raises(ValueError): e.apply(SessionStarted("unsettled new session"))
    assert e.paused
    next_day(e)
    assert not e.paused and e.eligibility().can_open
    assert e.state.daily_floor == D(940)


def test_hard_breach_wins_over_soft_pause_and_never_revives():
    e = engine(daily_loss_limit=D(50), daily_loss_action=DailyLossAction.SOFT_PAUSE)
    signals = e.apply(PnlSettled(D(-200)))
    assert e.state.status is AccountStatus.BREACHED
    assert any(type(s) is TrailingDrawdownBreached for s in signals)
    assert not any(type(s) is DailyLossPaused for s in signals)
    before = e.snapshot()
    e.apply(SessionStarted("after terminal")); e.apply(PnlSettled(D(10000)))
    e.apply(PositionInventoryUpdated(0)); e.apply(ClockAdvanced(T))
    assert e.snapshot() == before


def test_soft_limit_is_not_an_active_ruin_floor():
    e = engine(daily_loss_limit=D(50), daily_loss_action=DailyLossAction.SOFT_PAUSE)
    assert derive_ruin_distances(e.risk_context()).effective_ruin_distance == D(200)
    e.apply(PnlSettled(D(-50)))
    assert derive_ruin_distances(e.risk_context()).effective_ruin_distance == D(150)


@pytest.mark.parametrize("fraction,expected", [("0.5", Fraction(200)), ("0.3", Fraction(1000, 3)), ("0.4", Fraction(250))])
def test_consistency_target_extension_is_exact_never_failure(fraction, expected):
    e = engine(consistency_fraction=D(fraction), consistency_mode=ConsistencyMode.EXTEND_TARGET, daily_loss_limit=None)
    e.apply(PnlSettled(D(100)))
    assert e.state.status is AccountStatus.ALIVE
    assert e.base_target == D(1100) and e.required_profit == expected
    assert e.effective_target == Fraction(1000) + expected
    assert e.best_qualifying_day == D(100)
    assert e.target_extension == expected - 100
    assert Fraction(e.state.target_equity) == e.effective_target
    assert derive_ruin_distances(e.risk_context()).distance_to_target == expected - 100


def test_later_day_can_satisfy_extended_target_exactly():
    e = engine(consistency_fraction=D("0.5"), consistency_mode=ConsistencyMode.EXTEND_TARGET, daily_loss_limit=None)
    e.apply(PnlSettled(D(100)))
    next_day(e)
    e.apply(PnlSettled(D("199.99")))
    assert e.state.status is AccountStatus.ALIVE and e.required_profit == 200
    e.apply(PnlSettled(D(200)))
    assert e.state.status is AccountStatus.PASSED
    assert e.state.target_equity == D(1200)
    before = e.snapshot()
    e.apply(PnlSettled(D(0))); e.apply(EndOfDaySettled(e.state.session_id))
    assert e.snapshot() == before


def test_effective_target_does_not_fall_after_losing_money():
    e = engine(consistency_fraction=D("0.3"), consistency_mode=ConsistencyMode.EXTEND_TARGET,
               daily_loss_limit=None, trailing_floor_cap=D(1000))
    e.apply(PnlSettled(D(100)))
    target = e.effective_target
    e.apply(PnlSettled(D(50)))
    assert e.effective_target == target
    next_day(e)
    e.apply(PnlSettled(D(120)))
    assert e.effective_target == target


def test_extension_deterministic_under_low_decimal_context():
    def execute():
        e = engine(consistency_fraction=D("0.3"), consistency_mode=ConsistencyMode.EXTEND_TARGET, daily_loss_limit=None)
        e.apply(PnlSettled(D("100.01"))); next_day(e)
        e.apply(PnlSettled(D("200.02")))
        return e.snapshot()
    expected = execute()
    with localcontext() as context:
        context.prec = 2
        assert execute() == expected


def test_closed_target_requires_positive_flat_evidence_even_zero_unrealized():
    e = engine(target_uses_unrealized=False, target_requires_flat=True, daily_loss_limit=None)
    assert e.risk_context().session_state is SessionState.UNKNOWN
    e.apply(PositionInventoryUpdated(1)); e.apply(MarkToMarketUpdated(D(100)))
    assert e.state.equity == 1100 and e.state.status is AccountStatus.ALIVE
    e.apply(PnlSettled(D(100)))
    assert e.state.status is AccountStatus.ALIVE  # unrealized zero alone is not flat proof.
    e.apply(PositionInventoryUpdated(0))
    assert e.state.status is AccountStatus.PASSED


def test_closed_target_breach_wins_same_settlement_event():
    e = engine(target_uses_unrealized=False, target_requires_flat=True, daily_loss_limit=None,
               drawdown_type=DrawdownType.INTRADAY_TRAILING, unrealized_moves_floor=True)
    e.apply(PositionInventoryUpdated(1)); e.apply(MarkToMarketUpdated(D(500)))
    assert e.state.trailing_floor == 1300 and e.state.status is AccountStatus.ALIVE
    e.apply(PositionInventoryUpdated(0))
    signals = e.apply(PnlSettled(D(100)))
    assert e.state.status is AccountStatus.BREACHED
    assert any(type(s) is TrailingDrawdownBreached for s in signals)


@pytest.mark.parametrize("minutes,allowed,flat", [(-1, False, True), (0, True, False), (54, True, False),
    (55, False, True), (60, False, True)])
def test_calendar_half_open_windows_and_exact_flat_deadline(minutes, allowed, flat):
    value = calendar().eligibility(T + timedelta(minutes=minutes))
    assert value.can_open is allowed and value.must_flatten is flat


@pytest.mark.parametrize("action,flat,breach", [(HoldingAction.BLOCK_NEW, False, False),
    (HoldingAction.REQUIRE_FLAT, True, False), (HoldingAction.BREACH, True, True)])
def test_named_blackout_holding_consequences_are_explicit(action, flat, breach):
    news = BlackoutWindow("fictional externally supplied event", T + timedelta(minutes=10), T + timedelta(minutes=20), action)
    value = calendar(blackouts=(news,)).eligibility(T + timedelta(minutes=10))
    assert not value.can_open and value.must_flatten is flat and value.breaches_if_holding is breach
    assert calendar(blackouts=(news,)).eligibility(T + timedelta(minutes=20)).can_open


@pytest.mark.parametrize("action,status", [(HoldingAction.REQUIRE_FLAT, AccountStatus.ALIVE), (HoldingAction.BREACH, AccountStatus.BREACHED)])
def test_rule_engine_clock_exposes_flat_request_or_terminal_breach(action, status):
    e = engine(calendar_json=calendar(action).to_json(), target_requires_flat=True)
    e.apply(PositionInventoryUpdated(0)); e.apply(ClockAdvanced(T))
    e.apply(PositionInventoryUpdated(1)); signals = e.apply(ClockAdvanced(T + timedelta(minutes=55)))
    assert not e.eligibility().can_open and e.eligibility().must_flatten
    assert e.state.status is status
    assert any(type(s) is TradingRestrictionBreached for s in signals) is (status is AccountStatus.BREACHED)
    assert e.open_positions == 1  # A flat request does not fabricate a fill.


def test_no_overnight_boundary_cannot_hide_open_inventory():
    value = calendar()
    later = SessionWindow(T + timedelta(days=1), T + timedelta(days=1, hours=1), T + timedelta(days=1, minutes=55))
    value = replace(value, sessions=value.sessions + (later,))
    e = engine(calendar_json=value.to_json())
    e.apply(ClockAdvanced(T)); e.apply(PositionInventoryUpdated(1))
    e.apply(EndOfDaySettled(e.state.session_id))
    e.apply(ClockAdvanced(T + timedelta(days=1)))
    with pytest.raises(ValueError, match="flat inventory"): e.apply(SessionStarted("fictional next session"))
    e.apply(PositionInventoryUpdated(0)); e.apply(SessionStarted("fictional next session"))


def test_missing_clock_backward_clock_unknown_inventory_fail_closed():
    e = PlanRuleEngine(fictional_plan(calendar_json=calendar().to_json()))
    assert not e.eligibility().can_open and "MISSING_CLOCK" in e.eligibility().reasons
    e.apply(ClockAdvanced(T)); before = e.snapshot()
    with pytest.raises(ValueError): e.apply(ClockAdvanced(T - timedelta(seconds=1)))
    assert e.snapshot() == before
    with pytest.raises(ValueError): e.apply(ClockAdvanced(T.replace(tzinfo=None)))
    with pytest.raises(ValueError): e.apply(PositionInventoryUpdated(True))


def test_weekend_restriction_and_schedule_roundtrip():
    saturday = datetime(2026, 10, 10, 9, tzinfo=timezone.utc)
    value = TradingCalendar((SessionWindow(saturday, saturday + timedelta(hours=1), saturday + timedelta(minutes=55)),),
        (), True, False, HoldingAction.REQUIRE_FLAT)
    assert not value.eligibility(saturday).can_open
    assert replace(value, weekends_allowed=True).eligibility(saturday).can_open
    assert TradingCalendar.from_json(value.to_json()) == value


@pytest.mark.parametrize("changes", [dict(daily_loss_action="SOFT_PAUSE"),
    dict(consistency_mode="EXTEND_TARGET"), dict(target_requires_flat=1),
    dict(daily_loss_limit=None, daily_loss_action=DailyLossAction.SOFT_PAUSE),
    dict(consistency_mode=ConsistencyMode.EXTEND_TARGET, consistency_fraction=None), dict(calendar_json="{}")])
def test_invalid_or_ambiguous_semantic_configuration_rejects(changes):
    with pytest.raises(ValueError): fictional_plan(**changes)


def test_extended_plan_serialization_and_provenance_preserve_new_rules():
    p = fictional_plan(trailing_floor_cap=D(1000), daily_loss_action=DailyLossAction.SOFT_PAUSE,
        consistency_mode=ConsistencyMode.EXTEND_TARGET, consistency_fraction=D("0.5"),
        target_requires_flat=True, target_uses_unrealized=False, calendar_json=calendar().to_json())
    assert PropPlan.from_json(p.to_json()) == p
    config = external(trailing_floor_cap=D(1000), calendar_json=calendar().to_json())
    assert config.verify(CONTEXT).usable
    assert PlanConfiguration.from_json(config.to_json()) == config
    absent = replace(config, rules=tuple(r for r in config.rules if r.rule_path != "calendar_json"))
    assert not absent.verify(CONTEXT).usable


def test_legacy_serialization_defaults_do_not_verify_new_external_leaves():
    p = fictional_plan()
    payload = json.loads(p.to_json())
    keys = ("trailing_floor_cap", "daily_loss_action", "consistency_mode", "target_requires_flat", "calendar_json")
    for key in keys:
        del payload["plan"][key]
    assert PropPlan.from_json(json.dumps(payload)) == p
    payload["plan"]["target_requires_flat"] = False
    with pytest.raises(ValueError): PropPlan.from_json(json.dumps(payload))


def test_fictional_soft_dll_monte_carlo_pauses_then_recovers_next_session():
    p = named(daily_loss_limit=D(50), daily_loss_action=DailyLossAction.SOFT_PAUSE,
        drawdown_type=DrawdownType.STATIC)
    strategy = profile(("-40", "120"), repeat_sequence=False, opportunities_per_session=3)
    a, b = run(p, strategy=strategy, max_sessions=2), run(p, strategy=strategy, max_sessions=2)
    assert a == b and a.report.passes == 4 and a.report.breaches == 0
    assert all(path.trades_taken == 2 and path.sessions_started == 2 for path in a.paths)
    assert all(path.minimum_ruin_cushion == 150 for path in a.paths)


def test_fictional_extended_target_monte_carlo_uses_engine_not_shortcuts():
    p = named(consistency_mode=ConsistencyMode.EXTEND_TARGET, consistency_fraction=D("0.5"),
        daily_loss_limit=None)
    strategy = profile(("80",), opportunities_per_session=1)
    a, b = run(p, strategy=strategy, max_sessions=2), run(p, strategy=strategy, max_sessions=2)
    assert a == b and a.report.passes == 4
    assert all(path.final_state.target_equity == 1200 for path in a.report.paths)


def test_fictional_capped_and_closed_target_can_simulate_end_to_end():
    p = named(trailing_floor_cap=D(1000), target_requires_flat=True, target_uses_unrealized=False)
    cell = run(p, strategy=profile(("80",)))
    assert cell.report.passes == 4
    assert all(path.trace[-1].event == "PositionInventoryUpdated" for path in cell.report.paths)


def test_calendar_simulation_requires_explicit_complete_timeline():
    p = named(calendar_json=calendar().to_json(), target_requires_flat=True, target_uses_unrealized=False)
    assert run(p).status == "BLOCKED"
    p = replace(p, timeline=SimulationTimeline(((T, T + timedelta(minutes=1)),)))
    with pytest.raises(ValueError, match="complete explicit"): run(p)
    strategy = profile(("80",), opportunities_per_session=2)
    a = run(p, strategy=strategy, max_sessions=1)
    b = run(p, strategy=strategy, max_sessions=1)
    assert a == b and a.report.passes == 4
    assert any(s.event == "ClockAdvanced" for s in a.report.paths[0].trace)


def test_calendar_blocked_opportunities_create_no_exposure_and_remain_censored():
    p = named(calendar_json=calendar().to_json())
    p = replace(p, timeline=SimulationTimeline(((T + timedelta(minutes=55), T + timedelta(minutes=56)),)))
    cell = run(p, max_sessions=1)
    assert cell.report.censored == 4 and cell.report.average_contracts == 0
    assert all(path.final_equity == 1000 for path in cell.paths)


def test_representational_reports_remove_only_model_unsupported_not_evidence():
    data = Path(__file__).resolve().parents[1] / "config/prop_plans/fictional-catalog.json"
    before_bytes = data.read_bytes()
    for p in load_catalog(data):
        original = p.qualify(Operation.CHALLENGE, CONTEXT)
        report = reviewed_challenge_semantics_report(p, CONTEXT)
        before_evidence = {k: r for k, r in original.blockers if r != ("MODEL_UNSUPPORTED",)}
        assert all(dict(report.blockers)[k] == r for k, r in before_evidence.items())
        assert not report.admitted and not p.qualify(Operation.CHALLENGE, CONTEXT).admitted
        assert any("VERIFIED_SEMANTIC_BINDING_REQUIRED" in reasons for _, reasons in report.blockers)
    assert data.read_bytes() == before_bytes


def test_capabilities_require_verified_correct_binding_not_boolean_or_fictional_plan():
    p = load_catalog(Path(__file__).resolve().parents[1] / "config/prop_plans/fictional-catalog.json")[0]
    with pytest.raises(ValueError): reviewed_challenge_semantics_report(p, CONTEXT, binding=True)
    fictional = named(trailing_floor_cap=D(1000)).configuration
    report = reviewed_challenge_semantics_report(p, CONTEXT, binding=fictional)
    assert ("plan_binding", ("CHALLENGE_BINDING_NOT_VERIFIED",)) in report.blockers
    verified = external(trailing_floor_cap=D(1000), daily_loss_action=DailyLossAction.SOFT_PAUSE,
                        calendar_json=calendar().to_json())
    report = reviewed_challenge_semantics_report(p, CONTEXT, binding=verified,
        timeline=SimulationTimeline(((T,),)))
    assert "capped_trailing_floor" not in dict(report.blockers)
    assert "soft_daily_pause" not in dict(report.blockers)
    assert "Unrealized breaches" in dict(report.blockers) and not report.admitted
    absent = replace(verified, rules=tuple(r for r in verified.rules if r.rule_path != "trailing_floor_cap"))
    assert "capped_trailing_floor" in dict(reviewed_challenge_semantics_report(p, CONTEXT, binding=absent).blockers)


def test_same_calendar_window_cannot_reset_a_pause_by_changing_session_label():
    e = engine(calendar_json=calendar().to_json(), daily_loss_limit=D(50), daily_loss_action=DailyLossAction.SOFT_PAUSE)
    e.apply(PnlSettled(D(-50))); e.apply(EndOfDaySettled(e.state.session_id))
    before = e.snapshot()
    with pytest.raises(ValueError, match="fresh explicit calendar"): e.apply(SessionStarted("fake second day"))
    assert e.snapshot() == before and e.paused


@pytest.mark.parametrize("action,status", [(HoldingAction.REQUIRE_FLAT, AccountStatus.ALIVE), (HoldingAction.BREACH, AccountStatus.BREACHED)])
def test_jumping_past_blackout_cannot_hide_holding_violation(action, status):
    news = BlackoutWindow("fictional clock gap", T + timedelta(minutes=10), T + timedelta(minutes=20), action)
    e = engine(calendar_json=calendar(blackouts=(news,)).to_json())
    e.apply(PositionInventoryUpdated(1)); e.apply(ClockAdvanced(T + timedelta(minutes=30)))
    assert e.state.status is status and e.eligibility().must_flatten and not e.eligibility().can_open
    if status is AccountStatus.ALIVE:
        e.apply(PositionInventoryUpdated(0))
        assert e.eligibility().can_open


def test_allowed_only_session_can_have_no_flat_deadline_without_fabricated_overnight_breach():
    value = TradingCalendar((SessionWindow(T, T + timedelta(hours=1), None),), (), False, True, HoldingAction.BLOCK_NEW)
    assert not value.eligibility(T + timedelta(hours=2)).can_open
    assert not value.eligibility(T + timedelta(hours=2)).must_flatten
    assert TradingCalendar.from_json(value.to_json()) == value


def test_explicit_deadline_requires_flat_even_if_outside_window_only_blocks_new():
    value = TradingCalendar((SessionWindow(T, T + timedelta(hours=1), T + timedelta(minutes=50)),), (), False, True, HoldingAction.BLOCK_NEW)
    assert value.eligibility(T + timedelta(minutes=50)).must_flatten


@pytest.mark.parametrize("mutation", ["unknown", "missing", "naive", "backward", "duplicate"])
def test_calendar_serialization_fails_closed(mutation):
    text = calendar().to_json()
    value = json.loads(text)
    if mutation == "unknown": value["provider"] = "fictional"
    if mutation == "missing": del value["no_overnight"]
    if mutation == "naive": value["sessions"][0]["start"] = T.replace(tzinfo=None).isoformat()
    if mutation == "backward": value["sessions"][0]["end"] = (T - timedelta(seconds=1)).isoformat()
    if mutation == "duplicate":
        text = text.replace('"version":1', '"version":1,"version":1')
    else:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ValueError): TradingCalendar.from_json(text)


def test_realized_target_can_breach_before_pass_on_open_loss():
    e = engine(target_uses_unrealized=False, target_requires_flat=True, daily_loss_limit=None)
    e.apply(PositionInventoryUpdated(1)); e.apply(MarkToMarketUpdated(D(-200)))
    assert e.state.status is AccountStatus.BREACHED
    before = e.snapshot()
    e.apply(PnlSettled(D(100))); e.apply(PositionInventoryUpdated(0))
    assert e.snapshot() == before


def test_nonterminating_rational_target_survives_complete_monte_carlo():
    p = named(consistency_mode=ConsistencyMode.EXTEND_TARGET, consistency_fraction=D("0.3"), daily_loss_limit=None)
    strategy = profile(("80",), opportunities_per_session=1)
    a, b = run(p, strategy=strategy, max_sessions=4), run(p, strategy=strategy, max_sessions=4)
    assert a == b and a.report.passes == 4
    assert all(path.final_state.target_equity == Fraction(4000, 3) for path in a.report.paths)
    assert all(path.trades_taken == 4 for path in a.paths)


def test_capped_floor_monte_carlo_keeps_locked_floor_and_censoring():
    p = named(trailing_floor_cap=D(1000), profit_target_gain=D(10000), daily_loss_limit=None)
    strategy = profile(("40",), opportunities_per_session=2)
    a, b = run(p, strategy=strategy), run(p, strategy=strategy)
    assert a == b and a.report.censored == 4
    assert all(path.final_state.trailing_floor == 1000 for path in a.report.paths)
    assert all(point.state.trailing_floor <= 1000 for path in a.report.paths for point in path.trace)


def test_real_blocked_matrix_cells_remain_present_with_richer_fictional_cells():
    real = load_catalog(Path(__file__).resolve().parents[1] / "config/prop_plans/fictional-catalog.json")
    plans = (named(daily_loss_limit=D(50), daily_loss_action=DailyLossAction.SOFT_PAUSE),) + tuple(ChallengePlan(p.name, p) for p in real)
    cells = experiment_matrix(plans, ("MES",), (profile(),), (BASE,), (42,),
        context=CONTEXT, safety_config=SAFETY, simulations=2, max_sessions=1)
    assert len(cells) == 6 and sum(c.status == "BLOCKED" for c in cells) == 5


def test_external_new_semantic_leaf_ambiguity_blocks_even_representable_plan():
    config = external(trailing_floor_cap=D(1000))
    config = replace(config, rules=tuple(replace(r, status=VerificationStatus.AMBIGUOUS)
        if r.rule_path == "trailing_floor_cap" else r for r in config.rules))
    cell = run(ChallengePlan("fictional ambiguous lock", config))
    assert cell.status == "BLOCKED" and ("trailing_floor_cap", ("AMBIGUOUS",)) in cell.blockers


def test_late_inventory_after_deadline_stays_gated_when_next_window_opens():
    value = calendar()
    next_window = SessionWindow(T + timedelta(days=1), T + timedelta(days=1, hours=1), None)
    e = engine(calendar_json=replace(value, sessions=value.sessions + (next_window,)).to_json())
    e.apply(ClockAdvanced(T + timedelta(minutes=56)))
    e.apply(PositionInventoryUpdated(1))  # Inventory reported after the deadline.
    e.apply(ClockAdvanced(T + timedelta(days=1)))
    assert e.eligibility().must_flatten and not e.eligibility().can_open
    e.apply(PositionInventoryUpdated(0))
    assert e.eligibility().can_open


def test_zero_count_with_unsettled_unrealized_pnl_is_not_sequential_flat_state():
    e = engine(target_requires_flat=True, profit_target_gain=D(10000))
    e.apply(PositionInventoryUpdated(0)); e.apply(MarkToMarketUpdated(D(10)))
    with pytest.raises(ValueError, match="proven flat"): e.before_trade()
