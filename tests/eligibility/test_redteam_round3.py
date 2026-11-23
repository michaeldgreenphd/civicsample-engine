"""Round-3 clean-sweep adversarial verification suite for `eligibility/`.

Exercises all 6 Round-2 fix surfaces (`R2-D1`..`R2-D6`) for third-order
interactions, slice/JSON invariants, and pinning consistency:
  1. Treatment-naive Inclusion modalities (`have not received`, `free of prior`,
     `undergone surgical resection`, multi-modifier regimens, `to treat`).
  2. Compound Inclusion clauses mixing positive disease/age criteria with
     relative/comma-negated exclusions (`who must not have`, `, with no active`,
     `, and no history of`), plus `not eligible` / `will be excluded` HIV guards.
  3. Match-local chronological age polarity (`_age_match_polarity`): preserving
     both self-negated Inclusion age bounds (`Must not be under 18 or over 70`,
     `Patients aged < 18 years are ineligible`) and upstream positive age bounds
     alongside downstream domain negations.
  4. Gilbert's syndrome allowance vs negation (`exceeding 3.5 x ULN`, `above 3.0 x ULN`,
     `patients with Gilbert's syndrome are ineligible`, `no exception for Gilbert's`).
  5. Multi-cohort age envelope across `Run-in`, `Lead-in`, `Screening`, `Portion`,
     and `Segment` headers with cohort-scoped Exclusion caps.
  6. Hematologic/mesenchymal prior-malignancy exclusions (`lymphoma`, `sarcoma`,
     `leukemia`, `myeloma`) and `structural_checks` detection of corrupted
     `domain_statuses` and `domain_substatuses` on `lean_eligibility_row` records.
"""
from __future__ import annotations

import json
import time
from typing import Any

from eligibility import (
    DOMAINS,
    EXPECTED_VOCABULARY_SHA256,
    VOCABULARY_SHA256,
    all_structural_checks_pass,
    build_eligibility_row,
    lean_eligibility_row,
    parse_eligibility_sections,
    parse_eligibility_text,
    parse_error_row,
    structural_checks,
    summarize_cohort_barriers,
    verify_vocabulary_pin,
)


def _study(
    nct_id: str,
    criteria: str,
    *,
    min_age: str = "18 Years",
    max_age: str = "N/A",
    sex: str = "ALL",
    healthy_volunteers: bool = False,
    std_ages: tuple[str, ...] = ("ADULT", "OLDER_ADULT"),
) -> dict[str, Any]:
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id},
            "eligibilityModule": {
                "eligibilityCriteria": criteria,
                "minimumAge": min_age,
                "maximumAge": max_age,
                "sex": sex,
                "healthyVolunteers": healthy_volunteers,
                "stdAges": list(std_ages),
            },
        }
    }


def _assert_slices_and_json(raw: str, row: dict[str, Any]) -> None:
    assert row["parse_ok"] is True
    for dom in DOMAINS:
        for ev in row["domains"][dom]["evidence"]:
            assert 0 <= ev["start_char"] <= ev["end_char"] <= len(raw)
            assert raw[ev["start_char"] : ev["end_char"]] == ev["verbatim_text"]
    for dev in row["discordance"]["evidence"]:
        assert 0 <= dev["start_char"] <= dev["end_char"] <= len(raw)
        assert raw[dev["start_char"] : dev["end_char"]] == dev["verbatim_text"]
    assert isinstance(json.dumps(row, allow_nan=False), str)
    assert isinstance(json.dumps(lean_eligibility_row(row), allow_nan=False), str)


# ---------------------------------------------------------------------------
# R2-D1 Clean-Sweep: Treatment-Naive Inclusion Guards
# ---------------------------------------------------------------------------


def test_r3_treatment_naive_have_not_undergone_surgical_resection_for_brain_mets() -> None:
    raw = "Inclusion Criteria:\n - Patients who have not undergone surgical resection for active brain metastases"
    row = build_eligibility_row(_study("NCT99500001", raw))
    _assert_slices_and_json(raw, row)
    assert row["cns_or_brain_metastases_status"] == "explicitly_included_or_targeted"


