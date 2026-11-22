"""Baseline total versus participant-flow STARTED versus registered enrolment, on the 2,000 sampled trials.

Run: python3 -I scripts/denominators.py V1_DIR OUTDIR
Exact fields (one number per trial for each; a number that cannot be formed is recorded as missing with
a reason and is never read as zero):

BT baseline total = `resultsSection.baselineCharacteristicsModule.denoms[]`, the row whose `units` is
   "Participants" (case-insensitive; else the first row, flagged `bt_units_not_participants`),
   `counts[].value` of the Total group chosen by rule T (usable.py: single group; else the group titled
   exactly "Total"; else a last group equal to the sum of the earlier ones); when rule T finds no total,
   the sum over all groups. Never the Total plus the arms.
FS flow started = `resultsSection.participantFlowModule.periods[0].milestones[]` with
   `type == "STARTED"`, `achievements[].numSubjects` summed over the groups in
   `participantFlowModule.groups[]`, excluding any group titled exactly "Total" (none occurs in this
   sample). Only the first period is used, so participants who enter in a later period are not counted.
   `participantFlowModule.typeUnitsAnalyzed`, when present, means a second unit (eyes, clinics) is also
   reported in `numUnits`; `numSubjects` is still the participant count and is what is summed.
EN enrolment = `protocolSection.designModule.enrollmentInfo.count` with `.type` (ACTUAL / ESTIMATED).

Analysis populations are read from `baselineCharacteristicsModule.populationDescription` (free text;
present for a minority) and are reported verbatim for the reviewed mismatches; no population is inferred
where the text is absent.
"""
import sys, os, json, random, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
from common import is_num, num, frac

V1, OUT = sys.argv[1], sys.argv[2]
S, _ = common.load_sample(V1)
ids = [l.strip() for l in open(os.path.join(V1, 'sample_ids.txt')) if l.strip()]


def participants_row(denoms):
    for d in denoms:
        if (d.get('units') or '').strip().casefold() == 'participants':
            return d, True
    return (denoms[0], False) if denoms else (None, False)


def baseline_total(b):
    groups = b.get('groups') or []
    row, is_part = participants_row(b.get('denoms') or [])
    if not row or not groups:
        return None, 'no_denominator_row', None, None
    cnt = {c['groupId']: num(c.get('value')) for c in row.get('counts', [])}
    vals = [cnt.get(g['id']) for g in groups]
    if None in vals:
        return None, 'non_numeric_count', None, None
    titled = [i for i, g in enumerate(groups) if (g.get('title') or '').strip().casefold() == 'total']
    if len(groups) == 1:
        method, bt = 'single_group', vals[0]
    elif titled:
        method, bt = 'titled_total', vals[titled[0]]
    elif len(groups) >= 3 and vals[-1] > 0 and abs(vals[-1] - sum(vals[:-1])) < 1e-9:
        method, bt = 'last_equals_sum_untitled_total', vals[-1]
    else:
        method, bt = 'sum_of_arms', sum(vals)
    # what a naive "titled Total else sum of every group" rule would give (shows the double count)
    naive = vals[titled[0]] if titled else sum(vals)
    arms_sum = sum(v for i, v in enumerate(vals) if not (method != 'sum_of_arms' and len(groups) > 1 and i == (titled[0] if titled else len(groups) - 1)))
    return bt, method + ('' if is_part else '|bt_units_not_participants'), naive, (arms_sum if len(groups) > 1 else vals[0])


def flow_started(f):
    groups = [g for g in f.get('groups') or [] if (g.get('title') or '').strip().casefold() != 'total']
    periods = f.get('periods') or []
    if not periods:
        return None, 'no_period'
    ms = [m for m in periods[0].get('milestones') or [] if m.get('type') == 'STARTED']
    if not ms:
        return None, 'no_STARTED_in_first_period'
    a = {x['groupId']: x.get('numSubjects') for x in ms[0].get('achievements') or []}
    vals = [a.get(g['id']) for g in groups]
    if not all(is_num(v) for v in vals):
        return None, 'non_numeric_or_missing_numSubjects'
    return sum(num(v) for v in vals), 'ok'


