"""Which ethnicity tables are counted (src/ethnicity_extractor.py with
src/table_selection.py).

Every study here is fictional: invented NCT IDs (NCT99...), titles and counts.
Each fixture reproduces a pattern seen in the 2026-11-08 snapshot: a race
table titled "Race/Ethnicity, Customized" whose "Unknown" row was added to
ethnicity unknown, the same people posted twice, disjoint parts that post
their own Number Analyzed, and the same subset posted twice.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Optional

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.ethnicity_extractor import extract_ethnicity_data  # noqa: E402


def _denoms(n: int) -> list[dict[str, Any]]:
    return [{"units": "Participants", "counts": [{"groupId": "BG000", "value": str(n)}]}]


def _table(title: str, rows: list[tuple[str, int]], analyzed: Optional[int] = None) -> dict[str, Any]:
    """One untitled class, labels on categories; analyzed posts the table's
    own Number Analyzed on its class, as the registry returns most of them."""
    cls: dict[str, Any] = {"categories": [{"title": label, "measurements": [{"groupId": "BG000", "value": str(v)}]}
                                          for label, v in rows]}
    if analyzed is not None:
        cls["denoms"] = _denoms(analyzed)
    return {"title": title, "paramType": "COUNT_OF_PARTICIPANTS", "unitOfMeasure": "Participants", "classes": [cls]}


def _study(nct_id: str, measures: list[dict[str, Any]], participants: int) -> dict[str, Any]:
    return {"protocolSection": {"identificationModule": {"nctId": nct_id},
                                "designModule": {"enrollmentInfo": {"count": participants}}},
            "resultsSection": {"baselineCharacteristicsModule": {
                "groups": [{"groupId": "BG000", "title": "All Participants"}],
                "denoms": _denoms(participants), "measures": measures}}}


ETH_100 = _table("Ethnicity (NIH/OMB)", [("Hispanic or Latino", 12), ("Not Hispanic or Latino", 83),
                                         ("Unknown or Not Reported", 5)])


def test_a_race_tables_unknown_row_is_not_added_to_ethnicity() -> None:
    """The common case: the race table is titled "Race/Ethnicity, Customized";
    its race rows were already dropped, and now its "Unknown" row is too."""
    race = _table("Race/Ethnicity, Customized", [("White", 70), ("Black or African American", 22), ("Unknown", 8)])
    eth = extract_ethnicity_data(_study("NCT99000201", [ETH_100, race], participants=100))
    assert eth["omb_totals"] == {"hispanic_latino": 12, "not_hispanic_latino": 83, "unknown_not_reported": 5}
    assert {"ethnicity_table_superseded", "multiple_ethnicity_tables_2"} <= set(eth["flags"])
    assert "denominator_balanced" not in eth["flags"]


def test_the_same_people_posted_twice_are_counted_once() -> None:
    repost = _table("Race/Ethnicity, Customized", [("Hispanic", 12), ("Not Hispanic", 83), ("Unknown", 5)])
    for measures in ([ETH_100, repost], [repost, ETH_100]):
        eth = extract_ethnicity_data(_study("NCT99000202", measures, participants=100))
        assert sum(eth["omb_totals"].values()) == 100
        assert eth["omb_totals"]["hispanic_latino"] == 12


def test_disjoint_parts_with_their_own_numbers_analyzed_are_summed() -> None:
    part_a = _table("Ethnicity (NIH/OMB)", [("Hispanic or Latino", 4), ("Not Hispanic or Latino", 36)], analyzed=40)
    part_b = _table("Ethnicity (NIH/OMB)", [("Hispanic or Latino", 6), ("Not Hispanic or Latino", 54)], analyzed=60)
    eth = extract_ethnicity_data(_study("NCT99000203", [part_a, part_b], participants=100))
    assert eth["omb_totals"] == {"hispanic_latino": 10, "not_hispanic_latino": 90, "unknown_not_reported": 0}
    assert "ethnicity_tables_partitioned" in eth["flags"]
    assert "ethnicity_table_superseded" not in eth["flags"]


def test_the_same_subset_posted_twice_is_not_summed() -> None:
    """Both tables analyzed on the same 40 of 100: 80 is not the total, so
    nothing shows they are different people."""
    first = _table("Ethnicity (NIH/OMB)", [("Hispanic or Latino", 4), ("Not Hispanic or Latino", 36)], analyzed=40)
    again = _table("Race/Ethnicity, Customized", [("Hispanic", 4), ("Not Hispanic", 36)], analyzed=40)
    eth = extract_ethnicity_data(_study("NCT99000204", [first, again], participants=100))
    assert eth["omb_totals"] == {"hispanic_latino": 4, "not_hispanic_latino": 36, "unknown_not_reported": 60}
    assert "ethnicity_tables_partitioned" not in eth["flags"]


def test_a_repost_with_subgroup_detail_keeps_it() -> None:
    whole = _table("Ethnicity (NIH/OMB)", [("Hispanic or Latino", 30)])
    detail = _table("Ethnicity, Customized", [("Mexican", 20), ("Puerto Rican", 10)])
    for measures in ([whole, detail], [detail, whole]):
        eth = extract_ethnicity_data(_study("NCT99000205", measures, participants=30))
        assert eth["omb_totals"]["hispanic_latino"] == 30
        assert eth["subcategory_totals"] == {"hispanic_latino_mexican": 20, "hispanic_latino_puerto_rican": 10}


def test_one_combined_table_is_read_as_before() -> None:
    """A single "Race/Ethnicity, Customized" table holding both race and
    ethnicity rows: race rows dropped, ethnicity rows counted, unchanged."""
    combined = _table("Race/Ethnicity, Customized", [("White", 40), ("Black", 10), ("Hispanic or Latino", 8),
                                                     ("Not Hispanic or Latino", 42)])
    eth = extract_ethnicity_data(_study("NCT99000206", [combined], participants=50))
    assert eth["omb_totals"] == {"hispanic_latino": 8, "not_hispanic_latino": 42, "unknown_not_reported": 0}
    assert "ethnicity_table_superseded" not in eth["flags"]


@pytest.mark.parametrize("unknown_first", [False, True])
def test_when_no_table_names_anyone_the_fuller_unknown_table_counts(unknown_first: bool) -> None:
    stub = _table("Race and Ethnicity Not Collected", [("Number of participants", 0)])
    unknown = _table("Race/Ethnicity, Customized", [("Unknown", 25)])
    measures = [unknown, stub] if unknown_first else [stub, unknown]
    eth = extract_ethnicity_data(_study("NCT99000207", measures, participants=25))
    assert eth["reported"] is True
    assert eth["omb_totals"]["unknown_not_reported"] == 25


def test_flags_sorted_and_output_deterministic() -> None:
    race = _table("Race/Ethnicity, Customized", [("White", 70), ("Unknown", 30)])
    study = _study("NCT99000208", [ETH_100, race], participants=100)
    first, second = extract_ethnicity_data(study), extract_ethnicity_data(study)
    assert first == second and first["flags"] == sorted(first["flags"])


def _percent(table: dict[str, Any]) -> dict[str, Any]:
    table["paramType"] = "NUMBER"
    table["unitOfMeasure"] = "Percentage of participants"
    return table


def test_percentage_parts_convert_against_their_own_numbers_analyzed() -> None:
    """A reviewer's case: 100% Hispanic of a 40-person part and 100% Not
    Hispanic of a 60-person part, each posting Number Analyzed on its class.
    Converted against the whole baseline (100) both parts became 100; against
    their own numbers they are 40 and 60 and add up as parts."""
    part_a = _percent(_table("Ethnicity (NIH/OMB)", [("Hispanic or Latino", 100), ("Not Hispanic or Latino", 0)],
                             analyzed=40))
    part_b = _percent(_table("Ethnicity (NIH/OMB)", [("Hispanic or Latino", 0), ("Not Hispanic or Latino", 100)],
                             analyzed=60))
    eth = extract_ethnicity_data(_study("NCT99000209", [part_a, part_b], participants=100))
    assert eth["omb_totals"] == {"hispanic_latino": 40, "not_hispanic_latino": 60, "unknown_not_reported": 0}
    assert "ethnicity_tables_partitioned" in eth["flags"]



def _percent_classes(rows: list[tuple[str, str, int]]) -> dict[str, Any]:
    """A percentage table with labels on classes: (label, value %, class Number Analyzed)."""
    return {"title": "Ethnicity (NIH/OMB)", "paramType": "NUMBER", "unitOfMeasure": "Percentage of participants",
             "classes": [{"title": label, "denoms": _denoms(n),
                          "categories": [{"measurements": [{"groupId": "BG000", "value": value}]}]}
                         for label, value, n in rows]}


def test_class_numbers_that_differ_are_row_counts_not_denominators() -> None:
    """The registry pattern behind NCT02649218: each class "Number Analyzed"
    is that row's own count, and the percentages are of the whole baseline
    (23% of 100 = 23, 77% of 100 = 77). Converting each row against its own
    number would give 5 and 59; the baseline total is used instead."""
    table = _percent_classes([("Hispanic or Latino", "23", 23), ("Not Hispanic or Latino", "77", 77)])
    eth = extract_ethnicity_data(_study("NCT99000210", [table], participants=100))
    assert eth["omb_totals"] == {"hispanic_latino": 23, "not_hispanic_latino": 77, "unknown_not_reported": 0}


def test_a_class_number_every_class_repeats_is_the_tables_denominator() -> None:
    """The pattern behind NCT00812214: every class repeats one Number
    Analyzed (80 of 100), and the percentages add to 100% of those 80."""
    table = _percent_classes([("Hispanic or Latino", "20", 80), ("Not Hispanic or Latino", "80", 80)])
    eth = extract_ethnicity_data(_study("NCT99000210", [table], participants=100))
    assert eth["omb_totals"] == {"hispanic_latino": 16, "not_hispanic_latino": 64, "unknown_not_reported": 20}


def test_a_class_number_on_some_classes_only_is_not_the_tables_denominator() -> None:
    """A reviewer's case: 40 on one class and nothing on the other is not a
    number the table repeats; the rows convert against the baseline (100)."""
    table = _percent_classes([("Hispanic or Latino", "20", 40), ("Not Hispanic or Latino", "80", 40)])
    del table["classes"][1]["denoms"]
    eth = extract_ethnicity_data(_study("NCT99000211", [table], participants=100))
    assert eth["omb_totals"] == {"hispanic_latino": 20, "not_hispanic_latino": 80, "unknown_not_reported": 0}


def test_a_lone_rows_class_number_is_not_the_tables_denominator() -> None:
    """A reviewer's case: one class, one row (20% Hispanic) whose Number
    Analyzed is its own count (20). Nothing shows it is table-wide, so the
    row converts against the baseline: 20, not 4."""
    table = _percent_classes([("Hispanic or Latino", "20", 20)])
    eth = extract_ethnicity_data(_study("NCT99000212", [table], participants=100))
    assert eth["omb_totals"]["hispanic_latino"] == 20


def test_one_class_with_several_rows_uses_its_number() -> None:
    """The NIH/OMB template form: one class, a row per category, Number
    Analyzed 80 on the class: the rows are percentages of those 80."""
    eth = extract_ethnicity_data(_study("NCT99000213", [_percent(_table(
        "Ethnicity (NIH/OMB)", [("Hispanic or Latino", 25), ("Not Hispanic or Latino", 75)], analyzed=80))],
        participants=100))
    assert eth["omb_totals"] == {"hispanic_latino": 20, "not_hispanic_latino": 60, "unknown_not_reported": 20}