def test_r3_treatment_naive_five_modifier_regimen_to_treat_hiv1() -> None:
    raw = (
        "Inclusion Criteria:\n"
        " - Adults free of prior oral or injectable long-acting antiretroviral therapy to treat documented HIV-1 infection"
    )
    row = build_eligibility_row(_study("NCT99500002", raw))
    _assert_slices_and_json(raw, row)
    assert row["hiv_infection_status"] == "explicitly_included_or_targeted"


def test_r3_treatment_naive_cannot_have_received_biologic_for_chronic_hbv() -> None:
    raw = "Inclusion Criteria:\n - Participants cannot have received prior systemic biologic therapy for chronic Hepatitis B infection"
    row = build_eligibility_row(_study("NCT99500003", raw))
    _assert_slices_and_json(raw, row)
    assert row["hepatitis_b_or_c_status"] == "explicitly_included_or_targeted"


# ---------------------------------------------------------------------------
# R2-D2 Clean-Sweep: Compound Positive + Mid-Clause Negated Segments
# ---------------------------------------------------------------------------


def test_r3_compound_inclusion_positive_age_plus_relative_negated_cns_and_hiv() -> None:
    raw = (
        "Inclusion Criteria:\n"
        " - Patients aged >= 18 years with NSCLC, who must not have active brain metastases or HIV-1 infection."
    )
    row = build_eligibility_row(_study("NCT99500004", raw))
    _assert_slices_and_json(raw, row)
    assert row["text_min_age_years"] == 18.0
    assert row["text_max_age_years"] is None
    assert row["cns_or_brain_metastases_status"] == "strictly_excluded"
    assert row["hiv_infection_status"] == "strictly_excluded"


def test_r3_compound_inclusion_positive_target_plus_comma_and_no_history_of_hbv_hcv() -> None:
    raw = (
        "Inclusion Criteria:\n"
        " - Documented HIV-1 infection with plasma HIV-1 RNA >= 1,000 copies/mL, and no history of HBV or HCV."
    )
    row = build_eligibility_row(_study("NCT99500005", raw))
    _assert_slices_and_json(raw, row)
    assert row["hiv_infection_status"] == "explicitly_included_or_targeted"
    assert row["hepatitis_b_or_c_status"] == "strictly_excluded"


def test_r3_hiv_positive_not_eligible_and_will_be_excluded_guards_in_inclusion() -> None:
    raw = "Inclusion Criteria:\n - HIV-positive participants will be excluded from enrollment."
    row = build_eligibility_row(_study("NCT99500006", raw))
    _assert_slices_and_json(raw, row)
    assert row["hiv_infection_status"] == "strictly_excluded"


# ---------------------------------------------------------------------------
# R2-D3 Clean-Sweep: Match-Local Age Polarity Preservation
# ---------------------------------------------------------------------------


def test_r3_self_negated_inclusion_age_prefix_still_inverts_cleanly() -> None:
    raw = "Inclusion Criteria:\n - Participants must not be under 18 or over 70 years of age."
    row = build_eligibility_row(_study("NCT99500007", raw, min_age="18 Years", max_age="70 Years"))
    _assert_slices_and_json(raw, row)
    assert row["text_min_age_years"] == 18.0
    assert row["text_max_age_years"] == 70.0
    assert row["conflicting_age_bounds"] is False


def test_r3_self_negated_inclusion_age_suffix_are_ineligible_inverts_cleanly() -> None:
    raw = "Inclusion Criteria:\n - Patients aged < 18 years are ineligible."
    row = build_eligibility_row(_study("NCT99500008", raw, min_age="N/A", max_age="N/A"))
    _assert_slices_and_json(raw, row)
    assert row["text_min_age_years"] == 18.0
    assert row["text_max_age_years"] is None
    assert row["hidden_lower_age_bound"] is True


