"""Tests for `VOCABULARY_SHA256` pinning, all 12 domains, polarity algebra, and threshold extraction."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from eligibility.parser import DOMAIN_STATUS_LEVELS, ParsedEligibility, parse_eligibility_text
from eligibility.vocabulary import (
    DOMAINS,
    EXPECTED_VOCABULARY_SHA256,
    MODIFIER_PATTERNS,
    RULES,
    THRESHOLD_PATTERNS,
    VOCABULARY_SHA256,
    DomainRule,
    compute_vocabulary_sha256,
    validate_rules,
    verify_vocabulary_pin,
)


def _assert_evidence_slices(raw_text: str, parsed: ParsedEligibility) -> None:
    for dom in DOMAINS:
        a = parsed.domains[dom]
        assert a.status in DOMAIN_STATUS_LEVELS
        if a.status == "threshold_or_conditional":
            assert a.substatus in ("numeric_cutoff", "exception_permitted")
            assert len(a.evidence) >= 1
        else:
            assert a.substatus is None
        for ev in a.evidence:
            assert raw_text[ev.start_char:ev.end_char] == ev.verbatim_text


# ---------------------------------------------------------------------------
# Vocabulary SHA-256 Pinning & Schema Validation
# ---------------------------------------------------------------------------


def test_vocabulary_pin_matches_expected_sha256() -> None:
    assert len(VOCABULARY_SHA256) == 64
    assert VOCABULARY_SHA256 == EXPECTED_VOCABULARY_SHA256
    verify_vocabulary_pin()
    assert len(DOMAINS) == 12


def test_mutating_rule_pattern_moves_vocabulary_sha256() -> None:
    mutated = list(RULES)
    mutated[0] = replace(mutated[0], pattern=mutated[0].pattern + r"|\btrimester_edit\b")
    assert compute_vocabulary_sha256(mutated) != VOCABULARY_SHA256


def test_mutating_modifier_or_threshold_pattern_moves_vocabulary_sha256() -> None:
    alt_mods = dict(MODIFIER_PATTERNS)
    alt_mods["gilberts_syndrome"] = r"\bgilbert_syndrome_only\b"
    assert compute_vocabulary_sha256(RULES, modifier_patterns=alt_mods) != VOCABULARY_SHA256

    alt_thresh = dict(THRESHOLD_PATTERNS)
    alt_thresh["hiv_cd4"] = r"\bcd4\s*>=\s*(?P<val>\d+)\b"
    assert compute_vocabulary_sha256(RULES, threshold_patterns=alt_thresh) != VOCABULARY_SHA256


def test_validate_rules_rejects_duplicate_id_and_empty_rationale() -> None:
    dup = list(RULES) + [RULES[0]]
    with pytest.raises(ValueError, match="Duplicate rule_id"):
        validate_rules(dup)

    no_note = [replace(RULES[0], rationale="   ")] + list(RULES[1:])
    with pytest.raises(ValueError, match="non-empty rationale"):
        validate_rules(no_note)


def test_validate_rules_rejects_invalid_domain_effect_and_missing_coverage() -> None:
    bad_dom = [replace(RULES[0], rule_id="custom_bad_dom", domain="unknown_domain")] + list(RULES)
    with pytest.raises(ValueError, match="unknown domain"):
        validate_rules(bad_dom)

    bad_eff = [replace(RULES[0], rule_id="custom_bad_eff", effect="unknown_effect")] + list(RULES)
    with pytest.raises(ValueError, match="unknown effect"):
        validate_rules(bad_eff)

    without_hiv = [r for r in RULES if r.domain != "hiv_infection"]
    with pytest.raises(ValueError, match="Domains missing"):
        validate_rules(without_hiv)


def test_validate_rules_rejects_bad_applicable_sections() -> None:
    bad_sec = [
        DomainRule(
            rule_id="bad_sec_rule",
            domain="hiv_infection",
            effect="strictly_excluded",
            applicable_sections=("header_only",),
            pattern=r"\bhiv\b",
            guard_not_pattern=None,
            rationale="Invalid section test.",
        )
    ] + list(RULES)
    with pytest.raises(ValueError, match="invalid applicable_sections"):
        validate_rules(bad_sec)


# ---------------------------------------------------------------------------
# 1. pregnancy_or_lactation
# ---------------------------------------------------------------------------


def test_pregnancy_blanket_exclusion_and_contraception_do_not_collapse() -> None:
    # Standalone contraception requirement -> threshold_or_conditional / exception_permitted
    raw_contra = (
        "Inclusion Criteria:\n"
        "- Women of childbearing potential must agree to use highly effective contraception.\n"
    )
    p_contra = parse_eligibility_text(raw_contra)
    d_contra = p_contra.domains["pregnancy_or_lactation"]
    assert d_contra.status == "threshold_or_conditional"
    assert d_contra.substatus == "exception_permitted"
    assert d_contra.condition_type == "contraception_required"
    _assert_evidence_slices(raw_contra, p_contra)

    # Contraception in Inclusion + Pregnant/breastfeeding in Exclusion -> strictly_excluded!
    raw_both = (
        "Inclusion Criteria:\n"
        "- Participants of childbearing potential must agree to use adequate contraception.\n"
        "Exclusion Criteria:\n"
        "- Pregnant or breastfeeding women.\n"
    )
    p_both = parse_eligibility_text(raw_both)
    assert p_both.domains["pregnancy_or_lactation"].status == "strictly_excluded"
    _assert_evidence_slices(raw_both, p_both)


def test_pregnancy_maternal_target_inclusion_and_nursing_home_guard() -> None:
    raw_mat = (
        "Inclusion Criteria:\n"
        "- Pregnant women in the second trimester of pregnancy (14 to 26 weeks)\n"
        "Exclusion Criteria:\n"
        "- Residents of a skilled nursing facility or nursing home\n"
    )
    p = parse_eligibility_text(raw_mat)
    assert p.domains["pregnancy_or_lactation"].status == "explicitly_included_or_targeted"
    _assert_evidence_slices(raw_mat, p)

    # Verify nursing home alone never triggers pregnancy_or_lactation
    raw_nh = "Exclusion Criteria:\n- Living in a nursing home or receiving skilled nursing care.\n"
    p_nh = parse_eligibility_text(raw_nh)
    assert p_nh.domains["pregnancy_or_lactation"].status == "not_mentioned"


def test_pregnancy_conditional_lactation_allowance() -> None:
    raw = (
        "Exclusion Criteria:\n"
        "- Currently pregnant; if breastfeeding, agrees to pump and discard milk during study treatment.\n"
    )
    p = parse_eligibility_text(raw)
    d = p.domains["pregnancy_or_lactation"]
    assert d.status == "threshold_or_conditional"
    assert d.substatus == "exception_permitted"
    _assert_evidence_slices(raw, p)


# ---------------------------------------------------------------------------
# 2. hiv_infection
# ---------------------------------------------------------------------------


def test_hiv_blanket_vs_conditional_art_vs_targeted_and_hearing_aids_guard() -> None:
    raw_excl = "Exclusion Criteria:\n- Known HIV positive status or AIDS-defining illness.\n"
    p_excl = parse_eligibility_text(raw_excl)
    assert p_excl.domains["hiv_infection"].status == "strictly_excluded"

    raw_cond = (
        "Exclusion Criteria:\n"
        "- Known HIV infection.\n"
        "  Exception: HIV-infected participants on stable antiretroviral therapy with undetectable viral load "
        "and CD4+ count >= 350 cells/uL are eligible.\n"
    )
    p_cond = parse_eligibility_text(raw_cond)
    d_cond = p_cond.domains["hiv_infection"]
    assert d_cond.status == "threshold_or_conditional"
    assert d_cond.substatus == "exception_permitted"
    assert d_cond.extracted_thresholds["cd4_min_cells_ul"] == 350
    assert d_cond.extracted_thresholds["requires_undetectable_viral_load"] is True
    _assert_evidence_slices(raw_cond, p_cond)

    # NCI CTEP conditional HIV clause placed inside Inclusion Criteria
    raw_nci = (
        "Inclusion Criteria:\n"
        "- HIV-infected patients on effective antiretroviral therapy with undetectable viral load within 6 months are eligible.\n"
    )
    p_nci = parse_eligibility_text(raw_nci)
    assert p_nci.domains["hiv_infection"].status == "threshold_or_conditional"
    assert p_nci.domains["hiv_infection"].substatus == "exception_permitted"

    # Dedicated HIV-1 trial (with HIV-2 subtype exclusion)
    raw_target = (
        "Inclusion Criteria:\n"
        "- Documented HIV-1 infection with plasma HIV-1 RNA >= 1000 copies/mL\n"
        "Exclusion Criteria:\n"
        "- Co-infection with HIV-2\n"
    )
    p_target = parse_eligibility_text(raw_target)
    assert p_target.domains["hiv_infection"].status == "explicitly_included_or_targeted"

    # False-positive guard: hearing aids / walking aids
    raw_aids_guard = "Exclusion Criteria:\n- Bilateral cochlear implants (hearing aids and walking aids are allowed).\n"
    p_guard = parse_eligibility_text(raw_aids_guard)
    assert p_guard.domains["hiv_infection"].status == "not_mentioned"


# ---------------------------------------------------------------------------
# 3. hepatitis_b_or_c
# ---------------------------------------------------------------------------


def test_hepatitis_blanket_vs_cleared_rna_vs_targeted_and_vaccine_guard() -> None:
    raw_excl = "Exclusion Criteria:\n- Positive HBsAg or anti-HCV antibody.\n"
    assert parse_eligibility_text(raw_excl).domains["hepatitis_b_or_c"].status == "strictly_excluded"

    raw_cond = (
        "Exclusion Criteria:\n"
        "- Active Hepatitis B or Hepatitis C infection (patients with prior HCV with undetectable HCV RNA "
        "or controlled HBV on stable antiviral therapy with undetectable HBV DNA are eligible).\n"
    )
    p_cond = parse_eligibility_text(raw_cond)
    d_cond = p_cond.domains["hepatitis_b_or_c"]
    assert d_cond.status == "threshold_or_conditional"
    assert d_cond.substatus == "exception_permitted"
    assert d_cond.extracted_thresholds["hcv_rna_negative_allowed"] is True
    assert d_cond.extracted_thresholds["hbv_dna_suppressed_allowed"] is True
    _assert_evidence_slices(raw_cond, p_cond)

    raw_target = (
        "Inclusion Criteria:\n"
        "- Chronic Hepatitis C infection, HCV genotype 1, treatment-naive\n"
        "Exclusion Criteria:\n"
        "- HBsAg positive Hepatitis B co-infection\n"
    )
    assert parse_eligibility_text(raw_target).domains["hepatitis_b_or_c"].status == "explicitly_included_or_targeted"

    raw_vax = "Exclusion Criteria:\n- Live attenuated vaccine within 30 days (hepatitis B vaccination is permitted).\n"
    assert parse_eligibility_text(raw_vax).domains["hepatitis_b_or_c"].status == "not_mentioned"


# ---------------------------------------------------------------------------
# 4. renal_function
# ---------------------------------------------------------------------------


def test_renal_function_cutoffs_blanket_and_renal_impairment_trial() -> None:
    raw_cut = (
        "Inclusion Criteria:\n"
        "- eGFR >= 60 mL/min/1.73m2 or creatinine clearance >= 50 mL/min\n"
        "- Serum creatinine <= 1.5 x ULN\n"
    )
    p_cut = parse_eligibility_text(raw_cut)
    d_cut = p_cut.domains["renal_function"]
    assert d_cut.status == "threshold_or_conditional"
    assert d_cut.substatus == "numeric_cutoff"
    assert d_cut.extracted_thresholds["egfr_min_ml_min"] == 60.0
    assert d_cut.extracted_thresholds["crcl_min_ml_min"] == 50.0
    assert d_cut.extracted_thresholds["creatinine_max_uln"] == 1.5
    _assert_evidence_slices(raw_cut, p_cut)

    raw_exc_cut = "Exclusion Criteria:\n- Serum creatinine > 2.0 mg/dL or eGFR < 45 mL/min\n"
    d_exc = parse_eligibility_text(raw_exc_cut).domains["renal_function"]
    assert d_exc.status == "threshold_or_conditional"
    assert d_exc.substatus == "numeric_cutoff"
    assert d_exc.extracted_thresholds["creatinine_max_mg_dl"] == 2.0
    assert d_exc.extracted_thresholds["egfr_min_ml_min"] == 45.0

    raw_blanket = "Exclusion Criteria:\n- Chronic kidney disease or history of renal insufficiency.\n"
    assert parse_eligibility_text(raw_blanket).domains["renal_function"].status == "strictly_excluded"

    raw_target = "Inclusion Criteria:\n- Severe renal impairment defined as eGFR < 30 mL/min/1.73m2\n"
    d_target = parse_eligibility_text(raw_target).domains["renal_function"]
    assert d_target.status == "explicitly_included_or_targeted"
    assert d_target.extracted_thresholds["target_egfr_max_ml_min"] == 30.0


# ---------------------------------------------------------------------------
# 5. hepatic_function (including Gilbert's syndrome)
# ---------------------------------------------------------------------------


def test_hepatic_function_uln_gilberts_exception_and_cirrhosis_target() -> None:
    raw_num = (
        "Inclusion Criteria:\n"
        "- AST (SGOT) and ALT (SGPT) <= 2.5 x ULN (<= 5.0 x ULN for patients with liver metastases)\n"
        "- Total bilirubin <= 1.5 x ULN\n"
    )
    d_num = parse_eligibility_text(raw_num).domains["hepatic_function"]
    assert d_num.status == "threshold_or_conditional"
    assert d_num.substatus == "numeric_cutoff"
    assert d_num.has_gilberts_exception is False
    assert d_num.extracted_thresholds["ast_max_uln"] == 2.5
    assert d_num.extracted_thresholds["alt_max_uln"] == 2.5
    assert d_num.extracted_thresholds["ast_liver_mets_max_uln"] == 5.0
    assert d_num.extracted_thresholds["bilirubin_max_uln"] == 1.5

    raw_gilbert = (
        "Inclusion Criteria:\n"
        "- Total bilirubin <= 1.5 x ULN (<= 3.0 x ULN for Gilbert's syndrome)\n"
    )
    p_gil = parse_eligibility_text(raw_gilbert)
    d_gil = p_gil.domains["hepatic_function"]
    assert d_gil.status == "threshold_or_conditional"
    assert d_gil.substatus == "exception_permitted"
    assert d_gil.has_gilberts_exception is True
    assert d_gil.extracted_thresholds["has_gilberts_exception"] is True
    assert d_gil.extracted_thresholds["bilirubin_max_uln"] == 1.5
    assert d_gil.extracted_thresholds["gilberts_bilirubin_max_uln"] == 3.0
    _assert_evidence_slices(raw_gilbert, p_gil)

    # Guard: bare exclusion of Gilbert's syndrome without allowance word is NOT an exception
    raw_gil_exc = "Exclusion Criteria:\n- Hepatic insufficiency or known Gilbert's syndrome.\n"
    d_gil_exc = parse_eligibility_text(raw_gil_exc).domains["hepatic_function"]
    assert d_gil_exc.status == "strictly_excluded"

    raw_cirr = "Inclusion Criteria:\n- Patients with Child-Pugh Class B or C hepatic cirrhosis.\n"
    assert parse_eligibility_text(raw_cirr).domains["hepatic_function"].status == "explicitly_included_or_targeted"


# ---------------------------------------------------------------------------
# 6. cardiac_function
# ---------------------------------------------------------------------------


def test_cardiac_function_lvef_qtc_nyha_blanket_and_hf_target() -> None:
    raw_cut = (
        "Inclusion Criteria:\n"
        "- LVEF >= 50% by ECHO or MUGA\n"
        "- QTcF <= 470 msec\n"
        "Exclusion Criteria:\n"
        "- NYHA Class III or IV heart failure\n"
    )
    p_cut = parse_eligibility_text(raw_cut)
    d_cut = p_cut.domains["cardiac_function"]
    assert d_cut.status == "threshold_or_conditional"
    assert d_cut.substatus == "numeric_cutoff"
    assert d_cut.extracted_thresholds["lvef_min_pct"] == 50.0
    assert d_cut.extracted_thresholds["qtc_max_ms"] == 470.0
    assert d_cut.extracted_thresholds["nyha_max_allowed"] == 2
    _assert_evidence_slices(raw_cut, p_cut)

    raw_blanket = "Exclusion Criteria:\n- History of myocardial infarction, unstable angina, or congestive heart failure.\n"
    assert parse_eligibility_text(raw_blanket).domains["cardiac_function"].status == "strictly_excluded"

    raw_hf = "Inclusion Criteria:\n- Symptomatic chronic heart failure (HFrEF) with LVEF <= 40%.\n"
    d_hf = parse_eligibility_text(raw_hf).domains["cardiac_function"]
    assert d_hf.status == "explicitly_included_or_targeted"
    assert d_hf.extracted_thresholds["target_lvef_max_pct"] == 40.0


# ---------------------------------------------------------------------------
# 7. cns_or_brain_metastases
# ---------------------------------------------------------------------------


def test_cns_metastases_blanket_vs_treated_stable_vs_targeted() -> None:
    raw_excl = "Exclusion Criteria:\n- Known brain or leptomeningeal metastases.\n"
    assert parse_eligibility_text(raw_excl).domains["cns_or_brain_metastases"].status == "strictly_excluded"

    raw_cond = (
        "Exclusion Criteria:\n"
        "- Active CNS metastases.\n"
        "  Note: Patients with previously treated, radiographically stable, asymptomatic brain metastases "
        "off steroids for >= 2 weeks are permitted.\n"
    )
    p_cond = parse_eligibility_text(raw_cond)
    d_cond = p_cond.domains["cns_or_brain_metastases"]
    assert d_cond.status == "threshold_or_conditional"
    assert d_cond.substatus == "exception_permitted"
    assert d_cond.condition_type == "treated_stable_cns_metastases"
    _assert_evidence_slices(raw_cond, p_cond)

    raw_target = (
        "Inclusion Criteria:\n"
        "- Radiographically confirmed measurable brain metastases from HER2+ breast cancer\n"
        "Exclusion Criteria:\n"
        "- Leptomeningeal metastases\n"
    )
    assert parse_eligibility_text(raw_target).domains["cns_or_brain_metastases"].status == "explicitly_included_or_targeted"


# ---------------------------------------------------------------------------
# 8. performance_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_text", "expected_status", "expected_ecog_max"),
    [
        ("Inclusion Criteria:\n- ECOG performance status 0 or 1\n", "threshold_or_conditional", 1),
        ("Inclusion Criteria:\n- ECOG PS 0-2\n", "threshold_or_conditional", 2),
        ("Inclusion Criteria:\n- Zubrod performance status < 2\n", "threshold_or_conditional", 1),
        ("Inclusion Criteria:\n- ECOG performance status = 0\n", "threshold_or_conditional", 0),
        ("Exclusion Criteria:\n- ECOG performance status >= 2\n", "threshold_or_conditional", 1),
        ("Exclusion Criteria:\n- ECOG PS > 2\n", "threshold_or_conditional", 2),
        ("Exclusion Criteria:\n- ECOG score 3 or 4\n", "threshold_or_conditional", 2),
        ("Inclusion Criteria:\n- Frail patients with ECOG performance status 2 or 3\n", "explicitly_included_or_targeted", 3),
    ],
)
def test_performance_status_ecog_polarity_and_extraction(
    raw_text: str, expected_status: str, expected_ecog_max: int
) -> None:
    p = parse_eligibility_text(raw_text)
    d = p.domains["performance_status"]
    assert d.status == expected_status
    assert d.extracted_thresholds["ecog_max_allowed"] == expected_ecog_max
    _assert_evidence_slices(raw_text, p)


def test_performance_status_karnofsky_and_lansky() -> None:
    raw = "Inclusion Criteria:\n- Karnofsky performance status >= 70% (or Lansky >= 60% for pediatric patients)\n"
    d = parse_eligibility_text(raw).domains["performance_status"]
    assert d.status == "threshold_or_conditional"
    assert d.substatus == "numeric_cutoff"
    assert d.extracted_thresholds["karnofsky_min_pct"] == 70.0
    assert d.extracted_thresholds["lansky_min_pct"] == 60.0


# ---------------------------------------------------------------------------
# 9. psychiatric_or_substance_use
# ---------------------------------------------------------------------------


def test_psychiatric_and_substance_use_blanket_conditional_and_targeted() -> None:
    raw_excl = (
        "Exclusion Criteria:\n"
        "- Psychiatric illness or social situations that would limit compliance with study requirements\n"
        "- Active suicidal ideation or history of alcohol or substance abuse\n"
    )
    assert parse_eligibility_text(raw_excl).domains["psychiatric_or_substance_use"].status == "strictly_excluded"

    raw_cond = (
        "Exclusion Criteria:\n"
        "- History of substance abuse unless in sustained remission for >= 12 months.\n"
    )
    d_cond = parse_eligibility_text(raw_cond).domains["psychiatric_or_substance_use"]
    assert d_cond.status == "threshold_or_conditional"
    assert d_cond.substatus == "exception_permitted"

    raw_target = (
        "Inclusion Criteria:\n"
        "- DSM-5 diagnosis of Major Depressive Disorder\n"
        "Exclusion Criteria:\n"
        "- History of schizophrenia or active psychosis\n"
    )
    assert parse_eligibility_text(raw_target).domains["psychiatric_or_substance_use"].status == "explicitly_included_or_targeted"


# ---------------------------------------------------------------------------
# 10. prior_malignancy
# ---------------------------------------------------------------------------


def test_prior_malignancy_blanket_lookback_skin_insitu_and_primary_tumor_guard() -> None:
    # Primary tumor inclusion must NEVER trigger prior_malignancy
    raw_primary = "Inclusion Criteria:\n- Histologically confirmed metastatic pancreatic adenocarcinoma.\n"
    assert parse_eligibility_text(raw_primary).domains["prior_malignancy"].status == "not_mentioned"

    raw_blanket = "Exclusion Criteria:\n- History of any other primary malignancy.\n"
    assert parse_eligibility_text(raw_blanket).domains["prior_malignancy"].status == "strictly_excluded"

    raw_window = "Exclusion Criteria:\n- Prior invasive malignancy within the past 5 years.\n"
    d_win = parse_eligibility_text(raw_window).domains["prior_malignancy"]
    assert d_win.status == "threshold_or_conditional"
    assert d_win.substatus == "numeric_cutoff"
    assert d_win.extracted_thresholds["lookback_years"] == 5.0
    assert d_win.has_skin_or_insitu_exception is False

    raw_exc = (
        "Exclusion Criteria:\n"
        "- Prior or concurrent malignancy within 3 years, except adequately treated basal cell or "
        "squamous cell skin carcinoma or carcinoma in situ of the cervix.\n"
    )
    p_exc = parse_eligibility_text(raw_exc)
    d_exc = p_exc.domains["prior_malignancy"]
    assert d_exc.status == "threshold_or_conditional"
    assert d_exc.substatus == "exception_permitted"
    assert d_exc.has_skin_or_insitu_exception is True
    assert d_exc.extracted_thresholds["lookback_years"] == 3.0
    _assert_evidence_slices(raw_exc, p_exc)

    raw_target = "Inclusion Criteria:\n- Therapy-related acute myeloid leukemia following a prior malignancy.\n"
    assert parse_eligibility_text(raw_target).domains["prior_malignancy"].status == "explicitly_included_or_targeted"


# ---------------------------------------------------------------------------
# 11. language_or_literacy
# ---------------------------------------------------------------------------


def test_language_monolingual_exclusion_vs_multilingual_vs_interpreter_vs_lep() -> None:
    raw_mono = "Inclusion Criteria:\n- Must be able to read, speak, and understand English.\n"
    d_mono = parse_eligibility_text(raw_mono).domains["language_or_literacy"]
    assert d_mono.status == "strictly_excluded"
    assert d_mono.extracted_thresholds["languages"] == ["English"]

    raw_non_eng = "Exclusion Criteria:\n- Non-English speaking or illiteracy precluding questionnaire completion.\n"
    assert parse_eligibility_text(raw_non_eng).domains["language_or_literacy"].status == "strictly_excluded"

    raw_bi = "Inclusion Criteria:\n- Able to read and speak English or Spanish.\n"
    d_bi = parse_eligibility_text(raw_bi).domains["language_or_literacy"]
    assert d_bi.status == "threshold_or_conditional"
    assert d_bi.substatus == "exception_permitted"
    assert d_bi.extracted_thresholds["languages"] == ["English", "Spanish"]
    assert d_bi.extracted_thresholds["accommodation"] == "multilingual"

    raw_interp = (
        "Inclusion Criteria:\n"
        "- Able to provide written informed consent in English or with assistance of a qualified medical interpreter.\n"
    )
    d_int = parse_eligibility_text(raw_interp).domains["language_or_literacy"]
    assert d_int.status == "threshold_or_conditional"
    assert d_int.substatus == "exception_permitted"
    assert d_int.extracted_thresholds["accommodation"] == "interpreter_or_translation"

    raw_lep = "Inclusion Criteria:\n- Latino adults with Limited English Proficiency (LEP).\n"
    assert parse_eligibility_text(raw_lep).domains["language_or_literacy"].status == "explicitly_included_or_targeted"


# ---------------------------------------------------------------------------
# 12. free_text_age_cutoff & Polarity / False-Positive Guards
# ---------------------------------------------------------------------------


def test_age_cutoff_inclusion_and_exclusion_polarity_inversion() -> None:
    p_inc = parse_eligibility_text("Inclusion Criteria:\n- Age 18 to 70 years.\n")
    d_inc = p_inc.domains["free_text_age_cutoff"]
    assert d_inc.status == "threshold_or_conditional"
    assert d_inc.substatus == "numeric_cutoff"
    assert d_inc.extracted_thresholds["min_age_years"] == 18.0
    assert d_inc.extracted_thresholds["max_age_years"] == 70.0

    # Exact Exclusion inversion required by specification!
    p_exc = parse_eligibility_text("Exclusion Criteria:\n- Age < 18 years or > 70 years.\n")
    d_exc = p_exc.domains["free_text_age_cutoff"]
    assert d_exc.status == "threshold_or_conditional"
    assert d_exc.extracted_thresholds["min_age_years"] == 18.0
    assert d_exc.extracted_thresholds["max_age_years"] == 70.0

    # Lower bound only -> no upper cap!
    p_low = parse_eligibility_text("Inclusion Criteria:\n- Age >= 18 years.\n")
    d_low = p_low.domains["free_text_age_cutoff"]
    assert d_low.extracted_thresholds["min_age_years"] == 18.0
    assert "max_age_years" not in d_low.extracted_thresholds

    # Split across Inclusion and Exclusion sections -> combined [18.0, 65.0]
    p_split = parse_eligibility_text("Inclusion Criteria:\n- At least 18 years of age\nExclusion Criteria:\n- Age > 65 years\n")
    d_split = p_split.domains["free_text_age_cutoff"]
    assert d_split.extracted_thresholds["min_age_years"] == 18.0
    assert d_split.extracted_thresholds["max_age_years"] == 65.0


def test_age_cutoff_multi_cohort_union_and_pediatric_months() -> None:
    raw_mc = (
        "Part 1 Inclusion Criteria:\n"
        "- Adults aged 18 to 65 years\n"
        "Part 2 Inclusion Criteria:\n"
        "- Adolescents aged 12 to 17 years\n"
    )
    d_mc = parse_eligibility_text(raw_mc).domains["free_text_age_cutoff"]
    assert d_mc.status == "threshold_or_conditional"
    assert d_mc.extracted_thresholds["min_age_years"] == 12.0
    assert d_mc.extracted_thresholds["max_age_years"] == 65.0

    raw_peds = "Inclusion Criteria:\n- Children 6 months to 12 years of age.\n"
    d_peds = parse_eligibility_text(raw_peds).domains["free_text_age_cutoff"]
    assert d_peds.extracted_thresholds["min_age_years"] == pytest.approx(0.5, abs=1e-4)
    assert d_peds.extracted_thresholds["max_age_years"] == 12.0


@pytest.mark.parametrize(
    "non_age_clause",
    [
        "Exclusion Criteria:\n- Major surgery within the past 5 years.\n",
        "Exclusion Criteria:\n- Prior malignancy within 3 years.\n",
        "Inclusion Criteria:\n- Singleton infant with gestational age >= 37 weeks.\n",
        "Inclusion Criteria:\n- Smoking history >= 20 pack-years.\n",
        "Inclusion Criteria:\n- Age at diagnosis < 50 years.\n",
        "Inclusion Criteria:\n- Life expectancy >= 6 months.\n",
    ],
)
def test_age_cutoff_rejects_lookbacks_gestational_age_and_pack_years(non_age_clause: str) -> None:
    p = parse_eligibility_text(non_age_clause)
    assert p.domains["free_text_age_cutoff"].status == "not_mentioned"


def test_unheaded_bare_mentions_vs_explicit_verbs_and_contradictions() -> None:
    raw_bare = "- HIV infection\n- Pregnancy\n- Brain metastases\n- Age 18 to 65 years\n"
    p_bare = parse_eligibility_text(raw_bare)
    assert p_bare.domains["hiv_infection"].status == "ambiguous_or_unheaded"
    assert p_bare.domains["pregnancy_or_lactation"].status == "ambiguous_or_unheaded"
    assert p_bare.domains["cns_or_brain_metastases"].status == "ambiguous_or_unheaded"
    assert p_bare.domains["free_text_age_cutoff"].status == "ambiguous_or_unheaded"

    raw_verb = "Pregnant or breastfeeding women are excluded. Participants must be aged 18 to 65 years."
    p_verb = parse_eligibility_text(raw_verb)
    assert p_verb.domains["pregnancy_or_lactation"].status == "strictly_excluded"
    assert p_verb.domains["free_text_age_cutoff"].status == "threshold_or_conditional"
    assert p_verb.domains["free_text_age_cutoff"].extracted_thresholds["max_age_years"] == 65.0

    # Contradictory Inclusion vs Exclusion in single-cohort trial -> ambiguous_or_unheaded
    raw_contra_hiv = (
        "Inclusion Criteria:\n- Confirmed HIV-1 infection\n"
        "Exclusion Criteria:\n- Known HIV-1 positive status\n"
    )
    assert parse_eligibility_text(raw_contra_hiv).domains["hiv_infection"].status == "ambiguous_or_unheaded"

    raw_contra_age = (
        "Inclusion Criteria:\n- Age 18 to 50 years\n"
        "Exclusion Criteria:\n- Age < 60 years\n"
    )
    assert parse_eligibility_text(raw_contra_age).domains["free_text_age_cutoff"].status == "ambiguous_or_unheaded"


def test_pregnancy_negative_pregnancy_test_in_inclusion_is_strictly_excluded() -> None:
    raw = "Inclusion Criteria:\n- Negative serum or urine pregnancy test within 72 hours prior to Cycle 1 Day 1.\n"
    p = parse_eligibility_text(raw)
    assert p.domains["pregnancy_or_lactation"].status == "strictly_excluded"
    _assert_evidence_slices(raw, p)


def test_pregnancy_lactating_mothers_target_inclusion() -> None:
    raw = "Inclusion Criteria:\n- Currently breastfeeding mothers at 4 to 24 weeks postpartum.\n"
    p = parse_eligibility_text(raw)
    assert p.domains["pregnancy_or_lactation"].status == "explicitly_included_or_targeted"
    _assert_evidence_slices(raw, p)


def test_hiv_negative_requirement_in_inclusion_is_strictly_excluded() -> None:
    raw = "Inclusion Criteria:\n- Must be HIV-1 and HIV-2 negative at screening.\n"
    p = parse_eligibility_text(raw)
    assert p.domains["hiv_infection"].status == "strictly_excluded"
    _assert_evidence_slices(raw, p)


def test_hepatitis_b_chronic_hbsag_positive_target_inclusion() -> None:
    raw = "Inclusion Criteria:\n- HBsAg-positive for >= 6 months with detectable HBV DNA.\n"
    p = parse_eligibility_text(raw)
    assert p.domains["hepatitis_b_or_c"].status == "explicitly_included_or_targeted"
    _assert_evidence_slices(raw, p)


def test_hepatitis_negative_serology_in_inclusion_is_strictly_excluded() -> None:
    raw = "Inclusion Criteria:\n- Negative for HBsAg and anti-HCV at screening.\n"
    p = parse_eligibility_text(raw)
    assert p.domains["hepatitis_b_or_c"].status == "strictly_excluded"
    _assert_evidence_slices(raw, p)


def test_renal_esrd_on_hemodialysis_target_inclusion() -> None:
    raw = "Inclusion Criteria:\n- End-stage renal disease (ESRD) on chronic hemodialysis for >= 3 months.\n"
    p = parse_eligibility_text(raw)
    assert p.domains["renal_function"].status == "explicitly_included_or_targeted"
    _assert_evidence_slices(raw, p)


def test_renal_direct_negation_not_on_dialysis_in_inclusion() -> None:
    raw = "Inclusion Criteria:\n- Not on dialysis and no history of renal failure.\n"
    p = parse_eligibility_text(raw)
    assert p.domains["renal_function"].status == "strictly_excluded"
    _assert_evidence_slices(raw, p)


def test_hepatic_bilirubin_mg_dl_and_child_pugh_a_cutoff() -> None:
    raw = "Inclusion Criteria:\n- Total bilirubin <= 1.5 mg/dL and Child-Pugh Class A.\n"
    p = parse_eligibility_text(raw)
    d = p.domains["hepatic_function"]
    assert d.status == "threshold_or_conditional"
    assert d.substatus == "numeric_cutoff"
    assert d.extracted_thresholds["bilirubin_max_mg_dl"] == 1.5
    _assert_evidence_slices(raw, p)


def test_hepatic_gilberts_allowance_in_exclusion_with_except_cue() -> None:
    raw = "Exclusion Criteria:\n- Total bilirubin > 1.5 x ULN, except in participants with Gilbert's syndrome.\n"
    p = parse_eligibility_text(raw)
    d = p.domains["hepatic_function"]
    assert d.status == "threshold_or_conditional"
    assert d.substatus == "exception_permitted"
    assert d.has_gilberts_exception is True
    _assert_evidence_slices(raw, p)


def test_cardiac_exclusion_lvef_below_threshold_extracts_min_pct() -> None:
    raw = "Exclusion Criteria:\n- Left ventricular ejection fraction < 55% or QTc > 480 ms.\n"
    p = parse_eligibility_text(raw)
    d = p.domains["cardiac_function"]
    assert d.status == "threshold_or_conditional"
    assert d.substatus == "numeric_cutoff"
    assert d.extracted_thresholds["lvef_min_pct"] == 55.0
    assert d.extracted_thresholds["qtc_max_ms"] == 480.0
    _assert_evidence_slices(raw, p)


def test_cns_metastases_negated_in_inclusion_is_strictly_excluded() -> None:
    raw = "Inclusion Criteria:\n- No known brain metastases or leptomeningeal metastases.\n"
    p = parse_eligibility_text(raw)
    assert p.domains["cns_or_brain_metastases"].status == "strictly_excluded"
    _assert_evidence_slices(raw, p)


def test_psychiatric_controlled_depression_exception() -> None:
    raw = (
        "Exclusion Criteria:\n"
        "- Severe psychiatric illness (controlled depression on stable SSRI therapy is permitted).\n"
    )
    p = parse_eligibility_text(raw)
    d = p.domains["psychiatric_or_substance_use"]
    assert d.status == "threshold_or_conditional"
    assert d.substatus == "exception_permitted"
    _assert_evidence_slices(raw, p)


def test_prior_malignancy_months_lookback_conversion_to_years() -> None:
    raw = "Exclusion Criteria:\n- Other invasive malignancy within 24 months.\n"
    p = parse_eligibility_text(raw)
    d = p.domains["prior_malignancy"]
    assert d.status == "threshold_or_conditional"
    assert d.substatus == "numeric_cutoff"
    assert d.extracted_thresholds["lookback_years"] == 2.0
    _assert_evidence_slices(raw, p)


def test_language_no_interpreter_guard_stays_strictly_excluded() -> None:
    raw = (
        "Inclusion Criteria:\n"
        "- Must be fluent in spoken and written English (interpreters are not permitted for PROs).\n"
    )
    p = parse_eligibility_text(raw)
    assert p.domains["language_or_literacy"].status == "strictly_excluded"
    _assert_evidence_slices(raw, p)


def test_language_translated_consent_form_is_exception_permitted() -> None:
    raw = "Inclusion Criteria:\n- Signed informed consent; translated informed consent form is available.\n"
    p = parse_eligibility_text(raw)
    d = p.domains["language_or_literacy"]
    assert d.status == "threshold_or_conditional"
    assert d.substatus == "exception_permitted"
    assert d.extracted_thresholds["accommodation"] == "interpreter_or_translation"
    _assert_evidence_slices(raw, p)


def test_age_cutoff_children_zero_to_seventeen_excluded_yields_adult_min_age() -> None:
    raw = "Exclusion Criteria:\n- Children aged 0 to 17 years.\n"
    p = parse_eligibility_text(raw)
    d = p.domains["free_text_age_cutoff"]
    assert d.status == "threshold_or_conditional"
    assert d.extracted_thresholds["min_age_years"] == 18.0
    _assert_evidence_slices(raw, p)


def test_washout_period_between_weeks_never_misread_as_participant_age() -> None:
    raw = "Exclusion Criteria:\n- Washout period between 2 and 4 weeks prior to randomization.\n"
    p = parse_eligibility_text(raw)
    assert p.domains["free_text_age_cutoff"].status == "not_mentioned"


def test_between_18_and_70_years_of_age_parsed_accurately() -> None:
    raw = "Inclusion Criteria:\n- Between 18 and 70 years of age at screening.\n"
    p = parse_eligibility_text(raw)
    d = p.domains["free_text_age_cutoff"]
    assert d.status == "threshold_or_conditional"
    assert d.extracted_thresholds["min_age_years"] == 18.0
    assert d.extracted_thresholds["max_age_years"] == 70.0
    _assert_evidence_slices(raw, p)
