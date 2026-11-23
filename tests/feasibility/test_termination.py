"""Unit tests for `feasibility/termination.py` and `feasibility/termination_reviews.csv`."""
from __future__ import annotations

from pathlib import Path

import pytest

from feasibility.termination import (
    REVIEW_COLUMNS,
    TERMINATION_RULES_VERSION,
    classify_stopped_study_results_state,
    classify_why_stopped,
    evaluate_termination,
    load_termination_reviews,
)
from tests.feasibility.conftest import make_ctgov_study

ROOT = Path(__file__).resolve().parents[2]


def test_rules_version_and_curated_csv_is_header_only() -> None:
    assert TERMINATION_RULES_VERSION == "1.4.1"
    csv_path = ROOT / "feasibility" / "termination_reviews.csv"
    raw_lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert raw_lines == [",".join(REVIEW_COLUMNS)]
    assert load_termination_reviews(csv_path) == {}


@pytest.mark.parametrize(
    ("text", "expected_category"),
    [
        ("Closed early due to slow accrual across participating sites.", "accrual_or_recruitment"),
        ("Interim analysis showed futility on primary efficacy endpoint.", "futility_or_lack_of_efficacy"),
        ("Halted due to dose-limiting toxicity and serious adverse events.", "safety_or_toxicity"),
        ("Terminated following portfolio prioritization and strategic decision.", "business_or_strategic"),
        ("Study enrollment suspended due to the COVID-19 pandemic.", "covid_or_pandemic"),
        ("Placed on clinical hold by FDA pending protocol amendment.", "regulatory_or_clinical_hold"),
        ("Terminated due to study drug shortage and supply chain constraint.", "supply_or_drug_availability"),
        ("Investigator relocated to another institution.", "other_unclassified_text"),
        (None, "missing_why_stopped"),
        ("   ", "missing_why_stopped"),
    ],
)
def test_single_signal_and_fallback_categories(text: str | None, expected_category: str) -> None:
    res = classify_why_stopped(text)
    assert res["deterministic_category"] == expected_category


def test_negation_guards_prevent_false_positives() -> None:
    # Real CT.gov patterns where safety, COVID-19, or completed recruitment are explicitly negated.
    r1 = classify_why_stopped(
        "Terminated due to strategic portfolio reprioritization; there were no safety concerns."
    )
    assert r1["deterministic_category"] == "business_or_strategic"
    assert r1["matched_signals"] == ["business_or_strategic"]

    r2 = classify_why_stopped(
        "Recruitment was fully completed for this study; terminated after primary endpoint analysis due to futility."
    )
    assert r2["deterministic_category"] == "futility_or_lack_of_efficacy"
    assert r2["matched_signals"] == ["futility_or_lack_of_efficacy"]

    r3 = classify_why_stopped("Withdrawn due to loss of funding, unrelated to COVID-19 or safety.")
    assert r3["deterministic_category"] == "business_or_strategic"
    assert r3["matched_signals"] == ["business_or_strategic"]


def test_multiple_signals_detected_preserves_all_matched_categories() -> None:
    r_eff_tox = classify_why_stopped(
        "Discontinued due to modest clinical benefit and limited tolerability."
    )
    assert r_eff_tox["deterministic_category"] == "multiple_signals_detected"
    assert r_eff_tox["matched_signals"] == [
        "futility_or_lack_of_efficacy",
        "safety_or_toxicity",
    ]

    r_three = classify_why_stopped(
        "COVID-19 pandemic caused slow enrollment and study drug supply shortage."
    )
    assert r_three["deterministic_category"] == "multiple_signals_detected"
    assert r_three["matched_signals"] == [
        "accrual_or_recruitment",
        "covid_or_pandemic",
        "supply_or_drug_availability",
    ]


