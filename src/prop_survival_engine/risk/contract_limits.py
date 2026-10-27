"""Gross contract-limit arithmetic; no firm permission inferred from equivalence."""
from dataclasses import dataclass
from fractions import Fraction

from prop_survival_engine.core.instruments import (
    INSTRUMENTS, ContractClass, ContractExposure, ContractFamily, InstrumentSpec,
)


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("nonnegative integer contract limit required")


@dataclass(frozen=True)
class InstrumentContractLimit:
    instrument: str
    max_contracts: int

    def __post_init__(self):
        INSTRUMENTS.lookup(self.instrument)
        _count(self.max_contracts)


@dataclass(frozen=True)
class FamilyContractLimit:
    family: ContractFamily
    max_mini_equivalent: Fraction | None
    max_mini_contracts: int | None
    max_micro_contracts: int | None
    allow_mixed: bool

    def __post_init__(self):
        if type(self.family) is not ContractFamily or type(self.allow_mixed) is not bool:
            raise ValueError("explicit family and mixed-position policy required")
        if self.max_mini_equivalent is not None and (
                type(self.max_mini_equivalent) is not Fraction or self.max_mini_equivalent < 0):
            raise ValueError("nonnegative exact Fraction family limit required")
        for value in (self.max_mini_contracts, self.max_micro_contracts):
            if value is not None:
                _count(value)
        if all(v is None for v in (self.max_mini_equivalent, self.max_mini_contracts, self.max_micro_contracts)):
            raise ValueError("at least one explicit family limit required")


@dataclass(frozen=True)
class ContractLimits:
    instrument_limits: tuple[InstrumentContractLimit, ...]
    family_limits: tuple[FamilyContractLimit, ...]

    def __post_init__(self):
        for values, kind, key in ((self.instrument_limits, InstrumentContractLimit, "instrument"),
                                  (self.family_limits, FamilyContractLimit, "family")):
            if (type(values) is not tuple or any(type(v) is not kind for v in values)
                    or len({getattr(v, key) for v in values}) != len(values)):
                raise ValueError("unique immutable contract limits required")


@dataclass(frozen=True)
class ContractLimitResult:
    approved_contracts: int
    reason_codes: tuple[str, ...]


def constrain_contracts(spec: InstrumentSpec, risk_approved: int, limits: ContractLimits,
                        existing_exposure: tuple[ContractExposure, ...]) -> ContractLimitResult:
    """Caller supplies complete, gross, deduplicated outstanding quantities.

    No account inventory is fetched or netted. This is not an atomic reservation.
    """
    _count(risk_approved)
    if type(spec) is not InstrumentSpec or type(limits) is not ContractLimits:
        raise ValueError("typed instrument and contract limits required")
    if type(existing_exposure) is not tuple or any(type(e) is not ContractExposure for e in existing_exposure):
        raise ValueError("explicit immutable gross exposure required")
    # Prevent foreign/non-reviewed specifications from evading quantity bounds.
    if spec != INSTRUMENTS.lookup(spec.canonical_symbol) or any(
            e.instrument != INSTRUMENTS.lookup(e.instrument.canonical_symbol) for e in existing_exposure):
        raise ValueError("contract limits require the reviewed v0.17 catalog")
    caps, reasons = [], []
    same_symbol = sum(e.quantity for e in existing_exposure if e.instrument.canonical_symbol == spec.canonical_symbol)
    for limit in limits.instrument_limits:
        if limit.instrument == spec.canonical_symbol:
            caps.append((max(0, limit.max_contracts - same_symbol), "INSTRUMENT_LIMIT"))
    family = [e for e in existing_exposure if e.instrument.family is spec.family]
    opposite = any(e.quantity > 0 and e.instrument.contract_class is not spec.contract_class for e in family)
    policy = next((v for v in limits.family_limits if v.family is spec.family), None)
    if opposite and (policy is None or not policy.allow_mixed):
        caps.append((0, "MIXED_PERMISSION_UNSPECIFIED" if policy is None else "MIXED_NOT_PERMITTED"))
    if policy is not None:
        if policy.max_mini_equivalent is not None:
            remaining = max(Fraction(0), policy.max_mini_equivalent - sum((e.mini_equivalent for e in family), Fraction(0)))
            caps.append((remaining // spec.equivalence_weight, "FAMILY_EQUIVALENT_LIMIT"))
        class_cap = policy.max_mini_contracts if spec.contract_class is ContractClass.MINI else policy.max_micro_contracts
        if class_cap is not None:
            used = sum(e.quantity for e in family if e.instrument.contract_class is spec.contract_class)
            caps.append((max(0, class_cap - used), "CONTRACT_CLASS_LIMIT"))
    quantity = min([risk_approved] + [cap for cap, _ in caps])
    reasons = [reason for cap, reason in caps if cap < risk_approved]
    return ContractLimitResult(quantity, tuple(reasons))
