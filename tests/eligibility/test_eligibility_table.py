"""Tests for `eligibility/table.py`: row builders, lean projection, error quarantine, and cohort summaries."""
from __future__ import annotations

from typing import Any

import pytest

import eligibility.table as et
from eligibility import (
    DOMAINS,
    VOCABULARY_SHA256,
    all_structural_checks_pass,
    build_eligibility_row,
    lean_eligibility_row,
    parse_error_row,
    structural_checks,
    summarize_cohort_barriers,
)


def _study(
    nct_id: str,
    criteria: Any,
    min_age: str = "18 Years",
    max_age: str = "N/A",
    sex: str = "ALL",
) -> dict[str, Any]:
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id},
            "eligibilityModule": {
                "minimumAge": min_age,
                "maximumAge": max_age,
                "stdAges": ["ADULT", "OLDER_ADULT"],
                "sex": sex,
                "healthyVolunteers": False,
                "eligibilityCriteria": criteria,
            },
        }
    }


def test_build_eligibility_row_standard_oncology_study() -> None:
    criteria = (
        "Inclusion Criteria:\n"
        "- Age 18 to 70 years\n"
        "- ECOG performance status 0 or 1\n"
        "- Total bilirubin <= 1.5 x ULN (<= 3.0 x ULN for Gilbert's syndrome)\n"
        "- eGFR >= 60 mL/min\n"
        "- Able to read and speak English or Spanish\n"
        "Exclusion Criteria:\n"
        "- Pregnant or breastfeeding women\n"
        "- Known HIV positive\n"
        "- Prior malignancy within 3 years, except basal cell skin cancer\n"
    )
    row = build_eligibility_row(_study("NCT99000321", criteria), snapshot_date="2026-11-22")
    assert row["nct_id"] == "NCT99000321"
    assert row["snapshot_date"] == "2026-11-22"
    assert row["eligibility_parse_status"] == "parsed"
    assert row["parse_ok"] is True
    assert row["section_layout"] == "both_sections"
    assert row["has_gilberts_exception"] is True
    assert row["has_skin_or_insitu_exception"] is True
    assert row["ecog_max_allowed"] == 1
    assert row["text_min_age_years"] == 18.0
    assert row["text_max_age_years"] == 70.0
    assert row["hidden_upper_age_cap"] is True
    assert row["pregnancy_or_lactation_status"] == "strictly_excluded"
    assert row["hiv_infection_status"] == "strictly_excluded"
    assert row["hepatic_function_status"] == "threshold_or_conditional"
    assert row["hepatic_function_substatus"] == "exception_permitted"
    assert row["renal_function_status"] == "threshold_or_conditional"
    assert row["renal_function_substatus"] == "numeric_cutoff"
    assert row["language_or_literacy_status"] == "threshold_or_conditional"
    assert row["vocabulary_sha256"] == VOCABULARY_SHA256


def test_build_eligibility_row_deterministic_when_snapshot_date_none() -> None:
    s = _study("NCT99000322", "Inclusion Criteria:\n- Age >= 18 years.\n")
    r1 = build_eligibility_row(s)
    r2 = build_eligibility_row(s)
    assert r1 == r2
    assert r1["snapshot_date"] is None


def test_build_eligibility_row_empty_or_placeholder_not_collapsed_to_error() -> None:
    row = build_eligibility_row(_study("NCT99000323", "Please contact site for information"))
    assert row["eligibility_parse_status"] == "empty_or_placeholder"
    assert row["parse_ok"] is True
    assert row["section_layout"] == "empty_or_placeholder"
    assert row["placeholder_reason"] == "contact_site_placeholder"
    for dom in DOMAINS:
        assert row[f"{dom}_status"] == "not_mentioned"
    assert row["any_structured_discordance"] is False


def test_lean_eligibility_row_strips_evidence_and_copies_dicts() -> None:
    row = build_eligibility_row(
        _study("NCT99000324", "Inclusion Criteria:\n- Age 18 to 65 years.\n"),
        snapshot_date="2026-11-22",
    )
    lean = lean_eligibility_row(row)
    assert "domains" not in lean
    assert "discordance" not in lean
    assert lean["nct_id"] == "NCT99000324"
    assert lean["hidden_upper_age_cap"] is True
    # Mutating lean's domain_statuses dict must not mutate source row
    lean["domain_statuses"]["hiv_infection"] = "mutated"
    assert row["domain_statuses"]["hiv_infection"] == "not_mentioned"


