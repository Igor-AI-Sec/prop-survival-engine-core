"""Injectable gross per-contract PnL streams, never price/alpha providers."""

from dataclasses import dataclass
from decimal import Decimal
from random import Random
from typing import Protocol

from prop_survival_engine.risk.money import finite_decimal
from prop_survival_engine.simulation.seeds import PathSeedContext


@dataclass(frozen=True)
class TradeOutcome:
    realized_pnl: Decimal
    unrealized_marks: tuple[Decimal, ...] = ()
    no_trade: bool = False

    def __post_init__(self):
        if (not finite_decimal(self.realized_pnl) or not isinstance(self.unrealized_marks, tuple)
                or any(not finite_decimal(mark) for mark in self.unrealized_marks)):
            raise ValueError("trade PnL and marks must be finite Decimal values")
        if type(self.no_trade) is not bool or (self.no_trade and (self.realized_pnl != 0 or self.unrealized_marks)):
            raise ValueError("no-trade outcomes must explicitly have zero PnL and no marks")


class OutcomeStream(Protocol):
    def next_outcome(self) -> TradeOutcome | None: ...


class OutcomeSource(Protocol):
    # Future block bootstrap owns block/history cursors inside this fresh stream.
    def start_path(self, seed: int) -> OutcomeStream: ...


class OutcomeSourceFactory(Protocol):
    def __call__(self, context: PathSeedContext) -> OutcomeSource: ...


def _validate(outcomes):
    if (not isinstance(outcomes, tuple) or not outcomes
            or any(not isinstance(value, TradeOutcome) for value in outcomes)):
        raise ValueError("source requires a nonempty immutable tuple of outcomes")


@dataclass(frozen=True)
class SequenceOutcomeSource:
    outcomes: tuple[TradeOutcome, ...]
    repeat: bool = False

    def __post_init__(self):
        _validate(self.outcomes)
        if type(self.repeat) is not bool:
            raise ValueError("repeat must be bool")

    def start_path(self, seed: int) -> OutcomeStream:
        return _SequenceStream(self.outcomes, self.repeat)

    def __call__(self, context: PathSeedContext) -> OutcomeSource:
        """Immutable configuration doubles as a fresh-source factory."""
        return SequenceOutcomeSource(self.outcomes, self.repeat)


class _SequenceStream:
    def __init__(self, outcomes, repeat):
        self.outcomes, self.repeat, self.index = outcomes, repeat, 0

    def next_outcome(self):
        if self.index >= len(self.outcomes) and not self.repeat:
            return None
        value = self.outcomes[self.index % len(self.outcomes)]
        self.index += 1
        return value


@dataclass(frozen=True)
class IIDOutcomeSource:
    """Temporary seeded discrete IID model: empirical atoms or synthetic fixtures.

    Integer weights select atoms exactly. This models neither market dependence
    nor production-quality strategy returns. Every path owns its random stream.
    """
    outcomes: tuple[TradeOutcome, ...]
    weights: tuple[int, ...] | None = None

    def __post_init__(self):
        _validate(self.outcomes)
        if self.weights is not None and (
                not isinstance(self.weights, tuple) or len(self.weights) != len(self.outcomes)
                or any(type(weight) is not int or weight <= 0 for weight in self.weights)):
            raise ValueError("weights must be positive integers, one per outcome")

    def start_path(self, seed: int) -> OutcomeStream:
        return _IIDStream(self.outcomes, self.weights or (1,) * len(self.outcomes), seed)

    def __call__(self, context: PathSeedContext) -> OutcomeSource:
        return IIDOutcomeSource(self.outcomes, self.weights)


class _IIDStream:
    def __init__(self, outcomes, weights, seed):
        self.outcomes, self.weights, self.random = outcomes, weights, Random(seed)

    def next_outcome(self):
        draw = self.random.randrange(sum(self.weights))
        for outcome, weight in zip(self.outcomes, self.weights):
            if draw < weight:
                return outcome
            draw -= weight
        raise AssertionError("unreachable weighted draw")
