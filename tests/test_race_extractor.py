"""Race tables that cross race with sex (src/race_extractor.py).

Every study here is fictional: invented NCT IDs (NCT99...), titles and
counts. Each fixture reproduces one label pattern seen in the registry, in the
layout it was posted in (customized tables put the label on the class, the
NIH/OMB template on the category):

- "X : Female"                   race and sex joined by a colon
- "X (Female)"                   sex in brackets, one sex posted only
- "X- Male"                      hyphen, with ethnicity-only and residual rows
- "X Male Veterans"              a race-and-sex phrase with a descriptor
- race labels in a sex-titled table, plain and crossed with sex

and the shapes a looser rule would misread (a sex word on some rows only,
"Man" the Manchu ethnic group, sex rows with no race part, a waist-circumference
table split by sex). The rule for duplicate tables follows the sex/gender
parser: race-titled tables first, then one fullest table, the first on ties.
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

from src import race_extractor as rx  # noqa: E402
from src.race_extractor import extract_race_data, split_race_by_sex_label  # noqa: E402

RACE_KEYS = ("american_indian_alaska_native", "asian", "black_african_american",
             "native_hawaiian_pacific_islander", "white", "more_than_one_race",
             "unknown_not_reported", "other")


# ── fictional study builder ────────────────────────────────────────────────

def _class_row(label: str, value: Any) -> dict[str, Any]:
    """A customized-table row: the label is the class title."""
    return {"title": label, "categories": [{"measurements": [{"groupId": "BG000", "value": str(value)}]}]}


def _category_table(title: str, rows: list[tuple[str, Any]]) -> dict[str, Any]:
    """An NIH/OMB-template table: one untitled class, labels on categories."""
    return {"title": title, "paramType": "COUNT_OF_PARTICIPANTS", "unitOfMeasure": "Participants",
            "classes": [{"categories": [{"title": label, "measurements": [{"groupId": "BG000", "value": str(v)}]}
                                        for label, v in rows]}]}


def _class_table(title: str, rows: list[tuple[str, Any]]) -> dict[str, Any]:
    return {"title": title, "paramType": "NUMBER", "unitOfMeasure": "participants",
            "classes": [_class_row(label, v) for label, v in rows]}


def _study(nct_id: str, measures: list[dict[str, Any]], participants: Optional[int]) -> dict[str, Any]:
    """A single-group fictional study; participants fills the baseline denoms."""
    baseline: dict[str, Any] = {"groups": [{"groupId": "BG000", "title": "All Participants"}], "measures": measures}
    if participants is not None:
        baseline["denoms"] = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": str(participants)}]}]
    return {"protocolSection": {"identificationModule": {"nctId": nct_id},
                                "designModule": {"enrollmentInfo": {"count": participants}}},
            "resultsSection": {"baselineCharacteristicsModule": baseline}}


def _nonzero(race: dict[str, Any]) -> dict[str, int]:
    return {k: v for k, v in race["omb_totals"].items() if v}


SEX_TABLE = _category_table("Sex: Female, Male", [("Female", 20), ("Male", 20)])


# ── the label split itself ─────────────────────────────────────────────────

@pytest.mark.parametrize("label, expected", [
    ("White : Female", ("White", "female")),
    ("Black or African American : Male", ("Black or African American", "male")),
    ("White/Hispanic or Latino : Female", ("White/Hispanic or Latino", "female")),
    ("Caucasian (Female)", ("Caucasian", "female")),
    ("Asian (Females)", ("Asian", "female")),
    ("African American- Male", ("African American", "male")),
    ("Asian or Pacific Islander-Male", ("Asian or Pacific Islander", "male")),
    ("White, not Hispanic/Latino- Female", ("White, not Hispanic/Latino", "female")),
    ("Native American – Males", ("Native American", "male")),
    ("African American Male Veterans", ("African American", "male")),
    ("White male", ("White", "male")),
    ("Female: Black", ("Black", "female")),
    ("Male (Asian)", ("Asian", "male")),
    ("White Female, %", ("White", "female")),  # measurement suffix cleaned first
])
def test_split_finds_race_part_and_sex(label: str, expected: tuple[str, str]) -> None:
    assert split_race_by_sex_label(label) == expected


@pytest.mark.parametrize("label", [
    "White",                         # no sex word
    "Female",                        # nothing besides the sex word
    "Male :",
    "Condoms (female and male)",     # two sex words
    "Female and male",
    "Man Chinese",                   # gender word / Manchu: not a sex word
    "Black women",
    "Malaysian",                     # "male" only as a whole word
    "Femaleness",
])
def test_split_declines(label: str) -> None:
    assert split_race_by_sex_label(label) is None


# ── each posted pattern ────────────────────────────────────────────────────

def test_colon_pattern_maps_both_rows_of_a_pair_alike() -> None:
    """'X : Female' / 'X : Male': both rows of a pair land on the race; before
    the fix the Male row matched fuzzily and the Female row fell to other."""
    study = _study("NCT99000001", [
        SEX_TABLE,
        _class_table("Race/Ethnicity, Customized", [
            ("White : Female", 7), ("White : Male", 9),
            ("Black or African American : Female", 2), ("Black or African American : Male", 3),
            ("White/Hispanic or Latino : Female", 1), ("White/Hispanic or Latino : Male", 4),
            ("Asian : Female", 0), ("Asian : Male", 1),
        ]),
    ], participants=27)
    race = extract_race_data(study)
    assert race["reported"] is True
    assert _nonzero(race) == {"white": 21, "black_african_american": 5, "asian": 1}
    assert race["unknown_inferred"] == 0
    assert "race_by_sex_table" in race["flags"]
    assert "unmapped" not in race["flags"]
    assert not any(f.startswith("fuzzy_match") for f in race["flags"])
    joint = race["joint_sex_counts"]
    assert joint["source_table"] == "Race/Ethnicity, Customized"
    assert joint["used_for_totals"] is True
    assert joint["counts"] == {"white": {"female": 8, "male": 13},
                               "black_african_american": {"female": 2, "male": 3},
                               "asian": {"female": 0, "male": 1}}
    # the race totals are the sum over sex
    for omb, by_sex in joint["counts"].items():
        assert sum(by_sex.values()) == race["omb_totals"][omb]
    # every row keeps its posted label, race part and sex
    labels = {(r["source_label"], r["race_part"], r["sex"]) for r in joint["rows"]}
    assert ("White/Hispanic or Latino : Female", "White/Hispanic or Latino", "female") in labels
    rc = {c["original"]: c for c in race["raw_categories"]}
    assert rc["White : Female"]["omb_category"] == "white"
    assert rc["White : Female"]["sex"] == "female"
    assert rc["White/Hispanic or Latino : Male"]["race_part"] == "White/Hispanic or Latino"


def test_bracket_pattern_with_one_sex_posted_infers_nothing_for_the_other() -> None:
    """'X (Female)': only female rows were posted. Race totals take them; the
    rest of the denominator is unknown_inferred as for any table, and the
    joint counts carry no 'male' key."""
    study = _study("NCT99000002", [
        _class_table("Sex/Gender, Customized", [("Female", 30), ("Male", 30)]),
        _class_table("Race/Ethnicity, Customized", [
            ("Caucasian (Female)", 24), ("Asian (Female)", 4), ("Black (Female)", 2),
        ]),
    ], participants=60)
    race = extract_race_data(study)
    assert race["omb_totals"]["white"] == 24
    assert race["omb_totals"]["asian"] == 4
    assert race["omb_totals"]["black_african_american"] == 2
    assert race["omb_totals"]["other"] == 0
    assert race["unknown_inferred"] == 30
    assert race["joint_sex_counts"]["counts"] == {"white": {"female": 24}, "asian": {"female": 4},
                                                  "black_african_american": {"female": 2}}
    assert all("male" not in by_sex for by_sex in race["joint_sex_counts"]["counts"].values())


def test_hyphen_pattern_routes_each_race_part_as_a_plain_row() -> None:
    """'X- Male': an ethnicity-only race part is left to the ethnicity
    pipeline, 'Other' stays other, and a part the plain vocabulary does not
    map ('Asian or Pacific Islander') lands where a plain row would."""
    study = _study("NCT99000003", [
        _class_table("Race/Ethnicity, Customized", [
            ("White, not Hispanic/Latino-Male", 10), ("Hispanic/Latino- Male", 5),
            ("African American- Male", 3), ("Asian or Pacific Islander-Male", 2),
            ("Native American- Male", 1), ("Other- Male", 1),
            ("White, not Hispanic/Latino- Female", 12), ("Hispanic/Latino- Female", 6),
            ("African American- Female", 4), ("Asian or Pacific Islander-Female", 3),
            ("Native American- Female", 0), ("Other- Female", 2),
        ]),
    ], participants=49)
    race = extract_race_data(study)
    assert race["omb_totals"]["white"] == 22
    assert race["omb_totals"]["black_african_american"] == 7
    assert race["omb_totals"]["american_indian_alaska_native"] == 1
    # The plain vocabulary has no entry for "Asian or Pacific Islander" (it is
    # two OMB categories), so a plain row of that text is unmapped "other";
    # the crossed rows follow it, with the explicit "Other" rows.
    assert rx._map_race_label("Asian or Pacific Islander")["omb_category"] == "other"
    assert race["omb_totals"]["other"] == 8
    assert race["joint_sex_counts"]["counts"]["other"] == {"male": 3, "female": 5}
    assert race["joint_sex_counts"]["counts"]["black_african_american"] == {"male": 3, "female": 4}
    # Hispanic/Latino rows are not race rows: 11 participants are the gap
    assert race["unknown_inferred"] == 11
    assert not any(r["race_part"] == "Hispanic/Latino" for r in race["joint_sex_counts"]["rows"])
    assert rx._is_ethnicity_only_label("Hispanic/Latino")


def test_race_and_sex_phrase() -> None:
    """'African American Male Veterans': race part before the sex word, the
    descriptor after it kept only in the source label."""
    study = _study("NCT99000004", [
        _class_table("Sex/Gender, Customized", [("Male", 40)]),
        _class_table("Race/Ethnicity, Customized", [("African American Male Veterans", 40)]),
    ], participants=40)
    race = extract_race_data(study)
    assert _nonzero(race) == {"black_african_american": 40}
    assert race["subcategory_totals"] == {"black_african_american_african_american": 40}
    row = race["joint_sex_counts"]["rows"][0]
    assert row == {"source_label": "African American Male Veterans", "race_part": "African American",
                   "omb_category": "black_african_american", "subcategory": "african_american",
                   "sex": "male", "count": 40}


@pytest.mark.parametrize("posted, part", [
    ("White : Female", "White"),
    ("Caucasian (Female)", "Caucasian"),
    ("Black or African American : Male", "Black or African American"),
    ("White/Hispanic or Latino : Male", "White/Hispanic or Latino"),
    ("African American- Male", "African American"),
    ("Asian or Pacific Islander-Male", "Asian or Pacific Islander"),
    ("Native American- Female", "Native American"),
    ("White, not Hispanic/Latino-Male", "White, not Hispanic/Latino"),
    ("Other- Female", "Other"),
    ("African American Male Veterans", "African American"),
])
def test_race_part_maps_exactly_as_a_plain_row(posted: str, part: str) -> None:
    """The acceptance rule: a crossed row's race lands where a plain row with
    the race part as its label lands (category, subcategory, confidence)."""
    crossed = extract_race_data(_study("NCT99000005", [
        _class_table("Race/Ethnicity, Customized", [(posted, 5), ("Unknown : Male", 1)])], participants=6))
    plain = extract_race_data(_study("NCT99000006", [
        _class_table("Race/Ethnicity, Customized", [(part, 5), ("Unknown", 1)])], participants=6))
    got = next(c for c in crossed["raw_categories"] if c["original"] == posted)
    want = next(c for c in plain["raw_categories"] if c["count"] == 5)
    for key in ("omb_category", "subcategory", "confidence"):
        assert got[key] == want[key], key
    assert [f for f in got["flags"] if f != "race_by_sex"] == want["flags"]
    assert crossed["omb_totals"] == plain["omb_totals"]


# ── race labels inside a sex-titled table ──────────────────────────────────

def test_race_rows_in_a_sex_titled_table_count_when_no_race_table_exists() -> None:
    study = _study("NCT99000007", [
        _class_table("Sex/Gender, Customized", [
            ("African American/African Heritage", 6), ("East Asian/South East Asian", 2), ("White", 52),
        ]),
        _category_table("Sex: Female, Male", [("Female", 25), ("Male", 35)]),
    ], participants=60)
    race = extract_race_data(study)
    assert race["reported"] is True
    assert _nonzero(race) == {"black_african_american": 6, "asian": 2, "white": 52}
    assert "race_from_sex_titled_table" in race["flags"]
    assert race["joint_sex_counts"] is None


def test_race_by_sex_rows_in_a_sex_titled_table() -> None:
    study = _study("NCT99000008", [
        _class_table("Sex/Gender, Customized", [("White male", 3), ("White female", 2)]),
    ], participants=5)
    race = extract_race_data(study)
    assert _nonzero(race) == {"white": 5}
    assert race["joint_sex_counts"]["counts"] == {"white": {"male": 3, "female": 2}}
    assert race["joint_sex_counts"]["source_table"] == "Sex/Gender, Customized"
    assert {"race_by_sex_table", "race_from_sex_titled_table"} <= set(race["flags"])


def test_race_table_wins_over_race_rows_in_a_sex_titled_table() -> None:
    """The same participants in both: counted once, from the race table."""
    study = _study("NCT99000009", [
        _class_table("Sex/Gender, Customized", [("White", 44)]),
        _category_table("Race (NIH/OMB)", [("American Indian or Alaska Native", 0), ("Asian", 0),
                                           ("Black or African American", 0), ("White", 44),
                                           ("Unknown or Not Reported", 0)]),
    ], participants=44)
    race = extract_race_data(study)
    assert race["omb_totals"]["white"] == 44
    assert "race_table_superseded" in race["flags"]
    assert "race_from_sex_titled_table" not in race["flags"]
    assert len([c for c in race["raw_categories"] if c["omb_category"] == "white"]) == 1


@pytest.mark.parametrize("rows", [
    [("Female", 10), ("Male", 12)],
    [("Female", 10), ("Male", 12), ("Unknown", 1)],
    [("White", 10), ("Unknown", 2)],          # Unknown in a sex table is not race
    [("White", 10), ("Female", 2)],
    [("Women", 10), ("Men", 12)],
])
def test_sex_tables_without_race_only_rows_are_not_read_as_race(rows: list[tuple[str, int]]) -> None:
    race = extract_race_data(_study("NCT99000010", [_class_table("Sex/Gender, Customized", rows)], participants=22))
    assert race["reported"] is False
    assert race["flags"] == []
    assert race["joint_sex_counts"] is None


# ── shapes that must stay on the plain path ────────────────────────────────

@pytest.mark.parametrize("title, rows", [
    ("Race/Ethnicity, Customized", [("Han Chinese", 50), ("Hui Chinese", 2), ("Man Chinese", 1)]),
    ("Race/Ethnicity, Customized", [("Han", 50), ("Man", 3), ("Hui", 2)]),
    ("Contraception History", [("Oral contraceptive pill", 4), ("Condoms (female and male)", 2), ("Unknown", 1)]),
    ("Female method of contraception", [("Male Partner Sterilization", 1), ("Abstinence", 3)]),
    ("Race/Ethnicity, Customized", [("Female participants", 20), ("Male participants", 20)]),
    ("Race/Ethnicity, Customized", [("Non-Hispanic/Latino Black Female", 3), ("Hispanic/Latino Black Male", 2)]),
])
def test_tables_that_are_not_race_by_sex_keep_the_plain_mapping(title: str, rows: list[tuple[str, int]]) -> None:
    """Whatever the plain path made of these before, it still makes: compared
    with the extraction the module gives when the race-by-sex reader is off."""
    study = _study("NCT99000011", [_class_table(title, rows), SEX_TABLE], participants=sum(v for _, v in rows))
    race = extract_race_data(study)
    assert race["joint_sex_counts"] is None
    assert "race_by_sex_table" not in race["flags"]
    expected = _plain_only(study)
    for key in ("reported", "omb_totals", "raw_categories", "quarantined_labels", "flags"):
        assert race[key] == expected[key], key


def _plain_only(study: dict[str, Any]) -> dict[str, Any]:
    """extract_race_data with the race-by-sex split disabled: the plain path."""
    original = rx.split_race_by_sex_label
    rx.split_race_by_sex_label = lambda label: None
    try:
        return extract_race_data(copy.deepcopy(study))
    finally:
        rx.split_race_by_sex_label = original


def test_sex_split_tables_with_other_titles_are_not_race() -> None:
    study = _study("NCT99000012", [
        _class_table("Waist circumference", [("<85 cm (Male)", 5), ("Unknown (Male)", 1), ("<90 cm (Female)", 4)]),
        SEX_TABLE,
    ], participants=10)
    race = extract_race_data(study)
    assert race["reported"] is False
    assert race["joint_sex_counts"] is None


def test_joint_counts_are_never_inferred_from_separate_race_and_sex_tables() -> None:
    study = _study("NCT99000013", [
        SEX_TABLE,
        _category_table("Race (NIH/OMB)", [("White", 30), ("Black or African American", 10)]),
    ], participants=40)
    race = extract_race_data(study)
    assert _nonzero(race) == {"white": 30, "black_african_american": 10}
    assert race["joint_sex_counts"] is None
    assert "joint_sex_counts" in race  # present, null: absence stated, not omitted


# ── which table wins ───────────────────────────────────────────────────────

CROSSED = _class_table("Race/Ethnicity, Customized", [
    ("White : Female", 12), ("White : Male", 14), ("Black : Female", 6), ("Black : Male", 8),
])
PLAIN = _category_table("Race (NIH/OMB)", [("White", 26), ("Black or African American", 14), ("Unknown or Not Reported", 2)])


@pytest.mark.parametrize("order", ["crossed_first", "plain_first"])
def test_a_race_table_and_a_race_by_sex_table_are_never_added(order: str) -> None:
    """Same participants twice: one table is counted. Equal race-specific
    sums (40 and 40) are a tie, so the first in source order wins."""
    measures = [CROSSED, PLAIN] if order == "crossed_first" else [PLAIN, CROSSED]
    race = extract_race_data(_study("NCT99000014", measures + [SEX_TABLE], participants=42))
    assert sum(race["omb_totals"].values()) == 42  # one table's 42, not 40 + 42
    assert race["omb_totals"]["other"] == 0
    assert race["omb_totals"]["white"] == 26
    assert race["omb_totals"]["black_african_american"] == 14
    assert "race_table_superseded" in race["flags"]
    assert "multiple_race_tables_2" in race["flags"]
    joint = race["joint_sex_counts"]
    assert joint["counts"] == {"white": {"female": 12, "male": 14}, "black_african_american": {"female": 6, "male": 8}}
    assert joint["used_for_totals"] is (order == "crossed_first")
    tables = {c.get("sex") is not None for c in race["raw_categories"]}
    assert tables == {order == "crossed_first"}


def test_the_fuller_table_wins_whatever_its_position() -> None:
    partial = _class_table("Race/Ethnicity, Customized", [("White : Female", 12), ("White : Male", 14)])
    race = extract_race_data(_study("NCT99000015", [partial, PLAIN], participants=42))
    assert race["omb_totals"]["white"] == 26
    assert race["omb_totals"]["black_african_american"] == 14  # PLAIN won: 40 > 26
    assert race["joint_sex_counts"]["used_for_totals"] is False
    assert race["joint_sex_counts"]["counts"] == {"white": {"female": 12, "male": 14}}


def test_plain_race_tables_are_still_summed() -> None:
    """Unchanged behaviour outside race-by-sex tables (flagged, not fixed here)."""
    a = _category_table("Race (NIH/OMB)", [("White", 10)])
    b = _class_table("Race/Ethnicity, Customized", [("Asian", 3)])
    race = extract_race_data(_study("NCT99000016", [a, b], participants=13))
    assert _nonzero(race) == {"white": 10, "asian": 3}
    assert "multiple_race_tables_2" in race["flags"]
    assert "race_table_superseded" not in race["flags"]


def test_all_zero_race_by_sex_table_is_rejected_whole() -> None:
    study = _study("NCT99000017", [
        _class_table("Race/Ethnicity, Customized", [("White : Female", 0), ("White : Male", 0)]),
    ], participants=None)
    race = extract_race_data(study)
    assert race["reported"] is False
    assert race["joint_sex_counts"] is None
    assert race["flags"] == ["all_zero_rejection"]


def test_output_is_deterministic_and_flags_sorted() -> None:
    study = _study("NCT99000018", [CROSSED, PLAIN, SEX_TABLE], participants=42)
    first, second = extract_race_data(copy.deepcopy(study)), extract_race_data(copy.deepcopy(study))
    assert first == second
    assert first["flags"] == sorted(first["flags"])
    assert set(first["omb_totals"]) == set(RACE_KEYS)
