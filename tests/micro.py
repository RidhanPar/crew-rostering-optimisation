"""Tiny hand built instances where the right answer is obvious."""

from __future__ import annotations

import pandas as pd

from crew_roster.config import load_config
from crew_roster.model.data import ModelData


def config(**sections):
    base = {"solver": {"time_limit_seconds": 30, "gap_rel": 0.0}}
    for name, values in sections.items():
        base.setdefault(name, {}).update(values)
    return load_config(overrides=base)


def pilot(crew_id: str, rate: float = 100.0, fte: float = 1.0, available_days: int = 7, rank: str = "CPT") -> dict:
    return {"crew_id": crew_id, "rank": rank, "base": "MAN", "type_ratings": "A320",
            "pool_id": f"MAN-A320-{rank}", "hourly_rate_gbp": rate, "fte": fte, "available_days": available_days}


def duty(duty_id: str, report: str, release: str) -> dict:
    r, e = pd.Timestamp(report), pd.Timestamp(release)
    return {"duty_id": duty_id, "base": "MAN", "aircraft_type": "A320", "report_utc": r, "release_utc": e,
            "duty_hours": (e - r).total_seconds() / 3600, "day_index": (r.normalize() - pd.Timestamp("2026-10-01")).days,
            "captains_required": 1, "first_officers_required": 0}


def data(pilots: list[dict], duties: list[dict], period_days: int = 7, eligibility=None) -> ModelData:
    crew = pd.DataFrame(pilots).set_index("crew_id")
    dts = pd.DataFrame(duties).set_index("duty_id")
    if eligibility is None:
        eligibility = [(c["crew_id"], d["duty_id"]) for c in pilots for d in duties]
    elig = pd.DataFrame(eligibility, columns=["crew_id", "duty_id"])
    return ModelData(crew, dts, elig, period_days)
