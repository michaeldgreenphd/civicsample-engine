"""Module presence versus usable content, and the comparative denominators, on the 2,000 sampled trials.

Run: python3 -I scripts/usable.py V1_DIR OUTDIR
Reads only the first audit's raw-response cache and the slim release. Writes OUTDIR/usable.json
(aggregates, every figure with numerator and denominator) and OUTDIR/per_trial_usable.jsonl (one line
per trial with every flag, so any count can be recomputed or challenged trial by trial).

All paths are relative to `resultsSection` unless they start with `protocolSection`.
"Numeric" means the registry string parses as a finite number after removing thousands commas
(common.is_num); 'NA', '' and absent do not count and are never read as zero.

PRESENCE (the first audit's 99.8% figure): the module key exists and is a non-empty object.

USABLE CONTENT, one rule per module:

B1 baseline usable: at least one `baselineCharacteristicsModule.measures[]` entry M such that, for the
   Total group alone OR for every arm (every non-Total group), the group has (a) at least one
   `M.classes[].categories[].measurements[]` entry with a numeric `value`, and (b) a numeric
   denominator > 0 for that group in any of: module `denoms[].counts[]`, `M.denoms[].counts[]`,
   `M.classes[].denoms[].counts[]`.
   Total group (rule T, used everywhere a baseline total is needed): if there is exactly one group, that
   group; else the group whose trimmed, case-folded title is exactly "total"; else, when there are 3+
   groups and the last group's participant denominator equals the sum of all earlier groups', the last
   group (a total under another name, e.g. "Total Title", "Pooled ..."); else none (arms are summed).
F1 participant flow usable: at least one `participantFlowModule.periods[]` entry in which a milestone
   with `type == "STARTED"` and a milestone with `type == "COMPLETED"` each carry, for every group in
   `participantFlowModule.groups[]`, an `achievements[]` entry with numeric `numSubjects`, and STARTED
   is > 0 for at least one group.
O1 outcomes usable: at least one `outcomeMeasuresModule.outcomeMeasures[]` entry with
   `type == "PRIMARY"` that has a non-blank `unitOfMeasure`, at least one group, a numeric number
   analysed for every group in its `groups[]` (measure-level `denoms[].counts[]`: the row whose units
   is "Participants" if there is one, else the first row), at least one group with number analysed
   > 0, and for every group with number analysed > 0 at least one
   `classes[].categories[].measurements[]` entry with a numeric `value`.
   (O1_strict additionally requires number analysed > 0 for every group.)
A1 adverse events usable: `adverseEventsModule.eventGroups[]` is non-empty; every event group has
   numeric `seriousNumAtRisk` and numeric `otherNumAtRisk`, at least one group has either > 0; and
   EITHER at least one row exists in `seriousEvents[]` or `otherEvents[]` with a numeric `numAffected`
   or `numEvents` in `stats[]`, OR every event group has numeric `seriousNumAffected` and numeric
   `otherNumAffected` (explicit zeros / totals).
S1 statistical analysis usable: at least one `analyses[]` entry on a `type == "PRIMARY"` measure with
   a non-empty `groupIds` that all resolve to that measure's `groups[].id`, and EITHER a `pValue`
   string containing a digit, OR a numeric `paramValue` with numeric `ciLowerLimit` and numeric
   `ciUpperLimit`. S1_between additionally requires 2+ `groupIds` (a between-arm analysis).
MI moreInfoModule: reported as presence only, split into administrative content
   (`pointOfContact`, `certainAgreement`) and the one substantive free-text field
   (`limitationsAndCaveats.description`). It is not counted as a results module.

COMPARATIVE denominators (how "comparative" was determined):
C1 `protocolSection.designModule.studyType == "INTERVENTIONAL"` and at least one PRIMARY outcome
   measure whose `groups[]` has 2 or more entries. (Structure only: two groups need not be two
   randomised arms; cohorts, dose levels and periods also appear as groups.)
C2 C1 and `protocolSection.designModule.designInfo.allocation == "RANDOMIZED"`.
C3 C2 and the trial passes O1 on a PRIMARY measure that itself has 2+ groups.
"""
import sys, os, json, re, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
from common import is_num, num, frac

