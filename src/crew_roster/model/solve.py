"""Run CBC and turn its answer into something a planner can trust.

A solver returns more than a roster. It tells you whether the roster is
proven optimal, merely feasible (stopped on the time limit), or impossible,
and how far from the best possible answer it might be (the gap).
"""

from __future__ import annotations

import logging
import re
import tempfile
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pulp

from crew_roster.model.formulation import RosterModel

log = logging.getLogger(__name__)

OPTIMAL = "OPTIMAL"                  # proven best within the gap tolerance
FEASIBLE = "FEASIBLE"                # a valid roster, but the time limit hit before proof
INFEASIBLE = "INFEASIBLE"            # no roster satisfies the hard constraints
NO_SOLUTION = "NO_SOLUTION_FOUND"    # time ran out before any valid roster was found
UNBOUNDED = "UNBOUNDED"              # a modelling bug for this problem

_CBC_PATTERNS = {
    "result": re.compile(r"^Result - (.+)$", re.M),
    "objective": re.compile(r"^Objective value:\s+(-?[\d.eE+-]+)", re.M),
    "lower_bound": re.compile(r"^Lower bound:\s+(-?[\d.eE+-]+)", re.M),
    "gap": re.compile(r"^Gap:\s+(-?[\d.eE+-]+)", re.M),
    "nodes": re.compile(r"^Enumerated nodes:\s+(\d+)", re.M),
    "iterations": re.compile(r"^Total iterations:\s+(\d+)", re.M),
    "cbc_wallclock": re.compile(r"^Time \(Wallclock seconds\):\s+([\d.]+)", re.M),
    "root_bound": re.compile(r"Continuous objective value is\s+(-?[\d.eE+-]+)", re.M),
}


@dataclass
class SolveResult:
    status: str
    cbc_result: str | None
    objective: float | None
    best_bound: float | None
    gap: float | None
    build_seconds: float
    solve_seconds: float
    n_variables: int
    n_binaries: int
    n_constraints: int
    constraint_counts: dict[str, int]
    nodes: int | None = None
    iterations: int | None = None
    time_limit_hit: bool = False
    assignments: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=["crew_id", "duty_id"]))
    uncovered: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=["duty_id", "rank", "seats"]))
    components: dict[str, float] = field(default_factory=dict)
    log_text: str = ""
    wall_seconds: float | None = None
    pool_results: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def has_roster(self) -> bool:
        return self.status in (OPTIMAL, FEASIBLE)

    def summary(self) -> dict:
        return {
            "status": self.status,
            "cbc_result": self.cbc_result,
            "objective": self.objective,
            "best_bound": self.best_bound,
            "gap": self.gap,
            "build_seconds": round(self.build_seconds, 3),
            "solve_seconds": round(self.solve_seconds, 3),
            "wall_seconds": round(self.wall_seconds if self.wall_seconds is not None else self.build_seconds + self.solve_seconds, 3),
            "n_variables": self.n_variables,
            "n_binaries": self.n_binaries,
            "n_constraints": self.n_constraints,
            "nodes": self.nodes,
            "time_limit_hit": self.time_limit_hit,
            **{f"constraints_{k}": v for k, v in self.constraint_counts.items()},
            **self.components,
        }


def parse_cbc_log(text: str) -> dict:
    out: dict = {}
    for key, pattern in _CBC_PATTERNS.items():
        matches = pattern.findall(text)
        if matches:
            value = matches[-1].strip()
            out[key] = value if key == "result" else float(value)
    return out


def _value(expr) -> float:
    v = pulp.value(expr)
    return float(v) if v is not None else 0.0


