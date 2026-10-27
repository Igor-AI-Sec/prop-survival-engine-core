"""Deterministic floor boundaries, exact money and sizing-order properties."""

from copy import deepcopy
from dataclasses import replace, FrozenInstanceError
from decimal import Decimal, localcontext, ROUND_UP, Inexact
from fractions import Fraction

import pytest

from prop_survival_engine.core.models import OrderIntent, Side
from prop_survival_engine.oms.models import OmsState
from prop_survival_engine.oms.service import OmsService
from prop_survival_engine.persistence.sqlite import SQLiteOmsJournal
from prop_survival_engine.prop_rules.engine import StatefulPropRuleEngine
from prop_survival_engine.prop_rules.models import (
    AccountState, AccountStatus, MarkToMarketUpdated, PropRuleConfig, SessionStarted,
)
from prop_survival_engine.risk import (
    ActiveLossFloors, ContractLossEstimate, PolicyRiskEvaluator, RiskAction,
    RiskContext, RiskDecision, RiskPolicy, RiskPolicyConfig, RiskReason, SessionState,
    derive_ruin_distances,
)
from prop_survival_engine.simulation.execution import SimulatedExecutionAdapter


D = Decimal


def account(cushion="100", *, unrealized="0", daily_floor="900", trailing_floor="850", status=AccountStatus.ALIVE):
    equity = D(daily_floor) + D(cushion)
    return AccountState(D("1000"), equity, equity - D("1000") - D(unrealized), D(unrealized),
                        D("1000"), D("1000"), D(trailing_floor), D(daily_floor),
                        D("10000"), "session-1", status)


def context(state=None, **kwargs):
    return RiskContext(state or account(), OmsState.READY, SessionState.OPEN, **kwargs)


def policy(**kwargs):
    return RiskPolicy(RiskPolicyConfig(max_contracts=kwargs.pop("max_contracts", 10), **kwargs))


def evaluate(cushion="100", requested=10, stop="10", **config):
    return policy(**config).evaluate(OrderIntent("ES", Side.BUY, requested),
                                    context=context(account(cushion)), estimate=ContractLossEstimate(D(stop)))


def test_distances_use_both_floors_signed_target_and_unrealized_equity():
    state = account("100", unrealized="-25")
    result = derive_ruin_distances(context(state))
    assert result.equity == D("1000")
    assert result.distance_to_daily_floor == D("100")
    assert result.distance_to_trailing_floor == D("150")
    assert result.effective_ruin_distance == D("100")
    assert result.distance_to_target == D("9000")
    assert result.unrealized_pnl == D("-25")
    assert result.account_status is AccountStatus.ALIVE
    assert (result.session_id, result.session_state) == ("session-1", SessionState.OPEN)


@pytest.mark.parametrize("active,expected", [
    (ActiveLossFloors(), "100"), (ActiveLossFloors(False, True), "150"),
    (ActiveLossFloors(True, False), "100"), (ActiveLossFloors(False, False), None),
])
def test_effective_distance_uses_only_explicit_active_floors(active, expected):
    result = derive_ruin_distances(context(active_floors=active))
    assert result.effective_ruin_distance == (D(expected) if expected else None)
    decision = policy().evaluate(OrderIntent("ES", Side.BUY, 1),
                                 context=context(active_floors=active), estimate=ContractLossEstimate(D("10")))
    if expected is None:
        assert decision.action is RiskAction.REJECT
        assert RiskReason.NO_ACTIVE_LOSS_FLOOR in decision.reason_codes


@pytest.mark.parametrize("floor", ["daily", "trailing"])
@pytest.mark.parametrize("cushion", ["-0.01", "0", "0.01"])
def test_exact_and_cent_adjacent_floors(floor, cushion):
    state = account(cushion)
    if floor == "trailing":
        state = replace(state, daily_floor=D("850"), trailing_floor=D("900"))
    result = policy().evaluate(OrderIntent("ES", Side.BUY, 1), context=context(state),
                               estimate=ContractLossEstimate(D("0.01")))
    assert result.action is RiskAction.REJECT
    assert result.approved_contracts == 0
    reason = RiskReason.NON_POSITIVE_RUIN_DISTANCE if D(cushion) <= 0 else RiskReason.NO_SAFE_SIZE
    assert reason in result.reason_codes


