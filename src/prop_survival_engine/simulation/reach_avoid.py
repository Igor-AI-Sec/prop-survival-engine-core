"""Stateful offline reach-avoid Monte Carlo; no execution/OMS service or optimizer."""

from dataclasses import dataclass, replace
from copy import deepcopy
from decimal import Context, Decimal, Inexact, localcontext
from enum import Enum
from fractions import Fraction
from math import ceil, sqrt
from statistics import NormalDist, mean, median
from typing import Callable
from uuid import NAMESPACE_URL, uuid5

from prop_survival_engine.core.models import OrderIntent, Side
from prop_survival_engine.oms.models import OmsState
from prop_survival_engine.prop_rules.interfaces import PropRuleEngine
from prop_survival_engine.prop_rules.models import (
    AccountState, AccountStatus, DailyLossBreached, TrailingDrawdownBreached,
    EndOfDaySettled, SessionStarted, MarkToMarketUpdated, RealizedPnlUpdated, PnlSettled,
)
from prop_survival_engine.risk import (
    ContractLossEstimate, RiskPolicy, RiskPolicyConfig, RiskContext, SessionState, derive_ruin_distances,
)
from prop_survival_engine.risk.money import decimal_from_fraction, finite_decimal
from prop_survival_engine.simulation.outcomes import OutcomeSourceFactory, TradeOutcome
from prop_survival_engine.simulation.ownership import ownership
from prop_survival_engine.simulation.seeds import PathSeedContext, path_seed_context
from prop_survival_engine.simulation.sizing import SizingCandidate


class PathEnd(str, Enum):
    PASSED = "PASSED"
    BREACHED = "BREACHED"
    HORIZON = "HORIZON"
    SOURCE_EXHAUSTED = "SOURCE_EXHAUSTED"


class BreachCause(str, Enum):
    DAILY = "DAILY"
    TRAILING = "TRAILING"
    BOTH = "BOTH"
    UNSPECIFIED = "UNSPECIFIED"


@dataclass(frozen=True)
class PathConfig:
    max_sessions: int = 20
    opportunities_per_session: int = 5
    retain_trace: bool = False

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in (self.max_sessions, self.opportunities_per_session)):
            raise ValueError("path horizons must be positive integers")
        if type(self.retain_trace) is not bool:
            raise ValueError("retain_trace must be bool")


@dataclass(frozen=True)
class Scenario:
    name: str
    outcome_source_factory: OutcomeSourceFactory
    additional_fees: Decimal = Decimal("0")
    additional_execution_cost: Decimal = Decimal("0")
    additional_slippage: Decimal = Decimal("0")

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("scenario name must be nonempty")
        if not callable(self.outcome_source_factory):
            raise ValueError("scenario requires an outcome_source_factory, not a shared source")
        for value in (self.additional_fees, self.additional_execution_cost, self.additional_slippage):
            if not finite_decimal(value) or value < 0:
                raise ValueError("scenario costs must be finite nonnegative Decimal")


@dataclass(frozen=True)
class StatePoint:
    event: str
    trades: int
    state: AccountState


@dataclass(frozen=True)
class EntryRiskPoint:
    opportunity: int
    state: AccountState
    requested: int
    approved: int
    budget: Decimal | None
    loss: Decimal
    executed: bool = False


@dataclass(frozen=True)
class PathResult:
    end: PathEnd
    final_state: AccountState
    trades: int
    completed_trades: int
    opportunities: int
    rejected_entries: int
    abstentions: int
    contracts_used: tuple[int, ...]
    breach_cause: BreachCause | None
    estimate_exceedances: int
    trace: tuple[StatePoint, ...]
    seed_context: PathSeedContext
    risk_points: tuple[EntryRiskPoint, ...] = ()


@dataclass(frozen=True)
class TimeSummary:
    count: int
    minimum: int
    median: float
    p25: int
    p75: int
    p90: int
    maximum: int


@dataclass(frozen=True)
class MonteCarloReport:
    policy: str
    scenario: str
    seed: int
    simulations: int
    passes: int
    breaches: int
    censored: int
    horizon_paths: int
    source_exhausted_paths: int
    path_config: PathConfig | None
    pass_probability: float
    eventual_pass_probability_bounds: tuple[float, float]
    confidence_level: float
    confidence_interval: tuple[float, float]
    lower_confidence_bound: float
    mean_trades_to_absorption: float | None
    median_trades_to_absorption: float | None
    pass_times: TimeSummary | None
    breach_cause_counts: tuple[tuple[BreachCause, int], ...]
    average_contracts: float
    maximum_contracts: int
    rejected_entries: int
    abstentions: int
    estimate_exceedances: int
    paths: tuple[PathResult, ...]


