"""
Tests for sex_gender_parser.py.

Three layers, in order of how often they should run:
  1. Rule tests (always): vocabulary order, measure selection, total choice, the
     three-state rule, the sex/gender conflation rule. Snapshot-independent.
  2. Fixture tests (always): 32 real trials from the 2026-06-09 extract with
     expected outputs. Expected values equal the paper's R det_parse_measure()
     output on the same input, so a failure here means the port drifted from the
     paper, not that the registry changed.
  3. Vocabulary lock (always): every distinct label seen in the 2026-06-09 extract
     maps to the bucket recorded in fixtures/label_vocabulary_audit_2026-06-09.csv.
     A failure here means a regex edit changed a classification. That may be
     intended; if so, regenerate the audit file in the same commit and say why.

Run:  pytest -q tests/
"""
import csv
import json
import math
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
# Vendored from the bundle; the only edits are these three path lines so the
# module resolves from src/ and the fixtures from tests/sex_gender/fixtures/.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "src"))
import sex_gender_parser as sgp  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


# --------------------------------------------------------------------------- 1
@pytest.mark.parametrize("label,bucket", [
    # sex buckets
    ("Female", "female"), ("Male", "male"), ("F", "female"), ("M", "male"),
    ("Women", "female"), ("Men", "male"), ("Girls", "female"), ("Boys", "male"),
    ("Femal", "female"),                                    # typo caught by \bfemal
    ("Black Female", "female"),                             # race x sex is a sex row
    ("Female Cohort", "female"),                            # arm x sex is a sex row
    ("Phase 2 - Amlodipine+valsartan - Female", "female"),  # arm prefix guard order
    # structural (never a count bucket)
    ("Total", ""), ("All Genders", ""), ("Both genders", ""), ("Any gender", ""),
    ("Number of Participants", ""), ("Mother", ""), ("Parents", ""), ("Children", ""),
    ("Phase I - Candesartan+HCTZ", ""), ("White", ""), ("Cohort A", ""),
    ("Single gender identity", ""),
    # explicit unknown (reported data, never missingness)
    ("Unknown", "unknown"), ("Unknown or Not Reported", "unknown"), ("Not collected", "unknown"),
    ("Prefer not to answer", "unknown"), ("Missing", "unknown"), ("No data", "unknown"),
    ("Did not report", "unknown"), ("Refuse to answer", "unknown"), ("Not sure", "unknown"),
    ("Unsure", "unknown"), ("Sex not recorded", "unknown"), ("Don't know", ""),  # not in vocabulary: review inbox
    # ambiguous: cis/trans qualifier on a plain sex word, held out of both sex and gender
    ("Transgender Female", "ambiguous"), ("Cisgender Man", "ambiguous"), ("Trans woman", "ambiguous"),
    ("Cis-female", "ambiguous"),
    # gender diverse
    ("Non-binary", "gender_diverse"), ("Nonbinary", "gender_diverse"), ("Genderqueer", "gender_diverse"),
    ("Trans masculine", "gender_diverse"), ("Transman", "gender_diverse"), ("Trans/FTM", "gender_diverse"),
    ("Transgender", "gender_diverse"), ("Trandgender", "gender_diverse"), ("Other", "gender_diverse"),
    ("Two-Spirit", "gender_diverse"), ("Intersex", "gender_diverse"), ("Questioning", "gender_diverse"),
    ("Self-describe", "gender_diverse"), ("None of these describe me", "gender_diverse"),
    ("Do Not Identify As Any of These Options", "gender_diverse"),
    ("Sexual or gender minority", "gender_diverse"), ("Multiple gender identities", "gender_diverse"),
    ("Ambiguous", "gender_diverse"),
    # sexual orientation is excluded, but identity words win over orientation words
    ("Gay", ""), ("Lesbian", ""), ("Bisexual", ""), ("Queer", ""), ("Straight", ""),
    ("Gender Queer", "gender_diverse"), ("Queer or another Identity", "gender_diverse"),
    # normalization
    ("  female  ", "female"), ("Female*", "female"), ("MALE", "male"),
    (None, ""), ("", ""),
])
def test_canon_bucket_vocabulary(label, bucket):
    assert sgp.canon_bucket(label) == bucket


