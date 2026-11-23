"""Tests for exact character-offset section and clause tokenization (`eligibility/sections.py`)."""
from __future__ import annotations

import pytest

from eligibility.sections import (
    SECTION_KINDS,
    SECTION_LAYOUT_LEVELS,
    SectionedText,
    parse_eligibility_sections,
)


def _assert_exact_offsets(raw_text: str, res: SectionedText) -> None:
    for sec in res.sections:
        assert sec.section_kind in SECTION_KINDS
        assert 0 <= sec.start_char <= sec.end_char <= len(raw_text)
        assert raw_text[sec.start_char:sec.end_char] == sec.verbatim_text
        if sec.section_header is not None:
            assert sec.header_start_char is not None and sec.header_end_char is not None
            assert raw_text[sec.header_start_char:sec.header_end_char] == sec.section_header
    for cl in res.clauses:
        assert cl.section_kind in SECTION_KINDS
        assert 0 <= cl.start_char < cl.end_char <= len(raw_text)
        assert raw_text[cl.start_char:cl.end_char] == cl.verbatim_text
        for s_off, e_off in cl.attached_exception_spans:
            assert cl.start_char <= s_off < e_off <= cl.end_char
            assert len(raw_text[s_off:e_off]) > 0


def test_canonical_layout_levels_constant() -> None:
    assert SECTION_LAYOUT_LEVELS == (
        "both_sections",
        "inclusion_only",
        "exclusion_only",
        "unheaded_text",
        "empty_or_placeholder",
    )


@pytest.mark.parametrize(
    ("raw_input", "expected_reason"),
    [
        (None, "missing"),
        ("", "empty"),
        ("   \n\t\r\n  ", "whitespace_only"),
        ("N/A", "placeholder_token"),
        ("not applicable.", "placeholder_token"),
        ("None", "placeholder_token"),
        ("No eligibility criteria", "no_criteria_placeholder"),
        ("No inclusion or exclusion criteria provided.", "no_criteria_placeholder"),
        ("Please contact site for information", "contact_site_placeholder"),
        ("Please contact the study investigator for eligibility criteria.", "contact_site_placeholder"),
        ("See protocol for full eligibility criteria.", "contact_site_placeholder"),
        ("Eligibility Criteria: Please contact site for information", "contact_site_placeholder"),
    ],
)
def test_empty_or_placeholder_variants(raw_input: str | None, expected_reason: str) -> None:
    res = parse_eligibility_sections(raw_input)
    assert res.section_layout == "empty_or_placeholder"
    assert res.placeholder_reason == expected_reason
    assert res.sections == ()
    assert res.clauses == ()
    assert res.has_unheaded_preamble is False
    assert res.is_multi_cohort is False


def test_non_string_raises_type_error() -> None:
    with pytest.raises(TypeError, match="eligibilityCriteria must be str or None"):
        parse_eligibility_sections(12345)  # type: ignore[arg-type]


def test_both_sections_lf_bullets_exact_offsets() -> None:
    raw = (
        "Inclusion Criteria:\n"
        "  * Age >= 18 years\n"
        "  * ECOG performance status 0-1\n\n"
        "Exclusion Criteria:\n"
        "  - Pregnant or breastfeeding women\n"
        "  - Known HIV infection\n"
    )
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "both_sections"
    assert res.placeholder_reason is None
    assert len(res.sections) == 2
    assert len(res.clauses) == 4
    assert [c.section_kind for c in res.clauses] == ["inclusion", "inclusion", "exclusion", "exclusion"]
    assert res.clauses[0].bullet_marker == "*"
    assert res.clauses[2].bullet_marker == "-"
    _assert_exact_offsets(raw, res)


def test_crlf_and_lone_cr_with_unicode_preserve_exact_offsets() -> None:
    raw = (
        "Key Inclusion Criteria:\r\n"
        "1. Âge ≥ 18 years — confirmed diagnosis (naïve cohort) 🧪\r\n"
        "2. CrCl ≥ 60 mL/min/1.73m²\r"
        "\r"
        "Key Exclusion Criteria:\r\n"
        "(a) Total bilirubin > 1.5 × ULN\r\n"
        "(b) Active CNS metastases\r\n"
    )
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "both_sections"
    assert len(res.clauses) == 4
    assert "🧪" in res.clauses[0].verbatim_text
    assert "1.73m²" in res.clauses[1].verbatim_text
    _assert_exact_offsets(raw, res)


