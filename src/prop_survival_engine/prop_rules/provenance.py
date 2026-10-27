"""Offline rule-level provenance, explicit dates and conservative release gates.

This validates evidence supplied by a reviewer; it does not fetch sources or
prove that source statements are truthful. No firm-specific configuration lives here.
"""
from dataclasses import dataclass, fields, replace
from datetime import date
from enum import Enum
import json
from urllib.parse import urlsplit

from prop_survival_engine.prop_rules.plans import Fees, PayoutRules, PropPlan


class SourceType(str, Enum):
    OFFICIAL = "OFFICIAL"
    SECONDARY = "SECONDARY"
    INFERRED = "INFERRED"


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    AMBIGUOUS = "AMBIGUOUS"
    UNVERIFIED = "UNVERIFIED"
    STALE = "STALE"


class PlanOrigin(str, Enum):
    FICTIONAL = "FICTIONAL"
    EXTERNAL = "EXTERNAL"


def rule_paths() -> tuple[str, ...]:
    """Every leaf is required, including explicit disabled rules/null caps."""
    paths = []
    for field in fields(PropPlan):
        nested = {"fees": Fees, "payouts": PayoutRules}.get(field.name)
        paths.extend(f"{field.name}.{item.name}" for item in fields(nested)) if nested else paths.append(field.name)
    return tuple(sorted(paths))


def rule_values(plan: PropPlan) -> dict[str, str]:
    """Exact version-1 serialized values, not provider objects or float money."""
    data = json.loads(plan.to_json())["plan"]
    return {path: json.dumps(data[path.split(".")[0]][path.split(".")[1]] if "." in path else data[path],
                            sort_keys=True, separators=(",", ":")) for path in rule_paths()}


def _date(value):
    if type(value) is not date:
        raise ValueError("explicit date required (no datetime or implicit clock)")


def _text(value, *, required=False):
    if type(value) is not str or (required and not value.strip()):
        raise ValueError("explicit text required")


@dataclass(frozen=True)
class RuleSource:
    reference: str
    source_type: SourceType
    observed_date: date
    effective_date: date | None
    value_json: str
    notes: str = ""

    def __post_init__(self):
        _text(self.reference, required=True)
        _text(self.notes)
        if type(self.source_type) is not SourceType:
            raise ValueError("explicit source type required")
        _date(self.observed_date)
        if self.effective_date is not None:
            _date(self.effective_date)
        parsed = urlsplit(self.reference)
        if parsed.scheme:
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query:
                raise ValueError("source URLs must be HTTPS without auth or query parameters")
        _text(self.value_json, required=True)
        try:
            value = json.loads(self.value_json)
            if (type(value) not in (str, int, bool, type(None))
                    or json.dumps(value, separators=(",", ":")) != self.value_json):
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("source claim must be a canonical serialized rule leaf") from None


@dataclass(frozen=True)
class RuleProvenance:
    rule_path: str
    status: VerificationStatus
    sources: tuple[RuleSource, ...]
    conflict_notes: str = ""

    def __post_init__(self):
        if self.rule_path not in rule_paths() or type(self.rule_path) is not str:
            raise ValueError("unknown rule path")
        if type(self.status) is not VerificationStatus:
            raise ValueError("explicit verification status required")
        if (type(self.sources) is not tuple or any(type(s) is not RuleSource for s in self.sources)
                or len(set(self.sources)) != len(self.sources)):
            raise ValueError("unique immutable source observations required")
        _text(self.conflict_notes)

    def add_source(self, source: RuleSource):
        """Append evidence; never overwrite prior observations or auto-verify."""
        if type(source) is not RuleSource or source in self.sources:
            raise ValueError("new typed source observation required")
        combined = self.sources + (source,)
        status = (VerificationStatus.AMBIGUOUS if len({s.value_json for s in combined}) > 1 else self.status)
        return replace(self, sources=combined, status=status)


@dataclass(frozen=True)
class VerificationContext:
    as_of: date
    max_age_days: int = 30

    def __post_init__(self):
        _date(self.as_of)
        if type(self.max_age_days) is not int or self.max_age_days < 0:
            raise ValueError("explicit nonnegative freshness window required")


@dataclass(frozen=True)
class UnresolvedRule:
    rule_path: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class VerificationReport:
    origin: PlanOrigin
    context: VerificationContext
    required_rules: tuple[str, ...]
    unresolved: tuple[UnresolvedRule, ...]

    @property
    def usable(self):
        return not self.unresolved


class PlanVerificationError(ValueError):
    def __init__(self, report):
        self.report = report
        super().__init__("plan verification failed: " + ", ".join(r.rule_path for r in report.unresolved))


