"""Independent recount of the 2020 cohort's results-structure headline figures from the frozen
registry archive. Written from the rule wording in ../second_pass/definitions.json, without
reading outcomes/results_structure.py. Usage: recheck_cohort.py ARCHIVE.jsonl.gz ELIGIBLE_IDS.txt"""
import collections, gzip, json, re, sys

archive, eligible = sys.argv[1], set(open(sys.argv[2]).read().split())


def numeric(x):
    try:
        float(str(x).replace(",", "")); return True
    except (TypeError, ValueError):
        return False


c = collections.Counter()
for line in gzip.open(archive, 'rt'):
    s = json.loads(line); s = s.get('study', s)
    if s['protocolSection']['identificationModule']['nctId'] not in eligible:
        continue
    c['trials'] += 1
    if not s.get('hasResults'):
        continue
    c['posted_results'] += 1
    d = s['protocolSection'].get('designModule') or {}
    prim = [m for m in ((s.get('resultsSection') or {}).get('outcomeMeasuresModule') or {}).get('outcomeMeasures', [])
            if m.get('type') == 'PRIMARY']
    c1 = d.get('studyType') == 'INTERVENTIONAL' and any(len(m.get('groups') or []) >= 2 for m in prim)
    c2 = c1 and (d.get('designInfo') or {}).get('allocation') == 'RANDOMIZED'
    between = est = False
    for m in prim:
        ids = {g.get('id') for g in m.get('groups') or []}
        for a in m.get('analyses') or []:
            g = a.get('groupIds') or []
            if len(g) < 2 or not all(x in ids for x in g):
                continue
            has_p = bool(re.search(r'\d', str(a.get('pValue') or '')))
            has_ci = numeric(a.get('paramValue')) and numeric(a.get('ciLowerLimit')) and numeric(a.get('ciUpperLimit'))
            between |= has_p or has_ci; est |= has_ci
    c['C1'] += c1; c['C2'] += c2; c['C2_between_arm_analysis'] += c2 and between; c['C2_estimate_with_both_limits'] += c2 and est
for k, v in c.items():
    print(f'{k:32s}{v}')
