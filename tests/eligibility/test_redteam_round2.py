"""Round-2 adversarial red-team suite auditing Round-1 fixes in `eligibility/`.

Targets second-order bypasses, interaction effects, and boundary cases across:
  1. `_effective_segment_section_kind`, negated Inclusion + clinical exceptions,
     treatment-naive Inclusion clauses (`No prior therapy/radiation/...`, `Must not
     have received prior therapy...`), and mid-clause / comma-joined negations.
  2. `is_gilberts_allowance_in_clause` & `gilberts_uln` across Exclusion `> 3.0 x ULN`,
     negated Gilbert's clauses, `"excluding patients with Gilbert's"`, and Inclusion allowances.
  3. `malignancy_with_skin_or_insitu_exception` vs `malignancy_blanket_exclusion_exc`
     across cutaneous BCC/CIS allowances and melanoma / lung SCC exclusions.
  4. `tighten_lower_bound` / `tighten_upper_bound`, clause-level age negation bleed,
     multi-cohort unbounded unions (`Cohort 1: 18-64`, `Cohort 2: >= 65`), cohort-specific
     exclusions, and `Main Study` / `Extension` multi-cohort headers.
  5. Exact dict equality of `summarize_cohort_barriers` between full and `lean_eligibility_row`
     cohorts across all 12 domains and all 5 layouts + `parse_error`, `structural_checks`
     on lean `parse_error` rows, `bool`-subclassing-`int` quarantine, and 50k-char ReDoS bounds.
"""
from __future__ import annotations

import json
import time
from typing import Any

import pytest

from eligibility import (
    DOMAINS,
    EXPECTED_VOCABULARY_SHA256,
    VOCABULARY_SHA256,
    all_structural_checks_pass,
    build_eligibility_row,
    extract_clause_age_bounds,
    lean_eligibility_row,
    parse_eligibility_text,
    parse_error_row,
    structural_checks,
    summarize_cohort_barriers,
    verify_vocabulary_pin,
)
from eligibility.vocabulary import tighten_lower_bound, tighten_upper_bound


def _study(
    nct_id: str,
    criteria: Any,
    *,
    min_age: Any = "18 Years",
    max_age: Any = "N/A",
    sex: Any = "ALL",
    healthy_volunteers: Any = False,
    std_ages: Any = ("ADULT", "OLDER_ADULT"),
) -> dict[str, Any]:
    std_val = list(std_ages) if isinstance(std_ages, tuple) else std_ages
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id},
            "eligibilityModule": {
                "eligibilityCriteria": criteria,
                "minimumAge": min_age,
                "maximumAge": max_age,
                "sex": sex,
                "healthyVolunteers": healthy_volunteers,
                "stdAges": std_val,
            },
        }
    }


# ===========================================================================
# 1. Negated Inclusion + Exceptions, Treatment-Naive Guards, & Mid-Clause Negation
# ===========================================================================


def test_r2_negated_inclusion_hiv_unless_stable_art_resolves_to_conditional() -> None:
    raw = "Inclusion Criteria:\n - No history of HIV infection unless on stable ART with undetectable viral load"
    row = build_eligibility_row(_study("NCT99400001", raw))
    assert row["hiv_infection_status"] == "threshold_or_conditional"
    assert row["hiv_infection_substatus"] == "exception_permitted"
    assert row["domains"]["hiv_infection"]["extracted_thresholds"]["requires_undetectable_viral_load"] is True


def test_r2_negated_inclusion_cns_unless_treated_and_stable_resolves_to_conditional() -> None:
    raw = "Inclusion Criteria:\n - No brain metastases unless treated and stable for >= 4 weeks"
    row = build_eligibility_row(_study("NCT99400002", raw))
    assert row["cns_or_brain_metastases_status"] == "threshold_or_conditional"
    assert row["cns_or_brain_metastases_substatus"] == "exception_permitted"


def test_r2_negated_inclusion_prior_malignancy_except_basal_cell_resolves_to_conditional() -> None:
    raw = "Inclusion Criteria:\n - No prior malignancy within 3 years except basal cell carcinoma of the skin"
    row = build_eligibility_row(_study("NCT99400003", raw))
    assert row["prior_malignancy_status"] == "threshold_or_conditional"
    assert row["prior_malignancy_substatus"] == "exception_permitted"
    assert row["has_skin_or_insitu_exception"] is True