def solve(model: RosterModel, time_limit: float | None = None, gap_rel: float | None = None,
          log_dir: Path | None = None, write_lp: Path | None = None) -> SolveResult:
    cfg = model.config.solver
    time_limit = cfg.time_limit_seconds if time_limit is None else time_limit
    gap_rel = cfg.gap_rel if gap_rel is None else gap_rel

    if write_lp is not None:
        write_lp.parent.mkdir(parents=True, exist_ok=True)
        model.prob.writeLP(str(write_lp))

    log_dir = log_dir or Path(tempfile.gettempdir())
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"cbc_{model.prob.name}_{int(time.time() * 1000)}.log"
    with warnings.catch_warnings():
        # PULP_CBC_CMD is flagged for removal in PuLP 4; pinned to 3.3.2 in pyproject.
        warnings.simplefilter("ignore", DeprecationWarning)
        # No threads argument: the bundled CBC 2.10.3 crashed or hung with it on Windows.
        solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit, gapRel=gap_rel, logPath=str(log_path))

    started = time.perf_counter()
    model.prob.solve(solver)
    solve_seconds = time.perf_counter() - started

    log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
    parsed = parse_cbc_log(log_text)
    cbc_result = parsed.get("result")
    time_limit_hit = bool(cbc_result and "time" in cbc_result.lower())

    sol_status = model.prob.sol_status
    if sol_status == pulp.LpSolutionOptimal:
        status = OPTIMAL
    elif sol_status == pulp.LpSolutionIntegerFeasible:
        status = FEASIBLE
    elif sol_status == pulp.LpSolutionInfeasible or model.prob.status == pulp.LpStatusInfeasible:
        status = INFEASIBLE
    elif sol_status == pulp.LpSolutionUnbounded:
        status = UNBOUNDED
    else:
        status = NO_SOLUTION
    # CBC can report "Optimal" to PuLP when it stopped on time with an incumbent.
    if status == OPTIMAL and time_limit_hit:
        status = FEASIBLE

    variables = model.prob.variables()
    result = SolveResult(
        status=status,
        cbc_result=cbc_result,
        objective=_value(model.prob.objective) if status in (OPTIMAL, FEASIBLE) else None,
        best_bound=parsed.get("lower_bound"),
        gap=parsed.get("gap"),
        build_seconds=model.build_seconds,
        solve_seconds=solve_seconds,
        n_variables=len(variables),
        n_binaries=sum(1 for v in variables if v.cat == pulp.LpBinary or (v.cat == pulp.LpInteger and v.lowBound == 0 and v.upBound == 1)),
        n_constraints=len(model.prob.constraints),
        constraint_counts=model.constraint_counts,
        nodes=int(parsed["nodes"]) if "nodes" in parsed else None,
        iterations=int(parsed["iterations"]) if "iterations" in parsed else None,
        time_limit_hit=time_limit_hit,
        log_text=log_text,
    )
    if result.has_roster:
        if result.best_bound is None and status == OPTIMAL:
            result.best_bound = result.objective
        # CBC prints the gap rounded to 2 decimals, so 0.4% shows as "0.00".
        # Recompute it from the objective and the proven lower bound.
        if result.best_bound is not None and result.objective is not None:
            result.gap = max(0.0, result.objective - result.best_bound) / max(abs(result.objective), 1e-9)
        _extract(model, result)
    try:
        log_path.unlink()
    except OSError:
        pass
    log.info("Solve %s in %.1fs, objective %s, gap %s, CBC: %s", status, solve_seconds,
             f"{result.objective:,.0f}" if result.objective is not None else "n/a",
             f"{result.gap:.4%}" if result.gap is not None else "n/a", cbc_result)
    return result


def _extract(model: RosterModel, result: SolveResult) -> None:
    result.assignments = pd.DataFrame(
        [(c, d) for (c, d), v in model.x.items() if v.varValue is not None and v.varValue > 0.5],
        columns=["crew_id", "duty_id"],
    )
    result.uncovered = pd.DataFrame(
        [(d, r, int(round(v.varValue))) for (d, r), v in model.uncovered.items()
         if v.varValue is not None and v.varValue > 0.5],
        columns=["duty_id", "rank", "seats"],
    )
    result.components = {name: round(_value(expr), 4) for name, expr in model.terms.items()}