V1, OUT = sys.argv[1], sys.argv[2]
S, parse_failures = common.load_sample(V1)
REL = common.load_release(OUT)
ids = [l.strip() for l in open(os.path.join(V1, 'sample_ids.txt')) if l.strip()]


def total_group(bmod):
    """Rule T. Returns (group_id or None, method)."""
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


def participants_row(denoms):
    """The denominator row in participants: units == 'Participants' (case-insensitive) if present, else the first row."""
    for d in denoms:
        if (d.get('units') or '').strip().casefold() == 'participants':
            return d
    return denoms[0] if denoms else None


def baseline_usable(bmod):
    groups = [g['id'] for g in bmod.get('groups') or []]
    if not groups:
        return False, 'no_groups', {}
    tot, method = total_group(bmod)
    arms = [g for g in groups if g != tot] or groups
    mod_den = collections.defaultdict(list)
    for d in bmod.get('denoms') or []:
        for c in d.get('counts', []):
            mod_den[c['groupId']].append(c.get('value'))
    ok_total = ok_arms = False
    n_measures = n_measures_numeric = 0
    for m in bmod.get('measures') or []:
        n_measures += 1
        den = collections.defaultdict(list, {k: list(v) for k, v in mod_den.items()})
        for d in m.get('denoms') or []:
            for c in d.get('counts', []):
                den[c['groupId']].append(c.get('value'))
        has_val = set()
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
    reason = 'ok' if (ok_total or ok_arms) else ('no_measures' if not n_measures else
                                                  ('no_numeric_values' if not n_measures_numeric else 'values_or_denominators_incomplete'))
    return (ok_total or ok_arms), reason, {'total_method': method, 'ok_for_total': ok_total, 'ok_for_every_arm': ok_arms,
                                           'n_measures': n_measures, 'n_measures_with_numeric': n_measures_numeric}


def flow_usable(fmod):
    groups = [g['id'] for g in fmod.get('groups') or []]
    if not groups:
        return False, 'no_groups'
    any_started = any_completed = False
    for p in fmod.get('periods') or []:
        got = {}
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


def measure_usable(m):
    """O1 on one measure. Returns (usable, strict, reason)."""
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
    has_val = set()
    for cl in m.get('classes') or []:
        for cat in cl.get('categories') or []:
            for x in cat.get('measurements') or []:
                if is_num(x.get('value')):
                    has_val.add(x['groupId'])
    if not all(g in has_val for g in pos):
        return False, False, 'no_numeric_value_for_an_analysed_group'
    return True, len(pos) == len(groups), 'ok'


def ae_usable(amod):
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
    explicit_zero_only = rows == 0 and zeros and all(num(g['seriousNumAffected']) == 0 and num(g['otherNumAffected']) == 0 for g in eg)
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


def analysis_flags(m):
    """Per-measure S1 components over its analyses[]."""
    gids = {g['id'] for g in m.get('groups') or []}
    out = dict(n=0, named=0, p=0, est_ci=0, s1=0, s1_between=0, between_est_ci=0, between_p=0)
    for a in m.get('analyses') or []:
        out['n'] += 1
        ref = a.get('groupIds') or []
        named = bool(ref) and set(ref) <= gids
        p = bool(re.search(r'\d', str(a.get('pValue') or '')))
        ec = is_num(a.get('paramValue')) and is_num(a.get('ciLowerLimit')) and is_num(a.get('ciUpperLimit'))
        out['named'] += named
        out['p'] += p
        out['est_ci'] += ec
        if named and (p or ec):
            out['s1'] += 1
            if len(ref) >= 2:
                out['s1_between'] += 1
                out['between_est_ci'] += ec
                out['between_p'] += p
    return out


