"""Exact finite-Decimal arithmetic, independent of ambient Decimal context.

Fraction is used internally for ratios and integer bounds. Public monetary
values remain Decimal; there is no float conversion or probability model.
"""

from decimal import Decimal
from fractions import Fraction


def finite_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def decimal_from_fraction(value: Fraction) -> Decimal:
    """Convert a terminating base-ten fraction exactly without context rounding."""
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise ValueError("monetary result must be a terminating decimal")
    scale = max(twos, fives)
    coefficient = abs(value.numerator) * 2 ** (scale - twos) * 5 ** (scale - fives)
    return Decimal((int(value < 0), tuple(int(digit) for digit in str(coefficient)), -scale))


def subtract(left: Decimal, right: Decimal) -> Decimal:
    return decimal_from_fraction(Fraction(left) - Fraction(right))
