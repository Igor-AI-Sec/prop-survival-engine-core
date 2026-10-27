"""Offline fixtures exercise the real rule engine, not probability guesses."""

from dataclasses import replace, FrozenInstanceError
from decimal import Decimal, localcontext, ROUND_UP
import os
import subprocess
import sys

import pytest

from prop_survival_engine.prop_rules.engine import StatefulPropRuleEngine
from prop_survival_engine.prop_rules.models import (
    AccountStatus, PropRuleConfig, PnlSettled, RealizedPnlUpdated, MarkToMarketUpdated,
)
from prop_survival_engine.risk import (
    ContractLossEstimate, RiskPolicy, RiskPolicyConfig, RiskContext, SessionState, derive_ruin_distances,
)
from prop_survival_engine.oms.models import OmsState
from prop_survival_engine.core.models import OrderIntent, Side
from prop_survival_engine.simulation.outcomes import TradeOutcome, SequenceOutcomeSource, IIDOutcomeSource
from prop_survival_engine.simulation.sizing import FixedContracts, FractionOfCushion, CushionLadder
from prop_survival_engine.simulation.seeds import path_seed_context
from prop_survival_engine.simulation.reach_avoid import (
    PathConfig, Scenario, PathEnd, BreachCause, ReachAvoidEvaluator, simulate_path,
    wilson_interval, summarize, rank_reports,
)


D = Decimal


def factory(*, daily="100", trailing="100", target="40"):
    config = PropRuleConfig(D("1000"), D(trailing), D(daily), D(target))
    return lambda: StatefulPropRuleEngine(config)


def source(*values, repeat=False):
    return SequenceOutcomeSource(tuple(TradeOutcome(D(str(value))) for value in values), repeat=repeat)


def evaluator(engine_factory=None, **kwargs):
    return ReachAvoidEvaluator(engine_factory or factory(),
        RiskPolicyConfig(max_contracts=kwargs.pop("max_contracts", 10)),
        ContractLossEstimate(D(kwargs.pop("stop", "10"))),
        PathConfig(**({"max_sessions": 3, "opportunities_per_session": 4} | kwargs)))


def run(*values, **kwargs):
    candidate = kwargs.pop("candidate", FixedContracts("fixed", 1))
    scenario = Scenario("base", source(*values), **kwargs.pop("costs", {}))
    return evaluator(**kwargs).evaluate(candidate, scenario, simulations=1, seed=7).paths[0]


def test_fixed_seed_produces_identical_complete_reports():
    selected = evaluator()
    scenario = Scenario("base", IIDOutcomeSource((TradeOutcome(D("20")), TradeOutcome(D("-120"))), (4, 1)))
    first = selected.evaluate(FixedContracts("fixed", 1), scenario, simulations=40, seed=123)
    assert first == selected.evaluate(FixedContracts("fixed", 1), scenario, simulations=40, seed=123)
    assert first.simulations == first.passes + first.breaches + first.censored == 40
    assert first.passes and first.breaches
    assert first.lower_confidence_bound <= first.pass_probability <= first.confidence_interval[1]


@pytest.mark.parametrize("values,end,trades", [
    ((20, 20, -1000), PathEnd.PASSED, 2), ((-100, 1000), PathEnd.BREACHED, 1),
])
def test_reach_avoid_stops_at_first_absorption(values, end, trades):
    result = run(*values, retain_trace=True)
    assert result.end is end
    assert result.trades == trades
    assert result.completed_trades == trades
    assert result.trace[-1].state.status is (AccountStatus.PASSED if end is PathEnd.PASSED else AccountStatus.BREACHED)
    assert result.trace[-1].event == "PnlSettled"  # No subsequent EOD/session events.


@pytest.mark.parametrize("daily,trailing,cause", [
    ("100", "200", BreachCause.DAILY), ("200", "100", BreachCause.TRAILING),
    ("100", "100", BreachCause.BOTH),
])
def test_exact_floor_touch_uses_engine_signals(daily, trailing, cause):
    result = evaluator(factory(daily=daily, trailing=trailing)).evaluate(
        FixedContracts("fixed", 1), Scenario("base", source(-100)), simulations=1, seed=0)
    assert result.breaches == 1
    assert result.paths[0].final_state.equity == D("900")
    assert result.paths[0].breach_cause is cause
    assert dict(result.breach_cause_counts)[cause] == 1
    assert sum(count for _, count in result.breach_cause_counts) == result.breaches
    assert result.estimate_exceedances == 1  # Injected gap exceeds estimate, not risk bypass.


