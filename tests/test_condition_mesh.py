"""condition_mesh.jsonl.gz: CT.gov's MeSH indexing of each trial's conditions,
written beside the full records and never onto them (src/condition_mesh.py)."""
from __future__ import annotations

import copy
import gzip
import json
import pathlib
import sys
from typing import Any, Iterator

import pytest

from src import condition_mesh as cm

MESHES = [{"id": "D006086", "term": "Graft vs Host Disease"}]
ANCESTORS = [{"id": "D007154", "term": "Immune System Diseases"}]


def _study(nct: str = "NCT00000001", derived: Any = "absent") -> dict[str, Any]:
    study: dict[str, Any] = {"protocolSection": {"identificationModule": {"nctId": nct}}}
    if derived != "absent":
        study["derivedSection"] = derived
    return study


@pytest.mark.parametrize("derived,state,meshes,ancestors", [
    ({"conditionBrowseModule": {"meshes": MESHES, "ancestors": ANCESTORS}}, "indexed", MESHES, ANCESTORS),
    ({"conditionBrowseModule": {"meshes": MESHES}}, "indexed", MESHES, None),
    ({"conditionBrowseModule": {"meshes": [], "ancestors": []}}, "not_indexed", [], []),
    ({"conditionBrowseModule": {}}, "not_indexed", None, None),
    ({"miscInfoModule": {}}, "no_module", None, None),
    ("absent", "no_derived", None, None),
], ids=["indexed", "no-ancestors", "empty-lists", "empty-module", "no-module", "no-derived"])
def test_each_state_keeps_absence_as_absence(derived: Any, state: str, meshes: Any, ancestors: Any) -> None:
    line = cm.select_condition_mesh(_study(derived=derived))
    assert line["state"] == state and line["nct_id"] == "NCT00000001"
    assert line["meshes"] == meshes and line["ancestors"] == ancestors
    assert line["schema_version"] == cm.SCHEMA_VERSION and "problem" not in line


@pytest.mark.parametrize("module,problem", [
    ("D006086", "conditionBrowseModule is not an object"),
    ({"meshes": "D006086"}, "meshes is not a list of id/term pairs"),
    ({"meshes": MESHES, "ancestors": [{"id": "D007154"}]}, "ancestors is not a list of id/term pairs"),
])
def test_a_malformed_module_says_what_is_wrong(module: Any, problem: str) -> None:
    line = cm.select_condition_mesh(_study(derived={"conditionBrowseModule": module}))
    assert line["state"] == "malformed" and line["problem"] == problem


def test_the_registry_lists_are_kept_verbatim() -> None:
    extra = [{"id": "D006086", "term": "Graft vs Host Disease", "relevance": "HIGH"}]
    line = cm.select_condition_mesh(_study(derived={"conditionBrowseModule": {"meshes": extra}}))
    assert line["meshes"] == extra


# ── extract_all writes the file beside the output, not onto the records ─────

def _api_study(i: int, mesh: bool) -> dict[str, Any]:
    from validate_fixes import _make_study, _make_measure, _make_class, _make_category
    study = _make_study(f"NCT0000000{i}", f"study {i}", [
        _make_measure("Sex: Female, Male", [_make_class("", [_make_category("Female", 40), _make_category("Male", 35)])]),
    ], total_participants=75)
    if mesh:
        study["derivedSection"] = {"conditionBrowseModule": {"meshes": MESHES, "ancestors": ANCESTORS}}
    return study


def test_extract_all_writes_one_stamped_line_per_kept_study(tmp_path: pathlib.Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from src import extract_all
    studies = [_api_study(1, mesh=True), _api_study(2, mesh=False)]
    full = {s["protocolSection"]["identificationModule"]["nctId"]: copy.deepcopy(s) for s in studies}

    class Client:
        def __init__(self, **_: Any) -> None:
            pass

        def iter_all_studies_with_results(self, **_: Any) -> Iterator[dict[str, Any]]:
            yield from studies

        def get_study(self, nct: str) -> dict[str, Any]:
            return full[nct]

    monkeypatch.setattr(extract_all, "CTGovAPIClient", Client)
    out = tmp_path / "data" / "demographics.json"
    monkeypatch.setattr(sys, "argv", ["extract_all", "--output", str(out), "--snapshot-date", "2026-11-08"])
    extract_all.main()

    with gzip.open(out.parent / cm.FILENAME, "rt") as f:
        lines = [json.loads(line) for line in f]
    assert [(m["nct_id"], m["state"]) for m in lines] == [("NCT00000001", "indexed"), ("NCT00000002", "no_derived")]
    assert lines[0]["meshes"] == MESHES and lines[0]["ancestors"] == ANCESTORS
    records = json.loads(out.read_text())
    for m in lines:
        assert m["snapshot_date"] == "2026-11-08" and m["extracted_at"] == records["extracted_at"]
        assert "pipeline_commit" in m and m["refetched"] is False
    for record in records["data"]:
        assert not {"meshes", "ancestors", "condition_mesh"} & set(record), "MeSH leaked onto a site record"
