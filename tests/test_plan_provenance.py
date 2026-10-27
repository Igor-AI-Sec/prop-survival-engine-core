"""Fictional specifications and invented source references; no external research."""
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta
import json

import pytest

from prop_survival_engine.prop_rules.plan_engine import PlanRuleEngine, plan_evaluator
from prop_survival_engine.prop_rules.provenance import (
    PlanConfiguration, PlanOrigin, PlanVerificationError, RuleProvenance, RuleSource,
    SourceType, VerificationContext, VerificationStatus as V, rule_paths, rule_values,
)
from prop_survival_engine.risk.policy import ContractLossEstimate, RiskPolicyConfig
from prop_survival_engine.simulation.outcomes import SequenceOutcomeSource, TradeOutcome
from prop_survival_engine.simulation.reach_avoid import PathConfig, Scenario
from prop_survival_engine.simulation.sizing import FixedContracts
from tests.test_prop_plans import fictional_plan
from decimal import Decimal


AS_OF = date(2026, 1, 15)
CONTEXT = VerificationContext(AS_OF, 30)


def source(value, **changes):
    return replace(RuleSource("https://example.invalid/fictional-rules", SourceType.OFFICIAL,
        AS_OF, None, value, "Invented source for offline tests only"), **changes)


def external_fixture():
    # EXTERNAL exercises gates only. Every value/reference is fictional.
    plan = fictional_plan()
    return PlanConfiguration(plan, PlanOrigin.EXTERNAL, tuple(
        RuleProvenance(path, V.VERIFIED, (source(value),)) for path, value in rule_values(plan).items()))


def change_rule(config, path, **changes):
    return replace(config, rules=tuple(replace(r, **changes) if r.rule_path == path else r for r in config.rules))


def reasons(config, path="maximum_drawdown", context=CONTEXT):
    return next(r.reasons for r in config.verify(context).unresolved if r.rule_path == path)


def test_fictional_config_needs_explicit_origin_but_no_external_sources():
    config = PlanConfiguration(fictional_plan(), PlanOrigin.FICTIONAL)
    assert config.require_usable(CONTEXT) == config.plan
    assert config.verify(CONTEXT).usable
    assert PlanRuleEngine(config, verification_context=CONTEXT).state.equity == 1000
    with pytest.raises(ValueError): PlanConfiguration(config.plan, "FICTIONAL")
    with pytest.raises(TypeError): PlanConfiguration(config.plan)


def test_fictional_cannot_carry_claim_of_external_verification():
    with pytest.raises(ValueError): replace(external_fixture(), origin=PlanOrigin.FICTIONAL)


def test_missing_all_provenance_reports_every_leaf_including_disabled_rules():
    config = PlanConfiguration(fictional_plan(), PlanOrigin.EXTERNAL)
    report = config.verify(CONTEXT)
    assert tuple(r.rule_path for r in report.unresolved) == rule_paths()
    assert "daily_loss_limit" in report.required_rules
    assert "fees.activation" in report.required_rules and "payouts.enabled" in report.required_rules
    assert all(r.reasons == ("MISSING_PROVENANCE",) for r in report.unresolved)
    with pytest.raises(PlanVerificationError) as caught: config.require_usable(CONTEXT)
    assert caught.value.report == report


@pytest.mark.parametrize("status", [V.AMBIGUOUS, V.UNVERIFIED, V.STALE])
def test_unresolved_required_rule_blocks_engine_and_simulation(status):
    config = change_rule(external_fixture(), "maximum_drawdown", status=status)
    assert status.value in reasons(config)
    with pytest.raises(PlanVerificationError): PlanRuleEngine(config, verification_context=CONTEXT)
    with pytest.raises(PlanVerificationError): plan_evaluator(config, RiskPolicyConfig(1),
        ContractLossEstimate(Decimal(10)), verification_context=CONTEXT)


def test_verified_status_alone_without_evidence_does_not_authorize():
    config = change_rule(external_fixture(), "maximum_drawdown", sources=())
    assert reasons(config) == ("MISSING_SOURCE", "NO_OFFICIAL_SUPPORT")


@pytest.mark.parametrize("kind", [SourceType.SECONDARY, SourceType.INFERRED])
def test_secondary_or_inferred_only_cannot_establish_official_verification(kind):
    config = external_fixture()
    entry = next(r for r in config.rules if r.rule_path == "maximum_drawdown")
    config = change_rule(config, entry.rule_path, sources=(replace(entry.sources[0], source_type=kind),))
    assert reasons(config) == ("NO_OFFICIAL_SUPPORT",)


