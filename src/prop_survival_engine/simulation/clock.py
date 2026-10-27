from datetime import datetime, timedelta, timezone


class SimulatedClock:
    def __init__(self, current: datetime | None = None) -> None:
        self.current = current or datetime(2024, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> datetime:
        self.current += delta
        return self.current

