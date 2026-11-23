"""Polarity algebra, exception resolution, and structured-vs-text discordance auditor.

Evaluates tokenized `ClauseSpan`s from `eligibility.sections` against the pinned
12-domain rule table in `eligibility.vocabulary` to produce:
  1. Non-collapsing per-domain barrier statuses (`DOMAIN_STATUS_LEVELS`) with
     explicit substatus (`"numeric_cutoff"` vs `"exception_permitted"`) whenever
     `status == "threshold_or_conditional"`.
  2. Exact character-offset `EvidenceSpan`s verified against `raw_text` so
     `raw_text[span.start_char:span.end_char] == span.verbatim_text` always holds.
  3. `StructuredDiscordanceReport` auditing `protocolSection.eligibilityModule`
     (`minimumAge`, `maximumAge`, `stdAges`, `sex`, `healthyVolunteers`) against
     deterministic free-text eligibility constraints.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from eligibility.sections import ClauseSpan, SectionedText, parse_eligibility_sections
from eligibility.vocabulary import (
    COMPILED_MODIFIERS,
    COMPILED_RULES_BY_DOMAIN,
    DOMAINS,
    VOCABULARY_SHA256,
    VOCABULARY_VERSION,
    CompiledRule,
    extract_clause_age_bounds,
    extract_domain_thresholds,
    is_gilberts_allowance_in_clause,
    parse_structured_age_years,
    tighten_lower_bound,
    tighten_upper_bound,
    verify_vocabulary_pin,
)

DOMAIN_STATUS_LEVELS: tuple[str, ...] = (
    "strictly_excluded",
    "threshold_or_conditional",
    "explicitly_included_or_targeted",
    "ambiguous_or_unheaded",
    "not_mentioned",
)

DOMAIN_SUBSTATUS_LEVELS: tuple[str, ...] = (
    "numeric_cutoff",
    "exception_permitted",
)

AGE_CONFLICT_TOLERANCE_YEARS: float = 1.0

_CLAUSE_LEADING_BULLET_RX = re.compile(
    r"^(?:(?:\*(?!\*)|[-+•◦▪▸‣–—]|\([0-9a-zA-Z]{1,4}\)|[0-9a-zA-Z]{1,3}[.)])\s+)?"
)

_NEGATED_INCLUSION_STEM_RX = re.compile(
    r"^(?:(?:who|that|and|with)\s+)?"
    r"(?:(?:all\s+)?(?:participants?|patients?|subjects?|individuals?|adults?|women|men)\s+)?"
    r"(?:"
    r"(?:must|may|shall|should|can)\s+not\s+(?:have|be|exhibit|present\s+with)\b"
    r"|cannot\s+(?:have|be)\b"
    r"|(?:have|has)\s+no\s+(?:history\s+of|evidence\s+of|prior|previous|known|active|current|documented)?\b"
    r"|no\s+(?:history\s+of|evidence\s+of|prior|previous|known|active|current|documented|clinical|concurrent|sign\w*\s+of)?\b"
    r"|absence\s+of\b"
    r"|free\s+(?:of|from)\b"
    r"|without\b"
    r"|not\s+(?:have|having|be|being|pregnant|breast[- ]?feeding|lactating|on\s+dialysis)\b"
    r")",
    re.IGNORECASE,
)

_NO_PRIOR_THERAPY_GUARD_RX = re.compile(
    r"^(?:(?:who|that|and|with)\s+)?"
    r"(?:(?:all\s+)?(?:participants?|patients?|subjects?|individuals?|adults?|women|men)\s+)?"
    r"(?:"
    r"(?:no|without|free\s+(?:of|from)|absence\s+of)\s+(?:prior\s+|previous\s+)?"
    r"|(?:(?:must|may|shall|should|can)\s+not|cannot|have\s+not|has\s+not)\s+(?:have\s+)?(?:received|undergone|had(?:\s+prior)?)\s+"
    r")"
    r"(?:[a-z0-9/\-]+\s+){0,6}"
    r"(?:therap(?:y|ies)|treatment\w*|regimen\w*|chemotherap\w*|radiotherap\w*|radiation|irradiation|"
    r"immunotherap\w*|pharmacotherap\w*|biologic\w*|resection|surger(?:y|ies)|surgical|medication\w*|"
    r"antiretroviral\w*|antiviral\w*|antidepressant\w*|antibiotic\w*|vaccin\w*|exposure)\s+"
    r"(?:for|to\s+treat)\b",
    re.IGNORECASE,
)

_INCLUSION_SEGMENT_SPLIT_RX = re.compile(
    r"(?<=[.;])\s+"
    r"|,\s*(?=(?:with\s+|and\s+)?(?:no|without|neither|(?:must|may|shall|should|can)\s+not|cannot)\b)"
    r"|\s+(?=(?:who|that|and)\s+(?:(?:must|may|shall|should|can)\s+not|cannot|have\s+no|has\s+no)\b)"
    r"|\s+(?=with\s+no\s+(?:history|evidence|known|active|prior|current)\b)",
    re.IGNORECASE,
)

_INLINE_EXCEPTION_EXTRACT_RX = re.compile(
    r"(?:"
    r"(?:note|notes|exception|exceptions|allowance|clarification)\s*[:\-–—]\s*[^\r\n]+"
    r"|\([^()\r\n]{0,180}\b(?:except|unless|permitted|allowed|eligible|gilbert|in\s+situ|basal\s+cell|interpreter)[^()\r\n]{0,180}\)"
    r"|\b(?:except|unless)\b[^\r\n.;]{3,180}"
    r")",
    re.IGNORECASE,
)

# Sex-restriction patterns for structured-vs-text discordance auditing.
_CONDITIONAL_SEX_CLAUSE_RX = re.compile(
    r"\b(?:"
    r"if\s+(?:female|male|a\s+woman|a\s+man)"
    r"|for\s+(?:female|male|women|men)\b"
    r"|child[- ]?bearing\s+potential"
    r"|contracepti(?:on|ve)|birth\s+control|barrier\s+method|condom|sperm\s+donation"
    r"|pregnancy\s+test"
    r"|female\s+partners?\s+of\s+male"
    r"|male\s+partners?\s+of\s+female"
    r")\b",
    re.IGNORECASE,
)

_BOTH_SEXES_INCLUSION_RX = re.compile(
    r"\b(?:"
    r"male\s+(?:and|or|and/or)\s+female\s+(?:participants?|subjects?|patients?|adults?|volunteers?|individuals?)?"
    r"|female\s+(?:and|or|and/or)\s+male\s+(?:participants?|subjects?|patients?|adults?|volunteers?|individuals?)?"
    r"|men\s+(?:and|or|and/or)\s+(?:post[- ]?menopausal\s+)?women"
    r"|women\s+(?:and|or|and/or)\s+men"
    r"|both\s+males?\s+and\s+females?"
    r"|either\s+sex"
    r")\b",
    re.IGNORECASE,
)

_FEMALE_ONLY_INCLUSION_RX = re.compile(
    r"\b(?:"
    r"female\s+(?:participants?|subjects?|patients?|volunteers?|adults?|individuals?)\s+only"
    r"|(?:women|females?)\s+only"
    r"|sex\s*:\s*female\b"
    r"|must\s+be\s+(?:biologically\s+|assigned\s+)?female"
    r"|(?:participant|subject|patient)\s+is\s+female\b"
    r")\b"
    r"|^(?:(?:\*(?!\*)|[-+•◦▪–—]|\([0-9a-zA-Z]+\)|[0-9a-zA-Z]+[.)])\s+)?"
    r"(?:pre[- ]?\s*(?:or|and)\s+)?(?:post[- ]?menopausal|peri[- ]?menopausal|pregnant|breast[- ]?feeding|lactating)\s+"
    r"(?:women|females?|participants?|patients?|subjects?)\b",
    re.IGNORECASE,
)

_MALE_ONLY_INCLUSION_RX = re.compile(
    r"\b(?:"
    r"male\s+(?:participants?|subjects?|patients?|volunteers?|adults?|individuals?)\s+only"
    r"|(?:men|males?)\s+only"
    r"|sex\s*:\s*male\b"
    r"|must\s+be\s+(?:biologically\s+|assigned\s+)?male"
    r"|(?:participant|subject|patient)\s+is\s+male\b"
    r")\b",
    re.IGNORECASE,
)

_WHOLE_SEX_EXCLUSION_FEMALE_RX = re.compile(
    r"^(?:(?:\*(?!\*)|[-+•◦▪–—]|\([0-9a-zA-Z]+\)|[0-9a-zA-Z]+[.)])\s+)?"
    r"(?:female\s+(?:participants?|subjects?|patients?|individuals?|sex)|females?|women)\s*\.?$",
    re.IGNORECASE,
)

_WHOLE_SEX_EXCLUSION_MALE_RX = re.compile(
    r"^(?:(?:\*(?!\*)|[-+•◦▪–—]|\([0-9a-zA-Z]+\)|[0-9a-zA-Z]+[.)])\s+)?"
    r"(?:male\s+(?:participants?|subjects?|patients?|individuals?|sex)|males?|men)\s*\.?$",
    re.IGNORECASE,
)

_NEGATED_SEX_RESTRICTION_RX = re.compile(
    r"\b(?:not|no|neither)\s+(?:restricted|limited|confined)\s+to\b"
    r"|\b(?:regardless|irrespective)\s+of\s+(?:sex|gender)\b",
    re.IGNORECASE,
)

_HEALTHY_VOLUNTEER_INC_RX = re.compile(
    r"\b(?:"
    r"healthy\s+(?:adult\s+|male\s+|female\s+|male\s+and\s+female\s+|human\s+)?(?:volunteers?|subjects?|participants?|individuals?)"
    r"|medically\s+healthy\s+(?:volunteers?|subjects?|participants?|adults?)"
    r")\b",
    re.IGNORECASE,
)

_HEALTHY_VOLUNTEER_DIRECT_NEG_RX = re.compile(
    r"\bhealthy\s+(?:adult\s+)?(?:volunteers?|subjects?|participants?)\s+(?:are\s+)?(?:not\s+eligible|excluded|ineligible)\b",
    re.IGNORECASE,
)

_HEALTHY_VOLUNTEER_BARE_BULLET_RX = re.compile(
    r"^(?:(?:\*(?!\*)|[-+•◦▪–—]|\([0-9a-zA-Z]+\)|[0-9a-zA-Z]+[.)])\s+)?healthy\s+(?:adult\s+)?(?:volunteers?|subjects?|participants?)\s*\.?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EvidenceSpan:
    """Exact character-offset evidence record linking a domain match to `raw_text`."""

    rule_id: str
    domain: str
    section_kind: str
    start_char: int
    end_char: int
    verbatim_text: str
    extracted_thresholds: dict[str, Any]
    exception_note: str | None
    effect: str = "strictly_excluded"
    cohort_label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-serializable dictionary representation."""
        return {
            "rule_id": self.rule_id,
            "domain": self.domain,
            "section_kind": self.section_kind,
            "start_char": self.start_char,
            "end_char": self.end_char,
            "verbatim_text": self.verbatim_text,
            "extracted_thresholds": dict(self.extracted_thresholds),
            "exception_note": self.exception_note,
            "effect": self.effect,
            "cohort_label": self.cohort_label,
        }