def test_exact_target_touch_is_a_pass():
    result = run(40, -100)
    assert result.end is PathEnd.PASSED
    assert result.final_state.equity == result.final_state.target_equity == D("1040")


def test_eod_trailing_high_watermark_updates_only_at_settlement():
    result = evaluator(factory(target="1000"), max_sessions=2, opportunities_per_session=1,
                       retain_trace=True).evaluate(FixedContracts("fixed", 1), Scenario("base", source(50, -10)),
                                                 simulations=1, seed=0).paths[0]
    trade_points = [p for p in result.trace if p.event == "PnlSettled"]
    eods = [p for p in result.trace if p.event == "EndOfDaySettled"]
    assert trade_points[0].state.equity == D("1050")
    assert trade_points[0].state.trailing_floor == D("900")
    assert eods[0].state.eod_high_watermark == D("1050")
    assert eods[0].state.trailing_floor == D("950")
    assert eods[1].state.eod_high_watermark == D("1050")
    assert eods[1].state.trailing_floor == D("950")


def test_daily_floor_resets_at_session_start_not_during_trade_or_eod():
    result = evaluator(factory(target="1000", trailing="300"), max_sessions=2,
                       opportunities_per_session=1, retain_trace=True).evaluate(
        FixedContracts("fixed", 1), Scenario("base", source(-20, 10)), simulations=1, seed=0).paths[0]
    eod = next(p.state for p in result.trace if p.event == "EndOfDaySettled")
    starts = [p.state for p in result.trace if p.event == "SessionStarted"]
    assert eod.daily_floor == D("900")
    assert starts[1].day_start_equity == D("980")
    assert starts[1].daily_floor == D("880")
    assert result.final_state.daily_floor == D("880")


@pytest.mark.parametrize("mark,status,end", [("40", AccountStatus.PASSED, PathEnd.PASSED),
                                          ("-100", AccountStatus.BREACHED, PathEnd.BREACHED)])
def test_intratrade_unrealized_terminal_state_stops_before_close(mark, status, end):
    scenario = Scenario("base", SequenceOutcomeSource((TradeOutcome(D("0"), (D(mark), D("9999"))),)))
    result = evaluator(retain_trace=True).evaluate(FixedContracts("fixed", 1), scenario,
                                                  simulations=1, seed=0).paths[0]
    assert result.end is end
    assert result.final_state.status is status
    assert result.final_state.unrealized_pnl == D(mark)
    assert result.completed_trades == 0
    assert result.trades == 1
    assert result.trace[-1].event == "MarkToMarketUpdated"


def test_atomic_pnl_settlement_does_not_create_phantom_pass_or_breach():
    engine = factory(target="100")()
    engine.apply(RealizedPnlUpdated(D("80")))
    engine.apply(MarkToMarketUpdated(D("10")))
    assert engine.state.equity == D("1090")
    assert engine.apply(PnlSettled(D("90"))) == ()
    assert engine.state.equity == D("1090")
    assert engine.state.status is AccountStatus.ALIVE
    assert engine.state.unrealized_pnl == 0
    # A loss already present in the mark transfers without temporary restoration.
    engine.apply(MarkToMarketUpdated(D("-20")))
    assert engine.apply(PnlSettled(D("70"))) == ()
    assert engine.state.equity == D("1070")


def test_marked_trade_close_preserves_realized_unrealized_consistency():
    scenario = Scenario("base", SequenceOutcomeSource((TradeOutcome(D("10"), (D("5"), D("10"))),)))
    result = evaluator(factory(target="1000"), max_sessions=1, opportunities_per_session=1).evaluate(
        FixedContracts("fixed", 1), scenario, simulations=1, seed=0).paths[0]
    assert result.final_state.realized_pnl == D("10")
    assert result.final_state.unrealized_pnl == 0
    assert result.final_state.equity == D("1010")


