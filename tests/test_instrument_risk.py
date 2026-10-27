"""Reviewed v0.17 mechanics and synthetic costs/accounts only; no provider I/O."""
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from prop_survival_engine.core.instruments import (
    INSTRUMENTS, ContractClass, ContractExposure, ContractFamily,
    InstrumentCatalog, InstrumentSpec, family_equivalent_exposure,
)
from prop_survival_engine.core.models import OrderIntent, Side
from prop_survival_engine.oms.models import OmsState
from prop_survival_engine.prop_rules.models import AccountStatus
from prop_survival_engine.risk.contract_limits import (
    ContractLimits, FamilyContractLimit, InstrumentContractLimit, constrain_contracts,
)
from prop_survival_engine.risk.instruments import (
    ExecutionCostAssumptions, InstrumentRiskPolicy, StopDistance, StopUnit,
    analyze_granularity, instrument_loss, points_to_money, points_to_ticks,
    ticks_to_money, ticks_to_points,
)
from prop_survival_engine.risk.policy import ContractLossEstimate, RiskPolicy, RiskPolicyConfig
from prop_survival_engine.risk.service import RiskAction
from tests.test_risk_policy import account, context


D = Decimal
ZERO_COST = ExecutionCostAssumptions(D(0), D(0), D(0))


def evaluate(symbol="ES", *, cushion="100", stop="1", costs=ZERO_COST,
             limits=None, exposure=None, policy_buffer="0", requested=100):
    wrapper = InstrumentRiskPolicy(RiskPolicy(RiskPolicyConfig(100, D("0.5"), D(1), D(policy_buffer))))
    return wrapper.evaluate(OrderIntent(symbol, Side.BUY, requested), context=context(account(cushion)),
        stop=StopDistance(D(stop), StopUnit.TICKS), costs=costs, limits=limits, existing_exposure=exposure)


@pytest.mark.parametrize("symbol,tick,point", [("ES", "12.50", "50"), ("MES", "1.25", "5"),
    ("NQ", "5.00", "20"), ("MNQ", "0.50", "2")])
def test_exact_reviewed_instrument_mechanics(symbol, tick, point):
    spec = INSTRUMENTS.lookup(symbol)
    assert spec.tick_size == D("0.25") and spec.tick_value == D(tick) and spec.point_value == D(point)
    assert ticks_to_money(spec, 1) == D(tick)
    assert points_to_money(spec, D(1)) == D(point)
    assert Fraction(spec.tick_size) * Fraction(spec.point_value) == Fraction(spec.tick_value)


@pytest.mark.parametrize("mini,micro", [("ES", "MES"), ("NQ", "MNQ")])
def test_exact_ten_to_one_monetary_and_equivalence_relationship(mini, micro):
    a, b = INSTRUMENTS.lookup(mini), INSTRUMENTS.lookup(micro)
    assert Fraction(a.tick_value) == 10 * Fraction(b.tick_value)
    assert Fraction(a.point_value) == 10 * Fraction(b.point_value)
    assert ContractExposure(a, 1).mini_equivalent == ContractExposure(b, 10).mini_equivalent == 1


def test_mixed_gross_equivalence_keeps_families_separate():
    entries = (ContractExposure(INSTRUMENTS.lookup("ES"), 1), ContractExposure(INSTRUMENTS.lookup("MES"), 3),
               ContractExposure(INSTRUMENTS.lookup("NQ"), 2), ContractExposure(INSTRUMENTS.lookup("MNQ"), 5))
    totals = {e.family: e.mini_equivalent for e in family_equivalent_exposure(entries)}
    assert totals == {ContractFamily.SP500: Fraction(13, 10), ContractFamily.NASDAQ100: Fraction(5, 2)}
    assert family_equivalent_exposure(()) == ()


@pytest.mark.parametrize("symbol", ["ES", "MES", "NQ", "MNQ"])
def test_points_ticks_and_money_roundtrip_without_rounding(symbol):
    spec = INSTRUMENTS.lookup(symbol)
    for count in [0, 1, 2, 99, 1000000]:
        points = ticks_to_points(spec, count)
        assert points_to_ticks(spec, points) == count
        assert points_to_money(spec, points) == ticks_to_money(spec, count)


@pytest.mark.parametrize("distance", ["0.2499999999999999999999999999", "0.2500000000000000000000000001", "0.3", "0.01"])
def test_non_aligned_price_distances_fail_closed(distance):
    with pytest.raises(ValueError): points_to_ticks(INSTRUMENTS.lookup("ES"), D(distance))
    with pytest.raises(ValueError): instrument_loss(INSTRUMENTS.lookup("ES"), StopDistance(D(distance), StopUnit.POINTS), ZERO_COST)


