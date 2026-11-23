"""Round-1 adversarial red-team & fuzzing suite for Track 3 (`eligibility/`).

Attacks five surfaces across `sections.py`, `vocabulary.py`, `parser.py`, `table.py`,
and `README.md`:
  1. Character-offset slice fuzzing across 500 randomized Unicode/CRLF/CR/LF/Markdown
     documents (`NCT99300001`..`NCT99300500`) plus targeted astral/combining/ZWSP cases.
  2. Polarity inversion, negated Inclusion clauses, empty Exclusion placeholders, and
     double-negated / permissive Exclusion clauses.
  3. Disease-targeting vs barrier polarity across HIV, maternal/pregnancy, severe renal
     PK, CNS metastases, and psychiatric/substance-use protocols.
  4. Chronological age polarity inversion, boundary inclusivity (`>` vs `>=`, `<` vs `<=`),
     multi-cohort union/intersection algebra, and non-participant / non-age year traps.
  5. Vocabulary SHA-256 pinning, 20,000-character ReDoS latency bounds (`< 0.5s`),
     malformed study input quarantine, `json.dumps(..., allow_nan=False)` safety,
     `lean_eligibility_row` cohort summary parity, and `README.md` documentation parity.
"""
from __future__ import annotations

import inspect
import json
import random
import time
from pathlib import Path
from typing import Any

import pytest

import eligibility
from eligibility import (
    AGE_CONFLICT_TOLERANCE_YEARS,
    DISCORDANCE_FLAGS,
    DOMAINS,
    DOMAIN_STATUS_LEVELS,
    DOMAIN_SUBSTATUS_LEVELS,
    ELIGIBILITY_PARSE_STATUS_LEVELS,
    EXPECTED_VOCABULARY_SHA256,
    LEAN_COLUMNS,
    PARSER_MODULE_VERSION,
    RULES,
    RULE_EFFECTS,
    SECTION_KINDS,
    SECTION_LAYOUT_LEVELS,
    VOCABULARY_SHA256,
    VOCABULARY_VERSION,
    all_structural_checks_pass,
    audit_structured_vs_text,
    build_eligibility_row,
    compute_vocabulary_sha256,
    extract_clause_age_bounds,
    lean_eligibility_row,
    parse_eligibility_sections,
    parse_eligibility_text,
    parse_error_row,
    structural_checks,
    summarize_cohort_barriers,
    validate_rules,
    verify_vocabulary_pin,
)