def test_inclusion_only_layout() -> None:
    raw = "Main Inclusion Criteria:\n- Adults aged 18 to 65 years\n- Signed informed consent"
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "inclusion_only"
    assert len(res.sections) == 1
    assert all(c.section_kind == "inclusion" for c in res.clauses)
    _assert_exact_offsets(raw, res)


def test_exclusion_only_layout() -> None:
    raw = "Exclusion Criteria:\n1. Prior malignancy within 3 years\n2. Uncontrolled arrhythmia"
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "exclusion_only"
    assert len(res.sections) == 1
    assert all(c.section_kind == "exclusion" for c in res.clauses)
    _assert_exact_offsets(raw, res)


def test_unheaded_text_never_assumed_inclusion_or_exclusion() -> None:
    raw = (
        "Eligibility Criteria:\n"
        "- Adults aged 21 to 70 years with type 2 diabetes\n"
        "- No history of severe renal failure\n"
    )
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "unheaded_text"
    assert len(res.clauses) == 2
    assert all(c.section_kind == "unheaded" for c in res.clauses)
    assert all(c.section_header is None for c in res.clauses)
    _assert_exact_offsets(raw, res)


def test_unheaded_preamble_before_headed_sections_stays_unheaded() -> None:
    raw = (
        "Patients with histologically confirmed metastatic melanoma.\n\n"
        "Inclusion Criteria:\n"
        "- Age >= 18 years\n\n"
        "Exclusion Criteria:\n"
        "- Active brain metastases\n"
    )
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "both_sections"
    assert res.has_unheaded_preamble is True
    assert res.clauses[0].section_kind == "unheaded"
    assert res.clauses[1].section_kind == "inclusion"
    assert res.clauses[2].section_kind == "exclusion"
    _assert_exact_offsets(raw, res)


def test_negated_and_affirmative_obligation_headers() -> None:
    raw = (
        "Participants must meet all of the following:\n"
        "1. Age 18 to 75 years\n"
        "2. ECOG 0 or 1\n\n"
        "Participants must NOT have any of the following:\n"
        "1. Pregnant or lactating\n"
        "2. Known HIV infection\n"
    )
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "both_sections"
    assert [s.section_kind for s in res.sections] == ["inclusion", "exclusion"]
    assert [c.section_kind for c in res.clauses] == ["inclusion", "inclusion", "exclusion", "exclusion"]
    _assert_exact_offsets(raw, res)


def test_alternative_excluded_header_phrasings() -> None:
    raw = (
        "To be eligible, patients must satisfy all of the following criteria:\n"
        "* Age >= 18 years\n\n"
        "Patients with any of the following will be excluded:\n"
        "* Severe hepatic impairment\n"
    )
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "both_sections"
    assert res.clauses[0].section_kind == "inclusion"
    assert res.clauses[1].section_kind == "exclusion"
    _assert_exact_offsets(raw, res)


def test_multi_part_and_multi_cohort_headers() -> None:
    raw = (
        "Part 1 Inclusion Criteria:\n"
        "- Advanced solid tumors\n"
        "- Age 18 to 70 years\n"
        "Part 1 Exclusion Criteria:\n"
        "- Untreated CNS metastases\n"
        "Part 2 Inclusion Criteria:\n"
        "- Metastatic NSCLC\n"
        "- Age 12 to 17 years\n"
    )
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "both_sections"
    assert res.is_multi_cohort is True
    assert [s.cohort_label for s in res.sections] == ["Part 1", "Part 1", "Part 2"]
    assert res.clauses[1].cohort_label == "Part 1"
    assert res.clauses[4].cohort_label == "Part 2"
    _assert_exact_offsets(raw, res)


def test_markdown_bold_and_hash_headers() -> None:
    raw = (
        "### **Key Inclusion Criteria:**\n"
        "- Age 18 to 65 years\n\n"
        "### **Key Exclusion Criteria:**\n"
        "- Active Hepatitis B or C\n"
    )
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "both_sections"
    assert len(res.clauses) == 2
    _assert_exact_offsets(raw, res)


def test_inline_same_line_headers_and_dual_inline_headers() -> None:
    raw = "Inclusion Criteria: Adults aged 18 to 65 years. Exclusion Criteria: Pregnant women."
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "both_sections"
    assert len(res.clauses) == 2
    assert res.clauses[0].section_kind == "inclusion"
    assert res.clauses[0].verbatim_text == "Adults aged 18 to 65 years."
    assert res.clauses[1].section_kind == "exclusion"
    assert res.clauses[1].verbatim_text == "Pregnant women."
    _assert_exact_offsets(raw, res)


