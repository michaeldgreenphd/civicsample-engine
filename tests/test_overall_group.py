"""The baseline Total group (src/utils.py, block comment "The registry's
whole-population baseline group").

Every study here is fictional: invented NCT IDs (NCT99000701-703), group
titles and counts. The registry's v2 API names a baseline group's id "id";
the measurements and denominators refer to it as "groupId". The older
fixtures in this repository post groups under "groupId", which is still read.
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
from src.race_extractor import extract_race_data  # noqa: E402
from src.sex_extractor import extract_sex_data  # noqa: E402
from src.utils import get_overall_group_id, get_total_baseline_participants  # noqa: E402


def _groups_study(titles: list[str], key: str = "id") -> dict[str, Any]:
    groups = [{key: f"BG{i:03d}", "title": title} for i, title in enumerate(titles)]
    return {"resultsSection": {"baselineCharacteristicsModule": {"groups": groups}}}


@pytest.mark.parametrize("titles, expected", [
    (["Drug A", "Placebo", "Total"], "BG002"),
    (["Drug A", "Placebo", "TOTAL"], "BG002"),
    (["Drug A", "Placebo", "Total Title"], "BG002"),
    (["Drug A", "Placebo", "Total~(N=584)"], "BG002"),
    # An arm whose title holds "overall" or "ALL" before the Total.
    (["Tafamidis 61/80 mg Treated-Overall", "Placebo", "Total"], "BG002"),
    (["Overall BRCAm", "Total"], "BG001"),
    (["ALL Subjects", "AML Subjects", "Total"], "BG002"),
    # No Total last: only an exact whole-population title.
    (["Overall", "Cohort 1", "Cohort 2"], "BG000"),
    (["Cohort 1", "All Participants", "Cohort 2"], "BG001"),
    (["Overall Population - Part 1", "Part 2"], None),
    (["Drug A", "Pooled AF37702 Inj."], None),
    (["All Participants"], None),  # one group: every measurement is the overall
])
def test_the_overall_group_is_the_total_last_else_an_exact_whole_population_title(
        titles: list[str], expected: Optional[str]) -> None:
    assert get_overall_group_id(_groups_study(titles)) == expected


def test_a_fixture_posting_groupid_is_still_read() -> None:
    assert get_overall_group_id(_groups_study(["Drug A", "Placebo", "Total"], key="groupId")) == "BG002"


# ── Rows that post the Total before another group ──────────────────────────
# Two arms (30 and 20) and a Total (50). Each row, and the denominators, post
# the Total's measurement first; before this fix every reader took the last
# measurement, the second arm's.

ARMS = [("BG000", "Drug A", 30), ("BG001", "Placebo", 20), ("BG002", "Total", 50)]


def _row(label: str, values: dict[str, int]) -> dict[str, Any]:
    order = ["BG002", "BG000", "BG001"]
    return {"title": label, "measurements": [{"groupId": g, "value": str(values[g])} for g in order]}


def _study(nct_id: str, measure_title: str, rows: list[tuple[str, int, int]]) -> dict[str, Any]:
    """rows: (label, Drug A count, Placebo count); the Total is their sum."""
    categories = [_row(label, {"BG000": a, "BG001": b, "BG002": a + b}) for label, a, b in rows]
    measure = {"title": measure_title, "paramType": "COUNT_OF_PARTICIPANTS",
               "unitOfMeasure": "Participants", "classes": [{"categories": categories}]}
    baseline = {"groups": [{"id": g, "title": t} for g, t, _n in ARMS],
                "denoms": [{"units": "Participants", "counts": [
                    {"groupId": g, "value": str(n)} for g, _t, n in [ARMS[2], ARMS[0], ARMS[1]]]}],
                "measures": [measure]}
    return {"protocolSection": {"identificationModule": {"nctId": nct_id},
                                "designModule": {"enrollmentInfo": {"count": 50}}},
            "resultsSection": {"baselineCharacteristicsModule": baseline}}


def test_the_baseline_population_is_the_total_s_denominator() -> None:
    study = _study("NCT99000701", "Race (NIH/OMB)", [("White", 20, 15), ("Asian", 10, 5)])
    assert get_total_baseline_participants(study, get_overall_group_id(study)) == 50


def test_race_rows_are_read_at_the_total() -> None:
    study = _study("NCT99000701", "Race (NIH/OMB)", [("White", 20, 15), ("Asian", 10, 5)])
    race = extract_race_data(study)
    assert {k: v for k, v in race["omb_totals"].items() if v} == {"white": 35, "asian": 15}


def test_ethnicity_rows_are_read_at_the_total() -> None:
    study = _study("NCT99000702", "Ethnicity (NIH/OMB)",
                   [("Hispanic or Latino", 6, 4), ("Not Hispanic or Latino", 24, 16)])
    ethnicity = extract_ethnicity_data(study)
    assert {k: v for k, v in ethnicity["omb_totals"].items() if v} == {
        "hispanic_latino": 10, "not_hispanic_latino": 40}


def test_sex_rows_are_read_at_the_total() -> None:
    study = _study("NCT99000703", "Sex: Female, Male", [("Female", 18, 12), ("Male", 12, 8)])
    sex = extract_sex_data(study)
    assert (sex["totals"]["female"], sex["totals"]["male"]) == (30, 20)
