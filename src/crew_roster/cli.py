"""Command line entry point.

    python -m crew_roster generate     build raw CSVs
    python -m crew_roster prepare      clean, transform and validate into SQLite
    python -m crew_roster solve        build and solve the roster model
    python -m crew_roster diagnose     demonstrate and explain an infeasible model
    python -m crew_roster check        independently check the last solved roster
    python -m crew_roster scenarios    run what if scenarios and compare them
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import closing
from pathlib import Path

from crew_roster.analysis.consistency import check_roster, load_check_inputs
from crew_roster.analysis.scenarios import run_scenarios
from crew_roster.config import load_config
from crew_roster.data.generate import generate
from crew_roster.data.pipeline import connect, run_pipeline
from crew_roster.data.scenario_leave import LeaveWave, apply_leave_waves, clear_scenario_leave
from crew_roster.data.validate import ValidationFailed, assert_valid, run_validation
from crew_roster.logging_setup import setup_logging
from crew_roster.model.data import load_model_data
from crew_roster.model.diagnose import diagnose
from crew_roster.model.roster import solve_roster
from crew_roster.model.solve import INFEASIBLE

log = logging.getLogger("crew_roster")


def cmd_generate(config, _args) -> int:
    g = config.generation
    generate(config.paths.raw_dir, g.seed, g.profile, g.period_start, g.period_days, g.inject_dirty_rows)
    return 0


def cmd_prepare(config, _args) -> int:
    run_pipeline(config)
    with closing(connect(config.paths.database)) as conn:
        issues = run_validation(conn)
    try:
        assert_valid(issues)
    except ValidationFailed as exc:
        log.error("%s. Fix the input data before solving.", exc)
        return 2
    return 0


def write_result(result, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    result.assignments.to_csv(out_dir / "assignments.csv", index=False)
    result.uncovered.to_csv(out_dir / "uncovered_seats.csv", index=False)
    result.pool_results.to_csv(out_dir / "pool_results.csv", index=False)
    (out_dir / "solve_summary.json").write_text(json.dumps(result.summary(), indent=2, default=str))


def cmd_solve(config, args) -> int:
    if args.mode:
        config = config.with_overrides({"objective": {"mode": args.mode}})
    with closing(connect(config.paths.database)) as conn:
        clear_scenario_leave(conn)
        data = load_model_data(conn)
    result = solve_roster(data, config, elastic=False if args.strict else None,
                          decompose=False if args.no_decompose else None)
    out = config.paths.output_dir / "roster"
    write_result(result, out)
    log.info("Status %s, objective %s, %d assignments, %d uncovered seats, written to %s",
             result.status, result.objective, len(result.assignments),
             int(result.uncovered["seats"].sum()) if not result.uncovered.empty else 0, out)
    return 0 if result.has_roster else 3


def cmd_diagnose(config, args) -> int:
    """Knock out pilots in one pool, show the strict model fails, then explain why."""
    start, end = (int(x) for x in args.days.split("-"))
    wave = LeaveWave(args.pool, args.count, start, end)
    with closing(connect(config.paths.database)) as conn:
        apply_leave_waves(conn, [wave])
        data = load_model_data(conn)
        clear_scenario_leave(conn)
    pool_data = data.subset_pools({args.pool})
    strict = solve_roster(pool_data, config, elastic=False, decompose=False)
    log.info("Strict solve with %d %s pilots sick on days %s: %s", args.count, args.pool, args.days, strict.status)
    if strict.status != INFEASIBLE:
        log.info("Model is still feasible; try a larger --count")
        return 0
    diag = diagnose(pool_data, config, time_limit=args.time_limit)
    out = config.paths.output_dir / "diagnosis"
    out.mkdir(parents=True, exist_ok=True)
    diag.precheck.to_csv(out / "capacity_precheck.csv", index=False)
    diag.uncovered.to_csv(out / "min_uncovered_seats.csv", index=False)
    diag.single_relaxations.to_csv(out / "single_family_relaxations.csv", index=False)
    (out / "conflict_sets.json").write_text(json.dumps(diag.conflict_sets, indent=2))
    for line in diag.summary_lines():
        log.info(line)
    return 0


def cmd_check(config, _args) -> int:
    import pandas as pd

    out = config.paths.output_dir / "roster"
    if not (out / "assignments.csv").exists():
        log.error("No roster at %s; run solve first", out)
        return 2
    assignments = pd.read_csv(out / "assignments.csv")
    uncovered = pd.read_csv(out / "uncovered_seats.csv")
    summary = json.loads((out / "solve_summary.json").read_text())
    with closing(connect(config.paths.database)) as conn:
        clear_scenario_leave(conn)
        report = check_roster(load_check_inputs(conn), assignments, uncovered, config, reported=summary)
    report.issues.to_csv(out / "roster_check_issues.csv", index=False)
    report.crew_hours.to_csv(out / "crew_hours.csv", index_label="crew_id")
    for row in report.issues[report.issues.severity != "INFO"].head(20).itertuples():
        log.warning("[%s] %s %s: %s", row.severity, row.check, row.entity, row.detail)
    return 1 if not report.errors.empty else 0


def cmd_scenarios(config, args) -> int:
    names = args.only.split(",") if args.only else None
    with closing(connect(config.paths.database)) as conn:
        _outcomes, table = run_scenarios(conn, config, names, out_root=config.paths.output_dir / "scenarios")
    cols = ["scenario", "status", "total_cost_gbp", "total_cost_delta_pct", "uncovered_seats",
            "mean_abs_deviation_hours", "max_abs_deviation_hours", "solve_seconds", "check_errors"]
    log.info("Scenario comparison:\n%s", table[cols].to_string(index=False))
    return 1 if (table["check_errors"] > 0).any() else 0


COMMANDS = {
    "generate": (cmd_generate, "Generate synthetic raw CSVs"),
    "prepare": (cmd_prepare, "Clean, transform and validate raw data into SQLite"),
    "solve": (cmd_solve, "Build and solve the roster"),
    "diagnose": (cmd_diagnose, "Make one pool infeasible with a sickness wave and explain why"),
    "check": (cmd_check, "Independently re check outputs/roster against the rules"),
    "scenarios": (cmd_scenarios, "Run scenarios from config/scenarios.toml and compare to baseline"),
}


def _add_arguments(name: str, sub: argparse.ArgumentParser) -> None:
    if name == "solve":
        sub.add_argument("--mode", choices=["cost", "fairness", "weighted"])
        sub.add_argument("--strict", action="store_true", help="No uncovered seats allowed")
        sub.add_argument("--no-decompose", action="store_true", help="Solve all pools as one model")
    elif name == "diagnose":
        sub.add_argument("--pool", default="EDI-B737-CPT")
        sub.add_argument("--count", type=int, default=3)
        sub.add_argument("--days", default="8-14", help="Inclusive day index range, e.g. 8-14")
        sub.add_argument("--time-limit", type=float, default=60)
    elif name == "scenarios":
        sub.add_argument("--only", help="Comma separated scenario names (baseline always runs)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crew_roster", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="Path to a TOML config (default config/default.toml)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, (_fn, help_text) in COMMANDS.items():
        _add_arguments(name, sub.add_parser(name, help=help_text))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    setup_logging(config.log_level, config.paths.output_dir / "logs" / "crew_roster.log")
    fn, _ = COMMANDS[args.command]
    return fn(config, args)


if __name__ == "__main__":
    sys.exit(main())
