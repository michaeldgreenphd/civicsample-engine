"""Records, contracts and datasets shared by the split tests (test_split_data.py,
test_check_site_contract.py, test_full_records.py). Not a test module.

tests/fixtures/site_record_contract_layout_v1.json is a copy of the site's
tests/record_contract.json with its layout section (version 1, the split-layout
spec both repositories follow). The weekly job reads the live one from its site
checkout; this copy lets the engine's tests run the split on the site's real
classes, optional paths, whole lists and shard test vectors.
"""
from __future__ import annotations

import copy
import gzip
import json
import os
import pathlib
import sys
from typing import Any

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
for p in (ROOT, os.path.join(ROOT, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

STAMPS = {"extracted_at": "2026-10-11T06:12:09.454200+00:00",
          "pipeline_commit": "0123456789abcdef0123456789abcdef01234567"}
SITE_CONTRACT = os.path.join(TESTS, "fixtures", "site_record_contract_layout_v1.json")
# The site's shard test vectors (record_contract.json layout.detail.vectors):
# NCT01975376 -> 80 and NCT00663858 -> 50 at 256 and at 128 shards,
# NCT01174160 -> 144 at 256 and 16 at 128.
VECTOR_IDS = ["NCT01975376", "NCT00663858", "NCT01174160"]


def site_contract(enabled: bool = True) -> dict[str, Any]:
    """The site's contract with its layout switch set."""
    with open(SITE_CONTRACT, encoding="utf-8") as f:
        contract = json.load(f)
    contract["layout"]["enabled"] = enabled
    return contract


def without_layout(contract: dict[str, Any]) -> dict[str, Any]:
    """The contract as site main has it before the reader ships: no layout section."""
    out = copy.deepcopy(contract)
    out.pop("layout", None)
    return out


def ids(n: int, first: int = 2000003) -> list[str]:
    """n distinct NCT ids: the site's test vectors first, then ids spread over the shards."""
    out = list(VECTOR_IDS)
    k = 0
    while len(out) < n:
        nct = f"NCT{first + 7 * k:08d}"
        if nct not in out:
            out.append(nct)
        k += 1
    return out[:n]


def whole_records(nct_ids: list[str]) -> list[dict[str, Any]]:
    """Whole study records as the weekly extraction writes them (the extractor's
    own output from mock registry studies), with lists filled the way real
    records fill them, unread fields among them, and a few edge values."""
    from validate_fixes import _make_category, _make_class, _make_measure, _make_study
    from src.extract_all import extract_demographics_from_study
    out = []
    for i, nct in enumerate(nct_ids):
        study = _make_study(nct, f"study {i}", [
            _make_measure("Sex: Female, Male", [_make_class("", [_make_category("Female", 40 + i), _make_category("Male", 35)])]),
            _make_measure("Race (NIH/OMB)", [_make_class("", [_make_category("White", 50 + i),
                                                               _make_category("Black or African American", 25)])]),
        ], total_participants=75 + i)
        r = extract_demographics_from_study(study, snapshot_date="2026-10-11")
        if i % 2 == 0:
            r["study_sites"] = [
                {"facility": f"Harbor Clinic {i}", "city": "Boston", "state": "Massachusetts", "zip": "02115",
                 "country": "United States", "geo_identification_method": "High Precision (Zip)"},
                {"facility": "Lakeside Hospital", "city": "Toronto", "state": "Ontario", "zip": "M5G",
                 "country": "Canada", "geo_identification_method": "Medium Precision (City)"}]
            r["countries"] = [{"country": "United States"}, {"country": "Canada"}]
        if i % 3 == 0:
            r["collaborators"] = [{"name": "National Heart, Lung, and Blood Institute", "class": "NIH"}]
            r["references"] = [{"pmid": str(31000000 + i), "citation": "Doe J. A randomized trial. 2024.",
                                "source": "pubmed", "type": "RESULT", "title": "A randomized trial"}]
        r["secondary_outcomes"] = [{"measure": "Exacerbations per year", "time_frame": "12 months",
                                    "description": "A long description no view reads. " * 4}]
        out.append(r)
    out[0]["brief_title"] = "Café study – naïve participants"     # non-ASCII text
    out[0]["official_title"] = "An official title"                # an optional core path
    out[1]["status"] = None                                       # null is a value
    out[1]["why_stopped"] = None
    return out


def write_full(path: str | os.PathLike[str], records: list[dict[str, Any]],
               stamps: dict[str, Any] | None = None) -> None:
    """A full-record file as the weekly job writes it (indent=2), plain or gzipped."""
    body = json.dumps({**(stamps or STAMPS), "data": records}, indent=2)
    if str(path).endswith(".gz"):
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(body)
    else:
        pathlib.Path(path).write_text(body)


def read_gz(path: str | os.PathLike[str]) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def split(tmp: pathlib.Path, records: list[dict[str, Any]], contract: dict[str, Any],
          stamps: dict[str, Any] | None = None, out: str = "dataset") -> pathlib.Path:
    """Run scripts/split_data.py on these records under this contract; return the dataset folder."""
    import split_data
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "record_contract.json").write_text(json.dumps(contract))
    write_full(tmp / "demographics.json", records, stamps)
    rc = split_data.main(["--contract", str(tmp / "record_contract.json"),
                          "--demographics", str(tmp / "demographics.json"), "--out-dir", str(tmp / out)])
    assert rc == 0
    return tmp / out
