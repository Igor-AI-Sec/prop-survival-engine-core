"""Offline challenge experiments composed from the existing reach-avoid engine.

Money is Decimal/Fraction. Floats occur only in existing statistical summaries.
No catalog claim is converted into an executable rule or defaulted here.
"""
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from fractions import Fraction
import json

from prop_survival_engine.core.instruments import INSTRUMENTS
from prop_survival_engine.prop_rules.catalog import ReviewedPlan, Operation
from prop_survival_engine.prop_rules.operations import verify_challenge
from prop_survival_engine.prop_rules.plan_engine import PlanRuleEngine, plan_evaluator
from prop_survival_engine.prop_rules.plans import PlanStage, RetryFee
from prop_survival_engine.prop_rules.calendar import SimulationTimeline
from prop_survival_engine.prop_rules.provenance import PlanConfiguration, VerificationContext
from prop_survival_engine.risk.features import RiskContext, SessionState, ActiveLossFloors, derive_ruin_distances
from prop_survival_engine.oms.models import OmsState
from prop_survival_engine.risk.instruments import (
    StopDistance, ExecutionCostAssumptions, InstrumentRiskPolicy, instrument_loss, analyze_granularity,
)
from prop_survival_engine.risk.contract_limits import ContractLimits
from prop_survival_engine.risk.money import decimal_from_fraction, finite_decimal
from prop_survival_engine.risk.policy import RiskPolicy, RiskPolicyConfig
from prop_survival_engine.simulation.outcomes import TradeOutcome, IIDOutcomeSource, SequenceOutcomeSource
from prop_survival_engine.simulation.reach_avoid import (
    PathConfig, PathEnd, ReachAvoidEvaluator, Scenario, MonteCarloReport, rank_reports,
)
from prop_survival_engine.simulation.sizing import FixedContracts, FractionOfCushion, CushionLadder


def exact(value):
    return decimal_from_fraction(Fraction(value))


class Sampling(str, Enum):
    SEQUENCE = "SEQUENCE"
    IID = "IID"


@dataclass(frozen=True)
class StopMultipleOutcome:
    """Gross stop multiples and intra-trade marks; supplied assumptions, not alpha."""
    realized: Decimal
    marks: tuple[Decimal, ...] = ()

    def __post_init__(self):
        if (not finite_decimal(self.realized) or type(self.marks) is not tuple
                or any(not finite_decimal(v) for v in self.marks)):
            raise ValueError("finite Decimal gross stop multiples required")


@dataclass(frozen=True)
class StrategyProfile:
    name: str
    fictional: bool
    stop: StopDistance
    outcomes: tuple[StopMultipleOutcome, ...]
    sizing: FixedContracts | FractionOfCushion | CushionLadder
    opportunities_per_session: int
    sampling: Sampling
    weights: tuple[int, ...]
    no_trade_probability: Fraction
    repeat_sequence: bool

    def __post_init__(self):
        if type(self.name) is not str or not self.name.strip() or type(self.fictional) is not bool:
            raise ValueError("explicit profile identity and fictional label required")
        if (type(self.stop) is not StopDistance or type(self.outcomes) is not tuple or not self.outcomes
                or any(type(v) is not StopMultipleOutcome for v in self.outcomes)
                or type(self.sizing) not in (FixedContracts, FractionOfCushion, CushionLadder)):
            raise ValueError("typed stop, immutable outcomes and existing monotone sizing required")
        if type(self.opportunities_per_session) is not int or self.opportunities_per_session <= 0:
            raise ValueError("positive opportunity frequency required")
        if (type(self.sampling) is not Sampling or type(self.repeat_sequence) is not bool
                or type(self.weights) is not tuple or len(self.weights) != len(self.outcomes)
                or any(type(v) is not int or v <= 0 for v in self.weights)
                or type(self.no_trade_probability) is not Fraction or not 0 <= self.no_trade_probability <= 1):
            raise ValueError("explicit sampling, weights and exact no-trade probability required")
        if self.sampling is Sampling.SEQUENCE and (self.no_trade_probability or len(set(self.weights)) != 1):
            raise ValueError("sequence sampling cannot approximate probability or weighted atoms")

    def source(self, symbol):
        loss = instrument_loss(INSTRUMENTS.lookup(symbol), self.stop,
                               ExecutionCostAssumptions(Decimal(0), Decimal(0), Decimal(0)))
        atoms = tuple(TradeOutcome(exact(Fraction(v.realized) * Fraction(loss.stop_money)),
            tuple(exact(Fraction(m) * Fraction(loss.stop_money)) for m in v.marks)) for v in self.outcomes)
        if self.sampling is Sampling.SEQUENCE:
            return SequenceOutcomeSource(atoms, self.repeat_sequence)
        p = self.no_trade_probability
        # Integer weights keep the abstention probability exact, without float RNG thresholds.
        weights = tuple(v * (p.denominator - p.numerator) for v in self.weights)
        if p == 1:
            return IIDOutcomeSource((TradeOutcome(Decimal(0), no_trade=True),), (1,))
        if p:
            atoms += (TradeOutcome(Decimal(0), no_trade=True),)
            weights += (sum(self.weights) * p.numerator,)
        return IIDOutcomeSource(atoms, weights)


