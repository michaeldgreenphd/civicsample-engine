"""Deterministic eligibility-barrier and structured-discordance parser for ClinicalTrials.gov.

Public package surface re-exporting section/clause tokenization, the SHA-256-pinned
12-domain rule vocabulary, polarity & exception resolution, structured-vs-text
discordance auditing, and per-study/cohort table builders.
"""
from __future__ import annotations

from eligibility.parser import (
    AGE_CONFLICT_TOLERANCE_YEARS,
    DOMAIN_STATUS_LEVELS,
    DOMAIN_SUBSTATUS_LEVELS,
    DomainAssessment,
    EvidenceSpan,
    ParsedEligibility,
    StructuredDiscordanceReport,
    audit_structured_vs_text,
    parse_eligibility_text,
)
from eligibility.sections import (
    SECTION_KINDS,
    SECTION_LAYOUT_LEVELS,
    ClauseSpan,
    SectionSpan,
    SectionedText,
    parse_eligibility_sections,
)
from eligibility.table import (
    DISCORDANCE_FLAGS,
    ELIGIBILITY_PARSE_STATUS_LEVELS,
    LEAN_COLUMNS,
    PARSER_MODULE_VERSION,
    all_structural_checks_pass,
    build_eligibility_row,
    lean_eligibility_row,
    parse_error_row,
    structural_checks,
    summarize_cohort_barriers,
)
from eligibility.vocabulary import (
    DOMAINS,
    EXPECTED_VOCABULARY_SHA256,
    MODIFIER_PATTERNS,
    RULE_EFFECTS,
    RULES,
    THRESHOLD_PATTERNS,
    VOCABULARY_SHA256,
    VOCABULARY_VERSION,
    DomainRule,
    compute_vocabulary_sha256,
    extract_clause_age_bounds,
    extract_domain_thresholds,
    parse_structured_age_years,
    validate_rules,
    verify_vocabulary_pin,
)

__version__: str = PARSER_MODULE_VERSION

__all__: list[str] = [
    "AGE_CONFLICT_TOLERANCE_YEARS",
    "ClauseSpan",
    "DISCORDANCE_FLAGS",
    "DOMAINS",
    "DOMAIN_STATUS_LEVELS",
    "DOMAIN_SUBSTATUS_LEVELS",
    "DomainAssessment",
    "DomainRule",
    "ELIGIBILITY_PARSE_STATUS_LEVELS",
    "EXPECTED_VOCABULARY_SHA256",
    "EvidenceSpan",
    "LEAN_COLUMNS",
    "MODIFIER_PATTERNS",
    "PARSER_MODULE_VERSION",
    "ParsedEligibility",
    "RULES",
    "RULE_EFFECTS",
    "SECTION_KINDS",
    "SECTION_LAYOUT_LEVELS",
    "SectionSpan",
    "SectionedText",
    "StructuredDiscordanceReport",
    "THRESHOLD_PATTERNS",
    "VOCABULARY_SHA256",
    "VOCABULARY_VERSION",
    "__version__",
    "all_structural_checks_pass",
    "audit_structured_vs_text",
    "build_eligibility_row",
    "compute_vocabulary_sha256",
    "extract_clause_age_bounds",
    "extract_domain_thresholds",
    "lean_eligibility_row",
    "parse_eligibility_sections",
    "parse_eligibility_text",
    "parse_error_row",
    "parse_structured_age_years",
    "structural_checks",
    "summarize_cohort_barriers",
    "validate_rules",
    "verify_vocabulary_pin",
]
