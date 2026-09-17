"""Run what if scenarios and compare them against the baseline.

Every scenario goes through the same path as production: apply overrides,
load inputs, solve, then check the roster independently. A scenario whose
roster fails the checker is reported, never silently compared.
"""

from __future__ import annotations

import logging
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from crew_roster.analysis.consistency import CheckReport, check_roster, load_check_inputs
from crew_roster.config import PROJECT_ROOT, Config
from crew_roster.data.scenario_leave import LeaveWave, apply_leave_waves, clear_scenario_leave
from crew_roster.model.data import load_model_data
from crew_roster.model.roster import solve_roster
from crew_roster.model.solve import SolveResult

log = logging.getLogger(__name__)

SCENARIO_FILE = PROJECT_ROOT / "config" / "scenarios.toml"


@dataclass(frozen=True)
class Scenario:
    name: str
    group: str
    description: str
    overrides: dict = field(default_factory=dict)
    leave_waves: tuple[LeaveWave, ...] = ()


@dataclass
class ScenarioOutcome:
    scenario: Scenario
    config: Config
    result: SolveResult
    report: CheckReport
    kpis: dict
    scenario_leave: pd.DataFrame = field(default_factory=pd.DataFrame)


def load_scenarios(path: Path = SCENARIO_FILE) -> list[Scenario]:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)["scenario"]
    scenarios = [
        Scenario(name=s["name"], group=s.get("group", "rules"), description=s.get("description", ""),
                 overrides=s.get("overrides", {}),
                 leave_waves=tuple(LeaveWave.from_dict(w) for w in s.get("leave_waves", [])))
        for s in raw
    ]
    names = [s.name for s in scenarios]
    if len(set(names)) != len(names):
        raise ValueError("Scenario names must be unique")
    if "baseline" not in names:
        raise ValueError("scenarios.toml needs a scenario named baseline")
    return scenarios


def run_scenario(conn: sqlite3.Connection, base: Config, scenario: Scenario) -> ScenarioOutcome:
    config = base.with_overrides(scenario.overrides) if scenario.overrides else base
    log.info("Scenario %s: %s", scenario.name, scenario.description)
    try:
        if scenario.leave_waves:
            apply_leave_waves(conn, list(scenario.leave_waves))
        else:
            clear_scenario_leave(conn)
        extra_leave = pd.read_sql("SELECT * FROM scenario_leave", conn)
        data = load_model_data(conn)
        result = solve_roster(data, config)
        # Check against the scenario's own rules: a roster built under 14h rest
        # must be judged against 14h rest.
        report = check_roster(load_check_inputs(conn), result.assignments, result.uncovered, config,
                              reported=result.components if result.has_roster else None)
    finally:
        clear_scenario_leave(conn)
    return ScenarioOutcome(scenario, config, result, report, _kpis(scenario, result, report), extra_leave)


def _kpis(scenario: Scenario, result: SolveResult, report: CheckReport) -> dict:
    ch = report.crew_hours
    active = ch[ch["available_days"] > 0]
    issues = report.issues
    total_cost = report.recomputed["pay_cost"] + report.recomputed["overtime_cost"]
    covered_hours = float(ch["hours"].sum())
    return {
        "scenario": scenario.name,
        "group": scenario.group,
        "description": scenario.description,
        "status": result.status,
        "objective": result.objective,
        "best_bound": result.best_bound,
        "gap": result.gap,
        "time_limit_hit": result.time_limit_hit,
        "build_seconds": round(result.build_seconds, 2),
        "solve_seconds": round(result.solve_seconds, 2),
        "n_variables": result.n_variables,
        "n_constraints": result.n_constraints,
        "rest_constraints": result.constraint_counts.get("rest", 0),
        "uncovered_seats": int(report.recomputed["uncovered_seats"]),
        "rostered_hours": round(covered_hours, 1),
        "pay_cost_gbp": round(report.recomputed["pay_cost"], 2),
        "overtime_cost_gbp": round(report.recomputed["overtime_cost"], 2),
        "total_cost_gbp": round(total_cost, 2),
        "cost_per_rostered_hour_gbp": round(total_cost / covered_hours, 2) if covered_hours else None,
        "overtime_hours": round(float(ch["overtime_hours"].sum()), 1),
        "pilots_on_overtime": int((ch["overtime_hours"] > 0).sum()),
        "fairness_dev_outside_band_hours": round(report.recomputed["fairness_total_dev_hours"], 1),
        "mean_abs_deviation_hours": round(float(active["deviation_hours"].abs().mean()), 2),
        "max_abs_deviation_hours": round(float(active["deviation_hours"].abs().max()), 2),
        "pilots_outside_band": int((active["dev_outside_band"] > 0).sum()),
        "check_errors": int((issues["severity"] == "ERROR").sum()),
        "check_warnings": int((issues["severity"] == "WARNING").sum()),
    }


