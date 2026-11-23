"""Deterministic parser for ClinicalTrials.gov outcome analysis populations.

READS   Raw ClinicalTrials.gov API v2 study dicts (resultsSection.outcomeMeasuresModule
        and resultsSection.participantFlowModule), or retained raw outcome records
        from select_raw_outcome_populations().
WRITES  Nothing itself. Emits typed dataclasses (ParsedOutcomePopulation and
        TrialAnalysisPopulations) for downstream trial shards and outcome indices.

Design invariants (mirroring src/sex_gender_parser.py and src/sex_gender_table.py):
  1. Nine disjoint, non-collapsing PopulationClass labels:
       itt, modified_itt, full_analysis_set, per_protocol, evaluable,
       safety, other_stated, not_stated, ambiguous_multi.
  2. Verbatim preservation: population_description_raw (also exposed via
     raw_population_description) is stored byte-for-byte as received;
     Unicode normalization runs only on a transient copy for rule matching.
  3. Two-tier deterministic vocabulary with character-span masking:
     - Tier 0 (placeholder) catches pure placeholders ("N/A", "Not specified")
       as not_stated (not_stated_reason="placeholder_token"), distinct from
       field_absent.
     - Tier 1 (named_label) scans all six specific classes in fixed order;
       compound labels like "Modified Intent-to-Treat" or "Safety evaluable"
       mask their matched span for the subsumed class ("itt", "evaluable") so
       single-class compound labels do not falsely trigger ambiguous_multi,
       while genuinely distinct co-occurring labels ("ITT and mITT", "ITT and
       Per-Protocol") still classify as ambiguous_multi.
     - Tier 2 (definitional) runs ONLY when Tier 1 matched zero classes, so
       definitional prose attached to a named set ("Full Analysis Set (FAS):
       all randomized patients...") never triggers false ambiguous_multi.
  4. Strict separation of denominator units:
     - "Participants" denominators populate overall_participants_analyzed and
       is_participant_count=True (denom_unit_kind="PARTICIPANTS" or "BOTH").
     - Non-participant units (e.g. "Eyes", "Lesions", COUNT_OF_UNITS) populate
       overall_units_analyzed and units_analyzed_type and NEVER leak into
       overall_participants_analyzed.
  5. Strict absence semantics: missing, empty, or "NA" counts remain None with
     an explicit denom_status / arm_gap_reason; they are NEVER coerced to 0.
  6. Strict separation of attrition gap from population classification:
     arm_started_minus_analysed = started_participants - analyzed_participants
     is stored strictly for audit (including negative values without clamping)
     and has NO code path into classify_population_description().

DEPENDS  pandas (requirements.txt: pandas>=2.2.0), imported lazily inside
         parse_outcomes_frame(), the module's only tabular helper. Importing
         this module does not import pandas (its module-level mention is
         under TYPE_CHECKING, for parse_outcomes_frame's return annotation);
         calling parse_outcomes_frame() where pandas is not installed raises
         ImportError. Every import executed at module load is from the Python
         standard library. CI runs the tests on Python 3.11
         (.github/workflows/ci.yml).

Audit holdout (in_audit). This module does no I/O: it reads only the objects
it is handed. A trial whose ``in_audit`` is true in the site's
data/outcomes/index.json must not be parsed, retained or recorded. Which
entry point guards that:

  Enforce when the record carries an ``in_audit`` key (raise
  PopulationAuditHoldoutError; see _refuse_audit_holdout_record):
      parse_study_outcomes, parse_outcomes_frame (per study),
      select_raw_outcome_populations, build_outcomes_from_raw.
    A record that carries ``in_audit`` is refused when the value is True,
    when it is not a bool, or when the record's nct_id is not exactly
    ``^NCT[0-9]{8}$``. A bare CT.gov API v2 study dict carries no
    ``in_audit``; for it these four rely on their caller, as below.
  Rely on the caller (the input carries no ``in_audit`` and no entry point
  receives an audit id set):
      parse_error_trial_outcomes (an nct_id), parse_outcome_measure,
      extract_flow_started and align_flow_and_outcome_groups (fragments of
      one trial with no trial id), classify_population_description,
      normalize_population_text and population_audit (description text),
      structural_checks (parsed outputs, which carry no ``in_audit``).
  Callers today: none in code. The only importer in this repository is
  tests/test_analysis_population_parser.py. PR #97's outcomes-shard builder
  (scripts/build_outcomes_shards.py --population-parsed, on its own branch)
  reads this module's serialized output from a file without importing it;
  whoever writes that file must leave holdout trials out of it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable, Literal, Sequence

if TYPE_CHECKING:  # annotation only; pandas is imported lazily at run time
    import pandas as pd

__version__: str = "1.0.0"
PARSER_RULES_VERSION: str = "analysis_pop@2026-11-20.v1"
POPULATION_PARSER_VERSION: str = PARSER_RULES_VERSION

PopulationClass = Literal[
    "itt",
    "modified_itt",
    "full_analysis_set",
    "per_protocol",
    "evaluable",
    "safety",
    "other_stated",
    "not_stated",
    "ambiguous_multi",
]

MatchTier = Literal["placeholder", "named_label", "definitional", "none"]
NotStatedReason = Literal["field_absent", "placeholder_token"]
DenomSource = Literal["measure", "class_unanimous", "none"]
DenomUnitKind = Literal["PARTICIPANTS", "COUNT_OF_UNITS", "BOTH", "NONE"]

POPULATION_TAXONOMY: tuple[PopulationClass, ...] = (
    "itt",
    "modified_itt",
    "full_analysis_set",
    "per_protocol",
    "evaluable",
    "safety",
    "other_stated",
    "not_stated",
    "ambiguous_multi",
)

_TAXONOMY_ORDER: dict[str, int] = {
    cls: idx for idx, cls in enumerate(POPULATION_TAXONOMY)
}

DENOM_STATUS_LEVELS: tuple[str, ...] = (
    "present",                          # valid numeric participant count(s) parsed
    "not_posted",                       # reportingStatus == "NOT_POSTED"
    "no_denoms_array",                  # denoms absent/empty on both measure and classes
    "units_only_no_participant_denom",  # non-participant units denom exists; no "Participants" denom
    "empty_counts",                     # "Participants" denom has counts == [] (incl. stripped search records)
    "non_numeric_or_na",                # counts exist, all values "NA"/""/unparseable
    "partial_arm_counts",               # >=1 non-total arm has numeric count and >=1 non-total arm is None/NA
    "class_level_only_varying",         # measure-level denoms absent; classes[].denoms vary across rows/timepoints
)

ARM_GAP_REASONS: tuple[str, ...] = (
    "computed",                         # started_participants - analyzed_participants computed
    "no_participant_flow",              # resultsSection.participantFlowModule absent
    "no_started_milestone",             # periods[0] lacks a STARTED milestone
    "started_count_missing_or_na",      # matched FG achievement numSubjects is missing/"NA"
    "analyzed_count_missing",           # OG arm analyzed_participants is None
    "total_group_not_arm",              # OG group is a Total/Overall summary column, not an arm
    "duplicate_group_titles",           # duplicate normalized arm titles in FG or OG prevent 1:1 join
    "group_count_or_title_mismatch",    # OG arm cannot be unambiguously aligned 1:1 to an FG arm
)

AGGREGATION_METHODS: tuple[str, ...] = (
    "single_group",                     # 1 OG group
    "sum_disjoint_arms",                # >=2 non-total OG arms summed (PRS does NOT auto-add Total in outcomes)
    "explicit_total_group_sumcheck",    # 1 Total-titled OG group == sum(other OG groups)
    "explicit_total_group_unverified",  # 1 Total-titled OG group != sum(other OG groups); flagged
    "pooled_group_sumcheck",            # >=3 OG groups where last pooled-titled group == sum(preceding arms)
    "none",                             # overall_participants_analyzed is None
)


# ---------------------------------------------------------------------------
# 0. Audit holdout refusal (in_audit)
# ---------------------------------------------------------------------------

# The only nct_id shape an enforcing entry point accepts. fullmatch (not
# match + "$", which also accepts a trailing newline), ASCII [0-9] (not \d,
# which also matches non-ASCII digits), no IGNORECASE, and no strip(): an id
# that needs normalising to look canonical could be a holdout id in disguise,
# so it is refused rather than repaired.
_CANONICAL_NCT_ID_RE: re.Pattern[str] = re.compile(r"NCT[0-9]{8}")

HOLDOUT_REFUSAL_REASONS: tuple[str, ...] = (
    "in_audit_true",       # the record says it is an audit holdout trial
    "in_audit_not_bool",   # in_audit is present but is not exactly True/False
    "nct_id_malformed",    # in_audit is False but the nct_id is not ^NCT[0-9]{8}$
)


class PopulationAuditHoldoutError(Exception):
    """Raised when an entry point is handed a record it must not parse.

    ``reason`` is one of HOLDOUT_REFUSAL_REASONS. Deliberately NOT a
    ValueError subclass: an ``except ValueError`` written for malformed
    registry input must not swallow a holdout refusal. Messages name the
    entry point and the value's type, never the nct_id or the description
    text, so the refusal itself records nothing about a holdout trial.
    """

    def __init__(self, reason: str, message: str) -> None:
        if reason not in HOLDOUT_REFUSAL_REASONS:
            raise ValueError(f"unknown holdout refusal reason: {reason!r}")
        super().__init__(f"[{reason}] {message}")
        self.reason: str = reason


def _is_canonical_nct_id(value: Any) -> bool:
    """True iff value is a str that is exactly ^NCT[0-9]{8}$ (no coercion)."""
    return isinstance(value, str) and _CANONICAL_NCT_ID_RE.fullmatch(value) is not None


def _refuse_audit_holdout_record(
    record: Any,
    where: str,
    raw_nct_id_of: Callable[[dict[str, Any]], Any],
) -> None:
    """Refuse a record that carries ``in_audit`` unless it is a canonical non-holdout.

    Method: a record with no ``in_audit`` key is passed through unchanged
    (the caller is responsible for it; see the module docstring). A record
    WITH the key is checked in this order, before anything else in it is
    read: the value must be exactly a bool (``type(v) is bool``: 0/1,
    "True"/"false", None and numpy bools are all refused, never coerced);
    True is refused; then the nct_id the entry point would stamp on its
    output, read raw by ``raw_nct_id_of`` (no str() coercion), must be
    exactly ``^NCT[0-9]{8}$``. Non-dict input is left to the entry point's
    own handling, which already records it as a parse error.
    """
    if not isinstance(record, dict) or "in_audit" not in record:
        return
    flag = record["in_audit"]
    if type(flag) is not bool:
        raise PopulationAuditHoldoutError(
            "in_audit_not_bool",
            f"{where}: in_audit must be exactly True or False, got "
            f"{type(flag).__name__}; refusing the record rather than guessing",
        )
    if flag:
        raise PopulationAuditHoldoutError(
            "in_audit_true",
            f"{where}: record is an in_audit holdout trial; it must not be "
            "parsed, retained or recorded",
        )
    raw_nct = raw_nct_id_of(record)
    if not _is_canonical_nct_id(raw_nct):
        raise PopulationAuditHoldoutError(
            "nct_id_malformed",
            f"{where}: in_audit is False but the nct_id ({type(raw_nct).__name__}) "
            "is not exactly ^NCT[0-9]{8}$ (ASCII, case-sensitive, unstripped)",
        )


# ---------------------------------------------------------------------------
# 1. Ordered deterministic regex vocabulary & VOCABULARY_SHA256
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VocabRule:
    """Single deterministic vocabulary rule for populationDescription."""

    rule_id: str
    tier: Literal["placeholder", "named_label", "definitional"]
    target_class: str
    pattern: str
    masks_for: tuple[str, ...] = ()
    forbids_pattern: str | None = None
    note: str = ""


VOCABULARY_RULES: tuple[VocabRule, ...] = (
    # Tier 0: pure placeholders -> not_stated (not_stated_reason="placeholder_token")
    VocabRule(
        rule_id="T0_PLACEHOLDER",
        tier="placeholder",
        target_class="not_stated",
        pattern=(
            r"^(?:n/?a|not\s+(?:applicable|available|specified|stated|reported|"
            r"provided|given|known|determined|collected|defined)|"
            r"none(?:\s+specified)?|unknown|unspecified|unreported|pending|tbd|"
            r"[-.?]+)$"
        ),
        note="Pure placeholder strings map to not_stated with placeholder_token reason.",
    ),
    # Tier 1: named analysis-population labels (fixed evaluation order with span masking)
    VocabRule(
        rule_id="T1_MITT",
        tier="named_label",
        target_class="modified_itt",
        pattern=(
            r"\b(?:modified\s+intent(?:ion)?[- ]to[- ]treat|"
            r"modified\s+itt|m[- ]?itt|mod[- ]?itt)\b"
        ),
        masks_for=("itt",),
        note=(
            "Masks its matched span before T1_ITT runs so embedded 'intent-to-treat' "
            "does not double-fire itt, while an independent 'ITT and mITT' still "
            "fires both -> ambiguous_multi."
        ),
    ),
    VocabRule(
        rule_id="T1_ITT",
        tier="named_label",
        target_class="itt",
        pattern=r"(?<!\bnon[- ])(?<!\bnot\s)\b(?:intent(?:ion)?[- ]to[- ]treat|itt|ittp)\b",
        note="Named intention-to-treat population; guards against 'non-ITT'/'not ITT'.",
    ),
    VocabRule(
        rule_id="T1_FAS",
        tier="named_label",
        target_class="full_analysis_set",
        pattern=(
            r"\b(?:full[- ]analysis\s+(?:set|population)|"
            r"fas(?!\s*[-/]?\s*(?:ligand|receptor|l\b|pathway|expression|antigen|"
            r"positive|negative|gene|signaling|mediated|apoptotic)))\b"
        ),
        note="Full Analysis Set; lookahead guards CD95/Fas biology terminology.",
    ),
    VocabRule(
        rule_id="T1_PP",
        tier="named_label",
        target_class="per_protocol",
        pattern=(
            r"(?<!\bnon[- ])(?<!\bnot\s)\b(?:per[- ]protocol|according[- ]to[- ]protocol|"
            r"protocol[- ]compliant|"
            r"(?:pp|pps|ppp)\s+(?:population|set|analysis|cohort|sample|group|"
            r"participants|patients|subjects)|"
            r"atp\s+(?:population|set|cohort|analysis))\b|^(?:pp|pps|ppp)$"
        ),
        note="Per-protocol population; guards against disease acronyms (e.g. PPP/PPS).",
    ),
    VocabRule(
        rule_id="T1_SAFETY",
        tier="named_label",
        target_class="safety",
        pattern=(
            r"\b(?:safety[- ](?:evaluable|analysis\s+set|analysis\s+population|"
            r"population|set|cohort|sample|subjects|patients|participants)|"
            r"saf\s+(?:population|set)|"
            r"as[- ]treated(?:\s+(?:population|set|analysis|cohort))?)\b|^safety$"
        ),
        masks_for=("evaluable",),
        note=(
            "Safety / as-treated population; masks 'safety evaluable' before "
            "T1_EVALUABLE and ignores timepoint phrases like 'safety follow-up'."
        ),
    ),
    VocabRule(
        rule_id="T1_EVALUABLE",
        tier="named_label",
        target_class="evaluable",
        pattern=(
            r"(?<!\bnon[- ])(?<!\bnot\s)\b(?:(?:efficacy|response|clinically|clinical|"
            r"tumor|tumour|radiographically|radiologic|immunogenicity|biomarker|"
            r"pk|pd|pharmacokinetic|pharmacodynamic)[- ]+)?"
            r"(?:evaluable|assessable)\b"
        ),
        note="Evaluable/assessable population; guards against 'non-evaluable'/'not evaluable'.",
    ),
    # Tier 2: definitional prose (evaluated ONLY when Tier 1 matches 0 classes)
    VocabRule(
        rule_id="T2_DEF_MITT",
        tier="definitional",
        target_class="modified_itt",
        pattern=(
            r"\ball\s+(?:randomized|randomised)\s+(?:participants|patients|subjects|individuals)\b"
            r".*\b(?:who\s+(?:received|took|had)\s+(?:at\s+least\s+(?:one|1)|>=?\s*1)|"
            r"with\s+(?:at\s+least\s+(?:one|1)|>=?\s*1)\s+post[- ]?baseline)"
        ),
        note="Definitional modified ITT: all randomized who received >=1 dose or post-baseline.",
    ),
    VocabRule(
        rule_id="T2_DEF_ITT",
        tier="definitional",
        target_class="itt",
        pattern=(
            r"^\s*all\s+(?:randomized|randomised|assigned)\s+"
            r"(?:participants|patients|subjects|individuals)"
            r"(?:\s+in\s+the\s+(?:study|trial))?\s*\.?\s*$"
        ),
        forbids_pattern=(
            r"\b(?:who|with|without|excluding|except|completing|completed|"
            r"received|treated|evaluable|having)\b"
        ),
        note="Unrestricted all-randomized definition; exclusions prevent pure ITT match.",
    ),
    VocabRule(
        rule_id="T2_DEF_PP",
        tier="definitional",
        target_class="per_protocol",
        pattern=(
            r"\b(?:without\s+(?:any\s+)?(?:major|important|significant)\s+"
            r"protocol\s+(?:deviations?|violations?)|"
            r"completed\s+(?:the\s+)?(?:study|treatment)\s+without\s+"
            r"protocol\s+(?:deviations?|violations?))\b"
        ),
        note="Definitional per-protocol prose without major protocol deviations.",
    ),
    VocabRule(
        rule_id="T2_DEF_SAFETY",
        tier="definitional",
        target_class="safety",
        pattern=(
            r"\ball\s+(?:treated\s+(?:participants|patients|subjects)|"
            r"(?:participants|patients|subjects)\s+who\s+(?:received|took)\s+"
            r"(?:at\s+least\s+(?:one|1)|>=?\s*1|any)\s+"
            r"(?:dose|administration|infusion|study\s+(?:drug|medication|treatment|intervention)))\b"
        ),
        forbids_pattern=r"\b(?:randomized|randomised)\b",
        note="Definitional safety population (all treated / >=1 dose without randomization qualifier).",
    ),
    VocabRule(
        rule_id="T2_DEF_EVALUABLE",
        tier="definitional",
        target_class="evaluable",
        pattern=(
            r"\b(?:participants|patients|subjects)\s+(?:with|having|who\s+had)\s+"
            r"(?:a\s+)?(?:valid\s+)?baseline\s+and\s+"
            r"(?:at\s+least\s+(?:one|1)|>=?\s*1)\s+post[- ]?baseline\b"
        ),
        forbids_pattern=r"\b(?:randomized|randomised)\b",
        note="Definitional evaluable population (baseline + >=1 post-baseline without randomization qualifier).",
    ),
)


def compute_vocabulary_sha256(rules: Sequence[VocabRule] = VOCABULARY_RULES) -> str:
    """Deterministic SHA-256 digest of taxonomy + ordered vocabulary rules."""
    payload = {
        "taxonomy": list(POPULATION_TAXONOMY),
        "rules": [
            [
                idx,
                r.rule_id,
                r.tier,
                r.target_class,
                r.pattern,
                list(r.masks_for),
                r.forbids_pattern,
            ]
            for idx, r in enumerate(rules)
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


VOCABULARY_SHA256: str = compute_vocabulary_sha256(VOCABULARY_RULES)

# Pre-compiled patterns for fast, deterministic evaluation
_COMPILED_PATTERNS: dict[str, re.Pattern[str]] = {
    r.rule_id: re.compile(r.pattern, re.IGNORECASE) for r in VOCABULARY_RULES
}
_COMPILED_FORBIDS: dict[str, re.Pattern[str]] = {
    r.rule_id: re.compile(r.forbids_pattern, re.IGNORECASE)
    for r in VOCABULARY_RULES
    if r.forbids_pattern is not None
}

_ZERO_WIDTH_RE: re.Pattern[str] = re.compile(r"[\u200b\u200c\u200d\ufeff]")
_UNICODE_DASH_RE: re.Pattern[str] = re.compile(r"[\u2010-\u2015\u2212]")
_WHITESPACE_RE: re.Pattern[str] = re.compile(r"\s+")

# Audit-only detector for statements declaring no analysis took place.
# NEVER alters population_class.
_DECLARED_NOT_ANALYZED_RE: re.Pattern[str] = re.compile(
    r"\b(?:not\s+analy[sz]ed|no\s+(?:participants?|patients?|subjects?)\s+"
    r"(?:were\s+|was\s+)?analy[sz]ed|"
    r"0\s+(?:participants?|patients?|subjects?)\s+analy[sz]ed|"
    r"not\s+evaluated\s+due\s+to\s+(?:early\s+)?termination|"
    r"no\s+data\s+(?:collected|analyzed|analysed))\b",
    re.IGNORECASE,
)

# Group-title classifiers for CT.gov outcome groups (OG) and flow groups (FG).
# Unlike baselineCharacteristicsModule (where PRS auto-adds a Total BG column),
# outcomeMeasuresModule does NOT automatically add a Total OG column, though
# sponsors occasionally create one manually.
_TOTAL_GROUP_TITLE_RE: re.Pattern[str] = re.compile(
    r"^(?:total|overall|all\s+(?:groups?|arms?|participants?|patients?|subjects?|"
    r"combined|cohorts?)|combined(?:\s+(?:total|groups?|arms?))?)$",
    re.IGNORECASE,
)
_POOLED_GROUP_TITLE_RE: re.Pattern[str] = re.compile(
    r"\b(?:pooled|combined|all\s+(?:doses?|active|treated|cohorts?|arms?|groups?)|total|overall)\b",
    re.IGNORECASE,
)
_GENERIC_NUMBERED_ARM_RE: re.Pattern[str] = re.compile(
    r"^(?:arm|group)\s*(\d+)$",
    re.IGNORECASE,
)
_NON_ALNUM_GROUP_RE: re.Pattern[str] = re.compile(r"[^a-z0-9\- ]+")


# ---------------------------------------------------------------------------
# 2. Typed Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassificationResult:
    """Output of classify_population_description()."""

    population_description_raw: str | None
    population_class: PopulationClass
    match_tier: MatchTier
    matched_classes: list[str]
    matched_rule_ids: list[str]
    not_stated_reason: NotStatedReason | None
    declared_not_analyzed: bool

    @property
    def raw_population_description(self) -> str | None:
        """Alias preserving verbatim input string."""
        return self.population_description_raw


@dataclass(frozen=True)
class FlowGroupStarted:
    """Per-group STARTED counts extracted from participantFlowModule.periods[0]."""

    flow_group_id: str
    title: str | None
    is_total_group: bool
    started_participants: float | None
    started_units: float | None


@dataclass
class ArmAnalyzedRecord:
    """Per-outcome-group analyzed counts and strictly separate flow attrition gap."""

    outcome_group_id: str
    title: str | None
    is_total_group: bool
    analyzed_participants: float | None
    analyzed_units: float | None
    matched_flow_group_id: str | None
    started_participants: float | None
    arm_started_minus_analysed: float | None
    arm_gap_reason: str
    flag_analyzed_exceeds_started: bool = False


@dataclass
class ParsedOutcomePopulation:
    """Per-outcome analysis population, denominators, and attrition audit fields."""

    outcome_index: int
    outcome_type: str | None
    measure_title: str | None
    time_frame: str | None
    reporting_status: str | None
    param_type: str | None
    unit_of_measure: str | None
    population_description_raw: str | None
    population_class: PopulationClass | None
    match_tier: MatchTier | None
    matched_classes: list[str] = field(default_factory=list)
    matched_rule_ids: list[str] = field(default_factory=list)
    not_stated_reason: NotStatedReason | None = None
    declared_not_analyzed: bool = False
    denom_source: DenomSource = "none"
    denom_status: str = "no_denoms_array"
    denom_unit_kind: DenomUnitKind = "NONE"
    overall_participants_analyzed: float | None = None
    aggregation_method: str = "none"
    is_participant_count: bool = False
    units_analyzed_type: str | None = None
    units_denom_status: str = "no_denoms_array"
    overall_units_analyzed: float | None = None
    arms: list[ArmAnalyzedRecord] = field(default_factory=list)
    overall_started_participants: float | None = None
    overall_started_minus_analyzed: float | None = None
    enrollment_minus_analyzed: float | None = None
    flag_positive_attrition_gap: bool = False
    flag_analyzed_exceeds_started: bool = False
    flag_exceeds_enrollment: bool = False
    flag_total_group_mismatch: bool = False
    flag_partial_arm_counts: bool = False
    flag_class_varying_denoms: bool = False
    class_denom_min: float | None = None
    class_denom_max: float | None = None
    parse_ok: bool = True
    parse_error_reason: str | None = None
    parser_rules_version: str = PARSER_RULES_VERSION
    vocabulary_sha256: str = VOCABULARY_SHA256

    @property
    def raw_population_description(self) -> str | None:
        """Alias preserving verbatim populationDescription string."""
        return self.population_description_raw

    def to_dict(self) -> dict[str, Any]:
        """Serialize to plain JSON-compatible dict."""
        return asdict(self)


@dataclass
class TrialAnalysisPopulations:
    """Trial-level container of parsed outcome populations and primary rollups."""

    nct_id: str | None
    snapshot_date: str | None
    enrollment: float | None
    has_results_section: bool
    has_participant_flow: bool
    n_outcomes_total: int
    n_primary_outcomes: int
    outcomes: list[ParsedOutcomePopulation]
    primary_population_classes: list[str]
    parse_ok: bool = True
    parser_rules_version: str = PARSER_RULES_VERSION
    vocabulary_sha256: str = VOCABULARY_SHA256

    def to_dict(self) -> dict[str, Any]:
        """Serialize to plain JSON-compatible dict."""
        return asdict(self)


# ---------------------------------------------------------------------------
# 3. Text normalization & pure description classification
# ---------------------------------------------------------------------------


def normalize_population_text(raw: str | None) -> str | None:
    """Return a matching-only normalized copy of raw; never mutates raw.

    Removes zero-width characters (\\u200b/\\u200c/\\u200d/\\ufeff), maps
    Unicode dashes [\\u2010-\\u2015\\u2212] to ASCII '-', and collapses
    whitespace. Returns None iff raw is None.

    Audit holdout: RELIES ON ITS CALLER. It receives one description string
    with no trial id and no ``in_audit``, so it cannot refuse a holdout trial;
    the caller must not pass one. Callers today:
    classify_population_description in this module; none elsewhere in code (see
    the module docstring).
    """
    if raw is None:
        return None
    text = _ZERO_WIDTH_RE.sub("", str(raw))
    text = _UNICODE_DASH_RE.sub("-", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def _mask_spans(text: str, spans: Sequence[tuple[int, int]]) -> str:
    """Replace each [start, end) span in text with ASCII spaces of equal length."""
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        for pos in range(start, end):
            chars[pos] = " "
    return "".join(chars)


def classify_population_description(raw: str | None) -> ClassificationResult:
    """Deterministically classify a single populationDescription string.

    Takes ONLY raw: str | None — has zero access to counts, flow, enrollment,
    or attrition gaps. Preserves raw verbatim in population_description_raw.

    Audit holdout: RELIES ON ITS CALLER. It receives one description string
    with no trial id and no ``in_audit``, so it cannot refuse a holdout trial;
    the caller must not pass one. Callers today: parse_outcome_measure and
    population_audit in this module; none elsewhere in code (see the module
    docstring).
    """
    if raw is None:
        return ClassificationResult(
            population_description_raw=None,
            population_class="not_stated",
            match_tier="none",
            matched_classes=[],
            matched_rule_ids=[],
            not_stated_reason="field_absent",
            declared_not_analyzed=False,
        )

    raw_str = raw if isinstance(raw, str) else str(raw)
    norm = normalize_population_text(raw_str)
    if norm is None or norm == "":
        return ClassificationResult(
            population_description_raw=raw_str,
            population_class="not_stated",
            match_tier="none",
            matched_classes=[],
            matched_rule_ids=[],
            not_stated_reason="field_absent",
            declared_not_analyzed=False,
        )

    declared_not_analyzed = bool(_DECLARED_NOT_ANALYZED_RE.search(norm))

    # Tier 0: pure placeholder tokens
    for rule in VOCABULARY_RULES:
        if rule.tier != "placeholder":
            continue
        if _COMPILED_PATTERNS[rule.rule_id].search(norm):
            return ClassificationResult(
                population_description_raw=raw_str,
                population_class="not_stated",
                match_tier="placeholder",
                matched_classes=[],
                matched_rule_ids=[rule.rule_id],
                not_stated_reason="placeholder_token",
                declared_not_analyzed=declared_not_analyzed,
            )

    # Tier 1: named analysis-population labels with per-target span masking
    masked_by_target: dict[str, list[tuple[int, int]]] = {}
    t1_classes: list[str] = []
    t1_rule_ids: list[str] = []

    for rule in VOCABULARY_RULES:
        if rule.tier != "named_label":
            continue
        view = _mask_spans(norm, masked_by_target.get(rule.target_class, ()))
        forbids = _COMPILED_FORBIDS.get(rule.rule_id)
        if forbids is not None and forbids.search(view):
            continue
        matches = list(_COMPILED_PATTERNS[rule.rule_id].finditer(view))
        if matches:
            if rule.target_class not in t1_classes:
                t1_classes.append(rule.target_class)
            t1_rule_ids.append(rule.rule_id)
            if rule.masks_for:
                spans = [m.span() for m in matches]
                for masked_target in rule.masks_for:
                    masked_by_target.setdefault(masked_target, []).extend(spans)

    if t1_classes:
        ordered_classes = sorted(t1_classes, key=lambda c: _TAXONOMY_ORDER[c])
        final_class: PopulationClass = (
            ordered_classes[0]  # type: ignore[assignment]
            if len(ordered_classes) == 1
            else "ambiguous_multi"
        )
        return ClassificationResult(
            population_description_raw=raw_str,
            population_class=final_class,
            match_tier="named_label",
            matched_classes=ordered_classes,
            matched_rule_ids=t1_rule_ids,
            not_stated_reason=None,
            declared_not_analyzed=declared_not_analyzed,
        )

    # Tier 2: definitional prose (ONLY when Tier 1 matched zero classes)
    t2_classes: list[str] = []
    t2_rule_ids: list[str] = []
    for rule in VOCABULARY_RULES:
        if rule.tier != "definitional":
            continue
        forbids = _COMPILED_FORBIDS.get(rule.rule_id)
        if forbids is not None and forbids.search(norm):
            continue
        if _COMPILED_PATTERNS[rule.rule_id].search(norm):
            if rule.target_class not in t2_classes:
                t2_classes.append(rule.target_class)
            t2_rule_ids.append(rule.rule_id)

    if t2_classes:
        ordered_t2 = sorted(t2_classes, key=lambda c: _TAXONOMY_ORDER[c])
        final_t2_class: PopulationClass = (
            ordered_t2[0]  # type: ignore[assignment]
            if len(ordered_t2) == 1
            else "ambiguous_multi"
        )
        return ClassificationResult(
            population_description_raw=raw_str,
            population_class=final_t2_class,
            match_tier="definitional",
            matched_classes=ordered_t2,
            matched_rule_ids=t2_rule_ids,
            not_stated_reason=None,
            declared_not_analyzed=declared_not_analyzed,
        )

    return ClassificationResult(
        population_description_raw=raw_str,
        population_class="other_stated",
        match_tier="none",
        matched_classes=[],
        matched_rule_ids=[],
        not_stated_reason=None,
        declared_not_analyzed=declared_not_analyzed,
    )


# ---------------------------------------------------------------------------
# 4. Numeric parsing, participant-flow extraction, and strict 1:1 alignment
# ---------------------------------------------------------------------------


def _parse_number(value: Any) -> float | None:
    """Strictly parse a numeric count; missing/blank/'NA'/non-finite -> None.

    NEVER coerces missing or unparseable values to 0.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return None if (math.isnan(f) or math.isinf(f)) else f
    s = str(value).strip()
    if not s or s.upper() in ("NA", "N/A", "NONE", "NULL", "NAN"):
        return None
    try:
        f = float(s.replace(",", ""))
    except ValueError:
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _norm_group_title(title: str | None) -> str:
    """Normalized arm/group title for strict 1:1 FG<->OG title matching."""
    if title is None:
        return ""
    s = _ZERO_WIDTH_RE.sub("", str(title))
    s = _UNICODE_DASH_RE.sub("-", s).lower()
    s = _NON_ALNUM_GROUP_RE.sub(" ", s)
    return _WHITESPACE_RE.sub(" ", s).strip()