def test_r2_treatment_naive_no_prior_systemic_therapy_for_brain_mets_stays_targeted() -> None:
    raw = "Inclusion Criteria:\n - No prior systemic therapy for metastatic brain metastases"
    row = build_eligibility_row(_study("NCT99400004", raw))
    assert row["cns_or_brain_metastases_status"] == "explicitly_included_or_targeted"


def test_r2_treatment_naive_no_prior_therapy_for_documented_hiv_stays_targeted() -> None:
    raw = "Inclusion Criteria:\n - No prior antiretroviral therapy for documented HIV-1 infection"
    row = build_eligibility_row(_study("NCT99400005", raw))
    assert row["hiv_infection_status"] == "explicitly_included_or_targeted"


def test_r2_treatment_naive_no_prior_therapy_for_chronic_hcv_stays_targeted() -> None:
    raw = "Inclusion Criteria:\n - No prior antiviral therapy for chronic Hepatitis C infection"
    row = build_eligibility_row(_study("NCT99400006", raw))
    assert row["hepatitis_b_or_c_status"] == "explicitly_included_or_targeted"


def test_r2_semicolon_mixed_polarity_inclusion_targets_hiv_and_excludes_hbv_hcv() -> None:
    raw = "Inclusion Criteria:\n - Documented HIV-1 infection on stable ART; no active Hepatitis B or C infection."
    row = build_eligibility_row(_study("NCT99400007", raw))
    assert row["hiv_infection_status"] == "explicitly_included_or_targeted"
    assert row["hepatitis_b_or_c_status"] == "strictly_excluded"


def test_r2_defect_treatment_naive_no_prior_radiation_for_brain_mets_inverted_to_excluded() -> None:
    """R2 DEFECT: `'No prior radiation for brain metastases'` misses `_NO_PRIOR_THERAPY_GUARD_RX` and inverts to `strictly_excluded`."""
    raw = "Inclusion Criteria:\n - No prior radiation for brain metastases"
    row = build_eligibility_row(_study("NCT99400008", raw))
    assert row["cns_or_brain_metastases_status"] == "explicitly_included_or_targeted"


def test_r2_defect_treatment_naive_no_prior_local_or_systemic_therapy_for_brain_mets() -> None:
    """R2 DEFECT: `'No prior local or systemic therapy for brain metastases'` (>2 modifier tokens) inverts to `strictly_excluded`."""
    raw = "Inclusion Criteria:\n - No prior local or systemic therapy for brain metastases"
    row = build_eligibility_row(_study("NCT99400009", raw))
    assert row["cns_or_brain_metastases_status"] == "explicitly_included_or_targeted"


def test_r2_defect_treatment_naive_must_not_have_received_prior_therapy_brain_mets() -> None:
    """R2 DEFECT: `'Must not have received prior systemic therapy for brain metastases'` inverts to `strictly_excluded`."""
    raw = "Inclusion Criteria:\n - Must not have received prior systemic therapy for brain metastases"
    row = build_eligibility_row(_study("NCT99400010", raw))
    assert row["cns_or_brain_metastases_status"] == "explicitly_included_or_targeted"


def test_r2_defect_treatment_naive_must_not_have_received_prior_antiviral_therapy_hcv() -> None:
    """R2 DEFECT: `'Must not have received prior antiviral therapy for chronic Hepatitis C infection'` inverts to `strictly_excluded`."""
    raw = "Inclusion Criteria:\n - Must not have received prior antiviral therapy for chronic Hepatitis C infection"
    row = build_eligibility_row(_study("NCT99400011", raw))
    assert row["hepatitis_b_or_c_status"] == "explicitly_included_or_targeted"


def test_r2_defect_treatment_naive_without_prior_art_for_hiv_inverted_to_excluded() -> None:
    """R2 DEFECT: `'Without prior antiretroviral therapy for documented HIV-1 infection'` inverts to `strictly_excluded`."""
    raw = "Inclusion Criteria:\n - Without prior antiretroviral therapy for documented HIV-1 infection"
    row = build_eligibility_row(_study("NCT99400012", raw))
    assert row["hiv_infection_status"] == "explicitly_included_or_targeted"