rows = []
for n in ids:
    s = S[n]
    rs = s['resultsSection']
    b, f = rs.get('baselineCharacteristicsModule') or {}, rs.get('participantFlowModule') or {}
    ei = (s['protocolSection'].get('designModule') or {}).get('enrollmentInfo') or {}
    bt, bm, naive, arms_sum = baseline_total(b)
    fs, fm = flow_started(f)
    en = ei.get('count')
    r = {'nct_id': n, 'BT': bt, 'BT_method': bm, 'BT_naive': naive, 'BT_arms_sum': arms_sum, 'FS': fs, 'FS_method': fm,
         'EN': en, 'EN_type': ei.get('type'), 'n_periods': len(f.get('periods') or []),
         'first_period_title': ((f.get('periods') or [{}])[0]).get('title'),
         'flow_units': f.get('typeUnitsAnalyzed'), 'baseline_units': b.get('typeUnitsAnalyzed'),
         'baseline_population': b.get('populationDescription'),
         'baseline_groups': [g.get('title') for g in b.get('groups') or []],
         'flow_groups': [g.get('title') for g in f.get('groups') or []],
         'pre_assignment': f.get('preAssignmentDetails'),
         'study_type': (s['protocolSection'].get('designModule') or {}).get('studyType')}
    def rel(a, c):
        if a is None or c is None:
            return 'missing'
        return 'equal' if a == c else ('smaller' if a < c else 'larger')
    r['BT_vs_FS'], r['BT_vs_EN'], r['FS_vs_EN'] = rel(bt, fs), rel(bt, en), rel(fs, en)
    r['pattern'] = f"BT {r['BT_vs_FS']} FS; BT {r['BT_vs_EN']} EN; FS {r['FS_vs_EN']} EN"
    rows.append(r)
N = len(rows)
agg = {'n': N,
       'BT_method': dict(collections.Counter(r['BT_method'] for r in rows)),
       'FS_method': dict(collections.Counter(r['FS_method'] for r in rows)),
       'EN_type': dict(collections.Counter(str(r['EN_type']) for r in rows)),
       'BT_vs_FS': dict(collections.Counter(r['BT_vs_FS'] for r in rows)),
       'BT_vs_EN': dict(collections.Counter(r['BT_vs_EN'] for r in rows)),
       'FS_vs_EN': dict(collections.Counter(r['FS_vs_EN'] for r in rows)),
       'pattern': dict(collections.Counter(r['pattern'] for r in rows).most_common()),
       'all_three_equal': sum(1 for r in rows if r['BT_vs_FS'] == 'equal' and r['BT_vs_EN'] == 'equal'),
       'naive_rule_differs': [(r['nct_id'], r['BT'], r['BT_naive']) for r in rows if r['BT_naive'] != r['BT']],
       'total_not_sum_of_arms': [(r['nct_id'], r['BT'], r['BT_arms_sum']) for r in rows if r['BT_arms_sum'] is not None and r['BT'] is not None and r['BT_arms_sum'] != r['BT']],
       'population_description_present': sum(1 for r in rows if (r['baseline_population'] or '').strip()),
       'population_description_present_when_BT_ne_FS': [sum(1 for r in rows if r['BT_vs_FS'] not in ('equal', 'missing') and (r['baseline_population'] or '').strip()),
                                                        sum(1 for r in rows if r['BT_vs_FS'] not in ('equal', 'missing'))],
       'multi_period': sum(1 for r in rows if r['n_periods'] > 1),
       'second_unit_in_flow': sum(1 for r in rows if r['flow_units'])}
# 15 representative mismatches: allocated across mismatch patterns in proportion to their frequency
# (largest remainder, at least one from each of the six commonest patterns), drawn with seed 20261113.
mism = [r for r in rows if not (r['BT_vs_FS'] == 'equal' and r['BT_vs_EN'] == 'equal')]
by = collections.defaultdict(list)
for r in mism:
    by[r['pattern']].append(r)
pats = sorted(by, key=lambda p: (-len(by[p]), p))
alloc = {p: 0 for p in pats}
for p in pats[:6]:
    alloc[p] = 1
left = 15 - sum(alloc.values())
quota = {p: len(by[p]) / len(mism) * left for p in pats}
for p in pats:
    alloc[p] += int(quota[p])
rem = 15 - sum(alloc.values())
for p in sorted(pats, key=lambda p: -(quota[p] - int(quota[p])))[:rem]:
    alloc[p] += 1
rng = random.Random(20261113)
picked = []
for p in pats:
    pool = sorted(by[p], key=lambda r: r['nct_id'])
    picked += rng.sample(pool, min(alloc[p], len(pool)))
agg['n_mismatch'] = len(mism)
agg['mismatch_allocation'] = {p: [alloc[p], len(by[p])] for p in pats}
agg['representative_mismatches'] = picked
json.dump(agg, open(os.path.join(OUT, 'denominators.json'), 'w'), indent=1)
with open(os.path.join(OUT, 'per_trial_denominators.jsonl'), 'w') as f:
    for r in rows:
        f.write(json.dumps(r, sort_keys=True) + '\n')
for k, v in agg.items():
    if k != 'representative_mismatches':
        print(k, v)
for r in picked:
    print('\n', r['nct_id'], r['study_type'], '| BT', r['BT'], r['BT_method'], '| FS', r['FS'], '| EN', r['EN'], r['EN_type'], '|', r['pattern'])
    print('   periods', r['n_periods'], repr(r['first_period_title']), '| flow units', r['flow_units'], '| bgroups', [t[:30] for t in r['baseline_groups']], '| fgroups', [t[:30] for t in r['flow_groups']])
    print('   pop:', (r['baseline_population'] or '')[:400])
    print('   pre:', (r['pre_assignment'] or '')[:300])