def _is_total_group_title(title: str | None) -> bool:
    norm = _norm_group_title(title)
    return bool(norm and _TOTAL_GROUP_TITLE_RE.match(norm))


def _is_pooled_group_title(title: str | None) -> bool:
    norm = _norm_group_title(title)
    return bool(norm and _POOLED_GROUP_TITLE_RE.search(norm))


def extract_flow_started(
    participant_flow_module: dict[str, Any] | None,
) -> tuple[list[FlowGroupStarted], str]:
    """Extract per-group STARTED counts from participantFlowModule.periods[0].

    Returns (flow_groups, status) where status is one of:
      - "ok"
      - "no_participant_flow"
      - "no_started_milestone"

    Audit holdout: RELIES ON ITS CALLER. It receives one trial's
    participantFlowModule, which carries no trial id and no ``in_audit``, so it
    cannot refuse a holdout trial; the caller must not pass one. Callers today:
    _parse_outcomes_payload (behind parse_study_outcomes and
    build_outcomes_from_raw) in this module; none elsewhere in code (see the
    module docstring).
    """
    if not isinstance(participant_flow_module, dict) or not participant_flow_module:
        return [], "no_participant_flow"

    periods = participant_flow_module.get("periods")
    if not isinstance(periods, list) or not periods or not isinstance(periods[0], dict):
        return [], "no_started_milestone"

    milestones = periods[0].get("milestones")
    started_milestone: dict[str, Any] | None = None
    if isinstance(milestones, list):
        for ms in milestones:
            if not isinstance(ms, dict):
                continue
            if str(ms.get("type") or "").strip().upper() == "STARTED":
                started_milestone = ms
                break

    if started_milestone is None:
        return [], "no_started_milestone"

    ach_by_id: dict[str, dict[str, Any]] = {}
    ach_order: list[str] = []
    for ach in started_milestone.get("achievements") or []:
        if not isinstance(ach, dict):
            continue
        gid = ach.get("groupId") or ach.get("id")
        if gid is None:
            continue
        gid_str = str(gid)
        if gid_str not in ach_by_id:
            ach_order.append(gid_str)
        ach_by_id[gid_str] = ach

    raw_groups = participant_flow_module.get("groups")
    groups_list: list[dict[str, Any]] = (
        [g for g in raw_groups if isinstance(g, dict)]
        if isinstance(raw_groups, list) and raw_groups
        else [{"id": gid, "title": None} for gid in ach_order]
    )

    out: list[FlowGroupStarted] = []
    for grp in groups_list:
        gid = grp.get("id") or grp.get("groupId")
        if gid is None:
            continue
        gid_str = str(gid)
        title_raw = grp.get("title")
        title = None if title_raw is None else str(title_raw)
        ach = ach_by_id.get(gid_str, {})
        out.append(
            FlowGroupStarted(
                flow_group_id=gid_str,
                title=title,
                is_total_group=_is_total_group_title(title),
                started_participants=_parse_number(ach.get("numSubjects")),
                started_units=_parse_number(ach.get("numUnits")),
            )
        )
    return out, "ok"


