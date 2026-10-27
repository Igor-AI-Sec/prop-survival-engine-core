"""Conservative deterministic integer sizing; not a probability-of-passing model."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from typing import Callable

from prop_survival_engine.core.models import OrderIntent
from prop_survival_engine.oms.models import OmsState
from prop_survival_engine.prop_rules.models import AccountStatus
from prop_survival_engine.risk.features import RiskContext, RuinDistances, SessionState, derive_ruin_distances
from prop_survival_engine.risk.money import decimal_from_fraction, finite_decimal
from prop_survival_engine.risk.service import RiskAction, RiskDecision


class RiskReason(str, Enum):
    ALLOWED = "ALLOWED"
    ACCOUNT_BREACHED = "ACCOUNT_BREACHED"
    ACCOUNT_PASSED = "ACCOUNT_PASSED"
    OMS_NOT_READY = "OMS_NOT_READY"
    SESSION_NOT_OPEN = "SESSION_NOT_OPEN"
    INVALID_ACCOUNT_STATE = "INVALID_ACCOUNT_STATE"
    INVALID_REQUESTED_CONTRACTS = "INVALID_REQUESTED_CONTRACTS"
    NON_ENTRY_UNSUPPORTED = "NON_ENTRY_UNSUPPORTED"
    INVALID_LOSS_ESTIMATE = "INVALID_LOSS_ESTIMATE"
    NO_ACTIVE_LOSS_FLOOR = "NO_ACTIVE_LOSS_FLOOR"
    NON_POSITIVE_RUIN_DISTANCE = "NON_POSITIVE_RUIN_DISTANCE"
    TARGET_REACHED = "TARGET_REACHED"
    NO_SAFE_SIZE = "NO_SAFE_SIZE"
    RISK_BUDGET_LIMIT = "RISK_BUDGET_LIMIT"
    FLOOR_TOUCH_LIMIT = "FLOOR_TOUCH_LIMIT"
    MAX_CONTRACTS_LIMIT = "MAX_CONTRACTS_LIMIT"


@dataclass(frozen=True)
class RiskPolicyConfig:
    max_contracts: int
    max_fraction_of_ruin_distance_per_trade: Decimal = Decimal("0.25")
    minimum_ruin_buffer: Decimal = Decimal("1")
    execution_loss_buffer: Decimal = Decimal("0")  # per contract, account currency

    def __post_init__(self):
        fraction = self.max_fraction_of_ruin_distance_per_trade
        if type(self.max_contracts) is not int or self.max_contracts <= 0:
            raise ValueError("max_contracts must be a positive integer")
        if not finite_decimal(fraction) or not Decimal("0") < fraction <= Decimal("1"):
            raise ValueError("risk fraction must be a finite Decimal in (0, 1]")
        for value in (self.minimum_ruin_buffer, self.execution_loss_buffer):
            if not finite_decimal(value) or value < 0:
                raise ValueError("risk buffers must be finite nonnegative Decimal values")


@dataclass(frozen=True)
class ContractLossEstimate:
    """Explicit incremental per-contract adverse loss in account currency.

    stop_loss must be positive; costs are nonnegative round-trip estimates.
    Invalid/missing estimates are rejected by policy, never inferred from alpha,
    symbol, price or intent metadata. Stops are estimates, not fill guarantees.
    """
    stop_loss: Decimal | None = None
    fees_commissions: Decimal = Decimal("0")
    slippage_loss: Decimal = Decimal("0")


@dataclass(frozen=True)
class RiskEvaluation:
    """Detailed report wraps the stable, replay-compatible OMS decision type."""
    decision: RiskDecision
    requested_contracts: int | None  # None if the proposed quantity is not a valid integer
    approved_contracts: int
    reason_codes: tuple[RiskReason, ...]
    metrics: RuinDistances | None
    risk_budget: Decimal | None = None
    per_contract_worst_case_loss: Decimal | None = None
    approved_worst_case_loss: Decimal = Decimal("0")

    @property
    def action(self) -> RiskAction:
        return self.decision.action


class RiskPolicy:
    def __init__(self, config: RiskPolicyConfig):
        if not isinstance(config, RiskPolicyConfig):
            raise TypeError("RiskPolicy requires RiskPolicyConfig")
        self.config = config

    def evaluate(self, intent: OrderIntent, *, context: RiskContext,
                 estimate: ContractLossEstimate | None = None) -> RiskEvaluation:
        """Pure per-intent sizing. Only OMS may turn decision into an Order."""
        if not isinstance(intent, OrderIntent):
            raise TypeError("RiskPolicy evaluates OrderIntent only")
        requested = intent.quantity
        def reject(reasons, metrics=None, budget=None, loss=None):
            # OMS's existing rejected-intent journal omits quantity. Preserve
            # its None value for replay equality; the detailed approved size is 0.
            return RiskEvaluation(RiskDecision(RiskAction.REJECT, None, ",".join(r.value for r in reasons)),
                                  requested if type(requested) is int and requested > 0 else None,
                                  0, tuple(reasons), metrics, budget, loss)
        try:
            metrics = derive_ruin_distances(context)
        except ValueError:
            return reject([RiskReason.INVALID_ACCOUNT_STATE])
        reasons = []
        if type(requested) is not int or requested <= 0:
            reasons.append(RiskReason.INVALID_REQUESTED_CONTRACTS)
        if metrics.account_status is AccountStatus.BREACHED:
            reasons.append(RiskReason.ACCOUNT_BREACHED)
        elif metrics.account_status is AccountStatus.PASSED:
            reasons.append(RiskReason.ACCOUNT_PASSED)
        if context.oms_state is not OmsState.READY:
            reasons.append(RiskReason.OMS_NOT_READY)
        if metrics.session_state is not SessionState.OPEN:
            reasons.append(RiskReason.SESSION_NOT_OPEN)
        if intent.is_entry is not True:
            reasons.append(RiskReason.NON_ENTRY_UNSUPPORTED)
        distance = metrics.effective_ruin_distance
        if distance is None:
            reasons.append(RiskReason.NO_ACTIVE_LOSS_FLOOR)
        elif distance <= 0:
            reasons.append(RiskReason.NON_POSITIVE_RUIN_DISTANCE)
        if metrics.distance_to_target <= 0:
            reasons.append(RiskReason.TARGET_REACHED)
        if (not isinstance(estimate, ContractLossEstimate)
                or not finite_decimal(estimate.stop_loss) or estimate.stop_loss <= 0
                or any(not finite_decimal(value) or value < 0
                       for value in (estimate.fees_commissions, estimate.slippage_loss))):
            reasons.append(RiskReason.INVALID_LOSS_ESTIMATE)
        if reasons:
            return reject(reasons, metrics)
        # Fraction keeps division/rounding exact even under a low-precision or
        # non-default ambient Decimal context. Returned money remains Decimal.
        cushion = Fraction(distance)
        loss = (Fraction(estimate.stop_loss) + Fraction(estimate.fees_commissions)
                + Fraction(estimate.slippage_loss) + Fraction(self.config.execution_loss_buffer))
        budget = max(Fraction(0), min(
            Fraction(self.config.max_fraction_of_ruin_distance_per_trade) * cushion,
            cushion - Fraction(self.config.minimum_ruin_buffer)))
        budget_cap = budget // loss
        ratio = cushion / loss
        # Largest integer strictly less than D/L: touching a floor is unsafe,
        # including when fraction=1 and minimum buffer=0.
        touch_cap = (ratio.numerator - 1) // ratio.denominator
        approved = min(requested, self.config.max_contracts, budget_cap, touch_cap)
        reasons = []
        if requested > self.config.max_contracts:
            reasons.append(RiskReason.MAX_CONTRACTS_LIMIT)
        if requested > budget_cap:
            reasons.append(RiskReason.RISK_BUDGET_LIMIT)
        if requested > touch_cap:
            reasons.append(RiskReason.FLOOR_TOUCH_LIMIT)
        if approved == 0:
            return reject([RiskReason.NO_SAFE_SIZE, *reasons], metrics,
                          decimal_from_fraction(budget), decimal_from_fraction(loss))
        action = RiskAction.ALLOW if approved == requested else RiskAction.REDUCE
        if not reasons:
            reasons = [RiskReason.ALLOWED]
        decision = RiskDecision(action, approved, ",".join(r.value for r in reasons))
        return RiskEvaluation(decision, requested, approved, tuple(reasons), metrics,
                              decimal_from_fraction(budget), decimal_from_fraction(loss),
                              decimal_from_fraction(approved * loss))


class PolicyRiskEvaluator:
    """Keep the existing RiskEvaluator.evaluate(intent) -> RiskDecision contract.

    Providers supply a fresh context/estimate. This object neither polls a vendor
    nor mutates account state. Consumers needing metrics call evaluate_details.
    """
    def __init__(self, policy: RiskPolicy, context_provider: Callable[[], RiskContext],
                 estimate_provider: Callable[[OrderIntent], ContractLossEstimate | None]):
        self.policy = policy
        self.context_provider = context_provider
        self.estimate_provider = estimate_provider

    def evaluate_details(self, intent: OrderIntent) -> RiskEvaluation:
        return self.policy.evaluate(intent, context=self.context_provider(), estimate=self.estimate_provider(intent))

    def evaluate(self, intent: OrderIntent) -> RiskDecision:
        return self.evaluate_details(intent).decision
