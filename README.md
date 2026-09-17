# Crew Rostering Optimisation

Monthly airline crew rostering formulated as a Mixed-Integer Program.
Assigns pilots to duty periods while respecting rest rules, duty hour caps,
type ratings, licence expiry, crew bases and leave, and trades pay cost
against fairness.

Work in progress. Built in four phases: data layer, optimisation model,
output analysis, reporting.

## Setup

```bash
pip install -e ".[dev]"
pytest -q
```
