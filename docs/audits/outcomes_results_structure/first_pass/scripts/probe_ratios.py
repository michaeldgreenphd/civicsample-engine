"""Probe: trials with baseline_total/enrollment > 1.5, and trials with 0 completers in period 1."""
import gzip, json, sys, collections
S = {}
with gzip.open(sys.argv[1] + '/cache/raw_responses.jsonl.gz', 'rt') as f:
    for line in f:
        for s in json.loads(json.loads(line)['body'])['studies']:
            S[s['protocolSection']['identificationModule']['nctId']] = s
zero = collections.Counter(); n0 = 0
for i, s in sorted(S.items()):
    rs = s['resultsSection']; bm = rs['baselineCharacteristicsModule']
    gs = bm['groups']; dn = [d for d in bm['denoms'] if d['units'].lower() == 'participants'] or bm['denoms']
    counts = {c['groupId']: int(c['value']) for c in dn[0]['counts']}
    tot = [g for g in gs if g['title'].strip().lower() == 'total']
    bt = counts[tot[-1]['id']] if tot else (counts[gs[0]['id']] if len(gs) == 1 else sum(counts.values()))
    enr = s['protocolSection']['designModule']['enrollmentInfo']['count']
    p0 = rs['participantFlowModule']['periods'][0]
    st = sum(int(a['numSubjects']) for m in p0['milestones'] if m['type'] == 'STARTED' for a in m['achievements'])
    co = sum(int(a['numSubjects']) for m in p0['milestones'] if m['type'] == 'COMPLETED' for a in m['achievements'])
    if enr and bt / enr > 1.5:
        print(i, 'baseline', bt, 'enroll', enr, 'started', st, 'units', [d['units'] for d in bm['denoms']], 'flow units', rs['participantFlowModule'].get('typeUnitsAnalyzed'), 'groups', [g['title'][:20] for g in gs][:5])
    if st > 0 and co == 0:
        n0 += 1; zero[s['protocolSection']['statusModule']['overallStatus']] += 1
print('zero completers in period 1:', n0, zero)