def test_one_cent_cushion_can_allow_only_strictly_smaller_loss_when_configured():
    result = evaluate("0.01", requested=1, stop="0.001", minimum_ruin_buffer=D("0"))
    assert result.action is RiskAction.ALLOW
    assert result.approved_worst_case_loss < result.metrics.effective_ruin_distance


@pytest.mark.parametrize("status,reason", [
    (AccountStatus.BREACHED, RiskReason.ACCOUNT_BREACHED),
    (AccountStatus.PASSED, RiskReason.ACCOUNT_PASSED),
])
def test_terminal_accounts_reject_new_entries_even_with_large_cushion(status, reason):
    result = policy().evaluate(OrderIntent("ES", Side.BUY, 1), context=context(account(status=status)),
                               estimate=ContractLossEstimate(D("0.01")))
    assert result.action is RiskAction.REJECT
    assert reason in result.reason_codes
    assert result.approved_contracts == 0


@pytest.mark.parametrize("oms_state", [OmsState.STARTING, OmsState.RECOVERING, "READY", None])
def test_recovery_and_untrusted_ready_values_reject(oms_state):
    result = policy().evaluate(OrderIntent("ES", Side.BUY, 1),
                               context=replace(context(), oms_state=oms_state), estimate=ContractLossEstimate(D("10")))
    assert result.action is RiskAction.REJECT
    assert RiskReason.OMS_NOT_READY in result.reason_codes


@pytest.mark.parametrize("session", [SessionState.CLOSED, SessionState.UNKNOWN])
def test_session_must_explicitly_permit_entries(session):
    result = policy().evaluate(OrderIntent("ES", Side.BUY, 1),
                               context=replace(context(), session_state=session), estimate=ContractLossEstimate(D("10")))
    assert RiskReason.SESSION_NOT_OPEN in result.reason_codes
    assert result.action is RiskAction.REJECT


@pytest.mark.parametrize("requested,expected,size", [
    (1, RiskAction.ALLOW, 1), (2, RiskAction.ALLOW, 2), (3, RiskAction.REDUCE, 2), (25, RiskAction.REDUCE, 2),
])
def test_allow_reduce_integer_sizing_and_report(requested, expected, size):
    result = evaluate(requested=requested)
    assert result.action is expected
    assert result.requested_contracts == requested
    assert result.approved_contracts == size
    assert type(result.approved_contracts) is int
    assert result.decision.quantity == size
    assert result.risk_budget == D("25")
    assert result.approved_worst_case_loss == D(str(size * 10))
    assert result.approved_worst_case_loss <= result.risk_budget


def test_max_contracts_is_independent_hard_cap():
    result = evaluate("1000", requested=50, stop="1", max_contracts=3)
    assert result.action is RiskAction.REDUCE
    assert result.approved_contracts == 3
    assert RiskReason.MAX_CONTRACTS_LIMIT in result.reason_codes


def test_no_safe_integer_and_minimum_buffer_exhaustion_reject():
    assert evaluate("100", stop="25.01").action is RiskAction.REJECT
    assert evaluate("5", stop="1", minimum_ruin_buffer=D("5")).action is RiskAction.REJECT
    assert evaluate("5", stop="1", minimum_ruin_buffer=D("6")).risk_budget == D("0")


@pytest.mark.parametrize("estimate", [
    None, ContractLossEstimate(), ContractLossEstimate(D("0")), ContractLossEstimate(D("-1")),
    ContractLossEstimate(D("NaN")), ContractLossEstimate(D("sNaN")), ContractLossEstimate(D("Infinity")),
    ContractLossEstimate(10.0), ContractLossEstimate(True), ContractLossEstimate("10"),
    ContractLossEstimate(D("10"), D("-1")), ContractLossEstimate(D("10"), None),
    ContractLossEstimate(D("10"), D("Infinity")), ContractLossEstimate(D("10"), slippage_loss=D("-1")),
    ContractLossEstimate(D("10"), slippage_loss=1.0),
])
def test_invalid_or_missing_loss_estimate_fails_closed(estimate):
    result = policy().evaluate(OrderIntent("ES", Side.BUY, 1), context=context(), estimate=estimate)
    assert result.action is RiskAction.REJECT
    assert result.approved_contracts == 0
    assert RiskReason.INVALID_LOSS_ESTIMATE in result.reason_codes