def test_zero_safe_size_does_not_draw_an_outcome_or_trade():
    class MustNotDraw:
        def start_path(self, seed):
            return self
        def next_outcome(self):
            raise AssertionError("risk rejected; outcome must not be consumed")
    result = evaluator(stop="100", max_sessions=2, opportunities_per_session=2).evaluate(
        FixedContracts("fixed", 100), Scenario("base", lambda context: MustNotDraw()), simulations=1, seed=0)
    path = result.paths[0]
    assert path.end is PathEnd.HORIZON
    assert path.trades == 0 and path.rejected_entries == 4
    assert result.passes == result.breaches == 0
    assert result.censored == 1
    assert result.average_contracts == result.maximum_contracts == 0
    assert result.mean_trades_to_absorption is None


def test_risk_blocked_opportunity_can_resume_after_real_session_reset():
    path = evaluator(factory(target="1000", trailing="500"), max_sessions=2,
                     opportunities_per_session=2).evaluate(
        FixedContracts("fixed", 1), Scenario("base", source(-80, 10, 10)), simulations=1, seed=0).paths[0]
    assert path.trades == 3
    assert path.rejected_entries == 1
    assert path.final_state.equity == D("940")
    assert path.final_state.daily_floor == D("820")


def test_identical_sequence_policy_and_seed_have_identical_state_and_trace():
    first = run(10, -20, 15, retain_trace=True)
    assert first == run(10, -20, 15, retain_trace=True)
    assert first.end is PathEnd.SOURCE_EXHAUSTED


@pytest.mark.parametrize("candidate", [
    FixedContracts("fixed", 10), FractionOfCushion("fraction", D("0.5"), 20),
    CushionLadder("ladder", ((D("10"), 1), (D("50"), 2), (D("100"), 5))),
])
def test_candidates_are_monotone_and_safety_remains_authoritative(candidate):
    sizes, requests = [], []
    safety = RiskPolicy(RiskPolicyConfig(3))
    for equity in ["1000", "980", "960", "940", "920", "901", "900"]:
        engine = factory(target="1000")()
        engine.apply(RealizedPnlUpdated(D(equity) - D("1000")))
        context = RiskContext(engine.state, OmsState.READY, SessionState.OPEN)
        metrics = derive_ruin_distances(context)
        request = candidate.requested_contracts(metrics, D("10"))
        requests.append(request)
        size = 0 if request == 0 else safety.evaluate(OrderIntent("SIM", Side.BUY, request),
            context=context, estimate=ContractLossEstimate(D("10"))).approved_contracts
        sizes.append(size)
        assert size <= 3
        if size:
            assert D(size) * D("10") < metrics.effective_ruin_distance
    assert requests == sorted(requests, reverse=True)
    assert sizes == sorted(sizes, reverse=True)


def test_inverse_cushion_candidate_is_rejected_before_second_trade():
    class Gambler:
        name = "invalid-gambler"
        def requested_contracts(self, distances, loss):
            return 1 if distances.effective_ruin_distance >= D("100") else 10
    with pytest.raises(ValueError, match="increases requested risk"):
        evaluator().evaluate(Gambler(), Scenario("base", source(-10, 100)), simulations=1, seed=0)


def test_cushion_rebound_does_not_hide_resurrection_relative_to_prior_larger_cushion():
    class ReboundGambler:
        name = "invalid-rebound"
        def requested_contracts(self, distances, loss):
            return 10 if distances.effective_ruin_distance == D("95") else 1
    with pytest.raises(ValueError, match="increases requested risk"):
        evaluator().evaluate(ReboundGambler(), Scenario("base", source(-10, 5, 100)), simulations=1, seed=0)


@pytest.mark.parametrize("cost_field", ["additional_fees", "additional_execution_cost", "additional_slippage"])
def test_higher_costs_do_not_improve_identical_fixed_size_paths(cost_field):
    selected = evaluator(factory(target="40"), max_sessions=1, opportunities_per_session=4)
    baseline = selected.evaluate(FixedContracts("fixed", 1), Scenario("base", source(20, 20, 20, 20)), simulations=1, seed=0)
    stressed = selected.evaluate(FixedContracts("fixed", 1),
        Scenario("cost", source(20, 20, 20, 20), **{cost_field: D("5")}), simulations=1, seed=0)
    assert baseline.paths[0].contracts_used == (1, 1)
    assert stressed.paths[0].contracts_used == (1, 1, 1)
    assert stressed.pass_times.minimum >= baseline.pass_times.minimum
    assert stressed.passes <= baseline.passes


