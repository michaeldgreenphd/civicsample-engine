"""
age/parser.py

Deterministic baseline chronological age parser for ClinicalTrials.gov API v2
study records. Pure Python standard library only.

WHAT THIS MODULE DOES, IN ORDER
  1. measure_kind(title) & select_age_measures(measures)
       Classifies baseline measure titles into primary chronological participant
       baseline age ("participant_baseline_age"), four explicit excluded age
       kinds ("gestational_or_developmental_age", "disease_history_age",
       "non_participant_role_age", "subgroup_stratified_age"), or "not_age".
       English words containing "age" as a substring ("Dosage", "Disease Stage",
       "Coverage", "Percentage", "Hemorrhage", "Cartilage", "Drainage",
       "Language", "Storage", "Average", "Damage", "Leakage") and anthropometric
       indices ("Weight-for-Age", "Height-for-Age", "BMI-for-Age") map to
       "not_age". Excluded age kinds are recorded in audit trails and never
       mixed into whole-cohort participant baseline age.
  2. select_overall_group(groups_or_study)
       Selects the whole-population BG group following src/utils.py's logic:
       picks an explicit whole-population group ("Total", "Overall",
       "All Participants", etc.) when multiple groups exist; selects the single
       group when only one group exists; sums disjoint study arms ONLY when no
       Total group exists and group titles represent non-overlapping study arms
       (setting flag_summed_arms_no_total = True); refuses and sets
       flag_ambiguous_groups = True when groups could overlap or double count.
  3. parse_interval_label(label)
       Parses registrant categorical age brackets into half-open chronological
       year intervals [lo_years, hi_years) (hi_years = None for open top bands)
       using exact census/trials.py unit factors (years = 1, months = 1/12,
       weeks = 1/52, days = 1/365, hours = 1/8760, minutes = 1/525600), with
       explicit bound_basis provenance. Pairwise interval auditing flags
       overlaps and interior gaps; overlapping non-zero brackets are classified
       as uninformative ("overlapping_age_brackets") so nobody is counted twice.
  4. parse_continuous_measure(measure, ...)
       Extracts central tendency (MEAN, MEDIAN, GEOMETRIC_MEAN,
       LEAST_SQUARES_MEAN) and dispersion (STANDARD_DEVIATION, STANDARD_ERROR,
       INTER_QUARTILE_RANGE, FULL_RANGE, CONFIDENCE_INTERVAL_95) and normalizes
       to years ONLY when unitOfMeasure is an unambiguous chronological time
       unit. Missing, non-age, or conflicting mixed units set
       flag_unrecognized_or_mixed_unit = True without assuming years.
  5. classify_age_reporting(parsed)
       Seven non-collapsing reporting states:
       ("reported_both", "reported_categorical_only",
        "reported_continuous_only", "explicit_unknown_only", "uninformative",
        "not_reported", "parse_error").

RULE PROVENANCE & HASH PINNING
  VOCABULARY_SHA256 is computed deterministically over _CANONICAL_VOCABULARY_SPEC
  and asserted equal to EXPECTED_VOCABULARY_SHA256 at import time so rule edits
  cannot land silently without updating PARSER_RULES_VERSION and tests.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional, Sequence, Union

__version__: str = "1.1.0"
PARSER_RULES_VERSION: str = "age_parser@2026-11-23 / v1.1.0"

# ---------------------------------------------------------------------------
# 1. Canonical rule & vocabulary specification (hashed at import time)
# ---------------------------------------------------------------------------

UNIT_YEARS: dict[str, float] = {
    "year": 1.0,
    "month": 1.0 / 12.0,
    "week": 1.0 / 52.0,
    "day": 1.0 / 365.0,
    "hour": 1.0 / 8760.0,
    "minute": 1.0 / 525600.0,
}

UNIT_ALIASES: dict[str, str] = {
    "year": "year",
    "years": "year",
    "yr": "year",
    "yrs": "year",
    "y": "year",
    "yo": "year",
    "y.o": "year",
    "y.o.": "year",
    "year old": "year",
    "years old": "year",
    "years of age": "year",
    "year of age": "year",
    "age in years": "year",
    "month": "month",
    "months": "month",
    "mo": "month",
    "mos": "month",
    "mth": "month",
    "mths": "month",
    "month old": "month",
    "months old": "month",
    "months of age": "month",
    "age in months": "month",
    "week": "week",
    "weeks": "week",
    "wk": "week",
    "wks": "week",
    "week old": "week",
    "weeks old": "week",
    "weeks of age": "week",
    "age in weeks": "week",
    "day": "day",
    "days": "day",
    "d": "day",
    "day old": "day",
    "days old": "day",
    "days of age": "day",
    "age in days": "day",
    "hour": "hour",
    "hours": "hour",
    "hr": "hour",
    "hrs": "hour",
    "h": "hour",
    "hour old": "hour",
    "hours old": "hour",
    "hours of age": "hour",
    "age in hours": "hour",
    "minute": "minute",
    "minutes": "minute",
    "min": "minute",
    "mins": "minute",
    "minutes of age": "minute",
    "age in minutes": "minute",
}

# Continuous unitOfMeasure requires unambiguous chronological unit tokens
# (single letters like "d", "h", "y" or bare "min" are intentionally omitted
# from continuous unitOfMeasure acceptance unless unambiguous, except standard
# abbreviations yr/yrs/mo/mos/wk/wks/hr/hrs).
CONTINUOUS_UNIT_ALIASES: dict[str, str] = {
    "year": "year",
    "years": "year",
    "yr": "year",
    "yrs": "year",
    "years old": "year",
    "years of age": "year",
    "age in years": "year",
    "month": "month",
    "months": "month",
    "mo": "month",
    "mos": "month",
    "mth": "month",
    "mths": "month",
    "months old": "month",
    "months of age": "month",
    "age in months": "month",
    "week": "week",
    "weeks": "week",
    "wk": "week",
    "wks": "week",
    "weeks old": "week",
    "weeks of age": "week",
    "age in weeks": "week",
    "day": "day",
    "days": "day",
    "days old": "day",
    "days of age": "day",
    "age in days": "day",
    "hour": "hour",
    "hours": "hour",
    "hr": "hour",
    "hrs": "hour",
    "hours old": "hour",
    "hours of age": "hour",
    "age in hours": "hour",
    "minute": "minute",
    "minutes": "minute",
    "minutes of age": "minute",
    "age in minutes": "minute",
}

MEASURE_KINDS: tuple[str, ...] = (
    "participant_baseline_age",
    "gestational_or_developmental_age",
    "disease_history_age",
    "non_participant_role_age",
    "subgroup_stratified_age",
    "not_age",
)

EXCLUDED_AGE_KINDS: tuple[str, ...] = (
    "gestational_or_developmental_age",
    "disease_history_age",
    "non_participant_role_age",
    "subgroup_stratified_age",
)

AGE_REPORT_STATUS_LEVELS: tuple[str, ...] = (
    "reported_both",
    "reported_categorical_only",
    "reported_continuous_only",
    "explicit_unknown_only",
    "uninformative",
    "not_reported",
    "parse_error",
)

CENTRAL_TENDENCY_TYPES: tuple[str, ...] = (
    "MEAN",
    "MEDIAN",
    "GEOMETRIC_MEAN",
    "LEAST_SQUARES_MEAN",
)

DISPERSION_TYPES: tuple[str, ...] = (
    "STANDARD_DEVIATION",
    "STANDARD_ERROR",
    "INTER_QUARTILE_RANGE",
    "FULL_RANGE",
    "CONFIDENCE_INTERVAL_95",
)

WHOLE_POPULATION_GROUP_TITLES: tuple[str, ...] = (
    "total",
    "overall",
    "all participants",
    "all subjects",
    "all patients",
    "total participants",
    "total subjects",
    "total patients",
    "entire study population",
    "overall population",
    "overall study population",
    "overall study",
)

# Regex patterns (ordered; order is part of the rule and hashed below).
_AGE_WORD_PATTERN: str = r"\b(?:age|ages|aged)\b"

_NON_AGE_PHRASE_PATTERNS: tuple[str, ...] = (
    r"\b(?:weight|height|length|bmi|body\s+mass\s+index|circumference|stature)[- ]for[- ]age\b",
    r"\b(?:z[- ]?score|percentile|percentiles|standard\s+deviation\s+score|sds)\s+(?:for|by)\s+age\b",
    r"\bage[- ](?:adjusted|standardi[sz]ed|normali[sz]ed|matched|specific|appropriate|related)\b",
    r"\b(?:adjusted|standardi[sz]ed|normali[sz]ed|indexed)\s+(?:for|by)\s+age\b",
    r"\b(?:childbearing|reproductive|school|voting|drinking|retirement|working)[- ]age\b",
    r"\bof\s+(?:childbearing|reproductive)\s+age\b",
)

_OPT_UNIT_INTERPOSED: str = (
    r"(?:(?:\s*\([^()]{1,30}\))|(?:\s*\[[^\[\]]{1,30}\])|"
    r"(?:\s+in\s+(?:years?|yrs?|months?|mos?|weeks?|wks?|days?|hours?|hrs?)))?"
)

_GESTATIONAL_DEV_PATTERNS: tuple[str, ...] = (
    r"\b(?:gestational|post[- ]?menstrual|post[- ]?concept(?:ual|ional)|conceptional|corrected|"
    r"prematurity[- ]adjusted|bone|skeletal|dental|mental|developmental|biological|epigenetic|"
    r"fetal|embryonic)\s+ages?\b",
    r"\bages?" + _OPT_UNIT_INTERPOSED + r"\s*[,:\-(]?\s*(?:gestational|post[- ]?menstrual|"
    r"post[- ]?concept(?:ual|ional)|corrected|bone|skeletal|dental|mental|developmental|"
    r"biological|epigenetic)\b",
    r"^adjusted\s+ages?\b",
)

_NON_PARTICIPANT_ROLE_PATTERNS: tuple[str, ...] = (
    r"\b(?:maternal|mother(?:'s|s'?)?|paternal|father(?:'s|s'?)?|parent(?:al|'s|s'?)?|"
    r"caregiver(?:'s|s'?)?|carer(?:'s|s'?)?|guardian(?:'s|s'?)?|donor(?:'s|s'?)?|"
    r"partner(?:'s|s'?)?|spouse(?:'s|s'?)?|clinician(?:'s|s'?)?|provider(?:'s|s'?)?|"
    r"physician(?:'s|s'?)?|investigator(?:'s|s'?)?|surgeon(?:'s|s'?)?|nurse(?:'s|s'?)?|"
    r"therapist(?:'s|s'?)?|household\s+member(?:'s|s'?)?)\s+ages?\b",
    r"\bages?" + _OPT_UNIT_INTERPOSED + r"\s+of\s+(?:the\s+)?(?:mother|mothers|father|fathers|"
    r"parent|parents|caregiver|caregivers|carer|carers|guardian|guardians|donor|donors|"
    r"partner|partners|spouse|spouses|clinician|clinicians|provider|providers|"
    r"physician|physicians)\b",
    r"^ages?" + _OPT_UNIT_INTERPOSED + r"\s*[,:\-(]\s*(?:maternal|paternal|mothers?|fathers?|"
    r"parents?|caregivers?|carers?|guardians?|donors?|partners?|spouses?|providers?|"
    r"clinicians?|physicians?)\b",
)

_DISEASE_HISTORY_PATTERNS: tuple[str, ...] = (
    r"\bages?" + _OPT_UNIT_INTERPOSED + r"\s+(?:at|of)\s+(?:the\s+)?(?:time\s+of\s+)?"
    r"(?:initial\s+|first\s+|primary\s+|prior\s+|disease\s+|symptom\s+|clinical\s+|confirmed\s+)?"
    r"(?:diagnosis|onset|screening|screen|menarche|menopause|symptom|symptoms|"
    r"presentation|manifestation|episode|relapse|recurrence|progression|remission|"
    r"transplant|transplantation|implantation|surgery|operation|resection|biopsy|"
    r"injury|trauma|burn|stroke|infarction|infection|seroconversion|vaccination|"
    r"immunization|immunisation|exposure|initiation|cessation|abstinence|"
    r"smoking|drinking|hospitali[sz]ation|admission|discharge|death|mortality|"
    r"conception|pregnancy|delivery|birth\s+of\s+first|hiv\b|cancer\b|diabetes\b|"
    r"seizure|seizures|asthma|migraine|arthritis)",
    r"^(?:diagnosis|onset|screening|menarche|menopause|transplant|surgery|injury)\s+ages?\b",
    r"^ages?" + _OPT_UNIT_INTERPOSED + r"\s*[,:\-(]\s*(?:at\s+)?(?:diagnosis|onset|screening|menarche|menopause)\b",
)

_SUBGROUP_STRATIFIED_PATTERNS: tuple[str, ...] = (
    r"\bages?\b.*(?:\bfemales?\b|\bmales?\b|\bwom[ae]n\b|\bm[ae]n\b|\bgirls?\b|\bboys?\b)",
    r"^(?:female|male|women|men|girl|boy)s?(?:'s)?\s+ages?\b",
    r"\bages?\s+by\s+(?:sex|gender|race|ethnicity|subgroup|stratum|arm|cohort|stage)\b",
    r"^ages?\s*[,:\-(]+\s*(?:subgroup|sub-group|white|black|african\s+american|asian|"
    r"hispanic|non-hispanic|responder|responders|non-responder|non-responders)\b",
)

_PRIMARY_BASELINE_AGE_PATTERNS: tuple[str, ...] = (
    r"^ages?$",
    r"^ages?\s*(?:[,:;\/\-–(\[]|\bin\b|\bof\b|\bat\b)\s*(?:in\s+)?"
    r"(?:continuous|categorical|customi[sz]ed|group|groups|grouping|category|categories|"
    r"class|classes|bracket|brackets|band|bands|stratum|strata|range|ranges|"
    r"distribution|breakdown|summary|mean|median|geometric\s+mean|least\s+squares\s+mean|"
    r"sd|standard\s+deviation|se|standard\s+error|iqr|interquartile|"
    r"years?|months?|weeks?|days?|hours?|yrs?|mos?|wks?|"
    r"participants?|subjects?|patients?|enrolled|baseline|study\s+entry|entry|"
    r"enrollment|enrolment|randomi[sz]ation|informed\s+consent|consent|"
    r"under\b|over\b|between\b|[<>=\u2264\u2265\d]).*$",
    r"^(?:baseline|(?:participant|subject|patient)(?:'s|s'?)?|"
    r"chronological|enrolled|study|overall|recipient|demographic|demographics)\s*"
    r"(?:[-:,]\s*)?ages?\b.*$",
    r"^ages?\s+(?:group|groups|grouping|category|categories|class|classes|bracket|brackets|"
    r"band|bands|stratum|strata|range|ranges|distribution|breakdown|summary|"
    r"mean|median)\b.*$",
)

_AMBIGUOUS_GROUP_PATTERNS: tuple[str, ...] = (
    r"\bpooled\b",
    r"\bcombined\b",
    r"\btotal\b",
    r"\bsub[- ]?total\b",
    r"\boverall\b",
    r"\ball\s+(?:treated|doses|arms|cohorts|groups)\b",
    r"\bany\s+dose\b",
    r"\bboth\s+(?:arms|groups|cohorts)\b",
    r"\b(?:arm|group|cohort)\s*[a-z0-9]+\s*(?:\+|and|&)\s*(?:arm|group|cohort)?\s*[a-z0-9]+\b",
    r"\b(?:safety|efficacy|itt|intent[- ]to[- ]treat|intention[- ]to[- ]treat|per[- ]protocol|"
    r"full\s+analysis|fas|completer|completers|evaluable)\s*(?:population|set|analysis|cohort)?\b",
    r"\b(?:rollover|extension|open[- ]label\s+extension|ole|re[- ]?randomi[sz]ed)\b",
)

_NONPARTICIPANT_UNIT_PATTERNS: tuple[str, ...] = (
    r"\beyes?\b",
    r"\blesions?\b",
    r"\bcycles?\b",
    r"\bbiops(?:y|ies)\b",
    r"\binfusions?\b",
    r"\b(?:tooth|teeth)\b",
    r"\bknees?\b",
    r"\bhips?\b",
    r"\bjoints?\b",
    r"\blimbs?\b",
    r"\bears?\b",
    r"\btumou?rs?\b",
    r"\bvessels?\b",
    r"\barter(?:y|ies)\b",
    r"\bveins?\b",
    r"\bgrafts?\b",
    r"\bimplants?\b",
    r"\bcatheters?\b",
    r"\bstents?\b",
    r"\bdoses?\b",
    r"\binjections?\b",
    r"\bevents?\b",
    r"\bepisodes?\b",
    r"\bspecimens?\b",
    r"\bsamples?\b",
    r"\bsites?\b",
    r"\bclinics?\b",
    r"\bhospitals?\b",
    r"\bpractices?\b",
    r"\bclusters?\b",
)

_PERCENT_PATTERNS: tuple[str, ...] = (
    r"%",
    r"\bpercent(?:age|ages)?\b",
)

_CAT_STRUCTURAL_PATTERNS: tuple[str, ...] = (
    r"^(?:total|overall|all|all\s+participants|all\s+subjects|all\s+patients|"
    r"all\s+ages|any\s+age|number\s+of\s+participants|participants|subjects|patients|"
    r"count|n|total\s+participants)$",
)

_CAT_UNKNOWN_PATTERNS: tuple[str, ...] = (
    r"\bunknown\b",
    r"\bnot\s+reported\b",
    r"\bunreported\b",
    r"\bnot\s+available\b",
    r"\bunavailable\b",
    r"\bnot\s+collected\b",
    r"\bnot\s+specified\b",
    r"\bunspecified\b",
    r"\bnot\s+provided\b",
    r"\bnot\s+recorded\b",
    r"\bnot\s+stated\b",
    r"\bnot\s+known\b",
    r"\bno\s+data\b",
    r"\bmissing\b",
    r"\bundisclosed\b",
    r"\bnot\s+disclosed\b",
    r"\bprefer(?:s|red)?\s+not\b",
    r"\bdeclin(?:e|ed)\b",
    r"\brefus(?:e|ed)\b",
)

UNINFORMATIVE_REASON_PRIORITY: tuple[str, ...] = (
    "overlapping_age_brackets",
    "incomplete_categorical_brackets",
    "inconsistent_categorical_total",
    "negative_category_count",
    "percentage_exceeds_100",
    "percentage_sum_exceeds_100",
    "percentage_without_denominator",
    "non_participant_units",
    "unrecognized_or_mixed_unit",
    "impossible_continuous_value",
    "ambiguous_groups",
    "ambiguous_multiclass_table",
    "ambiguous_multicategory_class",
    "ambiguous_multicategory_continuous",
    "arms_not_poolable_for_continuous",
    "partial_unmapped_categories",
    "no_mapped_age_brackets",
    "no_usable_continuous_stat",
    "all_values_zero",
    "all_values_na",
    "no_measurements",
    "other",
)

_CANONICAL_VOCABULARY_SPEC: dict[str, Any] = {
    "parser_rules_version": PARSER_RULES_VERSION,
    "unit_years": [[k, repr(v)] for k, v in UNIT_YEARS.items()],
    "unit_aliases": list(UNIT_ALIASES.items()),
    "continuous_unit_aliases": list(CONTINUOUS_UNIT_ALIASES.items()),
    "measure_kinds": list(MEASURE_KINDS),
    "excluded_age_kinds": list(EXCLUDED_AGE_KINDS),
    "age_report_status_levels": list(AGE_REPORT_STATUS_LEVELS),
    "central_tendency_types": list(CENTRAL_TENDENCY_TYPES),
    "dispersion_types": list(DISPERSION_TYPES),
    "whole_population_group_titles": list(WHOLE_POPULATION_GROUP_TITLES),
    "uninformative_reason_priority": list(UNINFORMATIVE_REASON_PRIORITY),
    "age_word_pattern": _AGE_WORD_PATTERN,
    "non_age_phrase_patterns": list(_NON_AGE_PHRASE_PATTERNS),
    "gestational_dev_patterns": list(_GESTATIONAL_DEV_PATTERNS),
    "non_participant_role_patterns": list(_NON_PARTICIPANT_ROLE_PATTERNS),
    "disease_history_patterns": list(_DISEASE_HISTORY_PATTERNS),
    "subgroup_stratified_patterns": list(_SUBGROUP_STRATIFIED_PATTERNS),
    "primary_baseline_age_patterns": list(_PRIMARY_BASELINE_AGE_PATTERNS),
    "ambiguous_group_patterns": list(_AMBIGUOUS_GROUP_PATTERNS),
    "nonparticipant_unit_patterns": list(_NONPARTICIPANT_UNIT_PATTERNS),
    "percent_patterns": list(_PERCENT_PATTERNS),
    "cat_structural_patterns": list(_CAT_STRUCTURAL_PATTERNS),
    "cat_unknown_patterns": list(_CAT_UNKNOWN_PATTERNS),
    "spelled_comparator_normalization": "v1",
    "prs_between_flanking_rule": "v1",
}


def compute_vocabulary_sha256() -> str:
    """Deterministic SHA-256 over the canonical parser rule and regex specification."""
    payload = json.dumps(
        _CANONICAL_VOCABULARY_SPEC,
        sort_keys=False,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


VOCABULARY_SHA256: str = compute_vocabulary_sha256()
EXPECTED_VOCABULARY_SHA256: str = (
    "2a041535333497c35750a3a45e5e34908d1ceddf603a2a2503c6c531fda2fd5e"
)


def validate_vocabulary_pin() -> None:
    """Verify VOCABULARY_SHA256 matches EXPECTED_VOCABULARY_SHA256."""
    actual = compute_vocabulary_sha256()
    if actual != EXPECTED_VOCABULARY_SHA256:
        raise AssertionError(
            f"age/parser.py vocabulary SHA-256 drift: got {actual!r}, "
            f"expected {EXPECTED_VOCABULARY_SHA256!r}. Bump PARSER_RULES_VERSION "
            f"and update EXPECTED_VOCABULARY_SHA256 deliberately."
        )


validate_vocabulary_pin()


# Compiled regular expressions
_WS_RX = re.compile(r"\s+")
_FOOTNOTE_RX = re.compile(r"[*\u2020\u2021]+$")
_AGE_WORD_RX = re.compile(_AGE_WORD_PATTERN, re.IGNORECASE)
_NON_AGE_PHRASE_RX = re.compile("|".join(_NON_AGE_PHRASE_PATTERNS), re.IGNORECASE)
_GESTATIONAL_DEV_RX = re.compile("|".join(_GESTATIONAL_DEV_PATTERNS), re.IGNORECASE)
_NON_PARTICIPANT_ROLE_RX = re.compile("|".join(_NON_PARTICIPANT_ROLE_PATTERNS), re.IGNORECASE)
_DISEASE_HISTORY_RX = re.compile("|".join(_DISEASE_HISTORY_PATTERNS), re.IGNORECASE)
_SUBGROUP_STRATIFIED_RX = re.compile("|".join(_SUBGROUP_STRATIFIED_PATTERNS), re.IGNORECASE)
_PRIMARY_BASELINE_AGE_RX = re.compile("|".join(_PRIMARY_BASELINE_AGE_PATTERNS), re.IGNORECASE)
_AMBIGUOUS_GROUP_RX = re.compile("|".join(_AMBIGUOUS_GROUP_PATTERNS), re.IGNORECASE)
_NONPARTICIPANT_UNIT_RX = re.compile("|".join(_NONPARTICIPANT_UNIT_PATTERNS), re.IGNORECASE)
_PERCENT_RX = re.compile("|".join(_PERCENT_PATTERNS), re.IGNORECASE)
_CAT_STRUCTURAL_RX = re.compile("|".join(_CAT_STRUCTURAL_PATTERNS), re.IGNORECASE)
_CAT_UNKNOWN_RX = re.compile("|".join(_CAT_UNKNOWN_PATTERNS), re.IGNORECASE)

_PARTICIPANT_DENOM_UNITS: frozenset[str] = frozenset(
    {"participants", "participant", "subjects", "subject", "patients", "patient", "individuals"}
)

_OVERLAP_TOL_YEARS: float = 1e-9


# ---------------------------------------------------------------------------
# 2. Measure selection & classification
# ---------------------------------------------------------------------------


def _norm_text(text: Any) -> str:
    if text is None:
        return ""
    if isinstance(text, float) and math.isnan(text):
        return ""
    s = str(text).strip()
    s = _WS_RX.sub(" ", s)
    s = _FOOTNOTE_RX.sub("", s)
    return s.strip()


def measure_kind(title: Optional[str]) -> str:
    """
    Classify a baseline measure title into one of MEASURE_KINDS:
      - "participant_baseline_age"
      - "gestational_or_developmental_age"
      - "disease_history_age"
      - "non_participant_role_age"
      - "subgroup_stratified_age"
      - "not_age"
    """
    s = _norm_text(title)
    if not s:
        return "not_age"
    # Word-boundary guard rejects Dosage, Disease Stage, Coverage, Percentage,
    # Hemorrhage, Cartilage, Drainage, Language, Storage, Average, Damage, Leakage.
    if not _AGE_WORD_RX.search(s):
        return "not_age"
    # Anthropometric / age-adjusted index guard (e.g. Weight-for-Age).
    if _NON_AGE_PHRASE_RX.search(s):
        return "not_age"
    # Explicit excluded age kinds (evaluation order is deterministic).
    if _GESTATIONAL_DEV_RX.search(s):
        return "gestational_or_developmental_age"
    if _NON_PARTICIPANT_ROLE_RX.search(s):
        return "non_participant_role_age"
    if _DISEASE_HISTORY_RX.search(s):
        return "disease_history_age"
    if _SUBGROUP_STRATIFIED_RX.search(s):
        return "subgroup_stratified_age"
    if _PRIMARY_BASELINE_AGE_RX.search(s):
        return "participant_baseline_age"
    return "not_age"


def select_age_measures(measures: Optional[Iterable[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Return baseline measures representing participant chronological baseline age, in source order."""
    out: list[dict[str, Any]] = []
    for m in measures or []:
        if not isinstance(m, dict):
            continue
        if measure_kind(m.get("title")) == "participant_baseline_age":
            out.append(m)
    return out


