"""Explicit plan policies behind the existing PropRuleEngine protocol.

No provider calls. This is account-rule evaluation, not an execution/payout API.
"""
from dataclasses import dataclass, replace
from decimal import Context, Decimal, Inexact, localcontext
from fractions import Fraction

from prop_survival_engine.prop_rules.engine import StatefulPropRuleEngine
from prop_survival_engine.prop_rules.models import (
    AccountStatus, DailyLossBreached, EndOfDaySettled, MarkToMarketUpdated,
    PnlSettled, PropRuleConfig, RealizedPnlUpdated, SessionStarted,
    TargetReached, TrailingDrawdownBreached,
    ClockAdvanced, PositionInventoryUpdated, DailyLossPaused, TradingRestrictionBreached,
)
from prop_survival_engine.prop_rules.plans import (
    DrawdownType, PayoutFloorEffect, PlanStage, PropPlan, money,
    DailyLossAction, ConsistencyMode,
)
from prop_survival_engine.risk.money import decimal_from_fraction
from prop_survival_engine.prop_rules.provenance import PlanConfiguration, VerificationContext
from prop_survival_engine.prop_rules.calendar import TradingCalendar, SimulationTimeline, utc
from prop_survival_engine.risk.features import RiskContext, ActiveLossFloors, SessionState
from prop_survival_engine.oms.models import OmsState


def add(*values):
    return decimal_from_fraction(sum((Fraction(v) for v in values), Fraction(0)))


@dataclass(frozen=True)
class PayoutAssessment:
    eligible: bool
    maximum_gross_withdrawal: Decimal
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PayoutResult:
    gross_withdrawal: Decimal
    trader_payment: Decimal
    retained_split: Decimal
    equity_after: Decimal
    trailing_floor_after: Decimal