rows = []
for nct in ids:
    s = S.get(nct)
    if s is None:  # retrieval failure: recorded as its own state, not as "not usable"
        rows.append({'nct_id': nct, 'retrieved': False})
        continue
    rs = s.get('resultsSection') or {}
    dm = s['protocolSection'].get('designModule') or {}
    rel = REL.get(nct, {})
    r = {'nct_id': nct, 'retrieved': True, 'has_results': bool(s.get('hasResults')),
         'study_type': dm.get('studyType'), 'allocation': (dm.get('designInfo') or {}).get('allocation'),
         'phases': dm.get('phases'), 'rel_phase': rel.get('phase'), 'rel_sponsor_class': rel.get('sponsor_class'),
         'race_reported': rel.get('race_reported'), 'ethnicity_reported': rel.get('ethnicity_reported'),
         'sex_reported': rel.get('sex_reported'), 'results_date': rel.get('results_date')}
    for k in ('participantFlowModule', 'baselineCharacteristicsModule', 'outcomeMeasuresModule', 'adverseEventsModule', 'moreInfoModule'):
        r['present_' + k] = bool(rs.get(k))
    mi = rs.get('moreInfoModule') or {}
    r['mi_point_of_contact'] = bool(mi.get('pointOfContact'))
    r['mi_certain_agreement'] = bool(mi.get('certainAgreement'))
    r['mi_limitations_text'] = bool(((mi.get('limitationsAndCaveats') or {}).get('description') or '').strip())
    r['B1'], r['B1_reason'], binfo = baseline_usable(rs.get('baselineCharacteristicsModule') or {})
    r.update({'B1_' + k: v for k, v in binfo.items()})
    r['F1'], r['F1_reason'] = flow_usable(rs.get('participantFlowModule') or {})
    oms = (rs.get('outcomeMeasuresModule') or {}).get('outcomeMeasures') or []
    prim = [m for m in oms if m.get('type') == 'PRIMARY']
    r['n_measures'], r['n_primary'] = len(oms), len(prim)
    mu = [measure_usable(m) for m in prim]
    r['O1'] = any(u for u, _, _ in mu)
    r['O1_strict'] = any(st for _, st, _ in mu)
    r['O1_all_primaries'] = bool(mu) and all(u for u, _, _ in mu)
    r['O1_reasons'] = sorted({rsn for u, _, rsn in mu if not u}) if not r['O1'] else []
    r['O1_reason'] = 'ok' if r['O1'] else ('no_primary_measure' if not prim else '+'.join(r['O1_reasons']))
    r['primary_max_groups'] = max([len(m.get('groups') or []) for m in prim], default=0)
    r['primary_2plus_groups'] = r['primary_max_groups'] >= 2
    r['O1_on_2plus_group_primary'] = any(u and len(m.get('groups') or []) >= 2 for m, (u, _, _) in zip(prim, mu))
    af = collections.Counter()
    for m in prim:
        af.update(analysis_flags(m))
    r['primary_analyses'] = af['n']
    r['S_any_analysis'] = af['n'] > 0
    r['S_named'] = af['named'] > 0
    r['S_p'] = af['p'] > 0
    r['S_est_ci'] = af['est_ci'] > 0
    r['S1'] = af['s1'] > 0
    r['S1_between'] = af['s1_between'] > 0
    r['S1_between_est_ci'] = af['between_est_ci'] > 0
    r['S1_between_p'] = af['between_p'] > 0
    r['A1'], r['A1_reason'], ainfo = ae_usable(rs.get('adverseEventsModule'))
    r.update({'A1_' + k: v for k, v in ainfo.items()})
    r['C1'] = r['study_type'] == 'INTERVENTIONAL' and r['primary_2plus_groups']
    r['C2'] = r['C1'] and r['allocation'] == 'RANDOMIZED'
    r['C3'] = r['C2'] and r['O1_on_2plus_group_primary']
    r['all_five_present'] = all(r['present_' + k] for k in ('participantFlowModule', 'baselineCharacteristicsModule', 'outcomeMeasuresModule', 'adverseEventsModule', 'moreInfoModule'))
    r['four_usable'] = r['B1'] and r['F1'] and r['O1'] and r['A1']
    rows.append(r)

