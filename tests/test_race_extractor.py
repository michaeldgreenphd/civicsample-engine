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

A race table that posts a study's groups as rows ("White - Usual Care", each
row's count in its own group's column only) is read by its race parts (its own
section). Native Hawaiian or Pacific Islander wordings, and the rows they
meet in a table, have a section of their own, as do European-only wordings
and Asian wordings with the tables they meet; Black-only wordings, the
1977 Asian or Pacific Islander category, European nationalities and Roma
groups, and UK census wordings are the last four sections.

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
    # "Asian or Pacific Islander" is the 1977 grouped category, read as Asian
    # (block comment "The 1977 Asian or Pacific Islander category"); the
    # crossed rows follow the plain reading, and "Other" stays other.
    assert rx._map_race_label("Asian or Pacific Islander")["omb_category"] == "asian"
    assert race["omb_totals"]["asian"] == 5
    assert race["omb_totals"]["other"] == 3
    assert race["joint_sex_counts"]["counts"]["other"] == {"male": 1, "female": 2}
    assert race["joint_sex_counts"]["counts"]["asian"] == {"male": 2, "female": 3}
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
    """The detail table names the same 25 Asian participants as the whole
    ("Asian (Other Than Chinese or Japanese)" is Asian by the Asian label
    rule), so it does not add to them. With the same count in every category
    it is the one kept, for its subcategory detail (table_selection.fullest);
    before the Asian label rule its first row was unmapped and the whole was
    kept."""
    whole = _class_table("Race/Ethnicity, Customized", [("Asian", 25)])
    detail = _class_table("Race/Ethnicity, Customized", [
        ("Asian (Other Than Chinese or Japanese)", 15), ("Chinese", 3), ("Japanese", 7)])
    race = extract_race_data(_study("NCT99000021", [whole, detail], participants=25))
    assert _nonzero(race) == {"asian": 25}
    assert race["subcategory_totals"] == {"asian_chinese": 3, "asian_japanese": 7}


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


@pytest.mark.parametrize("label, race_part, category, subcategory", [
    # The generic mapping would split at the slash and keep "Native Hawaiian".
    ("Native Hawaiian/Pacific Islander Hispanic", "Native Hawaiian/Pacific Islander",
     "native_hawaiian_pacific_islander", None),
    ("Caucasian/White, Hispanic", "Caucasian/White", "white", None),
    ("Hispanic/Black", "Black", "black_african_american", None),
    # The generic mapping would reach this one by fuzzy match, at medium confidence.
    ("Black or African American Latino", "Black or African American", "black_african_american", None),
])
def test_a_joined_label_is_mapped_from_its_exact_race_part_first(label: str, race_part: str, category: str,
                                                                subcategory: Optional[str]) -> None:
    mapping = rx._map_race_label(label)
    assert (mapping["omb_category"], mapping["subcategory"], mapping["confidence"]) == (category, subcategory, "high")
    assert (mapping["original"], mapping["race_part"]) == (label, race_part)
    assert mapping["flags"] == ["split_from_combined"]


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
    # Negated outside the prefix: a phrase before the term, or a bare answer after it.
    ("Other than Hispanic or Latino", True),
    ("Hispanic or Latino: No", True),
    ("Hispanic or Latino ethnicity - No", True),
    ("Hispanic/Latino (none)", True),
    ("Hispanic or Latino: Yes", False),
    ("Hispanic: No retinopathy", False),
    ("Hispanic, not otherwise specified", False),
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
    ([("White", 30), ("Black", 12), ("Hispanic or Latino: No", 8)], 50, "a bare negative answer"),
    ([("White", 30), ("Black", 12), ("Other than Hispanic or Latino", 8)], 50, "negated by a phrase"),
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



# ── the combined classification the ethnicity extractor reads ──────────────

def test_combined_classification_describes_an_exact_table() -> None:
    table = _class_table(COMBINED_TITLE, [("Caucasian", 39), ("African-American", 10), ("Other/Unknown", 3),
                                          ("Hispanic", 2), ("Unknown", 2)])
    study = _study("NCT99000530", [SEX_TABLE, table], participants=56)
    assert rx.combined_classification(study) == {
        "index": 1, "title": COMBINED_TITLE,
        "race_rows": 52,            # Caucasian, African-American and Other/Unknown (race "other")
        "race_rows_named": 49,      # without the row that also names an unknown
        "ethnicity_rows": 2, "unknown_rows": 2}


@pytest.mark.parametrize("measures, participants", [
    ([_class_table(COMBINED_TITLE, [("White", 30), ("Black", 12), ("Hispanic", 6)])], 50),   # not exact
    ([_class_table(COMBINED_TITLE, [("White", 30), ("Black", 20)])], 50),                    # no Hispanic row
    ([_category_table("Race, Adults", [("White", 20), ("Hispanic", 10)], analyzed=30),
      _category_table("Race, Children", [("Asian", 15), ("Hispanic or Latino", 5)], analyzed=20)], 50),  # parts
    ([SEX_TABLE], 40),                                                                        # no race table
])
def test_combined_classification_is_none_otherwise(measures: list[dict[str, Any]], participants: int) -> None:
    assert rx.combined_classification(_study("NCT99000531", measures, participants=participants)) is None


@pytest.mark.parametrize("label, unknown", [
    ("Other/Unknown", True), ("Other/multiple/unknown", True), ("Other-Unspecified", True),
    ("Declined To Provide", True), ("Prefer not to answer", True), ("Missing", True), ("N/A", True),
    ("Other/No response", True), ("Other/UTD", True), ("Subject did not offer ethnicity", True),
    ("Other or declines to state", True), ("Other/Refused", True), ("Don't know", True),
    ("Race not listed", False), ("Other (not included above; including mixed race)", False),
    ("Other", False), ("Other or Multiple", False), ("White", False), ("Native American", False),
])
def test_unknown_terms_in_a_race_rows_label(label: str, unknown: bool) -> None:
    assert bool(rx._UNKNOWN_TERM_RX.search(label)) is unknown



@pytest.mark.parametrize("label, states", [
    ("Hispanic White", True), ("Black Latino", True), ("White - Hispanic/Latino", True),
    ("Hispanic Native American", True), ("Mixed Hispanic/Native American", True),
    ("Non-Hispanic White", False), ("White (not Hispanic)", False), ("Not Hispanic - White", False),
    ("White", False), ("Other", False),
])
def test_a_race_row_that_states_the_ethnicity(label: str, states: bool) -> None:
    assert rx._states_ethnicity(label) is states


def test_race_rows_that_state_the_ethnicity_are_not_counted_as_not_hispanic() -> None:
    """Non-Hispanic White 30 / Non-Hispanic Black 12 / Hispanic White 5 /
    Hispanic 3, of 50: the race block is exact (the Hispanic row is the 3 the
    race rows fall short by), but "Hispanic White" holds Hispanic people."""
    table = _class_table(COMBINED_TITLE, [("Non-Hispanic White", 30), ("Non-Hispanic Black", 12),
                                          ("Hispanic White", 5), ("Hispanic", 3)])
    combined = rx.combined_classification(_study("NCT99000532", [table], participants=50))
    assert combined is not None
    assert (combined["race_rows"], combined["race_rows_named"]) == (47, 42)


# ── a race dimension named in the row label ───────────────────────────────
#
# src/race_extractor.py, block comment "Race dimension named in the row
# label". The prefix is removed before routing and mapping, so the rest lands
# where the same text lands as a row of its own.

@pytest.mark.parametrize("posted, bare", [
    ("Race: Black", "Black"),
    ("Race : Asian/Oriental", "Asian/Oriental"),
    ("Race - Black or African American", "Black or African American"),
    ("Race-White", "White"),
    ("Race_Asian", "Asian"),
    ("Race: : White", "White"),
    ("Race/Ethnicity : White", "White"),
    ("Race & Ethnicity : White", "White"),
    ("Race or Ethnicity: White", "White"),
    ("Race and/or Ethnic group - Asian", "Asian"),
    ("Ethnicity/Race: Black", "Black"),
    ("Ethnicity or Race: White", "White"),
    ("Ethnicity and/or Race: Asian", "Asian"),
    ("Race: Not Reported", "Not Reported"),
    ("Race: Mixed/Unknown", "Mixed/Unknown"),
])
def test_a_prefixed_row_maps_as_its_bare_label(posted: str, bare: str) -> None:
    prefixed = rx._map_plain_row(posted, 5, False)
    plain = rx._map_plain_row(bare, 5, False)
    assert prefixed is not None and plain is not None
    assert (prefixed["omb_category"], prefixed["confidence"]) == (plain["omb_category"], plain["confidence"])
    assert prefixed["original"] == posted and "dimension_wording_stripped" in prefixed["flags"]


def test_the_prefixed_classifications_of_one_table() -> None:
    """The NCT00707031 shape: a race and an ethnicity classification of the
    same 634, every row prefixed. Before: White 588 (by slash fragment) and
    Black 18, Asian 7 and Other 21 all counted as other."""
    table = _class_table(COMBINED_TITLE, [("Race: Caucasian/White", 588), ("Race: Black", 18),
                                          ("Race: Asian/Oriental", 7), ("Race: Other", 21),
                                          ("Ethnicity: Hispanic", 170), ("Ethnicity: Non Hispanic", 464)])
    race = extract_race_data(_study("NCT99000540", [table], participants=634))
    assert _nonzero(race) == {"white": 588, "black_african_american": 18, "asian": 7, "other": 21}
    assert [(r["original"], r["count"]) for r in race["skipped_ethnicity_rows"]] == [
        ("Ethnicity: Hispanic", 170), ("Ethnicity: Non Hispanic", 464)]


def test_a_prefixed_joined_label_is_a_race_row() -> None:
    """The NCT04523714 shape: "Race and Ethnicity : White Non-Hispanic" was
    taken for an ethnicity-only row and left out of the race block; it is a
    White row, as "White Non-Hispanic" is."""
    table = _class_table(COMBINED_TITLE, [("Race and Ethnicity : White Non-Hispanic", 60),
                                          ("Race and Ethnicity : Black or African American Non-Hispanic", 30),
                                          ("Race and Ethnicity : Hispanic", 10)])
    race = extract_race_data(_study("NCT99000541", [table], participants=100))
    assert _nonzero(race) == {"white": 60, "black_african_american": 30, "unknown_not_reported": 10}
    assert race["unknown_ethnicity_only"] == 10 and race["unknown_inferred"] == 0
    assert [r["original"] for r in race["skipped_ethnicity_rows"]] == ["Race and Ethnicity : Hispanic"]


@pytest.mark.parametrize("label", ["Race unknown", "Race not reported", "Racial background", "Ethnicity: Unknown"])
def test_labels_without_a_race_prefix_are_mapped_as_posted(label: str) -> None:
    """A separator is required, and only the race dimension is removed here."""
    assert rx._strip_race_dimension(label) is None


# ── the negation check on a stripped label, and other prefixed shapes ──────
#
# src/race_extractor.py, "Race dimension named in the row label", paragraph
# "Negation check": a fuzzy match of a label whose prefix was removed must
# agree with its key on negation.

def test_a_fuzzy_match_that_drops_a_negation_is_not_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """"Non-African American" scores 88.9 against "African American"; with
    the prefix removed it would otherwise be counted as Black."""
    mapping = rx._map_plain_row("Race: Non-African American", 9, False)
    assert mapping is not None
    assert (mapping["omb_category"], mapping["confidence"]) == ("other", "low")
    assert "unmapped" in mapping["flags"] and "dimension_wording_stripped" in mapping["flags"]
    # The rule of block comment "Negated race labels" now stops the bare text
    # too. Without that rule the guard still stops it, not the score:
    # unguarded, the bare text matches.
    monkeypatch.setattr(rx, "negated_race_label", lambda label: False)
    assert rx.map_race_category("Non-African American")["omb_category"] == "black_african_american"
    assert rx.map_race_category("Non-African American", guard_negation=True)["omb_category"] == "other"


@pytest.mark.parametrize("label", ["Race: Not reportd", "Race - Unknown or Not reportd", "Race: Not Reported."])
def test_a_fuzzy_match_that_keeps_the_negation_is_kept(label: str) -> None:
    mapping = rx._map_plain_row(label, 2, False)
    assert mapping is not None
    assert (mapping["omb_category"], mapping["confidence"]) == ("unknown_not_reported", "medium")


@pytest.mark.parametrize("label", ["Non-African American", "White", "Black/African American"])
def test_an_unprefixed_label_is_matched_without_the_check(label: str) -> None:
    assert rx._map_plain_row(label, 3, False) == {**rx._map_race_label(label), "count": 3}


@pytest.mark.parametrize("text, negates", [
    ("Not Reported", True), ("Non-White", True), ("Nonwhite", True), ("No race given", True),
    ("Unknown or Not Reported", True), ("African American", False), ("Unknown", False),
    ("Native American", False), ("Another Race", False),
])
def test_negates_race(text: str, negates: bool) -> None:
    assert rx._negates_race(text) is negates


def test_prefixed_rows_of_one_exclusive_classification() -> None:
    """Race: White 30 / Race: Black 12 / Race: Hispanic 6 / Race: Unknown 2,
    of 50: the stripped Hispanic row is still skipped, and it accounts for
    the shortfall exactly."""
    table = _class_table(COMBINED_TITLE, [("Race: White", 30), ("Race: Black", 12),
                                          ("Race: Hispanic", 6), ("Race: Unknown", 2)])
    study = _study("NCT99000550", [table], participants=50)
    race = extract_race_data(study)
    assert _nonzero(race) == {"white": 30, "black_african_american": 12, "unknown_not_reported": 8}
    assert _skipped(race) == [("Race: Hispanic", 6)]
    assert _unknown_parts(race) == (2, 6, 0) and SPLIT_FLAG in race["flags"]
    assert rx.combined_classification(study) == {
        "index": 0, "title": COMBINED_TITLE, "race_rows": 42, "race_rows_named": 42,
        "ethnicity_rows": 6, "unknown_rows": 2}


def test_prefixed_race_by_sex_rows_split_and_map() -> None:
    table = _class_table("Race by Sex", [("Race: White : Female", 10), ("Race: White : Male", 12),
                                         ("Race: Black : Female", 5), ("Race: Black : Male", 3)])
    race = extract_race_data(_study("NCT99000551", [table], participants=30))
    assert _nonzero(race) == {"white": 22, "black_african_american": 8}
    assert "race_by_sex_table" in race["flags"]
    assert race["joint_sex_counts"]["counts"] == {"white": {"female": 10, "male": 12},
                                                  "black_african_american": {"female": 5, "male": 3}}
    assert all({"dimension_wording_stripped", "race_by_sex"} <= set(c["flags"]) for c in race["raw_categories"])
    assert race["raw_categories"][0]["original"] == "Race: White : Female"


def test_prefixed_race_rows_under_a_sex_title_are_read_as_race() -> None:
    """Every row maps to a race-specific category once the prefix is
    removed, so the sex/gender-titled table passes the gate."""
    table = _class_table("Sex/Gender, Customized", [("Race: White", 20), ("Race: Black", 10)])
    assert rx.is_sex_titled_race_table(table) is True


# ── the ethnicity dimension named in the row label ────────────────────────
#
# src/race_extractor.py, block comment "Ethnicity dimension named in the row
# label": such a row is a row of the table's ethnicity classification, left
# out of the race counts, unless every row of the table names it.

def test_ethnicity_rows_beside_race_rows_are_not_counted_as_race() -> None:
    """The NCT00452530 shape: the race rows hold all 3,057; "Ethnicity not
    reported" 2,946 was added to other on top of them."""
    table = _class_table(COMBINED_TITLE, [("White", 2427), ("Black/African American", 31), ("Asian", 506),
                                          ("Native Hawaiian/Other Pacific Islander", 2), ("Other [Not specified]", 91),
                                          ("Hispanic/Latino", 1), ("Not Hispanic/Latino", 110),
                                          ("Ethnicity not reported", 2946)])
    race = extract_race_data(_study("NCT99000550", [table], participants=3057))
    assert race["omb_totals"]["other"] == 91
    assert sum(race["omb_totals"].values()) == 3057
    recorded = {r["original"]: r for r in race["skipped_ethnicity_rows"]}
    assert recorded["Ethnicity not reported"]["count"] == 2946
    assert "ethnicity_dimension_row" in recorded["Ethnicity not reported"]["flags"]
    assert "ethnicity_dimension_row" not in recorded["Hispanic/Latino"]["flags"]


def test_a_second_ethnic_classification_is_not_counted_as_race() -> None:
    """The NCT03197376 shape: Race-African 2,249 / Race-Other 1, and eight
    ethnic groups of the same 2,250."""
    table = _class_table(COMBINED_TITLE, [("Race-African", 2249), ("Race-Other", 1), ("Ethnicity-Mandinka", 1174),
                                          ("Ethnicity-Wolof", 217), ("Ethnicity-Fula", 272), ("Ethnicity-Jola", 280),
                                          ("Ethnicity-Other", 307)])
    race = extract_race_data(_study("NCT99000551", [table], participants=2250))
    assert _nonzero(race) == {"black_african_american": 2249, "other": 1}
    assert len(race["skipped_ethnicity_rows"]) == 5


def test_an_unknown_ethnicity_row_of_an_exclusive_table_is_unknown_race() -> None:
    """The NCT01022268 shape: race rows, "Declined" and "Ethnicity not
    available" make up the 902; the last 291 were given no race."""
    table = _class_table(COMBINED_TITLE, [("Asian", 69), ("Black", 90), ("Mixed race", 36), ("Other", 42),
                                          ("White", 297), ("Declined", 77), ("Ethnicity not available", 291)])
    race = extract_race_data(_study("NCT99000552", [table], participants=902))
    assert race["omb_totals"]["other"] == 42
    assert race["omb_totals"]["unknown_not_reported"] == 77 + 291
    assert race["unknown_inferred"] == 291 and race["unknown_ethnicity_only"] == 0


def test_a_table_whose_every_row_names_ethnicity_is_read_as_race() -> None:
    """The NCT03476187 shape: the registrant called the table's one
    classification "Ethnicity"; its rows are races and Hispanic."""
    table = _class_table(COMBINED_TITLE, [("Ethnicity : Caucasian", 154), ("Ethnicity : Black or African American", 62),
                                          ("Ethnicity : Hispanic or Latin", 16), ("Ethnicity : Other", 5),
                                          ("Ethnicity : Asian", 1)])
    race = extract_race_data(_study("NCT99000553", [table], participants=238))
    assert _nonzero(race) == {"white": 154, "black_african_american": 62, "other": 5, "asian": 1,
                              "unknown_not_reported": 16}
    assert all("dimension_wording_stripped" in c["flags"] for c in race["raw_categories"])
    assert [r["original"] for r in race["skipped_ethnicity_rows"]] == ["Ethnicity : Hispanic or Latin"]


@pytest.mark.parametrize("label, names", [
    ("Ethnicity Not Reported", True), ("Ethnicity-Unknown or Not Reported", True), ("Ethnic group - Other", True),
    ("Ethnicity/Race Unknown", False), ("Ethnicity and Race: White", False), ("Ethnicities", False),
    ("Ethnicity or Race: White", False), ("Ethnicity and/or Race: White", False),
    ("Ethnic group or race - Asian", False), ("White", False), ("Race: Unknown", False),
])
def test_which_labels_name_the_ethnicity_dimension(label: str, names: bool) -> None:
    assert rx._names_ethnicity_dimension(label) is names