def _titles_allow_generic_positional(
    fg_title: str | None,
    og_title: str | None,
    arm_count: int,
) -> bool:
    """Return True iff (fg_title, og_title) can safely join positionally.

    Allows positional fallback ONLY when titles match exactly, or when both
    sides are generic numbered placeholders with the same index (e.g. 'Arm 1'
    and 'Group 1'), or when blank on either side in a non-conflicting generic
    context. Never guesses across distinct non-matching arm names like
    'Drug A' vs 'Cohort X'.
    """
    fg_norm = _norm_group_title(fg_title)
    og_norm = _norm_group_title(og_title)
    if fg_norm == og_norm:
        return True
    if not fg_norm or not og_norm:
        return True
    fg_m = _GENERIC_NUMBERED_ARM_RE.match(fg_norm)
    og_m = _GENERIC_NUMBERED_ARM_RE.match(og_norm)
    if fg_m and og_m and fg_m.group(1) == og_m.group(1):
        return True
    if arm_count == 1 and (fg_m is not None or og_m is not None):
        return True
    return False


def align_flow_and_outcome_groups(
    flow_groups: list[FlowGroupStarted],
    outcome_groups: list[dict[str, Any]],
) -> tuple[dict[str, FlowGroupStarted], dict[str, str]]:
    """Strictly align non-total outcome groups (OG) to non-total flow groups (FG).

    Matching rules:
      1. Total/Overall OG groups are never aligned (reason='total_group_not_arm').
      2. Duplicate non-empty normalized arm titles in FG or OG block 1:1 join
         (reason='duplicate_group_titles').
      3. Unique normalized title match joins OG -> FG even if reordered.
      4. If all remaining arms have equal cardinality and every pair passes
         _titles_allow_generic_positional (e.g. 'Arm 1' <-> 'Group 1'), join
         positionally. Distinct non-matching titles ('Drug A' vs 'Cohort X')
         are NEVER guessed (reason='group_count_or_title_mismatch').

    Audit holdout: RELIES ON ITS CALLER. It receives group lists from one trial
    with no trial id and no ``in_audit``, so it cannot refuse a holdout trial;
    the caller must not pass one. Callers today: parse_outcome_measure in this
    module; none elsewhere in code (see the module docstring).
    """
    matched: dict[str, FlowGroupStarted] = {}
    reasons: dict[str, str] = {}

    fg_arms: list[FlowGroupStarted] = [fg for fg in flow_groups if not fg.is_total_group]
    og_arms: list[tuple[str, str | None]] = []

    for og in outcome_groups:
        if not isinstance(og, dict):
            continue
        og_id = og.get("id") or og.get("groupId")
        if og_id is None:
            continue
        og_id_str = str(og_id)
        title_raw = og.get("title")
        title = None if title_raw is None else str(title_raw)
        if _is_total_group_title(title):
            reasons[og_id_str] = "total_group_not_arm"
        else:
            og_arms.append((og_id_str, title))

    if not og_arms:
        return matched, reasons

    if not fg_arms:
        for og_id_str, _ in og_arms:
            reasons[og_id_str] = "group_count_or_title_mismatch"
        return matched, reasons

    fg_title_counts = Counter(
        t for t in (_norm_group_title(fg.title) for fg in fg_arms) if t
    )
    og_title_counts = Counter(
        t for t in (_norm_group_title(title) for _, title in og_arms) if t
    )
    if any(c > 1 for c in fg_title_counts.values()) or any(
        c > 1 for c in og_title_counts.values()
    ):
        for og_id_str, _ in og_arms:
            reasons[og_id_str] = "duplicate_group_titles"
        return matched, reasons

    fg_by_norm: dict[str, FlowGroupStarted] = {
        _norm_group_title(fg.title): fg
        for fg in fg_arms
        if _norm_group_title(fg.title)
    }

    unmatched_og: list[tuple[str, str | None]] = []
    used_fg_ids: set[str] = set()
    for og_id_str, title in og_arms:
        norm = _norm_group_title(title)
        if norm and norm in fg_by_norm:
            fg = fg_by_norm[norm]
            matched[og_id_str] = fg
            used_fg_ids.add(fg.flow_group_id)
        else:
            unmatched_og.append((og_id_str, title))

    if unmatched_og:
        remaining_fg = [fg for fg in fg_arms if fg.flow_group_id not in used_fg_ids]
        if len(remaining_fg) == len(unmatched_og) and all(
            _titles_allow_generic_positional(fg.title, og_title, len(remaining_fg))
            for fg, (_, og_title) in zip(remaining_fg, unmatched_og)
        ):
            for fg, (og_id_str, _) in zip(remaining_fg, unmatched_og):
                matched[og_id_str] = fg
        else:
            for og_id_str, _ in unmatched_og:
                reasons[og_id_str] = "group_count_or_title_mismatch"

    return matched, reasons


