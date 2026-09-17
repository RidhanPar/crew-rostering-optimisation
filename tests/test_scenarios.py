import pytest

from crew_roster.analysis.scenarios import Scenario, compare, load_scenarios, run_scenario
from crew_roster.data.scenario_leave import LeaveWave
from crew_roster.config import load_config
from tests.conftest import build_dataset, small_config


def test_scenario_file_is_valid_and_every_override_builds_a_config():
    scenarios = load_scenarios()
    base = load_config()
    assert scenarios[0].name == "baseline"
    for s in scenarios:
        base.with_overrides(s.overrides)  # raises on unknown keys


@pytest.fixture(scope="module")
def small(tmp_path_factory):
    cfg = small_config(tmp_path_factory.mktemp("scen"), dirty=False, solver={"time_limit_seconds": 30})
    conn = build_dataset(cfg)
    yield cfg, conn
    conn.close()


def test_tightening_a_rule_never_makes_the_roster_cheaper(small):
    cfg, conn = small
    loose = run_scenario(conn, cfg, Scenario("baseline", "rules", "12h rest"))
    tight = run_scenario(conn, cfg, Scenario("rest_16h", "rules", "16h rest", {"rules": {"min_rest_hours": 16.0}}))
    # Every 16h rest roster is also a legal 12h rest roster, so the tight
    # roster cannot beat the loose problem's proven lower bound.
    assert tight.result.objective >= loose.result.best_bound - 1e-6
    assert loose.kpis["check_errors"] == tight.kpis["check_errors"] == 0


def test_sickness_wave_is_applied_then_removed(small):
    cfg, conn = small
    captains = conn.execute("SELECT COUNT(*) FROM crew_model WHERE pool_id = 'MAN-A320-CPT'").fetchone()[0]
    wave = Scenario("sick", "disruption", "all but one captain off",
                    leave_waves=(LeaveWave("MAN-A320-CPT", captains - 1, 2, 5),))
    outcome = run_scenario(conn, cfg, wave)
    assert outcome.kpis["uncovered_seats"] > 0
    assert outcome.kpis["check_errors"] == 0
    assert conn.execute("SELECT COUNT(*) FROM scenario_leave").fetchone()[0] == 0


def test_comparison_reports_deltas_against_baseline(small):
    cfg, conn = small
    outcomes = [
        run_scenario(conn, cfg, Scenario("baseline", "rules", "")),
        run_scenario(conn, cfg, Scenario("cost_only", "objective", "", {"objective": {"mode": "cost"}})),
    ]
    table = compare(outcomes).set_index("scenario")
    assert table.at["baseline", "total_cost_delta_gbp"] == 0
    # Dropping fairness can only lower pay, within the 0.5% optimality gap.
    assert table.at["cost_only", "total_cost_gbp"] <= table.at["baseline", "total_cost_gbp"] * 1.005
    # And the weighted baseline should be at least as fair on the measure it optimises.
    assert (table.at["cost_only", "fairness_dev_outside_band_hours"]
            >= table.at["baseline", "fairness_dev_outside_band_hours"] - 1e-6)