def test_comparison_uses_worst_scenario_not_best_case_point_estimate():
    # Deterministic report fixtures establish ranking independently of MC noise.
    passed = run(40)
    breached = run(-100)
    reports = (
        summarize("fragile", "base", (passed,) * 10, seed=0),
        summarize("fragile", "stress", (breached,) * 10, seed=0),
        summarize("robust", "base", (passed,) * 6 + (breached,) * 4, seed=0),
        summarize("robust", "stress", (passed,) * 6 + (breached,) * 4, seed=0),
    )
    ranking = rank_reports(reports)
    assert ranking[0].policy == "robust"
    assert ranking[0].base.pass_probability < ranking[1].base.pass_probability
    assert ranking[0].worst_scenario_score > ranking[1].worst_scenario_score


def test_ranking_uses_confidence_lower_bound_not_raw_probability():
    passed, breached = run(40), run(-100)
    small = summarize("tiny", "base", (passed,), seed=0)
    large = summarize("qualified", "base", (passed,) * 80 + (breached,) * 20, seed=0)
    assert small.pass_probability > large.pass_probability
    assert rank_reports((small, large))[0].policy == "qualified"


def test_multiple_named_scenarios_are_isolated_and_comparison_reproducible():
    selected = evaluator()
    shared = IIDOutcomeSource((TradeOutcome(D("20")), TradeOutcome(D("-120"))), (3, 1))
    scenarios = (Scenario("base", shared), Scenario("cost", shared, additional_fees=D("1")),
                 Scenario("slippage", shared, additional_slippage=D("2")),
                 Scenario("bad-outcomes", IIDOutcomeSource(shared.outcomes, (1, 3))))
    candidates = (FixedContracts("fixed", 1), FractionOfCushion("fraction", D("0.25"), 3))
    before = selected.evaluate(candidates[0], scenarios[0], simulations=8, seed=9)
    comparison = selected.compare(candidates, scenarios, simulations=8, seed=9)
    after = selected.evaluate(candidates[0], scenarios[0], simulations=8, seed=9)
    assert before == after
    assert comparison == selected.compare(candidates, scenarios, simulations=8, seed=9)
    assert all(len(value.scenarios) == 4 for value in comparison)
    with pytest.raises(FrozenInstanceError):
        comparison[0].base.paths[0].final_state.equity = D("0")


def test_horizon_censoring_is_not_conditioned_on_absorbed_paths():
    passed, censored = run(40), run(0)
    report = summarize("fixed", "base", (passed, censored), seed=0)
    assert report.pass_probability == 0.5  # Never passes/(passes+breaches).
    assert report.censored == 1
    assert report.eventual_pass_probability_bounds == (0.5, 1.0)
    assert report.mean_trades_to_absorption == report.median_trades_to_absorption == 1
    assert report.pass_times.count == 1


def test_iid_and_sequence_streams_are_fresh_for_each_path():
    seq = source(1, 2)
    a, b = seq.start_path(0), seq.start_path(0)
    assert a.next_outcome() == b.next_outcome()
    assert a.next_outcome().realized_pnl == D("2")
    assert b.next_outcome().realized_pnl == D("2")
    assert a.next_outcome() is None
    iid = IIDOutcomeSource(seq.outcomes, (1, 5))
    a, b = iid.start_path(13), iid.start_path(13)
    assert [a.next_outcome() for _ in range(20)] == [b.next_outcome() for _ in range(20)]


def test_already_terminal_engine_is_not_advanced():
    engine = factory()()
    engine.apply(RealizedPnlUpdated(D("40")))
    before = engine.snapshot(), engine.event_history
    class UnusedSource:
        def start_path(self, seed):
            raise AssertionError("terminal path must not initialize outcome sampling")
    result = simulate_path(engine, FixedContracts("fixed", 1), Scenario("base", lambda context: UnusedSource()),
        RiskPolicy(RiskPolicyConfig(3)), ContractLossEstimate(D("10")), PathConfig())
    assert result.end is PathEnd.PASSED and result.trades == 0
    assert (engine.snapshot(), engine.event_history) == before


def test_shared_mutable_engine_factory_fails_closed():
    engine = factory()()
    selected = evaluator(lambda: engine)
    with pytest.raises(ValueError, match="fresh engines"):
        selected.evaluate(FixedContracts("fixed", 1), Scenario("base", source(40)), simulations=2, seed=0)


