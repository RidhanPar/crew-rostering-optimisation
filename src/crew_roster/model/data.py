"""Solver inputs, read from the clean SQLite tables.

ModelData is plain pandas so tests can build tiny hand made instances
without a database.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pandas as pd

RANKS = ("CPT", "FO")


@dataclass
class ModelData:
    crew: pd.DataFrame         # index crew_id: rank, base, pool_id, hourly_rate_gbp, fte, available_days
    duties: pd.DataFrame       # index duty_id: base, aircraft_type, report_utc, release_utc, duty_hours, day_index, captains_required, first_officers_required
    eligibility: pd.DataFrame  # columns crew_id, duty_id
    period_days: int

    def __post_init__(self) -> None:
        self.duties = self.duties.copy()
        self.duties["report_utc"] = pd.to_datetime(self.duties["report_utc"])
        self.duties["release_utc"] = pd.to_datetime(self.duties["release_utc"])
        known = self.eligibility["crew_id"].isin(self.crew.index) & self.eligibility["duty_id"].isin(self.duties.index)
        self.eligibility = self.eligibility[known].reset_index(drop=True)

    @property
    def seats(self) -> pd.DataFrame:
        """One row per (duty, rank) that needs crew, with the pool it belongs to."""
        rows = []
        for duty_id, d in self.duties.iterrows():
            for rank, required in (("CPT", d.captains_required), ("FO", d.first_officers_required)):
                if required > 0:
                    rows.append((duty_id, rank, int(required), f"{d.base}-{d.aircraft_type}-{rank}", d.duty_hours))
        return pd.DataFrame(rows, columns=["duty_id", "rank", "required", "pool_id", "duty_hours"])

    def subset_pools(self, pool_ids: set[str]) -> "ModelData":
        """Keep only some crew pools. Pools share no constraints, so a pool can be solved alone."""
        crew = self.crew[self.crew["pool_id"].isin(pool_ids)]
        seat_pools = self.duties["base"] + "-" + self.duties["aircraft_type"]
        keep = seat_pools.apply(lambda p: any(pid.startswith(p + "-") for pid in pool_ids))
        duties = self.duties[keep].copy()
        ranks = {pid.rsplit("-", 1)[1] for pid in pool_ids}
        if "CPT" not in ranks:
            duties["captains_required"] = 0
        if "FO" not in ranks:
            duties["first_officers_required"] = 0
        return ModelData(crew, duties, self.eligibility, self.period_days)


def load_model_data(conn: sqlite3.Connection) -> ModelData:
    crew = pd.read_sql(
        "SELECT crew_id, rank, base, type_ratings, pool_id, hourly_rate_gbp, fte, available_days FROM crew_model",
        conn,
    ).set_index("crew_id")
    duties = pd.read_sql(
        """SELECT duty_id, base, aircraft_type, report_utc, release_utc, duty_hours, day_index,
                  captains_required, first_officers_required
           FROM duties ORDER BY report_utc""",
        conn,
    ).set_index("duty_id")
    eligibility = pd.read_sql("SELECT crew_id, duty_id FROM eligibility", conn)
    period_days = conn.execute("SELECT period_days FROM run_params").fetchone()[0]
    return ModelData(crew, duties, eligibility, int(period_days))