def test_r2_defect_treatment_naive_no_prior_pharmacotherapy_for_mdd_inverted_to_excluded() -> None:
    """R2 DEFECT: `'No prior antidepressant pharmacotherapy for Major Depressive Disorder'` inverts to `strictly_excluded`."""
    raw = "Inclusion Criteria:\n - No prior antidepressant pharmacotherapy for current Major Depressive Disorder"
    row = build_eligibility_row(_study("NCT99400013", raw))
    assert row["psychiatric_or_substance_use_status"] == "explicitly_included_or_targeted"


def test_r2_defect_hiv_positive_patients_are_ineligible_in_inclusion_inverted_to_targeted() -> None:
    """R2 DEFECT: `hiv_target_inclusion` omits `ineligible` in `guard_not_pattern`, inverting `'HIV-positive patients are ineligible'`."""
    raw = "Inclusion Criteria:\n - Age 18 to 75 years (HIV-positive patients are ineligible)."
    row = build_eligibility_row(_study("NCT99400014", raw))
    assert row["hiv_infection_status"] == "strictly_excluded"


def test_r2_defect_mid_clause_must_not_have_cirrhosis_or_chf_inverts_cardiac() -> None:
    """R2 DEFECT: Mid-clause `'who must not have hepatic cirrhosis or congestive heart failure'` inverts `cardiac_function` to targeted."""
    raw = "Inclusion Criteria:\n - Adults >= 18 years who must not have hepatic cirrhosis or congestive heart failure."
    row = build_eligibility_row(_study("NCT99400015", raw))
    assert row["hepatic_function_status"] == "strictly_excluded"
    assert row["cardiac_function_status"] == "strictly_excluded"


def test_r2_defect_comma_joined_no_history_of_hiv_hbv_hcv_drops_hepatitis() -> None:
    """R2 DEFECT: Comma-joined `', with no history of HIV, HBV, or HCV infection'` drops `hepatitis_b_or_c` to `not_mentioned`."""
    raw = "Inclusion Criteria:\n - Histologically confirmed NSCLC, with no history of HIV, HBV, or HCV infection."
    row = build_eligibility_row(_study("NCT99400016", raw))
    assert row["hiv_infection_status"] == "strictly_excluded"
    assert row["hepatitis_b_or_c_status"] == "strictly_excluded"


# ===========================================================================
# 2. Gilbert's Syndrome Allowance & Negation Edge Cases
# ===========================================================================


def test_r2_gilberts_not_permitted_in_exclusion_stays_numeric_cutoff() -> None:
    raw = "Exclusion Criteria:\n - Total bilirubin > 1.5 x ULN; Gilbert's syndrome is not permitted"
    row = build_eligibility_row(_study("NCT99400020", raw))
    assert row["has_gilberts_exception"] is False
    assert row["hepatic_function_status"] == "threshold_or_conditional"
    assert row["hepatic_function_substatus"] == "numeric_cutoff"


def test_r2_gilberts_no_exception_for_in_exclusion_stays_numeric_cutoff() -> None:
    raw = "Exclusion Criteria:\n - Total bilirubin > 1.5 x ULN (no exception for Gilbert's syndrome)"
    row = build_eligibility_row(_study("NCT99400021", raw))
    assert row["has_gilberts_exception"] is False
    assert row["hepatic_function_substatus"] == "numeric_cutoff"


def test_r2_gilberts_inclusion_allowance_extracts_3_uln() -> None:
    raw = "Inclusion Criteria:\n - Total bilirubin <= 1.5 x ULN (<= 3.0 x ULN if Gilbert's syndrome)"
    row = build_eligibility_row(_study("NCT99400022", raw))
    assert row["has_gilberts_exception"] is True
    assert row["hepatic_function_status"] == "threshold_or_conditional"
    assert row["hepatic_function_substatus"] == "exception_permitted"
    assert row["domains"]["hepatic_function"]["extracted_thresholds"]["gilberts_bilirubin_max_uln"] == 3.0


