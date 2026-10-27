from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from prop_survival_engine.core.models import Order, OrderIntent, Side
from prop_survival_engine.execution.interfaces import PlatformOrderStatus, PlatformSnapshot, PlatformOrder
from prop_survival_engine.oms.models import OmsState, OrderState
from prop_survival_engine.oms.service import OmsService, RecoveryRequired
from prop_survival_engine.persistence.memory import InMemoryOmsJournal
from prop_survival_engine.persistence.sqlite import SQLiteOmsJournal
from prop_survival_engine.prop_rules.engine import StatefulPropRuleEngine
from prop_survival_engine.prop_rules.models import (
    AccountStatus, EndOfDaySettled, PropRuleConfig, RealizedPnlUpdated,
)
from prop_survival_engine.risk.service import BasicRiskEvaluator, RiskAction, RiskDecision
from prop_survival_engine.simulation.execution import (
    SimulatedExecutionAdapter, SimulatedMarketDataAdapter, adapter_contracts_hold,
)


class CountingExecution(SimulatedExecutionAdapter):
    def __init__(self):
        super().__init__()
        self.submissions = 0
        self.fail_snapshot = False

    def submit(self, order):
        self.submissions += 1
        return super().submit(order)

    def snapshot(self):
        if self.fail_snapshot:
            raise RuntimeError("platform unavailable")
        return super().snapshot()


def approved_oms(journal=None, execution=None):
    journal = journal or InMemoryOmsJournal()
    execution = execution or CountingExecution()
    oms = OmsService(journal, execution)
    oms.recover()
    return oms, journal, execution


def intent(qty=2, *, intent_id=None):
    return OrderIntent("MES", Side.BUY, qty, intent_id=intent_id or uuid4())


def test_intent_is_distinct_from_executable_order():
    proposal = intent()
    assert isinstance(proposal, OrderIntent)
    assert not isinstance(proposal, Order)
    oms, _, _ = approved_oms()
    managed = oms.process(proposal, RiskDecision(RiskAction.ALLOW, proposal.quantity))
    assert isinstance(managed.order, Order)
    assert managed.order.intent_id == proposal.intent_id
    assert managed.order.order_id != proposal.intent_id
    assert managed.order.client_order_id


def test_risk_allow_reduce_reject_decisions():
    evaluator = BasicRiskEvaluator(3)
    assert evaluator.evaluate(intent(2)).action is RiskAction.ALLOW
    reduced = evaluator.evaluate(intent(5))
    assert (reduced.action, reduced.quantity) == (RiskAction.REDUCE, 3)
    assert RiskDecision(RiskAction.REJECT, reason="closed").action is RiskAction.REJECT


def test_prop_rule_engine_transitions_and_tracks_state():
    engine = StatefulPropRuleEngine(PropRuleConfig(
        starting_balance=Decimal("1000"), trailing_drawdown=Decimal("100"),
        daily_loss_limit=Decimal("80"), profit_target_gain=Decimal("200"),
    ))
    engine.apply(RealizedPnlUpdated(Decimal("100")))
    higher = engine.apply(EndOfDaySettled(engine.state.session_id))
    assert higher == ()
    higher = engine.state
    assert higher.eod_high_watermark == Decimal("1100")
    assert higher.trailing_floor == Decimal("1000")
    engine.apply(RealizedPnlUpdated(Decimal("200")))
    assert engine.state.status is AccountStatus.PASSED
    breached_engine = StatefulPropRuleEngine(PropRuleConfig(
        Decimal("1000"), Decimal("100"), Decimal("80"), Decimal("500")
    ))
    breached_engine.apply(RealizedPnlUpdated(Decimal("-100")))
    assert breached_engine.state.status is AccountStatus.BREACHED


def test_duplicate_intent_returns_existing_order_without_resubmit():
    oms, _, execution = approved_oms()
    proposal = intent()
    decision = RiskDecision(RiskAction.ALLOW, proposal.quantity)
    first = oms.process(proposal, decision)
    again = oms.process(proposal, decision)
    assert first.order.order_id == again.order.order_id
    assert execution.submissions == 1


