from datetime import datetime, timezone
from copy import deepcopy
from threading import RLock

from prop_survival_engine.persistence.interfaces import JournalRecord


class InMemoryOmsJournal:
    def __init__(self) -> None:
        self._records: list[JournalRecord] = []
        self._lock = RLock()

    def append(self, event_type: str, payload: dict) -> JournalRecord:
        with self._lock:
            record = JournalRecord(len(self._records) + 1, event_type, deepcopy(payload), datetime.now(timezone.utc).isoformat())
            self._records.append(record)
            return deepcopy(record)

    def replay(self) -> list[JournalRecord]:
        with self._lock:
            return deepcopy(self._records)

    def close(self) -> None:
        pass

