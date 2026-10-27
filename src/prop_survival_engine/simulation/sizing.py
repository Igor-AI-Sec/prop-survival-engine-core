"""Low-dimensional monotone requests; v0.9 RiskPolicy still approves every entry."""

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Protocol

from prop_survival_engine.risk.features import RuinDistances
from prop_survival_engine.risk.money import finite_decimal


class SizingCandidate(Protocol):
    name: str
    def requested_contracts(self, distances: RuinDistances, per_contract_loss: Decimal) -> int: ...


def _name(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("policy name must be nonempty")


@dataclass(frozen=True)
class FixedContracts:
    name: str
    contracts: int

    def __post_init__(self):
        _name(self.name)
        if type(self.contracts) is not int or self.contracts <= 0:
            raise ValueError("fixed contracts must be a positive integer")

    def requested_contracts(self, distances, per_contract_loss):
        return self.contracts


@dataclass(frozen=True)
class FractionOfCushion:
    name: str
    fraction: Decimal
    max_requested_contracts: int

    def __post_init__(self):
        _name(self.name)
        if not finite_decimal(self.fraction) or not Decimal("0") < self.fraction <= Decimal("1"):
            raise ValueError("request fraction must be a finite Decimal in (0, 1]")
        if type(self.max_requested_contracts) is not int or self.max_requested_contracts <= 0:
            raise ValueError("request cap must be a positive integer")

    def requested_contracts(self, distances, per_contract_loss):
        distance = distances.effective_ruin_distance
        if distance is None or distance <= 0:
            return 0
        return min(self.max_requested_contracts,
                   Fraction(self.fraction) * Fraction(distance) // Fraction(per_contract_loss))


@dataclass(frozen=True)
class CushionLadder:
    """Ascending (minimum cushion, requested contracts); below first band abstains."""
    name: str
    bands: tuple[tuple[Decimal, int], ...]

    def __post_init__(self):
        _name(self.name)
        if not isinstance(self.bands, tuple) or not self.bands:
            raise ValueError("ladder requires immutable nonempty bands")
        prior_floor, prior_size = Decimal("-1"), 0
        for band in self.bands:
            if not isinstance(band, tuple) or len(band) != 2:
                raise ValueError("each ladder band must be an immutable pair")
            floor, size = band
            if (not finite_decimal(floor) or floor < 0 or floor <= prior_floor
                    or type(size) is not int or size < prior_size or size <= 0):
                raise ValueError("bands require strictly increasing cushions and nondecreasing positive sizes")
            prior_floor, prior_size = floor, size

    def requested_contracts(self, distances, per_contract_loss):
        distance = distances.effective_ruin_distance
        if distance is None or distance <= 0:
            return 0
        requested = 0
        for floor, size in self.bands:
            if distance < floor:
                break
            requested = size
        return requested
