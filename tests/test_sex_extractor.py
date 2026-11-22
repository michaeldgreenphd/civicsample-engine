"""Race rows posted under a sex/gender title, on the legacy sex path
(src/sex_extractor.py, sg=v1).

Every study here is fictional: invented NCT IDs (NCT990002..), titles and
counts. Each fixture reproduces a table layout seen in the registry:

- a "Sex/Gender, Customized" table of race rows beside a "Sex: Female, Male"
  table for the same participants (the race rows were counted as race and
  again as sex "unknown");
- such a table as the trial's only sex-titled table, beside a race table;
- race rows in a sex-titled (not combined) table, including a race label the
  old keyword test let through ("Guamanian" contains "man");
- race crossed with sex under a sex/gender title, whose rows carry sex;
- sex/gender tables the race gate rejects (an "Unknown", "Female" or "Male"
  row among race rows), which keep the routing they had.

The table test is race_extractor's own gate, so the invariant checked
throughout is: a row the race extractor reads from a sex/gender-titled table
is never also counted as sex "unknown".
"""
from __future__ import annotations

import os
import sys
from typing import Any, Optional

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.race_extractor import extract_race_data, is_sex_titled_race_table  # noqa: E402
from src.sex_extractor import extract_sex_data  # noqa: E402

REASON = "race_label_in_sex_table"


# ── fictional study builder (the layouts of tests/test_race_extractor.py) ──

def _class_table(title: str, rows: list[tuple[str, int]]) -> dict[str, Any]:
    """A customized table: the label is the class title."""
    return {"title": title, "paramType": "NUMBER", "unitOfMeasure": "participants",
            "classes": [{"title": label, "categories": [{"measurements": [{"groupId": "BG000", "value": str(v)}]}]}
                        for label, v in rows]}


def _category_table(title: str, rows: list[tuple[str, int]]) -> dict[str, Any]:
    """An NIH/OMB-template table: one untitled class, labels on categories."""
    return {"title": title, "paramType": "COUNT_OF_PARTICIPANTS", "unitOfMeasure": "Participants",
            "classes": [{"categories": [{"title": label, "measurements": [{"groupId": "BG000", "value": str(v)}]}
                                        for label, v in rows]}]}


def _study(nct_id: str, measures: list[dict[str, Any]], participants: Optional[int]) -> dict[str, Any]:
    """A single-group fictional study; participants fills the baseline denoms."""
    baseline: dict[str, Any] = {"groups": [{"groupId": "BG000", "title": "All Participants"}], "measures": measures}
    if participants is not None:
        baseline["denoms"] = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": str(participants)}]}]
    return {"protocolSection": {"identificationModule": {"nctId": nct_id},
                                "designModule": {"enrollmentInfo": {"count": participants}}},
            "resultsSection": {"baselineCharacteristicsModule": baseline}}


def _quarantined(sex: dict[str, Any], reason: str = REASON) -> dict[str, int]:
    return {q["original"]: q["count"] for q in sex["quarantined_labels"] if q["reason"] == reason}


def _unknown_labels(sex: dict[str, Any]) -> set[str]:
    return {c["original"] for c in sex["raw_categories"] if c["category"] == "unknown"}


def _race_labels_from_sex_titled_tables(race: dict[str, Any]) -> set[str]:
    return {c["original"] for c in race["raw_categories"] if "race_from_sex_titled_table" in c["flags"]}


SEX_TABLE = _category_table("Sex: Female, Male", [("Female", 25), ("Male", 35)])
RACE_IN_COMBINED = _class_table("Sex/Gender, Customized", [
    ("African American/African Heritage", 6), ("East Asian/South East Asian", 2), ("White", 52),
])


# ── race rows quarantined, not counted as sex unknown ──────────────────────

def test_race_rows_beside_a_sex_table_are_quarantined_not_sex_unknown() -> None:
    """Counted once, as race; sex comes from the sex table alone. Before this
    rule the same 60 participants were also sex unknown 60."""
    study = _study("NCT99000201", [RACE_IN_COMBINED, SEX_TABLE], participants=60)
    sex = extract_sex_data(study)
    assert sex["reported"] is True
    assert sex["totals"] == {"female": 25, "male": 35, "unknown": 0}
    assert (sex["unknown_explicit"], sex["unknown_inferred"]) == (0, 0)
    assert _quarantined(sex) == {"African American/African Heritage": 6,
                                 "East Asian/South East Asian": 2, "White": 52}
    assert "quarantined_label" in sex["flags"]
    assert "ambiguous_combined_table" not in sex["flags"]
    race = extract_race_data(study)
    assert _race_labels_from_sex_titled_tables(race) == set(_quarantined(sex))


def test_a_trial_whose_only_sex_titled_table_is_race_reports_no_sex() -> None:
    """A race table supersedes the sex-titled one for race; its rows are still
    not sex, so nothing is counted for sex and the trial is not reported."""
    study = _study("NCT99000202", [
        _class_table("Sex/Gender, Customized", [("White", 30)]),
        _category_table("Race (NIH/OMB)", [("Asian", 0), ("Black or African American", 0), ("White", 30)]),
    ], participants=30)
    sex = extract_sex_data(study)
    assert sex["reported"] is False
    assert sex["totals"] == {"female": 0, "male": 0, "unknown": 0}
    assert sex["raw_categories"] == []
    assert _quarantined(sex) == {"White": 30}
    race = extract_race_data(study)
    assert race["omb_totals"]["white"] == 30
    assert "race_table_superseded" in race["flags"]