def test_rows_prefixed_with_a_combined_name_joined_by_or_are_read_as_race() -> None:
    """"Ethnicity or Race: White" names the one combined classification, not
    the ethnicity dimension (a review finding): the rows are read as race,
    not recorded as ethnicity rows, so the table publishes its race counts."""
    table = _class_table(COMBINED_TITLE, [("Ethnicity or Race: White", 30), ("Ethnicity or Race: Black", 20)])
    race = extract_race_data(_study("NCT99000560", [table], participants=50))
    assert race["reported"] is True
    assert _nonzero(race) == {"white": 30, "black_african_american": 20}
    assert race["skipped_ethnicity_rows"] == []


def test_a_combined_dimension_name_stays_a_row_of_the_classification() -> None:
    """"Ethnicity/Race Unknown" (NCT00552448) is the combined
    classification's unknown row: counted as race unknown, as before."""
    table = _class_table(COMBINED_TITLE, [("Black", 18), ("White", 11), ("Hispanic", 3), ("Ethnicity/Race Unknown", 2)])
    race = extract_race_data(_study("NCT99000554", [table], participants=34))
    assert race["unknown_explicit"] == 2 and race["unknown_ethnicity_only"] == 3


def test_ethnicity_rows_crossed_with_sex_are_not_counted_as_race() -> None:
    """A race-by-sex table whose ethnicity rows are crossed with sex too (a
    review finding): the race parts are routed as a plain table's rows are,
    and a recorded row keeps its race part and sex."""
    table = _class_table(COMBINED_TITLE, [("White Female", 30), ("White Male", 20),
                                          ("Ethnicity-Not Reported Female", 30), ("Ethnicity-Not Reported Male", 20)])
    race = extract_race_data(_study("NCT99000555", [table], participants=50))
    assert _nonzero(race) == {"white": 50}
    recorded = race["skipped_ethnicity_rows"]
    assert [(r["original"], r["race_part"], r["sex"], r["count"]) for r in recorded] == [
        ("Ethnicity-Not Reported Female", "Ethnicity-Not Reported", "female", 30),
        ("Ethnicity-Not Reported Male", "Ethnicity-Not Reported", "male", 20)]
    assert all({"race_by_sex", "ethnicity_dimension_row"} <= set(r["flags"]) for r in recorded)


def test_a_race_by_sex_table_whose_race_parts_all_name_ethnicity_is_read_as_race() -> None:
    table = _class_table(COMBINED_TITLE, [("Ethnicity: White Female", 30), ("Ethnicity: White Male", 20),
                                          ("Ethnicity: Asian Female", 6), ("Ethnicity: Asian Male", 4)])
    race = extract_race_data(_study("NCT99000556", [table], participants=60))
    assert _nonzero(race) == {"white": 50, "asian": 10}
    assert race["joint_sex_counts"] is not None


def test_an_all_prefixed_ethnicity_classification_publishes_no_race() -> None:
    """Every row names "Ethnicity" and none is a race (a review finding): the
    table is an ethnicity classification, so its "Unknown" is not race
    unknown and nothing is counted as race."""
    table = _class_table(COMBINED_TITLE, [("Ethnicity: Hispanic", 10), ("Ethnicity: Not Hispanic", 35),
                                          ("Ethnicity: Unknown", 5)])
    race = extract_race_data(_study("NCT99000557", [table], participants=50))
    assert race["reported"] is False
    assert [r["original"] for r in race["skipped_ethnicity_rows"]] == [
        "Ethnicity: Hispanic", "Ethnicity: Not Hispanic", "Ethnicity: Unknown"]
    assert "ethnicity_dimension_row" in race["skipped_ethnicity_rows"][2]["flags"]


def test_an_origin_group_row_is_not_evidence_that_an_all_prefixed_table_is_race() -> None:
    """"Japanese" is in the race vocabulary (Asian, subcategory japanese), but
    "Ethnicity: Japanese" / "Ethnicity: Not Japanese" classifies one
    ethnicity, not race (a review finding): no row names a race category
    itself, so neither row is counted as race and both are recorded."""
    table = _class_table(COMBINED_TITLE, [("Ethnicity: Japanese", 40), ("Ethnicity: Not Japanese", 60)])
    race = extract_race_data(_study("NCT99000558", [table], participants=100))
    assert race["reported"] is False
    assert race["omb_totals"]["asian"] == 0
    assert [r["original"] for r in race["skipped_ethnicity_rows"]] == [
        "Ethnicity: Japanese", "Ethnicity: Not Japanese"]
    assert all("ethnicity_dimension_row" in r["flags"] for r in race["skipped_ethnicity_rows"])


def test_an_origin_group_row_is_read_as_race_when_another_row_names_a_race() -> None:
    """The NCT01151215 shape: "Ethnicity: Native American" names a race
    category itself, so the table is a race classification and its
    "Ethnicity: Japanese" and "Ethnicity: Chinese" rows are read as Asian."""
    table = _class_table(COMBINED_TITLE, [("Ethnicity: Native American", 5), ("Ethnicity: Japanese", 30),
                                          ("Ethnicity: Chinese", 25), ("Ethnicity: Other", 40)])
    race = extract_race_data(_study("NCT99000559", [table], participants=100))
    assert race["reported"] is True
    assert race["omb_totals"]["american_indian_alaska_native"] == 5
    assert race["omb_totals"]["asian"] == 55
    assert race["omb_totals"]["other"] == 40
    assert race["skipped_ethnicity_rows"] == []


# ── Middle Eastern or North African labels ─────────────────────────────────
#
# src/race_extractor.py, block comments "Middle Eastern or North African
# labels" and "Middle Eastern or North African within White".

@pytest.mark.parametrize("label, subcategory", [
    ("Middle Eastern or North African", "middle_eastern_north_african"),
    ("White - Arabic/North African Heritage", "middle_eastern_north_african"),
    ("White - Arabic / North African heritage", "middle_eastern_north_african"),
    ("WHITE (W) ARABIC/NORTH AFRICAN HER", "middle_eastern_north_african"),
    ("Wh - Arabic/North African H", "middle_eastern_north_african"),
    ("White (Middle Eastern or North African)", "middle_eastern_north_african"),
    ("MENA: Middle Eastern North African", "middle_eastern_north_african"),
    ("Middle Eastern/North African", "middle_eastern_north_african"),
    ("White: Arabic", "middle_eastern_arab"),
    ("Arab ethnicity", "middle_eastern_arab"),
    ("North African descent", "north_african"),
    ("Iranian/Persian", "middle_eastern_persian"),
    ("Arab/Middle Eastern", "middle_eastern"),
    ("Middle Eastern (Arab)", "middle_eastern"),
])
def test_a_label_naming_only_mena_groups_is_white_with_a_mena_subcategory(label: str, subcategory: str) -> None:
    mapping = rx.map_race_category(label)
    assert (mapping["omb_category"], mapping["subcategory"], mapping["confidence"]) == ("white", subcategory, "high")
    assert subcategory in rx.MENA_SUBCATEGORIES


@pytest.mark.parametrize("label", [
    "White/Middle Eastern", "Arab or West Asian", "Other: Arabic", "Native Arab",
    "Chinese, Middle Eastern, or Other Ethnic Group", "Middle Eastern or North African/Mediterranean",
    "North African (Morocco)", "Caucasian and North African", "Midwest", "White", "Turkish",
])
def test_a_label_naming_another_group_is_not_read_by_the_mena_rule(label: str) -> None:
    assert rx.mena_subcategory(label) is None


def test_the_vocabularys_own_entries_keep_their_subcategories() -> None:
    assert rx.map_race_category("Arab")["subcategory"] == "middle_eastern_arab"
    assert rx.map_race_category("Middle Eastern")["subcategory"] == "middle_eastern"
    assert "middle_eastern_turkish" not in rx.MENA_SUBCATEGORIES


def test_mena_in_white_is_the_mena_part_of_white() -> None:
    """The GSK heritage shape: before, its MENA row was counted as other."""
    table = _category_table("Race/Ethnicity, Customized", [
        ("White", 120), ("White - Arabic/North African Heritage", 14),
        ("Black or African American", 10), ("White/Middle Eastern", 6)])
    race = extract_race_data(_study("NCT99000563", [table], participants=150))
    assert race["omb_totals"]["white"] == 140 and race["omb_totals"]["other"] == 0
    assert race["mena_in_white"] == 14            # "White/Middle Eastern" names two groups: not in it
    assert race["subcategory_totals"]["white_middle_eastern_north_african"] == 14


def test_every_block_carries_mena_in_white() -> None:
    plain = extract_race_data(_study("NCT99000561", [_category_table("Race (NIH/OMB)", [("White", 30), ("Asian", 5)])], 35))
    assert plain["mena_in_white"] == 0
    none = extract_race_data(_study("NCT99000562", [], 35))
    assert none["reported"] is False and none["mena_in_white"] == 0


# ── Heritage-template labels ───────────────────────────────────────────────
#
# src/race_extractor.py, block comment "Heritage-template labels".

@pytest.mark.parametrize("label, category, subcategory", [
    ("White - Caucasian / European Heritage", "white", None),
    ("White-Caucasian/ European heritage, n (%)", "white", None),
    ("Wt-Wt/Caucasian (Ca.)/European (Eu.) H.", "white", None),
    ("White(Wh)-Wh/Caucasian(Ca)/European(Eu)Heritage(H)", "white", None),
    ("White, European Heritage", "white", "european"),
    ("White Heritage", "white", None),
    ("Asian - East Asian Heritage", "asian", "east_asian"),
    ("Asian-East Asian H", "asian", "east_asian"),
    ("Asian - South East Asian Heritage", "asian", "southeast_asian"),
    ("Asian - Japanese Heritage", "asian", "japanese"),
    ("Asian - Chinese Heritage", "asian", "chinese"),
    ("Asian - Central/South Asian Heritage", "asian", None),
    ("East/South East Asian heritage", "asian", None),
    ("Japanese H./East Asian H./SouthEast Asian H.", "asian", None),
    ("Japanese Heritage", "asian", "japanese"),
    ("White-Arabic/NA H and white/caucasia/European H", "white", None),  # NA: North African
    ("JAPANESE HERITAGE(H)/EAST A H/SOUTH EAST A H", "asian", None),
    ("Black, of African Heritage", "black_african_american", "african"),
    ("Black of African heritage", "black_african_american", "african"),
    ("Asian; South East Asian Heritage", "asian", "southeast_asian"),
    ("Asian-South East Asian Heritag", "asian", "southeast_asian"),
    ("Black or African Heritage", "black_african_american", None),
    ("African American (Af Am)/African Heritage (Af Ht)", "black_african_american", None),
    ("American Indian/ Alaska native Heritage", "american_indian_alaska_native", None),
    ("Mixed Heritage", "more_than_one_race", None),
])
def test_a_heritage_template_label_maps_to_its_race(label: str, category: str, subcategory: Optional[str]) -> None:
    mapping = rx.map_race_category(label)
    assert (mapping["omb_category"], mapping["subcategory"]) == (category, subcategory)
    assert mapping["confidence"] in ("high", "medium") and "heritage_label" in mapping["flags"]


@pytest.mark.parametrize("label", [
    "African American/African Heritage & White",
    "African American/ African and White Heritage",
    "American Indian/Alaskan Native and White Heritage",
    "Multiple-Afr Amer/Afr Her/Amer Ind or Al N",
    "Multiple-Black/AA and White/Caucasian/EU H",
    "Asian - East Asian and White Heritage",
    "AA/African Heritage and AI or AN and White",
    "Af Am/Af Her & White",
    "Asian Heritage and Native Hawaiian Heritage",  # two races, not the 1977 grouping
    "Asian Heritage and Pacific Islander Heritage",  # each side its own heritage
    "Asian Heritage and Other Pacific Islander Heritage",
])
def test_a_heritage_combination_is_more_than_one_race(label: str) -> None:
    mapping = rx.map_race_category(label)
    assert mapping["omb_category"] == "more_than_one_race" and "heritage_label" in mapping["flags"]


@pytest.mark.parametrize("label", [
    "African Amer/African Heritage/Amer Ind or Alaska N",  # two races joined by "/": either or both
    "African - White / Caucasian Heritage",
    "Mixed Heritage or Other",
    "Af Amer/White Heritage",  # an unread group beside a race: either race or both
    "American/African Heritage",
    "Mixed-Heritage or Other",  # the "Mixed-" prefix does not override an other row
    "Mixed-Heritage Unknown",
    "Multiple-Heritage Refused",  # a tail that reads as no race
    "Multiple-Heritage N/A",
    "Multiple-White-Refused Heritage",  # a qualified tail group read whole
    "Multiple-African American-NA Heritage",  # not by a fuzzy match
    "American Indian or Alaska Native-NA Heritage",  # the whole text not fuzzily either
    "White - Caucasian-NA Heritage",  # a compact non-answer inside a group
    "White - (N/A) Heritage",  # a parenthesised non-answer is not a gloss
    "Asian - (Unk) Heritage",
    "Multiple-White-(N/A) Heritage",
    "American Indian or Alaska Native-NA H",  # not fuzzily matched before the rule
    "African American-NA H",
    "Native Hawaiian or Other Pacific Islander-Unk H",
    "White - Refused Heritage",  # a non-answer after a qualifier
    "White - N/A Heritage",
    "White - NA Heritage",  # a compact non-answer with no group beside it
    "Asian - Unk Heritage",
    "Non-European heritage",
])
def test_a_heritage_label_naming_no_single_race_is_not_read(label: str) -> None:
    mapping = rx.map_race_category(label)
    assert mapping["omb_category"] == "other" and "heritage_label" not in mapping["flags"]


@pytest.mark.parametrize("label, category, subcategory, flag", [
    ("White - White/Caucasian/European Heritage", "white", None, "slash_normalized"),
    ("African American/African Heritage", "black_african_american", "african_american", "slash_normalized"),
    ("Japanese/East Asian HER/South East Asian HER", "asian", "japanese", "slash_normalized"),
    ("White - Arabic/North African Heritage", "white", "middle_eastern_north_african", "mena_label"),
])
def test_a_heritage_label_the_vocabulary_already_reads_keeps_its_reading(
        label: str, category: str, subcategory: Optional[str], flag: str) -> None:
    mapping = rx.map_race_category(label)
    assert (mapping["omb_category"], mapping["subcategory"]) == (category, subcategory)
    assert flag in mapping["flags"] and "heritage_label" not in mapping["flags"]


def test_a_heritage_template_table_counts_no_row_as_other() -> None:
    """The template's rows, as one trial posts them: before, East Asian,
    South East Asian, the European row and the combination were other or
    the wrong race, and "Japanese Heritage" was quarantined as not race."""
    table = _category_table(COMBINED_TITLE, [
        ("White - Caucasian / European Heritage", 120), ("White - Arabic/North African Heritage", 6),
        ("Asian - East Asian Heritage", 30), ("Asian - South East Asian Heritage", 12),
        ("Japanese Heritage", 9), ("African American/African Heritage", 15),
        ("African American/African Heritage & White", 3)])
    race = extract_race_data(_study("NCT99000564", [table], participants=195))
    assert _nonzero(race) == {"white": 126, "asian": 51, "black_african_american": 15, "more_than_one_race": 3}
    assert race["quarantined_labels"] == [] and race["mena_in_white"] == 6
    assert race["subcategory_totals"]["asian_japanese"] == 9


# ── Rows naming two or more races ──────────────────────────────────────────
#
# src/race_extractor.py, block comment "Rows naming two or more races".

@pytest.mark.parametrize("label", [
    "American Indian or Alaska Native & White",  # was American Indian
    "Black or African American & White",  # was Black
    "Asian and White",
    "White + Asian",
    "Black/African American and White/Caucasian",  # was Black, by its first slash fragment
    "American Indian/Alaska Native and Asian and White",
    "Asian & Native Hawaiian or Other Pacific Islander",  # 1997 categories joined, not the 1977 grouping
    "Black & Middle Eastern/North African",
    "Multiple-Black or African American and White",
    "Multiple - Asian, White",
    "Mixed (Black/White)",
    "Mixed: Black/Indian",  # was Asian
    "Multiracial: black/white/native",  # an unread part beside two races
    "More Than One Race (Black/African American, White and American Indian/Alaska Native)",
    "MULTIPLE-ASIAN & BLACK OR AFRICAN AMERICAN",
    "Caucasian, Black, and Asian",  # a list with a joiner
    "American Indian and Alaska Native and White",  # a category name holding "and", then a joiner
    "African-American/Black, Hispanic, and Caucasian/White",  # an ethnicity part passed over
])
def test_a_label_naming_two_or_more_races_is_more_than_one_race(label: str) -> None:
    mapping = rx.map_race_category(label)
    assert mapping["omb_category"] == "more_than_one_race" and "multirace_label" in mapping["flags"]


@pytest.mark.parametrize("label", [
    "Asian and Pacific Islander",  # the 1977 grouped category
    "Asian & Other Pacific Islander",
    "Black and African American",  # one race
    "Asian and Asian American",
    "American Indian and Alaska Native",  # a part that is not read as a race
    "White and Hispanic",  # an ethnicity, not a race
    "White and Other",
    "Black and Unknown",
    "White, Asian",  # a comma alone may join one grouped category
    "Asian, Pacific Islander",
    "White or Black",  # either race
    "Black/White",
    "Multiple - child",  # a prefix naming no race
    "Mixed Asian",  # a prefix naming one race
    "Mixed - Any other mixed background",
    "Asian, Indigenous, Native Hawaiian and Pacific Islander, non-Hispanic",  # NCT03360539: a grouping
    "Native Hawaiian and Other Pacific Islander",  # one category holding "and"
])
def test_a_label_not_naming_two_races_is_not_read_as_more_than_one_race(label: str) -> None:
    assert "multirace_label" not in rx.map_race_category(label)["flags"]


def test_a_multiracial_breakdown_adds_to_the_table_population() -> None:
    """The NCT00609674 shape: single-race rows and combination rows add up
    to the population, so the combinations are counted as more than one race
    rather than as their first race or as other."""
    table = _category_table(COMBINED_TITLE, [
        ("White", 60), ("Black or African American", 20), ("Asian", 10),
        ("American Indian or Alaska Native & White", 6), ("Asian and White", 4)])
    race = extract_race_data(_study("NCT99000565", [table], participants=100))
    assert _nonzero(race) == {"white": 60, "black_african_american": 20, "asian": 10, "more_than_one_race": 10}


# ── refinements of the ethnicity-dimension rule ────────────────────────────
#
# src/race_extractor.py, "Ethnicity dimension named in the row label": a
# residual unnamed row does not make the named rows an ethnicity
# classification; a row named for race does; the negation check applies to
# the text left after the ethnicity name; and a label naming race after any
# joiner, or none, is not named for the ethnicity dimension.