@dataclass(frozen=True)
class ChallengePlan:
    name: str
    configuration: PlanConfiguration | ReviewedPlan
    timeline: SimulationTimeline | None = None

    def __post_init__(self):
        if type(self.name) is not str or not self.name.strip() or type(self.configuration) not in (PlanConfiguration, ReviewedPlan):
            raise ValueError("named, explicitly fictional or evidenced plan required")
        if self.timeline is not None and type(self.timeline) is not SimulationTimeline:
            raise ValueError("typed explicit opportunity timeline required")


@dataclass(frozen=True)
class CostScenario:
    name: str
    costs: ExecutionCostAssumptions

    def __post_init__(self):
        if type(self.name) is not str or not self.name.strip() or type(self.costs) is not ExecutionCostAssumptions:
            raise ValueError("explicit named provider-neutral costs required")


def challenge_blockers(named: ChallengePlan, context: VerificationContext):
    if type(named) is not ChallengePlan or type(context) is not VerificationContext:
        raise ValueError("typed plan and explicit evidence freshness context required")
    configuration = named.configuration
    if type(configuration) is ReviewedPlan:
        report = configuration.qualify(Operation.CHALLENGE, context)
        return report.blockers or (("plan_binding", ("COMPLETE_AUDITED_PLAN_BINDING_REQUIRED",)),)
    report = verify_challenge(configuration, context)
    blockers = tuple((r.rule_path, r.reasons) for r in report.unresolved)
    plan = configuration.plan
    if plan.stage is not PlanStage.EVALUATION:
        blockers += (("stage", ("CHALLENGE_ONLY",)),)
    if not plan.breach_on_touch or not plan.target_on_touch or (not plan.target_uses_unrealized and not plan.target_requires_flat):
        blockers += (("boundary_semantics", ("EXISTING_SAFETY_INCOMPATIBLE",)),)
    if plan.calendar_json is not None and named.timeline is None:
        blockers += (("calendar_timeline", ("EXPLICIT_OPPORTUNITY_CLOCK_REQUIRED",)),)
    return tuple(sorted(blockers))


class ChallengeEnd(str, Enum):
    PASS = "PASS"
    BREACH = "BREACH"
    CENSORED = "CENSORED"


@dataclass(frozen=True)
class ChallengePathMetrics:
    status: ChallengeEnd
    trades_taken: int
    sessions_started: int
    trading_sessions: int
    final_balance: Decimal
    final_equity: Decimal
    maximum_drawdown: Decimal
    minimum_ruin_cushion: Decimal
    average_approved_size: Fraction | None
    maximum_approved_size: int
    zero_size_count: int
    skipped_trade_count: int
    total_risk_budget_offered: Decimal
    total_risk_used: Decimal
    risk_utilization: Fraction | None
    granularity_unused_budget: Decimal
    commissions: Decimal
    adverse_slippage: Decimal
    execution_buffer_charged: Decimal
    first_passage_trades: int | None
    first_passage_sessions: int | None