@pytest.mark.parametrize(
    "bad_study",
    [
        None,
        "not_a_dict",
        [1, 2, 3],
        {"nct_id": "NCT99000325", "protocolSection": "corrupted"},
        {"protocolSection": {"identificationModule": {"nctId": "NCT99000326"}, "eligibilityModule": "corrupted"}},
        _study("NCT99000327", ["unexpected", "list", "criteria"]),
        _study("NCT99000328", {"nested": "dict"}),
    ],
)
def test_build_eligibility_row_quarantines_malformed_inputs_to_parse_error(bad_study: Any) -> None:
    row = build_eligibility_row(bad_study, snapshot_date="2026-11-22")
    assert row["eligibility_parse_status"] == "parse_error"
    assert row["parse_ok"] is False
    assert row["parse_error"] is not None
    assert row["section_layout"] is None
    for dom in DOMAINS:
        assert row[f"{dom}_status"] is None
        assert row["domain_statuses"][dom] is None
    assert row["hidden_upper_age_cap"] is None
    assert row["any_structured_discordance"] is None


def test_unexpected_internal_exception_quarantined_without_dropping_trial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated regex engine failure")

    monkeypatch.setattr(et, "parse_eligibility_text", _boom)
    row = build_eligibility_row(_study("NCT99000329", "Inclusion Criteria:\n- Age >= 18\n"))
    assert row["nct_id"] == "NCT99000329"
    assert row["eligibility_parse_status"] == "parse_error"
    assert "RuntimeError: simulated regex engine failure" in row["parse_error"]


def test_summarize_cohort_barriers_and_structural_checks_across_mixed_cohort() -> None:
    studies = [
        # 1. both_sections + hidden upper age cap + Gilbert's + skin/in-situ + contraception + strictly_excluded HIV
        _study(
            "NCT99000331",
            "Inclusion Criteria:\n"
            "- Age 18 to 65 years\n"
            "- Women of childbearing potential must agree to use effective contraception\n"
            "- Total bilirubin <= 1.5 x ULN (<= 3.0 x ULN for Gilbert's syndrome)\n"
            "Exclusion Criteria:\n"
            "- Known HIV positive\n"
            "- Prior malignancy within 3 years, except carcinoma in situ\n",
        ),
        # 2. inclusion_only + targeted HIV + sex discordance
        _study(
            "NCT99000332",
            "Inclusion Criteria:\n"
            "- Female participants only\n"
            "- Confirmed HIV-1 infection\n",
            sex="ALL",
        ),
        # 3. exclusion_only + numeric renal cutoff
        _study(
            "NCT99000333",
            "Exclusion Criteria:\n"
            "- eGFR < 45 mL/min\n",
        ),
        # 4. unheaded_text -> ambiguous_or_unheaded HIV
        _study(
            "NCT99000334",
            "- HIV infection\n- Brain metastases\n",
        ),
        # 5. empty_or_placeholder
        _study("NCT99000335", "N/A"),
        # 6. malformed -> parse_error
        _study("NCT99000336", 99999),
    ]
    rows = [build_eligibility_row(s, snapshot_date="2026-11-22") for s in studies]
    summary = summarize_cohort_barriers(rows)

    assert summary["n_trials"] == 6
    assert summary["parse_status_counts"] == {
        "parsed": 4,
        "empty_or_placeholder": 1,
        "parse_error": 1,
    }
    assert summary["section_layout_counts"] == {
        "both_sections": 1,
        "inclusion_only": 1,
        "exclusion_only": 1,
        "unheaded_text": 1,
        "empty_or_placeholder": 1,
        "parse_error": 1,
    }
    hiv_counts = summary["domain_counts"]["hiv_infection"]
    assert hiv_counts["strictly_excluded"] == 1
    assert hiv_counts["explicitly_included_or_targeted"] == 1
    assert hiv_counts["ambiguous_or_unheaded"] == 1
    assert hiv_counts["not_mentioned"] == 2
    assert hiv_counts["parse_error"] == 1

    assert summary["clinical_exception_counts"]["gilberts_syndrome_allowance"] == 1
    assert summary["clinical_exception_counts"]["skin_or_insitu_malignancy_exception"] == 1
    assert summary["clinical_exception_counts"]["contraception_only_requirement"] == 1
    assert summary["discordance_counts"]["hidden_upper_age_cap"]["true"] == 1
    assert summary["discordance_counts"]["sex_eligibility_discordance"]["true"] == 1
    assert summary["discordance_counts"]["any_structured_discordance"]["parse_error"] == 1

    checks = structural_checks(rows)
    assert all_structural_checks_pass(checks) is True


def test_structural_checks_detect_collapsed_parse_error_and_substatus_mismatch() -> None:
    bad_err = parse_error_row("NCT99000340", "2026-11-22", "err")
    bad_err["hiv_infection_status"] = "not_mentioned"  # forbidden collapse!
    checks = structural_checks([bad_err])
    assert checks["parse_error_rows_never_collapsed"]["pass"] is False
    assert all_structural_checks_pass(checks) is False

    good_row = build_eligibility_row(_study("NCT99000341", "Inclusion Criteria:\n- eGFR >= 60 mL/min\n"))
    good_row["renal_function_substatus"] = None  # broken substatus invariant!
    good_row["domain_substatuses"]["renal_function"] = None
    checks2 = structural_checks([good_row])
    assert checks2["threshold_or_conditional_equals_substatus_sum"]["pass"] is False


