import itertools

import numpy as np
import pytest

from crew_roster.model.formulation import FAMILIES, build_model, rest_cliques
from crew_roster.model.roster import solve_roster
from crew_roster.model.solve import INFEASIBLE, OPTIMAL, parse_cbc_log, solve
from tests import micro


def roster_of(result) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for c, d in result.assignments.itertuples(index=False):
        out.setdefault(c, set()).add(d)
    return out


@pytest.mark.parametrize("seed", range(5))
def test_rest_cliques_cover_exactly_the_clashing_pairs(seed):
    rng = np.random.default_rng(seed)
    n = 40
    report = np.sort(rng.uniform(0, 24 * 10, n).round(1))
    release = report + rng.uniform(3, 12, n).round(1)
    min_rest = 12.0
    cliques = rest_cliques(report, release, min_rest, rest_at_least_duty=True)
    rest = np.maximum(min_rest, release - report)

    def clash(i, j):
        a, b = (i, j) if report[i] <= report[j] else (j, i)
        return report[b] < release[a] + rest[a]

    in_a_clique = {frozenset(p) for q in cliques for p in itertools.combinations(q, 2)}
    for i, j in itertools.combinations(range(n), 2):
        assert clash(i, j) == (frozenset((i, j)) in in_a_clique), (i, j)


def test_rest_rule_decides_who_flies_what():
    # D1 early, D2 same evening (6h rest), D3 next morning (7h after D2, 16h after D1).
    duties = [
        micro.duty("D1", "2026-10-01 06:00", "2026-10-01 14:00"),
        micro.duty("D2", "2026-10-01 20:00", "2026-10-01 23:00"),
        micro.duty("D3", "2026-10-02 06:00", "2026-10-02 14:00"),
    ]
    data = micro.data([micro.pilot("A", rate=100), micro.pilot("B", rate=200)], duties)
    cfg = micro.config(objective={"mode": "cost"}, pay={"overtime_threshold_hours": 500})
    result = solve(build_model(data, cfg))
    assert result.status == OPTIMAL
    assert roster_of(result) == {"A": {"D1", "D3"}, "B": {"D2"}}


def test_strict_mode_is_infeasible_and_elastic_mode_reports_the_gap():
    duties = [micro.duty("D1", "2026-10-01 06:00", "2026-10-01 14:00"),
              micro.duty("D2", "2026-10-01 18:00", "2026-10-01 22:00")]
    data = micro.data([micro.pilot("A")], duties)
    cfg = micro.config()
    assert solve(build_model(data, cfg, elastic=False)).status == INFEASIBLE
    elastic = solve(build_model(data, cfg, elastic=True))
    assert elastic.status == OPTIMAL
    assert elastic.uncovered["seats"].sum() == 1
    assert elastic.components["uncovered_seats"] == 1


def _four_day_duties():
    return [micro.duty(f"D{i}", f"2026-10-0{i} 06:00", f"2026-10-0{i} 14:00") for i in range(1, 5)]


def test_cost_mode_loads_the_cheaper_pilot_and_fairness_mode_balances():
    pilots = [micro.pilot("A", rate=100), micro.pilot("B", rate=101)]
    data = micro.data(pilots, _four_day_duties())
    cost = solve(build_model(data, micro.config(objective={"mode": "cost"}, pay={"overtime_threshold_hours": 500})))
    assert roster_of(cost) == {"A": {"D1", "D2", "D3", "D4"}}
    fair = solve(build_model(data, micro.config(objective={"mode": "fairness"})))
    counts = sorted(len(v) for v in roster_of(fair).values())
    assert counts == [2, 2]
    assert fair.components["fairness_total_dev_hours"] == pytest.approx(0, abs=1e-6)


def test_fair_target_is_prorated_by_availability():
    pilots = [micro.pilot("A", available_days=6), micro.pilot("B", available_days=2)]
    data = micro.data(pilots, _four_day_duties())
    model = build_model(data, micro.config(objective={"mode": "fairness"}))
    assert model.targets["A"] == pytest.approx(24.0)
    assert model.targets["B"] == pytest.approx(8.0)


