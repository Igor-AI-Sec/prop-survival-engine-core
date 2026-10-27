"""Versioned, process-stable seed hierarchy independent of iteration order."""

from dataclasses import dataclass
from hashlib import sha256
import json


def _derive(parent: int, label: str, value: str | int) -> int:
    encoded = json.dumps(["prop-survival-engine/mc-seed/v1", str(parent), label, str(value)],
                         ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return int.from_bytes(sha256(encoded).digest(), "big")


@dataclass(frozen=True)
class PathSeedContext:
    master_seed: int
    scenario_name: str
    scenario_seed: int
    path_index: int
    path_seed: int


def path_seed_context(master_seed: int, scenario_name: str, path_index: int) -> PathSeedContext:
    if (type(master_seed) is not int or not isinstance(scenario_name, str) or not scenario_name.strip()
            or type(path_index) is not int or path_index < 0):
        raise ValueError("seed hierarchy requires integer seed/index and nonempty scenario name")
    scenario_seed = _derive(master_seed, "scenario", scenario_name)
    return PathSeedContext(master_seed, scenario_name, scenario_seed, path_index,
                           _derive(scenario_seed, "path", path_index))
