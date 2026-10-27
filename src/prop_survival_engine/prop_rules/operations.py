"""Fixed operation dependencies for complete v0.16A specifications.

No Monte Carlo methodology changes and no provider execution implementation.
"""
from prop_survival_engine.prop_rules.provenance import (
    PlanConfiguration, PlanVerificationError, VerificationReport, rule_paths,
)


def verify_challenge(configuration, context):
    """Fees/payout evidence is irrelevant to target-before-breach account paths."""
    if type(configuration) is not PlanConfiguration:
        raise ValueError("typed configuration required")
    report = configuration.verify(context)
    required = tuple(p for p in rule_paths() if not p.startswith(("fees.", "payouts.")))
    return VerificationReport(report.origin, report.context, required,
                              tuple(r for r in report.unresolved if r.rule_path in required))


def challenge_evaluator(configuration, context, safety_config, estimate, path_config=None):
    from prop_survival_engine.prop_rules.plan_engine import plan_evaluator
    report = verify_challenge(configuration, context)
    if not report.usable:
        raise PlanVerificationError(report)
    # The complete, caller-supplied plan is retained. No placeholder fees/payouts
    # are synthesized to coerce incomplete real records into PropPlan.
    return plan_evaluator(configuration.plan, safety_config, estimate, path_config)


def funded_payout_simulation(*args, **kwargs):
    raise NotImplementedError("funded payout Monte Carlo is not implemented or qualified")


def reviewed_challenge_semantics_report(reviewed, context, *, binding=None, timeline=None):
    """Separate representational support from source proof and complete binding.

    The stored catalog/legacy reports are immutable evidence checkpoints. This
    derived operation report never parses claims or upgrades source status. A
    supported primitive without a verified executable binding remains blocked.
    """
    from prop_survival_engine.prop_rules.catalog import ReviewedPlan, LayerReport, Operation
    from prop_survival_engine.prop_rules.plans import DailyLossAction, ConsistencyMode, DrawdownType, PlanStage
    from prop_survival_engine.prop_rules.calendar import SimulationTimeline
    if type(reviewed) is not ReviewedPlan:
        raise ValueError("reviewed plan required")
    original = reviewed.qualify(Operation.CHALLENGE, context)
    supported = {"capped_trailing_floor", "soft_daily_pause", "target_extending_consistency",
                 "calendar_news_constraints", "auto_close_news_constraints", "realized_closed_position_target"}
    verified = False
    if binding is not None:
        if type(binding) is not PlanConfiguration:
            raise ValueError("complete evidenced plan binding required")
        from prop_survival_engine.prop_rules.provenance import PlanOrigin
        verified = (binding.origin is PlanOrigin.EXTERNAL and verify_challenge(binding, context).usable
                    and binding.plan.stage is PlanStage.EVALUATION)
    def representable(marker):
        if not verified:
            return False
        p = binding.plan
        return {
            "capped_trailing_floor": p.trailing_floor_cap is not None and p.drawdown_type is not DrawdownType.STATIC,
            "soft_daily_pause": p.daily_loss_action is DailyLossAction.SOFT_PAUSE,
            "target_extending_consistency": p.consistency_mode is ConsistencyMode.EXTEND_TARGET,
            "calendar_news_constraints": p.calendar_json is not None and type(timeline) is SimulationTimeline,
            "auto_close_news_constraints": p.calendar_json is not None and type(timeline) is SimulationTimeline,
            "realized_closed_position_target": not p.target_uses_unrealized and p.target_requires_flat,
        }.get(marker, False)
    blockers = []
    for key, reasons in original.blockers:
        if reasons != ("MODEL_UNSUPPORTED",) or key not in supported:
            blockers.append((key, reasons))  # Evidence stays byte-for-byte equivalent.
        elif not representable(key):
            blockers.append((key, ("VERIFIED_SEMANTIC_BINDING_REQUIRED",)))
    if binding is None:
        blockers.append(("plan_binding", ("COMPLETE_AUDITED_PLAN_BINDING_REQUIRED",)))
    elif not verified:
        blockers.append(("plan_binding", ("CHALLENGE_BINDING_NOT_VERIFIED",)))
    # Binding is supplied reviewer evidence, not an automatic equivalence proof
    # between free-form catalog prose and numeric domain leaves.
    return LayerReport(Operation.CHALLENGE, tuple(sorted(blockers)))
