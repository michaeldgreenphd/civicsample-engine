"""Which ethnicity tables are counted (src/ethnicity_extractor.py with
src/table_selection.py).

Every study here is fictional: invented NCT IDs (NCT99...), titles and counts.
Each fixture reproduces a pattern seen in the 2026-11-08 snapshot: a race
table titled "Race/Ethnicity, Customized" whose "Unknown" row was added to
ethnicity unknown, the same people posted twice, disjoint parts that post
their own Number Analyzed, and the same subset posted twice.

The last section covers the race rows of an exact combined race and
ethnicity classification, which are counted as not Hispanic.
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
    its race rows were already dropped, and now its "Unknown" row is too. It
    has no Hispanic or Not Hispanic row, so it is skipped as a race table
    (race_only_table_skipped) before the table selection, which therefore no
    longer reports it as a superseded second ethnicity table."""
    race = _table("Race/Ethnicity, Customized", [("White", 70), ("Black or African American", 22), ("Unknown", 8)])
    eth = extract_ethnicity_data(_study("NCT99000201", [ETH_100, race], participants=100))
    assert eth["omb_totals"] == {"hispanic_latino": 12, "not_hispanic_latino": 83, "unknown_not_reported": 5}
    assert "race_only_table_skipped" in eth["flags"]
    assert not {"ethnicity_table_superseded", "multiple_ethnicity_tables_2"} & set(eth["flags"])
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


def test_a_part_with_only_unknown_rows_is_still_a_part() -> None:
    """A reviewer's case: parts of 40 and 60 by their own Number Analyzed;
    the 40-person part posts only "Unknown". Both are summed as parts, and
    the 40 stay explicit unknowns rather than becoming inferred ones."""
    part_a = _table("Ethnicity (NIH/OMB)", [("Unknown or Not Reported", 40)], analyzed=40)
    part_b = _table("Ethnicity (NIH/OMB)", [("Hispanic or Latino", 6), ("Not Hispanic or Latino", 54)], analyzed=60)
    eth = extract_ethnicity_data(_study("NCT99000214", [part_a, part_b], participants=100))
    assert eth["omb_totals"] == {"hispanic_latino": 6, "not_hispanic_latino": 54, "unknown_not_reported": 40}
    assert "ethnicity_tables_partitioned" in eth["flags"]
    assert "denominator_balanced" not in eth["flags"]



# ── race rows of a combined classification ────────────────────────────────
#
# When the race extractor finds a trial's one counted race table to be an
# exact combined classification (race_extractor.combined_classification) and
# this block counted that same table, the race rows' participants are not
# Hispanic (src/ethnicity_extractor.py, "Race rows of a combined
# classification"). Every other block is as before.

from src import race_extractor  # noqa: E402

COMBINED = "Race/Ethnicity, Customized"
MOVED_FLAG = "not_hispanic_from_combined_table"