def test_omitting_estimate_returns_reject_without_exception():
    result = policy().evaluate(OrderIntent("ES", Side.BUY, 1), context=context())
    assert RiskReason.INVALID_LOSS_ESTIMATE in result.reason_codes


def test_fees_slippage_and_execution_buffer_are_per_contract_and_reduce_size():
    proposal = OrderIntent("ES", Side.BUY, 10)
    estimate = ContractLossEstimate(D("10"), D("1.5"), D("1"))
    plain = policy().evaluate(proposal, context=context(), estimate=estimate)
    buffered = policy(execution_loss_buffer=D("0.01")).evaluate(proposal, context=context(), estimate=estimate)
    assert plain.per_contract_worst_case_loss == D("12.5")
    assert plain.approved_contracts == 2
    assert buffered.per_contract_worst_case_loss == D("12.51")
    assert buffered.approved_contracts == 1


def test_exact_budget_touch_allowed_but_floor_touch_forbidden():
    budget_touch = evaluate("100", requested=2, stop="12.5")
    assert budget_touch.action is RiskAction.ALLOW
    assert budget_touch.approved_worst_case_loss == budget_touch.risk_budget
    floor_touch = evaluate("100", requested=2, stop="50", minimum_ruin_buffer=D("0"),
                           max_fraction_of_ruin_distance_per_trade=D("1"))
    assert floor_touch.action is RiskAction.REDUCE
    assert floor_touch.approved_contracts == 1
    assert RiskReason.FLOOR_TOUCH_LIMIT in floor_touch.reason_codes
    no_contract = evaluate("100", requested=1, stop="100", minimum_ruin_buffer=D("0"),
                           max_fraction_of_ruin_distance_per_trade=D("1"))
    assert no_contract.action is RiskAction.REJECT


@pytest.mark.parametrize("invalid", [
    {"max_contracts": 0}, {"max_contracts": -1}, {"max_contracts": True}, {"max_contracts": 1.5},
    {"max_fraction_of_ruin_distance_per_trade": D("0")},
    {"max_fraction_of_ruin_distance_per_trade": D("1.01")},
    {"max_fraction_of_ruin_distance_per_trade": 0.25},
    {"max_fraction_of_ruin_distance_per_trade": D("NaN")},
    {"minimum_ruin_buffer": D("-1")}, {"minimum_ruin_buffer": D("Infinity")},
    {"execution_loss_buffer": D("-1")}, {"execution_loss_buffer": 1.0},
])
def test_config_validation(invalid):
    with pytest.raises(ValueError):
        RiskPolicyConfig(**({"max_contracts": 10} | invalid))


@pytest.mark.parametrize("requested", [True, 1.5, D("2")])
def test_noninteger_intent_quantity_is_rejected_at_policy_boundary(requested):
    result = policy().evaluate(OrderIntent("ES", Side.BUY, requested), context=context(),
                               estimate=ContractLossEstimate(D("1")))
    assert result.action is RiskAction.REJECT
    assert result.approved_contracts == 0
    assert result.requested_contracts is None
    assert RiskReason.INVALID_REQUESTED_CONTRACTS in result.reason_codes