@pytest.mark.parametrize("residual, category", [("Unknown", "unknown_not_reported"), ("Other", "other")])
def test_a_residual_unnamed_row_does_not_make_named_race_rows_an_ethnicity_classification(
        residual: str, category: str) -> None:
    """"Ethnicity: White" 50 / "Ethnicity: Black" 30 / a bare residual 5, of
    85: the residual is that classification's own, not a race classification;
    leaving the named rows out would balance 80 into unknown."""
    table = _class_table(COMBINED_TITLE, [("Ethnicity: White", 50), ("Ethnicity: Black", 30), (residual, 5)])
    race = extract_race_data(_study("NCT99000690", [table], participants=85))
    assert _nonzero(race) == {"white": 50, "black_african_american": 30, category: 5}
    assert race["skipped_ethnicity_rows"] == []
    assert ["dimension_wording_stripped" in c["flags"] for c in race["raw_categories"]] == [True, True, False]


def test_an_unmapped_unnamed_row_does_not_make_named_race_rows_an_ethnicity_classification() -> None:
    table = _class_table(COMBINED_TITLE, [("Ethnicity: White", 50), ("Ethnicity: Asian", 10),
                                          ("Prefer to self-describe", 4)])
    race = extract_race_data(_study("NCT99000691", [table], participants=64))
    assert (_nonzero(race)["white"], _nonzero(race)["asian"]) == (50, 10)
    assert race["skipped_ethnicity_rows"] == []


@pytest.mark.parametrize("rows, participants", [
    ([("Race,Asian", 279), ("Ethnicity, Chinese", 279)], 279),                  # NCT01256177
    ([("Race - Undisclosed", 13), ("Ethnicity - Undisclosed", 13)], 13),        # NCT03406507
    ([("Race", 13), ("Ethnicity", 12)], 13),                                    # NCT01606215
])
def test_an_unmapped_row_named_for_race_is_the_tables_race_classification(
        rows: list[tuple[str, int]], participants: int) -> None:
    """The race row maps to no race-specific category, but its label names
    the race dimension: the named ethnicity row is the second classification
    of the same people and is left out, never read as race beside it."""
    race = extract_race_data(_study("NCT99000692", [_class_table(COMBINED_TITLE, rows)], participants=participants))
    assert sum(race["omb_totals"].values()) == participants
    assert [r["original"] for r in race["skipped_ethnicity_rows"]] == [rows[1][0]]
    assert "ethnicity_dimension_row" in race["skipped_ethnicity_rows"][0]["flags"]


@pytest.mark.parametrize("label, named", [
    ("Race - Undisclosed", True), ("Race,Asian", True), ("Race", True), ("Race unknown", True),
    ("Racial group: Other", True), ("Racecourse", False), ("Black", False), ("Ethnicity: White", False),
])
def test_names_race_dimension(label: str, named: bool) -> None:
    assert rx._names_race_dimension(label) is named


def test_a_stripped_ethnicity_name_keeps_the_negation_check() -> None:
    """Read as race, "Ethnicity: Non-African American" becomes "Non-African
    American", 88.9 against "African American": without the check it would
    be counted as Black."""
    table = _class_table(COMBINED_TITLE, [("Ethnicity: White", 40), ("Ethnicity: Non-African American", 9)])
    race = extract_race_data(_study("NCT99000693", [table], participants=49))
    assert _nonzero(race) == {"white": 40, "other": 9}
    negated = race["raw_categories"][1]
    assert negated["original"] == "Ethnicity: Non-African American"
    assert {"unmapped", "dimension_wording_stripped"} <= set(negated["flags"])


def test_a_negated_named_row_alone_is_not_evidence_of_a_race_classification() -> None:
    """With the check, "Ethnicity: Non-African American" maps to no race, so
    a table of it and "Ethnicity: Unknown" is an ethnicity classification."""
    table = _class_table(COMBINED_TITLE, [("Ethnicity: Non-African American", 9), ("Ethnicity: Unknown", 3)])
    race = extract_race_data(_study("NCT99000694", [table], participants=12))
    assert race["reported"] is False


@pytest.mark.parametrize("label", ["Ethnicity, Race: Asian", "Ethnicity - Race Unknown", "Ethnicity Race Unknown",
                                   "Ethnicity/Races: Other", "Ethnicity & Racial group"])
def test_a_label_naming_race_after_any_joiner_does_not_name_the_ethnicity_dimension(label: str) -> None:
    assert rx._names_ethnicity_dimension(label) is False


def test_a_word_beginning_with_race_is_not_a_joined_race_name() -> None:
    assert rx._names_ethnicity_dimension("Ethnicity: Racecourse staff") is True


def test_a_label_naming_both_dimensions_beside_race_rows_stays_counted() -> None:
    """"Ethnicity, Race: White" names race too: a race row, not the table's
    ethnicity classification, so it is not left out."""
    table = _class_table(COMBINED_TITLE, [("Black", 10), ("Ethnicity, Race: White", 20)])
    race = extract_race_data(_study("NCT99000695", [table], participants=30))
    assert race["skipped_ethnicity_rows"] == []
    assert _nonzero(race) == {"black_african_american": 10, "white": 20}
    white = race["raw_categories"][1]
    assert white["original"] == "Ethnicity, Race: White" and "dimension_wording_stripped" in white["flags"]


@pytest.mark.parametrize("label, category", [
    ("Ethnicity, Race: White", "white"), ("Ethnicity - Race: Asian", "asian"),
    ("Ethnicity Race: Black", "black_african_american"), ("Ethnicity, Race Unknown", "unknown_not_reported"),
    ("Ethnicity Race Unknown", "unknown_not_reported"), ("Ethnic group, Race: White", "white"),
])
def test_a_combined_name_with_a_new_joiner_is_removed_before_mapping(label: str, category: str) -> None:
    """The race name after the ethnicity name is read as the race prefix
    rule reads it, so the row lands in its race, not in other."""
    mapping = rx._map_plain_row(label, 5, False)
    assert mapping is not None
    assert (mapping["omb_category"], mapping["original"]) == (category, label)
    assert "dimension_wording_stripped" in mapping["flags"]


@pytest.mark.parametrize("label, stripped", [
    ("Ethnicity or Race: White", "White"), ("Race and Ethnicity: White", "White"), ("Race: Black", "Black"),
    ("Ethnicity: White", None), ("Ethnicity: Racecourse staff", None), ("White", None),
])
def test_existing_prefix_forms_are_stripped_as_before(label: str, stripped: Optional[str]) -> None:
    assert rx._strip_race_dimension(label) == stripped


# ── Comma lists naming two or more races ───────────────────────────────────
#
# src/race_extractor.py, block comment "Comma lists naming two or more races".

@pytest.mark.parametrize("label, races", [
    ("White, Asian", {"white", "asian"}),
    ("Black or African American, White or Caucasian", {"black_african_american", "white"}),
    ("American Indian or Alaska Native,White", {"american_indian_alaska_native", "white"}),
    ("Asian; White", {"asian", "white"}),
    # An ethnicity part passed over; slash fragments read.
    ("Asian, Black/African American, Hispanic, White/Cau", {"asian", "black_african_american", "white"}),
    # A category name holding "and" is one part.
    ("White, Native Hawaiian and Other Pacific Islander", {"white", "native_hawaiian_pacific_islander"}),
    # A comma list whatever it means: the table decides (tests below).
    ("Asian, Pacific Islander", {"asian", "native_hawaiian_pacific_islander"}),
])
def test_a_comma_list_names_its_races(label: str, races: set[str]) -> None:
    assert rx._comma_list_races(label) == races


@pytest.mark.parametrize("label", [
    "White, Caucasian",  # one race
    "White, non-Hispanic",
    "Asian, Hispanic",
    "White, Asian, or Hispanic",  # NCT03506724: alternatives
    "White, Asian, or Black",
    "Asian, Black or African American, Other",  # a part that is not a race
    "White, Asian/Black",  # a part that may name either race (a review finding)
    "White, Asian/Other",  # Asian or another race (a review finding)
    "White, Asian/Refused",  # Asian or no answer (a review finding)
    "White, Asian/N/A",
    "White, Asian/Not Applicable",
    "Asian/Pacific Islander, White",  # the 1977 grouping, with a slash
    "More than one race, White",
    "Multiple - Asian, White",  # the prefix rule reads it (block comment "Rows naming two or more races")
    "Asian and White",  # the joiner rule reads it
    "Caucasian, Black, and Asian",
    "White - European Heritage, Asian - East Asian Heritage",  # a heritage word
    "White",
])
def test_a_label_that_is_not_a_comma_list(label: str) -> None:
    assert rx._comma_list_races(label) is None


def test_a_comma_list_is_a_combination_when_its_table_names_one_of_its_races_alone() -> None:
    """The NCT05064449 shape. Before, the fuzzy match read "White, American
    Indian or Alaska Native" as American Indian and "White, Black or African
    American" as Black, and "White, Native Hawaiian or Other Pacific
    Islander" was Native Hawaiian. American Indian has no row of its own
    (nobody reported it alone); White does, which is enough."""
    table = _category_table(COMBINED_TITLE, [
        ("Asian", 10), ("Black or African American", 20),
        ("Native Hawaiian or Other Pacific Islander", 2), ("White", 60),
        ("White, American Indian or Alaska Native", 3), ("White, Black or African American", 4),
        ("White, Native Hawaiian or Other Pacific Islander", 1)])
    race = extract_race_data(_study("NCT99000566", [table], participants=100))
    assert _nonzero(race) == {"asian": 10, "black_african_american": 20,
                              "native_hawaiian_pacific_islander": 2, "white": 60,
                              "more_than_one_race": 8}
    combos = [c for c in race["raw_categories"] if c["omb_category"] == "more_than_one_race"]
    assert [(c["original"], c["count"]) for c in combos] == [
        ("White, American Indian or Alaska Native", 3), ("White, Black or African American", 4),
        ("White, Native Hawaiian or Other Pacific Islander", 1)]
    for c in combos:
        assert c["flags"].count("multirace_comma_list") == 1 and c["confidence"] == "high"
        assert not any(f.startswith("fuzzy_match") for f in c["flags"])


def test_a_comma_list_naming_no_race_alone_in_its_table_keeps_its_reading() -> None:
    """The NCT01403051 shape: the 1977 Asian or Pacific Islander group beside
    rows that name neither race alone is a grouped category, not people who
    reported both."""
    table = _category_table(COMBINED_TITLE, [
        ("White Non-Hispanic", 50), ("Black Non-Hispanic", 30),
        ("Hispanic (Regardless of Race)", 12), ("Asian, Pacific Islander", 7),
        ("American Indian, Alaskan Native", 1)])
    race = extract_race_data(_study("NCT99000567", [table], participants=100))
    assert race["omb_totals"]["more_than_one_race"] == 0
    row = next(c for c in race["raw_categories"] if c["original"] == "Asian, Pacific Islander")
    # The group is read as Asian (block comment "The 1977 Asian or Pacific
    # Islander category"), not as people who reported both.
    assert row["omb_category"] == "asian" and "multirace_comma_list" not in row["flags"]
    assert "asian_pacific_grouped" in row["flags"]


def test_only_a_row_read_as_one_race_without_a_fuzzy_match_names_a_race_alone() -> None:
    """"Assian" (a fuzzy match) and the comma list "Asian, White" do not
    name Asian alone, so "Asian, Pacific Islander" keeps its reading;
    "Asian, White" is a combination through the White row. ("Asians" was
    the fuzzy match here until the Asian label rule read it.)"""
    table = _category_table(COMBINED_TITLE, [
        ("White", 40), ("Assian", 10), ("Asian, White", 3), ("Asian, Pacific Islander", 5)])
    race = extract_race_data(_study("NCT99000568", [table], participants=58))
    readings = {c["original"]: c["omb_category"] for c in race["raw_categories"]}
    assert readings["Asian, White"] == "more_than_one_race"
    assert readings["Asian, Pacific Islander"] == "asian"  # the 1977 group, not a combination


def test_a_comma_list_in_a_race_by_sex_table() -> None:
    """The race part is what is read, and the record keeps the posted label."""
    table = _category_table(COMBINED_TITLE, [
        ("White - Female", 20), ("White - Male", 18), ("Black - Female", 5),
        ("White, Black - Female", 2), ("White, Black - Male", 1)])
    race = extract_race_data(_study("NCT99000569", [table], participants=46))
    assert _nonzero(race) == {"white": 38, "black_african_american": 5, "more_than_one_race": 3}
    combos = [c for c in race["raw_categories"] if c["omb_category"] == "more_than_one_race"]
    assert [(c["original"], c["race_part"], c["sex"]) for c in combos] == [
        ("White, Black - Female", "White, Black", "female"), ("White, Black - Male", "White, Black", "male")]
    assert all({"multirace_comma_list", "race_by_sex"} <= set(c["flags"]) for c in combos)


def test_a_comma_list_keeps_the_row_level_steps_and_reads_alike_row_by_row() -> None:
    """Race dimension wording is removed before the list is read and is
    recorded as for any row; extract_race_from_measure reads the table the
    same way."""
    table = _category_table("Race, Customized", [
        ("Race: White", 30), ("Race: Black", 10), ("Race: White, Black", 2)])
    race = extract_race_data(_study("NCT99000570", [table], participants=42))
    row = next(c for c in race["raw_categories"] if c["original"] == "Race: White, Black")
    assert row["omb_category"] == "more_than_one_race"
    assert {"multirace_comma_list", "dimension_wording_stripped"} <= set(row["flags"])
    assert [(m["original"], m["omb_category"]) for m in rx.extract_race_from_measure(table)] == [
        ("Race: White", "white"), ("Race: Black", "black_african_american"),
        ("Race: White, Black", "more_than_one_race")]


def test_a_comma_list_read_as_more_than_one_race_is_race_specific_under_a_sex_title() -> None:
    """A sex/gender-titled measure is read as race only when every row is
    race-specific (_parse_race_table). "White, Asian" was other before, and
    the measure was not read as race."""
    table = _category_table("Sex/Gender, Customized", [("White", 30), ("Asian", 10), ("White, Asian", 2)])
    assert rx.is_sex_titled_race_table(table)
    race = extract_race_data(_study("NCT99000571", [table], participants=42))
    assert _nonzero(race) == {"white": 30, "asian": 10, "more_than_one_race": 2}


def test_a_comma_list_part_naming_either_of_two_races_leaves_the_row_as_it_was() -> None:
    """A review finding: "Asian/Black" was read as its first fragment, Asian,
    so "White, Asian/Black" beside a White row became more than one race. A
    slash part names one race only when its fragments agree."""
    table = _category_table(COMBINED_TITLE, [
        ("White", 40), ("Black/African American", 10), ("White, Asian/Black", 2),
        ("White, Black/African American", 3)])
    race = extract_race_data(_study("NCT99000572", [table], participants=55))
    readings = {c["original"]: c["flags"] for c in race["raw_categories"]}
    assert "multirace_comma_list" not in readings["White, Asian/Black"]
    assert "multirace_comma_list" in readings["White, Black/African American"]


def test_a_slash_row_naming_either_of_two_races_is_no_evidence_of_a_race_alone() -> None:
    """A review finding: "Asian/Black" was read as Asian (its first slash
    fragment), so it counted as an Asian row and "White, Asian" became more
    than one race. A slash row whose fragments agree still counts."""
    either = _category_table(COMBINED_TITLE, [("Asian/Black", 5), ("White, Asian", 2), ("Other", 3)])
    race = extract_race_data(_study("NCT99000573", [either], participants=10))
    readings = {c["original"]: c["flags"] for c in race["raw_categories"]}
    assert "multirace_comma_list" not in readings["White, Asian"]
    agree = _category_table(COMBINED_TITLE, [("Black/African American", 5), ("White, Black", 2), ("Other", 3)])
    race = extract_race_data(_study("NCT99000574", [agree], participants=10))
    readings = {c["original"]: c["flags"] for c in race["raw_categories"]}
    assert "multirace_comma_list" in readings["White, Black"]


@pytest.mark.parametrize("evidence", [
    "Asian/Other", "Asian/Unknown", "Asian/Multiracial",
    # Non-answers the vocabulary does not hold (review findings).
    "Asian/Refused", "Asian/N/A", "Asian/Not Available", "Asian/Unk", "Asian/NK", "Asian/None",
    "Asian/Not Applicable", "Asian/Not Listed", "Asian/Unavailable",
    # Text the vocabulary does not hold: possibly an alternative. ("Asian/
    # Oriental" was this case until the Asian label rule read "Oriental".)
    "Asian/Zhuang",
])
def test_a_slash_row_with_a_residual_alternative_is_no_evidence_of_a_race_alone(evidence: str) -> None:
    """Review findings: "Other" reads as a category, not as nothing, so
    "Asian/Other" may name Asian or another race and is no Asian row, nor is
    "Asian/Refused", Asian or no answer; the grouped "Asian, Pacific
    Islander" beside it keeps its reading."""
    table = _category_table(COMBINED_TITLE, [(evidence, 5), ("Asian, Pacific Islander", 3), ("Black", 2)])
    race = extract_race_data(_study("NCT99000575", [table], participants=10))
    readings = {c["original"]: c["flags"] for c in race["raw_categories"]}
    assert "multirace_comma_list" not in readings["Asian, Pacific Islander"]


def test_a_slash_row_whose_fragments_all_name_the_race_is_still_evidence() -> None:
    """"White/Cau" (a known abbreviation), "White/Hispanic" (an ethnicity
    term) and "Black/African American, non-Hispanic" (a race part beside an
    ethnicity phrase) each name one race."""
    for nct_id, evidence, combo in [("NCT99000576", "White/Cau", "White, Asian"),
                                    ("NCT99000577", "White/Hispanic", "White, Asian"),
                                    ("NCT99000578", "Black/African American, non-Hispanic", "Black, Asian"),
                                    # Both fragments Asian (block comment "Asian labels").
                                    ("NCT99000613", "Asian/Oriental", "Black, Asian")]:
        table = _category_table(COMBINED_TITLE, [(evidence, 4), (combo, 1), ("Other", 2)])
        race = extract_race_data(_study(nct_id, [table], participants=7))
        readings = {c["original"]: c["flags"] for c in race["raw_categories"]}
        assert "multirace_comma_list" in readings[combo], evidence


# ── American Indian or Alaska Native labels ────────────────────────────────
#
# src/race_extractor.py, block comment "American Indian or Alaska Native labels".

@pytest.mark.parametrize("label", [
    "American Indian",  # 6,113 participants in 114 trials were other
    "American indian",
    "American Indian Only",
    "Am Indian",
    "Alaska Native",
    "Alaskan Native",
    "North American Native",  # 4,257 in 4 US trials
    "Native American or Alaskan Native",
    "Native-American/Alaskan Native",
    "Alaskan Native or American Indian",
    "Native American, American Indian, or Alaskan Native",
    "American Indian (Amer. Ind.) or Alaska Native",
    "American (Amer) Indian (Ind) or Alaska Native (N)",
    "American(Amer) Indian(Ind) or Alaska(Al) Native(N)",
    "American Indian/Alaskan Native",  # was read by the fuzzy match
    "North, South or Central American Native",
    "Native South American",
    "Central American Indian",
    "North American Aboriginal (e.g., First Nations, Inuit, Metis)",
    "Indigenous (First Nations, Métis, non-status)",
    "Canadian Aboriginal",
    "Indigenous Mayan",
    "Indigenous (Brazil)",
    "Navajo",  # quarantined as not race before
    "Navajo Nation",  # a tribal suffix (a review finding)
    "Blackfeet Tribe",
    "American Indian tribal member",
    "White Mountain Apache",
    "Alaskan Native, Yup'ik",
    "Quechua",
    "Amerindian",
])
def test_a_label_naming_american_indian_or_alaska_native_peoples_maps_there(label: str) -> None:
    mapping = rx.map_race_category(label)
    assert mapping["omb_category"] == "american_indian_alaska_native"
    assert mapping["confidence"] == "high" and "aian_label" in mapping["flags"]