def _make_v2_study(
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


def _assert_all_span_invariants(raw_text: str, nct_id: str, study: dict[str, Any]) -> dict[str, Any]:
    sec = parse_eligibility_sections(raw_text)
    n = len(raw_text)
    for s in sec.sections:
        assert 0 <= s.start_char <= s.end_char <= n, (nct_id, s)
        assert raw_text[s.start_char : s.end_char] == s.verbatim_text, (nct_id, s)
        if s.section_header is not None:
            assert s.header_start_char is not None and s.header_end_char is not None
            assert 0 <= s.header_start_char <= s.header_end_char <= n
            assert raw_text[s.header_start_char : s.header_end_char] == s.section_header

    for c in sec.clauses:
        assert 0 <= c.start_char <= c.end_char <= n, (nct_id, c)
        assert raw_text[c.start_char : c.end_char] == c.verbatim_text, (nct_id, c)
        for es, ee in c.attached_exception_spans:
            assert 0 <= c.start_char <= es <= ee <= c.end_char <= n, (nct_id, c, es, ee)
            assert raw_text[es:ee] in c.verbatim_text

    parsed = parse_eligibility_text(raw_text, eligibility_module_or_study=study)
    for dom in DOMAINS:
        da = parsed.domains[dom]
        assert da.status in DOMAIN_STATUS_LEVELS
        if da.status == "threshold_or_conditional":
            assert da.substatus in DOMAIN_SUBSTATUS_LEVELS
        else:
            assert da.substatus is None
        for ev in da.evidence:
            assert 0 <= ev.start_char <= ev.end_char <= n, (nct_id, ev)
            assert raw_text[ev.start_char : ev.end_char] == ev.verbatim_text, (nct_id, ev)

    assert parsed.discordance is not None
    for dev in parsed.discordance.evidence:
        assert 0 <= dev.start_char <= dev.end_char <= n, (nct_id, dev)
        assert raw_text[dev.start_char : dev.end_char] == dev.verbatim_text, (nct_id, dev)

    row = build_eligibility_row(study, snapshot_date="2026-11-15")
    assert row["parse_ok"] is True
    encoded = json.dumps(row, allow_nan=False)
    assert isinstance(encoded, str) and len(encoded) > 2
    lean_encoded = json.dumps(lean_eligibility_row(row), allow_nan=False)
    assert isinstance(lean_encoded, str) and len(lean_encoded) > 2
    return row


# ===========================================================================
# 1. Character-Offset Slice Fuzzing (500 Randomized Documents + Targeted Cases)
# ===========================================================================

_LINE_ENDINGS: tuple[str, ...] = ("\n", "\r\n", "\r")
_WS_POOL: tuple[str, ...] = ("", " ", "  ", "\t", " \t ", "\xa0", " \xa0\t")
_UNICODE_TOKENS: tuple[str, ...] = (
    "",
    "≥",
    "≤",
    "μL",
    "×",
    "\u200b",
    "\xa0",
    "e\u0301",
    "🧪",
    "🫀",
    "🤰",
    "–",
    "—",
)
_INC_HEADERS: tuple[str, ...] = (
    "Inclusion Criteria:",
    "## **Key Inclusion Criteria:**",
    "Part 1 Inclusion Criteria:",
    "Cohort A (Dose Escalation) - Inclusion Criteria:",
    "Participants must meet all of the following criteria:",
    "1. Inclusion Criteria",
    "* Inclusion Criteria:",
)
_EXC_HEADERS: tuple[str, ...] = (
    "Exclusion Criteria:",
    "### _Main Exclusion Criteria:_",
    "Part 1 Exclusion Criteria:",
    "Cohort B - Exclusion Criteria:",
    "Participants must NOT have any of the following:",
    "Any of the following will exclude a participant:",
    "- Exclusion Criteria:",
)
_BULLETS: tuple[str, ...] = ("", "- ", "* ", "+ ", "• ", "1. ", "2) ", "(3) ", "a. ", "(b) ", "iv. ")
_CLAUSE_BODIES: tuple[str, ...] = (
    "Age ≥ 18 years and ≤ 75 years (eGFR ≥ 60 mL/min/1.73m²).",
    "Confirmed HIV-1 infection with CD4+ count ≥ 200 cells/μL 🧪.",
    "AST/ALT ≤ 2.5 × ULN (≤ 5 × ULN with liver metastases); bilirubin ≤ 1.5 × ULN.",
    "Total bilirubin > 1.5 × ULN (except Gilbert’s syndrome up to 3.0 × ULN).",
    "Untreated brain metastases; treated and stable brain metastases ≥ 4 weeks are allowed.",
    "Pregnant or breastfeeding women 🤰.",
    "Women of childbearing potential must agree to contraception.",
    "Prior malignancy within 3 years, except basal cell skin cancer or carcinoma in situ.",
    "Able to read and understand English or Spanish (or via medical interpreter).",
    "ECOG performance status 0–1; LVEF ≥ 50%; QTcF ≤ 470 ms 🫀.",
    "Chronic Hepatitis B (HBsAg positive) unless HBV DNA < 100 IU/mL on antiviral therapy.",
    "History of severe psychiatric illness or active suicidal ideation.",
    "None.",
    "Eligibility Criteria:",
    "---",
)
_QUALIFIERS: tuple[str, ...] = (
    "Note: Participants with Gilbert's syndrome ≤ 3.0 × ULN are eligible.",
    "Exception: Treated CNS metastases stable ≥ 4 weeks are permitted.",
    "Unless cleared HCV RNA is documented at screening.",
    "(Allowance: Medical interpreter services are available.)",
    "Patients with controlled HIV on stable ART are eligible.",
)


def _generate_synthetic_criteria(rng: random.Random) -> str:
    mode = rng.choice(("both", "inc_only", "exc_only", "unheaded", "inline", "multi_cohort", "placeholder"))
    if mode == "placeholder":
        return rng.choice(("", "   \r\n\t\xa0 ", "N/A", "Please contact site for information", "See study protocol"))
    if mode == "inline":
        nl = rng.choice(_LINE_ENDINGS)
        b1 = rng.choice(_CLAUSE_BODIES)
        b2 = rng.choice(_CLAUSE_BODIES)
        return f"  Inclusion Criteria: {b1} Exclusion Criteria: {b2}  {nl}"

    chunks: list[str] = []
    for _ in range(rng.randint(1, 4)):
        ws = rng.choice(_WS_POOL)
        nl = rng.choice(_LINE_ENDINGS)
        if mode == "both":
            hdr = rng.choice(_INC_HEADERS if rng.random() < 0.5 else _EXC_HEADERS)
            chunks.append(f"{ws}{hdr}{ws}{nl}")
        elif mode == "inc_only":
            chunks.append(f"{ws}{rng.choice(_INC_HEADERS)}{ws}{nl}")
        elif mode == "exc_only":
            chunks.append(f"{ws}{rng.choice(_EXC_HEADERS)}{ws}{nl}")
        elif mode == "multi_cohort":
            coh = rng.choice(("Cohort A", "Cohort B", "Part 1", "Part 2", "Dose Expansion"))
            kind = rng.choice(("Inclusion Criteria:", "Exclusion Criteria:"))
            chunks.append(f"{ws}{coh} {kind}{ws}{nl}")

        for _ in range(rng.randint(1, 5)):
            bul = rng.choice(_BULLETS)
            dec = rng.choice(_UNICODE_TOKENS)
            body = rng.choice(_CLAUSE_BODIES)
            lws = rng.choice(_WS_POOL)
            tws = rng.choice(_WS_POOL)
            lnl = rng.choice(_LINE_ENDINGS)
            chunks.append(f"{lws}{bul}{body} {dec}{tws}{lnl}")
            if rng.random() < 0.35:
                chunks.append(f"{lws}    and measurable disease per RECIST v1.1 {rng.choice(_UNICODE_TOKENS)}{lnl}")
            if rng.random() < 0.4:
                if rng.random() < 0.3:
                    chunks.append(rng.choice(_LINE_ENDINGS))
                q_prefix = lws + "    " + rng.choice(("", "- ", "* "))
                chunks.append(f"{q_prefix}{rng.choice(_QUALIFIERS)}{rng.choice(_LINE_ENDINGS)}")
    return "".join(chunks)


def test_fuzz_500_randomized_unicode_crlf_markdown_documents() -> None:
    """Fuzz 500 synthetic documents (NCT99300001..NCT99300500) for exact slice & JSON invariants."""
    rng = random.Random(20261123)
    rows: list[dict[str, Any]] = []
    for idx in range(1, 501):
        nct_id = "NCT" + f"{99300000 + idx}"
        raw_text = _generate_synthetic_criteria(rng)
        study = _make_v2_study(
            nct_id,
            raw_text,
            min_age=rng.choice(("18 Years", "N/A", "6 Months")),
            max_age=rng.choice(("75 Years", "N/A")),
            sex=rng.choice(("ALL", "FEMALE", "MALE")),
            healthy_volunteers=rng.choice((True, False)),
            std_ages=rng.choice((("ADULT", "OLDER_ADULT"), ("CHILD", "ADULT"))),
        )
        rows.append(_assert_all_span_invariants(raw_text, nct_id, study))

    assert len(rows) == 500
    checks = structural_checks(rows)
    assert all_structural_checks_pass(checks), checks


def test_slice_invariants_astral_emojis_combining_marks_and_mixed_line_endings() -> None:
    raw = (
        "\t\xa0Inclusion Criteria:\r\n"
        "  1. 🧪🤰 Re\u0301sume\u0301 of documented HIV-1 infection (CD4+ ≥ 200 cells/μL × 10⁰)\r"
        "     with plasma HIV-1 RNA ≥ 1,000 copies/mL.\r\n"
        "\r"
        "Exclusion Criteria:\r"
        "  (a) Total bilirubin > 1.5 × ULN 🫀\r\n"
        "      Note: Gilbert’s syndrome ≤ 3.0 × ULN is allowed.\r"
    )
    study = _make_v2_study("NCT99300501", raw)
    row = _assert_all_span_invariants(raw, "NCT99300501", study)
    assert row["hiv_infection_status"] == "explicitly_included_or_targeted"
    assert row["hepatic_function_status"] == "threshold_or_conditional"
    assert row["has_gilberts_exception"] is True


def test_slice_invariants_single_line_inline_and_midline_headers() -> None:
    raw = (
        "  \xa0Part 1 Inclusion Criteria: Age ≥ 18 years; ECOG 0–1 🧪. "
        "Part 1 Exclusion Criteria: Untreated brain metastases "
        "(except treated and stable ≥ 4 weeks).  "
    )
    study = _make_v2_study("NCT99300502", raw)
    row = _assert_all_span_invariants(raw, "NCT99300502", study)
    assert row["section_layout"] == "both_sections"
    assert row["cns_or_brain_metastases_status"] == "threshold_or_conditional"


def test_slice_invariants_lone_cr_only_document_with_blank_gap_qualifier() -> None:
    raw = (
        "Inclusion Criteria:\r"
        "- Age 18 to 70 years\r"
        "\r"
        "Exclusion Criteria:\r"
        "- Prior malignancy within 3 years\r"
        "\r"
        "  Exception: Basal cell carcinoma of the skin is permitted.\r"
    )
    study = _make_v2_study("NCT99300503", raw)
    row = _assert_all_span_invariants(raw, "NCT99300503", study)
    assert row["prior_malignancy_status"] == "threshold_or_conditional"
    assert row["prior_malignancy_substatus"] == "exception_permitted"
    assert row["has_skin_or_insitu_exception"] is True


def test_slice_invariants_crlf_multi_cohort_obligation_headers() -> None:
    raw = (
        "Cohort A: Inclusion Criteria:\r\n"
        "  * Age 18 to 64 years\r\n"
        "Cohort B: Inclusion Criteria:\r\n"
        "  * Age 65 to 85 years\r\n"
        "Participants must NOT have any of the following:\r\n"
        "  * Active Hepatitis B\r\n"
    )
    study = _make_v2_study("NCT99300504", raw, min_age="18 Years", max_age="85 Years")
    row = _assert_all_span_invariants(raw, "NCT99300504", study)
    assert row["is_multi_cohort"] is True
    assert row["section_layout"] == "both_sections"
    assert row["text_min_age_years"] == 18.0
    assert row["text_max_age_years"] == 85.0
    assert row["hepatitis_b_or_c_status"] == "strictly_excluded"


def test_slice_invariants_bullet_mentioning_inclusion_criteria_not_treated_as_header() -> None:
    raw = (
        "Exclusion Criteria:\n"
        "- Failure to meet all inclusion criteria: participant is ineligible.\n"
        "- Active Hepatitis C infection.\n"
    )
    study = _make_v2_study("NCT99300505", raw)
    row = _assert_all_span_invariants(raw, "NCT99300505", study)
    assert row["section_layout"] == "exclusion_only"
    assert row["hepatitis_b_or_c_status"] == "strictly_excluded"


def test_slice_invariants_organ_sublist_stem_keeps_child_bullets_independent() -> None:
    raw = (
        "Inclusion Criteria:\n"
        "1. Adequate organ function as defined below:\n"
        "   - eGFR >= 45 mL/min/1.73m2\n"
        "   - Total bilirubin <= 1.5 x ULN\n"
        "   - LVEF >= 50%\n"
    )
    study = _make_v2_study("NCT99300506", raw)
    row = _assert_all_span_invariants(raw, "NCT99300506", study)
    assert row["n_inclusion_clauses"] == 4
    assert row["renal_function_status"] == "threshold_or_conditional"
    assert row["hepatic_function_status"] == "threshold_or_conditional"
    assert row["cardiac_function_status"] == "threshold_or_conditional"


def test_defect_midline_header_skipped_when_line_starts_with_eligibility_banner() -> None:
    """DEFECT: `_split_inline_headers_on_line` returns early if `_INLINE_HEADER_RX` misses char 0, skipping midline headers."""
    raw = (
        "Study Eligibility Criteria: Inclusion Criteria: Age 18 to 65 years. "
        "Exclusion Criteria: Pregnant or breastfeeding women."
    )
    row = build_eligibility_row(_make_v2_study("NCT99300507", raw))
    assert row["section_layout"] == "both_sections"
    assert row["pregnancy_or_lactation_status"] == "strictly_excluded"


def test_defect_wrapped_indented_exception_subbullet_truncates_exception_span() -> None:
    """DEFECT: Soft-wrapped continuation line on an indented exception sub-bullet is omitted from `attached_exception_spans`."""
    raw = (
        "Exclusion Criteria:\n"
        "- Active brain metastases\n"
        "    - Except patients with treated brain metastases who are\n"
        "      clinically stable for >= 4 weeks\n"
    )
    parsed = parse_eligibility_text(raw)
    cns = parsed.domains["cns_or_brain_metastases"]
    assert cns.status == "threshold_or_conditional"
    assert len(cns.evidence) == 1
    note = cns.evidence[0].exception_note or ""
    assert "clinically stable for >= 4 weeks" in note


# ===========================================================================
# 2. Polarity Inversion, Negated Inclusion, & Empty / Permissive Exclusion
# ===========================================================================


def test_empty_exclusion_none_token_does_not_create_bogus_clause() -> None:
    row = build_eligibility_row(_make_v2_study("NCT99300510", "Exclusion Criteria:\n  None."))
    assert row["eligibility_parse_status"] == "empty_or_placeholder"
    assert row["n_clauses"] == 0
    assert row["n_exclusion_clauses"] == 0
    for dom in DOMAINS:
        assert row[f"{dom}_status"] == "not_mentioned"


def test_empty_or_placeholder_variants_return_empty_or_placeholder_layout() -> None:
    for idx, placeholder in enumerate(
        (None, "", "  \t\r\n ", "N/A", "Please contact site for eligibility criteria", "See study protocol"),
        start=540,
    ):
        row = build_eligibility_row(_make_v2_study("NCT" + f"{99300000 + idx}", placeholder))
        assert row["eligibility_parse_status"] == "empty_or_placeholder"
        assert row["section_layout"] == "empty_or_placeholder"
        assert row["n_clauses"] == 0


def test_defect_empty_exclusion_no_specific_exclusion_criteria_creates_bogus_clause() -> None:
    """DEFECT: `'Exclusion Criteria: No specific exclusion criteria'` emits a bogus exclusion ClauseSpan instead of 0 clauses / placeholder."""
    row = build_eligibility_row(
        _make_v2_study("NCT99300511", "Exclusion Criteria: No specific exclusion criteria")
    )
    assert row["n_exclusion_clauses"] == 0
    assert row["n_clauses"] == 0
    assert row["eligibility_parse_status"] == "empty_or_placeholder"


def test_defect_empty_exclusion_none_known_under_both_sections_creates_bogus_exclusion_clause() -> None:
    """DEFECT: `'No specific exclusion criteria.'` / `'None known.'` under Exclusion Criteria inflates `n_exclusion_clauses`."""
    raw = (
        "Inclusion Criteria:\n"
        "- Age >= 18 years\n"
        "Exclusion Criteria:\n"
        "- No specific exclusion criteria.\n"
    )
    row = build_eligibility_row(_make_v2_study("NCT99300512", raw))
    assert row["n_inclusion_clauses"] == 1
    assert row["n_exclusion_clauses"] == 0


def test_negated_inclusion_not_pregnant_or_breastfeeding_is_strictly_excluded() -> None:
    raw = "Inclusion Criteria:\n - Not pregnant or breastfeeding"
    row = build_eligibility_row(_make_v2_study("NCT99300513", raw))
    assert row["pregnancy_or_lactation_status"] == "strictly_excluded"


def test_negated_inclusion_no_active_brain_metastases_is_strictly_excluded() -> None:
    raw = "Inclusion Criteria:\n - No active brain metastases"
    row = build_eligibility_row(_make_v2_study("NCT99300514", raw))
    assert row["cns_or_brain_metastases_status"] == "strictly_excluded"


def test_separate_contraception_clause_never_overrides_separate_pregnancy_exclusion() -> None:
    raw = (
        "Inclusion Criteria:\n"
        "- Women of childbearing potential must agree to use highly effective contraception.\n"
        "Exclusion Criteria:\n"
        "- Pregnant or lactating women.\n"
    )
    row = build_eligibility_row(_make_v2_study("NCT99300535", raw))
    assert row["pregnancy_or_lactation_status"] == "strictly_excluded"


def test_defect_negated_inclusion_hiv_hbv_hcv_drops_hepatitis_domain() -> None:
    """DEFECT: Coordinated negated Inclusion `'No history of HIV, HBV, or HCV infection'` misses `hepatitis_b_or_c` (`not_mentioned`)."""
    raw = "Inclusion Criteria:\n - No history of HIV, HBV, or HCV infection"
    row = build_eligibility_row(_make_v2_study("NCT99300515", raw))
    assert row["hiv_infection_status"] == "strictly_excluded"
    assert row["hepatitis_b_or_c_status"] == "strictly_excluded"


def test_defect_negated_inclusion_hbv_hcv_hiv_drops_hiv_domain() -> None:
    """DEFECT: Reversed coordinated list `'No history of HBV, HCV, or HIV infection'` misses `hiv_infection` (`not_mentioned`)."""
    raw = "Inclusion Criteria:\n - No history of HBV, HCV, or HIV infection"
    row = build_eligibility_row(_make_v2_study("NCT99300516", raw))
    assert row["hepatitis_b_or_c_status"] == "strictly_excluded"
    assert row["hiv_infection_status"] == "strictly_excluded"


def test_defect_negated_inclusion_no_pregnancy_or_lactation_missed() -> None:
    """DEFECT: `'Inclusion Criteria: - No pregnancy or lactation'` resolves to `not_mentioned` instead of `strictly_excluded`."""
    raw = "Inclusion Criteria:\n - No pregnancy or lactation"
    row = build_eligibility_row(_make_v2_study("NCT99300517", raw))
    assert row["pregnancy_or_lactation_status"] == "strictly_excluded"


def test_defect_negated_inclusion_no_evidence_of_brain_metastases_inverted_to_targeted() -> None:
    """DEFECT: `'No evidence of brain metastases'` in Inclusion is inverted into `explicitly_included_or_targeted`."""
    raw = "Inclusion Criteria:\n - No evidence of brain metastases"
    row = build_eligibility_row(_make_v2_study("NCT99300518", raw))
    assert row["cns_or_brain_metastases_status"] == "strictly_excluded"


def test_defect_negated_inclusion_must_not_have_brain_metastases_inverted_to_targeted() -> None:
    """DEFECT: `'Must not have brain metastases'` in Inclusion is inverted into `explicitly_included_or_targeted`."""
    raw = "Inclusion Criteria:\n - Must not have brain metastases"
    row = build_eligibility_row(_make_v2_study("NCT99300519", raw))
    assert row["cns_or_brain_metastases_status"] == "strictly_excluded"


def test_defect_negated_inclusion_hiv_or_active_hbv_inverts_hbv_to_targeted() -> None:
    """DEFECT: `'No history of HIV or active Hepatitis B infection'` in Inclusion misclassifies HBV as `explicitly_included_or_targeted`."""
    raw = "Inclusion Criteria:\n - No history of HIV or active Hepatitis B infection"
    row = build_eligibility_row(_make_v2_study("NCT99300520", raw))
    assert row["hiv_infection_status"] == "strictly_excluded"
    assert row["hepatitis_b_or_c_status"] == "strictly_excluded"


def test_defect_negated_inclusion_no_prior_or_documented_hiv_inverted_to_targeted() -> None:
    """DEFECT: `'No prior or documented HIV-1 infection'` in Inclusion misclassifies HIV as `explicitly_included_or_targeted`."""
    raw = "Inclusion Criteria:\n - No prior or documented HIV-1 infection"
    row = build_eligibility_row(_make_v2_study("NCT99300521", raw))
    assert row["hiv_infection_status"] == "strictly_excluded"


def test_defect_negated_inclusion_must_not_have_cirrhosis_inverted_to_targeted() -> None:
    """DEFECT: `'Must not have hepatic cirrhosis'` in Inclusion misclassifies `hepatic_function` as `explicitly_included_or_targeted`."""
    raw = "Inclusion Criteria:\n - Must not have hepatic cirrhosis"
    row = build_eligibility_row(_make_v2_study("NCT99300522", raw))
    assert row["hepatic_function_status"] == "strictly_excluded"


def test_defect_negated_inclusion_must_not_have_heart_failure_inverted_to_targeted() -> None:
    """DEFECT: `'Must not have congestive heart failure'` in Inclusion misclassifies `cardiac_function` as `explicitly_included_or_targeted`."""
    raw = "Inclusion Criteria:\n - Must not have congestive heart failure"
    row = build_eligibility_row(_make_v2_study("NCT99300523", raw))
    assert row["cardiac_function_status"] == "strictly_excluded"


def test_defect_negated_inclusion_must_not_have_mdd_inverted_to_targeted() -> None:
    """DEFECT: `'Must not have current Major Depressive Disorder'` in Inclusion misclassifies psychiatry as `explicitly_included_or_targeted`."""
    raw = "Inclusion Criteria:\n - Must not have current Major Depressive Disorder"
    row = build_eligibility_row(_make_v2_study("NCT99300524", raw))
    assert row["psychiatric_or_substance_use_status"] == "strictly_excluded"


def test_defect_negated_inclusion_must_not_have_ecog_ge_2_inverted_to_targeted() -> None:
    """DEFECT: `'Must not have ECOG performance status >= 2'` in Inclusion misclassifies frail-PS targeting instead of numeric cutoff."""
    raw = "Inclusion Criteria:\n - Must not have ECOG performance status >= 2"
    row = build_eligibility_row(_make_v2_study("NCT99300525", raw))
    assert row["performance_status_status"] == "threshold_or_conditional"
    assert row["performance_status_substatus"] == "numeric_cutoff"
    assert row["ecog_max_allowed"] == 1


def test_defect_negated_inclusion_sex_not_restricted_to_female_only_false_discordance() -> None:
    """DEFECT: `'Not restricted to female participants only'` in Inclusion triggers false `FEMALE_ONLY` sex discordance."""
    raw = "Inclusion Criteria:\n - Not restricted to female participants only"
    row = build_eligibility_row(_make_v2_study("NCT99300526", raw, sex="ALL"))
    assert row["sex_eligibility_discordance"] is False


def test_permissive_exclusion_hiv_not_excluded_if_suppressed_is_conditional() -> None:
    raw = "Exclusion Criteria:\n - HIV-positive patients are NOT excluded if viral load < 50 copies/mL"
    row = build_eligibility_row(_make_v2_study("NCT99300527", raw))
    assert row["hiv_infection_status"] == "threshold_or_conditional"
    assert row["hiv_infection_substatus"] == "exception_permitted"


def test_permissive_exclusion_untreated_brain_mets_with_stable_allowance_is_conditional() -> None:
    raw = "Exclusion Criteria:\n - Untreated brain metastases (treated and stable brain metastases for >= 4 weeks are allowed)"
    row = build_eligibility_row(_make_v2_study("NCT99300528", raw))
    assert row["cns_or_brain_metastases_status"] == "threshold_or_conditional"
    assert row["cns_or_brain_metastases_substatus"] == "exception_permitted"
    assert row["free_text_age_cutoff_status"] == "not_mentioned"


def test_hiv_cd4_and_undetectable_viral_load_thresholds_extracted_in_exclusion() -> None:
    raw = (
        "Exclusion Criteria:\n"
        "- Known HIV infection unless on stable ART with CD4+ count >= 350 cells/uL and undetectable viral load."
    )
    row = build_eligibility_row(_make_v2_study("NCT99300536", raw))
    assert row["hiv_infection_status"] == "threshold_or_conditional"
    assert row["hiv_infection_substatus"] == "exception_permitted"
    thresh = row["domains"]["hiv_infection"]["extracted_thresholds"]
    assert thresh["cd4_min_cells_ul"] == 350
    assert thresh["requires_undetectable_viral_load"] is True


def test_hbv_hcv_cleared_rna_and_suppressed_dna_allowances_extracted() -> None:
    raw = (
        "Exclusion Criteria:\n"
        "- Active Hepatitis B or C, except cured HCV with undetectable HCV RNA or chronic HBV with undetectable HBV DNA on antiviral therapy."
    )
    row = build_eligibility_row(_make_v2_study("NCT99300537", raw))
    assert row["hepatitis_b_or_c_status"] == "threshold_or_conditional"
    assert row["hepatitis_b_or_c_substatus"] == "exception_permitted"
    thresh = row["domains"]["hepatitis_b_or_c"]["extracted_thresholds"]
    assert thresh["hcv_rna_negative_allowed"] is True
    assert thresh["hbv_dna_suppressed_allowed"] is True


def test_hepatic_liver_metastases_secondary_uln_without_bleeding_into_baseline() -> None:
    raw = (
        "Inclusion Criteria:\n"
        "- AST and ALT <= 2.5 x ULN (<= 5.0 x ULN for patients with liver metastases); total bilirubin <= 1.5 x ULN."
    )
    row = build_eligibility_row(_make_v2_study("NCT99300538", raw))
    thresh = row["domains"]["hepatic_function"]["extracted_thresholds"]
    assert thresh["ast_max_uln"] == 2.5
    assert thresh["alt_max_uln"] == 2.5
    assert thresh["ast_liver_mets_max_uln"] == 5.0
    assert thresh["alt_liver_mets_max_uln"] == 5.0
    assert thresh["bilirubin_max_uln"] == 1.5


def test_cardiac_lvef_qtcf_and_nyha_exclusion_inversion() -> None:
    raw = (
        "Inclusion Criteria:\n"
        "- LVEF >= 50%\n"
        "Exclusion Criteria:\n"
        "- QTcF > 470 ms or NYHA functional class III or IV heart failure."
    )
    row = build_eligibility_row(_make_v2_study("NCT99300539", raw))
    assert row["cardiac_function_status"] == "threshold_or_conditional"
    thresh = row["domains"]["cardiac_function"]["extracted_thresholds"]
    assert thresh["lvef_min_pct"] == 50.0
    assert thresh["qtc_max_ms"] == 470.0
    assert thresh["nyha_max_allowed"] == 2


def test_performance_status_ecog_exclusion_ge2_vs_gt2_inversion() -> None:
    r_ge2 = build_eligibility_row(_make_v2_study("NCT99300546", "Exclusion Criteria:\n- ECOG performance status >= 2"))
    assert r_ge2["ecog_max_allowed"] == 1
    r_gt2 = build_eligibility_row(_make_v2_study("NCT99300547", "Exclusion Criteria:\n- ECOG performance status > 2"))
    assert r_gt2["ecog_max_allowed"] == 2


def test_prior_malignancy_lookback_months_and_skin_insitu_exception() -> None:
    raw = "Exclusion Criteria:\n- Prior invasive malignancy within 24 months, except basal cell skin carcinoma or carcinoma in situ."
    row = build_eligibility_row(_make_v2_study("NCT99300548", raw))
    assert row["prior_malignancy_status"] == "threshold_or_conditional"
    assert row["prior_malignancy_substatus"] == "exception_permitted"
    assert row["has_skin_or_insitu_exception"] is True
    assert row["domains"]["prior_malignancy"]["extracted_thresholds"]["lookback_years"] == 2.0


def test_language_monolingual_exclusion_vs_multilingual_allowance() -> None:
    r_mono = build_eligibility_row(
        _make_v2_study("NCT99300549", "Inclusion Criteria:\n- Must be fluent in spoken and written English.")
    )
    assert r_mono["language_or_literacy_status"] == "strictly_excluded"

    r_multi = build_eligibility_row(
        _make_v2_study("NCT99300550", "Inclusion Criteria:\n- Able to read and understand English or Spanish.")
    )
    assert r_multi["language_or_literacy_status"] == "threshold_or_conditional"
    assert r_multi["language_or_literacy_substatus"] == "exception_permitted"
    assert r_multi["domains"]["language_or_literacy"]["extracted_thresholds"]["accommodation"] == "multilingual"


def test_defect_permissive_exclusion_non_english_not_excluded_with_interpreter() -> None:
    """DEFECT: `'Non-English-speaking patients are NOT excluded if a medical interpreter is used'` resolves to `strictly_excluded`."""
    raw = "Exclusion Criteria:\n - Non-English-speaking patients are NOT excluded if a medical interpreter is used"
    row = build_eligibility_row(_make_v2_study("NCT99300529", raw))
    assert row["language_or_literacy_status"] == "threshold_or_conditional"
    assert row["language_or_literacy_substatus"] == "exception_permitted"


def test_defect_pregnancy_exclusion_with_contraception_parenthetical_falsely_permitted() -> None:
    """DEFECT: Contraception allowance in Exclusion parenthetical overrides explicit pregnancy exclusion into `exception_permitted`."""
    raw = "Exclusion Criteria:\n - Pregnant or breastfeeding women (contraception is allowed)"
    row = build_eligibility_row(_make_v2_study("NCT99300530", raw))
    assert row["pregnancy_or_lactation_status"] == "strictly_excluded"


def test_defect_gilberts_explicitly_excluded_with_uln_misclassified_as_exception() -> None:
    """DEFECT: Explicit Gilbert's exclusion alongside `'ULN'` sets `has_gilberts_exception=True` and `exception_permitted`."""
    raw = "Exclusion Criteria:\n - Total bilirubin > 1.5 x ULN (patients with Gilbert's syndrome are also excluded)"
    row = build_eligibility_row(_make_v2_study("NCT99300531", raw))
    assert row["has_gilberts_exception"] is False
    assert row["hepatic_function_substatus"] != "exception_permitted"


def test_defect_prior_malignancy_including_squamous_cell_misclassified_as_skin_exception() -> None:
    """DEFECT: `'including squamous cell carcinoma'` in Exclusion is misread as a skin-cancer exception (`has_skin_or_insitu_exception=True`)."""
    raw = "Exclusion Criteria:\n - History of another primary malignancy, including squamous cell carcinoma of the head and neck"
    row = build_eligibility_row(_make_v2_study("NCT99300532", raw))
    assert row["has_skin_or_insitu_exception"] is False
    assert row["prior_malignancy_status"] == "strictly_excluded"


def test_defect_healthy_volunteers_bullet_in_inclusion_inverted_to_excluded() -> None:
    """DEFECT: Standalone `'- Healthy adult volunteers.'` in Inclusion matches `_HEALTHY_VOLUNTEER_EXC_RX` and inverts polarity."""
    raw = "Inclusion Criteria:\n - Healthy adult volunteers.\n - Age 18 to 55 years."
    row_true = build_eligibility_row(
        _make_v2_study("NCT99300533", raw, min_age="18 Years", max_age="55 Years", healthy_volunteers=True)
    )
    assert row_true["discordance"]["text_healthy_volunteers_status"] == "HEALTHY_INCLUDED"
    assert row_true["healthy_volunteers_discordance"] is False

    row_false = build_eligibility_row(
        _make_v2_study("NCT99300534", raw, min_age="18 Years", max_age="55 Years", healthy_volunteers=False)
    )
    assert row_false["healthy_volunteers_discordance"] is True


def test_sex_discordance_ignores_conditional_wocbp_and_partner_contraception() -> None:
    raw = (
        "Inclusion Criteria:\n"
        "- If female of childbearing potential, must have negative pregnancy test.\n"
        "- Female partners of male participants must use contraception.\n"
    )
    row = build_eligibility_row(_make_v2_study("NCT99300551", raw, sex="ALL"))
    assert row["sex_eligibility_discordance"] is False


def test_sex_discordance_flags_postmenopausal_women_only_when_structured_sex_all() -> None:
    raw = "Inclusion Criteria:\n- Postmenopausal women aged 50 to 75 years."
    row = build_eligibility_row(_make_v2_study("NCT99300552", raw, min_age="50 Years", max_age="75 Years", sex="ALL"))
    assert row["sex_eligibility_discordance"] is True


# ===========================================================================
# 3. Disease-Targeting False-Positive & Contradiction Attacks
# ===========================================================================


def test_disease_targeting_hiv_cure_protocol_with_hbv_exclusion() -> None:
    raw = (
        "Inclusion Criteria:\n"
        " 1. Documented HIV-1 infection with plasma HIV-1 RNA >= 1,000 copies/mL\n"
        "Exclusion Criteria:\n"
        " 1. Co-infection with active Hepatitis B"
    )
    row = build_eligibility_row(_make_v2_study("NCT99300601", raw))
    assert row["hiv_infection_status"] == "explicitly_included_or_targeted"
    assert row["hepatitis_b_or_c_status"] == "strictly_excluded"


def test_disease_targeting_maternal_singleton_pregnancy_weeks_gestation() -> None:
    raw = "Inclusion Criteria:\n 1. Singleton pregnancy between 24 and 36 weeks gestation"
    row = build_eligibility_row(_make_v2_study("NCT99300602", raw, sex="FEMALE"))
    assert row["pregnancy_or_lactation_status"] == "explicitly_included_or_targeted"
    assert row["free_text_age_cutoff_status"] == "not_mentioned"
    assert row["text_min_age_years"] is None
    assert row["text_max_age_years"] is None


def test_defect_disease_targeting_confirmed_pregnancy_between_weeks_gestation_missed() -> None:
    """DEFECT: `'Confirmed pregnancy between 24 and 36 weeks gestation'` misses `preg_target_inclusion` (`not_mentioned`)."""
    raw = "Inclusion Criteria:\n 1. Confirmed pregnancy between 24 and 36 weeks gestation"
    row = build_eligibility_row(_make_v2_study("NCT99300603", raw, sex="FEMALE"))
    assert row["pregnancy_or_lactation_status"] == "explicitly_included_or_targeted"
    assert row["free_text_age_cutoff_status"] == "not_mentioned"


def test_disease_targeting_severe_renal_impairment_pk_study() -> None:
    raw = (
        "Inclusion Criteria:\n"
        " 1. Severe renal impairment defined as eGFR < 30 mL/min/1.73m2 or end-stage renal disease on hemodialysis"
    )
    row = build_eligibility_row(_make_v2_study("NCT99300604", raw))
    assert row["renal_function_status"] == "explicitly_included_or_targeted"


def test_defect_disease_targeting_renal_pk_with_acute_renal_failure_exclusion_collapses() -> None:
    """DEFECT: Severe renal impairment PK trial excluding acute renal failure collapses from `explicitly_included_or_targeted` to `ambiguous_or_unheaded`."""
    raw = (
        "Inclusion Criteria:\n"
        " 1. Severe renal impairment defined as eGFR < 30 mL/min/1.73m2 or end-stage renal disease on hemodialysis\n"
        "Exclusion Criteria:\n"
        " 1. Acute renal failure within 30 days"
    )
    row = build_eligibility_row(_make_v2_study("NCT99300605", raw))
    assert row["renal_function_status"] == "explicitly_included_or_targeted"


def test_disease_targeting_brain_metastases_radiotherapy_trial() -> None:
    raw = "Inclusion Criteria:\n 1. 1 to 4 newly diagnosed brain metastases from NSCLC"
    row = build_eligibility_row(_make_v2_study("NCT99300606", raw))
    assert row["cns_or_brain_metastases_status"] == "explicitly_included_or_targeted"


def test_disease_targeting_moderate_to_severe_aud_trial() -> None:
    raw = "Inclusion Criteria:\n 1. DSM-5 diagnosis of Moderate-to-Severe Alcohol Use Disorder"
    row = build_eligibility_row(_make_v2_study("NCT99300607", raw))
    assert row["psychiatric_or_substance_use_status"] == "explicitly_included_or_targeted"


def test_defect_disease_targeting_dsm5_severe_mdd_missed() -> None:
    """DEFECT: `'DSM-5 diagnosis of Severe Major Depressive Disorder'` resolves to `not_mentioned` instead of `explicitly_included_or_targeted`."""
    raw = "Inclusion Criteria:\n 1. DSM-5 diagnosis of Severe Major Depressive Disorder"
    row = build_eligibility_row(_make_v2_study("NCT99300608", raw))
    assert row["psychiatric_or_substance_use_status"] == "explicitly_included_or_targeted"


# ===========================================================================
# 4. Free-Text Age Polarity, Inclusivity, & Non-Age Year Traps
# ===========================================================================


def test_age_exclusion_strict_lt18_or_gt75_yields_inclusive_18_to_75() -> None:
    raw = "Exclusion Criteria:\n - Age < 18 years or > 75 years"
    row = build_eligibility_row(_make_v2_study("NCT99300701", raw, min_age="N/A", max_age="N/A"))
    age_thresh = row["domains"]["free_text_age_cutoff"]["extracted_thresholds"]
    assert row["text_min_age_years"] == 18.0
    assert row["text_max_age_years"] == 75.0
    assert age_thresh["min_inclusive"] is True
    assert age_thresh["max_inclusive"] is True
    assert row["hidden_lower_age_bound"] is True
    assert row["hidden_upper_age_cap"] is True


def test_age_exclusion_nonstrict_le18_or_ge75_yields_exclusive_bounds() -> None:
    raw = "Exclusion Criteria:\n - Age <= 18 years or >= 75 years"
    row = build_eligibility_row(_make_v2_study("NCT99300702", raw, min_age="N/A", max_age="N/A"))
    age_thresh = row["domains"]["free_text_age_cutoff"]["extracted_thresholds"]
    assert row["text_min_age_years"] == 18.0
    assert row["text_max_age_years"] == 75.0
    assert age_thresh["min_inclusive"] is False
    assert age_thresh["max_inclusive"] is False


def test_age_exclusion_ge65_vs_gt65_std_ages_older_adult_boundary() -> None:
    r_ge65 = build_eligibility_row(
        _make_v2_study(
            "NCT99300703",
            "Exclusion Criteria:\n - Age >= 65 years",
            min_age="18 Years",
            max_age="N/A",
            std_ages=("ADULT", "OLDER_ADULT"),
        )
    )
    assert r_ge65["std_ages_discordance"] is True

    r_gt65 = build_eligibility_row(
        _make_v2_study(
            "NCT99300704",
            "Exclusion Criteria:\n - Age > 65 years",
            min_age="18 Years",
            max_age="N/A",
            std_ages=("ADULT", "OLDER_ADULT"),
        )
    )
    assert r_gt65["std_ages_discordance"] is False


def test_conflicting_age_bounds_respects_one_year_tolerance() -> None:
    r_within = build_eligibility_row(
        _make_v2_study("NCT99300707", "Inclusion Criteria:\n- Age 19 to 75 years", min_age="18 Years", max_age="75 Years")
    )
    assert r_within["conflicting_age_bounds"] is False

    r_conflict = build_eligibility_row(
        _make_v2_study("NCT99300708", "Inclusion Criteria:\n- Age 21 to 75 years", min_age="18 Years", max_age="75 Years")
    )
    assert r_conflict["conflicting_age_bounds"] is True


def test_defect_age_inclusion_strict_gt18_and_lt65_drops_upper_bound() -> None:
    """DEFECT: `'Age > 18 years and < 65 years'` drops `< 65 years` because `_AGE_DUAL_INEQ_RX` `op1` omits `>`."""
    raw = "Inclusion Criteria:\n - Age > 18 years and < 65 years"
    row = build_eligibility_row(_make_v2_study("NCT99300705", raw, min_age="N/A", max_age="N/A"))
    age_thresh = row["domains"]["free_text_age_cutoff"]["extracted_thresholds"]
    assert row["text_min_age_years"] == 18.0
    assert row["text_max_age_years"] == 65.0
    assert age_thresh["min_inclusive"] is False
    assert age_thresh["max_inclusive"] is False
    assert row["hidden_upper_age_cap"] is True


def test_defect_age_multiple_comparators_in_clause_overwrite_inclusivity() -> None:
    """DEFECT: `extract_clause_age_bounds` unconditionally overwrites `min_inclusive`/`max_inclusive` with non-winning comparators."""
    b_exc_hi = extract_clause_age_bounds("Age > 75 years or Age >= 80 years", "exclusion")
    assert b_exc_hi is not None
    assert b_exc_hi["max_age_years"] == 75.0
    assert b_exc_hi["max_inclusive"] is True

    b_exc_lo = extract_clause_age_bounds("Age < 18 years or Age <= 12 years", "exclusion")
    assert b_exc_lo is not None
    assert b_exc_lo["min_age_years"] == 18.0
    assert b_exc_lo["min_inclusive"] is True

    b_inc_hi = extract_clause_age_bounds("Age <= 65 years and Age < 75 years", "inclusion")
    assert b_inc_hi is not None
    assert b_inc_hi["max_age_years"] == 65.0
    assert b_inc_hi["max_inclusive"] is True


def test_defect_multi_cohort_age_inclusivity_tautology_and_global_exclusion_clobber() -> None:
    """DEFECT: `_resolve_age_domain` uses `True or cl_inc` tautology and overwrites global `Exclusion: Age >= 65` inclusivity, masking `std_ages_discordance`."""
    raw_strict = (
        "Cohort A Inclusion Criteria:\n"
        "- Age > 18 years\n"
        "Cohort B Inclusion Criteria:\n"
        "- Age > 25 years\n"
    )
    p_strict = parse_eligibility_text(raw_strict)
    assert p_strict.domains["free_text_age_cutoff"].extracted_thresholds["min_age_years"] == 18.0
    assert p_strict.domains["free_text_age_cutoff"].extracted_thresholds["min_inclusive"] is False

    raw_mc_65 = (
        "Cohort A Inclusion Criteria:\n"
        "- Age 18 to 80 years\n"
        "Cohort B Inclusion Criteria:\n"
        "- Age 18 to 85 years\n"
        "Exclusion Criteria:\n"
        "- Age >= 65 years\n"
    )
    row_mc_65 = build_eligibility_row(
        _make_v2_study("NCT99300706", raw_mc_65, std_ages=("ADULT", "OLDER_ADULT"))
    )
    assert row_mc_65["text_max_age_years"] == 65.0
    assert row_mc_65["domains"]["free_text_age_cutoff"]["extracted_thresholds"]["max_inclusive"] is False
    assert row_mc_65["std_ages_discordance"] is True


@pytest.mark.parametrize(
    ("nct_id", "clause_body"),
    [
        ("NCT99300710", "Diagnosis of type 2 diabetes for >= 2 years"),
        ("NCT99300711", "Prior malignancy within the past 5 years"),
        ("NCT99300712", "Smoking history >= 20 pack-years"),
        ("NCT99300713", "Life expectancy >= 12 weeks"),
        ("NCT99300714", "Washout >= 28 days"),
        ("NCT99300715", "Child-bearing potential within 2 years of menopause"),
    ],
)
def test_non_age_duration_and_lookback_traps_do_not_trigger_age_cutoff(
    nct_id: str, clause_body: str
) -> None:
    raw = f"Inclusion Criteria:\n - {clause_body}"
    row = build_eligibility_row(_make_v2_study(nct_id, raw, min_age="N/A", max_age="N/A"))
    assert row["free_text_age_cutoff_status"] == "not_mentioned"
    assert row["text_min_age_years"] is None
    assert row["text_max_age_years"] is None
    assert row["hidden_lower_age_bound"] is False
    assert row["hidden_upper_age_cap"] is False


def test_defect_non_age_trap_partner_aged_ge_18_years_triggers_participant_age() -> None:
    """DEFECT: `'Partner aged >= 18 years'` falsely triggers participant `free_text_age_cutoff` and `hidden_lower_age_bound`."""
    raw = "Inclusion Criteria:\n - Partner aged >= 18 years"
    row = build_eligibility_row(_make_v2_study("NCT99300716", raw, min_age="N/A", max_age="N/A"))
    assert row["free_text_age_cutoff_status"] == "not_mentioned"
    assert row["text_min_age_years"] is None
    assert row["hidden_lower_age_bound"] is False


def test_defect_non_age_traps_caregiver_donor_and_children_of_mothers() -> None:
    """DEFECT: Caregiver, donor (`donor aged`), and offspring (`children aged`) clauses falsely trigger participant age bounds."""
    for idx, body in enumerate(
        (
            "Caregiver aged >= 18 years",
            "Donor aged 18 to 60 years",
            "Mothers of children aged 2 to 5 years",
        ),
        start=717,
    ):
        row = build_eligibility_row(
            _make_v2_study("NCT" + f"{99300000 + idx}", f"Inclusion Criteria:\n - {body}", min_age="N/A", max_age="N/A")
        )
        assert row["free_text_age_cutoff_status"] == "not_mentioned", body
        assert row["hidden_lower_age_bound"] is False, body
        assert row["hidden_upper_age_cap"] is False, body


def test_defect_non_age_trap_pregnant_women_months_gestation_parsed_as_infant_age() -> None:
    """DEFECT: `'Pregnant women between 3 and 6 months gestation'` is parsed as participant age `0.25..0.5` years."""
    raw = "Inclusion Criteria:\n 1. Pregnant women between 3 and 6 months gestation"
    row = build_eligibility_row(_make_v2_study("NCT99300720", raw, min_age="18 Years", max_age="N/A", sex="FEMALE"))
    assert row["pregnancy_or_lactation_status"] == "explicitly_included_or_targeted"
    assert row["free_text_age_cutoff_status"] == "not_mentioned"
    assert row["hidden_upper_age_cap"] is False
    assert row["conflicting_age_bounds"] is False


def test_defect_non_age_trap_symptom_onset_at_age_triggers_upper_age_cap() -> None:
    """DEFECT: `'Symptom onset at age <= 40 years'` falsely triggers `max_age_years = 40.0` and `hidden_upper_age_cap`."""
    raw = "Inclusion Criteria:\n - Symptom onset at age <= 40 years"
    row = build_eligibility_row(_make_v2_study("NCT99300721", raw, min_age="18 Years", max_age="N/A"))
    assert row["free_text_age_cutoff_status"] == "not_mentioned"
    assert row["hidden_upper_age_cap"] is False


# ===========================================================================
# 5. Pinning, ReDoS (< 0.5s), Malformed Input Quarantine, JSON & Doc Parity
# ===========================================================================


@pytest.mark.parametrize(
    ("nct_id", "payload"),
    [
        ("NCT99300801", "Inclusion Criteria:\n- " + ("12345,   " * 2300)),
        ("NCT99300802", "Inclusion Criteria:\n- " + ("chronic " * 2495) + "heart failureX"),
        ("NCT99300803", "Exclusion Criteria:\n- " + ("HIV " * 4990) + "X"),
        ("NCT99300804", "Exclusion Criteria:\n- " + ("HBV " * 4990) + "X"),
        ("NCT99300805", "Inclusion Criteria:\n- " + ("able to read, and read, " * 850)),
        ("NCT99300806", "Patients with diabetes. " * 850),
    ],
)
def test_redos_20k_character_pathological_inputs_complete_under_half_second(
    nct_id: str, payload: str
) -> None:
    assert len(payload) >= 19900
    t0 = time.perf_counter()
    row = build_eligibility_row(_make_v2_study(nct_id, payload))
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.5, f"{nct_id} took {elapsed:.3f}s (>= 0.5s)"
    assert row["parse_ok"] is True


@pytest.mark.parametrize(
    ("bad_study", "expected_status"),
    [
        (None, "parse_error"),
        ("not_a_dict", "parse_error"),
        (12345, "parse_error"),
        ({"nct_id": "NCT99300810", "protocolSection": []}, "parse_error"),
        ({"nct_id": "NCT99300811", "protocolSection": {"eligibilityModule": "bad"}}, "parse_error"),
        (
            {
                "nct_id": "NCT99300812",
                "protocolSection": {"eligibilityModule": {"eligibilityCriteria": 12345}},
            },
            "parse_error",
        ),
        ({"nct_id": "NCT99300813", "eligibility_criteria": ["list", "not", "str"]}, "parse_error"),
        (
            _make_v2_study(
                "NCT99300814",
                "Inclusion Criteria:\n- Age >= 18 years",
                min_age="invalid",
                max_age="also_invalid",
            ),
            "parsed",
        ),
    ],
)
def test_malformed_study_inputs_quarantine_cleanly_without_uncaught_exceptions(
    bad_study: Any, expected_status: str
) -> None:
    row = build_eligibility_row(bad_study, snapshot_date="2026-11-15")
    assert row["eligibility_parse_status"] == expected_status
    if expected_status == "parse_error":
        assert row["parse_ok"] is False
        assert row["section_layout"] is None
        for dom in DOMAINS:
            assert row[f"{dom}_status"] is None
        for fl in DISCORDANCE_FLAGS:
            assert row[fl] is None
    encoded = json.dumps(row, allow_nan=False)
    assert isinstance(encoded, str)
    checks = structural_checks([row])
    assert all_structural_checks_pass(checks), checks


def test_defect_overflow_float_in_organ_threshold_crashes_strict_json_dumps() -> None:
    """DEFECT: 400-digit lab cutoff in `eligibilityCriteria` produces `float('inf')` and crashes `json.dumps(row, allow_nan=False)`."""
    huge_egfr = "Inclusion Criteria:\n - eGFR >= " + ("9" * 400) + " mL/min"
    row = build_eligibility_row(_make_v2_study("NCT99300820", huge_egfr))
    encoded = json.dumps(row, allow_nan=False)
    assert isinstance(encoded, str)


def test_defect_malformed_nan_or_object_minimum_age_crashes_strict_json_dumps() -> None:
    """DEFECT: Non-JSON `minimumAge` (`float('nan')` or `object()`) is echoed into `discordance.structured_min_age_raw`, crashing `json.dumps(..., allow_nan=False)`."""
    for idx, bad_min in enumerate((float("nan"), object()), start=821):
        study = _make_v2_study(
            "NCT" + f"{99300000 + idx}",
            "Inclusion Criteria:\n - Age >= 18 years",
            min_age=bad_min,
        )
        row = build_eligibility_row(study, snapshot_date="2026-11-15")
        encoded = json.dumps(row, allow_nan=False)
        assert isinstance(encoded, str)


def test_defect_lean_eligibility_row_drops_contraception_count_in_cohort_summary() -> None:
    """DEFECT: `summarize_cohort_barriers([lean_eligibility_row(row)])` silently loses `contraception_only_requirement`."""
    raw = "Inclusion Criteria:\n - Women of childbearing potential must agree to use effective contraception."
    full_row = build_eligibility_row(_make_v2_study("NCT99300830", raw))
    lean_row = lean_eligibility_row(full_row)

    full_summary = summarize_cohort_barriers([full_row])
    lean_summary = summarize_cohort_barriers([lean_row])
    assert full_summary["clinical_exception_counts"]["contraception_only_requirement"] == 1
    assert lean_summary["clinical_exception_counts"]["contraception_only_requirement"] == 1


def test_pinning_and_readme_documentation_parity() -> None:
    verify_vocabulary_pin()
    validate_rules(RULES)
    assert VOCABULARY_SHA256 == EXPECTED_VOCABULARY_SHA256
    assert compute_vocabulary_sha256() == EXPECTED_VOCABULARY_SHA256
    assert eligibility.__version__ == PARSER_MODULE_VERSION
    assert AGE_CONFLICT_TOLERANCE_YEARS == 1.0
    assert len(DOMAINS) == 12
    assert len(SECTION_KINDS) == 3
    assert len(SECTION_LAYOUT_LEVELS) == 5
    assert len(DOMAIN_STATUS_LEVELS) == 5
    assert len(DOMAIN_SUBSTATUS_LEVELS) == 2
    assert len(ELIGIBILITY_PARSE_STATUS_LEVELS) == 3
    assert len(RULE_EFFECTS) == 6

    sample_row = build_eligibility_row(
        _make_v2_study("NCT99300840", "Inclusion Criteria:\n- Age >= 18 years")
    )
    err_row = parse_error_row("NCT99300841", error="synthetic error")
    assert set(sample_row.keys()) == set(err_row.keys())
    assert set(LEAN_COLUMNS).issubset(sample_row.keys())

    sig_build = inspect.signature(build_eligibility_row)
    assert list(sig_build.parameters) == ["study", "snapshot_date"]
    sig_audit = inspect.signature(audit_structured_vs_text)
    assert list(sig_audit.parameters) == [
        "eligibility_module_or_study",
        "parsed",
        "raw_criteria_text",
    ]

    readme_path = Path(__file__).resolve().parents[2] / "eligibility" / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    for required_token in (
        *DOMAINS,
        *SECTION_LAYOUT_LEVELS,
        *DOMAIN_STATUS_LEVELS,
        *DOMAIN_SUBSTATUS_LEVELS,
        "VOCABULARY_SHA256",
        "EXPECTED_VOCABULARY_SHA256",
        "VOCABULARY_VERSION",
        "AGE_CONFLICT_TOLERANCE_YEARS",
        "build_eligibility_row",
        "lean_eligibility_row",
        "parse_error_row",
        "summarize_cohort_barriers",
        "audit_structured_vs_text",
        "hidden_upper_age_cap",
        "hidden_lower_age_bound",
        "conflicting_age_bounds",
        "std_ages_discordance",
        "sex_eligibility_discordance",
        "healthy_volunteers_discordance",
    ):
        assert required_token in readme, f"Missing {required_token!r} in eligibility/README.md"
