"""Extra unavailability for what if runs, without touching source data.

Rows go into the scenario_leave table, which the leave_all view unions with
real leave, so eligibility and availability pick them up automatically.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LeaveWave:
    pool_id: str          # for example "EDI-B737-CPT"
    count: int            # how many pilots in the pool go off
    start_day: int        # day index, 0 = first day of the period
    end_day: int          # inclusive
    leave_type: str = "SICK"

    @classmethod
    def from_dict(cls, d: dict) -> "LeaveWave":
        return cls(**d)


def clear_scenario_leave(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM scenario_leave")
    conn.commit()


def apply_leave_waves(conn: sqlite3.Connection, waves: list[LeaveWave]) -> list[str]:
    """Replace scenario leave with these waves.

    Takes the pilots with the most available days (ties by crew id), so the wave
    removes real capacity and runs are repeatable.
    """
    clear_scenario_leave(conn)
    period_start = date.fromisoformat(conn.execute("SELECT period_start FROM run_params").fetchone()[0])
    affected = []
    for n, wave in enumerate(waves):
        crew = [r[0] for r in conn.execute(
            "SELECT crew_id FROM crew_model WHERE pool_id = ? ORDER BY available_days DESC, crew_id LIMIT ?", (wave.pool_id, wave.count))]
        if len(crew) < wave.count:
            raise ValueError(f"Pool {wave.pool_id} has only {len(crew)} pilots, wave asks for {wave.count}")
        start = period_start + timedelta(days=wave.start_day)
        end = period_start + timedelta(days=wave.end_day)
        conn.executemany(
            "INSERT INTO scenario_leave VALUES (?, ?, ?, ?, ?)",
            [(f"S{n}-{c}", c, wave.leave_type, str(start), str(end)) for c in crew],
        )
        affected.extend(crew)
        log.info("Scenario leave: %s %s off %s to %s (%s)", wave.count, wave.pool_id, start, end, ", ".join(crew))
    conn.commit()
    return affected