def test_r2_gilberts_negated_in_inclusion_stays_numeric_cutoff() -> None:
    raw = "Inclusion Criteria:\n - Total bilirubin <= 1.5 x ULN; patients with Gilbert's syndrome are excluded"
    row = build_eligibility_row(_study("NCT99400023", raw))
    assert row["has_gilberts_exception"] is False
    assert row["hepatic_function_substatus"] == "numeric_cutoff"


def test_r2_defect_gilberts_exclusion_or_gt_3_uln_missed() -> None:
    """R2 DEFECT: `'Total bilirubin > 1.5 x ULN (or > 3.0 x ULN in patients with Gilbert's syndrome)'` misses `gilberts_uln` and `has_gilberts_exception`."""
    raw = "Exclusion Criteria:\n - Total bilirubin > 1.5 x ULN (or > 3.0 x ULN in patients with Gilbert's syndrome)"
    row = build_eligibility_row(_study("NCT99400024", raw))
    assert row["has_gilberts_exception"] is True
    assert row["hepatic_function_status"] == "threshold_or_conditional"
    assert row["hepatic_function_substatus"] == "exception_permitted"
    assert row["domains"]["hepatic_function"]["extracted_thresholds"]["gilberts_bilirubin_max_uln"] == 3.0


def test_r2_defect_gilberts_excluding_patients_with_gilberts_syndrome_false_allowance() -> None:
    """R2 DEFECT: `'Total bilirubin > 1.5 x ULN; excluding patients with Gilbert's syndrome'` treats `'excluding'` as an allowance cue."""
    raw = "Exclusion Criteria:\n - Total bilirubin > 1.5 x ULN; excluding patients with Gilbert's syndrome"
    row = build_eligibility_row(_study("NCT99400025", raw))
    assert row["has_gilberts_exception"] is False
    assert row["hepatic_function_substatus"] != "exception_permitted"


# ===========================================================================
# 3. Prior Malignancy Skin / In-Situ Allowances vs Non-Cutaneous Exclusions
# ===========================================================================


def test_r2_malignancy_except_bcc_or_cervical_cis_permits_exception() -> None:
    raw = (
        "Exclusion Criteria:\n"
        " - History of malignancy within 5 years, except basal cell carcinoma of the skin or cervical carcinoma in situ"
    )
    row = build_eligibility_row(_study("NCT99400030", raw))
    assert row["prior_malignancy_status"] == "threshold_or_conditional"
    assert row["prior_malignancy_substatus"] == "exception_permitted"
    assert row["has_skin_or_insitu_exception"] is True
    assert row["domains"]["prior_malignancy"]["extracted_thresholds"]["lookback_years"] == 5.0


def test_r2_defect_malignancy_history_of_melanoma_or_lung_scc_missed_by_blanket_exclusion() -> None:
    """R2 DEFECT: `'History of cutaneous melanoma or squamous cell carcinoma of the lung'` in Exclusion resolves to `not_mentioned`."""
    raw = "Exclusion Criteria:\n - History of cutaneous melanoma or squamous cell carcinoma of the lung"
    row = build_eligibility_row(_study("NCT99400031", raw))
    assert row["has_skin_or_insitu_exception"] is False
    assert row["prior_malignancy_status"] == "strictly_excluded"


# ===========================================================================
# 4. Chronological Age Tightening, Clause Negation Bleed, & Multi-Cohort Unions
# ===========================================================================


def test_r2_tighten_lower_and_upper_bound_unit_algebra() -> None:
    assert tighten_lower_bound(None, None, 18.0, True) == (18.0, True)
    assert tighten_lower_bound(18.0, True, 18.0, False) == (18.0, False)
    assert tighten_lower_bound(18.0, False, 18.0, True) == (18.0, False)
    assert tighten_lower_bound(21.0, False, 18.0, True) == (21.0, False)

    assert tighten_upper_bound(None, None, 75.0, True) == (75.0, True)
    assert tighten_upper_bound(75.0, True, 75.0, False) == (75.0, False)
    assert tighten_upper_bound(75.0, False, 75.0, True) == (75.0, False)
    assert tighten_upper_bound(65.0, True, 75.0, False) == (65.0, True)