with open(os.path.join(OUT, 'per_trial_usable.jsonl'), 'w') as f:
    for r in rows:
        f.write(json.dumps(r, sort_keys=True) + '\n')

got = [r for r in rows if r['retrieved']]
N = len(got)


def cnt(pred, pool=None):
    pool = got if pool is None else pool
    k = sum(1 for r in pool if pred(r))
    return {'numerator': k, 'denominator': len(pool), 'text': frac(k, len(pool))}


agg = {'n_sampled': len(ids), 'n_retrieved': N, 'retrieval_failures': [r['nct_id'] for r in rows if not r['retrieved']],
       'cache_parse_failures': parse_failures, 'presence': {}, 'usable': {}, 'reasons': {}, 'comparative': {}, 'statistics': {}, 'by_stratum': {}}
for k in ('participantFlowModule', 'baselineCharacteristicsModule', 'outcomeMeasuresModule', 'adverseEventsModule', 'moreInfoModule'):
    agg['presence'][k] = cnt(lambda r, k=k: r['present_' + k])
agg['presence']['all_five_modules'] = cnt(lambda r: r['all_five_present'])
agg['presence']['four_substantive_modules_excluding_moreInfo'] = cnt(lambda r: all(r['present_' + k] for k in ('participantFlowModule', 'baselineCharacteristicsModule', 'outcomeMeasuresModule', 'adverseEventsModule')))
agg['presence']['moreInfo_pointOfContact'] = cnt(lambda r: r['mi_point_of_contact'])
agg['presence']['moreInfo_certainAgreement'] = cnt(lambda r: r['mi_certain_agreement'])
agg['presence']['moreInfo_limitationsAndCaveats_text'] = cnt(lambda r: r['mi_limitations_text'])
agg['presence']['hasResults_true'] = cnt(lambda r: r['has_results'])
for k in ('B1', 'F1', 'O1', 'O1_strict', 'O1_all_primaries', 'A1', 'S1', 'S1_between', 'four_usable'):
    agg['usable'][k] = cnt(lambda r, k=k: r[k])
agg['usable']['B1_ok_for_total'] = cnt(lambda r: r.get('B1_ok_for_total'))
agg['usable']['B1_ok_for_every_arm'] = cnt(lambda r: r.get('B1_ok_for_every_arm'))
agg['usable']['four_usable_and_S1'] = cnt(lambda r: r['four_usable'] and r['S1'])
agg['usable']['A1_explicit_zero_only'] = cnt(lambda r: r['A1'] and r.get('A1_explicit_zero_only'))
agg['usable']['A1_deaths_table'] = cnt(lambda r: r.get('A1_deaths_table'))
for k in ('B1', 'F1', 'O1', 'A1'):
    agg['reasons'][k] = dict(collections.Counter(r[k + '_reason'] for r in got).most_common())
agg['reasons']['B1_total_method'] = dict(collections.Counter(r.get('B1_total_method') for r in got).most_common())
agg['reasons']['primary_measures_not_usable_by_reason'] = dict(collections.Counter(
    measure_usable(m)[2] for n in ids if n in S
    for m in ((S[n].get('resultsSection') or {}).get('outcomeMeasuresModule') or {}).get('outcomeMeasures') or []
    if m.get('type') == 'PRIMARY').most_common())
