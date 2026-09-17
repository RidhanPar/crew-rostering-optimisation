# Phase 2: the optimisation model

## Concept 1: what a Mixed-Integer Program is

A Mixed-Integer Program (MIP) has three parts.

1. **Decision variables.** The things you choose. Here: does pilot c fly duty d?
2. **Constraints.** Rules every answer must obey, written as linear
   inequalities. Here: rest, hours caps, every seat filled.
3. **Objective.** One number to minimise. Here: pay cost plus a price on
   unfairness.

"Mixed-Integer" means some variables must be whole numbers (a pilot either
flies a duty or does not; 0.4 of a pilot is meaningless) and some can be
continuous (overtime hours).

A solver (CBC here) searches the yes/no combinations cleverly. It first
solves a **relaxation** where yes/no can be fractional. That is fast and gives
a **lower bound**: no real roster can cost less. Then it branches ("what if
pilot 7 does fly duty 40? what if not?") until it finds whole number rosters
close to the bound.

**Real world case.** A planner asks "is this the cheapest roster?". With a
spreadsheet heuristic you cannot say. With a MIP you can say, as this
project's seed 42 month does, "this roster scores £1,435,056 (pay plus the
fairness penalty) and no legal roster can score below £1,431,061". That second
number, the bound, is what makes optimisation defensible to finance.

## Concept 2: the gap

`gap = (roster cost - lower bound) / roster cost`

A gap of 0.5% means the roster is at most 0.5% more expensive than the best
possible. We stop at 0.5% (`gap_rel` in config) because proving the last
fraction of a percent can take far longer than finding the roster.

**Interview defence.** "Why not solve to 0%?" On a £1.43m monthly pay bill,
0.5% is about £7k. The data (pay rates, leave forecasts) is not accurate to £7k,
so proving optimality below that is spending compute on noise.

## The formulation

### Sets

| Symbol | Meaning |
|---|---|
| C | pilots |
| D | duties (from the pairing step) |
| E | eligible (pilot, duty) pairs, from the SQL `eligibility` view |
| S | seats: (duty, rank) pairs, each needing `req` pilots |
| P | pools: pilots with the same base, rank and type rating |
| W | 7 day windows, one starting on each day of the period |

### Decision variables

| Variable | Type | Meaning |
|---|---|---|
| x[c,d] | binary | 1 if pilot c flies duty d. Only exists for (c,d) in E |
| u[s] | continuous, 0 to req | seats left unfilled (elastic mode). Fixed to 0 in strict mode |
| o[c] | continuous, >= 0 | overtime hours for pilot c |
| p[c], n[c] | continuous, >= 0 | hours above / below pilot c's fair target |
| b[c] | continuous, within +/- tolerance | free slack inside the fairness band |
| m[p] | continuous, >= 0 | worst deviation in pool p |

Helper: `H[c] = sum over d of hours[d] * x[c,d]`, total duty hours of pilot c.

### Constraints in plain language, then maths

**Coverage.** Every seat gets exactly the pilots it needs, or the gap is
counted.
`sum of x[c,d] over eligible pilots of that rank + u[d,r] = req[d,r]`

*Why equality, not >=?* Putting two captains on one flight costs money and
breaks the second captain's other duties. Equality says exactly what we mean.

*Why elastic by default?* Real airlines publish "open time": duties nobody is
rostered on, later covered by reserve pilots. A roster with 2 open duties is
useful; an "Infeasible" message is not. The penalty (£50,000 per seat) is far
above any pay saving, so the solver only leaves a seat open when it has no
legal alternative.

**Rest.** No pilot flies two duties closer than the required rest. Rest is
`max(12h, length of the previous duty)`, the common shape of EASA style rules.

The obvious model is one constraint per clashing pair: `x[c,d1] + x[c,d2] <= 1`.
We use **clique constraints** instead. Stretch each duty to
`[report, release + rest)`. Two duties clash exactly when their stretched
intervals overlap. Any set of intervals that all contain one point clash with
each other, so for each duty start time t:

`sum of x[c,d] over duties whose stretched interval contains t <= 1`

This is exact (a test compares it with brute force pairwise clashes on random
intervals) and much tighter. With pairwise rows, the relaxation can set three
mutually clashing duties to 0.5 each (every pair sums to 1, total 1.5 duties).
The clique row forbids that. Tighter relaxations mean better bounds and less
branching.

