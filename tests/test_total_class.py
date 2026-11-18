"""Subgroup classes beside a Total class (table_selection.total_class_view).

Every study here is fictional (NCT99... IDs). The first fixture copies the
labels and counts of NCT04911296: one baseline group of 150 participants, a
"Race/Ethnicity, Customized" measure and a "Sex: Female, Male" measure, each
with the classes "21 - 50 years", "51 - 65 years", "66 - 75 years" and
"Total". Read class by class it counted everyone twice (Hispanic 168 of
150); read from its Total class it counts them once.
"""
from __future__ import annotations

import copy
import os
import sys
from typing import Any, Optional

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import table_selection  # noqa: E402
from src.ethnicity_extractor import extract_ethnicity_data  # noqa: E402
from src.gender_extractor import extract_gender_data  # noqa: E402
from src.race_extractor import combined_classification, extract_race_data  # noqa: E402
from src.sex_extractor import extract_sex_data  # noqa: E402

FLAG = table_selection.TOTAL_CLASS_FLAG


def _denoms(n: int, group: str = "BG000") -> list[dict[str, Any]]:
    return [{"units": "Participants", "counts": [{"groupId": group, "value": str(n)}]}]


def _class(title: Optional[str], rows: list[tuple[str, Any]], analyzed: Optional[int] = None) -> dict[str, Any]:
    cls: dict[str, Any] = {"categories": [{"title": label, "measurements": [{"groupId": "BG000", "value": str(v)}]}
                                          for label, v in rows]}
    if title is not None:
        cls["title"] = title
    if analyzed is not None:
        cls["denoms"] = _denoms(analyzed)
    return cls


def _measure(title: str, classes: list[dict[str, Any]], pct: bool = False) -> dict[str, Any]:
    return {"title": title, "paramType": "NUMBER" if pct else "COUNT_OF_PARTICIPANTS",
            "unitOfMeasure": "percentage of participants" if pct else "Participants", "classes": classes}


def _study(nct_id: str, measures: list[dict[str, Any]], participants: int) -> dict[str, Any]:
    return {"protocolSection": {"identificationModule": {"nctId": nct_id},
                                "designModule": {"enrollmentInfo": {"count": participants}}},
            "resultsSection": {"baselineCharacteristicsModule": {
                "groups": [{"groupId": "BG000", "title": "Swallow Group"}],
                "denoms": _denoms(participants), "measures": measures}}}


RACE_LABELS = ["Black - Non-Hispanic", "American Indian/Alaskan Native", "Hispanic",
               "Asian/Pacific Islander", "White - Non-Hispanic"]
BANDS = ["21 - 50 years", "51 - 65 years", "66 - 75 years"]
# NCT04911296's counts, in RACE_LABELS order, per class.
RACE_COUNTS = {"21 - 50 years": [6, 1, 32, 0, 11], "51 - 65 years": [5, 1, 28, 0, 16],
               "66 - 75 years": [7, 0, 24, 2, 17], "Total": [18, 2, 84, 2, 44]}
SEX_COUNTS = {"21 - 50 years": [29, 21], "51 - 65 years": [28, 22], "66 - 75 years": [28, 22], "Total": [85, 65]}


def _race_ethnicity_measure(counts: dict[str, list[int]] = RACE_COUNTS) -> dict[str, Any]:
    return _measure("Race/Ethnicity, Customized",
                    [_class(name, list(zip(RACE_LABELS, values)), sum(values)) for name, values in counts.items()])


def _sex_measure() -> dict[str, Any]:
    return _measure("Sex: Female, Male",
                    [_class(name, list(zip(["Female", "Male"], values)), sum(values))
                     for name, values in SEX_COUNTS.items()])


SHAPE = _study("NCT99000113", [_race_ethnicity_measure(), _sex_measure()], 150)


