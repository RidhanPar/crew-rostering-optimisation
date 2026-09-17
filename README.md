# Crew Rostering Optimisation

Assigns airline pilots to a month of duty periods with a Mixed-Integer Program
(PuLP and CBC). It respects rest rules, daily, weekly and monthly duty caps,
type ratings, licence expiry, crew bases and leave, and trades pay cost
against fairness. The model is wrapped in a validated SQL data layer, an
independent roster checker, a scenario comparison and Power BI ready outputs.

All data is **synthetic** (seed 42): a fictional UK short haul airline with 3
bases, 136 pilots, 740 duty periods and 2,236 flight legs in October 2026.
Every number below comes from committed outputs and can be regenerated with
one command.

## Results at a glance

| | |
|---|---|
| Baseline roster | 1,480 seats covered, 0 uncovered, 0 errors from the independent checker |
| Proven quality | Objective £1,435,056, lower bound £1,431,061, gap 0.28% |
| Solve time | 9.7s for all 12 crew pools (i5-10200H laptop, CBC single thread) |
| Fairness band effect | Same month took 311.6s with 5 pools on time limits before the band, 9.4s with it |
| Decomposition effect | One combined model hit a 300s limit at 0.56% gap, £4,107 worse |
| Infeasibility demo | 3 Edinburgh 737 captains sick: diagnosis names the 5 duties in 7 rule as the conflict |
| Rest 12h to 14h | Costs between £0 and £4,196 a month, proven by bounds; 16h rest leaves seats uncovered |
| Cost vs fairness | Ignoring fairness saves £33,922 (2.4%) but leaves 14 available pilots with no flying |
| Full pipeline | `run-all` (generate to Power BI, 11 scenarios) in 833s |

## Quick start

```bash
pip install -e ".[dev]"
python -m crew_roster run-all
pytest -q
```

Or step by step:

```bash
python -m crew_roster generate      # raw CSVs with 22 injected data defects
python -m crew_roster prepare       # SQL cleaning, quarantine, validation gate
python -m crew_roster solve         # roster to outputs/roster
python -m crew_roster check         # independent re check of that roster
python -m crew_roster diagnose      # make a pool infeasible and explain why
python -m crew_roster scenarios     # 11 what ifs to outputs/scenarios
python -m crew_roster report        # Power BI star schema to outputs/powerbi
```

## How it fits together

```
data/raw/*.csv
   |  schema check (Python)
   v
SQLite: stg_* (TEXT) -> 02_clean.sql (normalise, quarantine to rejected_rows, build duties)
   |                 -> 03_model_inputs.sql (calendar, crew pools, eligibility view)
   |                 -> validation_checks.sql (ERROR blocks, WARNING flags)
   v
ModelData -> 12 pool models (PuLP) -> CBC -> SolveResult (status, bound, gap, roster)
   |                                            |
   |                          independent checker (pandas, no model imports)
   v                                            v
diagnose (prechecks, elastic, conflict set)   scenarios -> comparison -> Power BI CSVs
```

## Model formulation

Full plain language walk through with the reasoning for each choice:
[docs/02_model_formulation.md](docs/02_model_formulation.md).

**Sets.** Pilots C, duties D, eligible pairs E ⊆ C × D (from SQL), seats
S = (duty, rank), pools P (same base, rank and type rating), 7 day windows W.

**Variables.**

```
x[c,d]  ∈ {0,1}        pilot c flies duty d, only for (c,d) in E
u[d,r]  ∈ [0, req]     unfilled seats (fixed to 0 in strict mode)
o[c]    ≥ 0            overtime hours
p[c], n[c] ≥ 0         hours above / below fair target, outside the band
b[c]    ∈ [-4, 4]      free deviation inside the fairness band
m[p]    ≥ 0            worst deviation in pool p
H[c]    = Σ_d hours[d] · x[c,d]
```

**Constraints.**

