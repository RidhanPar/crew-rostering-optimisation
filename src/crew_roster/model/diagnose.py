"""Why is the model infeasible?

"Infeasible" only tells you that no roster satisfies every hard rule at once.
A planner needs to know which duties, which crew pool, and which rules clash.
Three tools, cheapest first:

1. Capacity prechecks (seconds, no solver). Necessary conditions such as
   "a pool needs 21 duty days this week but its pilots can give at most 15".
   If one fails, the model is certainly infeasible and you know where.
   Passing them does not prove feasibility; rules interact.

2. Elastic solve. Let every seat go unfilled at a price and minimise unfilled
   seats. The answer names the exact duties that cannot be covered, and which
   pools they belong to.

3. Conflict set search over constraint families (a deletion filter). Start
   with all rule families and try removing each one. If the model is still
   infeasible without it, that family is not needed to explain the conflict,
   so drop it for good. What remains is an irreducible set: remove any one of
   them and the pool becomes feasible. This is the family level version of an
   IIS (Irreducible Infeasible Subsystem). Commercial solvers such as Gurobi
   compute a row level IIS directly; CBC does not, so we do it by re solving.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from crew_roster.config import Config
from crew_roster.model.data import ModelData
from crew_roster.model.formulation import FAMILIES, build_model
from crew_roster.model.solve import INFEASIBLE, OPTIMAL, FEASIBLE, solve

log = logging.getLogger(__name__)


@dataclass
class Diagnosis:
    precheck: pd.DataFrame
    uncovered: pd.DataFrame
    problem_pools: list[str]
    single_relaxations: pd.DataFrame = field(default_factory=pd.DataFrame)
    conflict_sets: dict[str, list[str] | None] = field(default_factory=dict)

    def summary_lines(self) -> list[str]:
        lines = [f"Capacity precheck failures: {len(self.precheck)}"]
        for row in self.precheck.head(10).itertuples(index=False):
            lines.append(f"  {row.check} {row.pool_id} {row.window}: {row.detail}")
        lines.append(f"Minimum uncovered seats: {int(self.uncovered['seats'].sum()) if not self.uncovered.empty else 0}")
        for pool in self.problem_pools:
            lines.append(f"Pool {pool}: irreducible conflicting rule families = {self.conflict_sets.get(pool)}")
        return lines


def _seat_pool(data: ModelData) -> pd.DataFrame:
    seats = data.seats.merge(data.duties[["day_index"]], left_on="duty_id", right_index=True)
    return seats


def capacity_precheck(data: ModelData, config: Config) -> pd.DataFrame:
    """Necessary conditions per pool. Any row returned proves infeasibility in strict mode."""
    rules = config.rules
    seats = _seat_pool(data)
    elig = data.eligibility.merge(data.crew[["pool_id", "rank"]], left_on="crew_id", right_index=True)
    elig = elig.merge(data.duties[["day_index", "duty_hours"]], left_on="duty_id", right_index=True)
    rows = []

    # A seat nobody is eligible for.
    covered = elig.groupby(["duty_id", "rank"]).size()
    for s in seats.itertuples(index=False):
        if (s.duty_id, s.rank) not in covered.index:
            rows.append(("seat_without_eligible_crew", s.pool_id, f"day {s.day_index}", 1, 0,
                         f"{s.duty_id} {s.rank}: nobody eligible"))

    # Assumes one duty per pilot per day (checked on every output roster).
    crew_days = elig[["crew_id", "pool_id", "day_index"]].drop_duplicates()
    for pool_id, pool_seats in seats.groupby("pool_id"):
        pool_days = crew_days[crew_days["pool_id"] == pool_id]
        demand_by_day = pool_seats.groupby("day_index")["required"].sum()
        supply_by_day = pool_days.groupby("day_index")["crew_id"].nunique()
        for day, demand in demand_by_day.items():
            supply = int(supply_by_day.get(day, 0))
            if demand > supply:
                rows.append(("daily_crew_shortage", pool_id, f"day {day}", int(demand), supply,
                             f"{demand} seats, {supply} pilots available"))
        last = max(0, data.period_days - 7)
        for t in range(last + 1):
            demand = int(pool_seats[(pool_seats.day_index >= t) & (pool_seats.day_index < t + 7)]["required"].sum())
            window = pool_days[(pool_days.day_index >= t) & (pool_days.day_index < t + 7)]
            supply = int(window.groupby("crew_id")["day_index"].nunique().clip(upper=rules.max_duty_days_7d).sum())
            if demand > supply:
                rows.append(("7_day_duty_day_shortage", pool_id, f"days {t}-{t + 6}", demand, supply,
                             f"{demand} seats, at most {supply} duty days under the {rules.max_duty_days_7d} in 7 rule"))
        pool_elig = elig[elig["pool_id"] == pool_id]
        demand_h = float((pool_seats["duty_hours"] * pool_seats["required"]).sum())
        supply_h = float(pool_elig.groupby("crew_id")["duty_hours"].sum().clip(upper=rules.max_duty_hours_month).sum())
        if demand_h > supply_h + 1e-6:
            rows.append(("month_hours_shortage", pool_id, "period", round(demand_h, 1), round(supply_h, 1),
                         f"{demand_h:.0f} hours needed, at most {supply_h:.0f} under the monthly cap"))
    return pd.DataFrame(rows, columns=["check", "pool_id", "window", "demand", "capacity", "detail"])


def min_uncovered(data: ModelData, config: Config, time_limit: float = 120) -> pd.DataFrame:
    """Elastic solve: the smallest set of seats that must go unfilled."""
    model = build_model(data, config, mode="feasibility", elastic=True, name="diagnose_elastic")
    result = solve(model, time_limit=time_limit, gap_rel=0.0)
    if not result.has_roster:
        raise RuntimeError(f"Elastic model should always be feasible, got {result.status}")
    seats = data.seats.set_index(["duty_id", "rank"])["pool_id"]
    out = result.uncovered.copy()
    out["pool_id"] = [seats.get((d, r)) for d, r in zip(out["duty_id"], out["rank"])]
    return out


def _strict_status(data: ModelData, config: Config, families: tuple[str, ...], time_limit: float) -> str:
    model = build_model(data, config, mode="feasibility", families=families, elastic=False, name="diagnose_strict")
    return solve(model, time_limit=time_limit, gap_rel=0.0).status


def conflict_set(data: ModelData, config: Config, time_limit: float = 60) -> tuple[list[str] | None, pd.DataFrame]:
    """Deletion filter over rule families. Returns (irreducible set or None if feasible, single relaxation table)."""
    full = _strict_status(data, config, FAMILIES, time_limit)
    singles = []
    for fam in FAMILIES:
        status = _strict_status(data, config, tuple(f for f in FAMILIES if f != fam), time_limit)
        singles.append({"removed_family": fam, "status": status,
                        "fixes_it": status in (OPTIMAL, FEASIBLE)})
    single_df = pd.DataFrame(singles)
    if full != INFEASIBLE:
        return None, single_df
    if _strict_status(data, config, (), time_limit) == INFEASIBLE:
        return [], single_df  # coverage alone is impossible: a seat with nobody eligible

    current = list(FAMILIES)
    for fam in FAMILIES:
        trial = tuple(f for f in current if f != fam)
        status = _strict_status(data, config, trial, time_limit)
        if status == INFEASIBLE:
            current = list(trial)       # still infeasible without it, so it is not part of the conflict
        elif status not in (OPTIMAL, FEASIBLE):
            log.warning("Could not decide feasibility without %s (%s); keeping it in the set", fam, status)
    return current, single_df


def diagnose(data: ModelData, config: Config, time_limit: float = 60) -> Diagnosis:
    precheck = capacity_precheck(data, config)
    log.info("Capacity precheck: %d necessary condition failure(s)", len(precheck))
    uncovered = min_uncovered(data, config, time_limit=time_limit * 2)
    pools = sorted(uncovered["pool_id"].dropna().unique()) if not uncovered.empty else []
    log.info("Elastic solve: %d seat(s) cannot be covered, pools %s",
             int(uncovered["seats"].sum()) if not uncovered.empty else 0, pools)
    diag = Diagnosis(precheck, uncovered, pools)
    tables = []
    for pool in pools:
        subset = data.subset_pools({pool})
        conflict, singles = conflict_set(subset, config, time_limit)
        diag.conflict_sets[pool] = conflict
        tables.append(singles.assign(pool_id=pool))
        log.info("Pool %s: irreducible rule families %s", pool, conflict)
    if tables:
        diag.single_relaxations = pd.concat(tables, ignore_index=True)
    return diag