def test_r2_multi_cohort_unbounded_upper_cohort_yields_no_upper_cap() -> None:
    raw = (
        "Cohort 1 Inclusion Criteria:\n"
        " - Age 18 to 64 years\n"
        "Cohort 2 Inclusion Criteria:\n"
        " - Age >= 65 years\n"
    )
    row = build_eligibility_row(_study("NCT99400040", raw, min_age="18 Years", max_age="N/A"))
    assert row["is_multi_cohort"] is True
    assert row["text_min_age_years"] == 18.0
    assert row["text_max_age_years"] is None
    assert row["hidden_upper_age_cap"] is False
    assert row["std_ages_discordance"] is False


def test_r2_multi_cohort_unbounded_lower_cohort_yields_no_lower_bound() -> None:
    raw = (
        "Cohort 1 Inclusion Criteria:\n"
        " - Age <= 17 years\n"
        "Cohort 2 Inclusion Criteria:\n"
        " - Age 18 to 65 years\n"
    )
    row = build_eligibility_row(
        _study("NCT99400041", raw, min_age="N/A", max_age="65 Years", std_ages=("CHILD", "ADULT", "OLDER_ADULT"))
    )
    assert row["is_multi_cohort"] is True
    assert row["text_min_age_years"] is None
    assert row["text_max_age_years"] == 65.0
    assert row["hidden_lower_age_bound"] is False


def test_r2_defect_age_clause_negation_bleed_from_must_not_be_pregnant() -> None:
    """R2 DEFECT: `'Patients aged >= 18 years who must not be pregnant'` flips `>= 18` to `max_age_years = 18.0`."""
    raw = "Inclusion Criteria:\n - Patients aged >= 18 years who must not be pregnant."
    row = build_eligibility_row(_study("NCT99400042", raw, min_age="18 Years", max_age="N/A"))
    assert row["pregnancy_or_lactation_status"] == "strictly_excluded"
    assert row["text_min_age_years"] == 18.0
    assert row["text_max_age_years"] is None
    assert row["hidden_upper_age_cap"] is False
    assert row["std_ages_discordance"] is False


def test_r2_defect_age_clause_negation_bleed_from_ineligible_hiv_parenthetical() -> None:
    """R2 DEFECT: `'Age 18 to 75 years (HIV-positive patients are ineligible)'` flips `18 to 75` to exclusion polarity."""
    b = extract_clause_age_bounds("Age 18 to 75 years (HIV-positive patients are ineligible).", "inclusion")
    assert b is not None
    assert b["polarity_resolved"] is True
    assert b["min_age_years"] == 18.0
    assert b["max_age_years"] == 75.0


def test_r2_defect_multi_cohort_cohort1_exclusion_ge65_falsely_caps_whole_study() -> None:
    """R2 DEFECT: `Cohort 1 Exclusion Criteria: Age >= 65` is treated as global, falsely capping `Cohort 2: Age >= 65`."""
    raw = (
        "Cohort 1 Inclusion Criteria:\n"
        " - Age >= 18 years\n"
        "Cohort 1 Exclusion Criteria:\n"
        " - Age >= 65 years\n"
        "Cohort 2 Inclusion Criteria:\n"
        " - Age >= 65 years\n"
    )
    row = build_eligibility_row(
        _study("NCT99400043", raw, min_age="18 Years", max_age="N/A", std_ages=("ADULT", "OLDER_ADULT"))
    )
    assert row["is_multi_cohort"] is True
    assert row["text_min_age_years"] == 18.0
    assert row["text_max_age_years"] is None
    assert row["hidden_upper_age_cap"] is False
    assert row["std_ages_discordance"] is False


def test_r2_defect_multi_cohort_main_study_and_extension_headers_collapse_age_union() -> None:
    """R2 DEFECT: `Main Study` and `Extension` headers set `is_multi_cohort=True` with `cohort_label=None`, intersecting disjoint cohorts."""
    raw = (
        "Main Study Inclusion Criteria:\n"
        " - Age 18 to 64 years\n"
        "Extension Inclusion Criteria:\n"
        " - Age 65 to 85 years\n"
    )
    p = parse_eligibility_text(raw)
    assert p.sectioned.is_multi_cohort is True
    age_dom = p.domains["free_text_age_cutoff"]
    assert age_dom.status == "threshold_or_conditional"
    assert age_dom.extracted_thresholds["min_age_years"] == 18.0
    assert age_dom.extracted_thresholds["max_age_years"] == 85.0


