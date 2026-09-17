"""Synthetic but realistic crew and flight data for a UK short haul airline.

What it produces (raw CSVs, the way an upstream system would export them):
  bases.csv, aircraft_types.csv   reference data
  crew.csv                        pilots with rank, base, pay rate, contract FTE
  crew_qualifications.csv         type rating and licence expiry per pilot
  leave.csv                       annual leave, sickness, simulator training
  flights.csv                     flight legs already grouped into duty periods

Flights arrive grouped into duties because in a real airline a separate
"crew pairing" step builds legal duty periods first. Rostering then assigns
people to those duties. See docs/01_data_layer.md.

With inject_dirty_rows=True we deliberately add the kinds of errors seen in
real exports (duplicates, typos, broken timestamps) so the pipeline has to
catch them. Every injected defect is listed in generation_manifest.json.
"""

from __future__ import annotations

import json
import logging
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

BASES = {
    "MAN": "Manchester",
    "BHX": "Birmingham",
    "EDI": "Edinburgh",
}

AIRCRAFT_TYPES = {
    # type: (family, captains required, first officers required)
    "A320": ("Airbus A320", 1, 1),
    "B737": ("Boeing 737-800", 1, 1),
    "E190": ("Embraer 190", 1, 1),
}

# Destination and typical block time in minutes, by aircraft type.
SHORT_SECTORS = {"DUB": 60, "AMS": 75, "BRU": 70, "CDG": 85, "GVA": 105}
LONG_SECTORS = {"BCN": 130, "PRG": 125, "PMI": 140, "ALC": 150, "KRK": 150, "FCO": 160, "FAO": 165, "TFS": 250}
TYPE_SECTORS = {
    "A320": {**SHORT_SECTORS, **LONG_SECTORS},
    "B737": {**SHORT_SECTORS, **LONG_SECTORS},
    "E190": dict(SHORT_SECTORS),
}

# Duties per day for each (base, aircraft type), and a staffing buffer.
# A buffer of 1.12 means 12% more pilots than the flying needs at the target
# hours, which covers leave and training. EDI B737 is deliberately tight.
PROFILES = {
    "full": {
        ("MAN", "A320"): (6, 1.12),
        ("MAN", "B737"): (4, 1.12),
        ("BHX", "A320"): (4, 1.12),
        ("BHX", "E190"): (3, 1.15),
        ("EDI", "A320"): (3, 1.12),
        ("EDI", "B737"): (3, 1.00),
    },
    "small": {
        ("MAN", "A320"): (3, 1.35),
    },
}

TARGET_HOURS_PER_FTE_MONTH = 100.0
REPORT_MINUTES = 60
DEBRIEF_MINUTES = 30
TURN_MINUTES = 45
WAVE_START_MINUTES = {0: 5 * 60, 1: 10 * 60, 2: 14 * 60 + 30}  # early, mid, late waves (UTC)
TS_FMT = "%Y-%m-%d %H:%M"


@dataclass
class Duty:
    duty_id: str
    base: str
    aircraft_type: str
    legs: list[dict]
    hours: float


def _fmt(ts: datetime) -> str:
    return ts.strftime(TS_FMT)


def _round5(minutes: float) -> int:
    return int(5 * round(minutes / 5))


def _build_legs(rng: random.Random, base: str, ac_type: str, report: datetime, rotations: list[str],
                sectors: dict[str, int]) -> list[dict]:
    legs = []
    t = report + timedelta(minutes=REPORT_MINUTES)
    for i, dest in enumerate(rotations):
        for dep_ap, arr_ap in ((base, dest), (dest, base)):
            block = _round5(sectors[dest] + rng.uniform(-10, 10))
            arr = t + timedelta(minutes=block)
            legs.append({"dep_airport": dep_ap, "arr_airport": arr_ap, "dep": t, "arr": arr})
            t = arr + timedelta(minutes=TURN_MINUTES)
    return legs


def _duty_hours(legs: list[dict], report: datetime) -> float:
    release = legs[-1]["arr"] + timedelta(minutes=DEBRIEF_MINUTES)
    return (release - report).total_seconds() / 3600