def test_fresh_matching_official_and_secondary_support_preserved():
    config = external_fixture(); entry = config.rules[0]
    secondary = replace(entry.sources[0], reference="fictional archived document", source_type=SourceType.SECONDARY)
    config = change_rule(config, entry.rule_path, sources=entry.sources + (secondary,))
    assert config.verify(CONTEXT).usable
    assert config.require_usable(CONTEXT) == config.plan


def test_conflicts_never_overwrite_even_with_verified_label():
    config = external_fixture()
    entry = next(r for r in config.rules if r.rule_path == "maximum_drawdown")
    conflicting = replace(entry.sources[0], reference="fictional conflicting source", value_json='"201"')
    combined = entry.add_source(conflicting)
    assert combined.status is V.AMBIGUOUS and combined.sources[0] == entry.sources[0]
    assert combined.sources[1] == conflicting and len(entry.sources) == 1
    config = change_rule(config, entry.rule_path, sources=combined.sources, status=V.VERIFIED,
                         conflict_notes="Fictional discrepancy remains unresolved")
    assert reasons(config) == ("CONFLICTING_SOURCES", "VALUE_MISMATCH")
    restored = PlanConfiguration.from_json(config.to_json())
    assert restored.rules == config.rules and not restored.verify(CONTEXT).usable


def test_consistent_but_wrong_claim_does_not_verify_configured_value():
    config = change_rule(external_fixture(), "maximum_drawdown", sources=(source('"201"'),))
    assert reasons(config) == ("VALUE_MISMATCH",)


def test_plan_change_invalidates_prior_evidence():
    config = external_fixture()
    config = replace(config, plan=replace(config.plan, maximum_drawdown=Decimal(201)))
    assert reasons(config) == ("VALUE_MISMATCH",)


@pytest.mark.parametrize("age,usable", [(29, True), (30, True), (31, False)])
def test_explicit_staleness_boundary(age, usable):
    config = external_fixture()
    entry = next(r for r in config.rules if r.rule_path == "maximum_drawdown")
    old = replace(entry.sources[0], observed_date=AS_OF - timedelta(days=age))
    config = change_rule(config, entry.rule_path, sources=(old,))
    assert config.verify(CONTEXT).usable is usable
    if not usable: assert "STALE_OFFICIAL_SUPPORT" in reasons(config)


def test_unknown_effective_date_allowed_future_effective_date_blocks():
    config = external_fixture(); assert config.verify(CONTEXT).usable
    entry = config.rules[0]
    config = change_rule(config, entry.rule_path, sources=(replace(entry.sources[0], effective_date=AS_OF + timedelta(days=1)),))
    assert "NOT_YET_EFFECTIVE" in reasons(config, entry.rule_path)


def test_future_observation_rejects_no_local_clock_fallback():
    config = external_fixture(); entry = config.rules[0]
    config = change_rule(config, entry.rule_path, sources=(replace(entry.sources[0], observed_date=AS_OF + timedelta(days=1)),))
    assert "FUTURE_OBSERVATION" in reasons(config, entry.rule_path)
    with pytest.raises(ValueError): config.verify(None)
    with pytest.raises(ValueError): PlanRuleEngine(config)


def test_recent_secondary_does_not_refresh_old_official_source():
    config = external_fixture(); entry = config.rules[0]
    old = replace(entry.sources[0], observed_date=AS_OF - timedelta(days=31))
    secondary = replace(entry.sources[0], source_type=SourceType.SECONDARY)
    config = change_rule(config, entry.rule_path, sources=(old, secondary))
    assert "STALE_OFFICIAL_SUPPORT" in reasons(config, entry.rule_path)


def test_explicit_rereview_matching_official_evidence_can_refresh():
    config = external_fixture(); entry = config.rules[0]
    old = replace(entry.sources[0], observed_date=AS_OF - timedelta(days=31))
    config = change_rule(config, entry.rule_path, sources=(old, entry.sources[0]))
    assert config.verify(CONTEXT).usable


def test_disabled_rule_still_needs_provenance():
    config = external_fixture()
    assert config.plan.consistency_fraction is None
    config = replace(config, rules=tuple(r for r in config.rules if r.rule_path != "consistency_fraction"))
    assert reasons(config, "consistency_fraction") == ("MISSING_PROVENANCE",)


def test_duplicate_rule_entries_cannot_silently_overwrite():
    config = external_fixture()
    with pytest.raises(ValueError): replace(config, rules=config.rules + (config.rules[0],))
    with pytest.raises(ValueError): config.rules[0].add_source(config.rules[0].sources[0])