```
coverage     Σ_{c eligible, rank r} x[c,d] + u[d,r] = req[d,r]              every seat
rest         Σ_{d : report[d] ≤ t < release[d] + rest[d]} x[c,d] ≤ 1          every pilot, every duty start t
             rest[d] = max(12h, duty length of d)
daily_hours  Σ_{d starting day t} hours[d] · x[c,d] ≤ 13                     only where not implied by rest
hours_7d     Σ_{d in window w} hours[d] · x[c,d] ≤ 60                        every pilot, every rolling window
days_7d      Σ_{d in window w} x[c,d] ≤ 5                                    every pilot, every rolling window
hours_month  H[c] ≤ 190
overtime     o[c] ≥ H[c] − 110 · fte[c] · days / 31
fairness     H[c] − target[c] = p[c] − n[c] + b[c],   m[pool(c)] ≥ p[c] + n[c]
             target[c] = pool hours · (fte[c] · available_days[c]) / Σ_pool (fte · available_days)
```

**Objective (weighted mode, default).**

```
minimise   Σ_c rate[c] · H[c]  +  0.5 · Σ_c rate[c] · o[c]         pay and overtime premium
         + 20 · Σ_c (p[c] + n[c])  +  100 · Σ_p m[p]                fairness, as a price in £
         + 50,000 · Σ u                                             uncovered seats
```

Modes `cost` and `fairness` drop one part (fairness keeps cost × 0.001 as a
tie break). Every weight is in `config/default.toml`.

**Key modelling decisions**

| Decision | Why |
|---|---|
| Assign pilots to duties, not flights | Pairing (building legal duties) is a separate upstream problem in standard airline crew planning; rostering whole duties keeps the model small |
| Static rules in SQL, dynamic rules in the MIP | Base, rank, rating, licence and leave can be decided per pair, so no variable is created for impossible pairs (15,897 instead of 100,640) |
| Rest as clique constraints | Exact, and a much tighter relaxation than one row per clashing pair; tested against brute force |
| Elastic coverage by default | Airlines publish open time for reserves; a roster with 2 open seats is useful, "Infeasible" is not |
| Rolling 7 day windows | Rules say "any 7 consecutive days"; calendar weeks would allow 120h across a week boundary |
| Fairness target prorated by FTE and availability | A part timer back from leave should not be pushed to a full timer's hours |
| 4 hour fairness band | Duties come in ~8h blocks, so no integer roster hits a target exactly; charging for that weakened the bound and cost 33x in solve time |
| Solve each crew pool separately | No constraint links pools, so it is exact; checked at run time, falls back to one model if pilots hold two ratings |
| 0.5% gap tolerance | 0.5% of the monthly pay bill is about £7k, finer than the accuracy of the pay and leave data |

## Assumptions

- Duties arrive from an upstream pairing step and all start and end at a base.
- All times are UTC. The three bases share one time zone, so acclimatisation
  does not arise; local time effects on duty limits are out of scope.
- A duty belongs to the calendar day it reports on. Daily and weekly limits
  count duty hours from report to release (report 60 min before first
  departure, debrief 30 min after last arrival).
- Each pilot holds one type rating, so pools do not overlap (checked).
- At most one duty starts per pilot per day. The days_7d constraint relies on
  this; the checker verifies it on every output.
- The planning month stands alone: no hours or rest carried in from the
  previous month.
- Leave is whole days; a duty touching any part of a leave day is not allowed.
- Pay is hourly on duty hours, with overtime above 110h × FTE (prorated to the
  period length) at +50%.
- Uncovered seats are priced at £50,000 so the solver only leaves one open
  when no legal assignment exists.

## Constraints and where to change them