def test_monetary_state_does_not_depend_on_ambient_decimal_precision():
    first = run("10.01", "-2.02", "3.03")
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        assert run("10.01", "-2.02", "3.03") == first


@pytest.mark.parametrize("bad", [1.5, "10", D("NaN"), D("Infinity")])
def test_float_or_nonfinite_money_is_not_silently_converted(bad):
    with pytest.raises(ValueError):
        TradeOutcome(bad)
    with pytest.raises(ValueError):
        Scenario("bad", source(1), additional_fees=bad)


def test_invalid_ladder_and_statistical_inputs_fail_closed():
    with pytest.raises(ValueError):
        CushionLadder("gamble", ((D("10"), 5), (D("100"), 1)))
    with pytest.raises(ValueError):
        wilson_interval(1, 0)
    with pytest.raises(ValueError):
        rank_reports(())
    assert wilson_interval(0, 10)[0] == 0
    assert wilson_interval(10, 10)[1] == 1


def test_path_delegates_overridden_touch_semantics_to_real_rule_engine():
    class StrictBelowEngine(StatefulPropRuleEngine):
        def _is_daily_loss_breached(self, equity, floor):
            return equity < floor
        def _is_trailing_drawdown_breached(self, equity, floor):
            return equity < floor
    config = PropRuleConfig(D("1000"), D("100"), D("100"), D("40"))
    selected = evaluator(lambda: StrictBelowEngine(config), max_sessions=1, opportunities_per_session=1)
    result = selected.evaluate(FixedContracts("fixed", 1), Scenario("base", source(-100)), simulations=1, seed=0)
    assert result.breaches == 0
    assert result.paths[0].final_state.status is AccountStatus.ALIVE
    assert result.paths[0].final_state.equity == D("900")


@pytest.mark.parametrize("count,seed", [(0, 0), (True, 0), (1.5, 0), (1, 0.5)])
def test_invalid_simulation_controls_are_rejected(count, seed):
    with pytest.raises(ValueError):
        evaluator().evaluate(FixedContracts("fixed", 1), Scenario("base", source(1)),
                             simulations=count, seed=seed)


def test_ranking_does_not_mix_horizons_or_confidence_levels():
    selected = evaluator()
    candidate, scenario = FixedContracts("fixed", 1), Scenario("base", source(40))
    report = selected.evaluate(candidate, scenario, simulations=1, seed=0)
    other = replace(report, policy="other", path_config=PathConfig(10, 5))
    with pytest.raises(ValueError, match="matching horizons"):
        rank_reports((report, other))
    with pytest.raises(ValueError, match="confidence levels"):
        rank_reports((report, replace(report, policy="other", confidence_level=0.9)))


def test_precision_overflow_fails_instead_of_rounding_money():
    outcome = TradeOutcome(D("1.0000000000000000000000000000000000000000000000000000000000000000000001"))
    from decimal import Inexact
    with pytest.raises(Inexact):
        evaluator().evaluate(FixedContracts("fixed", 1), Scenario("base", SequenceOutcomeSource((outcome,))),
                             simulations=1, seed=0)


def test_uncertainty_and_time_and_contract_summaries():
    one, two, breach = run(40), run(20, 20), run(-100)
    report = summarize("fixed", "base", (one, two, breach), seed=0)
    assert (report.passes, report.breaches, report.censored) == (2, 1, 0)
    assert report.mean_trades_to_absorption == pytest.approx(4 / 3)
    assert report.median_trades_to_absorption == 1
    assert report.pass_times.minimum == 1 and report.pass_times.maximum == 2
    assert report.pass_times.median == 1.5
    assert report.average_contracts == report.maximum_contracts == 1


def test_shared_mutable_source_factory_is_rejected_across_paths_and_repeated_calls():
    shared = source(40, -100)
    scenario = Scenario("base", lambda context: shared)
    selected = evaluator()
    with pytest.raises(ValueError, match="fresh outcome sources"):
        selected.evaluate(FixedContracts("fixed", 1), scenario, simulations=2, seed=0)
    # Resetting or immutable configuration does not waive ownership.
    with pytest.raises(ValueError, match="fresh outcome sources"):
        selected.evaluate(FixedContracts("fixed", 1), scenario, simulations=1, seed=0)