def test_overtime_premium_changes_the_cheapest_choice():
    # Without overtime A is cheaper; with a low threshold A's extra hours cost 1.5x.
    pilots = [micro.pilot("A", rate=100), micro.pilot("B", rate=120)]
    data = micro.data(pilots, _four_day_duties())
    cfg = micro.config(objective={"mode": "cost"}, pay={"overtime_threshold_hours": 16 * 31 / 7, "overtime_premium": 0.5})
    result = solve(build_model(data, cfg))
    assert sorted(len(v) for v in roster_of(result).values()) == [2, 2]


@pytest.mark.parametrize("rule, value", [
    ("max_duty_hours_7d", 20.0),
    ("max_duty_days_7d", 2),
    ("max_duty_hours_month", 20.0),
])
def test_hours_and_days_caps_leave_one_duty_uncovered(rule, value):
    data = micro.data([micro.pilot("A")], _four_day_duties()[:3])
    cfg = micro.config(rules={rule: value})
    result = solve(build_model(data, cfg))
    assert result.status == OPTIMAL
    assert result.uncovered["seats"].sum() == 1
    assert len(result.assignments) == 2


def test_daily_cap_binds_when_rest_allows_two_duties():
    duties = [micro.duty("D1", "2026-10-01 00:30", "2026-10-01 04:30"),
              micro.duty("D2", "2026-10-01 17:00", "2026-10-01 23:00")]
    data = micro.data([micro.pilot("A")], duties)
    cfg = micro.config(rules={"max_duty_hours_day": 8.0, "min_rest_hours": 10.0})
    model = build_model(data, cfg)
    assert model.constraint_counts.get("daily_hours") == 1
    assert solve(model).uncovered["seats"].sum() == 1


def test_redundant_daily_constraints_are_skipped():
    duties = [micro.duty("D1", "2026-10-01 06:00", "2026-10-01 14:00"),
              micro.duty("D2", "2026-10-01 15:00", "2026-10-01 22:00")]
    model = build_model(micro.data([micro.pilot("A")], duties), micro.config())
    assert "daily_hours" not in model.constraint_counts


def test_constraint_families_can_be_switched_off():
    duties = [micro.duty("D1", "2026-10-01 06:00", "2026-10-01 14:00"),
              micro.duty("D2", "2026-10-01 18:00", "2026-10-01 22:00")]
    data = micro.data([micro.pilot("A")], duties)
    cfg = micro.config()
    without_rest = tuple(f for f in FAMILIES if f != "rest")
    assert solve(build_model(data, cfg, families=without_rest, elastic=False)).status == OPTIMAL


def test_decomposed_solve_matches_single_model(tmp_path):
    from crew_roster.model.data import load_model_data
    from tests.conftest import build_dataset, small_config

    cfg = small_config(tmp_path, dirty=False, solver={"gap_rel": 0.005, "time_limit_seconds": 20})
    conn = build_dataset(cfg)
    data = load_model_data(conn)
    conn.close()
    whole = solve_roster(data, cfg, decompose=False)
    parts = solve_roster(data, cfg, decompose=True)
    assert whole.has_roster and parts.has_roster
    assert len(parts.pool_results) == 2
    # If the two models describe the same problem, each proven lower bound must
    # sit below the other model's roster cost. This holds even on a time limit.
    assert parts.best_bound <= whole.objective + 1e-6
    assert whole.best_bound <= parts.objective + 1e-6


def test_cbc_log_parser_reads_the_summary_block():
    text = """Result - Stopped on time limit

Objective value:                243053.00867713
Lower bound:                    242077.110
Gap:                            0.00
Enumerated nodes:               12
Total iterations:               3400
Time (Wallclock seconds):       1.40
"""
    parsed = parse_cbc_log(text)
    assert parsed["result"] == "Stopped on time limit"
    assert parsed["objective"] == pytest.approx(243053.0087)
    assert parsed["lower_bound"] == pytest.approx(242077.11)
    assert parsed["nodes"] == 12