def select_excluded_age_measures(
    measures: Optional[Iterable[dict[str, Any]]],
) -> list[tuple[dict[str, Any], str]]:
    """Return (measure, kind) pairs for non-primary / non-participant / non-baseline age measures."""
    out: list[tuple[dict[str, Any], str]] = []
    for m in measures or []:
        if not isinstance(m, dict):
            continue
        kind = measure_kind(m.get("title"))
        if kind in EXCLUDED_AGE_KINDS:
            out.append((m, kind))
    return out


# ---------------------------------------------------------------------------
# 3. Whole-population BG group selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupSelection:
    strategy: str
    selected_group_ids: tuple[str, ...]
    overall_group_id: Optional[str] = None
    flag_summed_arms_no_total: bool = False
    flag_ambiguous_groups: bool = False
    reason: Optional[str] = None


def _group_id_of(group: dict[str, Any]) -> Optional[str]:
    gid = group.get("id") or group.get("groupId")
    if gid is None:
        return None
    s = str(gid).strip()
    return s if s else None


def select_overall_group(
    groups_or_study: Union[Sequence[dict[str, Any]], dict[str, Any], None],
) -> GroupSelection:
    """
    Select whole-population baseline group(s) from baselineCharacteristicsModule.groups
    (or a full study dict).

    Rules:
      1. 0 groups -> strategy="no_groups".
      2. 1 group  -> strategy="single_group", overall_group_id=that group's id.
      3. >1 groups:
         a) If last group's title starts with "total" (case-insensitive) -> pick it
            (mirrors src/utils.py get_overall_group_id).
         b) Else if exactly one group has a title in WHOLE_POPULATION_GROUP_TITLES
            (or starts with "total") -> pick first whole-population group.
         c) Else if no whole-population group exists:
            Check whether all groups have non-empty, distinct titles with zero
            pooling/analysis-set/rollover overlap markers. If disjoint study arms,
            return strategy="sum_disjoint_arms", flag_summed_arms_no_total=True.
            Otherwise refuse and return strategy="ambiguous_groups",
            flag_ambiguous_groups=True.
    """
    groups_raw: Any = groups_or_study
    if isinstance(groups_or_study, dict):
        if "resultsSection" in groups_or_study:
            groups_raw = (
                (groups_or_study.get("resultsSection") or {})
                .get("baselineCharacteristicsModule") or {}
            ).get("groups")
        elif "groups" in groups_or_study:
            groups_raw = groups_or_study.get("groups")
        else:
            groups_raw = None

    if not isinstance(groups_raw, Sequence) or isinstance(groups_raw, (str, bytes)):
        return GroupSelection(strategy="no_groups", selected_group_ids=(), reason="no_groups_metadata")

    groups = [g for g in groups_raw if isinstance(g, dict)]
    if not groups:
        return GroupSelection(strategy="no_groups", selected_group_ids=(), reason="empty_groups_list")

    if len(groups) == 1:
        gid = _group_id_of(groups[0])
        if gid is None:
            return GroupSelection(
                strategy="ambiguous_groups",
                selected_group_ids=(),
                flag_ambiguous_groups=True,
                reason="missing_group_id",
            )
        return GroupSelection(
            strategy="single_group",
            selected_group_ids=(gid,),
            overall_group_id=gid,
            reason="single_baseline_group",
        )

    # Multiple groups: reject duplicate groupIds across baseline groups immediately.
    present_gids = [_group_id_of(g) for g in groups if _group_id_of(g) is not None]
    if len(set(present_gids)) != len(present_gids):
        return GroupSelection(
            strategy="ambiguous_groups",
            selected_group_ids=(),
            flag_ambiguous_groups=True,
            reason="duplicate_group_ids",
        )

    whole_matches: list[str] = []
    for g in groups:
        tl = _norm_text(g.get("title")).lower()
        gid = _group_id_of(g)
        if gid is not None and tl in WHOLE_POPULATION_GROUP_TITLES:
            whole_matches.append(gid)
    if len(whole_matches) > 1:
        return GroupSelection(
            strategy="ambiguous_groups",
            selected_group_ids=(),
            flag_ambiguous_groups=True,
            reason="multiple_whole_population_groups",
        )

    # Check explicit whole-population group first (src/utils.py convention).
    last_title = _norm_text(groups[-1].get("title")).lower()
    last_gid = _group_id_of(groups[-1])
    if last_title.startswith("total") and last_gid is not None:
        return GroupSelection(
            strategy="explicit_total",
            selected_group_ids=(last_gid,),
            overall_group_id=last_gid,
            reason="last_group_starts_with_total",
        )

    if len(whole_matches) == 1:
        return GroupSelection(
            strategy="explicit_total",
            selected_group_ids=(whole_matches[0],),
            overall_group_id=whole_matches[0],
            reason="matched_whole_population_title",
        )

    total_prefix_matches = [
        _group_id_of(g)
        for g in groups
        if _norm_text(g.get("title")).lower().startswith("total") and _group_id_of(g) is not None
    ]
    if len(total_prefix_matches) == 1 and total_prefix_matches[0] is not None:
        gid0 = total_prefix_matches[0]
        return GroupSelection(
            strategy="explicit_total",
            selected_group_ids=(gid0,),
            overall_group_id=gid0,
            reason="single_group_starts_with_total",
        )

    # No explicit Total/Overall group exists. Decide whether groups can be safely
    # combined as disjoint study arms.
    gids: list[str] = []
    norm_titles: list[str] = []
    for g in groups:
        gid = _group_id_of(g)
        tl = _norm_text(g.get("title")).lower()
        if gid is None or not tl:
            return GroupSelection(
                strategy="ambiguous_groups",
                selected_group_ids=(),
                flag_ambiguous_groups=True,
                reason="missing_group_id_or_title",
            )
        if _AMBIGUOUS_GROUP_RX.search(tl):
            return GroupSelection(
                strategy="ambiguous_groups",
                selected_group_ids=(),
                flag_ambiguous_groups=True,
                reason="overlapping_or_pooled_group_title",
            )
        gids.append(gid)
        norm_titles.append(tl)

    if len(set(norm_titles)) != len(norm_titles) or len(set(gids)) != len(gids):
        return GroupSelection(
            strategy="ambiguous_groups",
            selected_group_ids=(),
            flag_ambiguous_groups=True,
            reason="duplicate_group_titles_or_ids",
        )

    return GroupSelection(
        strategy="sum_disjoint_arms",
        selected_group_ids=tuple(gids),
        overall_group_id=None,
        flag_summed_arms_no_total=True,
        flag_ambiguous_groups=False,
        reason="disjoint_study_arms_without_total",
    )