**Daily hours.** `sum of hours[d] * x[c,d] over duties starting on day t <= 13`.
The model skips this row when rest already allows at most one of that day's
duties, because each duty is under 13 hours (checked in Phase 1). With this
airline's timetable that removes every daily row. They come back automatically
if a timetable ever allows two duties in a day, and a test covers that case.

**Weekly hours.** For each 7 day window w: `sum of hours[d] * x[c,d] <= 60`.
Rolling windows, not calendar weeks: "in any 7 consecutive days" is how the
rules are written, and calendar weeks would let a pilot work 60 hours Thursday
to Sunday and 60 more Monday to Wednesday.

**Duty days per week.** For each window: `sum of x[c,d] <= 5`.
This counts duties, not days. It equals "duty days" only if nobody starts two
duties on one calendar day. That is an assumption, and the Phase 3 roster
checker tests it on every output rather than trusting it.

**Monthly hours.** `H[c] <= 190`.

Rows that can never bind are skipped (for example a pilot whose eligible
duties in a window total less than 60 hours).

### Objective

**Pay cost.** `sum of rate[c] * H[c]`, plus overtime:
`o[c] >= H[c] - threshold[c]`, charged at `premium * rate[c]`. The threshold is
110 hours scaled by FTE. Because o[c] is minimised, it settles at exactly
`max(0, H[c] - threshold)`, a standard way to model a kink in a linear model.

**Fairness.** Each pilot gets a fair target: their pool's total hours, shared
in proportion to `FTE * available days`. A half time pilot back from two weeks
of leave should not be pushed to match a full timer.

`H[c] - target[c] = p[c] - n[c] + b[c]`, with `-4 <= b[c] <= 4`

Penalty: `20 * sum of (p[c] + n[c])  +  100 * sum over pools of m[p]`,
with `m[p] >= p[c] + n[c]` for every pilot in the pool.

*Why two terms?* The total alone lets one pilot absorb a huge deviation if it
saves money elsewhere. The worst case term alone ignores everyone except the
worst pilot. Together they push the whole distribution in.

*Why the +/- 4 hour band?* Duties come in blocks of about 8 hours, so no whole
number roster can hit a target like 93.4 hours exactly. Without the band, the
relaxation (which can hand out fractions of duties) hits every target
perfectly, while any real roster cannot, so the bound stays weak and the gap
closes very slowly. Half a typical duty is the smallest band that removes
that unavoidable part. Measured effect below.

**Modes.** `cost` uses pay only. `fairness` uses the fairness penalty, with
cost × 0.001 as a tie break. `weighted` (default) adds them. The fairness
weights are prices: £20 per hour of deviation says "I will pay £20 to move a
pilot one hour closer to their fair share". Phase 3 sweeps this.

### Decomposition by pool

No constraint links two pools. A captain in Edinburgh on the 737 can only fly
Edinburgh 737 captain seats, and fairness is measured inside the pool. So we
solve each pool as its own model and add the results up. This is exact, not a
heuristic, and `pools_are_independent()` checks it before decomposing. If
pilots ever hold two type ratings, pools overlap and the code falls back to one
model.

A test solves a small dataset both ways and checks that each model's proven
lower bound is below the other's roster cost, which can only hold if they
describe the same problem.

## Concept 3: infeasibility, and how to diagnose it

A model is **infeasible** when no answer satisfies every hard constraint. The
solver just says "Infeasible". It does not say why, and on a 2,000 row model
that is useless to a planner.

**Real world case.** A norovirus outbreak takes 3 of Edinburgh's 8 Boeing 737
captains off for a week. Run in strict mode, the roster fails. Operations
needs to know within minutes: which flights, and would relaxing a rule (an
approved rest reduction, a sixth duty day) fix it, or do they need to borrow
captains from another base?

Try it: `python -m crew_roster diagnose --pool EDI-B737-CPT --count 3 --days 8-14`

Three tools, cheapest first (`model/diagnose.py`):

1. **Capacity prechecks**, no solver. Necessary conditions per pool:
   seats on a day vs pilots available that day, seats in a 7 day window vs
   `min(5, available days)` summed over pilots, hours needed vs the monthly
   cap. If one fails, infeasibility is proven and located. If all pass, the
   model can still be infeasible, because rules interact.
