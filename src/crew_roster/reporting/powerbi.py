"""Power BI ready star schema.

Reads the clean database and the per scenario outputs written by
`python -m crew_roster scenarios`, and writes flat CSVs with stable column
names, one fact grain per table, and surrogate free natural keys that Power
BI can relate directly. See docs/powerbi_dashboard_spec.md for the model and
the DAX measures.

Every fact table carries `scenario`, so one report can slice any page by
scenario or compare two side by side.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from crew_roster.analysis.scenarios import load_scenarios

log = logging.getLogger(__name__)

TABLES = [
    "dim_crew", "dim_duty", "dim_date", "dim_scenario",
    "fact_assignment", "fact_crew_period", "fact_crew_day", "fact_seat_coverage",
    "fact_scenario_summary", "fact_pool_solve", "fact_data_quality",
]


def _wave(hour: int) -> str:
    return "Early" if hour < 9 else ("Mid" if hour < 14 else "Late")


def build_dimensions(conn: sqlite3.Connection) -> dict[str, pd.DataFrame]:
    crew = pd.read_sql("""
        SELECT c.crew_id, c.rank, c.base, m.type_ratings, m.pool_id, c.seniority_years, c.hourly_rate_gbp, c.fte,
               q.licence_expiry
        FROM crew_clean c
        LEFT JOIN crew_model m USING (crew_id)
        LEFT JOIN (SELECT crew_id, MAX(licence_expiry) AS licence_expiry FROM qualifications_clean GROUP BY crew_id) q
          USING (crew_id)""", conn)
    crew["rank_name"] = crew["rank"].map({"CPT": "Captain", "FO": "First Officer"})

    duty = pd.read_sql("""
        SELECT d.duty_id, d.base, d.aircraft_type, d.report_utc, d.release_utc, d.duty_date, d.day_index,
               d.duty_hours, d.block_hours, d.n_legs, d.captains_required, d.first_officers_required,
               (SELECT GROUP_CONCAT(arr_airport, '-') FROM
                   (SELECT arr_airport FROM flights_clean f WHERE f.duty_id = d.duty_id ORDER BY leg_seq)) AS route
        FROM duties d""", conn)
    duty["route"] = duty["base"] + "-" + duty["route"]
    duty["report_wave"] = pd.to_datetime(duty["report_utc"]).dt.hour.map(_wave)

    date = pd.read_sql("SELECT cal_date AS date, day_index, weekday FROM calendar", conn)
    d = pd.to_datetime(date["date"])
    date["week_start"] = (d - pd.to_timedelta(d.dt.weekday, unit="D")).dt.strftime("%Y-%m-%d")
    date["is_weekend"] = d.dt.weekday >= 5

    scen = pd.DataFrame([{"scenario": s.name, "scenario_group": s.group, "description": s.description,
                          "sort_order": i} for i, s in enumerate(load_scenarios())])
    return {"dim_crew": crew, "dim_duty": duty, "dim_date": date, "dim_scenario": scen}


def build_facts(conn: sqlite3.Connection, scenario_root: Path, dims: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    crew, duty, date = dims["dim_crew"], dims["dim_duty"], dims["dim_date"]
    comparison = pd.read_csv(scenario_root / "scenario_comparison.csv")
    base_leave = pd.read_sql("SELECT crew_id, leave_type, start_date, end_date FROM leave_clean", conn)

    assignments, crew_period, crew_day, coverage, pools, quality = [], [], [], [], [], []
    for name in comparison["scenario"]:
        folder = scenario_root / name
        a = pd.read_csv(folder / "assignments.csv")
        a = a.merge(crew[["crew_id", "rank", "hourly_rate_gbp"]], on="crew_id").merge(
            duty[["duty_id", "duty_date", "duty_hours"]], on="duty_id")
        a["pay_cost_gbp"] = (a["duty_hours"] * a["hourly_rate_gbp"]).round(2)
        assignments.append(a[["crew_id", "duty_id", "duty_date", "rank", "duty_hours", "pay_cost_gbp"]].assign(scenario=name))

        ch = pd.read_csv(folder / "crew_hours.csv")
        crew_period.append(ch.assign(scenario=name))

        # Seat coverage: one row per duty and rank.
        filled = a.groupby(["duty_id", "rank"]).size().rename("assigned").reset_index()
        seats = pd.concat([
            duty.assign(rank="CPT", required=duty["captains_required"]),
            duty.assign(rank="FO", required=duty["first_officers_required"]),
        ])
        seats = seats[seats["required"] > 0][["duty_id", "duty_date", "base", "aircraft_type", "rank", "required"]]
        seats = seats.merge(filled, on=["duty_id", "rank"], how="left").fillna({"assigned": 0})
        seats["assigned"] = seats["assigned"].astype(int)
        seats["uncovered"] = seats["required"] - seats["assigned"]
        coverage.append(seats.assign(scenario=name))

        # Crew day grid for a roster heatmap: DUTY, LEAVE, UNAVAILABLE (licence) or OFF.
        leave_file = folder / "scenario_leave.csv"
        extra = pd.read_csv(leave_file) if leave_file.exists() and leave_file.stat().st_size > 1 else pd.DataFrame()
        leave = pd.concat([base_leave, extra[base_leave.columns]] if not extra.empty else [base_leave])
        crew_day.append(_crew_day_grid(crew, date, a, leave, conn).assign(scenario=name))

        pools.append(pd.read_csv(folder / "pool_results.csv").assign(scenario=name))
        issues = pd.read_csv(folder / "roster_check_issues.csv")
        quality.append(issues.assign(scenario=name))

    input_quality = pd.concat([
        pd.read_sql("SELECT 'cleaning' AS stage, 'rejected:' || rule AS \"check\", 'REJECTED' AS severity, "
                    "source_table || ':' || record_key AS entity, detail FROM rejected_rows", conn),
        pd.read_sql("SELECT stage, \"check\", severity, entity, detail FROM validation_issues", conn),
    ]).assign(scenario="(input)")

    pool_cols = ["scenario", "pool_id", "status", "solve_seconds", "gap", "n_variables", "n_binaries",
                 "n_constraints", "uncovered_seats", "pay_cost", "overtime_cost", "time_limit_hit"]
    pool_df = pd.concat(pools, ignore_index=True)
    return {
        "fact_assignment": pd.concat(assignments, ignore_index=True),
        "fact_crew_period": pd.concat(crew_period, ignore_index=True),
        "fact_crew_day": pd.concat(crew_day, ignore_index=True),
        "fact_seat_coverage": pd.concat(coverage, ignore_index=True),
        "fact_scenario_summary": comparison,
        "fact_pool_solve": pool_df[[c for c in pool_cols if c in pool_df.columns]],
        "fact_data_quality": pd.concat([input_quality, *quality], ignore_index=True),
    }


def _crew_day_grid(crew: pd.DataFrame, date: pd.DataFrame, a: pd.DataFrame, leave: pd.DataFrame,
                   conn: sqlite3.Connection) -> pd.DataFrame:
    grid = crew[["crew_id"]].merge(date[["date"]], how="cross")
    worked = a.groupby(["crew_id", "duty_date"])["duty_hours"].sum().rename("duty_hours").reset_index()
    grid = grid.merge(worked, left_on=["crew_id", "date"], right_on=["crew_id", "duty_date"], how="left").drop(columns="duty_date")
    grid["duty_hours"] = grid["duty_hours"].fillna(0.0)

    dates = pd.to_datetime(grid["date"])
    on_leave = pd.Series(False, index=grid.index)
    leave_type = pd.Series("", index=grid.index)
    for lv in leave.itertuples():
        hit = (grid["crew_id"] == lv.crew_id) & (dates >= pd.Timestamp(lv.start_date)) & (dates <= pd.Timestamp(lv.end_date))
        on_leave |= hit
        leave_type = leave_type.where(~hit, lv.leave_type)

    quals = pd.read_sql("SELECT crew_id, qualified_from, licence_expiry FROM qualifications_clean", conn)
    valid = pd.Series(False, index=grid.index)
    for q in quals.itertuples():
        valid |= (grid["crew_id"] == q.crew_id) & (dates >= pd.Timestamp(q.qualified_from)) & (dates <= pd.Timestamp(q.licence_expiry))

    grid["day_status"] = np.select(
        [grid["duty_hours"] > 0, on_leave, ~valid],
        ["DUTY", "LEAVE", "UNAVAILABLE"],
        default="OFF",
    )
    grid["leave_type"] = leave_type.where(on_leave, "")
    return grid


def export_powerbi(conn: sqlite3.Connection, scenario_root: Path, out_dir: Path) -> dict[str, int]:
    if not (scenario_root / "scenario_comparison.csv").exists():
        raise FileNotFoundError(f"{scenario_root / 'scenario_comparison.csv'} missing; run scenarios first")
    dims = build_dimensions(conn)
    tables = {**dims, **build_facts(conn, scenario_root, dims)}
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name in TABLES:
        df = tables[name]
        df.to_csv(out_dir / f"{name}.csv", index=False, lineterminator="\n")
        counts[name] = len(df)
    log.info("Power BI tables written to %s: %s", out_dir, counts)
    return counts
