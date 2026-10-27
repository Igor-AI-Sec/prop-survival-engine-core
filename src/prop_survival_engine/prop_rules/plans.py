"""Versioned, fictional-or-researched rule specifications; no firm identifiers.

None explicitly disables optional rules, never means an unknown rule. Research
must resolve unknown semantics before constructing a plan.
"""
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from enum import Enum
from fractions import Fraction
import json


class DrawdownType(str, Enum):
    STATIC = "STATIC"
    EOD_TRAILING = "EOD_TRAILING"
    INTRADAY_TRAILING = "INTRADAY_TRAILING"


class PlanStage(str, Enum):
    EVALUATION = "EVALUATION"
    FUNDED = "FUNDED"


class PayoutFloorEffect(str, Enum):
    UNCHANGED = "UNCHANGED"
    LOWER_BY_WITHDRAWAL = "LOWER_BY_WITHDRAWAL"


class RetryFee(str, Enum):
    RESET = "RESET"
    NEW_EVALUATION = "NEW_EVALUATION"


class DailyLossAction(str, Enum):
    HARD_BREACH = "HARD_BREACH"
    SOFT_PAUSE = "SOFT_PAUSE"


class ConsistencyMode(str, Enum):
    ELIGIBILITY = "ELIGIBILITY"
    EXTEND_TARGET = "EXTEND_TARGET"


def money(value, *, positive=False):
    if type(value) is not Decimal or not value.is_finite() or value < 0 or (positive and value == 0):
        raise ValueError("money must be finite nonnegative Decimal (positive where required)")


def integer(value, *, positive=False):
    if type(value) is not int or value < 0 or (positive and value == 0):
        raise ValueError("count must be nonnegative integer (positive where required)")


@dataclass(frozen=True)
class Fees:
    evaluation: Decimal
    reset: Decimal
    activation: Decimal
    retry_fee: RetryFee

    def __post_init__(self):
        for value in (self.evaluation, self.reset, self.activation):
            money(value)
        if type(self.retry_fee) is not RetryFee:
            raise ValueError("explicit retry fee policy required")


@dataclass(frozen=True)
class PayoutRules:
    enabled: bool
    minimum_trading_days: int
    minimum_profit: Decimal
    per_payout_cap: Decimal | None
    lifetime_cap: Decimal | None
    profit_split: Decimal
    safety_net: Decimal
    floor_effect: PayoutFloorEffect

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.floor_effect) is not PayoutFloorEffect:
            raise ValueError("explicit payout enablement and floor effect required")
        integer(self.minimum_trading_days)
        for value in (self.minimum_profit, self.safety_net, self.profit_split):
            money(value)
        if not 0 < self.profit_split <= 1:
            raise ValueError("profit split must be in (0, 1]")
        for value in (self.per_payout_cap, self.lifetime_cap):
            if value is not None:
                money(value, positive=True)