# ---------------------------------------------------------------------------
# 5. Denominator extraction & aggregation (Participants vs Count of Units)
# ---------------------------------------------------------------------------


def _is_participants_unit(units: Any) -> bool:
    return isinstance(units, str) and units.strip().lower() == "participants"


def _parse_denom_counts_map(denom: dict[str, Any]) -> tuple[bool, dict[str, float | None]]:
    """Return (has_counts_entries, {groupId: float | None}) for one denom dict."""
    counts = denom.get("counts")
    if not isinstance(counts, list) or len(counts) == 0:
        return False, {}
    mapping: dict[str, float | None] = {}
    for entry in counts:
        if not isinstance(entry, dict):
            continue
        gid = entry.get("groupId") or entry.get("id")
        if gid is None:
            continue
        mapping[str(gid)] = _parse_number(entry.get("value"))
    return (len(mapping) > 0), mapping


def _aggregate_counts(
    ordered_groups: list[tuple[str, str | None, bool]],
    counts_by_id: dict[str, float | None],
) -> tuple[str, float | None, str, bool, bool]:
    """Aggregate per-group counts into an overall analyzed count.

    Returns:
      (denom_status, overall_count, aggregation_method,
       flag_total_group_mismatch, flag_partial_arm_counts)
    """
    if not ordered_groups:
        return "empty_counts", None, "none", False, False

    if len(ordered_groups) == 1:
        gid, _, _ = ordered_groups[0]
        val = counts_by_id.get(gid)
        if val is None:
            return "non_numeric_or_na", None, "none", False, False
        return "present", val, "single_group", False, False

    total_groups = [g for g in ordered_groups if g[2]]
    arm_groups = [g for g in ordered_groups if not g[2]]

    # Case 1: Exactly one explicit Total/Overall summary column plus >=1 arms
    if len(total_groups) == 1 and len(arm_groups) >= 1:
        t_gid = total_groups[0][0]
        t_val = counts_by_id.get(t_gid)
        arm_vals = [counts_by_id.get(g[0]) for g in arm_groups]
        if t_val is not None:
            if all(v is not None for v in arm_vals):
                arm_sum = sum(v for v in arm_vals if v is not None)
                if abs(t_val - arm_sum) < 1e-9:
                    return "present", t_val, "explicit_total_group_sumcheck", False, False
                return "present", t_val, "explicit_total_group_unverified", True, False
            partial = any(v is not None for v in arm_vals)
            return "present", t_val, "explicit_total_group_unverified", True, partial
        # Explicit Total is NA/missing: evaluate remaining arms below
        eval_arms = arm_groups
    else:
        eval_arms = arm_groups if arm_groups else ordered_groups

    # Case 2: >=3 arms where the final group is pooled-titled and equals sum(preceding)
    if len(eval_arms) >= 3 and _is_pooled_group_title(eval_arms[-1][1]):
        last_val = counts_by_id.get(eval_arms[-1][0])
        prev_vals = [counts_by_id.get(g[0]) for g in eval_arms[:-1]]
        if last_val is not None and all(v is not None for v in prev_vals):
            prev_sum = sum(v for v in prev_vals if v is not None)
            if abs(last_val - prev_sum) < 1e-9:
                return "present", last_val, "pooled_group_sumcheck", False, False

    # Case 3: Disjoint arms (normal PRS outcomeMeasuresModule layout)
    eval_vals = [counts_by_id.get(g[0]) for g in eval_arms]
    non_null = [v for v in eval_vals if v is not None]
    if not non_null:
        return "non_numeric_or_na", None, "none", False, False
    if len(non_null) < len(eval_vals):
        return "partial_arm_counts", None, "none", False, True
    if len(eval_vals) == 1:
        return "present", non_null[0], "single_group", False, False
    return "present", sum(non_null), "sum_disjoint_arms", False, False