def test_ethnicity_counts_the_total_class_once() -> None:
    """NCT04911296's ethnicity: Hispanic 84 (not 32 + 28 + 24 + 84 = 168),
    Not Hispanic 18 + 44 from the "- Non-Hispanic" rows, and the 4 American
    Indian and Asian participants, whose ethnicity the table does not state,
    balanced into unknown. The block now sums to the 150 participants."""
    eth = extract_ethnicity_data(SHAPE)
    assert eth["reported"] is True
    assert eth["omb_totals"] == {"hispanic_latino": 84, "not_hispanic_latino": 62, "unknown_not_reported": 4}
    assert sum(eth["omb_totals"].values()) == 150
    assert FLAG in eth["flags"]
    assert all(FLAG in c["flags"] for c in eth["raw_categories"])
    # The posted rows of the Total class only: five, not twenty.
    assert [c["original"] for c in eth["raw_categories"]
            if c["omb_category"] in ("hispanic_latino", "not_hispanic_latino")] == [
        "Black - Non-Hispanic", "Hispanic", "White - Non-Hispanic"]


def test_race_counts_the_total_class_once() -> None:
    """The race block reads the same Total class: White 44, Black 18, and the
    84 Hispanic participants, whose race the table does not state, are
    unknown (as the "Hispanic" row of any combined classification is)."""
    race = extract_race_data(SHAPE)
    nonzero = {k: v for k, v in race["omb_totals"].items() if v}
    assert nonzero == {"black_african_american": 18, "american_indian_alaska_native": 2, "asian": 2,
                       "white": 44, "unknown_not_reported": 84}
    assert sum(race["omb_totals"].values()) == 150
    assert race["unknown_ethnicity_only"] == 84 and race["unknown_inferred"] == 0
    assert [(r["original"], r["count"]) for r in race["skipped_ethnicity_rows"]] == [("Hispanic", 84)]
    assert FLAG in race["skipped_ethnicity_rows"][0]["flags"]
    assert FLAG in race["flags"]
    # combined_classification, which the ethnicity block reads, sees the same table.
    combined = combined_classification(SHAPE)
    assert combined is not None and combined["race_rows"] == 66 and combined["ethnicity_rows"] == 84


def test_legacy_sex_counts_the_total_class_once() -> None:
    sex = extract_sex_data(SHAPE)
    assert sex["totals"] == {"female": 85, "male": 65, "unknown": 0}
    assert sex["unknown_inferred"] == 0
    assert FLAG in sex["flags"]


def test_legacy_gender_counts_the_total_class_once() -> None:
    """The legacy gender extractor shares the row loop's rule."""
    study = _study("NCT99000114", [_measure("Gender", [
        _class(name, [("Woman", w), ("Man", m)], w + m)
        for name, (w, m) in {"Site A": (10, 5), "Site B": (6, 9), "Total": (16, 14)}.items()])], 30)
    gender = extract_gender_data(study)
    assert gender["totals"]["woman"] == 16 and gender["totals"]["man"] == 14
    assert FLAG in gender["flags"]


def test_the_input_measure_is_not_modified() -> None:
    study = copy.deepcopy(SHAPE)
    extract_race_data(study)
    extract_ethnicity_data(study)
    extract_sex_data(study)
    assert study == SHAPE


def test_subgroups_that_do_not_add_up_still_give_the_total() -> None:
    """The Total class is read whether or not the subgroup rows add up to it
    (here they overlap: 40 + 40 Hispanic of a Total 60)."""
    counts = {"Site A": [0, 0, 40, 0, 10], "Site B": [0, 0, 40, 0, 10], "Total": [0, 0, 60, 0, 20]}
    eth = extract_ethnicity_data(_study("NCT99000115", [_race_ethnicity_measure(counts)], 80))
    assert eth["omb_totals"] == {"hispanic_latino": 60, "not_hispanic_latino": 20, "unknown_not_reported": 0}


def test_a_subgroup_may_leave_out_a_row() -> None:
    """A band with no Asian participants that posts no Asian row is still a
    breakdown of the Total's rows."""
    classes = [_class("Band 1", [("Hispanic or Latino", 3), ("Not Hispanic or Latino", 7)]),
               _class("Band 2", [("Hispanic or Latino", 2), ("Not Hispanic or Latino", 6), ("Unknown", 2)]),
               _class("Total", [("Hispanic or Latino", 5), ("Not Hispanic or Latino", 13), ("Unknown", 2)])]
    eth = extract_ethnicity_data(_study("NCT99000116", [_measure("Ethnicity (NIH/OMB)", classes)], 20))
    assert eth["omb_totals"] == {"hispanic_latino": 5, "not_hispanic_latino": 13, "unknown_not_reported": 2}
    assert FLAG in eth["flags"]