@dataclass(frozen=True)
class PropPlan:
    starting_balance: Decimal
    profit_target_gain: Decimal
    maximum_drawdown: Decimal
    drawdown_type: DrawdownType
    unrealized_moves_floor: bool
    # Boundary checks always include unrealized equity in v0.16A.
    daily_loss_limit: Decimal | None
    consistency_fraction: Decimal | None  # best positive day's net PnL / total net profit
    minimum_trading_days: int
    maximum_position_size: int
    stage: PlanStage
    fees: Fees
    payouts: PayoutRules
    breach_on_touch: bool
    target_on_touch: bool
    target_uses_unrealized: bool
    terminal_breach: bool
    terminal_pass: bool
    trailing_floor_cap: Decimal | None = None
    daily_loss_action: DailyLossAction = DailyLossAction.HARD_BREACH
    consistency_mode: ConsistencyMode = ConsistencyMode.ELIGIBILITY
    target_requires_flat: bool = False
    calendar_json: str | None = None

    def __post_init__(self):
        for value in (self.starting_balance, self.profit_target_gain, self.maximum_drawdown):
            money(value, positive=True)
        if self.maximum_drawdown >= self.starting_balance:
            raise ValueError("drawdown must be below starting balance")
        if type(self.drawdown_type) is not DrawdownType or type(self.stage) is not PlanStage:
            raise ValueError("explicit drawdown type and stage required")
        for value in (self.unrealized_moves_floor, self.breach_on_touch, self.target_on_touch,
                      self.target_uses_unrealized, self.terminal_breach, self.terminal_pass):
            if type(value) is not bool:
                raise ValueError("rule switches must be booleans")
        if not self.terminal_breach or not self.terminal_pass:
            raise ValueError("nonterminal breach/pass unsupported")
        if self.drawdown_type is DrawdownType.STATIC and self.unrealized_moves_floor:
            raise ValueError("static floor cannot move with unrealized PnL")
        if self.daily_loss_limit is not None:
            money(self.daily_loss_limit, positive=True)
        if self.consistency_fraction is not None:
            money(self.consistency_fraction, positive=True)
            if self.consistency_fraction > 1:
                raise ValueError("consistency fraction must be <= 1")
        integer(self.minimum_trading_days)
        integer(self.maximum_position_size, positive=True)
        if type(self.fees) is not Fees or type(self.payouts) is not PayoutRules:
            raise ValueError("typed fee and payout rules required")
        if self.stage is PlanStage.EVALUATION and self.payouts.enabled:
            raise ValueError("evaluation payouts unsupported; model funded stage separately")
        if (type(self.daily_loss_action) is not DailyLossAction or type(self.consistency_mode) is not ConsistencyMode
                or type(self.target_requires_flat) is not bool):
            raise ValueError("explicit challenge semantic modes required")
        if self.trailing_floor_cap is not None:
            money(self.trailing_floor_cap)
            if (self.drawdown_type is DrawdownType.STATIC or
                    Fraction(self.trailing_floor_cap) < Fraction(self.starting_balance) - Fraction(self.maximum_drawdown)):
                raise ValueError("trailing cap must be at or above the initial floor")
        if self.daily_loss_action is DailyLossAction.SOFT_PAUSE and self.daily_loss_limit is None:
            raise ValueError("soft DLL requires an explicit daily limit")
        if self.consistency_mode is ConsistencyMode.EXTEND_TARGET and (self.consistency_fraction is None or self.stage is not PlanStage.EVALUATION):
            raise ValueError("target extension requires evaluation and explicit consistency fraction")
        if self.calendar_json is not None:
            from prop_survival_engine.prop_rules.calendar import TradingCalendar
            if type(self.calendar_json) is not str:
                raise ValueError("canonical calendar serialization required")
            TradingCalendar.from_json(self.calendar_json)

    def to_json(self) -> str:
        def encode(value):
            if is_dataclass(value):
                return {f.name: encode(getattr(value, f.name)) for f in fields(value)}
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, Decimal):
                return str(value)
            return value
        return json.dumps({"version": 1, "plan": encode(self)}, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate serialization field")
                result[key] = value
            return result
        def exact(data, kind):
            if type(data) is not dict or set(data) != {f.name for f in fields(kind)}:
                raise ValueError("missing or unknown specification field")
            return dict(data)
        def amount(value):
            if type(value) is not str:
                raise ValueError("serialized money must be decimal strings")
            return Decimal(value)
        try:
            envelope = json.loads(text, object_pairs_hook=unique)
            if set(envelope) != {"version", "plan"} or type(envelope["version"]) is not int or envelope["version"] != 1:
                raise ValueError("unsupported plan serialization")
            serialized = envelope["plan"]
            # An entirely absent additive extension is the historical v1 policy,
            # not unknown external evidence. New external leaves still require
            # provenance; partially specified extensions reject rather than default.
            extension = {"trailing_floor_cap": None, "daily_loss_action": "HARD_BREACH",
                         "consistency_mode": "ELIGIBILITY", "target_requires_flat": False,
                         "calendar_json": None}
            if type(serialized) is dict and not set(extension).intersection(serialized):
                serialized = dict(serialized, **extension)
            data = exact(serialized, cls)
            fee = exact(data["fees"], Fees)
            for key in ("evaluation", "reset", "activation"):
                fee[key] = amount(fee[key])
            fee["retry_fee"] = RetryFee(fee["retry_fee"])
            data["fees"] = Fees(**fee)
            payout = exact(data["payouts"], PayoutRules)
            for key in ("minimum_profit", "profit_split", "safety_net", "per_payout_cap", "lifetime_cap"):
                if payout[key] is not None:
                    payout[key] = amount(payout[key])
            payout["floor_effect"] = PayoutFloorEffect(payout["floor_effect"])
            data["payouts"] = PayoutRules(**payout)
            for key in ("starting_balance", "profit_target_gain", "maximum_drawdown", "daily_loss_limit", "consistency_fraction", "trailing_floor_cap"):
                if data[key] is not None:
                    data[key] = amount(data[key])
            data["drawdown_type"] = DrawdownType(data["drawdown_type"])
            data["stage"] = PlanStage(data["stage"])
            data["daily_loss_action"] = DailyLossAction(data["daily_loss_action"])
            data["consistency_mode"] = ConsistencyMode(data["consistency_mode"])
            return cls(**data)
        except (TypeError, KeyError, ValueError, ArithmeticError):
            raise ValueError("invalid or ambiguous plan serialization") from None


@dataclass(frozen=True)
class PlanMetrics:
    # Fractions preserve repeating ratios exactly; no fabricated probability.
    target_drawdown_ratio: Fraction
    cost_per_drawdown: Fraction
    payout_drawdown_ratio: Fraction | None
    break_even_gross_payout: Fraction
    expected_attempts: Fraction | None
    expected_fees_to_first_pass: Fraction | None


def derive_plan_metrics(plan: PropPlan, *, pass_probability: Decimal | None = None,
                        gross_payout: Decimal | None = None) -> PlanMetrics:
    """One evaluation fee, retry fees after failures, one activation after success.

    Expected values assume independent, identical attempts and supplied p>0.
    No time value, recurring fees, taxes, refunds or payout probability inferred.
    A p of zero has infinite expectation and is rejected, not reported as zero.
    """
    if type(plan) is not PropPlan:
        raise ValueError("typed plan required")
    attempts = expected = None
    fees = plan.fees
    initial = Fraction(fees.evaluation) + Fraction(fees.activation)
    if pass_probability is not None:
        money(pass_probability, positive=True)
        if pass_probability > 1:
            raise ValueError("supplied probability must be <= 1")
        attempts = 1 / Fraction(pass_probability)
        retry = fees.reset if fees.retry_fee is RetryFee.RESET else fees.evaluation
        expected = Fraction(fees.evaluation) + (attempts - 1) * Fraction(retry) + Fraction(fees.activation)
    if gross_payout is not None:
        money(gross_payout)
    return PlanMetrics(Fraction(plan.profit_target_gain) / Fraction(plan.maximum_drawdown),
        initial / Fraction(plan.maximum_drawdown),
        Fraction(gross_payout) / Fraction(plan.maximum_drawdown) if gross_payout is not None else None,
        initial / Fraction(plan.payouts.profit_split), attempts, expected)