@pytest.mark.parametrize("label", [
    # Indigenous peoples outside the Americas stay as they were.
    "Indigenous African", "Aboriginal Australian", "Australian Aboriginal",
    "Torres Strait Islander", "Aboriginal and/or Torres Strait Islander", "Maori", "Indigenous swede",
    # An unqualified word names the trial country's Indigenous people.
    "Aboriginal", "Aborigine", "Native", "Native Indian", "Tribal members", "Native/Aboriginal",
    # A tribal suffix without a name.
    "Nation", "Tribe",
    # "Native" with a country may mean born there.
    "Native Mexican", "Native Canadian",
    # Another group beside, or a crossing with another variable.
    "Native American or Pacific Islander", "Other (including American Indian or Alaska Native)",
    "Native American or Alaskan Native - Usual Care", "Adult Native American",
    # Not American Indian.
    "Indian American", "American", "Latin American", "Maya ethnicity/language preference",
])
def test_a_label_not_naming_only_american_indian_peoples_is_not_read_by_the_rule(label: str) -> None:
    assert not rx.aian_label(label)
    assert "aian_label" not in rx.map_race_category(label)["flags"]


def test_the_vocabulary_entry_indigenous_is_unchanged() -> None:
    """"Indigenous" keeps its vocabulary entry (exact match first)."""
    mapping = rx.map_race_category("Indigenous")
    assert mapping["omb_category"] == "american_indian_alaska_native"
    assert "aian_label" not in mapping["flags"]


def test_a_table_of_tribal_names_is_counted_as_american_indian() -> None:
    """The NCT00121108 shape: tribal names were quarantined as not race (or
    other), leaving the participants to denominator balancing."""
    table = _category_table(COMBINED_TITLE, [
        ("Navajo", 60), ("White Mountain Apache", 15), ("San Carlos Apache", 5),
        ("Hopi", 3), ("Zuni", 1), ("Other - Not specified", 2)])
    race = extract_race_data(_study("NCT99000579", [table], participants=86))
    assert _nonzero(race) == {"american_indian_alaska_native": 84, "other": 2}
    assert race["quarantined_labels"] == [] and race["unknown_inferred"] == 0


def test_a_combination_naming_american_indian_is_now_more_than_one_race() -> None:
    """With "American Indian" read as a race, the multirace rules read the
    combinations that name it (NCT04839978, NCT03692078)."""
    table = _category_table(COMBINED_TITLE, [
        ("White", 50), ("Black", 20), ("American Indian Only", 6),
        ("American Indian and White", 3), ("American Indian, Black, White", 1)])
    race = extract_race_data(_study("NCT99000580", [table], participants=80))
    assert _nonzero(race) == {"white": 50, "black_african_american": 20,
                              "american_indian_alaska_native": 6, "more_than_one_race": 4}


def test_a_middle_eastern_part_is_not_matched_by_a_white_row() -> None:
    """The NCT05997901 shape: "White" beside "Metis, Black, Latino/Latina/
    Latinx, Middle Eastern", a grouping of the others. "Middle Eastern" is
    White in the 1997 categories, but the table lists it apart from White, so
    the White row is no evidence of a race of the list named alone."""
    table = _category_table(COMBINED_TITLE, [
        ("White", 34), ("Métis, Black, Latino/Latina/Latinx, Middle Eastern", 6)])
    race = extract_race_data(_study("NCT99000581", [table], participants=40))
    assert race["omb_totals"]["more_than_one_race"] == 0
    assert not any("multirace_comma_list" in c["flags"] for c in race["raw_categories"])
    # The same without the American Indian part: before the Middle Eastern
    # part had its own key, the White row made this a combination.
    table = _category_table(COMBINED_TITLE, [("White", 34), ("Black, Middle Eastern", 6)])
    race = extract_race_data(_study("NCT99000583", [table], participants=40))
    assert not any("multirace_comma_list" in c["flags"] for c in race["raw_categories"])
    # A Middle Eastern row alone is evidence for a Middle Eastern part.
    table = _category_table(COMBINED_TITLE, [
        ("Arab", 5), ("Arab, African", 1), ("Other", 2)])
    race = extract_race_data(_study("NCT99000582", [table], participants=8))
    assert race["omb_totals"]["more_than_one_race"] == 1


def test_white_and_middle_eastern_name_one_race_in_a_comma_list() -> None:
    assert rx._comma_list_races("White, Middle Eastern") is None


# ── Unqualified Indigenous labels, read by the trial's sites ───────────────
#
# src/race_extractor.py, block comment of that name.

def _study_at(nct_id: str, measures: list[dict[str, Any]], participants: int,
              countries: list[str]) -> dict[str, Any]:
    """_study with site locations in the given countries."""
    study = _study(nct_id, measures, participants)
    study["protocolSection"]["contactsLocationsModule"] = {"locations": [{"country": c} for c in countries]}
    return study


@pytest.mark.parametrize("label, unqualified", [
    ("Indigenous", True), ("Aboriginal", True), ("Aborigine", True), ("Aborigines", True),
    ("Native/Aboriginal", True), ("Native or Aboriginal", True), ("Aboriginal/Native Persons", True),
    ("Indigenous Person", True), ("Indigenous peoples", True), ("Indigenous/Aboriginal", True),
    ("Native", False), ("Natives", False), ("Tribal members", False), ("Native American", False),
    ("Indigenous Mayan", False), ("Aboriginal Australian", False), ("Indigenous African", False),
    ("North American Aboriginal", False), ("White", False),
])
def test_unqualified_indigenous(label: str, unqualified: bool) -> None:
    assert rx._unqualified_indigenous(label) is unqualified


@pytest.mark.parametrize("countries, region", [
    (["Canada"], "americas"), (["United States", "Puerto Rico"], "americas"), (["Brazil", "Chile"], "americas"),
    (["Australia", "New Zealand"], "outside_americas"), (["Guam"], "outside_americas"),
    (["Canada", "Spain", "United States"], "mixed"), (["United States", "American Samoa"], "mixed"),
    ([], "unknown"),
])
def test_trial_site_region(countries: list[str], region: str) -> None:
    study = _study_at("NCT99000584", [], 1, countries)
    assert rx._trial_site_region(study) == region


def test_a_trial_without_locations_has_an_unknown_region() -> None:
    assert rx._trial_site_region(_study("NCT99000585", [], 1)) == "unknown"


INDIGENOUS_TABLE = [("White", 60), ("Black", 10), ("Aboriginal", 8), ("Other", 2)]


@pytest.mark.parametrize("countries, category, flag", [
    (["Canada"], "american_indian_alaska_native", "indigenous_sites_americas"),
    (["Australia"], "other", "indigenous_sites_outside_americas"),
    (["Canada", "Australia"], "other", "indigenous_sites_mixed"),
    ([], "other", "indigenous_sites_unknown"),
])
def test_an_unqualified_indigenous_row_is_read_by_the_trial_sites(
        countries: list[str], category: str, flag: str) -> None:
    """"Aboriginal" in a Canadian trial is the First Nations, Inuit and
    Metis; elsewhere, or where the sites do not place the trial, it is other
    with the reason flagged, not "unmapped"."""
    table = _category_table(COMBINED_TITLE, INDIGENOUS_TABLE)
    race = extract_race_data(_study_at("NCT99000586", [table], 80, countries))
    row = next(c for c in race["raw_categories"] if c["original"] == "Aboriginal")
    assert row["omb_category"] == category and flag in row["flags"]
    assert "unmapped" not in row["flags"]
    assert race["omb_totals"][category] >= 8


def test_the_vocabulary_entry_indigenous_is_read_by_the_trial_sites_too() -> None:
    """"Indigenous" was American Indian wherever posted, also in an
    Australia and New Zealand trial (NCT05610176)."""
    table = _category_table(COMBINED_TITLE, [("White", 40), ("Indigenous", 5), ("Asian", 5)])
    outside = extract_race_data(_study_at("NCT99000587", [table], 50, ["Australia", "New Zealand"]))
    assert outside["omb_totals"]["american_indian_alaska_native"] == 0
    assert outside["omb_totals"]["other"] == 5
    inside = extract_race_data(_study_at("NCT99000587", [table], 50, ["Canada"]))
    assert inside["omb_totals"]["american_indian_alaska_native"] == 5


def test_a_race_by_sex_indigenous_row_is_read_by_the_trial_sites() -> None:
    table = _category_table(COMBINED_TITLE, [("White - Female", 30), ("White - Male", 25),
                                             ("Aboriginal - Female", 3), ("Aboriginal - Male", 2)])
    race = extract_race_data(_study_at("NCT99000588", [table], 60, ["Canada"]))
    assert _nonzero(race) == {"white": 55, "american_indian_alaska_native": 5}


def test_rows_read_without_the_study_are_unchanged() -> None:
    """extract_race_from_measure has no study, so no sites: "Aboriginal" is
    read as before."""
    table = _category_table(COMBINED_TITLE, INDIGENOUS_TABLE)
    row = next(m for m in rx.extract_race_from_measure(table) if m["original"] == "Aboriginal")
    assert row["omb_category"] == "other" and "unmapped" in row["flags"]


# ── Race crossed with baseline group ───────────────────────────────────────
# A study's groups posted as rows of the race table, "<race> - <group>",
# each row's count in its own group's column and the Total column only
# (race_extractor block comment "Race crossed with baseline group"; the
# registry case is NCT02675777). The fictional study has two periods, 60 and
# 40 participants, and a Total group.

PERIODS = [("BG000", "Quality Improvement (ALPHA) Intervention Period"),
           ("BG001", "Usual Care Period")]
TOTAL_GROUP = ("BG002", "Total")


def _group_row(label: str, values: dict[str, Any]) -> dict[str, Any]:
    """A customized-table row posting a value in each named group column."""
    return {"title": label, "categories": [{"measurements": [
        {"groupId": group_id, "value": str(v)} for group_id, v in values.items()]}]}


def _period_row(label: str, group_id: str, n: int) -> dict[str, Any]:
    """A row posted in one period's column and the Total column."""
    return _group_row(label, {group_id: n, TOTAL_GROUP[0]: n})


def _grouped_table(rows: list[dict[str, Any]], title: str = COMBINED_TITLE,
                   unit: str = "participants") -> dict[str, Any]:
    return {"title": title, "paramType": "COUNT_OF_PARTICIPANTS", "unitOfMeasure": unit,
            "classes": rows}


def _grouped_study(nct_id: str, measures: list[dict[str, Any]], key: str = "id",
                   groups: Optional[list[tuple[str, str]]] = None) -> dict[str, Any]:
    """A study with two period groups (60 and 40) and a Total group (100).
    key is the group id's key: "id" as the registry returns it, "groupId" as
    the other fixtures in this file post it."""
    pairs = groups if groups is not None else PERIODS + [TOTAL_GROUP]
    sizes = {pairs[0][0]: 60, pairs[1][0]: 40, pairs[-1][0]: 100}
    baseline: dict[str, Any] = {
        "groups": [{key: group_id, "title": title} for group_id, title in pairs],
        "denoms": [{"units": "Participants", "counts": [
            {"groupId": group_id, "value": str(n)} for group_id, n in sizes.items()]}],
        "measures": measures}
    return {"protocolSection": {"identificationModule": {"nctId": nct_id},
                                "designModule": {"enrollmentInfo": {"count": 100}}},
            "resultsSection": {"baselineCharacteristicsModule": baseline}}


RACE_BY_PERIOD = [
    _period_row("White - ALPHA", "BG000", 40),
    _period_row("White - Usual Care", "BG001", 25),
    _period_row("Black or African American - ALPHA", "BG000", 15),
    _period_row("Black or African American - Usual Care", "BG001", 10),
    _period_row("Unknown - ALPHA", "BG000", 5),
    _period_row("Unknown - Usual Care", "BG001", 5),
]


@pytest.mark.parametrize("label, expected", [
    ("White - ALPHA", ("White", "BG000")),
    ("Asian - Usual Care", ("Asian", "BG001")),
    ("Black or African American – Usual Care", ("Black or African American", "BG001")),
    ("Asian - East Asian Heritage - ALPHA", ("Asian - East Asian Heritage", "BG000")),  # last dash
    ("White - usual care, %", ("White", "BG001")),  # cleaned, any case
])
def test_a_race_by_group_label_splits_into_race_part_and_group(
        label: str, expected: tuple[str, str]) -> None:
    assert rx.split_race_by_group_label(label, PERIODS) == expected


@pytest.mark.parametrize("label", [
    "White",
    "White/Caucasian",
    "White-ALPHA",       # no spaced dash
    "White - Period",    # names both groups
    "White - Placebo",   # names no group
    "White - Total",     # the whole-population group is not a column
    "- ALPHA",           # nothing before the dash
    "ALPHA - White",     # the reverse order is not read
])
def test_a_label_naming_no_single_group_does_not_split(label: str) -> None:
    assert rx.split_race_by_group_label(label, PERIODS) is None


def test_group_columns_leave_out_the_one_whole_population_group() -> None:
    assert rx._baseline_group_columns(_grouped_study("NCT99000589", [])) == PERIODS
    # Two whole-population titles: neither is taken for the Total, so a row
    # repeated in a Total column fails the posting test.
    two = PERIODS + [("BG002", "Total"), ("BG003", "Overall")]
    assert rx._baseline_group_columns(_grouped_study("NCT99000589", [], groups=two)) == two


@pytest.mark.parametrize("key", ["id", "groupId"])
def test_a_race_by_group_table_is_read_by_its_race_parts(key: str) -> None:
    """Before, every row was mapped whole: "White - ALPHA" and the other rows
    reached a race only by fuzzy match, if at all."""
    race = extract_race_data(_grouped_study("NCT99000590", [_grouped_table(RACE_BY_PERIOD)], key=key))
    assert _nonzero(race) == {"white": 65, "black_african_american": 25, "unknown_not_reported": 10}
    assert "race_by_group_table" in race["flags"] and "race_by_sex_table" not in race["flags"]
    white = [c for c in race["raw_categories"] if c["omb_category"] == "white"]
    assert [(c["original"], c["race_part"], c["group"], c["count"]) for c in white] == [
        ("White - ALPHA", "White", PERIODS[0][1], 40),
        ("White - Usual Care", "White", PERIODS[1][1], 25)]
    for cat in race["raw_categories"]:
        assert "race_by_group" in cat["flags"] and "sex" not in cat
        assert not any(flag.startswith("fuzzy_match") for flag in cat["flags"])


@pytest.mark.parametrize("rows", [
    pytest.param([_group_row("White - ALPHA", {"BG000": 20, "BG001": 20, "BG002": 40}),
                  _group_row("White - Usual Care", {"BG000": 30, "BG001": 30, "BG002": 60})],
                 id="counts in both columns"),
    pytest.param([_group_row("White - ALPHA", {"BG000": 55, "BG002": 60}),
                  _period_row("White - Usual Care", "BG001", 40)],
                 id="count read is not the column's"),
    pytest.param([_group_row("White - ALPHA", {"BG000": 40, "BG001": 5, "BG002": 40}),
                  _period_row("White - Usual Care", "BG001", 60)],
                 id="a number in another column"),
    pytest.param([_period_row("White - ALPHA", "BG000", 50),
                  _period_row("Black - ALPHA", "BG000", 10),
                  _period_row("Black", "BG001", 40)],
                 id="a row naming no group"),
    pytest.param([_period_row("White - ALPHA", "BG000", 50),
                  _period_row("Black - ALPHA", "BG000", 50)],
                 id="one group named"),
])
def test_a_table_failing_a_race_by_group_test_is_read_as_plain_rows(
        rows: list[dict[str, Any]]) -> None:
    race = extract_race_data(_grouped_study("NCT99000591", [_grouped_table(rows)]))
    assert "race_by_group_table" not in race["flags"]
    assert all("group" not in c and "race_by_group" not in c["flags"] for c in race["raw_categories"])


def test_a_percentage_table_is_not_read_by_group() -> None:
    rows = [_period_row("White - ALPHA", "BG000", 60), _period_row("White - Usual Care", "BG001", 40)]
    race = extract_race_data(_grouped_study("NCT99000592", [_grouped_table(rows, unit="% of participants")]))
    assert "race_by_group_table" not in race["flags"]


def test_rows_posted_in_one_arm_but_naming_races_only_stay_plain() -> None:
    """Groups defined by race: each row's count is in one column, but no row
    names a group, so the table is read as it was."""
    groups = [("BG000", "White Adults"), ("BG001", "Black Adults"), TOTAL_GROUP]
    rows = [_period_row("White", "BG000", 60), _period_row("Black or African American", "BG001", 40)]
    race = extract_race_data(_grouped_study("NCT99000593", [_grouped_table(rows)], groups=groups))
    assert _nonzero(race) == {"white": 60, "black_african_american": 40}
    assert "race_by_group_table" not in race["flags"]


def test_a_race_by_sex_reading_is_kept_when_the_groups_are_the_sexes() -> None:
    """Rows that pass both tests are read by sex, as before."""
    groups = [("BG000", "Female Participants"), ("BG001", "Male Participants"), TOTAL_GROUP]
    rows = [_period_row("White - Female", "BG000", 45), _period_row("Asian - Female", "BG000", 15),
            _period_row("White - Male", "BG001", 30), _period_row("Asian - Male", "BG001", 10)]
    race = extract_race_data(_grouped_study("NCT99000594", [_grouped_table(rows)], groups=groups))
    assert _nonzero(race) == {"white": 75, "asian": 25}
    assert "race_by_sex_table" in race["flags"] and "race_by_group_table" not in race["flags"]
    assert all("group" not in c for c in race["raw_categories"])


def test_an_ethnicity_row_of_a_race_by_group_table_is_recorded_with_its_group() -> None:
    rows = [_period_row("White - ALPHA", "BG000", 40),
            _period_row("Hispanic or Latino - ALPHA", "BG000", 20),
            _period_row("White - Usual Care", "BG001", 30),
            _period_row("Hispanic or Latino - Usual Care", "BG001", 10)]
    race = extract_race_data(_grouped_study("NCT99000595", [_grouped_table(rows)]))
    assert race["omb_totals"]["white"] == 70
    assert [(r["original"], r["race_part"], r["group"], r["count"])
            for r in race["skipped_ethnicity_rows"]] == [
        ("Hispanic or Latino - ALPHA", "Hispanic or Latino", PERIODS[0][1], 20),
        ("Hispanic or Latino - Usual Care", "Hispanic or Latino", PERIODS[1][1], 10)]
    for row in race["skipped_ethnicity_rows"]:
        assert "race_by_group" in row["flags"] and "race_by_sex" not in row["flags"]
        assert "sex" not in row