def test_unknown_beats_sexword_and_identity():
    # Order rule 2 before 3/4/6: an unknown token wins even with a sex word present.
    assert sgp.canon_bucket("Female - not reported") == "unknown"
    assert sgp.canon_bucket("Transgender - unknown") == "unknown"


def test_unmapped_reason():
    assert sgp.canon_unmapped_reason("Female") == "mapped"
    assert sgp.canon_unmapped_reason("Total") == "structural_or_group"
    assert sgp.canon_unmapped_reason("Bisexual") == "sexual_orientation"
    assert sgp.canon_unmapped_reason("Caregivers") == "unrecognized"
    assert sgp.canon_unmapped_reason("") == "empty"


@pytest.mark.parametrize("title,mtype", [
    ("Sex: Female, Male", "sex"), ("Sex", "sex"), ("sex", "sex"),
    ("Sex/Gender, Customized", "sex_gender_customized"), ("Gender, Customized", "sex_gender_customized"),
    ("Gender", "gender"), ("Gender Identity", "gender"), ("Gender: Female, Male", "gender"),
    ("Child's Gender", "other"), ("Parent Sex", "other"), ("Sex at Birth", "other"), (None, None),
])
def test_measure_type(title, mtype):
    assert sgp.measure_type(title) == mtype


def test_measure_selection_is_title_based():
    measures = [
        {"title": "Age, Continuous"},
        {"title": "Sex: Female, Male"},
        {"title": "Female Reproductive Status"},   # NOT selected: no sex/gender in title
        {"title": "Gender Identity"},
        {"title": "Race (NIH/OMB)"},
        {"title": "Child's Sex"},
    ]
    sel = sgp.select_sex_gender_measures(measures)
    assert [m["title"] for m in sel] == ["Sex: Female, Male", "Gender Identity", "Child's Sex"]


def test_choose_total():
    # Total column present: the value equal to the sum of the others.
    ct = sgp.choose_total([{"groupId": "BG000", "value": "4"}, {"groupId": "BG001", "value": "6"}, {"groupId": "BG002", "value": "10"}])
    assert ct == {"total": 10.0, "by_position": False, "sumcheck": True}
    # No total column: last group, flagged.
    ct = sgp.choose_total([{"value": "4"}, {"value": "6"}, {"value": "7"}])
    assert ct == {"total": 7.0, "by_position": True, "sumcheck": False}
    # Single group.
    assert sgp.choose_total([{"value": "5"}])["total"] == 5.0
    # 'NA' strings are dropped.
    assert sgp.choose_total([{"value": "NA"}, {"value": "NA"}])["total"] is None
    # Inherited limitation, pinned so a change is deliberate: [5, 5] reads as 5.
    assert sgp.choose_total([{"value": "5"}, {"value": "5"}])["total"] == 5.0


def _std(title, cats, **extra):
    return {"title": title, "paramType": "COUNT_OF_PARTICIPANTS", "unitOfMeasure": "Participants",
            "classes": [{"categories": [{"title": t, "measurements": [{"groupId": "BG000", "value": str(v)}]} for t, v in cats]}],
            **extra}


