-- Stage 2: normalise, quarantine, and build clean tables.
--
-- Policy: reject at row level, block at dataset level.
--   * A bad row goes to rejected_rows with the rule it broke. It never reaches
--     the solver, and it is never silently dropped.
--   * A duty with any bad leg is rejected whole, because crew cannot fly half
--     a duty.
--   * Whether the run is allowed to continue is decided later by
--     validation_checks.sql (for example, too many rejections blocks the run).
--
-- Depends on run_params (one row) written by the Python pipeline.

DROP TABLE IF EXISTS rejected_rows;
CREATE TABLE rejected_rows (
    source_table TEXT NOT NULL,
    record_key   TEXT,
    parent_key   TEXT,
    rule         TEXT NOT NULL,
    detail       TEXT
);

-- Reference data ------------------------------------------------------------

DROP TABLE IF EXISTS bases_clean;
CREATE TABLE bases_clean AS
SELECT DISTINCT UPPER(TRIM(base_code)) AS base_code, TRIM(base_name) AS base_name, TRIM(timezone) AS timezone
FROM stg_bases
WHERE TRIM(COALESCE(base_code, '')) <> '';

DROP TABLE IF EXISTS aircraft_types_clean;
CREATE TABLE aircraft_types_clean AS
SELECT DISTINCT
    UPPER(TRIM(type_code)) AS type_code,
    TRIM(description) AS description,
    CAST(captains_required AS INTEGER) AS captains_required,
    CAST(first_officers_required AS INTEGER) AS first_officers_required
FROM stg_aircraft_types
WHERE TRIM(COALESCE(type_code, '')) <> '';

-- Crew -------------------------------------------------------------------------

DROP TABLE IF EXISTS temp.crew_norm;
CREATE TEMP TABLE crew_norm AS
SELECT DISTINCT
    TRIM(crew_id) AS crew_id,
    CASE UPPER(TRIM(rank))
        WHEN 'CPT' THEN 'CPT' WHEN 'CAPTAIN' THEN 'CPT'
        WHEN 'FO' THEN 'FO' WHEN 'FIRST OFFICER' THEN 'FO'
    END AS rank,
    TRIM(rank) AS rank_raw,
    UPPER(TRIM(base)) AS base,
    CAST(NULLIF(TRIM(seniority_years), '') AS INTEGER) AS seniority_years,
    CAST(NULLIF(TRIM(hourly_rate_gbp), '') AS REAL) AS hourly_rate_gbp,
    CAST(NULLIF(TRIM(fte), '') AS REAL) AS fte
FROM stg_crew;

INSERT INTO rejected_rows (source_table, record_key, rule, detail)
SELECT 'crew', crew_id, 'conflicting_duplicate_id', COUNT(*) || ' different rows share this crew_id'
FROM crew_norm GROUP BY crew_id HAVING COUNT(*) > 1;

INSERT INTO rejected_rows (source_table, record_key, rule, detail)
SELECT 'crew', crew_id, 'invalid_rank', 'rank value "' || COALESCE(rank_raw, '') || '"'
FROM crew_norm WHERE rank IS NULL;

INSERT INTO rejected_rows (source_table, record_key, rule, detail)
SELECT 'crew', crew_id, 'unknown_base', 'base "' || COALESCE(base, '') || '" not in bases'
FROM crew_norm WHERE base NOT IN (SELECT base_code FROM bases_clean);

INSERT INTO rejected_rows (source_table, record_key, rule, detail)
SELECT 'crew', crew_id, 'invalid_pay_rate', 'hourly rate missing or not positive'
FROM crew_norm WHERE hourly_rate_gbp IS NULL OR hourly_rate_gbp <= 0;

INSERT INTO rejected_rows (source_table, record_key, rule, detail)
SELECT 'crew', crew_id, 'invalid_fte', 'fte must be in (0, 1]'
FROM crew_norm WHERE fte IS NULL OR fte <= 0 OR fte > 1;

DROP TABLE IF EXISTS crew_clean;
CREATE TABLE crew_clean AS
SELECT crew_id, rank, base, seniority_years, hourly_rate_gbp, fte
FROM crew_norm
WHERE crew_id NOT IN (SELECT record_key FROM rejected_rows WHERE source_table = 'crew');

-- Qualifications -----------------------------------------------------------------

