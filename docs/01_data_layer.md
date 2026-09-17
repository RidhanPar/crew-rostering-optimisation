# Phase 1: the data layer

A solver does exactly what the data says. If a leave record has its dates
swapped, the solver will happily roster a pilot who is on a beach in Spain,
and it will report that roster as "optimal". So the first job is making sure
bad data never reaches the model.

## Concept 1: pairing first, rostering second

Airlines split crew planning into two problems.

1. **Crew pairing** builds legal duty periods out of flights. A duty is a
   sequence of legs that starts and ends at a crew base, for example
   MAN to PMI to MAN. Nobody is named yet.
2. **Crew rostering** assigns named people to those duties for the month.

This project solves step 2. The input `flights.csv` already carries a
`duty_id`, the way a pairing system would hand it over.

**Real world case.** The number of ways to chain 2,000 legs into legal
sequences, and then give each sequence to one of 150 named pilots, grows
combinatorially. Solving both at once is far too large for a monthly plan.
Splitting pairing from rostering is the standard approach in airline crew
planning.

**Interview defence.** "Why not assign people to flights directly?" Because
legality (a pilot must end the day at base, legs must chain) is a property of
a sequence of flights. Pairing settles that once. Rostering then only has to
reason about whole duties, which makes the model much smaller.

## Concept 2: eligibility filtering (sparse variables)

The model will have one yes/no decision per (pilot, duty) pair. We only
create that decision when the pair is possible at all:

| Static fact | Where it is checked |
|---|---|
| Pilot base equals duty base | `eligibility` view, SQL |
| Pilot rank is needed on the duty | `eligibility` view, SQL |
| Pilot is rated on the aircraft type | `eligibility` view, SQL |
| Licence valid until the duty ends | `eligibility` view, SQL |
| Pilot not on leave during the duty | `eligibility` view, SQL |

Rules that depend on the rest of the roster, such as rest between duties or
weekly hours, cannot be decided one pair at a time, so they live in the
solver (Phase 2).

**Real world case.** Without filtering, 136 pilots times 740 duties is about
100,600 binary variables. With filtering it is 15,897 (seed 42). Smaller models
solve faster and are easier to debug.

**Interview defence.** "Is filtering in SQL not hiding constraints from the
model?" No. Any constraint you can decide for a single pair, you should
decide before the solver. It is exact, not an approximation. The independent
roster checker in Phase 3 re checks these facts on the output anyway.

## Concept 3: reject at row level, block at dataset level

| Situation | What happens | Why |
|---|---|---|
| One leg has arrival before departure | The whole duty goes to `rejected_rows` | Crew cannot fly half a duty |
| Airport code is `" man "` | Normalised to `MAN` | Unambiguous, safe to fix |
| Timestamp `2026/10/05 06:00` | Normalised | Year first, so unambiguous |
| Leave date `03/10/2026` | Rejected | Could be 3 October or 10 March. Never guess |
| Same `flight_id`, different times | Both rows rejected | We cannot know which is right |
| Duty longer than 13 hours | Duty rejected | Illegal to fly, pairing team must fix it |
| More than 5% of flights rejected | Whole run blocked | Probably a broken export, not a few bad rows |

Nothing is silently dropped. Every rejection has a rule name and a detail
string, so the pairing or HR team can fix the source.

**Real world case.** An export job times out halfway and delivers 60% of
next month's flights. Row level checks all pass, because every row that
arrived is fine. Only a dataset level check (rejection share, row counts)
catches it. Without it, you publish a roster that ignores 40% of the flying.

## Pipeline stages

```
raw CSV  ->  schema check (Python, fail fast)
         ->  stg_* tables, all TEXT          sql/01_staging.sql
         ->  normalise, quarantine, duties   sql/02_clean.sql
         ->  calendar, eligibility, pools    sql/03_model_inputs.sql
         ->  validation gate                 sql/validation_checks.sql
```

Why load as TEXT first: a CSV has no types. If you let pandas guess, a
licence expiry of `2026-13-01` becomes NaT and disappears. Casting in SQL
means every failed cast can be written to `rejected_rows` with a reason.

## Validation gate

Checks live in `sql/validation_checks.sql`, one query per check, returning
one row per problem. Severity decides what happens:

- `ERROR` blocks the solve, for example a fleet with no rated first officers.
- `WARNING` lets the solve run, for example a licence expiring mid month, or
  a pool that needs more hours than its pilots can give before overtime.
- `INFO` is context, for example a summary of quarantined rows.

## Injected defects

`generate.py` deliberately adds 22 defects when `inject_dirty_rows = true`,
and lists each one in `data/raw/generation_manifest.json`. The test
`test_every_rejecting_defect_is_quarantined` asserts every defect that should
be rejected is rejected, and `test_normalisation_fixes_recoverable_defects`
covers the ones that should be repaired.

## Try it

```bash
python -m crew_roster generate
python -m crew_roster prepare
```

Then open `data/roster.db` and query `rejected_rows` and `validation_issues`.
