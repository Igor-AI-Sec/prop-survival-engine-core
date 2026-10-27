"""Reproducible offline benchmark; no SDK, credentials, network or Orders."""

from decimal import Decimal
from time import perf_counter
import json

from prop_survival_engine.prop_rules.engine import StatefulPropRuleEngine
from prop_survival_engine.prop_rules.models import PropRuleConfig
from prop_survival_engine.risk import ContractLossEstimate, RiskPolicyConfig
from prop_survival_engine.simulation.outcomes import IIDOutcomeSource, TradeOutcome
from prop_survival_engine.simulation.sizing import FixedContracts
from prop_survival_engine.simulation.reach_avoid import PathConfig, Scenario, ReachAvoidEvaluator


def main():
    rules = PropRuleConfig(Decimal("1000"), Decimal("250"), Decimal("150"), Decimal("100"))
    evaluator = ReachAvoidEvaluator(lambda: StatefulPropRuleEngine(rules), RiskPolicyConfig(3),
        ContractLossEstimate(Decimal("20"), Decimal("1"), Decimal("1")), PathConfig(10, 5))
    source = IIDOutcomeSource(tuple(TradeOutcome(Decimal(value)) for value in ("25", "-15", "-180")), (5, 3, 1))
    started = perf_counter()
    report = evaluator.evaluate(FixedContracts("fixed-1", 1), Scenario("base", source), simulations=1000, seed=20261006)
    elapsed = perf_counter() - started
    print(json.dumps({
        "simulations": report.simulations, "seed": report.seed, "elapsed_seconds": round(elapsed, 3),
        "passes": report.passes, "breaches": report.breaches, "censored": report.censored,
        "trades": sum(p.trades for p in report.paths), "pass_probability": report.pass_probability,
        "eventual_pass_probability_bounds": report.eventual_pass_probability_bounds,
        "confidence_interval": report.confidence_interval, "lower_confidence_bound": report.lower_confidence_bound,
        "mean_trades_to_absorption": report.mean_trades_to_absorption,
        "median_trades_to_absorption": report.median_trades_to_absorption,
        "average_contracts": report.average_contracts, "maximum_contracts": report.maximum_contracts,
        "pass_times": {key: getattr(report.pass_times, key) for key in
                       ("count", "minimum", "median", "p25", "p75", "p90", "maximum")} if report.pass_times else None,
        "breach_causes": {cause.value: count for cause, count in report.breach_cause_counts},
    }, indent=2))


if __name__ == "__main__":
    main()
