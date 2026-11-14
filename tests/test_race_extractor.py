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
parser: race-titled tables first, then one fullest table, the first on ties;
tables whose own Number Analyzed shows they cover different people are
summed.

A purely ethnicity row inside a counted race table ("Hispanic" beside
"Caucasian") is never counted; the block records it in
skipped_ethnicity_rows and flags ethnicity_rows_skipped. A label that names a
race beside the ethnicity term ("Non-Hispanic White") is a race row of that
race (last two sections).

Which measures are race tables at all is the title word rule
(race_extractor.is_race_table): titles that only contain the letters "race"
inside another word ("Contraception History", "GRACE score", "Intracerebral
hemorrhage") are not race tables, for the race block or for raceBreakdown.
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
from src.race_extractor import extract_race_data, is_race_table, split_race_by_sex_label  # noqa: E402
from src.utils import extract_demographic_breakdown  # noqa: E402

RACE_KEYS = ("american_indian_alaska_native", "asian", "black_african_american",
             "native_hawaiian_pacific_islander", "white", "more_than_one_race",
             "unknown_not_reported", "other")


# ── fictional study builder ────────────────────────────────────────────────

def _class_row(label: str, value: Any) -> dict[str, Any]:
    """A customized-table row: the label is the class title."""
    return {"title": label, "categories": [{"measurements": [{"groupId": "BG000", "value": str(value)}]}]}


def _denoms(n: int) -> list[dict[str, Any]]:
    return [{"units": "Participants", "counts": [{"groupId": "BG000", "value": str(n)}]}]


def _category_table(title: str, rows: list[tuple[str, Any]], analyzed: Optional[int] = None,
                    analyzed_on: str = "class") -> dict[str, Any]:
    """An NIH/OMB-template table: one untitled class, labels on categories.
    analyzed posts the table's own Number Analyzed, on its class (as the
    registry returns most) or, with analyzed_on="measure", on the measure."""
    cls: dict[str, Any] = {"categories": [{"title": label, "measurements": [{"groupId": "BG000", "value": str(v)}]}
                                          for label, v in rows]}
    table: dict[str, Any] = {"title": title, "paramType": "COUNT_OF_PARTICIPANTS",
                             "unitOfMeasure": "Participants", "classes": [cls]}
    if analyzed is not None:
        if analyzed_on == "measure":
            table["denoms"] = _denoms(analyzed)
        else:
            cls["denoms"] = _denoms(analyzed)
    return table


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
    # Hispanic/Latino rows are not race rows: their 11 participants are the
    # whole gap, filed as the ethnicity rows' part of unknown
    assert (race["unknown_ethnicity_only"], race["unknown_inferred"]) == (11, 0)
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
    # Contraception rows under a race title (the registry posted them under
    # "Contraception History" and "Female method of contraception", which
    # is_race_table no longer admits; see the title-rule tests below).
    ("Race/Ethnicity, Customized", [("Oral contraceptive pill", 4), ("Condoms (female and male)", 2), ("Unknown", 1)]),
    ("Race (NIH/OMB)", [("Male Partner Sterilization", 1), ("Abstinence", 3)]),
    ("Race/Ethnicity, Customized", [("Female participants", 20), ("Male participants", 20)]),
    # An ethnicity phrase beside text that is not a race label stays whole
    # (with "Black" in that place the rows are race by sex: see the tests of
    # labels that name a race beside an ethnicity term).
    ("Race/Ethnicity, Customized", [("Non-Hispanic/Latino Veteran Female", 3), ("Hispanic/Latino Veteran Male", 2)]),
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
    ("Unknown : Female", 1), ("Unknown : Male", 1),
])
PLAIN = _category_table("Race (NIH/OMB)", [("White", 26), ("Black or African American", 14), ("Unknown or Not Reported", 2)])


@pytest.mark.parametrize("order", ["crossed_first", "plain_first"])
def test_a_race_table_and_a_race_by_sex_table_are_never_added(order: str) -> None:
    """Same participants twice: one table is counted. Equal race-specific
    sums (40 and 40) and equal counted sums (42 and 42) are a tie, so the
    first in source order wins."""
    measures = [CROSSED, PLAIN] if order == "crossed_first" else [PLAIN, CROSSED]
    race = extract_race_data(_study("NCT99000014", measures + [SEX_TABLE], participants=42))
    assert sum(race["omb_totals"].values()) == 42  # one table's 42, not 40 + 42
    assert race["omb_totals"]["other"] == 0
    assert race["omb_totals"]["white"] == 26
    assert race["omb_totals"]["black_african_american"] == 14
    assert "race_table_superseded" in race["flags"]
    assert "multiple_race_tables_2" in race["flags"]
    joint = race["joint_sex_counts"]
    assert joint["counts"] == {"white": {"female": 12, "male": 14}, "black_african_american": {"female": 6, "male": 8},
                               "unknown_not_reported": {"female": 1, "male": 1}}
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


# ── several plain race tables ──────────────────────────────────────────────

RACE_42 = _category_table("Race (NIH/OMB)", [("American Indian or Alaska Native", 0), ("Asian", 3),
                                             ("Black or African American", 9), ("White", 28),
                                             ("Unknown or Not Reported", 2)])


def test_an_ethnicity_table_titled_race_adds_nothing_to_race() -> None:
    """The common case: the second "Race/Ethnicity, Customized" table is an
    ethnicity table. Its Hispanic rows were already left out; its Unknown row
    is no longer added to race unknown."""
    ethnicity = _class_table("Race/Ethnicity, Customized", [
        ("Hispanic or Latino", 5), ("Not Hispanic or Latino", 34), ("Unknown", 3)])
    race = extract_race_data(_study("NCT99000019", [RACE_42, ethnicity, SEX_TABLE], participants=42))
    assert _nonzero(race) == {"asian": 3, "black_african_american": 9, "white": 28, "unknown_not_reported": 2}
    assert sum(race["omb_totals"].values()) == 42
    assert {"race_table_superseded", "multiple_race_tables_2"} <= set(race["flags"])
    assert [c["original"] for c in race["raw_categories"]].count("Unknown") == 0


def test_one_part_table_beside_an_ethnicity_table_is_not_a_partition() -> None:
    """Only one table has race counts: it is counted alone, whatever its own
    Number Analyzed, and nothing is flagged as summed parts."""
    part = _category_table("Race (NIH/OMB)", [("White", 20), ("Asian", 5)], analyzed=25)
    ethnicity = _class_table("Race/Ethnicity, Customized", [("Not Hispanic or Latino", 45), ("Unknown", 5)])
    race = extract_race_data(_study("NCT99000028", [part, ethnicity], participants=50))
    assert _nonzero(race) == {"white": 20, "asian": 5, "unknown_not_reported": 25}
    assert race["unknown_explicit"] == 0
    assert "race_tables_partitioned" not in race["flags"]
    assert "race_table_superseded" in race["flags"]