@pytest.mark.parametrize("value", [D(0), D(-1), D("NaN"), D("Infinity"), 1.0, True])
def test_zero_negative_invalid_stop_rejects(value):
    with pytest.raises(ValueError): StopDistance(value, StopUnit.TICKS)


def test_fractional_stop_tick_rejected_but_expected_slippage_explicitly_can_be_fractional():
    with pytest.raises(ValueError): StopDistance(D("1.5"), StopUnit.TICKS)
    loss = instrument_loss(INSTRUMENTS.lookup("ES"), StopDistance(D(1), StopUnit.TICKS),
                           ExecutionCostAssumptions(D(0), D("0.5"), D(0)))
    assert loss.slippage_money == D("6.25") and loss.total_loss == D("18.75")


@pytest.mark.parametrize("field", ["tick_size", "tick_value", "point_value"])
@pytest.mark.parametrize("value", [D(0), D(-1), D("NaN"), 0.25])
def test_invalid_instrument_monetary_mechanics_reject(field, value):
    with pytest.raises(ValueError): replace(INSTRUMENTS.lookup("ES"), **{field: value})


def test_inconsistent_tick_product_and_weights_reject():
    with pytest.raises(ValueError): replace(INSTRUMENTS.lookup("ES"), tick_value=D("12.51"))
    with pytest.raises(ValueError): replace(INSTRUMENTS.lookup("MES"), equivalence_weight=Fraction(1, 9))
    with pytest.raises(ValueError): replace(INSTRUMENTS.lookup("MES"), equivalence_weight=0.1)


def test_incompatible_family_monetary_relationships_reject():
    bad = replace(INSTRUMENTS.lookup("MES"), point_value=D(1), tick_value=D("0.25"))
    with pytest.raises(ValueError): InstrumentCatalog((INSTRUMENTS.lookup("ES"), bad))
    with pytest.raises(ValueError): InstrumentCatalog((INSTRUMENTS.lookup("ES"), INSTRUMENTS.lookup("MNQ")))
    with pytest.raises(ValueError): InstrumentCatalog((INSTRUMENTS.lookup("ES"), INSTRUMENTS.lookup("ES")))


def test_specs_and_catalog_are_immutable_and_provider_symbols_not_accepted():
    with pytest.raises(FrozenInstanceError): INSTRUMENTS.lookup("ES").tick_size = D(1)
    for symbol in ("ESZ4", "es", "provider-contract-id", "RTY"):
        with pytest.raises(ValueError): INSTRUMENTS.lookup(symbol)


def test_no_hidden_cost_defaults():
    with pytest.raises(TypeError): ExecutionCostAssumptions()
    with pytest.raises(ValueError): instrument_loss(INSTRUMENTS.lookup("ES"), StopDistance(D(1), StopUnit.TICKS), None)


@pytest.mark.parametrize("field", ["round_trip_fees", "adverse_slippage_ticks", "execution_buffer"])
@pytest.mark.parametrize("value", [D(-1), D("NaN"), 1.0])
def test_invalid_cost_assumptions_reject(field, value):
    with pytest.raises(ValueError): replace(ZERO_COST, **{field: value})


def test_stop_and_cost_breakdown_maps_exactly_to_unchanged_policy():
    policy = RiskPolicy(RiskPolicyConfig(100, D("0.5"), D(1), D(4)))
    intent = OrderIntent("ES", Side.BUY, 100)
    ctx = context(account("1000"))
    result = InstrumentRiskPolicy(policy).evaluate(intent, context=ctx,
        stop=StopDistance(D(1), StopUnit.POINTS), costs=ExecutionCostAssumptions(D("3.25"), D(2), D(7)))
    direct = policy.evaluate(intent, context=ctx, estimate=ContractLossEstimate(D(50), D("3.25"), D(32)))
    assert result.risk_evaluation == direct and result.decision == direct.decision
    assert result.loss.total_loss == D("85.25") and result.effective_per_contract_loss == D("89.25")
    assert result.approved_worst_case_loss == direct.approved_worst_case_loss