@pytest.mark.parametrize("fraction,buffer,loss,max_contracts", [
    ("0.25", "1", "3.17", 10), ("1", "0", "0.01", 30),
    ("0.1", "10", "20", 7), ("0.333", "0.01", "12.51", 5),
])
def test_worsening_cushion_never_increases_size_and_every_result_satisfies_bounds(fraction, buffer, loss, max_contracts):
    cushions = [D(str(n)) / 100 for n in [0, 1, 2, 100, 399, 400, 1000, 10000, 25000, 100000]]
    previous = 0
    for cushion in cushions:
        result = evaluate(str(cushion), requested=50, stop=loss, max_contracts=max_contracts,
                          max_fraction_of_ruin_distance_per_trade=D(fraction), minimum_ruin_buffer=D(buffer))
        q = result.approved_contracts
        assert type(q) is int and previous <= q <= min(50, max_contracts)
        if q:
            adverse = Fraction(D(loss)) * q
            assert adverse <= Fraction(D(fraction)) * Fraction(cushion)
            assert adverse <= Fraction(cushion) - Fraction(D(buffer))
            assert adverse < Fraction(cushion)
        previous = q
    # Reverse ordering states the no-gamble-for-resurrection invariant directly.
    sizes = [evaluate(str(c), requested=50, stop=loss, max_contracts=max_contracts,
                      max_fraction_of_ruin_distance_per_trade=D(fraction), minimum_ruin_buffer=D(buffer)).approved_contracts
             for c in reversed(cushions)]
    assert sizes == sorted(sizes, reverse=True)


def test_each_floor_can_become_limiting_without_an_increase_in_size():
    proposal, estimate = OrderIntent("ES", Side.BUY, 10), ContractLossEstimate(D("5"))
    previous = 10
    for daily, trailing in [("800", "800"), ("850", "800"), ("850", "900"), ("950", "900"), ("950", "980")]:
        result = policy().evaluate(proposal, context=context(replace(account(), daily_floor=D(daily), trailing_floor=D(trailing))),
                                   estimate=estimate)
        assert result.approved_contracts <= previous
        previous = result.approved_contracts


def test_improving_cushion_can_increase_size_only_up_to_request_and_cap():
    assert [evaluate(c, requested=50, stop="10", max_contracts=6).approved_contracts
            for c in ["100", "200", "1000", "2000"]] == [2, 5, 6, 6]
    assert evaluate("2000", requested=3, stop="10", max_contracts=6).approved_contracts == 3


def test_increasing_execution_buffers_never_increases_size():
    sizes = [evaluate("100", stop="10", execution_loss_buffer=D(buffer)).approved_contracts
             for buffer in ["0", "1", "2.5", "2.51", "10", "100"]]
    assert sizes == sorted(sizes, reverse=True)


def test_account_context_and_loss_floors_require_trusted_types():
    with pytest.raises(ValueError):
        ActiveLossFloors(daily=1)
    with pytest.raises(ValueError):
        derive_ruin_distances(replace(context(), session_state="OPEN"))
    result = policy().evaluate(OrderIntent("ES", Side.BUY, 1), context=None,
                               estimate=ContractLossEstimate(D("1")))
    assert result.action is RiskAction.REJECT
    assert result.reason_codes == (RiskReason.INVALID_ACCOUNT_STATE,)


def test_repeated_evaluation_is_equal_read_only_and_context_independent():
    state = account("100", unrealized="-12.34")
    before = deepcopy(state)
    proposal = OrderIntent("ES", Side.BUY, 10)
    estimate = ContractLossEstimate(D("10.01"), D("1.11"), D("0.03"))
    selected = policy(max_fraction_of_ruin_distance_per_trade=D("0.33333"))
    expected = selected.evaluate(proposal, context=context(state), estimate=estimate)
    for _ in range(5):
        assert selected.evaluate(proposal, context=context(state), estimate=estimate) == expected
    with localcontext() as altered:
        altered.prec = 2
        altered.rounding = ROUND_UP
        altered.traps[Inexact] = True
        assert selected.evaluate(proposal, context=context(state), estimate=estimate) == expected
    assert state == before
    with pytest.raises(FrozenInstanceError):
        expected.metrics.equity = D("0")