def test_the_same_people_posted_twice_are_counted_once() -> None:
    repost = _class_table("Race/Ethnicity, Customized", [
        ("White", 28), ("African American", 9), ("Asian", 3), ("Unknown/not reported", 2)])
    for measures in ([RACE_42, repost], [repost, RACE_42]):
        race = extract_race_data(_study("NCT99000020", measures, participants=42))
        assert sum(race["omb_totals"].values()) == 42
        assert race["omb_totals"]["white"] == 28
        assert "race_tables_partitioned" not in race["flags"]


@pytest.mark.parametrize("detail_first", [False, True])
def test_a_repost_with_more_detail_keeps_its_subcategories(detail_first: bool) -> None:
    """The same 25 people as "Asian" and as "Chinese": counted once, and the
    choice keeps the subcategory whichever table comes first."""
    whole = _class_table("Race/Ethnicity, Customized", [("Asian", 25)])
    detail = _class_table("Race/Ethnicity, Customized", [("Chinese", 25)])
    measures = [detail, whole] if detail_first else [whole, detail]
    race = extract_race_data(_study("NCT99000029", measures, participants=25))
    assert _nonzero(race) == {"asian": 25}
    assert race["subcategory_totals"] == {"asian_chinese": 25}


def test_a_tie_with_different_categories_stays_with_the_first_table() -> None:
    """Equal sums but "Unknown" 8 in one table and "Other" 8 in the other:
    not the same counts, so source order decides, not detail."""
    omb = _category_table("Race (NIH/OMB)", [("Black or African American", 9), ("White", 82),
                                             ("Unknown or Not Reported", 8)])
    custom = _class_table("Race/Ethnicity, Customized", [("White", 82), ("African American", 9), ("Other", 8)])
    race = extract_race_data(_study("NCT99000030", [omb, custom], participants=99))
    assert _nonzero(race) == {"black_african_american": 9, "white": 82, "unknown_not_reported": 8}
    assert race["subcategory_totals"] == {}


def test_a_subgroup_detail_table_does_not_add_to_the_whole() -> None:
    whole = _class_table("Race/Ethnicity, Customized", [("Asian", 25)])
    detail = _class_table("Race/Ethnicity, Customized", [
        ("Asian (Other Than Chinese or Japanese)", 15), ("Chinese", 3), ("Japanese", 7)])
    race = extract_race_data(_study("NCT99000021", [whole, detail], participants=25))
    assert _nonzero(race) == {"asian": 25}
    assert race["subcategory_totals"] == {}


@pytest.mark.parametrize("analyzed_on", ["class", "measure"])
def test_tables_for_disjoint_parts_are_summed(analyzed_on: str) -> None:
    """Each table posts its own Number Analyzed (caregivers 20, patients 30)
    and together they make the 50 baseline participants: summed."""
    caregivers = _category_table("Race (NIH/OMB)", [("Asian", 2), ("White", 16), ("Black or African American", 2)],
                                 analyzed=20, analyzed_on=analyzed_on)
    patients = _category_table("Race (NIH/OMB)", [("Asian", 3), ("White", 21), ("Black or African American", 5),
                                                  ("Unknown or Not Reported", 1)],
                               analyzed=30, analyzed_on=analyzed_on)
    race = extract_race_data(_study("NCT99000022", [caregivers, patients], participants=50))
    assert _nonzero(race) == {"asian": 5, "white": 37, "black_african_american": 7, "unknown_not_reported": 1}
    assert "race_tables_partitioned" in race["flags"]
    assert "race_table_superseded" not in race["flags"]
    assert race["unknown_inferred"] == 0
    assert "race_tables_partitioned_by_description" not in race["flags"]


def test_parts_told_apart_only_by_description_are_flagged() -> None:
    """The NCT03711370 pattern: identical race counts for mothers and for
    infants, each analyzed on half the trial. Summed as parts, and flagged
    race_tables_partitioned_by_description because only the free-text
    population description separates them."""
    rows = [("Asian", 2), ("White", 16), ("Black or African American", 2)]
    mothers = _category_table("Race (NIH/OMB)", rows, analyzed=20)
    mothers["populationDescription"] = "Data are for mothers only (n = 20)"
    infants = _category_table("Race (NIH/OMB)", rows, analyzed=20)
    infants["populationDescription"] = "Data are for infants only (n = 20)"
    race = extract_race_data(_study("NCT99000091", [mothers, infants], participants=40))
    assert _nonzero(race) == {"asian": 4, "white": 32, "black_african_american": 4}
    assert "race_tables_partitioned" in race["flags"]
    assert "race_tables_partitioned_by_description" in race["flags"]


@pytest.mark.parametrize("first_n, second_n, first_rows", [
    (30, 30, [("White", 25), ("Asian", 5)]),   # numbers analyzed exceed the 50 participants
    (20, 30, [("White", 25), ("Asian", 5)]),   # first table counts more than its own 20
])
def test_disjoint_parts_need_consistent_numbers_analyzed(first_n: int, second_n: int,
                                                         first_rows: list[tuple[str, int]]) -> None:
    first = _category_table("Race (NIH/OMB)", first_rows, analyzed=first_n)
    second = _category_table("Race (NIH/OMB)", [("White", 20), ("Asian", 4)], analyzed=second_n)
    race = extract_race_data(_study("NCT99000023", [first, second], participants=50))
    assert _nonzero(race) == {"white": 25, "asian": 5, "unknown_not_reported": 20}
    assert race["unknown_inferred"] == 20
    assert "race_tables_partitioned" not in race["flags"]
    assert "race_table_superseded" in race["flags"]


def test_the_same_subset_posted_twice_is_not_summed() -> None:
    """Two plain tables each analyzed on the same 20 of 50 participants: the
    numbers analyzed (40) do not make up the baseline total, so nothing shows
    they are different people. One is counted; 30 are unknown_inferred."""
    first = _category_table("Race (NIH/OMB)", [("White", 15), ("Asian", 5)], analyzed=20)
    again = _class_table("Race/Ethnicity, Customized", [("White", 15), ("Asian", 5)])
    again["classes"][0]["denoms"] = _denoms(20)
    race = extract_race_data(_study("NCT99000031", [first, again], participants=50))
    assert _nonzero(race) == {"white": 15, "asian": 5, "unknown_not_reported": 30}
    assert race["unknown_inferred"] == 30
    assert "race_tables_partitioned" not in race["flags"]