def test_bullet_mentioning_inclusion_criteria_not_misread_as_header() -> None:
    raw = (
        "Exclusion Criteria:\n"
        "- Failure to meet all inclusion criteria: participant is screen failed\n"
        "- Active suicidality\n"
    )
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "exclusion_only"
    assert len(res.clauses) == 2
    assert all(c.section_kind == "exclusion" for c in res.clauses)
    _assert_exact_offsets(raw, res)


def test_indented_exception_and_note_subbullets_attach_to_parent() -> None:
    raw = (
        "Exclusion Criteria:\n"
        "- History of another primary malignancy within 3 years\n"
        "  * Exception: adequately treated basal cell skin cancer\n"
        "  * Note: carcinoma in situ of the cervix is permitted\n"
        "- Known HIV infection\n"
        "Exception: HIV-infected patients on stable ART with undetectable viral load are eligible.\n"
    )
    res = parse_eligibility_sections(raw)
    assert len(res.clauses) == 2
    c0, c1 = res.clauses
    assert c0.has_attached_subclause is True
    assert len(c0.attached_exception_spans) == 2
    assert "basal cell skin cancer" in c0.verbatim_text
    assert "carcinoma in situ" in c0.verbatim_text
    assert c1.has_attached_subclause is True
    assert "undetectable viral load are eligible" in c1.verbatim_text
    _assert_exact_offsets(raw, res)


def test_organ_function_sublist_introducer_keeps_subbullets_independent() -> None:
    raw = (
        "Inclusion Criteria:\n"
        "- Adequate organ function as defined below:\n"
        "  * Total bilirubin <= 1.5 x ULN\n"
        "    Note: <= 3.0 x ULN for patients with Gilbert's syndrome\n"
        "  * Creatinine clearance >= 60 mL/min\n"
    )
    res = parse_eligibility_sections(raw)
    # Stem + bilirubin (with attached Gilbert's Note) + CrCl = 3 clauses
    assert len(res.clauses) == 3
    assert "Total bilirubin" in res.clauses[1].verbatim_text
    assert "Gilbert's syndrome" in res.clauses[1].verbatim_text
    assert res.clauses[1].has_attached_subclause is True
    assert "Creatinine clearance" in res.clauses[2].verbatim_text
    assert "Gilbert" not in res.clauses[2].verbatim_text
    _assert_exact_offsets(raw, res)


def test_wrapped_multiline_sentence_stays_single_clause() -> None:
    raw = (
        "Inclusion Criteria:\n"
        "No active or uncontrolled\n"
        "HIV infection at screening.\n"
        "Age 18 to 70 years.\n"
    )
    res = parse_eligibility_sections(raw)
    assert len(res.clauses) == 2
    assert res.clauses[0].verbatim_text == "No active or uncontrolled\nHIV infection at screening."
    assert res.clauses[1].verbatim_text == "Age 18 to 70 years."
    _assert_exact_offsets(raw, res)


def test_none_clause_under_exclusion_header_is_dropped_cleanly() -> None:
    raw = "Inclusion Criteria:\n- Age >= 18 years\n\nExclusion Criteria:\n- None.\n"
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "both_sections"
    assert len(res.clauses) == 1
    assert res.clauses[0].section_kind == "inclusion"
    _assert_exact_offsets(raw, res)


def test_section_and_clause_to_dict_roundtrip() -> None:
    raw = "Part 1 Inclusion Criteria:\n- Age >= 18 years\n  Note: written consent required\n"
    res = parse_eligibility_sections(raw)
    sec_d = res.sections[0].to_dict()
    cl_d = res.clauses[0].to_dict()
    assert sec_d["section_kind"] == "inclusion"
    assert sec_d["cohort_label"] == "Part 1"
    assert cl_d["has_attached_subclause"] is True
    assert len(cl_d["attached_exception_spans"]) == 1


def test_dose_escalation_and_dose_expansion_cohort_headers() -> None:
    raw = (
        "Dose Escalation Inclusion Criteria:\n"
        "- Advanced solid tumors refractory to standard therapy\n"
        "Dose Expansion Inclusion Criteria:\n"
        "- Measurable disease per RECIST v1.1\n"
        "General Exclusion Criteria:\n"
        "- Uncontrolled intercurrent illness\n"
    )
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "both_sections"
    assert res.is_multi_cohort is True
    assert res.sections[0].cohort_label == "Dose Escalation"
    assert res.sections[1].cohort_label == "Dose Expansion"
    _assert_exact_offsets(raw, res)