DROP TABLE IF EXISTS temp.quals_norm;
CREATE TEMP TABLE quals_norm AS
SELECT DISTINCT
    TRIM(crew_id) AS crew_id,
    UPPER(TRIM(aircraft_type)) AS aircraft_type,
    date(TRIM(qualified_from)) AS qualified_from,
    date(TRIM(licence_expiry)) AS licence_expiry
FROM stg_crew_qualifications;

INSERT INTO rejected_rows (source_table, record_key, parent_key, rule, detail)
SELECT 'crew_qualifications', crew_id || ':' || aircraft_type, crew_id, 'unknown_or_rejected_crew', 'crew_id not in clean crew'
FROM quals_norm WHERE crew_id NOT IN (SELECT crew_id FROM crew_clean);

INSERT INTO rejected_rows (source_table, record_key, parent_key, rule, detail)
SELECT 'crew_qualifications', crew_id || ':' || aircraft_type, crew_id, 'unknown_aircraft_type', 'type "' || aircraft_type || '" not in aircraft_types'
FROM quals_norm WHERE aircraft_type NOT IN (SELECT type_code FROM aircraft_types_clean);

INSERT INTO rejected_rows (source_table, record_key, parent_key, rule, detail)
SELECT 'crew_qualifications', crew_id || ':' || aircraft_type, crew_id, 'invalid_date', 'qualified_from or licence_expiry is not an ISO date'
FROM quals_norm WHERE qualified_from IS NULL OR licence_expiry IS NULL;

DROP TABLE IF EXISTS qualifications_clean;
CREATE TABLE qualifications_clean AS
SELECT crew_id, aircraft_type, qualified_from, licence_expiry
FROM quals_norm
WHERE crew_id || ':' || aircraft_type NOT IN (
    SELECT record_key FROM rejected_rows WHERE source_table = 'crew_qualifications'
);

-- Leave ---------------------------------------------------------------------------

DROP TABLE IF EXISTS temp.leave_norm;
CREATE TEMP TABLE leave_norm AS
SELECT DISTINCT
    TRIM(leave_id) AS leave_id,
    TRIM(crew_id) AS crew_id,
    UPPER(TRIM(leave_type)) AS leave_type,
    -- date() returns NULL for anything that is not ISO. We do not try to guess
    -- whether 03/10/2026 means 3 October or 10 March.
    date(TRIM(start_date)) AS start_date,
    date(TRIM(end_date)) AS end_date
FROM stg_leave;

INSERT INTO rejected_rows (source_table, record_key, parent_key, rule, detail)
SELECT 'leave', leave_id, crew_id, 'unknown_or_rejected_crew', 'crew_id not in clean crew'
FROM leave_norm WHERE crew_id NOT IN (SELECT crew_id FROM crew_clean);

INSERT INTO rejected_rows (source_table, record_key, parent_key, rule, detail)
SELECT 'leave', leave_id, crew_id, 'invalid_date', 'start_date or end_date is not an ISO date'
FROM leave_norm WHERE start_date IS NULL OR end_date IS NULL;

INSERT INTO rejected_rows (source_table, record_key, parent_key, rule, detail)
SELECT 'leave', leave_id, crew_id, 'end_before_start', start_date || ' to ' || end_date
FROM leave_norm WHERE end_date < start_date;

INSERT INTO rejected_rows (source_table, record_key, parent_key, rule, detail)
SELECT 'leave', leave_id, crew_id, 'unknown_leave_type', 'leave_type "' || COALESCE(leave_type, '') || '"'
FROM leave_norm WHERE leave_type NOT IN ('ANNUAL', 'SICK', 'TRAINING', 'OTHER');

DROP TABLE IF EXISTS leave_clean;
CREATE TABLE leave_clean AS
SELECT leave_id, crew_id, leave_type, start_date, end_date
FROM leave_norm
WHERE leave_id NOT IN (SELECT record_key FROM rejected_rows WHERE source_table = 'leave');

-- Flights, leg level -----------------------------------------------------------------

