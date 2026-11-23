"""Tests for structured-vs-text eligibility discordance auditing (`audit_structured_vs_text`)."""
from __future__ import annotations

from typing import Any

import pytest

from eligibility.parser import audit_structured_vs_text, parse_eligibility_text


def _make_v2_study(
    nct_id: str = "NCT99000301",
    min_age: str | None = "18 Years",
    max_age: str | None = "N/A",
    std_ages: list[str] | None = None,
    sex: str | None = "ALL",
    healthy_volunteers: bool | None = False,
    criteria: str = "",
) -> dict[str, Any]:
    elig_mod: dict[str, Any] = {"eligibilityCriteria": criteria}
    if min_age is not None:
        elig_mod["minimumAge"] = min_age
    if max_age is not None:
        elig_mod["maximumAge"] = max_age
    if std_ages is not None:
        elig_mod["stdAges"] = std_ages
    if sex is not None:
        elig_mod["sex"] = sex
    if healthy_volunteers is not None:
        elig_mod["healthyVolunteers"] = healthy_volunteers
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id},
            "eligibilityModule": elig_mod,
        }
    }


def test_hidden_upper_age_cap_from_inclusion_range() -> None:
    raw = "Inclusion Criteria:\n- Male and female adults aged 18 to 65 years.\n"
    study = _make_v2_study("NCT99000302", min_age="18 Years", max_age="N/A", std_ages=["ADULT", "OLDER_ADULT"], criteria=raw)
    rep = audit_structured_vs_text(study)
    assert rep.hidden_upper_age_cap is True
    assert rep.hidden_lower_age_bound is False
    assert rep.conflicting_age_bounds is False
    assert rep.text_min_age_years == 18.0
    assert rep.text_max_age_years == 65.0
    assert rep.structured_max_age_years is None
    assert rep.any_discordance is True
    assert "hidden_upper_age_cap" in rep.discordance_reasons
    assert len(rep.evidence) >= 1
    assert raw[rep.evidence[0].start_char : rep.evidence[0].end_char] == rep.evidence[0].verbatim_text


def test_hidden_upper_age_cap_from_exclusion_clause() -> None:
    raw = "Inclusion Criteria:\n- Age >= 18 years\nExclusion Criteria:\n- Age > 70 years\n"
    study = _make_v2_study("NCT99000303", min_age="18 Years", max_age=None, std_ages=["ADULT", "OLDER_ADULT"], criteria=raw)
    rep = audit_structured_vs_text(study)
    assert rep.hidden_upper_age_cap is True
    assert rep.text_max_age_years == 70.0
    assert "Age > 70 years" in rep.evidence[0].verbatim_text


def test_hidden_lower_age_bound() -> None:
    raw = "Inclusion Criteria:\n- Participants must be at least 21 years of age.\n"
    study = _make_v2_study("NCT99000304", min_age="N/A", max_age="N/A", criteria=raw)
    rep = audit_structured_vs_text(study)
    assert rep.hidden_lower_age_bound is True
    assert rep.hidden_upper_age_cap is False
    assert rep.text_min_age_years == 21.0
    assert "hidden_lower_age_bound" in rep.discordance_reasons


def test_consistent_age_bounds_and_one_year_boundary_tolerance() -> None:
    raw_exact = "Inclusion Criteria:\n- Age 18 to 70 years.\n"
    s_exact = _make_v2_study("NCT99000305", min_age="18 Years", max_age="70 Years", std_ages=["ADULT", "OLDER_ADULT"], criteria=raw_exact)
    rep_exact = audit_structured_vs_text(s_exact)
    assert rep_exact.any_discordance is False

    # "< 65 years" in text vs "64 Years" in structured maximumAge differs by 1.0 year -> within tolerance
    raw_excl65 = "Inclusion Criteria:\n- Age >= 18 and < 65 years.\n"
    s_tol = _make_v2_study("NCT99000306", min_age="18 Years", max_age="64 Years", std_ages=["ADULT"], criteria=raw_excl65)
    rep_tol = audit_structured_vs_text(s_tol)
    assert rep_tol.conflicting_age_bounds is False
    assert rep_tol.std_ages_discordance is False


def test_conflicting_upper_and_lower_age_bounds() -> None:
    raw = "Inclusion Criteria:\n- Adults aged 25 to 60 years.\n"
    study = _make_v2_study("NCT99000307", min_age="18 Years", max_age="80 Years", std_ages=["ADULT", "OLDER_ADULT"], criteria=raw)
    rep = audit_structured_vs_text(study)
    assert rep.conflicting_age_bounds is True
    assert "conflicting_min_age" in rep.discordance_reasons
    assert "conflicting_max_age" in rep.discordance_reasons
    assert rep.std_ages_discordance is True
    assert "std_ages_older_adult_excluded_by_text" in rep.discordance_reasons