def _infer_group_selection_from_measure(measure: dict[str, Any]) -> GroupSelection:
    """Fallback when baselineCharacteristicsModule.groups metadata was not supplied."""
    seen_gids: list[str] = []
    for d in measure.get("denoms") or []:
        if not isinstance(d, dict):
            continue
        for c in d.get("counts") or []:
            if isinstance(c, dict) and c.get("groupId"):
                gid = str(c["groupId"]).strip()
                if gid and gid not in seen_gids:
                    seen_gids.append(gid)
    for cl in measure.get("classes") or []:
        if not isinstance(cl, dict):
            continue
        for cat in cl.get("categories") or []:
            if not isinstance(cat, dict):
                continue
            for mm in cat.get("measurements") or []:
                if isinstance(mm, dict) and mm.get("groupId"):
                    gid = str(mm["groupId"]).strip()
                    if gid and gid not in seen_gids:
                        seen_gids.append(gid)

    if not seen_gids:
        return GroupSelection(strategy="single_group", selected_group_ids=(), reason="unindexed_single_measurement")
    if len(seen_gids) == 1:
        return GroupSelection(
            strategy="single_group",
            selected_group_ids=(seen_gids[0],),
            overall_group_id=seen_gids[0],
            reason="single_group_id_in_measure",
        )

    # With >= 3 groupIds and no group titles, check whether the last groupId equals
    # the exact sum of preceding groupIds in participant denoms or across categories.
    if len(seen_gids) >= 3:
        last_gid = seen_gids[-1]
        other_gids = seen_gids[:-1]
        denom_map = _participant_denom_by_group(measure.get("denoms"), None)
        if last_gid in denom_map and all(g in denom_map for g in other_gids):
            if abs(denom_map[last_gid] - sum(denom_map[g] for g in other_gids)) < 1e-6:
                return GroupSelection(
                    strategy="explicit_total",
                    selected_group_ids=(last_gid,),
                    overall_group_id=last_gid,
                    reason="inferred_last_total_by_denom_sumcheck",
                )

        # Check across category rows if every non-empty row satisfies last == sum(others).
        row_checks: list[bool] = []
        for cl in measure.get("classes") or []:
            if not isinstance(cl, dict):
                continue
            for cat in cl.get("categories") or []:
                if not isinstance(cat, dict):
                    continue
                by_gid: dict[str, float] = {}
                for mm in cat.get("measurements") or []:
                    if isinstance(mm, dict) and mm.get("groupId"):
                        val = _parse_float(mm.get("value"))
                        if val is not None:
                            by_gid[str(mm["groupId"]).strip()] = val
                if last_gid in by_gid and all(g in by_gid for g in other_gids):
                    row_checks.append(abs(by_gid[last_gid] - sum(by_gid[g] for g in other_gids)) < 1e-6)
        if row_checks and all(row_checks):
            return GroupSelection(
                strategy="explicit_total",
                selected_group_ids=(last_gid,),
                overall_group_id=last_gid,
                reason="inferred_last_total_by_row_sumcheck",
            )

    return GroupSelection(
        strategy="ambiguous_groups",
        selected_group_ids=(),
        flag_ambiguous_groups=True,
        reason="multiple_group_ids_without_group_titles_or_sumcheck",
    )


# ---------------------------------------------------------------------------
# 4. Numeric & denominator helpers
# ---------------------------------------------------------------------------


_THOUSANDS_COMMA_RX = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_STRICT_THOUSANDS_NUM_RX = re.compile(r"^[+-]?[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?$")
_STRICT_THOUSANDS_INT_RX = re.compile(r"^[+-]?[1-9]\d{0,2}(?:,\d{3})+$")


def _strip_thousands_commas(s: str) -> str:
    return _THOUSANDS_COMMA_RX.sub("", s)


