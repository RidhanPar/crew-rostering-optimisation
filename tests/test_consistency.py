import pandas as pd
import pytest

from crew_roster.analysis.consistency import check_roster, load_check_inputs
from crew_roster.model.data import load_model_data
from crew_roster.model.roster import solve_roster
from tests.conftest import build_dataset, small_config


@pytest.fixture(scope="module")
def solved(tmp_path_factory):
    cfg = small_config(tmp_path_factory.mktemp("check"), dirty=True, solver={"time_limit_seconds": 30})
    conn = build_dataset(cfg)
    result = solve_roster(load_model_data(conn), cfg)
    assert result.has_roster
    yield cfg, conn, result
    conn.close()


def _checks(report, severity="ERROR"):
    return set(report.issues.loc[report.issues.severity == severity, "check"])


def test_solver_roster_passes_every_check_and_reconciles(solved):
    cfg, conn, result = solved
    report = check_roster(load_check_inputs(conn), result.assignments, result.uncovered, cfg, result.components)
    assert report.errors.empty, report.errors.to_string()
    assert report.recomputed["pay_cost"] == pytest.approx(result.components["pay_cost"], rel=1e-6)
    assert report.crew_hours["hours"].sum() == pytest.approx(
        report.crew_hours["target_hours"].sum(), rel=0.05)


def test_rest_violation_is_caught(solved):
    cfg, conn, result = solved
    inp = load_check_inputs(conn)
    d = inp.duties.sort_values("report_utc")
    late = d[d.report_utc.dt.hour >= 14].iloc[0]
    next_early = d[(d.report_utc > late.release_utc) & (d.report_utc < late.release_utc + pd.Timedelta(hours=10))].iloc[0]
    pilot = inp.crew[inp.crew["rank"] == "CPT"].index[0]
    roster = pd.DataFrame({"crew_id": [pilot, pilot], "duty_id": [late.name, next_early.name]})
    report = check_roster(inp, roster, pd.DataFrame(columns=["duty_id", "rank", "seats"]), cfg)
    assert "rest_violation" in _checks(report)


def test_pilot_on_leave_is_caught(solved):
    cfg, conn, result = solved
    crew_id, duty_id = result.assignments.iloc[0]
    day = conn.execute("SELECT duty_date FROM duties WHERE duty_id = ?", (duty_id,)).fetchone()[0]
    conn.execute("INSERT INTO scenario_leave VALUES ('T1', ?, 'SICK', ?, ?)", (crew_id, day, day))
    try:
        report = check_roster(load_check_inputs(conn), result.assignments, result.uncovered, cfg)
    finally:
        conn.execute("DELETE FROM scenario_leave")
    assert "rostered_on_leave" in _checks(report)


def test_double_covered_seat_is_caught(solved):
    cfg, conn, result = solved
    inp = load_check_inputs(conn)
    duty_id = result.assignments.iloc[0].duty_id
    captains = inp.crew[inp.crew["rank"] == "CPT"].index
    on_duty = set(result.assignments.loc[result.assignments.duty_id == duty_id, "crew_id"])
    extra = next(c for c in captains if c not in on_duty)
    roster = pd.concat([result.assignments, pd.DataFrame({"crew_id": [extra], "duty_id": [duty_id]})])
    assert "seat_overfilled" in _checks(check_roster(inp, roster, result.uncovered, cfg))


def test_dropped_assignment_breaks_coverage_reconciliation(solved):
    cfg, conn, result = solved
    report = check_roster(load_check_inputs(conn), result.assignments.iloc[1:], result.uncovered, cfg)
    assert "coverage_not_reconciled" in _checks(report)


def test_misreported_cost_is_caught(solved):
    cfg, conn, result = solved
    wrong = dict(result.components, pay_cost=result.components["pay_cost"] + 500)
    report = check_roster(load_check_inputs(conn), result.assignments, result.uncovered, cfg, wrong)
    assert "objective_not_reconciled" in _checks(report)


def test_model_with_a_looser_rule_than_the_checker_is_caught(solved):
    # Simulates a model bug: the roster was built with a 60h weekly cap, the law says 16h.
    cfg, conn, result = solved
    strict = cfg.with_overrides({"rules": {"max_duty_hours_7d": 16.0}})
    report = check_roster(load_check_inputs(conn), result.assignments, result.uncovered, strict)
    assert "hours_7d_exceeded" in _checks(report)


def test_two_duties_on_one_day_flags_the_model_assumption(solved):
    cfg, conn, result = solved
    inp = load_check_inputs(conn)
    first_day = inp.duties[inp.duties.day_index == 0].sort_values("report_utc")
    pilot = inp.crew[inp.crew["rank"] == "CPT"].index[0]
    roster = pd.DataFrame({"crew_id": [pilot, pilot], "duty_id": first_day.index[:2]})
    assert "model_assumption_one_duty_per_day" in _checks(check_roster(inp, roster, pd.DataFrame(columns=["duty_id", "rank", "seats"]), cfg))
