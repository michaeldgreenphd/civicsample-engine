"""Results-structure facts and subgroup-reporting states for one ClinicalTrials.gov study record.

Pure functions over one API v2 study record (`protocolSection`, `resultsSection`, `hasResults`).
No I/O apart from `load_reviews`, which reads the human review file. Every rule here is a port of
the audit scripts under docs/audits/outcomes_results_structure/second_pass/scripts/ (usable.py,
subgroup_rule.py, denominators.py); docs/audits/outcomes_results_structure/port_check/ compares
this port with the audit's own output on the audit's 2,000 cached records.

What a record says, and what it does not:

- It describes what the posted results section contains and whether each table can be read
  (rules B1, F1, O1, A1). It does not say the content is correct or complete.
- "Comparative" (C1, C2) is the structure of the record: two or more groups on a primary measure.
  Two groups are not necessarily two randomised arms.
- A subgroup signal is where the registrant's wording in 18 structured fields looks like subgroup
  reporting (rule SG2). It is not a finding that a subgroup result exists; only a human review
  record (outcomes/subgroup_reviews.csv) can settle a signal.
- No result is inferred for a subgroup from a trial's overall result and baseline proportions.
- Posted documents and publications are not read; `document_and_publication_review` says so in
  every record, so absence in the structured fields is never presented as absence everywhere.
- Outcome switching is not assessed.

Conventions:

- "Numeric" means the registry string parses as a finite number after removing thousands commas
  (`is_num`). 'NA', '' and absent are not numeric and are never read as zero.
- Absence is recorded as None with a reason beside it, never as zero.
- A section that cannot be parsed is recorded in the record's `parse.failures` with its reason;
  the record is kept, and its values for that section are None.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Callable, TypeVar

METHOD_VERSION = 'results-structure-1'
SUBGROUP_RULE = 'SG2'

# The four participant characteristics a subgroup state is given for.
DIMENSIONS = ('age', 'sex', 'race', 'ethnicity')
# A signal's dimension. 'other' is a signal that does not name one of the four: the bare words
# "subgroup" / "subset" / "subpopulation" (KEYWORD), or national-origin levels such as Japanese
# versus non-Japanese (ORIGIN). See `signal_dimension`.
SIGNAL_DIMENSIONS = DIMENSIONS + ('other',)

NOT_YET_PROCESSED = 'not_yet_processed'
FAILED_OR_INDETERMINATE = 'processing_failed_or_indeterminate'
NONE_DETECTED = 'no_explicit_subgroup_results_detected_in_reviewed_structured_fields'
DETECTED = 'explicit_subgroup_results_detected'
# Describes the registry, not our processing: the trial has no posted results to read.
NOT_APPLICABLE = 'not_applicable_no_posted_results'
SUBGROUP_STATES = (NOT_YET_PROCESSED, FAILED_OR_INDETERMINATE, NONE_DETECTED, DETECTED)
ALL_SUBGROUP_STATES = SUBGROUP_STATES + (NOT_APPLICABLE,)

# Constant for now: no posted document or publication is read by this method.
DOCUMENT_AND_PUBLICATION_REVIEW = 'not_reviewed'

RESULTS_STATUSES = ('posted', 'no_posted_results', 'unknown', 'not_retrieved')
# The four substantive results modules. moreInfoModule (point of contact, agreement statement) is
# administrative and is not counted.
MODULES = {
    'baseline': ('baselineCharacteristicsModule', 'B1'),
    'participant_flow': ('participantFlowModule', 'F1'),
    'outcome_measures': ('outcomeMeasuresModule', 'O1'),
    'adverse_events': ('adverseEventsModule', 'A1'),
}
OUTCOME_TYPES = ('PRIMARY', 'SECONDARY', 'OTHER_PRE_SPECIFIED', 'POST_HOC')

_OM = 'resultsSection.outcomeMeasuresModule.outcomeMeasures[]'
ANALYSIS_TEXT_FIELDS = ('groupDescription', 'statisticalComment', 'otherAnalysisDescription', 'nonInferiorityComment',
                        'pValueComment', 'estimateComment', 'ciLowerLimitComment', 'ciUpperLimitComment',
                        'statisticalMethod')
# The 18 fields rule SG2 reads, per posted outcome measure of every type.
FIELDS_INSPECTED = tuple(f'{_OM}.{f}' for f in (
    'title', 'description', 'populationDescription', 'classes[].title', 'classes[].categories[].title',
    'groups[].title', 'groups[].description', 'analyses[].groupIds',
    *(f'analyses[].{k}' for k in ANALYSIS_TEXT_FIELDS),
    'classes[].categories[].measurements[].comment'))
FIELDS_NOT_INSPECTED = (
    f'{_OM}.timeFrame', f'{_OM}.unitOfMeasure', f'{_OM}.paramType',
    'resultsSection.baselineCharacteristicsModule (all fields)',
    'resultsSection.participantFlowModule (all fields)',
    'resultsSection.adverseEventsModule (all fields)',
    'resultsSection.moreInfoModule (all fields)',
    'protocolSection (all free text, including eligibility criteria and registered outcome descriptions)',
    'documentSection: posted protocol, statistical analysis plan and consent documents',
    'publications and clinical study reports',
    'historical versions of the registry record',
)

T = TypeVar('T')


# ---------------------------------------------------------------- numbers

def is_num(v: Any) -> bool:
    """True when a registry value parses as a finite number ('NA', '', None do not).

    Port of the audit's common.is_num: thousands commas and surrounding whitespace are removed
    first. A value that is not numeric is never treated as zero by any caller."""
    if v is None:
        return False
    try:
        x = float(str(v).replace(',', '').strip())
    except ValueError:
        return False
    return math.isfinite(x)


def num(v: Any) -> float | None:
    """The number a registry value holds, or None when it is not numeric."""
    return float(str(v).replace(',', '').strip()) if is_num(v) else None


def _plain(x: float | None) -> int | float | None:
    """A whole number as an int, so a participant count is published as 120 and not 120.0."""
    if x is None:
        return None
    return int(x) if float(x).is_integer() else x


# ---------------------------------------------------------------- rule T and the usable-content rules

def participants_row(denoms: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The denominator row in participants: `units == "Participants"` (case-insensitive) if there
    is one, else the first row, else None. Some records carry a second row in another unit (eyes,
    lesions); that row is not the participant count."""
    for d in denoms:
        if (d.get('units') or '').strip().casefold() == 'participants':
            return d
    return denoms[0] if denoms else None


def total_group(bmod: dict[str, Any]) -> tuple[str | None, str]:
    """Rule T: the baseline group that holds the trial total. Returns (group id or None, method).

    One group: that group. Else the group whose trimmed, case-folded title is exactly "total".
    Else, with three or more groups, a last group whose participant denominator equals the sum of
    all earlier groups' and is above zero (a total under another title, such as "Total Title" or
    "Pooled ..."). Else none: the arms are summed by the caller. The third case exists because a
    rule that takes "the group titled Total, else the sum of all groups" counts those trials
    twice."""
    groups = bmod.get('groups') or []
    if len(groups) == 1:
        return groups[0]['id'], 'single_group'
    for g in groups:
        if (g.get('title') or '').strip().casefold() == 'total':
            return g['id'], 'titled_total'
    row = participants_row(bmod.get('denoms') or [])
    if row and len(groups) >= 3:
        cnt = {c['groupId']: num(c.get('value')) for c in row.get('counts', [])}
        vals = [cnt.get(g['id']) for g in groups]
        if None not in vals and vals[-1] > 0 and abs(vals[-1] - sum(vals[:-1])) < 1e-9:
            return groups[-1]['id'], 'last_equals_sum_untitled_total'
    return None, 'no_total_sum_arms'