def test_a_race_by_group_table_whose_race_parts_do_not_map_is_read_as_plain_rows() -> None:
    """As for a race-by-sex table: when no race part maps, the rows are read
    whole. "Native Hawaiian or Other" pools Native Hawaiian with other races
    and maps to no race (block comment "Native Hawaiian or Pacific Islander
    labels")."""
    rows = [_period_row("Native Hawaiian or Other - ALPHA", "BG000", 60),
            _period_row("Native Hawaiian or Other - Usual Care", "BG001", 40)]
    race = extract_race_data(_grouped_study("NCT99000596", [_grouped_table(rows)]))
    assert race["omb_totals"]["other"] == 100
    assert "race_by_group_table" not in race["flags"]
    assert [c["original"] for c in race["raw_categories"]] == [
        "Native Hawaiian or Other - ALPHA", "Native Hawaiian or Other - Usual Care"]
    assert all("group" not in c and "race_part" not in c for c in race["raw_categories"])


# ── Native Hawaiian or Pacific Islander labels ─────────────────────────────
# Wordings naming only Native Hawaiian or Pacific Islander peoples
# (race_extractor block comment "Native Hawaiian or Pacific Islander
# labels"). Before, "Hawaiian or Pacific Islander" and "Hawaiian-Pacific" were
# counted as other, and "Marshallese", "Maori" and "Polynesian" quarantined as
# not race; "Tonga" was read as Tongan by fuzzy match.

@pytest.mark.parametrize("label, subcategory", [
    ("Hawaiian or Pacific Islander", None),
    ("Hawaiian-Pacific", None),
    ("Hawaiian Native or Pacific Islander", None),
    ("Native Hawaiian or other Pacific", None),          # a label cut short
    ("Other Pacific Islander", None),
    ("Other-Pacific Islander", None),
    ("South Pacific Islander", None),
    ("Pacific Peoples", None),
    ("Pacific Island Native", None),
    ("Hawaiian or Pacific Islander only", None),
    ("Native Hawaiian or other Pacific ethnicity", None),
    ("Native Hawaiin/ PI", None),                        # posted misspelling, abbreviation
    ("Native Hawaiian (NH) or other Pacific Islander", None),
    ("Native Hawaiian or Other Paciﬁc Islander", None),  # "fi" ligature
    ("Marshallese", None),
    ("Maori", None),
    ("Māori", None),
    ("Maori/Polynesian", None),
    ("Micronesian", None),
    ("Tahitian", None),
    ("Native Hawaiians", "native_hawaiian"),
    ("Samoans", "samoan"),
    ("Guamanian or Chamorro", "guamanian_chamorro"),
    ("Samoan/Tongan", None),
])
def test_a_label_naming_pacific_peoples_only_is_native_hawaiian_or_pacific_islander(
        label: str, subcategory: Optional[str]) -> None:
    assert rx.nhpi_label(label) == (True, subcategory)
    mapping = rx.map_race_category(label)
    assert mapping["omb_category"] == "native_hawaiian_pacific_islander"
    assert mapping["subcategory"] == subcategory


@pytest.mark.parametrize("label", [
    "Native Hawaiian or Other",              # pools other races
    "Native Hawaiian or Some other race(s)",
    "Asian or Pacific Islander",             # the 1977 grouped category
    "Asian and/or Pacific Islander",
    "Native American or Pacific Islander",
    "Pacific Islander-White",
    "Adolescent Native Hawaiian or Pacific Islander",
    "Torres Strait Islander",                # kept with Aboriginal Australian
    "Islander",
    "Pacific",
    "Native",
    "Other: Maori",
    "Indian (Fiji)",
    "Tonga",
    "/or Pacific Islander",                  # a piece of a longer label
    "Native Hawaiian or",
])
def test_a_label_naming_more_or_less_than_pacific_peoples_is_not_read_by_the_rule(label: str) -> None:
    assert rx.nhpi_label(label) == (False, None)


def test_the_vocabulary_entries_keep_their_subcategory() -> None:
    assert rx.map_race_category("Native Hawaiian")["subcategory"] == "native_hawaiian"
    assert rx.map_race_category("Samoan")["flags"] == []


def test_a_slash_label_naming_hawaiian_and_pacific_islander_has_no_single_subcategory() -> None:
    """Before, the slash fragment "Hawaiian" made it Native Hawaiian."""
    mapping = rx.map_race_category("Hawaiian/Pacific Islander")
    assert (mapping["omb_category"], mapping["subcategory"]) == ("native_hawaiian_pacific_islander", None)
    assert mapping["flags"] == ["nhpi_label"]


def test_tonga_is_not_read_as_tongan_and_is_not_quarantined() -> None:
    """The Tonga of Zambia and Zimbabwe (NCT01241318: 34,653 participants at
    Zambian sites) were read as Tongan by fuzzy match."""
    mapping = rx.map_race_category("Tonga")
    assert mapping["omb_category"] == "other"
    assert "fuzzy_match_withheld" in mapping["flags"] and "unmapped" in mapping["flags"]
    assert not rx._should_quarantine(mapping)
    table = _category_table(COMBINED_TITLE, [("Tonga", 90), ("Other", 10)])
    race = extract_race_data(_study_at("NCT99000605", [table], 100, ["Zambia"]))
    assert _nonzero(race) == {"other": 100}
    assert race["quarantined_labels"] == []


def test_pacific_rows_of_a_table_are_counted() -> None:
    table = _category_table(COMBINED_TITLE, [
        ("White", 50), ("Hawaiian or Pacific Islander", 20), ("Maori", 12), ("Marshallese", 8),
        ("Torres Strait Islander", 4), ("Asian", 6)])
    race = extract_race_data(_study("NCT99000606", [table], 100))
    assert _nonzero(race) == {"white": 50, "native_hawaiian_pacific_islander": 40, "asian": 6,
                              "unknown_not_reported": 4}
    # Torres Strait Islander is quarantined as before; its 4 are balanced to unknown.
    assert [q["original"] for q in race["quarantined_labels"]] == ["Torres Strait Islander"]


@pytest.mark.parametrize("joined", [
    "Asian and Native Hawaiian or Pacific Islander",          # NCT01912287
    "Asian and Hawaiian or Pacific Islander",                 # wordings this rule reads (review finding)
    "Asian & Hawaiian-Pacific",
    "Native Hawaiian or Pacific Islander and Asian",          # either order
    "Asian and Native Hawaiian and Other Pacific Islander",   # a category name holding "and"
])
def test_asian_joined_to_pacific_islander_is_the_grouped_category_without_asian_alone(joined: str) -> None:
    """NCT01912287's table: no Asian or Pacific row besides the joined one."""
    table = _category_table(COMBINED_TITLE, [
        ("American Indian or Alaska Native", 2), (joined, 21),
        ("Black or African American", 11), ("White", 66)])
    race = extract_race_data(_study("NCT99000607", [table], 100))
    # The group is read as Asian (block comment "The 1977 Asian or Pacific
    # Islander category"; it was other before).
    assert race["omb_totals"]["asian"] == 21 and race["omb_totals"]["more_than_one_race"] == 0
    assert race["omb_totals"]["other"] == 0
    row = next(c for c in race["raw_categories"] if c["original"] == joined)
    assert "asian_pacific_grouped" in row["flags"]


@pytest.mark.parametrize("label", [
    "Asian and White", "Asian and Native Hawaiian or Pacific Islander and White",
    # a part naming a people, not the category (review finding)
    "Asian and Samoan", "Samoan and Asian", "Asian and Native Hawaiian", "Asian and Maori"])
def test_other_combinations_naming_asian_are_not_the_grouped_category(label: str) -> None:
    table = _category_table(COMBINED_TITLE, [(label, 21), ("Black or African American", 11), ("White", 68)])
    race = extract_race_data(_study("NCT99000610", [table], 100))
    assert race["omb_totals"]["more_than_one_race"] == 21


@pytest.mark.parametrize("asian_row", [
    "Asian", "Japanese/East Asian Heritage (AH)/South East AH", "Native Hawaiian or Pacific Islander"])
def test_asian_joined_to_pacific_islander_is_a_combination_beside_either_race_alone(asian_row: str) -> None:
    table = _category_table(COMBINED_TITLE, [
        (asian_row, 20), ("Asian & Native Hawaiian or Other Pacific Islander", 2),
        ("Asian and White", 3), ("White", 75)])
    race = extract_race_data(_study("NCT99000608", [table], 100))
    assert race["omb_totals"]["more_than_one_race"] == 5
    assert not any("asian_pacific_grouped" in c["flags"] for c in race["raw_categories"])


def test_a_slash_row_naming_asian_and_pacific_islander_is_not_evidence_of_asian_alone() -> None:
    """"Asian/Pacific Islander" is read as Asian through a slash fragment, but it
    names the grouped category too: no evidence."""
    table = _category_table(COMBINED_TITLE, [
        ("Asian/Pacific Islander", 10), ("Asian and Native Hawaiian or Pacific Islander", 5), ("White", 85)])
    race = extract_race_data(_study("NCT99000609", [table], 100))
    assert race["omb_totals"]["more_than_one_race"] == 0
    assert race["omb_totals"]["asian"] == 15  # both the group (block comment "The 1977 ...")
    assert race["omb_totals"]["other"] == 0


def test_a_combination_naming_pacific_islander_and_white_is_more_than_one_race() -> None:
    """Before, the fuzzy match read it as Native Hawaiian or Pacific Islander
    (NCT02712047)."""
    mapping = rx.map_race_category("Native Hawaiin or other Pacific Islander and White")
    assert mapping["omb_category"] == "more_than_one_race"


# ── European labels ────────────────────────────────────────────────────────
# Wordings naming only European origin (race_extractor block comment
# "European labels"). Before, "White (European)", "White or European
# American" and "Western European White" were counted as other, and "North/
# Mid/East European", "South European" and the New Zealand census wordings
# ("NZ European/Pākehā", "New Zealand European") quarantined as not race.

@pytest.mark.parametrize("label, subcategory", [
    ("White (European)", "european"),
    ("North/Mid/East European", "european"),
    ("North/Middle European", "european"),
    ("South European", "european"),
    ("Southern European", "european"),
    ("Scandinavian/Central European", "european"),
    ("White or European American", "european"),
    ("Caucasian, White, or European American", "european"),
    ("White -White or Caucasian or European", "european"),
    ("European (e.g., British, French, Western European, Eastern European)", "european"),
    ("Caucasian (European descent)", "european"),
    ("European Ancestry", "european"),
    ("White or of European background", "european"),
    ("White-European", "european"),
    ("Other European", "european"),
    ("Europe", "european"),
    ("NZ European/Pākehā", "european"),
    ("NZ European/Pakeha", "european"),
    ("New Zealand European", "european"),
    ("NZ European", "european"),
    ("New Zealander, European", "european"),
    ("New Zealander, of European descent", "european"),   # fillers are passed over
    ("Western European White", "european_western"),
    ("White: Western European", "european_western"),
    ("Eastern European White", "european_eastern"),
    ("White: Eastern European", "european_eastern"),
    ("Western or Eastern European White", "european"),
])
def test_a_label_naming_european_origin_only_is_white(label: str, subcategory: str) -> None:
    assert rx.european_label(label) == (True, subcategory)
    mapping = rx.map_race_category(label)
    assert (mapping["omb_category"], mapping["subcategory"]) == ("white", subcategory)
    assert mapping["flags"] == ["european_label"]


@pytest.mark.parametrize("label, subcategory", [
    # a direction names the subcategory only when it qualifies every group (review finding)
    ("European or Western European", "european"),
    ("European, Eastern European", "european"),
    ("White or Eastern European", "european"),
    ("White and Eastern European", "european"),
    ("Eastern European/White", "european"),
    ("Western or Eastern European White", "european"),
    ("White - Eastern European", "european_eastern"),   # White describing the group
    ("Eastern European White", "european_eastern"),
    ("Other Western European", "european_western"),
    ("Eastern European or British", "european"),        # a nationality is a group too (review finding)
    ("British or Western European", "european"),
])
def test_a_direction_names_the_subcategory_only_when_it_qualifies_every_group(
        label: str, subcategory: str) -> None:
    assert rx.european_label(label) == (True, subcategory)


@pytest.mark.parametrize("label", [
    "Western background (European countries (excluding Turkey), North America, Japan, Indonesia,Oceania)",
    "Other: Brazil, China, Mexico, MENA, Sub-Saharan Africa, Latin America, Europe, Asia, Oceania",
    "White(origins of Europe, Middle East, or North Africa)",  # names the Middle East and North Africa
    "Middle European/Middle Eastern",
    "Middle Eastern European",                                 # "Middle East" is not a direction
    "Not Collected - European Participants",                   # a non-answer
    "Non-White/European descent",
    "Caucasian, non-European descent",
    "Other (European/Arab)",
    "African-European",
    "European or Other",                                       # pools other origins
    "New Zealander",                                           # a nationality outside Europe
    "White",                                                   # no European name
    "/or European",                                            # a piece of a longer label
    # a qualifier that qualifies no European name (review finding)
    "European or New Zealander",
    "New Zealander or European",
    "European/NZ",
    "European or Western",
    "North or European",                                       # a direction does not cross "or" (review finding)
    "North/European",
])
def test_a_label_naming_more_or_less_than_european_origin_is_not_read_by_the_rule(label: str) -> None:
    assert rx.european_label(label) == (False, None)


@pytest.mark.parametrize("label, subcategory", [
    # every accepted join between alternatives counts (review finding)
    ("White & Eastern European", "european"),
    ("Caucasian; Western European", "european"),
    ("White or Eastern European", "european"),
    ("North & East European", "european"),
    ("White: Eastern European", "european_eastern"),   # ":" describes
    ("White - Western European", "european_western"),  # a dash describes
    # a filler does not hide an unqualified alternative (review finding)
    ("White people or Eastern European", "european"),
    ("Caucasian background & Western European", "european"),
])
def test_the_subcategory_of_a_mapped_row_counts_every_alternative_join(
        label: str, subcategory: str) -> None:
    mapping = rx.map_race_category(label)
    assert (mapping["omb_category"], mapping["subcategory"], mapping["flags"]) == (
        "white", subcategory, ["european_label"])


@pytest.mark.parametrize("label, subcategory, flags", [
    ("European", "european", []),                                   # vocabulary entry
    ("Eastern European", "european_eastern", []),
    ("White - White/Caucasian/European Heritage", None, ["slash_normalized"]),
    ("White/European", None, ["slash_normalized"]),        # the rule would say european
    ("Caucasian/European", None, ["slash_normalized"]),
    ("Middle Eastern/European", "middle_eastern", ["slash_normalized"]),
])
def test_readings_before_the_rule_are_kept(label: str, subcategory: Optional[str],
                                           flags: list[str]) -> None:
    mapping = rx.map_race_category(label)
    assert (mapping["omb_category"], mapping["subcategory"], mapping["flags"]) == ("white", subcategory, flags)


def test_new_zealand_census_rows_are_counted_as_white() -> None:
    table = _category_table(COMBINED_TITLE, [
        ("NZ European/Pākehā", 60), ("New Zealand European", 10), ("Asian", 20), ("Other", 10)])
    race = extract_race_data(_study_at("NCT99000611", [table], 100, ["New Zealand"]))
    assert _nonzero(race) == {"white": 70, "asian": 20, "other": 10}
    assert race["quarantined_labels"] == []
    assert race["subcategory_totals"] == {"white_european": 70}


def test_quarantined_european_rows_are_counted_and_so_is_the_roma_row() -> None:
    """"Roma European" stayed quarantined until the maintainer's decision to
    read Roma groups as White (section "European nationalities and Roma
    groups" below)."""
    table = _category_table(COMBINED_TITLE, [
        ("North/Mid/East European", 50), ("South European", 30), ("Roma European", 5), ("Other", 15)])
    race = extract_race_data(_study("NCT99000612", [table], 100))
    assert _nonzero(race) == {"white": 85, "other": 15}
    assert race["quarantined_labels"] == []
    assert race["subcategory_totals"].get("white_european") == 80  # the Roma row has none


def test_a_heritage_row_naming_caucasian_european_carries_the_european_subcategory() -> None:
    """The heritage rule reads its group through map_race_category, which now
    reads "Caucasian European" (NCT00553358: 282 participants)."""
    mapping = rx.map_race_category("White - Caucasian European Heritage")
    assert (mapping["omb_category"], mapping["subcategory"]) == ("white", "european")



# ── Asian labels ───────────────────────────────────────────────────────────
#
# Fictional studies NCT99000613–625 (block comment "Asian labels" in
# src/race_extractor.py). Each label comes from the 2026-11-15 inputs, where
# it was counted as other or quarantined as not race unless noted.

@pytest.mark.parametrize("label, subcategory", [
    ("Asian (Japanese)", "japanese"),
    ("Asian(Japanese)", "japanese"),
    ("Asian-Chinese", "chinese"),
    ("Asian - Japanese", "japanese"),
    ("Asian: Korean", "korean"),
    ("Asian : Pakistani", "south_asian_pakistani"),
    ("Asian: East", "east_asian"),
    ("Asian - South East Asian", "southeast_asian"),
    ("Asian (Southeast)", "southeast_asian"),
    ("North East Asian", "east_asian"),
    ("Other Asian", None),
    ("Asian Other", None),
    ("Any other Asian background", None),
    ("Asian or Asian American", None),
    ("American Asian", None),
    ("Asian Decent", None),
    ("Race,Asian", None),
    ("Asian (Total)", None),
    ("Oriental", None),
    ("Oriental (Japanese)", "japanese"),
    ("Han", "chinese"),
    ("Han Chinese", "chinese"),
    ("Nationality: Han", "chinese"),
    ("Ethnic Chinese", "chinese"),
    ("Japanese Origin", "japanese"),
    ("Japanese from Japan", "japanese"),
    ("Number of South Korean participants", "korean"),
    ("Malay", "southeast_asian"),
    ("Malaysian Malay", "southeast_asian_malaysian"),
    ("Sri Lankan", "south_asian"),
    ("Nepalese", "south_asian"),
    ("Hmong", "southeast_asian"),
    ("Javanese", "southeast_asian_indonesian"),  # fuzzy-matched to Japanese before
    ("East Indian", "south_asian_indian"),
    ("Indian Subcontinent", "south_asian"),
    ("Indian (Indian subcontinent)", "south_asian_indian"),
    ("West Asian (Indian sub-continent)", "south_asian"),
    ("West Asian (Indian sub continent", "south_asian"),  # posted without its bracket closed
    ("Asian, Bangladeshi", "south_asian_bangladeshi"),
    ("Asian, Non-Japanese", None),
    ("Asian (China Mainland)", "chinese"),
])
def test_a_label_naming_asian_groups_only_is_read_as_asian(label: str,
                                                           subcategory: Optional[str]) -> None:
    assert rx.asian_label(label) == (True, subcategory)