@pytest.mark.parametrize("symbol", ["ES", "MES", "NQ", "MNQ"])
def test_wrapper_matches_direct_policy_for_reviewed_instruments(symbol):
    wrapper = InstrumentRiskPolicy(RiskPolicy(RiskPolicyConfig(20)))
    intent = OrderIntent(symbol, Side.BUY, 20)
    result = wrapper.evaluate(intent, context=context(), stop=StopDistance(D(4), StopUnit.TICKS), costs=ZERO_COST)
    direct = wrapper.policy.evaluate(intent, context=context(), estimate=ContractLossEstimate(
        ticks_to_money(INSTRUMENTS.lookup(symbol), 4), D(0), D(0)))
    assert result.risk_evaluation == direct and result.decision == direct.decision


@pytest.mark.parametrize("cost_field", ["round_trip_fees", "adverse_slippage_ticks", "execution_buffer"])
def test_cost_increases_never_increase_approved_size(cost_field):
    previous = 100
    for value in [D(0), D("0.01"), D(1), D(10), D(1000)]:
        result = evaluate("MES", costs=replace(ZERO_COST, **{cost_field: value}))
        assert result.approved_contracts <= previous
        previous = result.approved_contracts


def test_worsening_cushion_and_wider_stops_never_increase_size():
    for symbol in ["ES", "MES", "NQ", "MNQ"]:
        previous = 100
        for cushion in ["1000", "100", "50", "25", "1", "0.01", "0"]:
            result = evaluate(symbol, cushion=cushion)
            assert result.approved_contracts <= previous
            previous = result.approved_contracts
        previous = 100
        for stop in ["1", "2", "3", "10", "1000000000000000000000000000000"]:
            result = evaluate(symbol, stop=stop)
            assert result.approved_contracts <= previous
            previous = result.approved_contracts


@pytest.mark.parametrize("budget,loss,n", [("12.50", "12.50", 1), ("12.49", "12.50", 0),
    ("50", "12.50", 4), ("0", "12.50", 0), ("49.99", "12.50", 3)])
def test_granularity_exact_and_cent_boundary(budget, loss, n):
    result = analyze_granularity(D(budget), D(loss))
    assert result.max_contracts == n
    assert result.used_risk <= D(budget) and result.unused_risk >= 0
    assert 0 <= result.utilization <= 1


def test_micros_can_use_budget_unaffordable_for_one_mini():
    mini = analyze_granularity(D(10), ticks_to_money(INSTRUMENTS.lookup("ES"), 1))
    micro = analyze_granularity(D(10), ticks_to_money(INSTRUMENTS.lookup("MES"), 1))
    assert mini.max_contracts == 0 and micro.max_contracts == 8
    assert mini.utilization == 0 and micro.utilization == 1


def test_budget_fit_is_not_permission_to_touch_account_floor():
    assert analyze_granularity(D("12.5"), D("12.5")).max_contracts == 1
    policy = RiskPolicy(RiskPolicyConfig(10, D(1), D(0)))
    result = InstrumentRiskPolicy(policy).evaluate(OrderIntent("ES", Side.BUY, 1),
        context=context(account("12.5")), stop=StopDistance(D(1), StopUnit.TICKS), costs=ZERO_COST)
    assert result.approved_contracts == 0 and result.decision.action is RiskAction.REJECT


def test_property_style_granularity_and_wrapper_budget_conservation():
    for cents in range(0, 251, 7):
        budget = D(cents) / D(100)
        for loss in [D("0.01"), D("0.50"), D("1.25"), D("12.50")]:
            result = analyze_granularity(budget, loss)
            assert type(result.max_contracts) is int and result.max_contracts >= 0
            assert result.used_risk + result.unused_risk == budget
            assert 0 <= result.utilization <= 1
        result = evaluate("MNQ", cushion=str(budget))
        if result.risk_evaluation.risk_budget is not None:
            assert result.approved_worst_case_loss <= result.risk_evaluation.risk_budget
            assert result.unused_risk_budget >= 0 and 0 <= result.effective_risk_utilization <= 1


def test_exact_money_under_low_decimal_precision():
    with localcontext() as ctx:
        ctx.prec = 2
        loss = instrument_loss(INSTRUMENTS.lookup("ES"), StopDistance(D("1000000.25"), StopUnit.POINTS),
                               ExecutionCostAssumptions(D("0.123456"), D("0.123456"), D("0.000001")))
        result = analyze_granularity(D("12345.67"), D("1.234567"))
    assert loss.stop_money == D("50000012.50")
    assert loss.slippage_money == D("1.5432")
    assert loss.total_loss == D("50000014.166657")
    assert result.max_contracts == 10000 and result.utilization == 1


