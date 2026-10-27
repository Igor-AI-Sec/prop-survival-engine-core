"""Injected UTC restrictions only; no provider calendar, news feed or timezone guesses."""
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json


def utc(value):
    if type(value) is not datetime or value.tzinfo is not timezone.utc:
        raise ValueError("explicit timezone.utc datetime required")


class HoldingAction(str, Enum):
    BLOCK_NEW = "BLOCK_NEW"
    REQUIRE_FLAT = "REQUIRE_FLAT"
    BREACH = "BREACH"


@dataclass(frozen=True)
class SessionWindow:
    start: datetime
    end: datetime
    forced_flat_at: datetime | None

    def __post_init__(self):
        for value in (self.start, self.end):
            utc(value)
        if self.forced_flat_at is not None:
            utc(self.forced_flat_at)
        if not self.start < self.end or (self.forced_flat_at is not None and not self.start < self.forced_flat_at <= self.end):
            raise ValueError("session must have ordered start/flat/end")


@dataclass(frozen=True)
class BlackoutWindow:
    name: str
    start: datetime
    end: datetime
    holding_action: HoldingAction

    def __post_init__(self):
        utc(self.start); utc(self.end)
        if (type(self.name) is not str or not self.name.strip() or not self.start < self.end
                or type(self.holding_action) is not HoldingAction):
            raise ValueError("named ordered blackout and explicit holding consequence required")


@dataclass(frozen=True)
class CalendarEligibility:
    can_open: bool
    must_flatten: bool
    breaches_if_holding: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class TradingCalendar:
    sessions: tuple[SessionWindow, ...]
    blackouts: tuple[BlackoutWindow, ...]
    no_overnight: bool
    weekends_allowed: bool
    outside_session_action: HoldingAction

    def __post_init__(self):
        if (type(self.sessions) is not tuple or not self.sessions
                or any(type(v) is not SessionWindow for v in self.sessions)
                or type(self.blackouts) is not tuple or any(type(v) is not BlackoutWindow for v in self.blackouts)
                or type(self.no_overnight) is not bool or type(self.weekends_allowed) is not bool
                or type(self.outside_session_action) is not HoldingAction):
            raise ValueError("explicit immutable calendar and consequences required")
        if any(a.end > b.start for a, b in zip(self.sessions, self.sessions[1:])):
            raise ValueError("sessions must be chronological and nonoverlapping")
        if tuple(sorted(self.blackouts, key=lambda b: (b.start, b.end, b.name))) != self.blackouts:
            raise ValueError("blackouts must have deterministic chronological order")
        if self.no_overnight and self.outside_session_action is HoldingAction.BLOCK_NEW:
            raise ValueError("no-overnight needs an explicit flat/breach consequence")

    def eligibility(self, at):
        if at is None:
            return CalendarEligibility(False, False, False, ("MISSING_CLOCK",))
        utc(at)
        reasons, actions = [], []
        window = next((w for w in self.sessions if w.start <= at < w.end), None)
        if window is None:
            reasons.append("OUTSIDE_SESSION")
            actions.append(self.outside_session_action)
        elif window.forced_flat_at is not None and at >= window.forced_flat_at:
            reasons.append("FORCED_FLAT_DEADLINE")
            actions.append(HoldingAction.REQUIRE_FLAT if self.outside_session_action is HoldingAction.BLOCK_NEW else self.outside_session_action)
        if not self.weekends_allowed and at.weekday() >= 5:
            reasons.append("WEEKEND_RESTRICTED")
            actions.append(self.outside_session_action)
        for blackout in self.blackouts:
            if blackout.start <= at < blackout.end:
                reasons.append("BLACKOUT:" + blackout.name)
                actions.append(blackout.holding_action)
        return CalendarEligibility(not reasons,
            any(a in (HoldingAction.REQUIRE_FLAT, HoldingAction.BREACH) for a in actions),
            HoldingAction.BREACH in actions, tuple(reasons))

    def to_json(self):
        return json.dumps(dict(version=1,
            sessions=[dict(start=w.start.isoformat(), end=w.end.isoformat(), forced_flat_at=w.forced_flat_at.isoformat() if w.forced_flat_at else None) for w in self.sessions],
            blackouts=[dict(name=w.name, start=w.start.isoformat(), end=w.end.isoformat(), holding_action=w.holding_action.value) for w in self.blackouts],
            no_overnight=self.no_overnight, weekends_allowed=self.weekends_allowed,
            outside_session_action=self.outside_session_action.value), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text):
        def unique(pairs):
            d = {}
            for k, v in pairs:
                if k in d:
                    raise ValueError("duplicate calendar field")
                d[k] = v
            return d
        try:
            d = json.loads(text, object_pairs_hook=unique)
            if set(d) != {"version", "sessions", "blackouts", "no_overnight", "weekends_allowed", "outside_session_action"} or type(d["version"]) is not int or d["version"] != 1:
                raise ValueError("calendar envelope mismatch")
            result = cls(tuple(SessionWindow(**{k: datetime.fromisoformat(v) if v is not None else None for k, v in w.items()}) for w in d["sessions"]),
                tuple(BlackoutWindow(w["name"], datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"]), HoldingAction(w["holding_action"])) for w in d["blackouts"]),
                d["no_overnight"], d["weekends_allowed"], HoldingAction(d["outside_session_action"]))
            if result.to_json() != text:
                raise ValueError("calendar must use canonical complete serialization")
            return result
        except (TypeError, KeyError, ValueError):
            raise ValueError("invalid/ambiguous calendar serialization") from None


@dataclass(frozen=True)
class SimulationTimeline:
    """Explicit per-session opportunity clocks, never sampled or fetched."""
    opportunities: tuple[tuple[datetime, ...], ...]

    def __post_init__(self):
        if type(self.opportunities) is not tuple or not self.opportunities or any(type(s) is not tuple or not s for s in self.opportunities):
            raise ValueError("immutable nonempty opportunity grid required")
        times = [t for s in self.opportunities for t in s]
        for t in times:
            utc(t)
        if any(a > b for a, b in zip(times, times[1:])):
            raise ValueError("opportunity clock cannot move backward")