def test_uncertain_submission_is_retained():
    class Uncertain(CountingExecution):
        def submit(self, order):
            self.submissions += 1
            return __import__("prop_survival_engine.execution.interfaces", fromlist=["SubmissionResult"]).SubmissionResult(
                __import__("prop_survival_engine.execution.interfaces", fromlist=["SubmissionStatus"]).SubmissionStatus.UNCERTAIN
            )
    oms, _, _ = approved_oms(execution=Uncertain())
    managed = oms.process(intent(), RiskDecision(RiskAction.ALLOW, 2))
    assert managed.state is OrderState.UNCERTAIN


def test_sqlite_journal_survives_close_reopen_and_replay(tmp_path: Path):
    path = tmp_path / "oms.sqlite3"
    first = SQLiteOmsJournal(path)
    first.append("probe", {"intent_id": "stable", "value": 7})
    first.close()
    reopened = SQLiteOmsJournal(path)
    rows = reopened.replay()
    assert len(rows) == 1 and rows[0].payload["value"] == 7
    reopened.close()


def test_oms_replays_order_after_restart_and_reconciles_authoritative_state():
    journal = InMemoryOmsJournal()
    platform = CountingExecution()
    first, _, _ = approved_oms(journal, platform)
    proposal = intent()
    managed = first.process(proposal, RiskDecision(RiskAction.ALLOW, proposal.quantity))
    platform.set_status(managed.order.client_order_id, PlatformOrderStatus.FILLED, 2)
    restarted = OmsService(journal, platform)
    restarted.recover()
    recovered = restarted.by_intent[proposal.intent_id]
    assert recovered.order.order_id == managed.order.order_id
    assert recovered.state is OrderState.FILLED
    assert recovered.filled_quantity == 2


def test_sqlite_oms_journal_recovers_order_after_process_restart(tmp_path: Path):
    path = tmp_path / "restart.sqlite3"
    journal = SQLiteOmsJournal(path)
    platform = CountingExecution()
    oms, _, _ = approved_oms(journal, platform)
    proposal = intent()
    original = oms.process(proposal, RiskDecision(RiskAction.ALLOW, proposal.quantity))
    journal.close()

    reopened = SQLiteOmsJournal(path)
    restarted = OmsService(reopened, platform)
    restarted.recover()
    recovered = restarted.process(proposal, RiskDecision(RiskAction.ALLOW, proposal.quantity))
    assert recovered.order.order_id == original.order.order_id
    assert platform.submissions == 1
    reopened.close()


def test_oms_blocks_new_entries_until_reconciliation_and_then_becomes_ready():
    journal = InMemoryOmsJournal()
    platform = CountingExecution()
    platform.fail_snapshot = True
    oms = OmsService(journal, platform)
    with pytest.raises(RuntimeError):
        oms.recover()
    assert oms.state is OmsState.RECOVERING
    with pytest.raises(RecoveryRequired):
        oms.process(intent(), RiskDecision(RiskAction.ALLOW, 1))
    platform.fail_snapshot = False
    oms.recover()
    assert oms.state is OmsState.READY
    assert oms.process(intent(), RiskDecision(RiskAction.ALLOW, 1)) is not None


def test_reconciliation_prefers_platform_snapshot_over_local_submission_state():
    journal = InMemoryOmsJournal()
    platform = CountingExecution()
    first, _, _ = approved_oms(journal, platform)
    proposal = intent()
    managed = first.process(proposal, RiskDecision(RiskAction.ALLOW, 2))
    # Platform reports an authoritative rejection even though local journal says accepted.
    platform.set_status(managed.order.client_order_id, PlatformOrderStatus.REJECTED)
    second = OmsService(journal, platform)
    second.recover()
    assert second.by_intent[proposal.intent_id].state is OrderState.REJECTED


def test_simulated_adapters_satisfy_real_adapter_protocols():
    assert adapter_contracts_hold()
    md = SimulatedMarketDataAdapter()
    received = []
    md.subscribe("MES", received.append)
    from prop_survival_engine.core.models import MarketEvent
    md.publish(MarketEvent("MES", "quote", datetime.now(timezone.utc), Decimal("5000")))
    assert len(received) == 1