def generate_flights(rng: random.Random, profile: dict, start: date, days: int) -> list[Duty]:
    duties: list[Duty] = []
    for day in range(days):
        current = start + timedelta(days=day)
        weekday = current.weekday()  # Monday = 0
        for (base, ac_type), (per_day, _buffer) in profile.items():
            n = per_day
            if weekday in (4, 6):      # Friday and Sunday peaks
                n += 1
            elif weekday == 1 and n > 2:  # quiet Tuesday
                n -= 1
            sectors = TYPE_SECTORS[ac_type]
            long_options = [d for d, b in sectors.items() if b >= 120]
            short_options = [d for d, b in sectors.items() if b <= 90]
            for seq in range(n):
                wave = seq % 3
                offset = rng.choice(range(0, 91, 15))
                report = datetime(current.year, current.month, current.day) + timedelta(
                    minutes=WAVE_START_MINUTES[wave] + offset
                )
                if long_options and rng.random() < 0.55:
                    rotations = [rng.choice(long_options)]
                else:
                    rotations = [rng.choice(short_options), rng.choice(short_options)]
                legs = _build_legs(rng, base, ac_type, report, rotations, sectors)
                duty_id = f"D{current:%Y%m%d}-{base}-{ac_type}-{seq + 1:02d}"
                duties.append(Duty(duty_id, base, ac_type, legs, _duty_hours(legs, report)))
    return duties


def _flight_rows(duties: list[Duty]) -> list[dict]:
    rows = []
    counter = 0
    for duty in duties:
        for seq, leg in enumerate(duty.legs, start=1):
            counter += 1
            rows.append({
                "flight_id": f"F{counter:06d}",
                "flight_number": f"ZX{1000 + counter % 9000}",
                "duty_id": duty.duty_id,
                "leg_seq": seq,
                "aircraft_type": duty.aircraft_type,
                "dep_airport": leg["dep_airport"],
                "arr_airport": leg["arr_airport"],
                "dep_time_utc": _fmt(leg["dep"]),
                "arr_time_utc": _fmt(leg["arr"]),
            })
    return rows


def generate_crew(rng: random.Random, profile: dict, duties: list[Duty], start: date, days: int):
    crew_rows, qual_rows, leave_rows = [], [], []
    counter = 0
    for (base, ac_type), (_per_day, buffer) in profile.items():
        pool_hours = sum(d.hours for d in duties if d.base == base and d.aircraft_type == ac_type)
        target = TARGET_HOURS_PER_FTE_MONTH * days / 31
        headcount = math.ceil(pool_hours / target * buffer)
        for rank in ("CPT", "FO"):
            for _ in range(headcount):
                counter += 1
                crew_id = f"C{counter:04d}"
                if rank == "CPT":
                    seniority = rng.randint(3, 25)
                    rate = 115 + 3 * seniority
                else:
                    seniority = rng.randint(1, 12)
                    rate = 55 + 3 * seniority
                fte = rng.choices([1.0, 0.75, 0.5], weights=[85, 10, 5])[0]
                crew_rows.append({
                    "crew_id": crew_id,
                    "rank": rank,
                    "base": base,
                    "seniority_years": seniority,
                    "hourly_rate_gbp": f"{rate + rng.uniform(-4, 4):.2f}",
                    "fte": fte,
                })
                roll = rng.random()
                if roll < 0.01:
                    expiry = start - timedelta(days=rng.randint(5, 40))
                elif roll < 0.06:
                    expiry = start + timedelta(days=rng.randint(7, max(8, days - 5)))
                else:
                    expiry = start + timedelta(days=rng.randint(180, 720))
                qual_rows.append({
                    "crew_id": crew_id,
                    "aircraft_type": ac_type,
                    "qualified_from": str(start - timedelta(days=365 * seniority)),
                    "licence_expiry": str(expiry),
                })
                leave_rows.extend(_leave_for(rng, crew_id, start, days))
    for i, row in enumerate(leave_rows, start=1):
        row["leave_id"] = f"L{i:05d}"
    return crew_rows, qual_rows, leave_rows


def _leave_for(rng: random.Random, crew_id: str, start: date, days: int) -> list[dict]:
    rows = []
    specs = [("ANNUAL", 0.30, (5, 12)), ("SICK", 0.06, (1, 4)), ("TRAINING", 0.20, (1, 2))]
    for leave_type, prob, (lo, hi) in specs:
        if rng.random() < prob:
            length = rng.randint(lo, hi)
            earliest = -5 if leave_type == "ANNUAL" else 0
            first = rng.randint(earliest, days - 2)
            s = start + timedelta(days=first)
            rows.append({
                "crew_id": crew_id,
                "leave_type": leave_type,
                "start_date": str(s),
                "end_date": str(s + timedelta(days=length - 1)),
            })
    return rows


