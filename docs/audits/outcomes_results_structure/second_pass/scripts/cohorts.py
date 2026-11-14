"""Sizes of the recommended narrow cohorts on the full release, and a profile of the existing candidate
cohort ("industry Phase 3, primary completion 2020") as it appears in the release.

Run: python3 -I scripts/cohorts.py OUTDIR
Every cohort below is: family membership (families.py, gated) AND allocation == "RANDOMIZED" AND
phase == "PHASE3" AND the extra conditions listed. `time_weeks` parses the release's
`primary_outcome_time_frame` (the first registered primary outcome's time frame) with the same parser
as family_detail.py; a trial whose time frame is not one fixed time point is outside any time window.
These are counts of records that MATCH A TEXT DEFINITION; they are an upper bound on a usable cohort,
because posted values, units, populations and estimates still have to be checked per trial.
"""
import sys, os, json, re, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common, families

OUT = sys.argv[1]
REL = common.load_release(OUT)


def time_weeks(tf):
    t = (tf or '').lower()
    if not t.strip():
        return None
    if re.search(r'\bup to\b|\buntil\b|\bthrough(out)?\b|\bmaximum\b|\bapproximately\b|\bassessed\b|\bmedian\b|\bfrom\b.*\bto\b|\band\b|,', t):
        m = re.fullmatch(r'\s*(?:from\s+)?(?:baseline|day 1|day 0|week 0|randomi[sz]ation|pre-?dose)[^0-9]{0,40}(week|month|day|year)s?\s*(\d+(?:\.\d+)?)\s*\.?\s*', t) or \
            re.fullmatch(r'\s*(?:from\s+)?(?:baseline|day 1|day 0|week 0|randomi[sz]ation)[^0-9]{0,40}(\d+(?:\.\d+)?)\s*(week|month|day|year)s?\s*\.?\s*', t)
        if not m:
            return None
    vals = []
    for a, b, c, d in re.findall(r'(week|month|day|year)s?\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*-?\s*(week|month|day|year)s?', t):
        unit, v = (a, float(b)) if a else (d, float(c))
        vals.append(v * {'week': 1, 'month': 52 / 12, 'day': 1 / 7, 'year': 52}[unit])
    vals = [v for v in vals if v > 0.2]
    return round(max(vals), 1) if vals else None


def cond(r):
    return '; '.join(r.get('conditions') or []).lower()


COHORTS = collections.OrderedDict([
    ('T2DM: HbA1c change from baseline at 24-26 weeks', ('hba1c_change', lambda r: re.search(r'type 2|type ii|t2dm', cond(r)) and re.search(r'change', r['primary_endpoint'], re.I) and (time_weeks(r.get('primary_outcome_time_frame')) or 0) >= 24 and (time_weeks(r.get('primary_outcome_time_frame')) or 99) <= 26.1)),
    ('T2DM: HbA1c change from baseline, any time point', ('hba1c_change', lambda r: re.search(r'type 2|type ii|t2dm', cond(r)) and re.search(r'change', r['primary_endpoint'], re.I))),
    ('MDD (not bipolar/resistant): MADRS total change at 6-8 weeks', ('depression_scale', lambda r: re.search(r'madrs|montgomery', r['primary_endpoint'], re.I) and re.search(r'change', r['primary_endpoint'], re.I) and not re.search(r'bipolar|resistant|refractory', cond(r)) and 6 <= (time_weeks(r.get('primary_outcome_time_frame')) or 0) <= 8.1)),
    ('Depression: MADRS total change, any time point', ('depression_scale', lambda r: re.search(r'madrs|montgomery', r['primary_endpoint'], re.I) and re.search(r'change', r['primary_endpoint'], re.I))),
    ('Rheumatoid arthritis: ACR20 at week 24', ('acr20', lambda r: re.search(r'rheumatoid', cond(r)) and 23.9 <= (time_weeks(r.get('primary_outcome_time_frame')) or 0) <= 24.5)),
    ('Rheumatoid arthritis: ACR20, any time point', ('acr20', lambda r: re.search(r'rheumatoid', cond(r)))),
    ('Psoriatic arthritis: ACR20 at weeks 12-24', ('acr20', lambda r: re.search(r'psoria', cond(r)) and 12 <= (time_weeks(r.get('primary_outcome_time_frame')) or 0) <= 24.5)),
    ('COPD: trough/pre-dose FEV1 at 12-24 weeks', ('fev1', lambda r: re.search(r'copd|chronic obstructive', cond(r)) and re.search(r'trough|pre-?dose|pre-?bronchodilator|morning', r['primary_endpoint'], re.I) and 12 <= (time_weeks(r.get('primary_outcome_time_frame')) or 0) <= 24.5)),
    ('COPD: any FEV1 primary', ('fev1', lambda r: re.search(r'copd|chronic obstructive', cond(r)))),
    ('Asthma: any FEV1 primary', ('fev1', lambda r: re.search(r'asthma', cond(r)))),
    ('Breast cancer: PFS', ('progression_free_survival', lambda r: re.search(r'breast', cond(r)))),
    ('Lung cancer: PFS', ('progression_free_survival', lambda r: re.search(r'lung|nsclc|sclc', cond(r)))),
    ('Lung cancer: OS', ('overall_survival', lambda r: re.search(r'lung|nsclc|sclc', cond(r)))),
    ('Multiple myeloma: PFS', ('progression_free_survival', lambda r: re.search(r'myeloma', cond(r)))),
    ('Oncology: PFS, all tumour types', ('progression_free_survival', lambda r: True)),
    ('Oncology: OS, all tumour types', ('overall_survival', lambda r: True)),
])
out = {'cohorts': {}}
for name, (fam, extra) in COHORTS.items():
    mem = [r for r in REL.values() if families.member(r, fam) and families.is_rp3(r) and extra(r)]
    out['cohorts'][name] = {'family': fam, 'n': len(mem), 'race_reported': sum(1 for r in mem if r.get('race_reported') is True),
                            'ethnicity_reported': sum(1 for r in mem if r.get('ethnicity_reported') is True),
                            'industry': sum(1 for r in mem if r.get('sponsor_class') == 'INDUSTRY'),
                            'results_2017_or_later': sum(1 for r in mem if (r.get('results_date') or '') >= '2017'),
                            'race_reported_results_2017_or_later': sum(1 for r in mem if (r.get('results_date') or '') >= '2017' and r.get('race_reported') is True)}