2. **Elastic solve.** Allow unfilled seats and minimise how many. The answer is
   the smallest set of duties that cannot be covered, by name.
3. **Conflict set search** over rule families (a deletion filter). Start with
   all families. For each one, remove it and re solve. If still infeasible,
   it is not needed to explain the conflict, so leave it out for good. The
   families left form an irreducible set: drop any one of them and the pool
   becomes feasible. This is the family level version of an **IIS**
   (Irreducible Infeasible Subsystem). Gurobi and CPLEX compute a row level
   IIS directly; CBC does not, so we re solve.

The diagnosis also reports each family removed on its own ("would relaxing
only the rest rule fix it?"), which is the question operations actually asks.

## Solver engineering notes (found while building this)

- **Threads.** The CBC 2.10.3 binary bundled with PuLP crashed or hung
  whenever `-threads` was passed on this Windows machine, including
  `threads=1`. Without the option it ran reliably and respected the time
  limit. So the config has no threads setting.
- **CBC's printed gap is rounded** to 2 decimals, so a 0.4% gap prints as
  `0.00`. `solve.py` recomputes the gap from the objective and bound.
- **PuLP status "Optimal" after a time limit.** When CBC stops on the time
  limit with a roster, PuLP can still report Optimal. `solve.py` reads CBC's
  own result line and reports FEASIBLE instead, so nobody claims optimality
  that was not proven.

## Measured results

Seed 42 month: 136 pilots, 740 duties, 1,480 seats. Intel Core i5-10200H
laptop, Windows 11, Python 3.11, PuLP 3.3.2 with its bundled CBC 2.10.3, single
thread, `gap_rel = 0.005`. Raw logs are in `docs/measurements/`. Timings will
vary on other machines; rerun with `python -m crew_roster solve`.

| Run | Time limit | Status | Solve seconds | Gap | Objective |
|---|---|---|---|---|---|
| 12 pool models, no fairness band | 60s per pool | 7 pools OPTIMAL, 5 stopped on time limit | 311.6 | 0.44% | not comparable (different fairness definition) |
| 12 pool models, 4h fairness band (default) | 60s per pool | all 12 OPTIMAL | 9.4 | 0.28% | £1,435,056 |
| 1 combined model, 4h fairness band | 300s | stopped on time limit | 304.7 | 0.56% | £1,439,163 |

What this shows:

- **The fairness band cut solve time by about 33x** and took every pool from
  "time limit" to "proven within tolerance". Same data, same solver. The only
  change was not charging for deviation that no whole number roster can avoid.
  This is the "tighten the formulation" lesson: model changes usually beat
  solver tuning.
- **Decomposition beat the combined model.** Same problem and same rules, yet
  after 300 seconds the combined model's roster was £4,107 worse and still not
  proven. Twelve small searches are easier than one large one, because the
  solver's branching and cuts do not have to juggle unrelated pools.
- Model size: 17,933 variables (15,897 binary) and 18,274 constraints, of
  which 9,911 are rest cliques. Zero daily hours rows were needed.

### Infeasibility demo

`python -m crew_roster diagnose --pool EDI-B737-CPT --count 3 --days 8-14`
takes the 3 Edinburgh 737 captains with the most available days off sick for
days 8 to 14. Outputs are in `outputs/diagnosis/`.

1. Strict solve: **INFEASIBLE**.
2. Capacity precheck: days 8 to 14 need 22 captain seats, but under the
   5 duties in 7 days rule the remaining captains can give at most 19.
3. Elastic solve: at least 3 seats cannot be covered, all in EDI-B737-CPT.
4. Conflict set: `['days_7d']`. Relaxing only the 5 in 7 rule makes it
   feasible; relaxing any other single rule does not.

So the operational answer is concrete: approve a sixth duty day for some
captains that week, borrow captains, or accept 3 open seats for reserves.

With `--count 4` the answer changes in an instructive way. No single rule
relaxation fixes it (every row of the relaxation table stays infeasible), and
there is a day with 4 seats and 3 captains, which the rest rule alone makes
impossible. The filter reports one irreducible set, but more than one exists.
**An IIS is not unique**; the one you get depends on the order families are
tested. Say this in an interview before someone asks.