def test_specific_instrument_limit_and_existing_exposure_only_reduce():
    limits = ContractLimits((InstrumentContractLimit("MES", 12),), ())
    result = evaluate("MES", limits=limits, exposure=(ContractExposure(INSTRUMENTS.lookup("MES"), 4),))
    assert result.risk_evaluation.approved_contracts == 40 and result.approved_contracts == 8
    assert result.decision.action is RiskAction.REDUCE
    assert result.approved_worst_case_loss == 10 and result.effective_risk_utilization == Fraction(1, 5)
    assert result.unused_risk_budget == 40


def test_family_equivalence_and_independent_class_limits():
    family = FamilyContractLimit(ContractFamily.SP500, Fraction(1), 1, 15, True)
    limits = ContractLimits((), (family,))
    existing = (ContractExposure(INSTRUMENTS.lookup("MES"), 3),)
    assert evaluate("MES", limits=limits, exposure=existing).approved_contracts == 7
    assert evaluate("ES", limits=limits, exposure=existing).approved_contracts == 0
    classes = ContractLimits((), (FamilyContractLimit(ContractFamily.SP500, None, 2, 7, True),))
    assert evaluate("ES", limits=classes, exposure=()).approved_contracts == 2
    assert evaluate("MES", limits=classes, exposure=()).approved_contracts == 7


def test_mixed_permission_requires_separate_explicit_policy():
    existing = (ContractExposure(INSTRUMENTS.lookup("MES"), 1),)
    denied = ContractLimits((), (FamilyContractLimit(ContractFamily.SP500, Fraction(10), None, None, False),))
    result = evaluate("ES", limits=denied, exposure=existing)
    assert result.approved_contracts == 0 and "MIXED_NOT_PERMITTED" in result.contract_limits.reason_codes
    unspecified = ContractLimits((InstrumentContractLimit("ES", 10),), ())
    assert evaluate("ES", limits=unspecified, exposure=existing).approved_contracts == 0


def test_limits_do_not_net_or_consume_unrelated_family_capacity():
    limits = ContractLimits((), (FamilyContractLimit(ContractFamily.SP500, Fraction(1), None, None, False),))
    unrelated = (ContractExposure(INSTRUMENTS.lookup("NQ"), 100),)
    assert evaluate("MES", limits=limits, exposure=unrelated).approved_contracts == 10
    with pytest.raises(ValueError): ContractExposure(INSTRUMENTS.lookup("MES"), -1)


def test_explicit_zero_contract_limit_rejects_and_missing_inventory_is_not_assumed_flat():
    limits = ContractLimits((InstrumentContractLimit("ES", 0),), ())
    assert evaluate("ES", limits=limits, exposure=()).decision.action is RiskAction.REJECT
    with pytest.raises(ValueError): evaluate("ES", limits=limits)
    with pytest.raises(ValueError): evaluate("ES", exposure=())


def test_duplicate_unknown_or_inexact_limit_specs_reject():
    item = InstrumentContractLimit("ES", 1)
    with pytest.raises(ValueError): ContractLimits((item, item), ())
    with pytest.raises(ValueError): InstrumentContractLimit("ESZ4", 2)
    with pytest.raises(ValueError): FamilyContractLimit(ContractFamily.SP500, D(1), None, None, True)
    with pytest.raises(ValueError): FamilyContractLimit(ContractFamily.SP500, None, None, None, True)


@pytest.mark.parametrize("status", [AccountStatus.BREACHED, AccountStatus.PASSED])
def test_terminal_account_remains_rejected_by_unchanged_policy(status):
    wrapper = InstrumentRiskPolicy(RiskPolicy(RiskPolicyConfig(10)))
    result = wrapper.evaluate(OrderIntent("MES", Side.BUY, 10), context=context(account(status=status)),
                              stop=StopDistance(D(1), StopUnit.TICKS), costs=ZERO_COST)
    assert result.approved_contracts == 0 and result.decision.action is RiskAction.REJECT
    assert result.unused_risk_budget is None


def test_recovery_state_unchanged_and_account_not_mutated():
    state = account(); ctx = replace(context(state), oms_state=OmsState.RECOVERING)
    wrapper = InstrumentRiskPolicy(RiskPolicy(RiskPolicyConfig(10)))
    result = wrapper.evaluate(OrderIntent("MES", Side.BUY, 10), context=ctx,
                              stop=StopDistance(D(1), StopUnit.TICKS), costs=ZERO_COST)
    assert result.approved_contracts == 0 and ctx.account is state and ctx.account == account()


def test_repeated_wrapper_evaluation_is_deterministic():
    assert evaluate("MNQ") == evaluate("MNQ")