@dataclass(frozen=True)
class DomainAssessment:
    """Resolved trial-level eligibility status for one of the 12 clinical/demographic domains."""

    domain: str
    status: str
    substatus: str | None
    extracted_thresholds: dict[str, Any]
    evidence: tuple[EvidenceSpan, ...]
    has_gilberts_exception: bool = False
    has_skin_or_insitu_exception: bool = False
    condition_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-serializable dictionary representation."""
        return {
            "domain": self.domain,
            "status": self.status,
            "substatus": self.substatus,
            "extracted_thresholds": dict(self.extracted_thresholds),
            "has_gilberts_exception": self.has_gilberts_exception,
            "has_skin_or_insitu_exception": self.has_skin_or_insitu_exception,
            "condition_type": self.condition_type,
            "evidence": [ev.to_dict() for ev in self.evidence],
        }


@dataclass(frozen=True)
class StructuredDiscordanceReport:
    """Audit comparing `protocolSection.eligibilityModule` structured fields to free text."""

    structured_min_age_raw: str | None
    structured_max_age_raw: str | None
    structured_min_age_years: float | None
    structured_max_age_years: float | None
    structured_std_ages: tuple[str, ...]
    structured_sex: str | None
    structured_healthy_volunteers: bool | None
    text_min_age_years: float | None
    text_max_age_years: float | None
    text_sex_restriction: str | None
    text_healthy_volunteers_status: str | None
    hidden_upper_age_cap: bool
    hidden_lower_age_bound: bool
    conflicting_age_bounds: bool
    std_ages_discordance: bool
    sex_eligibility_discordance: bool
    healthy_volunteers_discordance: bool
    any_discordance: bool
    discordance_reasons: tuple[str, ...]
    evidence: tuple[EvidenceSpan, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-serializable dictionary representation."""
        return {
            "structured_min_age_raw": self.structured_min_age_raw,
            "structured_max_age_raw": self.structured_max_age_raw,
            "structured_min_age_years": self.structured_min_age_years,
            "structured_max_age_years": self.structured_max_age_years,
            "structured_std_ages": list(self.structured_std_ages),
            "structured_sex": self.structured_sex,
            "structured_healthy_volunteers": self.structured_healthy_volunteers,
            "text_min_age_years": self.text_min_age_years,
            "text_max_age_years": self.text_max_age_years,
            "text_sex_restriction": self.text_sex_restriction,
            "text_healthy_volunteers_status": self.text_healthy_volunteers_status,
            "hidden_upper_age_cap": self.hidden_upper_age_cap,
            "hidden_lower_age_bound": self.hidden_lower_age_bound,
            "conflicting_age_bounds": self.conflicting_age_bounds,
            "std_ages_discordance": self.std_ages_discordance,
            "sex_eligibility_discordance": self.sex_eligibility_discordance,
            "healthy_volunteers_discordance": self.healthy_volunteers_discordance,
            "any_discordance": self.any_discordance,
            "discordance_reasons": list(self.discordance_reasons),
            "evidence": [ev.to_dict() for ev in self.evidence],
        }


