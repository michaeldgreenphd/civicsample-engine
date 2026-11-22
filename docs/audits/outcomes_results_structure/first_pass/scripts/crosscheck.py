"""Independent recomputation of headline figures, by a separate code path, compared with analysis.json.
Run: python3 -I crosscheck.py OUTDIR   (exits non-zero on any mismatch)"""
import gzip, json, sys, os
OUT = sys.argv[1]
A = json.load(open(os.path.join(OUT, 'analysis.json')))
F = json.load(open(os.path.join(OUT, 'sample_frame.json')))
st = []
for line in gzip.open(os.path.join(OUT, 'cache', 'raw_responses.jsonl.gz'), 'rt', encoding='utf-8'):
    st += json.loads(json.loads(line)['body'])['studies']
assert len(st) == 2000
got = dict(all5=0, prim_an=0, multi=0, pval=0, est_ci=0, race_and_an=0, serious=0, base_eq_started=0, prim_data=0, n_primary=0, n_prim_an=0, deaths=0)
for s in st:
    nct = s['protocolSection']['identificationModule']['nctId']
    rs = s['resultsSection']
    got['all5'] += all(k in rs and rs[k] for k in ('participantFlowModule', 'baselineCharacteristicsModule', 'outcomeMeasuresModule', 'adverseEventsModule', 'moreInfoModule'))
    prim = [m for m in rs['outcomeMeasuresModule']['outcomeMeasures'] if m['type'] == 'PRIMARY']
    got['n_primary'] += len(prim)
    ans = [a for m in prim for a in m.get('analyses', [])]
    got['n_prim_an'] += len(ans)
    got['prim_an'] += bool(ans)
    got['multi'] += any(len(m.get('groups', [])) >= 2 for m in prim)
    got['pval'] += any(a.get('pValue', '').strip() for a in ans)
    got['est_ci'] += any(a.get('paramValue', '').strip() and a.get('ciLowerLimit', '').strip() and a.get('ciUpperLimit', '').strip() for a in ans)
    got['race_and_an'] += bool(ans) and F[nct]['race']['reported'] is True
    got['prim_data'] += any(x.get('value', '').strip() for m in prim for c in m.get('classes', []) for cat in c.get('categories', []) for x in cat.get('measurements', []))
    ae = rs.get('adverseEventsModule', {})
    got['serious'] += len(ae.get('seriousEvents', [])) > 0
    eg = ae.get('eventGroups', [])
    got['deaths'] += bool(eg) and all('deathsNumAtRisk' in g for g in eg)
    bm = rs['baselineCharacteristicsModule']
    den = {c['groupId']: int(c['value']) for d in bm['denoms'] if d['units'].lower() == 'participants' for c in d['counts']}
    titles = [g['title'].lower() for g in bm['groups']]
    vals = [den[g['id']] for g in bm['groups']]
    if any('total' in t.split() or t.startswith('total') for t in titles): bt = vals[max(i for i, t in enumerate(titles) if 'total' in t.split() or t.startswith('total'))]
    elif len(vals) == 1: bt = vals[0]
    elif len(vals) >= 3 and vals[-1] == sum(vals[:-1]) and vals[-1] > 0: bt = vals[-1]
    else: bt = sum(vals)
    p0 = rs['participantFlowModule']['periods'][0]
    started = sum(int(a['numSubjects']) for m in p0['milestones'] if m['type'] == 'STARTED' for a in m['achievements'])
    got['base_eq_started'] += bt == started
exp = dict(all5=A['modules']['all_five']['k'], prim_an=A['analyses']['trials_with_primary_analysis']['of_all_sampled']['k'],
           multi=A['primary_measures']['trials_with_primary_having_2plus_groups']['k'], pval=A['analyses']['trial_level_of_all_sampled']['primary_analysis_with_pValue']['k'],
           est_ci=A['analyses']['trial_level_of_all_sampled']['primary_analysis_with_estimate_and_both_CI_limits']['k'],
           race_and_an=A['cooccurrence']['race_reported_x_primary_analysis']['a1_b1'], serious=A['adverse_events']['seriousEvents_nonempty']['k'],
           base_eq_started=next(r['k'] for r in A['baseline']['baseline_vs_flow_STARTED']['rows'] if r['value'] == 'equal'),
           prim_data=A['registered_vs_posted']['trials_with_posted_PRIMARY_carrying_data']['k'], n_primary=A['primary_measures']['n_primary_measures'],
           n_prim_an=A['analyses']['n_primary_analyses'], deaths=A['adverse_events']['all_groups_have_deathsNumAtRisk']['k'])
bad = 0
for k in exp:
    ok = got[k] == exp[k]; bad += not ok
    print(f"{'OK ' if ok else 'MISMATCH'} {k}: independent {got[k]} analysis.json {exp[k]}")
sys.exit(1 if bad else 0)
