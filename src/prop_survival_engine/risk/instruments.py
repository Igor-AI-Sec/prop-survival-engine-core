"""Exact instrument loss conversion and delegation to the existing RiskPolicy."""
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from fractions import Fraction

from prop_survival_engine.core.instruments import INSTRUMENTS, InstrumentCatalog, InstrumentSpec
from prop_survival_engine.core.models import OrderIntent
from prop_survival_engine.risk.money import decimal_from_fraction
from prop_survival_engine.risk.policy import ContractLossEstimate, RiskEvaluation, RiskPolicy
from prop_survival_engine.risk.contract_limits import ContractLimitResult, ContractLimits, constrain_contracts
from prop_survival_engine.risk.service import RiskAction, RiskDecision


def _money(value, *, positive=False):
    if type(value) is not Decimal or not value.is_finite() or value < 0 or (positive and value == 0):
        raise ValueError("finite nonnegative Decimal required (positive for stop/loss)")


def _ticks(value):
    if type(value) is not int or value < 0:
        raise ValueError("nonnegative integer ticks required")


def points_to_ticks(spec: InstrumentSpec, points: Decimal) -> int:
    _money(points)
    if type(spec) is not InstrumentSpec:
        raise ValueError("InstrumentSpec required")
    ticks = Fraction(points) / Fraction(spec.tick_size)
    if ticks.denominator != 1:
        raise ValueError("price distance is not tick-aligned; no rounding permitted")
    return ticks.numerator


def ticks_to_points(spec: InstrumentSpec, ticks: int) -> Decimal:
    _ticks(ticks)
    if type(spec) is not InstrumentSpec:
        raise ValueError("InstrumentSpec required")
    return decimal_from_fraction(ticks * Fraction(spec.tick_size))


def ticks_to_money(spec: InstrumentSpec, ticks: int) -> Decimal:
    _ticks(ticks)
    if type(spec) is not InstrumentSpec:
        raise ValueError("InstrumentSpec required")
    return decimal_from_fraction(ticks * Fraction(spec.tick_value))


def points_to_money(spec: InstrumentSpec, points: Decimal) -> Decimal:
    return ticks_to_money(spec, points_to_ticks(spec, points))


class StopUnit(str, Enum):
    POINTS = "POINTS"
    TICKS = "TICKS"


@dataclass(frozen=True)
class StopDistance:
    value: Decimal
    unit: StopUnit

    def __post_init__(self):
        _money(self.value, positive=True)
        if type(self.unit) is not StopUnit:
            raise ValueError("explicit stop unit required")
        if self.unit is StopUnit.TICKS and Fraction(self.value).denominator != 1:
            raise ValueError("stop ticks must be whole ticks")


@dataclass(frozen=True)
class ExecutionCostAssumptions:
    # All three are mandatory. Zero-cost research must explicitly supply zeros.
    round_trip_fees: Decimal
    adverse_slippage_ticks: Decimal
    execution_buffer: Decimal

    def __post_init__(self):
        for value in (self.round_trip_fees, self.adverse_slippage_ticks, self.execution_buffer):
            _money(value)


@dataclass(frozen=True)
class InstrumentLoss:
    instrument: str
    stop_ticks: int
    stop_points: Decimal
    stop_money: Decimal
    round_trip_fees: Decimal
    slippage_money: Decimal
    execution_buffer: Decimal
    total_loss: Decimal

    def policy_estimate(self) -> ContractLossEstimate:
        # Policy's separate configured buffer remains additive and untouched.
        return ContractLossEstimate(self.stop_money, self.round_trip_fees,
            decimal_from_fraction(Fraction(self.slippage_money) + Fraction(self.execution_buffer)))