def _combined(rows: list[tuple[str, int]], participants: int, title: str = COMBINED,
              extra: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    return _study("NCT99000601", [_table(title, rows)] + (extra or []), participants=participants)


def test_an_exact_combined_classifications_race_rows_are_not_hispanic() -> None:
    """The NCT00416078 shape: Caucasian 39 / African-American 10 / Hispanic 2 /
    Unknown 2, of 53. Before, 49 participants were balanced into unknown."""
    eth = extract_ethnicity_data(_combined([("Caucasian", 39), ("African-American", 10), ("Hispanic", 2),
                                            ("Unknown", 2)], 53))
    assert eth["omb_totals"] == {"hispanic_latino": 2, "not_hispanic_latino": 49, "unknown_not_reported": 2}
    assert eth["not_hispanic_from_race_rows"] == 49
    assert MOVED_FLAG in eth["flags"] and "denominator_balanced" not in eth["flags"]
    # raw_categories keeps the posted ethnicity rows only.
    assert [(c["original"], c["count"]) for c in eth["raw_categories"]] == [("Hispanic", 2), ("Unknown", 2)]


def test_a_race_row_that_also_names_an_unknown_stays_unknown() -> None:
    """"Other/Unknown" is race other, but some of its participants are
    unknown and may be Hispanic: it is balanced into unknown, as before."""
    eth = extract_ethnicity_data(_combined([("White", 60), ("Black", 20), ("Hispanic", 12), ("Other/Unknown", 8)], 100))
    assert eth["omb_totals"] == {"hispanic_latino": 12, "not_hispanic_latino": 80, "unknown_not_reported": 8}
    assert eth["not_hispanic_from_race_rows"] == 80
    assert {MOVED_FLAG, "denominator_balanced"} <= set(eth["flags"])


@pytest.mark.parametrize("rows, participants, why", [
    ([("White", 30), ("Black", 12), ("Hispanic", 6)], 50, "the Hispanic row does not account for the shortfall"),
    ([("White", 30), ("Black", 12), ("Hispanic", 8), ("Not Hispanic or Latino", 42)], 50,
     "the table counts Not Hispanic itself"),
    ([("White", 30), ("Black", 20)], 50, "no Hispanic row"),
    ([("Hispanic or Latino", 8), ("Not Hispanic or Latino", 42)], 50, "an ethnicity table, no race rows"),
])
def test_tables_that_are_not_an_exact_combined_classification_are_as_before(
        rows: list[tuple[str, int]], participants: int, why: str) -> None:
    study = _combined(rows, participants)
    assert race_extractor.combined_classification(study) is None, why
    eth = extract_ethnicity_data(study)
    assert eth["not_hispanic_from_race_rows"] == 0 and MOVED_FLAG not in eth["flags"], why


def test_a_separate_ethnicity_table_is_left_alone() -> None:
    """The race table is exact, but this block counts the trial's own
    ethnicity table: that table's Not Hispanic count stands."""
    study = _combined([("Caucasian", 39), ("African-American", 10), ("Hispanic", 2), ("Unknown", 2)], 53,
                      extra=[_table("Ethnicity (NIH/OMB)", [("Hispanic or Latino", 2), ("Not Hispanic or Latino", 48),
                                                            ("Unknown or Not Reported", 3)])])
    assert race_extractor.combined_classification(study) is not None
    eth = extract_ethnicity_data(study)
    assert eth["omb_totals"] == {"hispanic_latino": 2, "not_hispanic_latino": 48, "unknown_not_reported": 3}
    assert eth["not_hispanic_from_race_rows"] == 0


def test_a_combined_table_without_ethnicity_in_its_title_is_not_read_here() -> None:
    """Titled "Race": the race extractor reads it, this extractor does not, so
    this block has no table and is not reported."""
    study = _combined([("Caucasian", 39), ("African-American", 10), ("Hispanic", 2), ("Unknown", 2)], 53, title="Race")
    assert race_extractor.combined_classification(study) is not None
    eth = extract_ethnicity_data(study)
    assert eth["reported"] is False and eth["not_hispanic_from_race_rows"] == 0


def test_counts_that_disagree_between_the_two_readings_are_left_alone() -> None:
    """"Latina/Hispanic" is a Hispanic row to the race extractor, but this
    extractor does not map it, so its Hispanic count differs: nothing moves.
    The label is a stand-in for any row the race extractor reads as Hispanic
    and this vocabulary does not; earlier versions of this test used
    "American Hispanic" and "Another Hispanic, Latino"."""
    study = _combined([("White", 30), ("Black", 12), ("Latina/Hispanic", 8)], 50)
    combined = race_extractor.combined_classification(study)
    assert combined is not None and combined["ethnicity_rows"] == 8
    eth = extract_ethnicity_data(study)
    assert eth["omb_totals"]["hispanic_latino"] != 8
    assert eth["not_hispanic_from_race_rows"] == 0 and MOVED_FLAG not in eth["flags"]


def test_american_hispanic_now_agrees_between_the_two_readings() -> None:
    """"American Hispanic" is now Hispanic to both extractors: the Hispanic
    counts agree, so the exact combined classification's race rows are
    counted as not Hispanic. Before, the block ended in all-zero rejection."""
    study = _combined([("White", 30), ("Black", 12), ("American Hispanic", 8)], 50)
    combined = race_extractor.combined_classification(study)
    assert combined is not None and combined["ethnicity_rows"] == 8
    eth = extract_ethnicity_data(study)
    assert eth["omb_totals"] == {"hispanic_latino": 8, "not_hispanic_latino": 42, "unknown_not_reported": 0}
    assert eth["not_hispanic_from_race_rows"] == 42 and MOVED_FLAG in eth["flags"]
    assert "ethnicity_rows_unread" not in eth["flags"]


def test_summed_race_parts_are_not_one_classification() -> None:
    adults = _table("Race/Ethnicity, Adults", [("White", 20), ("Hispanic", 10)], analyzed=30)
    children = _table("Race/Ethnicity, Children", [("Asian", 15), ("Hispanic or Latino", 5)], analyzed=20)
    study = _study("NCT99000602", [adults, children], participants=50)
    assert race_extractor.combined_classification(study) is None
    assert extract_ethnicity_data(study)["not_hispanic_from_race_rows"] == 0


def test_every_block_carries_the_field() -> None:
    assert extract_ethnicity_data(_study("NCT99000603", [ETH_100], participants=100))["not_hispanic_from_race_rows"] == 0
    assert extract_ethnicity_data(_study("NCT99000604", [], participants=100))["not_hispanic_from_race_rows"] == 0



def test_hispanic_race_rows_stay_out_of_not_hispanic() -> None:
    """"Hispanic White" names Hispanic participants: they are never counted as
    not Hispanic. Joined labels are now read (block comment "Race joined to
    ethnicity in one label"): "Hispanic White" is Hispanic and the
    "Non-Hispanic" rows are Not Hispanic on their own labels, so nothing is
    moved from race rows and nothing is balanced into unknown. Before, the 5
    were balanced into unknown and the 42 moved from the race reading."""
    eth = extract_ethnicity_data(_combined([("Non-Hispanic White", 30), ("Non-Hispanic Black", 12),
                                            ("Hispanic White", 5), ("Hispanic", 3)], 50))
    assert eth["omb_totals"] == {"hispanic_latino": 8, "not_hispanic_latino": 42, "unknown_not_reported": 0}
    assert eth["not_hispanic_from_race_rows"] == 0
    assert not {MOVED_FLAG, "denominator_balanced"} & set(eth["flags"])
    assert "race_joined_label" in eth["flags"]


def test_a_race_row_with_no_response_stays_unknown() -> None:
    eth = extract_ethnicity_data(_combined([("White", 60), ("Black", 20), ("Hispanic", 12), ("Other/No response", 8)], 100))
    assert eth["omb_totals"] == {"hispanic_latino": 12, "not_hispanic_latino": 80, "unknown_not_reported": 8}


# ── combined race and ethnicity tables read row by row ───────────────────
#
# src/ethnicity_extractor.py, block comments "Dimension wording around an
# ethnicity label", "A race table under a race-and-ethnicity title" and "Race
# joined to ethnicity in one label". Fictional studies; each reproduces a
# registry shape named in those comments.


def _class_rows(title: str, rows: list[tuple[str, int]]) -> dict[str, Any]:
    """Labels on the classes, one untitled category each, as the registry
    returns a "Race/Ethnicity, Customized" table posted with class titles."""
    return {"title": title, "paramType": "COUNT_OF_PARTICIPANTS", "unitOfMeasure": "Participants",
            "classes": [{"title": label, "categories": [{"measurements": [{"groupId": "BG000", "value": str(v)}]}]}
                        for label, v in rows]}


def test_two_prefixed_classifications_in_one_table_are_read() -> None:
    """The NCT00707031 shape: a race classification and an ethnicity
    classification of the same 634 participants, every row prefixed. Before:
    all-zero rejection, reported False."""
    table = _class_rows(COMBINED, [("Race: Caucasian/White", 588), ("Race: Black", 18), ("Race: Asian/Oriental", 7),
                                   ("Race: Other", 21), ("Ethnicity: Hispanic", 170), ("Ethnicity: Non Hispanic", 464)])
    eth = extract_ethnicity_data(_study("NCT99000701", [table], participants=634))
    assert eth["reported"] is True
    assert eth["omb_totals"] == {"hispanic_latino": 170, "not_hispanic_latino": 464, "unknown_not_reported": 0}
    assert [(c["original"], c["count"]) for c in eth["raw_categories"]] == [
        ("Ethnicity: Hispanic", 170), ("Ethnicity: Non Hispanic", 464)]
    assert "dimension_wording_stripped" in eth["flags"]
    assert "all_zero_rejection" not in eth["flags"]


def test_a_race_unknown_row_beside_an_ethnicity_classification_is_not_ethnicity() -> None:
    """The NCT04459715 shape: "Race-Unknown or Not Reported" scored 90 against
    "Unknown or Not Reported" and was counted as ethnicity unknown on top of
    an ethnicity classification of everyone."""
    table = _class_rows(COMBINED, [("Ethnicity-Hispanic or Latino", 9), ("Ethnicity-Not Hispanic or Latino", 60),
                                   ("Ethnicity-Unknown or Not Reported", 4), ("Race-White", 55),
                                   ("Race-Unknown or Not Reported", 18)])
    eth = extract_ethnicity_data(_study("NCT99000702", [table], participants=73))
    assert eth["omb_totals"] == {"hispanic_latino": 9, "not_hispanic_latino": 60, "unknown_not_reported": 4}
    assert "Race-Unknown or Not Reported" not in [c["original"] for c in eth["raw_categories"]]


@pytest.mark.parametrize("label, category", [
    ("Race : Hispanic", "hispanic_latino"),
    ("Ethnicity - Not Hispanic or Latino", "not_hispanic_latino"),
    ("Ethnic group - Hispanic or Latino", "hispanic_latino"),
    ("Race and Ethnicity : Hispanic", "hispanic_latino"),
    ("Hispanic (Regardless of Race)", "hispanic_latino"),
    ("Hispanic or Latino (any race)", "hispanic_latino"),
])
def test_dimension_wording_is_removed(label: str, category: str) -> None:
    from src.ethnicity_extractor import map_ethnicity_category
    mapping = map_ethnicity_category(label)
    assert (mapping["omb_category"], mapping["confidence"]) == (category, "high")
    assert mapping["original"] == label and "dimension_wording_stripped" in mapping["flags"]


@pytest.mark.parametrize("label", ["Race: Unknown", "Race - Not Reported", "Race-Unknown or Not Reported",
                                   "Race: Missing"])
def test_a_race_dimension_label_is_not_ethnicity(label: str) -> None:
    from src.ethnicity_extractor import map_ethnicity_category
    mapping = map_ethnicity_category(label)
    assert mapping["confidence"] == "low"
    assert {"race_dimension_label", "unmapped"} <= set(mapping["flags"])


def test_a_label_naming_the_dimension_without_a_separator_is_matched_as_posted() -> None:
    from src.ethnicity_extractor import map_ethnicity_category
    mapping = map_ethnicity_category("Ethnicity unknown")
    assert mapping["omb_category"] == "unknown_not_reported" and mapping["flags"] == []


def test_a_race_table_under_an_ethnicity_title_is_not_an_ethnicity_report() -> None:
    """The NCT02787551 shape: race rows and the race classification's unknown
    row. Before: reported, 514 of unknown ethnicity (2 posted, 512 balanced)."""
    table = _class_rows(COMBINED, [("American Indian or Alaska Native", 0), ("Asian/Oriental", 7), ("Black", 19),
                                   ("Native Hawaiian or Other Pacific Islander", 1), ("White", 485),
                                   ("Unknown or Not Reported'", 2)])
    eth = extract_ethnicity_data(_study("NCT99000703", [table], participants=514))
    assert eth["reported"] is False
    assert eth["omb_totals"] == {"hispanic_latino": 0, "not_hispanic_latino": 0, "unknown_not_reported": 0}
    assert eth["raw_categories"] == [] and eth["flags"] == ["race_only_table_skipped"]


@pytest.mark.parametrize("label", ["Ethnicity: Not Collected", "Ethnicity - Other", "Ethnic group: declined"])
def test_an_unread_row_of_the_ethnicity_dimension_keeps_the_table_an_ethnicity_report(label: str) -> None:
    """A dropped row that names the ethnicity dimension is not a race row, so
    the table is read as before (a review finding on the race-only rule)."""
    table = _table(COMBINED, [("Unknown", 5), (label, 45)])
    eth = extract_ethnicity_data(_study("NCT99000710", [table], participants=50))
    assert eth["reported"] is True and "race_only_table_skipped" not in eth["flags"]
    assert eth["omb_totals"]["unknown_not_reported"] == 50


def test_a_mapped_row_of_the_ethnicity_dimension_keeps_the_table_an_ethnicity_report() -> None:
    """"Ethnicity: Unknown" maps to unknown and is kept; beside "Race: White"
    it is the table's own ethnicity classification (a review finding)."""
    table = _class_rows(COMBINED, [("Race: White", 40), ("Ethnicity: Unknown", 40)])
    eth = extract_ethnicity_data(_study("NCT99000711", [table], participants=40))
    assert eth["reported"] is True and "race_only_table_skipped" not in eth["flags"]
    assert eth["omb_totals"] == {"hispanic_latino": 0, "not_hispanic_latino": 0, "unknown_not_reported": 40}


def test_a_spanish_row_keeps_the_table_an_ethnicity_report() -> None:
    """"Spanish" is Hispanic evidence ("Spanish origin" is in the vocabulary):
    not a race row, so the table is not skipped (a review finding)."""
    eth = extract_ethnicity_data(_study("NCT99000712", [_table(COMBINED, [("Unknown", 5), ("Spanish", 45)])],
                                        participants=50))
    assert eth["reported"] is True and "race_only_table_skipped" not in eth["flags"]
    assert "ethnicity_rows_unread" in eth["flags"]


def test_a_table_classifying_by_something_other_than_ethnicity_is_skipped() -> None:
    """Nationality rows beside an unknown row (the NCT02123329 shape): no
    Hispanic or Not Hispanic row, so the unknown row is not an ethnicity count."""
    eth = extract_ethnicity_data(_study("NCT99000713", [_table(COMBINED, [("Qatar", 61), ("Egypt", 90),
                                                                          ("Unknown", 3)])], participants=154))
    assert eth["reported"] is False and eth["flags"] == ["race_only_table_skipped"]


def test_race_inside_another_word_of_the_title_is_not_a_race_table() -> None:
    """"Contraception" holds the letters "race", not the word: the race-only
    rule does not apply (a review finding)."""
    table = _table("Ethnicity by Contraception", [("Unknown", 5), ("Not Collected", 45)])
    eth = extract_ethnicity_data(_study("NCT99000714", [table], participants=50))
    assert eth["reported"] is True and "race_only_table_skipped" not in eth["flags"]


def test_a_zero_hispanic_row_keeps_the_table_an_ethnicity_report() -> None:
    """A posted Hispanic row says how many were Hispanic, even when that is 0."""
    table = _table(COMBINED, [("White", 40), ("Black", 8), ("Hispanic", 0), ("Unknown", 2)])
    eth = extract_ethnicity_data(_study("NCT99000704", [table], participants=50))
    assert eth["reported"] is True and "race_only_table_skipped" not in eth["flags"]


def test_a_race_table_without_participants_in_race_rows_is_counted_as_before() -> None:
    """Only the unknown row has participants: the rule needs race rows that
    classify someone, so the table is read as before (unknown 25 + balanced)."""
    table = _table(COMBINED, [("White", 0), ("Unknown", 25)])
    eth = extract_ethnicity_data(_study("NCT99000705", [table], participants=25))
    assert eth["reported"] is True and eth["omb_totals"]["unknown_not_reported"] == 25


@pytest.mark.parametrize("label, category", [
    ("Non-Hispanic White", "not_hispanic_latino"),
    ("White (non-Hispanic)", "not_hispanic_latino"),
    ("Black Non-Hispanic", "not_hispanic_latino"),
    ("Hispanic White", "hispanic_latino"),
])
def test_a_label_joining_race_and_ethnicity_is_read_for_its_ethnicity(label: str, category: str) -> None:
    table = _table(COMBINED, [(label, 30), ("Asian", 20)])
    eth = extract_ethnicity_data(_study("NCT99000706", [table], participants=50))
    assert eth["omb_totals"][category] == 30
    assert eth["raw_categories"][0]["original"] == label
    assert "race_joined_label" in eth["raw_categories"][0]["flags"]


@pytest.mark.parametrize("label, category", [
    ("Race: Non-Hispanic White", "not_hispanic_latino"),
    ("Ethnicity: Hispanic White", "hispanic_latino"),
    ("Ethnicity - Non-Hispanic Black", "not_hispanic_latino"),
])
def test_a_prefixed_joined_label_is_read_for_its_ethnicity(label: str, category: str) -> None:
    """Both forms in one label: the dimension wording is removed before the
    joined reading (a review finding on the first version of this rule)."""
    table = _table(COMBINED, [(label, 30), ("Asian", 20)])
    eth = extract_ethnicity_data(_study("NCT99000709", [table], participants=50))
    assert eth["omb_totals"][category] == 30
    flags = eth["raw_categories"][0]["flags"]
    assert eth["raw_categories"][0]["original"] == label
    assert {"race_joined_label", "dimension_wording_stripped"} <= set(flags)


def test_a_fuzzy_match_that_drops_the_negation_is_refused() -> None:
    """"non-Hispanic/Latino" scores 88 against "Hispanic/Latino" (the ratio is
    case-sensitive): the joined reading must not count these as Hispanic."""
    table = _table(COMBINED, [("White (non-Hispanic/Latino)", 30), ("Hispanic", 5), ("Asian", 15)])
    eth = extract_ethnicity_data(_study("NCT99000707", [table], participants=50))
    assert eth["omb_totals"]["hispanic_latino"] == 5
    assert "ethnicity_rows_unread" in eth["flags"]


def test_an_unread_ethnicity_row_survives_all_zero_rejection() -> None:
    """"Hispanic or Latino origin" is not in the vocabulary: no counts, but
    the block says why, and the table is not taken for a race table. Earlier
    versions of this test used "American Hispanic" and "Non-Hispanic and
    Latino", both now entries."""
    table = _table(COMBINED, [("White", 30), ("Black", 12), ("Hispanic or Latino origin", 8)])
    eth = extract_ethnicity_data(_study("NCT99000708", [table], participants=50))
    assert eth["reported"] is False
    assert eth["flags"] == ["all_zero_rejection", "ethnicity_rows_unread"]


# ── plain vocabulary gaps ─────────────────────────────────────────────────
#
# Wordings of Hispanic and not Hispanic the vocabulary did not hold, now
# matched as posted (src/ethnicity_extractor.py, block comment "Plain
# vocabulary gaps"). One parametrized case per label family, each read in a
# race-titled table (where the row was dropped and flagged
# ethnicity_rows_unread) and in an ethnicity-only table (where its
# participants were counted as unknown).

VOCABULARY_GAPS = [
    # Hispanic named with "American", either order, hyphenated or not
    ("American Hispanic", "hispanic_latino"),
    ("Hispanic American", "hispanic_latino"),
    ("Hispanic-American", "hispanic_latino"),
    # both terms in one row
    ("Latino, Hispanic", "hispanic_latino"),
    ("Latino or Hispanic", "hispanic_latino"),
    ("Hispanic, Latinx", "hispanic_latino"),
    # the category named with its dimension
    ("Hispanic ethnicity", "hispanic_latino"),
    ("Hispanic Ethnicity", "hispanic_latino"),
    # "not of ... origin"
    ("not of Hispanic origin", "not_hispanic_latino"),
    ("not of Hispanic Origin", "not_hispanic_latino"),
    ("not of Hispanic-American Origin", "not_hispanic_latino"),
    # both terms negated
    ("Non-Hispanic and Non-Latino", "not_hispanic_latino"),
    ("Non-Hispanic, Non-Latino", "not_hispanic_latino"),
    ("Non-Hispanic or Non-Latino", "not_hispanic_latino"),
    # Latino negated alone
    ("non-Latino", "not_hispanic_latino"),
    ("Non-Latino", "not_hispanic_latino"),
    # counterparts posted beside the entries above
    ("Latino American", "hispanic_latino"),
    ("of Hispanic origin", "hispanic_latino"),
    ("Not Hispanic ethnicity", "not_hispanic_latino"),
    ("Not Hispanic Ethnicity", "not_hispanic_latino"),
    ("Non-Hispanic ethnicity", "not_hispanic_latino"),
    ("Non-Hispanic Ethnicity", "not_hispanic_latino"),
    ("Not Latino or Hispanic", "not_hispanic_latino"),
    ("Non American Hispanic", "not_hispanic_latino"),
    # follow-up wordings: the census "Another Hispanic" origin, in each spelling posted
    ("Another Hispanic, Latino, or Spanish origin", "hispanic_latino"),
    ("Another Hispanic, Latino/a, or Spanish Origin", "hispanic_latino"),
    ("Another Hispanic, Latino/a, or Spanish origin", "hispanic_latino"),
    ("Another Hispanic, Latino/a or Spanish Origin", "hispanic_latino"),
    ("Another Hispanic, Latino, or Spanish origin (e.g. Savadorian, Dominican, Columbian, Guatemalan, etc)",
     "hispanic_latino"),
    # follow-up wordings: not meeting the OMB definition
    ("Not meeting definition for Hispanic or Latino", "not_hispanic_latino"),
    ("Not meeting the definition for Hispanic or Latino", "not_hispanic_latino"),
    ("A person not meeting definition of Hispanic/Latino", "not_hispanic_latino"),
    # follow-up wordings: the complement of "Hispanic or Latino" in its tables
    ("Non-Hispanic and Latino", "not_hispanic_latino"),
]


@pytest.mark.parametrize("label, category", VOCABULARY_GAPS)
def test_a_vocabulary_gap_label_maps_as_posted(label: str, category: str) -> None:
    from src.ethnicity_extractor import map_ethnicity_category
    mapping = map_ethnicity_category(label)
    assert (mapping["omb_category"], mapping["subcategory"], mapping["confidence"]) == (category, None, "high")
    assert mapping["flags"] == [] and mapping["original"] == label


@pytest.mark.parametrize("label, category", VOCABULARY_GAPS)
def test_a_vocabulary_gap_row_of_a_race_titled_table_is_counted(label: str, category: str) -> None:
    other = "not_hispanic_latino" if category == "hispanic_latino" else "hispanic_latino"
    other_label = "Not Hispanic or Latino" if category == "hispanic_latino" else "Hispanic or Latino"
    table = _table(COMBINED, [("White", 30), ("Black", 12), (label, 8), (other_label, 42)])
    eth = extract_ethnicity_data(_study("NCT99000801", [table], participants=50))
    assert eth["reported"] is True
    assert eth["omb_totals"] == {category: 8, other: 42, "unknown_not_reported": 0}
    assert "ethnicity_rows_unread" not in eth["flags"]


@pytest.mark.parametrize("label, category", VOCABULARY_GAPS)
def test_a_vocabulary_gap_row_of_an_ethnicity_table_leaves_unknown(label: str, category: str) -> None:
    other_label = "Not Hispanic or Latino" if category == "hispanic_latino" else "Hispanic or Latino"
    table = _table("Ethnicity (NIH/OMB)", [(label, 8), (other_label, 40), ("Unknown or Not Reported", 2)])
    eth = extract_ethnicity_data(_study("NCT99000802", [table], participants=50))
    assert eth["omb_totals"][category] == 8
    assert eth["omb_totals"]["unknown_not_reported"] == 2
    assert "unmapped" not in eth["flags"]


@pytest.mark.parametrize("label", [
    "Caucasian (White, including Hispanic)",
    "including Hispanic)",
    "non-Latino, Hispanic",
    "White (non-latino or unknown ethnicity)",
])
def test_an_ambiguous_label_stays_unmapped(label: str) -> None:
    """A row whose participants the table does not divide by ethnicity
    (block comment "Plain vocabulary gaps"). In a race-titled table it is
    dropped and the block says so."""
    from src.ethnicity_extractor import map_ethnicity_category
    mapping = map_ethnicity_category(label)
    assert mapping["confidence"] == "low" and "unmapped" in mapping["flags"]
    # 50 rows of 60 participants: not an exact combined classification, so no
    # race rows are counted as not Hispanic and only the label's reading shows.
    table = _table(COMBINED, [("White", 30), ("Hispanic", 5), (label, 15)])
    eth = extract_ethnicity_data(_study("NCT99000803", [table], participants=60))
    assert eth["omb_totals"]["hispanic_latino"] == 5
    assert eth["omb_totals"]["not_hispanic_latino"] == 0
    assert "ethnicity_rows_unread" in eth["flags"]


def test_vocabulary_gap_entries_are_never_fuzzy_targets() -> None:
    """As a fuzzy target "Non-Hispanic and Non-Latino" read "Non-Hispanic and
    Latino" (ratio 92) as not Hispanic before its tables had been looked at.
    The exact-only keys are all in the vocabulary and none is offered to the
    fuzzy match: "Hispanic Americans" scores 97 against the exact-only
    "Hispanic American" and at most 71 against the rest, so it stays
    unmapped."""
    from src import ethnicity_extractor as ee
    assert ee._EXACT_ONLY_KEYS <= set(ee.ETHNICITY_MAPPINGS)
    assert not ee._EXACT_ONLY_KEYS & set(ee._FUZZY_KEYS)
    assert list(ee._FUZZY_KEYS) == [k for k in ee.ETHNICITY_MAPPINGS if k not in ee._EXACT_ONLY_KEYS]
    mapping = ee.map_ethnicity_category("Hispanic Americans")
    assert mapping["omb_category"] == "unknown_not_reported" and "unmapped" in mapping["flags"]


@pytest.mark.parametrize("hispanic, not_hispanic", [
    ("Hispanic Ethnicity", "Not Hispanic Ethnicity"),
    ("Hispanic Ethnicity", "Non-Hispanic Ethnicity"),
    ("Latino or Hispanic", "Not Latino or Hispanic"),
    ("Ethnicity: American hispanic", "Ethnicity: Non American hispanic"),
    ("White, of Hispanic origin", "White, not of Hispanic origin"),
])
def test_both_sides_of_a_posted_pair_are_read(hispanic: str, not_hispanic: str) -> None:
    """A table posting a wording and its opposite (block comment "Plain
    vocabulary gaps", "Counterparts"): reading one side alone would make the
    block all Hispanic, or all not Hispanic, among those it classifies."""
    table = _table(COMBINED, [("White", 40), ("Black", 10), (hispanic, 9), (not_hispanic, 41)])
    eth = extract_ethnicity_data(_study("NCT99000804", [table], participants=50))
    assert eth["omb_totals"] == {"hispanic_latino": 9, "not_hispanic_latino": 41, "unknown_not_reported": 0}
    assert "ethnicity_rows_unread" not in eth["flags"]
