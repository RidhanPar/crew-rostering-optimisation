from __future__ import annotations

import sqlite3

import pytest

from crew_roster.config import load_config
from crew_roster.data.generate import generate
from crew_roster.data.pipeline import connect, run_pipeline


def small_config(tmp_path, dirty: bool = True, **overrides):
    base = {
        "paths": {
            "raw_dir": str(tmp_path / "raw"),
            "database": str(tmp_path / "roster.db"),
            "output_dir": str(tmp_path / "outputs"),
        },
        "generation": {"profile": "small", "period_days": 14, "inject_dirty_rows": dirty},
        # A 14 day, one base dataset has few duties, so the 6 injected bad
        # duties are about 10% of flying. Relax the gate for tests only.
        "pipeline": {"max_rejected_share": 0.25},
        "solver": {"time_limit_seconds": 60},
    }
    for section, values in overrides.items():
        base.setdefault(section, {}).update(values)
    return load_config(overrides=base)


def build_dataset(config) -> sqlite3.Connection:
    g = config.generation
    generate(config.paths.raw_dir, g.seed, g.profile, g.period_start, g.period_days, g.inject_dirty_rows)
    run_pipeline(config)
    return connect(config.paths.database)


@pytest.fixture
def dirty_config(tmp_path):
    return small_config(tmp_path, dirty=True)


@pytest.fixture
def dirty_db(dirty_config):
    conn = build_dataset(dirty_config)
    yield conn
    conn.close()