# ===========================================================================
# 5. Lean Summary Exact Equality, Structural Checks, Bool-as-Int, & 50k ReDoS
# ===========================================================================


def test_r2_lean_vs_full_summarize_cohort_barriers_exact_dict_equality() -> None:
    studies: list[Any] = [
        None,
        _study("NCT99400050", None),
        _study("NCT99400051", "Exclusion Criteria: No specific exclusion criteria"),
        _study("NCT99400052", "HIV infection. Bilirubin elevation."),
        _study(
            "NCT99400053",
            "Inclusion Criteria:\n- Women of childbearing potential must agree to contraception.\n- Age 18 to 60 years.",
        ),
        _study(
            "NCT99400054",
            "Inclusion Criteria:\n- Singleton pregnancy between 24 and 36 weeks gestation",
            sex="ALL",
        ),
        _study(
            "NCT99400055",
            (
                "Inclusion Criteria:\n"
                "- eGFR >= 60 mL/min; AST <= 2.5 x ULN; LVEF >= 50%; ECOG 0-1\n"
                "Exclusion Criteria:\n"
                "- Known HIV unless undetectable viral load on stable ART\n"
                "- Chronic HBV unless undetectable HBV DNA\n"
                "- Untreated brain metastases (treated and stable >= 4 weeks allowed)\n"
                "- Prior malignancy within 3 years except basal cell skin carcinoma\n"
                "- Total bilirubin > 1.5 x ULN (except Gilbert's syndrome <= 3.0 x ULN)\n"
                "- Severe psychiatric illness\n"
                "- Must be fluent in spoken and written English\n"
            ),
        ),
        _study(
            "NCT99400056",
            "Exclusion Criteria:\n- Pregnant or breastfeeding women; HIV; Hepatitis C; renal failure; hepatic failure; CHF; brain metastases; prior malignancy.",
        ),
    ]
    full_rows = [build_eligibility_row(s, snapshot_date="2026-11-15") for s in studies]
    lean_rows = [lean_eligibility_row(r) for r in full_rows]

    full_summary = summarize_cohort_barriers(full_rows)
    lean_summary = summarize_cohort_barriers(lean_rows)
    assert lean_summary == full_summary

    assert all_structural_checks_pass(structural_checks(full_rows))
    assert all_structural_checks_pass(structural_checks(lean_rows))


def test_r2_defect_structural_checks_misses_collapsed_domain_statuses_on_lean_parse_error_row() -> None:
    """R2 DEFECT: `structural_checks` checks `r.get(f'{d}_status')` only, missing corrupted `domain_statuses` on `lean_eligibility_row`."""
    corrupted_lean = lean_eligibility_row(parse_error_row("NCT99400059", error="synthetic"))
    corrupted_lean["domain_statuses"]["hiv_infection"] = "not_mentioned"
    checks = structural_checks([corrupted_lean])
    assert checks["parse_error_rows_never_collapsed"]["pass"] is False


@pytest.mark.parametrize(
    ("nct_id", "study_payload", "expected_status"),
    [
        ("NCT99400060", {"nct_id": "NCT99400060", "eligibility_criteria": True}, "parse_error"),
        ("NCT99400061", {"nct_id": "NCT99400061", "eligibility_criteria": False}, "parse_error"),
        (
            "NCT99400062",
            _study("NCT99400062", "Inclusion Criteria:\n- Age >= 18 years", min_age=True, max_age=False),
            "parsed",
        ),
        (
            "NCT99400063",
            _study("NCT99400063", "Inclusion Criteria:\n- Age >= 18 years", sex=True, std_ages=[True, False]),
            "parsed",
        ),
    ],
)
def test_r2_bool_subclassing_int_inputs_never_crash_and_serialize_strictly(
    nct_id: str, study_payload: dict[str, Any], expected_status: str
) -> None:
    row = build_eligibility_row(study_payload, snapshot_date="2026-11-15")
    assert row["nct_id"] == nct_id
    assert row["eligibility_parse_status"] == expected_status
    encoded = json.dumps(row, allow_nan=False)
    assert isinstance(encoded, str)