def compare(outcomes: list[ScenarioOutcome]) -> pd.DataFrame:
    table = pd.DataFrame([o.kpis for o in outcomes])
    base = table.loc[table["scenario"] == "baseline"].iloc[0]
    table["total_cost_delta_gbp"] = (table["total_cost_gbp"] - base["total_cost_gbp"]).round(2)
    table["total_cost_delta_pct"] = (100 * table["total_cost_delta_gbp"] / base["total_cost_gbp"]).round(3)
    table["uncovered_seats_delta"] = table["uncovered_seats"] - base["uncovered_seats"]
    table["mean_abs_deviation_delta_hours"] = (table["mean_abs_deviation_hours"] - base["mean_abs_deviation_hours"]).round(2)
    # Cost differences only mean something when the same flying was covered.
    table["cost_comparable_to_baseline"] = (table["uncovered_seats"] == base["uncovered_seats"]) & (table["check_errors"] == 0)
    # Each roster is only proven within its gap, so a small difference in
    # objective can be solver tolerance rather than a real effect. A scenario
    # is proven worse only if its lower bound is above the baseline roster's
    # objective (and proven better in the mirror case). Only meaningful when
    # the objective function is the same, so not for the objective group.
    same_objective = table["group"] != "objective"
    table["objective_delta_pct"] = (100 * (table["objective"] - base["objective"]) / base["objective"]).round(3)
    table["proven_worse_than_baseline"] = same_objective & (table["best_bound"] > base["objective"] + 1e-6)
    table["proven_better_than_baseline"] = same_objective & (table["objective"] < base["best_bound"] - 1e-6)
    table["difference_within_solver_gap"] = same_objective & ~table["proven_worse_than_baseline"] & ~table["proven_better_than_baseline"]
    return table


def write_outcome(outcome: ScenarioOutcome, root: Path) -> None:
    out = root / outcome.scenario.name
    out.mkdir(parents=True, exist_ok=True)
    r = outcome.result
    r.assignments.to_csv(out / "assignments.csv", index=False)
    r.uncovered.to_csv(out / "uncovered_seats.csv", index=False)
    r.pool_results.to_csv(out / "pool_results.csv", index=False)
    outcome.report.issues.to_csv(out / "roster_check_issues.csv", index=False)
    outcome.report.crew_hours.to_csv(out / "crew_hours.csv", index_label="crew_id")
    outcome.scenario_leave.to_csv(out / "scenario_leave.csv", index=False)


def run_scenarios(conn: sqlite3.Connection, base: Config, names: list[str] | None = None,
                  out_root: Path | None = None) -> tuple[list[ScenarioOutcome], pd.DataFrame]:
    scenarios = load_scenarios()
    if names:
        unknown = set(names) - {s.name for s in scenarios}
        if unknown:
            raise ValueError(f"Unknown scenarios {sorted(unknown)}")
        scenarios = [s for s in scenarios if s.name in names or s.name == "baseline"]
    outcomes = []
    for scenario in scenarios:
        outcome = run_scenario(conn, base, scenario)
        if out_root is not None:
            write_outcome(outcome, out_root)
        k = outcome.kpis
        log.info("  %-24s %-9s cost £%s, uncovered %d, mean |dev| %.1fh, solve %.1fs, checker errors %d",
                 scenario.name, k["status"], f"{k['total_cost_gbp']:,.0f}", k["uncovered_seats"],
                 k["mean_abs_deviation_hours"], k["solve_seconds"], k["check_errors"])
        outcomes.append(outcome)
    table = compare(outcomes)
    if out_root is not None:
        out_root.mkdir(parents=True, exist_ok=True)
        table.to_csv(out_root / "scenario_comparison.csv", index=False)
    return outcomes, table