def path_metrics(path, loss, initial, active_floors=ActiveLossFloors()):
    trace = path.trace
    states = (initial,) + tuple(point.state for point in trace)
    high = Fraction(initial.equity)
    drawdown = Fraction(0)
    cushion = []
    for state in states:
        high = max(high, Fraction(state.equity))
        drawdown = max(drawdown, high - Fraction(state.equity))
        cushion.append(derive_ruin_distances(RiskContext(state, OmsState.READY, SessionState.OPEN, active_floors)).effective_ruin_distance)
    sessions = {p.state.session_id for p in trace if p.event == "SessionStarted"}
    trading = {p.state.session_id for p in trace if p.event == "PnlSettled" or
               (p.event == "RealizedPnlUpdated" and p.trades > 0)}
    points = path.risk_points
    offered = sum((Fraction(p.budget) for p in points if p.budget is not None), Fraction(0))
    used = sum((p.approved * Fraction(p.loss) for p in points if p.executed), Fraction(0))
    granular = sum((Fraction(analyze_granularity(p.budget, p.loss).unused_risk)
                   for p in points if p.budget is not None), Fraction(0))
    quantity = sum(path.contracts_used)
    status = (ChallengeEnd.PASS if path.end is PathEnd.PASSED else
              ChallengeEnd.BREACH if path.end is PathEnd.BREACHED else ChallengeEnd.CENSORED)
    return ChallengePathMetrics(status, path.trades, len(sessions), len(trading),
        exact(Fraction(initial.starting_balance) + Fraction(path.final_state.realized_pnl)),
        path.final_state.equity, exact(drawdown), min(cushion),
        Fraction(quantity, len(path.contracts_used)) if path.contracts_used else None,
        max(path.contracts_used, default=0), path.rejected_entries, path.abstentions,
        exact(offered), exact(used), used / offered if offered else None, exact(granular),
        exact(quantity * Fraction(loss.round_trip_fees)),
        exact(quantity * Fraction(loss.slippage_money)),
        exact(quantity * Fraction(loss.execution_buffer)),
        path.trades if status is not ChallengeEnd.CENSORED else None,
        len(sessions) if status is not ChallengeEnd.CENSORED else None)


@dataclass(frozen=True)
class AttemptEconomics:
    status: str
    reasons: tuple[tuple[str, tuple[str, ...]], ...]
    expected_attempts: Fraction | None
    expected_evaluation_fees: Fraction | None


def attempt_economics(configuration, context, probability: Fraction, *, lower_bound: float,
                      minimum_lower_bound: Decimal):
    """Independent bounded attempts; excludes activation, payout and recurring charges."""
    if (type(probability) is not Fraction or not 0 <= probability <= 1
            or not finite_decimal(minimum_lower_bound) or not 0 <= minimum_lower_bound <= 1
            or not 0 <= lower_bound <= 1):
        raise ValueError("explicit probability and confidence threshold required")
    if type(configuration) is not PlanConfiguration:
        return AttemptEconomics("BLOCKED", (("fees", ("AUDITED_FEE_BINDING_REQUIRED",)),), None, None)
    report = configuration.verify(context)
    required = {"fees.evaluation", "fees.reset", "fees.retry_fee"}
    blockers = tuple((r.rule_path, r.reasons) for r in report.unresolved if r.rule_path in required)
    if blockers:
        return AttemptEconomics("BLOCKED", blockers, None, None)
    if probability == 0:
        return AttemptEconomics("UNAVAILABLE_ZERO_PASS_PROBABILITY", (), None, None)
    if Decimal(str(lower_bound)) < minimum_lower_bound:
        return AttemptEconomics("UNAVAILABLE_INSUFFICIENT_CONFIDENCE", (), None, None)
    attempts = 1 / probability
    fees = configuration.plan.fees
    retry = fees.reset if fees.retry_fee is RetryFee.RESET else fees.evaluation
    return AttemptEconomics("AVAILABLE", (), attempts,
        Fraction(fees.evaluation) + (attempts - 1) * Fraction(retry))


@dataclass(frozen=True)
class ChallengeCell:
    plan: str
    instrument: str
    strategy: str
    scenario: str
    seed: int
    status: str
    blockers: tuple[tuple[str, tuple[str, ...]], ...]
    report: MonteCarloReport | None = None
    paths: tuple[ChallengePathMetrics, ...] = ()
    economics: AttemptEconomics | None = None

    @property
    def mean_sessions_to_pass(self):
        values = [p.first_passage_sessions for p in self.paths if p.status is ChallengeEnd.PASS]
        return Fraction(sum(values), len(values)) if values else None

    @property
    def mean_trades_to_pass(self):
        values = [p.trades_taken for p in self.paths if p.status is ChallengeEnd.PASS]
        return Fraction(sum(values), len(values)) if values else None

    @property
    def average_risk_utilization(self):
        values = [p.risk_utilization for p in self.paths if p.risk_utilization is not None]
        return sum(values, Fraction(0)) / len(values) if values else None