def test_the_baseline_gap_stays_inferred_unknown() -> None:
    """Quarantine removes the double count only: participants the sex table
    leaves out are still balanced into unknown_inferred."""
    study = _study("NCT99000203", [
        _category_table("Sex: Female, Male", [("Female", 20), ("Male", 25)]),
        _class_table("Sex/Gender, Customized", [("White", 50)]),
    ], participants=50)
    sex = extract_sex_data(study)
    assert sex["totals"] == {"female": 20, "male": 25, "unknown": 5}
    assert (sex["unknown_explicit"], sex["unknown_inferred"]) == (0, 5)
    assert _quarantined(sex) == {"White": 50}


def test_race_rows_in_a_sex_titled_table_get_the_race_reason() -> None:
    """A sex-titled (not combined) table. "White" was already quarantined by
    the keyword test, as an unmapped label; "Guamanian" was not ("man" is a
    sex keyword) and was counted as sex unknown. Both are race rows."""
    study = _study("NCT99000204", [
        _class_table("Sex, Customized", [("White", 20), ("Guamanian", 4)]),
        _category_table("Sex: Female, Male", [("Female", 10), ("Male", 14)]),
    ], participants=24)
    sex = extract_sex_data(study)
    assert sex["totals"] == {"female": 10, "male": 14, "unknown": 0}
    assert _quarantined(sex) == {"White": 20, "Guamanian": 4}
    assert _quarantined(sex, "unmapped_non_sex_label") == {}
    race = extract_race_data(study)
    assert _race_labels_from_sex_titled_tables(race) == {"White", "Guamanian"}


def test_race_by_sex_rows_under_a_sex_title_stay_counted_as_sex() -> None:
    """"White male" is race and sex at once: race reads its race part, sex
    reads its sex word. Nothing is quarantined."""
    study = _study("NCT99000205", [
        _class_table("Sex/Gender, Customized", [("White male", 3), ("White female", 2)]),
    ], participants=5)
    sex = extract_sex_data(study)
    assert sex["totals"] == {"female": 2, "male": 3, "unknown": 0}
    assert sex["quarantined_labels"] == []
    race = extract_race_data(study)
    assert "race_by_sex_table" in race["flags"]
    assert race["omb_totals"]["white"] == 5


# ── tables the race gate rejects keep their routing ────────────────────────

@pytest.mark.parametrize("rows, expected_totals", [
    # "Unknown" in a sex table is more likely sex than race: the gate rejects
    # the table, so race does not read "White" and sex keeps both rows.
    ([("White", 10), ("Unknown", 2)], {"female": 0, "male": 0, "unknown": 12}),
    # A sex row beside a race row: the race row stays sex unknown.
    ([("White", 10), ("Female", 2)], {"female": 2, "male": 0, "unknown": 10}),
    ([("Female", 10), ("Male", 12), ("White", 3)], {"female": 10, "male": 12, "unknown": 3}),
])
def test_tables_the_race_gate_rejects_are_unchanged(rows: list[tuple[str, int]],
                                                     expected_totals: dict[str, int]) -> None:
    study = _study("NCT99000206", [_class_table("Sex/Gender, Customized", rows)], participants=None)
    sex = extract_sex_data(study)
    assert sex["totals"] == expected_totals
    assert _quarantined(sex) == {}
    assert "White" in _unknown_labels(sex)
    race = extract_race_data(study)
    assert race["reported"] is False


def test_a_race_titled_sex_table_is_not_the_gate() -> None:
    """A table titled for race is a typed race table, not a sex/gender-titled
    one; the gate (and so the new quarantine) does not apply to it."""
    assert is_sex_titled_race_table(_class_table("Race/Sex", [("White", 10)])) is False
    assert is_sex_titled_race_table(_class_table("Sex/Gender, Customized", [("White", 10)])) is True


# ── the invariant over every fixture above ─────────────────────────────────

@pytest.mark.parametrize("measures, participants", [
    ([RACE_IN_COMBINED, SEX_TABLE], 60),
    ([_class_table("Sex/Gender, Customized", [("White", 30)])], 30),
    ([_class_table("Sex, Customized", [("White", 20), ("Guamanian", 4)])], 24),
    ([_class_table("Sex/Gender, Customized", [("White male", 3), ("White female", 2)])], 5),
    ([_class_table("Sex/Gender, Customized", [("White", 10), ("Unknown", 2)])], 12),
    ([_class_table("Sex/Gender, Customized", [("White", 10), ("Female", 2)])], 12),
])
def test_no_row_is_both_race_from_a_sex_title_and_sex_unknown(measures: list[dict[str, Any]],
                                                              participants: int) -> None:
    study = _study("NCT99000207", measures, participants)
    sex, race = extract_sex_data(study), extract_race_data(study)
    assert not (_race_labels_from_sex_titled_tables(race) & _unknown_labels(sex))
    assert sex["flags"] == sorted(set(sex["flags"]))