@pytest.mark.parametrize("title", ["total", "  TOTAL ", "Total"])
def test_the_title_is_matched_trimmed_and_case_insensitively(title: str) -> None:
    measure = _measure("Sex: Female, Male", [_class("Arm 1", [("Female", 3), ("Male", 2)]),
                                              _class(title, [("Female", 3), ("Male", 2)])])
    assert table_selection.total_class_view(measure, None)[1] is True


def test_a_subgroup_row_the_total_lacks_leaves_the_measure_as_before() -> None:
    """When a subgroup posts a row the Total class does not have, the classes
    are not one breakdown of the Total's rows: every class is read, as
    before, and no row carries the flag."""
    measure = _measure("Sex: Female, Male", [_class("Part 1", [("Female", 3), ("Male", 2), ("Unknown", 1)]),
                                              _class("Total", [("Female", 3), ("Male", 2)])])
    view, total_only = table_selection.total_class_view(measure, None)
    assert total_only is False and view is measure
    sex = extract_sex_data(_study("NCT99000117", [measure], 6))
    assert sex["totals"] == {"female": 6, "male": 4, "unknown": 1}
    assert FLAG not in sex["flags"]


def test_a_class_titled_total_holding_a_count_is_a_row_not_a_total_class() -> None:
    """Rows on classes, each holding one "Count" category: a class titled
    Total is then a row named Total, and nothing is a breakdown of it."""
    measure = _measure("Race (NIH/OMB)", [_class("White", [("Count", 30)]), _class("Black", [("Count", 10)]),
                                          _class("Total", [("Count", 40)])])
    assert table_selection.total_class_view(measure, None)[1] is False


@pytest.mark.parametrize("classes", [
    [_class("Total", [("Female", 3), ("Male", 2)])],                                     # Total alone
    [_class("Arm 1", [("Female", 3), ("Male", 2)]), _class("Arm 2", [("Female", 1), ("Male", 1)])],  # no Total
    [_class("Total", [("Female", 3), ("Male", 2)]), _class("total", [("Female", 3), ("Male", 2)])],  # two
    [_class("Arm 1", [("Count", 5)]), _class("Total", [("Female", 3), ("Male", 2)])],    # unlabelled subgroup
])
def test_other_shapes_are_read_as_before(classes: list[dict[str, Any]]) -> None:
    assert table_selection.total_class_view(_measure("Sex: Female, Male", classes), None)[1] is False


def test_percentages_convert_against_the_total_class() -> None:
    """A percentage measure read from its Total class converts against that
    class's Number Analyzed (80), not the baseline (100): the subgroup
    classes' numbers (30, 50) differ and would not be a denominator."""
    classes = [_class("Site A", [("Female", 50.0), ("Male", 50.0)], 30),
               _class("Site B", [("Female", 40.0), ("Male", 60.0)], 50),
               _class("Total", [("Female", 25.0), ("Male", 75.0)], 80)]
    measure = _measure("Sex: Female, Male", classes, pct=True)
    assert table_selection.number_analyzed(measure, None) == 80
    sex = extract_sex_data(_study("NCT99000118", [measure], 100))
    assert sex["totals"]["female"] == 20 and sex["totals"]["male"] == 60
    assert sex["unknown_inferred"] == 20


