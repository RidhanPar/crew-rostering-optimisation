# Phase 3: output analysis

A solver answer has three questions attached before anyone should use it.

1. **Did it solve?** Optimal, feasible on a time limit, infeasible, or nothing.
2. **Is the roster actually legal?** Independent of what the model believes.
3. **What does it mean?** Compared with what, and is the difference real?

## Concept 1: reading solver status honestly

| Status | Meaning | What you may say |
|---|---|---|
| OPTIMAL | Proven within `gap_rel` (0.5%) of the best possible | "No legal roster is more than 0.5% better" |
| FEASIBLE | Legal roster found, time ran out before the proof | "Legal, and at most X% from best", using the reported gap |
| INFEASIBLE | No roster satisfies the hard rules | Run the diagnosis (Phase 2) |
| NO_SOLUTION_FOUND | Time ran out before any roster | Increase time, simplify, or warm start |

Every run records status, objective, lower bound, gap, solve seconds, model
size, and constraint counts per family, per pool (`pool_results.csv`) and in
total (`solve_summary.json`).

**Real world case.** A vendor demo says "optimal roster in 30 seconds". Ask
for the gap. If the solver stopped on a time limit with a 4% gap, "optimal"
means "the best it found", and on a £17m annual pay bill that uncertainty is
worth £680k a year.

## Concept 2: an independent checker, not a trusted model

`analysis/consistency.py` re checks every roster using only the clean input
tables and pandas. It never imports the model or the SQL eligibility view,
so a bug in either cannot hide itself.

| Check | Catches |
|---|---|
| seat_overfilled, coverage_not_reconciled | Coverage constraint bugs, or uncovered seats not reported |
| base_mismatch, rank_not_required, not_rated_on_type, licence_not_valid | Eligibility view bugs |
| rostered_on_leave | Leave overlap logic bugs, such as an off by one on the end date |
| overlapping_duties, rest_violation | Rest clique construction bugs |
| daily_hours_exceeded, hours_7d_exceeded, duty_days_7d_exceeded, month_hours_exceeded | Window bugs |
| model_assumption_one_duty_per_day | The days_7d rule counts duties, which equals duty days only if nobody starts two duties on a day. Checked, not assumed |
| objective_not_reconciled | Reported pay, overtime or fairness differs from a recomputation from the roster |
| available_pilot_unused (warning) | A pilot with 7 or more available days and no duties. Either a staffing surplus, or an eligibility data problem |

Tests prove each check fires: they corrupt a valid roster (add a rest
breach, a pilot on leave, a double covered seat, a misreported cost, a looser
weekly cap than the law) and assert the checker catches it.

**Real world case.** A rostering system counts a duty that reports at 23:30
and releases at 07:00 against the release day instead of the report day. Its
weekly hours constraint passes. A regulator's audit, which counts by report
day, does not. An independent checker written from the rule text, not from
the model, is how you find that before the audit does.

**Interview defence.** "Isn't a second implementation of the rules duplicated
effort?" It is deliberate redundancy, the same reason accountants reconcile two
ledgers. The model is optimised for solver speed (cliques, skipped rows,
pools); the checker is optimised for being obviously correct.

## Concept 3: comparing scenarios without fooling yourself

`config/scenarios.toml` defines what ifs: rule changes (rest, weekly caps),
objective weights (cost vs fairness), and disruption (a sickness wave). Each
scenario uses the same data and solver and changes only the listed settings.

Two traps, both built into `scenario_comparison.csv`:

1. **Uncovered seats look cheap.** A scenario that leaves 4 seats open pays
   nobody to fly them, so its pay cost drops. `cost_comparable_to_baseline` is
   false whenever coverage differs.
2. **Differences inside the solver gap are not results.** Each roster is only
   proven within 0.5%. If baseline scores £1,435,056 with bound £1,431,061,
   and a scenario scores £1,435,257, that £201 difference could be tolerance.
   A scenario is `proven_worse_than_baseline` only when its lower bound is
   above the baseline's objective, and `proven_better` in the mirror case.
   Everything else is `difference_within_solver_gap`.

**Real world case.** An operations director asks "what does moving from 12
to 14 hours minimum rest cost us?". Answering "£1,628 a month" from two runs
with 0.5% gaps is false precision; the honest answer states what the solver
proved, and what would need a tighter solve (or a better model) to settle.

## Results (seed 42 month)

Produced by `python -m crew_roster scenarios`; full table in
`outputs/scenarios/scenario_comparison.csv`. Same machine and solver settings
as Phase 2. Two full runs on different occasions gave identical numbers.

### Solver status

