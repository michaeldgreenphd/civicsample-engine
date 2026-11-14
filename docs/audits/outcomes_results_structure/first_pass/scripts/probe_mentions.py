"""Probe (not part of the measured results): list measures that MENTION a demographic word in the
title or level titles, to hand-check what the conservative rule keeps and drops."""
import gzip, json, sys, re, collections
S = {}
with gzip.open(sys.argv[1] + '/cache/raw_responses.jsonl.gz', 'rt') as f:
    for line in f:
        for s in json.loads(json.loads(line)['body'])['studies']:
            S[s['protocolSection']['identificationModule']['nctId']] = s
flag = {(json.loads(l)['nct_id'], json.loads(l)['title']) for l in open(sys.argv[1] + '/subgroup_hits.jsonl')}
pats = {'sex': r'\b(females?|women|girls?|males?|men|boys?|sex|gender)\b', 'race': r'\b(race|racial|caucasians?|asians?|african[- ]americans?|black|white|hispanics?|latin[oax]s?|ethnic\w*)\b',
        'age': r'\b(age[ds]?|adults?|adolescents?|children|elderly|years old|pediatric)\b'}
which = sys.argv[2]
seen = collections.OrderedDict()
for i in sorted(S):
    for om in S[i]['resultsSection']['outcomeMeasuresModule']['outcomeMeasures']:
        lv = [c.get('title', '') for c in om.get('classes') or []] + [cat.get('title', '') for c in om.get('classes') or [] for cat in c.get('categories') or []]
        src = 'title' if re.search(pats[which], om.get('title', ''), re.I) else ('levels' if any(re.search(pats[which], x, re.I) for x in lv) else None)
        if src and (i, om.get('title')) not in flag:
            seen.setdefault(i, []).append((src, om['type'], om['title'][:100], [x[:30] for x in lv if x][:5], [g.get('title', '')[:25] for g in om.get('groups') or []][:4]))
print(which, 'trials with unflagged mention:', len(seen))
for i, v in list(seen.items())[: int(sys.argv[3])]:
    print(i, len(v), v[0])