@pytest.mark.parametrize("label, subcategory", [
    # Within one alternative, the term every other one refines.
    ("East Asian (Chinese)", "chinese"),
    ("Southeast Asian (Filipino)", "filipino"),
    ("Asian (Only Indian)", "south_asian_indian"),
    # Else the region the terms all lie in.
    ("South Asian (India, Pakistan, Bangladesh, etc.)", "south_asian"),
    ("Asian (Chinese, Japanese)", "east_asian"),
    ("Chinese in Taiwan", "east_asian"),
    ("Malaysian Chinese", None),
    # Across alternatives, theirs when all agree; the region when none is unqualified.
    ("Indian or Pakistani", "south_asian"),
    ("Thai and Southeast Asian", "southeast_asian"),
    ("South Asian or Indian American", "south_asian"),
    ("Asian or Asian Indian", None),
    ("East/South East Asian", None),
    ("Asian: Central/South", None),
    # Examples, "other" in a bracket and a Central direction name no subgroup.
    ("Asian (including Indian)", None),
    ("Asian (China, Philippines, Vietnam, Korea, etc.)", None),
    ("East Asian (Korean, Japanese, Chinese, etc.)", "east_asian"),
    ("Asian Eastern (Chinese, other)", "east_asian"),
    ("Other North East Asian", "east_asian"),
    ("Asian - Central South Asian", None),
    # A negation takes the names after it out of the reading.
    ("Asian (other than Chinese or Japanese)", None),
    ("Asian Non Chinese", None),
    ("Asian - Not Japanese", None),
    ("Oriental (Asian other than Chinese or Japanese)", None),
    # A bare "Asian" heads what follows its comma only in a two-part label.
    ("Asian, Chinese", "chinese"),
    ("Asian, Chinese, Japanese", None),
])
def test_the_subcategory_comes_from_the_terms_that_specify_each_alternative(
        label: str, subcategory: Optional[str]) -> None:
    assert rx.asian_label(label) == (True, subcategory)


@pytest.mark.parametrize("label", [
    "Asian (Comparison 1)",  # crossed with a comparison
    "Asian (11 to 18 years)",  # crossed with an age band
    "South Asian Infants",
    "Japanese 1st Generation",
    "East Asian (enrolled in an East Asian country)",
    "Asian or Other",  # an alternative naming no group
    "Asian and Others",
    "Other (Asian, multiracial, unknown)",
    "Non-Asian",  # negated only
    "Non-Chinese",
    "Other (Not Chinese)",
    "Asian (non-Japanese)-",  # a piece of a longer label
    "/Asian",
    "Asian or Pacific Islander",  # the 1977 grouped category
    "Asian Pacific Islander",
    "Asian or Asian British",  # UK census: a rule of its own
    "Asian - Mixed Race",
    "Mixed Asian",
    "Hui Chinese",  # a Chinese minority the rule does not name
    "Indian ancestry",  # "Indian" in no Asian company
    "Indian (American)",
    "Native or Indian American",
    "West Indian",  # the Caribbean
    "Asian or West Indian",
    "West Asian",  # read only by its table (_west_asian_template_rows)
    "Western Asian",
    "South or West Asian",
    "Arab or West Asian",
    "China",  # a place describes a group; alone it names none
    "Asian or Japan",
    "East or Asian",  # a direction alone, with no noun beside it to share
    "Asian or East",
])
def test_a_label_naming_more_or_less_than_asian_groups_is_not_read_by_the_rule(label: str) -> None:
    assert rx.asian_label(label) == (False, None)


@pytest.mark.parametrize("label, subcategory, flags", [
    ("Asian", None, []),  # exact
    ("Chinese", "chinese", []),
    ("Asian Indian", "south_asian_indian", []),
    ("Asian/Oriental", None, ["slash_normalized"]),  # the slash fragments run first
    ("Asian (Japanese)", "japanese", ["asian_label"]),
    ("Asian American", None, ["asian_label"]),  # fuzzy-matched to African American before
    ("Asian - East Asian", "east_asian", ["asian_label"]),  # no heritage word
    ("ASIAN - SOUTH EAST ASIAN HERITAGE", "southeast_asian", ["heritage_label"]),
])
def test_asian_readings_through_map_race_category(label: str, subcategory: Optional[str],
                                                  flags: list[str]) -> None:
    mapping = rx.map_race_category(label)
    assert (mapping["omb_category"], mapping["subcategory"], mapping["flags"]) == ("asian", subcategory, flags)


def test_asian_american_beside_pacific_islander_stays_the_grouped_category() -> None:
    """"Asian American" now reads as Asian, so the multirace rule would read
    "Asian American and Pacific Islander" as two races; with no row naming
    either apart it is the grouped category (_asian_joined_to_nhpi)."""
    table = _category_table(COMBINED_TITLE, [
        ("White/Caucasian", 20), ("Black/African American", 5),
        ("Asian American and Pacific Islander", 3), ("Other", 2)])
    race = extract_race_data(_study("NCT99000614", [table], 30))
    row = next(c for c in race["raw_categories"] if c["original"] == "Asian American and Pacific Islander")
    assert row["omb_category"] == "asian" and "asian_pacific_grouped" in row["flags"]


def test_a_trial_whose_only_race_row_names_an_asian_people_reports_race() -> None:
    """The NCT01146496 shape: one row, "Sri Lankan", quarantined before."""
    table = _class_table(COMBINED_TITLE, [("Sri Lankan", 200)])
    race = extract_race_data(_study("NCT99000615", [table], 200))
    assert race["reported"]
    assert _nonzero(race) == {"asian": 200}
    assert race["subcategory_totals"] == {"asian_south_asian": 200}
    assert race["quarantined_labels"] == []


LILLY_SHAPE = [("Caucasian", 120), ("East Asian", 30), ("African", 10), ("Native American", 2)]


@pytest.mark.parametrize("west", ["West Asian", "Western Asian"])
def test_west_asian_in_the_template_table_is_read_as_south_asian(west: str) -> None:
    table = _class_table(COMBINED_TITLE, LILLY_SHAPE + [(west, 38)])
    race = extract_race_data(_study("NCT99000616", [table], 200))
    row = next(c for c in race["raw_categories"] if c["original"] == west)
    assert (row["omb_category"], row["subcategory"], row["confidence"]) == ("asian", "south_asian", "medium")
    assert "west_asian_template" in row["flags"] and "unmapped" not in row["flags"]
    assert _nonzero(race)["asian"] == 68


@pytest.mark.parametrize("beside", [
    ("South Asian", 5),  # South Asia listed apart: West Asian is the Middle East
    ("Arabian Peninsula/Arab Middle East", 5),
    ("Chinese", 5),  # a national origin: not the template (VBI's Canadian census)
])
def test_west_asian_beside_another_reading_of_asia_is_not_read(beside: tuple[str, int]) -> None:
    table = _class_table(COMBINED_TITLE, LILLY_SHAPE + [("West Asian", 38), beside])
    race = extract_race_data(_study("NCT99000617", [table], 205))
    row = next(c for c in race["raw_categories"] if c["original"] == "West Asian")
    assert row["omb_category"] == "other" and "west_asian_template" not in row["flags"]


def test_an_american_indian_row_does_not_veto_the_template() -> None:
    """Review finding: "American Indian or Alaska Native" names the
    Americas, not India, so beside it the template's West Asian is read."""
    table = _class_table(COMBINED_TITLE, [("Caucasian", 120), ("East Asian", 30), ("African", 10),
                                          ("American Indian or Alaska Native", 2), ("West Asian", 38)])
    race = extract_race_data(_study("NCT99000622", [table], 200))
    row = next(c for c in race["raw_categories"] if c["original"] == "West Asian")
    assert (row["omb_category"], row["subcategory"]) == ("asian", "south_asian")


def test_west_asian_without_an_east_asian_row_is_not_read() -> None:
    table = _class_table(COMBINED_TITLE, [("Caucasian", 120), ("African", 10), ("West Asian", 38)])
    race = extract_race_data(_study("NCT99000618", [table], 168))
    row = next(c for c in race["raw_categories"] if c["original"] == "West Asian")
    assert row["omb_category"] == "other"


@pytest.mark.parametrize("rows, total, counted, left_out", [
    # Two classifications of the same people: the later one is the breakdown.
    ([("Chinese", 44), ("Han", 44)], 44, {"asian": 44}, ["Han"]),
    ([("Asian", 32), ("Japanese Ancestry", 32)], 32, {"asian": 32}, ["Japanese Ancestry"]),
    # A total the rule reads beside its own breakdown: the breakdown is left out.
    ([("Asian (Japanese)", 4), ("Asian (Except for Japanese)", 0), ("Asian (Total)", 4),
      ("Black or African American", 2), ("White", 11), ("Other", 1)], 18,
     {"asian": 4, "black_african_american": 2, "white": 11, "other": 1},
     ["Asian (Japanese)", "Asian (Except for Japanese)"]),
    # A total the rule reads beside a breakdown read before it: the total is.
    ([("Asian (Total)", 9), ("Chinese", 4), ("Japanese", 5), ("White", 11)], 20,
     {"asian": 9, "white": 11}, ["Asian (Total)"]),
    # Of two equal rows, the one read before the rule is the parent.
    ([("Han", 40), ("Chinese", 40), ("Other Asian", 0)], 40, {"asian": 40}, ["Han", "Other Asian"]),
])
def test_an_asian_breakdown_of_another_row_is_not_counted_again(
        rows: list[tuple[str, int]], total: int, counted: dict[str, int], left_out: list[str]) -> None:
    race = extract_race_data(_study("NCT99000619", [_class_table(COMBINED_TITLE, rows)], total))
    assert _nonzero(race) == counted
    assert [q["original"] for q in race["quarantined_labels"]] == left_out
    assert all(q["reason"] == "asian_breakdown" for q in race["quarantined_labels"])


@pytest.mark.parametrize("rows, total", [
    # Over its population by less than the breakdown (NCT01829295): as posted.
    ([("White", 47), ("Black", 9), ("Asian", 11), ("Indian Subcontinent", 139),
      ("Native American", 3), ("Pacific Islander", 4), ("Middle Eastern", 10)], 216),
    # The parent smaller than the rest (NCT05597787's Malaysian rows): as posted.
    ([("Malay", 28), ("Chinese", 35), ("Indian", 24), ("Other", 1)], 78),
    # The excess exactly the rest, but the parent smaller than the rest: as posted.
    ([("Malay", 6), ("Chinese", 5), ("Indian", 4)], 6),
    # Within its population: no evidence of a second count.
    ([("Asian", 10), ("Asian (Japanese)", 10), ("White", 30)], 50),
    ([("Asian", 10), ("Asian (Japanese)", 0), ("White", 30)], 40),
])
def test_asian_rows_that_are_not_a_breakdown_are_all_counted(rows: list[tuple[str, int]], total: int) -> None:
    race = extract_race_data(_study("NCT99000620", [_class_table(COMBINED_TITLE, rows)], total))
    assert race["quarantined_labels"] == []
    assert race["omb_totals"]["asian"] == sum(n for label, n in rows
                                              if rx.map_race_category(label)["omb_category"] == "asian")


def test_an_asian_breakdown_in_a_race_by_sex_table_is_not_counted_again() -> None:
    """Review finding: the guard reads each sex of a race-by-sex table apart."""
    table = _class_table(COMBINED_TITLE, [("Asian: Female", 10), ("Han: Female", 10), ("White: Male", 5)])
    race = extract_race_data(_study("NCT99000623", [table], 15))
    assert "race_by_sex_table" in race["flags"]
    assert _nonzero(race) == {"asian": 10, "white": 5}
    assert [(q["original"], q["reason"]) for q in race["quarantined_labels"]] == [
        ("Han: Female", "asian_breakdown")]


def test_asian_rows_of_different_sexes_are_not_a_breakdown_of_each_other() -> None:
    """"Asian: Female" and "Han: Male" are different people: the table's
    excess stays as posted."""
    table = _class_table(COMBINED_TITLE, [("Asian: Female", 10), ("Han: Male", 10), ("White: Male", 5)])
    race = extract_race_data(_study("NCT99000624", [table], 15))
    assert race["quarantined_labels"] == []
    assert race["omb_totals"]["asian"] == 20


def test_an_asian_breakdown_in_a_race_by_group_table_is_not_counted_again() -> None:
    """Review finding: the guard reads each group of a race-by-group table apart."""
    table = _grouped_table([
        _period_row("Asian - ALPHA", "BG000", 10), _period_row("Han - ALPHA", "BG000", 10),
        _period_row("White - ALPHA", "BG000", 50), _period_row("White - Usual Care", "BG001", 40)])
    race = extract_race_data(_grouped_study("NCT99000625", [table]))
    assert "race_by_group_table" in race["flags"]
    assert _nonzero(race) == {"asian": 10, "white": 90}
    assert [(q["original"], q["reason"]) for q in race["quarantined_labels"]] == [
        ("Han - ALPHA", "asian_breakdown")]


def test_a_combined_table_with_every_race_row_read_counts_them_not_hispanic() -> None:
    """The NCT00216476 shape: with "Oriental" read, every row but "Hispanic"
    is a race, so the ethnicity extractor counts the race rows as not
    Hispanic (they were unknown while "Oriental" was quarantined)."""
    from src.ethnicity_extractor import extract_ethnicity_data
    table = _class_table(COMBINED_TITLE, [("Caucasian", 60), ("Oriental", 3), ("Hispanic", 2), ("Black", 5)])
    ethnicity = extract_ethnicity_data(_study("NCT99000621", [table], 70))
    assert ethnicity["omb_totals"]["hispanic_latino"] == 2
    assert ethnicity["omb_totals"]["not_hispanic_latino"] == 68
    assert ethnicity["omb_totals"]["unknown_not_reported"] == 0


# ── Black labels ───────────────────────────────────────────────────────────
#
# Fictional studies NCT99000629–631 (block comment "Black labels" in
# src/race_extractor.py). Each label comes from the 2026-11-15 inputs, where
# it was counted as other or quarantined as not race unless noted.

BLACK_CATEGORY = "black_african_american"


@pytest.mark.parametrize("label, subcategory", [
    ("Black African", "african"),
    ("Race - Black African", "african"),
    ("African Descent", "african"),
    ("African Ancestry", "african"),
    ("Black of African origin", "african"),
    ("Sub-Saharan African origin", "african"),
    ("Indigenous African", "african"),
    ("Other African tribe", "african"),
    ("African (Rwandan)", "african"),
    ("Black Kenyan", "african"),
    ("Zulu", "african"),
    ("Hausa", "african"),
    ("Luo ethnic group", "african"),
    ("Ghanaian", "african"),
    ("Afro-descendant", "african"),
    ("Ethnicity: African continent", "african"),
    ("Afro-Caribbean", "caribbean"),
    ("Afro-Carribean", "caribbean"),  # as posted
    ("Black Carribean", "caribbean"),
    ("Black (Caribbean)", "caribbean"),
    ("Black American", "african_american"),
    ("Afro-American", "african_american"),
    ("African-American (black)", None),  # Black beside African American: the category's name
    ("African - American (Black)", None),
    ("African, black", None),
    ("African American or Black", None),
    ("Black, African American, or African", None),
    ("Black or African", None),
    ("Black only", None),
    ("Black race", None),
    ("Other Black", None),
    ("Black (all ethnicities)", None),
    ("Black South African", None),
    ("Black (Brazil)", None),
    ("Black Canadian or African or African Caribbean", None),
    ("Black, African American, or Negro", None),
    ("Black or African (Afr) Amer", None),  # a bracketed abbreviation
    ("BLACK (BLA) OR AFRICAN AMERICAN (AFR AME)", None),
    ("Black or African American (B or Af Am)", None),
    ("Black or Af Am", None),
    ("Black (US)", None),
    ("Jamaicans of West African descent", None),
])
def test_a_label_naming_black_groups_only_is_read_as_black(label: str, subcategory: Optional[str]) -> None:
    assert rx.black_label(label) == (True, subcategory)


@pytest.mark.parametrize("label, subcategory", [
    # Black beside African American is the category's own name.
    ("Black (or African American)", None),
    ("Black of African American", None),
    # Names given as examples name no subgroup.
    ("Black or African American (African American, Jamaican, Haitian, Nigerian, Ethiopian, Somali, etc.)", None),
    ("Black (e.g., Haitian, Jamaican)", None),  # examples alone would make it caribbean
    ("Black including Haitian", None),  # fictional: an example word outside brackets (review finding)
    ("Black African including Nigerian", "african"),  # fictional: the names before it still count
    ("Race and Ethnicity : Black", None),  # a heading of fillers before the colon (NCT03302910)
    ("Race/Ethnicity: Black", None),  # fictional: a "/" heading
    ("Ethnic group and race: Black", None),  # fictional: a heading word beside a dimension word
    ("Self-reported race and ethnicity: Black", None),  # fictional
    ("Black including Caribbean (Haitian)", None),  # fictional: a bracket after the example word
    ("Black (including Caribbean (Haitian))", None),  # fictional: a bracket within the example's (review finding)
    ("Black (African) (e.g. Haitian)", "african"),  # fictional: a sibling bracket is not an example
    ("African American including Black", "african_american"),  # fictional: an example Black (review finding)
    ("African American (e.g. Black)", "african_american"),  # fictional
    ("Black (e.g., Haitian, Jamaican, or other)", None),  # fictional: "or other" in a list of examples is read
    ("Black (e.g., Haitian, Jamaican, and other)", None),  # fictional: "and other" likewise
    ("Black (Haitian, Jamaican, other)", None),  # fictional: a bare "other" after "," too
    ("Black (Haitian, Jamaican, or other)", None),  # fictional: the list names some members only
    ("Black (Haitian, Other)", None),  # fictional
    ("Black African (Nigerian, Ghanaian, or other)", "african"),  # fictional: by the names outside
    ("Caribbean (Haitian or other)", "caribbean"),  # fictional
    ("Black (Haitian or other Caribbean)", "caribbean"),  # fictional: Haitian lies within it
    ("Black (African, Haitian, or other Caribbean)", None),  # fictional: African does not (review finding)
    ("Black (African or other Caribbean)", None),  # fictional
    ("Black (Nigerian or other African)", "african"),  # fictional
    ("Black (Haitian or other Black)", None),  # fictional: "other Black" widens Black itself
    # A bracket's choices are reduced as alternatives are (review finding).
    ("Black (Haitian or Black)", None),  # fictional: as "Haitian or Black"
    ("Black (African or Negro)", None),  # fictional
    ("Black (Caribbean or Haitian)", "caribbean"),  # fictional: as "Caribbean or Haitian"
    ("Black (Haitian or Jamaican)", "caribbean"),  # fictional
    ("Black (Nigerian or Kenyan)", "african"),  # fictional
    ("Black (African (Nigerian or Kenyan) or Caribbean)", None),  # fictional: innermost bracket first
    ("Caribbean (Haitian or Jamaican)", "caribbean"),  # fictional: the choices refine the name outside
    ("African (Black or Negro)", "african"),  # fictional: Black in each choice
    ("Caribbean (Haitian or Black)", None),  # fictional: a bracket sharing no subgroup overrides the name outside
    ("Black (Caribbean (Haitian or Black) or Jamaican)", None),  # fictional: the inner bracket shares none
    ("African (or Caribbean (Haitian or Black))", None),  # fictional: and passes that up its one-choice bracket
    ("African (Caribbean (Haitian or Black) or Negro)", None),  # fictional: and up a bracket with Black in each choice
    # A bracket opening with a join takes the names before it as its first choice (review finding).
    ("Black (or Haitian)", None),  # fictional: as "Black or Haitian"
    ("Caribbean (or Haitian)", "caribbean"),  # fictional: as "Caribbean or Haitian"
    ("Black African (or Nigerian)", "african"),  # fictional
    ("Black (or of African descent)", None),  # fictional: as "Black or of African descent"
    ("Black (Caribbean (or Haitian) or African)", None),  # fictional: in a bracket, the names before it in its choice
    ("Caribbean (Haitian, Black (or Jamaican))", None),  # fictional: not a name in another choice
    ("Caribbean (Jamaican) (or Haitian)", "caribbean"),  # fictional: a bracket before it is in its first choice
    ("Caribbean (Haitian or Black) (or Jamaican)", None),  # fictional: reduced before it is taken in
    ("Black (or Haitian) Caribbean", None),  # fictional: a name after the bracket is not in its first choice
    # "Indigenous" qualifies the names in its own choice (review finding).
    ("Black (Indigenous African)", "african"),  # fictional
    ("Black (Indigenous African or Haitian)", None),  # fictional
    ("Black (African/Caribbean)", None),  # fictional: each "/" choice names a group
    ("Black (including African, Caribbean descent)", None),  # a "," choice with a filler
    ("African, Caribbean, or non-specified Black", None),  # "non-specified" describes Black
    # Haitian lies in Caribbean; across alternatives, theirs when all agree.
    ("Caribbean (Haitian)", "caribbean_haitian"),
    ("Afro-Caribbean or Caribbean", "caribbean"),
    ("Afro-Caribbean or African Origin", None),
])
def test_the_black_subcategory_comes_from_the_names(label: str, subcategory: Optional[str]) -> None:
    assert rx.black_label(label) == (True, subcategory)


