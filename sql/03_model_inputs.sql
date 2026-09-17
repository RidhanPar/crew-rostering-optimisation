-- Stage 3: tables and views the optimisation model reads.
--
-- The single most important view here is eligibility. It answers "could this
-- pilot legally be put on this duty, ignoring the rest of their roster?"
-- Base, rank, type rating, licence validity and leave are all static facts,
-- so we resolve them in SQL and only create solver variables for eligible
-- pairs. Rules that depend on the rest of the roster (rest, hours caps) must
-- live in the solver.

DROP TABLE IF EXISTS calendar;
CREATE TABLE calendar AS
WITH RECURSIVE days(day_index, cal_date) AS (
    SELECT 0, (SELECT period_start FROM run_params)
    UNION ALL
    SELECT day_index + 1, date(cal_date, '+1 day')
    FROM days
    WHERE day_index + 1 < (SELECT period_days FROM run_params)
)
SELECT
    day_index,
    cal_date,
    CASE CAST(strftime('%w', cal_date) AS INTEGER)
        WHEN 0 THEN 'Sun' WHEN 1 THEN 'Mon' WHEN 2 THEN 'Tue' WHEN 3 THEN 'Wed'
        WHEN 4 THEN 'Thu' WHEN 5 THEN 'Fri' ELSE 'Sat'
    END AS weekday
FROM days;

-- Scenario leave lets "what if 3 pilots go sick" scenarios add unavailability
-- without editing source data. Python clears and refills it per scenario.
CREATE TABLE IF NOT EXISTS scenario_leave (
    leave_id TEXT, crew_id TEXT, leave_type TEXT, start_date TEXT, end_date TEXT
);

DROP VIEW IF EXISTS leave_all;
CREATE VIEW leave_all AS
SELECT leave_id, crew_id, leave_type, start_date, end_date, 'source' AS origin FROM leave_clean
UNION ALL
SELECT leave_id, crew_id, leave_type, start_date, end_date, 'scenario' AS origin FROM scenario_leave;

-- One row per pilot and calendar day on which they can work at all.
DROP VIEW IF EXISTS crew_available_days;
CREATE VIEW crew_available_days AS
SELECT c.crew_id, cal.day_index, cal.cal_date
FROM crew_clean c
CROSS JOIN calendar cal
WHERE NOT EXISTS (
        SELECT 1 FROM leave_all l
        WHERE l.crew_id = c.crew_id AND cal.cal_date BETWEEN l.start_date AND l.end_date
    )
  AND EXISTS (
        SELECT 1 FROM qualifications_clean q
        WHERE q.crew_id = c.crew_id AND cal.cal_date BETWEEN q.qualified_from AND q.licence_expiry
    );

-- Crew as the model sees them. A pool is the set of pilots who can swap
-- duties with each other: same base, same rank, same type rating(s).
-- Fairness is measured inside a pool, never across pools.
DROP VIEW IF EXISTS crew_model;
CREATE VIEW crew_model AS
WITH ratings AS (
    SELECT crew_id, GROUP_CONCAT(aircraft_type, '+') AS type_ratings
    FROM (SELECT crew_id, aircraft_type FROM qualifications_clean ORDER BY crew_id, aircraft_type)
    GROUP BY crew_id
),
avail AS (
    SELECT crew_id, COUNT(*) AS available_days FROM crew_available_days GROUP BY crew_id
)
SELECT
    c.crew_id,
    c.rank,
    c.base,
    r.type_ratings,
    c.base || '-' || r.type_ratings || '-' || c.rank AS pool_id,
    c.hourly_rate_gbp,
    c.fte,
    COALESCE(a.available_days, 0) AS available_days
FROM crew_clean c
JOIN ratings r ON r.crew_id = c.crew_id
LEFT JOIN avail a ON a.crew_id = c.crew_id;

DROP VIEW IF EXISTS eligibility;
CREATE VIEW eligibility AS
SELECT DISTINCT c.crew_id, d.duty_id
FROM crew_clean c
JOIN duties d
  ON d.base = c.base
JOIN qualifications_clean q
  ON q.crew_id = c.crew_id
 AND q.aircraft_type = d.aircraft_type
 AND d.duty_date >= q.qualified_from
 AND date(d.release_utc) <= q.licence_expiry        -- licence valid for the whole duty
WHERE ((c.rank = 'CPT' AND d.captains_required > 0)
    OR (c.rank = 'FO' AND d.first_officers_required > 0))
  AND NOT EXISTS (                                   -- duty does not touch a leave day
        SELECT 1 FROM leave_all l
        WHERE l.crew_id = c.crew_id
          AND d.report_utc < datetime(l.end_date, '+1 day')
          AND d.release_utc > datetime(l.start_date)
    );