def test_std_ages_child_discordance_when_text_requires_adults() -> None:
    raw = "Inclusion Criteria:\n- Age >= 18 years.\n"
    study = _make_v2_study("NCT99000308", min_age="18 Years", max_age="N/A", std_ages=["CHILD", "ADULT"], criteria=raw)
    rep = audit_structured_vs_text(study)
    assert rep.std_ages_discordance is True
    assert "std_ages_child_excluded_by_text" in rep.discordance_reasons


def test_sex_discordance_all_vs_postmenopausal_or_female_only() -> None:
    raw = "Inclusion Criteria:\n- Postmenopausal women aged 50 to 75 years with osteopenia.\n"
    study = _make_v2_study("NCT99000309", min_age="50 Years", max_age="75 Years", sex="ALL", criteria=raw)
    rep = audit_structured_vs_text(study)
    assert rep.sex_eligibility_discordance is True
    assert rep.text_sex_restriction == "FEMALE_ONLY"
    assert "structured_sex_all_vs_female_only" in rep.discordance_reasons
    assert raw[rep.evidence[0].start_char : rep.evidence[0].end_char] == rep.evidence[0].verbatim_text


def test_sex_discordance_all_vs_male_excluded_in_exclusion() -> None:
    raw = "Inclusion Criteria:\n- Age >= 18 years\nExclusion Criteria:\n- Male participants.\n"
    study = _make_v2_study("NCT99000310", sex="ALL", criteria=raw)
    rep = audit_structured_vs_text(study)
    assert rep.sex_eligibility_discordance is True
    assert rep.text_sex_restriction == "FEMALE_ONLY"


def test_sex_discordance_female_structured_vs_both_sexes_or_male_in_text() -> None:
    raw_both = "Inclusion Criteria:\n- Male and female participants aged 18 to 65 years.\n"
    s_both = _make_v2_study("NCT99000311", min_age="18 Years", max_age="65 Years", sex="FEMALE", criteria=raw_both)
    rep_both = audit_structured_vs_text(s_both)
    assert rep_both.sex_eligibility_discordance is True
    assert rep_both.text_sex_restriction == "BOTH_SEXES"

    raw_male = "Inclusion Criteria:\n- Male participants only, aged 18 to 55 years.\n"
    s_male = _make_v2_study("NCT99000312", min_age="18 Years", max_age="55 Years", sex="FEMALE", criteria=raw_male)
    rep_male = audit_structured_vs_text(s_male)
    assert rep_male.sex_eligibility_discordance is True
    assert rep_male.text_sex_restriction == "MALE_ONLY"


def test_sex_discordance_male_structured_vs_female_only_in_text() -> None:
    raw_fem = "Inclusion Criteria:\n- Female participants only.\n"
    s_fem = _make_v2_study("NCT99000313", sex="MALE", criteria=raw_fem)
    rep = audit_structured_vs_text(s_fem)
    assert rep.sex_eligibility_discordance is True
    assert rep.text_sex_restriction == "FEMALE_ONLY"


def test_wocbp_contraception_and_males_or_postmenopausal_females_do_not_false_positive_sex() -> None:
    raw = (
        "Inclusion Criteria:\n"
        "- Male participants, or female participants who are postmenopausal or surgically sterile.\n"
        "- Women of childbearing potential must agree to use effective contraception.\n"
        "Exclusion Criteria:\n"
        "- Pregnant or breastfeeding women.\n"
    )
    study = _make_v2_study("NCT99000314", sex="ALL", criteria=raw)
    rep = audit_structured_vs_text(study)
    assert rep.sex_eligibility_discordance is False


def test_maternal_pregnancy_trial_triggers_sex_discordance_when_structured_sex_is_all() -> None:
    raw = "Inclusion Criteria:\n- Singleton pregnancy in second trimester.\n"
    study = _make_v2_study("NCT99000315", sex="ALL", criteria=raw)
    rep = audit_structured_vs_text(study)
    assert rep.sex_eligibility_discordance is True
    assert rep.text_sex_restriction == "FEMALE_ONLY"


def test_healthy_volunteers_discordances_both_directions() -> None:
    raw_inc_hv = "Inclusion Criteria:\n- Healthy adult volunteers aged 18 to 45 years.\n"
    s1 = _make_v2_study("NCT99000316", min_age="18 Years", max_age="45 Years", healthy_volunteers=False, criteria=raw_inc_hv)
    r1 = audit_structured_vs_text(s1)
    assert r1.healthy_volunteers_discordance is True
    assert "structured_healthy_volunteers_false_vs_text_included" in r1.discordance_reasons

    raw_exc_hv = "Inclusion Criteria:\n- Stage IV melanoma\nExclusion Criteria:\n- Healthy volunteers.\n"
    s2 = _make_v2_study("NCT99000317", healthy_volunteers=True, criteria=raw_exc_hv)
    r2 = audit_structured_vs_text(s2)
    assert r2.healthy_volunteers_discordance is True
    assert "structured_healthy_volunteers_true_vs_text_excluded" in r2.discordance_reasons