| Rule | Config key | Default |
|---|---|---|
| Minimum rest between duties | `rules.min_rest_hours` | 12h |
| Rest at least as long as the previous duty | `rules.rest_at_least_previous_duty` | true |
| Duty hours per calendar day | `rules.max_duty_hours_day` | 13h (also rejects longer duties at load) |
| Duty hours in any 7 days | `rules.max_duty_hours_7d` | 60h |
| Duty days in any 7 days | `rules.max_duty_days_7d` | 5 |
| Duty hours in the period | `rules.max_duty_hours_month` | 190h |
| Type rating and licence validity | data: `crew_qualifications.csv` | per pilot |
| Crew base | data: `crew.csv` | per pilot |
| Leave and unavailability | data: `leave.csv`, plus scenario leave waves | per pilot |
| Fairness weights and band | `objective.*` | £20 / £100 / ±4h |

## Measured solve times

Intel Core i5-10200H, Windows 11, Python 3.11.9, PuLP 3.3.2 with its bundled
CBC 2.10.3, single thread, 60s limit per pool, 0.5% gap. Raw logs in
[docs/measurements/](docs/measurements/). These are one machine's numbers;
rerun to get yours.

| Run | Status | Solve seconds | Gap |
|---|---|---|---|
| Baseline, 12 pools, 4h fairness band | all OPTIMAL | 9.4 to 9.7 | 0.28% |
| Baseline, 12 pools, no fairness band | 5 pools on time limit | 311.6 | 0.44% |
| Baseline, one combined model, 300s limit | time limit | 304.7 | 0.56% |
| Scenarios: rest 10h / 14h / 16h | OPTIMAL / FEASIBLE / OPTIMAL | 41.4 / 69.2 / 31.5 | 0.27% / 0.29% / 0.24% |
| Scenarios: 50h weekly cap / 4 days in 7 | FEASIBLE / OPTIMAL | 95.3 / 38.8 | 0.26% / 0.22% |
| Scenarios: cost only / fairness weight 80 / fairness only | OPTIMAL / FEASIBLE / FEASIBLE | 9.6 / 191.9 / 272.3 | 0.31% / 0.45% / 6.9% of fairness score |
| Cost only, 0.05% gap target | stopped at 0.15% | 666.2 | 0.15% |
| Full `run-all` | exit 0 | 833 wall | |

Model size (baseline): 17,933 variables, 15,897 binary, 18,274 constraints
(9,911 rest cliques, 3,188 duty day windows, 3,154 hour windows, 1,480
coverage rows).

## Data quality

`generate` injects 22 realistic defects (duplicate rows, lower case airport
codes, slash timestamps, swapped arrival and departure, aircraft type typos,
broken leg chains, conflicting duplicate IDs, an illegal 13.6h duty, unknown
bases, missing pay rates, ambiguous `03/10/2026` dates). The pipeline repairs
the unambiguous ones, quarantines the rest into `rejected_rows` with a rule
name, rejects a whole duty when any leg is bad, and blocks the run if more
than 5% of flights are rejected. Tests assert every injected defect is caught.
See [docs/01_data_layer.md](docs/01_data_layer.md).

## Output analysis

- **Status**: OPTIMAL only when CBC proved the gap; a time limit with a roster
  is FEASIBLE, never Optimal.
- **Independent checker**: 21 checks re derived from the rule text in pandas,
  plus reconciliation of the reported cost and fairness. Tests corrupt valid
  rosters and assert each check fires.
- **Scenario comparison**: flags costs that are not comparable (different
  coverage) and uses the bounds to separate proven differences from solver
  tolerance.

See [docs/03_output_analysis.md](docs/03_output_analysis.md) for the findings,
including the Edinburgh 737 captain pool that fails under every stress test.

## Power BI

`python -m crew_roster report` writes a star schema to `outputs/powerbi/`:
4 dimensions (crew, duty, date, scenario) and 7 facts (assignments, crew
period hours and cost, crew day grid, seat coverage, scenario summary, pool
solve stats, data quality). Every fact carries `scenario`. Model,
relationships, DAX measures and a 5 page layout are in
[docs/powerbi_dashboard_spec.md](docs/powerbi_dashboard_spec.md).