def test_fresh_sources_cannot_hide_a_shared_mutable_stream():
    shared_stream = source(40, -100).start_path(0)
    class Wrapper:
        def start_path(self, seed):
            return shared_stream
    selected = evaluator()
    scenario = Scenario("base", lambda context: Wrapper())
    first = selected.evaluate(FixedContracts("fixed", 1), scenario, simulations=1, seed=0)
    assert first.passes == 1
    with pytest.raises(ValueError, match="fresh outcome streams"):
        selected.evaluate(FixedContracts("fixed", 1), scenario, simulations=1, seed=0)


def test_shared_stream_is_rejected_within_one_multi_path_evaluation():
    shared_stream = source(40, -100).start_path(0)
    class Wrapper:
        def start_path(self, seed):
            return shared_stream
    with pytest.raises(ValueError, match="fresh outcome streams"):
        evaluator().evaluate(FixedContracts("fixed", 1), Scenario("base", lambda context: Wrapper()),
                             simulations=2, seed=0)


def test_shared_engine_across_single_path_scenarios_is_rejected():
    engine = factory()()
    selected = evaluator(lambda: engine)
    scenarios = (Scenario("base", source(40)), Scenario("stress", source(-100)))
    with pytest.raises(ValueError, match="fresh engines"):
        selected.compare((FixedContracts("fixed", 1),), scenarios, simulations=1, seed=0)


def test_engine_source_and_stream_identities_differ_across_paths_and_scenarios():
    engines, sources, streams, contexts = [], [], [], []
    make_engine = factory()
    def tracked_engine():
        engine = make_engine()
        engines.append(engine)
        return engine
    class TrackedSource:
        def start_path(self, seed):
            stream = source(40).start_path(seed)
            streams.append(stream)
            return stream
    def tracked_source(context):
        contexts.append(context)
        value = TrackedSource()
        sources.append(value)
        return value
    selected = evaluator(tracked_engine)
    selected.compare((FixedContracts("fixed", 1),),
        (Scenario("base", tracked_source), Scenario("stress", tracked_source)), simulations=3, seed=41)
    assert len(engines) == len(sources) == len(streams) == 6
    assert all(len({id(value) for value in values}) == 6 for values in (engines, sources, streams))
    assert {context.scenario_name for context in contexts} == {"base", "stress"}
    assert len({context.path_seed for context in contexts}) == 6


def test_reversed_scenario_order_and_repeated_evaluation_have_identical_reports():
    selected = evaluator()
    atoms = (TradeOutcome(D("20")), TradeOutcome(D("-120")))
    scenarios = (Scenario("base", IIDOutcomeSource(atoms, (3, 1))),
                 Scenario("stress", IIDOutcomeSource(atoms, (1, 3)), additional_slippage=D("1")))
    candidate = FixedContracts("fixed", 1)
    forward = selected.compare((candidate,), scenarios, simulations=20, seed=7)
    backward = selected.compare((candidate,), tuple(reversed(scenarios)), simulations=20, seed=7)
    assert forward == backward
    for scenario in scenarios:
        first = selected.evaluate(candidate, scenario, simulations=20, seed=7)
        assert first == selected.evaluate(candidate, scenario, simulations=20, seed=7)
        assert first == next(report for report in forward[0].scenarios if report.scenario == scenario.name)


def test_starting_configuration_is_checked_across_scenarios():
    calls = 0
    def varying_factory():
        nonlocal calls
        calls += 1
        return factory(target="40" if calls == 1 else "50")()
    selected = evaluator(varying_factory)
    selected.evaluate(FixedContracts("fixed", 1), Scenario("base", source(40)), simulations=1, seed=0)
    with pytest.raises(ValueError, match="starting configuration/state"):
        selected.evaluate(FixedContracts("fixed", 1), Scenario("stress", source(40)), simulations=1, seed=0)


def test_seed_hierarchy_is_stable_named_and_prefix_invariant():
    first = path_seed_context(123, "base", 0)
    assert first == path_seed_context(123, "base", 0)
    assert first.path_seed != path_seed_context(123, "base", 1).path_seed
    assert first.scenario_seed != path_seed_context(123, "stress", 0).scenario_seed
    selected = evaluator()
    scenario = Scenario("base", IIDOutcomeSource((TradeOutcome(D("20")), TradeOutcome(D("-120")))))
    short = selected.evaluate(FixedContracts("fixed", 1), scenario, simulations=2, seed=123)
    long = selected.evaluate(FixedContracts("fixed", 1), scenario, simulations=5, seed=123)
    assert short.paths == long.paths[:2]
    assert short.paths[0].seed_context == first


