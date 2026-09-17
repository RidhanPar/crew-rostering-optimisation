import pytest

from crew_roster.data.validate import SEVERITIES, ValidationFailed, assert_valid, parse_checks, run_validation


def test_check_file_parses():
    checks = parse_checks()
    assert len(checks) >= 8
    assert all(c.severity in SEVERITIES and c.sql.strip().endswith(";") for c in checks)


def test_generated_data_passes_the_gate(dirty_db):
    issues = run_validation(dirty_db)
    assert not (issues["severity"] == "ERROR").any()
    assert_valid(issues)
    assert "rejected_rows_summary" in set(issues["check"])


def test_empty_crew_blocks_the_solve(dirty_db):
    dirty_db.execute("DELETE FROM crew_clean")
    issues = run_validation(dirty_db)
    assert "no_crew_after_cleaning" in set(issues.loc[issues.severity == "ERROR", "check"])
    with pytest.raises(ValidationFailed):
        assert_valid(issues)


def test_fleet_with_no_rated_first_officers_blocks_the_solve(dirty_db):
    dirty_db.execute("DELETE FROM qualifications_clean WHERE crew_id IN (SELECT crew_id FROM crew_clean WHERE rank = 'FO')")
    issues = run_validation(dirty_db)
    errors = issues[issues.severity == "ERROR"]
    assert (errors["check"] == "fleet_without_qualified_crew").any()
    assert errors["entity"].str.endswith("-FO").all()


def test_high_rejection_share_blocks_the_solve(dirty_db):
    dirty_db.execute("UPDATE run_params SET max_rejected_share = 0.01")
    issues = run_validation(dirty_db)
    assert "rejected_flight_share_too_high" in set(issues.loc[issues.severity == "ERROR", "check"])


def test_everyone_on_leave_warns_about_uncovered_seats(dirty_db):
    dirty_db.execute("""INSERT INTO scenario_leave
        SELECT 'X' || crew_id, crew_id, 'SICK', '2026-10-03', '2026-10-03' FROM crew_clean WHERE rank = 'CPT'""")
    issues = run_validation(dirty_db)
    seats = issues[issues.check == "seat_with_no_eligible_crew"]
    # Late duties reporting on the 2nd release after midnight, so they touch the 3rd too.
    assert not seats.empty and seats["detail"].str.contains("2026-10-0[23]").all()
    assert seats["detail"].str.contains("2026-10-03").any()