@dataclass(frozen=True)
class ParsedEligibility:
    """Complete deterministic parse of a study's eligibility text and optional structured audit."""

    sectioned: SectionedText
    domains: dict[str, DomainAssessment]
    discordance: StructuredDiscordanceReport | None
    vocabulary_version: str
    vocabulary_sha256: str

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-serializable dictionary representation."""
        return {
            "section_layout": self.sectioned.section_layout,
            "placeholder_reason": self.sectioned.placeholder_reason,
            "has_unheaded_preamble": self.sectioned.has_unheaded_preamble,
            "is_multi_cohort": self.sectioned.is_multi_cohort,
            "n_sections": len(self.sectioned.sections),
            "n_clauses": len(self.sectioned.clauses),
            "vocabulary_version": self.vocabulary_version,
            "vocabulary_sha256": self.vocabulary_sha256,
            "domains": {d: a.to_dict() for d, a in self.domains.items()},
            "discordance": self.discordance.to_dict() if self.discordance is not None else None,
        }


def _make_evidence_span(
    raw_text: str,
    clause: ClauseSpan,
    rule_id: str,
    domain: str,
    effect: str,
    extracted_thresholds: dict[str, Any],
    exception_note: str | None,
) -> EvidenceSpan:
    """Construct an `EvidenceSpan` and verify the exact slice invariant against `raw_text`."""
    if raw_text[clause.start_char:clause.end_char] != clause.verbatim_text:
        raise AssertionError(
            f"EvidenceSpan invariant violated at {clause.start_char}:{clause.end_char}"
        )
    return EvidenceSpan(
        rule_id=rule_id,
        domain=domain,
        section_kind=clause.section_kind,
        start_char=clause.start_char,
        end_char=clause.end_char,
        verbatim_text=clause.verbatim_text,
        extracted_thresholds=extracted_thresholds,
        exception_note=exception_note,
        effect=effect,
        cohort_label=clause.cohort_label,
    )


def _extract_exception_note(
    raw_text: str,
    clause: ClauseSpan,
    effect: str,
    thresholds: dict[str, Any],
) -> str | None:
    """Extract attached or inline exception note when a clause permits conditional enrollment."""
    if effect not in ("exception_permitted", "contraception_only"):
        return None
    if clause.attached_exception_spans:
        parts = [raw_text[s:e] for s, e in clause.attached_exception_spans]
        return " | ".join(parts)
    m = _INLINE_EXCEPTION_EXTRACT_RX.search(clause.verbatim_text)
    if m:
        return m.group(0).strip()
    if effect == "contraception_only":
        return "contraception_required"
    if thresholds.get("has_gilberts_exception"):
        return "gilberts_syndrome_allowance"
    if thresholds.get("has_skin_or_insitu_exception"):
        return "non_melanoma_skin_or_in_situ_exception"
    if thresholds.get("accommodation"):
        return str(thresholds["accommodation"])
    return "conditional_clinical_allowance"


_POSITIVE_RESTART_RX = re.compile(
    r",?\s+\band\s+(?:documented|confirmed|histologically|measurable|must\s+(?:be|have|meet))\b",
    re.IGNORECASE,
)


def _effective_segment_section_kind(segment_text: str, base_section_kind: str) -> str:
    """Resolve effective polarity section kind for one sentence/semicolon segment."""
    if base_section_kind != "inclusion":
        return base_section_kind
    core = _CLAUSE_LEADING_BULLET_RX.sub("", segment_text.strip())
    if _NO_PRIOR_THERAPY_GUARD_RX.match(core):
        return "inclusion"
    if _NEGATED_INCLUSION_STEM_RX.match(core) and not _POSITIVE_RESTART_RX.search(core):
        return "exclusion"
    return "inclusion"


def _rule_matches_segment(crule: CompiledRule, full_clause_text: str, segment_text: str, effective_section: str) -> bool:
    """Evaluate one compiled rule against a clause segment, enforcing guards and local negation."""
    if effective_section not in crule.spec.applicable_sections:
        return False
    m = crule.regex.search(segment_text)
    if not m:
        return False
    if crule.guard_not_regex is not None and crule.guard_not_regex.search(full_clause_text):
        return False
    # Guard against local negation prefix ("no evidence of ", "must not have ") before a target_inclusion hit.
    if crule.spec.effect == "target_inclusion":
        prefix_raw = segment_text[max(0, m.start() - 65) : m.start()]
        prefix = re.split(r"[.;]", prefix_raw)[-1]
        if COMPILED_MODIFIERS["local_negation_prefix"].search(prefix):
            return False
    # Guard against Gilbert's syndrome exclusions being misclassified as a Gilbert's allowance.
    if crule.spec.rule_id == "hepatic_gilberts_allowance":
        if not is_gilberts_allowance_in_clause(full_clause_text, effective_section):
            return False
    return True


def _evaluate_clause_for_domain(
    raw_text: str,
    clause: ClauseSpan,
    domain: str,
) -> EvidenceSpan | None:
    """Match a single `ClauseSpan` against `domain` and return at most one winning `EvidenceSpan`."""
    if domain == "free_text_age_cutoff":
        bounds = extract_clause_age_bounds(clause.verbatim_text, clause.section_kind)
        if bounds is None:
            return None
        resolved = bool(bounds.get("polarity_resolved"))
        effect = "numeric_cutoff" if resolved else "domain_mention"
        clean_thresholds: dict[str, Any] = {}
        if bounds.get("min_age_years") is not None:
            clean_thresholds["min_age_years"] = bounds["min_age_years"]
            clean_thresholds["min_inclusive"] = bounds["min_inclusive"]
        if bounds.get("max_age_years") is not None:
            clean_thresholds["max_age_years"] = bounds["max_age_years"]
            clean_thresholds["max_inclusive"] = bounds["max_inclusive"]
        return _make_evidence_span(
            raw_text=raw_text,
            clause=clause,
            rule_id="age_chronological_bound",
            domain=domain,
            effect=effect,
            extracted_thresholds=clean_thresholds,
            exception_note=None,
        )

    # Evaluate whole clause first under its primary polarity; if Inclusion contains semicolon,
    # comma-negated, or relative-negated sub-segments with mixed polarity, inspect per-segment polarity.
    raw_segments = [s for s in _INCLUSION_SEGMENT_SPLIT_RX.split(clause.verbatim_text) if s.strip()]
    segments_with_polarity: list[tuple[str, str]] = [
        (clause.verbatim_text, _effective_segment_section_kind(clause.verbatim_text, clause.section_kind))
    ]
    if clause.section_kind == "inclusion" and len(raw_segments) > 1:
        seg_kinds = [_effective_segment_section_kind(seg, "inclusion") for seg in raw_segments]
        if len(set(seg_kinds)) > 1:
            segments_with_polarity = list(zip(raw_segments, seg_kinds))

    for crule in COMPILED_RULES_BY_DOMAIN[domain]:
        matched_eff_section: str | None = None
        matched_seg_text: str = clause.verbatim_text
        for seg_txt, eff_sec in segments_with_polarity:
            if _rule_matches_segment(crule, clause.verbatim_text, seg_txt, eff_sec):
                matched_eff_section = eff_sec
                matched_seg_text = seg_txt
                break
        if matched_eff_section is None:
            continue

        eff_section = matched_eff_section
        effect = crule.spec.effect
        # In unheaded text, promote a bare domain_mention when explicit exclusion/inclusion verbs are present.
        if eff_section == "unheaded" and effect == "domain_mention":
            has_exc = bool(COMPILED_MODIFIERS["unheaded_explicit_exclusion"].search(clause.verbatim_text))
            has_inc = bool(COMPILED_MODIFIERS["unheaded_explicit_inclusion"].search(clause.verbatim_text))
            has_allow = bool(COMPILED_MODIFIERS["general_exception_cue"].search(clause.verbatim_text))
            if has_exc and not has_inc:
                effect = "exception_permitted" if has_allow else "strictly_excluded"

        # If an Exclusion-polarity clause matches a blanket exclusion rule yet carries an attached or inline
        # general exception cue ("except ...", "unless ...", "not excluded if ..."), temper it.
        # Pregnancy exclusions are tempered ONLY by `preg_exception_allowance` (never by contraception notes).
        if (
            effect == "strictly_excluded"
            and eff_section == "exclusion"
            and domain != "pregnancy_or_lactation"
            and COMPILED_MODIFIERS["general_exception_cue"].search(clause.verbatim_text)
        ):
            if domain == "hepatic_function" and COMPILED_MODIFIERS["gilberts_syndrome"].search(clause.verbatim_text):
                if is_gilberts_allowance_in_clause(clause.verbatim_text, eff_section):
                    effect = "exception_permitted"
            else:
                effect = "exception_permitted"

        thresholds = extract_domain_thresholds(domain, matched_seg_text, eff_section)
        if matched_seg_text != clause.verbatim_text and not thresholds:
            thresholds = extract_domain_thresholds(domain, clause.verbatim_text, eff_section)
        exc_note = _extract_exception_note(raw_text, clause, effect, thresholds)
        return _make_evidence_span(
            raw_text=raw_text,
            clause=clause,
            rule_id=crule.spec.rule_id,
            domain=domain,
            effect=effect,
            extracted_thresholds=thresholds,
            exception_note=exc_note,
        )
    return None


def _is_flat_target_contradiction(
    domain: str,
    target_spans: list[EvidenceSpan],
    excl_spans: list[EvidenceSpan],
    is_multi_cohort: bool,
) -> bool:
    """Return True when Exclusion directly contradicts a target Inclusion in a single-cohort study."""
    if is_multi_cohort:
        return False
    inc_text = " ".join(s.verbatim_text.lower() for s in target_spans)
    exc_text = " ".join(s.verbatim_text.lower() for s in excl_spans)

    if domain == "hiv_infection":
        # Targeting HIV-1 while excluding HIV-2 or AIDS opportunistic illness is clinically consistent.
        if "hiv-1" in inc_text and "hiv-2" in exc_text and "hiv-1" not in exc_text:
            return False
        if "opportunistic" in exc_text or "aids-defining" in exc_text:
            return False
        return True

    if domain == "hepatitis_b_or_c":
        inc_hbv = bool(re.search(r"\b(?:hepatitis\s+b|hbv|hbsag)\b", inc_text))
        inc_hcv = bool(re.search(r"\b(?:hepatitis\s+c|hcv)\b", inc_text))
        exc_hbv = bool(re.search(r"\b(?:hepatitis\s+b|hbv|hbsag)\b", exc_text))
        exc_hcv = bool(re.search(r"\b(?:hepatitis\s+c|hcv)\b", exc_text))
        if (inc_hbv and exc_hcv and not exc_hbv) or (inc_hcv and exc_hbv and not exc_hcv):
            return False
        return True

    if domain == "pregnancy_or_lactation":
        # Targeting pregnancy while excluding breastfeeding (or twin/multiple gestation) is consistent.
        if "pregnan" in inc_text and ("breast" in exc_text or "lactat" in exc_text or "multiple" in exc_text) and "pregnant" not in exc_text:
            return False
        return True

    if domain == "cns_or_brain_metastases":
        # Targeting brain metastases while excluding leptomeningeal disease is standard neuro-oncology practice.
        if "leptomeningeal" in exc_text and "leptomeningeal" not in inc_text:
            return False
        return True

    if domain == "renal_function":
        # Targeting chronic CKD/ESRD/impaired GFR while excluding acute kidney injury/failure or transplant is standard.
        if any(k in exc_text for k in ("acute", "aki", "transplant", "hepatorenal")) and "acute" not in inc_text:
            return False
        return True

    if domain == "psychiatric_or_substance_use":
        # Targeting MDD or AUD while excluding active suicidality or psychosis is clinically standard.
        if any(k in exc_text for k in ("suicid", "psychosis", "schizophrenia")) and not any(
            k in inc_text for k in ("suicid", "schizophrenia")
        ):
            return False
        return True

    return True


def _resolve_age_domain(
    spans: list[EvidenceSpan],
    is_multi_cohort: bool,
) -> DomainAssessment:
    """Combine chronological age bounds across Inclusion/Exclusion clauses and cohorts."""
    if not spans:
        return DomainAssessment(
            domain="free_text_age_cutoff",
            status="not_mentioned",
            substatus=None,
            extracted_thresholds={},
            evidence=(),
        )

    resolved_spans = [s for s in spans if s.effect == "numeric_cutoff"]
    if not resolved_spans:
        return DomainAssessment(
            domain="free_text_age_cutoff",
            status="ambiguous_or_unheaded",
            substatus=None,
            extracted_thresholds={},
            evidence=tuple(spans),
        )

    # Separate cohort-specific bounds (both inclusion and cohort-scoped exclusion) from global bounds.
    cohort_groups: dict[str, list[EvidenceSpan]] = {}
    global_spans: list[EvidenceSpan] = []
    for sp in resolved_spans:
        if is_multi_cohort and sp.cohort_label:
            cohort_groups.setdefault(sp.cohort_label.lower(), []).append(sp)
        else:
            global_spans.append(sp)

    def _intersect_group(group: list[EvidenceSpan]) -> tuple[float | None, float | None, bool, bool, bool]:
        lo: float | None = None
        hi: float | None = None
        lo_inc = True
        hi_inc = True
        inc_ranges: list[tuple[float, float]] = []
        for gsp in group:
            t = gsp.extracted_thresholds
            mn = t.get("min_age_years")
            mx = t.get("max_age_years")
            if gsp.section_kind == "inclusion" and mn is not None and mx is not None:
                inc_ranges.append((float(mn), float(mx)))
            if mn is not None:
                mn_f = float(mn)
                if lo is None or mn_f > lo:
                    lo = mn_f
                    lo_inc = bool(t.get("min_inclusive", True))
                elif mn_f == lo:
                    lo_inc = lo_inc and bool(t.get("min_inclusive", True))
            if mx is not None:
                mx_f = float(mx)
                if hi is None or mx_f < hi:
                    hi = mx_f
                    hi_inc = bool(t.get("max_inclusive", True))
                elif mx_f == hi:
                    hi_inc = hi_inc and bool(t.get("max_inclusive", True))

        # Check disjoint ranges in the same cohort.
        if len(inc_ranges) >= 2:
            max_lo = max(r[0] for r in inc_ranges)
            min_hi = min(r[1] for r in inc_ranges)
            if max_lo > min_hi:
                return lo, hi, lo_inc, hi_inc, True
        if lo is not None and hi is not None:
            if lo > hi or (lo == hi and not (lo_inc and hi_inc)):
                return lo, hi, lo_inc, hi_inc, True
        return lo, hi, lo_inc, hi_inc, False

    g_lo, g_hi, g_lo_inc, g_hi_inc, g_conflict = _intersect_group(global_spans)
    if g_conflict:
        return DomainAssessment(
            domain="free_text_age_cutoff",
            status="ambiguous_or_unheaded",
            substatus=None,
            extracted_thresholds={},
            evidence=tuple(spans),
        )

    if cohort_groups:
        c_min_pairs: list[tuple[float, bool]] = []
        c_max_pairs: list[tuple[float, bool]] = []
        all_have_min = True
        all_have_max = True
        for c_spans in cohort_groups.values():
            clo, chi, cl_inc, ch_inc, c_conf = _intersect_group(c_spans)
            if c_conf:
                return DomainAssessment(
                    domain="free_text_age_cutoff",
                    status="ambiguous_or_unheaded",
                    substatus=None,
                    extracted_thresholds={},
                    evidence=tuple(spans),
                )
            if clo is not None:
                c_min_pairs.append((clo, cl_inc))
            else:
                all_have_min = False
            if chi is not None:
                c_max_pairs.append((chi, ch_inc))
            else:
                all_have_max = False

        union_lo: float | None = None
        union_lo_inc = True
        if c_min_pairs and all_have_min:
            union_lo = min(p[0] for p in c_min_pairs)
            union_lo_inc = any(p[1] for p in c_min_pairs if p[0] == union_lo)

        union_hi: float | None = None
        union_hi_inc = True
        if c_max_pairs and all_have_max:
            union_hi = max(p[0] for p in c_max_pairs)
            union_hi_inc = any(p[1] for p in c_max_pairs if p[0] == union_hi)

        if union_lo is not None and g_lo is not None:
            final_lo_v, final_lo_inc = tighten_lower_bound(g_lo, g_lo_inc, union_lo, union_lo_inc)
            final_lo: float | None = final_lo_v
        elif union_lo is not None:
            final_lo, final_lo_inc = union_lo, union_lo_inc
        else:
            final_lo, final_lo_inc = g_lo, g_lo_inc

        if union_hi is not None and g_hi is not None:
            final_hi_v, final_hi_inc = tighten_upper_bound(g_hi, g_hi_inc, union_hi, union_hi_inc)
            final_hi: float | None = final_hi_v
        elif union_hi is not None:
            final_hi, final_hi_inc = union_hi, union_hi_inc
        else:
            final_hi, final_hi_inc = g_hi, g_hi_inc
    else:
        final_lo, final_hi, final_lo_inc, final_hi_inc = g_lo, g_hi, g_lo_inc, g_hi_inc

    if final_lo is not None and final_hi is not None:
        if final_lo > final_hi or (final_lo == final_hi and not (final_lo_inc and final_hi_inc)):
            return DomainAssessment(
                domain="free_text_age_cutoff",
                status="ambiguous_or_unheaded",
                substatus=None,
                extracted_thresholds={},
                evidence=tuple(spans),
            )

    merged: dict[str, Any] = {}
    if final_lo is not None:
        merged["min_age_years"] = final_lo
        merged["min_inclusive"] = final_lo_inc
    if final_hi is not None:
        merged["max_age_years"] = final_hi
        merged["max_inclusive"] = final_hi_inc

    return DomainAssessment(
        domain="free_text_age_cutoff",
        status="threshold_or_conditional",
        substatus="numeric_cutoff",
        extracted_thresholds=merged,
        evidence=tuple(spans),
    )


def _infer_condition_type(
    domain: str,
    effects: set[str],
    merged_thresholds: dict[str, Any],
) -> str | None:
    if "contraception_only" in effects and "strictly_excluded" not in effects and "exception_permitted" not in effects:
        return "contraception_required"
    if domain == "hepatic_function" and merged_thresholds.get("has_gilberts_exception"):
        return "gilberts_syndrome"
    if domain == "prior_malignancy" and merged_thresholds.get("has_skin_or_insitu_exception"):
        return "skin_or_insitu_cancer"
    if domain == "language_or_literacy" and merged_thresholds.get("accommodation"):
        return str(merged_thresholds["accommodation"])
    if domain == "hiv_infection" and "exception_permitted" in effects:
        return "controlled_hiv"
    if domain == "hepatitis_b_or_c" and "exception_permitted" in effects:
        return "cleared_or_suppressed_hepatitis"
    if domain == "cns_or_brain_metastases" and "exception_permitted" in effects:
        return "treated_stable_cns_metastases"
    if "exception_permitted" in effects:
        return "clinical_exception"
    return None


def _resolve_domain_assessment(
    domain: str,
    spans: list[EvidenceSpan],
    is_multi_cohort: bool,
) -> DomainAssessment:
    """Resolve all matched `EvidenceSpan`s for `domain` into one non-collapsing `DomainAssessment`."""
    if domain == "free_text_age_cutoff":
        return _resolve_age_domain(spans, is_multi_cohort=is_multi_cohort)

    if not spans:
        return DomainAssessment(
            domain=domain,
            status="not_mentioned",
            substatus=None,
            extracted_thresholds={},
            evidence=(),
        )

    merged_thresholds: dict[str, Any] = {}
    for sp in spans:
        for k, v in sp.extracted_thresholds.items():
            if k == "languages" and k in merged_thresholds:
                existing = list(merged_thresholds[k])
                for lang in v:
                    if lang not in existing:
                        existing.append(lang)
                merged_thresholds[k] = existing
            else:
                merged_thresholds[k] = v

    has_gilberts = bool(merged_thresholds.get("has_gilberts_exception", False))
    has_skin_insitu = bool(merged_thresholds.get("has_skin_or_insitu_exception", False))
    effects = {sp.effect for sp in spans}
    cond_type = _infer_condition_type(domain, effects, merged_thresholds)

    target_spans = [sp for sp in spans if sp.effect == "target_inclusion"]
    excl_spans = [sp for sp in spans if sp.effect == "strictly_excluded"]

    if target_spans:
        if excl_spans and _is_flat_target_contradiction(domain, target_spans, excl_spans, is_multi_cohort):
            return DomainAssessment(
                domain=domain,
                status="ambiguous_or_unheaded",
                substatus=None,
                extracted_thresholds=merged_thresholds,
                evidence=tuple(spans),
                has_gilberts_exception=has_gilberts,
                has_skin_or_insitu_exception=has_skin_insitu,
                condition_type=None,
            )
        return DomainAssessment(
            domain=domain,
            status="explicitly_included_or_targeted",
            substatus=None,
            extracted_thresholds=merged_thresholds,
            evidence=tuple(spans),
            has_gilberts_exception=has_gilberts,
            has_skin_or_insitu_exception=has_skin_insitu,
            condition_type=None,
        )

    if "exception_permitted" in effects:
        return DomainAssessment(
            domain=domain,
            status="threshold_or_conditional",
            substatus="exception_permitted",
            extracted_thresholds=merged_thresholds,
            evidence=tuple(spans),
            has_gilberts_exception=has_gilberts,
            has_skin_or_insitu_exception=has_skin_insitu,
            condition_type=cond_type,
        )

    if "numeric_cutoff" in effects:
        return DomainAssessment(
            domain=domain,
            status="threshold_or_conditional",
            substatus="numeric_cutoff",
            extracted_thresholds=merged_thresholds,
            evidence=tuple(spans),
            has_gilberts_exception=has_gilberts,
            has_skin_or_insitu_exception=has_skin_insitu,
            condition_type=None,
        )

    if "strictly_excluded" in effects:
        return DomainAssessment(
            domain=domain,
            status="strictly_excluded",
            substatus=None,
            extracted_thresholds=merged_thresholds,
            evidence=tuple(spans),
            has_gilberts_exception=has_gilberts,
            has_skin_or_insitu_exception=has_skin_insitu,
            condition_type=None,
        )

    if "contraception_only" in effects:
        merged_thresholds["condition_type"] = "contraception_required"
        return DomainAssessment(
            domain=domain,
            status="threshold_or_conditional",
            substatus="exception_permitted",
            extracted_thresholds=merged_thresholds,
            evidence=tuple(spans),
            has_gilberts_exception=has_gilberts,
            has_skin_or_insitu_exception=has_skin_insitu,
            condition_type="contraception_required",
        )

    return DomainAssessment(
        domain=domain,
        status="ambiguous_or_unheaded",
        substatus=None,
        extracted_thresholds=merged_thresholds,
        evidence=tuple(spans),
        has_gilberts_exception=has_gilberts,
        has_skin_or_insitu_exception=has_skin_insitu,
        condition_type=None,
    )


def _normalize_std_ages(raw_std: Any) -> tuple[str, ...]:
    if raw_std is None:
        return ()
    if isinstance(raw_std, (list, tuple)):
        return tuple(str(x).strip().upper() for x in raw_std if x is not None and str(x).strip())
    return ()


def _clean_raw_age_field(val: Any) -> str | None:
    return val if isinstance(val, str) else None


def _extract_structured_fields(
    eligibility_module_or_study: dict[str, Any] | None,
) -> dict[str, Any]:
    """Extract normalized structured eligibility fields from either an API v2 study, extracted study, or module."""
    if not isinstance(eligibility_module_or_study, dict):
        return {
            "min_age_raw": None,
            "max_age_raw": None,
            "std_ages": (),
            "sex": None,
            "healthy_volunteers": None,
            "criteria_text": None,
        }

    if "protocolSection" in eligibility_module_or_study:
        protocol = eligibility_module_or_study.get("protocolSection")
        if not isinstance(protocol, dict):
            raise TypeError("protocolSection must be a dict")
        mod = protocol.get("eligibilityModule")
        if mod is not None and not isinstance(mod, dict):
            raise TypeError("eligibilityModule must be a dict or None")
        mod = mod or {}
        return {
            "min_age_raw": _clean_raw_age_field(mod.get("minimumAge")),
            "max_age_raw": _clean_raw_age_field(mod.get("maximumAge")),
            "std_ages": _normalize_std_ages(mod.get("stdAges")),
            "sex": str(mod["sex"]).strip().upper() if isinstance(mod.get("sex"), str) else None,
            "healthy_volunteers": mod.get("healthyVolunteers") if isinstance(mod.get("healthyVolunteers"), bool) else None,
            "criteria_text": mod.get("eligibilityCriteria"),
        }

    # Check extracted CivicSample record layout (`min_age`, `max_age`, `std_ages`, `eligibility.sex`).
    if any(k in eligibility_module_or_study for k in ("min_age", "max_age", "std_ages", "eligibility")):
        elig_sub = eligibility_module_or_study.get("eligibility")
        sex_val = None
        if isinstance(elig_sub, dict) and isinstance(elig_sub.get("sex"), str):
            sex_val = elig_sub["sex"].strip().upper()
        elif isinstance(eligibility_module_or_study.get("sex"), str):
            sex_val = eligibility_module_or_study["sex"].strip().upper()
        hv = eligibility_module_or_study.get("healthy_volunteers")
        return {
            "min_age_raw": _clean_raw_age_field(eligibility_module_or_study.get("min_age")),
            "max_age_raw": _clean_raw_age_field(eligibility_module_or_study.get("max_age")),
            "std_ages": _normalize_std_ages(eligibility_module_or_study.get("std_ages")),
            "sex": sex_val,
            "healthy_volunteers": hv if isinstance(hv, bool) else None,
            "criteria_text": eligibility_module_or_study.get("eligibility_criteria"),
        }

    # Direct `eligibilityModule` dict.
    mod = eligibility_module_or_study
    return {
        "min_age_raw": _clean_raw_age_field(mod.get("minimumAge")),
        "max_age_raw": _clean_raw_age_field(mod.get("maximumAge")),
        "std_ages": _normalize_std_ages(mod.get("stdAges")),
        "sex": str(mod["sex"]).strip().upper() if isinstance(mod.get("sex"), str) else None,
        "healthy_volunteers": mod.get("healthyVolunteers") if isinstance(mod.get("healthyVolunteers"), bool) else None,
        "criteria_text": mod.get("eligibilityCriteria"),
    }


def _detect_text_sex_restriction(parsed: ParsedEligibility) -> tuple[str | None, list[EvidenceSpan]]:
    """Detect deterministic participant-sex restriction or explicit both-sexes inclusion in free text."""
    raw_text = parsed.sectioned.raw_text
    female_only_spans: list[EvidenceSpan] = []
    male_only_spans: list[EvidenceSpan] = []
    both_sexes_spans: list[EvidenceSpan] = []

    for cl in parsed.sectioned.clauses:
        txt = cl.verbatim_text
        if cl.section_kind == "inclusion":
            if _BOTH_SEXES_INCLUSION_RX.search(txt):
                both_sexes_spans.append(
                    _make_evidence_span(
                        raw_text=raw_text,
                        clause=cl,
                        rule_id="discordance_both_sexes_inclusion",
                        domain="sex_eligibility",
                        effect="target_inclusion",
                        extracted_thresholds={"text_sex_restriction": "BOTH_SEXES"},
                        exception_note=None,
                    )
                )
                continue
            if _CONDITIONAL_SEX_CLAUSE_RX.search(txt) or _NEGATED_SEX_RESTRICTION_RX.search(txt):
                continue
            if _FEMALE_ONLY_INCLUSION_RX.search(txt) and not re.search(r"\b(?:males?|men)\b", txt, re.IGNORECASE):
                female_only_spans.append(
                    _make_evidence_span(
                        raw_text=raw_text,
                        clause=cl,
                        rule_id="discordance_female_only_inclusion",
                        domain="sex_eligibility",
                        effect="target_inclusion",
                        extracted_thresholds={"text_sex_restriction": "FEMALE_ONLY"},
                        exception_note=None,
                    )
                )
            elif _MALE_ONLY_INCLUSION_RX.search(txt) and not re.search(r"\b(?:females?|women)\b", txt, re.IGNORECASE):
                male_only_spans.append(
                    _make_evidence_span(
                        raw_text=raw_text,
                        clause=cl,
                        rule_id="discordance_male_only_inclusion",
                        domain="sex_eligibility",
                        effect="target_inclusion",
                        extracted_thresholds={"text_sex_restriction": "MALE_ONLY"},
                        exception_note=None,
                    )
                )
        elif cl.section_kind == "exclusion":
            if _WHOLE_SEX_EXCLUSION_MALE_RX.match(txt.strip()):
                female_only_spans.append(
                    _make_evidence_span(
                        raw_text=raw_text,
                        clause=cl,
                        rule_id="discordance_male_excluded",
                        domain="sex_eligibility",
                        effect="strictly_excluded",
                        extracted_thresholds={"text_sex_restriction": "FEMALE_ONLY"},
                        exception_note=None,
                    )
                )
            elif _WHOLE_SEX_EXCLUSION_FEMALE_RX.match(txt.strip()):
                male_only_spans.append(
                    _make_evidence_span(
                        raw_text=raw_text,
                        clause=cl,
                        rule_id="discordance_female_excluded",
                        domain="sex_eligibility",
                        effect="strictly_excluded",
                        extracted_thresholds={"text_sex_restriction": "MALE_ONLY"},
                        exception_note=None,
                    )
                )

    # Also incorporate positive obstetric/maternal targeting when no male cohort is co-enrolled.
    preg_dom = parsed.domains["pregnancy_or_lactation"]
    if (
        preg_dom.status == "explicitly_included_or_targeted"
        and not female_only_spans
        and not both_sexes_spans
        and not male_only_spans
    ):
        female_only_spans.extend(preg_dom.evidence)

    if both_sexes_spans:
        return "BOTH_SEXES", both_sexes_spans
    if female_only_spans and male_only_spans:
        if parsed.sectioned.is_multi_cohort:
            return "BOTH_SEXES", female_only_spans + male_only_spans
        return None, []
    if female_only_spans:
        return "FEMALE_ONLY", female_only_spans
    if male_only_spans:
        return "MALE_ONLY", male_only_spans
    return None, []


def _detect_text_healthy_volunteers(parsed: ParsedEligibility) -> tuple[str | None, list[EvidenceSpan]]:
    """Detect explicit healthy-volunteer inclusion or exclusion in free text."""
    raw_text = parsed.sectioned.raw_text
    inc_hv: list[EvidenceSpan] = []
    exc_hv: list[EvidenceSpan] = []
    for cl in parsed.sectioned.clauses:
        txt = cl.verbatim_text
        if cl.section_kind == "inclusion":
            if _HEALTHY_VOLUNTEER_DIRECT_NEG_RX.search(txt):
                exc_hv.append(
                    _make_evidence_span(
                        raw_text=raw_text,
                        clause=cl,
                        rule_id="discordance_healthy_volunteers_excluded",
                        domain="healthy_volunteers",
                        effect="strictly_excluded",
                        extracted_thresholds={},
                        exception_note=None,
                    )
                )
            elif _HEALTHY_VOLUNTEER_INC_RX.search(txt) and not re.search(
                r"\b(?:no|not|excluded|ineligible)\b", txt, re.IGNORECASE
            ):
                inc_hv.append(
                    _make_evidence_span(
                        raw_text=raw_text,
                        clause=cl,
                        rule_id="discordance_healthy_volunteers_included",
                        domain="healthy_volunteers",
                        effect="target_inclusion",
                        extracted_thresholds={},
                        exception_note=None,
                    )
                )
        elif cl.section_kind == "exclusion" and (
            _HEALTHY_VOLUNTEER_DIRECT_NEG_RX.search(txt) or _HEALTHY_VOLUNTEER_BARE_BULLET_RX.search(txt)
        ):
            exc_hv.append(
                _make_evidence_span(
                    raw_text=raw_text,
                    clause=cl,
                    rule_id="discordance_healthy_volunteers_excluded",
                    domain="healthy_volunteers",
                    effect="strictly_excluded",
                    extracted_thresholds={},
                    exception_note=None,
                )
            )

    if exc_hv and not inc_hv:
        return "PATIENTS_ONLY_NO_HEALTHY", exc_hv
    if inc_hv and not exc_hv:
        return "HEALTHY_INCLUDED", inc_hv
    return None, []


def audit_structured_vs_text(
    eligibility_module_or_study: dict[str, Any] | None,
    parsed: ParsedEligibility | None = None,
    raw_criteria_text: str | None = None,
) -> StructuredDiscordanceReport:
    """Audit structured `eligibilityModule` metadata against deterministic free-text criteria."""
    sf = _extract_structured_fields(eligibility_module_or_study)
    criteria_str = raw_criteria_text if raw_criteria_text is not None else sf["criteria_text"]
    if parsed is None:
        parsed = parse_eligibility_text(criteria_str)

    try:
        s_min_years = parse_structured_age_years(sf["min_age_raw"])
    except ValueError:
        s_min_years = None
    try:
        s_max_years = parse_structured_age_years(sf["max_age_raw"])
    except ValueError:
        s_max_years = None

    age_dom = parsed.domains["free_text_age_cutoff"]
    t_min_years: float | None = None
    t_max_years: float | None = None
    t_max_inc: bool = True
    if age_dom.status == "threshold_or_conditional":
        raw_t_min = age_dom.extracted_thresholds.get("min_age_years")
        raw_t_max = age_dom.extracted_thresholds.get("max_age_years")
        t_min_years = float(raw_t_min) if raw_t_min is not None else None
        t_max_years = float(raw_t_max) if raw_t_max is not None else None
        t_max_inc = bool(age_dom.extracted_thresholds.get("max_inclusive", True))

    reasons: list[str] = []
    evidence: list[EvidenceSpan] = []

    hidden_upper = s_max_years is None and t_max_years is not None
    if hidden_upper:
        reasons.append("hidden_upper_age_cap")
        for ev in age_dom.evidence:
            if ev.extracted_thresholds.get("max_age_years") is not None and ev not in evidence:
                evidence.append(ev)

    hidden_lower = s_min_years is None and t_min_years is not None and t_min_years > 0.0
    if hidden_lower:
        reasons.append("hidden_lower_age_bound")
        for ev in age_dom.evidence:
            if ev.extracted_thresholds.get("min_age_years") is not None and ev not in evidence:
                evidence.append(ev)

    conflicting_min = (
        s_min_years is not None
        and t_min_years is not None
        and abs(s_min_years - t_min_years) > AGE_CONFLICT_TOLERANCE_YEARS
    )
    conflicting_max = (
        s_max_years is not None
        and t_max_years is not None
        and abs(s_max_years - t_max_years) > AGE_CONFLICT_TOLERANCE_YEARS
    )
    conflicting_age = conflicting_min or conflicting_max
    if conflicting_min:
        reasons.append("conflicting_min_age")
    if conflicting_max:
        reasons.append("conflicting_max_age")
    if conflicting_age:
        for ev in age_dom.evidence:
            if ev not in evidence:
                evidence.append(ev)

    std_ages: tuple[str, ...] = sf["std_ages"]
    std_discordance = False
    if "OLDER_ADULT" in std_ages and t_max_years is not None:
        if t_max_years < 65.0 or (t_max_years == 65.0 and not t_max_inc):
            std_discordance = True
            reasons.append("std_ages_older_adult_excluded_by_text")
    if "CHILD" in std_ages and t_min_years is not None and t_min_years >= 18.0:
        std_discordance = True
        reasons.append("std_ages_child_excluded_by_text")
    if std_discordance:
        for ev in age_dom.evidence:
            if ev not in evidence:
                evidence.append(ev)

    s_sex: str | None = sf["sex"]
    text_sex_res, sex_spans = _detect_text_sex_restriction(parsed)
    sex_discordance = False
    if s_sex == "ALL" and text_sex_res in ("FEMALE_ONLY", "MALE_ONLY"):
        sex_discordance = True
        reasons.append(f"structured_sex_all_vs_{text_sex_res.lower()}")
    elif s_sex == "FEMALE" and text_sex_res in ("MALE_ONLY", "BOTH_SEXES"):
        sex_discordance = True
        reasons.append(f"structured_sex_female_vs_{text_sex_res.lower()}")
    elif s_sex == "MALE" and text_sex_res in ("FEMALE_ONLY", "BOTH_SEXES"):
        sex_discordance = True
        reasons.append(f"structured_sex_male_vs_{text_sex_res.lower()}")
    if sex_discordance:
        for ev in sex_spans:
            if ev not in evidence:
                evidence.append(ev)

    s_hv: bool | None = sf["healthy_volunteers"]
    text_hv_status, hv_spans = _detect_text_healthy_volunteers(parsed)
    hv_discordance = False
    if s_hv is True and text_hv_status == "PATIENTS_ONLY_NO_HEALTHY":
        hv_discordance = True
        reasons.append("structured_healthy_volunteers_true_vs_text_excluded")
    elif s_hv is False and text_hv_status == "HEALTHY_INCLUDED":
        hv_discordance = True
        reasons.append("structured_healthy_volunteers_false_vs_text_included")
    if hv_discordance:
        for ev in hv_spans:
            if ev not in evidence:
                evidence.append(ev)

    # Final invariant verification across all discordance evidence spans.
    raw_text = parsed.sectioned.raw_text
    for ev in evidence:
        if raw_text[ev.start_char:ev.end_char] != ev.verbatim_text:
            raise AssertionError("Discordance EvidenceSpan slice invariant violated")

    any_disc = (
        hidden_upper
        or hidden_lower
        or conflicting_age
        or std_discordance
        or sex_discordance
        or hv_discordance
    )

    return StructuredDiscordanceReport(
        structured_min_age_raw=sf["min_age_raw"],
        structured_max_age_raw=sf["max_age_raw"],
        structured_min_age_years=s_min_years,
        structured_max_age_years=s_max_years,
        structured_std_ages=std_ages,
        structured_sex=s_sex,
        structured_healthy_volunteers=s_hv,
        text_min_age_years=t_min_years,
        text_max_age_years=t_max_years,
        text_sex_restriction=text_sex_res,
        text_healthy_volunteers_status=text_hv_status,
        hidden_upper_age_cap=hidden_upper,
        hidden_lower_age_bound=hidden_lower,
        conflicting_age_bounds=conflicting_age,
        std_ages_discordance=std_discordance,
        sex_eligibility_discordance=sex_discordance,
        healthy_volunteers_discordance=hv_discordance,
        any_discordance=any_disc,
        discordance_reasons=tuple(reasons),
        evidence=tuple(evidence),
    )


def parse_eligibility_text(
    raw_text: str | None,
    eligibility_module_or_study: dict[str, Any] | None = None,
) -> ParsedEligibility:
    """Parse `eligibilityCriteria` free text across all 12 domains (and audit structured fields if supplied)."""
    verify_vocabulary_pin()
    sectioned = parse_eligibility_sections(raw_text)

    if sectioned.section_layout == "empty_or_placeholder":
        empty_domains = {
            dom: DomainAssessment(
                domain=dom,
                status="not_mentioned",
                substatus=None,
                extracted_thresholds={},
                evidence=(),
            )
            for dom in DOMAINS
        }
        prelim = ParsedEligibility(
            sectioned=sectioned,
            domains=empty_domains,
            discordance=None,
            vocabulary_version=VOCABULARY_VERSION,
            vocabulary_sha256=VOCABULARY_SHA256,
        )
        if eligibility_module_or_study is not None:
            disc = audit_structured_vs_text(
                eligibility_module_or_study=eligibility_module_or_study,
                parsed=prelim,
                raw_criteria_text=raw_text,
            )
            return ParsedEligibility(
                sectioned=sectioned,
                domains=empty_domains,
                discordance=disc,
                vocabulary_version=VOCABULARY_VERSION,
                vocabulary_sha256=VOCABULARY_SHA256,
            )
        return prelim

    spans_by_domain: dict[str, list[EvidenceSpan]] = {dom: [] for dom in DOMAINS}
    for clause in sectioned.clauses:
        for dom in DOMAINS:
            ev = _evaluate_clause_for_domain(sectioned.raw_text, clause, dom)
            if ev is not None:
                spans_by_domain[dom].append(ev)

    assessments: dict[str, DomainAssessment] = {
        dom: _resolve_domain_assessment(dom, spans_by_domain[dom], is_multi_cohort=sectioned.is_multi_cohort)
        for dom in DOMAINS
    }

    prelim_parsed = ParsedEligibility(
        sectioned=sectioned,
        domains=assessments,
        discordance=None,
        vocabulary_version=VOCABULARY_VERSION,
        vocabulary_sha256=VOCABULARY_SHA256,
    )

    if eligibility_module_or_study is not None:
        disc_report = audit_structured_vs_text(
            eligibility_module_or_study=eligibility_module_or_study,
            parsed=prelim_parsed,
            raw_criteria_text=raw_text,
        )
        return ParsedEligibility(
            sectioned=sectioned,
            domains=assessments,
            discordance=disc_report,
            vocabulary_version=VOCABULARY_VERSION,
            vocabulary_sha256=VOCABULARY_SHA256,
        )

    return prelim_parsed
