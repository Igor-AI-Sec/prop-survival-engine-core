"""Reviewed v0.17 USD futures mechanics; no venue, broker or expiry identifiers."""
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from fractions import Fraction


class ContractFamily(str, Enum):
    SP500 = "S&P 500"
    NASDAQ100 = "Nasdaq-100"


class ContractClass(str, Enum):
    MINI = "MINI"
    MICRO = "MICRO"


@dataclass(frozen=True)
class InstrumentSpec:
    canonical_symbol: str
    family: ContractFamily
    contract_class: ContractClass
    tick_size: Decimal
    tick_value: Decimal
    point_value: Decimal
    equivalence_weight: Fraction

    def __post_init__(self):
        if (type(self.canonical_symbol) is not str or not self.canonical_symbol
                or not self.canonical_symbol.isascii() or not self.canonical_symbol.isalpha()
                or self.canonical_symbol != self.canonical_symbol.upper()):
            raise ValueError("explicit canonical root symbol required")
        if type(self.family) is not ContractFamily or type(self.contract_class) is not ContractClass:
            raise ValueError("explicit family and contract class required")
        for value in (self.tick_size, self.tick_value, self.point_value):
            if type(value) is not Decimal or not value.is_finite() or value <= 0:
                raise ValueError("tick/point mechanics must be positive finite Decimal")
        if Fraction(self.tick_size) * Fraction(self.point_value) != Fraction(self.tick_value):
            raise ValueError("tick size times point value must equal tick value exactly")
        expected = Fraction(1) if self.contract_class is ContractClass.MINI else Fraction(1, 10)
        if type(self.equivalence_weight) is not Fraction or self.equivalence_weight != expected:
            raise ValueError("reviewed mini/micro weights must be exactly 1 and 1/10")


@dataclass(frozen=True)
class InstrumentCatalog:
    instruments: tuple[InstrumentSpec, ...]

    def __post_init__(self):
        if (type(self.instruments) is not tuple or not self.instruments
                or any(type(s) is not InstrumentSpec for s in self.instruments)
                or len({s.canonical_symbol for s in self.instruments}) != len(self.instruments)):
            raise ValueError("unique immutable instrument specifications required")
        for family in {s.family for s in self.instruments}:
            members = [s for s in self.instruments if s.family is family]
            if len(members) != 2 or {s.contract_class for s in members} != set(ContractClass):
                raise ValueError("each reviewed family requires one mini and one micro")
            mini = next(s for s in members if s.contract_class is ContractClass.MINI)
            micro = next(s for s in members if s.contract_class is ContractClass.MICRO)
            if (mini.tick_size != micro.tick_size or Fraction(mini.point_value) != 10 * Fraction(micro.point_value)
                    or Fraction(mini.tick_value) != 10 * Fraction(micro.tick_value)):
                raise ValueError("incompatible mini/micro family monetary equivalence")

    def lookup(self, symbol: str) -> InstrumentSpec:
        if type(symbol) is not str:
            raise ValueError("canonical symbol required")
        for spec in self.instruments:
            if spec.canonical_symbol == symbol:
                return spec
        raise ValueError("instrument is not in the reviewed catalog")


INSTRUMENTS = InstrumentCatalog((
    InstrumentSpec("ES", ContractFamily.SP500, ContractClass.MINI,
                   Decimal("0.25"), Decimal("12.50"), Decimal("50"), Fraction(1)),
    InstrumentSpec("MES", ContractFamily.SP500, ContractClass.MICRO,
                   Decimal("0.25"), Decimal("1.25"), Decimal("5"), Fraction(1, 10)),
    InstrumentSpec("NQ", ContractFamily.NASDAQ100, ContractClass.MINI,
                   Decimal("0.25"), Decimal("5.00"), Decimal("20"), Fraction(1)),
    InstrumentSpec("MNQ", ContractFamily.NASDAQ100, ContractClass.MICRO,
                   Decimal("0.25"), Decimal("0.50"), Decimal("2"), Fraction(1, 10)),
))


@dataclass(frozen=True)
class ContractExposure:
    instrument: InstrumentSpec
    quantity: int

    def __post_init__(self):
        if type(self.instrument) is not InstrumentSpec or type(self.quantity) is not int or self.quantity < 0:
            raise ValueError("typed instrument and nonnegative gross integer quantity required")

    @property
    def mini_equivalent(self) -> Fraction:
        return self.quantity * self.instrument.equivalence_weight


@dataclass(frozen=True)
class FamilyExposure:
    family: ContractFamily
    mini_equivalent: Fraction


def family_equivalent_exposure(exposures: tuple[ContractExposure, ...]) -> tuple[FamilyExposure, ...]:
    """Gross equivalence; no directional netting, hedging or mixed-use permission."""
    if type(exposures) is not tuple or any(type(e) is not ContractExposure for e in exposures):
        raise ValueError("immutable gross exposure tuple required")
    families = {e.instrument.family for e in exposures}
    return tuple(FamilyExposure(f, sum((e.mini_equivalent for e in exposures if e.instrument.family is f), Fraction(0)))
                 for f in sorted(families, key=lambda f: f.value))