def instrument_loss(spec: InstrumentSpec, stop: StopDistance, costs: ExecutionCostAssumptions) -> InstrumentLoss:
    if type(spec) is not InstrumentSpec or type(stop) is not StopDistance or type(costs) is not ExecutionCostAssumptions:
        raise ValueError("explicit typed instrument, stop and costs required")
    ticks = points_to_ticks(spec, stop.value) if stop.unit is StopUnit.POINTS else Fraction(stop.value).numerator
    stop_money = ticks_to_money(spec, ticks)
    # Expected adverse slippage may be fractional ticks; stop distance may not.
    slip = decimal_from_fraction(Fraction(costs.adverse_slippage_ticks) * Fraction(spec.tick_value))
    total = decimal_from_fraction(Fraction(stop_money) + Fraction(costs.round_trip_fees)
                                  + Fraction(slip) + Fraction(costs.execution_buffer))
    return InstrumentLoss(spec.canonical_symbol, ticks, ticks_to_points(spec, ticks), stop_money,
                          costs.round_trip_fees, slip, costs.execution_buffer, total)


@dataclass(frozen=True)
class RiskGranularity:
    max_contracts: int
    used_risk: Decimal
    unused_risk: Decimal
    utilization: Fraction


def analyze_granularity(budget: Decimal, per_contract_loss: Decimal, *, max_contracts: int | None = None) -> RiskGranularity:
    """Inclusive monetary fit only; not account-floor or trading authorization."""
    _money(budget)
    _money(per_contract_loss, positive=True)
    if max_contracts is not None and (type(max_contracts) is not int or max_contracts < 0):
        raise ValueError("nonnegative integer cap required")
    quantity = Fraction(budget) // Fraction(per_contract_loss)
    if max_contracts is not None:
        quantity = min(quantity, max_contracts)
    used = quantity * Fraction(per_contract_loss)
    return RiskGranularity(quantity, decimal_from_fraction(used),
        decimal_from_fraction(Fraction(budget) - used), used / Fraction(budget) if budget else Fraction(0))


@dataclass(frozen=True)
class InstrumentRiskEvaluation:
    loss: InstrumentLoss
    risk_evaluation: RiskEvaluation
    contract_limits: ContractLimitResult
    decision: RiskDecision
    approved_contracts: int
    effective_per_contract_loss: Decimal
    approved_worst_case_loss: Decimal
    effective_risk_utilization: Fraction | None
    unused_risk_budget: Decimal | None


class InstrumentRiskPolicy:
    def __init__(self, policy: RiskPolicy, catalog: InstrumentCatalog = INSTRUMENTS):
        if type(policy) is not RiskPolicy or type(catalog) is not InstrumentCatalog:
            raise ValueError("existing RiskPolicy and reviewed catalog required")
        self.policy, self.catalog = policy, catalog

    def evaluate(self, intent: OrderIntent, *, context, stop: StopDistance,
                 costs: ExecutionCostAssumptions, limits: ContractLimits | None = None,
                 existing_exposure=None) -> InstrumentRiskEvaluation:
        if type(intent) is not OrderIntent:
            raise ValueError("OrderIntent required; executable orders remain OMS-owned")
        if limits is None and existing_exposure is not None:
            raise ValueError("exposure input requires explicit contract-limit policy")
        loss = instrument_loss(self.catalog.lookup(intent.instrument), stop, costs)
        risk = self.policy.evaluate(intent, context=context, estimate=loss.policy_estimate())
        bounds = (constrain_contracts(self.catalog.lookup(intent.instrument), risk.approved_contracts, limits, existing_exposure)
                  if limits is not None else ContractLimitResult(risk.approved_contracts, ()))
        approved = bounds.approved_contracts
        decision = risk.decision
        if approved < risk.approved_contracts:
            action = RiskAction.REDUCE if approved else RiskAction.REJECT
            decision = RiskDecision(action, approved if approved else None, ",".join(bounds.reason_codes))
        effective = Fraction(loss.total_loss) + Fraction(self.policy.config.execution_loss_buffer)
        used = approved * effective
        budget = Fraction(risk.risk_budget) if risk.risk_budget is not None else None
        return InstrumentRiskEvaluation(loss, risk, bounds, decision, approved,
            decimal_from_fraction(effective), decimal_from_fraction(used),
            used / budget if budget else Fraction(0) if budget is not None else None,
            decimal_from_fraction(budget - used) if budget is not None else None)