def test_roman_numeral_and_parenthesized_section_headers() -> None:
    raw = (
        "I. Inclusion Criteria:\n"
        "(1) Age 18 to 75 years\n"
        "(2) Signed informed consent\n\n"
        "II. Exclusion Criteria:\n"
        "(1) Pregnant or lactating\n"
    )
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "both_sections"
    assert len(res.clauses) == 3
    assert res.clauses[0].bullet_marker == "(1)"
    _assert_exact_offsets(raw, res)


def test_criteria_for_inclusion_and_criteria_for_exclusion_headers() -> None:
    raw = (
        "Criteria for Inclusion:\n"
        "• Confirmed diagnosis\n\n"
        "Criteria for Exclusion:\n"
        "• Active infection\n"
    )
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "both_sections"
    assert [c.bullet_marker for c in res.clauses] == ["•", "•"]
    _assert_exact_offsets(raw, res)


def test_empty_headers_only_yield_empty_or_placeholder() -> None:
    raw = "Inclusion Criteria:\n   \nExclusion Criteria:\n   \n"
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "empty_or_placeholder"
    assert res.clauses == ()


def test_unless_subline_across_single_blank_line_attaches_to_parent() -> None:
    raw = (
        "Exclusion Criteria:\n"
        "1. Known CNS metastases.\n\n"
        "Unless previously treated and radiographically stable for 4 weeks.\n"
    )
    res = parse_eligibility_sections(raw)
    assert len(res.clauses) == 1
    assert res.clauses[0].has_attached_subclause is True
    assert "Unless previously treated" in res.clauses[0].verbatim_text
    _assert_exact_offsets(raw, res)


def test_tab_indented_subbullet_attaches_with_exact_char_offsets() -> None:
    raw = (
        "Exclusion Criteria:\n"
        "- Prior malignancy within 2 years\n"
        "\t- Except basal cell carcinoma of the skin\n"
    )
    res = parse_eligibility_sections(raw)
    assert len(res.clauses) == 1
    assert res.clauses[0].has_attached_subclause is True
    _assert_exact_offsets(raw, res)


def test_abbreviation_eg_does_not_prematurely_split_unbulleted_clause() -> None:
    raw = (
        "Exclusion Criteria:\n"
        "Severe cardiac disease, e.g.\n"
        "Unstable angina or myocardial infarction.\n"
    )
    res = parse_eligibility_sections(raw)
    assert len(res.clauses) == 1
    assert "Unstable angina" in res.clauses[0].verbatim_text
    _assert_exact_offsets(raw, res)


def test_subjects_excluded_if_following_apply_header() -> None:
    raw = (
        "Participants are eligible to be included in the study only if all of the following criteria apply:\n"
        "- Age >= 18 years\n"
        "Participants are excluded from the study if any of the following criteria apply:\n"
        "- HIV positive\n"
    )
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "both_sections"
    assert [c.section_kind for c in res.clauses] == ["inclusion", "exclusion"]
    _assert_exact_offsets(raw, res)


def test_asterisk_wrapped_header_without_subtext() -> None:
    raw = "* Inclusion Criteria:\n- Age 18-65\n* Exclusion Criteria:\n- Pregnancy\n"
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "both_sections"
    assert len(res.clauses) == 2
    _assert_exact_offsets(raw, res)


def test_astral_plane_emoji_before_and_inside_clauses_preserves_python_indexing() -> None:
    raw = "Inclusion Criteria:\n- 🧬 Biomarker positive tumor\nExclusion Criteria:\n- 🚫 Active hepatitis B\n"
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "both_sections"
    _assert_exact_offsets(raw, res)


def test_markdown_horizontal_rule_divider_does_not_emit_bogus_clause() -> None:
    raw = (
        "Inclusion Criteria:\n"
        "- Age >= 18 years\n"
        "---\n"
        "Exclusion Criteria:\n"
        "- HIV positive\n"
    )
    res = parse_eligibility_sections(raw)
    assert res.section_layout == "both_sections"
    assert len(res.clauses) == 2
    _assert_exact_offsets(raw, res)


def test_long_repeated_header_keywords_run_in_linear_time() -> None:
    adversarial_line = ("key " * 40) + "not a header at all\n"
    res = parse_eligibility_sections(adversarial_line)
    assert res.section_layout == "unheaded_text"
    assert len(res.clauses) == 1
    _assert_exact_offsets(adversarial_line, res)