def test_three_state_rule():
    # State 1: reported.
    p = sgp.parse_trial([_std("Sex: Female, Male", [("Female", 4), ("Male", 5)])])
    assert sgp.classify_reporting(p) == "reported"
    # State 2: registrant explicitly reported Unknown and nothing else.
    p = sgp.parse_trial([_std("Sex: Female, Male", [("Female", 0), ("Male", 0), ("Unknown", 9)])])
    assert sgp.classify_reporting(p) == "explicit_unknown_only"
    # State 3: measure absent entirely.
    p = sgp.parse_trial([{"title": "Age, Continuous"}])
    assert sgp.classify_reporting(p) == "not_reported"
    assert sgp.parse_trial([]).raw_present is False
    assert sgp.classify_reporting(sgp.parse_trial(None)) == "not_reported"
    # Measure present, no usable counts: uninformative, with sub-reason, NOT not_reported.
    p = sgp.parse_trial([{"title": "Sex: Female, Male", "populationDescription": "sex data was not collected",
                          "classes": [{"denoms": [{"counts": [{"value": "0"}]}], "categories": [{"title": "Female"}, {"title": "Male"}]}]}])
    assert sgp.classify_reporting(p) == "uninformative"
    assert p.uninformative_reason == "no_measurements"
    assert p.declared_not_collected is True
    # Parse error is its own state and yields None outcomes.
    pe = sgp.ParsedTrial(raw_present=True, parse_ok=False)
    assert sgp.classify_reporting(pe) == "parse_error"
    assert sgp.derive_outcomes(pe, "parse_error")["reported_sex"] is None


def test_gender_label_with_binary_only_is_sex_not_gender():
    p = sgp.parse_trial([_std("Gender", [("Woman", 10), ("Man", 12)])])
    st = sgp.classify_reporting(p)
    o = sgp.derive_outcomes(p, st)
    assert st == "reported"
    assert o["reported_sex"] is True
    assert o["reported_gender"] is False
    assert o["gender_labeled_binary_only"] is True


def test_gender_diverse_category_makes_reported_gender():
    p = sgp.parse_trial([_std("Gender", [("Woman", 10), ("Man", 12), ("Non-binary", 1)])])
    o = sgp.derive_outcomes(p, sgp.classify_reporting(p))
    assert o == {"reported_sex": True, "reported_gender": True, "reported_both": True,
                 "reported_any": True, "gender_labeled_binary_only": False}


def test_ambiguous_cistrans_counts_as_reported_gender_but_not_sex_bucket():
    p = sgp.parse_trial([_std("Gender", [("Cisgender Female", 10), ("Cisgender Male", 12), ("Transgender Female", 1)])])
    st = sgp.classify_reporting(p)
    o = sgp.derive_outcomes(p, st)
    assert st == "reported"
    assert p.n_female is None and p.n_male is None            # held out, never summed into sex
    assert p.n_ambiguous_gender == 23.0
    assert o["reported_gender"] is True and o["reported_sex"] is False


def test_relatives_table_does_not_drive_counts_when_enrollee_table_exists():
    ms = [_std("Sex: Female, Male", [("Female", 4), ("Male", 5)]),
          _std("Child's Gender", [("Female", 100), ("Male", 101), ("Unknown", 50)])]
    p = sgp.parse_trial(ms)
    assert p.n_female == 4 and p.n_male == 5
    assert p.n_unknown is None          # relatives' Unknown must not leak in
    assert p.n_measures == 2
    # but a lone "other"-typed table still counts (count-based reported_sex)
    p2 = sgp.parse_trial([_std("Child's Gender", [("Female", 100), ("Male", 101)])])
    assert sgp.derive_outcomes(p2, sgp.classify_reporting(p2))["reported_sex"] is True
    assert p2.measure_type == "other"


def test_max_not_sum_across_sex_and_gender_tables():
    ms = [_std("Sex: Female, Male", [("Female", 40), ("Male", 60)]),
          _std("Gender Identity", [("Woman", 39), ("Man", 58), ("Non-binary", 3)])]
    p = sgp.parse_trial(ms)
    assert (p.n_female, p.n_male) == (40.0, 60.0)     # fullest F+M table drives sex
    assert p.n_gender_diverse == 3.0
    assert p.has_sex_table and p.has_gender_table