def test_seed_hierarchy_does_not_depend_on_python_hash_randomization():
    script = ("import sys; sys.path.insert(0, 'src'); "
              "from prop_survival_engine.simulation.seeds import path_seed_context; "
              "print(path_seed_context(123, 'base', 0))")
    outputs = []
    for hash_seed in ("1", "42"):
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                env=dict(os.environ, PYTHONHASHSEED=hash_seed), timeout=10, check=True)
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1] == f"{path_seed_context(123, 'base', 0)}\n"


def test_resetting_cached_engine_does_not_waive_ownership():
    engine = factory()()
    selected = evaluator(lambda: engine)
    selected.evaluate(FixedContracts("fixed", 1), Scenario("base", source(40)), simulations=1, seed=0)
    replacement = factory()()
    engine.__dict__.update(replacement.__dict__)
    with pytest.raises(ValueError, match="fresh engines"):
        selected.evaluate(FixedContracts("fixed", 1), Scenario("stress", source(40)), simulations=1, seed=0)


@pytest.mark.parametrize("cost_field", ["additional_fees", "additional_execution_cost", "additional_slippage"])
def test_adaptive_cost_derisking_example_is_expected_not_a_monotonicity_bug(cost_field):
    selected = evaluator(max_sessions=1, opportunities_per_session=2)
    candidate = FixedContracts("fixed-2", 2)
    base = selected.evaluate(candidate, Scenario("base", source(-40, 100)), simulations=1, seed=0).paths[0]
    higher = selected.evaluate(candidate, Scenario("cost", source(-40, 100), **{cost_field: D("3")}),
                               simulations=1, seed=0).paths[0]
    assert base.end is PathEnd.HORIZON
    assert base.contracts_used == (2,)
    assert base.final_state.equity == D("920")
    assert higher.end is PathEnd.PASSED
    assert higher.contracts_used == (1, 1)
    assert higher.final_state.equity == D("1054")


@pytest.mark.parametrize("cost_field", ["additional_fees", "additional_execution_cost", "additional_slippage"])
def test_same_gross_example_with_identical_executed_quantity_has_direct_cost_monotonicity(cost_field):
    # Use a legal safety configuration with sufficient capacity in both scenarios
    # to isolate direct costs. Every size still passes the unmodified v0.9 policy.
    selected = ReachAvoidEvaluator(factory(), RiskPolicyConfig(10, D("1"), D("0")),
                                  ContractLossEstimate(D("1")), PathConfig(1, 2, True))
    candidate = FixedContracts("fixed-2", 2)
    base = selected.evaluate(candidate, Scenario("base", source(-40, 100)), simulations=1, seed=0).paths[0]
    higher = selected.evaluate(candidate, Scenario("cost", source(-40, 100), **{cost_field: D("3")}),
                               simulations=1, seed=0).paths[0]
    assert base.contracts_used == higher.contracts_used == (2, 2)
    assert base.end is higher.end is PathEnd.PASSED
    assert base.trades == higher.trades == 2
    assert higher.final_state.equity == D("1108") <= base.final_state.equity == D("1120")
    for low, high in zip(base.trace, higher.trace):
        assert low.event == high.event
        assert high.state.equity <= low.state.equity


def test_fixed_exposure_cost_stress_can_worsen_but_not_improve_terminal_outcome():
    selected = ReachAvoidEvaluator(factory(), RiskPolicyConfig(10, D("1"), D("0")),
                                  ContractLossEstimate(D("1")), PathConfig(1, 2))
    candidate = FixedContracts("fixed-2", 2)
    base = selected.evaluate(candidate, Scenario("base", source(-40, 100)), simulations=1, seed=0).paths[0]
    higher = selected.evaluate(candidate, Scenario("cost", source(-40, 100), additional_execution_cost=D("20")),
                               simulations=1, seed=0).paths[0]
    assert base.end is PathEnd.PASSED
    assert higher.end is PathEnd.BREACHED
    assert higher.contracts_used == base.contracts_used[:1] == (2,)
