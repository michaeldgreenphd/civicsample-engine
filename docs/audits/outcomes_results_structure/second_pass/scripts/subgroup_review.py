"""Join the rule's signals with the reviewer's judgements; write subgroup_review.csv and subgroup_summary.json.

Run: python3 -I scripts/subgroup_review.py V1_DIR OUTDIR
Inputs: subgroup_detected.jsonl, subgroup_negative_samples.json, review/judgements_positives.tsv,
review/judgements_negatives.tsv (negatives not listed there were judged `no_subgroup_result`).
The judgements are those of one reviewer (the auditing model) reading review/*_digest.txt and, for
ambiguous records, the cached record itself. They are not an independent human review.
"""
import sys, os, json, csv, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
from common import frac

V1, OUT = sys.argv[1], sys.argv[2]
REL = common.load_release(OUT)
det = {json.loads(l)['nct_id']: json.loads(l)['signals'] for l in open(os.path.join(OUT, 'subgroup_detected.jsonl'))}
neg = json.load(open(os.path.join(OUT, 'subgroup_negative_samples.json')))
JP = {r['nct_id']: r for r in csv.DictReader(open(os.path.join(OUT, 'review', 'judgements_positives.tsv')), delimiter='\t')}
JN = {r['nct_id']: r for r in csv.DictReader(open(os.path.join(OUT, 'review', 'judgements_negatives.tsv')), delimiter='\t')}
assert set(JP) == set(det), 'every detected trial needs exactly one judgement'
N = 2000
rows = []
for n in sorted(det):
    fields = list(dict.fromkeys(s['field'] for s in det[n]))
    seen, texts = set(), []
    for s in det[n]:
        k = (s['field'], s['text'][:50])
        if k not in seen and len(texts) < 3:
            seen.add(k)
            texts.append(f"[{s['field']}|{s['dimension']}|{s['measure_type']}] " + ' '.join(s['text'][:220].split()))
    j = JP[n]
    rows.append({'nct_id': n, 'sample_type': 'detected', 'matched_field': '; '.join(fields), 'matched_text': ' || '.join(texts),
                 'judgement': j['judgement'], 'reason': f"[{j['kind']}] {j['reason']}"})
targ = {x['nct_id']: x for x in neg['targeted40']}
for kind, lst in (('negative_random', neg['random60']), ('negative_targeted', [x['nct_id'] for x in neg['targeted40']])):
    for n in lst:
        j = JN.get(n)
        why = (' (selection score %d: %s)' % (targ[n]['score'], ', '.join(targ[n]['why']))) if n in targ else ''
        rows.append({'nct_id': n, 'sample_type': kind, 'matched_field': '', 'matched_text': '',
                     'judgement': j['judgement'] if j else 'no_subgroup_result',
                     'reason': (f"[{j['kind']}] {j['reason']}" if j else '[none] group, class and category titles, by/stratified titles and all loose demographic or stratification mentions read; no outcome posted by subgroup') + why})
with open(os.path.join(OUT, 'subgroup_review.csv'), 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=['nct_id', 'sample_type', 'matched_field', 'matched_text', 'judgement', 'reason'])
    w.writeheader()
    w.writerows(rows)

def is_dem(n):
    return JP[n]['kind'].startswith('demographic')

def dims(n):
    k = JP[n]['kind']
    if not k.startswith('demographic'):
        return set()
    return {d.strip() for d in k.split(';')[0].replace('demographic:', '').split(',')}

true = [n for n in det if JP[n]['judgement'] == 'true_subgroup_result']
part = [n for n in det if JP[n]['judgement'] == 'partial']
fp = [n for n in det if JP[n]['judgement'] == 'false_positive']
V1_FIELDS = {'class_titles', 'category_titles', 'measure_title'}
def base(f):
    return f.split(':')[0]
by_field = {}
for fld in sorted({base(s['field']) for v in det.values() for s in v}):
    tr = [n for n in det if any(base(s['field']) == fld for s in det[n])]
    by_field[fld] = {'trials': len(tr), 'true': sum(1 for n in tr if n in true), 'true_demographic': sum(1 for n in tr if n in true and is_dem(n)),
                     'partial': sum(1 for n in tr if n in part), 'false_positive': sum(1 for n in tr if n in fp)}
