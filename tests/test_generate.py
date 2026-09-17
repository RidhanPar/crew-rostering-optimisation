import json

import pandas as pd

from crew_roster.data.generate import generate


def test_same_seed_gives_identical_files(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    generate(a, 7, "small", "2026-10-01", 14, dirty=True)
    generate(b, 7, "small", "2026-10-01", 14, dirty=True)
    for name in ("flights", "crew", "crew_qualifications", "leave"):
        assert (a / f"{name}.csv").read_bytes() == (b / f"{name}.csv").read_bytes()


def test_different_seed_changes_the_data(tmp_path):
    generate(tmp_path / "a", 1, "small", "2026-10-01", 14, dirty=False)
    generate(tmp_path / "b", 2, "small", "2026-10-01", 14, dirty=False)
    assert (tmp_path / "a" / "flights.csv").read_bytes() != (tmp_path / "b" / "flights.csv").read_bytes()


def test_manifest_lists_defects_only_when_dirty(tmp_path):
    clean = generate(tmp_path / "clean", 3, "small", "2026-10-01", 14, dirty=False)
    dirty = generate(tmp_path / "dirty", 3, "small", "2026-10-01", 14, dirty=True)
    assert clean["injected_defects"] == []
    assert len(dirty["injected_defects"]) >= 15
    on_disk = json.loads((tmp_path / "dirty" / "generation_manifest.json").read_text())
    assert on_disk["injected_defects"] == dirty["injected_defects"]


def test_generated_duties_are_based_round_trips(tmp_path):
    generate(tmp_path, 5, "small", "2026-10-01", 14, dirty=False)
    flights = pd.read_csv(tmp_path / "flights.csv")
    for _, legs in flights.sort_values("leg_seq").groupby("duty_id"):
        assert legs["dep_airport"].iloc[0] == legs["arr_airport"].iloc[-1] == "MAN"
        assert (legs["arr_airport"].iloc[:-1].to_numpy() == legs["dep_airport"].iloc[1:].to_numpy()).all()
