"""Typed configuration loaded from TOML.

All business rules live in config/default.toml. Code never hard codes a rest
period or an hours cap, so a scenario is just a set of overrides on this file.
"""

from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "default.toml"


@dataclass(frozen=True)
class Paths:
    raw_dir: Path
    database: Path
    output_dir: Path


@dataclass(frozen=True)
class GenerationConfig:
    seed: int
    profile: str
    period_start: str
    period_days: int
    inject_dirty_rows: bool


@dataclass(frozen=True)
class PipelineConfig:
    max_rejected_share: float


@dataclass(frozen=True)
class Rules:
    report_minutes: int
    debrief_minutes: int
    min_rest_hours: float
    rest_at_least_previous_duty: bool
    max_duty_hours_day: float
    max_duty_hours_7d: float
    max_duty_days_7d: int
    max_duty_hours_month: float


@dataclass(frozen=True)
class PayConfig:
    overtime_threshold_hours: float
    overtime_premium: float


@dataclass(frozen=True)
class ObjectiveConfig:
    mode: str
    fairness_weight_total: float
    fairness_weight_max: float
    uncovered_penalty: float

    def __post_init__(self) -> None:
        if self.mode not in {"cost", "fairness", "weighted", "feasibility"}:
            raise ValueError(f"Unknown objective mode: {self.mode}")


@dataclass(frozen=True)
class SolverConfig:
    elastic_coverage: bool
    time_limit_seconds: float
    gap_rel: float
    threads: int


@dataclass(frozen=True)
class Config:
    paths: Paths
    log_level: str
    generation: GenerationConfig
    pipeline: PipelineConfig
    rules: Rules
    pay: PayConfig
    objective: ObjectiveConfig
    solver: SolverConfig
    raw: dict[str, Any]

    def with_overrides(self, overrides: dict[str, Any]) -> "Config":
        """Return a new Config with nested section overrides applied."""
        merged = deep_merge(self.raw, overrides)
        return config_from_dict(merged, root=PROJECT_ROOT)


def deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _section(cls: type, data: dict[str, Any]):
    names = {f.name for f in fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"Unknown keys for {cls.__name__}: {sorted(unknown)}")
    missing = names - set(data)
    if missing:
        raise ValueError(f"Missing keys for {cls.__name__}: {sorted(missing)}")
    return cls(**data)


def config_from_dict(data: dict[str, Any], root: Path = PROJECT_ROOT) -> Config:
    paths = data["paths"]
    return Config(
        paths=Paths(
            raw_dir=(root / paths["raw_dir"]).resolve(),
            database=(root / paths["database"]).resolve(),
            output_dir=(root / paths["output_dir"]).resolve(),
        ),
        log_level=data.get("logging", {}).get("level", "INFO"),
        generation=_section(GenerationConfig, data["generation"]),
        pipeline=_section(PipelineConfig, data["pipeline"]),
        rules=_section(Rules, data["rules"]),
        pay=_section(PayConfig, data["pay"]),
        objective=_section(ObjectiveConfig, data["objective"]),
        solver=_section(SolverConfig, data["solver"]),
        raw=data,
    )


def load_config(path: Path | str | None = None, overrides: dict[str, Any] | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    if overrides:
        data = deep_merge(data, overrides)
    return config_from_dict(data)