@pytest.mark.parametrize("label", [
    "South African",  # a country of every race
    "Coloured",  # the South African census's own group
    "Southern African Colored",
    "Coloured/Mixed Ancestry",
    "Black or mixed race",  # pooled
    "African American or Other",
    "Black, Euro-African, or African American",
    "Black or other race",
    "Black or unspecified",  # fictional: a non-answer pooled (review finding)
    "Black or non-specified",  # fictional
    "Black or Indigenous",  # fictional: another race pooled (review finding)
    "Black, Indigenous",  # fictional
    "Indigenous Caribbean",  # fictional: Indigenous qualifies African peoples only (review finding)
    "Indigenous Black",  # fictional
    "Black (Indigenous Caribbean)",  # fictional: in a bracket too
    "Black (Indigenous Caribbean or African)",  # fictional: its own choice is Caribbean
    "Indigenous Black (African)",  # fictional: its choice holds Black too
    "Indigenous African (or Caribbean)",  # fictional: a bracket within its choice counts
    "Black (Indigenous)",  # fictional: its choice names no group
    "Black or participants of all ethnicities",  # fictional: fillers only pooled (review finding)
    "all participants or Race: Black",  # fictional: "or" before the heading's colon pools (review finding)
    "all participants and Race: Black",  # fictional: not a heading's words (review finding)
    "all participants, Race: Black",  # fictional
    "groups and race: Black",  # fictional: no dimension word in the part
    "Black (or Indigenous)",  # fictional: a choice in a bracket names another race (review finding)
    "Black (or unspecified)",  # fictional
    "Black (or Other)",  # fictional: Other as a bracketed choice (review finding)
    "Black (or other race)",  # fictional
    "Black (and Other)",  # fictional: "and" in a bracket (review finding)
    "Black (and unspecified)",  # fictional
    "Black (Haitian or other race)",  # fictional: "other race" names any race (review finding)
    "Black (unspecified or Black)",  # fictional: the choice before the join (review finding)
    "Black (Haitian / unspecified)",  # fictional: a "/" choice in a bracket (review finding)
    "Black (Haitian, unspecified)",  # fictional: a "," choice in a bracket (review finding)
    "Black (Haitian; unspecified)",  # fictional
    "Black (Haitian, other race)",  # fictional
    "Not Black",  # negated
    "Non-Black",
    "Black or African American (Comparison 1)",  # crossed with something else
    "African Mothers",
    "Black : Children",
    "Black (11 to 18 years)",
    "Black (Part C)",
    "Phase 1, Black",
    "Black not Hisp",  # an ethnicity qualifier
    "NH-Black",
    "African (e.g., Central and West African, North African, Southern Africa)",  # North Africa
    "Middle Eastern or North African/Mediterranean",
    "Black or Black British",  # the UK census rule's
    "AA",  # an abbreviation alone
    "Tonga",
    "/Black",
    "Black or",
])
def test_a_label_naming_more_or_less_than_black_groups_is_not_read_by_the_rule(label: str) -> None:
    assert rx.black_label(label) == (False, None)


@pytest.mark.parametrize("label, subcategory, flags", [
    ("Black or African American", None, []),  # the vocabulary's own
    ("African", "african", []),
    ("Black/African American", None, ["slash_normalized"]),  # the slash fragments run first
    ("African/American", "african_american", ["black_label"]),  # one name: not the "African" fragment (review finding)
    ("African/American, Black", None, ["black_label"]),  # fictional: the Black labels read the rejoined label
    ("Black African", "african", ["black_label"]),
])
def test_black_readings_through_map_race_category(label: str, subcategory: Optional[str],
                                                  flags: list[str]) -> None:
    mapping = rx.map_race_category(label)
    assert (mapping["omb_category"], mapping["subcategory"], mapping["flags"]) == (
        BLACK_CATEGORY, subcategory, flags)


def test_rejoining_african_american_keeps_the_other_slash_fragments() -> None:
    """Only the slash inside "African/American" is rejoined; the label's
    other fragments are read as before (fictional; a review finding)."""
    mapping = rx.map_race_category("White/African/American")
    assert (mapping["omb_category"], mapping["subcategory"], mapping["flags"]) == (
        "white", None, ["slash_normalized"])
    # A slash after "American" ends its fragment: the rejoined name is read,
    # also where the Black labels do not read the whole label.
    for label in ("African/American/Black", "African/American/Asian"):
        mapping = rx.map_race_category(label)
        assert (mapping["omb_category"], mapping["subcategory"], mapping["flags"]) == (
            "black_african_american", "african_american", ["slash_normalized"]), label


@pytest.mark.parametrize("label", ["African/American Indian", "African/American-Indian", "African/American (Indian)",
                                   "African/American [Indian]", "African/American/Indian",  # review finding
                                   "African/American {Indian}", "African/American. Indian",
                                   # "American" does not end its fragment (review finding)
                                   "African/American Samoan", "African/American Pacific Islander",
                                   "African/American (Black)", "African/Americans",
                                   # the rejoined label is read by nothing (review finding)
                                   "African/American or Indian", "African/American, or Indian",
                                   "African/American or Samoan"])
def test_african_slash_american_indian_is_not_rejoined(label: str) -> None:
    """"American Indian" after the slash names a second race, with or without
    punctuation, and an "American" that does not end its fragment goes on to
    name something else; either way the label keeps its slash reading
    (fictional; review findings)."""
    mapping = rx.map_race_category(label)
    assert (mapping["omb_category"], mapping["subcategory"], mapping["flags"]) == (
        "black_african_american", "african", ["slash_normalized"])


def test_a_table_of_african_peoples_is_counted_as_black() -> None:
    table = _class_table(COMBINED_TITLE, [("Zulu", 40), ("Xhosa", 30), ("Coloured", 20), ("White", 10)])
    race = extract_race_data(_study("NCT99000629", [table], 100))
    assert _nonzero(race) == {BLACK_CATEGORY: 70, "white": 10, "unknown_not_reported": 20}
    assert race["subcategory_totals"] == {"black_african_american_african": 70}
    assert [q["original"] for q in race["quarantined_labels"]] == ["Coloured"]


def test_a_trial_whose_only_race_row_names_an_african_nationality_reports_race() -> None:
    """The NCT03062228 shape: one row, "Ghanaian", quarantined before."""
    race = extract_race_data(_study("NCT99000630", [_class_table(COMBINED_TITLE, [("Ghanaian", 162)])], 162))
    assert race["reported"] and _nonzero(race) == {BLACK_CATEGORY: 162}


def test_the_multirace_rule_reads_a_black_part_the_rule_reads() -> None:
    """"Black or African Amer. & White" (148 participants in the 2026-11-15
    inputs) is two races once its Black part reads."""
    table = _class_table(COMBINED_TITLE, [("White", 50), ("Black or African Amer.", 30),
                                          ("Black or African Amer. & White", 20)])
    race = extract_race_data(_study("NCT99000631", [table], 100))
    assert _nonzero(race) == {"white": 50, BLACK_CATEGORY: 30, "more_than_one_race": 20}


# ── The 1977 Asian or Pacific Islander category ───────────────────────────
#
# Fictional studies NCT99000632–635 (block comment "The 1977 Asian or
# Pacific Islander category" in src/race_extractor.py). The maintainer's
# decision: the grouped category is counted as Asian. Each label comes from
# the 2026-11-15 inputs unless noted.

GROUPED = "asian_pacific_grouped"


@pytest.mark.parametrize("label", [
    "Asian or Pacific Islander",
    "Asian or pacific islander",
    "Asian or Other Pacific Islander",
    "Asian and/or Pacific Islander",
    "Asian and / or Pacific Islander",  # fictional
    "Asian and Pacific Islander",  # the group in any table
    "Asian & Other Pacific Islander",
    "Asian and Pacific Islander Heritage",  # its heritage form
    "Asian & Other Pacific Islander Heritage",
    "Asian/Pacific Islander",
    "Asian / Pacific Islander",
    "Asian/Pacific",
    "Asian/PI",
    "Asian/Pacific Isl.",
    "Asian/Islander",
    "Asian, Pacific Islander",
    "Asian, Asian American, Pacific Islander",
    "Asian, Asian-American, or Pacific Islander",
    "Asian Pacific Islander",
    "Asian Pacific",
    "Asian Hawaiian or Other Pacific Islander",
    "Asian, Native Hawaiian, or Other Pacific Islander",  # the fuzzy match read it as NHPI
    "Asian American/Pacific Islander",  # the slash fragments read it as NHPI
    "Non-Hispanic Asian / Pacific Islander",
    "Asian/Pacific Islander, non-Hispanic",
    "Native Hawaiian or Pacific Islander/Asian",
    "Asian or Native Hawaiian or Pacific Islander",
    "Asian or Native Hawaiian",
    "Asian; Native Hawaiian; Pacific Islander",
    "Asian/Oriental or Pacific Islander",
    "Filipino, Asian, or Asian Pacific Islander",
    "East Asian/Pacific",
    "Asian/PI (Pacific Islander)",  # a Pacific part only nhpi_label reads
    "Non-Hispanic Asian or Pacific Islander only",
    "Asian or Pacific Islander (Hawaiian and Samoan)",  # fictional: "and" inside a part
    "Asian, Native Hawaiian and Other Pacific Islander",  # fictional: the "and" name of NHPI
])
def test_a_label_naming_the_1977_group_is_read_as_asian(label: str) -> None:
    assert rx.asian_pacific_grouped_label(label)
    mapping = rx.map_race_category(label)
    assert (mapping["omb_category"], mapping["subcategory"]) == ("asian", None)
    assert GROUPED in mapping["flags"]


@pytest.mark.parametrize("label", [
    "Asian and Native Hawaiian or Pacific Islander",  # "and": the table decides
    "Asian American and Pacific Islander",
    "Asian & Native Hawaiian or Other Pacific Islander",
    "Asian/American Indian/Pacific (non-Hispanic)",  # another race pooled
    "AI/AN/Asian/NH/PI",
    "American Indian, Alaska Native, Asian, Native Hawaiian, or Other Pacific Islander",
    "Asian, Pacific-Islander, other",
    "Asian/Pacific Islander and White",  # fictional: another race joined by "and"
    "Asian and White/Pacific Islander",  # fictional
    "Caregivers: Asian/Pacific Islander",  # crossed
    "Asian",
    "Pacific Islander",
    "Asian or Other",
    "Asian/Pacific American",
])
def test_a_label_that_is_not_the_1977_group_by_itself(label: str) -> None:
    assert not rx.asian_pacific_grouped_label(label)


def test_the_group_counts_as_asian_in_a_table() -> None:
    table = _category_table(COMBINED_TITLE, [
        ("Asian or Pacific Islander", 30), ("White", 60), ("Black or African American", 10)])
    race = extract_race_data(_study("NCT99000632", [table], 100))
    assert _nonzero(race) == {"asian": 30, "white": 60, "black_african_american": 10}


def test_a_comma_list_of_the_two_races_beside_asian_alone_is_still_a_combination() -> None:
    """The comma-list rule reads "Asian, Pacific Islander" as two races when
    the table names Asian alone (1 trial in the 2026-11-15 inputs)."""
    table = _category_table(COMBINED_TITLE, [("Asian", 20), ("Asian, Pacific Islander", 2), ("White", 78)])
    race = extract_race_data(_study("NCT99000633", [table], 100))
    row = next(c for c in race["raw_categories"] if c["original"] == "Asian, Pacific Islander")
    assert row["omb_category"] == "more_than_one_race" and "multirace_comma_list" in row["flags"]


def test_a_grouped_row_names_no_race_alone_for_the_comma_list_rule() -> None:
    table = _category_table(COMBINED_TITLE, [
        ("Asian or Pacific Islander", 10), ("Asian, Black", 2), ("Other", 88)])
    race = extract_race_data(_study("NCT99000634", [table], 100))
    row = next(c for c in race["raw_categories"] if c["original"] == "Asian, Black")
    assert "multirace_comma_list" not in row["flags"]


def test_asian_joined_to_pacific_islander_beside_asian_alone_is_still_a_combination() -> None:
    table = _category_table(COMBINED_TITLE, [
        ("Asian", 20), ("Asian and Native Hawaiian or Pacific Islander", 2), ("White", 78)])
    race = extract_race_data(_study("NCT99000635", [table], 100))
    assert race["omb_totals"]["more_than_one_race"] == 2 and race["omb_totals"]["asian"] == 20


# ── European nationalities and Roma groups ────────────────────────────────
#
# Fictional studies NCT99000636–639 (block comment "European labels" in
# src/race_extractor.py). The maintainer's decisions: a European nationality
# alone is read as White, and so are Roma, Gypsy and Traveller groups. Each
# label comes from the 2026-11-15 inputs, where it was quarantined as not
# race or counted as other, unless noted.

@pytest.mark.parametrize("label", [
    "Russian", "Italian", "Romanian", "British", "Irish", "Danish", "Dutch", "Greek", "Greeks",
    "Albanian", "Albanians", "Macedonians", "Belarusian", "Ukrainian", "Hungarian", "Slovak",
    "Polish", "Portuguese",  # not Hispanic in OMB's definition
    "Serbian", "Bosnian", "Croatian", "English", "French",
    "Northern Irish",  # fictional
    "Russians",  # fictional
    "Other: Russian", "Other: Macedonian", "Caucasian-Russians", "White, Other: German",
])
def test_a_european_nationality_is_read_as_white(label: str) -> None:
    assert rx.european_label(label) == (True, "european")
    mapping = rx.map_race_category(label)
    assert (mapping["omb_category"], mapping["subcategory"]) == ("white", "european")
    assert "european_label" in mapping["flags"]


@pytest.mark.parametrize("label", [
    "Roma", "Gipsy", "Gypsy", "gypsies", "Irish Traveller", "Roma European", "Other (Sinti)",
    "Gypsy or Irish Traveller",  # fictional: the census wording
    "White: Gypsy or Irish Traveller",  # fictional
    "White - Roma",  # fictional
    "Gypsy/Traveller",  # fictional: every alternative a Roma group
])
def test_a_roma_group_is_read_as_white_with_no_subcategory(label: str) -> None:
    assert rx.european_label(label) == (True, None)
    mapping = rx.map_race_category(label)
    assert (mapping["omb_category"], mapping["subcategory"]) == ("white", None)


@pytest.mark.parametrize("label", [
    "Spanish",  # Hispanic or Latino in OMB's definition, an ethnicity
    "Kazakh", "Tartarian", "Baskirian", "Chuvash",  # peoples of Russia and Central Asia
    "Russian or Kazakh",  # fictional: pools a people the rule does not read
    "Traveller", "Travellers",  # fictional: names no group alone
    "French Canadian",
    "Turkish",  # a vocabulary entry, not this rule's
    # UK census wordings, left to the rule for those
    "White British", "White - Irish", "White-Irish", "White British/Irish", "White or White British",
    "English/Welsh/Scottish/Northern Irish/British",
    "White- English / Welsh / Scottish / Northern Irish / British",
])
def test_a_label_the_nationality_rule_does_not_read(label: str) -> None:
    assert rx.european_label(label) == (False, None)


@pytest.mark.parametrize("label", ["White British/European", "White British/ European"])
def test_a_uk_nation_beside_a_european_name_keeps_its_reading(label: str) -> None:
    """Read as White before this rule through "European" (NCT02972606,
    NCT03699163); the UK census exclusion applies only to labels naming no
    European name."""
    assert rx.european_label(label) == (True, "european")


def test_a_table_of_peoples_of_russia_reads_the_european_ones() -> None:
    """The NCT03532932 shape: Russian and Belarusian read as White; Tatar
    and Kazakh rows stay quarantined, their participants balanced to
    unknown."""
    table = _category_table(COMBINED_TITLE, [
        ("Russian", 60), ("Belarusian", 5), ("Tartarian", 15), ("Kazakh", 10), ("Other", 10)])
    race = extract_race_data(_study("NCT99000636", [table], 100))
    assert _nonzero(race) == {"white": 65, "other": 10, "unknown_not_reported": 25}
    assert sorted(q["original"] for q in race["quarantined_labels"]) == ["Kazakh", "Tartarian"]


def test_a_table_of_nationalities_and_roma_newly_reports_race() -> None:
    """The NCT03912753 shape: every row was quarantined but "Other"; now the
    trial's race is read."""
    table = _category_table(COMBINED_TITLE, [("Romanian", 80), ("Hungarian", 10), ("Roma", 5), ("Other", 5)])
    race = extract_race_data(_study("NCT99000637", [table], 100))
    assert _nonzero(race) == {"white": 95, "other": 5}
    assert race["subcategory_totals"].get("white_european") == 90


def test_a_combined_table_whose_last_unread_row_was_a_nationality_counts_race_rows_not_hispanic() -> None:
    """The NCT00955708 shape: with "Russian" read, every row but "Hispanic"
    is a race, so the ethnicity extractor counts the race rows as not
    Hispanic (they were unknown while "Russian" was quarantined)."""
    from src.ethnicity_extractor import extract_ethnicity_data
    table = _class_table(COMBINED_TITLE, [("Caucasian", 60), ("Russian", 3), ("Hispanic", 2), ("Black", 5)])
    ethnicity = extract_ethnicity_data(_study("NCT99000638", [table], 70))
    assert ethnicity["omb_totals"]["hispanic_latino"] == 2
    assert ethnicity["omb_totals"]["not_hispanic_latino"] == 68
    assert ethnicity["omb_totals"]["unknown_not_reported"] == 0


def test_a_comma_list_naming_a_nationality_counts_it_as_a_race() -> None:
    """NCT01199042: "African American, Irish, Native American" names three
    races once "Irish" reads as White."""
    table = _category_table(COMBINED_TITLE, [
        ("Caucasian", 25), ("African American", 1), ("African American, Irish, Native American", 1)])
    race = extract_race_data(_study("NCT99000639", [table], 27))
    assert _nonzero(race) == {"white": 25, "black_african_american": 1, "more_than_one_race": 1}