| Scenario | Status | Gap | Solve s | Uncovered seats | Checker errors |
|---|---|---|---|---|---|
| baseline | OPTIMAL | 0.28% | 9.7 | 0 | 0 |
| rest_10h_flat | OPTIMAL | 0.27% | 41.4 | 0 | 0 |
| rest_14h | FEASIBLE (1 pool on time limit) | 0.29% | 69.2 | 0 | 0 |
| rest_16h | OPTIMAL | 0.24% | 31.5 | 2 | 0 |
| max_50h_7d | FEASIBLE (1 pool on time limit) | 0.26% | 95.3 | 0 | 0 |
| max_4_days_7d | OPTIMAL | 0.22% | 38.8 | 4 | 0 |
| cost_only | OPTIMAL | 0.31% | 9.6 | 0 | 0 |
| fairness_weight_5 | OPTIMAL | 0.21% | 10.9 | 0 | 0 |
| fairness_weight_80 | FEASIBLE (3 pools on time limit) | 0.45% | 191.9 | 0 | 0 |
| fairness_only | FEASIBLE (time limits) | 6.9% of a small fairness score | 272.3 | 0 | 0 |
| sickness_wave_edi_b737 | OPTIMAL | 0.26% | 9.2 | 3 | 0 |

Every roster in every scenario passed the independent checker with zero
errors. Heavier fairness weights and tighter rules make pools harder to
prove, which is visible in the solve times.

### Finding 1: rule changes. What is proven, and what is not

| Scenario | Pay + overtime vs baseline | Uncovered | Verdict from the bounds |
|---|---|---|---|
| rest_10h_flat | +£686 | 0 | within solver gap |
| rest_14h | +£1,628 | 0 | within solver gap |
| max_50h_7d | +£1,406 | 0 | within solver gap |
| rest_16h | not comparable | 2 | proven worse |
| max_4_days_7d | not comparable | 4 | proven worse |

Honest reading:

- Moving minimum rest from 12h to 14h, or cutting the weekly cap to 50h,
  has a pay effect **smaller than the solver's 0.5% tolerance** on this
  timetable. Note the looser 10h rule even shows a slightly higher pay cost,
  which is only possible because the difference is noise inside the gap and
  because the objective also prices fairness. All four runs share the same
  lower bound (£1,431,061), which says the relaxation does not feel these
  rules at all.
- An attempt to resolve it with a 0.05% gap in cost only mode ran 666
  seconds and still stopped at 0.15% (`docs/measurements/`). So the defensible
  statement is "less than about 0.3% of monthly pay, and resolving it more
  finely needs a stronger formulation or a commercial solver".
- The real cost of tighter rules is **coverage**, not pay. At 16h rest, or
  4 duty days in 7, the solver proves it cannot cover every seat.

### Finding 2: one pool is the weak point

Every uncovered seat in every scenario (rest_16h: 2, max_4_days_7d: 4,
sickness wave: 3) is an **Edinburgh 737 captain** seat. That pool was
generated with no staffing buffer, and the Phase 1 validation gate already
warned `pool_needs_overtime` at 131% of straight time capacity.

The operational recommendation is therefore not about rules at all: hire or
base one to two more 737 captains in Edinburgh, or give that pool a standing
reserve. This is the kind of insight that justifies connecting input
validation, optimisation and output analysis: a warning at data load became a
proven coverage failure under stress.

### Finding 3: the cost of fairness

| Scenario | Pay + overtime | vs cost only | Mean abs deviation | Max deviation | Pilots with zero duties |
|---|---|---|---|---|---|
| cost_only | £1,394,809 | | 27.8h | 108.5h | 14 |
| fairness_weight_5 | £1,405,337 | +£10,528 | 13.9h | 56.8h | 0 |
| baseline (weight 20) | £1,428,731 | +£33,922 | 3.7h | 17.0h | 0 |
| fairness_weight_80 | £1,436,208 | +£41,399 | 2.6h | 6.5h | 0 |
| fairness_only | £1,437,638 | +£42,829 | 2.2h | 4.5h | 0 |

- Cost only is 2.4% cheaper, and it achieves that by loading the lowest paid
  pilots: one reaches 189.7h against a 190h cap, 43 pilots earn overtime, and
  14 pilots with a week or more available get no flying at all. No union
  would accept it, and in practice idle pilots lose recency.
- The default weight (£20 per hour of deviation) sits at the knee of the
  curve. Going from cost only to baseline buys 24h of mean deviation for
  £33.9k. Going further to weight 80 buys 1.1h more for £7.5k. Past the knee,
  each hour of fairness gets roughly 5 times more expensive.
- Fairness mode costs more overtime than baseline (£12.4k vs £7.6k), because
  forcing everyone to target sometimes means paying premium hours to part
  timers or to pilots whose leave shrank their target.

**Interview defence.** "How did you pick the fairness weight?" By sweeping it
and showing the trade off curve to the people who own the decision. The
weight is a price, and finance, rostering and the pilots' union set prices,
not the analyst. The model makes the price explicit.