def test_customized_layout_class_titles_are_the_labels():
    m = {"title": "Sex/Gender, Customized", "paramType": "COUNT_OF_PARTICIPANTS",
         "classes": [
             {"title": "Female", "categories": [{"measurements": [{"value": "3"}, {"value": "4"}, {"value": "7"}]}]},
             {"title": "Male", "categories": [{"measurements": [{"value": "1"}, {"value": "2"}, {"value": "3"}]}]},
             {"title": "Non-binary", "categories": [{"measurements": [{"value": "1"}, {"value": "0"}, {"value": "1"}]}]},
         ]}
    p = sgp.parse_trial([m])
    assert p.layout == "customized"
    assert (p.n_female, p.n_male, p.n_gender_diverse) == (7.0, 3.0, 1.0)


def test_count_of_units_is_flagged_not_dropped():
    m = _std("Sex: Female, Male", [("Female", 168993), ("Male", 126868)])
    m["paramType"] = "COUNT_OF_UNITS"; m["unitOfMeasure"] = "tests"
    p = sgp.parse_trial([m], enrollment=6)
    assert p.is_participant_count is False
    assert p.flag_nonparticipant_units and p.flag_exceeds_enrollment
    assert sgp.classify_reporting(p) == "reported"       # reporting is unaffected; percent-female consumers filter


def test_percent_and_mean_rows_are_not_participant_counts():
    # v1.1.0 dashboard rule (README D6): percent-like units and MEAN/MEDIAN rows
    # stay in reporting but leave every participant sum.
    m = _std("Sex: Female, Male", [("Female", 48), ("Male", 52)])
    m["paramType"] = "NUMBER"; m["unitOfMeasure"] = "percentage of participants"
    p = sgp.parse_trial([m])
    assert p.flag_percentage_units and p.is_participant_count is False
    assert sgp.classify_reporting(p) == "reported"
    m2 = _std("Sex: Female, Male", [("Female", 0.48), ("Male", 0.52)])
    m2["paramType"] = "MEAN"; m2["unitOfMeasure"] = "participants"
    assert sgp.parse_trial([m2]).is_participant_count is False
    # NUMBER with a participant unit is still a participant count (paper rule kept).
    m3 = _std("Sex: Female, Male", [("Female", 48), ("Male", 52)])
    m3["paramType"] = "NUMBER"; m3["unitOfMeasure"] = "participants"
    assert sgp.parse_trial([m3]).is_participant_count is True


def test_parse_study_record_shape():
    study = {"protocolSection": {"identificationModule": {"nctId": "NCT00000000"},
                                 "designModule": {"enrollmentInfo": {"count": 9}}},
             "resultsSection": {"baselineCharacteristicsModule": {"measures": [_std("Sex: Female, Male", [("Female", 4), ("Male", 5)])]}}}
    row = sgp.parse_study_record(study)
    assert row["nct_id"] == "NCT00000000" and row["sex_report_status"] == "reported"
    assert row["reported_sex"] is True and row["reported_gender"] is False
    assert row["parser_rules_version"] == sgp.PARSER_RULES_VERSION


# --------------------------------------------------------------------------- 2
def _load_fixtures():
    with open(os.path.join(FIX, "fixture_trials.json")) as fh:
        return json.load(fh)["cases"]


def _eq(a, b):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) or isinstance(b, float):
        return math.isclose(float(a), float(b), abs_tol=1e-9)
    return a == b


@pytest.mark.parametrize("case", _load_fixtures(), ids=lambda c: c["nct_id"])
def test_fixture_trial(case):
    row = sgp.parse_measures_row(case["measures"], case["enrollment"], nct_id=case["nct_id"], preselected=True)
    for k, v in case["expected"].items():
        assert _eq(row[k], v), f"{case['nct_id']} {k}: got {row[k]!r}, expected {v!r} (tags {case['tags']})"


# --------------------------------------------------------------------------- 3
def test_vocabulary_lock_against_2026_06_09_audit():
    path = os.path.join(FIX, "label_vocabulary_audit_2026-06-09.csv")
    drift = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            got = sgp.canon_bucket(r["label"])
            if got != r["bucket"]:
                drift.append((r["label"], r["bucket"], got))
    assert not drift, f"{len(drift)} label(s) changed bucket; regenerate the audit file if intended. First: {drift[:5]}"