def test_r3_positive_age_range_unaffected_by_upstream_and_downstream_domain_negations() -> None:
    raw = (
        "Inclusion Criteria:\n"
        " - Not pregnant or breastfeeding, aged 21 to 65 years, and must not have hepatic cirrhosis."
    )
    row = build_eligibility_row(_study("NCT99500009", raw, min_age="21 Years", max_age="N/A"))
    _assert_slices_and_json(raw, row)
    assert row["pregnancy_or_lactation_status"] == "strictly_excluded"
    assert row["hepatic_function_status"] == "strictly_excluded"
    assert row["text_min_age_years"] == 21.0
    assert row["text_max_age_years"] == 65.0
    assert row["hidden_upper_age_cap"] is True


# ---------------------------------------------------------------------------
# R2-D4 Clean-Sweep: Gilbert's Allowance vs Explicit Exclusion
# ---------------------------------------------------------------------------


def test_r3_gilberts_exclusion_exceeding_3_5_uln_extracts_threshold_and_allowance() -> None:
    raw = "Exclusion Criteria:\n - Total bilirubin >= 2.0 x ULN (exceeding 3.5 x ULN in Gilbert's syndrome)"
    row = build_eligibility_row(_study("NCT99500010", raw))
    _assert_slices_and_json(raw, row)
    assert row["has_gilberts_exception"] is True
    assert row["hepatic_function_status"] == "threshold_or_conditional"
    assert row["hepatic_function_substatus"] == "exception_permitted"
    thresh = row["domains"]["hepatic_function"]["extracted_thresholds"]
    assert thresh["bilirubin_max_uln"] == 2.0
    assert thresh["gilberts_bilirubin_max_uln"] == 3.5


def test_r3_gilberts_patients_ineligible_with_uln_suppresses_gilberts_flag() -> None:
    raw = "Exclusion Criteria:\n - Total bilirubin > 1.5 x ULN (patients with Gilbert's syndrome are ineligible)"
    row = build_eligibility_row(_study("NCT99500011", raw))
    _assert_slices_and_json(raw, row)
    assert row["has_gilberts_exception"] is False
    assert row["hepatic_function_substatus"] == "numeric_cutoff"


# ---------------------------------------------------------------------------
# R2-D5 Clean-Sweep: Multi-Cohort Labels & Cohort-Scoped Exclusions
# ---------------------------------------------------------------------------


def test_r3_run_in_and_lead_in_headers_extract_distinct_cohort_labels_and_scope_exclusion() -> None:
    raw = (
        "Run-in Inclusion Criteria:\n"
        " - Age 18 to 55 years\n"
        "Lead-in Inclusion Criteria:\n"
        " - Age 56 to 80 years\n"
        "Lead-in Exclusion Criteria:\n"
        " - Age > 75 years\n"
    )
    sec = parse_eligibility_sections(raw)
    assert sec.is_multi_cohort is True
    assert [s.cohort_label for s in sec.sections] == ["Run-in", "Lead-in", "Lead-in"]

    row = build_eligibility_row(_study("NCT99500012", raw, min_age="18 Years", max_age="75 Years"))
    _assert_slices_and_json(raw, row)
    assert row["text_min_age_years"] == 18.0
    assert row["text_max_age_years"] == 75.0
    assert row["conflicting_age_bounds"] is False


def test_r3_portion_and_segment_headers_union_disjoint_pediatric_and_adult_cohorts() -> None:
    raw = (
        "Portion A Inclusion Criteria:\n"
        " - Children aged 6 to 17 years\n"
        "Segment 2 Inclusion Criteria:\n"
        " - Adults aged 18 to 70 years\n"
    )
    row = build_eligibility_row(
        _study("NCT99500013", raw, min_age="6 Years", max_age="70 Years", std_ages=("CHILD", "ADULT", "OLDER_ADULT"))
    )
    _assert_slices_and_json(raw, row)
    assert row["is_multi_cohort"] is True
    assert row["text_min_age_years"] == 6.0
    assert row["text_max_age_years"] == 70.0
    assert row["std_ages_discordance"] is False