DROP TABLE IF EXISTS temp.flights_norm;
CREATE TEMP TABLE flights_norm AS
SELECT DISTINCT
    TRIM(flight_id) AS flight_id,
    UPPER(TRIM(flight_number)) AS flight_number,
    TRIM(duty_id) AS duty_id,
    CAST(TRIM(leg_seq) AS INTEGER) AS leg_seq,
    UPPER(TRIM(aircraft_type)) AS aircraft_type,
    UPPER(TRIM(dep_airport)) AS dep_airport,
    UPPER(TRIM(arr_airport)) AS arr_airport,
    -- 2026/10/05 06:00 is unambiguous (year first), so we normalise it.
    datetime(REPLACE(TRIM(dep_time_utc), '/', '-')) AS dep_time_utc,
    datetime(REPLACE(TRIM(arr_time_utc), '/', '-')) AS arr_time_utc
FROM stg_flights;

INSERT INTO rejected_rows (source_table, record_key, parent_key, rule, detail)
SELECT 'flights', flight_id, MIN(duty_id), 'conflicting_duplicate_id', COUNT(*) || ' different rows share this flight_id'
FROM flights_norm GROUP BY flight_id HAVING COUNT(*) > 1;

INSERT INTO rejected_rows (source_table, record_key, parent_key, rule, detail)
SELECT 'flights', flight_id, duty_id, 'invalid_timestamp', 'departure or arrival time could not be parsed'
FROM flights_norm WHERE dep_time_utc IS NULL OR arr_time_utc IS NULL;

INSERT INTO rejected_rows (source_table, record_key, parent_key, rule, detail)
SELECT 'flights', flight_id, duty_id, 'arrival_not_after_departure', dep_time_utc || ' to ' || arr_time_utc
FROM flights_norm WHERE arr_time_utc <= dep_time_utc;

INSERT INTO rejected_rows (source_table, record_key, parent_key, rule, detail)
SELECT 'flights', flight_id, duty_id, 'unknown_aircraft_type', 'type "' || aircraft_type || '" not in aircraft_types'
FROM flights_norm WHERE aircraft_type NOT IN (SELECT type_code FROM aircraft_types_clean);

INSERT INTO rejected_rows (source_table, record_key, parent_key, rule, detail)
SELECT 'flights', flight_id, duty_id, 'missing_duty_id', 'leg is not assigned to a duty'
FROM flights_norm WHERE COALESCE(duty_id, '') = '';

-- Duty level ----------------------------------------------------------------------------

DROP TABLE IF EXISTS temp.candidate_legs;
CREATE TEMP TABLE candidate_legs AS
SELECT *
FROM flights_norm
WHERE COALESCE(duty_id, '') <> ''
  AND duty_id NOT IN (
      SELECT parent_key FROM rejected_rows WHERE source_table = 'flights' AND parent_key IS NOT NULL
  );

INSERT INTO rejected_rows (source_table, record_key, rule, detail)
SELECT DISTINCT 'duties', parent_key, 'contains_rejected_flight', 'at least one leg failed a flight rule'
FROM rejected_rows WHERE source_table = 'flights' AND COALESCE(parent_key, '') <> '';

INSERT INTO rejected_rows (source_table, record_key, rule, detail)
SELECT 'duties', duty_id, 'mixed_aircraft_types', GROUP_CONCAT(DISTINCT aircraft_type)
FROM candidate_legs GROUP BY duty_id HAVING COUNT(DISTINCT aircraft_type) > 1;

INSERT INTO rejected_rows (source_table, record_key, rule, detail)
SELECT 'duties', duty_id, 'duplicate_leg_sequence', 'leg_seq values repeat'
FROM candidate_legs GROUP BY duty_id HAVING COUNT(*) > COUNT(DISTINCT leg_seq);

-- Window functions: compare each leg to the leg before it in the same duty.
INSERT INTO rejected_rows (source_table, record_key, rule, detail)
SELECT DISTINCT 'duties', duty_id, rule, detail
FROM (
    SELECT
        duty_id,
        CASE
            WHEN prev_arr_airport <> dep_airport THEN 'broken_leg_chain'
            WHEN dep_time_utc < prev_arr_time THEN 'overlapping_legs'
        END AS rule,
        'leg ' || leg_seq || ' departs ' || dep_airport || ' at ' || dep_time_utc
            || ', previous leg arrived ' || prev_arr_airport || ' at ' || prev_arr_time AS detail
    FROM (
        SELECT
            duty_id, leg_seq, dep_airport, dep_time_utc,
            LAG(arr_airport) OVER (PARTITION BY duty_id ORDER BY leg_seq) AS prev_arr_airport,
            LAG(arr_time_utc) OVER (PARTITION BY duty_id ORDER BY leg_seq) AS prev_arr_time
        FROM candidate_legs
    )
    WHERE prev_arr_airport IS NOT NULL
)
WHERE rule IS NOT NULL;