@pytest.mark.parametrize("participants", [50, 40])
def test_a_race_by_sex_table_is_never_summed_with_a_plain_table(participants: int) -> None:
    """The case a reviewer traced: a plain table (White 20) and a race-by-sex
    table (White F10/M10), each analyzed on the same 20 participants. Not
    summed, even when the two numbers analyzed happen to make up the total."""
    plain = _category_table("Race (NIH/OMB)", [("White", 20)], analyzed=20)
    crossed = _class_table("Race/Ethnicity, Customized", [("White : Female", 10), ("White : Male", 10)])
    for cls in crossed["classes"]:
        cls["denoms"] = _denoms(participants - 20 if participants == 40 else 20)
    race = extract_race_data(_study("NCT99000032", [plain, crossed], participants=participants))
    assert race["omb_totals"]["white"] == 20
    assert race["unknown_inferred"] == participants - 20
    assert "race_tables_partitioned" not in race["flags"]
    assert race["joint_sex_counts"]["counts"] == {"white": {"female": 10, "male": 10}}


def test_a_whole_population_table_beats_subset_tables() -> None:
    """Subset tables (own Number Analyzed below the total) re-count people the
    whole-population table already counts, even when they add up to it."""
    season_a = _category_table("Race (NIH/OMB) (Season 2, Group A)", [("White", 15), ("Asian", 5)], analyzed=20)
    season_b = _category_table("Race (NIH/OMB) (Season 2, Group B)", [("White", 25), ("Asian", 5)], analyzed=30)
    whole = _category_table("Race (NIH/OMB)", [("White", 40), ("Asian", 10)])
    race = extract_race_data(_study("NCT99000024", [season_a, season_b, whole], participants=50))
    assert _nonzero(race) == {"white": 40, "asian": 10}
    assert "race_tables_partitioned" not in race["flags"]


def test_parts_without_their_own_numbers_analyzed_are_not_summed() -> None:
    """A "Child Race" table beside "Race" with no Number Analyzed of its own
    cannot be told from a re-count: the fullest is counted and the rest of the
    baseline total is unknown_inferred, never a second count."""
    parents = _class_table("Race/Ethnicity, Customized", [("White", 20), ("Black", 6)])
    children = _class_table("Child Race and Ethnicity", [("White", 18), ("Black", 5)])
    race = extract_race_data(_study("NCT99000025", [parents, children], participants=49))
    assert _nonzero(race) == {"white": 20, "black_african_american": 6, "unknown_not_reported": 23}
    assert race["unknown_inferred"] == 23


def test_equal_race_sums_go_to_the_table_that_counts_more() -> None:
    """A stub with nothing in it never wins over a table with rows; between
    equal race-specific sums, the table that also counts unknowns wins."""
    stub = _category_table("Race and Ethnicity Not Collected", [("Number of participants", 0)])
    unknown_only = _class_table("Race/Ethnicity, Customized", [("Unknown", 20)])
    race = extract_race_data(_study("NCT99000026", [stub, unknown_only], participants=20))
    assert race["reported"] is True
    assert _nonzero(race) == {"unknown_not_reported": 20}
    short = _category_table("Race (NIH/OMB)", [("White", 18)])
    full = _category_table("Race (NIH/OMB)", [("White", 18), ("Unknown or Not Reported", 2)])
    for measures in ([short, full], [full, short]):
        race = extract_race_data(_study("NCT99000027", measures, participants=20))
        assert race["unknown_explicit"] == 2 and race["unknown_inferred"] == 0


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


# ── which titles are race tables ───────────────────────────────────────────
#
# Titles below are shapes posted in the registry; the studies around them are
# fictional. Before the word rule, every title containing the letters "race"
# or "racial" was a race table.

@pytest.mark.parametrize("title", [
    "Race (NIH/OMB)",
    "RACE (NIH/OMB)",
    "Race/Ethnicity, Customized",
    "Ethnicity/Race",
    "Racial group",
    "Child Race",
    "Race - Peru",
    "Race (NIH/OMB) (Influenza Season 2015-2016)",
    "RaceEthnicityOther",    # camelCase: a \b regex finds no word break after "Race"
    "Race_Ethnicity",        # "_" is a regex word character, not a separator here
    "Races",
])
def test_titles_with_the_word_race_are_race_tables(title: str) -> None:
    assert is_race_table(title) is True


@pytest.mark.parametrize("title", [
    "Contraception History",
    "Female method of contraception",
    "Method of Contraception (Female)",
    "Oral Contraceptive Use History",
    "GRACE score",
    "Intracerebral hemorrhage (ICH) volume",
    "Total paracetamol ingested",
    "Extracellular water",
    "Out-of-brace Cobb angle at Baseline",
    "Paravalvular aortic regurgitation - none or trace",
    "Use of Levetiracetam as Baseline AED",
    "",
    None,
])
def test_titles_with_race_only_inside_another_word_are_not(title: Optional[str]) -> None:
    assert is_race_table(title) is False


CONTRACEPTION = _class_table("Contraception use at time of enrollment",
                             [("None", 33), ("Condoms", 62), ("Other method", 7)])


def test_a_contraception_table_alone_does_not_report_race() -> None:
    """Before the word rule this table was the trial's race table: "Other
    method" counted 7 as race "other" (the word "other" keeps it out of
    quarantine) and balancing filled the rest of the 102 with unknown."""
    study = _study("NCT99000033", [CONTRACEPTION, SEX_TABLE], participants=102)
    race = extract_race_data(study)
    assert race["reported"] is False
    assert race["flags"] == []
    assert race["raw_categories"] == [] and race["quarantined_labels"] == []
    measures = study["resultsSection"]["baselineCharacteristicsModule"]["measures"]
    assert extract_demographic_breakdown(measures, "race") is None


def test_a_contraception_table_is_not_a_second_race_table() -> None:
    study = _study("NCT99000034", [RACE_42, CONTRACEPTION], participants=42)
    race = extract_race_data(study)
    assert _nonzero(race) == {"asian": 3, "black_african_american": 9, "white": 28, "unknown_not_reported": 2}
    assert not any(f.startswith("multiple_race_tables_") for f in race["flags"])
    assert "race_table_superseded" not in race["flags"]
    assert race["quarantined_labels"] == []


def test_a_contraception_other_row_no_longer_outcounts_an_unknown_only_race_table() -> None:
    """Neither table has a race-specific count, so the fullest by counted sum
    won: the contraception table's "Other" 30 beat the race table's Unknown
    20 and became race "other". Now the race table is the only one."""
    unknown_only = _category_table("Race (NIH/OMB)", [("Unknown or Not Reported", 20)])
    contraception = _class_table("Contraception History", [("Pill", 12), ("Other", 30), ("None", 8)])
    race = extract_race_data(_study("NCT99000035", [unknown_only, contraception], participants=50))
    assert _nonzero(race) == {"unknown_not_reported": 50}
    assert race["unknown_explicit"] == 20 and race["unknown_inferred"] == 30
    assert race["flags"] == ["denominator_balanced"]