@dataclass(frozen=True)
class PolicyComparison:
    rank: int
    policy: str
    base: MonteCarloReport
    scenarios: tuple[MonteCarloReport, ...]
    worst_scenario_score: float


def _estimate(estimate: ContractLossEstimate, scenario: Scenario) -> ContractLossEstimate:
    if (not isinstance(estimate, ContractLossEstimate) or not finite_decimal(estimate.stop_loss)
            or estimate.stop_loss <= 0 or any(not finite_decimal(v) or v < 0
                                            for v in (estimate.fees_commissions, estimate.slippage_loss))):
        raise ValueError("simulation requires an explicit valid per-contract loss estimate")
    return ContractLossEstimate(estimate.stop_loss,
        decimal_from_fraction(Fraction(estimate.fees_commissions) + Fraction(scenario.additional_fees)),
        decimal_from_fraction(Fraction(estimate.slippage_loss) + Fraction(scenario.additional_execution_cost)
                              + Fraction(scenario.additional_slippage)))


def simulate_path(engine: PropRuleEngine, candidate: SizingCandidate, scenario: Scenario,
                  safety: RiskPolicy, estimate: ContractLossEstimate, config: PathConfig,
                  *, seed: int = 0, path_context: PathSeedContext | None = None,
                  _owner=None, entry_evaluator=None, instrument="SIM") -> PathResult:
    """Own one fresh engine/stream. Stops immediately at the engine's terminal state.

    All rule decisions come from apply/state, including during costs and marks.
    Sequential positions only; zero-size opportunities do not draw an outcome.
    """
    with localcontext(Context(prec=64)) as money_context:
        money_context.traps[Inexact] = True  # Never silently round account money.
        context_seed = path_context or path_seed_context(seed, scenario.name, 0)
        if context_seed.scenario_name != scenario.name:
            raise ValueError("path seed context must match scenario")
        owner = _owner if _owner is not None else object()
        ownership.claim(engine, owner, "engines")
        effective = _estimate(estimate, scenario)
        cost = decimal_from_fraction(Fraction(effective.fees_commissions) + Fraction(effective.slippage_loss))
        loss = decimal_from_fraction(Fraction(effective.stop_loss) + Fraction(cost)
                                     + Fraction(safety.config.execution_loss_buffer))
        trades = completed = opportunities = rejected = abstentions = exceedances = 0
        sizes, trace = [], []
        cause = None
        observed_requests = []
        risk_points = []

        def apply(event):
            nonlocal cause
            signals = engine.apply(event)
            if config.retain_trace:
                trace.append(StatePoint(type(event).__name__, trades, engine.state))
            if engine.state.status is AccountStatus.BREACHED:
                daily = any(isinstance(s, DailyLossBreached) for s in signals)
                trailing = any(isinstance(s, TrailingDrawdownBreached) for s in signals)
                cause = (BreachCause.BOTH if daily and trailing else BreachCause.DAILY if daily
                         else BreachCause.TRAILING if trailing else BreachCause.UNSPECIFIED)

        def finish(end):
            return PathResult(end, engine.state, trades, completed, opportunities, rejected, abstentions,
                              tuple(sizes), cause or (BreachCause.UNSPECIFIED if end is PathEnd.BREACHED else None),
                              exceedances, tuple(trace), context_seed, tuple(risk_points))

        def terminal():
            return {AccountStatus.PASSED: PathEnd.PASSED, AccountStatus.BREACHED: PathEnd.BREACHED}.get(engine.state.status)

        if terminal():
            return finish(terminal())
        if engine.state.unrealized_pnl != 0:
            raise ValueError("paths must start flat; pre-existing position attribution is not modeled")
        source = scenario.outcome_source_factory(context_seed)
        ownership.claim(source, owner, "outcome sources")
        stream = source.start_path(context_seed.path_seed)
        ownership.claim(stream, owner, "outcome streams")
        initial_session = engine.state.session_id
        for session in range(config.max_sessions):
            if hasattr(engine, "before_session"):
                event = engine.before_session(session)
                if event is not None:
                    apply(event)
                    if terminal():
                        return finish(terminal())
            apply(SessionStarted(f"{initial_session}:mc:{session}"))
            if terminal():
                return finish(terminal())
            for opportunity in range(config.opportunities_per_session):
                opportunities += 1
                if hasattr(engine, "advance_opportunity"):
                    event = engine.advance_opportunity(session, opportunity)
                    if event is not None:
                        apply(event)
                    if terminal():
                        return finish(terminal())
                context = (engine.risk_context() if hasattr(engine, "risk_context") else
                           RiskContext(engine.state, OmsState.READY, SessionState.OPEN))
                distances = derive_ruin_distances(context)
                request = candidate.requested_contracts(distances, loss)
                if type(request) is not int or request < 0:
                    raise ValueError("candidate must request a nonnegative integer")
                distance = distances.effective_ruin_distance
                if any(distance <= prior_distance and request > prior_request
                       for prior_distance, prior_request in observed_requests):
                    raise ValueError("candidate increases requested risk with unchanged/worse cushion")
                observed_requests.append((distance, request))
                if request == 0:
                    abstentions += 1
                    continue
                intent = OrderIntent(instrument, Side.BUY, request,
                                     intent_id=uuid5(NAMESPACE_URL, f"mc:{context_seed.path_seed}:{opportunities}"))
                approval = safety.evaluate(intent, context=context, estimate=effective)
                if entry_evaluator is not None:
                    converted = entry_evaluator(intent, context)
                    if (converted.risk_budget != approval.risk_budget
                            or converted.per_contract_worst_case_loss != approval.per_contract_worst_case_loss
                            or type(converted.approved_contracts) is not int
                            or not 0 <= converted.approved_contracts <= approval.approved_contracts):
                        raise ValueError("entry boundary must preserve monetary policy and may only reduce sizing")
                    approval = converted
                    risk_points.append(EntryRiskPoint(opportunities, engine.state, request,
                        approval.approved_contracts, approval.risk_budget, loss))
                quantity = approval.approved_contracts
                if quantity == 0:
                    rejected += 1
                    continue
                outcome = stream.next_outcome()
                if outcome is None:
                    return finish(PathEnd.SOURCE_EXHAUSTED)
                if not isinstance(outcome, TradeOutcome):
                    raise TypeError("outcome streams must return neutral TradeOutcome values")
                if outcome.no_trade:
                    abstentions += 1
                    continue
                if risk_points:
                    risk_points[-1] = replace(risk_points[-1], executed=True)
                if hasattr(engine, "before_trade"):
                    event = engine.before_trade()
                    if event is not None:
                        apply(event)
                trades += 1
                sizes.append(quantity)
                worst_gross = min((outcome.realized_pnl, *outcome.unrealized_marks))
                if -worst_gross + cost > loss:
                    exceedances += 1
                # Modeled round-trip costs are conservatively charged at entry.
                apply(RealizedPnlUpdated(engine.state.realized_pnl - cost * quantity))
                if terminal():
                    return finish(terminal())
                for mark in outcome.unrealized_marks:
                    apply(MarkToMarketUpdated(mark * quantity))
                    if terminal():
                        return finish(terminal())
                # Atomic transfer avoids phantom target/breach between components.
                apply(PnlSettled(engine.state.realized_pnl + outcome.realized_pnl * quantity))
                completed += 1
                if terminal():
                    return finish(terminal())
                if hasattr(engine, "after_trade"):
                    event = engine.after_trade()
                    if event is not None:
                        apply(event)
                    if terminal():
                        return finish(terminal())
            apply(EndOfDaySettled(engine.state.session_id))
            if terminal():
                return finish(terminal())
        return finish(PathEnd.HORIZON)