class PlanRuleEngine(StatefulPropRuleEngine):
    """Reuses the account state machine with explicit neutral policy overrides.

    Trading days are distinct *completed* sessions containing a PnlSettled event
    (including flat results), or a nonzero realized update. Best-day consistency
    uses positive net realized daily PnL / cumulative net realized trading profit.
    Withdrawals are excluded from trading profit; commissions are included.
    """
    def __init__(self, plan: PropPlan | PlanConfiguration, *, verification_context: VerificationContext | None = None,
                 timeline: SimulationTimeline | None = None, initial_open_positions: int | None = None):
        configuration = plan if type(plan) is PlanConfiguration else None
        if configuration is not None:
            plan = configuration.require_usable(verification_context)
        if type(plan) is not PropPlan:
            raise ValueError("validated PropPlan required")
        self._plan = plan
        self._configuration = configuration
        if timeline is not None and type(timeline) is not SimulationTimeline:
            raise ValueError("typed deterministic timeline required")
        if initial_open_positions is not None and (type(initial_open_positions) is not int or initial_open_positions < 0):
            raise ValueError("explicit nonnegative position inventory required")
        self.timeline = timeline
        self.calendar = TradingCalendar.from_json(plan.calendar_json) if plan.calendar_json is not None else None
        self.clock = None
        self.paused = False
        self.open_positions = initial_open_positions if initial_open_positions is not None else (None if plan.target_requires_flat else 0)
        self.best_qualifying_day = Decimal(0)
        self.required_profit = Fraction(plan.profit_target_gain)
        self.used_calendar_sessions = set()
        self.holding_restrictions = ()
        self.holding_breach = False
        self.completed_sessions = set()
        self.trading_days = 0
        self.best_day = Decimal(0)
        self.withdrawals = Decimal(0)
        self.day_start_profit = Decimal(0)
        self.day_active = False
        self.intraday_high_watermark = plan.starting_balance
        with localcontext(Context(prec=128)) as ctx:
            ctx.traps[Inexact] = True
            super().__init__(PropRuleConfig(plan.starting_balance, plan.maximum_drawdown,
                plan.daily_loss_limit or plan.maximum_drawdown, plan.profit_target_gain))

    @property
    def plan(self):
        return self._plan

    @property
    def base_target(self):
        return add(self.plan.starting_balance, self.plan.profit_target_gain)

    @property
    def effective_target(self):
        return Fraction(self.plan.starting_balance) + self.required_profit

    @property
    def target_extension(self):
        return self.required_profit - Fraction(self.plan.profit_target_gain)

    def eligibility(self):
        from prop_survival_engine.prop_rules.calendar import CalendarEligibility
        if self.calendar is None:
            return CalendarEligibility(self.state.status is AccountStatus.ALIVE and not self.paused,
                                       False, False, ("SOFT_DLL_PAUSED",) if self.paused else ())
        value = self.calendar.eligibility(self.clock)
        return replace(value, can_open=value.can_open and not self.paused and not self.holding_restrictions and self.state.status is AccountStatus.ALIVE,
                       must_flatten=value.must_flatten or bool(self.holding_restrictions),
                       breaches_if_holding=value.breaches_if_holding or self.holding_breach,
                       reasons=value.reasons + self.holding_restrictions + (("SOFT_DLL_PAUSED",) if self.paused else ()))

    def risk_context(self):
        session = (SessionState.PAUSED if self.paused else SessionState.OPEN if self.eligibility().can_open else SessionState.CLOSED)
        if self.plan.target_requires_flat and self.open_positions is None:
            session = SessionState.UNKNOWN
        return RiskContext(self.state, OmsState.READY, session,
            ActiveLossFloors(daily=self.plan.daily_loss_limit is not None and self.plan.daily_loss_action is DailyLossAction.HARD_BREACH,
                             trailing=True))

    def advance_opportunity(self, session, opportunity):
        if self.calendar is not None:
            if self.timeline is None:
                raise ValueError("calendar simulation requires explicit opportunity times")
            return ClockAdvanced(self.timeline.opportunities[session][opportunity])
        return None

    def before_session(self, session):
        return self.advance_opportunity(session, 0)

    def before_trade(self):
        if not self.eligibility().can_open:
            raise ValueError("rule engine does not admit trading now")
        if self.plan.target_requires_flat or self.calendar is not None:
            if self.open_positions != 0 or self.state.unrealized_pnl != 0:
                raise ValueError("sequential simulation requires proven flat inventory")
            return PositionInventoryUpdated(1)
        return None

    def after_trade(self):
        if self.plan.target_requires_flat or self.calendar is not None:
            return PositionInventoryUpdated(0)
        return None

    def _cap_floor(self, floor):
        return min(floor, self.plan.trailing_floor_cap) if self.plan.trailing_floor_cap is not None else floor

    def _calculate_daily_floor(self, day_start_equity):
        # AccountState/RiskPolicy require finite floors. Disabled DLL mirrors the
        # active drawdown boundary, adding no extra loss constraint.
        if self.plan.daily_loss_limit is None:
            return (self._state.trailing_floor if hasattr(self, "_state")
                    else add(day_start_equity, -self.plan.maximum_drawdown))
        return add(day_start_equity, -self.plan.daily_loss_limit)

    def _basis(self, state):
        return state.equity if self.plan.unrealized_moves_floor else add(state.starting_balance, state.realized_pnl)

    def _settle_eod_trailing(self, state):
        high = max(state.eod_high_watermark, self._basis(state))
        if self.plan.drawdown_type is DrawdownType.EOD_TRAILING:
            floor = max(state.trailing_floor, self._cap_floor(add(high, -self.plan.maximum_drawdown)))
        else:
            floor = state.trailing_floor
        return high, floor

    def _is_daily_loss_breached(self, equity, floor):
        return (self.plan.daily_loss_limit is not None and self.plan.daily_loss_action is DailyLossAction.HARD_BREACH
                and self._loss_crossed(equity, floor))

    def _loss_crossed(self, equity, floor):
        return equity <= floor if self.plan.breach_on_touch else equity < floor

    def _is_trailing_drawdown_breached(self, equity, floor):
        return self._loss_crossed(equity, floor)

    def _profit(self):
        return add(self.state.realized_pnl, self.withdrawals)

    def _consistent(self):
        limit = self.plan.consistency_fraction
        if limit is None:
            return True
        total = self._profit()
        best = max(self.best_day, add(total, -self.day_start_profit))
        return total > 0 and Fraction(best) <= Fraction(limit) * Fraction(total)

    def _evaluate_thresholds(self):
        state = self.state
        if state.status is not AccountStatus.ALIVE:
            return []
        if self.plan.drawdown_type is DrawdownType.INTRADAY_TRAILING:
            self.intraday_high_watermark = max(self.intraday_high_watermark, self._basis(state))
            state = replace(state, trailing_floor=max(state.trailing_floor,
                self._cap_floor(add(self.intraday_high_watermark, -self.plan.maximum_drawdown))))
        if self.plan.daily_loss_limit is None:
            state = replace(state, daily_floor=state.trailing_floor)
        self._state = state
        signals = []
        if self._is_daily_loss_breached(state.equity, state.daily_floor):
            signals.append(DailyLossBreached(state.equity, state.daily_floor))
        if self._is_trailing_drawdown_breached(state.equity, state.trailing_floor):
            signals.append(TrailingDrawdownBreached(state.equity, state.trailing_floor))
        eligibility = self.eligibility()
        if eligibility.breaches_if_holding and self.open_positions is not None and self.open_positions > 0:
            signals.append(TradingRestrictionBreached(eligibility.reasons))
        if signals:
            self._state = replace(state, status=AccountStatus.BREACHED)
            return signals
        if (self.plan.daily_loss_action is DailyLossAction.SOFT_PAUSE and not self.paused
                and self._loss_crossed(state.equity, state.daily_floor)):
            self.paused = True
            signals.append(DailyLossPaused(state.equity, state.daily_floor))
        if self.plan.consistency_mode is ConsistencyMode.EXTEND_TARGET:
            day = max(Decimal(0), self.best_day, add(self._profit(), -self.day_start_profit))
            self.best_qualifying_day = max(self.best_qualifying_day, day)
            self.required_profit = max(self.required_profit,
                Fraction(self.best_qualifying_day) / Fraction(self.plan.consistency_fraction))
            target = self.effective_target
            try:
                target = decimal_from_fraction(target)
            except ValueError:
                pass  # Explicit exact rational target, never rounded to money.
            state = replace(state, target_equity=target)
            self._state = state
        target_basis = state.equity if self.plan.target_uses_unrealized else add(state.starting_balance, state.realized_pnl)
        reached = target_basis >= state.target_equity if self.plan.target_on_touch else target_basis > state.target_equity
        if (self.plan.stage is PlanStage.EVALUATION and reached
                and self.trading_days >= self.plan.minimum_trading_days and self._consistent()
                and (not self.plan.target_requires_flat or (self.open_positions == 0 and state.unrealized_pnl == 0))):
            self._state = replace(state, status=AccountStatus.PASSED)
            return [TargetReached(target_basis, state.target_equity)]
        return signals

    def apply(self, event):
        # Exact Decimal updates through the reused engine, independent of ambient
        # context. Unsupported precision fails closed and rolls back bookkeeping.
        before = {key: value.copy() if isinstance(value, (list, set)) else value
                  for key, value in self.__dict__.items()}
        try:
            with localcontext(Context(prec=128)) as ctx:
                ctx.traps[Inexact] = True
                return self._apply(event)
        except Exception:
            self.__dict__.clear()
            self.__dict__.update(before)
            raise

    def _apply(self, event):
        allowed = (SessionStarted, MarkToMarketUpdated, RealizedPnlUpdated, PnlSettled, EndOfDaySettled,
                   ClockAdvanced, PositionInventoryUpdated)
        if type(event) not in allowed:
            raise ValueError("plan engine accepts input events, not caller-assigned terminal signals")
        for name in ("realized_pnl", "unrealized_pnl"):
            if hasattr(event, name):
                value = getattr(event, name)
                if type(value) is not Decimal or not value.is_finite():
                    raise ValueError("PnL must be finite Decimal")
        if self.state.status is not AccountStatus.ALIVE:
            return ()  # terminal: no financial or session mutation
        if type(event) in (ClockAdvanced, PositionInventoryUpdated):
            if type(event) is ClockAdvanced:
                utc(event.at)
                if self.clock is not None and event.at < self.clock:
                    raise ValueError("authoritative injected clock cannot move backward")
                if self.calendar is not None and self.clock is not None and self.open_positions is not None and self.open_positions > 0:
                    from prop_survival_engine.prop_rules.calendar import HoldingAction
                    reasons, breach = list(self.holding_restrictions), self.holding_breach
                    for w in self.calendar.sessions:
                        deadline = w.forced_flat_at if w.forced_flat_at is not None else (w.end if self.calendar.no_overnight else None)
                        if deadline is not None and self.clock < deadline <= event.at:
                            reasons.append("CROSSED_FLAT_DEADLINE")
                            breach |= self.calendar.outside_session_action is HoldingAction.BREACH
                    for w in self.calendar.blackouts:
                        if self.clock < w.end and event.at >= w.start and w.holding_action is not HoldingAction.BLOCK_NEW:
                            reasons.append("CROSSED_BLACKOUT:" + w.name)
                            breach |= w.holding_action is HoldingAction.BREACH
                    self.holding_restrictions = tuple(sorted(set(reasons)))
                    self.holding_breach = breach
                self.clock = event.at
            else:
                if type(event.open_positions) is not int or event.open_positions < 0:
                    raise ValueError("explicit nonnegative position inventory required")
                self.open_positions = event.open_positions
                if self.open_positions == 0:
                    self.holding_restrictions, self.holding_breach = (), False
                elif self.calendar is not None:
                    restriction = self.calendar.eligibility(self.clock)
                    if restriction.must_flatten:
                        self.holding_restrictions = tuple(sorted(set(self.holding_restrictions + restriction.reasons)))
                        self.holding_breach |= restriction.breaches_if_holding
            self._event_history.append(event)
            signals = tuple(self._evaluate_thresholds())
            self._signal_history.extend(signals)
            return signals
        if isinstance(event, SessionStarted):
            if not event.session_id or type(event.session_id) is not str:
                raise ValueError("explicit session ID required")
            if event.session_id == self.state.session_id:
                return ()
            if event.session_id in self.completed_sessions:
                raise ValueError("cannot reuse a completed session")
            if self.state.session_id != self.INITIAL_SESSION_ID and self.state.session_id not in self.completed_sessions:
                raise ValueError("settle prior session before opening another")
            self.day_start_profit = self._profit()
            self.day_active = False
            if self.calendar is not None and self.calendar.no_overnight and self.open_positions != 0:
                raise ValueError("no-overnight boundary requires proven flat inventory")
            if self.calendar is not None:
                window = next((w for w in self.calendar.sessions if self.clock is not None and w.start <= self.clock < w.end), None)
                if window is None or window.start in self.used_calendar_sessions:
                    raise ValueError("session boundary needs a fresh explicit calendar window")
                self.used_calendar_sessions.add(window.start)
            self.paused = False
        elif isinstance(event, EndOfDaySettled):
            if event.session_id != self.state.session_id:
                raise ValueError("settlement must match active session")
            if event.session_id in self.completed_sessions:
                return ()
            self.completed_sessions.add(event.session_id)
            if self.day_active:
                self.trading_days += 1
                self.best_day = max(self.best_day, add(self._profit(), -self.day_start_profit))
        elif self.state.session_id in self.completed_sessions:
            raise ValueError("new session required before further PnL updates")
        elif isinstance(event, PnlSettled):
            self.day_active = True
        elif isinstance(event, RealizedPnlUpdated) and event.realized_pnl != self.state.realized_pnl:
            self.day_active = True
        return super().apply(event)

    def assess_payout(self) -> PayoutAssessment:
        rules, state = self.plan.payouts, self.state
        reasons = []
        if not rules.enabled or self.plan.stage is not PlanStage.FUNDED:
            reasons.append("PAYOUT_DISABLED")
        if state.status is not AccountStatus.ALIVE:
            reasons.append("TERMINAL_ACCOUNT")
        if state.unrealized_pnl != 0:
            reasons.append("OPEN_UNREALIZED_PNL")
        if self.trading_days < rules.minimum_trading_days:
            reasons.append("MINIMUM_DAYS")
        available_profit = state.realized_pnl  # after all prior gross withdrawals
        if available_profit < rules.minimum_profit or not self._consistent():
            reasons.append("PROFIT_OR_CONSISTENCY")
        # safety_net is retained account profit above starting balance. Caps and
        # withdrawals are gross; profit split is applied to trader cash afterwards.
        cap = max(Decimal(0), add(available_profit, -rules.safety_net))
        if rules.per_payout_cap is not None:
            cap = min(cap, rules.per_payout_cap)
        if rules.lifetime_cap is not None:
            cap = min(cap, max(Decimal(0), add(rules.lifetime_cap, -self.withdrawals)))
        if cap <= 0:
            reasons.append("NO_WITHDRAWABLE_PROFIT")
        return PayoutAssessment(not reasons, cap if not reasons else Decimal(0), tuple(reasons))

    def withdraw(self, gross: Decimal) -> PayoutResult:
        """Offline account event only. No provider payout or automatic approval.

        Withdrawals cannot cross a retained floor, even for breach-below policies.
        Floor-touch is conservatively forbidden here, as in deterministic sizing.
        """
        money(gross, positive=True)
        assessment = self.assess_payout()
        if not assessment.eligible or gross > assessment.maximum_gross_withdrawal:
            raise ValueError("payout not eligible or exceeds cap/buffer")
        state = self.state
        floor = state.trailing_floor
        high = state.eod_high_watermark
        intraday = self.intraday_high_watermark
        if self.plan.payouts.floor_effect is PayoutFloorEffect.LOWER_BY_WITHDRAWAL:
            floor, high, intraday = add(floor, -gross), add(high, -gross), add(intraday, -gross)
        equity = add(state.equity, -gross)
        daily_floor = state.daily_floor if self.plan.daily_loss_limit is not None else floor
        if equity <= floor or (self.plan.daily_loss_limit is not None and equity <= daily_floor):
            raise ValueError("payout would touch/cross active loss floor")
        payment = decimal_from_fraction(Fraction(gross) * Fraction(self.plan.payouts.profit_split))
        updated = replace(state, equity=equity, realized_pnl=add(state.realized_pnl, -gross),
                          trailing_floor=floor, eod_high_watermark=high, daily_floor=daily_floor)
        self._state = updated
        self.intraday_high_watermark = intraday
        self.withdrawals = add(self.withdrawals, gross)
        return PayoutResult(gross, payment, add(gross, -payment), equity, floor)

    def snapshot(self):
        result = super().snapshot()
        result["plan"] = self.plan.to_json()
        if self._configuration is not None:
            result["plan_configuration"] = self._configuration.to_json()
        result["plan_state"] = dict(trading_days=self.trading_days, best_day=str(self.best_day),
            withdrawals=str(self.withdrawals), day_start_profit=str(self.day_start_profit),
            day_active=self.day_active, completed_sessions=sorted(self.completed_sessions),
            intraday_high_watermark=str(self.intraday_high_watermark), paused=self.paused,
            open_positions=self.open_positions, clock=self.clock.isoformat() if self.clock else None,
            best_qualifying_day=str(self.best_qualifying_day), required_profit=str(self.required_profit))
        result["plan_state"].update(used_calendar_sessions=sorted(t.isoformat() for t in self.used_calendar_sessions),
            holding_restrictions=self.holding_restrictions, holding_breach=self.holding_breach)
        return result

    @classmethod
    def from_snapshot(cls, snapshot):
        # Avoid inherited default/mock restoration silently dropping plan rules.
        raise ValueError("plan-engine durable restoration is not qualified in v0.16A")