def _parse_float(val: Any) -> Optional[float]:
    """Parse finite float from API string/number (stripping strict thousands commas); None for NA/NaN/Inf."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return f if math.isfinite(f) else None
    s = str(val).strip()
    if not s or s.upper() in ("NA", "N/A", "NONE", "NULL", "NAN", "-", "--", "NR", "NE"):
        return None
    if "," in s:
        if not _STRICT_THOUSANDS_NUM_RX.match(s):
            return None
        s = s.replace(",", "")
    try:
        f = float(s)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _parse_raw_float_keeping_nonfinite(val: Any) -> tuple[Optional[float], bool]:
    """Return (value, is_nonfinite_token) so NaN/Inf tokens in continuous stats are explicitly flagged."""
    if val is None or isinstance(val, bool):
        return None, False
    if isinstance(val, (int, float)):
        f = float(val)
        if not math.isfinite(f):
            return None, True
        return f, False
    s = str(val).strip()
    if not s or s.upper() in ("NA", "N/A", "NONE", "NULL", "-", "--", "NR", "NE"):
        return None, False
    if s.lower() in ("nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"):
        return None, True
    if "," in s:
        if not _STRICT_THOUSANDS_NUM_RX.match(s):
            return None, False
        s = s.replace(",", "")
    try:
        f = float(s)
    except ValueError:
        return None, False
    if not math.isfinite(f):
        return None, True
    return f, False


def _participant_denom_by_group(
    measure_denoms: Optional[Iterable[dict[str, Any]]],
    module_denoms: Optional[Iterable[dict[str, Any]]],
) -> dict[str, float]:
    """Map groupId -> positive participant denominator count, preferring measure-level denoms."""
    for source in (measure_denoms, module_denoms):
        if not source:
            continue
        for d in source:
            if not isinstance(d, dict):
                continue
            units = _norm_text(d.get("units")).lower()
            if units not in _PARTICIPANT_DENOM_UNITS:
                continue
            out: dict[str, float] = {}
            dup_conflict = False
            for c in d.get("counts") or []:
                if not isinstance(c, dict):
                    continue
                gid = str(c.get("groupId") or "").strip()
                val = _parse_float(c.get("value"))
                if val is not None and val > 0:
                    key = gid if gid else "__single__"
                    if key in out and abs(out[key] - val) > 1e-6:
                        dup_conflict = True
                        break
                    out[key] = val
            if out and not dup_conflict:
                return out
    return {}


def is_percentage_measure(measure: dict[str, Any]) -> bool:
    """True if paramType or unitOfMeasure marks values as percentages."""
    pt = _norm_text(measure.get("paramType")).upper()
    um = _norm_text(measure.get("unitOfMeasure"))
    if pt in ("PERCENTAGE", "PERCENT"):
        return True
    return bool(um and _PERCENT_RX.search(um))


def is_nonparticipant_unit_measure(measure: dict[str, Any]) -> bool:
    """True if paramType is COUNT_OF_UNITS or unitOfMeasure names non-participant anatomical/cluster units."""
    pt = _norm_text(measure.get("paramType")).upper()
    if pt == "COUNT_OF_UNITS":
        return True
    um = _norm_text(measure.get("unitOfMeasure"))
    if um and _NONPARTICIPANT_UNIT_RX.search(um):
        return True
    return False


# ---------------------------------------------------------------------------
# 5. Categorical age bracket grammar & pairwise interval audit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedInterval:
    raw_label: str
    kind: str  # "interval" | "unknown" | "structural" | "unmapped"
    lo_years: Optional[float] = None
    hi_years: Optional[float] = None
    bound_basis: Optional[str] = None
    unit_inferred: bool = False


@dataclass(frozen=True)
class ParsedBracket:
    label: str
    lo_years: float
    hi_years: Optional[float]
    count: float
    bound_basis: str
    unit_inferred: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "lo_years": self.lo_years,
            "hi_years": self.hi_years,
            "count": self.count,
            "bound_basis": self.bound_basis,
            "unit_inferred": self.unit_inferred,
        }


_UNIT_TOKEN_SORTED: tuple[str, ...] = tuple(
    sorted(UNIT_ALIASES.keys(), key=lambda s: (-len(s), s))
)
_UNIT_ALT_PATTERN: str = "|".join(re.escape(u) for u in _UNIT_TOKEN_SORTED)

_UPPER_LT_RX = re.compile(
    rf"^(?:<\s*|under\s+|less\s+than\s+|below\s+|younger\s+than\s+|up\s+to\s*<\s*)"
    rf"(?P<val>\d+(?:\.\d+)?)(?:\s*(?P<unit>{_UNIT_ALT_PATTERN}))?$",
    re.IGNORECASE,
)

_UPPER_LE_RX = re.compile(
    rf"^(?:(?:<=\s*|at\s+most\s+|up\s+to(?:\s+and\s+including)?\s+)"
    rf"(?P<val>\d+(?:\.\d+)?)(?:\s*(?P<unit>{_UNIT_ALT_PATTERN}))?"
    rf"|(?P<val2>\d+(?:\.\d+)?)(?:\s*(?P<unit2>{_UNIT_ALT_PATTERN}))?\s+"
    rf"(?:and|or)\s+(?:under|younger|less|below)(?:\s*(?P<unit4>{_UNIT_ALT_PATTERN}))?)$",
    re.IGNORECASE,
)

_LOWER_GE_RX = re.compile(
    rf"^(?:(?:>=\s*|at\s+least\s+)(?P<val>\d+(?:\.\d+)?)(?:\s*(?P<unit>{_UNIT_ALT_PATTERN}))?"
    rf"|(?P<val2>\d+(?:\.\d+)?)(?:\s*(?P<unit2>{_UNIT_ALT_PATTERN}))?\s*"
    rf"(?:\+|(?:\s+(?:and|or)\s+(?:older|over|above|more|up|greater))|\s+plus)"
    rf"(?:\s*(?P<unit4>{_UNIT_ALT_PATTERN}))?"
    rf"|(?P<val3>\d+(?:\.\d+)?)\s*\+\s*(?P<unit3>{_UNIT_ALT_PATTERN}))$",
    re.IGNORECASE,
)

_LOWER_GT_RX = re.compile(
    rf"^(?:>\s*|over\s+|older\s+than\s+|more\s+than\s+|greater\s+than\s+|above\s+)"
    rf"(?P<val>\d+(?:\.\d+)?)(?:\s*(?P<unit>{_UNIT_ALT_PATTERN}))?$",
    re.IGNORECASE,
)

_RANGE_RX = re.compile(
    rf"^(?:between\s+|from\s+)?"
    rf"(?P<lo_op>>=|<=|>|<)?\s*"
    rf"(?P<lo_val>\d+(?:\.\d+)?)"
    rf"(?:\s*(?P<lo_unit>{_UNIT_ALT_PATTERN}))?"
    rf"\s*(?:to|through|thru|and|-)\s*"
    rf"(?P<hi_op><=|>=|<|>|under\s+|less\s+than\s+|below\s+)?"
    rf"\s*(?P<hi_val>\d+(?:\.\d+)?)"
    rf"(?:\s*(?P<hi_unit>{_UNIT_ALT_PATTERN}))?$",
    re.IGNORECASE,
)

_SINGLE_AGE_RX = re.compile(
    rf"^(?P<val>\d+(?:\.\d+)?)\s*(?P<unit>{_UNIT_ALT_PATTERN})$",
    re.IGNORECASE,
)

_PAREN_SUFFIX_RX = re.compile(r"^[^()\d<>=\u2264\u2265]+\(([^()]+)\)$")
_COLON_SUFFIX_RX = re.compile(r"^[A-Za-z][A-Za-z\s/-]*[:,-]\s*(.+)$")
_TRAILING_PAREN_UNIT_RX = re.compile(
    rf"^(?P<body>.+?)\s*\(\s*(?P<unit>{_UNIT_ALT_PATTERN})\s*\)$",
    re.IGNORECASE,
)


_LEADING_AGE_PREFIX_RX = re.compile(
    r"^(?:ages?|aged)\s+(?=(?:[<>=\d]|under\b|less\b|below\b|younger\b|at\s+(?:least|most)\b|"
    r"up\s+to\b|over\b|older\b|more\b|greater\b|above\b|between\b|from\b))",
    re.IGNORECASE,
)


_SPELLED_GE_RX = re.compile(
    r"\b(?:greater|more|older)\s+than\s+or\s+equal\s+to(?:\s*\(\s*>=\s*\))?",
    re.IGNORECASE,
)
_SPELLED_LE_RX = re.compile(
    r"\b(?:less|fewer|younger)\s+than\s+or\s+equal\s+to(?:\s*\(\s*<=\s*\))?",
    re.IGNORECASE,
)
_SPELLED_GT_PAREN_RX = re.compile(
    r"\b(?:greater|more|older)\s+than\s*\(\s*>\s*\)",
    re.IGNORECASE,
)
_SPELLED_LT_PAREN_RX = re.compile(
    r"\b(?:less|fewer|younger)\s+than\s*\(\s*<\s*\)",
    re.IGNORECASE,
)


def _normalize_interval_text(raw: str) -> str:
    s = _norm_text(raw)
    s = s.replace("\u2264", "<=").replace("\u2265", ">=")
    s = s.replace("=<", "<=").replace("=>", ">=")
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    s = s.replace("&", " and ")
    s = _SPELLED_GE_RX.sub(">=", s)
    s = _SPELLED_LE_RX.sub("<=", s)
    s = _SPELLED_GT_PAREN_RX.sub(">", s)
    s = _SPELLED_LT_PAREN_RX.sub("<", s)
    s = _WS_RX.sub(" ", s).strip()
    if s.endswith(".") and not s.lower().endswith("y.o."):
        s = s[:-1].rstrip()
    s = _LEADING_AGE_PREFIX_RX.sub("", s).strip()
    return s


def _is_integer_token(num_str: str) -> bool:
    return "." not in num_str.strip()


def _resolve_unit(unit_token: Optional[str], default_unit: Optional[str]) -> tuple[Optional[str], bool]:
    """Return (canonical_unit, unit_was_inferred)."""
    if unit_token:
        canon = UNIT_ALIASES.get(_WS_RX.sub(" ", unit_token.strip().lower()))
        if canon:
            return canon, False
        return None, False
    if default_unit:
        canon_def = UNIT_ALIASES.get(_WS_RX.sub(" ", default_unit.strip().lower()))
        if canon_def:
            return canon_def, True
    return None, False


def _try_parse_core_interval(text: str, default_unit: Optional[str]) -> Optional[ParsedInterval]:
    """Attempt to parse normalized interval core text into a ParsedInterval of kind 'interval'."""
    # Unwrap trailing "(years)" unit if present, e.g. "18 - 64 (years)" -> "18 - 64 years"
    m_pu = _TRAILING_PAREN_UNIT_RX.match(text)
    if m_pu:
        text = f"{m_pu.group('body').strip()} {m_pu.group('unit').strip()}"

    # 1. Strict upper bound: < 18 years, Under 18 years
    m = _UPPER_LT_RX.match(text)
    if m:
        val_s = m.group("val")
        unit, inferred = _resolve_unit(m.group("unit"), default_unit)
        if unit is None:
            return None
        hi = float(val_s) * UNIT_YEARS[unit]
        if hi <= 0.0:
            return None
        basis = "strict_upper_lt_default_years" if inferred else f"strict_upper_lt_{unit}"
        return ParsedInterval(
            raw_label=text,
            kind="interval",
            lo_years=0.0,
            hi_years=hi,
            bound_basis=basis,
            unit_inferred=inferred,
        )

    # 2. Inclusive upper bound: <= 17 years -> [0, 18), <= 18 years -> [0, 19) when integer
    m = _UPPER_LE_RX.match(text)
    if m:
        val_s = m.group("val") or m.group("val2")
        unit_tok = m.group("unit") or m.group("unit2") or m.group("unit4")
        unit, inferred = _resolve_unit(unit_tok, default_unit)
        if unit is None or val_s is None:
            return None
        v = float(val_s)
        if v < 0.0:
            return None
        if _is_integer_token(val_s):
            hi = (v + 1.0) * UNIT_YEARS[unit]
            basis = f"inclusive_integer_upper_plus_one_{unit}"
        else:
            hi = v * UNIT_YEARS[unit]
            basis = f"inclusive_decimal_upper_{unit}"
        if inferred:
            basis = f"{basis}_default_years"
        if hi <= 0.0:
            return None
        return ParsedInterval(
            raw_label=text,
            kind="interval",
            lo_years=0.0,
            hi_years=hi,
            bound_basis=basis,
            unit_inferred=inferred,
        )

    # 3. Inclusive lower open-top bound: >= 65 years, 65 years and older, 65+
    m = _LOWER_GE_RX.match(text)
    if m:
        val_s = m.group("val") or m.group("val2") or m.group("val3")
        unit_tok = m.group("unit") or m.group("unit2") or m.group("unit3") or m.group("unit4")
        unit, inferred = _resolve_unit(unit_tok, default_unit)
        if unit is None or val_s is None:
            return None
        lo = float(val_s) * UNIT_YEARS[unit]
        if lo < 0.0:
            return None
        basis = "inclusive_lower_open_top_default_years" if inferred else f"inclusive_lower_open_top_{unit}"
        return ParsedInterval(
            raw_label=text,
            kind="interval",
            lo_years=lo,
            hi_years=None,
            bound_basis=basis,
            unit_inferred=inferred,
        )

    # 4. Strict lower open-top bound: > 64 years -> [65, None), > 65 years -> [66, None)
    # In integer completed age units, "> N" admits N+1, N+2, ... which tiles cleanly
    # alongside "<= N" ([0, N+1)) or "a to N" ([a, N+1)).
    m = _LOWER_GT_RX.match(text)
    if m:
        val_s = m.group("val")
        unit, inferred = _resolve_unit(m.group("unit"), default_unit)
        if unit is None:
            return None
        v = float(val_s)
        if v < 0.0:
            return None
        if _is_integer_token(val_s):
            lo = (v + 1.0) * UNIT_YEARS[unit]
            basis = f"strict_integer_lower_plus_one_{unit}"
        else:
            lo = v * UNIT_YEARS[unit]
            basis = f"strict_decimal_lower_{unit}"
        if inferred:
            basis = f"{basis}_default_years"
        return ParsedInterval(
            raw_label=text,
            kind="interval",
            lo_years=lo,
            hi_years=None,
            bound_basis=basis,
            unit_inferred=inferred,
        )

    # 5. Range: 18 to 64 years, 18-64 years, 18 - <65 years, 28 days to 23 months
    m = _RANGE_RX.match(text)
    if m:
        lo_op = (m.group("lo_op") or "").strip().lower()
        hi_op = _WS_RX.sub(" ", (m.group("hi_op") or "").strip().lower())
        lo_val_s = m.group("lo_val")
        hi_val_s = m.group("hi_val")
        lo_unit_tok = m.group("lo_unit")
        hi_unit_tok = m.group("hi_unit")

        # Reject reverse/nonsensical operators on endpoints (e.g. "< 18 to 64" or "18 to > 64")
        if lo_op in ("<", "<=") or hi_op in (">", ">="):
            return None

        # Shared trailing unit: "18 to 64 years" gives hi_unit="years", lo_unit=None -> both "years".
        hi_unit, hi_inferred = _resolve_unit(hi_unit_tok, lo_unit_tok or default_unit)
        if lo_unit_tok:
            lo_unit, lo_inferred = _resolve_unit(lo_unit_tok, default_unit)
        else:
            lo_unit, lo_inferred = hi_unit, hi_inferred

        if lo_unit is None or hi_unit is None:
            return None

        lo_v = float(lo_val_s)
        hi_v = float(hi_val_s)
        if lo_v < 0.0 or hi_v < 0.0:
            return None

        if lo_op == ">":
            lo_years = (
                (lo_v + 1.0) * UNIT_YEARS[lo_unit]
                if _is_integer_token(lo_val_s)
                else lo_v * UNIT_YEARS[lo_unit]
            )
            lo_basis = "gt_int_plus_one" if _is_integer_token(lo_val_s) else "gt_dec"
        else:
            lo_years = lo_v * UNIT_YEARS[lo_unit]
            lo_basis = "ge"

        if hi_op in ("<", "under", "less than", "below"):
            hi_years = hi_v * UNIT_YEARS[hi_unit]
            hi_basis = "lt_explicit"
        else:
            if _is_integer_token(hi_val_s):
                hi_years = (hi_v + 1.0) * UNIT_YEARS[hi_unit]
                hi_basis = "le_int_plus_one"
            elif hi_op == "<=":
                hi_years = hi_v * UNIT_YEARS[hi_unit]
                hi_basis = "le_explicit_dec"
            else:
                hi_years = hi_v * UNIT_YEARS[hi_unit]
                hi_basis = "le_dec"

        if hi_years <= lo_years + _OVERLAP_TOL_YEARS:
            return None

        inferred = bool(lo_inferred and hi_inferred and not lo_unit_tok and not hi_unit_tok)
        unit_desc = lo_unit if lo_unit == hi_unit else f"{lo_unit}_to_{hi_unit}"
        basis = f"range_{lo_basis}_{hi_basis}_{unit_desc}"
        if inferred:
            basis = f"{basis}_default_years"
        return ParsedInterval(
            raw_label=text,
            kind="interval",
            lo_years=lo_years,
            hi_years=hi_years,
            bound_basis=basis,
            unit_inferred=inferred,
        )

    # 6. Single explicit-unit age: "18 years", "0 days"
    m = _SINGLE_AGE_RX.match(text)
    if m:
        val_s = m.group("val")
        unit, _ = _resolve_unit(m.group("unit"), None)
        if unit is None:
            return None
        v = float(val_s)
        if v < 0.0 or not _is_integer_token(val_s):
            return None
        lo = v * UNIT_YEARS[unit]
        hi = (v + 1.0) * UNIT_YEARS[unit]
        return ParsedInterval(
            raw_label=text,
            kind="interval",
            lo_years=lo,
            hi_years=hi,
            bound_basis=f"single_integer_age_{unit}",
            unit_inferred=False,
        )

    return None


def parse_interval_label(
    label: Any,
    *,
    default_unit: Optional[str] = "years",
) -> ParsedInterval:
    """
    Parse one categorical age row label into a half-open chronological year interval
    [lo_years, hi_years), or classify it as "structural", "unknown", or "unmapped".
    """
    raw = "" if label is None else str(label)
    norm = _normalize_interval_text(raw)
    if not norm:
        return ParsedInterval(raw_label=raw, kind="unmapped")

    low = norm.lower()
    if _CAT_STRUCTURAL_RX.search(low):
        return ParsedInterval(raw_label=raw, kind="structural")
    if _CAT_UNKNOWN_RX.search(low):
        return ParsedInterval(raw_label=raw, kind="unknown")

    direct = _try_parse_core_interval(norm, default_unit)
    if direct is not None:
        return ParsedInterval(
            raw_label=raw,
            kind="interval",
            lo_years=direct.lo_years,
            hi_years=direct.hi_years,
            bound_basis=direct.bound_basis,
            unit_inferred=direct.unit_inferred,
        )

    # Cohort-prefixed labels such as "Adolescents (12 to 17 years)" or "Adult: 18-64 years"
    for wrapper_rx in (_PAREN_SUFFIX_RX, _COLON_SUFFIX_RX):
        mw = wrapper_rx.match(norm)
        if mw:
            inner = mw.group(1).strip()
            sub = _try_parse_core_interval(inner, default_unit)
            if sub is not None:
                return ParsedInterval(
                    raw_label=raw,
                    kind="interval",
                    lo_years=sub.lo_years,
                    hi_years=sub.hi_years,
                    bound_basis=f"prefixed_{sub.bound_basis}",
                    unit_inferred=sub.unit_inferred,
                )

    return ParsedInterval(raw_label=raw, kind="unmapped")


@dataclass(frozen=True)
class BracketStructureAudit:
    brackets_tile_cleanly: bool
    flag_overlapping_brackets: bool
    flag_gap_between_brackets: bool
    has_nonzero_overlap: bool
    span_lo_years: Optional[float]
    span_hi_years: Optional[float]


def audit_bracket_intervals(
    brackets: Sequence[ParsedBracket],
    *,
    has_unmapped_category: bool = False,
) -> BracketStructureAudit:
    """
    Pairwise audit across all parsed non-unknown brackets of a categorical measure.
    Checks pairwise overlaps (including whether both overlapping brackets carry
    non-zero counts) and interior gaps between sorted intervals.
    """
    if not brackets:
        return BracketStructureAudit(
            brackets_tile_cleanly=False,
            flag_overlapping_brackets=False,
            flag_gap_between_brackets=False,
            has_nonzero_overlap=False,
            span_lo_years=None,
            span_hi_years=None,
        )

    ordered = sorted(
        brackets,
        key=lambda b: (b.lo_years, 1 if b.hi_years is None else 0, b.hi_years or 0.0),
    )

    flag_overlap = False
    nonzero_overlap = False
    n = len(ordered)
    for i in range(n):
        bi = ordered[i]
        bi_hi_incl = "inclusive_decimal_upper" in bi.bound_basis or "_le_explicit_dec_" in bi.bound_basis
        for j in range(i + 1, n):
            bj = ordered[j]
            bj_lo_excl = "strict_decimal_lower" in bj.bound_basis or "_gt_dec_" in bj.bound_basis
            # [bi.lo, bi.hi) and [bj.lo, bj.hi) overlap iff bi.hi is None or bi.hi > bj.lo + tol,
            # or at the point bi.hi == bj.lo when bi includes bi.hi AND bj includes bj.lo.
            point_seam_overlap = (
                bi.hi_years is not None
                and abs(bi.hi_years - bj.lo_years) <= _OVERLAP_TOL_YEARS
                and bi_hi_incl
                and not bj_lo_excl
            )
            if bi.hi_years is None or bi.hi_years > bj.lo_years + _OVERLAP_TOL_YEARS or point_seam_overlap:
                flag_overlap = True
                if bi.count > 0.0 and bj.count > 0.0:
                    nonzero_overlap = True

    flag_gap = False
    for i in range(n - 1):
        bi = ordered[i]
        bj = ordered[i + 1]
        if bi.hi_years is not None and (bj.lo_years - bi.hi_years) > _OVERLAP_TOL_YEARS:
            flag_gap = True

    span_lo = ordered[0].lo_years
    span_hi: Optional[float] = None if any(b.hi_years is None for b in ordered) else max(
        b.hi_years for b in ordered if b.hi_years is not None
    )
    tiles_cleanly = bool(not flag_overlap and not flag_gap and not has_unmapped_category)

    return BracketStructureAudit(
        brackets_tile_cleanly=tiles_cleanly,
        flag_overlapping_brackets=flag_overlap,
        flag_gap_between_brackets=flag_gap,
        has_nonzero_overlap=nonzero_overlap,
        span_lo_years=span_lo,
        span_hi_years=span_hi,
    )


# ---------------------------------------------------------------------------
# 6. Categorical measure parser
# ---------------------------------------------------------------------------


@dataclass
class ParsedCategoricalMeasure:
    title: Optional[str] = None
    param_type: Optional[str] = None
    unit_of_measure: Optional[str] = None
    layout: Optional[str] = None
    is_participant_count: bool = True
    parse_valid: bool = False
    uninformative_reason: Optional[str] = None
    brackets: list[ParsedBracket] = field(default_factory=list)
    n_classified: Optional[float] = None
    n_unknown: Optional[float] = None
    n_total_parsed: Optional[float] = None
    brackets_tile_cleanly: bool = False
    flag_overlapping_brackets: bool = False
    flag_gap_between_brackets: bool = False
    flag_nonparticipant_units: bool = False
    flag_percentage_units: bool = False
    flag_percentage_converted: bool = False
    flag_summed_arms_no_total: bool = False
    flag_ambiguous_groups: bool = False
    flag_unmapped_category: bool = False
    flag_customized_layout: bool = False
    flag_unrecognized_or_mixed_unit: bool = False
    unknown_labels: list[str] = field(default_factory=list)
    unmapped_labels: list[str] = field(default_factory=list)


def _decimal_places_of_raw_number(raw: Any) -> int:
    if raw is None or isinstance(raw, bool):
        return 0
    s = _strip_thousands_commas(str(raw).strip()).rstrip("%").strip()
    if "." not in s:
        return 0
    frac = s.split(".", 1)[1]
    digits = re.match(r"^\d+", frac)
    return len(digits.group(0)) if digits else 0


def _extract_group_cell_value(
    measurements: Sequence[dict[str, Any]],
    group_sel: GroupSelection,
    *,
    is_pct: bool,
    denom_by_group: dict[str, float],
) -> tuple[Optional[float], bool, bool, Optional[str], float]:
    """
    Extract participant count for one category row across group_sel.
    Returns (count_or_none, had_any_measurement_obj, pct_converted, error_reason, pct_round_slack).
    """
    meas_list = [m for m in measurements if isinstance(m, dict)]
    if not meas_list:
        return None, False, False, None, 0.0

    by_gid: dict[str, Optional[float]] = {}
    raw_by_gid: dict[str, Any] = {}
    ordered_vals: list[Optional[float]] = []
    ordered_raws: list[Any] = []
    for mm in meas_list:
        raw_cell = mm.get("value")
        v = _parse_float(raw_cell)
        ordered_vals.append(v)
        ordered_raws.append(raw_cell)
        gid = str(mm.get("groupId") or "").strip()
        if gid:
            if gid in by_gid:
                return None, True, False, "ambiguous_groups", 0.0
            by_gid[gid] = v
            raw_by_gid[gid] = raw_cell

    if group_sel.strategy == "ambiguous_groups":
        return None, True, False, "ambiguous_groups", 0.0

    if group_sel.strategy in ("single_group", "explicit_total"):
        target_gid = group_sel.overall_group_id
        raw_val: Optional[float] = None
        raw_token: Any = None
        if target_gid and target_gid in by_gid:
            raw_val = by_gid[target_gid]
            raw_token = raw_by_gid[target_gid]
        elif not target_gid and len(ordered_vals) == 1:
            raw_val = ordered_vals[0]
            raw_token = ordered_raws[0]
        elif group_sel.strategy == "single_group" and len(ordered_vals) == 1:
            raw_val = ordered_vals[0]
            raw_token = ordered_raws[0]
        else:
            return None, True, False, None, 0.0

        if raw_val is None:
            return None, True, False, None, 0.0
        if raw_val < 0.0:
            return None, True, False, "negative_category_count", 0.0

        if is_pct:
            denom: Optional[float] = None
            if target_gid and target_gid in denom_by_group:
                denom = denom_by_group[target_gid]
            elif len(denom_by_group) == 1 and group_sel.strategy == "single_group":
                denom = next(iter(denom_by_group.values()))
            if denom is None or denom <= 0.0:
                return None, True, False, "percentage_without_denominator", 0.0
            if raw_val > 100.0 + 1e-6:
                return None, True, False, "percentage_exceeds_100", 0.0
            dec_places = _decimal_places_of_raw_number(raw_token)
            row_slack = 0.5 * (10.0 ** (-dec_places)) * (denom / 100.0) + 0.5
            return float(round((raw_val / 100.0) * denom)), True, True, None, row_slack

        return raw_val, True, False, None, 0.0

    if group_sel.strategy == "sum_disjoint_arms":
        arm_vals: list[float] = []
        arm_slack = 0.0
        any_present = False
        converted_any = False
        for gid in group_sel.selected_group_ids:
            if gid not in by_gid or by_gid[gid] is None:
                continue
            any_present = True
            v = by_gid[gid]
            assert v is not None
            if v < 0.0:
                return None, True, False, "negative_category_count", 0.0
            if is_pct:
                d = denom_by_group.get(gid)
                if d is None or d <= 0.0:
                    return None, True, False, "percentage_without_denominator", 0.0
                if v > 100.0 + 1e-6:
                    return None, True, False, "percentage_exceeds_100", 0.0
                dec_places = _decimal_places_of_raw_number(raw_by_gid.get(gid))
                arm_slack += 0.5 * (10.0 ** (-dec_places)) * (d / 100.0) + 0.5
                arm_vals.append(float(round((v / 100.0) * d)))
                converted_any = True
            else:
                arm_vals.append(v)
        if not any_present:
            return None, True, False, None, 0.0
        return float(sum(arm_vals)), True, converted_any, None, arm_slack

    return None, True, False, "ambiguous_groups", 0.0


_TITLE_UNIT_TOKEN_MAP: dict[str, str] = {
    k: v for k, v in CONTINUOUS_UNIT_ALIASES.items() if " " not in k and len(k) >= 2
}


def _units_mentioned_in_text(text: Optional[str]) -> set[str]:
    """Return distinct canonical chronological units explicitly named as words in text."""
    s = _norm_text(text).lower()
    if not s:
        return set()
    found: set[str] = set()
    for tok, canon in _TITLE_UNIT_TOKEN_MAP.items():
        if re.search(r"\b" + re.escape(tok) + r"\b", s):
            found.add(canon)
    return found


def _infer_categorical_default_unit(measure: dict[str, Any]) -> tuple[Optional[str], bool, bool]:
    """
    Determine (default_unit, has_hard_unit_conflict, has_mixed_title_units) for
    categorical bracket labels from measure['unitOfMeasure'] and measure['title'].
      - Rejects conflicting explicit units (e.g. title='Age in Months' vs unitOfMeasure='Years')
        or mixed-unit unitOfMeasure strings by returning (None, True, False).
      - Flags mixed-unit titles (e.g. 'Age (Days or Months)') via has_mixed_title_units=True
        so unitless brackets are rejected rather than guessing one unit by token length.
      - Uses unambiguous CONTINUOUS_UNIT_ALIASES words so 'min' ('minimum') never collides
        with 'minute'.
    """
    um_canon = _resolve_continuous_unit(measure.get("unitOfMeasure"))
    uom_units = _units_mentioned_in_text(measure.get("unitOfMeasure"))
    title_units = _units_mentioned_in_text(measure.get("title"))
    if len(uom_units) > 1:
        return None, True, False
    if um_canon is not None and len(title_units) >= 1 and title_units != {um_canon}:
        return None, True, False
    if len(title_units) > 1:
        return "years", False, True
    title_unit = next(iter(title_units)) if len(title_units) == 1 else None
    if um_canon is not None:
        return um_canon, False, False
    if title_unit is not None:
        return title_unit, False, False
    return "years", False, False


def _resolve_prs_between_brackets(brackets: list[ParsedBracket]) -> list[ParsedBracket]:
    """Resolve NIH PRS's standard ``Between A and B <unit>`` interior bracket when flanked
    by inclusive boundary brackets (e.g. ``<=18 years``, ``Between 18 and 65 years``,
    ``>=65 years``).

    ClinicalTrials.gov's built-in ``Age, Categorical`` radio-button template labels its
    three mutually exclusive partition rows ``<=18 years`` ([0, 19)),
    ``Between 18 and 65 years``, and ``>=65 years`` ([65, inf)). Standalone ``Between A and B``
    is inclusive ([A, B+1)), but when paired alongside ``<=A`` (ending at A + 1 unit) and/or
    ``>=B`` (starting at B), "Between A and B" denotes the strict interior [A+1, B) so the
    three PRS rows tile [0, 19) + [19, 65) + [65, inf) with zero overlap and an exact 65.0
    cutpoint while keeping <=18 straddling 18.0. Hyphen/to labels (``18-65`` + ``65-80``)
    do not start with ``between`` and remain flagged as overlapping.
    """
    if len(brackets) < 2:
        return brackets
    out: list[ParsedBracket] = []
    for idx, b in enumerate(brackets):
        norm_lbl = _normalize_interval_text(b.label).lower()
        if not norm_lbl.startswith("between ") or b.hi_years is None:
            out.append(b)
            continue
        # Identify the unit step used when expanding integer upper bound (B -> B + step)
        step: Optional[float] = None
        for u_name, u_factor in UNIT_YEARS.items():
            if f"range_ge_le_int_plus_one_{u_name}" in b.bound_basis:
                step = u_factor
                break
        if step is None:
            out.append(b)
            continue
        new_lo = b.lo_years
        new_hi = b.hi_years
        for jdx, other in enumerate(brackets):
            if jdx == idx:
                continue
            if other.hi_years is not None and abs(other.hi_years - (b.lo_years + step)) <= _OVERLAP_TOL_YEARS:
                new_lo = other.hi_years
            if abs(other.lo_years - (b.hi_years - step)) <= _OVERLAP_TOL_YEARS:
                new_hi = other.lo_years
        if (new_lo != b.lo_years or new_hi != b.hi_years) and new_hi > new_lo + _OVERLAP_TOL_YEARS:
            out.append(
                ParsedBracket(
                    label=b.label,
                    lo_years=new_lo,
                    hi_years=new_hi,
                    count=b.count,
                    bound_basis=f"prs_between_flanked_{b.bound_basis}",
                    unit_inferred=b.unit_inferred,
                )
            )
        else:
            out.append(b)
    return out


def _resolve_target_denom(
    gsel: GroupSelection,
    denom_map: dict[str, float],
) -> Optional[float]:
    if not denom_map:
        return None
    if gsel.strategy in ("single_group", "explicit_total"):
        if gsel.overall_group_id and gsel.overall_group_id in denom_map:
            return denom_map[gsel.overall_group_id]
        if gsel.strategy == "single_group" and len(denom_map) == 1:
            return next(iter(denom_map.values()))
        return None
    if gsel.strategy == "sum_disjoint_arms":
        if gsel.selected_group_ids and all(gid in denom_map for gid in gsel.selected_group_ids):
            return sum(denom_map[gid] for gid in gsel.selected_group_ids)
    return None


def parse_categorical_measure(
    measure: dict[str, Any],
    *,
    group_selection: Optional[GroupSelection] = None,
    baseline_denoms: Optional[Iterable[dict[str, Any]]] = None,
) -> ParsedCategoricalMeasure:
    """Parse one categorical baseline age measure into brackets, counts, and structural flags."""
    res = ParsedCategoricalMeasure()
    if not isinstance(measure, dict):
        res.uninformative_reason = "no_measurements"
        return res

    title_raw = measure.get("title")
    res.title = None if title_raw is None else str(title_raw)
    pt_raw = measure.get("paramType")
    res.param_type = None if pt_raw is None else str(pt_raw)
    um_raw = measure.get("unitOfMeasure")
    res.unit_of_measure = None if um_raw is None else str(um_raw)

    gsel = (
        group_selection
        if (group_selection is not None and group_selection.strategy != "no_groups")
        else _infer_group_selection_from_measure(measure)
    )
    res.flag_summed_arms_no_total = gsel.flag_summed_arms_no_total
    res.flag_ambiguous_groups = gsel.flag_ambiguous_groups

    if is_nonparticipant_unit_measure(measure):
        res.is_participant_count = False
        res.flag_nonparticipant_units = True
        res.uninformative_reason = "non_participant_units"
        return res

    is_pct = is_percentage_measure(measure)
    res.flag_percentage_units = is_pct
    denom_map = _participant_denom_by_group(measure.get("denoms"), baseline_denoms)
    cat_default_unit, cat_unit_conflict, cat_mixed_title_units = _infer_categorical_default_unit(measure)
    if cat_unit_conflict or cat_default_unit is None:
        res.is_participant_count = False
        res.flag_unrecognized_or_mixed_unit = True
        res.uninformative_reason = "unrecognized_or_mixed_unit"
        return res

    if gsel.flag_ambiguous_groups:
        res.is_participant_count = False
        res.uninformative_reason = "ambiguous_groups"
        return res

    classes = [cl for cl in (measure.get("classes") or []) if isinstance(cl, dict)]
    if not classes:
        res.uninformative_reason = "no_measurements"
        return res

    # Detect layout: customized multi-class (where class titles are age brackets/unknown)
    # vs standard single-class (where category titles are age brackets/unknown).
    class_parses = [parse_interval_label(cl.get("title"), default_unit=cat_default_unit) for cl in classes]
    is_custom_classes = any(cp.kind in ("interval", "unknown") for cp in class_parses)

    # Each parsed row entry: (raw_label, list_of_category_measurement_lists, row_denoms, has_ambig_subcats)
    rows_to_parse: list[tuple[str, list[list[dict[str, Any]]], dict[str, float], bool]] = []
    if is_custom_classes:
        res.layout = "customized"
        res.flag_customized_layout = True
        for cl in classes:
            lbl = "" if cl.get("title") is None else str(cl.get("title"))
            cl_denoms = _participant_denom_by_group(cl.get("denoms"), None) or denom_map
            cats_in_cl = [cat for cat in (cl.get("categories") or []) if isinstance(cat, dict)]
            if len(cats_in_cl) <= 1:
                mm_single = [
                    mm
                    for cat in cats_in_cl
                    for mm in (cat.get("measurements") or [])
                    if isinstance(mm, dict)
                ]
                rows_to_parse.append((lbl, [mm_single] if mm_single else [], cl_denoms, False))
            else:
                # Multiple sub-categories inside one age-bracket class: prefer explicit Total
                # sub-category if present; otherwise sum distinct named non-overlapping sub-categories
                # (e.g. Female + Male), refusing untitled/duplicate sub-categories.
                total_cats = [
                    cat
                    for cat in cats_in_cl
                    if _norm_text(cat.get("title")).lower() in WHOLE_POPULATION_GROUP_TITLES
                    or _norm_text(cat.get("title")).lower().startswith("total")
                ]
                if len(total_cats) == 1:
                    mm_tot = [mm for mm in (total_cats[0].get("measurements") or []) if isinstance(mm, dict)]
                    rows_to_parse.append((lbl, [mm_tot] if mm_tot else [], cl_denoms, False))
                elif len(total_cats) > 1:
                    rows_to_parse.append((lbl, [], cl_denoms, True))
                else:
                    sub_titles = [_norm_text(cat.get("title")).lower() for cat in cats_in_cl]
                    if any(not st or _AMBIGUOUS_GROUP_RX.search(st) for st in sub_titles) or len(
                        set(sub_titles)
                    ) != len(sub_titles):
                        rows_to_parse.append((lbl, [], cl_denoms, True))
                    else:
                        sub_lists = [
                            [mm for mm in (cat.get("measurements") or []) if isinstance(mm, dict)]
                            for cat in cats_in_cl
                        ]
                        rows_to_parse.append((lbl, [sl for sl in sub_lists if sl], cl_denoms, False))
    else:
        res.layout = "standard"
        # Pick explicit Total/Overall class if multi-class, or single class; if multiple
        # incompatible classes exist (e.g. stratified sub-tables), mark ambiguous.
        chosen_class: Optional[dict[str, Any]] = None
        if len(classes) == 1:
            chosen_class = classes[0]
        else:
            for cl in classes:
                ct = _norm_text(cl.get("title")).lower()
                if ct in WHOLE_POPULATION_GROUP_TITLES or ct.startswith("total"):
                    chosen_class = cl
                    break
            if chosen_class is None:
                res.is_participant_count = False
                res.uninformative_reason = "ambiguous_multiclass_table"
                return res
        cl_denoms = _participant_denom_by_group(chosen_class.get("denoms"), None) or denom_map
        for cat in chosen_class.get("categories") or []:
            if not isinstance(cat, dict):
                continue
            lbl = "" if cat.get("title") is None else str(cat.get("title"))
            meas_objs = [mm for mm in (cat.get("measurements") or []) if isinstance(mm, dict)]
            rows_to_parse.append((lbl, [meas_objs] if meas_objs else [], cl_denoms, False))

    if not rows_to_parse:
        res.uninformative_reason = "no_measurements"
        return res

    had_any_meas = False
    had_any_numeric = False
    pct_converted_any = False
    pct_round_slack_total = 0.0
    any_inferred_bracket_unit = False
    raw_brackets: list[ParsedBracket] = []
    unknown_sum: Optional[float] = None
    structural_totals: list[float] = []
    unmapped_nonzero = False

    for raw_lbl, sub_meas_lists, row_denoms, ambig_sub in rows_to_parse:
        if ambig_sub:
            res.is_participant_count = False
            res.uninformative_reason = "ambiguous_multicategory_class"
            return res
        p_lbl = parse_interval_label(raw_lbl, default_unit=cat_default_unit)
        val: Optional[float] = None
        row_slack = 0.0
        for meas_objs in sub_meas_lists:
            sub_val, had_m, pct_conv, err, sub_slack = _extract_group_cell_value(
                meas_objs,
                gsel,
                is_pct=is_pct,
                denom_by_group=row_denoms,
            )
            if had_m:
                had_any_meas = True
            if err == "percentage_without_denominator":
                res.is_participant_count = False
                res.uninformative_reason = "percentage_without_denominator"
                return res
            if err is not None:
                res.is_participant_count = False
                if err == "ambiguous_groups":
                    res.flag_ambiguous_groups = True
                res.uninformative_reason = err
                return res
            if pct_conv:
                pct_converted_any = True
                row_slack += sub_slack
            if sub_val is not None:
                had_any_numeric = True
                val = (val or 0.0) + sub_val

        if p_lbl.kind == "structural":
            if val is not None and not is_pct:
                structural_totals.append(val)
            continue
        pct_round_slack_total += row_slack
        if p_lbl.kind == "unknown":
            if raw_lbl and raw_lbl not in res.unknown_labels:
                res.unknown_labels.append(raw_lbl)
            if val is not None:
                unknown_sum = (unknown_sum or 0.0) + val
            continue
        if p_lbl.kind == "unmapped":
            res.flag_unmapped_category = True
            if raw_lbl and raw_lbl not in res.unmapped_labels:
                res.unmapped_labels.append(raw_lbl)
            if val is not None and val > 0.0:
                unmapped_nonzero = True
            continue

        assert p_lbl.lo_years is not None and p_lbl.bound_basis is not None
        if p_lbl.unit_inferred:
            any_inferred_bracket_unit = True
        if val is not None:
            raw_brackets.append(
                ParsedBracket(
                    label=raw_lbl,
                    lo_years=p_lbl.lo_years,
                    hi_years=p_lbl.hi_years,
                    count=val,
                    bound_basis=p_lbl.bound_basis,
                    unit_inferred=p_lbl.unit_inferred,
                )
            )

    if cat_mixed_title_units and (any_inferred_bracket_unit or not raw_brackets):
        res.is_participant_count = False
        res.flag_percentage_converted = False
        res.flag_unrecognized_or_mixed_unit = True
        res.uninformative_reason = "unrecognized_or_mixed_unit"
        return res

    res.flag_percentage_converted = pct_converted_any
    if is_pct and not pct_converted_any:
        res.is_participant_count = False
        res.uninformative_reason = (
            "percentage_without_denominator" if not denom_map else "all_values_na"
        )
        return res

    raw_brackets = _resolve_prs_between_brackets(raw_brackets)
    audit = audit_bracket_intervals(raw_brackets, has_unmapped_category=res.flag_unmapped_category)
    res.brackets_tile_cleanly = audit.brackets_tile_cleanly
    res.flag_overlapping_brackets = audit.flag_overlapping_brackets
    res.flag_gap_between_brackets = audit.flag_gap_between_brackets

    # Overlapping brackets with non-zero counts MUST NOT be summed into a bogus total.
    if audit.has_nonzero_overlap:
        res.brackets = []
        res.n_classified = None
        res.n_unknown = None
        res.n_total_parsed = None
        res.parse_valid = False
        res.uninformative_reason = "overlapping_age_brackets"
        return res

    target_denom = _resolve_target_denom(gsel, denom_map)

    # Guard against impossible converted percentage totals exceeding or omitting the group denominator.
    if is_pct and pct_converted_any:
        conv_sum = sum(b.count for b in raw_brackets) + (unknown_sum or 0.0)
        max_round_slack = max(2.0, pct_round_slack_total + 1.0)
        if target_denom is not None and target_denom > 0.0:
            if conv_sum > target_denom + max_round_slack:
                res.is_participant_count = False
                res.flag_percentage_converted = False
                res.parse_valid = False
                res.uninformative_reason = "percentage_sum_exceeds_100"
                return res
            if conv_sum + max_round_slack < target_denom:
                res.is_participant_count = False
                res.flag_percentage_converted = False
                res.parse_valid = False
                res.uninformative_reason = "incomplete_categorical_brackets"
                return res

    if not had_any_meas:
        res.uninformative_reason = "no_measurements"
        return res
    if not had_any_numeric:
        res.uninformative_reason = "all_values_na"
        return res

    if unmapped_nonzero and not raw_brackets:
        res.uninformative_reason = "no_mapped_age_brackets"
        return res
    if unmapped_nonzero and raw_brackets:
        # Non-zero unmapped categories alongside mapped brackets leave part of the
        # table unclassified without being explicit unknown; refuse partial totals.
        res.uninformative_reason = "partial_unmapped_categories"
        return res

    classified_total = sum(b.count for b in raw_brackets)
    unk_val = unknown_sum
    total_parsed = classified_total + (unk_val or 0.0)

    # Completeness guard against gapped / omitted-bin categorical tables:
    # when an explicit structural 'Total' row or explicit Participants denominator
    # attests N participants in this table, yet parsed brackets + Unknown account for
    # strictly fewer than N participants (with > 0 parsed or > 0 structural total),
    # an age bin was omitted; never publish a partial sub-sum as the cohort distribution!
    if not is_pct and total_parsed > 0.0:
        attested_totals = [t for t in structural_totals if t > 0.0]
        if target_denom is not None and target_denom > 0.0:
            attested_totals.append(target_denom)
        if attested_totals:
            if total_parsed < max(attested_totals) - 1e-6:
                res.brackets = []
                res.n_classified = None
                res.n_unknown = None
                res.n_total_parsed = None
                res.parse_valid = False
                res.uninformative_reason = "incomplete_categorical_brackets"
                return res
            if total_parsed > min(attested_totals) + 1e-6:
                res.brackets = []
                res.n_classified = None
                res.n_unknown = None
                res.n_total_parsed = None
                res.parse_valid = False
                res.uninformative_reason = "inconsistent_categorical_total"
                return res

    res.brackets = raw_brackets
    res.n_classified = classified_total if raw_brackets else (0.0 if unk_val is not None else None)
    res.n_unknown = unk_val
    res.n_total_parsed = total_parsed

    if classified_total > 0.0:
        res.parse_valid = True
        res.uninformative_reason = None
    elif (unk_val or 0.0) > 0.0:
        res.parse_valid = True
        res.uninformative_reason = None
    else:
        res.parse_valid = False
        res.uninformative_reason = "all_values_zero" if (raw_brackets or res.unknown_labels) else "no_mapped_age_brackets"

    return res


# ---------------------------------------------------------------------------
# 7. Continuous age summary grammar
# ---------------------------------------------------------------------------


@dataclass
class ParsedContinuousMeasure:
    title: Optional[str] = None
    param_type: Optional[str] = None
    central_tendency_type: Optional[str] = None
    dispersion_type: Optional[str] = None
    unit_of_measure: Optional[str] = None
    canonical_unit: Optional[str] = None
    parse_valid: bool = False
    uninformative_reason: Optional[str] = None
    # Raw verbatim numeric values (in unit_of_measure)
    central_value: Optional[float] = None
    dispersion_value: Optional[float] = None
    dispersion_low: Optional[float] = None
    dispersion_high: Optional[float] = None
    full_range_min: Optional[float] = None
    full_range_max: Optional[float] = None
    # Normalized values in chronological years (None unless unit is unambiguous)
    central_years: Optional[float] = None
    mean_years: Optional[float] = None
    sd_years: Optional[float] = None
    se_years: Optional[float] = None
    median_years: Optional[float] = None
    geometric_mean_years: Optional[float] = None
    least_squares_mean_years: Optional[float] = None
    iqr_width_years: Optional[float] = None
    iqr_low_years: Optional[float] = None
    iqr_high_years: Optional[float] = None
    ci95_low_years: Optional[float] = None
    ci95_high_years: Optional[float] = None
    min_years: Optional[float] = None
    max_years: Optional[float] = None
    # Flags
    flag_unrecognized_or_mixed_unit: bool = False
    flag_impossible_continuous_value: bool = False
    flag_summed_arms_no_total: bool = False
    flag_ambiguous_groups: bool = False


_NUM_OR_NONFINITE_PAT = (
    r"[+-]?(?:(?:[1-9]\d{0,2}(?:,\d{3})+|\d+)(?:\.\d+)?|nan|infinity|inf)"
)
_STRICT_COMMA_ENDPOINT_NUM_PAT = (
    r"[+-]?(?:(?:[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d*|0)(?:\.\d+)?|nan|infinity|inf)"
)
_INLINE_PARENS_STAT_RX = re.compile(
    rf"^\s*(?P<central>{_NUM_OR_NONFINITE_PAT})\s*(?P<open>[\(\[])\s*"
    rf"(?P<inner>[^\(\)\[\]]+)\s*(?P<close>[\)\]])\s*$",
    re.IGNORECASE,
)
_RANGE_TO_OR_DASH_RX = re.compile(
    rf"^\s*(?P<lo>{_NUM_OR_NONFINITE_PAT})\s*(?:to|-)\s*"
    rf"(?P<hi>{_NUM_OR_NONFINITE_PAT})(?:\s*(?P<unit>{_UNIT_ALT_PATTERN}))?\s*$",
    re.IGNORECASE,
)
_ENDPOINT_WITH_OPT_UNIT_RX = re.compile(
    rf"^\s*(?P<num>{_STRICT_COMMA_ENDPOINT_NUM_PAT})(?:\s*(?P<unit>{_UNIT_ALT_PATTERN}))?\s*$",
    re.IGNORECASE,
)


def _parse_endpoint_and_optional_unit(
    token: str,
) -> tuple[Optional[float], bool, Optional[str]]:
    m = _ENDPOINT_WITH_OPT_UNIT_RX.match(token.strip())
    if not m:
        return None, False, None
    val, nf = _parse_raw_float_keeping_nonfinite(m.group("num"))
    u_tok = m.group("unit")
    u_canon = UNIT_ALIASES.get(_WS_RX.sub(" ", u_tok.strip().lower())) if u_tok else None
    return val, nf, u_canon


def _find_comma_range_split(
    inner: str,
) -> tuple[Optional[tuple[float, float, Optional[str]]], bool]:
    """
    Split a comma-delimited range body (e.g. '18, 82', '1,000, 10,000', '18,100',
    '1,000,10,000', '1,000,2,000', '1,000,100,000', '10, Inf') into
    ((lo, hi, unit), had_nonfinite) using strict thousands-grouping rules so
    leading-'000' fragments never produce bogus splits.
    """
    spaced_parts = re.split(r",\s+", inner)
    if len(spaced_parts) == 2:
        lo, nf1, u1 = _parse_endpoint_and_optional_unit(spaced_parts[0])
        hi, nf2, u2 = _parse_endpoint_and_optional_unit(spaced_parts[1])
        if nf1 or nf2:
            return None, True
        if lo is not None and hi is not None and (u1 is None or u2 is None or u1 == u2):
            return (lo, hi, (u2 or u1)), False

    candidates: list[tuple[float, float, Optional[str]]] = []
    saw_nonfinite = False
    for idx, ch in enumerate(inner):
        if ch != ",":
            continue
        left_tok = inner[:idx]
        right_tok = inner[idx + 1 :]
        if not left_tok.strip() or not right_tok.strip():
            continue
        lo, nf1, u1 = _parse_endpoint_and_optional_unit(left_tok)
        hi, nf2, u2 = _parse_endpoint_and_optional_unit(right_tok)
        if (nf1 and (hi is not None or nf2)) or (nf2 and (lo is not None or nf1)):
            saw_nonfinite = True
            continue
        if lo is not None and hi is not None and (u1 is None or u2 is None or u1 == u2):
            candidates.append((lo, hi, (u2 or u1)))

    if saw_nonfinite and not candidates:
        return None, True
    if len(candidates) == 1:
        return candidates[0], False
    if len(candidates) > 1:
        ordered = [c for c in candidates if c[0] <= c[1]]
        if len(ordered) == 1:
            return ordered[0], False
    return None, False


def _parse_range_pair_string_with_nonfinite(
    raw: Any,
) -> tuple[Optional[tuple[float, float, Optional[str]]], bool]:
    """
    Parse standalone range strings such as '18 to 82', '18 - 82', '18-82 years',
    '(18, 82)', '(18,100)', '[18,105]', '[18.5, 79.2]', '(1,000,10,000)' BEFORE
    stripping thousands commas so '(18,100)' is never collapsed into '18100',
    while rejecting unbracketed European decimal commas such as '12,50'.
    Also flags non-finite range endpoints such as '(10, Inf)' or '10 to NaN'.
    """
    if not isinstance(raw, str):
        return None, False
    s = raw.strip().replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    if not s:
        return None, False

    was_bracketed = False
    if s[0] in "([":
        expected_close = ")" if s[0] == "(" else "]"
        if s[-1] != expected_close:
            return None, False
        inner = s[1:-1].strip()
        was_bracketed = True
    elif s[-1] in ")]":
        return None, False
    else:
        inner = s

    m_dash = _RANGE_TO_OR_DASH_RX.match(inner)
    if m_dash:
        lo, nf1 = _parse_raw_float_keeping_nonfinite(m_dash.group("lo"))
        hi, nf2 = _parse_raw_float_keeping_nonfinite(m_dash.group("hi"))
        if nf1 or nf2:
            return None, True
        if lo is None or hi is None:
            return None, False
        u_tok = m_dash.group("unit")
        u_canon = UNIT_ALIASES.get(_WS_RX.sub(" ", u_tok.strip().lower())) if u_tok else None
        return (lo, hi, u_canon), False

    if was_bracketed and "," in inner:
        return _find_comma_range_split(inner)

    return None, False


def _parse_range_pair_string(raw: Any) -> Optional[tuple[float, float, Optional[str]]]:
    pair, _ = _parse_range_pair_string_with_nonfinite(raw)
    return pair


def _resolve_continuous_unit(unit_of_measure: Optional[str]) -> Optional[str]:
    """Return canonical unit key ONLY when unitOfMeasure is an unambiguous chronological time unit."""
    if unit_of_measure is None:
        return None
    s = _norm_text(unit_of_measure).lower()
    if not s:
        return None
    # Reject mixed-unit strings such as "years / months" or "months or years"
    if "/" in s or " or " in s or " and " in s or ";" in s:
        return None
    return CONTINUOUS_UNIT_ALIASES.get(s)


def _detect_conflicting_row_units(measure: dict[str, Any], expected_canon_unit: str) -> bool:
    """True if any class or category title explicitly names a different chronological unit."""
    for cl in measure.get("classes") or []:
        if not isinstance(cl, dict):
            continue
        titles_to_check = [_norm_text(cl.get("title"))]
        for cat in cl.get("categories") or []:
            if isinstance(cat, dict):
                titles_to_check.append(_norm_text(cat.get("title")))
        for t in titles_to_check:
            if not t:
                continue
            tl = t.lower()
            for tok, u_canon in CONTINUOUS_UNIT_ALIASES.items():
                if len(tok) >= 3 and re.search(r"\b" + re.escape(tok) + r"\b", tl):
                    if u_canon != expected_canon_unit:
                        return True
    return False


def _parse_measurement_stat_fields(
    mm: dict[str, Any],
    *,
    dispersion_type: Optional[str] = None,
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float], bool, Optional[str]]:
    """
    Extract (central, spread, lower, upper, had_nonfinite, inline_unit) from a single
    API v2 measurement dict. Supports value/spread/dispersionValue/lowerLimit/upperLimit,
    inline '54.2 (11.8)' / '54.2 (45.0, 62.0)' / '50 (10,100)' / '1,200 (18, 2,400)' strings,
    and range strings such as '18 to 82', '(18, 82)', '(18,100)', '[18,105]', '18-82 years'
    in value/spread/dispersionValue.
    """
    raw_v = mm.get("value")
    raw_sp = mm.get("spread") if mm.get("spread") is not None else mm.get("dispersionValue")
    c_val, nf_c = _parse_raw_float_keeping_nonfinite(raw_v)
    s_val, nf_s = _parse_raw_float_keeping_nonfinite(raw_sp)
    lo_val, nf_lo = _parse_raw_float_keeping_nonfinite(mm.get("lowerLimit"))
    hi_val, nf_hi = _parse_raw_float_keeping_nonfinite(mm.get("upperLimit"))
    nonfinite = nf_c or nf_s or nf_lo or nf_hi
    inline_unit: Optional[str] = None
    dt_norm = (dispersion_type or "").strip().upper()

    if s_val is None and lo_val is None and hi_val is None and isinstance(raw_sp, str) and not nonfinite:
        sp_pair, sp_nf = _parse_range_pair_string_with_nonfinite(raw_sp)
        nonfinite = nonfinite or sp_nf
        if sp_pair is not None:
            lo_val, hi_val, inline_unit = sp_pair

    if c_val is None and isinstance(raw_v, str) and not nonfinite:
        norm_v = raw_v.strip().replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
        m_in = _INLINE_PARENS_STAT_RX.match(norm_v)
        if m_in and (
            (m_in.group("open") == "(" and m_in.group("close") == ")")
            or (m_in.group("open") == "[" and m_in.group("close") == "]")
        ):
            c_val, nf1 = _parse_raw_float_keeping_nonfinite(m_in.group("central"))
            inner = m_in.group("inner").strip()
            nonfinite = nonfinite or nf1

            parsed_inner_pair = False
            m_dash = _RANGE_TO_OR_DASH_RX.match(inner)
            if m_dash:
                lo_val, nf2 = _parse_raw_float_keeping_nonfinite(m_dash.group("lo"))
                hi_val, nf3 = _parse_raw_float_keeping_nonfinite(m_dash.group("hi"))
                u_tok = m_dash.group("unit")
                if u_tok:
                    inline_unit = UNIT_ALIASES.get(_WS_RX.sub(" ", u_tok.strip().lower()))
                nonfinite = nonfinite or nf2 or nf3
                parsed_inner_pair = (lo_val is not None and hi_val is not None) or nf2 or nf3
            elif "," in inner and dt_norm not in ("STANDARD_DEVIATION", "STANDARD_ERROR"):
                central_raw = m_in.group("central")
                has_spaced_sep = len(re.split(r",\s+", inner)) == 2
                split_res, split_nf = _find_comma_range_split(inner)
                nonfinite = nonfinite or split_nf
                if split_nf:
                    parsed_inner_pair = True
                elif split_res is not None:
                    lo_c, hi_c, u_c = split_res
                    accept_pair = False
                    if has_spaced_sep or not _STRICT_THOUSANDS_INT_RX.match(inner):
                        # Spaced comma ('1,000, 10,000') or non-thousands-integer string
                        # ('1,000,10,000', '1,000,2,000', '45.0,62.0', '80,90') cannot be a
                        # single scalar number, so it is unambiguously a (lo, hi) pair.
                        accept_pair = True
                    elif dt_norm in ("FULL_RANGE", "CONFIDENCE_INTERVAL_95"):
                        accept_pair = True
                    elif dt_norm == "INTER_QUARTILE_RANGE":
                        if inner.count(",") >= 2:
                            accept_pair = c_val is None or (lo_c - 1e-6 <= c_val <= hi_c + 1e-6)
                        else:
                            accept_pair = (
                                "," not in central_raw
                                and c_val is not None
                                and (lo_c - 1e-6 <= c_val <= hi_c + 1e-6)
                            )
                    if accept_pair:
                        lo_val, hi_val = lo_c, hi_c
                        inline_unit = u_c
                        parsed_inner_pair = True

            if not parsed_inner_pair:
                s_val, nf2 = _parse_raw_float_keeping_nonfinite(inner)
                nonfinite = nonfinite or nf2
        elif lo_val is None and hi_val is None:
            v_pair, v_nf = _parse_range_pair_string_with_nonfinite(raw_v)
            nonfinite = nonfinite or v_nf
            if v_pair is not None:
                lo_val, hi_val, inline_unit = v_pair

    return c_val, s_val, lo_val, hi_val, nonfinite, inline_unit


def parse_continuous_measure(
    measure: dict[str, Any],
    *,
    group_selection: Optional[GroupSelection] = None,
    baseline_denoms: Optional[Iterable[dict[str, Any]]] = None,
) -> ParsedContinuousMeasure:
    """
    Parse one continuous baseline age measure and normalize stats to chronological years
    when unitOfMeasure is an unambiguous time unit.
    """
    res = ParsedContinuousMeasure()
    if not isinstance(measure, dict):
        res.uninformative_reason = "no_measurements"
        return res

    t_raw = measure.get("title")
    res.title = None if t_raw is None else str(t_raw)
    pt_raw = measure.get("paramType")
    res.param_type = None if pt_raw is None else str(pt_raw).strip().upper()
    if res.param_type in CENTRAL_TENDENCY_TYPES:
        res.central_tendency_type = res.param_type
    dt_raw = measure.get("dispersionType")
    res.dispersion_type = None if dt_raw is None else str(dt_raw).strip().upper()
    um_raw = measure.get("unitOfMeasure")
    res.unit_of_measure = None if um_raw is None else str(um_raw)

    gsel = (
        group_selection
        if (group_selection is not None and group_selection.strategy != "no_groups")
        else _infer_group_selection_from_measure(measure)
    )
    res.flag_summed_arms_no_total = gsel.flag_summed_arms_no_total
    res.flag_ambiguous_groups = gsel.flag_ambiguous_groups

    canon_unit = _resolve_continuous_unit(res.unit_of_measure)
    title_units = _units_mentioned_in_text(res.title)
    if (
        canon_unit is None
        or len(title_units) > 1
        or (len(title_units) == 1 and next(iter(title_units)) != canon_unit)
        or _detect_conflicting_row_units(measure, canon_unit)
    ):
        res.flag_unrecognized_or_mixed_unit = True

    if gsel.flag_ambiguous_groups:
        res.uninformative_reason = "ambiguous_groups"
        return res

    classes = [cl for cl in (measure.get("classes") or []) if isinstance(cl, dict)]
    if not classes:
        res.uninformative_reason = (
            "unrecognized_or_mixed_unit" if res.flag_unrecognized_or_mixed_unit else "no_measurements"
        )
        return res

    # Pick single class or explicit Total/Overall class.
    chosen_class: Optional[dict[str, Any]] = None
    if len(classes) == 1:
        chosen_class = classes[0]
    else:
        for cl in classes:
            ct = _norm_text(cl.get("title")).lower()
            if ct in WHOLE_POPULATION_GROUP_TITLES or ct.startswith("total"):
                chosen_class = cl
                break
        if chosen_class is None:
            res.uninformative_reason = "ambiguous_multiclass_table"
            return res

    cats = [c for c in (chosen_class.get("categories") or []) if isinstance(c, dict)]
    if not cats:
        res.uninformative_reason = (
            "unrecognized_or_mixed_unit" if res.flag_unrecognized_or_mixed_unit else "no_measurements"
        )
        return res

    chosen_cat: Optional[dict[str, Any]] = None
    if len(cats) == 1:
        chosen_cat = cats[0]
    else:
        for c in cats:
            ct = _norm_text(c.get("title")).lower()
            if ct in WHOLE_POPULATION_GROUP_TITLES or ct.startswith("total") or not ct:
                chosen_cat = c
                break
        if chosen_cat is None:
            res.uninformative_reason = "ambiguous_multicategory_continuous"
            return res

    meas_list = [m for m in (chosen_cat.get("measurements") or []) if isinstance(m, dict)]
    if not meas_list:
        res.uninformative_reason = (
            "unrecognized_or_mixed_unit" if res.flag_unrecognized_or_mixed_unit else "no_measurements"
        )
        return res

    by_gid: dict[str, dict[str, Any]] = {}
    for mm in meas_list:
        gid = str(mm.get("groupId") or "").strip()
        if gid:
            if gid in by_gid:
                res.flag_ambiguous_groups = True
                res.uninformative_reason = "ambiguous_groups"
                return res
            by_gid[gid] = mm

    central_val: Optional[float] = None
    spread_val: Optional[float] = None
    low_val: Optional[float] = None
    high_val: Optional[float] = None

    if gsel.strategy in ("single_group", "explicit_total"):
        target_mm: Optional[dict[str, Any]] = None
        if gsel.overall_group_id and gsel.overall_group_id in by_gid:
            target_mm = by_gid[gsel.overall_group_id]
        elif gsel.strategy == "single_group" and len(meas_list) == 1:
            target_mm = meas_list[0]
        elif not gsel.overall_group_id and len(meas_list) == 1 and not by_gid:
            target_mm = meas_list[0]
        if target_mm is None:
            res.uninformative_reason = "no_measurements"
            return res
        central_val, spread_val, low_val, high_val, nonfinite, inline_u = _parse_measurement_stat_fields(
            target_mm, dispersion_type=res.dispersion_type
        )
        if inline_u is not None and canon_unit is not None and inline_u != canon_unit:
            res.flag_unrecognized_or_mixed_unit = True
        if nonfinite:
            res.flag_impossible_continuous_value = True
            res.uninformative_reason = "impossible_continuous_value"
            return res

    elif gsel.strategy == "sum_disjoint_arms":
        # Continuous summary pooling across disjoint arms is mathematically valid ONLY
        # for MEAN (weighted by exact per-arm participant denominators), exact pooled SD
        # (when every arm has n_g and SD_g), and FULL_RANGE (min of mins, max of maxes).
        # Medians/IQRs/geometric means cannot be pooled from arm summaries.
        denom_map = _participant_denom_by_group(measure.get("denoms"), baseline_denoms)
        pt = res.param_type or ""
        dt = res.dispersion_type or ""
        if pt != "MEAN" and dt != "FULL_RANGE":
            res.uninformative_reason = "arms_not_poolable_for_continuous"
            return res

        arm_ns: list[float] = []
        arm_means: list[float] = []
        arm_sds: list[float] = []
        arm_mins: list[float] = []
        arm_maxs: list[float] = []
        all_have_sd = True
        all_have_range = True

        for gid in gsel.selected_group_ids:
            mm_arm = by_gid.get(gid)
            if mm_arm is None:
                res.uninformative_reason = "arms_not_poolable_for_continuous"
                return res
            cv, sv, lv, hv, nf, inl_u = _parse_measurement_stat_fields(
                mm_arm, dispersion_type=res.dispersion_type
            )
            if inl_u is not None and canon_unit is not None and inl_u != canon_unit:
                res.flag_unrecognized_or_mixed_unit = True
            if (
                nf
                or (cv is not None and cv < 0.0)
                or (sv is not None and sv < 0.0)
                or (lv is not None and lv < 0.0)
                or (hv is not None and hv < 0.0)
                or (lv is not None and hv is not None and lv > hv + 1e-9)
            ):
                res.flag_impossible_continuous_value = True
                res.uninformative_reason = "impossible_continuous_value"
                return res
            if pt == "MEAN":
                n_arm = denom_map.get(gid)
                if n_arm is None or n_arm <= 0.0 or cv is None:
                    res.uninformative_reason = "arms_not_poolable_for_continuous"
                    return res
                arm_ns.append(n_arm)
                arm_means.append(cv)
                if dt == "STANDARD_DEVIATION" and sv is not None:
                    arm_sds.append(sv)
                else:
                    all_have_sd = False
            if dt == "FULL_RANGE":
                if lv is not None and hv is not None:
                    arm_mins.append(lv)
                    arm_maxs.append(hv)
                else:
                    all_have_range = False

        if pt == "MEAN" and arm_ns:
            total_n = sum(arm_ns)
            central_val = sum(n_i * m_i for n_i, m_i in zip(arm_ns, arm_means)) / total_n
            if dt == "STANDARD_DEVIATION" and all_have_sd and len(arm_sds) == len(arm_ns) and total_n > 1.0:
                if any(s_i < 0.0 for s_i in arm_sds):
                    res.flag_impossible_continuous_value = True
                    res.uninformative_reason = "impossible_continuous_value"
                    return res
                ss_within = sum((n_i - 1.0) * (s_i ** 2) for n_i, s_i in zip(arm_ns, arm_sds))
                ss_between = sum(n_i * ((m_i - central_val) ** 2) for n_i, m_i in zip(arm_ns, arm_means))
                spread_val = math.sqrt(max(0.0, (ss_within + ss_between) / (total_n - 1.0)))
        if dt == "FULL_RANGE" and all_have_range and arm_mins and arm_maxs:
            low_val = min(arm_mins)
            high_val = max(arm_maxs)

    if central_val is None and low_val is None and high_val is None:
        res.uninformative_reason = (
            "unrecognized_or_mixed_unit" if res.flag_unrecognized_or_mixed_unit else "all_values_na"
        )
        return res

    # Record raw verbatim values prior to validation/normalization
    res.central_value = central_val
    res.dispersion_value = spread_val
    res.dispersion_low = low_val
    res.dispersion_high = high_val
    if res.dispersion_type == "FULL_RANGE":
        res.full_range_min = low_val
        res.full_range_max = high_val

    # Validate mathematical consistency & non-negativity
    impossible = False
    if central_val is not None and central_val < 0.0:
        impossible = True
    if spread_val is not None and spread_val < 0.0:
        impossible = True
    if low_val is not None and low_val < 0.0:
        impossible = True
    if high_val is not None and high_val < 0.0:
        impossible = True
    if low_val is not None and high_val is not None and low_val > high_val + 1e-9:
        impossible = True
    if (
        central_val is not None
        and low_val is not None
        and high_val is not None
        and res.dispersion_type in ("FULL_RANGE", "INTER_QUARTILE_RANGE", "CONFIDENCE_INTERVAL_95")
    ):
        if central_val < low_val - 1e-6 or central_val > high_val + 1e-6:
            impossible = True

    if impossible:
        res.flag_impossible_continuous_value = True
        res.uninformative_reason = "impossible_continuous_value"
        return res

    # Never assume years if unitOfMeasure was missing, non-age, or mixed!
    if res.flag_unrecognized_or_mixed_unit or canon_unit is None:
        res.uninformative_reason = "unrecognized_or_mixed_unit"
        return res

    res.canonical_unit = canon_unit
    factor = UNIT_YEARS[canon_unit]

    pt_eff = res.param_type or ""
    dt_eff = res.dispersion_type or ""

    if central_val is not None:
        c_yrs = central_val * factor
        res.central_years = c_yrs
        if pt_eff == "MEAN":
            res.mean_years = c_yrs
        elif pt_eff == "MEDIAN":
            res.median_years = c_yrs
        elif pt_eff == "GEOMETRIC_MEAN":
            res.geometric_mean_years = c_yrs
        elif pt_eff == "LEAST_SQUARES_MEAN":
            res.least_squares_mean_years = c_yrs
        elif pt_eff == "NUMBER" and dt_eff in ("STANDARD_DEVIATION", "STANDARD_ERROR"):
            res.mean_years = c_yrs
        elif pt_eff == "NUMBER" and dt_eff == "INTER_QUARTILE_RANGE":
            res.median_years = c_yrs

    if dt_eff == "STANDARD_DEVIATION" and spread_val is not None:
        res.sd_years = spread_val * factor
    elif dt_eff == "STANDARD_ERROR" and spread_val is not None:
        res.se_years = spread_val * factor
    elif dt_eff == "INTER_QUARTILE_RANGE":
        if low_val is not None and high_val is not None:
            res.iqr_low_years = low_val * factor
            res.iqr_high_years = high_val * factor
            res.iqr_width_years = (high_val - low_val) * factor
        elif spread_val is not None:
            res.iqr_width_years = spread_val * factor
    elif dt_eff == "FULL_RANGE":
        if low_val is not None:
            res.min_years = low_val * factor
        if high_val is not None:
            res.max_years = high_val * factor
    elif dt_eff == "CONFIDENCE_INTERVAL_95":
        if low_val is not None:
            res.ci95_low_years = low_val * factor
        if high_val is not None:
            res.ci95_high_years = high_val * factor

    has_usable_stat = any(
        v is not None
        for v in (
            res.mean_years,
            res.median_years,
            res.geometric_mean_years,
            res.least_squares_mean_years,
            res.central_years,
        )
    ) or (res.min_years is not None and res.max_years is not None)

    if has_usable_stat:
        res.parse_valid = True
        res.uninformative_reason = None
    else:
        res.parse_valid = False
        res.uninformative_reason = "no_usable_continuous_stat"

    return res


# ---------------------------------------------------------------------------
# 8. Trial-level orchestration & seven-state reporting classification
# ---------------------------------------------------------------------------


def _is_continuous_measure_candidate(measure: dict[str, Any]) -> bool:
    pt = _norm_text(measure.get("paramType")).upper()
    dt = _norm_text(measure.get("dispersionType")).upper()
    title = _norm_text(measure.get("title")).lower()
    if pt in ("MEAN", "MEDIAN", "GEOMETRIC_MEAN", "LEAST_SQUARES_MEAN", "LOG_MEAN"):
        return True
    if pt in ("COUNT_OF_PARTICIPANTS", "COUNT_OF_UNITS", "PERCENTAGE", "PERCENT"):
        return False
    if dt in DISPERSION_TYPES:
        return True
    if "continuous" in title:
        return True
    # If paramType is NUMBER or absent, inspect whether any class/category label parses as
    # an age bracket or unknown: if none do and unitOfMeasure is a chronological unit, treat
    # as continuous.
    cat_def_u, _, _ = _infer_categorical_default_unit(measure)
    eff_def_u = cat_def_u or "years"
    has_bracket_or_unk = False
    for cl in measure.get("classes") or []:
        if not isinstance(cl, dict):
            continue
        if parse_interval_label(cl.get("title"), default_unit=eff_def_u).kind in ("interval", "unknown"):
            has_bracket_or_unk = True
            break
        for cat in cl.get("categories") or []:
            if isinstance(cat, dict) and parse_interval_label(
                cat.get("title"), default_unit=eff_def_u
            ).kind in ("interval", "unknown"):
                has_bracket_or_unk = True
                break
    if not has_bracket_or_unk and _resolve_continuous_unit(measure.get("unitOfMeasure")) is not None:
        return True
    return False


@dataclass
class ParsedAgeTrial:
    # Presence & measure inventory
    raw_present: bool = False
    parse_ok: bool = True
    n_selected_measures: int = 0
    n_excluded_age_measures: int = 0
    excluded_age_measure_titles: list[str] = field(default_factory=list)
    excluded_age_measure_kinds: list[dict[str, str]] = field(default_factory=list)

    # Group selection
    group_selection_strategy: Optional[str] = None
    overall_group_id: Optional[str] = None

    # Categorical summary
    has_reported_categorical: bool = False
    categorical_measure_title: Optional[str] = None
    categorical_param_type: Optional[str] = None
    categorical_unit_of_measure: Optional[str] = None
    categorical_layout: Optional[str] = None
    is_participant_count: Optional[bool] = None
    parsed_brackets: list[ParsedBracket] = field(default_factory=list)
    n_classified: Optional[float] = None
    n_unknown: Optional[float] = None
    n_total_parsed: Optional[float] = None
    brackets_tile_cleanly: Optional[bool] = None

    # Continuous summary
    has_reported_continuous: bool = False
    continuous_measure_title: Optional[str] = None
    central_tendency_type: Optional[str] = None
    dispersion_type: Optional[str] = None
    continuous_unit_of_measure: Optional[str] = None
    central_value: Optional[float] = None
    dispersion_value: Optional[float] = None
    dispersion_low: Optional[float] = None
    dispersion_high: Optional[float] = None
    full_range_min: Optional[float] = None
    full_range_max: Optional[float] = None
    mean_years: Optional[float] = None
    sd_years: Optional[float] = None
    se_years: Optional[float] = None
    median_years: Optional[float] = None
    geometric_mean_years: Optional[float] = None
    least_squares_mean_years: Optional[float] = None
    iqr_width_years: Optional[float] = None
    iqr_low_years: Optional[float] = None
    iqr_high_years: Optional[float] = None
    ci95_low_years: Optional[float] = None
    ci95_high_years: Optional[float] = None
    min_years: Optional[float] = None
    max_years: Optional[float] = None

    # Audit flags & trails
    flag_overlapping_brackets: bool = False
    flag_gap_between_brackets: bool = False
    flag_nonparticipant_units: bool = False
    flag_percentage_units: bool = False
    flag_percentage_converted: bool = False
    flag_summed_arms_no_total: bool = False
    flag_ambiguous_groups: bool = False
    flag_unmapped_category: bool = False
    flag_customized_layout: bool = False
    flag_unrecognized_or_mixed_unit: bool = False
    flag_impossible_continuous_value: bool = False
    flag_exceeds_enrollment: bool = False
    unknown_labels: list[str] = field(default_factory=list)
    unmapped_labels: list[str] = field(default_factory=list)
    uninformative_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["parsed_brackets"] = [b.to_dict() for b in self.parsed_brackets]
        return d


def _cat_rank_key(c: ParsedCategoricalMeasure, idx: int) -> tuple[int, float, int, int, int]:
    has_known = 1 if (c.parse_valid and c.is_participant_count and (c.n_classified or 0.0) > 0.0) else 0
    has_unk = 1 if (c.parse_valid and c.is_participant_count and (c.n_unknown or 0.0) > 0.0) else 0
    tiles = 1 if c.brackets_tile_cleanly else 0
    n_cls = c.n_classified or 0.0
    n_nonzero_b = sum(1 for b in c.brackets if b.count > 0.0)
    # Prefer valid participant breakdown, then largest classified cohort count (so a single-row
    # subset indicator never outranks a full-cohort table), then clean tiling, then finest bins.
    return (has_known * 2 + has_unk, n_cls, tiles, n_nonzero_b, -idx)


def parse_trial(
    measures: Optional[Iterable[dict[str, Any]]],
    *,
    enrollment: Any = None,
    baseline_groups: Optional[Sequence[dict[str, Any]]] = None,
    baseline_denoms: Optional[Iterable[dict[str, Any]]] = None,
    excluded_measures: Optional[Iterable[dict[str, Any]]] = None,
    preselected: bool = False,
) -> ParsedAgeTrial:
    """
    Deterministic parse of a trial's baseline age measures into categorical brackets,
    continuous statistics in years, and audit flags.
    """
    out = ParsedAgeTrial()
    if measures is None and excluded_measures is None:
        return out

    meas_list: list[dict[str, Any]] = []
    if measures is not None:
        if isinstance(measures, (str, bytes)) or not isinstance(measures, Iterable):
            out.raw_present = True
            out.parse_ok = False
            return out
        for item in measures:
            if isinstance(item, dict):
                meas_list.append(item)
            else:
                out.raw_present = True
                out.parse_ok = False
                return out

    if preselected:
        selected = meas_list
        excl_pairs = select_excluded_age_measures(excluded_measures)
    else:
        selected = select_age_measures(meas_list)
        excl_pairs = select_excluded_age_measures(meas_list)
        if excluded_measures is not None:
            for m_ex, k_ex in select_excluded_age_measures(excluded_measures):
                excl_pairs.append((m_ex, k_ex))

    out.n_excluded_age_measures = len(excl_pairs)
    for m_ex, k_ex in excl_pairs:
        t_ex = str(m_ex.get("title") or "")
        if t_ex not in out.excluded_age_measure_titles:
            out.excluded_age_measure_titles.append(t_ex)
        rec_ex = {"title": t_ex, "kind": k_ex}
        if rec_ex not in out.excluded_age_measure_kinds:
            out.excluded_age_measure_kinds.append(rec_ex)

    if not selected:
        return out

    out.raw_present = True
    out.n_selected_measures = len(selected)

    gsel = select_overall_group(baseline_groups)
    if gsel.strategy != "no_groups":
        out.group_selection_strategy = gsel.strategy
        out.overall_group_id = gsel.overall_group_id
        out.flag_summed_arms_no_total = gsel.flag_summed_arms_no_total
        out.flag_ambiguous_groups = gsel.flag_ambiguous_groups

    cat_parsed: list[ParsedCategoricalMeasure] = []
    cont_parsed: list[ParsedContinuousMeasure] = []

    for m in selected:
        if _is_continuous_measure_candidate(m):
            cont_parsed.append(
                parse_continuous_measure(m, group_selection=gsel, baseline_denoms=baseline_denoms)
            )
        else:
            cat_parsed.append(
                parse_categorical_measure(m, group_selection=gsel, baseline_denoms=baseline_denoms)
            )

    # Aggregate categorical measures (pick primary fullest valid categorical measure; never sum across tables)
    if cat_parsed:
        best_idx = max(range(len(cat_parsed)), key=lambda i: _cat_rank_key(cat_parsed[i], i))
        prim_cat = cat_parsed[best_idx]
        out.categorical_measure_title = prim_cat.title
        out.categorical_param_type = prim_cat.param_type
        out.categorical_unit_of_measure = prim_cat.unit_of_measure
        out.categorical_layout = prim_cat.layout
        out.is_participant_count = prim_cat.is_participant_count
        out.brackets_tile_cleanly = prim_cat.brackets_tile_cleanly

        if prim_cat.parse_valid and prim_cat.is_participant_count:
            out.parsed_brackets = list(prim_cat.brackets)
            out.n_classified = prim_cat.n_classified
            out.n_unknown = prim_cat.n_unknown
            out.n_total_parsed = prim_cat.n_total_parsed
            if (prim_cat.n_classified or 0.0) > 0.0:
                out.has_reported_categorical = True
            flag_sources = [prim_cat]
        else:
            flag_sources = cat_parsed

        # Propagate categorical audit flags from the selected primary measure when valid,
        # or across candidate categorical measures when none was usable.
        out.flag_overlapping_brackets = any(c.flag_overlapping_brackets for c in flag_sources)
        out.flag_gap_between_brackets = any(c.flag_gap_between_brackets for c in flag_sources)
        out.flag_nonparticipant_units = any(c.flag_nonparticipant_units for c in flag_sources)
        out.flag_percentage_units = any(c.flag_percentage_units for c in flag_sources)
        out.flag_percentage_converted = any(c.flag_percentage_converted for c in flag_sources)
        out.flag_unmapped_category = any(c.flag_unmapped_category for c in flag_sources)
        out.flag_customized_layout = any(c.flag_customized_layout for c in flag_sources)
        out.flag_unrecognized_or_mixed_unit = out.flag_unrecognized_or_mixed_unit or any(
            c.flag_unrecognized_or_mixed_unit for c in flag_sources
        )
        out.flag_summed_arms_no_total = out.flag_summed_arms_no_total or any(
            c.flag_summed_arms_no_total for c in flag_sources
        )
        out.flag_ambiguous_groups = out.flag_ambiguous_groups or any(
            c.flag_ambiguous_groups for c in flag_sources
        )

        for lbl in prim_cat.unknown_labels:
            if lbl not in out.unknown_labels:
                out.unknown_labels.append(lbl)
        for c in cat_parsed:
            for lbl in c.unmapped_labels:
                if lbl not in out.unmapped_labels:
                    out.unmapped_labels.append(lbl)

    # Aggregate continuous measures (merge complementary valid summary measures, e.g. MEAN+SD and MEDIAN+FULL_RANGE)
    if cont_parsed:
        valid_cont = [c for c in cont_parsed if c.parse_valid]
        prim_cont = valid_cont[0] if valid_cont else cont_parsed[0]
        out.continuous_measure_title = prim_cont.title
        out.central_tendency_type = prim_cont.central_tendency_type or prim_cont.param_type
        out.dispersion_type = prim_cont.dispersion_type
        out.continuous_unit_of_measure = prim_cont.unit_of_measure
        out.central_value = prim_cont.central_value
        out.dispersion_value = prim_cont.dispersion_value
        out.dispersion_low = prim_cont.dispersion_low
        out.dispersion_high = prim_cont.dispersion_high
        out.full_range_min = prim_cont.full_range_min
        out.full_range_max = prim_cont.full_range_max

        if valid_cont:
            out.has_reported_continuous = True
            for vc in valid_cont:
                for attr in (
                    "mean_years",
                    "sd_years",
                    "se_years",
                    "median_years",
                    "geometric_mean_years",
                    "least_squares_mean_years",
                    "iqr_width_years",
                    "iqr_low_years",
                    "iqr_high_years",
                    "ci95_low_years",
                    "ci95_high_years",
                    "min_years",
                    "max_years",
                    "full_range_min",
                    "full_range_max",
                ):
                    if getattr(out, attr) is None and getattr(vc, attr) is not None:
                        setattr(out, attr, getattr(vc, attr))

            # Cross-measure consistency check: if merged min/max or IQR conflict with
            # central tendency or full range, flag impossible and invalidate continuous stats.
            if out.iqr_low_years is not None and out.iqr_high_years is not None:
                if out.iqr_low_years > out.iqr_high_years + 1e-9:
                    out.flag_impossible_continuous_value = True
                    out.has_reported_continuous = False
                if out.median_years is not None and (
                    out.median_years < out.iqr_low_years - 1e-6
                    or out.median_years > out.iqr_high_years + 1e-6
                ):
                    out.flag_impossible_continuous_value = True
                    out.has_reported_continuous = False

            if out.min_years is not None and out.max_years is not None:
                if out.min_years > out.max_years + 1e-9:
                    out.flag_impossible_continuous_value = True
                    out.has_reported_continuous = False
                for c_stat in (
                    out.mean_years,
                    out.median_years,
                    out.geometric_mean_years,
                    out.least_squares_mean_years,
                    out.iqr_low_years,
                    out.iqr_high_years,
                ):
                    if c_stat is not None and (
                        c_stat < out.min_years - 1e-6 or c_stat > out.max_years + 1e-6
                    ):
                        out.flag_impossible_continuous_value = True
                        out.has_reported_continuous = False

            if not out.has_reported_continuous:
                for attr in (
                    "mean_years",
                    "sd_years",
                    "se_years",
                    "median_years",
                    "geometric_mean_years",
                    "least_squares_mean_years",
                    "iqr_width_years",
                    "iqr_low_years",
                    "iqr_high_years",
                    "ci95_low_years",
                    "ci95_high_years",
                    "min_years",
                    "max_years",
                    "full_range_min",
                    "full_range_max",
                ):
                    setattr(out, attr, None)

        out.flag_unrecognized_or_mixed_unit = out.flag_unrecognized_or_mixed_unit or any(
            c.flag_unrecognized_or_mixed_unit for c in cont_parsed
        )
        out.flag_impossible_continuous_value = out.flag_impossible_continuous_value or any(
            c.flag_impossible_continuous_value for c in cont_parsed
        )
        out.flag_summed_arms_no_total = out.flag_summed_arms_no_total or any(
            c.flag_summed_arms_no_total for c in cont_parsed
        )
        out.flag_ambiguous_groups = out.flag_ambiguous_groups or any(
            c.flag_ambiguous_groups for c in cont_parsed
        )

    enr = _parse_float(enrollment)
    if enr is not None and enr < 0.0:
        enr = None
    if enr is not None and out.n_total_parsed is not None and out.is_participant_count:
        if out.n_total_parsed > enr:
            out.flag_exceeds_enrollment = True

    # Populate uninformative_reason ONLY when the trial will classify as uninformative.
    status = classify_age_reporting(out)
    if status == "uninformative":
        reasons = [
            c.uninformative_reason for c in cat_parsed if c.uninformative_reason
        ] + [
            c.uninformative_reason for c in cont_parsed if c.uninformative_reason
        ]
        if out.flag_impossible_continuous_value and "impossible_continuous_value" not in reasons:
            reasons.append("impossible_continuous_value")
        picked: Optional[str] = None
        for pr in UNINFORMATIVE_REASON_PRIORITY:
            if pr in reasons:
                picked = pr
                break
        if picked is None:
            picked = reasons[0] if reasons else "other"
        out.uninformative_reason = picked

    return out


def classify_age_reporting(p: ParsedAgeTrial) -> str:
    """
    Classify a parsed trial into one of the seven non-collapsing age_report_status states:
      - "parse_error"
      - "not_reported"
      - "reported_both"
      - "reported_categorical_only"
      - "reported_continuous_only"
      - "explicit_unknown_only"
      - "uninformative"

    explicit_unknown_only, uninformative, and not_reported are NEVER collapsed.
    enrollment_minus_parsed is NEVER an input to this function.
    """
    if not p.parse_ok:
        return "parse_error"
    if not p.raw_present:
        return "not_reported"
    if p.has_reported_categorical and p.has_reported_continuous:
        return "reported_both"
    if p.has_reported_categorical:
        return "reported_categorical_only"
    if p.has_reported_continuous:
        return "reported_continuous_only"
    if p.is_participant_count is True and (p.n_unknown or 0.0) > 0.0:
        return "explicit_unknown_only"
    return "uninformative"


def derive_age_outcomes(p: ParsedAgeTrial, status: str) -> dict[str, Optional[bool]]:
    """Boolean reporting outcomes; all None when status == 'parse_error'."""
    if status == "parse_error":
        return {
            "reported_age_any": None,
            "reported_categorical": None,
            "reported_continuous": None,
        }
    return {
        "reported_age_any": status in (
            "reported_both",
            "reported_categorical_only",
            "reported_continuous_only",
        ),
        "reported_categorical": status in ("reported_both", "reported_categorical_only"),
        "reported_continuous": status in ("reported_both", "reported_continuous_only"),
    }