def wilson_interval(successes: int, trials: int, confidence: float = 0.95) -> tuple[float, float]:
    """Two-sided Wilson score interval for observed finite-horizon success."""
    if (type(trials) is not int or trials <= 0 or type(successes) is not int
            or not 0 <= successes <= trials or not 0 < confidence < 1):
        raise ValueError("invalid confidence interval inputs")
    z = NormalDist().inv_cdf((1 + confidence) / 2)
    p = successes / trials
    denominator = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denominator
    radius = z * sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return (0.0 if successes == 0 else max(0.0, center - radius),
            1.0 if successes == trials else min(1.0, center + radius))


def _times(values):
    if not values:
        return None
    ordered = sorted(values)
    def quantile(fraction):
        return ordered[max(0, ceil(fraction * len(ordered)) - 1)]
    return TimeSummary(len(ordered), ordered[0], float(median(ordered)),
                       quantile(.25), quantile(.75), quantile(.90), ordered[-1])


def summarize(policy: str, scenario: str, paths: tuple[PathResult, ...], *, seed=0, confidence=0.95,
              path_config: PathConfig | None = None):
    n = len(paths)
    passes = sum(path.end is PathEnd.PASSED for path in paths)
    breaches = sum(path.end is PathEnd.BREACHED for path in paths)
    interval = wilson_interval(passes, n, confidence)
    absorbed = [p.trades for p in paths if p.end in {PathEnd.PASSED, PathEnd.BREACHED}]
    sizes = [size for path in paths for size in path.contracts_used]
    return MonteCarloReport(policy, scenario, seed, n, passes, breaches, n - passes - breaches,
        sum(p.end is PathEnd.HORIZON for p in paths), sum(p.end is PathEnd.SOURCE_EXHAUSTED for p in paths), path_config,
        passes / n, (passes / n, (n - breaches) / n), confidence, interval, interval[0],
        float(mean(absorbed)) if absorbed else None,
        float(median(absorbed)) if absorbed else None,
        _times([p.trades for p in paths if p.end is PathEnd.PASSED]),
        tuple((cause, sum(p.breach_cause is cause for p in paths)) for cause in BreachCause),
        float(mean(sizes)) if sizes else 0.0, max(sizes, default=0),
        sum(p.rejected_entries for p in paths), sum(p.abstentions for p in paths),
        sum(p.estimate_exceedances for p in paths), paths)


