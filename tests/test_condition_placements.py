"""Reviewed disease-area placements for Uncategorized trials
(src/condition_placements.py) and their use in the industry analysis.

Fictional trial ids and records throughout, except the last test, which
checks the committed file itself loads and names its source."""
from __future__ import annotations

import copy
import json
import os
import pathlib
import sys
from typing import Any

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from src import condition_placements as cp  # noqa: E402

AREAS = {"Ophthalmology", "Infectious Disease", "Oncology"}


def _doc(rows: list[dict[str, str]]) -> dict[str, Any]:
    placements = {r["nct_id"]: {"area": r["area"], "source": r["source"]} for r in rows}
    return {"format": cp.FORMAT, "applies_when": {"primary": "Other", "secondary": "Uncategorized"},
            "provenance": {"commit": "f" * 40}, "counts": cp.summarize(placements), "placements": rows}


ROWS = [{"nct_id": "NCT90000001", "area": "Ophthalmology", "source": "mesh_map_v3"},
        {"nct_id": "NCT90000002", "area": "Infectious Disease", "source": "ai_label"},
        {"nct_id": "NCT90000003", "area": "Oncology", "source": "procedure_rule"}]


def test_check_accepts_a_well_formed_file() -> None:
    got = cp.check(_doc(ROWS), AREAS)
    assert got["NCT90000002"] == {"area": "Infectious Disease", "source": "ai_label"}
    assert len(got) == 3


@pytest.mark.parametrize("mutate,why", [
    (lambda d: d.update(format="something-else/1"), "not a civicsample-condition-placements"),
    (lambda d: d.update(applies_when={"primary": "Other", "secondary": "Other"}), "applies_when"),
    (lambda d: d.update(placements=[]), "no placements"),
    (lambda d: d["placements"][0].update(extra=1), "exactly"),
    (lambda d: d["placements"][0].update(nct_id="NCT123"), "NCT id"),
    (lambda d: d["placements"].append(dict(d["placements"][0])), "twice"),
    (lambda d: d["placements"][0].update(area="No disease area"), "primary category"),
    (lambda d: d["placements"][0].update(source="healthy_volunteer_rule"), "unknown source"),
    (lambda d: d["counts"].update(trials=4), "counts"),
])
def test_check_refuses_every_malformed_file(mutate: Any, why: str) -> None:
    d = copy.deepcopy(_doc(ROWS))
    mutate(d)
    with pytest.raises(cp.PlacementError, match=why):
        cp.check(d, AREAS)


def _row(nct: str, primary: str = "Other", secondary: str = "Uncategorized") -> dict[str, Any]:
    return {"nct_id": nct, "primary": primary, "secondary": secondary}


def test_apply_moves_only_rows_still_other_uncategorized_and_keeps_the_secondary() -> None:
    placements = cp.check(_doc(ROWS), AREAS)
    rows = [_row("NCT90000001"),                                   # placed: moves
            _row("NCT90000002", "Infectious Disease", "HIV"),      # keywords place it now: stays
            _row("NCT90000009"),                                   # uncategorized, no placement: stays
            _row("NCT90000010", "Oncology", "Lung Cancer")]        # ordinary trial: untouched
    report = cp.apply(rows, placements)
    assert [(r["primary"], r["secondary"]) for r in rows] == [
        ("Ophthalmology", "Uncategorized"), ("Infectious Disease", "HIV"),
        ("Other", "Uncategorized"), ("Oncology", "Lung Cancer")]
    assert report == {"applied": 1, "applied_by_source": {"mesh_map_v3": 1},
                      "applied_by_area": {"Ophthalmology": 1},
                      "not_applied": {"not_in_cohort": 1, "categorized_by_keywords_now": 1},
                      "uncategorized_remaining": 1}


def test_apply_never_moves_a_row_whose_secondary_is_not_uncategorized() -> None:
    placements = cp.check(_doc(ROWS), AREAS)
    rows = [_row("NCT90000001", "Other", "Other Rare Conditions")]
    report = cp.apply(rows, placements)
    assert rows[0]["primary"] == "Other" and report["applied"] == 0
    assert report["not_applied"]["categorized_by_keywords_now"] == 1


