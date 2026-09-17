"""Raw CSV to clean, solver ready SQLite tables.

Stages:
  1. Schema check in Python: fail fast if a column is missing or renamed.
  2. Load everything as TEXT into staging tables (sql/01_staging.sql).
  3. Normalise, quarantine bad rows, build duties (sql/02_clean.sql).
  4. Build calendar, eligibility and crew views (sql/03_model_inputs.sql).
  5. Dataset level validation gate (validate.py + sql/validation_checks.sql).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from crew_roster.config import PROJECT_ROOT, Config

log = logging.getLogger(__name__)

SQL_DIR = PROJECT_ROOT / "sql"

EXPECTED_COLUMNS: dict[str, list[str]] = {
    "bases": ["base_code", "base_name", "timezone"],
    "aircraft_types": ["type_code", "description", "captains_required", "first_officers_required"],
    "crew": ["crew_id", "rank", "base", "seniority_years", "hourly_rate_gbp", "fte"],
    "crew_qualifications": ["crew_id", "aircraft_type", "qualified_from", "licence_expiry"],
    "leave": ["leave_id", "crew_id", "leave_type", "start_date", "end_date"],
    "flights": ["flight_id", "flight_number", "duty_id", "leg_seq", "aircraft_type",
                "dep_airport", "arr_airport", "dep_time_utc", "arr_time_utc"],
}


class SchemaError(ValueError):
    """Raised when a raw file is missing or its columns do not match the contract."""


@dataclass
class PipelineResult:
    raw_rows: dict[str, int]
    clean_rows: dict[str, int]
    rejected: pd.DataFrame


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(db_path)


def run_sql_file(conn: sqlite3.Connection, path: Path) -> None:
    log.debug("Running %s", path.name)
    conn.executescript(path.read_text(encoding="utf-8"))


def load_raw(conn: sqlite3.Connection, raw_dir: Path) -> dict[str, int]:
    counts = {}
    for table, columns in EXPECTED_COLUMNS.items():
        path = raw_dir / f"{table}.csv"
        if not path.exists():
            raise SchemaError(f"Missing raw file {path}")
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise SchemaError(f"{path.name} is missing columns {missing}; found {list(df.columns)}")
        extra = [c for c in df.columns if c not in columns]
        if extra:
            log.warning("%s has unexpected columns %s, ignoring them", path.name, extra)
        placeholders = ",".join("?" for _ in columns)
        conn.executemany(
            f"INSERT INTO stg_{table} ({','.join(columns)}) VALUES ({placeholders})",
            df[columns].itertuples(index=False, name=None),
        )
        counts[table] = len(df)
    conn.commit()
    return counts


def write_run_params(conn: sqlite3.Connection, config: Config) -> None:
    conn.executescript("DROP TABLE IF EXISTS run_params;")
    conn.execute(
        """CREATE TABLE run_params (
            period_start TEXT, period_days INTEGER, report_minutes INTEGER, debrief_minutes INTEGER,
            max_duty_hours_day REAL, max_rejected_share REAL, overtime_threshold_hours REAL)"""
    )
    conn.execute(
        "INSERT INTO run_params VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            config.generation.period_start,
            config.generation.period_days,
            config.rules.report_minutes,
            config.rules.debrief_minutes,
            config.rules.max_duty_hours_day,
            config.pipeline.max_rejected_share,
            config.pay.overtime_threshold_hours,
        ),
    )
    conn.commit()


def run_pipeline(config: Config, conn: sqlite3.Connection | None = None) -> PipelineResult:
    own_conn = conn is None
    if own_conn:
        if config.paths.database.exists():
            config.paths.database.unlink()
        conn = connect(config.paths.database)
    try:
        run_sql_file(conn, SQL_DIR / "01_staging.sql")
        raw_rows = load_raw(conn, config.paths.raw_dir)
        write_run_params(conn, config)
        run_sql_file(conn, SQL_DIR / "02_clean.sql")
        run_sql_file(conn, SQL_DIR / "03_model_inputs.sql")
        clean_rows = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("crew_clean", "qualifications_clean", "leave_clean", "flights_clean", "duties")
        }
        rejected = pd.read_sql("SELECT * FROM rejected_rows", conn)
        log.info("Raw rows %s", raw_rows)
        log.info("Clean rows %s", clean_rows)
        if not rejected.empty:
            summary = rejected.groupby(["source_table", "rule"]).size()
            for (table, rule), n in summary.items():
                log.info("Quarantined %s %s row(s): %s", n, table, rule)
        return PipelineResult(raw_rows, clean_rows, rejected)
    finally:
        if own_conn:
            conn.close()