def test_a_contraception_table_no_longer_supersedes_race_rows_in_a_sex_titled_table() -> None:
    """Rule 1 sets sex/gender-titled tables aside whenever a race-titled table
    exists; a contraception table was enough to do that."""
    sex_titled = _class_table("Sex/Gender, Customized", [("White", 30), ("Black or African American", 10)])
    contraception = _class_table("Contraception History", [("Other", 5), ("Pill", 35)])
    race = extract_race_data(_study("NCT99000036", [sex_titled, contraception], participants=40))
    assert _nonzero(race) == {"white": 30, "black_african_american": 10}
    assert "race_from_sex_titled_table" in race["flags"]
    assert "race_table_superseded" not in race["flags"]


def test_a_camel_case_race_title_is_read() -> None:
    """The race rows sit under "RaceEthnicityOther"; the other race-titled
    table holds ethnicity rows only. A \\b regex rule would leave this trial
    with the ethnicity table alone, and race not reported."""
    ethnicity = _class_table("Race/Ethnicity, Customized", [("Hispanic or Latino", 4), ("Not Hispanic or Latino", 36)])
    camel = _class_table("RaceEthnicityOther", [("White", 30), ("Black/African American", 8), ("Other", 2)])
    study = _study("NCT99000037", [ethnicity, camel], participants=40)
    race = extract_race_data(study)
    assert _nonzero(race) == {"white": 30, "black_african_american": 8, "other": 2}
    measures = study["resultsSection"]["baselineCharacteristicsModule"]["measures"]
    breakdown = extract_demographic_breakdown(measures, "race")
    assert {k: v["count"] for k, v in breakdown.items()} == {
        "Hispanic or Latino": 4, "Not Hispanic or Latino": 36,
        "White": 30, "Black/African American": 8, "Other": 2}


def test_race_breakdown_uses_the_race_title_rule() -> None:
    """raceBreakdown used its own substring test ("race" only), so it read
    contraception tables and missed "Racial ..." titles. It now uses
    is_race_table; ethnicity and sex keep their substring tests."""
    racial = _class_table("Racial Group", [("White", 18), ("Asian", 4)])
    contraception = _class_table("Contraception History", [("Pill", 12), ("Other", 3)])
    ethnicity = _category_table("Ethnicity (NIH/OMB)", [("Hispanic or Latino", 2), ("Not Hispanic or Latino", 20)])
    measures = [racial, contraception, ethnicity, SEX_TABLE]
    breakdown = extract_demographic_breakdown(measures, "race")
    assert {k: v["count"] for k, v in breakdown.items()} == {"White": 18, "Asian": 4}
    assert {k: v["count"] for k, v in extract_demographic_breakdown(measures, "ethnicity").items()} == {
        "Hispanic or Latino": 2, "Not Hispanic or Latino": 20}
    assert {k: v["count"] for k, v in extract_demographic_breakdown(measures, "sex").items()} == {
        "Female": 20, "Male": 20}


def test_percentage_parts_convert_against_their_own_numbers_analyzed() -> None:
    """As for ethnicity: 100% White of a 40-person part and 100% Asian of a
    60-person part, Number Analyzed on each class, are 40 and 60 summed as
    parts, not 100 each."""
    part_a = _category_table("Race (NIH/OMB)", [("White", 100), ("Asian", 0)], analyzed=40)
    part_b = _category_table("Race (NIH/OMB)", [("White", 0), ("Asian", 100)], analyzed=60)
    for table in (part_a, part_b):
        table["paramType"] = "NUMBER"
        table["unitOfMeasure"] = "Percentage of participants"
    race = extract_race_data(_study("NCT99000033", [part_a, part_b], participants=100))
    assert _nonzero(race) == {"white": 40, "asian": 60}
    assert "race_tables_partitioned" in race["flags"]



def _percent_classes(rows: list[tuple[str, str, int]]) -> dict[str, Any]:
    """A percentage table with labels on classes: (label, value %, class Number Analyzed)."""
    return {"title": "Race/Ethnicity, Customized", "paramType": "NUMBER", "unitOfMeasure": "Percentage of participants",
             "classes": [{"title": label, "denoms": _denoms(n),
                          "categories": [{"measurements": [{"groupId": "BG000", "value": value}]}]}
                         for label, value, n in rows]}


def test_class_numbers_that_differ_are_row_counts_not_denominators() -> None:
    """The registry pattern behind NCT02649218: each class "Number Analyzed"
    is that row's own count, and the percentages are of the whole baseline
    (23% of 100 = 23, 77% of 100 = 77). Converting each row against its own
    number would give 5 and 59; the baseline total is used instead."""
    table = _percent_classes([("White", "23", 23), ("Asian", "77", 77)])
    race = extract_race_data(_study("NCT99000034", [table], participants=100))
    assert _nonzero(race) == {"white": 23, "asian": 77}


def test_a_class_number_every_class_repeats_is_the_tables_denominator() -> None:
    """The pattern behind NCT00812214: every class repeats one Number
    Analyzed (80 of 100), and the percentages add to 100% of those 80."""
    table = _percent_classes([("White", "20", 80), ("Asian", "80", 80)])
    race = extract_race_data(_study("NCT99000034", [table], participants=100))
    assert _nonzero(race) == {"white": 16, "asian": 64, "unknown_not_reported": 20}


# ── ethnicity rows inside a race table ─────────────────────────────────────
#
# A purely ethnicity row ("Hispanic", "Not Hispanic or Latino") in a race
# table is never counted as race. It is recorded in skipped_ethnicity_rows,
# and the block is flagged ethnicity_rows_skipped when a recorded row has
# participants (src/race_extractor.py, "Ethnicity rows inside a race table").
# Recording changes no count: every total below is what the extractor gave
# before the field existed.

COMBINED_TITLE = "Race/Ethnicity, Customized"
NEW_FLAG = "ethnicity_rows_skipped"
SPLIT_FLAG = "shortfall_is_ethnicity_rows"


def _unknown_parts(race: dict[str, Any]) -> tuple[int, int, int]:
    """(explicit, ethnicity rows, inferred): the three parts of unknown."""
    return race["unknown_explicit"], race["unknown_ethnicity_only"], race["unknown_inferred"]


def _skipped(race: dict[str, Any]) -> list[tuple[str, int]]:
    return [(row["original"], row["count"]) for row in race["skipped_ethnicity_rows"]]


