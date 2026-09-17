# Crew Rostering Optimisation

[![CI](https://github.com/RidhanPar/crew-rostering-optimisation/actions/workflows/ci.yml/badge.svg)](https://github.com/RidhanPar/crew-rostering-optimisation/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Solver](https://img.shields.io/badge/solver-PuLP%20%2B%20CBC-informational)
![Tests](https://img.shields.io/badge/tests-59%20passing-brightgreen)

A monthly airline crew rostering system built as a Mixed-Integer Program. It
assigns pilots to duty periods while respecting rest rules, duty hour limits,
type ratings, licence expiry, crew bases and leave, and it balances pay cost
against fairness between pilots.

The optimisation model sits inside a full pipeline: a validated SQL data
layer, a solver wrapper that reports status honestly, infeasibility
diagnostics, an independent roster checker, a scenario engine, and Power BI
ready output tables.

> **Data notice.** All data is synthetic, generated with seed 42 for a
> fictional UK short haul airline. Every figure in this document was measured
> from a real run of the code in this repository and can be reproduced with
> `python -m crew_roster run-all`.

---

## Contents

1. [Background: the business problem](#1-background-the-business-problem)
2. [What the system does](#2-what-the-system-does)
3. [Results summary](#3-results-summary)
4. [Architecture](#4-architecture)
5. [Getting started](#5-getting-started)
6. [Command line reference](#6-command-line-reference)
7. [Configuration reference](#7-configuration-reference)
8. [Input data contracts](#8-input-data-contracts)
9. [Data pipeline and validation](#9-data-pipeline-and-validation)
10. [Optimisation model](#10-optimisation-model)
11. [Solving, status and performance](#11-solving-status-and-performance)
12. [Infeasibility diagnosis](#12-infeasibility-diagnosis)
13. [Output verification](#13-output-verification)
14. [Scenario analysis](#14-scenario-analysis)
15. [Outputs and Power BI](#15-outputs-and-power-bi)
16. [Testing and CI](#16-testing-and-ci)
17. [Logging and operations](#17-logging-and-operations)
18. [Troubleshooting](#18-troubleshooting)
19. [Assumptions](#19-assumptions)
20. [Scope and limitations](#20-scope-and-limitations)
21. [Project structure](#21-project-structure)
22. [Further documentation](#22-further-documentation)
23. [Glossary](#23-glossary)

---

## 1. Background: the business problem

An airline builds its flying programme months ahead. Crew planning then turns
that programme into named rosters in two stages:

| Stage | Question | Output |
|---|---|---|
| **Crew pairing** | How do flights chain into legal duty periods that start and end at a crew base? | Anonymous duties, e.g. `MAN to PMI to MAN, report 05:30, release 13:05` |
| **Crew rostering** | Which named pilot flies each duty this month? | A roster per pilot |

This project solves **crew rostering**. Duties arrive from an upstream pairing
step, and the system decides who flies them.

A roster is only usable if it is:

- **Legal.** Minimum rest between duties, maximum duty hours per day, per 7
  days and per month, and a limit on duty days per week.
- **Qualified.** The pilot holds a type rating for the aircraft, has a valid
  licence on the day, and is the right rank (captain or first officer).
- **Possible.** The pilot is based where the duty starts and is not on leave,
  sick or in training.
- **Affordable.** Pay and overtime are kept down.
- **Fair.** Hours are spread in proportion to each pilot's contract and
  availability, so nobody is overloaded while a colleague sits idle.

Cost and fairness pull in opposite directions: the cheapest roster loads the
lowest paid pilots. The system makes that trade off explicit and measurable.

---

## 2. What the system does

| Capability | Summary |
|---|---|
| Synthetic data generation | 3 bases, 3 aircraft types, 136 pilots, 740 duties, leave and licence records, with 22 deliberately injected data defects |
| Data pipeline | CSV to SQLite, SQL cleaning and normalisation, row quarantine with reasons, duty building with window functions |
| Validation gate | SQL checks with ERROR (blocks the solve), WARNING and INFO severities |
| Optimisation model | PuLP MIP with coverage, rest, daily, weekly and monthly limits, overtime, and fairness |
| Decomposition | Each crew pool solved as an independent model (exact) |
| Honest solver reporting | Status, objective, proven lower bound, gap, time, model size per pool |
| Infeasibility diagnosis | Capacity prechecks, minimum uncovered seats, irreducible conflicting rule families |
| Independent checker | 21 checks re derived from the rules in pandas, plus cost and fairness reconciliation |
| Scenario engine | 11 what ifs on rules, objective weights and disruption, compared against a baseline using proven bounds |
| Reporting | Power BI star schema (4 dimensions, 7 facts) and a dashboard specification |

---

## 3. Results summary

Measured on the seed 42 dataset (October 2026, 31 days) on an Intel Core
i5-10200H laptop, Windows 11, Python 3.11.9, PuLP 3.3.2, CBC 2.10.3 single
thread.

### Baseline roster

| Metric | Value |
|---|---|
| Seats to fill (740 duties × captain + first officer) | 1,480 |
| Seats filled | 1,480 (0 uncovered) |
| Status | OPTIMAL in all 12 crew pools |
| Objective (pay + overtime + fairness penalty) | £1,435,056 |
| Proven lower bound | £1,431,061 |
| Optimality gap | 0.28% |
| Pay + overtime | £1,428,731 (overtime £7,579 across 18 pilots) |
| Mean absolute deviation from fair hours | 3.7 hours |
| Independent checker errors | 0 |
| Solve time | 9.7 seconds |
| Model size | 17,933 variables (15,897 binary), 18,274 constraints |

### Key findings

| Finding | Evidence |
|---|---|
| A 4 hour fairness tolerance band made the model about 33x faster | 311.6s with 5 pools stopped on time limits before, 9.4s with all pools proven after |
| Solving pools separately beat one combined model | Combined model stopped at the 300s limit with a 0.56% gap and a roster £4,107 worse |
| Tightening rest from 12h to 14h costs between £0 and £4,196 a month | Range proven by the lower bounds; the solver cannot narrow it further at a practical tolerance |
| Rest of 16h, or 4 duty days in 7, cannot be fully covered | Proven by bounds: 2 and 4 seats uncovered |
| Every uncovered seat in every stress test is an Edinburgh 737 captain seat | The same pool the input validation flagged at 131% of straight time capacity |
| Ignoring fairness saves £33,922 a month (2.4%) | But leaves 14 available pilots with no flying and one pilot at 189.7h of a 190h cap |
| Sickness wave diagnosis | 3 Edinburgh 737 captains off for a week: the "5 duty days in 7" rule is the single rule in conflict |

---

## 4. Architecture

### Pipeline

```
 data/raw/*.csv                              config/default.toml, scenarios.toml
      |                                                  |
      v                                                  v
 [1] Schema check (Python) --------------------> fail fast on missing columns
      |
      v
 [2] SQLite staging (all TEXT)                  sql/01_staging.sql
      |
      v
 [3] Clean, normalise, quarantine, build duties sql/02_clean.sql
      |        \
      |         +--> rejected_rows (table, key, rule, detail)
      v
 [4] Calendar, crew pools, eligibility view      sql/03_model_inputs.sql
      |
      v
 [5] Validation gate                             sql/validation_checks.sql
      |        \
      |         +--> ERROR: stop.  WARNING / INFO: log and continue
      v
 [6] ModelData ---> one PuLP model per crew pool ---> CBC ---> SolveResult
      |                                                          |
      |                     +------------------------------------+
      v                     v
 [7] Diagnose          [8] Independent checker (pandas only)
                            |
                            v
                       [9] Scenario engine ---> comparison vs baseline
                            |
                            v
                      [10] Power BI star schema (outputs/powerbi)
```

### Design principles

| Principle | How it shows up |
|---|---|
| Bad data never reaches the solver | TEXT staging, explicit casts, quarantine with reasons, dataset level gate |
| Static rules in SQL, roster dependent rules in the model | Eligibility view removes impossible pairs before any variable is created |
| Rules live in configuration | Every limit, weight and solver setting is in `config/default.toml` |
| Never claim more than was proven | OPTIMAL only when CBC proved the gap; comparisons use lower bounds |
| Trust but verify | A second implementation of the rules checks every roster |
| Reproducible | Fixed seed, deterministic solves, committed outputs, one command rebuild |

### Technology

| Layer | Tool | Why |
|---|---|---|
| Language | Python 3.11 | `tomllib` in the standard library, typed dataclasses |
| Storage and transformation | SQLite + SQL | No server needed, window functions and recursive CTEs, readable by analysts |
| Data handling | pandas, NumPy | Loading, checker, reporting |
| Modelling | PuLP 3.3.2 | Readable algebraic modelling, writes LP files for inspection |
| Solver | CBC 2.10.3 (bundled with PuLP) | Free, no licence server, adequate for pool sized models |
| Tests | pytest | 59 tests, run in GitHub Actions |
| Reporting | CSV star schema for Power BI | Folder connector, no gateway required |

---

## 5. Getting started

### Requirements

- Python 3.11 or later
- About 50 MB of disk for data and outputs
- No database server and no solver licence (CBC ships inside PuLP)

### Install

```bash
git clone https://github.com/RidhanPar/crew-rostering-optimisation.git
cd crew-rostering-optimisation
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux
pip install -e ".[dev]"
```

### Run everything

```bash
python -m crew_roster run-all
```

This generates data, cleans and validates it, solves the baseline roster,
checks it, runs the infeasibility demo, runs all 11 scenarios and writes the
Power BI tables. Measured end to end time: 833 seconds. Most of it is the
scenario runs; the baseline solve alone takes about 10 seconds.

### Run the tests

```bash
pytest -q
```

Around 2 minutes; the tests use a small 14 day, single base dataset.

---

## 6. Command line reference

All commands accept `--config PATH` to use a different configuration file.

```bash
python -m crew_roster [--config PATH] <command> [options]
```

| Command | What it does | Writes | Exit codes |
|---|---|---|---|
| `generate` | Creates raw CSVs, with injected defects if enabled | `data/raw/*.csv`, `generation_manifest.json` | 0 |
| `prepare` | Loads, cleans, builds duties and eligibility, runs the validation gate | `data/roster.db` | 0 ok, 2 validation ERROR |
| `solve` | Builds and solves the roster | `outputs/roster/` | 0 roster found, 3 no roster |
| `check` | Independently re checks `outputs/roster` | `roster_check_issues.csv`, `crew_hours.csv` | 0 clean, 1 errors found |
| `diagnose` | Adds a sickness wave to one pool, shows the strict model fails, explains why | `outputs/diagnosis/` | 0 |
| `scenarios` | Runs scenarios from `config/scenarios.toml` and compares to baseline | `outputs/scenarios/` | 0 clean, 1 checker errors in any scenario |
| `report` | Builds Power BI tables from scenario outputs | `outputs/powerbi/` | 0 |
| `run-all` | All of the above in order, stopping at the first failure | everything | first non zero code |

### Options

| Command | Option | Default | Meaning |
|---|---|---|---|
| `solve` | `--mode {cost,fairness,weighted}` | from config | Override the objective mode |
| `solve` | `--strict` | off | Do not allow uncovered seats; the model may become infeasible |
| `solve` | `--no-decompose` | off | Solve all pools as one model |
| `diagnose` | `--pool` | `EDI-B737-CPT` | Crew pool to disrupt |
| `diagnose` | `--count` | `3` | Pilots taken off (those with the most available days) |
| `diagnose` | `--days` | `8-14` | Inclusive day index range of the absence |
| `diagnose` | `--time-limit` | `60` | Seconds per diagnostic solve |
| `scenarios` | `--only a,b` | all | Run only these scenarios (baseline always runs) |

### Examples

```bash
# Cheapest roster, ignoring fairness
python -m crew_roster solve --mode cost

# Prove whether every seat can be covered with no open time
python -m crew_roster solve --strict

# What if 4 Manchester A320 first officers are off for days 10 to 16?
python -m crew_roster diagnose --pool MAN-A320-FO --count 4 --days 10-16

# Compare only the rest scenarios
python -m crew_roster scenarios --only rest_10h_flat,rest_14h,rest_16h
```

---

## 7. Configuration reference

File: `config/default.toml`. Configuration is loaded into typed dataclasses;
unknown or missing keys raise an error at start up rather than being ignored.

### `[paths]`

| Key | Default | Meaning |
|---|---|---|
| `raw_dir` | `data/raw` | Location of input CSVs |
| `database` | `data/roster.db` | SQLite database, rebuilt by `prepare` |
| `output_dir` | `outputs` | Root for all outputs and logs |

### `[generation]`

| Key | Default | Meaning |
|---|---|---|
| `seed` | `42` | Random seed; same seed gives byte identical CSVs |
| `profile` | `full` | `full` = 3 bases and 6 fleets; `small` = 1 base, used by tests |
| `period_start` | `2026-10-01` | First day of the roster period |
| `period_days` | `31` | Length of the period |
| `inject_dirty_rows` | `true` | Add 22 realistic defects to test the pipeline |

### `[pipeline]`

| Key | Default | Meaning |
|---|---|---|
| `max_rejected_share` | `0.05` | Block the run if more than this share of flights is rejected |

### `[rules]`

| Key | Default | Meaning |
|---|---|---|
| `report_minutes` | `60` | Duty starts this long before the first departure |
| `debrief_minutes` | `30` | Duty ends this long after the last arrival |
| `min_rest_hours` | `12.0` | Minimum rest between two duties |
| `rest_at_least_previous_duty` | `true` | Rest must also be at least as long as the preceding duty |
| `max_duty_hours_day` | `13.0` | Duty hours starting on one calendar day; longer duties are rejected at load |
| `max_duty_hours_7d` | `60.0` | Duty hours in any 7 consecutive days |
| `max_duty_days_7d` | `5` | Duty days in any 7 consecutive days |
| `max_duty_hours_month` | `190.0` | Duty hours in the planning period |

### `[pay]`

| Key | Default | Meaning |
|---|---|---|
| `overtime_threshold_hours` | `110.0` | Monthly hours before overtime for a full time pilot; scaled by FTE and period length |
| `overtime_premium` | `0.5` | Overtime hours cost an extra 50% of the hourly rate |

### `[objective]`

| Key | Default | Meaning |
|---|---|---|
| `mode` | `weighted` | `cost`, `fairness` or `weighted` |
| `fairness_weight_total` | `20.0` | £ per hour of deviation from a pilot's fair target, summed over pilots |
| `fairness_weight_max` | `100.0` | £ per hour of the worst deviation in each pool |
| `fairness_tolerance_hours` | `4.0` | Deviation within ± this many hours is not penalised |
| `uncovered_penalty` | `50000.0` | £ per unfilled seat |

### `[solver]`

| Key | Default | Meaning |
|---|---|---|
| `elastic_coverage` | `true` | Allow unfilled seats at a penalty instead of failing |
| `decompose_by_pool` | `true` | Solve each crew pool as its own model |
| `time_limit_seconds` | `60` | Per model, so per pool when decomposing |
| `gap_rel` | `0.005` | Stop when proven within 0.5% of the best possible |

### Scenarios: `config/scenarios.toml`

Each scenario overrides any part of the configuration and can add leave
waves. A `baseline` scenario is required.

```toml
[[scenario]]
name = "rest_14h"
group = "rules"                         # rules | objective | disruption
description = "Tighter rest: 14h minimum"
overrides = { rules = { min_rest_hours = 14.0 } }

[[scenario]]
name = "sickness_wave_edi_b737"
group = "disruption"
description = "3 Edinburgh 737 captains sick on days 8 to 14"
leave_waves = [ { pool_id = "EDI-B737-CPT", count = 3, start_day = 8, end_day = 14 } ]
```

---

## 8. Input data contracts

Files are read from `data/raw/`. All columns are required. Extra columns are
logged and ignored. A missing file or column stops the run before any data is
loaded.

### `bases.csv`

| Column | Type | Example | Rules |
|---|---|---|---|
| `base_code` | text | `MAN` | Upper cased and trimmed |
| `base_name` | text | `Manchester` | |
| `timezone` | text | `Europe/London` | Informational |

### `aircraft_types.csv`

| Column | Type | Example | Rules |
|---|---|---|---|
| `type_code` | text | `A320` | Upper cased and trimmed |
| `description` | text | `Airbus A320` | |
| `captains_required` | integer | `1` | Seats per duty |
| `first_officers_required` | integer | `1` | Seats per duty |

### `crew.csv`

| Column | Type | Example | Rules |
|---|---|---|---|
| `crew_id` | text | `C0001` | Unique; conflicting duplicates are rejected |
| `rank` | text | `CPT` | `CPT`, `Captain`, `FO`, `First Officer` accepted; anything else rejected |
| `base` | text | `MAN` | Must exist in `bases.csv` |
| `seniority_years` | integer | `12` | |
| `hourly_rate_gbp` | decimal | `151.20` | Must be positive |
| `fte` | decimal | `0.75` | Must be in (0, 1] |

### `crew_qualifications.csv`

| Column | Type | Example | Rules |
|---|---|---|---|
| `crew_id` | text | `C0001` | Must be a clean crew member |
| `aircraft_type` | text | `A320` | Must exist in `aircraft_types.csv` |
| `qualified_from` | ISO date | `2014-10-03` | |
| `licence_expiry` | ISO date | `2027-06-30` | Pilot cannot fly a duty that ends after this date |

### `leave.csv`

| Column | Type | Example | Rules |
|---|---|---|---|
| `leave_id` | text | `L00012` | |
| `crew_id` | text | `C0001` | Must be a clean crew member |
| `leave_type` | text | `ANNUAL` | `ANNUAL`, `SICK`, `TRAINING`, `OTHER` |
| `start_date` | ISO date | `2026-10-05` | Non ISO dates are rejected, never guessed |
| `end_date` | ISO date | `2026-10-12` | Inclusive; must not be before `start_date` |

### `flights.csv`

| Column | Type | Example | Rules |
|---|---|---|---|
| `flight_id` | text | `F000123` | Unique; conflicting duplicates reject the duty |
| `flight_number` | text | `ZX1123` | |
| `duty_id` | text | `D20261005-MAN-A320-02` | Groups legs into a duty |
| `leg_seq` | integer | `1` | Order within the duty |
| `aircraft_type` | text | `A320` | Must exist; one type per duty |
| `dep_airport`, `arr_airport` | text | `MAN`, `PMI` | Legs must chain; duty must start and end at the same base |
| `dep_time_utc`, `arr_time_utc` | timestamp | `2026-10-05 06:30` | `YYYY-MM-DD HH:MM` or `YYYY/MM/DD HH:MM`; arrival after departure |

### Seed 42 volumes

| Table | Raw rows | Clean rows |
|---|---|---|
| crew | 139 | 136 |
| crew_qualifications | 140 | 136 |
| leave | 77 | 73 |
| flights | 2,254 | 2,236 legs in 740 duties |

---

## 9. Data pipeline and validation

### Policy

**Reject at row level, block at dataset level.**

- A bad row is moved to `rejected_rows` with a rule name and detail. It is
  never silently dropped.
- A duty with any bad leg is rejected as a whole, because a pilot cannot fly
  half a duty.
- Recoverable formatting issues are repaired only when the fix is unambiguous.
- The run is blocked when the problem is systemic, for example more than 5%
  of flights rejected.

### Cleaning rules (`sql/02_clean.sql`)

| Table | Repaired | Rejected |
|---|---|---|
| crew | Whitespace, case, rank text variants, exact duplicates | Conflicting duplicate id, invalid rank, unknown base, missing or non positive pay, FTE out of range |
| qualifications | Whitespace, case | Unknown crew, unknown aircraft type, invalid date |
| leave | Whitespace, case | Unknown crew, invalid or ambiguous date, end before start, unknown leave type |
| flights | Airport code case and spaces, slash timestamps, exact duplicates | Conflicting duplicate id, unparseable time, arrival not after departure, unknown type, missing duty |
| duties | | Contains a rejected leg, mixed aircraft types, duplicate leg sequence, broken leg chain, overlapping legs, not a round trip from a base, longer than the daily limit, outside the period |

### Validation gate (`sql/validation_checks.sql`)

Each check is one SQL query that returns one row per problem.

| Check | Severity | Purpose |
|---|---|---|
| `no_crew_after_cleaning` | ERROR | Nothing to roster |
| `no_duties_after_cleaning` | ERROR | Nothing to cover |
| `rejected_flight_share_too_high` | ERROR | Likely a broken export |
| `fleet_without_qualified_crew` | ERROR | A base flies a type with no rated pilots of a required rank |
| `seat_with_no_eligible_crew` | WARNING | Every rated pilot is on leave or out of licence for that duty |
| `crew_without_usable_qualification` | WARNING | Pilot cannot fly any day this period |
| `licence_expires_in_period` | WARNING | Renewal needed mid month |
| `overlapping_leave_records` | WARNING | Double counted leave in HR data |
| `pool_needs_overtime` | WARNING | Required hours exceed straight time capacity |
| `rejected_rows_summary` | INFO | Counts by table and rule |

### Seed 42 validation result

- 22 injected defects: every one repaired or quarantined as expected (tested).
- 5 duties rejected, 0 ERROR checks, warnings for 2 unusable pilots, 6
  licences expiring mid month, 2 overlapping leave records and 3 pools needing
  overtime, including Edinburgh 737 captains at 131% of straight time
  capacity.

---

## 10. Optimisation model

A full plain language explanation of every element, with the reasoning and
real world cases, is in [docs/02_model_formulation.md](docs/02_model_formulation.md).

### Sets

| Symbol | Meaning |
|---|---|
| C | Pilots |
| D | Duties |
| E ⊆ C × D | Eligible (pilot, duty) pairs from the SQL `eligibility` view |
| S | Seats: (duty, rank) with a required count |
| P | Crew pools: pilots with the same base, rank and type rating |
| W | Rolling 7 day windows, one starting on each day |

### Decision variables

| Variable | Domain | Meaning |
|---|---|---|
| `x[c,d]` | {0, 1} | Pilot c flies duty d; created only for eligible pairs |
| `u[d,r]` | [0, required] | Unfilled seats of rank r on duty d (fixed to 0 in strict mode) |
| `o[c]` | ≥ 0 | Overtime hours of pilot c |
| `p[c]`, `n[c]` | ≥ 0 | Hours above and below the fair target, outside the tolerance band |
| `b[c]` | [-4, 4] | Free deviation inside the tolerance band |
| `m[p]` | ≥ 0 | Worst deviation in pool p |

Helper expression: `H[c] = Σ_d hours[d] · x[c,d]`, total duty hours of pilot c.

### Constraints

| Family | Formulation | Plain meaning |
|---|---|---|
| coverage | `Σ_{c eligible, rank r} x[c,d] + u[d,r] = required[d,r]` | Every seat filled exactly once, or counted as open |
| rest | `Σ_{d: report[d] ≤ t < release[d] + rest[d]} x[c,d] ≤ 1` for each duty start t | No two duties closer than the required rest; `rest[d] = max(12h, length of d)` |
| daily_hours | `Σ_{d on day t} hours[d] · x[c,d] ≤ 13` | Only added when rest does not already imply it |
| hours_7d | `Σ_{d in window w} hours[d] · x[c,d] ≤ 60` | Rolling 7 day hours |
| days_7d | `Σ_{d in window w} x[c,d] ≤ 5` | Rolling 7 day duty days |
| hours_month | `H[c] ≤ 190` | Period hours |
| overtime | `o[c] ≥ H[c] − 110 · fte[c] · days / 31` | Hours above the prorated threshold |
| fairness | `H[c] − target[c] = p[c] − n[c] + b[c]`, `m[pool] ≥ p[c] + n[c]` | Deviation from a fair share |

Fair target: `target[c] = pool hours × (fte[c] × available_days[c]) / Σ_pool (fte × available_days)`.

Constraints that can never bind (for example a window whose eligible duties
total less than 60 hours) are not generated.

### Objective

```
minimise   Σ_c rate[c] · H[c]                    pay
         + 0.5 · Σ_c rate[c] · o[c]              overtime premium
         + 20  · Σ_c (p[c] + n[c])               fairness, every pilot
         + 100 · Σ_p m[p]                        fairness, worst pilot per pool
         + 50,000 · Σ u                          uncovered seats
```

| Mode | Objective |
|---|---|
| `weighted` (default) | All terms |
| `cost` | Pay, overtime, uncovered |
| `fairness` | Fairness, uncovered, plus pay × 0.001 as a tie break |

### Modelling decisions

| Decision | Alternative considered | Why this choice |
|---|---|---|
| Assign pilots to duties | Assign pilots to flights | Legality of a sequence is settled once in pairing; far fewer variables |
| Eligibility filtered in SQL | Create all pairs, constrain in the model | 15,897 variables instead of 100,640; exact, not an approximation |
| Rest as clique constraints | One constraint per clashing pair | Exact and a much tighter LP relaxation; verified against brute force |
| Elastic coverage | Hard coverage | Airlines publish open time for reserves; a usable roster beats "Infeasible" |
| Rolling 7 day windows | Calendar weeks | Matches rule wording; calendar weeks allow 120h across a week boundary |
| Targets prorated by FTE and availability | Equal hours per pilot | Part timers and pilots returning from leave are treated fairly |
| Total plus worst case fairness | Only one of them | Total alone lets one pilot absorb a large deviation; worst case alone ignores everyone else |
| 4 hour tolerance band | No band | Integer duties cannot hit exact targets; the band removed that noise and cut solve time 33x |
| Pool decomposition | One model | No constraint links pools, so the result is identical and much faster |
| 0.5% gap tolerance | Prove to 0% | 0.5% is about £7k a month, finer than the accuracy of pay and leave data |

---

## 11. Solving, status and performance

### Status reporting

| Status | Meaning |
|---|---|
| `OPTIMAL` | Proven within the gap tolerance |
| `FEASIBLE` | A legal roster, but the time limit stopped the proof; gap reported |
| `INFEASIBLE` | No roster satisfies the hard constraints |
| `NO_SOLUTION_FOUND` | Time ran out before any roster was found |

Implementation notes:

- The gap is recomputed from objective and bound, because CBC prints it
  rounded to two decimals (0.4% shows as `0.00`).
- When CBC stops on a time limit PuLP can still report "Optimal"; the wrapper
  reads CBC's own result line and reports `FEASIBLE`.
- For decomposed runs, overall status is the worst pool status, and objective
  and bound are summed across pools.

### Measured solve times

Hardware and software as in [Results summary](#3-results-summary). Limit 60s
per pool unless stated. Raw logs in [docs/measurements/](docs/measurements/).

| Run | Status | Solve seconds | Gap |
|---|---|---|---|
| Baseline, 12 pools, 4h band | All OPTIMAL | 9.4 to 9.7 | 0.28% |
| Baseline, 12 pools, no band | 5 pools on time limit | 311.6 | 0.44% |
| Baseline, one combined model, 300s limit | Time limit | 304.7 | 0.56% |
| rest_10h_flat | OPTIMAL | 41.4 | 0.27% |
| rest_14h | FEASIBLE | 69.2 | 0.29% |
| rest_16h | OPTIMAL | 31.5 | 0.24% |
| max_50h_7d | FEASIBLE | 95.3 | 0.26% |
| max_4_days_7d | OPTIMAL | 38.8 | 0.22% |
| cost_only | OPTIMAL | 9.6 | 0.31% |
| fairness_weight_5 | OPTIMAL | 10.9 | 0.21% |
| fairness_weight_80 | FEASIBLE | 191.9 | 0.45% |
| fairness_only | FEASIBLE | 272.3 | 6.9% of a small fairness score |
| sickness_wave_edi_b737 | OPTIMAL | 9.2 | 0.26% |
| Cost only, 0.05% gap target | Stopped at 0.15% | 666.2 | 0.15% |
| Full `run-all` | Exit 0 | 833 wall clock | |

### Model size (baseline)

| Constraint family | Rows |
|---|---|
| rest (cliques) | 9,911 |
| days_7d | 3,188 |
| hours_7d | 3,154 |
| coverage | 1,480 |
| fairness links | 272 |
| overtime links | 136 |
| hours_month | 133 |
| daily_hours | 0 (all implied by rest) |
| **Total** | **18,274** |

---

## 12. Infeasibility diagnosis

A solver reports "Infeasible" without saying why. `python -m crew_roster diagnose`
answers which duties, which pool and which rule.

| Step | Method | Cost | Output |
|---|---|---|---|
| 1 | Capacity prechecks: daily seats vs available pilots, 7 day seats vs `min(5, available days)`, hours vs monthly cap | Seconds, no solver | `capacity_precheck.csv` |
| 2 | Elastic solve minimising uncovered seats | One solve | `min_uncovered_seats.csv` |
| 3 | Remove each rule family alone and re solve | One solve per family | `single_family_relaxations.csv` |
| 4 | Deletion filter over rule families | Up to one solve per family | `conflict_sets.json`: an irreducible set of conflicting families |

### Demo result

`diagnose --pool EDI-B737-CPT --count 3 --days 8-14`

| Step | Result |
|---|---|
| Strict solve | INFEASIBLE |
| Precheck | Days 8 to 14 need 22 captain seats; at most 19 duty days available under the 5 in 7 rule |
| Minimum uncovered | 3 seats, all EDI 737 captain |
| Single relaxations | Only removing `days_7d` makes it feasible |
| Conflict set | `["days_7d"]` |

With `--count 4`, no single relaxation fixes it and both rest and the 5 in 7
rule conflict independently. The conflict set found depends on search order:
an irreducible infeasible set is not unique.

---

## 13. Output verification

`src/crew_roster/analysis/consistency.py` re checks every roster using only
the clean input tables and pandas. It does not import the model or the SQL
eligibility view.

| Group | Checks |
|---|---|
| Integrity | `unknown_crew_or_duty`, `duplicate_assignment` |
| Coverage | `seat_overfilled`, `coverage_not_reconciled`, `seat_uncovered` |
| Eligibility | `base_mismatch`, `rank_not_required`, `not_rated_on_type`, `licence_not_valid`, `rostered_on_leave` |
| Rest and limits | `overlapping_duties`, `rest_violation`, `daily_hours_exceeded`, `hours_7d_exceeded`, `duty_days_7d_exceeded`, `month_hours_exceeded` |
| Model assumptions | `model_assumption_one_duty_per_day` |
| Reconciliation | `objective_not_reconciled` (pay, overtime, fairness, uncovered seats) |
| Planning signals | `available_pilot_unused`, `month_cap_binding`, `overtime_used` |

Results: 0 errors in the baseline roster and in all 11 scenario rosters.
Tests corrupt valid rosters (rest breach, pilot on leave, double covered seat,
dropped assignment, misreported cost, a looser weekly cap than the rule, two
duties in one day) and assert the right check fires.

---

## 14. Scenario analysis

Each scenario uses the same data and solver; only the listed settings change.
Every scenario roster is checked against its own rules.

### Comparison safeguards

| Column | Purpose |
|---|---|
| `cost_comparable_to_baseline` | False when coverage differs, because open seats make pay look lower |
| `proven_worse_than_baseline` | Scenario lower bound above baseline objective |
| `proven_better_than_baseline` | Scenario objective below baseline lower bound |
| `difference_within_solver_gap` | Neither is proven; the difference may be solver tolerance |

### Rule scenarios

| Scenario | Pay + overtime vs baseline | Uncovered seats | Verdict |
|---|---|---|---|
| rest_10h_flat | +£686 | 0 | Within solver gap |
| rest_14h | +£1,628 | 0 | Within solver gap; proven range £0 to £4,196 |
| max_50h_7d | +£1,406 | 0 | Within solver gap |
| rest_16h | Not comparable | 2 | Proven worse |
| max_4_days_7d | Not comparable | 4 | Proven worse |
| sickness_wave_edi_b737 | Not comparable | 3 | Proven worse |

### Objective scenarios (cost vs fairness)

| Scenario | Pay + overtime | vs cost only | Mean abs deviation | Max deviation | Available pilots with no duties |
|---|---|---|---|---|---|
| cost_only | £1,394,809 | | 27.8h | 108.5h | 14 |
| fairness_weight_5 | £1,405,337 | +£10,528 | 13.9h | 56.8h | 0 |
| baseline (weight 20) | £1,428,731 | +£33,922 | 3.7h | 17.0h | 0 |
| fairness_weight_80 | £1,436,208 | +£41,399 | 2.6h | 6.5h | 0 |
| fairness_only | £1,437,638 | +£42,829 | 2.2h | 4.5h | 0 |

### Recommendations from the analysis

1. **Staffing, not rules, is the constraint.** Add one to two 737 captains at
   Edinburgh or a standing reserve for that pool; it fails under every stress
   scenario.
2. **Rest from 12h to 14h is affordable on this timetable.** Proven cost is
   at most £4,196 a month with full coverage.
3. **Keep the default fairness weight.** It sits at the knee of the trade
   off: beyond it each hour of fairness costs about five times more.

Full analysis: [docs/03_output_analysis.md](docs/03_output_analysis.md).

---

## 15. Outputs and Power BI

### Output folders

| Path | Contents |
|---|---|
| `outputs/roster/` | `assignments.csv`, `uncovered_seats.csv`, `pool_results.csv`, `solve_summary.json`, `crew_hours.csv`, `roster_check_issues.csv` |
| `outputs/diagnosis/` | Precheck, minimum uncovered seats, single relaxations, conflict sets |
| `outputs/scenarios/<name>/` | Assignments, uncovered seats, pool results, crew hours, check issues, scenario leave |
| `outputs/scenarios/scenario_comparison.csv` | One row per scenario with KPIs, deltas and proof flags |
| `outputs/powerbi/` | Star schema tables |
| `outputs/logs/crew_roster.log` | Run log |

### Power BI star schema

| Table | Grain | Rows (seed 42) |
|---|---|---|
| `dim_crew` | Pilot | 136 |
| `dim_duty` | Duty | 740 |
| `dim_date` | Day | 31 |
| `dim_scenario` | Scenario | 11 |
| `fact_assignment` | Scenario, pilot, duty | 16,271 |
| `fact_crew_period` | Scenario, pilot | 1,496 |
| `fact_crew_day` | Scenario, pilot, day (DUTY / LEAVE / UNAVAILABLE / OFF) | 46,376 |
| `fact_seat_coverage` | Scenario, duty, rank | 16,280 |
| `fact_scenario_summary` | Scenario | 11 |
| `fact_pool_solve` | Scenario, pool | 132 |
| `fact_data_quality` | Issue (input, cleaning and output stages) | 76 |

Dashboard pages specified: roster overview, fairness, roster heatmap,
scenario comparison, solver and data quality. Relationships, DAX measures and
visual layout: [docs/powerbi_dashboard_spec.md](docs/powerbi_dashboard_spec.md).

---

## 16. Testing and CI

```bash
pytest -q                          # all 59 tests
pytest tests/test_model.py -q      # one module
```

| Module | Tests | Covers |
|---|---|---|
| `test_generate.py` | 4 | Determinism, defect manifest, duty structure |
| `test_pipeline.py` | 8 | Every defect quarantined, normalisation, eligibility, schema fail fast, scenario leave |
| `test_validation.py` | 6 | Check parsing, each blocking error, uncovered seat warning |
| `test_model.py` | 18 | Clique rows vs brute force, known answer instances for each constraint and mode, strict vs elastic, decomposition bounds, CBC log parsing |
| `test_diagnose.py` | 6 | Conflict sets, prechecks, full diagnosis |
| `test_consistency.py` | 8 | Clean roster passes; each corruption is caught |
| `test_scenarios.py` | 4 | Scenario file validity, rule monotonicity, leave waves, comparison deltas |
| `test_powerbi.py` | 5 | Key uniqueness, fact grains, referential integrity, cross table totals |

GitHub Actions (`.github/workflows/ci.yml`) runs the suite on Ubuntu with
Python 3.11 on every push to `main` and every pull request.

---

## 17. Logging and operations

- Logs go to the console and `outputs/logs/crew_roster.log`. Level is set by
  `[logging] level`.
- Each run logs raw and clean row counts, every quarantine rule with counts,
  each validation check result, model size per pool, solver status and time
  per pool, checker results and the scenario comparison table.
- `run-all` stops at the first failing step and returns its exit code, so it
  can be scheduled by any job runner.
- The database is rebuilt from raw files on every `prepare`; runs are
  idempotent.

Example log lines:

```
INFO  crew_roster.data.pipeline: Quarantined 1 duties row(s): exceeds_max_duty_hours_day
WARN  crew_roster.data.validate: [WARNING] pool_needs_overtime: 3 issue(s), e.g. EDI-A320-FO: 799 hours required, 788 straight time hours available (101%)
INFO  crew_roster.model.roster: Pool MAN-A320-CPT   OPTIMAL in 1.0s
INFO  crew_roster: Status OPTIMAL, objective 1435056.02, 1480 assignments, 0 uncovered seats
```

---

## 18. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `SchemaError: crew.csv is missing columns` | Input file does not match the contract | Check section 8 |
| `prepare` exits with code 2 | A validation ERROR | Read `validation_issues` in `data/roster.db` or the log |
| CBC crashes or hangs on Windows | The bundled CBC 2.10.3 is unstable when a thread count is passed | Do not add a threads option; this project deliberately omits it |
| Status `FEASIBLE` instead of `OPTIMAL` | A pool hit `time_limit_seconds` | Raise the limit or relax `gap_rel`; the reported gap is still valid |
| Solve slow after changing fairness settings | `fairness_tolerance_hours = 0` weakens the bound | Keep a band of about half a typical duty |
| Uncovered seats in the roster | No legal assignment exists for those seats | Run `diagnose` on the pool shown in `uncovered_seats.csv` |
| `check` exits with code 1 | The roster breaks a rule | See `roster_check_issues.csv`; indicates a model or data bug |
| Unicode errors when editing files with Python on Windows | Default cp1252 encoding | Open files with `encoding="utf-8"` |

---

## 19. Assumptions

- Duties come from an upstream pairing step and start and end at a base.
- All times are UTC; the three bases share one time zone, so acclimatisation
  does not arise.
- A duty belongs to the calendar day it reports on; duty time runs from report
  (60 minutes before departure) to release (30 minutes after arrival).
- Each pilot holds one type rating, so crew pools do not overlap. Checked at
  run time.
- At most one duty starts per pilot per day. The duty day constraint relies on
  this; the checker verifies it on every roster.
- The month stands alone: no hours or rest carried in from the previous month.
- Leave is in whole days; a duty touching any part of a leave day is not
  allowed.
- Pay is hourly on duty hours with a 50% overtime premium above 110h × FTE,
  prorated to the period length.
- An uncovered seat is priced at £50,000 so it is used only when no legal
  assignment exists.

---

## 20. Scope and limitations

This is a faithful core of a rostering system, not a production crew
management product. A real airline system would also need:

**Regulation**
- The full flight time limitation scheme (for example EASA ORO.FTL or the UK
  CAA equivalent): flight duty period limits by report time and number of
  sectors, acclimatisation, split duty, extended recovery rest of 36 hours
  including two local nights, disruptive schedules, and block hour limits
  (100 in 28 days, 900 in a calendar year, 1,000 in 12 months). This project
  uses simplified duty hour windows and a period cap in place of 28 day
  rolling limits.
- Carry in of hours and rest from the previous roster period.
- Standby and reserve duties, positioning and deadheading between bases.
- Recency, route and airport qualifications, and experience pairing
  restrictions.
- Cabin crew rostering.

**People and pay**
- Preferential bidding for days off and trips, usually in seniority order.
- Training (simulator, line checks) scheduled within the roster.
- Collective agreement pay rules: guarantees, per diems, sector pay.

**Scale and method**
- Pairing optimisation upstream, typically set partitioning with column
  generation.
- Column generation or branch and price for rostering, and commercial solvers
  (Gurobi, CPLEX) for large fleets. The single combined model here already
  failed to prove a 0.5% gap in 300 seconds for 136 pilots.
- Disruption recovery: re rostering on the day of operations while minimising
  changes to published rosters.

**Operations**
- Integration with HR, scheduling (SSIM) and crew management systems.
- Local time aware timestamps, a production database, audit trails of
  published rosters, scheduling and monitoring.

---

## 21. Project structure

```
crew-rostering-optimisation/
├── config/
│   ├── default.toml               rules, pay, objective, solver settings
│   └── scenarios.toml             what if scenarios
├── data/raw/                      generated input CSVs and defect manifest
├── sql/
│   ├── 01_staging.sql             TEXT staging tables
│   ├── 02_clean.sql               normalisation, quarantine, duty building
│   ├── 03_model_inputs.sql        calendar, pools, eligibility
│   └── validation_checks.sql      dataset level gate
├── src/crew_roster/
│   ├── cli.py                     command line entry point
│   ├── config.py                  typed configuration
│   ├── logging_setup.py
│   ├── data/
│   │   ├── generate.py            synthetic data and defects
│   │   ├── pipeline.py            CSV to SQLite, runs SQL stages
│   │   ├── validate.py            runs validation checks
│   │   └── scenario_leave.py      leave waves for scenarios
│   ├── model/
│   │   ├── data.py                ModelData
│   │   ├── formulation.py         the MIP
│   │   ├── solve.py               CBC wrapper and status reporting
│   │   ├── roster.py              pool decomposition
│   │   └── diagnose.py            infeasibility diagnosis
│   ├── analysis/
│   │   ├── consistency.py         independent roster checker
│   │   └── scenarios.py           scenario engine and comparison
│   └── reporting/
│       └── powerbi.py             star schema export
├── tests/                         59 pytest tests
├── docs/                          learning guide, Power BI spec, measurements
├── outputs/                       committed results of run-all
├── .github/workflows/ci.yml
└── pyproject.toml
```

---

## 22. Further documentation

| Document | Contents |
|---|---|
| [docs/01_data_layer.md](docs/01_data_layer.md) | Pairing vs rostering, eligibility filtering, quarantine policy, validation gate |
| [docs/02_model_formulation.md](docs/02_model_formulation.md) | MIP basics, relaxation and bounds, each constraint explained, fairness pricing, decomposition, infeasibility and IIS, measured results |
| [docs/03_output_analysis.md](docs/03_output_analysis.md) | Honest status reporting, independent checking, comparing scenarios within the solver gap, findings |
| [docs/powerbi_dashboard_spec.md](docs/powerbi_dashboard_spec.md) | Data model, relationships, DAX measures, page layouts |
| [docs/measurements/](docs/measurements/) | Raw solver timing logs |

---

## 23. Glossary

| Term | Meaning |
|---|---|
| Duty (duty period) | Continuous working time from report to release, containing one or more flights |
| Pairing | A legal sequence of flights forming a duty that starts and ends at a base |
| Seat | One crew position on a duty, e.g. the captain seat |
| Crew pool | Pilots who can swap duties: same base, rank and type rating |
| Type rating | Licence endorsement to fly a specific aircraft type |
| FTE | Full time equivalent contract fraction |
| Open time | Duties left without crew, later covered by reserves |
| MIP | Mixed-Integer Program: optimisation with some whole number variables |
| LP relaxation | The MIP with whole number requirements removed; solves fast and gives a bound |
| Lower bound | A value no legal roster can beat, proven by the solver |
| Gap | `(objective − lower bound) / objective`: how far from proven best |
| Clique constraint | One constraint covering a group of mutually conflicting choices |
| Elastic constraint | A constraint that may be broken at a penalty |
| Decomposition | Splitting a model into independent smaller models |
| IIS | Irreducible Infeasible Subsystem: a set of constraints that conflict, where removing any one resolves it |
| FDP | Flight Duty Period, the regulated duty measure in flight time limitations |