def inject_dirty_rows(rng: random.Random, flights: list[dict], crew: list[dict], quals: list[dict],
                      leave: list[dict], start: date) -> list[dict]:
    """Add realistic defects. Returns a manifest of what was injected."""
    manifest: list[dict] = []
    duty_ids = sorted({f["duty_id"] for f in flights})
    picks = rng.sample(duty_ids, 6)

    # 1. Exact duplicate legs, as produced by a double export. Cleaning should dedupe.
    for f in rng.sample(flights, 3):
        flights.append(dict(f))
        manifest.append({"table": "flights", "defect": "exact_duplicate", "key": f["flight_id"], "expected": "deduplicated"})

    # 2. Messy airport codes. Cleaning should normalise.
    for f in rng.sample(flights, 2):
        f["dep_airport"] = " " + f["dep_airport"].lower() + " "
        manifest.append({"table": "flights", "defect": "messy_airport_code", "key": f["flight_id"], "expected": "normalised"})

    # 3. Slash timestamp format. Unambiguous, so cleaning should normalise.
    for f in rng.sample(flights, 2):
        f["dep_time_utc"] = f["dep_time_utc"].replace("-", "/")
        manifest.append({"table": "flights", "defect": "slash_timestamp", "key": f["flight_id"], "expected": "normalised"})

    by_duty = {d: [f for f in flights if f["duty_id"] == d] for d in picks}

    # 4. Arrival before departure (timestamps swapped). Duty rejected.
    leg = by_duty[picks[0]][0]
    leg["dep_time_utc"], leg["arr_time_utc"] = leg["arr_time_utc"], leg["dep_time_utc"]
    manifest.append({"table": "flights", "defect": "arrival_before_departure", "key": picks[0], "expected": "duty_rejected"})

    # 5. Aircraft type typo (letter O instead of zero). Duty rejected.
    for leg in by_duty[picks[1]]:
        leg["aircraft_type"] = leg["aircraft_type"].replace("0", "O")
    manifest.append({"table": "flights", "defect": "unknown_aircraft_type", "key": picks[1], "expected": "duty_rejected"})

    # 6. Broken chain: leg 2 departs from an airport leg 1 never arrived at.
    by_duty[picks[2]][1]["dep_airport"] = "LBA"
    manifest.append({"table": "flights", "defect": "broken_leg_chain", "key": picks[2], "expected": "duty_rejected"})

    # 7. Conflicting duplicate: same flight_id, different departure time. Duty rejected.
    original = by_duty[picks[3]][0]
    clash = dict(original)
    clash["dep_time_utc"] = (datetime.strptime(original["dep_time_utc"].replace("/", "-"), TS_FMT)
                             + timedelta(minutes=20)).strftime(TS_FMT)
    flights.append(clash)
    manifest.append({"table": "flights", "defect": "conflicting_duplicate_id", "key": picks[3], "expected": "duty_rejected"})

    # 8. Duty over the daily duty limit: two ALC rotations is about 13.6 hours.
    base = next(iter(BASES))
    report = datetime(start.year, start.month, start.day) + timedelta(days=3, hours=5)
    long_legs = _build_legs(rng, base, "A320", report, ["ALC", "ALC"], {"ALC": 150})
    duty_id = f"D{report:%Y%m%d}-{base}-A320-99"
    next_id = len(flights) + 1
    for seq, leg in enumerate(long_legs, start=1):
        flights.append({
            "flight_id": f"F9{next_id + seq:05d}", "flight_number": f"ZX9{seq:03d}", "duty_id": duty_id,
            "leg_seq": seq, "aircraft_type": "A320", "dep_airport": leg["dep_airport"],
            "arr_airport": leg["arr_airport"], "dep_time_utc": _fmt(leg["dep"]), "arr_time_utc": _fmt(leg["arr"]),
        })
    manifest.append({"table": "flights", "defect": "duty_exceeds_daily_limit", "key": duty_id, "expected": "duty_rejected"})

    # Crew defects
    dup = dict(rng.choice(crew[15:]))
    crew.append(dup)
    manifest.append({"table": "crew", "defect": "exact_duplicate", "key": dup["crew_id"], "expected": "deduplicated"})
    variants = {"CPT": "Captain", "FO": " first officer"}
    # Indices 5 to 14 are reserved for the rejection defects below.
    for c in rng.sample([c for c in crew[15:-1] if c["crew_id"] != dup["crew_id"]], 2):
        c["rank"] = variants[c["rank"]]
        manifest.append({"table": "crew", "defect": "rank_text_variant", "key": c["crew_id"], "expected": "normalised"})
    bad_base = crew[5]
    bad_base["base"] = "LGW"
    manifest.append({"table": "crew", "defect": "unknown_base", "key": bad_base["crew_id"], "expected": "rejected"})
    bad_rate = crew[8]
    bad_rate["hourly_rate_gbp"] = ""
    manifest.append({"table": "crew", "defect": "missing_pay_rate", "key": bad_rate["crew_id"], "expected": "rejected"})

    # Qualification defects
    quals.append({"crew_id": crew[10]["crew_id"], "aircraft_type": "A350", "qualified_from": str(start),
                  "licence_expiry": str(start + timedelta(days=400))})
    manifest.append({"table": "crew_qualifications", "defect": "unknown_aircraft_type", "key": crew[10]["crew_id"], "expected": "rejected"})
    quals.append({"crew_id": "C9999", "aircraft_type": "A320", "qualified_from": str(start),
                  "licence_expiry": str(start + timedelta(days=400))})
    manifest.append({"table": "crew_qualifications", "defect": "unknown_crew", "key": "C9999", "expected": "rejected"})

    # Leave defects
    n = len(leave)
    leave.append({"leave_id": f"L{n + 1:05d}", "crew_id": crew[12]["crew_id"], "leave_type": "ANNUAL",
                  "start_date": str(start + timedelta(days=10)), "end_date": str(start + timedelta(days=4))})
    manifest.append({"table": "leave", "defect": "end_before_start", "key": f"L{n + 1:05d}", "expected": "rejected"})
    leave.append({"leave_id": f"L{n + 2:05d}", "crew_id": "C8888", "leave_type": "SICK",
                  "start_date": str(start), "end_date": str(start + timedelta(days=2))})
    manifest.append({"table": "leave", "defect": "unknown_crew", "key": f"L{n + 2:05d}", "expected": "rejected"})
    leave.append({"leave_id": f"L{n + 3:05d}", "crew_id": crew[14]["crew_id"], "leave_type": "ANNUAL",
                  "start_date": "03/10/2026", "end_date": "07/10/2026"})
    manifest.append({"table": "leave", "defect": "ambiguous_date_format", "key": f"L{n + 3:05d}", "expected": "rejected"})
    return manifest