def _select_denoms_from_measure(
    measure: dict[str, Any],
) -> tuple[
    DenomSource,
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[list[dict[str, Any]]],
    list[list[dict[str, Any]]],
]:
    """Collect measure-level or class-level participant & unit denom objects.

    Returns:
      (source_hint, measure_part_denoms, measure_unit_denoms,
       class_part_denoms_per_class, class_unit_denoms_per_class)
    """
    m_denoms = measure.get("denoms")
    if isinstance(m_denoms, list) and len(m_denoms) > 0:
        valid = [d for d in m_denoms if isinstance(d, dict)]
        part = [d for d in valid if _is_participants_unit(d.get("units"))]
        units = [
            d
            for d in valid
            if isinstance(d.get("units"), str)
            and d["units"].strip() != ""
            and not _is_participants_unit(d.get("units"))
        ]
        if valid:
            return "measure", part, units, [], []

    classes = measure.get("classes")
    class_part: list[list[dict[str, Any]]] = []
    class_units: list[list[dict[str, Any]]] = []
    if isinstance(classes, list):
        for cl in classes:
            if not isinstance(cl, dict):
                continue
            cd = cl.get("denoms")
            if not isinstance(cd, list) or not cd:
                continue
            valid = [d for d in cd if isinstance(d, dict)]
            part = [d for d in valid if _is_participants_unit(d.get("units"))]
            units = [
                d
                for d in valid
                if isinstance(d.get("units"), str)
                and d["units"].strip() != ""
                and not _is_participants_unit(d.get("units"))
            ]
            if part:
                class_part.append(part)
            if units:
                class_units.append(units)

    if class_part or class_units:
        return "class_unanimous", [], [], class_part, class_units
    return "none", [], [], [], []


def _extract_ordered_groups(
    measure: dict[str, Any],
    fallback_counts_maps: Sequence[dict[str, float | None]],
) -> list[tuple[str, str | None, bool]]:
    """Build ordered list of (group_id, title, is_total_group) from measure.groups."""
    raw_groups = measure.get("groups")
    out: list[tuple[str, str | None, bool]] = []
    seen: set[str] = set()
    if isinstance(raw_groups, list):
        for grp in raw_groups:
            if not isinstance(grp, dict):
                continue
            gid = grp.get("id") or grp.get("groupId")
            if gid is None:
                continue
            gid_str = str(gid)
            if gid_str in seen:
                continue
            seen.add(gid_str)
            title_raw = grp.get("title")
            title = None if title_raw is None else str(title_raw)
            out.append((gid_str, title, _is_total_group_title(title)))

    if not out:
        for cmap in fallback_counts_maps:
            for gid_str in cmap:
                if gid_str not in seen:
                    seen.add(gid_str)
                    out.append((gid_str, None, False))
    return out


