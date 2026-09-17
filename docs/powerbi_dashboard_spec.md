# Power BI dashboard specification

Tables are produced by `python -m crew_roster report` into `outputs/powerbi/`.
All are flat CSVs with LF line endings, ISO dates and UTC timestamps.

## Data model

Star schema. Every fact table has a `scenario` column so any page can be
filtered to one scenario, or two scenarios compared with a second slicer
bound to a disconnected copy of `dim_scenario`.

```
                 dim_scenario (scenario)
                        |
dim_crew (crew_id) -- fact_assignment -- dim_duty (duty_id)
      |                 fact_crew_period         |
      |                 fact_crew_day ---- dim_date (date)
      |                 fact_seat_coverage ------+
                        fact_pool_solve
                        fact_scenario_summary
                        fact_data_quality
```

| Table | Grain | Key columns | Relationships (many to one, single direction) |
|---|---|---|---|
| dim_crew | pilot | crew_id | |
| dim_duty | duty period | duty_id | |
| dim_date | calendar day | date | |
| dim_scenario | scenario | scenario | |
| fact_assignment | scenario, pilot, duty | scenario, crew_id, duty_id | crew_id to dim_crew, duty_id to dim_duty, duty_date to dim_date, scenario to dim_scenario |
| fact_crew_period | scenario, pilot | scenario, crew_id | crew_id, scenario |
| fact_crew_day | scenario, pilot, day | scenario, crew_id, date | crew_id, date, scenario |
| fact_seat_coverage | scenario, duty, rank | scenario, duty_id, rank | duty_id, duty_date to dim_date, scenario |
| fact_pool_solve | scenario, crew pool | scenario, pool_id | scenario |
| fact_scenario_summary | scenario | scenario | scenario |
| fact_data_quality | issue | stage, check, entity | scenario ("(input)" for pipeline issues) |

Mark `dim_date[date]` as the date table. Set `dim_scenario[scenario]` to sort
by `sort_order`.

## DAX measures

```DAX
Rostered Hours = SUM ( fact_assignment[duty_hours] )

Pay Cost = SUM ( fact_crew_period[pay_cost] )
Overtime Cost = SUM ( fact_crew_period[overtime_cost] )
Total Crew Cost = [Pay Cost] + [Overtime Cost]
Cost per Rostered Hour = DIVIDE ( [Total Crew Cost], [Rostered Hours] )

Seats Required = SUM ( fact_seat_coverage[required] )
Seats Uncovered = SUM ( fact_seat_coverage[uncovered] )
Coverage % = 1 - DIVIDE ( [Seats Uncovered], [Seats Required] )

Mean Abs Deviation (h) =
    AVERAGEX (
        FILTER ( fact_crew_period, fact_crew_period[available_days] > 0 ),
        ABS ( fact_crew_period[deviation_hours] )
    )
Pilots Outside Fair Band =
    CALCULATE ( COUNTROWS ( fact_crew_period ), fact_crew_period[dev_outside_band] > 0 )
Utilisation % = DIVIDE ( SUM ( fact_crew_period[hours] ), SUM ( fact_crew_period[target_hours] ) )

Baseline Total Cost =
    CALCULATE ( [Total Crew Cost], REMOVEFILTERS ( dim_scenario ), dim_scenario[scenario] = "baseline" )
Cost vs Baseline = [Total Crew Cost] - [Baseline Total Cost]
Cost vs Baseline % = DIVIDE ( [Cost vs Baseline], [Baseline Total Cost] )

Solve Seconds = SUM ( fact_pool_solve[solve_seconds] )
Pools Not Proven =
    CALCULATE ( COUNTROWS ( fact_pool_solve ), fact_pool_solve[status] <> "OPTIMAL" )
Roster Check Errors =
    CALCULATE ( COUNTROWS ( fact_data_quality ), fact_data_quality[severity] = "ERROR",
                fact_data_quality[stage] = "output" )
```

## Pages

### 1. Roster overview (audience: crew planning manager)

- Slicers: scenario, base, aircraft type, rank.
- KPI cards: Total Crew Cost, Cost vs Baseline %, Coverage %, Seats Uncovered,
  Pilots Outside Fair Band, Roster Check Errors.
  Conditional format: Roster Check Errors red when above 0; Seats Uncovered
  amber when above 0.
- Stacked column: Rostered Hours by `dim_date[date]`, legend base.
- Table: uncovered seats (fact_seat_coverage where uncovered > 0) with duty,
  report time from dim_duty, rank. This is the reserve crew work list.

### 2. Fairness (audience: crew union rep, HR)

- Scatter: x = `target_hours`, y = `hours`, one dot per pilot, colour by pool.
  Add a y = x reference line and a shaded band of +/- 4 hours. Dots outside
  the band are the pilots the objective charged for.
- Histogram (column chart on binned `deviation_hours`, 4 hour bins).
- Table: pool, pilots, Mean Abs Deviation (h), max deviation, Pilots Outside
  Fair Band, overtime hours.
- Tooltip on a dot: crew_id, FTE, available days, leave days, overtime hours.

### 3. Roster heatmap (audience: rostering officer)

- Matrix: rows crew_id (grouped by pool), columns date, values
  `fact_crew_day[duty_hours]`.
- Conditional background by `day_status`: DUTY blue, LEAVE grey, UNAVAILABLE
  dark grey, OFF white. Scan for long runs of duty days and short gaps.

### 4. Scenario comparison (audience: operations director, finance)

- Table from fact_scenario_summary: scenario, description, status, total cost,
  cost delta %, uncovered seats, mean abs deviation, max deviation, solve seconds,
  `cost_comparable_to_baseline`.
- Clustered bar: Cost vs Baseline by scenario, grouped by `scenario_group`.
- Scatter (cost vs fairness trade off): x = mean_abs_deviation_hours,
  y = total_cost_gbp, filter scenario_group = "objective". Points form the
  frontier: how much pay each hour of fairness costs.
- Note on the page: compare costs only where `cost_comparable_to_baseline` is
  true. A scenario that leaves seats open looks cheaper because nobody is paid
  to fly them.

### 5. Solver and data quality (audience: analytics team)

- Table: fact_pool_solve by scenario and pool with status, solve seconds, gap,
  variables, constraints. Highlight status not OPTIMAL.
- Bar: Solve Seconds by scenario.
- Table: fact_data_quality, filter stage in (cleaning, input), grouped by
  check with counts, drill through to entity and detail.
- Card: Roster Check Errors across all scenarios. Must be zero to publish.

## Refresh

Folder data source on `outputs/powerbi/`. Run the pipeline
(`python -m crew_roster run-all`), then refresh. No calculated columns need
Power Query steps beyond type detection (set date columns to Date, timestamps
to Date/Time).