def test_build_eligibility_row_missing_eligibility_module_returns_empty_placeholder() -> None:
    study: dict[str, Any] = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT99000360"},
        }
    }
    row = build_eligibility_row(study, snapshot_date="2026-11-22")
    assert row["nct_id"] == "NCT99000360"
    assert row["eligibility_parse_status"] == "empty_or_placeholder"
    assert row["placeholder_reason"] == "missing"
    assert row["raw_criteria_present"] is False
    assert row["raw_criteria_char_len"] == 0


def test_build_eligibility_row_supports_extracted_civicsample_dict() -> None:
    extracted: dict[str, Any] = {
        "nct_id": "NCT99000361",
        "min_age": "18 Years",
        "max_age": "N/A",
        "std_ages": ["ADULT"],
        "eligibility": {"sex": "ALL"},
        "healthy_volunteers": False,
        "eligibility_criteria": "Inclusion Criteria:\n- Age 18 to 55 years\n- ECOG 0-1\n",
    }
    row = build_eligibility_row(extracted, snapshot_date="2026-11-22")
    assert row["nct_id"] == "NCT99000361"
    assert row["eligibility_parse_status"] == "parsed"
    assert row["ecog_max_allowed"] == 1
    assert row["hidden_upper_age_cap"] is True
    assert row["text_max_age_years"] == 55.0


def test_summarize_cohort_barriers_on_empty_cohort() -> None:
    summary = summarize_cohort_barriers([])
    assert summary["n_trials"] == 0
    assert summary["parse_status_counts"] == {"parsed": 0, "empty_or_placeholder": 0, "parse_error": 0}
    checks = structural_checks([])
    assert all_structural_checks_pass(checks) is True


def test_lean_eligibility_row_preserves_parse_error_nones() -> None:
    err_row = parse_error_row("NCT99000362", "2026-11-22", "ValueError: bad")
    lean = lean_eligibility_row(err_row)
    assert lean["nct_id"] == "NCT99000362"
    assert lean["eligibility_parse_status"] == "parse_error"
    assert lean["parse_ok"] is False
    assert lean["section_layout"] is None
    assert lean["hidden_upper_age_cap"] is None
    assert all(v is None for v in lean["domain_statuses"].values())


def test_structural_checks_detect_mixed_vocabulary_sha256_stamps() -> None:
    r1 = build_eligibility_row(_study("NCT99000363", "Inclusion Criteria:\n- Age >= 18\n"))
    r2 = build_eligibility_row(_study("NCT99000364", "Inclusion Criteria:\n- Age >= 21\n"))
    r2["vocabulary_sha256"] = "0" * 64
    checks = structural_checks([r1, r2])
    assert checks["single_pinned_vocabulary_sha256"]["pass"] is False
    assert all_structural_checks_pass(checks) is False


def test_parse_structured_age_years_units_and_errors() -> None:
    from eligibility.vocabulary import parse_structured_age_years

    assert parse_structured_age_years(None) is None
    assert parse_structured_age_years("N/A") is None
    assert parse_structured_age_years("18 Years") == 18.0
    assert parse_structured_age_years("6 Months") == pytest.approx(0.5)
    assert parse_structured_age_years("4 Weeks") == pytest.approx(4.0 / 52.0)
    assert parse_structured_age_years("10 Days") == pytest.approx(10.0 / 365.0)
    with pytest.raises(ValueError, match="Unsupported structured age unit"):
        parse_structured_age_years("5 Decades")


def test_all_twelve_domains_present_in_row_and_lean_row() -> None:
    row = build_eligibility_row(_study("NCT99000365", "Inclusion Criteria:\n- Age 18 to 65 years\n"))
    lean = lean_eligibility_row(row)
    assert set(row["domains"].keys()) == set(DOMAINS)
    assert set(row["domain_statuses"].keys()) == set(DOMAINS)
    assert set(lean["domain_statuses"].keys()) == set(DOMAINS)
    for dom in DOMAINS:
        assert f"{dom}_status" in row
        assert f"{dom}_substatus" in row


def test_json_dumps_allow_nan_false_succeeds_across_all_outputs() -> None:
    import json

    studies = [
        _study("NCT99000370", "Inclusion Criteria:\n- Age 18 to 65 years\n- eGFR >= 60 mL/min\n"),
        _study("NCT99000371", "N/A"),
        _study("NCT99000372", ["bad"]),
    ]
    rows = [build_eligibility_row(s, snapshot_date="2026-11-22") for s in studies]
    leans = [lean_eligibility_row(r) for r in rows]
    summary = summarize_cohort_barriers(rows)
    checks = structural_checks(rows)
    assert json.dumps(rows, allow_nan=False)
    assert json.dumps(leans, allow_nan=False)
    assert json.dumps(summary, allow_nan=False)
    assert json.dumps(checks, allow_nan=False)
