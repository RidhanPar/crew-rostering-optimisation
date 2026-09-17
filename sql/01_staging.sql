-- Stage 1: staging tables.
-- Everything lands as TEXT, exactly as exported. We never trust types from a
-- CSV, so casting happens in 02_clean.sql where failures can be recorded.

DROP TABLE IF EXISTS stg_bases;
CREATE TABLE stg_bases (base_code TEXT, base_name TEXT, timezone TEXT);

DROP TABLE IF EXISTS stg_aircraft_types;
CREATE TABLE stg_aircraft_types (
    type_code TEXT, description TEXT, captains_required TEXT, first_officers_required TEXT
);

DROP TABLE IF EXISTS stg_crew;
CREATE TABLE stg_crew (
    crew_id TEXT, rank TEXT, base TEXT, seniority_years TEXT, hourly_rate_gbp TEXT, fte TEXT
);

DROP TABLE IF EXISTS stg_crew_qualifications;
CREATE TABLE stg_crew_qualifications (
    crew_id TEXT, aircraft_type TEXT, qualified_from TEXT, licence_expiry TEXT
);

DROP TABLE IF EXISTS stg_leave;
CREATE TABLE stg_leave (leave_id TEXT, crew_id TEXT, leave_type TEXT, start_date TEXT, end_date TEXT);

DROP TABLE IF EXISTS stg_flights;
CREATE TABLE stg_flights (
    flight_id TEXT, flight_number TEXT, duty_id TEXT, leg_seq TEXT, aircraft_type TEXT,
    dep_airport TEXT, arr_airport TEXT, dep_time_utc TEXT, arr_time_utc TEXT
);