# ── UK census labels ───────────────────────────────────────────────────────
#
# Fictional studies NCT99000626–628 (block comment "UK census labels" in
# src/race_extractor.py). Each label comes from the 2026-11-15 inputs, where
# it was counted as other or quarantined as not race unless noted.

BLACK = "black_african_american"


@pytest.mark.parametrize("label, category, subcategory", [
    ("Asian or Asian British", "asian", None),
    ("Asian or Asian British (Indian)", "asian", "south_asian_indian"),
    ("Asian or Asian British - Pakistani", "asian", "south_asian_pakistani"),
    ("Asian British", "asian", None),
    ("British Indian", "asian", "south_asian_indian"),
    ("British or Asian Pakistani", "asian", "south_asian_pakistani"),
    ("Asian, Asian British or Asian Welsh", "asian", None),  # the 2021 heading in Wales
    ("Black or Black British", BLACK, None),
    ("Black or Black British (African)", BLACK, "african"),
    ("Asian or Asian British (Indian or Other)", "asian", None),  # fictional: Other pooled (review finding)
    ("Black or Black British (Caribbean, Other)", BLACK, None),  # fictional
    ("Black, African, Caribbean or Black British", BLACK, None),
    ("Black, Black British, Black Welsh, Caribbean or African", BLACK, None),
    ("Black / African / Caribbean / Black British - Caribbean", BLACK, "caribbean"),
    ("Black African/Caribbean/British", BLACK, None),  # two groups: no subcategory
    ("British African", BLACK, "african"),
    ("Caribbean British", BLACK, "caribbean"),
    ("Afro-Caribbean or British African", BLACK, None),
    ("Any other Black background", BLACK, None),
    ("White British", "white", None),
    ("White - British", "white", None),
    ("White British/Irish", "white", None),
    ("White Irish", "white", None),
    ("White (British, Irish, Other)", "white", None),
    ("English/Welsh/Scottish/Northern Irish/British", "white", None),
    ("White - English / Welsh / Scottish / Northern Iris", "white", None),  # truncated as posted
    ("Any other white background", "white", None),
    ("Mixed or Multiple Ethnic Groups", "more_than_one_race", None),
    ("Mixed ethnic group", "more_than_one_race", None),
    ("Mixed - White & Black Caribbean", "more_than_one_race", None),
    ("Mixed white and black carribean", "more_than_one_race", None),
    ("Any other mixed background", "more_than_one_race", None),
    ("(Mixed ethnic group:) White / Black Caribbean", "more_than_one_race", None),
    ("(Mixed ethnic group:) White / Other", "more_than_one_race", None),  # the parts of the mix
    ("Mixed ethnic group white and other", "more_than_one_race", None),  # fictional: "and" lists the parts of the mix
    ("Mixed ethnic group: White or Other", "more_than_one_race", None),  # fictional: "or" after the heading (review finding)
    ("Multiple (Asian or White)", "more_than_one_race", None),  # fictional: "or" in a bracket lists parts (review finding)
    ("Multiple (Asian-White)", "more_than_one_race", None),
])
def test_a_uk_census_wording_is_read_by_its_heading(label: str, category: str,
                                                    subcategory: Optional[str]) -> None:
    assert rx.uk_census_label(label) == (category, subcategory)


@pytest.mark.parametrize("label", [
    "British",  # a nationality alone: the nationality rule's
    "Irish",
    "Other ethnic group",  # stays other
    "Any other ethnic group",
    "White-British-UK patients",  # crossed with another dimension
    "Child - Mixed/Multiple",
    "Other (Specify): Black British",
    "Black or White British",  # two races as alternatives
    "Asian - Mixed Race",  # no UK wording (the heritage template's)
    "White - Mixed Race",
    "Mixed Asian Race",
    "White: Gypsy or Irish Traveller, Roma or Other White",  # left to the Roma rule
    "English Canadian",
    "White or English speaker",
    "/Black British",  # a piece of a longer label
    "White and Black African",  # no UK wording without a Mixed heading
    "White British or Mixed ethnic group",  # Mixed pooled with another group (a review finding)
    "Mixed ethnic group or Other",
    "White British or Other ethnic group",  # fictional: Other pooled (a review finding)
    "Asian or Asian British or Other",  # fictional
    "Black or Black British or any other ethnic group",  # fictional
    "White British/Other",  # fictional
    "White British and Other ethnic group",  # fictional: joined by "and" (a review finding)
    "White British & Other",  # fictional
    "Mixed ethnic group / Other",  # fictional: Other beside Mixed, no heading (review finding)
    "Mixed ethnic group, Other",  # fictional
    "Mixed ethnic group / White British",  # fictional: another group pooled with Mixed (review finding)
    "Mixed ethnic group and Other ethnic group",  # fictional: a complete Other heading (review finding)
    "Mixed ethnic group & Other ethnic group",  # fictional
    "Mixed ethnic group and White ethnic group",  # fictional: a second complete heading (review finding)
    "White British or Mixed - White & Asian",  # fictional: "or" in the heading pools
])
def test_a_label_without_a_uk_census_reading_is_not_read_by_the_rule(label: str) -> None:
    assert rx.uk_census_label(label) is None


@pytest.mark.parametrize("label, category, subcategory, flags", [
    ("(Mixed ethnic group:) White / Other", "more_than_one_race", None, ["uk_census_label"]),  # before slash
    ("Asian/Asian British", "asian", None, ["uk_census_label"]),
    ("Caribbean", BLACK, "caribbean", []),  # the vocabulary's own
    ("Mixed", "more_than_one_race", None, []),
])
def test_uk_census_readings_through_map_race_category(label: str, category: str,
                                                      subcategory: Optional[str], flags: list[str]) -> None:
    mapping = rx.map_race_category(label)
    assert (mapping["omb_category"], mapping["subcategory"], mapping["flags"]) == (category, subcategory, flags)


def test_a_uk_census_table_is_counted_by_its_headings() -> None:
    table = _class_table(COMBINED_TITLE, [
        ("White British", 55), ("White Irish", 5), ("Asian or Asian British", 20),
        ("Black, African, Caribbean or Black British", 10), ("Mixed or Multiple Ethnic Groups", 5),
        ("Other ethnic group", 5)])
    race = extract_race_data(_study("NCT99000626", [table], 100))
    assert _nonzero(race) == {"white": 60, "asian": 20, BLACK: 10, "more_than_one_race": 5, "other": 5}
    assert race["quarantined_labels"] == []
    assert all("multirace_comma_list" not in c["flags"] for c in race["raw_categories"])


def test_an_ethnicity_titled_uk_table_now_reports_race() -> None:
    """The NCT01452191 shape: every row names the ethnicity dimension, and
    "White British" now reads as a race, so the table is a race
    classification."""
    table = _class_table(COMBINED_TITLE, [("Ethnicity: White British", 100), ("Ethnicity: Other ethnic group", 10)])
    race = extract_race_data(_study("NCT99000627", [table], 110))
    assert race["reported"]
    assert _nonzero(race) == {"white": 100, "other": 10}


def test_the_census_white_groups_are_counted_with_no_quarantine() -> None:
    table = _class_table(COMBINED_TITLE, [
        ("English/Welsh/Scottish/Northern Irish/British", 40), ("Any other White background", 10),
        ("Asian/Asian British - Indian", 5)])
    race = extract_race_data(_study("NCT99000628", [table], 55))
    assert _nonzero(race) == {"white": 50, "asian": 5}
    assert race["subcategory_totals"] == {"asian_south_asian_indian": 5}
    assert race["quarantined_labels"] == []


# ── negated race labels ────────────────────────────────────────────────────
#
# src/race_extractor.py, block comment "Negated race labels": a label negating
# a race named directly after the negation, with no race named before it, is
# counted as other, never as the race it excludes.

# Posted in the 2026-11-15 snapshot and counted as the race they exclude
# before the rule, through a fuzzy match, a slash fragment or the split of a
# combined label.
NEGATED_POSTED = [
    "Not African-American", "Non-Black/African American", "Non-Black/African-American",
    "Non-African American", "Non African American", "Non- African-American",
    "Other than White or Black/African American", "Non-Native American", "Non Caucasian/Asian",
    "Non-White Hispanic/Asian", "Non-white/more than one race", "Non-White/Multiple selected/Missing",
    "Hispanic, not African American", "Hispanic, Non-African American",
]


@pytest.mark.parametrize("label", NEGATED_POSTED)
def test_a_negated_race_label_is_counted_as_other(label: str) -> None:
    mapping = rx._map_race_label(label)
    assert (mapping["omb_category"], mapping["subcategory"], mapping["confidence"]) == ("other", None, "low")
    assert mapping["flags"] == ["negated_race", "unmapped"]
    assert mapping["original"] == label
    assert rx._should_quarantine(mapping) is False


@pytest.mark.parametrize("label", ["Nonwhite", "Other (Nonblack)", "Not a Native Hawaiian", "Excluding Asian",
                                   "Except Black", "Neither Aboriginal nor Torres Strait Islander",
                                   "NON-AFRICAN AMERICAN", "Non–White", "not an Asian",
                                   # A race named outside brackets, negated there.
                                   "Non-Black (African American)",
                                   # A race named only inside brackets: read whole.
                                   "(Non-White)"])
def test_negated_race_label(label: str) -> None:
    assert rx.negated_race_label(label) is True


@pytest.mark.parametrize("label", [
    # A race named before the negation: the negation qualifies or contrasts it.
    "Asian - Not Japanese", "White vs. non-white", "White & Non-White/Multiple", "Race (White vs Non-White)",
    "Caucasian - Non-white", "Caucasian, non-European descent", "Asian (Other than Indian subcontinent)",
    # An ethnicity negated, not a race; "minority" is not a race word.
    "Non-Hispanic White", "Not Hispanic or Latino", "Nonhispanic Black", "White/Non-Minority",
    # No race word after the negation.
    "Not Reported", "Unknown or Not Reported", "None of the above", "Non-Hispanic",
    # A negation inside a longer word, or a race word inside one.
    "Nonusers", "Notwhite", "Non-Whiteside clinic",
    # A negation inside brackets qualifying a race named outside them (a
    # review finding), nested brackets included.
    "(Non-Indian) Asian", "(Not Japanese) Asian", "[Not Black] White", "((Non-Indian)) Asian",
    "(Non-Indian (South Asian)) Asian",
])
def test_not_a_negated_race_label(label: str) -> None:
    assert rx.negated_race_label(label) is False


@pytest.mark.parametrize("label, category", [
    ("White, not Hispanic", "white"), ("Asian - Not Japanese", "asian"), ("White/Non-Minority", "white"),
    ("Non-Hispanic White", "white"), ("Asian (Other than Indian subcontinent)", "asian"),
    ("Not Reported", "unknown_not_reported"), ("(Non-Indian) Asian", "asian"), ("(non-Japanese) Asian", "asian"),
])
def test_labels_the_rule_leaves_keep_their_reading(label: str, category: str) -> None:
    mapping = rx._map_race_label(label)
    assert mapping["omb_category"] == category
    assert "negated_race" not in mapping["flags"]


def test_a_vocabulary_key_is_read_before_the_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rule runs after the exact and case-insensitive matches, so a key
    of RACE_MAPPINGS keeps its reading whatever words it holds."""
    monkeypatch.setitem(rx.RACE_MAPPINGS, "Not Black (test key)", ("black_african_american", None))
    assert rx.map_race_category("not black (TEST KEY)")["flags"] == []
    assert rx.map_race_category("Not Black (test key)")["omb_category"] == "black_african_american"


def test_a_negated_label_is_not_split_into_its_race_part() -> None:
    """Split as a combined label, "Non-White Hispanic/Asian" keeps "Asian";
    the row pools non-White Hispanic and Asian participants."""
    assert rx._split_combined_label("Non-White Hispanic/Asian") == ("Asian", "Non-White Hispanic")
    assert rx._map_race_label("Non-White Hispanic/Asian")["omb_category"] == "other"


def test_a_table_of_a_race_and_its_negation() -> None:
    """The NCT02134314 shape: "African-American" 8, "Not African-American"
    62 was counted as 70 Black."""
    table = _class_table(COMBINED_TITLE, [("African-American", 8), ("Not African-American", 62)])
    race = extract_race_data(_study("NCT99000906", [table], 70))
    assert race["reported"]
    assert _nonzero(race) == {BLACK: 8, "other": 62}
    assert race["quarantined_labels"] == []
    assert "negated_race" in race["raw_categories"][1]["flags"]


def test_a_negated_pooled_row_beside_race_rows() -> None:
    """The NCT04655170 shape: White 54, Black 5, "Non-White Hispanic/Asian"
    1, which was counted as Asian."""
    table = _class_table(COMBINED_TITLE, [("White", 54), ("Black", 5), ("Non-White Hispanic/Asian", 1)])
    race = extract_race_data(_study("NCT99000907", [table], 60))
    assert _nonzero(race) == {"white": 54, BLACK: 5, "other": 1}
    assert race["quarantined_labels"] == []


def test_a_negated_american_indian_row() -> None:
    """The NCT05773833 shape: "Non-Native American" 42 was counted as
    American Indian or Alaska Native."""
    table = _class_table(COMBINED_TITLE, [("Native American", 385), ("Non-Native American", 42),
                                          ("Unknown/Missing", 4)])
    race = extract_race_data(_study("NCT99000908", [table], 431))
    assert _nonzero(race) == {"american_indian_alaska_native": 385, "other": 42, "unknown_not_reported": 4}


# ── where a race row's wording comes from ──────────────────────────────────
#
# src/race_extractor.py, block comment "Where a race row's wording comes
# from": every race row records whether its words are the registry template's,
# UK census wording, a United States identity, a narrower origin (its
# subcategory) or the category alone. The field changes no count.

CUSTOM = "Race/Ethnicity, Customized"


def _row(original: str, subcategory: Optional[str] = None, flags: Optional[list[str]] = None) -> dict[str, Any]:
    return {"original": original, "subcategory": subcategory, "flags": flags or []}


@pytest.mark.parametrize("label", ["White", "Black or African American", "Asian", "More than one race",
                                   "American Indian or Alaska Native", "Native Hawaiian or Other Pacific Islander",
                                   "Unknown or Not Reported", "black or african american", "More than One Race"])
def test_a_template_name_in_a_template_table_is_the_registry_template(label: str) -> None:
    assert rx.wording_source(_row(label), "Race (NIH/OMB)") == "registry_template"
    assert rx.wording_source(_row(label), " Race  (NIH/OMB) ") == "registry_template"


@pytest.mark.parametrize("label, source", [
    # Rows a registrant added to a template table are classified by their words.
    ("Black", "generic"), ("Other", "generic"), ("Hawaiian", "generic"),
    ("American Indian/ Alaska Native", "us_term"),
])
def test_a_row_outside_the_seven_in_a_template_table(label: str, source: str) -> None:
    assert rx.wording_source(_row(label), "Race (NIH/OMB)") == source


@pytest.mark.parametrize("label", [
    "Black or African American", "African American", "African-American", "Black/African American",
    "Afro-American", "Black American", "Asian American", "Asian/Asian-American", "European American",
    "Arab American", "Mexican-American", "South Asian or Indian American", "Native American",
    "American Indian or Alaska Native", "Alaskan Native", "Non-African American",
    "Black (African American)", "American (Am) Indian or Alaska (Al.) native",
    "American(Amer) Indian(Ind) or Alaska(Al) Native(N)", "Black or African American (Comparison 1)",
])
def test_a_united_states_identity_in_a_registrants_table(label: str) -> None:
    assert rx.wording_source(_row(label), CUSTOM) == "us_term"


def test_the_african_american_subcategory_is_a_united_states_identity() -> None:
    """A misspelling the reader resolves to African American."""
    assert rx.wording_source(_row("Afican American", "african_american"), CUSTOM) == "us_term"


@pytest.mark.parametrize("label", ["Latin American", "South American Indian", "North American Native",
                                   "Native South American", "Central American", "Americas"])
def test_a_region_before_american_is_not_a_united_states_identity(label: str) -> None:
    assert rx.wording_source(_row(label), CUSTOM) == "generic"


@pytest.mark.parametrize("label, subcategory", [
    ("Nigerian", "african"), ("Black African", "african"), ("Afro-Caribbean", "caribbean"),
    ("Japanese", "japanese"), ("Russian", "european"), ("Arab", "middle_eastern_arab"), ("Samoan", "samoan"),
])
def test_a_subcategory_names_the_origin(label: str, subcategory: str) -> None:
    assert rx.wording_source(_row(label, subcategory), CUSTOM) == "origin_term"


@pytest.mark.parametrize("label", ["Black", "White", "Caucasian", "Asian", "Native Hawaiian or Other Pacific Islander",
                                   "Other", "Unknown", "Navajo", "Non-Hispanic Black"])
def test_the_category_alone_is_generic(label: str) -> None:
    assert rx.wording_source(_row(label), CUSTOM) == "generic"


def test_uk_census_wording_is_its_own_source() -> None:
    assert rx.wording_source(_row("Black or Black British", flags=["uk_census_label"]), CUSTOM) == "uk_census"
    # Before the United States terms and the subcategory.
    assert rx.wording_source(_row("Black British/African American", "african", ["uk_census_label"]), CUSTOM) == "uk_census"


def test_the_first_value_that_applies_wins() -> None:
    assert rx.wording_source(_row("Black or African American", "african_american"), "Race (NIH/OMB)") == "registry_template"
    assert rx.wording_source(_row("Asian American", "japanese"), CUSTOM) == "us_term"
    assert rx.WORDING_SOURCES == ("registry_template", "uk_census", "us_term", "origin_term", "generic")


def test_every_row_of_a_template_table_is_the_registry_template() -> None:
    table = _category_table("Race (NIH/OMB)", [("White", 30), ("Black or African American", 10), ("Asian", 5),
                                               ("Unknown or Not Reported", 2)])
    race = extract_race_data(_study("NCT99000909", [table], 47))
    assert [c["wording_source"] for c in race["raw_categories"]] == ["registry_template"] * 4


def test_a_registrants_table_records_each_rows_source_and_no_count_moves() -> None:
    rows = [("Black or Black British", 20), ("African American", 15), ("Nigerian", 10), ("Black", 25), ("White", 30)]
    race = extract_race_data(_study("NCT99000910", [_class_table(COMBINED_TITLE, rows)], 100))
    assert [(c["original"], c["wording_source"]) for c in race["raw_categories"]] == [
        ("Black or Black British", "uk_census"), ("African American", "us_term"), ("Nigerian", "origin_term"),
        ("Black", "generic"), ("White", "generic")]
    assert _nonzero(race) == {BLACK: 70, "white": 30}
    assert race["subcategory_totals"] == {f"{BLACK}_african_american": 15, f"{BLACK}_african": 10}


def test_rows_of_a_sex_titled_race_table_carry_a_source() -> None:
    """A table read from a sex/gender-titled measure is never the template."""
    table = _class_table("Sex/Gender, Customized", [("White Female", 10), ("White Male", 8),
                                                    ("Black Female", 4), ("Black Male", 3)])
    race = extract_race_data(_study("NCT99000911", [table], 25))
    assert race["raw_categories"], race
    assert {c["wording_source"] for c in race["raw_categories"]} == {"generic"}