def _private(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"format": cp.PRIVATE_FORMAT, "trials": len(rows), "by_placement": {},
            "experiments": {"x-v1": {"ledger_tip": "a" * 64, "plan_sha256": "b" * 64}}, "rows": rows}


def test_from_private_keeps_disease_areas_only_and_drops_the_labelling_detail() -> None:
    rows = [{"nct_id": "NCT90000003", "placement": "Oncology", "source": "procedure_rule",
             "detail": {"rule": "x"}, "vaccine": None},
            {"nct_id": "NCT90000001", "placement": "Ophthalmology", "source": "mesh_map_v3",
             "detail": {"reason": "one_area"}, "vaccine": None},
            {"nct_id": "NCT90000004", "placement": "No disease area", "source": "healthy_volunteer_rule",
             "detail": {}, "vaccine": None},
            {"nct_id": "NCT90000005", "placement": "No disease area", "source": "ai_label",
             "detail": {"initial": "NONE"}, "vaccine": {"target": "influenza"}},
            {"nct_id": "NCT90000006", "placement": "Other", "source": "mesh_map_v3", "detail": {}, "vaccine": None},
            {"nct_id": "NCT90000007", "placement": "Uncertain", "source": "ai_label", "detail": {}, "vaccine": None},
            {"nct_id": "NCT90000008", "placement": "Missing", "source": "ai_label", "detail": {}, "vaccine": None}]
    out = cp.from_private(_private(rows), AREAS, "c" * 40, "d" * 64)
    assert out["placements"] == [
        {"nct_id": "NCT90000001", "area": "Ophthalmology", "source": "mesh_map_v3"},
        {"nct_id": "NCT90000003", "area": "Oncology", "source": "procedure_rule"}]
    assert out["provenance"]["commit"] == "c" * 40 and out["provenance"]["file_sha256"] == "d" * 64
    assert out["provenance"]["experiments"] == {"x-v1": {"ledger_tip": "a" * 64}}
    cp.check(out, AREAS)


def test_from_private_refuses_a_disease_area_from_the_healthy_volunteer_rule() -> None:
    rows = [{"nct_id": "NCT90000004", "placement": "Oncology", "source": "healthy_volunteer_rule",
             "detail": {}, "vaccine": None}]
    with pytest.raises(cp.PlacementError, match="healthy_volunteer_rule"):
        cp.from_private(_private(rows), AREAS, "c" * 40, "d" * 64)


def test_import_script_writes_a_file_the_loader_accepts(tmp_path: pathlib.Path) -> None:
    import import_condition_placements as icp
    src = tmp_path / "placements.json"
    src.write_text(json.dumps(_private([
        {"nct_id": "NCT90000001", "placement": "Ophthalmology", "source": "mesh_map_v3", "detail": {}, "vaccine": None},
        {"nct_id": "NCT90000002", "placement": "No disease area", "source": "ai_label", "detail": {}, "vaccine": None}])))
    out = tmp_path / "condition_placements.json"
    icp.main([str(src), "--source-commit", "e" * 40, "--out", str(out)])
    placements, meta = cp.load(str(out))
    assert placements == {"NCT90000001": {"area": "Ophthalmology", "source": "mesh_map_v3"}}
    assert meta["file_sha256"] == cp.file_sha256(str(out))
    assert meta["provenance"]["file_sha256"] == cp.file_sha256(str(src))
    with pytest.raises(SystemExit):
        icp.main([str(src), "--source-commit", "abc", "--out", str(out)])


# ── the industry analysis ─────────────────────────────────────────────────

