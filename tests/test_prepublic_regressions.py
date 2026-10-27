"""Synthetic offline regressions; stdlib unittest, no SDKs or external services."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Context, Decimal, Inexact, Rounded, localcontext
from fractions import Fraction
import json
import os
from pathlib import Path
import tempfile
import unittest

from prop_survival_engine.core import events
from prop_survival_engine.core.models import OrderIntent, Side
from prop_survival_engine.execution.events import (
    ExecutionEvent, ExecutionEventKind, execution_payload, execution_from_payload,
)
from prop_survival_engine.execution.interfaces import (
    PlatformExecution, PlatformOrder, PlatformOrderStatus, PlatformSnapshot,
    SubmissionResult, SubmissionStatus,
)
from prop_survival_engine.oms.models import OmsState, OrderState
from prop_survival_engine.oms.replay_validation import validate_replay
from prop_survival_engine.oms.service import OmsService, RecoveryRequired
from prop_survival_engine.persistence.memory import InMemoryOmsJournal
from prop_survival_engine.persistence.sqlite import SQLiteOmsJournal
from prop_survival_engine.prop_rules.engine import StatefulPropRuleEngine
from prop_survival_engine.prop_rules.models import PropRuleConfig, SessionStarted, PnlSettled
from prop_survival_engine.prop_rules.plan_engine import PlanRuleEngine
from prop_survival_engine.prop_rules.plans import PropPlan, ConsistencyMode
from prop_survival_engine.risk.features import RiskContext, SessionState
from prop_survival_engine.risk.policy import RiskPolicyConfig, ContractLossEstimate
from prop_survival_engine.risk.reservations import (
    SQLiteRiskLedger, ReservationRecoveryRequired, _context, _encode_context,
)
from prop_survival_engine.risk.service import BasicRiskEvaluator, RiskAction, RiskDecision


def intent(quantity=3):
    return OrderIntent("SIM", Side.BUY, quantity)


class FaultJournal(InMemoryOmsJournal):
    def __init__(self):
        super().__init__()
        self.failure = None
        self.replays = 0

    def arm(self, event_type, state=None, after_commit=False):
        self.failure = (event_type, state, after_commit)

    def append(self, event_type, payload):
        fault = self.failure
        if fault and event_type == fault[0] and (fault[1] is None or payload.get("state") == fault[1]):
            self.failure = None
            if fault[2]:
                super().append(event_type, payload)
            raise OSError("synthetic journal failure")
        return super().append(event_type, payload)

    def replay(self):
        self.replays += 1
        return super().replay()


class SyntheticExecution:
    def __init__(self, status=SubmissionStatus.ACCEPTED, raises=False):
        self.status = status
        self.raises = raises
        self.orders = {}
        self.submissions = self.snapshots = self.cancellations = 0
        self.executions = ()

    def submit(self, order):
        self.submissions += 1
        state = {
            SubmissionStatus.ACCEPTED: PlatformOrderStatus.ACCEPTED,
            SubmissionStatus.REJECTED: PlatformOrderStatus.REJECTED,
            SubmissionStatus.UNCERTAIN: PlatformOrderStatus.UNKNOWN,
        }[self.status]
        self.orders[order.client_order_id] = PlatformOrder(
            order.client_order_id, state, provider_order_id=f"synthetic-{self.submissions}",
            our_order_id=str(order.order_id),
        )
        if self.raises:
            raise RuntimeError("synthetic response unavailable")
        return SubmissionResult(self.status, provider_order_id=f"synthetic-{self.submissions}")

    def snapshot(self):
        self.snapshots += 1
        return PlatformSnapshot(tuple(self.orders.values()), {}, self.executions)

    def cancel(self, client_order_id):
        self.cancellations += 1
        self.orders[client_order_id] = replace(self.orders[client_order_id], status=PlatformOrderStatus.CANCELLED)


def ready(status=SubmissionStatus.ACCEPTED, raises=False):
    journal, execution = FaultJournal(), SyntheticExecution(status, raises)
    oms = OmsService(journal, execution)
    oms.recover()
    return oms, journal, execution


def fill(price=Decimal("123.456")):
    return PlatformExecution("synthetic-fill", "synthetic-order", "SIM", "BUY", 1,
                             price, datetime(2030, 1, 1, tzinfo=timezone.utc))


class OmsWriteFailureTests(unittest.TestCase):
    def test_result_write_failure_gates_all_outcomes_and_requires_replay(self):
        for status in SubmissionStatus:
            with self.subTest(status=status):
                oms, journal, execution = ready(status)
                proposal = intent()
                state = {SubmissionStatus.ACCEPTED: "ACCEPTED", SubmissionStatus.REJECTED: "REJECTED",
                         SubmissionStatus.UNCERTAIN: "UNCERTAIN"}[status]
                journal.arm(events.SUBMISSION_STATE, state)
                with self.assertRaises(OSError):
                    oms.process(proposal, RiskDecision(RiskAction.ALLOW, 3))
                self.assertIs(oms.state, OmsState.RECOVERING)
                self.assertIs(oms.by_intent[proposal.intent_id].state, OrderState.SUBMITTING)
                self.assertIsNone(oms.by_intent[proposal.intent_id].provider_order_id)
                with self.assertRaises(RecoveryRequired):
                    oms.process(intent(), RiskDecision(RiskAction.ALLOW, 3))
                replays, snapshots = journal.replays, execution.snapshots
                oms.recover()
                self.assertGreater(journal.replays, replays)
                self.assertGreater(execution.snapshots, snapshots)
                self.assertEqual(execution.submissions, 1)
                if status is SubmissionStatus.UNCERTAIN:
                    with self.assertRaises(RecoveryRequired):
                        oms.process(intent(), RiskDecision(RiskAction.ALLOW, 3))
                else:
                    self.assertIs(oms.state, OmsState.READY)
                validate_replay(journal.replay())

    def test_commit_then_error_is_reconciled_without_resubmission(self):
        oms, journal, execution = ready()
        proposal = intent()
        journal.arm(events.SUBMISSION_STATE, "ACCEPTED", after_commit=True)
        with self.assertRaises(OSError):
            oms.process(proposal, RiskDecision(RiskAction.ALLOW, 3))
        oms.recover()
        row = oms.process(proposal, RiskDecision(RiskAction.ALLOW, 3))
        self.assertIs(row.state, OrderState.ACCEPTED)
        self.assertEqual(execution.submissions, 1)

    def test_exception_outcome_write_failure_also_requires_recovery(self):
        oms, journal, execution = ready(raises=True)
        journal.arm(events.SUBMISSION_STATE, "UNCERTAIN")
        with self.assertRaises(OSError):
            oms.process(intent(), RiskDecision(RiskAction.ALLOW, 3))
        self.assertIs(oms.state, OmsState.RECOVERING)
        oms.recover()
        self.assertEqual(execution.submissions, 1)

    def test_admission_writes_gate_before_any_submit(self):
        for event_type, state in [(events.INTENT_DECIDED, None), (events.ORDER_CREATED, None),
                                  (events.SUBMISSION_STATE, "SUBMITTING")]:
            with self.subTest(event=event_type):
                oms, journal, execution = ready()
                journal.arm(event_type, state)
                with self.assertRaises(OSError):
                    oms.process(intent(), RiskDecision(RiskAction.ALLOW, 3))
                self.assertIs(oms.state, OmsState.RECOVERING)
                self.assertEqual(execution.submissions, 0)

    def test_recovery_writes_leave_recovery_gated(self):
        for event_type in (events.PLATFORM_POSITIONS, events.PLATFORM_ORDER,
                           events.PLATFORM_EXECUTION, events.RECONCILIATION):
            with self.subTest(event=event_type):
                oms, journal, execution = ready()
                row = oms.process(intent(), RiskDecision(RiskAction.ALLOW, 3))
                execution.executions = (replace(fill(), provider_order_id=row.provider_order_id),)
                journal.arm(event_type)
                oms.state = OmsState.RECOVERING
                with self.assertRaises(OSError):
                    oms.recover()
                self.assertIs(oms.state, OmsState.RECOVERING)
                with self.assertRaises(RecoveryRequired):
                    oms.process(intent(), RiskDecision(RiskAction.ALLOW, 3))
                self.assertEqual(execution.submissions, 1)

    def test_cancel_authorization_failure_does_not_call_adapter(self):
        oms, journal, execution = ready()
        row = oms.process(intent(), RiskDecision(RiskAction.ALLOW, 3))
        journal.arm(events.EXECUTION_CANCEL_REQUESTED)
        with self.assertRaises(OSError):
            oms.cancel(row.order.order_id)
        self.assertIs(oms.state, OmsState.RECOVERING)
        self.assertEqual(execution.cancellations, 0)

    def test_existing_inbox_write_guards_remain_active(self):
        for event_type in (events.EXECUTION_EVENT_RECEIVED, events.EXECUTION_EVENT_DECISION):
            with self.subTest(event=event_type):
                oms, journal, _ = ready()
                journal.arm(event_type)
                with self.assertRaises(OSError):
                    oms.accept_execution_event(ExecutionEvent(ExecutionEventKind.STREAM_CONNECTED))
                    oms.drain_execution_events()
                self.assertIs(oms.state, OmsState.RECOVERING)

    def test_normal_results_and_duplicate_intents_are_unchanged(self):
        for status in SubmissionStatus:
            with self.subTest(status=status):
                oms, journal, execution = ready(status)
                proposal = intent()
                row = oms.process(proposal, RiskDecision(RiskAction.ALLOW, 3))
                self.assertEqual(row.state.value, status.value)
                self.assertIs(oms.process(proposal, RiskDecision(RiskAction.ALLOW, 3)), row)
                self.assertEqual(execution.submissions, 1)
                validate_replay(journal.replay())


class DecimalFingerprintTests(unittest.TestCase):
    def test_high_precision_payload_and_fingerprint_ignore_context(self):
        price = Decimal("123456789012345678901234567890.123456789012345678900")
        event = ExecutionEvent(ExecutionEventKind.EXECUTION, "synthetic-order",
                               fill().timestamp, execution=fill(price))
        baseline = None
        for precision in (1, 4, 28, 80):
            for capitals in (0, 1):
                with self.subTest(precision=precision, capitals=capitals):
                    with localcontext(Context(prec=precision, capitals=capitals)) as ctx:
                        ctx.traps[Inexact] = ctx.traps[Rounded] = True
                        payload = execution_payload(fill(price))
                        pair = (payload["price"], event.fingerprint)
                        baseline = baseline or pair
                        self.assertEqual(pair, baseline)
                        self.assertEqual(execution_from_payload(payload).price, price)
                        self.assertFalse(ctx.flags[Inexact])
                        self.assertFalse(ctx.flags[Rounded])

    def test_numeric_equivalents_trailing_zeros_and_signed_zero(self):
        for group in [("32.100", "0.321000e+2", "32.1"), ("100", "1E+2", "100.00"),
                      ("0", "0E-999999", "0.000"), ("-0", "-0E-999999", "-0.000")]:
            fingerprints = set()
            for value in group:
                record = fill(Decimal(value))
                event = ExecutionEvent(ExecutionEventKind.EXECUTION, "synthetic-order",
                                       record.timestamp, execution=record)
                fingerprints.add(event.fingerprint)
                self.assertEqual(execution_from_payload(execution_payload(record)).price, record.price)
            self.assertEqual(len(fingerprints), 1)

    def test_ordinary_legacy_text_and_extreme_exponents(self):
        pairs = [("32.100", "32.1"), ("100.0", "1E+2"), ("0.0000010", "0.000001"),
                 ("0.00000010", "1E-7"), ("-123.4500", "-123.45"), ("-0.000", "-0"),
                 ("1E+999999", "1E+999999"), ("-1E-999999", "-1E-999999")]
        for value, text in pairs:
            with self.subTest(value=value), localcontext(Context(prec=1, Emax=9, Emin=-9, capitals=0)):
                self.assertEqual(execution_payload(fill(Decimal(value)))["price"], text)

    def test_nonfinite_or_nondecimal_prices_are_rejected(self):
        for value in (Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity"), 1.5, True):
            with self.subTest(value=str(value)), self.assertRaises(ValueError):
                execution_payload(fill(value))


class QuantityTests(unittest.TestCase):
    def test_valid_intents_and_limits(self):
        for value in (1, 3):
            self.assertEqual(intent(value).quantity, value)
            self.assertEqual(BasicRiskEvaluator(value).evaluate(intent(value)).quantity, value)

    def test_invalid_quantity_types_and_values(self):
        for value in (0, -1, 1.5, True, False, Decimal(1), Fraction(1), "1", None, type("IntSubclass", (int,), {})(1)):
            with self.subTest(value=str(value)):
                with self.assertRaises(ValueError):
                    intent(value)
                with self.assertRaises(ValueError):
                    BasicRiskEvaluator(value)

    def test_forged_invalid_intent_is_rejected_before_journal_write(self):
        for value in (1.5, True, False, 0, -1):
            oms, journal, execution = ready()
            proposal = intent()
            object.__setattr__(proposal, "quantity", value)
            before = journal.replay()
            with self.assertRaises(ValueError):
                BasicRiskEvaluator(3).evaluate(proposal)
            with self.assertRaises(ValueError):
                oms.process(proposal, RiskDecision(RiskAction.ALLOW, 1))
            self.assertEqual(journal.replay(), before)
            self.assertEqual(execution.submissions, 0)

    def test_bad_risk_decisions_do_not_write(self):
        decisions = [RiskDecision(RiskAction.ALLOW, v) for v in (0, -1, 1.5, True, False, Decimal(1), 4)]
        decisions += [RiskDecision("ALLOW", 1), RiskDecision(RiskAction.ALLOW, 1, []),
                      RiskDecision(RiskAction.REJECT, True), None]
        for decision in decisions:
            with self.subTest(decision=str(decision)):
                oms, journal, execution = ready()
                before = journal.replay()
                with self.assertRaises(ValueError):
                    oms.process(intent(), decision)
                self.assertEqual(journal.replay(), before)
                self.assertEqual(execution.submissions, 0)

    def test_allow_reduce_reject_and_sqlite_replay(self):
        for action, quantity in [(RiskAction.ALLOW, None), (RiskAction.ALLOW, 3),
                                 (RiskAction.REDUCE, 1), (RiskAction.REJECT, None)]:
            with self.subTest(action=action, quantity=quantity):
                with tempfile.TemporaryDirectory(dir=os.environ.get("PROP_TEST_SCRATCH")) as tmp:
                    path = Path(tmp) / "synthetic.sqlite3"
                    journal = SQLiteOmsJournal(path)
                    execution = SyntheticExecution()
                    oms = OmsService(journal, execution)
                    try:
                        oms.recover()
                        proposal = intent()
                        row = oms.process(proposal, RiskDecision(action, quantity))
                        self.assertEqual(execution.submissions, 0 if action is RiskAction.REJECT else 1)
                        if row:
                            self.assertEqual(row.order.quantity, quantity or 3)
                        validate_replay(journal.replay())
                    finally:
                        journal.close()
                    journal = SQLiteOmsJournal(path)
                    try:
                        restored = OmsService(journal, execution)
                        restored.recover()
                        self.assertEqual(restored.by_intent[proposal.intent_id], row)
                    finally:
                        journal.close()

    def test_replay_refuses_invalid_persisted_decision_quantity(self):
        for value in (0, -1, 1.5, True, False, None):
            journal = InMemoryOmsJournal()
            journal.append(events.INTENT_DECIDED, {"intent_id": str(intent().intent_id),
                                                  "action": "ALLOW", "quantity": value})
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_replay(journal.replay())


class MemoryJournalTests(unittest.TestCase):
    def test_original_nested_payload_is_detached(self):
        journal = InMemoryOmsJournal()
        payload = {"rows": [{"quantity": 1}], "meta": {"tags": ["synthetic"]}}
        expected = deepcopy(payload)
        journal.append("synthetic", payload)
        payload["rows"][0]["quantity"] = 99
        payload["meta"]["tags"].append("changed")
        self.assertEqual(journal.replay()[0].payload, expected)

    def test_append_result_is_detached(self):
        journal = InMemoryOmsJournal()
        row = journal.append("synthetic", {"nested": [{"quantity": 1}]})
        row.payload["nested"][0]["quantity"] = 99
        row.payload["new"] = True
        self.assertEqual(journal.replay()[0].payload, {"nested": [{"quantity": 1}]})

    def test_replay_payload_and_list_are_detached_and_order_preserved(self):
        journal = InMemoryOmsJournal()
        journal.append("first", {"nested": [1]})
        journal.append("second", {"nested": [2]})
        rows = journal.replay()
        rows[0].payload["nested"].append(99)
        rows.reverse()
        rows.pop()
        self.assertEqual([(r.sequence, r.event_type, r.payload) for r in journal.replay()],
                         [(1, "first", {"nested": [1]}), (2, "second", {"nested": [2]})])


def context(target=Decimal("1100")):
    engine = StatefulPropRuleEngine(PropRuleConfig(Decimal(1000), Decimal(300), Decimal(200), Decimal(100)))
    return RiskContext(replace(engine.state, target_equity=target), OmsState.READY, SessionState.OPEN)


class FractionContextTests(unittest.TestCase):
    def test_fraction_decimal_and_terminating_fraction_round_trip(self):
        for target in (Fraction(4000, 3), Decimal("1100.00"), Fraction(2201, 2)):
            with self.subTest(target=str(target)):
                value = context(target)
                encoded = _encode_context(value)
                decoded = _context(json.loads(json.dumps(encoded)))
                self.assertEqual(decoded, value)
                self.assertIs(type(decoded.account.target_equity), type(target))
                if type(target) is Decimal:
                    self.assertEqual(encoded["account"]["target_equity"], "1100.00")

    def test_plan_emits_exact_nonterminating_target_unchanged(self):
        source = Path(__file__).resolve().parents[1] / "config/prop_plans/fictional-aurora-static.json"
        plan = PropPlan.from_json(json.dumps(json.loads(source.read_text())["plan"]))
        plan = replace(plan, starting_balance=Decimal(1000), profit_target_gain=Decimal(100),
                       consistency_fraction=Decimal("0.3"), consistency_mode=ConsistencyMode.EXTEND_TARGET)
        engine = PlanRuleEngine(plan)
        engine.apply(SessionStarted("synthetic-session"))
        engine.apply(PnlSettled(Decimal(100)))
        self.assertEqual(engine.state.target_equity, Fraction(4000, 3))
        self.assertEqual(_context(_encode_context(engine.risk_context())), engine.risk_context())

    def test_publish_revision_reopen_replay_and_reservation(self):
        for target in (Fraction(4000, 3), Decimal("1100.00"), Fraction(2201, 2)):
            with self.subTest(target=str(target)), tempfile.TemporaryDirectory(dir=os.environ.get("PROP_TEST_SCRATCH")) as tmp:
                path = Path(tmp) / "synthetic.sqlite3"
                ledger = SQLiteRiskLedger(path, account_id="synthetic-account", config=RiskPolicyConfig(3))
                try:
                    ledger.publish_context(context(target), expected_revision=0)
                    self.assertEqual(ledger.context_revision, 1)
                    before = ledger.replay()
                    with self.assertRaises(ReservationRecoveryRequired):
                        ledger.publish_context(context(target), expected_revision=0)
                    self.assertEqual(ledger.replay(), before)
                    ledger.publish_context(context(target), expected_revision=1)
                    self.assertEqual(ledger.context_revision, 2)
                    oms = OmsService(ledger, SyntheticExecution())
                    oms.recover()
                    self.assertTrue(ledger.risk_ready())
                    result = ledger.reserve(intent(1), ContractLossEstimate(Decimal(10)))
                    self.assertIs(result.decision.action, RiskAction.ALLOW)
                    self.assertEqual(_context(json.loads(ledger._account()[0])).account.target_equity, target)
                    validate_replay(ledger.replay())
                finally:
                    ledger.close()
                ledger = SQLiteRiskLedger(path, account_id="synthetic-account", config=RiskPolicyConfig(3))
                try:
                    OmsService(ledger, SyntheticExecution()).recover()
                    self.assertEqual(ledger.context_revision, 2)
                    self.assertEqual(_context(json.loads(ledger._account()[0])).account.target_equity, target)
                    self.assertEqual(len(ledger.reservations()), 1)
                    self.assertTrue(ledger.risk_ready())
                finally:
                    ledger.close()

    def test_invalid_target_publish_leaves_history_and_revision_unchanged(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("PROP_TEST_SCRATCH")) as tmp:
            ledger = SQLiteRiskLedger(Path(tmp) / "synthetic.sqlite3", account_id="synthetic-account", config=RiskPolicyConfig(3))
            try:
                ledger.publish_context(context(), expected_revision=0)
                for bad in (Fraction(-1, 3), Fraction(0), Decimal("NaN"), Decimal("Infinity"), True, 1.5):
                    before = ledger.replay()
                    with self.subTest(target=str(bad)), self.assertRaises(ValueError):
                        ledger.publish_context(context(bad), expected_revision=1)
                    self.assertEqual(ledger.replay(), before)
                    self.assertEqual(ledger.context_revision, 1)
            finally:
                ledger.close()

    def test_malformed_tagged_target_rolls_back_journal_and_projection(self):
        good = {"type": "fraction", "numerator": 4000, "denominator": 3}
        invalid = [dict(good, numerator=True), dict(good, denominator=True), dict(good, denominator=0),
                   dict(good, denominator=-3), dict(good, numerator=0), dict(good, numerator=-1),
                   dict(good, type="other"), dict(good, numerator="4000"), dict(good, extra=1),
                   {"type": "fraction", "numerator": 4000}, "4000/3"]
        for bad in invalid:
            with self.subTest(target=str(bad)), tempfile.TemporaryDirectory(dir=os.environ.get("PROP_TEST_SCRATCH")) as tmp:
                ledger = SQLiteRiskLedger(Path(tmp) / "synthetic.sqlite3", account_id="synthetic-account", config=RiskPolicyConfig(3))
                try:
                    ledger.publish_context(context(), expected_revision=0)
                    encoded = _encode_context(context())
                    encoded["account"]["target_equity"] = bad
                    before = ledger.replay()
                    with self.assertRaises((ValueError, ArithmeticError)):
                        ledger.append(events.RISK_CONTEXT, {"context": encoded, "revision": 2})
                    self.assertEqual(ledger.replay(), before)
                    self.assertEqual(ledger.context_revision, 1)
                    self.assertFalse(ledger.risk_ready())
                finally:
                    ledger.close()


if __name__ == "__main__":
    unittest.main()
