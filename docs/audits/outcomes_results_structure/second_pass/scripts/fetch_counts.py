"""Count-level answer to "do eligible trials post results?" on a frame that does NOT require results.

Run: python3 -I scripts/fetch_counts.py OUTDIR
For each primary-completion year 2015-2023 and each stratum below, asks the registry for two counts
(`countTotal=true&pageSize=1&fields=NCTId`): all matching studies, and those that also satisfy the
engine's own results condition `AREA[ResultsFirstPostDate]RANGE[MIN,MAX]`. "Without results" is the
difference; for the all-interventional row it is also asked for directly three other ways
(NOT ..., aggFilters=results:with / results:without, AREA[HasResults]true) as a consistency check.
These are registry-wide counts on the retrieval date, not a per-trial audit: no record is read, legal
applicability (FDAAA "applicable clinical trial"), extensions and certified delays are not established.
Writes OUTDIR/results_posting_counts.json; responses cached in cache/count_queries.jsonl.gz.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ctgov

OUT = sys.argv[1]
cache = ctgov.Cache(OUT, 'count_queries.jsonl.gz')
HAS = 'AREA[ResultsFirstPostDate]RANGE[MIN,MAX]'
STRATA = {
    'interventional_all': 'AREA[StudyType]INTERVENTIONAL',
    'interventional_completed_or_terminated': 'AREA[StudyType]INTERVENTIONAL AND AREA[OverallStatus](COMPLETED OR TERMINATED)',
    'interventional_phase3': 'AREA[StudyType]INTERVENTIONAL AND AREA[Phase]PHASE3',
    'interventional_industry_lead': 'AREA[StudyType]INTERVENTIONAL AND AREA[LeadSponsorClass]INDUSTRY',
    'interventional_phase3_industry_randomized': ('AREA[StudyType]INTERVENTIONAL AND AREA[LeadSponsorClass]INDUSTRY AND '
                                                  'AREA[Phase]PHASE3 AND AREA[DesignAllocation]RANDOMIZED'),
    'interventional_fda_regulated_drug_or_device': ('AREA[StudyType]INTERVENTIONAL AND '
                                                    '(AREA[IsFDARegulatedDrug]true OR AREA[IsFDARegulatedDevice]true)'),
    'interventional_us_location': 'AREA[StudyType]INTERVENTIONAL AND AREA[LocationCountry]"United States"',
    'observational_all': 'AREA[StudyType]OBSERVATIONAL',
}
YEARS = list(range(2015, 2024))


def count(adv, purpose, **extra):
    p = {'countTotal': 'true', 'pageSize': 1, 'fields': 'NCTId'}
    if adv:
        p['filter.advanced'] = adv
    p.update(extra)
    url = ctgov.studies_url(p)
    body, rec = cache.get(url, purpose)
    return body.get('totalCount'), url, rec['fetched_at']


out = {'definition': {'has_results_expression': HAS, 'year_expression': 'AREA[PrimaryCompletionDate]RANGE[Y-01-01,Y-12-31]',
                      'strata': STRATA, 'request': 'GET /api/v2/studies?countTotal=true&pageSize=1&fields=NCTId&filter.advanced=<expr>'},
       'cells': [], 'frame_checks': {}, 'consistency_checks': [], 'failures': []}
times = []
for name, base in STRATA.items():
    for y in YEARS:
        expr = f'{base} AND AREA[PrimaryCompletionDate]RANGE[{y}-01-01,{y}-12-31]'
        try:
            n_all, u1, t1 = count(expr, f'count:{name}:{y}:all')
            n_res, u2, t2 = count(f'{expr} AND {HAS}', f'count:{name}:{y}:with_results')
        except RuntimeError as e:
            out['failures'].append({'stratum': name, 'year': y, 'error': str(e)})
            continue
        times += [t1, t2]
        out['cells'].append({'stratum': name, 'year': y, 'expr_all': expr, 'expr_with_results': f'{expr} AND {HAS}',
                             'n_all': n_all, 'n_with_results': n_res, 'n_without_results': n_all - n_res})
        print(name, y, n_all, n_res, flush=True)
# Consistency checks on the all-interventional row: four ways of asking for the same split.
for y in YEARS:
    expr = f'AREA[StudyType]INTERVENTIONAL AND AREA[PrimaryCompletionDate]RANGE[{y}-01-01,{y}-12-31]'
    try:
        a, _, _ = count(f'{expr} AND NOT {HAS}', f'check:{y}:not_results_first_post')
        b, _, _ = count(expr, f'check:{y}:agg_with', aggFilters='results:with')
        c, _, _ = count(expr, f'check:{y}:agg_without', aggFilters='results:without')
        d, _, _ = count(f'{expr} AND AREA[HasResults]true', f'check:{y}:has_results_true')
    except RuntimeError as e:
        out['failures'].append({'check': y, 'error': str(e)})
        continue
    out['consistency_checks'].append({'year': y, 'not_results_first_post': a, 'aggFilters_results_with': b,
                                      'aggFilters_results_without': c, 'HasResults_true': d})
# The engine frame itself, as the registry stands on the retrieval date.
for key, expr in {
    'engine_frame_now': 'AREA[ResultsFirstPostDate]RANGE[2009-01-01,MAX]',
    'engine_frame_through_2026-11-08': 'AREA[ResultsFirstPostDate]RANGE[2009-01-01,2026-11-08]',
    'engine_frame_through_2026-11-06': 'AREA[ResultsFirstPostDate]RANGE[2009-01-01,2026-11-06]',
    'any_results_first_post_date': 'AREA[ResultsFirstPostDate]RANGE[MIN,MAX]',
    'results_first_post_before_2009': 'AREA[ResultsFirstPostDate]RANGE[MIN,2008-12-31]',
    'has_results_true': 'AREA[HasResults]true',
    'engine_frame_now_interventional': 'AREA[ResultsFirstPostDate]RANGE[2009-01-01,MAX] AND AREA[StudyType]INTERVENTIONAL',
    'engine_frame_now_observational': 'AREA[ResultsFirstPostDate]RANGE[2009-01-01,MAX] AND AREA[StudyType]OBSERVATIONAL',
    'all_registered_studies': '',
    'all_interventional': 'AREA[StudyType]INTERVENTIONAL',
    'all_observational': 'AREA[StudyType]OBSERVATIONAL',
}.items():
    try:
        n, url, t = count(expr, f'frame:{key}')
        out['frame_checks'][key] = {'expr': expr, 'n': n, 'fetched_at': t}
        times.append(t)
    except RuntimeError as e:
        out['failures'].append({'frame_check': key, 'error': str(e)})
body, rec = cache.get(f'{ctgov.BASE}/version', 'version')
out['api_version'] = body
out['first_fetch'], out['last_fetch'] = min(times), max(times)
json.dump(out, open(os.path.join(OUT, 'results_posting_counts.json'), 'w'), indent=1)
print(json.dumps(out['frame_checks'], indent=1)); print(out['consistency_checks']); print(out['failures'], out['api_version'])
