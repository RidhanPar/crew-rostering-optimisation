from crew_roster.model.diagnose import capacity_precheck, conflict_set, diagnose
from tests import micro


def _three_early_duties():
    return [micro.duty(f"D{i}", f"2026-10-0{i} 06:00", f"2026-10-0{i} 14:00") for i in range(1, 4)]


def test_days_rule_alone_explains_infeasibility():
    data = micro.data([micro.pilot("A")], _three_early_duties())
    cfg = micro.config(rules={"max_duty_days_7d": 2})
    conflict, singles = conflict_set(data, cfg, time_limit=20)
    assert conflict == ["days_7d"]
    assert singles.set_index("removed_family").at["days_7d", "fixes_it"]
    assert not singles.set_index("removed_family").at["rest", "fixes_it"]


def test_rest_rule_alone_explains_infeasibility():
    duties = [micro.duty("D1", "2026-10-01 06:00", "2026-10-01 14:00"),
              micro.duty("D2", "2026-10-01 18:00", "2026-10-01 22:00")]
    conflict, _ = conflict_set(micro.data([micro.pilot("A")], duties), micro.config(), time_limit=20)
    assert conflict == ["rest"]


def test_feasible_model_has_no_conflict_set():
    conflict, _ = conflict_set(micro.data([micro.pilot("A"), micro.pilot("B")], _three_early_duties()),
                               micro.config(), time_limit=20)
    assert conflict is None


def test_seat_with_nobody_eligible_is_a_coverage_conflict():
    data = micro.data([micro.pilot("A")], _three_early_duties(), eligibility=[("A", "D1"), ("A", "D2")])
    conflict, _ = conflict_set(data, micro.config(), time_limit=20)
    assert conflict == []
    pre = capacity_precheck(data, micro.config())
    assert "seat_without_eligible_crew" in set(pre["check"])


def test_precheck_catches_a_weekly_shortage_before_any_solve():
    data = micro.data([micro.pilot("A")], _three_early_duties())
    pre = capacity_precheck(data, micro.config(rules={"max_duty_days_7d": 2}))
    assert "7_day_duty_day_shortage" in set(pre["check"])


def test_full_diagnosis_names_the_pool_and_the_rule():
    data = micro.data([micro.pilot("A")], _three_early_duties())
    diag = diagnose(data, micro.config(rules={"max_duty_days_7d": 2}), time_limit=20)
    assert diag.problem_pools == ["MAN-A320-CPT"]
    assert diag.conflict_sets["MAN-A320-CPT"] == ["days_7d"]
    assert int(diag.uncovered["seats"].sum()) == 1