def test_combined_exclusive_table_records_its_hispanic_row() -> None:
    """The NCT00416078 shape: Caucasian / African-American / Hispanic /
    Unknown, each participant in one row. The race rows fall short of the
    population by the Hispanic row's count, which arrives as unknown_inferred;
    the recorded row is what tells it from race the table did not report."""
    table = _class_table(COMBINED_TITLE, [("Caucasian", 30), ("African-American", 12),
                                          ("Hispanic", 6), ("Unknown", 2)])
    race = extract_race_data(_study("NCT99000501", [table], participants=50))
    assert race["skipped_ethnicity_rows"] == [
        {"original": "Hispanic", "count": 6, "source_table": COMBINED_TITLE, "flags": []}]
    assert NEW_FLAG in race["flags"]
    # The totals are what they were before the row was recorded; the Hispanic
    # participants are the ethnicity rows' part of unknown (next section).
    assert _nonzero(race) == {"white": 30, "black_african_american": 12, "unknown_not_reported": 8}
    assert _unknown_parts(race) == (2, 6, 0)
    assert [c["original"] for c in race["raw_categories"]] == ["Caucasian", "African-American", "Unknown"]
    assert race["quarantined_labels"] == []


def test_ethnicity_rows_beside_race_rows_that_cover_everyone() -> None:
    """Ethnicity listed again beside a complete race breakdown: the rows are
    recorded and flagged the same way, and nothing is inferred as unknown.
    The reader tells this from the combined table by the counts."""
    table = _class_table(COMBINED_TITLE, [("White", 30), ("Black", 20),
                                          ("Hispanic or Latino", 8), ("Not Hispanic or Latino", 42)])
    race = extract_race_data(_study("NCT99000502", [table], participants=50))
    assert _skipped(race) == [("Hispanic or Latino", 8), ("Not Hispanic or Latino", 42)]
    assert {row["source_table"] for row in race["skipped_ethnicity_rows"]} == {COMBINED_TITLE}
    assert NEW_FLAG in race["flags"]
    assert _nonzero(race) == {"white": 30, "black_african_american": 20}
    assert (race["unknown_explicit"], race["unknown_inferred"]) == (0, 0)
    assert "denominator_balanced" not in race["flags"]


def test_all_ethnicity_table_under_a_race_title_keeps_its_rows_through_rejection() -> None:
    """Every row is ethnicity: no race count, so the block is rejected as
    before (reported False, zero totals). The rows and the flag stay, which
    says why a posted "race" table gave no race counts."""
    table = _class_table(COMBINED_TITLE, [("Hispanic or Latino", 8), ("Not Hispanic or Latino", 42)])
    race = extract_race_data(_study("NCT99000503", [table], participants=50))
    assert race["reported"] is False
    assert _nonzero(race) == {}
    assert race["raw_categories"] == [] and race["joint_sex_counts"] is None
    assert race["flags"] == ["all_zero_rejection", NEW_FLAG]
    assert _skipped(race) == [("Hispanic or Latino", 8), ("Not Hispanic or Latino", 42)]


def test_plain_race_table_has_an_empty_list_and_no_flag() -> None:
    race = extract_race_data(_study("NCT99000504", [RACE_42], participants=42))
    assert race["skipped_ethnicity_rows"] == []
    assert NEW_FLAG not in race["flags"]
    # A combined TITLE alone is not an ethnicity row: the title flag is set, the new one is not.
    titled = _class_table(COMBINED_TITLE, [("White", 28), ("Black", 12)])
    race = extract_race_data(_study("NCT99000505", [titled], participants=40))
    assert race["skipped_ethnicity_rows"] == []
    assert "combined_race_ethnicity" in race["flags"] and NEW_FLAG not in race["flags"]


def test_no_race_table_has_an_empty_list() -> None:
    race = extract_race_data(_study("NCT99000506", [SEX_TABLE], participants=40))
    assert race["reported"] is False
    assert race["skipped_ethnicity_rows"] == [] and race["flags"] == []


def test_ethnicity_rows_of_zero_are_recorded_without_the_flag() -> None:
    table = _class_table(COMBINED_TITLE, [("White", 28), ("Black", 12), ("Hispanic", 0)])
    race = extract_race_data(_study("NCT99000507", [table], participants=40))
    assert _skipped(race) == [("Hispanic", 0)]
    assert NEW_FLAG not in race["flags"]


def test_a_superseded_tables_ethnicity_rows_are_not_recorded() -> None:
    """Only counted tables are recorded, as raw_categories lists only their
    rows: here the ethnicity table titled with race is superseded by the
    race table, whose totals owe nothing to it."""
    ethnicity = _class_table(COMBINED_TITLE, [("Hispanic or Latino", 5), ("Not Hispanic or Latino", 34),
                                              ("Unknown", 3)])
    race = extract_race_data(_study("NCT99000508", [RACE_42, ethnicity, SEX_TABLE], participants=42))
    assert "race_table_superseded" in race["flags"]
    assert race["skipped_ethnicity_rows"] == []
    assert NEW_FLAG not in race["flags"]


def test_race_by_sex_ethnicity_rows_keep_their_race_part_and_sex() -> None:
    """'Hispanic/Latino- Male' in a race-by-sex table: the posted label, with
    the race part and sex as on a counted race-by-sex row."""
    table = _class_table(COMBINED_TITLE, [
        ("White, not Hispanic/Latino-Male", 10), ("Hispanic/Latino- Male", 5), ("African American- Male", 3),
        ("White, not Hispanic/Latino- Female", 12), ("Hispanic/Latino- Female", 6), ("African American- Female", 4),
    ])
    race = extract_race_data(_study("NCT99000509", [table], participants=40))
    assert "race_by_sex_table" in race["flags"] and NEW_FLAG in race["flags"]
    assert race["skipped_ethnicity_rows"] == [
        {"original": "Hispanic/Latino- Male", "count": 5, "source_table": COMBINED_TITLE,
         "race_part": "Hispanic/Latino", "sex": "male", "flags": ["race_by_sex"]},
        {"original": "Hispanic/Latino- Female", "count": 6, "source_table": COMBINED_TITLE,
         "race_part": "Hispanic/Latino", "sex": "female", "flags": ["race_by_sex"]},
    ]
    assert _unknown_parts(race) == (0, 11, 0)
    # The joint counts hold counted rows only, as before.
    assert not any(r["race_part"] == "Hispanic/Latino" for r in race["joint_sex_counts"]["rows"])


def test_a_percentage_ethnicity_row_is_marked_as_converted() -> None:
    """The count of a percentage row is an estimate (12% of 50), and says so."""
    table = {"title": COMBINED_TITLE, "paramType": "NUMBER", "unitOfMeasure": "Percentage of participants",
             "classes": [_class_row("White", "60"), _class_row("Black", "24"), _class_row("Hispanic", "12")]}
    race = extract_race_data(_study("NCT99000510", [table], participants=50))
    assert _nonzero(race) == {"white": 30, "black_african_american": 12, "unknown_not_reported": 8}
    assert race["skipped_ethnicity_rows"] == [
        {"original": "Hispanic", "count": 6, "source_table": COMBINED_TITLE, "flags": ["pct_to_count"]}]