def test_extracted_civicsample_study_record_layout_parity() -> None:
    raw = "Inclusion Criteria:\n- Female participants only, aged 20 to 60 years.\n"
    extracted_record: dict[str, Any] = {
        "nct_id": "NCT99000318",
        "min_age": "20 Years",
        "max_age": "N/A",
        "std_ages": ["ADULT"],
        "eligibility": {"sex": "ALL", "gender_based": None, "gender_description": None},
        "healthy_volunteers": False,
        "eligibility_criteria": raw,
    }
    parsed = parse_eligibility_text(raw, eligibility_module_or_study=extracted_record)
    assert parsed.discordance is not None
    assert parsed.discordance.hidden_upper_age_cap is True
    assert parsed.discordance.text_max_age_years == 60.0
    assert parsed.discordance.sex_eligibility_discordance is True
    d_dict = parsed.to_dict()
    assert d_dict["discordance"]["hidden_upper_age_cap"] is True


def test_unreadable_structured_age_string_does_not_crash_auditor() -> None:
    mod = {
        "minimumAge": "Eighteen",
        "maximumAge": "Sixty-Five",
        "sex": "ALL",
        "eligibilityCriteria": "Inclusion Criteria:\n- Age 18 to 65 years.\n",
    }
    rep = audit_structured_vs_text(mod)
    assert rep.structured_min_age_years is None
    assert rep.structured_max_age_years is None
    assert rep.hidden_upper_age_cap is True
    assert rep.hidden_lower_age_bound is True


def test_sex_discordance_all_vs_male_participants_only_in_inclusion() -> None:
    raw = "Inclusion Criteria:\n- Male participants only, aged 40 to 80 years.\n"
    study = _make_v2_study("NCT99000350", min_age="40 Years", max_age="80 Years", sex="ALL", criteria=raw)
    rep = audit_structured_vs_text(study)
    assert rep.sex_eligibility_discordance is True
    assert rep.text_sex_restriction == "MALE_ONLY"
    assert "structured_sex_all_vs_male_only" in rep.discordance_reasons


def test_sex_discordance_all_vs_female_excluded_in_exclusion() -> None:
    raw = "Inclusion Criteria:\n- Age >= 18 years\nExclusion Criteria:\n- Female participants.\n"
    study = _make_v2_study("NCT99000351", sex="ALL", criteria=raw)
    rep = audit_structured_vs_text(study)
    assert rep.sex_eligibility_discordance is True
    assert rep.text_sex_restriction == "MALE_ONLY"


def test_sex_discordance_male_structured_vs_both_sexes_in_inclusion() -> None:
    raw = "Inclusion Criteria:\n- Men and women aged 18 to 70 years.\n"
    study = _make_v2_study("NCT99000352", min_age="18 Years", max_age="70 Years", sex="MALE", criteria=raw)
    rep = audit_structured_vs_text(study)
    assert rep.sex_eligibility_discordance is True
    assert rep.text_sex_restriction == "BOTH_SEXES"
    assert "structured_sex_male_vs_both_sexes" in rep.discordance_reasons


def test_multi_cohort_separate_male_and_female_cohorts_resolve_to_both_sexes() -> None:
    raw = (
        "Cohort A Inclusion Criteria:\n"
        "- Female participants only\n"
        "Cohort B Inclusion Criteria:\n"
        "- Male participants only\n"
    )
    study = _make_v2_study("NCT99000353", sex="ALL", criteria=raw)
    rep = audit_structured_vs_text(study)
    assert rep.text_sex_restriction == "BOTH_SEXES"
    assert rep.sex_eligibility_discordance is False


def test_std_ages_older_adult_discordance_when_strict_under_65_in_exclusion() -> None:
    raw = "Inclusion Criteria:\n- Age >= 18 years\nExclusion Criteria:\n- Age >= 65 years\n"
    study = _make_v2_study("NCT99000354", min_age="18 Years", max_age="65 Years", std_ages=["ADULT", "OLDER_ADULT"], criteria=raw)
    rep = audit_structured_vs_text(study)
    assert rep.std_ages_discordance is True
    assert "std_ages_older_adult_excluded_by_text" in rep.discordance_reasons


def test_audit_structured_vs_text_handles_none_module_gracefully() -> None:
    rep = audit_structured_vs_text(None, raw_criteria_text="Inclusion Criteria:\n- Age 18 to 65 years.\n")
    assert rep.hidden_lower_age_bound is True
    assert rep.hidden_upper_age_cap is True
    assert rep.text_min_age_years == 18.0
    assert rep.text_max_age_years == 65.0


def test_discordance_report_to_dict_contains_all_keys() -> None:
    raw = "Inclusion Criteria:\n- Age 18 to 65 years.\n"
    rep = audit_structured_vs_text(_make_v2_study("NCT99000355", min_age="18 Years", max_age="N/A", criteria=raw))
    d = rep.to_dict()
    assert d["hidden_upper_age_cap"] is True
    assert d["text_max_age_years"] == 65.0
    assert isinstance(d["evidence"], list)
    assert len(d["evidence"]) == 1