def generate(raw_dir: Path, seed: int, profile: str, period_start: str, period_days: int,
             dirty: bool) -> dict:
    """Generate all raw CSVs into raw_dir. Returns the manifest."""
    rng = random.Random(seed)
    prof = PROFILES[profile]
    start = date.fromisoformat(period_start)
    raw_dir.mkdir(parents=True, exist_ok=True)

    duties = generate_flights(rng, prof, start, period_days)
    flights = _flight_rows(duties)
    crew, quals, leave = generate_crew(rng, prof, duties, start, period_days)
    defects = inject_dirty_rows(rng, flights, crew, quals, leave, start) if dirty else []

    used_bases = sorted({b for b, _ in prof})
    used_types = sorted({t for _, t in prof})
    tables = {
        "bases": pd.DataFrame([{"base_code": b, "base_name": BASES[b], "timezone": "Europe/London"} for b in used_bases]),
        "aircraft_types": pd.DataFrame([
            {"type_code": t, "description": AIRCRAFT_TYPES[t][0], "captains_required": AIRCRAFT_TYPES[t][1],
             "first_officers_required": AIRCRAFT_TYPES[t][2]} for t in used_types
        ]),
        "crew": pd.DataFrame(crew),
        "crew_qualifications": pd.DataFrame(quals),
        "leave": pd.DataFrame(leave)[["leave_id", "crew_id", "leave_type", "start_date", "end_date"]],
        "flights": pd.DataFrame(flights),
    }
    for name, df in tables.items():
        df.to_csv(raw_dir / f"{name}.csv", index=False)

    manifest = {
        "seed": seed,
        "profile": profile,
        "period_start": period_start,
        "period_days": period_days,
        "row_counts": {name: len(df) for name, df in tables.items()},
        "clean_duties_generated": len(duties),
        "injected_defects": defects,
    }
    (raw_dir / "generation_manifest.json").write_text(json.dumps(manifest, indent=2))
    log.info("Generated %s duties, %s flight rows, %s crew rows, %s defects into %s",
             len(duties), len(flights), len(crew), len(defects), raw_dir)
    return manifest