def test_summed_parts_record_each_tables_rows_under_its_title() -> None:
    """Disjoint parts (rule 3) are both counted, so both are recorded, in
    source order, each row under the title of the table it was posted in."""
    adults = _category_table("Race, Adults", [("White", 20), ("Hispanic", 10)], analyzed=30)
    children = _category_table("Race, Children", [("Asian", 15), ("Hispanic or Latino", 5)], analyzed=20)
    race = extract_race_data(_study("NCT99000511", [adults, children], participants=50))
    assert "race_tables_partitioned" in race["flags"]
    assert [(r["source_table"], r["original"], r["count"]) for r in race["skipped_ethnicity_rows"]] == [
        ("Race, Adults", "Hispanic", 10), ("Race, Children", "Hispanic or Latino", 5)]
    assert _nonzero(race) == {"white": 20, "asian": 15, "unknown_not_reported": 15}


def test_a_sex_titled_table_with_an_ethnicity_row_is_still_not_a_race_table() -> None:
    """The sex/gender-title gate is unchanged: one skipped row and the table
    is not read as race at all, so nothing is recorded."""
    table = _class_table("Sex/Gender, Customized", [("White", 30), ("Hispanic", 10)])
    assert not rx.is_sex_titled_race_table(table)
    race = extract_race_data(_study("NCT99000512", [table], participants=40))
    assert race["reported"] is False
    assert race["skipped_ethnicity_rows"] == []


def _without_ethnicity_rows(table: dict[str, Any]) -> dict[str, Any]:
    """The same class-labelled table with its ethnicity-only rows removed
    (whole label, or the race part of a race-by-sex label)."""
    def ethnicity_only(label: str) -> bool:
        split = split_race_by_sex_label(label)
        return rx._is_ethnicity_only_label(label) or (split is not None and rx._is_ethnicity_only_label(split[0]))
    return {**table, "classes": [cls for cls in table["classes"] if not ethnicity_only(cls["title"])]}


@pytest.mark.parametrize("rows, participants", [
    ([("Caucasian", 30), ("African-American", 12), ("Hispanic", 6), ("Unknown", 2)], 50),
    ([("White", 30), ("Black", 20), ("Hispanic or Latino", 8), ("Not Hispanic or Latino", 42)], 50),
    ([("White", 28), ("Black", 12), ("Hispanic", 0)], 40),
    ([("White, not Hispanic/Latino-Male", 10), ("Hispanic/Latino- Male", 5), ("African American- Male", 3),
      ("White, not Hispanic/Latino- Female", 12), ("Hispanic/Latino- Female", 6), ("African American- Female", 4)], 40),
    ([("White", 25), ("Latino", 9), ("Condom", 4), ("Other", 2)], 40),
    ([("White", 10), ("Hispanic", 3)], None),
])
def test_recorded_rows_enter_no_count(rows: list[tuple[str, int]], participants: Optional[int]) -> None:
    """The recorded rows are in none of the block's totals: taking them out
    of the posted table leaves the block the same, apart from the list, its
    flag, and which part of unknown a shortfall is filed under (the rows'
    counts decide that; the amount is the same)."""
    table = _class_table(COMBINED_TITLE, rows)
    with_rows = extract_race_data(_study("NCT99000513", [table], participants=participants))
    without = extract_race_data(_study("NCT99000513", [_without_ethnicity_rows(table)], participants=participants))
    assert with_rows["skipped_ethnicity_rows"] and without["skipped_ethnicity_rows"] == []
    with_rows["skipped_ethnicity_rows"] = []
    with_rows["flags"] = [f for f in with_rows["flags"] if f not in (NEW_FLAG, SPLIT_FLAG)]
    with_rows["unknown_inferred"] += with_rows["unknown_ethnicity_only"]
    with_rows["unknown_ethnicity_only"] = 0
    assert with_rows == without


# ── a race named beside an ethnicity term in one label ─────────────────────
#
# "Non-Hispanic White" names a race. One ethnicity phrase is taken out of the
# label and the row is a race row when what is left is exactly a known race
# label (src/race_extractor.py, "A race named beside an ethnicity term in one
# label"). Before this rule the ethnicity-only test accepted such labels and
# their participants were in no race category.

@pytest.mark.parametrize("label, expected", [
    ("Non-Hispanic White", ("White", "Non-Hispanic")),
    ("White Non-Hispanic", ("White", "Non-Hispanic")),
    ("White (non-Hispanic)", ("White", "non-Hispanic")),
    ("White (not of Hispanic origin)", ("White", "not of Hispanic origin")),
    ("Nonhispanic White", ("White", "Nonhispanic")),
    ("Non-Latino Black", ("Black", "Non-Latino")),
    ("Black or African American (not Hispanic)", ("Black or African American", "not Hispanic")),
    ("Not Hispanic or Latino - White", ("White", "Not Hispanic or Latino")),
    ("Non-Hispanic/Latino Black", ("Black", "Non-Hispanic/Latino")),
    ("American Indian/Alaska Native, non-Hispanic", ("American Indian/Alaska Native", "non-Hispanic")),
    ("Hispanic White", ("White", "Hispanic")),
    ("Black Hispanic", ("Black", "Hispanic")),
    ("Hispanic Other", ("Other", "Hispanic")),
    ("Non-Hispanic White only", ("White", "Non-Hispanic")),
    ("White, Non-Hispanic", ("White", "Non-Hispanic")),
    # Ethnicity alone.
    ("Hispanic", None),
    ("Hispanic or Latino", None),
    ("Not Hispanic or Latino", None),
    ("Non-Hispanic", None),
    ("Latinx", None),
    # Text that limits or joins groups, or is not a race label, is not a race.
    ("Non-Black Hispanic", None),
    ("Hispanic (not African American)", None),
    ("Hispanic or African-American", None),
    ("non-White or Hispanic", None),
    ("Black and Latino", None),
    ("Hispanic (Regardless of Race)", None),
    ("American Hispanic", None),
    ("Race Hispanic", None),
    # "Latin" inside "Latin American" is not an ethnicity phrase to take out.
    ("Native Latin American", None),
    # Exact, never fuzzy: a plural or a longer form is left alone.
    ("Non-Hispanic Whites", None),
    ("Non-Hispanic White or European American", None),
    # No ethnicity phrase at all.
    ("White", None),
])
def test_race_joined_to_ethnicity(label: str, expected: Optional[tuple[str, str]]) -> None:
    assert rx._race_joined_to_ethnicity(label) == expected
    if expected is not None:
        assert not rx._is_ethnicity_only_label(label)


