"""Fictional mathematical profiles/plans only; sparse fictional catalog records tested as blockers."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from tests.test_prop_plans import fictional_plan
from prop_survival_engine.core.instruments import INSTRUMENTS
from prop_survival_engine.core.models import OrderIntent, Side
from prop_survival_engine.prop_rules.catalog import load_catalog, Operation
from prop_survival_engine.prop_rules.plans import DrawdownType, PlanStage
from prop_survival_engine.prop_rules.provenance import (
    PlanConfiguration, PlanOrigin, VerificationContext, RuleSource, SourceType,
    RuleProvenance, VerificationStatus, rule_values,
)
from prop_survival_engine.risk.instruments import (
    StopDistance, StopUnit, ExecutionCostAssumptions, instrument_loss,
)
from prop_survival_engine.risk.contract_limits import ContractLimits, InstrumentContractLimit
from prop_survival_engine.risk.policy import RiskPolicyConfig, RiskPolicy
from prop_survival_engine.risk.features import RiskContext, SessionState
from prop_survival_engine.oms.models import OmsState
from prop_survival_engine.prop_rules.models import AccountStatus
from prop_survival_engine.simulation.sizing import FixedContracts, FractionOfCushion, CushionLadder
from prop_survival_engine.simulation.challenge import (
    StrategyProfile, StopMultipleOutcome, Sampling, ChallengePlan, CostScenario,
    challenge_blockers, simulate_challenge, experiment_matrix, format_experiment,
    rank_challenges, ChallengeEnd, attempt_economics,
)
from prop_survival_engine.simulation.outcomes import TradeOutcome
from prop_survival_engine.simulation.reach_avoid import PathEnd, ReachAvoidEvaluator

D = Decimal
CONTEXT = VerificationContext(date(2026, 10, 7))
ZERO = ExecutionCostAssumptions(D(0), D(0), D(0))
BASE = CostScenario("base", ZERO)
SAFETY = RiskPolicyConfig(100, D("0.25"), D(1))


def plan(**changes):
    return ChallengePlan("explicitly fictional challenge", PlanConfiguration(
        fictional_plan(maximum_position_size=100, **changes), PlanOrigin.FICTIONAL))


def profile(multiples=("2", "-1"), **changes):
    value = StrategyProfile("fictional price-space assumptions", True,
        StopDistance(D(1), StopUnit.TICKS), tuple(StopMultipleOutcome(D(v)) for v in multiples),
        FixedContracts("fictional fixed request", 1), 2, Sampling.SEQUENCE,
        (1,) * len(multiples), Fraction(0), True)
    return replace(value, **changes)


def run(named=None, symbol="MES", strategy=None, scenario=BASE, **changes):
    options = dict(context=CONTEXT, safety_config=SAFETY, simulations=4, seed=42, max_sessions=3)
    options.update(changes)
    return simulate_challenge(named or plan(), symbol, strategy or profile(), scenario, **options)


def external(**changes):
    p = fictional_plan(maximum_position_size=100, **changes)
    entries = tuple(RuleProvenance(key, VerificationStatus.VERIFIED,
        (RuleSource("offline fictional official-evidence fixture", SourceType.OFFICIAL,
            CONTEXT.as_of, None, value),)) for key, value in rule_values(p).items())
    return PlanConfiguration(p, PlanOrigin.EXTERNAL, entries)


@pytest.mark.parametrize("kind", list(DrawdownType))
def test_fictional_plans_end_to_end_use_existing_rule_engine(kind):
    cell = run(plan(drawdown_type=kind), "ES", profile(("8",)))
    assert cell.status == "SIMULATED" and cell.report.passes == 4
    assert all(p.final_state.status is AccountStatus.PASSED for p in cell.report.paths)
    assert all(p.status is ChallengeEnd.PASS and p.trades_taken == 1 for p in cell.paths)
    assert cell.mean_sessions_to_pass == cell.mean_trades_to_pass == 1


@pytest.mark.parametrize("index", range(5))
def test_reviewed_real_plans_never_enter_monte_carlo_and_exact_blockers(index, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    reviewed = load_catalog(root / "config/prop_plans/fictional-catalog.json")[index]
    expected = reviewed.qualify(Operation.CHALLENGE, CONTEXT).blockers
    assert expected
    monkeypatch.setattr(ReachAvoidEvaluator, "evaluate", lambda *a, **k: pytest.fail("blocked plan reached MC"))
    cell = run(ChallengePlan(reviewed.name, reviewed))
    assert cell.status == "BLOCKED" and cell.blockers == expected and cell.report is None


@pytest.mark.parametrize("missing", ["payouts.floor_effect", "payouts.profit_split", "fees.evaluation", "fees.reset"])
def test_non_challenge_missing_evidence_does_not_block_probability(missing):
    config = external()
    config = replace(config, rules=tuple(r for r in config.rules if r.rule_path != missing))
    cell = run(ChallengePlan("fictional external-evidence fixture", config), "ES", profile(("8",)))
    assert cell.status == "SIMULATED"
    assert cell.economics.status == ("BLOCKED" if missing.startswith("fees.") else "AVAILABLE")


@pytest.mark.parametrize("key", ["unrealized_moves_floor", "consistency_fraction", "minimum_trading_days", "drawdown_type"])
def test_unresolved_challenge_semantics_blocks(key):
    config = external()
    config = replace(config, rules=tuple(replace(r, status=VerificationStatus.AMBIGUOUS)
        if r.rule_path == key else r for r in config.rules))
    cell = run(ChallengePlan("fictional ambiguous semantics", config))
    assert cell.status == "BLOCKED" and (key, ("AMBIGUOUS",)) in cell.blockers


def test_soft_dll_or_unrepresentable_consistency_remain_blocked():
    reviewed = load_catalog(Path(__file__).resolve().parents[1] / "config/prop_plans/fictional-catalog.json")[0]
    reviewed = replace(reviewed, model_blockers=((Operation.CHALLENGE, ("soft_DLL", "unknown_consistency")),))
    blockers = run(ChallengePlan("fictional unsupported-rule fixture", reviewed)).blockers
    assert ("soft_DLL", ("MODEL_UNSUPPORTED",)) in blockers
    assert ("unknown_consistency", ("MODEL_UNSUPPORTED",)) in blockers


@pytest.mark.parametrize("changes", [dict(breach_on_touch=False), dict(target_on_touch=False),
    dict(target_uses_unrealized=False), dict(stage=PlanStage.FUNDED)])
def test_representability_fail_closed(changes):
    assert run(plan(**changes)).status == "BLOCKED"


def test_repeat_seed_and_matrix_order_invariance():
    p = profile(sampling=Sampling.IID, no_trade_probability=Fraction(1, 3))
    assert run(strategy=p) == run(strategy=p)
    plans = (plan(), replace(plan(), name="fictional second plan"))
    scenarios = (BASE, CostScenario("higher cost", ExecutionCostAssumptions(D(1), D(1), D(1))))
    options = dict(context=CONTEXT, safety_config=SAFETY, simulations=3, max_sessions=2)
    a = experiment_matrix(plans, ("ES", "MES"), (p,), scenarios, (1, 2), **options)
    b = experiment_matrix(plans[::-1], ("MES", "ES"), (p,), scenarios[::-1], (2, 1), **options)
    key = lambda c: (c.plan, c.instrument, c.strategy, c.scenario, c.seed)
    assert {key(c): c for c in a} == {key(c): c for c in b}
    assert format_experiment(a) == format_experiment(b)


@pytest.mark.parametrize("mini,micro", [("ES", "MES"), ("NQ", "MNQ")])
def test_same_price_space_profile_preserves_exact_instrument_risk_ratio(mini, micro):
    a, b = run(symbol=mini), run(symbol=micro)
    x, y = a.report.paths[0].risk_points[0], b.report.paths[0].risk_points[0]
    assert x.loss == 10 * y.loss
    assert a.report.paths[0].final_state.equity - D(1000) == 10 * (b.report.paths[0].final_state.equity - D(1000))


@pytest.mark.parametrize("symbol", ["ES", "MES", "NQ", "MNQ"])
def test_wrapper_equals_direct_risk_policy_and_integer_budget(symbol):
    strategy = profile(sizing=FixedContracts("fictional request", 100))
    costs = ExecutionCostAssumptions(D("0.25"), D("0.5"), D("0.1"))
    cell = run(symbol=symbol, strategy=strategy, scenario=CostScenario("base", costs))
    loss = instrument_loss(INSTRUMENTS.lookup(symbol), strategy.stop, costs)
    for path in cell.report.paths:
        for point in path.risk_points:
            direct = RiskPolicy(SAFETY).evaluate(OrderIntent(symbol, Side.BUY, point.requested),
                context=RiskContext(point.state, OmsState.READY, SessionState.OPEN), estimate=loss.policy_estimate())
            assert point.approved == direct.approved_contracts
            assert type(point.approved) is int
            if point.budget is not None:
                assert point.approved * point.loss <= point.budget
            assert point.approved * point.loss < point.state.equity - max(point.state.daily_floor, point.state.trailing_floor)


def test_contract_limits_only_reduce_size():
    strategy = profile(sizing=FixedContracts("fictional request", 100))
    full = run(strategy=strategy)
    capped = run(strategy=strategy, limits=ContractLimits((InstrumentContractLimit("MES", 2),), ()))
    assert capped.report.paths[0].contracts_used[0] <= full.report.paths[0].contracts_used[0]
    assert capped.report.maximum_contracts <= 2


@pytest.mark.parametrize("field", ["round_trip_fees", "adverse_slippage_ticks", "execution_buffer"])
def test_increased_cost_never_increases_size_in_identical_account_state(field):
    sizes = []
    for amount in [D(0), D("0.01"), D(1), D(100)]:
        cell = run(strategy=profile(sizing=FixedContracts("fictional request", 100)),
                   scenario=CostScenario("base", replace(ZERO, **{field: amount})))
        sizes.append(cell.report.paths[0].risk_points[0].approved)
    assert sizes == sorted(sizes, reverse=True)


def test_larger_stop_and_smaller_cushion_cannot_increase_first_size():
    previous = 100
    for stop in [1, 2, 10, 1000000]:
        cell = run(strategy=profile(stop=StopDistance(D(stop), StopUnit.TICKS),
                                   sizing=FixedContracts("fictional request", 100)))
        quantity = cell.report.paths[0].risk_points[0].approved
        assert quantity <= previous
        previous = quantity
    previous = 100
    for cushion in [D(200), D(100), D(50), D("0.01")]:
        cell = run(plan(maximum_drawdown=cushion, daily_loss_limit=None),
                   strategy=profile(sizing=FixedContracts("fictional request", 100)))
        quantity = cell.report.paths[0].risk_points[0].approved
        assert quantity <= previous
        previous = quantity


@pytest.mark.parametrize("mini,micro,dd", [("ES", "MES", "40"), ("NQ", "MNQ", "16")])
def test_micro_granularity_reduces_zero_size_and_unused_budget(mini, micro, dd):
    named = plan(maximum_drawdown=D(dd), daily_loss_limit=None)
    strategy = profile(("0",), sizing=FixedContracts("fictional budget capacity", 100))
    a, b = run(named, mini, strategy), run(named, micro, strategy)
    assert a.report.average_contracts == 0 and b.report.average_contracts > 0
    assert a.paths[0].zero_size_count > b.paths[0].zero_size_count
    assert a.paths[0].granularity_unused_budget > b.paths[0].granularity_unused_budget
    assert a.paths[0].total_risk_used == 0 and a.paths[0].final_equity == D(1000)
    assert b.paths[0].risk_utilization > 0


@pytest.mark.parametrize("dd,expected", [("50", 1), ("49.96", 0)])
def test_budget_exactly_one_contract_and_cent_below(dd, expected):
    cell = run(plan(maximum_drawdown=D(dd), daily_loss_limit=None), "ES", profile(("0",)))
    assert cell.report.paths[0].risk_points[0].approved == expected


def test_exact_ruin_touch_not_admitted_and_one_cent_cushion_no_exposure():
    safety = RiskPolicyConfig(10, D(1), D(0))
    cell = run(plan(maximum_drawdown=D("12.50"), daily_loss_limit=None), "ES", safety_config=safety)
    assert cell.report.paths[0].risk_points[0].approved == 0
    cell = run(plan(maximum_drawdown=D("0.01"), daily_loss_limit=None), "MNQ")
    assert cell.report.breaches == 0 and cell.report.passes == 0
    assert cell.paths[0].final_equity == D(1000) and cell.paths[0].trades_taken == 0


@pytest.mark.parametrize("multiple,status,equity", [("2", ChallengeEnd.PASS, "1100"),
    ("-4", ChallengeEnd.BREACH, "800"), ("-3.9998", ChallengeEnd.CENSORED, "800.01"),
    ("-4.0002", ChallengeEnd.BREACH, "799.99")])
def test_exact_target_floor_and_cent_boundaries_engine_only(multiple, status, equity):
    strategy = profile((multiple,), stop=StopDistance(D(1), StopUnit.POINTS), repeat_sequence=False)
    cell = run(plan(daily_loss_limit=None, drawdown_type=DrawdownType.STATIC), "ES", strategy)
    assert cell.paths[0].status is status and cell.paths[0].final_equity == D(equity)
    assert cell.paths[0].trades_taken == 1
    if status is not ChallengeEnd.CENSORED:
        assert cell.paths[0].first_passage_sessions == 1


def test_terminal_mid_session_stops_immediately_many_wins_losses():
    wins = run(strategy=profile(("100",), opportunities_per_session=50))
    losses = run(strategy=profile(("-1000",), opportunities_per_session=50))
    assert wins.report.passes == losses.report.breaches == 4
    assert all(p.trades_taken == 1 for c in (wins, losses) for p in c.paths)
    assert all(p.final_state.status is AccountStatus.BREACHED for p in losses.report.paths)


def test_path_dependent_size_monotone_after_consecutive_losses():
    cell = run(plan(daily_loss_limit=None), strategy=profile(("-1",),
        sizing=FractionOfCushion("fictional fraction", D("0.25"), 100)))
    for path in cell.report.paths:
        sizes = list(path.contracts_used)
        assert sizes == sorted(sizes, reverse=True)


def test_all_no_trade_sessions_censored_not_failure_no_costs_or_exposure():
    strategy = profile(sampling=Sampling.IID, no_trade_probability=Fraction(1))
    costs = CostScenario("base", ExecutionCostAssumptions(D(1), D(1), D(1)))
    cell = run(strategy=strategy, scenario=costs)
    assert cell.report.censored == 4 and cell.report.passes == cell.report.breaches == 0
    assert cell.report.pass_probability == 0
    assert cell.mean_sessions_to_pass is None and cell.mean_trades_to_pass is None
    for path in cell.paths:
        assert path.trades_taken == path.trading_sessions == 0
        assert path.skipped_trade_count == 6 and path.final_equity == D(1000)
        assert path.commissions == path.adverse_slippage == path.total_risk_used == 0


def test_censoring_wilson_and_conservative_ranking_with_blocked_cells():
    root = Path(__file__).resolve().parents[1]
    real = load_catalog(root / "config/prop_plans/fictional-catalog.json")[0]
    cells = experiment_matrix((plan(), ChallengePlan(real.name, real)), ("ES", "MES"),
        (profile(("0",)),), (BASE, CostScenario("worse cost", ExecutionCostAssumptions(D(1), D(1), D(1)))),
        (42,), context=CONTEXT, safety_config=SAFETY, simulations=4, max_sessions=2)
    assert len(cells) == 8
    rankings = rank_challenges(cells, seed=42)
    assert len(rankings) == 2
    for rank in rankings:
        assert rank.worst_scenario_score == min(r.lower_confidence_bound for r in rank.scenarios)
        assert 0 <= rank.base.lower_confidence_bound <= 1
        assert rank.base.censored == 4 and rank.base.passes == rank.base.breaches == 0
    assert "BLOCKED" in format_experiment(cells) and "unavailable" in format_experiment(cells)


def test_attempt_economics_zero_confidence_and_excludes_activation():
    config = plan().configuration
    args = dict(lower_bound=1.0, minimum_lower_bound=D("0.05"))
    assert attempt_economics(config, CONTEXT, Fraction(0), **args).expected_attempts is None
    value = attempt_economics(config, CONTEXT, Fraction(1, 2), **args)
    assert value.expected_attempts == 2 and value.expected_evaluation_fees == D(40)
    value = attempt_economics(config, CONTEXT, Fraction(1, 2), lower_bound=0.0, minimum_lower_bound=D("0.05"))
    assert value.status == "UNAVAILABLE_INSUFFICIENT_CONFIDENCE"


def test_existing_consistency_minimum_days_semantics_not_rewritten():
    # Safety stops new entries at the absolute target while eligibility is pending.
    cell = run(plan(consistency_fraction=D("0.5"), minimum_trading_days=2), "ES", profile(("8",)))
    assert cell.report.censored == 4 and cell.report.passes == 0
    assert all(p.final_state.status is AccountStatus.ALIVE for p in cell.report.paths)


def test_metrics_costs_drawdown_cushion_and_risk_conservation():
    costs = ExecutionCostAssumptions(D(1), D(2), D(3))
    strategy = profile(("-1",), sizing=FixedContracts("fictional request", 1))
    cell = run(strategy=strategy, scenario=CostScenario("base", costs))
    path = cell.paths[0]
    assert path.commissions == path.trades_taken * D(1)
    assert path.adverse_slippage == path.trades_taken * D("2.5")
    assert path.execution_buffer_charged == path.trades_taken * D(3)
    assert path.maximum_drawdown > 0 and path.minimum_ruin_cushion < D(150)
    assert path.total_risk_used <= path.total_risk_budget_offered
    assert 0 <= path.risk_utilization <= 1


@pytest.mark.parametrize("changes", [dict(no_trade_probability=0.1), dict(opportunities_per_session=0),
    dict(sampling="IID"), dict(fictional=None), dict(weights=(0, 1)), dict(repeat_sequence=None)])
def test_invalid_profile_fails_closed(changes):
    with pytest.raises(ValueError): profile(**changes)


def test_invalid_no_trade_and_raw_plan_or_duplicate_matrix_reject():
    with pytest.raises(ValueError): TradeOutcome(D(1), no_trade=True)
    with pytest.raises(ValueError): ChallengePlan("unlabelled", fictional_plan())
    with pytest.raises(ValueError): experiment_matrix((plan(),), ("ES", "ES"), (profile(),), (BASE,), (1,))


def test_r_multiple_marks_preserved_and_points_stop_not_rescaled_between_families():
    strategy = profile(("1",), stop=StopDistance(D(2), StopUnit.POINTS),
        outcomes=(StopMultipleOutcome(D(1), (D("-0.5"), D("0.5"))),))
    a, b = strategy.source("ES").start_path(1).next_outcome(), strategy.source("NQ").start_path(1).next_outcome()
    assert a.realized_pnl == D(100) and b.realized_pnl == D(40)
    assert a.unrealized_marks == (D(-50), D(50)) and b.unrealized_marks == (D(-20), D(20))


def test_no_trade_probability_and_ladder_reproducible_without_new_seeding():
    strategy = profile(sampling=Sampling.IID, no_trade_probability=Fraction(1, 2),
        sizing=CushionLadder("fictional ladder", ((D(0), 1), (D(100), 2))))
    a, b = run(strategy=strategy), run(strategy=strategy)
    assert a == b
    assert a.report.paths[0].seed_context.master_seed == 42
    assert a.report.paths[0].seed_context.scenario_name == "base"


def test_snapshot_of_real_catalog_gates_unchanged_after_experiments():
    reviewed = load_catalog(Path(__file__).resolve().parents[1] / "config/prop_plans/fictional-catalog.json")
    before = tuple(p.qualify(Operation.CHALLENGE, CONTEXT) for p in reviewed)
    for p in reviewed:
        run(ChallengePlan(p.name, p))
    after = tuple(p.qualify(Operation.CHALLENGE, CONTEXT) for p in reviewed)
    assert before == after and all(not r.admitted for r in after)


def test_no_api_dependency_added_to_challenge_gate():
    # The complete domain model has no API fields. Reviewed API evidence never
    # appears in probability blockers unless explicitly required by its challenge layer.
    reviewed = load_catalog(Path(__file__).resolve().parents[1] / "config/prop_plans/fictional-catalog.json")
    for p in reviewed:
        cell = run(ChallengePlan(p.name, p))
        required = dict(p.requirements)[Operation.CHALLENGE]
        assert all(key in required or reasons == ("MODEL_UNSUPPORTED",) for key, reasons in cell.blockers)


def test_freshness_conflict_and_reset_verification_not_silently_repaired():
    config = external()
    target = next(r for r in config.rules if r.rule_path == "profit_target_gain")
    alternative = replace(target.sources[0], reference="fictional conflicting source", value_json='"101"')
    conflict = target.add_source(alternative)
    config = replace(config, rules=tuple(conflict if r.rule_path == target.rule_path else r for r in config.rules))
    blocked = run(ChallengePlan("fictional conflict", config))
    assert "CONFLICTING_SOURCES" in dict(blocked.blockers)["profit_target_gain"]
    stale = run(ChallengePlan("fictional stale", external()), context=VerificationContext(date(2026, 12, 1)))
    assert stale.status == "BLOCKED"
    assert all("STALE_OFFICIAL_SUPPORT" in reasons for _, reasons in stale.blockers)
    config = external()
    config = replace(config, rules=tuple(replace(r, status=VerificationStatus.UNVERIFIED)
        if r.rule_path == "fees.reset" else r for r in config.rules))
    cell = run(ChallengePlan("fictional reset unknown", config), "ES", profile(("8",)))
    assert cell.report.passes == 4 and cell.economics.status == "BLOCKED"


def test_default_zero_no_trade_path_matches_original_evaluator_exactly():
    from prop_survival_engine.prop_rules.plan_engine import plan_evaluator
    from prop_survival_engine.simulation.reach_avoid import PathConfig, Scenario
    named, strategy = plan(), profile(sampling=Sampling.IID)
    cell = run(named, "MES", strategy)
    loss = instrument_loss(INSTRUMENTS.lookup("MES"), strategy.stop, ZERO)
    evaluator = plan_evaluator(named.configuration.plan, SAFETY, loss.policy_estimate(), PathConfig(3, 2, True))
    candidate = replace(strategy.sizing, name=cell.report.policy)
    baseline = evaluator.evaluate(candidate, Scenario("base", strategy.source("MES")), simulations=4, seed=42)
    # Instrument entry observations are additive; outcomes/state/statistics remain identical.
    stripped = replace(cell.report, paths=tuple(replace(p, risk_points=()) for p in cell.report.paths))
    assert stripped == baseline


@pytest.mark.parametrize("corruption", ["size", "budget", "loss"])
def test_entry_seam_cannot_bypass_original_policy(corruption):
    from prop_survival_engine.prop_rules.plan_engine import PlanRuleEngine
    from prop_survival_engine.simulation.reach_avoid import Scenario, PathConfig
    from prop_survival_engine.risk.policy import ContractLossEstimate
    estimate = ContractLossEstimate(D(1))
    safety = RiskPolicy(SAFETY)
    def invalid(intent, context):
        result = safety.evaluate(intent, context=context, estimate=estimate)
        if corruption == "size":
            return replace(result, approved_contracts=result.approved_contracts + 1)
        if corruption == "budget":
            return replace(result, risk_budget=result.risk_budget + D(1))
        return replace(result, per_contract_worst_case_loss=D(2))
    evaluator = ReachAvoidEvaluator(lambda: PlanRuleEngine(plan().configuration.plan), SAFETY, estimate,
        PathConfig(1, 1), entry_evaluator=invalid, instrument="MES")
    with pytest.raises(ValueError, match="entry boundary"):
        evaluator.evaluate(FixedContracts("fictional", 1), Scenario("base", profile().source("MES")),
                           simulations=1, seed=1)


def test_cost_adaptive_derisking_does_not_claim_probability_monotonicity():
    # Only first-entry size is monotone under costs; later survival may change
    # because the safety layer adapts quantity. Do not impose false P(pass) monotonicity.
    strategy = profile(("-1", "4"), sizing=FixedContracts("fictional", 100))
    zero = run(strategy=strategy)
    high = run(strategy=strategy, scenario=CostScenario("base", ExecutionCostAssumptions(D(4), D(0), D(0))))
    assert high.report.paths[0].risk_points[0].approved <= zero.report.paths[0].risk_points[0].approved
    assert high.report.simulations == zero.report.simulations


def test_source_exhaustion_distinct_censoring_and_no_zero_times():
    cell = run(strategy=profile(("0",), repeat_sequence=False))
    assert cell.report.source_exhausted_paths == 4 and cell.report.horizon_paths == 0
    assert all(p.end is PathEnd.SOURCE_EXHAUSTED for p in cell.report.paths)
    assert all(p.first_passage_trades is None for p in cell.paths)
    assert cell.mean_trades_to_pass is None


def test_horizon_or_scenario_mismatch_never_silently_changes_ranking():
    a = run(symbol="ES")
    b = run(symbol="MES", strategy=profile(opportunities_per_session=3))
    with pytest.raises(ValueError, match="matching horizons"):
        rank_challenges((a, b), seed=42)