def parse_outcome_measure(
    measure: dict[str, Any],
    outcome_index: int = 0,
    flow_groups: list[FlowGroupStarted] | None = None,
    flow_status: str = "no_participant_flow",
    enrollment: float | None = None,
) -> ParsedOutcomePopulation:
    """Parse one ClinicalTrials.gov outcomeMeasuresModule.outcomeMeasures entry.

    Audit holdout: RELIES ON ITS CALLER. It receives one outcome measure (and
    optionally that trial's flow groups), with no trial id and no ``in_audit``,
    so it cannot refuse a holdout trial; the caller must not pass one. Callers
    today: _parse_outcomes_payload (behind parse_study_outcomes and
    build_outcomes_from_raw) in this module; none elsewhere in code (see the
    module docstring).
    """
    if not isinstance(measure, dict):
        return ParsedOutcomePopulation(
            outcome_index=outcome_index,
            outcome_type=None,
            measure_title=None,
            time_frame=None,
            reporting_status=None,
            param_type=None,
            unit_of_measure=None,
            population_description_raw=None,
            population_class=None,
            match_tier=None,
            parse_ok=False,
            parse_error_reason="measure_not_a_dict",
        )

    outcome_type = (
        None if measure.get("type") is None else str(measure.get("type"))
    )
    measure_title = (
        None if measure.get("title") is None else str(measure.get("title"))
    )
    time_frame = (
        None if measure.get("timeFrame") is None else str(measure.get("timeFrame"))
    )
    reporting_status = (
        None
        if measure.get("reportingStatus") is None
        else str(measure.get("reportingStatus"))
    )
    param_type = (
        None if measure.get("paramType") is None else str(measure.get("paramType"))
    )
    unit_of_measure = (
        None
        if measure.get("unitOfMeasure") is None
        else str(measure.get("unitOfMeasure"))
    )

    # 1. Pure text classification (strictly independent of counts / flow / gaps)
    raw_pop = measure.get("populationDescription")
    cls = classify_population_description(
        raw_pop if (raw_pop is None or isinstance(raw_pop, str)) else str(raw_pop)
    )

    # 2. Denominator parsing (Participants vs COUNT_OF_UNITS)
    type_units_raw = measure.get("typeUnitsAnalyzed")
    units_analyzed_type: str | None = (
        str(type_units_raw)
        if type_units_raw is not None and str(type_units_raw).strip() != ""
        else None
    )

    denom_source: DenomSource = "none"
    denom_status: str = "no_denoms_array"
    overall_participants_analyzed: float | None = None
    aggregation_method: str = "none"
    is_participant_count: bool = False
    units_denom_status: str = "no_denoms_array"
    overall_units_analyzed: float | None = None
    flag_total_group_mismatch: bool = False
    flag_partial_arm_counts: bool = False
    flag_class_varying_denoms: bool = False
    class_denom_min: float | None = None
    class_denom_max: float | None = None

    part_counts_by_id: dict[str, float | None] = {}
    unit_counts_by_id: dict[str, float | None] = {}
    ordered_groups: list[tuple[str, str | None, bool]] = []

    if reporting_status is not None and reporting_status.strip().upper() == "NOT_POSTED":
        denom_status = "not_posted"
        units_denom_status = "not_posted"
        ordered_groups = _extract_ordered_groups(measure, [])
    else:
        (
            src_hint,
            m_part,
            m_units,
            c_part,
            c_units,
        ) = _select_denoms_from_measure(measure)

        # Resolve units_analyzed_type from unit denoms if typeUnitsAnalyzed absent
        if units_analyzed_type is None:
            if m_units:
                units_analyzed_type = str(m_units[0].get("units"))
            elif c_units and c_units[0]:
                units_analyzed_type = str(c_units[0][0].get("units"))

        # Parse active participant denom & unit denom depending on measure vs class level
        if src_hint == "measure":
            p_has_entries, p_map = (
                _parse_denom_counts_map(m_part[0]) if m_part else (False, {})
            )
            u_has_entries, u_map = (
                _parse_denom_counts_map(m_units[0]) if m_units else (False, {})
            )
            ordered_groups = _extract_ordered_groups(measure, [p_map, u_map])

            if m_part:
                denom_source = "measure"
                if not p_has_entries:
                    denom_status = "empty_counts"
                else:
                    part_counts_by_id = p_map
                    (
                        denom_status,
                        overall_participants_analyzed,
                        aggregation_method,
                        flag_total_group_mismatch,
                        flag_partial_arm_counts,
                    ) = _aggregate_counts(ordered_groups, part_counts_by_id)
                    is_participant_count = overall_participants_analyzed is not None
            elif m_units:
                denom_source = "none"
                denom_status = "units_only_no_participant_denom"
            else:
                denom_source = "none"
                denom_status = "no_denoms_array"

            if m_units:
                if not u_has_entries:
                    units_denom_status = "empty_counts"
                else:
                    unit_counts_by_id = u_map
                    (
                        units_denom_status,
                        overall_units_analyzed,
                        _,
                        _,
                        _,
                    ) = _aggregate_counts(ordered_groups, unit_counts_by_id)

        elif src_hint == "class_unanimous":
            # Evaluate participant denoms across classes
            parsed_class_parts: list[tuple[bool, dict[str, float | None]]] = [
                _parse_denom_counts_map(d_list[0]) for d_list in c_part
            ]
            parsed_class_units: list[tuple[bool, dict[str, float | None]]] = [
                _parse_denom_counts_map(d_list[0]) for d_list in c_units
            ]
            ordered_groups = _extract_ordered_groups(
                measure,
                [m for _, m in parsed_class_parts] + [m for _, m in parsed_class_units],
            )

            if parsed_class_parts:
                canonical_maps = [
                    tuple(sorted(m.items())) for _, m in parsed_class_parts
                ]
                all_empty = all(not has_e for has_e, _ in parsed_class_parts)
                if all_empty:
                    denom_source = "class_unanimous"
                    denom_status = "empty_counts"
                elif len(set(canonical_maps)) == 1:
                    denom_source = "class_unanimous"
                    part_counts_by_id = parsed_class_parts[0][1]
                    (
                        denom_status,
                        overall_participants_analyzed,
                        aggregation_method,
                        flag_total_group_mismatch,
                        flag_partial_arm_counts,
                    ) = _aggregate_counts(ordered_groups, part_counts_by_id)
                    is_participant_count = overall_participants_analyzed is not None
                else:
                    denom_source = "none"
                    denom_status = "class_level_only_varying"
                    flag_class_varying_denoms = True
                    class_totals: list[float] = []
                    for has_e, cmap in parsed_class_parts:
                        if not has_e:
                            continue
                        _, tot, _, _, _ = _aggregate_counts(ordered_groups, cmap)
                        if tot is not None:
                            class_totals.append(tot)
                    if class_totals:
                        class_denom_min = min(class_totals)
                        class_denom_max = max(class_totals)
            elif parsed_class_units:
                denom_source = "none"
                denom_status = "units_only_no_participant_denom"

            if parsed_class_units:
                u_canonical = [
                    tuple(sorted(m.items())) for _, m in parsed_class_units
                ]
                if all(not has_e for has_e, _ in parsed_class_units):
                    units_denom_status = "empty_counts"
                elif len(set(u_canonical)) == 1:
                    unit_counts_by_id = parsed_class_units[0][1]
                    (
                        units_denom_status,
                        overall_units_analyzed,
                        _,
                        _,
                        _,
                    ) = _aggregate_counts(ordered_groups, unit_counts_by_id)
                else:
                    units_denom_status = "class_level_only_varying"
        else:
            ordered_groups = _extract_ordered_groups(measure, [])

    # Determine DenomUnitKind (explicitly distinguishing PARTICIPANTS vs COUNT_OF_UNITS)
    has_units_signal = (
        overall_units_analyzed is not None
        or denom_status == "units_only_no_participant_denom"
        or units_denom_status
        in ("present", "partial_arm_counts", "class_level_only_varying")
        or (param_type is not None and param_type.strip().upper() == "COUNT_OF_UNITS")
    )
    if is_participant_count and (overall_units_analyzed is not None or units_denom_status == "present"):
        denom_unit_kind: DenomUnitKind = "BOTH"
    elif is_participant_count:
        denom_unit_kind = "PARTICIPANTS"
    elif has_units_signal:
        denom_unit_kind = "COUNT_OF_UNITS"
    else:
        denom_unit_kind = "NONE"

    # 3. Participant flow alignment & per-arm attrition gap (strictly separate from classification)
    raw_og_dicts: list[dict[str, Any]] = [
        {"id": gid, "title": title} for gid, title, _ in ordered_groups
    ]
    matched_fg: dict[str, FlowGroupStarted] = {}
    unmapped_reasons: dict[str, str] = {}
    if flow_status == "ok" and flow_groups is not None:
        matched_fg, unmapped_reasons = align_flow_and_outcome_groups(
            flow_groups, raw_og_dicts
        )

    arms: list[ArmAnalyzedRecord] = []
    for gid, title, is_total in ordered_groups:
        analyzed_p = part_counts_by_id.get(gid)
        analyzed_u = unit_counts_by_id.get(gid)
        if is_total:
            arms.append(
                ArmAnalyzedRecord(
                    outcome_group_id=gid,
                    title=title,
                    is_total_group=True,
                    analyzed_participants=analyzed_p,
                    analyzed_units=analyzed_u,
                    matched_flow_group_id=None,
                    started_participants=None,
                    arm_started_minus_analysed=None,
                    arm_gap_reason="total_group_not_arm",
                    flag_analyzed_exceeds_started=False,
                )
            )
            continue

        if flow_status != "ok" or flow_groups is None:
            reason = (
                flow_status
                if flow_status in ARM_GAP_REASONS
                else "no_participant_flow"
            )
            arms.append(
                ArmAnalyzedRecord(
                    outcome_group_id=gid,
                    title=title,
                    is_total_group=False,
                    analyzed_participants=analyzed_p,
                    analyzed_units=analyzed_u,
                    matched_flow_group_id=None,
                    started_participants=None,
                    arm_started_minus_analysed=None,
                    arm_gap_reason=reason,
                    flag_analyzed_exceeds_started=False,
                )
            )
            continue

        fg = matched_fg.get(gid)
        if fg is None:
            arms.append(
                ArmAnalyzedRecord(
                    outcome_group_id=gid,
                    title=title,
                    is_total_group=False,
                    analyzed_participants=analyzed_p,
                    analyzed_units=analyzed_u,
                    matched_flow_group_id=None,
                    started_participants=None,
                    arm_started_minus_analysed=None,
                    arm_gap_reason=unmapped_reasons.get(
                        gid, "group_count_or_title_mismatch"
                    ),
                    flag_analyzed_exceeds_started=False,
                )
            )
            continue

        started_p = fg.started_participants
        if started_p is None:
            arms.append(
                ArmAnalyzedRecord(
                    outcome_group_id=gid,
                    title=title,
                    is_total_group=False,
                    analyzed_participants=analyzed_p,
                    analyzed_units=analyzed_u,
                    matched_flow_group_id=fg.flow_group_id,
                    started_participants=None,
                    arm_started_minus_analysed=None,
                    arm_gap_reason="started_count_missing_or_na",
                    flag_analyzed_exceeds_started=False,
                )
            )
        elif analyzed_p is None:
            arms.append(
                ArmAnalyzedRecord(
                    outcome_group_id=gid,
                    title=title,
                    is_total_group=False,
                    analyzed_participants=None,
                    analyzed_units=analyzed_u,
                    matched_flow_group_id=fg.flow_group_id,
                    started_participants=started_p,
                    arm_started_minus_analysed=None,
                    arm_gap_reason="analyzed_count_missing",
                    flag_analyzed_exceeds_started=False,
                )
            )
        else:
            gap = started_p - analyzed_p
            arms.append(
                ArmAnalyzedRecord(
                    outcome_group_id=gid,
                    title=title,
                    is_total_group=False,
                    analyzed_participants=analyzed_p,
                    analyzed_units=analyzed_u,
                    matched_flow_group_id=fg.flow_group_id,
                    started_participants=started_p,
                    arm_started_minus_analysed=gap,
                    arm_gap_reason="computed",
                    flag_analyzed_exceeds_started=(gap < 0),
                )
            )

    # 4. Overall started & gap summaries
    overall_started_participants: float | None = None
    if flow_status == "ok" and flow_groups:
        fg_non_total = [fg for fg in flow_groups if not fg.is_total_group]
        if fg_non_total and all(fg.started_participants is not None for fg in fg_non_total):
            overall_started_participants = sum(
                fg.started_participants
                for fg in fg_non_total
                if fg.started_participants is not None
            )

    overall_started_minus_analyzed: float | None = (
        overall_started_participants - overall_participants_analyzed
        if (
            overall_started_participants is not None
            and overall_participants_analyzed is not None
        )
        else None
    )

    enr = _parse_number(enrollment)
    enrollment_minus_analyzed: float | None = (
        enr - overall_participants_analyzed
        if (enr is not None and overall_participants_analyzed is not None)
        else None
    )

    flag_positive_attrition_gap = (
        (
            overall_started_minus_analyzed is not None
            and overall_started_minus_analyzed > 0
        )
        or any(
            a.arm_started_minus_analysed is not None
            and a.arm_started_minus_analysed > 0
            for a in arms
        )
    )
    flag_analyzed_exceeds_started = (
        (
            overall_started_minus_analyzed is not None
            and overall_started_minus_analyzed < 0
        )
        or any(a.flag_analyzed_exceeds_started for a in arms)
    )
    flag_exceeds_enrollment = (
        enrollment_minus_analyzed is not None and enrollment_minus_analyzed < 0
    )

    return ParsedOutcomePopulation(
        outcome_index=outcome_index,
        outcome_type=outcome_type,
        measure_title=measure_title,
        time_frame=time_frame,
        reporting_status=reporting_status,
        param_type=param_type,
        unit_of_measure=unit_of_measure,
        population_description_raw=cls.population_description_raw,
        population_class=cls.population_class,
        match_tier=cls.match_tier,
        matched_classes=cls.matched_classes,
        matched_rule_ids=cls.matched_rule_ids,
        not_stated_reason=cls.not_stated_reason,
        declared_not_analyzed=cls.declared_not_analyzed,
        denom_source=denom_source,
        denom_status=denom_status,
        denom_unit_kind=denom_unit_kind,
        overall_participants_analyzed=overall_participants_analyzed,
        aggregation_method=aggregation_method,
        is_participant_count=is_participant_count,
        units_analyzed_type=units_analyzed_type,
        units_denom_status=units_denom_status,
        overall_units_analyzed=overall_units_analyzed,
        arms=arms,
        overall_started_participants=overall_started_participants,
        overall_started_minus_analyzed=overall_started_minus_analyzed,
        enrollment_minus_analyzed=enrollment_minus_analyzed,
        flag_positive_attrition_gap=flag_positive_attrition_gap,
        flag_analyzed_exceeds_started=flag_analyzed_exceeds_started,
        flag_exceeds_enrollment=flag_exceeds_enrollment,
        flag_total_group_mismatch=flag_total_group_mismatch,
        flag_partial_arm_counts=flag_partial_arm_counts,
        flag_class_varying_denoms=flag_class_varying_denoms,
        class_denom_min=class_denom_min,
        class_denom_max=class_denom_max,
        parse_ok=True,
        parse_error_reason=None,
        parser_rules_version=PARSER_RULES_VERSION,
        vocabulary_sha256=VOCABULARY_SHA256,
    )


