"""The crew rostering Mixed-Integer Program.

Plain language version (full maths in docs/02_model_formulation.md):

Decisions
  x[c,d]  1 if pilot c flies duty d, else 0. Only created for eligible pairs.
  u[d,r]  number of rank r seats on duty d left unfilled (elastic mode only).

Constraints, grouped into families so diagnostics can switch them off
  coverage     every seat is filled exactly once (or counted in u)
  rest         no two duties closer than the required rest
  daily_hours  duty hours starting on one calendar day stay under the cap
  hours_7d     duty hours in any 7 consecutive days stay under the cap
  days_7d      duty days in any 7 consecutive days stay under the cap
  hours_month  duty hours in the period stay under the cap

Objective (by mode)
  cost       pay + overtime premium + uncovered penalty
  fairness   deviation from each pilot's fair share of hours + uncovered penalty
  weighted   cost + fairness
  feasibility  uncovered seats only, used by diagnostics
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pulp

from crew_roster.config import Config
from crew_roster.model.data import ModelData

log = logging.getLogger(__name__)

FAMILIES = ("rest", "daily_hours", "hours_7d", "days_7d", "hours_month")
WINDOW_DAYS = 7
FAIRNESS_TIE_BREAK = 1e-3  # in fairness mode, cost only breaks ties


@dataclass
class RosterModel:
    prob: pulp.LpProblem
    data: ModelData
    config: Config
    mode: str
    families: tuple[str, ...]
    x: dict[tuple[str, str], pulp.LpVariable]
    uncovered: dict[tuple[str, str], pulp.LpVariable]
    hours: dict[str, pulp.LpAffineExpression]
    overtime: dict[str, pulp.LpVariable] = field(default_factory=dict)
    dev_pos: dict[str, pulp.LpVariable] = field(default_factory=dict)
    dev_neg: dict[str, pulp.LpVariable] = field(default_factory=dict)
    pool_max_dev: dict[str, pulp.LpVariable] = field(default_factory=dict)
    targets: dict[str, float] = field(default_factory=dict)
    overtime_thresholds: dict[str, float] = field(default_factory=dict)
    terms: dict[str, pulp.LpAffineExpression] = field(default_factory=dict)
    constraint_counts: dict[str, int] = field(default_factory=dict)
    build_seconds: float = 0.0


def rest_cliques(report_h: np.ndarray, release_h: np.ndarray, min_rest_h: float,
                 rest_at_least_duty: bool) -> list[tuple[int, ...]]:
    """Groups of duties where at most one can be flown by the same pilot.

    Stretch each duty to [report, release + required rest). Two duties clash
    exactly when their stretched intervals overlap. For intervals, every set
    that shares a common point clashes pairwise (a clique), and every clashing
    pair shares the start point of the later duty. So one constraint per
    distinct start time, "sum of x over duties covering this start <= 1",
    is exact and much tighter than one constraint per pair.
    """
    duty_len = release_h - report_h
    rest = np.maximum(min_rest_h, duty_len) if rest_at_least_duty else np.full_like(duty_len, min_rest_h)
    stretched_end = release_h + rest
    seen: set[tuple[int, ...]] = set()
    cliques = []
    for anchor in np.unique(report_h):
        members = tuple(np.flatnonzero((report_h <= anchor) & (stretched_end > anchor)).tolist())
        if len(members) >= 2 and members not in seen:
            seen.add(members)
            cliques.append(members)
    # Drop cliques contained in a bigger one; they add rows but no strength.
    cliques.sort(key=len, reverse=True)
    kept: list[set[int]] = []
    result = []
    for clique in cliques:
        s = set(clique)
        if not any(s <= k for k in kept):
            kept.append(s)
            result.append(clique)
    return result


def _hours_since_epoch(ts: pd.Series) -> np.ndarray:
    return (ts - pd.Timestamp("2000-01-01")).dt.total_seconds().to_numpy() / 3600.0


def build_model(data: ModelData, config: Config, mode: str | None = None,
                families: tuple[str, ...] = FAMILIES, elastic: bool | None = None,
                name: str = "crew_roster") -> RosterModel:
    start = time.perf_counter()
    rules, pay, obj = config.rules, config.pay, config.objective
    mode = mode or obj.mode
    elastic = config.solver.elastic_coverage if elastic is None else elastic
    unknown = set(families) - set(FAMILIES)
    if unknown:
        raise ValueError(f"Unknown constraint families {unknown}")

    prob = pulp.LpProblem(name, pulp.LpMinimize)
    counts: dict[str, int] = defaultdict(int)
    duties, crew = data.duties, data.crew

    def add(constraint, family: str, label: str) -> None:
        prob.addConstraint(constraint, name=f"{family}_{label}")
        counts[family] += 1

    # Decision variables -------------------------------------------------------
    x = {
        (c, d): pulp.LpVariable(f"x_{c}_{d}", cat=pulp.LpBinary)
        for c, d in data.eligibility[["crew_id", "duty_id"]].itertuples(index=False)
    }
    by_crew: dict[str, list[str]] = defaultdict(list)
    by_seat: dict[tuple[str, str], list[str]] = defaultdict(list)
    for c, d in x:
        by_crew[c].append(d)
        by_seat[(d, crew.at[c, "rank"])].append(c)

    # Coverage: every seat filled exactly once, or counted as uncovered ---------
    seats = data.seats
    uncovered = {}
    for s in seats.itertuples(index=False):
        u = pulp.LpVariable(f"u_{s.duty_id}_{s.rank}", lowBound=0, upBound=s.required if elastic else 0)
        uncovered[(s.duty_id, s.rank)] = u
        add(pulp.lpSum(x[(c, s.duty_id)] for c in by_seat[(s.duty_id, s.rank)]) + u == s.required,
            "coverage", f"{s.duty_id}_{s.rank}")

    # Per pilot rules ----------------------------------------------------------
    hours_expr: dict[str, pulp.LpAffineExpression] = {}
    for c in crew.index:
        ids = sorted(by_crew.get(c, []), key=lambda d: duties.at[d, "report_utc"])
        dh = duties.loc[ids, "duty_hours"].to_numpy() if ids else np.array([])
        xs = [x[(c, d)] for d in ids]
        hours_expr[c] = pulp.lpSum(h * v for h, v in zip(dh, xs))
        if not ids:
            continue
        day = duties.loc[ids, "day_index"].to_numpy()

        cliques: list[tuple[int, ...]] = []
        if "rest" in families:
            report_h = _hours_since_epoch(duties.loc[ids, "report_utc"])
            release_h = _hours_since_epoch(duties.loc[ids, "release_utc"])
            cliques = rest_cliques(report_h, release_h, rules.min_rest_hours, rules.rest_at_least_previous_duty)
            for k, clique in enumerate(cliques):
                add(pulp.lpSum(xs[i] for i in clique) <= 1, "rest", f"{c}_{k}")

        if "daily_hours" in families:
            clique_sets = [set(q) for q in cliques]
            for t in np.unique(day):
                idx = np.flatnonzero(day == t)
                if dh[idx].sum() <= rules.max_duty_hours_day:
                    continue  # can never bind
                if len(idx) <= 1 or any(set(idx.tolist()) <= q for q in clique_sets):
                    # Rest already allows at most one of these duties, and each
                    # duty is under the daily cap (validated in Phase 1). Redundant.
                    continue
                add(pulp.lpSum(dh[i] * xs[i] for i in idx) <= rules.max_duty_hours_day,
                    "daily_hours", f"{c}_{t}")

        last_window = max(0, data.period_days - WINDOW_DAYS)
        for t in range(last_window + 1):
            idx = np.flatnonzero((day >= t) & (day < t + WINDOW_DAYS))
            if "hours_7d" in families and dh[idx].sum() > rules.max_duty_hours_7d:
                add(pulp.lpSum(dh[i] * xs[i] for i in idx) <= rules.max_duty_hours_7d, "hours_7d", f"{c}_{t}")
            # Counts duties, not distinct days. Exact while no pilot can start two
            # duties on one day; the output checker verifies that assumption.
            if "days_7d" in families and len(idx) > rules.max_duty_days_7d:
                add(pulp.lpSum(xs[i] for i in idx) <= rules.max_duty_days_7d, "days_7d", f"{c}_{t}")

        if "hours_month" in families and dh.sum() > rules.max_duty_hours_month:
            add(hours_expr[c] <= rules.max_duty_hours_month, "hours_month", c)

    model = RosterModel(prob, data, config, mode, tuple(families), x, uncovered, hours_expr)
    uncovered_seats = pulp.lpSum(uncovered.values())
    model.terms["uncovered_seats"] = uncovered_seats

    if mode == "feasibility":
        prob.setObjective(uncovered_seats)
        return _finish(model, counts, start)

    # Pay and overtime ---------------------------------------------------------
    scale = data.period_days / 31.0
    for c, row in crew.iterrows():
        threshold = pay.overtime_threshold_hours * row.fte * scale
        model.overtime_thresholds[c] = threshold
        o = pulp.LpVariable(f"overtime_{c}", lowBound=0)
        model.overtime[c] = o
        add(o >= hours_expr[c] - threshold, "overtime_link", c)
    model.terms["pay_cost"] = pulp.lpSum(crew.at[c, "hourly_rate_gbp"] * hours_expr[c] for c in crew.index)
    model.terms["overtime_cost"] = pulp.lpSum(
        pay.overtime_premium * crew.at[c, "hourly_rate_gbp"] * model.overtime[c] for c in crew.index)

    # Fairness: each pilot's fair share of their pool's hours ---------------------
    pool_hours = (seats["duty_hours"] * seats["required"]).groupby(seats["pool_id"]).sum()
    weight = crew["fte"] * crew["available_days"]
    for pool_id, members in crew.groupby("pool_id"):
        total_weight = weight[members.index].sum()
        m = pulp.LpVariable(f"maxdev_{pool_id}", lowBound=0)
        model.pool_max_dev[pool_id] = m
        for c in members.index:
            target = float(pool_hours.get(pool_id, 0.0) * weight[c] / total_weight) if total_weight > 0 else 0.0
            model.targets[c] = target
            p = pulp.LpVariable(f"devpos_{c}", lowBound=0)
            n = pulp.LpVariable(f"devneg_{c}", lowBound=0)
            model.dev_pos[c], model.dev_neg[c] = p, n
            if obj.fairness_tolerance_hours > 0:
                # Free slack inside the band. Duties are ~8 hour blocks, so no
                # integer roster hits a target like 93.4 hours exactly. Charging
                # for that unavoidable remainder weakens the LP bound and makes
                # the gap very slow to close.
                band = pulp.LpVariable(f"band_{c}", lowBound=-obj.fairness_tolerance_hours,
                                       upBound=obj.fairness_tolerance_hours)
                add(hours_expr[c] - target == p - n + band, "fairness_link", c)
            else:
                add(hours_expr[c] - target == p - n, "fairness_link", c)
            add(m >= p + n, "fairness_link", f"max_{c}")
    model.terms["fairness_total_dev_hours"] = pulp.lpSum(model.dev_pos[c] + model.dev_neg[c] for c in crew.index)
    model.terms["fairness_max_dev_hours"] = pulp.lpSum(model.pool_max_dev.values())

    cost = model.terms["pay_cost"] + model.terms["overtime_cost"]
    fairness = (obj.fairness_weight_total * model.terms["fairness_total_dev_hours"]
                + obj.fairness_weight_max * model.terms["fairness_max_dev_hours"])
    penalty = obj.uncovered_penalty * uncovered_seats
    if mode == "cost":
        prob.setObjective(cost + penalty)
    elif mode == "fairness":
        prob.setObjective(fairness + penalty + FAIRNESS_TIE_BREAK * cost)
    else:
        prob.setObjective(cost + fairness + penalty)
    return _finish(model, counts, start)


def _finish(model: RosterModel, counts: dict[str, int], start: float) -> RosterModel:
    model.constraint_counts = dict(counts)
    model.build_seconds = time.perf_counter() - start
    log.info("Built %s model (%s): %d variables, %d constraints %s in %.1fs",
             model.mode, "+".join(model.families) or "coverage only", len(model.prob.variables()),
             len(model.prob.constraints), model.constraint_counts, model.build_seconds)
    return model
