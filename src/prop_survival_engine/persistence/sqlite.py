import json
import sqlite3
from pathlib import Path
from threading import RLock
from time import monotonic, sleep

from prop_survival_engine.persistence.interfaces import JournalRecord


class SQLiteOmsJournal:
    """Append-only SQLite journal using WAL and FULL synchronous commits."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        # Concurrent first open can make this pragma return SQLITE_BUSY without
        # waiting for sqlite3's busy timeout. Retry only this idempotent setup;
        # exhausted contention fails closed before any journal mutation.
        deadline = monotonic() + 30
        while True:
            try:
                self._connection.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as error:
                if (getattr(error, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY
                        or monotonic() >= deadline):
                    self._connection.close()
                    raise
                sleep(0.01)
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS journal ("
            "sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, "
            "payload TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        self._connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS unique_created_intent ON journal "
            "(json_extract(payload, '$.intent_id')) WHERE event_type = 'order_created'"
        )

    def append(self, event_type: str, payload: dict) -> JournalRecord:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._lock:
            if (not getattr(self, "operational_managed", False)
                    and self._connection.execute("SELECT 1 FROM sqlite_master WHERE name='operational_owner' AND type='table'").fetchone()):
                raise RuntimeError("Operational journal requires an active fenced ledger")
            if (not getattr(self, "risk_managed", False)
                    and self._connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_account'"
                    ).fetchone()):
                raise RuntimeError("Risk-managed journal requires its transactional risk ledger")
            cursor = self._connection.execute(
                "INSERT INTO journal (event_type, payload) VALUES (?, ?)", (event_type, encoded)
            )
            row = self._connection.execute(
                "SELECT sequence, created_at FROM journal WHERE sequence = ?", (cursor.lastrowid,)
            ).fetchone()
        return JournalRecord(row[0], event_type, payload, row[1])

    def replay(self) -> list[JournalRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT sequence, event_type, payload, created_at FROM journal ORDER BY sequence"
            ).fetchall()
        return [JournalRecord(seq, kind, json.loads(payload), created) for seq, kind, payload, created in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()