# ---------------------------------------------------------------------------
# 6. Study-level entry points, raw retention & re-parse parity
# ---------------------------------------------------------------------------


def _nct_of(study: dict[str, Any]) -> str | None:
    ident = (study.get("protocolSection") or {}).get("identificationModule") or {}
    val = ident.get("nctId")
    return None if val is None else str(val)


def _raw_nct_of_study(study: dict[str, Any]) -> Any:
    """protocolSection.identificationModule.nctId exactly as stored (no str()).

    Used only by the holdout check, which must see the value before _nct_of's
    str() coercion could turn a non-string into something id-shaped. Any
    missing or non-dict level yields None, which that check refuses.
    """
    proto = study.get("protocolSection")
    ident = proto.get("identificationModule") if isinstance(proto, dict) else None
    return ident.get("nctId") if isinstance(ident, dict) else None


def _raw_nct_of_retained(raw: dict[str, Any]) -> Any:
    """Top-level ``nct_id`` of a retained raw outcome record, exactly as stored."""
    return raw.get("nct_id")


def _enrollment_of(study: dict[str, Any]) -> Any:
    return (
        ((study.get("protocolSection") or {}).get("designModule") or {})
        .get("enrollmentInfo")
        or {}
    ).get("count")


def _parse_outcomes_payload(
    nct_id: str | None,
    enrollment: Any,
    has_results_section: bool,
    participant_flow_module: dict[str, Any] | None,
    raw_measures: Any,
    snapshot_date: str | None = None,
    outcome_types: tuple[str, ...] | None = None,
) -> TrialAnalysisPopulations:
    """Shared implementation for parse_study_outcomes and build_outcomes_from_raw."""
    enr = _parse_number(enrollment)
    flow_groups, flow_status = extract_flow_started(participant_flow_module)
    has_participant_flow = flow_status != "no_participant_flow"

    allowed_types: set[str] | None = (
        {t.strip().upper() for t in outcome_types}
        if outcome_types is not None
        else None
    )

    measures_list = raw_measures if isinstance(raw_measures, list) else []
    parsed_outcomes: list[ParsedOutcomePopulation] = []

    for idx, measure in enumerate(measures_list):
        if allowed_types is not None and isinstance(measure, dict):
            m_type = str(measure.get("type") or "").strip().upper()
            if m_type not in allowed_types:
                continue
        parsed_outcomes.append(
            parse_outcome_measure(
                measure=measure,
                outcome_index=idx,
                flow_groups=flow_groups,
                flow_status=flow_status,
                enrollment=enr,
            )
        )

    primary_outcomes = [
        o
        for o in parsed_outcomes
        if (o.outcome_type or "").strip().upper() == "PRIMARY"
    ]
    primary_classes_seen = {
        o.population_class
        for o in primary_outcomes
        if o.parse_ok and o.population_class is not None
    }
    primary_population_classes = sorted(
        primary_classes_seen, key=lambda c: _TAXONOMY_ORDER[c]
    )

    return TrialAnalysisPopulations(
        nct_id=nct_id,
        snapshot_date=snapshot_date,
        enrollment=enr,
        has_results_section=bool(has_results_section),
        has_participant_flow=has_participant_flow,
        n_outcomes_total=len(parsed_outcomes),
        n_primary_outcomes=len(primary_outcomes),
        outcomes=parsed_outcomes,
        primary_population_classes=primary_population_classes,
        parse_ok=all(o.parse_ok for o in parsed_outcomes),
        parser_rules_version=PARSER_RULES_VERSION,
        vocabulary_sha256=VOCABULARY_SHA256,
    )


def parse_study_outcomes(
    study: dict[str, Any],
    snapshot_date: str | None = None,
    outcome_types: tuple[str, ...] | None = None,
) -> TrialAnalysisPopulations:
    """Parse all (or filtered) outcome measures from a raw CT.gov API v2 study dict.

    Audit holdout: ENFORCES when ``study`` carries an ``in_audit`` key (a
    manifest-merged record does). Raises PopulationAuditHoldoutError, before
    reading anything else in the record, if in_audit is True
    (reason "in_audit_true"), is not exactly a bool ("in_audit_not_bool"),
    or is False while protocolSection.identificationModule.nctId is not
    exactly ^NCT[0-9]{8}$ ("nct_id_malformed"). A refused record gets no
    parse-error record either: that would record the trial. A bare CT.gov
    study dict carries no ``in_audit``; for it this function relies on its
    caller to exclude holdout trials. Callers today: none in code (see the
    module docstring).
    """
    _refuse_audit_holdout_record(study, "parse_study_outcomes", _raw_nct_of_study)
    if not isinstance(study, dict):
        return parse_error_trial_outcomes(
            nct_id=None,
            enrollment=None,
            snapshot_date=snapshot_date,
            reason="study_not_a_dict",
        )
    results_section = study.get("resultsSection")
    has_results = isinstance(results_section, dict) and bool(results_section)
    flow_mod = (
        results_section.get("participantFlowModule")
        if isinstance(results_section, dict)
        else None
    )
    om_mod = (
        results_section.get("outcomeMeasuresModule")
        if isinstance(results_section, dict)
        else None
    )
    raw_measures = (
        om_mod.get("outcomeMeasures") if isinstance(om_mod, dict) else []
    )
    return _parse_outcomes_payload(
        nct_id=_nct_of(study),
        enrollment=_enrollment_of(study),
        has_results_section=has_results,
        participant_flow_module=flow_mod if isinstance(flow_mod, dict) else None,
        raw_measures=raw_measures,
        snapshot_date=snapshot_date,
        outcome_types=outcome_types,
    )


def select_raw_outcome_populations(study: dict[str, Any]) -> dict[str, Any]:
    """Extract the minimal raw outcome + flow payload for offline re-parse parity.

    Audit holdout: ENFORCES when ``study`` carries an ``in_audit`` key, with
    the same checks and PopulationAuditHoldoutError reasons as
    parse_study_outcomes, so a holdout trial's raw outcomes are never
    retained. A bare CT.gov study dict carries no ``in_audit``; for it this
    function relies on its caller to exclude holdout trials. Callers today:
    none in code (see the module docstring). The returned payload carries
    no ``in_audit`` key.
    """
    _refuse_audit_holdout_record(
        study, "select_raw_outcome_populations", _raw_nct_of_study
    )
    results_section = study.get("resultsSection") if isinstance(study, dict) else None
    has_results = isinstance(results_section, dict) and bool(results_section)
    flow_mod = (
        results_section.get("participantFlowModule")
        if isinstance(results_section, dict)
        else None
    )
    om_mod = (
        results_section.get("outcomeMeasuresModule")
        if isinstance(results_section, dict)
        else None
    )
    raw_measures = (
        om_mod.get("outcomeMeasures")
        if isinstance(om_mod, dict) and isinstance(om_mod.get("outcomeMeasures"), list)
        else []
    )
    return {
        "nct_id": _nct_of(study) if isinstance(study, dict) else None,
        "enrollment": _enrollment_of(study) if isinstance(study, dict) else None,
        "has_results_section": has_results,
        "participant_flow_module": flow_mod if isinstance(flow_mod, dict) else None,
        "outcome_measures": raw_measures,
    }