def test_reports_are_deterministic_and_independent_of_rule_entry_order():
    config = external_fixture()
    config = change_rule(config, "maximum_drawdown", status=V.AMBIGUOUS)
    config = change_rule(config, "profit_target_gain", status=V.UNVERIFIED)
    reversed_config = replace(config, rules=tuple(reversed(config.rules)))
    assert config.verify(CONTEXT) == reversed_config.verify(CONTEXT)
    assert config.to_json() == reversed_config.to_json()
    assert config.verify(CONTEXT) == config.verify(CONTEXT)


def test_serialization_preserves_every_source_date_status_note_and_claim():
    config = external_fixture()
    entry = config.rules[0]
    config = change_rule(config, entry.rule_path, status=V.AMBIGUOUS, conflict_notes="Fictional source conflict",
        sources=(replace(entry.sources[0], effective_date=AS_OF - timedelta(days=10), notes="Reviewed fictional claim"),))
    restored = PlanConfiguration.from_json(config.to_json())
    assert restored == config and restored.to_json() == config.to_json()
    assert restored.verify(CONTEXT) == config.verify(CONTEXT)
    with pytest.raises(FrozenInstanceError): restored.origin = PlanOrigin.FICTIONAL


@pytest.mark.parametrize("mutation", ["missing", "unknown", "status", "date", "duplicate_rule", "version", "origin", "source_type", "numeric_claim"])
def test_serialization_rejects_invalid_or_ambiguous_metadata(mutation):
    data = json.loads(external_fixture().to_json())
    if mutation == "missing": del data["rules"][0]["sources"][0]["observed_date"]
    if mutation == "unknown": data["rules"][0]["qualified"] = True
    if mutation == "status": data["rules"][0]["status"] = "unknown"
    if mutation == "date": data["rules"][0]["sources"][0]["observed_date"] = "20260115"
    if mutation == "duplicate_rule": data["rules"].append(data["rules"][0])
    if mutation == "version": data["version"] = True
    if mutation == "origin": data["origin"] = "REAL"
    if mutation == "source_type": data["rules"][0]["sources"][0]["source_type"] = "guessed"
    if mutation == "numeric_claim": data["rules"][0]["sources"][0]["value_json"] = "1.5"
    with pytest.raises(ValueError): PlanConfiguration.from_json(json.dumps(data))


def test_duplicate_json_keys_reject():
    text = external_fixture().to_json().replace('"version":1', '"version":1,"version":1')
    with pytest.raises(ValueError): PlanConfiguration.from_json(text)


@pytest.mark.parametrize("change", [dict(reference=""), dict(reference="https://name:secret@example.invalid/rules"),
    dict(reference="https://example.invalid/rules?token=fictional"), dict(source_type="OFFICIAL"),
    dict(observed_date="2026-01-15"), dict(observed_date=datetime(2026, 1, 15)), dict(effective_date="unknown"),
    dict(value_json="{}"), dict(value_json="NaN")])
def test_invalid_source_metadata_rejects(change):
    with pytest.raises(ValueError): source('"200"', **change)


def test_verified_config_is_rechecked_at_new_as_of_date():
    config = external_fixture()
    assert config.verify(CONTEXT).usable
    with pytest.raises(PlanVerificationError): PlanRuleEngine(config,
        verification_context=VerificationContext(AS_OF + timedelta(days=31)))


@pytest.mark.parametrize("origin", list(PlanOrigin))
def test_wrapped_config_simulation_is_reproducible_without_changing_methodology(origin):
    config = external_fixture() if origin is PlanOrigin.EXTERNAL else PlanConfiguration(fictional_plan(), origin)
    kwargs = dict(simulations=2, seed=166)
    scenario = Scenario("fictional-provenance", SequenceOutcomeSource((TradeOutcome(Decimal(50)),), repeat=True))
    candidate = FixedContracts("fictional-one", 1)
    wrapped = plan_evaluator(config, RiskPolicyConfig(1), ContractLossEstimate(Decimal(10)),
                             PathConfig(2, 2), verification_context=CONTEXT)
    plain = plan_evaluator(config.plan, RiskPolicyConfig(1), ContractLossEstimate(Decimal(10)), PathConfig(2, 2))
    first = wrapped.evaluate(candidate, scenario, **kwargs)
    assert first == wrapped.evaluate(candidate, scenario, **kwargs)
    assert first == plain.evaluate(candidate, scenario, **kwargs)
    assert "plan_configuration" in PlanRuleEngine(config, verification_context=CONTEXT).snapshot()