def baseline_usable(bmod: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Rule B1. Usable when at least one baseline measure has, for the Total group (rule T) or for
    every arm (every non-Total group): (a) a numeric value in
    `classes[].categories[].measurements[]`, and (b) a numeric denominator above zero in the
    module's `denoms[]`, the measure's `denoms[]` or a class's `denoms[]`.

    Returns (usable, reason, detail). The reason of a failure says what is missing; a baseline
    table that is a template with category titles and zero participants fails here."""
    groups = [g['id'] for g in bmod.get('groups') or []]
    if not groups:
        return False, 'no_groups', {}
    tot, method = total_group(bmod)
    arms = [g for g in groups if g != tot] or groups
    mod_den: dict[str, list[Any]] = defaultdict(list)
    for d in bmod.get('denoms') or []:
        for c in d.get('counts', []):
            mod_den[c['groupId']].append(c.get('value'))
    ok_total = ok_arms = False
    n_measures = n_measures_numeric = 0
    for m in bmod.get('measures') or []:
        n_measures += 1
        den: dict[str, list[Any]] = defaultdict(list, {k: list(v) for k, v in mod_den.items()})
        for d in m.get('denoms') or []:
            for c in d.get('counts', []):
                den[c['groupId']].append(c.get('value'))
        has_val: set[str] = set()
        for cl in m.get('classes') or []:
            for d in cl.get('denoms') or []:
                for c in d.get('counts', []):
                    den[c['groupId']].append(c.get('value'))
            for cat in cl.get('categories') or []:
                for x in cat.get('measurements') or []:
                    if is_num(x.get('value')):
                        has_val.add(x['groupId'])
        if has_val:
            n_measures_numeric += 1
        good = {g for g in groups if g in has_val and any(is_num(v) and num(v) > 0 for v in den[g])}
        if tot is not None and tot in good:
            ok_total = True
        if arms and all(g in good for g in arms):
            ok_arms = True
    usable = ok_total or ok_arms
    reason = 'ok' if usable else ('no_measures' if not n_measures else
                                  ('no_numeric_values' if not n_measures_numeric
                                   else 'values_or_denominators_incomplete'))
    return usable, reason, {'total_method': method, 'ok_for_total': ok_total, 'ok_for_every_arm': ok_arms,
                            'n_measures': n_measures, 'n_measures_with_numeric': n_measures_numeric}


def flow_usable(fmod: dict[str, Any]) -> tuple[bool, str]:
    """Rule F1. Usable when at least one period has a STARTED and a COMPLETED milestone that each
    carry a numeric `numSubjects` for every flow group, with STARTED above zero in at least one
    group. A record closed without data posts STARTED = 0 in every group and fails here."""
    groups = [g['id'] for g in fmod.get('groups') or []]
    if not groups:
        return False, 'no_groups'
    any_started = any_completed = False
    for p in fmod.get('periods') or []:
        got: dict[str, dict[str, Any]] = {}
        for ms in p.get('milestones') or []:
            if ms.get('type') in ('STARTED', 'COMPLETED'):
                got[ms['type']] = {a['groupId']: a.get('numSubjects') for a in ms.get('achievements') or []}
        any_started |= 'STARTED' in got
        any_completed |= 'COMPLETED' in got
        if 'STARTED' in got and 'COMPLETED' in got:
            if all(is_num(got['STARTED'].get(g)) and is_num(got['COMPLETED'].get(g)) for g in groups) \
                    and any(num(got['STARTED'][g]) > 0 for g in groups):
                return True, 'ok'
    if not any_started:
        return False, 'no_STARTED_milestone'
    if not any_completed:
        return False, 'no_COMPLETED_milestone'
    return False, 'counts_missing_for_a_group_or_all_zero'


def measure_usable(m: dict[str, Any]) -> tuple[bool, bool, str]:
    """Rule O1 on one outcome measure. Returns (usable, strict, reason).

    Usable: a non-blank `unitOfMeasure`; at least one group; a numeric number analysed for every
    group, read from the measure-level `denoms[]` (the Participants row if present, else the first
    row); number analysed above zero in at least one group; and a numeric value in
    `classes[].categories[].measurements[]` for every group with number analysed above zero.
    Strict additionally requires number analysed above zero in every group.

    A measure with no unit and no classes is a registered outcome with no data table posted
    (`no_data_table`); that is the commonest failure."""
    if not (m.get('unitOfMeasure') or '').strip():
        return False, False, 'no_unit' if m.get('classes') else 'no_data_table'
    groups = [g['id'] for g in m.get('groups') or []]
    if not groups:
        return False, False, 'no_groups'
    row = participants_row(m.get('denoms') or [])
    if not row:
        return False, False, 'no_number_analysed'
    den = {c['groupId']: c.get('value') for c in row.get('counts', [])}
    if not all(is_num(den.get(g)) for g in groups):
        return False, False, 'number_analysed_missing_for_a_group'
    pos = [g for g in groups if num(den[g]) > 0]
    if not pos:
        return False, False, 'zero_analysed_in_every_group'
    has_val = _groups_with_values(m)
    if not all(g in has_val for g in pos):
        return False, False, 'no_numeric_value_for_an_analysed_group'
    return True, len(pos) == len(groups), 'ok'


def _groups_with_values(m: dict[str, Any]) -> set[str]:
    """Group ids with at least one numeric measurement value on this measure."""
    has_val: set[str] = set()
    for cl in m.get('classes') or []:
        for cat in cl.get('categories') or []:
            for x in cat.get('measurements') or []:
                if is_num(x.get('value')):
                    has_val.add(x['groupId'])
    return has_val


def ae_usable(amod: dict[str, Any] | None) -> tuple[bool, str, dict[str, Any]]:
    """Rule A1. Usable when `eventGroups[]` is non-empty; every event group has a numeric
    `seriousNumAtRisk` and `otherNumAtRisk`; at least one group has either above zero; and either
    at least one row of `seriousEvents[]` / `otherEvents[]` has a numeric `numAffected` or
    `numEvents` in its `stats[]`, or every event group has a numeric `seriousNumAffected` and
    `otherNumAffected` (explicit zeros or totals).

    A module that is present with zero participants at risk in every group (adverse events not
    collected or not reported) is present but not usable. The all-cause mortality table is not
    required: it did not exist before 2017."""
    if not amod:
        return False, 'module_absent', {}
    eg = amod.get('eventGroups') or []
    if not eg:
        return False, 'no_event_groups', {}
    at_risk_ok = all(is_num(g.get('seriousNumAtRisk')) and is_num(g.get('otherNumAtRisk')) for g in eg)
    any_pos = any((is_num(g.get('seriousNumAtRisk')) and num(g['seriousNumAtRisk']) > 0) or
                  (is_num(g.get('otherNumAtRisk')) and num(g['otherNumAtRisk']) > 0) for g in eg)
    rows = 0
    for key in ('seriousEvents', 'otherEvents'):
        for ev in amod.get(key) or []:
            if any(is_num(st.get('numAffected')) or is_num(st.get('numEvents')) for st in ev.get('stats') or []):
                rows += 1
    zeros = all(is_num(g.get('seriousNumAffected')) and is_num(g.get('otherNumAffected')) for g in eg)
    explicit_zero_only = rows == 0 and zeros and all(
        num(g['seriousNumAffected']) == 0 and num(g['otherNumAffected']) == 0 for g in eg)
    info = {'event_rows': rows, 'explicit_zero_only': explicit_zero_only,
            'deaths_table': all(is_num(g.get('deathsNumAtRisk')) for g in eg),
            'serious_rows': len(amod.get('seriousEvents') or []), 'other_rows': len(amod.get('otherEvents') or [])}
    if not at_risk_ok:
        return False, 'numbers_at_risk_missing_for_a_group', info
    if not any_pos:
        return False, 'zero_at_risk_in_every_group', info
    if rows == 0 and not zeros:
        return False, 'no_event_rows_and_no_group_totals', info
    return True, 'ok', info


# ---------------------------------------------------------------- statistical analyses (rule S1)

_P_BOUND = re.compile(r'^[<>≤≥]\s*=?\s*(.+)$')


def p_value_form(v: Any) -> str:
    """How a posted p-value is written: 'absent' (no value), 'number' (a plain number such as
    "0.023"; the registry also posts these with a leading equals sign, "=0.023", which is the same
    statement), 'bound' (a comparator and a number, such as "<0.001"; the exact value is not
    posted), 'other_with_digit' (anything else that contains a digit) or 'no_digit'.

    Rule S1 counts a p-value as posted when the string contains a digit, so 'number', 'bound' and
    'other_with_digit' all count; the form is kept so a bound is never read as an exact value."""
    s = '' if v is None else str(v).strip()
    if not s:
        return 'absent'
    if is_num(s) or (s.startswith('=') and is_num(s[1:])):
        return 'number'
    m = _P_BOUND.match(s)
    if m and is_num(m.group(1)):
        return 'bound'
    return 'other_with_digit' if re.search(r'\d', s) else 'no_digit'


def analysis_facts(m: dict[str, Any], measure_index: int) -> list[dict[str, Any]]:
    """One entry per `analyses[]` row of a measure, with the components of rule S1.

    `names_measure_groups`: a non-empty `groupIds` that all resolve to this measure's `groups[].id`.
    `usable` (S1): names its groups and has a p-value containing a digit, or a numeric `paramValue`
    with a numeric `ciLowerLimit` and a numeric `ciUpperLimit`.
    `between_arm` (S1_between): usable and names two or more groups.
    A one-sided interval posts one limit and so has no "estimate with both limits"."""
    gids = {g['id'] for g in m.get('groups') or []}
    out = []
    for j, a in enumerate(m.get('analyses') or []):
        ref = a.get('groupIds') or []
        named = bool(ref) and set(ref) <= gids
        form = p_value_form(a.get('pValue'))
        p = bool(re.search(r'\d', str(a.get('pValue') or '')))
        lo, hi = is_num(a.get('ciLowerLimit')), is_num(a.get('ciUpperLimit'))
        ec = is_num(a.get('paramValue')) and lo and hi
        usable = named and (p or ec)
        out.append({
            'measure_index': measure_index, 'analysis_index': j, 'group_ids': list(ref),
            'names_measure_groups': named, 'usable': usable, 'between_arm': usable and len(ref) >= 2,
            'estimate_type': a.get('paramType'), 'estimate_value_numeric': is_num(a.get('paramValue')),
            'ci_lower_limit_numeric': lo, 'ci_upper_limit_numeric': hi, 'both_confidence_limits': lo and hi,
            'estimate_with_both_limits': ec, 'ci_percent': a.get('ciPctValue'), 'ci_sides': a.get('ciNumSides'),
            'p_value': a.get('pValue'), 'p_value_posted': p, 'p_value_form': form,
            'statistical_method': a.get('statisticalMethod'), 'non_inferiority_type': a.get('nonInferiorityType')})
    return out


# ---------------------------------------------------------------- registered and posted outcomes

def registered_outcomes(protocol: dict[str, Any]) -> dict[str, Any]:
    """Registered outcomes by type from `protocolSection.outcomesModule`: count, titles and time
    frames as registered. A type with no list is a count of zero outcomes registered under it (the
    registry field is a list), which is what the record states; nothing is inferred."""
    om = protocol.get('outcomesModule') or {}
    out: dict[str, Any] = {}
    for key, name in (('primaryOutcomes', 'primary'), ('secondaryOutcomes', 'secondary'), ('otherOutcomes', 'other')):
        items = [{'title': o.get('measure'), 'time_frame': o.get('timeFrame')} for o in om.get(key) or []]
        out[name] = {'count': len(items), 'items': items}
    out['source_field'] = 'protocolSection.outcomesModule'
    return out


def posted_outcomes(measures: list[dict[str, Any]]) -> dict[str, Any]:
    """Posted outcome measures by `type`: how many there are, how many carry data (at least one
    numeric measurement value), the distinct units and parameter types, the number of groups, and
    how many state their number analysed in participants (the measure-level `denoms[]` row has
    units "Participants" and a numeric count for every group). A measure whose type is not one of
    the four registry values is counted under its own value, never dropped."""
    by: dict[str, dict[str, Any]] = {}
    for m in measures:
        t = m.get('type') or 'TYPE_NOT_STATED'
        e = by.setdefault(t, {'count': 0, 'with_data': 0, 'units': set(), 'param_types': set(), 'n_groups': [],
                              'with_participant_counts': 0, 'denominator_units': set()})
        e['count'] += 1
        e['with_data'] += bool(_groups_with_values(m))
        if (m.get('unitOfMeasure') or '').strip():
            e['units'].add(m['unitOfMeasure'].strip())
        if m.get('paramType'):
            e['param_types'].add(m['paramType'])
        groups = [g['id'] for g in m.get('groups') or []]
        e['n_groups'].append(len(groups))
        row = participants_row(m.get('denoms') or [])
        if row is not None:
            units = (row.get('units') or '').strip()
            e['denominator_units'].add(units or 'UNITS_NOT_STATED')
            cnt = {c['groupId']: c.get('value') for c in row.get('counts', [])}
            if units.casefold() == 'participants' and groups and all(is_num(cnt.get(g)) for g in groups):
                e['with_participant_counts'] += 1
    out: dict[str, Any] = {}
    for t in sorted(by, key=lambda k: (OUTCOME_TYPES.index(k) if k in OUTCOME_TYPES else len(OUTCOME_TYPES), k)):
        e = by[t]
        out[t] = {'count': e['count'], 'with_data': e['with_data'], 'units': sorted(e['units']),
                  'param_types': sorted(e['param_types']), 'n_groups_min': min(e['n_groups']),
                  'n_groups_max': max(e['n_groups']), 'with_participant_counts': e['with_participant_counts'],
                  'denominator_units': sorted(e['denominator_units'])}
    return out


def primary_measures(measures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per posted PRIMARY measure: its position in `outcomeMeasures[]` (the registry
    gives measures no identifier, so the index and title are the locator), rule O1 with its
    reason, and its analyses."""
    out = []
    for i, m in enumerate(measures):
        if m.get('type') != 'PRIMARY':
            continue
        usable, strict, reason = measure_usable(m)
        out.append({'measure_index': i, 'title': m.get('title'), 'time_frame': m.get('timeFrame'),
                    'unit': m.get('unitOfMeasure'), 'param_type': m.get('paramType'),
                    'n_groups': len(m.get('groups') or []), 'usable': usable, 'usable_strict': strict,
                    'reason': reason, 'analyses': analysis_facts(m, i)})
    return out


# ---------------------------------------------------------------- flow, baseline population, adverse events

def flow_first_period(fmod: dict[str, Any]) -> dict[str, Any]:
    """Started and completed per arm, FIRST PERIOD ONLY (`periods[0]`). Participants who enter in a
    later period are not counted, and a trial with several periods has later counts this does not
    show (`n_periods` says how many there are). A count that is not numeric is None."""
    periods = fmod.get('periods') or []
    out: dict[str, Any] = {'period': 'first', 'n_periods': len(periods),
                           'period_title': periods[0].get('title') if periods else None,
                           'second_unit': fmod.get('typeUnitsAnalyzed'),
                           'source_field': 'resultsSection.participantFlowModule.periods[0].milestones[]'}
    got: dict[str, dict[str, Any]] = {}
    for ms in (periods[0].get('milestones') or []) if periods else []:
        # The first milestone of each type is used, as the audit's started count does.
        if ms.get('type') in ('STARTED', 'COMPLETED') and ms['type'] not in got:
            got[ms['type']] = {a['groupId']: a.get('numSubjects') for a in ms.get('achievements') or []}
    out['arms'] = [{'group_id': g['id'], 'title': g.get('title'),
                    'started': _plain(num(got.get('STARTED', {}).get(g['id']))),
                    'completed': _plain(num(got.get('COMPLETED', {}).get(g['id'])))}
                   for g in fmod.get('groups') or []]
    out['reason'] = ('no_period' if not periods else 'no_STARTED_in_first_period' if 'STARTED' not in got
                     else 'no_COMPLETED_in_first_period' if 'COMPLETED' not in got else 'ok')
    return out


def baseline_total(bmod: dict[str, Any]) -> dict[str, Any]:
    """BT, the baseline total, from exactly the field denominators.py uses:
    `baselineCharacteristicsModule.denoms[]`, the Participants row (else the first row, flagged),
    `counts[].value` of the Total group chosen by rule T; when rule T finds no total, the sum over
    all groups. Never the Total plus the arms. The posted Total is used as posted even when it is
    not the sum of the arms (`arms_sum` shows the difference)."""
    base = 'resultsSection.baselineCharacteristicsModule.denoms[]'
    groups = bmod.get('groups') or []
    denoms = bmod.get('denoms') or []
    row = participants_row(denoms)
    if not row or not groups:
        return {'value': None, 'method': None, 'reason': 'no_denominator_row', 'source_field': base}
    is_part = (row.get('units') or '').strip().casefold() == 'participants'
    cnt = {c['groupId']: num(c.get('value')) for c in row.get('counts', [])}
    vals = [cnt.get(g['id']) for g in groups]
    if None in vals:
        return {'value': None, 'method': None, 'reason': 'non_numeric_count', 'source_field': base}
    titled = [i for i, g in enumerate(groups) if (g.get('title') or '').strip().casefold() == 'total']
    if len(groups) == 1:
        method, pick = 'single_group', 0
    elif titled:
        method, pick = 'titled_total', titled[0]
    elif len(groups) >= 3 and vals[-1] > 0 and abs(vals[-1] - sum(vals[:-1])) < 1e-9:
        method, pick = 'last_equals_sum_untitled_total', len(groups) - 1
    else:
        method, pick = 'sum_of_arms', None
    where = f"{base[:-2]}[units={row.get('units')!r}].counts[]"
    if pick is None:
        value, source, arms_sum = sum(vals), f'{where}.value, summed over all {len(groups)} groups', sum(vals)
    else:
        value = vals[pick]
        source = f"{where}.value where groupId == {groups[pick]['id']!r} (group title {groups[pick].get('title')!r})"
        arms_sum = sum(v for i, v in enumerate(vals) if i != pick) if len(groups) > 1 else vals[0]
    return {'value': _plain(value), 'method': method, 'reason': 'ok', 'source_field': source,
            'units_are_participants': is_part, 'arms_sum': _plain(arms_sum),
            'total_equals_arms_sum': value == arms_sum}


def flow_started(fmod: dict[str, Any]) -> dict[str, Any]:
    """FS, participants started, from exactly the field denominators.py uses:
    `participantFlowModule.periods[0].milestones[]` with `type == "STARTED"`,
    `achievements[].numSubjects` summed over the flow groups, excluding any group titled exactly
    "Total". First period only. Where `typeUnitsAnalyzed` is present a second unit is posted in
    `numUnits`; `numSubjects` is still the participant count and is what is summed."""
    source = ('resultsSection.participantFlowModule.periods[0].milestones[type=STARTED].achievements[].numSubjects, '
              'summed over participantFlowModule.groups[] excluding a group titled "Total"; first period only')
    groups = [g for g in fmod.get('groups') or [] if (g.get('title') or '').strip().casefold() != 'total']
    periods = fmod.get('periods') or []
    if not periods:
        return {'value': None, 'reason': 'no_period', 'source_field': source}
    ms = [m for m in periods[0].get('milestones') or [] if m.get('type') == 'STARTED']
    if not ms:
        return {'value': None, 'reason': 'no_STARTED_in_first_period', 'source_field': source}
    if not groups:
        # No flow group to sum over (none posted, or only one titled "Total"). Without this the sum
        # below is over nothing and would publish 0 as a count that was read, where nothing was.
        return {'value': None, 'reason': 'no_flow_groups', 'source_field': source}
    a = {x['groupId']: x.get('numSubjects') for x in ms[0].get('achievements') or []}
    vals = [a.get(g['id']) for g in groups]
    if not all(is_num(v) for v in vals):
        return {'value': None, 'reason': 'non_numeric_or_missing_numSubjects', 'source_field': source}
    return {'value': _plain(sum(num(v) for v in vals)), 'reason': 'ok', 'source_field': source}


def _relation(a: Any, c: Any) -> str:
    """'equal', 'smaller' or 'larger' for the first against the second; 'missing' when either is absent."""
    if a is None or c is None:
        return 'missing'
    return 'equal' if a == c else ('smaller' if a < c else 'larger')


def baseline_population(bmod: dict[str, Any], fmod: dict[str, Any], design: dict[str, Any],
                        posted: bool) -> dict[str, Any]:
    """Baseline total, number started and registered enrolment side by side, each with the field
    it came from, and the baseline population description when the record gives one. The three
    numbers measure different things (analysed at baseline, entered the first period, registered)
    and often differ; none replaces another. No population is inferred where the text is absent."""
    ei = design.get('enrollmentInfo') or {}
    en = ei.get('count')
    enrollment = {'value': en if isinstance(en, (int, float)) and not isinstance(en, bool) else None,
                  'type': ei.get('type'), 'source_field': 'protocolSection.designModule.enrollmentInfo.count',
                  'reason': 'ok' if isinstance(en, (int, float)) and not isinstance(en, bool) else 'not_registered'}
    if not posted:
        absent = {'value': None, 'reason': 'no_posted_results'}
        return {'total': dict(absent), 'started': dict(absent), 'enrollment': enrollment,
                'population_description': None, 'relations': None}
    bt, fs = baseline_total(bmod), flow_started(fmod)
    desc = bmod.get('populationDescription')
    return {
        'total': bt, 'started': fs, 'enrollment': enrollment,
        # Free text, quoted as posted; None when the record gives none.
        'population_description': ({'text': desc,
                                    'source_field': 'resultsSection.baselineCharacteristicsModule.populationDescription'}
                                   if isinstance(desc, str) and desc.strip() else None),
        'relations': {'total_vs_started': _relation(bt['value'], fs['value']),
                      'total_vs_enrollment': _relation(bt['value'], enrollment['value']),
                      'started_vs_enrollment': _relation(fs['value'], enrollment['value'])}}


def adverse_events(amod: dict[str, Any] | None) -> dict[str, Any] | None:
    """Whether serious and other adverse-event rows are posted, the numbers at risk per group, and
    the reporting frequency threshold for other events (`frequencyThreshold`, a percentage: other
    events below it are not listed, so their absence is not absence of events). None when the
    module is absent. Zero event rows with numeric zero totals is "no events reported"; zero rows
    without totals is not."""
    if not amod:
        return None
    eg = amod.get('eventGroups') or []
    serious, other = amod.get('seriousEvents') or [], amod.get('otherEvents') or []
    return {
        'serious_events_posted': bool(serious), 'serious_event_rows': len(serious),
        'other_events_posted': bool(other), 'other_event_rows': len(other),
        'serious_totals_posted': bool(eg) and all(is_num(g.get('seriousNumAffected')) for g in eg),
        'other_totals_posted': bool(eg) and all(is_num(g.get('otherNumAffected')) for g in eg),
        'frequency_threshold': amod.get('frequencyThreshold'),
        'frequency_threshold_source_field': 'resultsSection.adverseEventsModule.frequencyThreshold',
        'groups': [{'group_id': g.get('id'), 'title': g.get('title'),
                    'serious_at_risk': _plain(num(g.get('seriousNumAtRisk'))),
                    'other_at_risk': _plain(num(g.get('otherNumAtRisk'))),
                    'deaths_at_risk': _plain(num(g.get('deathsNumAtRisk')))} for g in eg]}


# ---------------------------------------------------------------- subgroup rule SG2

_I = re.IGNORECASE
FEMALE = re.compile(r'\b(females?|wom[ae]n|girls?)\b', _I)
MALE = re.compile(r'\b(males?|m[ae]n|boys?)\b', _I)
RACE_EXCLUDE = re.compile(r'white blood|white cell|white matter|white coat|white light|white noise|black box|blackout|'
                          r'asian flu|non-?hodgkin', _I)
RACE_TOKENS = [
    ('white', re.compile(r'\b(white|caucasian)s?\b', _I)),
    ('black', re.compile(r'\b(black|african[- ]americans?)\b', _I)),
    ('asian', re.compile(r'\basians?\b', _I)),
    ('aian', re.compile(r'american indian|alaska native', _I)),
    ('nhpi', re.compile(r'native hawaiian|pacific islander', _I)),
    ('multi', re.compile(r'multiracial|more than one race|multiple races', _I)),
    ('nonwhite', re.compile(r'\bnon[- ]?(white|caucasian)\b', _I)),
    ('nonblack', re.compile(r'\bnon[- ]?black\b', _I)),
    ('nonasian', re.compile(r'\bnon[- ]?asian\b', _I)),
    ('other_race', re.compile(r'\bother races?\b', _I)),
]
HISPANIC = re.compile(r'\b(hispanic|latin[oax]s?|latine)\b', _I)
ORIGIN = re.compile(r'\b(japanese|chinese|korean|taiwanese|indian)\b', _I)
NON_ORIGIN = re.compile(r'\bnon[- ]?(japanese|chinese|korean|taiwanese|asian)\b|\brest of (the )?world\b', _I)
AGE_BAND = re.compile(
    r'(?:[<>≤≥]\s?=?\s?\d{1,3}(?:\.\d+)?\s?(?:years?|yrs?|y\.?o\.?|months?|mos?)\b)'
    r'|(?:\bage[ds]?\b[^,;()]{0,25}?\d{1,3})'
    r'|(?:\d{1,3}\s?(?:-|–|to|through)\s?(?:<\s?)?\d{1,3}\s?(?:years?|yrs?|months?)\s?(?:of age|old)\b)'
    r'|(?:\d{1,3}\s?(?:years?|yrs?|months?)\s?(?:of age|old)(?:\s?(?:and|or)\s?(?:older|younger|above|below|over|under))?)'
    r'|(?:\d{1,3}\s?(?:-|–|to)\s?(?:<\s?)?\d{1,3}\s?(?:years?|yrs?)\b)', _I)
LIFE_STAGE = [
    ('infant', re.compile(r'\b(infants?|neonates?|newborns?|toddlers?)\b', _I)),
    ('child', re.compile(r'\b(child|children|pediatrics?|paediatrics?)\b', _I)),
    ('adolescent', re.compile(r'\b(adolescents?|teens?|teenagers?)\b', _I)),
    ('adult', re.compile(r'\b(adults?)\b', _I)),
    ('older', re.compile(r'\b(elderly|older adults?|seniors?|geriatrics?)\b', _I)),
]
KEYWORD = re.compile(r'\bsub-?\s?(?:groups?|populations?|sets?|cohorts?)\b|\bsubpopulations?\b|\bsubsets?\b', _I)
_DEMOG_WORD = (r'(?:sex|gender|race|racial|ethnic(?:ity)?(?: group)?|'
               r'age(?: group| category| cohort| stratum| strata| range| band)?s?)')
BY_DEMOG = re.compile(
    r'\b(?:by|according to|per|stratified by|within|across|for each)\s+(?:the\s+)?(?:participant\s+|patient\s+|subject\s+)?'
    + _DEMOG_WORD + r'\b(?!\s*(?:of|at)\b)'
    r'|\b(?:sex|gender|race|ethnic|age)[- ](?:specific|stratified|based subgroups?)\b', _I)
# "per age-appropriate", "adjusted for age" and similar describe the model or the instrument, not
# a subgroup result; a BY_DEMOG match with one of these in its surrounding 25 characters is dropped.
BY_DEMOG_EXCLUDE = re.compile(
    r'age[- ](?:appropriate|adjusted|matched|related|standardi[sz]ed|corrected|equivalent|at\b|of\b)|'
    r'(?:adjusted|corrected|standardi[sz]ed|normali[sz]ed|matched) (?:for|by) (?:age|sex|gender)|'
    r'for age\b|sex[- ]hormone|gender[- ]affirm|sex(?:ual)? (?:partner|behavio|activity|function)', _I)
# Which of the four dimensions a BY_DEMOG match names: the first demographic word in the match.
_DEMOG_NAMED = re.compile(r'\b(sex|gender|rac(?:e|ial)|ethnic|age)', _I)


def race_tokens(t: str) -> set[str]:
    """Race tokens in a title after removing phrases with another meaning ("white blood cell",
    "non-Hodgkin"). "non-white" also matches "white": only the more specific token is kept."""
    t2 = RACE_EXCLUDE.sub(' ', t)
    found = {name for name, rx in RACE_TOKENS if rx.search(t2)}
    for a, b in (('nonwhite', 'white'), ('nonblack', 'black'), ('nonasian', 'asian')):
        if a in found:
            found.discard(b)
    return found


def age_token(t: str) -> str | None:
    """An age level in a title: an explicit band with a number ('band:...'), else exactly one
    life-stage word ('stage:...'), else None. A title naming two life stages names no single level."""
    m = AGE_BAND.search(t)
    if m:
        return 'band:' + re.sub(r'\s+', '', m.group(0).lower())
    stages = [n for n, rx in LIFE_STAGE if rx.search(t)]
    if len(stages) == 1:
        return 'stage:' + stages[0]
    return None


def _level_matches(titles: list[str], dims: tuple[str, ...]) -> dict[str, tuple[list[str], ...]]:
    """Every sibling title that carries a level, for each dimension the level rule fires on (the
    conditions are in level_dimensions). Titles are in the order given; sex is two lists (female
    titles, male titles), every other dimension one. Whether a dimension fires, and which titles
    belong to it, depend on the set of titles only, never on their order."""
    titles = list(dict.fromkeys(t.strip() for t in titles if t and t.strip()))
    out: dict[str, tuple[list[str], ...]] = {}
    if len(titles) < 2:
        return out
    if 'sex' in dims:
        f = [t for t in titles if FEMALE.search(t) and not MALE.search(t)]
        m = [t for t in titles if MALE.search(t) and not FEMALE.search(t)]
        if f and m:
            out['sex'] = (f, m)
    if 'race' in dims:
        tok = {t: race_tokens(t) for t in titles}
        distinct = set().union(*tok.values()) if tok else set()
        carriers = [t for t in titles if tok[t]]
        if len(distinct) >= 2 and len(carriers) >= 2:
            out['race'] = (carriers,)
    if 'ethnicity' in dims:
        h = [t for t in titles if HISPANIC.search(t)]
        if h:
            out['ethnicity'] = (h,)
    if 'origin' in dims:
        o = [t for t in titles if ORIGIN.search(t) or NON_ORIGIN.search(t)]
        toks = {'non' if NON_ORIGIN.search(t) else ORIGIN.search(t).group(0).lower() for t in o}
        if len(o) >= 2 and len(toks) >= 2:
            out['origin'] = (o,)
    if 'age' in dims:
        atok = {t: age_token(t) for t in titles}
        carriers = [t for t in titles if atok[t]]
        if len({atok[t] for t in carriers}) >= 2:
            out['age'] = (carriers,)
    return out


# How many matching titles a level signal displays, per list (the audit's display cut).
_LEVEL_SHOWN = {'sex': 2, 'race': 4, 'ethnicity': 3, 'origin': 4, 'age': 4}


def level_identity(titles: list[str],
                   dims: tuple[str, ...] = ('sex', 'race', 'ethnicity', 'origin', 'age')) -> dict[str, list[str]]:
    """{rule dimension: every title carrying one of its levels, sorted}. This is what a level signal's
    id is taken from. Sibling rows are an unordered set to the rule, so the registry listing them in
    another order must not change the id; and the displayed titles (level_dimensions) are the first
    few in posted order, which a reordering would change."""
    return {d: sorted(t for g in groups for t in g) for d, groups in _level_matches(titles, dims).items()}


def level_dimensions(titles: list[str],
                     dims: tuple[str, ...] = ('sex', 'race', 'ethnicity', 'origin', 'age')) -> dict[str, list[str]]:
    """Level rule: the dimensions for which sibling titles of ONE measure show two or more levels.
    Returns {rule dimension: the titles that matched}, in posted order, cut to the first two female
    and two male titles for sex, the first three for ethnicity and the first four otherwise.

      sex        one title matches FEMALE and not MALE, and another MALE and not FEMALE
      race       two titles carry race tokens and at least two different tokens appear
      ethnicity  one title matches HISPANIC, among two or more titles
      origin     two titles match ORIGIN / NON_ORIGIN with at least two different tokens
      age        two titles carry different age tokens
    Titles are trimmed and de-duplicated first; fewer than two distinct titles fire nothing."""
    return {d: [t for g in groups for t in g[:_LEVEL_SHOWN[d]]] for d, groups in _level_matches(titles, dims).items()}


def cue(text: Any) -> tuple[str, str, str] | None:
    """Cue rule on one text: (kind, snippet, matched substring) for the first cue, or None.

    KEYWORD is tried first; only when it finds nothing is BY_DEMOG tried, and a BY_DEMOG match is
    dropped when BY_DEMOG_EXCLUDE matches within 25 characters either side. The snippet is the
    match with up to 90 characters before and 110 after, newlines replaced by spaces."""
    if not text:
        return None
    m = KEYWORD.search(text)
    kind = 'keyword'
    if not m:
        for mm in BY_DEMOG.finditer(text):
            ctx = text[max(0, mm.start() - 25): mm.end() + 25]
            if not BY_DEMOG_EXCLUDE.search(ctx):
                m, kind = mm, 'by_demographic'
                break
    if not m:
        return None
    return kind, text[max(0, m.start() - 90): m.end() + 110].replace('\n', ' ').strip(), m.group(0)


def detect_measure(m: dict[str, Any]) -> list[dict[str, Any]]:
    """Every SG2 signal of one outcome measure, in the audit's order.

    Each signal carries the audit's three keys (`field`, `dimension`, `text`, where `text` is the
    audit's display string with its truncation) and three this port adds without changing what
    fires: `matched` (the matched titles or substring, verbatim and untruncated), `path` (the
    field's path inside the measure) and, on level signals only, `ident` (the titles the signal's
    id is taken from, sorted so their posted order does not matter; see level_identity). The port check compares the first three with the audit.

    Level rules run on the set of class titles, the set of category titles and the set of group
    titles; then on group descriptions (sex and age only) for dimensions the group titles did not
    fire; then one `analysis_groupIds` signal for the first analysis whose `groupIds` reference
    two or more groups that carry different levels, labelled with the dimensions those groups
    differ on. Cue rules run on the measure title,
    description and population description, on the first class title and first category title
    with a cue, on each of nine analysis text fields, and on the first measurement comment with
    a cue."""
    sig: list[dict[str, Any]] = []
    classes = m.get('classes') or []
    class_titles = [c.get('title') or '' for c in classes]
    cat_titles = [cat.get('title') or '' for c in classes for cat in c.get('categories') or []]
    groups = m.get('groups') or []
    gt = [g.get('title') or '' for g in groups]
    for field, titles, path in (('class_titles', class_titles, 'classes[].title'),
                                ('category_titles', cat_titles, 'classes[].categories[].title'),
                                ('group_titles', gt, 'groups[].title')):
        ident = level_identity(titles)
        for dim, hits in level_dimensions(titles).items():
            sig.append({'field': field, 'dimension': dim, 'text': ' | '.join(h[:70] for h in hits),
                        'matched': hits, 'path': path, 'ident': ident[dim]})
    group_level = {s['dimension'] for s in sig if s['field'] == 'group_titles'}
    gd = [g.get('description') or '' for g in groups]
    gd_ident = level_identity(gd, dims=('sex', 'age'))
    for dim, hits in level_dimensions(gd, dims=('sex', 'age')).items():
        if dim not in group_level:
            sig.append({'field': 'group_descriptions', 'dimension': dim,
                        'text': ' | '.join(h[:110] for h in hits[:2]), 'matched': hits[:2],
                        'path': 'groups[].description', 'ident': gd_ident[dim]})
    lvl_dims = {s['dimension'] for s in sig if s['field'] in ('group_titles', 'group_descriptions')}
    if lvl_dims:
        by_id = {g['id']: (g.get('title') or '') + ' ' + (g.get('description') or '') for g in groups}
        title_of = {g['id']: g.get('title') or '' for g in groups}
        for j, a in enumerate(m.get('analyses') or []):
            ids = a.get('groupIds') or []
            ref = [by_id.get(i, '') for i in ids]
            # Only the dimensions the compared groups differ on. The audit's script labelled this
            # signal with every dimension the measure's groups carry, so an analysis of "Women age 18"
            # against "Men age 18", in a measure that also has age-65 groups, was labelled age as
            # well as sex. Which analysis fires is unchanged. In the audit's 2,000 records all 12 such
            # signals compare groups that differ on every labelled dimension, so none changes.
            compared = level_dimensions(ref, dims=tuple(sorted(lvl_dims))) if len(ref) >= 2 else {}
            if compared:
                sig.append({'field': 'analysis_groupIds', 'dimension': '+'.join(sorted(compared)),
                            'text': 'analysis compares level-defined groups: ' + ' vs '.join(r[:40] for r in ref[:3]),
                            'matched': [title_of.get(i, '') for i in ids], 'path': f'analyses[{j}].groupIds',
                            # the compared groups are a set: their titles, sorted
                            'ident': sorted(title_of.get(i, '') for i in ids)})
                break
    for field, key in (('measure_title', 'title'), ('measure_description', 'description'),
                       ('population_description', 'populationDescription')):
        c = cue(m.get(key))
        if c:
            sig.append({'field': field, 'dimension': c[0], 'text': c[1], 'matched': [c[2]], 'path': key})
    for field, titles, path in (('class_title_cue', class_titles, 'classes[].title'),
                                ('category_title_cue', cat_titles, 'classes[].categories[].title')):
        for t in titles:
            c = cue(t)
            if c:
                sig.append({'field': field, 'dimension': c[0], 'text': c[1], 'matched': [c[2]], 'path': path})
                break
    for j, a in enumerate(m.get('analyses') or []):
        for k in ANALYSIS_TEXT_FIELDS:
            c = cue(a.get(k))
            if c:
                sig.append({'field': 'analysis_text:' + k, 'dimension': c[0], 'text': c[1], 'matched': [c[2]],
                            'path': f'analyses[{j}].{k}'})
    done = False
    for ci, cl in enumerate(classes):
        for ki, cat in enumerate(cl.get('categories') or []):
            for x in cat.get('measurements') or []:
                c = cue(x.get('comment'))
                if c and not done:
                    sig.append({'field': 'measurement_comment', 'dimension': c[0], 'text': c[1], 'matched': [c[2]],
                                'path': f'classes[{ci}].categories[{ki}].measurements[].comment'})
                    done = True
    return sig


def signal_dimensions(rule_dimension: str, matched: list[str]) -> list[str]:
    """The published dimension(s) of one SG2 signal, each one of age, sex, race, ethnicity, other.

    The audit rule names sex, race, ethnicity, age and origin for level rules, and for cue rules
    only the kind of cue. This mapping is the one thing this module adds to the rule, and it does
    not change which signals fire:
      sex / race / ethnicity / age   that dimension
      origin                         other (national origin; not one of the four)
      keyword                        other (the text says "subgroup" without the rule knowing of what)
      by_demographic                 the demographic word in the matched text ("by age group" is age,
                                     "by gender" is sex, "by race" is race, "by ethnicity" is ethnicity)
      a+b (analysis_groupIds)        one entry per joined dimension, mapped as above
    """
    out: list[str] = []
    for d in rule_dimension.split('+'):
        if d in DIMENSIONS:
            mapped = d
        elif d == 'by_demographic':
            word = _DEMOG_NAMED.search(matched[0] if matched else '')
            w = word.group(1).lower() if word else ''
            mapped = ('sex' if w in ('sex', 'gender') else 'race' if w.startswith('rac') else
                      'ethnicity' if w == 'ethnic' else 'age' if w == 'age' else 'other')
        else:
            mapped = 'other'
        if mapped not in out:
            out.append(mapped)
    return out


_MAX_MATCHED = 400   # characters of one matched text kept; a longer text is cut and flagged


def subgroup_signals(nct_id: str, measures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Published signals for one study: rule SG2 over every posted outcome measure of every type.

    One entry per signal and dimension, with the field path, the measure's index, type and title,
    the matched text verbatim, and for cue rules the surrounding context. `signal_id` identifies
    the signal for a human review row: a digest of the NCT id, measure type and title, field, rule
    dimension, dimension and the signal's own words. For a cue those words are the matched text and
    the text around it. For a level signal they are every sibling title carrying one of the
    dimension's levels, sorted: sibling rows are an unordered set, so the registry listing them in
    another order gives the same id, while renaming, adding or removing a level gives a new one.
    The id does not include the
    measure index, the method version or anything else in the measure, so a review still attaches
    after measures are reordered, the rules are re-versioned, or the registrant changes a unit or
    a value elsewhere in the measure, as long as the words the review was about are unchanged.

    Two measures with one title that both say "subgroup" in their description, in different
    sentences, therefore get different ids from their own sentences, not from their order or
    from unrelated fields. Signals that agree in every one of those texts are numbered in the
    order they stand. Nothing a reviewer is shown tells such signals apart, so there is no
    stable text to number them by; a reordering can exchange their numbers, and a review of
    one should be read as a review of that wording wherever it stands in the trial."""
    out: list[dict[str, Any]] = []
    seen: dict[str, int] = defaultdict(int)
    for i, m in enumerate(measures):
        for s in detect_measure(m):
            matched = [t[:_MAX_MATCHED] for t in s['matched']]
            for dim in signal_dimensions(s['dimension'], s['matched']):
                words = [s['ident']] if 'ident' in s else [s['matched'], s['text']]
                key = json.dumps([nct_id, m.get('type'), m.get('title'), s['field'], s['dimension'], dim, *words],
                                 ensure_ascii=False)
                seen[key] += 1
                digest = hashlib.sha256(f'{key}#{seen[key]}'.encode()).hexdigest()[:12]
                entry = {'signal_id': f'sg-{digest}', 'dimension': dim, 'rule_dimension': s['dimension'],
                         'field': s['field'],
                         'field_path': f"resultsSection.outcomeMeasuresModule.outcomeMeasures[{i}].{s['path']}",
                         'measure_index': i, 'measure_type': m.get('type'), 'measure_title': m.get('title'),
                         'matched_text': matched,
                         'matched_text_truncated': any(len(t) > _MAX_MATCHED for t in s['matched']),
                         # For a cue: the match with up to 90 characters before and 110 after, as posted.
                         'context': s['text'] if s['dimension'] in ('keyword', 'by_demographic') else None,
                         'review': []}
                out.append(entry)
    return out


# ---------------------------------------------------------------- human reviews

REVIEW_COLUMNS = ('nct_id', 'dimension', 'signal_id', 'decision', 'reviewer', 'reviewed_on', 'note')
REVIEW_DECISIONS = ('accepted', 'rejected', 'unresolved')
_NCT = re.compile(r'^NCT\d{8}$')
_SIGNAL_ID = re.compile(r'^sg-[0-9a-f]{12}$')
_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


class ReviewError(ValueError):
    """The human review file cannot be used as written."""


def load_reviews(path: Path) -> dict[str, dict[tuple[str, str], dict[str, str]]]:
    """Human review records from outcomes/subgroup_reviews.csv, as {nct_id: {(signal_id, dimension): row}}.

    One row is one person's decision on one signal for one dimension:
      accepted    the signal is a posted subgroup result for that dimension
      rejected    it is not
      unresolved  a person looked and could not settle it
    For a signal whose own dimension is 'other', a row may name one of the four dimensions (the
    reviewer says which characteristic the subgroup is by) or 'other' (a decision on the signal as
    a whole: rejected for every dimension, or accepted as a subgroup by some other characteristic).

    Raises ReviewError on a wrong header, a malformed id or date, an unknown decision or
    dimension, an empty reviewer, an empty note, or two rows for the same signal and dimension,
    whether they agree (a duplicate) or not (a conflict). Nothing is resolved silently. The
    audit's own labels (docs/audits/outcomes_results_structure/second_pass/subgroup_review.csv)
    were made by one automated reader and are not human reviews; they must not be loaded here."""
    with path.open(newline='', encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        if tuple(reader.fieldnames or ()) != REVIEW_COLUMNS:
            raise ReviewError(f'{path.name}: header must be exactly {",".join(REVIEW_COLUMNS)}')
        out: dict[str, dict[tuple[str, str], dict[str, str]]] = {}
        for line, raw in enumerate(reader, start=2):
            row = {k: (raw.get(k) or '').strip() for k in REVIEW_COLUMNS}
            where = f'{path.name} line {line}'
            if None in raw:
                raise ReviewError(f'{where}: more cells than columns')
            if not _NCT.match(row['nct_id']):
                raise ReviewError(f'{where}: nct_id {row["nct_id"]!r} is not an NCT id')
            if row['dimension'] not in SIGNAL_DIMENSIONS:
                raise ReviewError(f'{where}: dimension {row["dimension"]!r} is not one of {SIGNAL_DIMENSIONS}')
            if not _SIGNAL_ID.match(row['signal_id']):
                raise ReviewError(f'{where}: signal_id {row["signal_id"]!r} is not a signal id')
            if row['decision'] not in REVIEW_DECISIONS:
                raise ReviewError(f'{where}: decision {row["decision"]!r} is not one of {REVIEW_DECISIONS}')
            if not row['reviewer']:
                raise ReviewError(f'{where}: no reviewer; a review record names the person who made it')
            if not _DATE.match(row['reviewed_on']):
                raise ReviewError(f'{where}: reviewed_on {row["reviewed_on"]!r} is not YYYY-MM-DD')
            try:
                # the shape alone admits 2026-02-31; the date is published with the signal
                date.fromisoformat(row['reviewed_on'])
            except ValueError:
                raise ReviewError(f'{where}: reviewed_on {row["reviewed_on"]!r} is not a calendar date') from None
            if not row['note']:
                raise ReviewError(f'{where}: empty note; every review records why it was decided')
            key = (row['signal_id'], row['dimension'])
            prior = out.setdefault(row['nct_id'], {}).get(key)
            if prior is not None:
                kind = 'duplicate' if prior['decision'] == row['decision'] else 'conflicting'
                raise ReviewError(f'{where}: {kind} review of {row["nct_id"]} {key[0]} for {key[1]} '
                                  f'({prior["decision"]} and {row["decision"]})')
            out[row['nct_id']][key] = row
    return out


def _decision_for(signal: dict[str, Any], dim: str, reviews: dict[tuple[str, str], dict[str, str]]) -> str | None:
    """One signal's settled decision for one of the four dimensions: 'accepted', 'rejected',
    'unresolved', or None when no review covers it.

    A typed signal (its dimension is one of the four) is decided by the review row for that
    dimension. An 'other' signal is decided for `dim` by a row naming `dim`; failing that, by a
    row naming 'other': rejected there means rejected for every dimension, and accepted there
    means a subgroup by some other characteristic, which settles the signal as not being `dim`."""
    sid = signal['signal_id']
    row = reviews.get((sid, dim))
    if row is not None:
        return row['decision']
    if signal['dimension'] == 'other':
        whole = reviews.get((sid, 'other'))
        if whole is not None:
            return 'unresolved' if whole['decision'] == 'unresolved' else 'rejected'
    return None


def _state(decisions: list[str | None]) -> str:
    """The state a list of per-signal decisions gives: any accepted is detected; none at all, or
    every one rejected, is none detected; anything else (a signal no person has settled) is
    indeterminate."""
    if 'accepted' in decisions:
        return DETECTED
    if all(d == 'rejected' for d in decisions):
        return NONE_DETECTED
    return FAILED_OR_INDETERMINATE


def apply_reviews(record: dict[str, Any], reviews: dict[tuple[str, str], dict[str, str]] | None = None) -> list[str]:
    """Set the record's subgroup states from its signals and the human reviews of this trial.
    Returns the review keys ("signal_id/dimension") that match no current signal, so the caller
    can report them; a review of a signal the record no longer has is not applied to anything.

    Per study and per dimension, exactly one of:
      not_applicable_no_posted_results   the registry holds no posted results for the trial
      not_yet_processed                  the record was not retrieved, so its results were not read
      processing_failed_or_indeterminate the record or its outcome measures could not be parsed, or
                                         the rule fired and no human review has settled it
      no_explicit_subgroup_results_detected_in_reviewed_structured_fields
                                         the listed fields were read and the rule found nothing, or
                                         a human review rejected every signal
      explicit_subgroup_results_detected at least one signal accepted by a human review record

    For a dimension, the signals that count are those of that dimension and every 'other' signal:
    an 'other' signal says "subgroup" without the rule knowing of which characteristic, so until a
    person settles it no dimension can be called "none detected". With no review rows no trial can
    be in the detected state; that is intended.

    Raises ReviewError when a review names a dimension its signal cannot have (a typed signal
    reviewed under another dimension)."""
    reviews = reviews or {}
    sg = record['subgroup']
    by_id = {s['signal_id']: s for s in sg['signals'] or []}
    unmatched = []
    for (sid, dim), row in sorted(reviews.items()):
        s = by_id.get(sid)
        if s is None:
            unmatched.append(f'{sid}/{dim}')
            continue
        if s['dimension'] != 'other' and dim != s['dimension']:
            raise ReviewError(f"{record['nct_id']} {sid}: reviewed for {dim!r}, but the signal's dimension is "
                              f"{s['dimension']!r}")
        # The reviewer's name and note stay in the review file; the record carries the decision.
        s['review'].append({'dimension': dim, 'decision': row['decision'], 'reviewed_on': row['reviewed_on']})
    status = record['results_status']
    if status == 'no_posted_results':
        fixed: str | None = NOT_APPLICABLE
    elif status == 'not_retrieved':
        fixed = NOT_YET_PROCESSED
    elif status == 'unknown' or sg['signals'] is None:
        fixed = FAILED_OR_INDETERMINATE
    else:
        fixed = None
    if fixed is not None:
        sg['state'], sg['by_dimension'] = fixed, {d: fixed for d in DIMENSIONS}
        return unmatched
    signals = sg['signals']
    sg['by_dimension'] = {
        d: _state([_decision_for(s, d, reviews) for s in signals if s['dimension'] in (d, 'other')])
        for d in DIMENSIONS}
    # Study level: a signal is accepted when any review of it is; rejected when its own dimension
    # is rejected, or, for an 'other' signal, when it is settled for all four dimensions.
    per_signal: list[str | None] = []
    for s in signals:
        own = [r['decision'] for r in s['review']]
        if 'accepted' in own:
            per_signal.append('accepted')
        elif s['dimension'] != 'other':
            per_signal.append(_decision_for(s, s['dimension'], reviews))
        else:
            each = [_decision_for(s, d, reviews) for d in DIMENSIONS]
            per_signal.append('rejected' if all(e == 'rejected' for e in each) else
                              'unresolved' if 'unresolved' in each else None)
    sg['state'] = _state(per_signal)
    return unmatched


# ---------------------------------------------------------------- the record

def _guard(section: str, failures: list[dict[str, str]], fn: Callable[[], T]) -> T | None:
    """Run one section's parser. A record that does not have the shape the parser reads (a missing
    group id, a list where an object is expected) is recorded as a failure of that section with
    its reason and the section's value is None; it is never dropped and never read as zero."""
    try:
        return fn()
    except (KeyError, TypeError, AttributeError, ValueError, IndexError) as exc:
        failures.append({'section': section, 'reason': f'{type(exc).__name__}: {exc}'})
        return None


def nct_id_of(study: Any) -> str | None:
    """The record's NCT id, or None when the record does not carry one where the registry puts it."""
    try:
        v = study['protocolSection']['identificationModule']['nctId']
    except (KeyError, TypeError):
        return None
    return v if isinstance(v, str) else None


def not_retrieved_record(nct_id: str, reason: str) -> dict[str, Any]:
    """The record of a requested trial the registry did not return. Its results were not read, so
    every subgroup state is not_yet_processed; nothing about its results is stated."""
    rec = _empty_record(nct_id, 'not_retrieved')
    rec['retrieval'] = {'status': 'failed', 'reason': reason}
    apply_reviews(rec)
    return rec


def _empty_record(nct_id: str | None, status: str) -> dict[str, Any]:
    return {
        'nct_id': nct_id, 'method_version': METHOD_VERSION, 'results_status': status,
        'retrieval': {'status': 'ok', 'reason': None}, 'parse': {'status': 'ok', 'failures': []},
        'design': None, 'registered_outcomes': None,
        'modules': None, 'all_four_usable': None, 'posted_outcomes': None, 'primary_measures': None,
        'comparative': None, 'between_arm_analysis': None, 'participant_flow': None,
        'baseline_population': None, 'adverse_events': None,
        'subgroup': {'rule': SUBGROUP_RULE, 'signals': None, 'n_signals': None, 'state': None, 'by_dimension': None},
        'document_and_publication_review': DOCUMENT_AND_PUBLICATION_REVIEW}


def study_record(study: Any) -> dict[str, Any]:
    """The results-structure record of one study as retrieved. Subgroup states are set by
    `apply_reviews`, which the caller runs next (with no reviews, a signal leaves its dimensions
    indeterminate).

    `results_status` comes from `hasResults`: true is 'posted', false is 'no_posted_results'.
    A record without a boolean `hasResults`, or with `hasResults` true and no results section, is
    a parsing failure ('unknown'): what the registry holds cannot be stated from it."""
    nct = nct_id_of(study)
    failures: list[dict[str, str]] = []
    if not isinstance(study, dict):
        rec = _empty_record(nct, 'unknown')
        rec['parse'] = {'status': 'failed', 'failures': [{'section': 'record', 'reason': 'the record is not an object'}]}
        return rec
    has = study.get('hasResults')
    rs = study.get('resultsSection')
    if has is True and isinstance(rs, dict) and rs:
        status = 'posted'
    elif has is False:
        status = 'no_posted_results'
    else:
        status = 'unknown'
        failures.append({'section': 'record', 'reason': (
            'hasResults is true but resultsSection is absent or empty' if has is True
            else 'hasResults is absent or not a boolean')})
    rec = _empty_record(nct, status)
    protocol = study.get('protocolSection') if isinstance(study.get('protocolSection'), dict) else {}
    design = _guard('design', failures, lambda: protocol.get('designModule') or {}) or {}
    rec['design'] = _guard('design', failures, lambda: {
        'study_type': design.get('studyType'), 'allocation': (design.get('designInfo') or {}).get('allocation'),
        'phases': design.get('phases')})
    rec['registered_outcomes'] = _guard('registered_outcomes', failures, lambda: registered_outcomes(protocol))
    posted = status == 'posted'
    rs = rs if posted else {}
    bmod = rs.get('baselineCharacteristicsModule') or {}
    fmod = rs.get('participantFlowModule') or {}
    amod = rs.get('adverseEventsModule')
    rec['baseline_population'] = _guard('baseline_population', failures,
                                        lambda: baseline_population(bmod, fmod, design, posted))
    if posted:
        measures = _guard('outcome_measures', failures,
                          lambda: list((rs.get('outcomeMeasuresModule') or {}).get('outcomeMeasures') or []))
        rec['modules'] = _modules(rs, bmod, fmod, amod, measures, failures)
        flags = [rec['modules'][k]['usable'] for k in MODULES]
        # None when any rule could not be evaluated: an unevaluated rule is not a failed rule.
        rec['all_four_usable'] = None if None in flags else all(flags)
        if measures is not None:
            rec['posted_outcomes'] = _guard('posted_outcomes', failures, lambda: posted_outcomes(measures))
            rec['primary_measures'] = _guard('primary_measures', failures, lambda: primary_measures(measures))
            rec['subgroup']['signals'] = _guard('subgroup', failures, lambda: subgroup_signals(nct or '', measures))
        if rec['primary_measures'] is not None and rec['design'] is not None:
            rec['comparative'] = comparative(rec['design'], rec['primary_measures'])
            rec['between_arm_analysis'] = between_arm(rec['primary_measures'])
        rec['participant_flow'] = _guard('participant_flow', failures, lambda: flow_first_period(fmod))
        rec['adverse_events'] = _guard('adverse_events', failures, lambda: adverse_events(amod))
        if rec['subgroup']['signals'] is not None:
            rec['subgroup']['n_signals'] = len(rec['subgroup']['signals'])
    if failures:
        rec['parse'] = {'status': 'failed', 'failures': failures}
    return rec


def _modules(rs: dict[str, Any], bmod: dict[str, Any], fmod: dict[str, Any], amod: Any,
             measures: list[dict[str, Any]] | None, failures: list[dict[str, str]]) -> dict[str, Any]:
    """Presence and usable content for the four substantive modules. Presence is only that the key
    exists under `resultsSection` and is a non-empty object; usable content is the module's rule.
    `usable` is None, with reason 'not_evaluated_parse_failure', when the rule could not be run."""
    def entry(name: str, result: Any) -> dict[str, Any]:
        key, rule = MODULES[name]
        e: dict[str, Any] = {'present': bool(rs.get(key)), 'rule': rule}
        if result is None:
            e.update(usable=None, reason='not_evaluated_parse_failure', detail=None)
        else:
            e.update(usable=result[0], reason=result[1], detail=result[2])
        return e

    def o1() -> tuple[bool, str, dict[str, Any]]:
        # Trial-level O1: at least one PRIMARY measure passes measure_usable. The reason of a
        # failure is the reasons of its primary measures joined with '+', as the audit reports it.
        if measures is None:
            raise ValueError('outcome measures could not be read')
        mu = [measure_usable(m) for m in measures if m.get('type') == 'PRIMARY']
        usable = any(u for u, _, _ in mu)
        reasons = sorted({r for u, _, r in mu if not u})
        reason = 'ok' if usable else ('no_primary_measure' if not mu else '+'.join(reasons))
        return usable, reason, {'n_measures': len(measures), 'n_primary': len(mu),
                                'n_primary_usable': sum(1 for u, _, _ in mu if u),
                                'usable_strict': any(s for _, s, _ in mu)}

    def f1() -> tuple[bool, str, dict[str, Any]]:
        usable, reason = flow_usable(fmod)
        return usable, reason, {}

    return {'baseline': entry('baseline', _guard('baseline', failures, lambda: baseline_usable(bmod))),
            'participant_flow': entry('participant_flow', _guard('participant_flow', failures, f1)),
            'outcome_measures': entry('outcome_measures', None if measures is None else _guard('outcome_measures', failures, o1)),
            'adverse_events': entry('adverse_events', _guard('adverse_events', failures, lambda: ae_usable(amod)))}


def comparative(design: dict[str, Any], primaries: list[dict[str, Any]]) -> dict[str, Any]:
    """C1: `studyType == "INTERVENTIONAL"` and at least one posted PRIMARY measure with two or more
    groups. C2: C1 and `designInfo.allocation == "RANDOMIZED"`. Structure of the record only: dose
    cohorts, periods and disease cohorts also appear as groups."""
    most = max((p['n_groups'] for p in primaries), default=0)
    c1 = design.get('study_type') == 'INTERVENTIONAL' and most >= 2
    return {'C1': c1, 'C2': c1 and design.get('allocation') == 'RANDOMIZED', 'primary_max_groups': most}


def between_arm(primaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Trial-level statistical-analysis flags over the analyses of PRIMARY measures (rule S1).

    `posted` is S1_between: an analysis that names two or more of its measure's groups and has a
    p-value or an estimate with both confidence limits. Absence of a posted analysis is not absence
    of an analysis: the registry does not require one for every measure."""
    rows = [a for p in primaries for a in p['analyses']]
    between = [a for a in rows if a['between_arm']]
    return {
        'n_analyses_on_primary': len(rows), 'any_usable_analysis': any(a['usable'] for a in rows),
        'posted': bool(between),
        'with_p_value': any(a['p_value_posted'] for a in between),
        'with_estimate_and_both_limits': any(a['estimate_with_both_limits'] for a in between),
        'estimate_types': sorted({a['estimate_type'] for a in between if a['estimate_type']}),
        'p_value_forms': sorted({a['p_value_form'] for a in between if a['p_value_form'] != 'absent'})}