## Engineering

- **Config**: every rule, weight, path and solver setting in
  `config/default.toml`, typed dataclasses reject unknown or missing keys.
  Scenarios are overrides in `config/scenarios.toml`.
- **Logging**: one setup for console and `outputs/logs/crew_roster.log`;
  per pool status and timing, quarantine counts, validation results.
- **Tests**: 59 pytest tests: generator determinism, every quarantine rule,
  validation gate, known answer micro instances for each constraint, clique
  construction against brute force, strict vs elastic, decomposition bound
  agreement, diagnosis naming the right rule, checker catching corrupted
  rosters, scenario monotonicity, Power BI key integrity. CI on every push
  and pull request.
- **Solver notes**: the bundled CBC crashed or hung whenever `-threads` was
  passed on Windows, so no threads option is used; CBC prints its gap rounded,
  so the gap is recomputed from objective and bound.

```
config/            default.toml, scenarios.toml
sql/               01_staging, 02_clean, 03_model_inputs, validation_checks
src/crew_roster/
  data/            generate, pipeline, validate, scenario_leave
  model/           data, formulation, solve, roster (decomposition), diagnose
  analysis/        consistency (checker), scenarios
  reporting/       powerbi
  cli.py
tests/             59 tests
docs/              01 data layer, 02 formulation, 03 output analysis, Power BI spec, measurements
outputs/           diagnosis, scenarios, powerbi (committed results of run-all)
```

## Scope: what a real airline system would add

This is a faithful core, not a production crew system. An airline would need:

**Rules**
- The full flight time limitation scheme (for example EASA ORO.FTL or UK CAA
  equivalent): maximum flight duty period depending on report time and number
  of sectors, acclimatisation and time zones, split duties, extended recovery
  rest (36 hours including two local nights, at least every 168 hours),
  disruptive schedule limits, and block hour limits (100 in 28 days, 900 in a
  calendar year, 1,000 in 12 months). This project uses simplified duty hour
  windows and a period cap in place of 28 day rolling limits.
- Carry in from the previous month for every rolling window and rest period.
- Standby and reserve duties with their own rules, and positioning or
  deadheading between bases with hotel costs.
- Recency (take offs and landings in 90 days), route and airport
  qualifications, and pairing restrictions such as not rostering two low
  experience pilots together.
- Cabin crew, with different complements, rules and qualifications.

**People**
- Preferential bidding: pilots bid for days off, patterns and trips, usually
  resolved in seniority order. This is often the largest part of a real
  rostering objective.
- Training (simulator, line checks) scheduled into the same roster.
- Collective agreement pay: guarantees, per diems, sector pay, not only hourly.

**Scale and method**
- Pairing optimisation upstream, typically set partitioning with column
  generation.
- Rostering with column generation or branch and price over whole monthly
  lines, and commercial solvers (Gurobi, CPLEX) for large fleets; this
  project's monolithic model already struggled at one month and 136 pilots.
- Day of operations recovery: re rostering after disruption while minimising
  changes to published rosters (a stability objective).

**Operations**
- Integration with HR, scheduling (SSIM) and crew management systems, local
  time aware timestamps, a production database instead of SQLite, audit
  trails of who published which roster, scheduled runs and monitoring.

## Learning guide

The docs are written to teach the Operations Research concepts as they are
used, each with a real world case and an interview defence:

1. [Data layer](docs/01_data_layer.md): pairing vs rostering, eligibility
   filtering, reject at row level and block at dataset level.
2. [Model](docs/02_model_formulation.md): MIP basics, relaxation, bound and
   gap, clique constraints, fairness pricing, decomposition, infeasibility and
   IIS.
3. [Output analysis](docs/03_output_analysis.md): honest status reporting,
   independent checking, comparing scenarios inside the solver gap.
4. [Power BI spec](docs/powerbi_dashboard_spec.md): star schema, DAX, pages.
