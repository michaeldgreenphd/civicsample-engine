"""Per-trial baseline readings (src/baseline_overrides.py).

NCT07465926 posts eight groups in four matched comparisons whose treated arms
share two cohorts; its Total column counts people twice. Read through the
exception, BG000 and BG004 are dropped, the Total column is the sum of the six
kept groups (187,700) and rows differing only by their comparison tag are
merged. The fixture is the trial's record as posted in the 2026-11-15 snapshot
(the whole baseline module, verbatim), whose measures' sha256 the exception
pins: any edit of the module reads the trial as posted.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import baseline_overrides as bo  # noqa: E402
from src.extract_all import extract_demographics_from_study  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "nct07465926_baseline.json")
KEPT = ["BG001", "BG002", "BG003", "BG005", "BG006", "BG007"]
PARTITIONS = {"Age, Customized", "Sex/Gender, Customized", "Ethnicity (NIH/OMB)", "Race/Ethnicity, Customized"}


@pytest.fixture()
def study() -> dict[str, Any]:
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def _module(study: dict[str, Any]) -> dict[str, Any]:
    return study["resultsSection"]["baselineCharacteristicsModule"]


def _measure(study: dict[str, Any], title: str) -> dict[str, Any]:
    return next(m for m in _module(study)["measures"] if m["title"] == title)


def _values(cat: dict[str, Any]) -> dict[str, int]:
    return {m["groupId"]: int(m["value"]) for m in cat["measurements"]}


def test_the_exception_drops_the_repeated_treated_arms(study: dict[str, Any]) -> None:
    read, flag = bo.apply_baseline_override(study)
    assert flag == bo.APPLIED_FLAG
    module = _module(read)
    assert [g["id"] for g in module["groups"]] == KEPT + ["BG008"]
    counts = {c["groupId"]: int(c["value"]) for c in module["denoms"][0]["counts"]}
    assert counts == {"BG001": 17146, "BG002": 40152, "BG003": 40152, "BG005": 19202, "BG006": 35524,
                      "BG007": 35524, "BG008": 187700}


def test_every_total_is_the_sum_of_the_kept_groups(study: dict[str, Any]) -> None:
    read, _flag = bo.apply_baseline_override(study)
    for measure in _module(read)["measures"]:
        for cls in measure["classes"]:
            for cat in cls["categories"]:
                values = _values(cat)
                assert list(values) == KEPT + ["BG008"], (measure["title"], cat)
                assert values["BG008"] == sum(values[g] for g in KEPT), (measure["title"], cat)
        # The four tables that divide the participants among their rows sum to
        # them; the others (CKD, Hypertension, ...) count one condition.
        if measure["title"] in PARTITIONS:
            total = sum(_values(cat)["BG008"] for cls in measure["classes"] for cat in cls["categories"])
            assert total == 187700, measure["title"]


def test_rows_differing_only_by_their_comparison_are_merged(study: dict[str, Any]) -> None:
    read, _flag = bo.apply_baseline_override(study)
    race = _measure(read, "Race/Ethnicity, Customized")
    assert [c.get("title") for c in race["classes"]] == [
        "White", "Black or African American", "Asian", "Other Races", "Other, Unknown, or Not Reported"]
    white = _values(race["classes"][0]["categories"][0])
    assert white == {"BG001": 11773, "BG002": 26579, "BG003": 26813, "BG005": 12937, "BG006": 23485,
                     "BG007": 23707, "BG008": 125294}
    # The ethnicity table's classes are only the tag: one untitled class.
    ethnicity = _measure(read, "Ethnicity (NIH/OMB)")
    assert len(ethnicity["classes"]) == 1 and "title" not in ethnicity["classes"][0]
    assert [c["title"] for c in ethnicity["classes"][0]["categories"]] == [
        "Hispanic or Latino", "Not Hispanic or Latino", "Unknown or Not Reported"]


def test_the_study_passed_in_is_not_changed(study: dict[str, Any]) -> None:
    before = copy.deepcopy(study)
    bo.apply_baseline_override(study)
    extract_demographics_from_study(study, snapshot_date="2026-11-15")
    assert study == before


def test_the_record_counts_187700_in_race_ethnicity_and_sex(study: dict[str, Any]) -> None:
    record = extract_demographics_from_study(study, snapshot_date="2026-11-15")
    assert record is not None
    race = {k: v for k, v in record["race"]["omb_totals"].items() if v}
    assert race == {"white": 125294, "black_african_american": 32705, "asian": 7313, "other": 6517,
                    "unknown_not_reported": 15871}
    assert record["ethnicity"]["omb_totals"] == {"hispanic_latino": 20548, "not_hispanic_latino": 135074,
                                                 "unknown_not_reported": 32078}
    assert record["sex"]["totals"] == {"female": 97899, "male": 89520, "unknown": 281}
    for block in ("race", "ethnicity", "sex", "gender"):
        assert bo.APPLIED_FLAG in record[block]["flags"], block
        assert record[block]["flags"] == sorted(set(record[block]["flags"])), block
    assert record["raceBreakdown"]["Black or African American"] == {"count": 32705, "percent": 17.4}
    assert sum(r["count"] for r in record["sexBreakdown"].values()) == 187700
    # The registry's enrollment is not changed.
    assert record["enrollment"] == 451036


def test_the_manuscript_parser_reads_the_trial_as_posted(study: dict[str, Any]) -> None:
    """The sex/gender manuscript parser is vendored (AGENTS.md): its row is
    not read through the exception."""
    record = extract_demographics_from_study(study, snapshot_date="2026-11-15")
    assert record is not None
    sg = record["sex_gender"]
    assert (sg["n_female"], sg["n_male"], sg["n_unknown"]) == (116457, 107246, 345)


def _not_applied(study: dict[str, Any]) -> dict[str, Any]:
    read, flag = bo.apply_baseline_override(study)
    assert flag == bo.NOT_APPLIED_FLAG
    assert read is study
    record = extract_demographics_from_study(study, snapshot_date="2026-11-15")
    assert record is not None
    for block in ("race", "ethnicity", "sex", "gender"):
        assert bo.NOT_APPLIED_FLAG in record[block]["flags"], block
        assert bo.APPLIED_FLAG not in record[block]["flags"], block
    return record


def test_a_changed_group_title_reads_the_trial_as_posted(study: dict[str, Any]) -> None:
    _module(study)["groups"][4]["title"] = "SGLT2i With GLP-1 RA Add-On (Comparison 3, revised)"
    record = _not_applied(study)
    assert record["race"]["omb_totals"]["other"] == 224048


def test_a_changed_denominator_reads_the_trial_as_posted(study: dict[str, Any]) -> None:
    _module(study)["denoms"][0]["counts"][0]["value"] = "17000"
    _not_applied(study)


def test_a_measure_that_is_not_a_count_reads_the_trial_as_posted(study: dict[str, Any]) -> None:
    _module(study)["measures"].append({"title": "Age, Continuous", "paramType": "MEAN", "classes": []})
    _not_applied(study)


def test_a_value_that_is_not_a_count_reads_the_trial_as_posted(study: dict[str, Any]) -> None:
    _measure(study, "Race/Ethnicity, Customized")["classes"][0]["categories"][0]["measurements"][1]["value"] = "NA"
    _not_applied(study)


def test_the_fixture_is_the_pinned_module(study: dict[str, Any]) -> None:
    spec = bo.OVERRIDES["NCT07465926"]
    assert bo.measures_sha256(_module(study)) == spec["measures_sha256"]
    assert len(_module(study)["measures"]) == 11


def test_a_missing_row_reads_the_trial_as_posted(study: dict[str, Any]) -> None:
    """Without "White (Comparison 1)", White would be 113,521 and the race rows
    175,927 (a review finding)."""
    race = _measure(study, "Race/Ethnicity, Customized")
    race["classes"] = [c for c in race["classes"] if c["title"] != "White (Comparison 1)"]
    _not_applied(study)


def test_a_changed_value_reads_the_trial_as_posted(study: dict[str, Any]) -> None:
    """A registry correction is read as posted until the exception is
    reviewed again."""
    _measure(study, "Sex/Gender, Customized")["classes"][0]["categories"][0]["measurements"][1]["value"] = "9532"
    _not_applied(study)


def test_a_row_missing_a_kept_group_reads_the_trial_as_posted(study: dict[str, Any]) -> None:
    """Summed as zero, the missing BG001 would cut White by 11,773 (a review
    finding). Read as posted, the race reader takes the posted Total column."""
    row = _measure(study, "Race/Ethnicity, Customized")["classes"][0]["categories"][0]
    row["measurements"] = [m for m in row["measurements"] if m["groupId"] != "BG001"]
    record = _not_applied(study)
    assert record["race"]["omb_totals"]["other"] == 224048


def test_a_row_repeating_a_group_reads_the_trial_as_posted(study: dict[str, Any]) -> None:
    row = _measure(study, "Sex/Gender, Customized")["classes"][0]["categories"][0]
    row["measurements"].append(dict(row["measurements"][1]))
    _not_applied(study)


def test_another_trial_is_read_as_posted(study: dict[str, Any]) -> None:
    study["protocolSection"]["identificationModule"]["nctId"] = "NCT99000912"
    read, flag = bo.apply_baseline_override(study)
    assert read is study and flag is None
    record = extract_demographics_from_study(study, snapshot_date="2026-11-15")
    assert record is not None
    assert all(not f.startswith("baseline_override") for f in record["race"]["flags"])
    assert record["race"]["omb_totals"]["other"] == 224048


@pytest.mark.parametrize("title, untagged", [
    ("White (Comparison 1)", "White"), ("Other, Unknown, or Not Reported (Comparison 4)",
                                        "Other, Unknown, or Not Reported"),
    ("Comparison 2", None), (None, None), ("White", "White"), ("Comparison of arms", "Comparison of arms"),
])
def test_untagged(title: str | None, untagged: str | None) -> None:
    assert bo._untagged(title) == untagged


def test_flag_blocks_keeps_sorted_unique_flags() -> None:
    blocks: list[dict[str, Any] | None] = [{"flags": ["z", "a", "a"]}, None, {"flags": []}]
    bo.flag_blocks(blocks, bo.APPLIED_FLAG)
    assert blocks[0] == {"flags": ["a", "baseline_override", "z"]}
    assert blocks[2] == {"flags": ["baseline_override"]}
    bo.flag_blocks(blocks, None)
    assert blocks[0] == {"flags": ["a", "baseline_override", "z"]}