@pytest.mark.parametrize("label, category", [
    ("Latine White", "white"),
    ("White Latine", "white"),
    ("Black (Latine)", "black_african_american"),
])
def test_an_ethnicity_form_the_keyword_list_lacks_still_reaches_the_joined_reader(label: str, category: str) -> None:
    """"Latine" is in the joined reader's vocabulary but not in
    _ETHNICITY_KEYWORDS, so the keyword test must not run first: the row is
    a race row of the race it names, not an unmapped "other"."""
    assert not any(kw in label.lower() for kw in rx._ETHNICITY_KEYWORDS)
    mapping = rx._map_plain_row(label, 7, False)
    assert mapping is not None
    assert (mapping["omb_category"], mapping["original"], mapping["count"]) == (category, label, 7)
    assert "split_from_combined" in mapping["flags"] and "unmapped" not in mapping["flags"]


def test_non_hispanic_race_rows_are_counted_in_their_race() -> None:
    """One exclusive classification written with joined labels: the race rows
    are counted, the Hispanic row is still not race and is still recorded.
    Before the rule every row was skipped and the block reported no race."""
    table = _class_table(COMBINED_TITLE, [("Non-Hispanic White", 30), ("Non-Hispanic Black", 12), ("Hispanic", 8)])
    race = extract_race_data(_study("NCT99000514", [table], participants=50))
    assert race["reported"] is True
    assert _nonzero(race) == {"white": 30, "black_african_american": 12, "unknown_not_reported": 8}
    assert _unknown_parts(race) == (0, 8, 0)
    assert _skipped(race) == [("Hispanic", 8)]
    # The posted label is kept, with the part that was mapped beside it.
    assert [(c["original"], c["race_part"], c["omb_category"], c["confidence"]) for c in race["raw_categories"]] == [
        ("Non-Hispanic White", "White", "white", "high"),
        ("Non-Hispanic Black", "Black", "black_african_american", "high")]
    assert all("split_from_combined" in c["flags"] for c in race["raw_categories"])
    assert {"split_from_combined", NEW_FLAG, "denominator_balanced"} <= set(race["flags"])


def test_hispanic_and_non_hispanic_rows_of_one_race_are_both_that_race() -> None:
    """The NCT03150173 shape: race crossed with ethnicity, every participant in
    one row. The ethnicity word does not change the race, and the two source
    labels stay apart in raw_categories."""
    table = _class_table(COMBINED_TITLE, [
        ("Hispanic Black", 16), ("Hispanic White", 9), ("Hispanic Other", 27),
        ("Non-Hispanic Black", 16), ("Non-Hispanic White", 24), ("Non-Hispanic Other", 5)])
    race = extract_race_data(_study("NCT99000515", [table], participants=97))
    assert _nonzero(race) == {"black_african_american": 32, "white": 33, "other": 32}
    assert race["unknown_inferred"] == 0 and race["skipped_ethnicity_rows"] == []
    assert [c["original"] for c in race["raw_categories"]] == [
        "Hispanic Black", "Hispanic White", "Hispanic Other",
        "Non-Hispanic Black", "Non-Hispanic White", "Non-Hispanic Other"]
    assert NEW_FLAG not in race["flags"] and "unmapped" not in race["flags"]


def test_plain_rows_beside_joined_rows_add_up_to_the_population() -> None:
    """The NCT00360555 shape: "White" beside "White Hispanic" holds the people
    the joined row does not, so counting both is counting each person once."""
    table = _class_table(COMBINED_TITLE, [("White", 1363), ("White Hispanic", 83), ("Black Hispanic", 2),
                                          ("Black", 110), ("Asian", 23), ("Asian Hispanic", 0)])
    race = extract_race_data(_study("NCT99000516", [table], participants=1581))
    assert _nonzero(race) == {"white": 1446, "black_african_american": 112, "asian": 23}
    assert sum(race["omb_totals"].values()) == 1581
    assert "denominator_balanced" not in race["flags"]


def test_labels_that_limit_or_join_groups_stay_ethnicity_rows() -> None:
    """The NCT01351805 shape: "Hispanic (not African American)" names no race,
    so it stays out of race and is recorded; the joined white row is counted."""
    table = _class_table(COMBINED_TITLE, [("Non-Hispanic white", 180), ("African American", 51),
                                          ("Hispanic (not African American)", 10), ("Non-Black Hispanic", 4),
                                          ("Hispanic (regardless of race)", 5)])
    race = extract_race_data(_study("NCT99000517", [table], participants=250))
    assert _nonzero(race) == {"white": 180, "black_african_american": 51, "unknown_not_reported": 19}
    assert _skipped(race) == [("Hispanic (not African American)", 10), ("Non-Black Hispanic", 4),
                              ("Hispanic (regardless of race)", 5)]


def test_a_separated_label_keeps_the_posted_label_too() -> None:
    """"White, Non-Hispanic" was counted as White before the rule, recorded
    under the fragment "White". Its count is the same; the record now shows
    the posted label, as for a label with no separator."""
    table = _class_table(COMBINED_TITLE, [("White, Non-Hispanic", 30), ("Black, Non-Hispanic", 12), ("Hispanic", 8)])
    race = extract_race_data(_study("NCT99000518", [table], participants=50))
    assert _nonzero(race) == {"white": 30, "black_african_american": 12, "unknown_not_reported": 8}
    assert [(c["original"], c["race_part"]) for c in race["raw_categories"]] == [
        ("White, Non-Hispanic", "White"), ("Black, Non-Hispanic", "Black")]
    # A label the exact match does not read keeps the fragment, as before.
    other = _class_table(COMBINED_TITLE, [("Caucasian/White, Hispanic or Latino origin group", 5), ("Asian", 5)])
    race = extract_race_data(_study("NCT99000519", [other], participants=10))
    assert "race_part" not in race["raw_categories"][0]


def test_joined_labels_crossed_with_sex_are_a_race_by_sex_table() -> None:
    """"Non-Hispanic/Latino Black Female": the sex word splits off, and the
    race part "Non-Hispanic/Latino Black" is Black. Before the rule both rows
    were skipped and the table gave no race."""
    table = _class_table(COMBINED_TITLE, [("Non-Hispanic/Latino Black Female", 3), ("Hispanic/Latino Black Male", 2)])
    race = extract_race_data(_study("NCT99000520", [table, SEX_TABLE], participants=5))
    assert _nonzero(race) == {"black_african_american": 5}
    assert "race_by_sex_table" in race["flags"]
    assert race["joint_sex_counts"]["counts"] == {"black_african_american": {"female": 3, "male": 2}}
    assert [(r["source_label"], r["race_part"]) for r in race["joint_sex_counts"]["rows"]] == [
        ("Non-Hispanic/Latino Black Female", "Non-Hispanic/Latino Black"),
        ("Hispanic/Latino Black Male", "Hispanic/Latino Black")]
    assert race["skipped_ethnicity_rows"] == []


