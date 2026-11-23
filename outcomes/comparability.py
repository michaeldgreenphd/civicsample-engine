"""Deterministic per-trial comparability, denominator-funnel, arm-alignment, and estimand inspector.

Pure functions over one ClinicalTrials.gov API v2 study record (`protocolSection`, `resultsSection`,
`documentSection`, `hasResults`) plus optional integration with `outcomes.outcome_history`.

Methodological invariants:
  - Zero RNG and zero hardcoded pass/fail thresholds on funnel differences, ratios, or attrition.
  - Missing or non-numeric registry values are recorded as `None` with an explicit state/reason;
    they are never coerced to `0`, `0.0`, `""`, or `False`.
  - Non-participant denominator units (such as eyes, lesions, teeth) are flagged as
    `unit_not_participants` and excluded from participant-count arithmetic.
  - `OG -> BG` and `OG -> FG` arm matching relies strictly on normalized non-empty title/description
    equivalence (`exact_unique_title_match`). Array index (`OG000 == BG000`) and participant count
    equality are NEVER used to match or break ties.
  - Legacy CT.gov hypothesis enums (`SUPERIORITY_OR_OTHER`, `NON_INFERIORITY_OR_EQUIVALENCE` and
    their `*_LEGACY` variants) stay `ambiguous_legacy_enum` unless structured analysis text
    explicitly disambiguates the hypothesis type.
  - Pairwise contrasts within a primary measure that share a common `OG` arm are flagged as
    statistically non-independent (`shared_arm_contrast_groups`).
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Callable, TypeVar

from outcomes import outcome_history as OH

METHOD_VERSION = 'outcomes-comparability-1'

RESULTS_STATUSES = ('posted', 'no_posted_results', 'unknown', 'not_retrieved')

FLOW_STARTED_STATES = (
    'ok',
    'flow_absent',
    'unit_not_participants',
    'no_periods',
    'no_started_milestone',
    'no_flow_groups',
    'non_numeric_or_missing_num_subjects',
    'no_posted_results',
)

BASELINE_TOTAL_STATES = (
    'ok',
    'baseline_absent',
    'no_denoms',
    'unit_not_participants',
    'no_groups',
    'non_numeric_count',
    'no_posted_results',
)

MEASURE_DENOM_STATES = (
    'ok',
    'denoms_absent',
    'unit_not_participants',
    'no_groups',
    'incomplete_or_non_numeric',
)

ARM_ALIGNMENT_STATES = (
    'exact_unique_title_match',
    'ambiguous_duplicate_titles',
    'pooled_or_subset_suspected',
    'no_title_match',
    'module_groups_absent',
)

MISSING_DATA_ATOMIC_METHODS = (
    'mmrm',
    'multiple_imputation',
    'locf',
    'bocf',
    'wocf',
    'observed_cases_only',
)

MISSING_DATA_STATES = MISSING_DATA_ATOMIC_METHODS + (
    'multiple_methods_named',
    'not_stated',
)

INTERCURRENT_EVENT_STATES = (
    'rescue_excluded_or_censored',
    'treatment_policy_regardless_of_rescue_or_discontinuation',
    'both_co_reported',
    'not_stated',
)

HYPOTHESIS_CLARITY_STATES = (
    'superiority_explicit',
    'non_inferiority_with_margin',
    'non_inferiority_missing_margin',
    'equivalence_with_margin',
    'equivalence_missing_margin',
    'ambiguous_legacy_enum',
    'other_explicit',
    'not_stated',
)

CARDINALITY_STATES = (
    'empty',
    'single_arm',
    'pairwise_two_arm',
    'multi_arm_joint',
)

FIELDS_INSPECTED = (
    'protocolSection.identificationModule.(nctId,briefTitle)',
    'protocolSection.designModule.(studyType,phases,designInfo.allocation,enrollmentInfo)',
    'protocolSection.outcomesModule.(primaryOutcomes,secondaryOutcomes)',
    'documentSection.largeDocumentModule.largeDocs',
    'resultsSection.participantFlowModule.(typeUnitsAnalyzed,groups,periods[0].milestones)',
    'resultsSection.baselineCharacteristicsModule.(populationDescription,groups,denoms)',
    'resultsSection.outcomeMeasuresModule.outcomeMeasures[type=PRIMARY].(title,description,type,'
    'unitOfMeasure,paramType,dispersionType,timeFrame,populationDescription,groups,denoms,'
    'classes[].denoms,analyses[])',
)

T = TypeVar('T')

_I = re.IGNORECASE

# Generic arm placeholder titles ("Arm 1", "Group A", "Cohort 2"): description equivalence may pair
# two arms ONLY when both descriptions are non-empty, identical, unique, and at least one title is a
# placeholder rather than a conflicting substantive intervention name.
_PLACEHOLDER_TITLE_RE = re.compile(
    r'^(?:arm|group|cohort|treatment\s+(?:arm|group)|study\s+(?:arm|group))\s*[#:]?\s*[a-z0-9]+$',
    _I,
)

# Keywords suggesting an arm in outcome measures or baseline is a pooled combination or an analysis
# subset/subpopulation of another module's arms rather than a 1:1 randomized arm.
_POOLED_OR_SUBSET_CUE_RE = re.compile(
    r'\b(?:pooled|combined|all\s+doses?|total\s+[a-z0-9]|subset|sub-?group|sub-?population|'
    r'completers?|per[- ]protocol|evaluable|modified\s+itt|mitt|responders?|non[- ]responders?)\b',
    _I,
)

# Missing-data / longitudinal estimation patterns, ordered canonically.
_MISSING_DATA_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        'mmrm',
        re.compile(
            r'\bMMRM\b|'
            r'\bmixed[- ]effects?\s+models?\s+(?:for|with)\s+repeated\s+measures?\b|'
            r'\bmixed\s+models?\s+(?:(?:for|with)\s+)?repeated\s+measures?\b',
            _I,
        ),
    ),
    (
        'multiple_imputation',
        re.compile(
            r'\bmultiple\s+imputations?\b|'
            r'\bmultiply\s+imputed\b|'
            r'\bchained\s+equations\b|'
            r'\bwashout\s+multiple\s+imputation\b|'
            r'\breturn[- ]to[- ]baseline\s+imputation\b|'
            r'\bjump[- ]to[- ]reference\b|'
            r'\bcopy[- ]increments?[- ]from[- ]reference\b|'
            r'\bpattern[- ]mixture\s+models?\b',
            _I,
        ),
    ),
    (
        'locf',
        re.compile(r'\bLOCF\b|\blast\s+observations?\s+carried\s+forward\b', _I),
    ),
    (
        'bocf',
        re.compile(r'\bBOCF\b|\bbaseline\s+observations?\s+carried\s+forward\b', _I),
    ),
    (
        'wocf',
        re.compile(r'\bWOCF\b|\bworst\s+observations?\s+carried\s+forward\b', _I),
    ),
    (
        'observed_cases_only',
        re.compile(
            r'\bobserved\s+cases?(?:\s+only)?\b|'
            r'\bobserved\s+(?:data|values?)\s+only\b|'
            r'\bcomplete[- ]cases?\s+(?:analysis|only)\b|'
            r'\bavailable[- ]cases?\s+(?:analysis|only)\b|'
            r'\bno\s+imputation\s+(?:of|for)\s+missing\b|'
            r'\bwithout\s+imputation\s+of\s+missing\b',
            _I,
        ),
    ),
)

# Intercurrent-event / rescue-medication handling patterns.
_TREATMENT_POLICY_RE = re.compile(
    r'\btreatment[- ]policy\s+(?:estimand|strategy)\b|'
    r'\b(?:regardless|irrespective)\s+of\s+(?:[^.;\n]{0,60}?\b)?'
    r'(?:rescue|antihyperglyc(?:a?emic)|study\s+(?:drug|treatment|medication)\s+discontinuation|'
    r'premature\s+discontinuation|treatment\s+discontinuation|discontinuation|adherence|'
    r'intercurrent\s+events?)\b|'
    r'\bincluding\s+(?:data|measurements?|values?|observations?|assessments?)\s+'
    r'(?:(?:collected|obtained)\s+)?(?:after|following|post[- ]?)\s*'
    r'(?:initiation\s+of\s+)?(?:rescue|study\s+drug\s+discontinuation|treatment\s+discontinuation)\b|'
    r'\bretrieved\s+dropouts?\b',
    _I,
)

_RESCUE_EXCLUDED_RE = re.compile(
    r'\bhypothetical\s+estimand\b|'
    r'\b(?:while[- ]on[- ]treatment|on[- ]treatment)\s+without\s+rescue\b|'
    r'\bexcluding\s+(?:data|measurements?|values?|observations?|assessments?)\s+'
    r'(?:(?:collected|obtained)\s+)?(?:after|following|post[- ]?)\s*'
    r'(?:initiation\s+of\s+)?(?:rescue|glyc(?:a?emic)\s+rescue|antihyperglyc(?:a?emic)\s+rescue|'
    r'study\s+(?:drug|treatment|medication)\s+discontinuation|treatment\s+discontinuation)\b|'
    r'\b(?:data|measurements?|values?|observations?|assessments?)\s+'
    r'(?:(?:collected|obtained)\s+)?(?:after|following|post[- ]?)\s*'
    r'(?:initiation\s+of\s+)?(?:rescue|treatment\s+discontinuation)[^.;\n]{0,50}?'
    r'\b(?:excluded|censored|omitted|set\s+to\s+missing|not\s+included)\b|'
    r'\b(?:censored|excluded|omitted)\s+(?:at|after|upon|following)\s+'
    r'(?:initiation\s+of\s+)?(?:rescue|study\s+(?:drug|treatment)\s+discontinuation|'
    r'treatment\s+discontinuation)\b|'
    r'\bprior\s+to\s+(?:initiation\s+of\s+)?rescue\s+(?:medication|therapy|treatment)\b|'
    r'\bwithout\s+(?:glyc(?:a?emic)\s+|antihyperglyc(?:a?emic)\s+)?rescue\s+(?:medication|therapy|treatment)\b',
    _I,
)

# Hypothesis and margin extraction helpers.
# Noise spans (confidence levels, alpha / Type-I error / p-value thresholds, power, SD, sample size,
# sidedness, timepoint labels, weight/viral-load covariates) are masked with equal-length spaces so
# regex match offsets `[m.start():m.end()]` slice the exact verbatim substring from the raw comment.
_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r'\b\d+(?:\.\d+)?\s*%\s*(?:(?:two|one|2|1)[- ]sided\s+)?'
        r'(?:confidence\s+(?:intervals?|limits?|bounds?)\b(?:\s*\(CIs?\))?|CIs?\b)',
        _I,
    ),
    re.compile(
        r'\b(?:(?:two|one|2|1)[- ]sided\s+)\d+(?:\.\d+)?\s*%\s*'
        r'(?:confidence\s+(?:intervals?|limits?|bounds?)\b(?:\s*\(CIs?\))?|CIs?\b)',
        _I,
    ),
    re.compile(
        r'\bbetween\s+the\s+\d+(?:\.\d+)?(?:st|nd|rd|th)\s+and\s+'
        r'\d+(?:\.\d+)?(?:st|nd|rd|th)\s+percentiles?\b',
        _I,
    ),
    re.compile(
        r'\b(?:alpha|significance\s+level|level\s+of\s+significance|type\s+(?:i|1)\s+error(?:\s+rate)?|'
        r'p[- ]?values?|p)\s*(?:of\s+|is\s+|was\s+|had\s+|set\s+(?:at|as|to)\s+|=\s*|\\?[<>≤≥]=?\s*)?'
        r'(?:0?\.\d+|\d+(?:\.\d+)?\s*%)',
        _I,
    ),
    re.compile(
        r'(?:0?\.\d+|\d+(?:\.\d+)?\s*%)\s+(?:set\s+as\s+)?(?:(?:one|two|1|2)[- ]sided\s+)?'
        r'(?:alpha|level\s+of\s+significance|significance\s+level|type\s+(?:i|1)\s+error)',
        _I,
    ),
    re.compile(r'\b\d+(?:\.\d+)?\s*%\s*(?:statistical\s+)?power\b|\bpower\s+(?:of\s+|=\s*|was\s+)?\d+(?:\.\d+)?\s*%', _I),
    re.compile(r'\b(?:SD|standard\s+deviation)\s*(?:of\s+|is\s+|was\s+|=\s*)?\d+(?:\.\d+)?\s*%?', _I),
    re.compile(r'\b(?:sample\s+size\s+of\s+|n\s*=\s*|up\s+to\s+)\d+\b', _I),
    re.compile(r'\b\d+\s+(?:subjects?|participants?|patients?|infants?|adults?|arms?|groups?|events?)\b', _I),
    re.compile(r'\b(?:[12]|one|two)[- ]sided\b|\b[12]\s+sided\b', _I),
    re.compile(r'\b(?:week|day|month|year|visit|period|cycle|stage|step|phase|part)s?\s+\d+\b', _I),
    re.compile(r'\b\d+(?:\.\d+)?[- ](?:week|day|month|year|hour|hr)s?\b', _I),
    re.compile(r'(?:\\?[<>≤≥]=?\s*|\b)\d+(?:\.\d+)?\s*(?:kilograms?|kg|copies/ml)\b', _I),
)

_MARGIN_UNIT = r'(?:\s*(?!(?:to|and|or|is|was|be|in|of|at|by)\b)[a-zA-Z]{1,8})?'
_MARGIN_NUM = r'(?:[+-±]?\s*(?:\d+(?:\.\d+)?|\.\d+)\s*%?)'
_MARGIN_RANGE = _MARGIN_NUM + _MARGIN_UNIT + r'(?:\s*(?:to|and|,|[-–])\s*' + _MARGIN_NUM + _MARGIN_UNIT + r')?'

_MARGIN_IN_NI_COMMENT_RE = re.compile(
    r'\b(?:margin|delta|threshold|criterion|'
    r'(?:equivalence|non[- ]?inferior(?:ity)?|acceptance|hypothesized|predefined|pre-?specified)\s+'
    r'(?:limit|bound|range|interval|margin)s?)\b'
    r'(?:\s+(?:of|is|was|were|set\s+(?:to|at|as)|pre-?specified\s+(?:at|as)|defined\s+as|from|equal\s+to))*'
    r'\s*(?:[:=]|\\?[\[(])?\s*' + _MARGIN_RANGE + r'(?:\s*\\?[\])])?|'
    r'\b(?:LL|UL|(?:lower|upper)\s+(?:limit|bound)s?)\b[^.;\n]{0,160}?'
    r'\b(?:(?:is|was|were|be|are)\s+)?'
    r'(?:excludes?|exceeds?|above|below|within|less\s+than(?:\s+or\s+equal\s+to)?|'
    r'greater\s+than(?:\s+or\s+equal\s+to)?|is|was|were|be)\s*'
    r'(?:\(\\?[<>≤≥]=?\)\s*)?(?:[:=]|\\?[\[(])?\s*' + _MARGIN_NUM + _MARGIN_UNIT + r'|'
    r'\b(?:included\s+in|falls?\s+within|within|excludes?|above|below|'
    r'less\s+than(?:\s+or\s+equal\s+to)?|greater\s+than(?:\s+or\s+equal\s+to)?|'
    r'at\s+most|at\s+least|no\s+(?:more|less|worse)\s+than)\b'
    r'(?:\s*\(\\?[<>≤≥]=?\))?(?:\s+the)?'
    r'(?:\s+(?:predefined|pre-?specified|hypothesized|acceptance))?'
    r'(?:\s+(?:equivalence\s+|non[- ]?inferior(?:ity)?\s+)?(?:interval|range|limit|bound|margin)s?)?'
    r'(?:\s+(?:from|of))?'
    r'\s*(?:[:=]|\\?[\[(])?\s*' + _MARGIN_RANGE + r'(?:\s*\\?[\])])?'
    r'(?:\s+(?:equivalence\s+)?(?:interval|range))?|'
    r'\b(?:is|was|were|be)\s+' + _MARGIN_NUM + r'\s+or\s+(?:greater|less|higher|lower|more|smaller)\b|'
    r'\\?[<>≤≥]=?\s*' + _MARGIN_NUM + r'|'
    r'±\s*(?:\d+(?:\.\d+)?|\.\d+)\s*%?|'
    r'\\?[\[(]\s*[+-]?\s*(?:\d+(?:\.\d+)?|\.\d+)\s*(?:[a-zA-Z%]+\s*)?'
    r'(?:to|and|,|[-–])\s*[+-]?\s*(?:\d+(?:\.\d+)?|\.\d+)\s*(?:[a-zA-Z%]+)?\s*\\?[\])]|'
    r'\b\d+(?:\.\d+)?\s*%?\s*(?:to|[-–])\s*\d+(?:\.\d+)?\s*%|'
    r'\b0\.\d+\s*(?:to|and|,|[-–])\s*1\.\d+\b',
    _I,
)

_ANCHORED_MARGIN_IN_OTHER_TEXT_RE = re.compile(
    r'\b(?:non[- ]?inferior(?:ity)?|equivalen(?:ce|t)|bioequivalen(?:ce|t))\s+'
    r'(?:margin|delta|bound|limit|threshold|criterion|interval|range)s?\b[^.;\n]{0,60}?'
    + _MARGIN_RANGE,
    _I,
)

_SUPERIORITY_CUE_RE = re.compile(r'\bsuperior(?:ity)?\b', _I)
_NON_INFERIORITY_CUE_RE = re.compile(r'\bnon[- ]?inferior(?:ity)?\b', _I)
_EQUIVALENCE_CUE_RE = re.compile(r'\b(?:bio)?equivalen(?:ce|t)\b', _I)


# ---------------------------------------------------------------- numbers & guards

def is_num(v: Any) -> bool:
    """True iff `v` parses as a finite number after removing thousands commas (`'NA'`, `''`, `None`,
    booleans, `inf`, and `nan` are False and are never read as zero)."""
    if v is None or isinstance(v, bool):
        return False
    try:
        x = float(str(v).replace(',', '').strip())
    except ValueError:
        return False
    return math.isfinite(x)


def num(v: Any) -> float | None:
    """Parsed finite float for `v`, or `None` when not numeric."""
    return float(str(v).replace(',', '').strip()) if is_num(v) else None


def plain_num(x: float | None) -> int | float | None:
    """Whole floats rendered as `int` (`120` rather than `120.0`); `None` preserved."""
    if x is None:
        return None
    return int(x) if float(x).is_integer() else round(x, 6)


def nct_id_of(study: Any) -> str | None:
    """Study NCT ID from `protocolSection.identificationModule.nctId`, or `None`."""
    try:
        v = study['protocolSection']['identificationModule']['nctId']
    except (KeyError, TypeError):
        return None
    return v if isinstance(v, str) and v.strip() else None


def _guard(section: str, failures: list[dict[str, str]], fn: Callable[[], T]) -> T | None:
    try:
        return fn()
    except (KeyError, TypeError, AttributeError, ValueError, IndexError) as exc:
        failures.append({'section': section, 'reason': f'{type(exc).__name__}: {exc}'})
        return None


def normalize_label(text: Any) -> str:
    """Unicode-NFC, whitespace-collapsed, case-folded group label; `''` when absent or blank."""
    if not isinstance(text, str):
        return ''
    norm = unicodedata.normalize('NFC', text).strip()
    if not norm:
        return ''
    return ' '.join(norm.split()).casefold()


def _snippet(text: str, start: int, end: int, pad: int = 80) -> str:
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    return text[lo:hi].replace('\n', ' ').strip()


# ---------------------------------------------------------------- denominator funnel

def extract_registered_enrollment(protocol: dict[str, Any]) -> dict[str, Any]:
    """Registered enrollment (`count` + `ACTUAL`/`ESTIMATED`) from `designModule.enrollmentInfo`."""
    design = protocol.get('designModule') or {}
    if not isinstance(design, dict):
        raise TypeError('protocolSection.designModule is not an object')
    enr = design.get('enrollmentInfo') or {}
    if not isinstance(enr, dict):
        raise TypeError('protocolSection.designModule.enrollmentInfo is not an object')
    raw_count = enr.get('count')
    enr_type = enr.get('type') if isinstance(enr.get('type'), str) else None
    source = 'protocolSection.designModule.enrollmentInfo'
    if raw_count is None:
        return {'count': None, 'type': enr_type, 'state': 'not_registered', 'source_field': source}
    parsed = num(raw_count)
    if parsed is None:
        return {'count': None, 'type': enr_type, 'state': 'non_numeric', 'source_field': source}
    return {'count': plain_num(parsed), 'type': enr_type, 'state': 'ok', 'source_field': source}


def extract_period1_started(fmod: dict[str, Any] | None, posted: bool) -> dict[str, Any]:
    """Period 1 `STARTED` participant sum from `participantFlowModule`, flagging `flow_absent` and
    `unit_not_participants` explicitly without coercing missing or non-participant counts to zero."""
    source = 'resultsSection.participantFlowModule.periods[0].milestones[type=STARTED]'
    if not posted:
        return {
            'value': None, 'state': 'no_posted_results', 'reason': 'no_posted_results',
            'n_periods': None, 'period_title': None, 'type_units_analyzed': None,
            'second_unit_flag': None, 'arms': None, 'source_field': source,
        }
    if not fmod:
        return {
            'value': None, 'state': 'flow_absent', 'reason': 'flow_absent',
            'n_periods': None, 'period_title': None, 'type_units_analyzed': None,
            'second_unit_flag': None, 'arms': None, 'source_field': source,
        }
    if not isinstance(fmod, dict):
        raise TypeError('resultsSection.participantFlowModule is not an object')

    explicit_units = fmod.get('units')
    type_units = fmod.get('typeUnitsAnalyzed') if isinstance(fmod.get('typeUnitsAnalyzed'), str) else None
    type_units_clean = type_units.strip() if type_units else None
    has_non_participant_type_unit = bool(
        type_units_clean and type_units_clean.casefold() != 'participants'
    )

    if isinstance(explicit_units, str) and explicit_units.strip() and explicit_units.strip().casefold() != 'participants':
        return {
            'value': None, 'state': 'unit_not_participants', 'reason': f'units={explicit_units.strip()!r}',
            'n_periods': len(fmod.get('periods') or []), 'period_title': None,
            'type_units_analyzed': explicit_units.strip(), 'second_unit_flag': 'unit_not_participants',
            'arms': None, 'source_field': source,
        }

    periods = fmod.get('periods') or []
    if not isinstance(periods, list):
        raise TypeError('participantFlowModule.periods is not a list')
    if not periods:
        return {
            'value': None, 'state': 'no_periods', 'reason': 'no_periods',
            'n_periods': 0, 'period_title': None, 'type_units_analyzed': type_units_clean,
            'second_unit_flag': 'unit_not_participants' if has_non_participant_type_unit else None,
            'arms': [], 'source_field': source,
        }

    first_period = periods[0]
    if not isinstance(first_period, dict):
        raise TypeError('participantFlowModule.periods[0] is not an object')
    period_title = first_period.get('title') if isinstance(first_period.get('title'), str) else None

    raw_groups = fmod.get('groups') or []
    if not isinstance(raw_groups, list):
        raise TypeError('participantFlowModule.groups is not a list')
    non_total_groups = [
        g for g in raw_groups
        if isinstance(g, dict) and normalize_label(g.get('title')) != 'total'
    ]
    if not non_total_groups:
        return {
            'value': None, 'state': 'no_flow_groups', 'reason': 'no_flow_groups',
            'n_periods': len(periods), 'period_title': period_title,
            'type_units_analyzed': type_units_clean,
            'second_unit_flag': 'unit_not_participants' if has_non_participant_type_unit else None,
            'arms': [], 'source_field': source,
        }

    milestones = first_period.get('milestones') or []
    started_ms: dict[str, Any] | None = None
    completed_ms: dict[str, Any] | None = None
    for ms in milestones:
        if not isinstance(ms, dict):
            continue
        if ms.get('type') == 'STARTED' and started_ms is None:
            started_ms = ms
        elif ms.get('type') == 'COMPLETED' and completed_ms is None:
            completed_ms = ms

    if started_ms is None:
        return {
            'value': None, 'state': 'no_started_milestone', 'reason': 'no_started_milestone',
            'n_periods': len(periods), 'period_title': period_title,
            'type_units_analyzed': type_units_clean,
            'second_unit_flag': 'unit_not_participants' if has_non_participant_type_unit else None,
            'arms': [], 'source_field': source,
        }

    started_subj: dict[str, Any] = {}
    started_units: dict[str, Any] = {}
    for ach in started_ms.get('achievements') or []:
        if isinstance(ach, dict) and isinstance(ach.get('groupId'), str):
            started_subj[ach['groupId']] = ach.get('numSubjects')
            started_units[ach['groupId']] = ach.get('numUnits')

    completed_subj: dict[str, Any] = {}
    if completed_ms is not None:
        for ach in completed_ms.get('achievements') or []:
            if isinstance(ach, dict) and isinstance(ach.get('groupId'), str):
                completed_subj[ach['groupId']] = ach.get('numSubjects')

    arms: list[dict[str, Any]] = []
    subj_vals: list[float | None] = []
    unit_vals: list[float | None] = []
    for g in non_total_groups:
        gid = g['id']
        s_val = num(started_subj.get(gid))
        u_val = num(started_units.get(gid))
        c_val = num(completed_subj.get(gid))
        subj_vals.append(s_val)
        unit_vals.append(u_val)
        arms.append({
            'group_id': gid,
            'title': g.get('title'),
            'description': g.get('description'),
            'started': plain_num(s_val),
            'completed': plain_num(c_val),
            'started_units': plain_num(u_val),
        })

    if any(v is None for v in subj_vals):
        # If participant counts (`numSubjects`) are absent while `typeUnitsAnalyzed` or `numUnits`
        # provides non-participant counts, report `unit_not_participants` explicitly.
        if has_non_participant_type_unit or any(v is not None for v in unit_vals):
            return {
                'value': None,
                'state': 'unit_not_participants',
                'reason': f'numSubjects missing; typeUnitsAnalyzed={type_units_clean!r}',
                'n_periods': len(periods),
                'period_title': period_title,
                'type_units_analyzed': type_units_clean,
                'second_unit_flag': 'unit_not_participants',
                'arms': arms,
                'source_field': source,
            }
        return {
            'value': None,
            'state': 'non_numeric_or_missing_num_subjects',
            'reason': 'non_numeric_or_missing_num_subjects',
            'n_periods': len(periods),
            'period_title': period_title,
            'type_units_analyzed': type_units_clean,
            'second_unit_flag': 'unit_not_participants' if has_non_participant_type_unit else None,
            'arms': arms,
            'source_field': source,
        }

    total_started = sum(v for v in subj_vals if v is not None)
    return {
        'value': plain_num(total_started),
        'state': 'ok',
        'reason': 'ok',
        'n_periods': len(periods),
        'period_title': period_title,
        'type_units_analyzed': type_units_clean,
        'second_unit_flag': 'unit_not_participants' if has_non_participant_type_unit else None,
        'arms': arms,
        'source_field': source,
    }


def _find_participants_denom_row(denoms: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[str]]:
    """Return `(participants_row, all_reported_units)`. Never substitutes Eyes/Lesions for Participants."""
    units_seen: list[str] = []
    part_row: dict[str, Any] | None = None
    for d in denoms:
        if not isinstance(d, dict):
            continue
        u = (d.get('units') or '').strip()
        if u:
            units_seen.append(u)
        if u.casefold() == 'participants' and part_row is None:
            part_row = d
    return part_row, units_seen


def extract_baseline_total(bmod: dict[str, Any] | None, posted: bool) -> dict[str, Any]:
    """Baseline participant count from `baselineCharacteristicsModule` via Rule T:
    uses the `Total` column when present (or untitled trailing sum-of-arms column on 3+ groups),
    and sums non-Total groups when no Total column exists."""
    source = 'resultsSection.baselineCharacteristicsModule.denoms'
    if not posted:
        return {
            'value': None, 'state': 'no_posted_results', 'method': None, 'reason': 'no_posted_results',
            'total_group_id': None, 'arms_sum': None, 'total_equals_arms_sum': None,
            'reported_units': [], 'groups': None, 'population_description': None, 'source_field': source,
        }
    if not bmod:
        return {
            'value': None, 'state': 'baseline_absent', 'method': None, 'reason': 'baseline_absent',
            'total_group_id': None, 'arms_sum': None, 'total_equals_arms_sum': None,
            'reported_units': [], 'groups': None, 'population_description': None, 'source_field': source,
        }
    if not isinstance(bmod, dict):
        raise TypeError('resultsSection.baselineCharacteristicsModule is not an object')

    pop_desc = bmod.get('populationDescription')
    pop_clean = pop_desc if isinstance(pop_desc, str) and pop_desc.strip() else None

    groups = bmod.get('groups') or []
    if not isinstance(groups, list):
        raise TypeError('baselineCharacteristicsModule.groups is not a list')
    if not groups:
        return {
            'value': None, 'state': 'no_groups', 'method': None, 'reason': 'no_groups',
            'total_group_id': None, 'arms_sum': None, 'total_equals_arms_sum': None,
            'reported_units': [], 'groups': [], 'population_description': pop_clean, 'source_field': source,
        }

    denoms = bmod.get('denoms') or []
    if not isinstance(denoms, list):
        raise TypeError('baselineCharacteristicsModule.denoms is not a list')
    if not denoms:
        return {
            'value': None, 'state': 'no_denoms', 'method': None, 'reason': 'no_denoms',
            'total_group_id': None, 'arms_sum': None, 'total_equals_arms_sum': None,
            'reported_units': [],
            'groups': [{'group_id': g['id'], 'title': g.get('title'), 'description': g.get('description'),
                        'is_total_group': normalize_label(g.get('title')) == 'total', 'count': None}
                       for g in groups],
            'population_description': pop_clean, 'source_field': source,
        }

    part_row, units_seen = _find_participants_denom_row(denoms)
    if part_row is None:
        return {
            'value': None, 'state': 'unit_not_participants', 'method': None,
            'reason': f'units={units_seen!r}', 'total_group_id': None,
            'arms_sum': None, 'total_equals_arms_sum': None, 'reported_units': units_seen,
            'groups': [{'group_id': g['id'], 'title': g.get('title'), 'description': g.get('description'),
                        'is_total_group': normalize_label(g.get('title')) == 'total', 'count': None}
                       for g in groups],
            'population_description': pop_clean, 'source_field': source,
        }

    cnt_by_id: dict[str, float | None] = {}
    for c in part_row.get('counts') or []:
        if isinstance(c, dict) and isinstance(c.get('groupId'), str):
            cnt_by_id[c['groupId']] = num(c.get('value'))

    vals = [cnt_by_id.get(g['id']) for g in groups]

    # Identify Total group index even when counts are incomplete if explicitly titled "Total",
    # so non-Total baseline arms stay cleanly separated for arm alignment.
    titled_indices = [i for i, g in enumerate(groups) if normalize_label(g.get('title')) == 'total']
    if any(v is None for v in vals):
        tot_idx = titled_indices[0] if titled_indices else None
        return {
            'value': None, 'state': 'non_numeric_count', 'method': None, 'reason': 'non_numeric_count',
            'total_group_id': groups[tot_idx]['id'] if tot_idx is not None else None,
            'arms_sum': None, 'total_equals_arms_sum': None, 'reported_units': units_seen,
            'groups': [
                {'group_id': g['id'], 'title': g.get('title'), 'description': g.get('description'),
                 'is_total_group': (i in titled_indices), 'count': plain_num(vals[i])}
                for i, g in enumerate(groups)
            ],
            'population_description': pop_clean, 'source_field': source,
        }

    num_vals = [v for v in vals if v is not None]
    if len(groups) == 1:
        method = 'single_group'
        pick: int | None = 0
        is_total_flags = [normalize_label(groups[0].get('title')) == 'total']
    elif titled_indices:
        method = 'titled_total'
        pick = titled_indices[0]
        is_total_flags = [i in titled_indices for i in range(len(groups))]
    elif len(groups) >= 3 and num_vals[-1] > 0 and abs(num_vals[-1] - sum(num_vals[:-1])) < 1e-9:
        method = 'last_equals_sum_untitled_total'
        pick = len(groups) - 1
        is_total_flags = [i == (len(groups) - 1) for i in range(len(groups))]
    else:
        method = 'non_total_group_sum'
        pick = None
        is_total_flags = [False for _ in groups]

    if pick is None:
        total_val = sum(num_vals)
        arms_sum = total_val
        total_gid = None
    else:
        total_val = num_vals[pick]
        arms_sum = sum(v for i, v in enumerate(num_vals) if not is_total_flags[i]) if len(groups) > 1 else num_vals[0]
        total_gid = groups[pick]['id']

    group_rows = [
        {
            'group_id': g['id'],
            'title': g.get('title'),
            'description': g.get('description'),
            'is_total_group': is_total_flags[i],
            'count': plain_num(num_vals[i]),
        }
        for i, g in enumerate(groups)
    ]

    return {
        'value': plain_num(total_val),
        'state': 'ok',
        'method': method,
        'reason': 'ok',
        'total_group_id': total_gid,
        'arms_sum': plain_num(arms_sum),
        'total_equals_arms_sum': abs(total_val - arms_sum) < 1e-9,
        'reported_units': units_seen,
        'groups': group_rows,
        'population_description': pop_clean,
        'source_field': source,
    }


def _extract_measure_denominators(m: dict[str, Any], measure_index: int) -> dict[str, Any]:
    """Extract measure-level `OG` participant counts and detect `classes[].denoms` deviations."""
    groups = m.get('groups') or []
    if not isinstance(groups, list):
        raise TypeError(f'outcomeMeasures[{measure_index}].groups is not a list')
    denoms = m.get('denoms') or []
    if not isinstance(denoms, list):
        raise TypeError(f'outcomeMeasures[{measure_index}].denoms is not a list')

    titled_total_ids = {
        g['id'] for g in groups
        if isinstance(g, dict) and isinstance(g.get('id'), str) and normalize_label(g.get('title')) == 'total'
    }
    # When a single group is literally titled "Total" with no arm groups, keep it out of arm matching
    # while still reading its participant count if posted.
    non_total_groups = [g for g in groups if g['id'] not in titled_total_ids] if len(groups) > len(titled_total_ids) else list(groups)

    if not groups:
        return {
            'denom_state': 'no_groups',
            'reported_units': [],
            'og_denominators': [],
            'analysed_total': None,
            'class_denom_deviations': [],
            'has_class_denom_deviation': False,
        }

    if not denoms:
        return {
            'denom_state': 'denoms_absent',
            'reported_units': [],
            'og_denominators': [
                {'group_id': g['id'], 'title': g.get('title'), 'description': g.get('description'),
                 'is_total_group': g['id'] in titled_total_ids and len(groups) > len(titled_total_ids),
                 'count': None, 'raw_value': None}
                for g in groups
            ],
            'analysed_total': None,
            'class_denom_deviations': _detect_class_denom_deviations(m, measure_index, None, {}),
            'has_class_denom_deviation': bool(_detect_class_denom_deviations(m, measure_index, None, {})),
        }

    part_row, units_seen = _find_participants_denom_row(denoms)
    if part_row is None:
        deviations = _detect_class_denom_deviations(m, measure_index, units_seen[0] if units_seen else None, {})
        return {
            'denom_state': 'unit_not_participants',
            'reported_units': units_seen,
            'og_denominators': [
                {'group_id': g['id'], 'title': g.get('title'), 'description': g.get('description'),
                 'is_total_group': g['id'] in titled_total_ids and len(groups) > len(titled_total_ids),
                 'count': None, 'raw_value': None}
                for g in groups
            ],
            'analysed_total': None,
            'class_denom_deviations': deviations,
            'has_class_denom_deviation': bool(deviations),
        }

    raw_by_gid: dict[str, Any] = {}
    num_by_gid: dict[str, float | None] = {}
    for c in part_row.get('counts') or []:
        if isinstance(c, dict) and isinstance(c.get('groupId'), str):
            raw_by_gid[c['groupId']] = c.get('value')
            num_by_gid[c['groupId']] = num(c.get('value'))

    og_rows = [
        {
            'group_id': g['id'],
            'title': g.get('title'),
            'description': g.get('description'),
            'is_total_group': g['id'] in titled_total_ids and len(groups) > len(titled_total_ids),
            'count': plain_num(num_by_gid.get(g['id'])),
            'raw_value': raw_by_gid.get(g['id']),
        }
        for g in groups
    ]

    arm_vals = [num_by_gid.get(g['id']) for g in non_total_groups]
    if any(v is None for v in arm_vals):
        denom_state = 'incomplete_or_non_numeric'
        analysed_total: int | float | None = None
    else:
        denom_state = 'ok'
        analysed_total = plain_num(sum(v for v in arm_vals if v is not None))

    deviations = _detect_class_denom_deviations(
        m, measure_index, (part_row.get('units') or '').strip(), num_by_gid
    )
    return {
        'denom_state': denom_state,
        'reported_units': units_seen,
        'og_denominators': og_rows,
        'analysed_total': analysed_total,
        'class_denom_deviations': deviations,
        'has_class_denom_deviation': bool(deviations),
    }


def _detect_class_denom_deviations(
    m: dict[str, Any],
    measure_index: int,
    measure_unit: str | None,
    measure_counts: dict[str, float | None],
) -> list[dict[str, Any]]:
    """Detect row-specific `classes[].denoms` that deviate from measure-level `denoms`."""
    deviations: list[dict[str, Any]] = []
    classes = m.get('classes') or []
    if not isinstance(classes, list):
        return deviations
    for ci, cl in enumerate(classes):
        if not isinstance(cl, dict):
            continue
        cl_denoms = cl.get('denoms') or []
        if not cl_denoms or not isinstance(cl_denoms, list):
            continue
        for di, d in enumerate(cl_denoms):
            if not isinstance(d, dict):
                continue
            cl_unit = (d.get('units') or '').strip()
            unit_differs = (
                measure_unit is None
                or cl_unit.casefold() != measure_unit.casefold()
            )
            group_diffs: list[dict[str, Any]] = []
            if not unit_differs:
                for c in d.get('counts') or []:
                    if not isinstance(c, dict) or not isinstance(c.get('groupId'), str):
                        continue
                    gid = c['groupId']
                    cl_val = num(c.get('value'))
                    m_val = measure_counts.get(gid)
                    if cl_val is None and m_val is None:
                        if str(c.get('value') or '').strip() != '':
                            group_diffs.append({
                                'group_id': gid,
                                'measure_count': None,
                                'class_count': None,
                                'raw_class_value': c.get('value'),
                                'class_minus_measure': None,
                            })
                    elif cl_val is None or m_val is None or abs(cl_val - m_val) > 1e-9:
                        group_diffs.append({
                            'group_id': gid,
                            'measure_count': plain_num(m_val),
                            'class_count': plain_num(cl_val),
                            'raw_class_value': c.get('value'),
                            'class_minus_measure': (
                                plain_num(cl_val - m_val) if cl_val is not None and m_val is not None else None
                            ),
                        })
            if unit_differs or group_diffs:
                deviations.append({
                    'class_index': ci,
                    'denom_index': di,
                    'class_title': cl.get('title'),
                    'json_path': (
                        f'resultsSection.outcomeMeasuresModule.outcomeMeasures[{measure_index}]'
                        f'.classes[{ci}].denoms[{di}]'
                    ),
                    'measure_units': measure_unit,
                    'class_units': cl_unit or None,
                    'unit_differs': unit_differs,
                    'group_deviations': group_diffs,
                })
    return deviations


# ---------------------------------------------------------------- conservative OG -> BG / FG arm alignment

def _contains_whole_phrase(haystack_norm: str, needle_norm: str) -> bool:
    if not haystack_norm or not needle_norm or len(needle_norm) < 3 or haystack_norm == needle_norm:
        return False
    pattern = r'(?<![a-z0-9])' + re.escape(needle_norm) + r'(?![a-z0-9])'
    return bool(re.search(pattern, haystack_norm))


def align_ogs_to_target_groups(
    ogs: list[dict[str, Any]],
    target_groups: list[dict[str, Any]] | None,
    target_module_name: str,
) -> dict[str, Any]:
    """Conservatively align one primary measure's non-Total `OG`s to non-Total `BG` or `FG` groups.

    States (`ARM_ALIGNMENT_STATES`):
      - `module_groups_absent`: target module absent or has zero non-Total groups.
      - `exact_unique_title_match`: unique 1-to-1 equivalence on normalized non-empty title (or
        `(title, description)` when duplicate titles are uniquely disambiguated by non-empty
        descriptions, or unique non-empty description equivalence when at least one title is a
        generic placeholder such as `"Arm 1"`).
      - `ambiguous_duplicate_titles`: duplicate normalized titles/descriptions prevent unique 1-to-1
        resolution. Ties are NEVER broken by array index (`OG000 == BG000`) or participant count.
      - `pooled_or_subset_suspected`: no exact unique match, and an `OG` or target label carries
        pooling/subset cues or properly embeds one or more target/`OG` arm titles.
      - `no_title_match`: non-empty target groups exist, with no title/description match or
        pooling/subset cue.
    """
    if not target_groups:
        return {
            'target_module': target_module_name,
            'module_groups_present': False,
            'og_alignments': [
                {
                    'og_id': og['group_id'],
                    'og_title': og.get('title'),
                    'state': 'module_groups_absent',
                    'matched_target_id': None,
                    'matched_target_title': None,
                    'match_basis': None,
                    'candidate_target_ids': [],
                }
                for og in ogs
            ],
            'matched_target_ids': [],
            'unmatched_target_groups': [],
        }

    og_norm = [
        {
            'id': og['group_id'],
            'title': og.get('title'),
            'desc': og.get('description'),
            'nt': normalize_label(og.get('title')),
            'nd': normalize_label(og.get('description')),
        }
        for og in ogs
    ]
    tg_norm = [
        {
            'id': tg['group_id'],
            'title': tg.get('title'),
            'desc': tg.get('description'),
            'count': tg.get('count') if 'count' in tg else tg.get('started'),
            'nt': normalize_label(tg.get('title')),
            'nd': normalize_label(tg.get('description')),
        }
        for tg in target_groups
    ]

    og_title_freq: dict[str, int] = {}
    for x in og_norm:
        if x['nt']:
            og_title_freq[x['nt']] = og_title_freq.get(x['nt'], 0) + 1

    tg_by_title: dict[str, list[dict[str, Any]]] = {}
    for y in tg_norm:
        if y['nt']:
            tg_by_title.setdefault(y['nt'], []).append(y)

    og_td_freq: dict[tuple[str, str], int] = {}
    for x in og_norm:
        if x['nt'] and x['nd']:
            og_td_freq[(x['nt'], x['nd'])] = og_td_freq.get((x['nt'], x['nd']), 0) + 1

    tg_by_td: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for y in tg_norm:
        if y['nt'] and y['nd']:
            tg_by_td.setdefault((y['nt'], y['nd']), []).append(y)

    og_desc_freq: dict[str, int] = {}
    for x in og_norm:
        if x['nd']:
            og_desc_freq[x['nd']] = og_desc_freq.get(x['nd'], 0) + 1

    tg_by_desc: dict[str, list[dict[str, Any]]] = {}
    for y in tg_norm:
        if y['nd']:
            tg_by_desc.setdefault(y['nd'], []).append(y)

    alignments: list[dict[str, Any]] = []
    claimed_targets: set[str] = set()

    for x in og_norm:
        nt, nd = x['nt'], x['nd']
        state = 'no_title_match'
        matched_target: dict[str, Any] | None = None
        basis: str | None = None
        candidates: list[str] = []

        if nt and nt in tg_by_title:
            same_title_targets = tg_by_title[nt]
            if og_title_freq.get(nt, 0) == 1 and len(same_title_targets) == 1:
                matched_target = same_title_targets[0]
                state = 'exact_unique_title_match'
                basis = 'normalized_title'
                candidates = [matched_target['id']]
            elif nd and og_td_freq.get((nt, nd), 0) == 1 and len(tg_by_td.get((nt, nd), [])) == 1:
                matched_target = tg_by_td[(nt, nd)][0]
                state = 'exact_unique_title_match'
                basis = 'normalized_title_and_description'
                candidates = [matched_target['id']]
            else:
                state = 'ambiguous_duplicate_titles'
                candidates = [y['id'] for y in same_title_targets]
        else:
            # Cross-check title <-> description or placeholder-title + identical description.
            cross_matches = [
                y for y in tg_norm
                if (nt and y['nd'] and nt == y['nd']) or (nd and y['nt'] and nd == y['nt'])
            ]
            if cross_matches:
                # Require uniqueness on the OG side too so two OGs sharing `nt` or `nd` never both
                # claim the same target group.
                og_cross_dupes = sum(
                    1 for ox in og_norm
                    if any(
                        (ox['nt'] and y['nd'] and ox['nt'] == y['nd'])
                        or (ox['nd'] and y['nt'] and ox['nd'] == y['nt'])
                        for y in cross_matches
                    )
                )
                if len(cross_matches) == 1 and og_cross_dupes == 1:
                    matched_target = cross_matches[0]
                    state = 'exact_unique_title_match'
                    basis = 'title_to_description_equivalence'
                    candidates = [matched_target['id']]
                else:
                    state = 'ambiguous_duplicate_titles'
                    candidates = [y['id'] for y in cross_matches]
            elif nd and nd in tg_by_desc:
                # Only allow description-to-description matching when at least one title is empty
                # or a generic placeholder ("Arm 1", "Group A"); substantive differing titles
                # ("Drug 10 mg" vs "Drug 20 mg") sharing boilerplate descriptions must NEVER match.
                desc_targets = tg_by_desc[nd]
                og_is_placeholder = (not nt) or bool(_PLACEHOLDER_TITLE_RE.match(nt))
                compatible_targets = [
                    y for y in desc_targets
                    if og_is_placeholder or (not y['nt']) or bool(_PLACEHOLDER_TITLE_RE.match(y['nt']))
                ]
                if compatible_targets:
                    if og_desc_freq.get(nd, 0) == 1 and len(compatible_targets) == 1:
                        matched_target = compatible_targets[0]
                        state = 'exact_unique_title_match'
                        basis = 'description_with_placeholder_title'
                        candidates = [matched_target['id']]
                    else:
                        state = 'ambiguous_duplicate_titles'
                        candidates = [y['id'] for y in compatible_targets]

        if state == 'no_title_match':
            # Test for conservative pooled/subset suspicion (never treated as aligned!).
            contained_targets = [
                y['id'] for y in tg_norm
                if y['nt'] and (_contains_whole_phrase(nt, y['nt']) or _contains_whole_phrase(nd, y['nt']))
            ]
            containing_targets = [
                y['id'] for y in tg_norm
                if nt and (_contains_whole_phrase(y['nt'], nt) or _contains_whole_phrase(y['nd'], nt))
            ]
            cue_hit = bool(_POOLED_OR_SUBSET_CUE_RE.search(f'{nt} {nd}'.strip()))
            if contained_targets or containing_targets or cue_hit:
                state = 'pooled_or_subset_suspected'
                candidates = sorted(set(contained_targets + containing_targets))

        alignments.append({
            'og_id': x['id'],
            'og_title': x['title'],
            'state': state,
            'matched_target_id': matched_target['id'] if matched_target else None,
            'matched_target_title': matched_target['title'] if matched_target else None,
            'match_basis': basis,
            'candidate_target_ids': candidates,
        })

    # Enforce strict 1-to-1 target uniqueness across all passes: if two or more OGs claimed the
    # same target group (e.g. OG000 via normalized_title and OG001 via title_to_description_equivalence),
    # downgrade every colliding OG to `ambiguous_duplicate_titles` and release the target from
    # `claimed_targets` so neither arm is treated as uniquely aligned.
    target_claim_counts: dict[str, int] = {}
    for al in alignments:
        tid = al['matched_target_id']
        if tid is not None:
            target_claim_counts[tid] = target_claim_counts.get(tid, 0) + 1

    claimed_targets = set()
    for al in alignments:
        tid = al['matched_target_id']
        if tid is not None:
            if target_claim_counts[tid] > 1:
                al['state'] = 'ambiguous_duplicate_titles'
                al['matched_target_id'] = None
                al['matched_target_title'] = None
                al['match_basis'] = None
                al['candidate_target_ids'] = [tid]
            else:
                claimed_targets.add(tid)

    unmatched_targets = [
        {
            'group_id': y['id'],
            'title': y['title'],
            'description': y['desc'],
            'count': y['count'],
        }
        for y in tg_norm
        if y['id'] not in claimed_targets
    ]

    return {
        'target_module': target_module_name,
        'module_groups_present': True,
        'og_alignments': alignments,
        'matched_target_ids': sorted(claimed_targets),
        'unmatched_target_groups': unmatched_targets,
    }


def _compute_two_arm_attrition(
    og_a_id: str,
    og_b_id: str,
    og_counts: dict[str, int | float | None],
    og_titles: dict[str, str | None],
    bg_align_by_og: dict[str, dict[str, Any]],
    fg_align_by_og: dict[str, dict[str, Any]],
    bg_counts: dict[str, int | float | None],
    fg_started_counts: dict[str, int | float | None],
) -> dict[str, Any]:
    """Compute arm-specific analysis attrition and `abs_differential_attrition` for a 2-arm pair
    strictly when both `OG`s have `exact_unique_title_match` to distinct `FG` and/or `BG` groups."""
    n_an_a = og_counts.get(og_a_id)
    n_an_b = og_counts.get(og_b_id)

    def _against(
        align_map: dict[str, dict[str, Any]],
        ref_counts: dict[str, int | float | None],
        ref_label: str,
    ) -> dict[str, Any]:
        al_a = align_map.get(og_a_id)
        al_b = align_map.get(og_b_id)
        if al_a is None or al_b is None:
            return {
                'computable': False,
                'reason': f'og_missing_in_{ref_label}_alignment',
                'arm_a': None, 'arm_b': None,
                'signed_differential_attrition': None,
                'abs_differential_attrition': None,
            }
        if al_a['state'] != 'exact_unique_title_match' or al_b['state'] != 'exact_unique_title_match':
            return {
                'computable': False,
                'reason': f'og_to_{ref_label}_not_exact_unique_match ({al_a["state"]}, {al_b["state"]})',
                'arm_a': None, 'arm_b': None,
                'signed_differential_attrition': None,
                'abs_differential_attrition': None,
            }
        tgt_a = al_a['matched_target_id']
        tgt_b = al_b['matched_target_id']
        if not tgt_a or not tgt_b or tgt_a == tgt_b:
            return {
                'computable': False,
                'reason': f'og_pair_mapped_to_same_{ref_label}_group',
                'arm_a': None, 'arm_b': None,
                'signed_differential_attrition': None,
                'abs_differential_attrition': None,
            }
        if n_an_a is None or n_an_b is None:
            return {
                'computable': False,
                'reason': 'analysed_participants_missing',
                'arm_a': None, 'arm_b': None,
                'signed_differential_attrition': None,
                'abs_differential_attrition': None,
            }
        ref_a = ref_counts.get(tgt_a)
        ref_b = ref_counts.get(tgt_b)
        if ref_a is None or ref_b is None or ref_a <= 0 or ref_b <= 0:
            return {
                'computable': False,
                'reason': f'{ref_label}_denominator_missing_or_non_positive',
                'arm_a': None, 'arm_b': None,
                'signed_differential_attrition': None,
                'abs_differential_attrition': None,
            }
        rate_a = round(1.0 - (float(n_an_a) / float(ref_a)), 6)
        rate_b = round(1.0 - (float(n_an_b) / float(ref_b)), 6)
        return {
            'computable': True,
            'reason': 'ok',
            'arm_a': {
                'og_id': og_a_id,
                'og_title': og_titles.get(og_a_id),
                f'{ref_label}_group_id': tgt_a,
                f'n_{ref_label}': ref_a,
                'n_analysed': n_an_a,
                'attrition_rate': rate_a,
            },
            'arm_b': {
                'og_id': og_b_id,
                'og_title': og_titles.get(og_b_id),
                f'{ref_label}_group_id': tgt_b,
                f'n_{ref_label}': ref_b,
                'n_analysed': n_an_b,
                'attrition_rate': rate_b,
            },
            'signed_differential_attrition': round(rate_a - rate_b, 6),
            'abs_differential_attrition': round(abs(rate_a - rate_b), 6),
        }

    from_started = _against(fg_align_by_og, fg_started_counts, 'started')
    from_baseline = _against(bg_align_by_og, bg_counts, 'baseline')
    return {
        'group_ids': [og_a_id, og_b_id],
        'from_period1_started': from_started,
        'from_baseline': from_baseline,
    }


# ---------------------------------------------------------------- documents, estimands, contrasts

def extract_posted_documents(study: dict[str, Any]) -> dict[str, Any]:
    """Posted protocol, SAP, and ICF flags, filenames, and upload dates from `documentSection`."""
    doc_sec = study.get('documentSection') or {}
    if not isinstance(doc_sec, dict):
        raise TypeError('documentSection is not an object')
    ldm = doc_sec.get('largeDocumentModule') or {}
    if not isinstance(ldm, dict):
        raise TypeError('documentSection.largeDocumentModule is not an object')
    raw_docs = ldm.get('largeDocs') or []
    if not isinstance(raw_docs, list):
        raise TypeError('documentSection.largeDocumentModule.largeDocs is not a list')

    docs: list[dict[str, Any]] = []
    for i, d in enumerate(raw_docs):
        if not isinstance(d, dict):
            raise TypeError(f'largeDocs[{i}] is not an object')
        docs.append({
            'filename': d.get('filename') if isinstance(d.get('filename'), str) else None,
            'upload_date': d.get('uploadDate') if isinstance(d.get('uploadDate'), str) else None,
            'document_date': d.get('date') if isinstance(d.get('date'), str) else None,
            'label': d.get('label') if isinstance(d.get('label'), str) else None,
            'type_abbrev': d.get('typeAbbrev') if isinstance(d.get('typeAbbrev'), str) else None,
            'has_protocol': bool(d.get('hasProtocol')),
            'has_sap': bool(d.get('hasSap')),
            'has_icf': bool(d.get('hasIcf')),
        })

    return {
        'has_posted_protocol': any(d['has_protocol'] for d in docs),
        'has_posted_sap': any(d['has_sap'] for d in docs),
        'has_posted_icf': any(d['has_icf'] for d in docs),
        'n_documents': len(docs),
        'filenames': [d['filename'] for d in docs if d['filename']],
        'upload_dates': [d['upload_date'] for d in docs if d['upload_date']],
        'documents': docs,
        'source_field': 'documentSection.largeDocumentModule.largeDocs',
    }


def detect_missing_data_signals(field_texts: list[tuple[str, Any]]) -> dict[str, Any]:
    """Scan `(json_path, text)` pairs for missing-data / longitudinal estimation methods."""
    evidence: list[dict[str, str]] = []
    methods_seen: set[str] = set()
    for json_path, raw_text in field_texts:
        if not isinstance(raw_text, str) or not raw_text.strip():
            continue
        for method_name, rx in _MISSING_DATA_PATTERNS:
            for match in rx.finditer(raw_text):
                methods_seen.add(method_name)
                evidence.append({
                    'method': method_name,
                    'json_path': json_path,
                    'matched_substring': match.group(0),
                })

    ordered = [m for m in MISSING_DATA_ATOMIC_METHODS if m in methods_seen]
    if not ordered:
        state = 'not_stated'
    elif len(ordered) == 1:
        state = ordered[0]
    else:
        state = 'multiple_methods_named'
    return {
        'state': state,
        'methods_detected': ordered,
        'evidence': evidence,
    }


def detect_intercurrent_event_signals(field_texts: list[tuple[str, Any]]) -> dict[str, Any]:
    """Scan `(json_path, text)` pairs for rescue-medication / intercurrent-event handling signals."""
    evidence: list[dict[str, str]] = []
    has_excluded = False
    has_policy = False
    for json_path, raw_text in field_texts:
        if not isinstance(raw_text, str) or not raw_text.strip():
            continue
        for match in _RESCUE_EXCLUDED_RE.finditer(raw_text):
            has_excluded = True
            evidence.append({
                'signal': 'rescue_excluded_or_censored',
                'json_path': json_path,
                'matched_substring': match.group(0),
                'snippet': _snippet(raw_text, match.start(), match.end()),
            })
        for match in _TREATMENT_POLICY_RE.finditer(raw_text):
            has_policy = True
            evidence.append({
                'signal': 'treatment_policy_regardless_of_rescue_or_discontinuation',
                'json_path': json_path,
                'matched_substring': match.group(0),
                'snippet': _snippet(raw_text, match.start(), match.end()),
            })

    if has_excluded and has_policy:
        state = 'both_co_reported'
    elif has_excluded:
        state = 'rescue_excluded_or_censored'
    elif has_policy:
        state = 'treatment_policy_regardless_of_rescue_or_discontinuation'
    else:
        state = 'not_stated'
    return {
        'state': state,
        'evidence': evidence,
    }


def _mask_margin_noise(text: str) -> str:
    """Replace CI-level, alpha, power, SD, sample-size, and timepoint spans with equal-length spaces
    so match indices `[m.start():m.end()]` remain exact offsets into `text`."""
    masked = text
    for rx in _NOISE_PATTERNS:
        masked = rx.sub(lambda m: ' ' * len(m.group(0)), masked)
    return masked


def _extract_margin_evidence(analysis: dict[str, Any], base_path: str) -> dict[str, str] | None:
    """Detect an explicit numeric non-inferiority or equivalence margin in structured analysis text."""
    ni_comment = analysis.get('nonInferiorityComment')
    if isinstance(ni_comment, str) and ni_comment.strip():
        raw_ni = ni_comment.strip()
        cleaned = _mask_margin_noise(raw_ni)
        m = _MARGIN_IN_NI_COMMENT_RE.search(cleaned)
        if m:
            matched = raw_ni[m.start():m.end()].strip()
            return {
                'json_path': f'{base_path}.nonInferiorityComment',
                'matched_substring': matched,
                'comment_verbatim': raw_ni,
            }

    for field in ('statisticalComment', 'otherAnalysisDescription'):
        txt = analysis.get(field)
        if isinstance(txt, str) and txt.strip():
            raw_txt = txt.strip()
            cleaned = _mask_margin_noise(raw_txt)
            m = _ANCHORED_MARGIN_IN_OTHER_TEXT_RE.search(cleaned)
            if m:
                matched = raw_txt[m.start():m.end()].strip()
                return {
                    'json_path': f'{base_path}.{field}',
                    'matched_substring': matched,
                    'comment_verbatim': raw_txt,
                }
    return None


def classify_analysis_hypothesis(analysis: dict[str, Any], base_path: str) -> dict[str, Any]:
    """Classify hypothesis & non-inferiority/equivalence margin clarity for one `analyses[]` entry."""
    raw_enum = analysis.get('nonInferiorityType')
    enum_str = raw_enum.strip() if isinstance(raw_enum, str) else ''
    margin_ev = _extract_margin_evidence(analysis, base_path)

    text_sources: list[tuple[str, str]] = []
    for field in ('nonInferiorityComment', 'statisticalComment', 'otherAnalysisDescription'):
        val = analysis.get(field)
        if isinstance(val, str) and val.strip():
            text_sources.append((f'{base_path}.{field}', val))

    sup_ev: dict[str, str] | None = None
    ni_ev: dict[str, str] | None = None
    eq_ev: dict[str, str] | None = None
    for path, txt in text_sources:
        if sup_ev is None:
            ms = _SUPERIORITY_CUE_RE.search(txt)
            if ms:
                sup_ev = {'json_path': path, 'matched_substring': ms.group(0)}
        if ni_ev is None:
            mn = _NON_INFERIORITY_CUE_RE.search(txt)
            if mn:
                ni_ev = {'json_path': path, 'matched_substring': mn.group(0)}
        if eq_ev is None:
            me = _EQUIVALENCE_CUE_RE.search(txt)
            if me:
                eq_ev = {'json_path': path, 'matched_substring': me.group(0)}

    disambiguated_from: str | None = None
    disambiguation_ev: dict[str, str] | None = None

    if enum_str == 'SUPERIORITY':
        state = 'superiority_explicit'
    elif enum_str == 'NON_INFERIORITY':
        state = 'non_inferiority_with_margin' if margin_ev else 'non_inferiority_missing_margin'
    elif enum_str == 'EQUIVALENCE':
        state = 'equivalence_with_margin' if margin_ev else 'equivalence_missing_margin'
    elif enum_str == 'OTHER':
        state = 'other_explicit'
    elif enum_str in ('SUPERIORITY_OR_OTHER', 'SUPERIORITY_OR_OTHER_LEGACY'):
        if sup_ev is not None and ni_ev is None and eq_ev is None:
            state = 'superiority_explicit'
            disambiguated_from = enum_str
            disambiguation_ev = sup_ev
        else:
            state = 'ambiguous_legacy_enum'
    elif enum_str in ('NON_INFERIORITY_OR_EQUIVALENCE', 'NON_INFERIORITY_OR_EQUIVALENCE_LEGACY'):
        if ni_ev is not None and eq_ev is None:
            state = 'non_inferiority_with_margin' if margin_ev else 'non_inferiority_missing_margin'
            disambiguated_from = enum_str
            disambiguation_ev = ni_ev
        elif eq_ev is not None and ni_ev is None:
            state = 'equivalence_with_margin' if margin_ev else 'equivalence_missing_margin'
            disambiguated_from = enum_str
            disambiguation_ev = eq_ev
        else:
            state = 'ambiguous_legacy_enum'
    elif not enum_str:
        # When `nonInferiorityType` is absent, check `nonInferiorityComment` only if explicitly populated.
        ni_comment_only = analysis.get('nonInferiorityComment')
        if isinstance(ni_comment_only, str) and ni_comment_only.strip():
            if ni_ev is not None and eq_ev is None:
                state = 'non_inferiority_with_margin' if margin_ev else 'non_inferiority_missing_margin'
            elif eq_ev is not None and ni_ev is None:
                state = 'equivalence_with_margin' if margin_ev else 'equivalence_missing_margin'
            elif sup_ev is not None and ni_ev is None and eq_ev is None:
                state = 'superiority_explicit'
            else:
                state = 'not_stated'
        else:
            state = 'not_stated'
    else:
        state = 'other_explicit'

    return {
        'state': state,
        'raw_non_inferiority_type': raw_enum if isinstance(raw_enum, str) else None,
        'margin_stated': margin_ev is not None,
        'margin_evidence': margin_ev,
        'disambiguated_from_legacy_enum': disambiguated_from,
        'disambiguation_evidence': disambiguation_ev,
    }


def _inspect_measure_contrasts_and_analyses(
    m: dict[str, Any],
    measure_index: int,
    og_counts: dict[str, int | float | None],
    og_titles: dict[str, str | None],
    bg_align_by_og: dict[str, dict[str, Any]],
    fg_align_by_og: dict[str, dict[str, Any]],
    bg_counts: dict[str, int | float | None],
    fg_started_counts: dict[str, int | float | None],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Inspect all `analyses[]` of one `PRIMARY` measure for contrast cardinality, shared-arm
    non-independence, hypothesis clarity, analysis-level estimands, and 2-arm differential attrition."""
    raw_analyses = m.get('analyses') or []
    if not isinstance(raw_analyses, list):
        raise TypeError(f'outcomeMeasures[{measure_index}].analyses is not a list')

    known_og_ids = set(og_titles)
    pairwise_entries: list[tuple[int, list[str]]] = []
    analysis_rows: list[dict[str, Any]] = []
    contrast_attrition: list[dict[str, Any]] = []

    m_base = f'resultsSection.outcomeMeasuresModule.outcomeMeasures[{measure_index}]'
    for ai, a in enumerate(raw_analyses):
        if not isinstance(a, dict):
            raise TypeError(f'{m_base}.analyses[{ai}] is not an object')
        a_path = f'{m_base}.analyses[{ai}]'
        raw_gids = a.get('groupIds') or []
        if not isinstance(raw_gids, list):
            raise TypeError(f'{a_path}.groupIds is not a list')
        clean_gids = [g.strip() for g in raw_gids if isinstance(g, str) and g.strip()]
        distinct_gids = list(dict.fromkeys(clean_gids))
        has_dup_gids = len(clean_gids) != len(distinct_gids)
        unresolved_gids = [g for g in distinct_gids if g not in known_og_ids]

        n_distinct = len(distinct_gids)
        if n_distinct == 0:
            card_state = 'empty'
        elif n_distinct == 1:
            card_state = 'single_arm'
        elif n_distinct == 2:
            card_state = 'pairwise_two_arm'
        else:
            card_state = 'multi_arm_joint'

        if card_state == 'pairwise_two_arm' and not unresolved_gids:
            pairwise_entries.append((ai, distinct_gids))

        a_missing = detect_missing_data_signals([
            (f'{a_path}.statisticalMethod', a.get('statisticalMethod')),
            (f'{a_path}.statisticalComment', a.get('statisticalComment')),
            (f'{a_path}.otherAnalysisDescription', a.get('otherAnalysisDescription')),
        ])
        a_ice = detect_intercurrent_event_signals([
            (f'{a_path}.groupDescription', a.get('groupDescription')),
            (f'{a_path}.statisticalComment', a.get('statisticalComment')),
            (f'{a_path}.otherAnalysisDescription', a.get('otherAnalysisDescription')),
        ])
        hyp = classify_analysis_hypothesis(a, a_path)

        attrition_obj: dict[str, Any] | None = None
        if len(raw_gids) == 2 and card_state == 'pairwise_two_arm' and not unresolved_gids:
            attrition_obj = {
                'measure_index': measure_index,
                'analysis_index': ai,
                **_compute_two_arm_attrition(
                    distinct_gids[0],
                    distinct_gids[1],
                    og_counts,
                    og_titles,
                    bg_align_by_og,
                    fg_align_by_og,
                    bg_counts,
                    fg_started_counts,
                ),
            }
            contrast_attrition.append(attrition_obj)

        analysis_rows.append({
            'analysis_index': ai,
            'json_path': a_path,
            'raw_group_ids': list(raw_gids),
            'distinct_group_ids': distinct_gids,
            'group_ids_cardinality_state': card_state,
            'has_duplicate_group_ids': has_dup_gids,
            'unresolved_group_ids': unresolved_gids,
            'param_type': a.get('paramType'),
            'param_value': a.get('paramValue'),
            'ci_pct_value': a.get('ciPctValue'),
            'ci_num_sides': a.get('ciNumSides'),
            'ci_lower_limit': a.get('ciLowerLimit'),
            'ci_upper_limit': a.get('ciUpperLimit'),
            'p_value': a.get('pValue'),
            'statistical_method': a.get('statisticalMethod'),
            'hypothesis_clarity': hyp,
            'missing_data_signal': a_missing,
            'intercurrent_event_signal': a_ice,
            'differential_attrition': attrition_obj,
            'shares_arm_with_analysis_indices': [],
            'statistically_independent_within_measure': True if card_state == 'pairwise_two_arm' else None,
        })

    # Detect shared-arm pairwise contrasts within this primary measure.
    by_og: dict[str, list[tuple[int, list[str]]]] = {}
    for ai, gids in pairwise_entries:
        for gid in gids:
            by_og.setdefault(gid, []).append((ai, gids))

    shared_groups: list[dict[str, Any]] = []
    peers_by_ai: dict[int, set[int]] = {}
    for gid in sorted(by_og):
        uses = by_og[gid]
        if len(uses) < 2:
            continue
        indices = sorted(ai for ai, _ in uses)
        other_gids = [gids[1] if gids[0] == gid else gids[0] for _, gids in uses]
        pair_sets = {frozenset(gids) for _, gids in uses}
        shared_groups.append({
            'shared_group_id': gid,
            'shared_group_title': og_titles.get(gid),
            'analysis_indices': indices,
            'contrasts': [
                {
                    'analysis_index': ai,
                    'group_ids': gids,
                    'partner_group_id': gids[1] if gids[0] == gid else gids[0],
                    'partner_group_title': og_titles.get(gids[1] if gids[0] == gid else gids[0]),
                }
                for ai, gids in uses
            ],
            'includes_distinct_partner_arms': len(set(other_gids)) >= 2,
            'includes_repeated_identical_pair': len(pair_sets) < len(uses),
            'non_independent': True,
        })
        for ai in indices:
            peers_by_ai.setdefault(ai, set()).update(x for x in indices if x != ai)

    for ai, peers in peers_by_ai.items():
        analysis_rows[ai]['shares_arm_with_analysis_indices'] = sorted(peers)
        analysis_rows[ai]['statistically_independent_within_measure'] = False

    cardinality_counts = {
        s: sum(1 for r in analysis_rows if r['group_ids_cardinality_state'] == s)
        for s in CARDINALITY_STATES
    }
    contrast_summary = {
        'n_analyses': len(analysis_rows),
        'cardinality_counts': cardinality_counts,
        'n_analyses_with_duplicate_group_ids': sum(1 for r in analysis_rows if r['has_duplicate_group_ids']),
        'n_analyses_with_unresolved_group_ids': sum(1 for r in analysis_rows if bool(r['unresolved_group_ids'])),
        'has_shared_arm_contrasts': bool(shared_groups),
        'shared_arm_contrast_groups': shared_groups,
    }
    return analysis_rows, contrast_summary, contrast_attrition


