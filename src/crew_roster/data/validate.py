"""Dataset level validation gate.

Checks live in sql/validation_checks.sql so an analyst can read and extend
them without touching Python. This module parses that file, runs each check
and decides whether the solver is allowed to run.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from crew_roster.data.pipeline import SQL_DIR

log = logging.getLogger(__name__)

SEVERITIES = ("ERROR", "WARNING", "INFO")
HEADER = re.compile(r"^--\s*(name|severity|description):\s*(.+)$")


@dataclass(frozen=True)
class Check:
    name: str
    severity: str
    description: str
    sql: str


class ValidationFailed(RuntimeError):
    def __init__(self, issues: pd.DataFrame):
        errors = issues[issues["severity"] == "ERROR"]
        names = ", ".join(sorted(errors["check"].unique()))
        super().__init__(f"{len(errors)} blocking validation error(s): {names}")
        self.issues = issues


def parse_checks(path: Path = SQL_DIR / "validation_checks.sql") -> list[Check]:
    checks: list[Check] = []
    meta: dict[str, str] = {}
    body: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = HEADER.match(line.strip())
        if match:
            meta[match.group(1)] = match.group(2).strip()
            continue
        if "name" in meta and not line.strip().startswith("--"):
            body.append(line)
            if line.rstrip().endswith(";"):
                severity = meta.get("severity", "").upper()
                if severity not in SEVERITIES:
                    raise ValueError(f"Check {meta['name']} has invalid severity {severity!r}")
                checks.append(Check(meta["name"], severity, meta.get("description", ""), "\n".join(body)))
                meta, body = {}, []
    names = [c.name for c in checks]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate check names in validation file")
    return checks


def run_validation(conn: sqlite3.Connection, checks: list[Check] | None = None) -> pd.DataFrame:
    """Run every check. Returns one row per issue and stores it in validation_issues."""
    checks = checks if checks is not None else parse_checks()
    frames = []
    for check in checks:
        rows = conn.execute(check.sql).fetchall()
        level = {"ERROR": logging.ERROR, "WARNING": logging.WARNING}.get(check.severity, logging.INFO)
        if rows and check.severity != "INFO":
            log.log(level, "[%s] %s: %d issue(s), e.g. %s: %s", check.severity, check.name, len(rows), *rows[0])
        elif not rows:
            log.debug("[PASS] %s", check.name)
        if rows:
            frames.append(pd.DataFrame(rows, columns=["entity", "detail"]).assign(
                stage="input", check=check.name, severity=check.severity))
    columns = ["stage", "check", "severity", "entity", "detail"]
    issues = pd.concat(frames, ignore_index=True)[columns] if frames else pd.DataFrame(columns=columns)
    issues.to_sql("validation_issues", conn, if_exists="replace", index=False)
    counts = issues["severity"].value_counts().to_dict()
    log.info("Validation: %d checks, %s", len(checks), counts or "no issues")
    return issues


def assert_valid(issues: pd.DataFrame) -> None:
    if (issues["severity"] == "ERROR").any():
        raise ValidationFailed(issues)