def test_r3_global_exclusion_still_caps_multi_cohort_union_envelope() -> None:
    raw = (
        "Main Study Inclusion Criteria:\n"
        " - Age 18 to 80 years\n"
        "Extension Inclusion Criteria:\n"
        " - Age 18 to 85 years\n"
        "Exclusion Criteria:\n"
        " - Age > 72 years\n"
    )
    row = build_eligibility_row(_study("NCT99500014", raw, min_age="18 Years", max_age="N/A"))
    _assert_slices_and_json(raw, row)
    assert row["text_min_age_years"] == 18.0
    assert row["text_max_age_years"] == 72.0
    assert row["hidden_upper_age_cap"] is True


# ---------------------------------------------------------------------------
# R2-D6 Clean-Sweep: Malignancy Histologies & Lean `structural_checks`
# ---------------------------------------------------------------------------


def test_r3_malignancy_blanket_exclusion_covers_lymphoma_sarcoma_leukemia_myeloma() -> None:
    for idx, hist in enumerate(("lymphoma", "sarcoma", "leukemia", "myeloma"), start=15):
        raw = f"Exclusion Criteria:\n - History of prior {hist}"
        row = build_eligibility_row(_study("NCT" + f"{99500000 + idx}", raw))
        _assert_slices_and_json(raw, row)
        assert row["prior_malignancy_status"] == "strictly_excluded"
        assert row["has_skin_or_insitu_exception"] is False


def test_r3_structural_checks_rejects_corrupted_lean_domain_substatuses_on_parse_error() -> None:
    corrupt_lean = lean_eligibility_row(parse_error_row("NCT99500020", error="synthetic"))
    corrupt_lean["domain_substatuses"]["renal_function"] = "numeric_cutoff"
    checks = structural_checks([corrupt_lean])
    assert checks["parse_error_rows_never_collapsed"]["pass"] is False
    assert all_structural_checks_pass(checks) is False


def test_r3_structural_checks_and_summary_pass_on_valid_mixed_lean_cohort() -> None:
    r_ok = build_eligibility_row(_study("NCT99500021", "Inclusion Criteria:\n - Age 18 to 65 years"))
    r_empty = build_eligibility_row(_study("NCT99500022", "N/A"))
    r_err = parse_error_row("NCT99500023", error="bad payload")
    full_cohort = [r_ok, r_empty, r_err]
    lean_cohort = [lean_eligibility_row(r) for r in full_cohort]

    assert summarize_cohort_barriers(lean_cohort) == summarize_cohort_barriers(full_cohort)
    assert all_structural_checks_pass(structural_checks(lean_cohort))


# ---------------------------------------------------------------------------
# Performance & Vocabulary Pin Clean-Sweep
# ---------------------------------------------------------------------------


def test_r3_no_prior_therapy_and_segment_split_25k_adversarial_latency() -> None:
    raw = "Inclusion Criteria:\n - " + ("oral-antiviral " * 1600) + "therapy for chronic Hepatitis C infection, " + (
        "with no active HIV, " * 200
    )
    t0 = time.perf_counter()
    row = build_eligibility_row(_study("NCT99500024", raw))
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.5, f"Elapsed {elapsed:.3f}s >= 0.5s"
    _assert_slices_and_json(raw, row)


def test_r3_vocabulary_pin_and_round2_sha256_integrity() -> None:
    verify_vocabulary_pin()
    assert VOCABULARY_SHA256 == EXPECTED_VOCABULARY_SHA256
    assert len(VOCABULARY_SHA256) == 64
    p = parse_eligibility_text("Inclusion Criteria:\n - Age >= 18 years")
    assert p.vocabulary_sha256 == EXPECTED_VOCABULARY_SHA256
