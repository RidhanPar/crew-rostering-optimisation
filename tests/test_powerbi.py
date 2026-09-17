import pandas as pd
import pytest

from crew_roster.analysis.scenarios import run_scenarios
from crew_roster.data.validate import run_validation
from crew_roster.reporting.powerbi import TABLES, export_powerbi
from tests.conftest import build_dataset, small_config


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    root = tmp_path_factory.mktemp("pbi")
    cfg = small_config(root, dirty=True, solver={"time_limit_seconds": 30})
    conn = build_dataset(cfg)
    run_validation(conn)
    run_scenarios(conn, cfg, ["cost_only"], out_root=root / "scenarios")
    export_powerbi(conn, root / "scenarios", root / "powerbi")
    conn.close()
    return {name: pd.read_csv(root / "powerbi" / f"{name}.csv") for name in TABLES}


def test_every_table_is_written_and_dimensions_have_unique_keys(exported):
    assert set(exported) == set(TABLES)
    for table, key in [("dim_crew", ["crew_id"]), ("dim_duty", ["duty_id"]), ("dim_date", ["date"]),
                       ("dim_scenario", ["scenario"])]:
        assert not exported[table].duplicated(key).any(), table


def test_fact_grains_are_unique(exported):
    for table, grain in [("fact_assignment", ["scenario", "crew_id", "duty_id"]),
                         ("fact_crew_period", ["scenario", "crew_id"]),
                         ("fact_crew_day", ["scenario", "crew_id", "date"]),
                         ("fact_seat_coverage", ["scenario", "duty_id", "rank"]),
                         ("fact_scenario_summary", ["scenario"])]:
        assert not exported[table].duplicated(grain).any(), table


def test_facts_only_reference_known_dimension_keys(exported):
    crew, duty, date = set(exported["dim_crew"].crew_id), set(exported["dim_duty"].duty_id), set(exported["dim_date"].date)
    for table in ("fact_assignment", "fact_crew_period", "fact_crew_day"):
        assert set(exported[table].crew_id) <= crew, table
    assert set(exported["fact_assignment"].duty_id) <= duty
    assert set(exported["fact_seat_coverage"].duty_id) <= duty
    assert set(exported["fact_crew_day"].date) <= date
    assert set(exported["fact_assignment"].duty_date) <= date


def test_coverage_and_hours_agree_across_tables(exported):
    for scenario in ("baseline", "cost_only"):
        cov = exported["fact_seat_coverage"].query("scenario == @scenario")
        asg = exported["fact_assignment"].query("scenario == @scenario")
        day = exported["fact_crew_day"].query("scenario == @scenario")
        per = exported["fact_crew_period"].query("scenario == @scenario")
        assert cov["assigned"].sum() == len(asg)
        assert (cov["uncovered"] >= 0).all()
        assert day["duty_hours"].sum() == pytest.approx(asg["duty_hours"].sum())
        assert per["hours"].sum() == pytest.approx(asg["duty_hours"].sum())
        assert set(day["day_status"]) <= {"DUTY", "LEAVE", "UNAVAILABLE", "OFF"}


def test_data_quality_includes_pipeline_rejections(exported):
    dq = exported["fact_data_quality"]
    assert (dq["stage"] == "cleaning").any()
    assert dq.loc[dq["stage"] == "output", "severity"].ne("ERROR").all()