def test_human_review_override_merge_and_csv_validation(tmp_path: Path) -> None:
    valid_csv = tmp_path / "reviews.csv"
    valid_csv.write_text(
        "nct_id,reviewer,reviewed_at,override_category,notes\n"
        "NCT99100050,Dr Fictional,2026-11-20,safety_or_toxicity,Protocol amendment cited DLT details\n",
        encoding="utf-8",
    )
    loaded = load_termination_reviews(valid_csv)
    assert "NCT99100050" in loaded

    study = make_ctgov_study(
        "NCT99100050",
        overall_status="TERMINATED",
        why_stopped="See detailed description.",
    )
    merged = evaluate_termination(
        study,
        "TERMINATED",
        "See detailed description.",
        review=loaded["NCT99100050"],
    )
    assert merged["classification_source"] == "human_review_override"
    assert merged["category"] == "safety_or_toxicity"
    assert merged["deterministic_category"] == "other_unclassified_text"
    assert merged["why_stopped_verbatim"] == "See detailed description."
    assert "reviewer" not in merged
    assert "notes" not in merged

    # Invalid CSV cases must raise ValueError loudly.
    bad_header = tmp_path / "bad_header.csv"
    bad_header.write_text("nct_id,override_category\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected exact header"):
        load_termination_reviews(bad_header)

    dup_csv = tmp_path / "dup.csv"
    dup_csv.write_text(
        "nct_id,reviewer,reviewed_at,override_category,notes\n"
        "NCT99100050,Rev A,2026-11-20,safety_or_toxicity,Note 1\n"
        "NCT99100050,Rev B,2026-11-21,business_or_strategic,Note 2\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate review override"):
        load_termination_reviews(dup_csv)

    blank_note = tmp_path / "blank_note.csv"
    blank_note.write_text(
        "nct_id,reviewer,reviewed_at,override_category,notes\n"
        "NCT99100050,Rev A,2026-11-20,safety_or_toxicity,   \n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="notes must be non-empty"):
        load_termination_reviews(blank_note)


def test_stopped_study_results_states() -> None:
    analysed = make_ctgov_study("NCT99100060", overall_status="TERMINATED", has_results=True)
    assert classify_stopped_study_results_state(analysed) == "results_with_analysed_primary"

    empty_shell = make_ctgov_study(
        "NCT99100061",
        overall_status="TERMINATED",
        has_results=True,
        outcome_measures=[
            {
                "type": "PRIMARY",
                "title": "Primary Endpoint",
                "groups": [{"id": "OG000", "title": "Arm A"}, {"id": "OG001", "title": "Arm B"}],
                "denoms": [
                    {
                        "units": "Participants",
                        "counts": [
                            {"groupId": "OG000", "value": "0"},
                            {"groupId": "OG001", "value": "0"},
                        ],
                    }
                ],
            }
        ],
    )
    assert classify_stopped_study_results_state(empty_shell) == "empty_results_shell_zero_analysed"

    no_denom = make_ctgov_study(
        "NCT99100062",
        overall_status="SUSPENDED",
        has_results=True,
        outcome_measures=[
            {
                "type": "PRIMARY",
                "title": "Primary Endpoint Without Denom",
                "groups": [{"id": "OG000", "title": "Arm A"}],
            }
        ],
    )
    assert classify_stopped_study_results_state(no_denom) == "results_posted_no_primary_denom"

    not_posted = make_ctgov_study("NCT99100063", overall_status="WITHDRAWN", has_results=False)
    assert classify_stopped_study_results_state(not_posted) == "results_not_posted"


@pytest.mark.parametrize(
    "reviewed_at, ok",
    [
        ("2026-11-20", True),
        ("2026-11-20T14:05:00Z", True),
        ("2026-11-20T14:05:00+00:00", True),
        ("2026-11-20garbage", False),
        ("2026-11-20 extra", False),
        ("2026-02-30", False),
        ("2026-11-20T99:00:00Z", False),
        ("20-11-2026", False),
    ],
)
def test_reviewed_at_is_validated_whole(tmp_path: Path, reviewed_at: str, ok: bool) -> None:
    path = tmp_path / "reviews.csv"
    path.write_text(
        "nct_id,reviewer,reviewed_at,override_category,notes\n"
        f"NCT99100051,Dr Fictional,{reviewed_at},safety_or_toxicity,Fictional note\n",
        encoding="utf-8",
    )
    if ok:
        assert load_termination_reviews(path)["NCT99100051"]["reviewed_at"] == reviewed_at
    else:
        with pytest.raises(ValueError, match="reviewed_at"):
            load_termination_reviews(path)


@pytest.mark.parametrize(
    "why, category",
    [
        # "not" negates the verb, and the signal is the stated reason
        ("Could not continue due to COVID-19", "covid_or_pandemic"),
        ("Could not achieve recruitment goal due to COVID delays and staffing.", "covid_or_pandemic"),
        ("no further enrolment due to slow accrual", "accrual_or_recruitment"),
        ("Non-safety related business decision", "business_or_strategic"),
        # explicit negations of the signal stay masked
        ("not due to COVID-19", "other_unclassified_text"),
        ("Study stopped, unrelated to the COVID-19 pandemic", "other_unclassified_text"),
        ("The study was not terminated due to safety concerns", "other_unclassified_text"),
        ("Not considered a safety issue", "other_unclassified_text"),
        ("Decision was not based on safety or efficacy of the study product.", "other_unclassified_text"),
        ("not because of safety or tolerability", "other_unclassified_text"),
        ("The study was prematurely terminated for strategic reasons, not for a safety concern.", "business_or_strategic"),
        ("Terminated for strategic reasons; there were no safety concerns.", "business_or_strategic"),
        ("terminated prior to the COVID pandemic due to low enrollment", "accrual_or_recruitment"),
    ],
)
def test_negation_masks_only_a_signal_it_governs(why: str, category: str) -> None:
    assert classify_why_stopped(why)["deterministic_category"] == category


@pytest.mark.parametrize(
    "why, category",
    [
        # rules 1.2.0: "no" heading the noun phrase that holds COVID negates it
        ("No COVID-19 impact; terminated for strategic reasons", "business_or_strategic"),
        ("there were no COVID-19 related issues", "other_unclassified_text"),
        ("Terminated for strategic reasons; there were no significant COVID-19 related delays.", "business_or_strategic"),
        ("Sponsor decision. No COVID or pandemic impact on the study.", "business_or_strategic"),
        # "no" governs something else, so COVID stays the stated cause or circumstance
        ("Could not continue due to COVID-19", "covid_or_pandemic"),
        ("Could not achieve recruitment goal due to COVID delays and staffing.", "covid_or_pandemic"),
        ("No further enrollment due to COVID-19", "covid_or_pandemic"),
        ("No enrollment during the COVID-19 pandemic", "covid_or_pandemic"),
        ("No participants enrolled during the COVID-19 pandemic", "multiple_signals_detected"),
    ],
)
def test_no_masks_covid_only_in_the_phrase_it_heads(why: str, category: str) -> None:
    assert classify_why_stopped(why)["deterministic_category"] == category


@pytest.mark.parametrize(
    "why, category",
    [
        # rules 1.3.0: "no" + business word + reason noun denies a business cause
        ("No business reason; investigator relocated", "other_unclassified_text"),
        ("There were no strategic reasons", "other_unclassified_text"),
        ("No business or strategic reasons; PI left", "other_unclassified_text"),
        ("no specific business reasons", "other_unclassified_text"),
        ("No strategic reasons; slow accrual", "accrual_or_recruitment"),
        # without a reason noun, "no" governs something else and the business cause stands (real
        # weekly whyStopped texts, apart from the first two)
        ("No financial support", "business_or_strategic"),
        ("No commercial interest", "business_or_strategic"),
        ("App developer is no longer in business", "business_or_strategic"),
        ("Study no longer met business objectives", "business_or_strategic"),
        ("Stopped by sponsor. No interest in continuing trial due to business changes.", "business_or_strategic"),
        ("No safety concerns; strategic reasons", "business_or_strategic"),
        ("There were no financial concerns; sponsor withdrew support due to lack of funding", "business_or_strategic"),
    ],
)
def test_no_masks_business_only_when_it_denies_a_reason(why: str, category: str) -> None:
    assert classify_why_stopped(why)["deterministic_category"] == category


@pytest.mark.parametrize(
    "why, category",
    [
        # rules 1.4.0: "not ... due to" / "unrelated to" exclude every phrase the detector matches
        ("Not due to FDA feedback; terminated for strategic reasons", "business_or_strategic"),
        ("Not due to manufacturing issues; terminated for strategic reasons", "business_or_strategic"),
        ("Not related to drug supply; sponsor decision", "business_or_strategic"),
        # rules 1.4.1: one excluded phrase for each alternative of the supply detector, in its order
        ("Not due to a study drug shortage; terminated for strategic reasons", "business_or_strategic"),
        ("Not due to a lack of supply; terminated for strategic reasons", "business_or_strategic"),
        ("Not due to a manufacturing delay; terminated for strategic reasons", "business_or_strategic"),
        ("Not due to an unavailable study drug; terminated for strategic reasons", "business_or_strategic"),
        ("Unrelated to unavailable medication; funding ended", "business_or_strategic"),
        ("Not based on a regulatory decision; funding ended", "business_or_strategic"),
        ("Unrelated to the clinical hold; funding ended", "business_or_strategic"),
        # rules 1.4.0: "no" / "without" deny only a supply problem noun; "no drug supply" states one
        # (real weekly whyStopped texts)
        ("Company supplying drug declared bankruptcy, thus there was no drug supply.", "supply_or_drug_availability"),
        ("slow recruitment and no further drug supply", "multiple_signals_detected"),
        ("There was no shortage; business reasons", "business_or_strategic"),
        ("No supply issues; sponsor decision", "business_or_strategic"),
        # controls: the stated causes stand
        ("Terminated due to FDA feedback", "regulatory_or_clinical_hold"),
        ("Manufacturing issues", "supply_or_drug_availability"),
        ("Terminated because of unavailable study drug", "supply_or_drug_availability"),
        ("Clinical hold", "regulatory_or_clinical_hold"),
    ],
)
def test_linked_negations_cover_every_detected_phrase(why: str, category: str) -> None:
    assert classify_why_stopped(why)["deterministic_category"] == category


def _primary(title: str, counts: list | None) -> dict:
    """One PRIMARY measure; `counts=None` leaves out the denominator row."""
    measure: dict = {"type": "PRIMARY", "title": title, "groups": [{"id": "OG000"}, {"id": "OG001"}]}
    if counts is not None:
        measure["denoms"] = [{"units": "Participants", "counts": counts}]
    return measure


def _cnt(*values: object) -> list:
    return [{"groupId": f"OG00{i}", "value": v} for i, v in enumerate(values)]


@pytest.mark.parametrize(
    "measures, state",
    [
        # rules 1.3.0: zero analysed is established only when every primary count is present
        ([_primary("A", _cnt("0", "NA"))], "results_posted_primary_denom_incomplete"),
        ([_primary("A", _cnt("0", "-1"))], "results_posted_primary_denom_incomplete"),
        ([_primary("A", _cnt("0", None))], "results_posted_primary_denom_incomplete"),
        ([_primary("A", _cnt("0") + ["not an object"])], "results_posted_primary_denom_incomplete"),
        ([_primary("A", _cnt("0", "0")), _primary("B", None)], "results_posted_primary_denom_incomplete"),
        ([_primary("A", _cnt("0", "0")), _primary("B", [])], "results_posted_primary_denom_incomplete"),
        # counts must match the measure's groups one to one (both measures list OG000 and OG001)
        ([_primary("A", _cnt("0"))], "results_posted_primary_denom_incomplete"),
        ([_primary("A", _cnt("0", "0") + [{"groupId": "OG000", "value": "0"}])],
         "results_posted_primary_denom_incomplete"),
        ([_primary("A", [{"groupId": "OG000", "value": "0"}, {"groupId": "OG009", "value": "0"}])],
         "results_posted_primary_denom_incomplete"),
        ([_primary("A", [{"groupId": "OG000", "value": "0"}, {"groupId": "OG000", "value": "0"}])],
         "results_posted_primary_denom_incomplete"),
        ([{**_primary("A", _cnt("0", "0")), "groups": []}], "results_posted_primary_denom_incomplete"),
        # a measure that is not an object could be a primary one
        (["Primary outcome", _primary("A", _cnt("0", "0"))], "results_posted_primary_denom_incomplete"),
        # malformed ids and units are incomplete or unmatched, never an exception that stops the build
        ([_primary("A", [{"groupId": ["OG000"], "value": "0"}, {"groupId": "OG001", "value": "0"}])],
         "results_posted_primary_denom_incomplete"),
        ([_primary("A", [{"groupId": {"id": "OG000"}, "value": "0"}, {"groupId": "OG001", "value": "0"}])],
         "results_posted_primary_denom_incomplete"),
        ([{**_primary("A", None), "denoms": [{"units": ["Participants"], "counts": _cnt("0", "0")}]}],
         "empty_results_shell_zero_analysed"),
        ([{**_primary("A", None), "denoms": [{"units": 5, "counts": _cnt("0", "NA")}]}],
         "results_posted_primary_denom_incomplete"),
        # rules 1.4.0: two Participants rows; the first no longer decides by order
        ([{**_primary("A", None), "denoms": [{"units": "Participants", "counts": _cnt("0", "0")},
                                              {"units": "Participants", "counts": _cnt("4", "6")}]}],
         "results_with_analysed_primary"),
        ([{**_primary("A", None), "denoms": [{"units": "Participants", "counts": _cnt("0", "0")},
                                              {"units": "participants", "counts": _cnt("0", "0")}]}],
         "results_posted_primary_denom_incomplete"),
        # controls: complete zeros, one positive count anywhere, no numeric count at all
        ([_primary("A", _cnt("0", "0")), _primary("B", _cnt("0", "0"))], "empty_results_shell_zero_analysed"),
        ([_primary("A", _cnt("0", "NA")), _primary("B", _cnt("5", "0"))], "results_with_analysed_primary"),
        (
            [_primary("A", _cnt("0", "0")), _primary("B", None), _primary("C", _cnt("3"))],
            "results_with_analysed_primary",
        ),
        ([_primary("A", None)], "results_posted_no_primary_denom"),
        ([_primary("A", _cnt("NA", "NA"))], "results_posted_no_primary_denom"),
    ],
)
def test_zero_analysed_shell_needs_every_primary_count(measures: list, state: str) -> None:
    study = make_ctgov_study("NCT99100070", overall_status="TERMINATED", has_results=True, outcome_measures=measures)
    assert classify_stopped_study_results_state(study) == state