def simulate_challenge(named, symbol, profile, scenario, *, context, safety_config,
                       simulations, seed, max_sessions, confidence=0.95, limits=None,
                       minimum_economics_lower_bound=Decimal("0.05")):
    if type(profile) is not StrategyProfile or type(scenario) is not CostScenario or type(safety_config) is not RiskPolicyConfig:
        raise ValueError("typed profile, costs and safety configuration required")
    INSTRUMENTS.lookup(symbol)
    if limits is not None and type(limits) is not ContractLimits:
        raise ValueError("typed contract limits required")
    blockers = challenge_blockers(named, context)
    identity = (named.name, symbol, profile.name, scenario.name, seed)
    if blockers:
        return ChallengeCell(*identity, "BLOCKED", blockers)
    plan = named.configuration.plan
    loss = instrument_loss(INSTRUMENTS.lookup(symbol), profile.stop, scenario.costs)
    horizon = PathConfig(max_sessions, profile.opportunities_per_session, True)
    # Reuse the existing plan adapter's representability checks and plan size cap.
    template = plan_evaluator(plan, safety_config, loss.policy_estimate(), horizon, timeline=named.timeline)
    wrapper = InstrumentRiskPolicy(RiskPolicy(template.safety.config))

    def approve(intent, state_context):
        converted = wrapper.evaluate(intent, context=state_context, stop=profile.stop,
            costs=scenario.costs, limits=limits, existing_exposure=() if limits is not None else None)
        return replace(converted.risk_evaluation, decision=converted.decision,
            approved_contracts=converted.approved_contracts,
            approved_worst_case_loss=converted.approved_worst_case_loss)

    evaluator = ReachAvoidEvaluator(template.engine_factory, template.safety.config,
        loss.policy_estimate(), horizon, entry_evaluator=approve, instrument=symbol)
    candidate = replace(profile.sizing, name=json.dumps([named.name, symbol, profile.name], separators=(",", ":")))
    report = evaluator.evaluate(candidate, Scenario(scenario.name, profile.source(symbol)),
                                simulations=simulations, seed=seed, confidence=confidence)
    # Slippage is reported in money; policy safety buffer is not charged as a fee.
    initial_engine = PlanRuleEngine(plan)
    initial = initial_engine.state
    paths = tuple(path_metrics(p, loss, initial, initial_engine.risk_context().active_floors) for p in report.paths)
    economics = attempt_economics(named.configuration, context, Fraction(report.passes, simulations),
        lower_bound=report.lower_confidence_bound, minimum_lower_bound=minimum_economics_lower_bound)
    return ChallengeCell(*identity, "SIMULATED", (), report, paths, economics)


def experiment_matrix(plans, instruments, profiles, scenarios, seeds, **kwargs):
    dimensions = (plans, instruments, profiles, scenarios, seeds)
    if any(type(values) is not tuple for values in dimensions):
        raise ValueError("immutable matrix dimensions required")
    keys = ([p.name for p in plans], list(instruments), [p.name for p in profiles],
            [s.name for s in scenarios], list(seeds))
    if any(not values or len(values) != len(set(values)) for values in keys):
        raise ValueError("nonempty unique matrix dimensions required")
    return tuple(simulate_challenge(p, instrument, profile, scenario, seed=seed, **kwargs)
        for p in dimensions[0] for instrument in dimensions[1] for profile in dimensions[2]
        for scenario in dimensions[3] for seed in dimensions[4])


def rank_challenges(cells, *, seed, base_scenario="base"):
    reports = tuple(c.report for c in cells if c.status == "SIMULATED" and c.seed == seed)
    return rank_reports(reports, base_scenario=base_scenario) if reports else ()


def format_experiment(cells):
    rows = ["PLAN | INSTRUMENT | STRATEGY | SCENARIO | SEED | STATUS | P(pass) | Wilson lower | P(breach) | censoring | risk utilization | mean size | mean sessions/pass | blockers"]
    for c in sorted(cells, key=lambda v: (v.plan, v.instrument, v.strategy, v.scenario, v.seed)):
        r = c.report
        fields = (c.plan, c.instrument, c.strategy, c.scenario, c.seed, c.status,
            r.pass_probability if r else None, r.lower_confidence_bound if r else None,
            r.breaches / r.simulations if r else None, r.censored / r.simulations if r else None,
            c.average_risk_utilization, r.average_contracts if r else None, c.mean_sessions_to_pass,
            json.dumps(c.blockers, separators=(",", ":")) if c.blockers else "")
        rows.append(" | ".join("unavailable" if v is None else str(v) for v in fields))
    return "\n".join(rows)
