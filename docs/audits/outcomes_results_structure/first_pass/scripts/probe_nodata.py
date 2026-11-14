"""Probe: PRIMARY measures with no data table; registered-vs-posted field equality; one odd trial."""
import gzip, json, sys, collections
S = {}
with gzip.open(sys.argv[1] + '/cache/raw_responses.jsonl.gz', 'rt') as f:
    for line in f:
        for s in json.loads(json.loads(line)['body'])['studies']:
            S[s['protocolSection']['identificationModule']['nctId']] = s
c = collections.Counter(); shown = 0; zero = 0; tr = set()
for i, s in S.items():
    for om in s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures']:
        if om['type'] != 'PRIMARY' or om.get('classes'): continue
        tr.add(i)
        vals = [x.get('value') for d in om.get('denoms') or [] for x in d.get('counts') or []]
        allzero = bool(vals) and all(v == '0' for v in vals)
        zero += allzero
        c[(om.get('reportingStatus'), 'denoms' in om, 'groups' in om, 'allzero' if allzero else 'notzero', 'popdesc' in om)] += 1
        if shown < 6 and not allzero and om.get('reportingStatus') == 'POSTED':
            shown += 1; print(i, json.dumps({k: v for k, v in om.items() if k not in ('groups', 'description')})[:600])
print(c.most_common(), zero, len(tr))
# registered vs posted equality of raw fields
eq = collections.Counter()
for i, s in S.items():
    rp = s['protocolSection']['outcomesModule'].get('primaryOutcomes') or []
    pp = [om for om in s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'] if om['type'] == 'PRIMARY']
    if len(rp) != len(pp): print('count differs', i, len(rp), len(pp), [o['title'][:60] for o in pp]); continue
    for a, b in zip(rp, pp):
        eq['title_raw_equal_same_position'] += a.get('measure') == b.get('title')
        eq['timeFrame_raw_equal'] += a.get('timeFrame') == b.get('timeFrame')
        eq['description_raw_equal'] += a.get('description') == b.get('description')
        eq['n'] += 1
    rs_ = s['protocolSection']['outcomesModule'].get('secondaryOutcomes') or []
    sp = [om for om in s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'] if om['type'] == 'SECONDARY']
    if len(rs_) == len(sp):
        for a, b in zip(rs_, sp):
            eq['sec_title_raw_equal_same_position'] += a.get('measure') == b.get('title'); eq['sec_n'] += 1
print(eq)
