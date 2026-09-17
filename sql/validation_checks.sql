-- Dataset level validation gate, run after cleaning and before the solver.
--
-- Each check is a query that returns one row per problem (columns: entity,
-- detail). Zero rows means the check passed.
--   ERROR    blocks the solve. The data cannot produce a meaningful roster.
--   WARNING  the solve runs, but a planner should look at this.
--   INFO     context for the run log.
--
-- Format: a header of "-- name:", "-- severity:", "-- description:" lines
-- followed by one SELECT ending in a semicolon.

-- name: no_crew_after_cleaning
-- severity: ERROR
-- description: Nothing to roster if every crew row was rejected or the export was empty.
SELECT 'crew_clean' AS entity, 'zero crew rows after cleaning' AS detail
WHERE (SELECT COUNT(*) FROM crew_clean) = 0;

-- name: no_duties_after_cleaning
-- severity: ERROR
-- description: Nothing to cover if every duty was rejected or the export was empty.
SELECT 'duties' AS entity, 'zero duties after cleaning' AS detail
WHERE (SELECT COUNT(*) FROM duties) = 0;

-- name: rejected_flight_share_too_high
-- severity: ERROR
-- description: A high rejection rate usually means a broken export, not a few bad rows. Publishing a roster that silently skips 10% of flying is worse than stopping.
SELECT 'flights' AS entity,
       printf('%.1f%% of flight ids rejected, limit %.1f%%',
              100.0 * (s.distinct_flight_ids - s.clean_flight_rows) / s.distinct_flight_ids,
              100.0 * p.max_rejected_share) AS detail
FROM pipeline_stats s, run_params p
WHERE s.distinct_flight_ids > 0
  AND 1.0 * (s.distinct_flight_ids - s.clean_flight_rows) / s.distinct_flight_ids > p.max_rejected_share;

-- name: fleet_without_qualified_crew
-- severity: ERROR
-- description: A base flies a type but no pilot of a required rank holds that type rating there. Every one of those duties would be uncovered.
SELECT d.base || '-' || d.aircraft_type || '-' || r.rank AS entity,
       COUNT(*) || ' duties, zero ' || r.rank || ' rated on type at base' AS detail
FROM duties d
CROSS JOIN (SELECT 'CPT' AS rank UNION ALL SELECT 'FO') r
WHERE ((r.rank = 'CPT' AND d.captains_required > 0) OR (r.rank = 'FO' AND d.first_officers_required > 0))
  AND NOT EXISTS (
      SELECT 1 FROM crew_clean c
      JOIN qualifications_clean q ON q.crew_id = c.crew_id
      WHERE c.base = d.base AND c.rank = r.rank AND q.aircraft_type = d.aircraft_type
  )
GROUP BY d.base, d.aircraft_type, r.rank;

-- name: seat_with_no_eligible_crew
-- severity: WARNING
-- description: Crew exist for the fleet, but on this duty every one is on leave or out of licence. The seat will be uncovered whatever the solver does.
SELECT d.duty_id || ':' || r.rank AS entity,
       'no eligible ' || r.rank || ' (leave or licence) on ' || d.duty_date AS detail
FROM duties d
CROSS JOIN (SELECT 'CPT' AS rank UNION ALL SELECT 'FO') r
WHERE ((r.rank = 'CPT' AND d.captains_required > 0) OR (r.rank = 'FO' AND d.first_officers_required > 0))
  AND EXISTS (
      SELECT 1 FROM crew_clean c JOIN qualifications_clean q ON q.crew_id = c.crew_id
      WHERE c.base = d.base AND c.rank = r.rank AND q.aircraft_type = d.aircraft_type
  )
  AND NOT EXISTS (
      SELECT 1 FROM eligibility e JOIN crew_clean c ON c.crew_id = e.crew_id
      WHERE e.duty_id = d.duty_id AND c.rank = r.rank
  );

-- name: crew_without_usable_qualification
-- severity: WARNING
-- description: Pilot is on the books but cannot fly any day this period (no rating, or licence already expired). Usually an HR data lag.
SELECT c.crew_id AS entity,
       COALESCE('licence expired ' || MAX(q.licence_expiry), 'no type rating on file') AS detail
FROM crew_clean c
LEFT JOIN qualifications_clean q ON q.crew_id = c.crew_id
GROUP BY c.crew_id
HAVING MAX(q.licence_expiry) IS NULL OR MAX(q.licence_expiry) < (SELECT period_start FROM run_params);

-- name: licence_expires_in_period
-- severity: WARNING
-- description: Pilot becomes ineligible part way through the month. The roster respects it, but training should book a renewal.
SELECT q.crew_id AS entity, q.aircraft_type || ' licence expires ' || q.licence_expiry AS detail
FROM qualifications_clean q, run_params p
WHERE q.licence_expiry >= p.period_start
  AND q.licence_expiry < date(p.period_start, '+' || p.period_days || ' days');

-- name: overlapping_leave_records
-- severity: WARNING
-- description: Two leave records for the same pilot overlap, often a sick day logged during annual leave. Harmless for the solver, but it double counts in HR reports.
SELECT a.crew_id AS entity,
       a.leave_id || ' (' || a.leave_type || ') overlaps ' || b.leave_id || ' (' || b.leave_type || ')' AS detail
FROM leave_clean a
JOIN leave_clean b ON a.crew_id = b.crew_id AND a.leave_id < b.leave_id
WHERE a.start_date <= b.end_date AND b.start_date <= a.end_date;

-- name: pool_needs_overtime
-- severity: WARNING
-- description: Flying hours in a pool exceed what its pilots can do before overtime, given leave and FTE. Expect overtime cost or uncovered seats.
WITH demand AS (
    SELECT c.pool_id, SUM(d.duty_hours) AS required_hours
    FROM duties d
    JOIN (SELECT DISTINCT pool_id, base, rank, type_ratings FROM crew_model) c
      ON c.base = d.base AND ('+' || c.type_ratings || '+') LIKE ('%+' || d.aircraft_type || '+%')
    GROUP BY c.pool_id
),
supply AS (
    SELECT pool_id,
           SUM(fte * available_days * 1.0 / (SELECT period_days FROM run_params))
               * (SELECT overtime_threshold_hours FROM run_params)
               * (SELECT period_days FROM run_params) / 31.0 AS straight_time_hours
    FROM crew_model GROUP BY pool_id
)
SELECT d.pool_id AS entity,
       printf('%.0f hours required, %.0f straight time hours available (%.0f%%)',
              d.required_hours, s.straight_time_hours, 100.0 * d.required_hours / s.straight_time_hours) AS detail
FROM demand d JOIN supply s USING (pool_id)
WHERE d.required_hours > s.straight_time_hours;

-- name: rejected_rows_summary
-- severity: INFO
-- description: What the cleaning stage quarantined, by table and rule.
SELECT source_table || '.' || rule AS entity, COUNT(*) || ' rows' AS detail
FROM rejected_rows
GROUP BY source_table, rule;