def build_outcomes_from_raw(
    raw: dict[str, Any],
    snapshot_date: str | None = None,
    outcome_types: tuple[str, ...] | None = None,
) -> TrialAnalysisPopulations:
    """Re-parse a retained raw outcome record; exact parity with parse_study_outcomes.

    Audit holdout: ENFORCES when ``raw`` carries an ``in_audit`` key. Raises
    PopulationAuditHoldoutError if in_audit is True ("in_audit_true"), is
    not exactly a bool ("in_audit_not_bool"), or is False while the
    record's top-level ``nct_id`` is not exactly ^NCT[0-9]{8}$
    ("nct_id_malformed"). A record from select_raw_outcome_populations
    carries no ``in_audit``; for it this function relies on its caller to
    exclude holdout trials. Callers today: none in code (see the module
    docstring).
    """
    _refuse_audit_holdout_record(raw, "build_outcomes_from_raw", _raw_nct_of_retained)
    if not isinstance(raw, dict):
        return parse_error_trial_outcomes(
            nct_id=None,
            enrollment=None,
            snapshot_date=snapshot_date,
            reason="raw_not_a_dict",
        )
    return _parse_outcomes_payload(
        nct_id=None if raw.get("nct_id") is None else str(raw.get("nct_id")),
        enrollment=raw.get("enrollment"),
        has_results_section=bool(raw.get("has_results_section")),
        participant_flow_module=(
            raw.get("participant_flow_module")
            if isinstance(raw.get("participant_flow_module"), dict)
            else None
        ),
        raw_measures=raw.get("outcome_measures") or [],
        snapshot_date=snapshot_date,
        outcome_types=outcome_types,
    )


def parse_error_trial_outcomes(
    nct_id: str | None,
    enrollment: Any,
    snapshot_date: str | None,
    reason: str,
) -> TrialAnalysisPopulations:
    """Emit an explicit parse-error record without dropping the trial.

    Audit holdout: RELIES ON ITS CALLER. It receives an nct_id but no
    ``in_audit`` and no audit id set, so it cannot tell a holdout trial from
    any other; the caller must not ask for a record for one. Callers today:
    parse_study_outcomes and build_outcomes_from_raw in this module (with
    nct_id=None, for non-dict input); none elsewhere in code (see the module
    docstring).
    """
    err_outcome = ParsedOutcomePopulation(
        outcome_index=0,
        outcome_type=None,
        measure_title=None,
        time_frame=None,
        reporting_status=None,
        param_type=None,
        unit_of_measure=None,
        population_description_raw=None,
        population_class=None,
        match_tier=None,
        parse_ok=False,
        parse_error_reason=reason,
    )
    return TrialAnalysisPopulations(
        nct_id=nct_id,
        snapshot_date=snapshot_date,
        enrollment=_parse_number(enrollment),
        has_results_section=False,
        has_participant_flow=False,
        n_outcomes_total=1,
        n_primary_outcomes=0,
        outcomes=[err_outcome],
        primary_population_classes=[],
        parse_ok=False,
        parser_rules_version=PARSER_RULES_VERSION,
        vocabulary_sha256=VOCABULARY_SHA256,
    )


# ---------------------------------------------------------------------------
# 7. Structural checks, vocabulary audit & pandas DataFrame helper (lazy import)
# ---------------------------------------------------------------------------


def structural_checks(
    trials: Iterable[TrialAnalysisPopulations],
) -> dict[str, dict[str, Any]]:
    """Structural invariants that must hold on any parsed snapshot.

    Audit holdout: RELIES ON ITS CALLER. It receives parsed
    TrialAnalysisPopulations, which carry an nct_id but no ``in_audit``, so it
    cannot refuse a holdout trial; the caller must not pass one. Callers today:
    none in code (see the module docstring).
    """
    trial_list = list(trials)
    all_outcomes = [o for t in trial_list for o in t.outcomes]
    checks: dict[str, dict[str, Any]] = {}

    def put(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = {"pass": bool(ok), "detail": detail}

    bad_class = [
        (t.nct_id, o.outcome_index, o.population_class, o.parse_ok)
        for t in trial_list
        for o in t.outcomes
        if (o.parse_ok and o.population_class not in POPULATION_TAXONOMY)
        or (not o.parse_ok and o.population_class is not None)
    ]
    put(
        "every_outcome_has_valid_population_class",
        not bad_class,
        f"{len(bad_class)} invalid outcomes (first: {bad_class[:3]})",
    )

    bad_ambig = [
        (t.nct_id, o.outcome_index)
        for t in trial_list
        for o in t.outcomes
        if o.parse_ok
        and ((o.population_class == "ambiguous_multi") != (len(o.matched_classes) >= 2))
    ]
    put(
        "ambiguous_multi_iff_ge_2_matched_classes",
        not bad_ambig,
        f"{len(bad_ambig)} inconsistent outcomes",
    )

    bad_not_stated = [
        (t.nct_id, o.outcome_index)
        for t in trial_list
        for o in t.outcomes
        if o.parse_ok
        and (
            (o.population_class == "not_stated")
            != (o.not_stated_reason in ("field_absent", "placeholder_token"))
        )
    ]
    put(
        "not_stated_has_explicit_reason",
        not bad_not_stated,
        f"{len(bad_not_stated)} inconsistent outcomes",
    )

    bad_denom = [
        (t.nct_id, o.outcome_index, o.denom_status)
        for t in trial_list
        for o in t.outcomes
        if o.denom_status not in DENOM_STATUS_LEVELS
    ]
    put(
        "denom_status_in_vocabulary",
        not bad_denom,
        f"{len(bad_denom)} invalid denom_status values",
    )

    bad_agg = [
        (t.nct_id, o.outcome_index, o.aggregation_method)
        for t in trial_list
        for o in t.outcomes
        if o.aggregation_method not in AGGREGATION_METHODS
    ]
    put(
        "aggregation_method_in_vocabulary",
        not bad_agg,
        f"{len(bad_agg)} invalid aggregation_method values",
    )

    bad_arms = [
        (t.nct_id, o.outcome_index, a.outcome_group_id, a.arm_gap_reason)
        for t in trial_list
        for o in t.outcomes
        for a in o.arms
        if a.arm_gap_reason not in ARM_GAP_REASONS
        or ((a.arm_gap_reason == "computed") != (a.arm_started_minus_analysed is not None))
    ]
    put(
        "arm_gap_reasons_consistent",
        not bad_arms,
        f"{len(bad_arms)} inconsistent arm gap records",
    )

    bad_part_flag = [
        (t.nct_id, o.outcome_index)
        for t in trial_list
        for o in t.outcomes
        if o.is_participant_count != (o.overall_participants_analyzed is not None)
    ]
    put(
        "is_participant_count_matches_overall_analyzed",
        not bad_part_flag,
        f"{len(bad_part_flag)} mismatched outcomes",
    )

    versions = {t.parser_rules_version for t in trial_list} | {
        o.parser_rules_version for o in all_outcomes
    }
    shas = {t.vocabulary_sha256 for t in trial_list} | {
        o.vocabulary_sha256 for o in all_outcomes
    }
    put(
        "single_parser_rules_version_and_vocab_sha",
        (not trial_list)
        or (versions == {PARSER_RULES_VERSION} and shas == {VOCABULARY_SHA256}),
        f"versions={sorted(versions)}, shas={sorted(shas)}",
    )
    return checks


def all_pass(checks: dict[str, dict[str, Any]]) -> bool:
    """Return True iff every check in structural_checks() passed."""
    return all(bool(entry.get("pass")) for entry in checks.values())


def population_audit(descriptions: Iterable[str | None]) -> list[dict[str, Any]]:
    """Aggregate distinct raw populationDescription strings with classifications.

    Audit holdout: RELIES ON ITS CALLER. It receives description strings with
    no trial id and no ``in_audit``, so it cannot refuse a holdout trial; the
    caller must not pass one. Callers today: none in code (see the module
    docstring).
    """
    counts: Counter[str | None] = Counter(descriptions)
    rows: list[dict[str, Any]] = []
    for raw, n in counts.items():
        res = classify_population_description(raw)
        rows.append(
            {
                "population_description_raw": raw,
                "count": n,
                "population_class": res.population_class,
                "match_tier": res.match_tier,
                "matched_classes": list(res.matched_classes),
                "matched_rule_ids": list(res.matched_rule_ids),
                "not_stated_reason": res.not_stated_reason,
                "declared_not_analyzed": res.declared_not_analyzed,
            }
        )
    rows.sort(
        key=lambda r: (
            -int(r["count"]),
            "" if r["population_description_raw"] is None else str(r["population_description_raw"]),
        )
    )
    return rows


def parse_outcomes_frame(
    studies: Iterable[dict[str, Any]],
    snapshot_date: str | None = None,
    outcome_types: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """pandas helper returning one flat row per parsed outcome measure.

    pandas is a declared dependency (requirements.txt) imported here and
    nowhere else in the module, so that the parser itself can be imported
    and used without it.

    Audit holdout: ENFORCES per study through parse_study_outcomes. The
    first study that carries ``in_audit`` and is refused raises
    PopulationAuditHoldoutError (reasons as there) and no frame is
    returned, so no row for any study escapes. Studies with no ``in_audit``
    key rely on the caller to exclude holdout trials. Callers today: none
    in code (see the module docstring).
    """
    import pandas as pd  # lazy: the module's only pandas use (see DEPENDS above)

    rows: list[dict[str, Any]] = []
    for study in studies:
        parsed = parse_study_outcomes(
            study, snapshot_date=snapshot_date, outcome_types=outcome_types
        )
        for out in parsed.outcomes:
            row = out.to_dict()
            row["nct_id"] = parsed.nct_id
            row["snapshot_date"] = parsed.snapshot_date
            row["enrollment"] = parsed.enrollment
            row["arms_count"] = len(out.arms)
            row.pop("arms", None)
            rows.append(row)
    return pd.DataFrame(rows)
