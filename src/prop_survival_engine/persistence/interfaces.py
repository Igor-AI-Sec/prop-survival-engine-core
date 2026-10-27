from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class JournalRecord:
    sequence: int
    event_type: str
    payload: dict
    created_at: str


class OmsJournal(Protocol):
    def append(self, event_type: str, payload: dict) -> JournalRecord: ...
    def replay(self) -> list[JournalRecord]: ...
    def close(self) -> None: ...