def _records() -> list[dict[str, Any]]:
    """120 fictional industry trials: two sponsors, varied phase, size and
    sex split, so every fit in the generator has data. Trials 0-29 are Other /
    Uncategorized; trial 30 is categorized by keywords."""
    phases = ["PHASE1", "PHASE2", "PHASE3", "PHASE4"]
    out = []
    for i in range(120):
        primary, secondary = ("Other", "Uncategorized") if i < 30 else ("Oncology", "Lung Cancer")
        if i >= 60:
            primary, secondary = "Cardiovascular", "Heart Failure"
        out.append({
            "nct_id": f"NCT9{i:07d}", "study_type": "INTERVENTIONAL", "status": "COMPLETED",
            "primary_completion_date": f"{2010 + i % 12}-06-01", "results_date": f"{2011 + i % 12}-01-01",
            "sponsor_class": "INDUSTRY", "lead_sponsor_name": "Alpha Pharma Inc" if i % 2 else "Beta Corp",
            "collaborators": [], "phase": phases[i % 4], "enrollment": 40 + 7 * i,
            "countries": [{"country": c} for c in ("A", "B", "C")[: 1 + i % 3]],
            "sex": {"reported": True, "totals": {"female": 20 + (i * 13) % 37, "male": 25 + (i * 7) % 29}},
            "race": None, "ethnicity": None,
            "primary_condition": primary, "secondary_condition": secondary,
        })
    return out


def _run(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> dict[str, Any]:
    import generate_industry_sponsors as gis
    monkeypatch.setattr(gis, "load_records", lambda p: ("2026-11-08T00:00:00Z", "abc1234", _records()))
    gis.main([str(tmp_path), *args])
    return json.loads((tmp_path / "industry_sponsors.json").read_text())


def test_the_generator_applies_placements_and_records_what_moved(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    placements = tmp_path / "placements.json"
    placements.write_text(json.dumps(_doc([
        {"nct_id": "NCT90000000", "area": "Ophthalmology", "source": "mesh_map_v3"},
        {"nct_id": "NCT90000001", "area": "Infectious Disease", "source": "ai_label"},
        {"nct_id": "NCT90000030", "area": "Ophthalmology", "source": "ai_label"},     # keywords place it: stays
        {"nct_id": "NCT99999999", "area": "Oncology", "source": "procedure_rule"},    # not in the cohort
    ])))
    on = _run(tmp_path, monkeypatch, "--placements", str(placements))
    prim = [on["primaries"][t[4]] for t in on["trials"]]
    sec = [on["secondaries"][t[5]] for t in on["trials"]]
    assert prim[0] == "Ophthalmology" and prim[1] == "Infectious Disease" and prim[30] == "Oncology"
    assert sec[0] == sec[1] == "Uncategorized"
    assert prim.count("Other") == 28
    block = on["condition_placements"]
    assert block["applied"] == 2 and block["uncategorized_remaining"] == 28
    assert block["not_applied"] == {"not_in_cohort": 1, "categorized_by_keywords_now": 1}
    assert block["file_sha256"] == cp.file_sha256(str(placements))
    assert block["provenance"] == {"commit": "f" * 40}

    off = _run(tmp_path, monkeypatch, "--no-placements")
    assert off["condition_placements"] is None
    assert [off["primaries"][t[4]] for t in off["trials"]].count("Other") == 30
    # The placed rows are the only difference in the per-trial rows.
    assert on["cohort_n"] == off["cohort_n"] and on["companies"] == off["companies"]


def test_the_generator_stops_on_a_malformed_placements_file(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = _doc([{"nct_id": "NCT90000000", "area": "Eyes", "source": "mesh_map_v3"}])
    placements = tmp_path / "placements.json"
    placements.write_text(json.dumps(bad))
    with pytest.raises(cp.PlacementError, match="primary category"):
        _run(tmp_path, monkeypatch, "--placements", str(placements))
    assert not (tmp_path / "industry_sponsors.json").exists()


def test_the_committed_file_loads_and_names_its_source() -> None:
    placements, meta = cp.load()
    prov = meta["provenance"]
    assert prov["repository"] == "civicsample-verify-ai-private"
    assert len(prov["commit"]) == 40 and len(prov["file_sha256"]) == 64
    assert len(placements) == sum(v for k, v in prov["population_by_placement"].items()
                                  if k in cp.ontology_areas())
    assert {p["source"] for p in placements.values()} <= set(cp.SOURCES)
