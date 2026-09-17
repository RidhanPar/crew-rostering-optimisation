"""Independent roster checker.

The solver's roster is only as right as the model. A typo in a constraint
(an off by one in a 7 day window, hours counted from departure instead of
report) produces a roster the solver calls optimal and that breaks the law.

So this module re checks every rule on the output, using only the clean input
tables and pandas. It deliberately does not import the model, the
eligibility view, or anything else the solver used. If the model and this
checker disagree, one of them has a bug, and a test tells you which.

It also checks the assumptions the model makes (at most one duty starting per
pilot per day) and reconciles the solver's reported cost and fairness numbers
against a recomputation from the roster.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

import numpy as np
import pandas as pd

from crew_roster.config import Config

log = logging.getLogger(__name__)

COLUMNS = ["stage", "check", "severity", "entity", "detail"]
RECONCILE_TOLERANCE = 1e-4  # relative


@dataclass
class CheckInputs:
    duties: pd.DataFrame
    crew: pd.DataFrame
    qualifications: pd.DataFrame
    leave: pd.DataFrame
    period_start: pd.Timestamp
    period_days: int


@dataclass
class CheckReport:
    issues: pd.DataFrame
    crew_hours: pd.DataFrame
    recomputed: dict[str, float]

    @property
    def errors(self) -> pd.DataFrame:
        return self.issues[self.issues["severity"] == "ERROR"]


def load_check_inputs(conn: sqlite3.Connection) -> CheckInputs:
    duties = pd.read_sql("SELECT * FROM duties", conn, parse_dates=["report_utc", "release_utc"]).set_index("duty_id")
    crew = pd.read_sql("SELECT * FROM crew_clean", conn).set_index("crew_id")
    quals = pd.read_sql("SELECT * FROM qualifications_clean", conn, parse_dates=["qualified_from", "licence_expiry"])
    leave = pd.read_sql("SELECT crew_id, leave_type, start_date, end_date FROM leave_all", conn,
                        parse_dates=["start_date", "end_date"])
    start, days = conn.execute("SELECT period_start, period_days FROM run_params").fetchone()
    return CheckInputs(duties, crew, quals, leave, pd.Timestamp(start), int(days))


def _issue(rows: list, check: str, severity: str, entity: str, detail: str) -> None:
    rows.append(("output", check, severity, entity, detail))


def available_days(inp: CheckInputs) -> pd.Series:
    """Days each pilot is neither on leave nor out of licence. Recomputed here, not read from SQL views."""
    days = pd.date_range(inp.period_start, periods=inp.period_days, freq="D")
    out = {}
    for crew_id in inp.crew.index:
        ok = np.zeros(len(days), dtype=bool)
        for q in inp.qualifications[inp.qualifications.crew_id == crew_id].itertuples():
            ok |= (days >= q.qualified_from) & (days <= q.licence_expiry)
        for lv in inp.leave[inp.leave.crew_id == crew_id].itertuples():
            ok &= ~((days >= lv.start_date) & (days <= lv.end_date))
        out[crew_id] = int(ok.sum())
    return pd.Series(out, name="available_days")


def check_roster(inp: CheckInputs, assignments: pd.DataFrame, uncovered: pd.DataFrame, config: Config,
                 reported: dict[str, float] | None = None, mode: str | None = None) -> CheckReport:
    rules, pay, obj = config.rules, config.pay, config.objective
    mode = mode or obj.mode
    rows: list = []
    a = assignments.copy()

    # Referential integrity ------------------------------------------------------
    unknown = a[~a.crew_id.isin(inp.crew.index) | ~a.duty_id.isin(inp.duties.index)]
    for r in unknown.itertuples():
        _issue(rows, "unknown_crew_or_duty", "ERROR", f"{r.crew_id}:{r.duty_id}", "not in clean inputs")
    a = a.drop(unknown.index)
    dup = a[a.duplicated()]
    for r in dup.itertuples():
        _issue(rows, "duplicate_assignment", "ERROR", f"{r.crew_id}:{r.duty_id}", "same pilot twice on one duty")
    a = a.drop_duplicates()

    a = a.join(inp.crew[["rank", "base", "hourly_rate_gbp", "fte"]], on="crew_id")
    a = a.join(inp.duties[["base", "aircraft_type", "report_utc", "release_utc", "duty_hours",
                           "captains_required", "first_officers_required"]], on="duty_id", rsuffix="_duty")
    a["duty_date"] = a["report_utc"].dt.normalize()
    a["day_index"] = (a["duty_date"] - inp.period_start).dt.days

    # Coverage ---------------------------------------------------------------------
    filled = a.groupby(["duty_id", "rank"]).size()
    reported_open = uncovered.groupby(["duty_id", "rank"])["seats"].sum() if not uncovered.empty else pd.Series(dtype=float)
    for duty_id, d in inp.duties.iterrows():
        for rank, required in (("CPT", d.captains_required), ("FO", d.first_officers_required)):
            got = int(filled.get((duty_id, rank), 0))
            open_ = int(reported_open.get((duty_id, rank), 0))
            if got > required:
                _issue(rows, "seat_overfilled", "ERROR", f"{duty_id}:{rank}", f"{got} assigned, {required} required")
            elif got + open_ != required:
                _issue(rows, "coverage_not_reconciled", "ERROR", f"{duty_id}:{rank}",
                       f"{got} assigned + {open_} reported uncovered != {required} required")
            elif open_ > 0:
                _issue(rows, "seat_uncovered", "WARNING", f"{duty_id}:{rank}",
                       f"{open_} seat(s) open on {d.report_utc:%Y-%m-%d %H:%M}, needs reserve cover")

    # Static eligibility, recomputed ---------------------------------------------------
    for r in a[a.base != a.base_duty].itertuples():
        _issue(rows, "base_mismatch", "ERROR", f"{r.crew_id}:{r.duty_id}", f"pilot base {r.base}, duty base {r.base_duty}")
    rank_needed = np.where(a["rank"] == "CPT", a["captains_required"], a["first_officers_required"])
    for r in a[rank_needed <= 0].itertuples():
        _issue(rows, "rank_not_required", "ERROR", f"{r.crew_id}:{r.duty_id}", f"{r.rank} not needed on this duty")

    q = inp.qualifications
    merged = a.merge(q, on=["crew_id", "aircraft_type"], how="left")
    no_rating = merged[merged["licence_expiry"].isna()]
    for r in no_rating.itertuples():
        _issue(rows, "not_rated_on_type", "ERROR", f"{r.crew_id}:{r.duty_id}", f"no {r.aircraft_type} rating")
    rated = merged.dropna(subset=["licence_expiry"])
    bad_licence = rated[(rated["release_utc"].dt.normalize() > rated["licence_expiry"])
                        | (rated["duty_date"] < rated["qualified_from"])]
    for r in bad_licence.itertuples():
        _issue(rows, "licence_not_valid", "ERROR", f"{r.crew_id}:{r.duty_id}",
               f"licence valid {r.qualified_from:%Y-%m-%d} to {r.licence_expiry:%Y-%m-%d}")

    on_leave = a.merge(inp.leave, on="crew_id")
    on_leave = on_leave[(on_leave["report_utc"] < on_leave["end_date"] + pd.Timedelta(days=1))
                        & (on_leave["release_utc"] > on_leave["start_date"])]
    for r in on_leave.itertuples():
        _issue(rows, "rostered_on_leave", "ERROR", f"{r.crew_id}:{r.duty_id}",
               f"{r.leave_type} {r.start_date:%Y-%m-%d} to {r.end_date:%Y-%m-%d}")

    # Roster dependent rules, per pilot --------------------------------------------------
    for crew_id, g in a.sort_values("report_utc").groupby("crew_id"):
        g = g.reset_index(drop=True)
        prev = g.shift(1)
        gap_h = (g["report_utc"] - prev["release_utc"]).dt.total_seconds() / 3600
        prev_len = prev["duty_hours"]
        required_rest = np.maximum(rules.min_rest_hours, prev_len) if rules.rest_at_least_previous_duty \
            else pd.Series(rules.min_rest_hours, index=g.index)
        for i in g.index[1:]:
            if gap_h[i] < 0:
                _issue(rows, "overlapping_duties", "ERROR", crew_id, f"{prev.at[i, 'duty_id']} overlaps {g.at[i, 'duty_id']}")
            elif gap_h[i] < required_rest[i] - 1e-9:
                _issue(rows, "rest_violation", "ERROR", crew_id,
                       f"{gap_h[i]:.2f}h rest before {g.at[i, 'duty_id']}, needs {required_rest[i]:.2f}h")

        per_day = g.groupby("day_index").agg(hours=("duty_hours", "sum"), duties=("duty_id", "count"))
        for day, r in per_day[per_day.hours > rules.max_duty_hours_day + 1e-9].iterrows():
            _issue(rows, "daily_hours_exceeded", "ERROR", crew_id, f"{r.hours:.2f}h on day {day}")
        for day, r in per_day[per_day.duties > 1].iterrows():
            _issue(rows, "model_assumption_one_duty_per_day", "ERROR", crew_id,
                   f"{r.duties} duties start on day {day}; days_7d constraint counted duties, not days")

        hours_by_day = per_day["hours"].reindex(range(inp.period_days), fill_value=0.0).to_numpy()
        worked = (per_day["duties"] > 0).reindex(range(inp.period_days), fill_value=False).to_numpy().astype(int)
        for t in range(max(1, inp.period_days - 6)):
            h7 = hours_by_day[t:t + 7].sum()
            d7 = worked[t:t + 7].sum()
            if h7 > rules.max_duty_hours_7d + 1e-6:
                _issue(rows, "hours_7d_exceeded", "ERROR", crew_id, f"{h7:.2f}h in days {t}-{t + 6}")
            if d7 > rules.max_duty_days_7d:
                _issue(rows, "duty_days_7d_exceeded", "ERROR", crew_id, f"{d7} duty days in days {t}-{t + 6}")
        total = g["duty_hours"].sum()
        if total > rules.max_duty_hours_month + 1e-6:
            _issue(rows, "month_hours_exceeded", "ERROR", crew_id, f"{total:.2f}h")

    # Hours, cost and fairness, recomputed ---------------------------------------------------
    crew_hours = _crew_hours(inp, a, config)
    recomputed = {
        "pay_cost": float(crew_hours["pay_cost"].sum()),
        "overtime_cost": float(crew_hours["overtime_cost"].sum()),
        "fairness_total_dev_hours": float(crew_hours["dev_outside_band"].sum()),
        "fairness_max_dev_hours": float(crew_hours.groupby("pool_id")["dev_outside_band"].max().sum()),
        "uncovered_seats": float(uncovered["seats"].sum()) if not uncovered.empty else 0.0,
    }
    if reported:
        keys = ["pay_cost", "uncovered_seats"]
        # Overtime and deviation variables are only pushed to their true value
        # when the objective charges for them.
        if mode != "feasibility":
            keys.append("overtime_cost")
        if mode in ("weighted", "fairness"):
            keys.append("fairness_total_dev_hours")
            if obj.fairness_weight_max > 0:
                keys.append("fairness_max_dev_hours")
        for key in keys:
            if key not in reported:
                continue
            want, got = recomputed[key], reported[key]
            if abs(want - got) > max(1e-3, RECONCILE_TOLERANCE * abs(want)):
                _issue(rows, "objective_not_reconciled", "ERROR", key,
                       f"solver reported {got:,.3f}, recomputed from roster {want:,.3f}")

    # Planning signals (not errors) ----------------------------------------------------------
    for r in crew_hours[(crew_hours.hours == 0) & (crew_hours.available_days >= 7)].itertuples():
        _issue(rows, "available_pilot_unused", "WARNING", r.Index,
               f"{r.available_days} available days, no duties. Check eligibility or staffing")
    at_cap = crew_hours[crew_hours.hours >= rules.max_duty_hours_month - 1.0]
    if not at_cap.empty:
        _issue(rows, "month_cap_binding", "INFO", f"{len(at_cap)} pilots",
               "within 1h of the monthly cap; this rule is limiting the roster")
    ot = crew_hours[crew_hours.overtime_hours > 0]
    if not ot.empty:
        _issue(rows, "overtime_used", "INFO", f"{len(ot)} pilots",
               f"{ot.overtime_hours.sum():.1f} overtime hours, £{ot.overtime_cost.sum():,.0f} premium")

    issues = pd.DataFrame(rows, columns=COLUMNS)
    counts = issues["severity"].value_counts().to_dict()
    log.info("Roster check: %s", counts or "no issues")
    return CheckReport(issues, crew_hours, recomputed)


def _crew_hours(inp: CheckInputs, a: pd.DataFrame, config: Config) -> pd.DataFrame:
    pay, obj = config.pay, config.objective
    crew = inp.crew.copy()
    crew = crew.join(inp.qualifications.sort_values("aircraft_type").groupby("crew_id")["aircraft_type"]
                     .agg("+".join).rename("type_ratings"), how="inner")
    crew["pool_id"] = crew["base"] + "-" + crew["type_ratings"] + "-" + crew["rank"]
    crew = crew.join(available_days(inp))

    stats = a.groupby("crew_id").agg(hours=("duty_hours", "sum"), duties=("duty_id", "count"))
    crew = crew.join(stats).fillna({"hours": 0.0, "duties": 0})
    crew["duties"] = crew["duties"].astype(int)

    # Fair target: the pool's required seat hours, shared by FTE x available days.
    d = inp.duties
    seat_hours = pd.concat([
        pd.DataFrame({"pool_id": d.base + "-" + d.aircraft_type + "-CPT", "h": d.duty_hours * d.captains_required}),
        pd.DataFrame({"pool_id": d.base + "-" + d.aircraft_type + "-FO", "h": d.duty_hours * d.first_officers_required}),
    ]).groupby("pool_id")["h"].sum()
    weight = crew["fte"] * crew["available_days"]
    pool_weight = weight.groupby(crew["pool_id"]).transform("sum")
    crew["target_hours"] = np.where(pool_weight > 0, crew["pool_id"].map(seat_hours).fillna(0) * weight / pool_weight, 0.0)
    crew["deviation_hours"] = crew["hours"] - crew["target_hours"]
    crew["dev_outside_band"] = (crew["deviation_hours"].abs() - obj.fairness_tolerance_hours).clip(lower=0)
    crew["utilisation"] = np.where(crew["target_hours"] > 0, crew["hours"] / crew["target_hours"], np.nan)

    threshold = pay.overtime_threshold_hours * crew["fte"] * inp.period_days / 31.0
    crew["overtime_threshold_hours"] = threshold
    crew["overtime_hours"] = (crew["hours"] - threshold).clip(lower=0)
    crew["pay_cost"] = crew["hours"] * crew["hourly_rate_gbp"]
    crew["overtime_cost"] = crew["overtime_hours"] * crew["hourly_rate_gbp"] * pay.overtime_premium
    return crew[["rank", "base", "type_ratings", "pool_id", "fte", "hourly_rate_gbp", "available_days", "duties",
                 "hours", "target_hours", "deviation_hours", "dev_outside_band", "utilisation",
                 "overtime_threshold_hours", "overtime_hours", "pay_cost", "overtime_cost"]]
