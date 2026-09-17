"""Solve a whole month, optionally one crew pool at a time.

Why decomposition is exact here: a pool is pilots with the same base, rank
and type rating. Every constraint in the model touches either one pilot
(rest, hours caps, overtime) or one seat (coverage), and a seat can only be
filled from one pool. Fairness is measured inside a pool. So no constraint
links two pools, and the sum of the pool optima is the optimum of the whole.

Why it matters: smaller models give CBC tighter per pool time budgets, a
bound per pool, and a failure in one pool does not hide the rosters of the
others. Measured comparison in docs/measurements/.
"""

from __future__ import annotations

import logging
import time

import pandas as pd

from crew_roster.config import Config
from crew_roster.model.data import ModelData
from crew_roster.model.formulation import FAMILIES, build_model
from crew_roster.model.solve import FEASIBLE, INFEASIBLE, NO_SOLUTION, OPTIMAL, UNBOUNDED, SolveResult, solve

log = logging.getLogger(__name__)

_STATUS_RANK = {OPTIMAL: 0, FEASIBLE: 1, NO_SOLUTION: 2, INFEASIBLE: 3, UNBOUNDED: 4}


def pools_are_independent(data: ModelData) -> bool:
    """True when every seat can only be filled by crew from one pool."""
    elig = data.eligibility.merge(data.crew[["pool_id", "rank"]], left_on="crew_id", right_index=True)
    return bool((elig.groupby(["duty_id", "rank"])["pool_id"].nunique() <= 1).all())


def solve_roster(data: ModelData, config: Config, mode: str | None = None,
                 families: tuple[str, ...] = FAMILIES, elastic: bool | None = None,
                 decompose: bool | None = None, time_limit: float | None = None) -> SolveResult:
    decompose = config.solver.decompose_by_pool if decompose is None else decompose
    if decompose and not pools_are_independent(data):
        log.warning("Some seats can be filled from more than one pool; solving as one model")
        decompose = False
    if not decompose:
        model = build_model(data, config, mode=mode, families=families, elastic=elastic)
        result = solve(model, time_limit=time_limit)
        result.pool_results = pd.DataFrame([{"pool_id": "ALL", **result.summary()}])
        return result

    wall_start = time.perf_counter()
    parts: list[tuple[str, SolveResult]] = []
    for pool_id in sorted(data.crew["pool_id"].unique()):
        subset = data.subset_pools({pool_id})
        model = build_model(subset, config, mode=mode, families=families, elastic=elastic, name=f"pool_{pool_id}")
        res = solve(model, time_limit=time_limit)
        log.info("Pool %-14s %s in %.1fs", pool_id, res.status, res.solve_seconds)
        parts.append((pool_id, res))
    return combine(parts, time.perf_counter() - wall_start)


def combine(parts: list[tuple[str, SolveResult]], wall_seconds: float) -> SolveResult:
    results = [r for _, r in parts]
    status = max((r.status for r in results), key=lambda s: _STATUS_RANK[s])
    with_roster = [r for r in results if r.has_roster]
    objective = sum(r.objective for r in with_roster) if status in (OPTIMAL, FEASIBLE) else None
    bounds = [r.best_bound for r in with_roster]
    best_bound = sum(bounds) if objective is not None and all(b is not None for b in bounds) else None
    gap = None
    if objective is not None and best_bound is not None:
        gap = max(0.0, objective - best_bound) / max(abs(objective), 1e-9)

    counts: dict[str, int] = {}
    components: dict[str, float] = {}
    for r in results:
        for k, v in r.constraint_counts.items():
            counts[k] = counts.get(k, 0) + v
    for r in with_roster:
        for k, v in r.components.items():
            components[k] = round(components.get(k, 0.0) + v, 4)

    combined = SolveResult(
        status=status,
        cbc_result=f"{len(parts)} pool models",
        objective=objective,
        best_bound=best_bound,
        gap=gap,
        build_seconds=sum(r.build_seconds for r in results),
        solve_seconds=sum(r.solve_seconds for r in results),
        n_variables=sum(r.n_variables for r in results),
        n_binaries=sum(r.n_binaries for r in results),
        n_constraints=sum(r.n_constraints for r in results),
        constraint_counts=counts,
        nodes=sum(r.nodes or 0 for r in results),
        time_limit_hit=any(r.time_limit_hit for r in results),
        components=components,
        wall_seconds=wall_seconds,
        pool_results=pd.DataFrame([{"pool_id": p, **r.summary()} for p, r in parts]),
    )
    # Keep the rosters of pools that solved, even if another pool did not, so a
    # planner can see the partial picture. status still reports the worst pool.
    if with_roster:
        combined.assignments = pd.concat([r.assignments for r in with_roster], ignore_index=True)
        combined.uncovered = pd.concat([r.uncovered for r in with_roster], ignore_index=True)
    return combined