def test_r2_cardiac_bounded_quantifier_handles_50k_chronic_prefix_in_under_100ms() -> None:
    payload = "Inclusion Criteria:\n- " + ("chronic " * 6250) + "heart failureX"
    assert len(payload) > 50000
    t0 = time.perf_counter()
    row = build_eligibility_row(_study("NCT99400070", payload))
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.25, f"50k cardiac payload took {elapsed:.3f}s"
    assert row["parse_ok"] is True


def test_r2_vocabulary_pin_matches_round1_updated_sha256() -> None:
    verify_vocabulary_pin()
    assert VOCABULARY_SHA256 == EXPECTED_VOCABULARY_SHA256
    assert len(DOMAINS) == 12


def test_r2_single_header_inline_placeholders_return_empty_or_placeholder() -> None:
    for idx, text in enumerate(
        (
            "Inclusion Criteria: None",
            "Exclusion Criteria: None known",
            "Key Exclusion Criteria: None listed",
            "Study Eligibility Criteria: N/A",
        ),
        start=71,
    ):
        row = build_eligibility_row(_study("NCT" + f"{99400000 + idx}", text))
        assert row["eligibility_parse_status"] == "empty_or_placeholder"
        assert row["n_clauses"] == 0


def test_r2_midline_header_after_unheaded_sentence_preserves_exact_slices() -> None:
    raw = (
        "Patients with advanced solid tumors. "
        "Inclusion Criteria: ECOG performance status 0-1. "
        "Exclusion Criteria: Pregnant or breastfeeding women."
    )
    parsed = parse_eligibility_text(raw)
    assert parsed.sectioned.section_layout == "both_sections"
    assert parsed.sectioned.has_unheaded_preamble is True
    for s in parsed.sectioned.sections:
        assert raw[s.start_char : s.end_char] == s.verbatim_text
    for c in parsed.sectioned.clauses:
        assert raw[c.start_char : c.end_char] == c.verbatim_text
    assert parsed.domains["pregnancy_or_lactation"].status == "strictly_excluded"


def test_r2_wrapped_qualifier_across_blank_line_extends_attached_exception_span() -> None:
    raw = (
        "Exclusion Criteria:\n"
        "- Untreated CNS metastases\n"
        "\n"
        "  Note: Patients with treated brain metastases that remain\n"
        "  radiographically stable for >= 4 weeks are eligible.\n"
    )
    parsed = parse_eligibility_text(raw)
    cl = parsed.sectioned.clauses[0]
    assert len(cl.attached_exception_spans) == 1
    es, ee = cl.attached_exception_spans[0]
    assert cl.start_char <= es <= ee <= cl.end_char
    assert "radiographically stable for >= 4 weeks are eligible." in raw[es:ee]


def test_r2_hiv_cd4_comma_thousands_parsed_and_huge_cd4_stays_json_safe() -> None:
    r_comma = build_eligibility_row(
        _study(
            "NCT99400075",
            "Exclusion Criteria:\n- HIV infection unless on stable ART with CD4+ count >= 1,200 cells/uL",
        )
    )
    assert r_comma["domains"]["hiv_infection"]["extracted_thresholds"]["cd4_min_cells_ul"] == 1200

    r_huge = build_eligibility_row(
        _study(
            "NCT99400076",
            "Exclusion Criteria:\n- HIV infection unless on stable ART with CD4+ count >= " + ("9" * 400),
        )
    )
    assert isinstance(json.dumps(r_huge, allow_nan=False), str)


def test_r2_combined_non_age_traps_leave_chronological_age_not_mentioned() -> None:
    raw = (
        "Inclusion Criteria:\n"
        "- Singleton pregnancy at 24 to 34 weeks of gestation\n"
        "- Partner who is aged >= 18 years\n"
        "- Caregiver must be aged >= 21 years\n"
        "- Disease onset before age 40 years\n"
    )
    row = build_eligibility_row(_study("NCT99400077", raw, min_age="N/A", max_age="N/A", sex="FEMALE"))
    assert row["free_text_age_cutoff_status"] == "not_mentioned"
    assert row["hidden_lower_age_bound"] is False
    assert row["hidden_upper_age_cap"] is False