only_new = [n for n in true if not any(base(s['field']) in V1_FIELDS for s in det[n])]
prim = lambda n: any(s['measure_type'] == 'PRIMARY' for s in det[n])
def prim_true(n):
    # a true demographic result whose level/cue signal sits on a PRIMARY measure
    return any(s['measure_type'] == 'PRIMARY' and s['field'] in ('class_titles', 'category_titles', 'group_titles', 'group_descriptions') for s in det[n])
v1_hits = collections.defaultdict(set)
for l in open(os.path.join(V1, 'subgroup_hits.jsonl')):
    r = json.loads(l)
    v1_hits[r['nct_id']].add('tier1' if r.get('tier1') else 'cue')
summ = {
    'n': N, 'detected': len(det), 'detected_text': frac(len(det), N),
    'judgement_counts': dict(collections.Counter(JP[n]['judgement'] for n in det)),
    'true_any': frac(len(true), N), 'true_demographic': frac(sum(1 for n in true if is_dem(n)), N),
    'true_demographic_ids': sorted(n for n in true if is_dem(n)),
    'true_non_demographic_only': frac(sum(1 for n in true if not is_dem(n)), N),
    'true_non_demographic_ids': sorted(n for n in true if not is_dem(n)),
    'partial': frac(len(part), N), 'false_positive': frac(len(fp), N),
    'precision_true_over_detected': frac(len(true), len(det)),
    'precision_true_or_partial_over_detected': frac(len(true) + len(part), len(det)),
    'true_by_dimension': {d: sorted(n for n in true if d in dims(n)) for d in ('sex', 'race', 'ethnicity', 'origin', 'age')},
    'true_race_or_ethnicity': sorted(n for n in true if dims(n) & {'race', 'ethnicity'}),
    'true_demographic_on_primary_measure': sorted(n for n in true if is_dem(n) and prim_true(n)),
    'true_demographic_race_reported_in_release': sum(1 for n in true if is_dem(n) and REL[n].get('race_reported')),
    'by_field': by_field,
    'true_found_only_through_fields_added_in_v2': sorted(only_new),
    'true_demographic_found_only_through_fields_added_in_v2': sorted(n for n in only_new if is_dem(n)),
    'keyword_only_trials': {'n': sum(1 for n in det if all(s['dimension'] == 'keyword' for s in det[n])),
                            'false_positive': sum(1 for n in fp if all(s['dimension'] == 'keyword' for s in det[n]))},
    'v1_flagged_measure_trials': len(v1_hits), 'v1_flagged_not_detected_by_v2': sorted(set(v1_hits) - set(det)),
    'v1_tier1_trials': sum(1 for v in v1_hits.values() if 'tier1' in v),
    'negatives': {
        'non_detected': neg['n_non_detected'],
        'random_n': 60, 'random_missed_demographic': sum(1 for n in neg['random60'] if n in JN and JN[n]['judgement'] == 'missed_subgroup_result' and JN[n]['kind'].startswith('demographic')),
        'random_missed_non_demographic': sum(1 for n in neg['random60'] if n in JN and JN[n]['judgement'] == 'missed_subgroup_result' and not JN[n]['kind'].startswith('demographic')),
        'random_partial': sum(1 for n in neg['random60'] if n in JN and JN[n]['judgement'] == 'partial'),
        'targeted_n': 40, 'targeted_missed_demographic': sum(1 for n in targ if n in JN and JN[n]['judgement'] == 'missed_subgroup_result' and JN[n]['kind'].startswith('demographic')),
        'targeted_missed_non_demographic': sum(1 for n in targ if n in JN and JN[n]['judgement'] == 'missed_subgroup_result' and not JN[n]['kind'].startswith('demographic')),
        'targeted_partial': sum(1 for n in targ if n in JN and JN[n]['judgement'] == 'partial'),
        'targeted_cutoff_score': neg['targeted_cutoff_score'], 'n_rest_at_or_above_cutoff': neg['n_rest_at_or_above_cutoff'],
        'random_upper95_demographic': common.wilson(0, 60)[1],
    },
}
json.dump(summ, open(os.path.join(OUT, 'subgroup_summary.json'), 'w'), indent=1)
print(json.dumps({k: v for k, v in summ.items() if not k.endswith('_ids')}, indent=1))