# the existing candidate cohort as it appears in the release (posted results only, by construction)
ex = [r for r in REL.values() if r.get('study_type') == 'INTERVENTIONAL' and r.get('sponsor_class') == 'INDUSTRY' and r.get('phase') == 'PHASE3'
      and r.get('allocation') == 'RANDOMIZED' and (r.get('primary_completion_date') or '').startswith('2020')]
fam_any = collections.Counter()
for r in ex:
    hit = [f for f in families.FAMILIES if f not in ('svr_hcv', 'hiv_rna') and families.member(r, f)]
    for f in hit:
        fam_any[f] += 1
    if not hit:
        fam_any['(no family)'] += 1
out['existing_cohort_in_release'] = {
    'definition': 'study_type INTERVENTIONAL, sponsor_class INDUSTRY, phase == PHASE3, allocation RANDOMIZED, primary_completion_date starts with 2020',
    'n': len(ex), 'race_reported': sum(1 for r in ex if r.get('race_reported') is True),
    'ethnicity_reported': sum(1 for r in ex if r.get('ethnicity_reported') is True),
    'primary_condition': collections.Counter(r.get('primary_condition') for r in ex).most_common(),
    'families': fam_any.most_common(),
    'distinct_normalised_primary_endpoint_titles': len({re.sub(r'[^a-z0-9]+', ' ', (r.get('primary_endpoint') or '').lower()).strip() for r in ex}),
    'status': collections.Counter(r.get('status') for r in ex).most_common(),
}
onc = [r for r in ex if r.get('primary_condition') == 'Oncology']
out['existing_cohort_in_release']['oncology'] = {'n': len(onc), 'race_reported': sum(1 for r in onc if r.get('race_reported') is True),
                                                 'pfs': sum(1 for r in onc if families.member(r, 'progression_free_survival')),
                                                 'os': sum(1 for r in onc if families.member(r, 'overall_survival')),
                                                 'orr': sum(1 for r in onc if families.member(r, 'objective_response_rate'))}
# release by primary-completion year (for the count comparison in section 1)
by_year = collections.Counter()
for r in REL.values():
    if r.get('study_type') == 'INTERVENTIONAL':
        by_year[(r.get('primary_completion_date') or '')[:4]] += 1
out['release_interventional_by_primary_completion_year'] = {y: by_year[y] for y in map(str, range(2015, 2024))}
out['release_missing_primary_completion_date'] = sum(1 for r in REL.values() if not r.get('primary_completion_date'))
json.dump(out, open(os.path.join(OUT, 'cohorts.json'), 'w'), indent=1)
for k, v in out['cohorts'].items():
    print(f"{k:62s} n={v['n']:4d} race={v['race_reported']:4d} eth={v['ethnicity_reported']:4d} ind={v['industry']:4d} 2017+={v['results_2017_or_later']:4d} race&2017+={v['race_reported_results_2017_or_later']}")
print(json.dumps(out['existing_cohort_in_release'])[:1500]); print(out['release_interventional_by_primary_completion_year'], out['release_missing_primary_completion_date'])
