"""Sparse reviewed research records, not coerced complete executable plans.

Plan names and source-specific labels live in the external catalog. The core
PropPlan and simulation method remain provider-neutral and unchanged.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from fractions import Fraction
import json
import re
from pathlib import Path

from prop_survival_engine.prop_rules.provenance import (
    RuleSource, SourceType, VerificationContext, VerificationStatus as V,
)


class Operation(str, Enum):
    CHALLENGE = "CHALLENGE"
    FUNDED_PAYOUT = "FUNDED_PAYOUT"
    EXECUTION = "EXECUTION"


class ExecutionScope(str, Enum):
    PERSONAL_SIM_NON_HFT = "PERSONAL_SIM_NON_HFT"
    CLOUD_SIM = "CLOUD_SIM"
    LIVE = "LIVE"


@dataclass(frozen=True)
class ResearchRule:
    key: str
    claim: str
    original_status: str
    status: V
    original_source: str
    effective_conflict: str
    sources: tuple[RuleSource, ...]


@dataclass(frozen=True)
class LayerReport:
    operation: Operation
    blockers: tuple[tuple[str, tuple[str, ...]], ...]
    origin: str = "FICTIONAL"
    admission_basis: str = "fictional-offline-model-only"
    provider_permission_verified: bool = False

    @property
    def admitted(self):
        return not self.blockers


class LayerAdmissionError(ValueError):
    def __init__(self, report):
        self.report = report
        super().__init__("layer blocked: " + ", ".join(k for k, _ in report.blockers))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate dataset key")
        result[key] = value
    return result


def _status(text):
    # Composite/mixed statuses are never flattened to VERIFIED.
    tokens = [s for s in V if s.value in text]
    if text.startswith("VERIFIED") and not text.startswith("UNVERIFIED") and len(tokens) == 1:
        return V.VERIFIED
    if text.startswith("UNVERIFIED"):
        return V.UNVERIFIED
    if text.startswith("STALE"):
        return V.STALE
    return V.AMBIGUOUS


@dataclass(frozen=True)
class ReviewedPlan:
    name: str
    observed_date: date
    rules: tuple[ResearchRule, ...]
    requirements: tuple[tuple[Operation, tuple[str, ...]], ...]
    model_blockers: tuple[tuple[Operation, tuple[str, ...]], ...]
    execution_scopes: tuple[ExecutionScope, ...]
    # Exact monetary extracts with an explicit binding back to reviewed rows.
    monetary_fields: tuple[tuple[str, Decimal, str], ...]
    origin: str = "FICTIONAL"

    def __post_init__(self):
        if (type(self.name) is not str or not self.name or type(self.observed_date) is not date
                or type(self.rules) is not tuple or any(type(r) is not ResearchRule for r in self.rules)
                or len({r.key for r in self.rules}) != len(self.rules)):
            raise ValueError("invalid reviewed plan or duplicate rule")
        if ({op for op, _ in self.requirements} != set(Operation)
                or len(self.requirements) != len(Operation)):
            raise ValueError("all operation dependencies required")
        if any(type(keys) is not tuple or not keys or len(set(keys)) != len(keys)
               or any(type(key) is not str or not key for key in keys)
               for _, keys in self.requirements):
            raise ValueError("nonempty explicit operation dependencies required")
        if (type(self.execution_scopes) is not tuple
                or any(type(scope) is not ExecutionScope for scope in self.execution_scopes)):
            raise ValueError("explicit supported execution scopes required")
        if len({name for name, _, _ in self.monetary_fields}) != len(self.monetary_fields):
            raise ValueError("duplicate normalized field")
        for _, value, source in self.monetary_fields:
            if type(value) is not Decimal or not value.is_finite() or source not in {r.key for r in self.rules}:
                raise ValueError("invalid monetary extraction or binding")

    def _problems(self, key, context):
        rule = next((r for r in self.rules if r.key == key), None)
        if rule is None:
            return ("MISSING_RULE",)
        reasons = []
        if rule.status is not V.VERIFIED:
            reasons.append(rule.status.value)
        if len({s.value_json for s in rule.sources}) > 1:
            reasons.append("CONFLICTING_SOURCES")
        expected = json.dumps(rule.claim, ensure_ascii=True)
        if any(s.value_json != expected for s in rule.sources):
            reasons.append("VALUE_MISMATCH")
        if any(s.observed_date > context.as_of for s in rule.sources):
            reasons.append("FUTURE_OBSERVATION")
        if any(s.effective_date and s.effective_date > context.as_of for s in rule.sources):
            reasons.append("NOT_YET_EFFECTIVE")
        official = [s for s in rule.sources if s.source_type is SourceType.OFFICIAL]
        if not official:
            reasons.append("NO_OFFICIAL_SUPPORT")
        elif not any(0 <= (context.as_of - s.observed_date).days <= context.max_age_days for s in official):
            reasons.append("STALE_OFFICIAL_SUPPORT")
        return tuple(reasons)

    def qualify(self, operation, context, *, execution_scope=None):
        if type(operation) is not Operation or type(context) is not VerificationContext:
            raise ValueError("explicit operation and verification context required")
        problems = []
        if execution_scope in (ExecutionScope.LIVE, ExecutionScope.CLOUD_SIM):
            problems.append(("fictional_origin", ("NO_REAL_PROVIDER_PERMISSION",)))
        for key in dict(self.requirements)[operation]:
            reasons = self._problems(key, context)
            if reasons:
                problems.append((key, reasons))
        for limitation in dict(self.model_blockers).get(operation, ()):
            problems.append((limitation, ("MODEL_UNSUPPORTED",)))
        if operation is Operation.EXECUTION:
            if type(execution_scope) is not ExecutionScope:
                problems.append(("execution_scope", ("EXPLICIT_SCOPE_REQUIRED",)))
            elif execution_scope not in self.execution_scopes:
                problems.append(("execution_scope", ("PERMISSION_NOT_VERIFIED_FOR_SCOPE",)))
        return LayerReport(operation, tuple(sorted(problems)))

    def require(self, operation, context, **kwargs):
        report = self.qualify(operation, context, **kwargs)
        if not report.admitted:
            raise LayerAdmissionError(report)
        return report

    def challenge_evaluator(self, context, *args, **kwargs):
        self.require(Operation.CHALLENGE, context)
        # Sparse data is not an executable PropPlan, even if provenance passes.
        raise ValueError("complete audited PropPlan binding required; no defaults may fill missing fields")

    def target_drawdown_ratio(self, context):
        """Exact offline mathematics requires only verified target/DD evidence."""
        values = {key: (value, rule) for key, value, rule in self.monetary_fields}
        selected = []
        for key in ("profit_target_gain", "maximum_drawdown"):
            if key not in values:
                raise ValueError("required monetary field missing")
            value, rule = values[key]
            if self._problems(rule, context) or value <= 0:
                raise ValueError("target/drawdown evidence not qualified")
            selected.append(Fraction(value))
        return selected[0] / selected[1]


def load_catalog(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique)
    if type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("unsupported catalog version")
    if data.get("origin") != "FICTIONAL":
        raise ValueError("This public catalog accepts explicit FICTIONAL records only")
    observed = date.fromisoformat(data["observed_date"])
    plans = []
    for plan in data["plans"]:
        rules = []
        for row in plan["rows"]:
            origin = row["source"]
            kind = (SourceType.OFFICIAL if "/ official" in origin else
                    SourceType.SECONDARY if "secondary" in origin else SourceType.INFERRED)
            source = RuleSource(origin if origin != "—" else data["source_reference"], kind,
                observed, None, json.dumps(row["claim"], ensure_ascii=True),
                row["effective_conflict"])
            # Explicit alternatives are retained as additional evidence, not
            # overwritten by a selected preferred number.
            sources = (source,) + tuple(RuleSource(item["reference"], SourceType(item["source_type"]),
                observed, date.fromisoformat(item["effective_date"]) if item.get("effective_date") else None,
                json.dumps(item["claim"], ensure_ascii=True), item["notes"])
                for item in row.get("alternatives", []))
            rules.append(ResearchRule(row["rule"], row["claim"], row["status"], _status(row["status"]),
                                      origin, row["effective_conflict"], sources))
        plans.append(ReviewedPlan(plan["name"], observed, tuple(rules),
            tuple((op, tuple(plan["requirements"][op.value])) for op in Operation),
            tuple((op, tuple(plan["model_blockers"].get(op.value, []))) for op in Operation),
            tuple(ExecutionScope(s) for s in plan["execution_scopes"]),
            tuple((key, Decimal(item["value"]), item["rule"]) for key, item in sorted(plan["monetary_fields"].items()))))
        for item in plan["monetary_fields"].values():
            claim = next(r.claim for r in rules if r.key == item["rule"])
            amounts = re.findall(r"\$([0-9][0-9,]*(?:\.[0-9]+)?)", claim)
            index = item["amount_index"]
            if (type(item["value"]) is not str or type(index) is not int or not 0 <= index < len(amounts)
                    or Decimal(item["value"]) != Decimal(amounts[index].replace(",", ""))):
                raise ValueError("monetary extraction differs from reviewed source claim")
    if len({p.name for p in plans}) != len(plans):
        raise ValueError("duplicate plan names")
    return tuple(plans)


def canonical_catalog_json(path):
    """Preserve raw statuses, context, bibliography and conflicts on round-trip."""
    load_catalog(path)  # Validate before a serialized record is treated as usable.
    data = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Offline reviewed prop-plan layer reports; no providers")
    parser.add_argument("catalog")
    parser.add_argument("--as-of", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    context = VerificationContext(args.as_of)
    output = []
    for plan in load_catalog(args.catalog):
        layers = []
        for op in Operation:
            report = plan.qualify(op, context, execution_scope=ExecutionScope.PERSONAL_SIM_NON_HFT)
            layers.append(dict(operation=op.value, admitted=report.admitted, blockers=report.blockers,
                               origin=report.origin, admission_basis=report.admission_basis,
                               provider_permission_verified=report.provider_permission_verified))
        output.append(dict(plan=plan.name, origin=plan.origin, layers=layers, target_drawdown_ratio=str(plan.target_drawdown_ratio(context))))
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