def plan_evaluator(plan, safety_config, estimate, path_config=None, *, verification_context=None, timeline=None):
    """Adapt to the unchanged v0.10 evaluator with a fresh engine per path.

    RiskPolicy stops entries at the absolute target even if eligibility is still
    pending. Such paths may stay censored; no target rewrite or safety bypass.
    """
    from prop_survival_engine.simulation.reach_avoid import PathConfig, ReachAvoidEvaluator
    configuration = plan if type(plan) is PlanConfiguration else None
    if configuration is not None:
        plan = configuration.require_usable(verification_context)
    if type(plan) is not PropPlan or plan.stage is not PlanStage.EVALUATION:
        raise ValueError("target-before-breach simulation requires an evaluation plan")
    # Existing v0.9 safety is stricter than breach-below/target-beyond semantics.
    # Reject incompatibility rather than silently estimating a different plan.
    if not plan.breach_on_touch or not plan.target_on_touch or (not plan.target_uses_unrealized and not plan.target_requires_flat):
        raise ValueError("Monte Carlo adapter requires existing equity/touch target semantics")
    horizon = path_config or PathConfig()
    if plan.calendar_json is not None:
        if (type(timeline) is not SimulationTimeline or len(timeline.opportunities) < horizon.max_sessions
                or any(len(times) < horizon.opportunities_per_session for times in timeline.opportunities[:horizon.max_sessions])):
            raise ValueError("complete explicit opportunity clock required for calendar simulation")
    return ReachAvoidEvaluator(lambda: PlanRuleEngine(configuration or plan, verification_context=verification_context,
                                timeline=timeline, initial_open_positions=0),
        replace(safety_config, max_contracts=min(safety_config.max_contracts, plan.maximum_position_size)),
        estimate, horizon)
