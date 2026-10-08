"""The fields each recentStudies row of dashboard-summary.json carries from
its full record (owner decisions 6d and 5a, 2026-10-07).

Phones and summary-only archives show the summary's 500 recentStudies rows,
not the records. A row did not say whether its study lists any location, nor
carry the fields the study pop-up reads its Population from, so the site had
to leave both out or guess. scripts/generate_mobile_data.py now appends to
every row, from that study's full record:

- lists_locations: true when study_sites or countries lists an entry (the
  site's studyHasGeography), false when the record carries both keys and both
  are empty or null, and no key at all when a list is absent and the other
  lists nothing (absence is not "no locations");
- pediatric_status and std_ages, copied unchanged (null included) when the
  record has the key, and left out when it does not.

These tests pin those rules on records the extractor builds, and that the
new keys are the only change: every other key of the summary, firstView
included, and every existing key of every row keep their bytes.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
from typing import Any

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import generate_mobile_data as gmd  # noqa: E402

STAMPS = {"extracted_at": "2026-10-04T12:09:21+00:00", "pipeline_commit": "abc1234"}
ABSENT = object()       # the record lacks the key
SITE = {"facility": "General Hospital", "city": "Boston", "state": "Massachusetts", "zip": "02114",
        "country": "United States"}
COUNTRY = {"country": "United States"}


def _record(i: int, **fields: Any) -> dict[str, Any]:
    """A whole study record built by the extractor, then given these fields
    (ABSENT deletes the key)."""
    from src.extract_all import extract_demographics_from_study
    from validate_fixes import _make_study, _make_measure, _make_class, _make_category
    study = _make_study(f"NCT{i:08d}", f"study {i}", [
        _make_measure("Sex: Female, Male", [_make_class("", [_make_category("Female", 40), _make_category("Male", 35)])]),
    ], total_participants=75)
    record = extract_demographics_from_study(study, snapshot_date="2026-10-04")
    # A distinct results date each, newest first, so the rows keep this order.
    record["results_date"] = f"2026-{12 - i // 28:02d}-{28 - i % 28:02d}"
    for key, value in fields.items():
        if value is ABSENT:
            record.pop(key, None)
        else:
            record[key] = value
    return record


# (fields given to the record, the row's lists_locations: None for no key)
LOCATION_CASES: dict[str, tuple[dict[str, Any], bool | None]] = {
    "sites and countries": ({"study_sites": [SITE], "countries": [COUNTRY]}, True),
    "sites only": ({"study_sites": [SITE], "countries": []}, True),
    "countries only": ({"study_sites": [], "countries": [COUNTRY]}, True),
    "neither: both empty": ({"study_sites": [], "countries": []}, False),
    "neither: both null": ({"study_sites": None, "countries": None}, False),
    "neither: one null, one empty": ({"study_sites": None, "countries": []}, False),
    "both lists missing": ({"study_sites": ABSENT, "countries": ABSENT}, None),
    "sites missing, countries empty": ({"study_sites": ABSENT, "countries": []}, None),
    "countries missing, sites null": ({"study_sites": None, "countries": ABSENT}, None),
    "sites missing, countries listed": ({"study_sites": ABSENT, "countries": [COUNTRY]}, True),
    "countries missing, sites listed": ({"study_sites": [SITE], "countries": ABSENT}, True),
    # (x || []).length > 0 in JavaScript: a value without a length lists nothing.
    "an object, not a list": ({"study_sites": {"country": "France"}, "countries": []}, False),
}

# (fields given to the record, what the row carries of the two)
POPULATION_CASES: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "both present": ({"pediatric_status": "Pediatric Included", "std_ages": ["CHILD", "ADULT"]},
                     {"pediatric_status": "Pediatric Included", "std_ages": ["CHILD", "ADULT"]}),
    "pediatric_status only": ({"pediatric_status": "Adult Only", "std_ages": ABSENT},
                              {"pediatric_status": "Adult Only"}),
    "std_ages only": ({"pediatric_status": ABSENT, "std_ages": ["CHILD"]}, {"std_ages": ["CHILD"]}),
    "neither": ({"pediatric_status": ABSENT, "std_ages": ABSENT}, {}),
    "null and empty, copied as they are": ({"pediatric_status": None, "std_ages": []},
                                           {"pediatric_status": None, "std_ages": []}),
    "the extractor's own values": ({}, {"pediatric_status": "Not Specified", "std_ages": []}),
}


def _records() -> tuple[list[dict[str, Any]], dict[str, bool | None], dict[str, dict[str, Any]]]:
    records, flags, populations = [], {}, {}
    i = 0
    for fields, flag in LOCATION_CASES.values():
        records.append(_record(i, **fields))
        flags[records[-1]["nct_id"]] = flag
        populations[records[-1]["nct_id"]] = {"pediatric_status": "Not Specified", "std_ages": []}
        i += 1
    for fields, carried in POPULATION_CASES.values():
        records.append(_record(i, **fields))
        flags[records[-1]["nct_id"]] = False     # the extractor's empty lists
        populations[records[-1]["nct_id"]] = carried
        i += 1
    # One without a results date: it is no row, and nothing here changes that.
    records.append(_record(i, results_date=None))
    return records, flags, populations


def _summarise(tmp: pathlib.Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    src = tmp / "demographics.json"
    src.write_text(json.dumps({**STAMPS, "data": records}))
    out = tmp / "dashboard-summary.json"
    gmd.main(["--demographics", str(src), "--table", str(tmp / "absent.csv.gz"), "--out", str(out)])
    return json.loads(out.read_text())


@pytest.mark.parametrize("case", sorted(LOCATION_CASES))
def test_lists_locations_follows_the_sites_rule_and_says_nothing_for_absent_lists(case: str) -> None:
    fields, flag = LOCATION_CASES[case]
    record = _record(1, **fields)
    assert gmd.lists_locations(record) is flag
    assert ("lists_locations" in gmd.summary_row_fields(record)) is (flag is not None)


@pytest.mark.parametrize("case", sorted(POPULATION_CASES))
def test_the_population_fields_are_copied_unchanged_and_never_invented(case: str) -> None:
    fields, carried = POPULATION_CASES[case]
    row = gmd.summary_row_fields(_record(1, **fields))
    assert {k: v for k, v in row.items() if k != "lists_locations"} == carried


def test_every_row_carries_its_records_fields_last(tmp_path: pathlib.Path) -> None:
    records, flags, populations = _records()
    summary = _summarise(tmp_path, records)
    rows = summary["recentStudies"]
    assert [r["nct_id"] for r in rows] == [r["nct_id"] for r in records if r.get("results_date")]
    for row in rows:
        flag, carried = flags[row["nct_id"]], populations[row["nct_id"]]
        added = ([] if flag is None else ["lists_locations"]) + [k for k in gmd.POPULATION_FIELDS if k in carried]
        assert list(row)[list(row).index("reference_count") + 1:] == added, row["nct_id"]
        assert row.get("lists_locations", None) is flag, row["nct_id"]
        assert {k: row[k] for k in gmd.POPULATION_FIELDS if k in row} == carried, row["nct_id"]
        # The lists themselves stay off the row.
        assert "study_sites" not in row and "countries" not in row


def test_nothing_else_in_the_summary_changes(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The summary as it was (no fields added) against the summary now: every
    other key, firstView included, is byte-identical, and each row is the old
    row with the new keys appended."""
    records, _, _ = _records()
    (tmp_path / "now").mkdir()
    (tmp_path / "before").mkdir()
    now = _summarise(tmp_path / "now", records)
    monkeypatch.setattr(gmd, "summary_row_fields", lambda record: {})
    before = _summarise(tmp_path / "before", records)
    assert list(now) == list(before)
    for key in before:
        if key != "recentStudies":
            assert json.dumps(now[key], separators=(",", ":")) == json.dumps(before[key], separators=(",", ":")), key
    assert len(now["recentStudies"]) == len(before["recentStudies"])
    for new, old in zip(now["recentStudies"], before["recentStudies"]):
        new_bytes = json.dumps(new, separators=(",", ":"))
        old_bytes = json.dumps(old, separators=(",", ":"))
        assert new_bytes.startswith(old_bytes[:-1]), new["nct_id"]
        assert {k: v for k, v in new.items() if k in old} == old
        assert set(new) - set(old) <= {"lists_locations", *gmd.POPULATION_FIELDS}


def test_lists_locations_is_written_as_a_json_boolean(tmp_path: pathlib.Path) -> None:
    """The site reads lists_locations only when it is a real JSON true or false
    (site PR #261); a string, 0/1 or null would read as absent. So the flag is
    a Python bool, and the file holds the literals true and false."""
    records, _, _ = _records()
    summary = _summarise(tmp_path, records)
    raw = (tmp_path / "dashboard-summary.json").read_text()
    written = [row["lists_locations"] for row in summary["recentStudies"] if "lists_locations" in row]
    assert written and all(type(v) is bool for v in written)
    assert {True, False} <= set(written), "the fixture writes both values"
    # Every flag in the file is a bare true or false, never quoted, a number or null.
    values = [m.split(":", 1)[1] for m in re.findall(r'"lists_locations":[^,}]*', raw)]
    assert len(values) == len(written)
    assert set(values) == {"true", "false"}
    for case in LOCATION_CASES.values():
        flag = gmd.lists_locations(_record(1, **case[0]))
        assert flag is None or type(flag) is bool