@pytest.mark.parametrize("empty_row", [
    {"title": "Male"},                                                   # no measurements key
    {"title": "Male", "measurements": []},                               # empty list
    {"title": "Male", "measurements": [{"groupId": "BG000", "value": None}]},  # a null value
])
def test_a_total_row_without_a_value_leaves_the_measure_as_before(empty_row: dict[str, Any]) -> None:
    """A Total class whose row posts no value would read as 0 while the
    subgroup rows that hold the count were discarded: the measure is read
    as before, every class summed, and nothing is flagged."""
    total = _class("Total", [("Female", 5)])
    total["categories"].append(empty_row)
    measure = _measure("Sex: Female, Male", [_class("Site A", [("Female", 3), ("Male", 4)]),
                                              _class("Site B", [("Female", 2), ("Male", 1)]), total])
    view, total_only = table_selection.total_class_view(measure, None)
    assert total_only is False and view is measure
    sex = extract_sex_data(_study("NCT99000119", [measure], 10))
    assert sex["totals"]["male"] == 5
    assert FLAG not in sex["flags"]


def test_a_one_row_total_class_converts_percentages_as_any_one_row_table() -> None:
    """percentage_denominator keeps its own rules on the Total class: a lone
    row's class number is as likely its own count as a denominator, so a
    one-row Total converts against the baseline (100), as main does for this
    measure, while number_analyzed reports the Total class's 80."""
    classes = [_class("Site A", [("White", 50.0)], 30), _class("Site B", [("White", 10.0)], 50),
               _class("Total", [("White", 25.0)], 80)]
    measure = _measure("Race (NIH/OMB)", classes, pct=True)
    view, total_only = table_selection.total_class_view(measure, None)
    assert total_only is True
    assert table_selection.number_analyzed(measure, None) == 80
    assert table_selection.percentage_denominator(view, None, 100) == 100


def _two_group_class(title: str, rows: list[tuple[str, Any, Any]]) -> dict[str, Any]:
    """A class in a two-group study: BG000 is the arm, BG001 the Overall group."""
    return {"title": title, "categories": [
        {"title": label, "measurements": [{"groupId": "BG000", "value": arm}, {"groupId": "BG001", "value": overall}]}
        for label, arm, overall in rows]}


def test_the_total_row_must_hold_a_number_for_the_overall_group() -> None:
    """A two-group study (an arm and the Overall group BG001): the Total row
    "Male" posts the arm's value but a null Overall value, which
    sum_measurements reads as 0 for BG001. The measure is read as before
    (main's reading): the rule does not repair a measure whose Total class
    cannot be read, it only declines to drop the subgroup rows that hold the
    count. Female, which the Total posts, is still summed with the subgroups
    there, as on main."""
    measure = _measure("Sex: Female, Male", [
        _two_group_class("Site A", [("Female", "3", "3"), ("Male", "4", "4")]),
        _two_group_class("Site B", [("Female", "2", "2"), ("Male", "1", "1")]),
        _two_group_class("Total", [("Female", "5", "5"), ("Male", "5", None)])])
    assert table_selection.total_class_view(measure, "BG001")[1] is False
    study = _study("NCT99000121", [measure], 10)
    study["resultsSection"]["baselineCharacteristicsModule"]["groups"] = [
        {"groupId": "BG000", "title": "Arm"}, {"groupId": "BG001", "title": "Total"}]
    study["resultsSection"]["baselineCharacteristicsModule"]["denoms"] = [
        {"units": "Participants", "counts": [{"groupId": "BG000", "value": "10"}, {"groupId": "BG001", "value": "10"}]}]
    sex = extract_sex_data(study)
    assert sex["totals"]["male"] == 5          # 4 + 1 from the subgroups, the Total's null read as 0
    assert sex["totals"]["female"] == 10       # 3 + 2 + Total 5: main's double count, unchanged
    assert FLAG not in sex["flags"]
    # With the Overall value posted, the Total class is read.
    measure["classes"][2]["categories"][1]["measurements"][1]["value"] = "5"
    assert table_selection.total_class_view(measure, "BG001")[1] is True


def test_a_total_row_that_is_not_a_number_leaves_the_measure_as_before() -> None:
    measure = _measure("Sex: Female, Male", [_class("Site A", [("Female", 3), ("Male", 4)]),
                                              _class("Site B", [("Female", 2), ("Male", 1)]),
                                              _class("Total", [("Female", 5), ("Male", "NA")])])
    assert table_selection.total_class_view(measure, None)[1] is False
