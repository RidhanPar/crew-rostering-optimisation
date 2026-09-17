import json

import pandas as pd
import pytest

from crew_roster.data.pipeline import SchemaError, connect, run_pipeline
from tests.conftest import build_dataset, small_config


def _manifest(config):
    return json.loads((config.paths.raw_dir / "generation_manifest.json").read_text())["injected_defects"]


def test_every_rejecting_defect_is_quarantined(dirty_config, dirty_db):
    rejected = pd.read_sql("SELECT * FROM rejected_rows", dirty_db)
    for defect in _manifest(dirty_config):
        if defect["expected"] == "duty_rejected":
            keys = rejected.loc[rejected.source_table == "duties", "record_key"]
            assert defect["key"] in set(keys), defect
        elif defect["expected"] == "rejected":
            table = defect["table"]
            hits = rejected[(rejected.source_table == table)
                            & ((rejected.record_key == defect["key"]) | (rejected.parent_key == defect["key"])
                               | rejected.record_key.str.startswith(defect["key"] + ":"))]
            assert not hits.empty, defect


def test_rejected_duties_never_reach_clean_tables(dirty_db):
    rejected = {r[0] for r in dirty_db.execute(
        "SELECT record_key FROM rejected_rows WHERE source_table = 'duties'")}
    duties = {r[0] for r in dirty_db.execute("SELECT duty_id FROM duties")}
    legs = {r[0] for r in dirty_db.execute("SELECT DISTINCT duty_id FROM flights_clean")}
    assert rejected and not (rejected & duties) and not (rejected & legs)


def test_normalisation_fixes_recoverable_defects(dirty_db):
    flights = pd.read_sql("SELECT * FROM flights_clean", dirty_db)
    assert flights["dep_airport"].str.fullmatch(r"[A-Z]{3}").all()
    assert flights["dep_time_utc"].str.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}").all()
    assert not flights.duplicated().any()
    crew = pd.read_sql("SELECT * FROM crew_clean", dirty_db)
    assert set(crew["rank"]) == {"CPT", "FO"}
    assert crew["crew_id"].is_unique


def test_duties_respect_daily_limit_and_chain(dirty_config, dirty_db):
    duties = pd.read_sql("SELECT * FROM duties", dirty_db)
    assert (duties["duty_hours"] <= dirty_config.rules.max_duty_hours_day).all()
    assert duties["day_index"].between(0, dirty_config.generation.period_days - 1).all()
    report = pd.to_datetime(duties["report_utc"])
    release = pd.to_datetime(duties["release_utc"])
    hours = (release - report).dt.total_seconds() / 3600
    assert (hours.round(3) == duties["duty_hours"].round(3)).all()


def test_eligibility_excludes_leave_and_expired_licences(dirty_db):
    bad = dirty_db.execute("""
        SELECT COUNT(*) FROM eligibility e
        JOIN duties d ON d.duty_id = e.duty_id
        JOIN leave_all l ON l.crew_id = e.crew_id
        WHERE d.report_utc < datetime(l.end_date, '+1 day') AND d.release_utc > datetime(l.start_date)
    """).fetchone()[0]
    assert bad == 0
    expired = dirty_db.execute("""
        SELECT COUNT(*) FROM eligibility e
        JOIN duties d ON d.duty_id = e.duty_id
        JOIN qualifications_clean q ON q.crew_id = e.crew_id AND q.aircraft_type = d.aircraft_type
        WHERE date(d.release_utc) > q.licence_expiry
    """).fetchone()[0]
    assert expired == 0


def test_missing_column_fails_fast(tmp_path):
    config = small_config(tmp_path, dirty=False)
    build_dataset(config).close()
    crew = pd.read_csv(config.paths.raw_dir / "crew.csv").drop(columns=["fte"])
    crew.to_csv(config.paths.raw_dir / "crew.csv", index=False)
    with pytest.raises(SchemaError, match="fte"):
        run_pipeline(config)


def test_clean_input_rejects_nothing(tmp_path):
    config = small_config(tmp_path, dirty=False)
    conn = build_dataset(config)
    assert conn.execute("SELECT COUNT(*) FROM rejected_rows").fetchone()[0] == 0
    conn.close()
