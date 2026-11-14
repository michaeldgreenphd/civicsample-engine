"""Top up the examined set for the endpoint-comparability assessment and cache every response.

Run: python3 -I scripts/fetch_family.py V1_DIR OUTDIR
For each family in EXAMINED: the family's randomised Phase 3 members (families.py: gated, allocation
RANDOMIZED, phase PHASE3) that are in the 2,000 sample are used from the first audit's cache; because
every family has fewer than 15 such members in the sample, up to 40 more are drawn from the family's
randomised Phase 3 members NOT in the sample: random.Random(20261113).sample(sorted(candidates), 40),
one generator per family seeded with the same seed. Full records are fetched for the drawn IDs
(filter.ids, 40 per request); for the sample members only ConditionsModule and
ArmsInterventionsModule are fetched (their results are already cached).
Writes family_examined.json (per family: sample members, drawn IDs, returned, missing).
"""
import sys, os, json, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common, ctgov, families

V1, OUT = sys.argv[1], sys.argv[2]
EXAMINED = ['progression_free_survival', 'overall_survival', 'hba1c_change', 'fev1', 'acr20', 'depression_scale']
FIELDS_FULL = 'NCTId,HasResults,ResultsSection,OutcomesModule,StatusModule,DesignModule,ConditionsModule,ArmsInterventionsModule,SponsorCollaboratorsModule'
FIELDS_PROTO = 'NCTId,ConditionsModule,ArmsInterventionsModule,SponsorCollaboratorsModule'
REL = common.load_release(OUT)
sample = [l.strip() for l in open(os.path.join(V1, 'sample_ids.txt')) if l.strip()]
sset = set(sample)
cache = ctgov.Cache(OUT, 'family_studies.jsonl.gz')
out = {}
for fam in EXAMINED:
    mem = [n for n, r in REL.items() if families.member(r, fam)]
    rp3 = sorted(n for n in mem if families.is_rp3(REL[n]))
    in_sample_all = sorted(n for n in mem if n in sset)
    in_sample_rp3 = sorted(n for n in rp3 if n in sset)
    cand = [n for n in rp3 if n not in sset]
    drawn = sorted(random.Random(20261113).sample(cand, min(40, len(cand)))) if len(in_sample_rp3) < 15 else []
    got, failures = set(), []
    for label, idlist, fields in (('full', drawn, FIELDS_FULL), ('proto', in_sample_rp3, FIELDS_PROTO)):
        if not idlist:
            continue
        url = ctgov.studies_url({'filter.ids': ','.join(idlist), 'pageSize': len(idlist), 'fields': fields})
        try:
            body, rec = cache.get(url, f'family:{fam}:{label}')
        except RuntimeError as e:
            failures.append({'label': label, 'error': str(e)})
            continue
        ret = {s['protocolSection']['identificationModule']['nctId'] for s in body.get('studies', [])}
        if label == 'full':
            got = ret
        missing = sorted(set(idlist) - ret)
        if missing:
            failures.append({'label': label, 'missing': missing})
    out[fam] = {'gated_members_in_release': len(mem), 'rp3_members_in_release': len(rp3), 'sample_members_all_designs': in_sample_all,
                'sample_members_rp3': in_sample_rp3, 'drawn_rp3_not_in_sample': drawn, 'drawn_returned': len(got),
                'candidates_not_in_sample': len(cand), 'failures': failures, 'seed': 20261113}
    print(fam, len(mem), len(rp3), 'sample all', len(in_sample_all), 'sample rp3', len(in_sample_rp3), 'drawn', len(drawn), 'returned', len(got), failures)
json.dump(out, open(os.path.join(OUT, 'family_examined.json'), 'w'), indent=1)