# ---------------------------------------------------------------- full per-trial inspector

def not_retrieved_record(
    nct_id: str,
    reason: str,
    history_snapshots: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Record emitted when a requested NCT ID was not returned by the registry."""
    oh_rec = OH.inspect_outcome_history(None, 'not_retrieved', history_snapshots)
    if nct_id and history_snapshots is not None and nct_id in history_snapshots:
        oh_rec['version_history'] = OH.compare_version_history(None, nct_id, history_snapshots)
        oh_rec['version_history_state'] = oh_rec['version_history']['version_history_state']
    return {
        'nct_id': nct_id,
        'brief_title': None,
        'method_version': METHOD_VERSION,
        'results_status': 'not_retrieved',
        'retrieval': {'status': 'failed', 'reason': reason},
        'parse': {'status': 'ok', 'failures': []},
        'design': None,
        'posted_documents': None,
        'denominator_funnel': None,
        'arm_alignment': None,
        'primary_measures': None,
        'estimand_signals': None,
        'contrast_structure': None,
        'outcome_history': oh_rec,
    }


def inspect_study(
    study: Any,
    history_snapshots: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run deterministic comparability, funnel, arm-alignment, estimand, and outcome-history checks
    on one ClinicalTrials.gov API v2 study record."""
    nct = nct_id_of(study)
    failures: list[dict[str, str]] = []

    if not isinstance(study, dict):
        oh_bad = OH.inspect_outcome_history(study, 'unknown', history_snapshots)
        return {
            'nct_id': nct,
            'brief_title': None,
            'method_version': METHOD_VERSION,
            'results_status': 'unknown',
            'retrieval': {'status': 'ok', 'reason': None},
            'parse': {'status': 'failed', 'failures': [{'section': 'record', 'reason': 'study record is not an object'}]},
            'design': None,
            'posted_documents': None,
            'denominator_funnel': None,
            'arm_alignment': None,
            'primary_measures': None,
            'estimand_signals': None,
            'contrast_structure': None,
            'outcome_history': oh_bad,
        }

    has_res = study.get('hasResults')
    rs = study.get('resultsSection')
    if has_res is True and isinstance(rs, dict) and bool(rs):
        status = 'posted'
    elif has_res is False:
        status = 'no_posted_results'
    else:
        status = 'unknown'
        failures.append({
            'section': 'record',
            'reason': (
                'hasResults is true but resultsSection is absent or empty'
                if has_res is True else 'hasResults is absent or not a boolean'
            ),
        })

    protocol = study.get('protocolSection') if isinstance(study.get('protocolSection'), dict) else {}
    ident = protocol.get('identificationModule') if isinstance(protocol.get('identificationModule'), dict) else {}
    brief_title = ident.get('briefTitle') if isinstance(ident.get('briefTitle'), str) else None

    def _read_design() -> dict[str, Any]:
        dm = protocol.get('designModule') or {}
        if not isinstance(dm, dict):
            raise TypeError('protocolSection.designModule is not an object')
        di = dm.get('designInfo') or {}
        if not isinstance(di, dict):
            raise TypeError('protocolSection.designModule.designInfo is not an object')
        phases = dm.get('phases')
        return {
            'study_type': dm.get('studyType') if isinstance(dm.get('studyType'), str) else None,
            'allocation': di.get('allocation') if isinstance(di.get('allocation'), str) else None,
            'intervention_model': di.get('interventionModel') if isinstance(di.get('interventionModel'), str) else None,
            'phases': list(phases) if isinstance(phases, list) else None,
        }

    design = _guard('design', failures, _read_design)
    docs = _guard('posted_documents', failures, lambda: extract_posted_documents(study))
    enrollment = _guard('registered_enrollment', failures, lambda: extract_registered_enrollment(protocol))
    posted = status == 'posted'
    rs_dict = rs if posted and isinstance(rs, dict) else {}

    p1_started = _guard(
        'participant_flow',
        failures,
        lambda: extract_period1_started(rs_dict.get('participantFlowModule'), posted),
    )
    b_total = _guard(
        'baseline_characteristics',
        failures,
        lambda: extract_baseline_total(rs_dict.get('baselineCharacteristicsModule'), posted),
    )

    oh_rec = OH.inspect_outcome_history(study, status, history_snapshots)
    if oh_rec['parse_failures']:
        failures.extend(oh_rec['parse_failures'])

    if not posted:
        funnel_no_res: dict[str, Any] | None = None
        if enrollment is not None and p1_started is not None and b_total is not None:
            funnel_no_res = {
                'registered_enrollment': enrollment,
                'period1_started': p1_started,
                'baseline_total': b_total,
                'baseline_minus_period1_started': None,
                'baseline_over_period1_started': None,
                'primary_measures': [],
            }
        return {
            'nct_id': nct,
            'brief_title': brief_title,
            'method_version': METHOD_VERSION,
            'results_status': status,
            'retrieval': {'status': 'ok', 'reason': None},
            'parse': {'status': 'failed' if failures else 'ok', 'failures': failures},
            'design': design,
            'posted_documents': docs,
            'denominator_funnel': funnel_no_res,
            'arm_alignment': None,
            'primary_measures': None,
            'estimand_signals': None,
            'contrast_structure': None,
            'outcome_history': oh_rec,
        }

    # Posted results: inspect primary measures, arm alignment, estimands, and contrast structure.
    def _inspect_posted_primary_measures() -> tuple[
        list[dict[str, Any]],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
    ]:
        om_mod = rs_dict.get('outcomeMeasuresModule') or {}
        if not isinstance(om_mod, dict):
            raise TypeError('resultsSection.outcomeMeasuresModule is not an object')
        raw_measures = om_mod.get('outcomeMeasures') or []
        if not isinstance(raw_measures, list):
            raise TypeError('resultsSection.outcomeMeasuresModule.outcomeMeasures is not a list')

        non_total_bg: list[dict[str, Any]] | None = None
        bg_counts: dict[str, int | float | None] = {}
        if b_total is not None and b_total['groups'] is not None:
            all_bg = b_total['groups']
            non_total = [g for g in all_bg if not g['is_total_group']]
            non_total_bg = non_total if non_total else None
            bg_counts = {g['group_id']: g['count'] for g in non_total}

        non_total_fg: list[dict[str, Any]] | None = None
        fg_started_counts: dict[str, int | float | None] = {}
        if p1_started is not None and p1_started['arms'] is not None:
            non_total_fg = p1_started['arms'] if p1_started['arms'] else None
            fg_started_counts = {g['group_id']: g['started'] for g in p1_started['arms']}

        bt_val = b_total['value'] if b_total is not None else None
        fs_val = p1_started['value'] if p1_started is not None else None

        primary_out: list[dict[str, Any]] = []
        all_missing_texts: list[tuple[str, Any]] = []
        all_ice_texts: list[tuple[str, Any]] = []
        all_bg_matched_across_primaries: set[str] = set()
        all_bg_pooled_candidates_across_primaries: set[str] = set()
        any_og_matched_bg = False
        max_non_total_og_count = 0
        all_contrast_attrition: list[dict[str, Any]] = []

        for mi, m in enumerate(raw_measures):
            if not isinstance(m, dict):
                raise TypeError(f'outcomeMeasures[{mi}] is not an object')
            if m.get('type') != 'PRIMARY':
                continue

            m_base = f'resultsSection.outcomeMeasuresModule.outcomeMeasures[{mi}]'
            denom_info = _extract_measure_denominators(m, mi)
            an_val = denom_info['analysed_total']

            an_minus_bt = plain_num(float(an_val) - float(bt_val)) if an_val is not None and bt_val is not None else None
            an_minus_fs = plain_num(float(an_val) - float(fs_val)) if an_val is not None and fs_val is not None else None
            an_over_fs = (
                round(float(an_val) / float(fs_val), 6)
                if an_val is not None and fs_val is not None and fs_val > 0 else None
            )
            an_over_bt = (
                round(float(an_val) / float(bt_val), 6)
                if an_val is not None and bt_val is not None and bt_val > 0 else None
            )

            non_total_ogs = [g for g in denom_info['og_denominators'] if not g['is_total_group']]
            max_non_total_og_count = max(max_non_total_og_count, len(non_total_ogs))
            og_counts = {g['group_id']: g['count'] for g in non_total_ogs}
            og_titles = {g['group_id']: g.get('title') for g in non_total_ogs}

            bg_alignment = align_ogs_to_target_groups(non_total_ogs, non_total_bg, 'baselineCharacteristicsModule')
            fg_alignment = align_ogs_to_target_groups(non_total_ogs, non_total_fg, 'participantFlowModule')

            all_bg_matched_across_primaries.update(bg_alignment['matched_target_ids'])
            measure_pooled_bg_ids = {
                cid
                for al in bg_alignment['og_alignments']
                if al['state'] in ('pooled_or_subset_suspected', 'ambiguous_duplicate_titles')
                for cid in al['candidate_target_ids']
            }
            all_bg_pooled_candidates_across_primaries.update(measure_pooled_bg_ids)
            if bg_alignment['matched_target_ids']:
                any_og_matched_bg = True

            # Per-measure extra baseline cohort flag: non-Total BGs neither uniquely matched nor
            # implicated in pooled/duplicate candidates, when >= 1 OG matched BG or non-Total BGs
            # outnumber non-Total OGs on a populated measure.
            n_nt_bg = len(non_total_bg) if non_total_bg else 0
            measure_unaccounted_bg = [
                g for g in bg_alignment['unmatched_target_groups']
                if g['group_id'] not in measure_pooled_bg_ids
            ]
            measure_has_extra_bg = bool(
                non_total_ogs
                and measure_unaccounted_bg
                and (bool(bg_alignment['matched_target_ids']) or n_nt_bg > len(non_total_ogs))
            )

            bg_align_map = {a['og_id']: a for a in bg_alignment['og_alignments']}
            fg_align_map = {a['og_id']: a for a in fg_alignment['og_alignments']}

            analysis_rows, contrast_summary, c_attr = _inspect_measure_contrasts_and_analyses(
                m, mi, og_counts, og_titles, bg_align_map, fg_align_map, bg_counts, fg_started_counts
            )
            all_contrast_attrition.extend(c_attr)

            measure_two_arm_attr: dict[str, Any] | None = None
            if len(non_total_ogs) == 2:
                measure_two_arm_attr = _compute_two_arm_attrition(
                    non_total_ogs[0]['group_id'],
                    non_total_ogs[1]['group_id'],
                    og_counts,
                    og_titles,
                    bg_align_map,
                    fg_align_map,
                    bg_counts,
                    fg_started_counts,
                )

            m_missing_texts: list[tuple[str, Any]] = [
                (f'{m_base}.populationDescription', m.get('populationDescription')),
            ]
            m_ice_texts: list[tuple[str, Any]] = [
                (f'{m_base}.title', m.get('title')),
                (f'{m_base}.description', m.get('description')),
                (f'{m_base}.populationDescription', m.get('populationDescription')),
            ]
            for ai, raw_a in enumerate(m.get('analyses') or []):
                if isinstance(raw_a, dict):
                    m_missing_texts.extend([
                        (f'{m_base}.analyses[{ai}].statisticalMethod', raw_a.get('statisticalMethod')),
                        (f'{m_base}.analyses[{ai}].statisticalComment', raw_a.get('statisticalComment')),
                        (f'{m_base}.analyses[{ai}].otherAnalysisDescription', raw_a.get('otherAnalysisDescription')),
                    ])
                    m_ice_texts.extend([
                        (f'{m_base}.analyses[{ai}].groupDescription', raw_a.get('groupDescription')),
                        (f'{m_base}.analyses[{ai}].statisticalComment', raw_a.get('statisticalComment')),
                        (f'{m_base}.analyses[{ai}].otherAnalysisDescription', raw_a.get('otherAnalysisDescription')),
                    ])

            all_missing_texts.extend(m_missing_texts)
            all_ice_texts.extend(m_ice_texts)

            m_missing = detect_missing_data_signals(m_missing_texts)
            m_ice = detect_intercurrent_event_signals(m_ice_texts)

            pop_desc = m.get('populationDescription')
            primary_out.append({
                'measure_index': mi,
                'json_path': m_base,
                'title': m.get('title') if isinstance(m.get('title'), str) else None,
                'type': 'PRIMARY',
                'unit_of_measure': m.get('unitOfMeasure') if isinstance(m.get('unitOfMeasure'), str) else None,
                'param_type': m.get('paramType') if isinstance(m.get('paramType'), str) else None,
                'dispersion_type': m.get('dispersionType') if isinstance(m.get('dispersionType'), str) else None,
                'time_frame': m.get('timeFrame') if isinstance(m.get('timeFrame'), str) else None,
                'population_description': pop_desc if isinstance(pop_desc, str) else None,
                'denom_state': denom_info['denom_state'],
                'reported_denom_units': denom_info['reported_units'],
                'og_denominators': denom_info['og_denominators'],
                'analysed_total': an_val,
                'analysed_minus_baseline_total': an_minus_bt,
                'analysed_minus_period1_started': an_minus_fs,
                'analysed_over_period1_started': an_over_fs,
                'analysed_over_baseline_total': an_over_bt,
                'class_denom_deviations': denom_info['class_denom_deviations'],
                'has_class_denom_deviation': denom_info['has_class_denom_deviation'],
                'og_to_bg_alignment': bg_alignment,
                'og_to_fg_alignment': fg_alignment,
                'extra_baseline_groups_outside_measure': (
                    measure_unaccounted_bg if measure_has_extra_bg else []
                ),
                'has_extra_baseline_groups_outside_measure': measure_has_extra_bg,
                'measure_two_arm_attrition': measure_two_arm_attr,
                'missing_data_signal': m_missing,
                'intercurrent_event_signal': m_ice,
                'analyses': analysis_rows,
                'contrast_structure': contrast_summary,
            })

        # Trial-level extra baseline cohorts outside all primary measures.
        unaccounted_bg_all = [
            {
                'group_id': g['group_id'],
                'title': g.get('title'),
                'description': g.get('description'),
                'count': g.get('count'),
            }
            for g in (non_total_bg or [])
            if g['group_id'] not in all_bg_matched_across_primaries
            and g['group_id'] not in all_bg_pooled_candidates_across_primaries
        ]
        n_nt_bg_total = len(non_total_bg) if non_total_bg else 0
        extra_bg_flag = bool(
            primary_out
            and max_non_total_og_count > 0
            and unaccounted_bg_all
            and (any_og_matched_bg or n_nt_bg_total > max_non_total_og_count)
        )
        extra_bg_list = unaccounted_bg_all if extra_bg_flag else []

        bg_state_counts = {
            s: sum(1 for pm in primary_out for a in pm['og_to_bg_alignment']['og_alignments'] if a['state'] == s)
            for s in ARM_ALIGNMENT_STATES
        }
        fg_state_counts = {
            s: sum(1 for pm in primary_out for a in pm['og_to_fg_alignment']['og_alignments'] if a['state'] == s)
            for s in ARM_ALIGNMENT_STATES
        }
        arm_alignment_summary = {
            'n_primary_measures': len(primary_out),
            'n_primary_measure_ogs': sum(len(pm['og_to_bg_alignment']['og_alignments']) for pm in primary_out),
            'og_to_bg_state_counts': bg_state_counts,
            'og_to_fg_state_counts': fg_state_counts,
            'extra_baseline_groups_outside_primary': extra_bg_flag,
            'extra_baseline_groups': extra_bg_list,
            'two_arm_contrast_attrition': all_contrast_attrition,
        }

        trial_missing = detect_missing_data_signals(all_missing_texts)
        trial_ice = detect_intercurrent_event_signals(all_ice_texts)
        hyp_counts = {
            s: sum(1 for pm in primary_out for a in pm['analyses'] if a['hypothesis_clarity']['state'] == s)
            for s in HYPOTHESIS_CLARITY_STATES
        }
        estimand_summary = {
            'missing_data_signal': trial_missing,
            'intercurrent_event_signal': trial_ice,
            'hypothesis_clarity_counts': hyp_counts,
        }

        card_totals = {
            s: sum(pm['contrast_structure']['cardinality_counts'][s] for pm in primary_out)
            for s in CARDINALITY_STATES
        }
        contrast_rollup = {
            'n_primary_analyses': sum(pm['contrast_structure']['n_analyses'] for pm in primary_out),
            'cardinality_counts': card_totals,
            'n_analyses_with_duplicate_group_ids': sum(
                pm['contrast_structure']['n_analyses_with_duplicate_group_ids'] for pm in primary_out
            ),
            'n_analyses_with_unresolved_group_ids': sum(
                pm['contrast_structure']['n_analyses_with_unresolved_group_ids'] for pm in primary_out
            ),
            'has_shared_arm_contrasts': any(pm['contrast_structure']['has_shared_arm_contrasts'] for pm in primary_out),
            'measures_with_shared_arm_contrasts': [
                {
                    'measure_index': pm['measure_index'],
                    'measure_title': pm['title'],
                    'shared_arm_contrast_groups': pm['contrast_structure']['shared_arm_contrast_groups'],
                }
                for pm in primary_out
                if pm['contrast_structure']['has_shared_arm_contrasts']
            ],
        }
        return primary_out, arm_alignment_summary, estimand_summary, contrast_rollup

    inspected = _guard('primary_measures', failures, _inspect_posted_primary_measures)
    if inspected is not None:
        primaries, arm_align, estimands, contrasts = inspected
    else:
        primaries, arm_align, estimands, contrasts = None, None, None, None

    bt_val_top = b_total['value'] if b_total is not None else None
    fs_val_top = p1_started['value'] if p1_started is not None else None
    bt_minus_fs = (
        plain_num(float(bt_val_top) - float(fs_val_top))
        if bt_val_top is not None and fs_val_top is not None else None
    )
    bt_over_fs = (
        round(float(bt_val_top) / float(fs_val_top), 6)
        if bt_val_top is not None and fs_val_top is not None and fs_val_top > 0 else None
    )

    funnel: dict[str, Any] | None = None
    if enrollment is not None and p1_started is not None and b_total is not None:
        pm_funnels = [
            {
                'measure_index': pm['measure_index'],
                'title': pm['title'],
                'denom_state': pm['denom_state'],
                'analysed_total': pm['analysed_total'],
                'analysed_minus_baseline_total': pm['analysed_minus_baseline_total'],
                'analysed_minus_period1_started': pm['analysed_minus_period1_started'],
                'analysed_over_period1_started': pm['analysed_over_period1_started'],
                'analysed_over_baseline_total': pm['analysed_over_baseline_total'],
                'has_class_denom_deviation': pm['has_class_denom_deviation'],
                'n_class_denom_deviations': len(pm['class_denom_deviations']),
            }
            for pm in (primaries or [])
        ]
        funnel = {
            'registered_enrollment': enrollment,
            'period1_started': p1_started,
            'baseline_total': b_total,
            'baseline_minus_period1_started': bt_minus_fs,
            'baseline_over_period1_started': bt_over_fs,
            'primary_measures': pm_funnels,
        }

    return {
        'nct_id': nct,
        'brief_title': brief_title,
        'method_version': METHOD_VERSION,
        'results_status': status,
        'retrieval': {'status': 'ok', 'reason': None},
        'parse': {'status': 'failed' if failures else 'ok', 'failures': failures},
        'design': design,
        'posted_documents': docs,
        'denominator_funnel': funnel,
        'arm_alignment': arm_align,
        'primary_measures': primaries,
        'estimand_signals': estimands,
        'contrast_structure': contrasts,
        'outcome_history': oh_rec,
    }
