from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    journal_path: Path = Path("data/oms.sqlite3")