def rank_reports(reports: tuple[MonteCarloReport, ...], *, base_scenario="base") -> tuple[PolicyComparison, ...]:
    """Rank minimum scenario LCB; never rank on best-case point estimate."""
    if not reports:
        raise ValueError("ranking requires reports")
    grouped = {}
    for report in reports:
        scenarios = grouped.setdefault(report.policy, {})
        if report.scenario in scenarios:
            raise ValueError("duplicate policy/scenario report")
        scenarios[report.scenario] = report
    expected = set(next(iter(grouped.values())))
    horizons = {r.path_config for r in reports}
    confidence_levels = {r.confidence_level for r in reports}
    if len(horizons) != 1 or len(confidence_levels) != 1:
        raise ValueError("ranking requires matching horizons and confidence levels")
    comparisons = []
    for name, scenarios in grouped.items():
        if set(scenarios) != expected or base_scenario not in scenarios:
            raise ValueError("all policies need the same scenarios including base")
        values = tuple(scenarios[key] for key in sorted(scenarios))
        comparisons.append(PolicyComparison(0, name, scenarios[base_scenario], values,
                                             min(r.lower_confidence_bound for r in values)))
    comparisons.sort(key=lambda r: (-r.worst_scenario_score, -r.base.lower_confidence_bound, r.policy))
    return tuple(replace(value, rank=index) for index, value in enumerate(comparisons, 1))


class ReachAvoidEvaluator:
    def __init__(self, engine_factory: Callable[[], PropRuleEngine], safety_config: RiskPolicyConfig,
                 estimate: ContractLossEstimate, path_config: PathConfig = PathConfig(), *,
                 entry_evaluator=None, instrument="SIM"):
        self.engine_factory, self.safety = engine_factory, RiskPolicy(safety_config)
        self.estimate, self.path_config = estimate, path_config
        self.entry_evaluator, self.instrument = entry_evaluator, instrument
        if not callable(engine_factory):
            raise ValueError("evaluator requires an engine factory, not a shared engine")
        self._starting_snapshot = None
        self._starting_state = None

    def evaluate(self, candidate: SizingCandidate, scenario: Scenario, *, simulations: int,
                 seed: int, confidence: float = 0.95) -> MonteCarloReport:
        if type(simulations) is not int or simulations <= 0 or type(seed) is not int:
            raise ValueError("simulation count must be positive integer and seed integer")
        wilson_interval(0, simulations, confidence)  # Validate before starting paths.
        paths = []
        for index in range(simulations):
            with localcontext(Context(prec=64)) as money_context:
                money_context.traps[Inexact] = True
                engine = self.engine_factory()
            owner = object()
            ownership.claim(engine, owner, "engines")
            if self._starting_snapshot is None:
                self._starting_snapshot = deepcopy(engine.snapshot())
                self._starting_state = deepcopy(engine.state)
            elif engine.state != self._starting_state or engine.snapshot() != self._starting_snapshot:
                raise ValueError("engine factory must return fresh engines with identical starting configuration/state")
            paths.append(simulate_path(engine, candidate, scenario, self.safety, self.estimate,
                                       self.path_config, path_context=path_seed_context(seed, scenario.name, index),
                                       _owner=owner, entry_evaluator=self.entry_evaluator, instrument=self.instrument))
        return summarize(candidate.name, scenario.name, tuple(paths), seed=seed, confidence=confidence,
                         path_config=self.path_config)

    def compare(self, candidates: tuple[SizingCandidate, ...], scenarios: tuple[Scenario, ...], *,
                simulations: int, seed: int, confidence: float = 0.95, base_scenario="base"):
        if (not candidates or not scenarios or len({c.name for c in candidates}) != len(candidates)
                or len({s.name for s in scenarios}) != len(scenarios)):
            raise ValueError("comparison requires unique nonempty policy/scenario names")
        # Scenario-name/path-index seeds are invariant under iteration order.
        # Policies share seeds within a scenario, never mutable source resources.
        reports = tuple(self.evaluate(c, s, simulations=simulations, seed=seed, confidence=confidence)
                        for c in candidates for s in scenarios)
        return rank_reports(reports, base_scenario=base_scenario)