DROP TABLE IF EXISTS temp.duty_summary;
CREATE TEMP TABLE duty_summary AS
SELECT
    duty_id,
    MIN(aircraft_type) AS aircraft_type,
    COUNT(*) AS n_legs,
    (SELECT dep_airport FROM candidate_legs c2 WHERE c2.duty_id = c.duty_id ORDER BY leg_seq LIMIT 1) AS first_dep,
    (SELECT arr_airport FROM candidate_legs c2 WHERE c2.duty_id = c.duty_id ORDER BY leg_seq DESC LIMIT 1) AS last_arr,
    datetime(MIN(dep_time_utc), '-' || (SELECT report_minutes FROM run_params) || ' minutes') AS report_utc,
    datetime(MAX(arr_time_utc), '+' || (SELECT debrief_minutes FROM run_params) || ' minutes') AS release_utc,
    ROUND(SUM((julianday(arr_time_utc) - julianday(dep_time_utc)) * 24), 3) AS block_hours
FROM candidate_legs c
GROUP BY duty_id;

INSERT INTO rejected_rows (source_table, record_key, rule, detail)
SELECT 'duties', duty_id, 'not_based_round_trip', 'starts ' || first_dep || ', ends ' || last_arr
FROM duty_summary
WHERE first_dep NOT IN (SELECT base_code FROM bases_clean) OR last_arr <> first_dep;

INSERT INTO rejected_rows (source_table, record_key, rule, detail)
SELECT 'duties', duty_id, 'exceeds_max_duty_hours_day',
       printf('%.2f hours, limit %.1f', (julianday(release_utc) - julianday(report_utc)) * 24,
              (SELECT max_duty_hours_day FROM run_params))
FROM duty_summary
WHERE (julianday(release_utc) - julianday(report_utc)) * 24 > (SELECT max_duty_hours_day FROM run_params);

INSERT INTO rejected_rows (source_table, record_key, rule, detail)
SELECT 'duties', duty_id, 'outside_planning_period', 'report ' || report_utc
FROM duty_summary, run_params p
WHERE date(report_utc) < p.period_start
   OR date(report_utc) >= date(p.period_start, '+' || p.period_days || ' days');

DROP TABLE IF EXISTS duties;
CREATE TABLE duties AS
SELECT
    s.duty_id,
    s.first_dep AS base,
    s.aircraft_type,
    s.report_utc,
    s.release_utc,
    ROUND((julianday(s.release_utc) - julianday(s.report_utc)) * 24, 4) AS duty_hours,
    s.block_hours,
    s.n_legs,
    date(s.report_utc) AS duty_date,
    CAST(julianday(date(s.report_utc)) - julianday((SELECT period_start FROM run_params)) AS INTEGER) AS day_index,
    t.captains_required,
    t.first_officers_required
FROM duty_summary s
JOIN aircraft_types_clean t ON t.type_code = s.aircraft_type
WHERE s.duty_id NOT IN (SELECT record_key FROM rejected_rows WHERE source_table = 'duties');

DROP TABLE IF EXISTS flights_clean;
CREATE TABLE flights_clean AS
SELECT * FROM candidate_legs WHERE duty_id IN (SELECT duty_id FROM duties);

-- Rejection share, used by the dataset level gate in validation_checks.sql.
DROP TABLE IF EXISTS pipeline_stats;
CREATE TABLE pipeline_stats AS
SELECT
    (SELECT COUNT(*) FROM stg_flights) AS raw_flight_rows,
    (SELECT COUNT(*) FROM flights_norm) AS normalised_flight_rows,
    (SELECT COUNT(*) FROM flights_clean) AS clean_flight_rows,
    (SELECT COUNT(DISTINCT flight_id) FROM flights_norm) AS distinct_flight_ids,
    (SELECT COUNT(*) FROM duties) AS clean_duties,
    (SELECT COUNT(DISTINCT record_key) FROM rejected_rows WHERE source_table = 'duties') AS rejected_duties;