def test_open_unrealized_loss_reduces_equity_and_size_without_rule_mutation():
    engine = StatefulPropRuleEngine(PropRuleConfig(D("1000"), D("100"), D("80"), D("500")))
    estimate, proposal = ContractLossEstimate(D("5")), OrderIntent("ES", Side.BUY, 10)
    initial = policy().evaluate(proposal, context=context(engine.state), estimate=estimate)
    engine.apply(SessionStarted("test-session"))
    engine.apply(MarkToMarketUpdated(D("-40")))
    before = engine.snapshot()
    reduced = policy().evaluate(proposal, context=context(engine.state), estimate=estimate)
    assert initial.approved_contracts == 4
    assert reduced.approved_contracts == 2
    assert reduced.metrics.unrealized_pnl == D("-40")
    assert reduced.metrics.effective_ruin_distance == D("40")
    assert engine.snapshot() == before


def test_target_threshold_and_nonentry_path_are_conservatively_rejected():
    state = replace(account(), target_equity=D("1000"))
    result = policy().evaluate(OrderIntent("ES", Side.BUY, 1), context=context(state),
                               estimate=ContractLossEstimate(D("1")))
    assert RiskReason.TARGET_REACHED in result.reason_codes
    nonentry = policy().evaluate(OrderIntent("ES", Side.SELL, 1, is_entry=False), context=context(),
                                 estimate=ContractLossEstimate(D("1")))
    assert nonentry.action is RiskAction.REJECT
    assert RiskReason.NON_ENTRY_UNSUPPORTED in nonentry.reason_codes


@pytest.mark.parametrize("corruption", ["non_decimal_floor", "infinite_floor", "invalid_status", "empty_session", "inconsistent_equity"])
def test_invalid_account_features_fail_closed(corruption):
    # Model corrupt input at the trust boundary without weakening AccountState.
    state = deepcopy(account())
    name, value = {
        "non_decimal_floor": ("daily_floor", 900.0), "infinite_floor": ("daily_floor", D("Infinity")),
        "invalid_status": ("status", "ALIVE"), "empty_session": ("session_id", ""),
        "inconsistent_equity": ("equity", D("1001")),
    }[corruption]
    object.__setattr__(state, name, value)
    result = policy().evaluate(OrderIntent("ES", Side.BUY, 1), context=context(state),
                               estimate=ContractLossEstimate(D("1")))
    assert result.action is RiskAction.REJECT
    assert RiskReason.INVALID_ACCOUNT_STATE in result.reason_codes
    assert result.metrics is None


def test_existing_risk_evaluator_contract_and_oms_replay_keep_rich_report_separate(tmp_path):
    platform = SimulatedExecutionAdapter()
    journal = SQLiteOmsJournal(tmp_path / "risk-oms.db")
    try:
        oms = OmsService(journal, platform)
        proposal = OrderIntent("ES", Side.BUY, 10)
        evaluator = PolicyRiskEvaluator(policy(), lambda: context(), lambda intent: ContractLossEstimate(D("10")))
        detailed = evaluator.evaluate_details(proposal)
        decision = evaluator.evaluate(proposal)
        assert isinstance(decision, RiskDecision)
        assert decision == detailed.decision
        oms.recover()
        created = oms.process(proposal, decision)
        assert created.order.quantity == detailed.approved_contracts == 2
        restarted = OmsService(journal, platform)
        restarted.recover()
        again = restarted.process(proposal, decision)
        assert again.order == created.order
        assert len(restarted.orders) == 1
    finally:
        journal.close()


def test_rejected_intent_replay_preserves_legacy_decision_equality(tmp_path):
    platform = SimulatedExecutionAdapter()
    journal = SQLiteOmsJournal(tmp_path / "reject-replay.db")
    try:
        proposal = OrderIntent("ES", Side.BUY, 1)
        report = policy().evaluate(proposal, context=context())  # Missing estimate.
        assert report.action is RiskAction.REJECT
        assert report.approved_contracts == 0
        assert report.decision.quantity is None
        first = OmsService(journal, platform)
        first.recover()
        assert first.process(proposal, report.decision) is None
        restarted = OmsService(journal, platform)
        restarted.recover()
        assert restarted.process(proposal, report.decision) is None
        assert restarted.decisions[proposal.intent_id] == report.decision
        assert restarted.orders == {}
    finally:
        journal.close()