@dataclass(frozen=True)
class PlanConfiguration:
    plan: PropPlan
    origin: PlanOrigin
    rules: tuple[RuleProvenance, ...] = ()

    def __post_init__(self):
        if type(self.plan) is not PropPlan or type(self.origin) is not PlanOrigin:
            raise ValueError("validated plan and explicit origin required")
        if (type(self.rules) is not tuple or any(type(r) is not RuleProvenance for r in self.rules)
                or len({r.rule_path for r in self.rules}) != len(self.rules)):
            raise ValueError("one immutable provenance entry per rule; duplicates cannot overwrite")
        if self.origin is PlanOrigin.FICTIONAL and self.rules:
            raise ValueError("fictional examples must not masquerade as externally verified rules")

    def verify(self, context: VerificationContext) -> VerificationReport:
        if type(context) is not VerificationContext:
            raise ValueError("verification requires explicit as-of/freshness context")
        paths = rule_paths()
        unresolved = []
        if self.origin is PlanOrigin.EXTERNAL:
            entries, expected = {r.rule_path: r for r in self.rules}, rule_values(self.plan)
            for path in paths:
                reasons = []
                entry = entries.get(path)
                if entry is None:
                    reasons.append("MISSING_PROVENANCE")
                else:
                    if entry.status is not VerificationStatus.VERIFIED:
                        reasons.append(entry.status.value)
                    if not entry.sources:
                        reasons.append("MISSING_SOURCE")
                    if len({s.value_json for s in entry.sources}) > 1:
                        reasons.append("CONFLICTING_SOURCES")
                    if any(s.value_json != expected[path] for s in entry.sources):
                        reasons.append("VALUE_MISMATCH")
                    if any(s.observed_date > context.as_of for s in entry.sources):
                        reasons.append("FUTURE_OBSERVATION")
                    if any(s.effective_date is not None and s.effective_date > context.as_of for s in entry.sources):
                        reasons.append("NOT_YET_EFFECTIVE")
                    official = [s for s in entry.sources if s.source_type is SourceType.OFFICIAL]
                    if not official:
                        reasons.append("NO_OFFICIAL_SUPPORT")
                    elif not any(0 <= (context.as_of - s.observed_date).days <= context.max_age_days for s in official):
                        reasons.append("STALE_OFFICIAL_SUPPORT")
                if reasons:
                    unresolved.append(UnresolvedRule(path, tuple(reasons)))
        return VerificationReport(self.origin, context, paths, tuple(unresolved))

    def require_usable(self, context: VerificationContext) -> PropPlan:
        report = self.verify(context)
        if not report.usable:
            raise PlanVerificationError(report)
        return self.plan

    def to_json(self):
        def source(s):
            return dict(reference=s.reference, source_type=s.source_type.value,
                observed_date=s.observed_date.isoformat(),
                effective_date=s.effective_date.isoformat() if s.effective_date else None,
                value_json=s.value_json, notes=s.notes)
        rules = [dict(rule_path=r.rule_path, status=r.status.value, conflict_notes=r.conflict_notes,
                      sources=[source(s) for s in r.sources]) for r in sorted(self.rules, key=lambda r: r.rule_path)]
        return json.dumps(dict(version=1, plan=json.loads(self.plan.to_json()), origin=self.origin.value,
                               rules=rules), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate serialization key")
                result[key] = value
            return result
        def exact(obj, keys):
            if type(obj) is not dict or set(obj) != set(keys):
                raise ValueError("missing or unknown provenance fields")
        def parse_date(value):
            if type(value) is not str:
                raise ValueError("ISO date string required")
            result = date.fromisoformat(value)
            if result.isoformat() != value:
                raise ValueError("canonical ISO date required")
            return result
        try:
            data = json.loads(text, object_pairs_hook=unique)
            exact(data, ("version", "plan", "origin", "rules"))
            if type(data["version"]) is not int or data["version"] != 1 or type(data["rules"]) is not list:
                raise ValueError("unsupported configuration format")
            rules = []
            for entry in data["rules"]:
                exact(entry, ("rule_path", "status", "sources", "conflict_notes"))
                if type(entry["sources"]) is not list:
                    raise ValueError("source array required")
                sources = []
                for item in entry["sources"]:
                    exact(item, ("reference", "source_type", "observed_date", "effective_date", "value_json", "notes"))
                    sources.append(RuleSource(item["reference"], SourceType(item["source_type"]),
                        parse_date(item["observed_date"]), parse_date(item["effective_date"]) if item["effective_date"] is not None else None,
                        item["value_json"], item["notes"]))
                rules.append(RuleProvenance(entry["rule_path"], VerificationStatus(entry["status"]),
                                            tuple(sources), entry["conflict_notes"]))
            return cls(PropPlan.from_json(json.dumps(data["plan"])), PlanOrigin(data["origin"]), tuple(rules))
        except (TypeError, KeyError, ValueError, ArithmeticError):
            raise ValueError("invalid or ambiguous provenance serialization") from None
