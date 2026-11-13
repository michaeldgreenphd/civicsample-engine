"""Which tables the legacy sex and gender extractors count
(src/sex_extractor.py, src/gender_extractor.py, src/table_selection.py).

Every study here is fictional: invented NCT IDs (NCT99...), titles and counts.
Each fixture reproduces a pattern seen in the 2026-11-08 snapshot: the same
people in a "Sex/Gender, Customized" and a "Sex: Female, Male" table, two
identical sex or gender tables, a typed table beside a "Donor Sex" or
"Sexual Activity" table, an all-zero typed template beside the real tables,
and disjoint parts that post their own Number Analyzed.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Optional

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.gender_extractor import extract_gender_data  # noqa: E402
from src.sex_extractor import extract_sex_data  # noqa: E402


def _denoms(n: int) -> list[dict[str, Any]]:
    return [{"units": "Participants", "counts": [{"groupId": "BG000", "value": str(n)}]}]


def _category_table(title: str, rows: list[tuple[str, int]], analyzed: Optional[int] = None) -> dict[str, Any]:
    """NIH/OMB template layout: one untitled class, labels on categories."""
    cls: dict[str, Any] = {"categories": [{"title": label, "measurements": [{"groupId": "BG000", "value": str(v)}]}
                                          for label, v in rows]}
    if analyzed is not None:
        cls["denoms"] = _denoms(analyzed)
    return {"title": title, "paramType": "COUNT_OF_PARTICIPANTS", "unitOfMeasure": "Participants", "classes": [cls]}


def _class_table(title: str, rows: list[tuple[str, int]], analyzed: Optional[int] = None) -> dict[str, Any]:
    """Customized layout: labels on classes."""
    classes = []
    for label, v in rows:
        cls: dict[str, Any] = {"title": label, "categories": [{"measurements": [{"groupId": "BG000", "value": str(v)}]}]}
        if analyzed is not None:
            cls["denoms"] = _denoms(analyzed)
        classes.append(cls)
    return {"title": title, "paramType": "COUNT_OF_PARTICIPANTS", "unitOfMeasure": "Participants", "classes": classes}


def _study(nct_id: str, measures: list[dict[str, Any]], participants: int) -> dict[str, Any]:
    return {"protocolSection": {"identificationModule": {"nctId": nct_id},
                                "designModule": {"enrollmentInfo": {"count": participants}}},
            "resultsSection": {"baselineCharacteristicsModule": {
                "groups": [{"groupId": "BG000", "title": "All Participants"}],
                "denoms": _denoms(participants), "measures": measures}}}


SEX_60 = _category_table("Sex: Female, Male", [("Female", 34), ("Male", 26)])


@pytest.mark.parametrize("customized_first", [False, True])
def test_the_same_people_in_a_customized_and_a_standard_sex_table_count_once(customized_first: bool) -> None:
    customized = _class_table("Sex/Gender, Customized", [("Female", 34), ("Male", 26)])
    measures = [customized, SEX_60] if customized_first else [SEX_60, customized]
    sex = extract_sex_data(_study("NCT99000301", measures, participants=60))
    assert sex["totals"] == {"female": 34, "male": 26, "unknown": 0}
    assert "sex_table_superseded" in sex["flags"]


def test_two_identical_sex_tables_count_once() -> None:
    sex = extract_sex_data(_study("NCT99000302", [SEX_60, SEX_60], participants=60))
    assert sex["totals"] == {"female": 34, "male": 26, "unknown": 0}


def test_a_typed_table_beats_another_populations_sex_table() -> None:
    """"Donor Sex" is not one of the parser's sex/gender measure types: used
    only when no typed table counts anyone, as in the manuscript parser."""
    donor = _category_table("Donor Sex", [("Female", 50), ("Male", 30)])
    sex = extract_sex_data(_study("NCT99000303", [donor, SEX_60], participants=60))
    assert sex["totals"] == {"female": 34, "male": 26, "unknown": 0}
    assert "sex_table_superseded" in sex["flags"]


def test_an_empty_typed_template_does_not_hide_the_real_tables() -> None:
    """An all-zero "Sex/Gender, Customized" template beside two part tables
    that hold the data and post Number Analyzed adding up to the total."""
    template = _class_table("Sex/Gender, Customized", [("Male", 0), ("Female", 0)])
    phase_1 = _class_table("Phase I: Youth sex", [("Male", 10), ("Female", 14)], analyzed=24)
    phase_2 = _class_table("Phase II: Youth sex", [("Male", 16), ("Female", 20)], analyzed=36)
    sex = extract_sex_data(_study("NCT99000304", [template, phase_1, phase_2], participants=60))
    assert sex["reported"] is True
    assert sex["totals"] == {"female": 34, "male": 26, "unknown": 0}
    assert "sex_tables_partitioned" in sex["flags"]


def test_disjoint_parts_with_their_own_numbers_analyzed_are_summed() -> None:
    caregivers = _category_table("Sex: Female, Male", [("Female", 20), ("Male", 10)], analyzed=30)
    patients = _category_table("Sex: Female, Male", [("Female", 14), ("Male", 16)], analyzed=30)
    sex = extract_sex_data(_study("NCT99000305", [caregivers, patients], participants=60))
    assert sex["totals"] == {"female": 34, "male": 26, "unknown": 0}
    assert "sex_tables_partitioned" in sex["flags"]


def test_the_same_subset_posted_twice_is_not_summed() -> None:
    first = _category_table("Sex: Female, Male", [("Female", 12), ("Male", 8)], analyzed=20)
    again = _class_table("Sex/Gender, Customized", [("Female", 12), ("Male", 8)], analyzed=20)
    sex = extract_sex_data(_study("NCT99000306", [first, again], participants=60))
    assert sex["totals"] == {"female": 12, "male": 8, "unknown": 40}
    assert sex["unknown_inferred"] == 40
    assert "sex_tables_partitioned" not in sex["flags"]


def test_one_sex_table_is_read_as_before() -> None:
    sex = extract_sex_data(_study("NCT99000307", [SEX_60], participants=60))
    assert sex["totals"] == {"female": 34, "male": 26, "unknown": 0}
    assert "sex_table_superseded" not in sex["flags"]


def test_two_identical_gender_tables_count_once() -> None:
    gender_table = _category_table("Gender", [("Woman", 30), ("Man", 28), ("Non-binary", 2)])
    gender = extract_gender_data(_study("NCT99000308", [gender_table, gender_table], participants=60))
    assert gender["totals"]["woman"] == 30 and gender["totals"]["man"] == 28 and gender["totals"]["nonbinary"] == 2
    assert gender["totals"]["unknown"] == 0
    assert "gender_table_superseded" in gender["flags"]


def test_flags_sorted_and_output_deterministic() -> None:
    customized = _class_table("Sex/Gender, Customized", [("Female", 34), ("Male", 26)])
    study = _study("NCT99000309", [SEX_60, customized], participants=60)
    first, second = extract_sex_data(study), extract_sex_data(study)
    assert first == second and first["flags"] == sorted(first["flags"])


def test_percentage_parts_convert_against_their_own_numbers_analyzed() -> None:
    """100% female of a 40-person part and 100% male of a 60-person part,
    Number Analyzed on each class: 40 and 60 summed as parts, not 100 each."""
    part_a = _category_table("Sex: Female, Male", [("Female", 100), ("Male", 0)], analyzed=40)
    part_b = _category_table("Sex: Female, Male", [("Female", 0), ("Male", 100)], analyzed=60)
    for table in (part_a, part_b):
        table["paramType"] = "NUMBER"
        table["unitOfMeasure"] = "Percentage of participants"
    sex = extract_sex_data(_study("NCT99000310", [part_a, part_b], participants=100))
    assert sex["totals"] == {"female": 40, "male": 60, "unknown": 0}
    assert "sex_tables_partitioned" in sex["flags"]


def test_identical_halves_are_a_repost_not_two_parts() -> None:
    """A reviewer's case: two identical tables (12 female / 8 male, each
    analyzed on 20) in a 40-person trial. Their numbers make up the total,
    but identical tables are one subset posted twice: counted once, and the
    other 20 are unknown_inferred."""
    first = _category_table("Sex: Female, Male", [("Female", 12), ("Male", 8)], analyzed=20)
    again = _class_table("Sex/Gender, Customized", [("Female", 12), ("Male", 8)], analyzed=20)
    sex = extract_sex_data(_study("NCT99000311", [first, again], participants=40))
    assert sex["totals"] == {"female": 12, "male": 8, "unknown": 20}
    assert "sex_tables_partitioned" not in sex["flags"]


def test_identical_halves_described_as_different_people_are_parts() -> None:
    """The registry pattern behind NCT03711370 and NCT05314712: the same
    counts for two populations the registrant describes differently
    (mothers / infants, adults / children). Those are parts."""
    mothers = _category_table("Sex: Female, Male", [("Female", 12), ("Male", 8)], analyzed=20)
    mothers["populationDescription"] = "Data are for mothers only (n = 20)"
    infants = _class_table("Sex/Gender, Customized", [("Female", 12), ("Male", 8)], analyzed=20)
    infants["populationDescription"] = "Data are for infants only (n = 20)"
    sex = extract_sex_data(_study("NCT99000312", [mothers, infants], participants=40))
    assert sex["totals"] == {"female": 24, "male": 16, "unknown": 0}
    assert "sex_tables_partitioned" in sex["flags"]