# ── a shortfall that the ethnicity rows account for ────────────────────────
#
# unknown_not_reported = unknown_explicit + unknown_ethnicity_only +
# unknown_inferred. A shortfall is filed under the ethnicity rows only when
# the rows that state the ethnicity add up to exactly the shortfall and no
# row that negates it has participants (src/race_extractor.py, "A shortfall
# that the ethnicity rows account for"). The amount in unknown_not_reported
# is the same either way.

@pytest.mark.parametrize("label, negates", [
    ("Not Hispanic or Latino", True),
    ("Non-Hispanic", True),
    ("Nonhispanic", True),
    ("Not of Hispanic, Latino/a, or Spanish origin", True),
    ("Ethnicity: Not Hispanic or Latino", True),
    ("Hispanic", False),
    ("Hispanic or Latino", False),
    ("Latinx", False),
    ("Hispanic (regardless of race)", False),
    ("Hispanic (not African American)", False),
    ("Latino, non-Black", False),
    # A negation before the term reads as negating it, whatever it belongs
    # to: the shortfall then stays in unknown_inferred.
    ("Non-Black Hispanic", True),
    ("Prefer not to answer (Hispanic)", True),
])
def test_negates_ethnicity(label: str, negates: bool) -> None:
    assert rx._negates_ethnicity(label) is negates


def test_an_exact_shortfall_is_the_ethnicity_rows_part_of_unknown() -> None:
    """The NCT00416078 shape: 39 + 10 race, 2 in the table's Unknown row and 2
    Hispanic, of 53. The 2 Hispanic participants are the whole shortfall."""
    table = _class_table(COMBINED_TITLE, [("Caucasian", 39), ("African-American", 10), ("Hispanic", 2), ("Unknown", 2)])
    race = extract_race_data(_study("NCT99000521", [table], participants=53))
    assert _nonzero(race) == {"white": 39, "black_african_american": 10, "unknown_not_reported": 4}
    assert _unknown_parts(race) == (2, 2, 0)
    assert {SPLIT_FLAG, NEW_FLAG, "denominator_balanced"} <= set(race["flags"])


@pytest.mark.parametrize("rows, participants, why", [
    ([("White", 30), ("Black", 12), ("Hispanic", 6)], 50, "the shortfall (8) is larger than the Hispanic row"),
    ([("White", 30), ("Black", 12), ("Hispanic", 9)], 50, "the Hispanic row is larger than the shortfall (8)"),
    ([("White", 30), ("Black", 12), ("Hispanic", 8), ("Not Hispanic or Latino", 42)], 50,
     "a row that negates the ethnicity has participants"),
    ([("White", 30), ("Black", 12), ("Not Hispanic or Latino", 8)], 50, "only a negating row matches the shortfall"),
    ([("White", 30), ("Black", 12), ("Non-Black Hispanic", 8)], 50, "the label reads as negating"),
    ([("White", 30), ("Black", 12)], 50, "no ethnicity row at all"),
])
def test_a_shortfall_the_rows_do_not_account_for_exactly_stays_inferred(
        rows: list[tuple[str, int]], participants: int, why: str) -> None:
    race = extract_race_data(_study("NCT99000522", [_class_table(COMBINED_TITLE, rows)], participants=participants))
    assert _unknown_parts(race) == (0, 0, 8), why
    assert SPLIT_FLAG not in race["flags"] and "denominator_balanced" in race["flags"], why
    assert race["omb_totals"]["unknown_not_reported"] == 8


def test_a_negating_row_of_zero_does_not_block_the_split() -> None:
    table = _class_table(COMBINED_TITLE, [("White", 30), ("Black", 12), ("Hispanic", 8), ("Not Hispanic or Latino", 0)])
    race = extract_race_data(_study("NCT99000523", [table], participants=50))
    assert _unknown_parts(race) == (0, 8, 0)
    assert SPLIT_FLAG in race["flags"]


def test_no_shortfall_or_no_population_leaves_the_new_part_at_zero() -> None:
    covered = _class_table(COMBINED_TITLE, [("White", 30), ("Black", 20), ("Hispanic or Latino", 8)])
    race = extract_race_data(_study("NCT99000524", [covered], participants=50))
    assert _unknown_parts(race) == (0, 0, 0) and SPLIT_FLAG not in race["flags"]
    # No baseline population: nothing is balanced, so there is no shortfall to file.
    race = extract_race_data(_study("NCT99000525", [_class_table(COMBINED_TITLE, [("White", 30), ("Hispanic", 8)])],
                                    participants=None))
    assert _unknown_parts(race) == (0, 0, 0) and SPLIT_FLAG not in race["flags"]
    # No race table at all.
    race = extract_race_data(_study("NCT99000526", [SEX_TABLE], participants=40))
    assert _unknown_parts(race) == (0, 0, 0)


def test_several_hispanic_rows_and_summed_parts_are_added_up() -> None:
    """The stated rows are summed over every counted table: two rows in one
    table, and one row in each of two disjoint parts (rule 3)."""
    table = _class_table(COMBINED_TITLE, [("White", 30), ("Black", 12), ("Hispanic", 5), ("Latino", 3)])
    race = extract_race_data(_study("NCT99000527", [table], participants=50))
    assert _unknown_parts(race) == (0, 8, 0)
    adults = _category_table("Race, Adults", [("White", 20), ("Hispanic", 10)], analyzed=30)
    children = _category_table("Race, Children", [("Asian", 15), ("Hispanic or Latino", 5)], analyzed=20)
    race = extract_race_data(_study("NCT99000528", [adults, children], participants=50))
    assert "race_tables_partitioned" in race["flags"]
    assert _unknown_parts(race) == (0, 15, 0)


@pytest.mark.parametrize("rows, participants", [
    ([("Caucasian", 39), ("African-American", 10), ("Hispanic", 2), ("Unknown", 2)], 53),
    ([("White", 30), ("Black", 12), ("Hispanic", 6), ("Unknown", 1)], 50),
    ([("White", 30), ("Black", 12), ("Unknown", 3)], 50),
    ([("Non-Hispanic White", 30), ("Non-Hispanic Black", 12), ("Hispanic", 8)], 50),
    ([("White", 30), ("Black", 20), ("Hispanic", 8)], 50),
])
def test_the_three_parts_of_unknown_always_add_up(rows: list[tuple[str, int]], participants: int) -> None:
    race = extract_race_data(_study("NCT99000529", [_class_table(COMBINED_TITLE, rows)], participants=participants))
    assert sum(_unknown_parts(race)) == race["omb_totals"]["unknown_not_reported"]
    assert (race["unknown_ethnicity_only"] > 0) is (SPLIT_FLAG in race["flags"])
    assert not (race["unknown_ethnicity_only"] and race["unknown_inferred"])