agg['comparative']['interventional'] = cnt(lambda r: r['study_type'] == 'INTERVENTIONAL')
agg['comparative']['interventional_by_allocation'] = dict(collections.Counter(str(r['allocation']) for r in got if r['study_type'] == 'INTERVENTIONAL'))
agg['comparative']['C1'] = cnt(lambda r: r['C1'])
agg['comparative']['C2'] = cnt(lambda r: r['C2'])
agg['comparative']['C3'] = cnt(lambda r: r['C3'])
agg['comparative']['randomized_interventional'] = cnt(lambda r: r['study_type'] == 'INTERVENTIONAL' and r['allocation'] == 'RANDOMIZED')
agg['comparative']['C2_of_randomized'] = cnt(lambda r: r['C2'], [r for r in got if r['study_type'] == 'INTERVENTIONAL' and r['allocation'] == 'RANDOMIZED'])
agg['comparative']['observational_with_2plus_group_primary'] = cnt(lambda r: r['study_type'] == 'OBSERVATIONAL' and r['primary_2plus_groups'])
pools = {'all_sampled': got, 'C1': [r for r in got if r['C1']], 'C2': [r for r in got if r['C2']], 'C3': [r for r in got if r['C3']],
         'C2_phase3': [r for r in got if r['C2'] and r['rel_phase'] == 'PHASE3'],
         'C2_phase3_industry': [r for r in got if r['C2'] and r['rel_phase'] == 'PHASE3' and r['rel_sponsor_class'] == 'INDUSTRY'],
         'C2_race_reported': [r for r in got if r['C2'] and r['race_reported']],
         'not_C1': [r for r in got if not r['C1']]}
for name, pool in pools.items():
    agg['statistics'][name] = {k: cnt(lambda r, k=k: r[k], pool) for k in
                               ('S_any_analysis', 'S_named', 'S_p', 'S_est_ci', 'S1', 'S1_between', 'S1_between_p', 'S1_between_est_ci')}
    agg['statistics'][name]['race_reported'] = cnt(lambda r: bool(r['race_reported']), pool)
    agg['statistics'][name]['race_reported_and_S1_between'] = cnt(lambda r: bool(r['race_reported']) and r['S1_between'], pool)
    agg['statistics'][name]['race_reported_and_S1_between_est_ci'] = cnt(lambda r: bool(r['race_reported']) and r['S1_between_est_ci'], pool)
for name, pred in {'INTERVENTIONAL': lambda r: r['study_type'] == 'INTERVENTIONAL', 'OBSERVATIONAL': lambda r: r['study_type'] == 'OBSERVATIONAL'}.items():
    pool = [r for r in got if pred(r)]
    agg['by_stratum'][name] = {k: cnt(lambda r, k=k: r[k], pool) for k in ('B1', 'F1', 'O1', 'A1', 'S1', 'four_usable', 'all_five_present')}
# demographics x usable outcomes (among trials with results)
agg['demographics_and_outcomes'] = {
    'race_reported': cnt(lambda r: bool(r['race_reported'])),
    'ethnicity_reported': cnt(lambda r: bool(r['ethnicity_reported'])),
    'sex_reported': cnt(lambda r: bool(r['sex_reported'])),
    'race_reported_and_O1': cnt(lambda r: bool(r['race_reported']) and r['O1']),
    'race_reported_and_four_usable': cnt(lambda r: bool(r['race_reported']) and r['four_usable']),
    'O1_among_race_reported': cnt(lambda r: r['O1'], [r for r in got if r['race_reported']]),
    'O1_among_race_not_reported': cnt(lambda r: r['O1'], [r for r in got if not r['race_reported']]),
}
agg['not_usable_ids'] = {k: sorted(r['nct_id'] for r in got if not r[k]) for k in ('B1', 'F1', 'O1', 'A1')}
json.dump(agg, open(os.path.join(OUT, 'usable.json'), 'w'), indent=1)
for sec in ('presence', 'usable', 'reasons', 'comparative'):
    print('==', sec)
    for k, v in agg[sec].items():
        print(' ', k, v['text'] if isinstance(v, dict) and 'text' in v else v)
for name in pools:
    print('== stats', name)
    for k, v in agg['statistics'][name].items():
        print('  ', k, v['text'])
print(agg['by_stratum']); print({k: v['text'] for k, v in agg['demographics_and_outcomes'].items()})
print({k: len(v) for k, v in agg['not_usable_ids'].items()})
