"""Re-draw the first audit's sample from the release and confirm it reproduces sample_ids.txt exactly.

Run: python3 -I scripts/verify_sample.py OUTDIR
Method (unchanged from the first audit's draw_sample.py): strata = the record's `study_type`;
proportional allocation of 2,000 by largest remainder; NCT IDs sorted ascending within stratum; one
random.Random(20261113) generator, strata visited in sorted name order, rng.sample(sorted_ids, n_h).
Writes sampling_verification.json.
"""
import sys, os, json, random, hashlib, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common

OUT = sys.argv[1]
REL = common.load_release(OUT)
by = collections.defaultdict(list)
for n, r in REL.items():
    by[str(r.get('study_type'))].append(n)
N = len(REL)
quota = {k: len(v) / N * 2000 for k, v in by.items()}
alloc = {k: int(q) for k, q in quota.items()}
for k in sorted(quota, key=lambda k: (-(quota[k] - int(quota[k])), k))[:2000 - sum(alloc.values())]:
    alloc[k] += 1
rng = random.Random(20261113)
sample = sorted(i for k in sorted(by) for i in rng.sample(sorted(by[k]), alloc[k]))
text = '\n'.join(sample) + '\n'
have = open(os.path.join(OUT, 'sample_ids.txt')).read()
meta = json.load(open(os.path.join(OUT, 'sampling.json')))
res = {'redraw_matches_sample_ids_txt': text == have, 'redraw_sha256': hashlib.sha256(text.encode()).hexdigest(),
       'sample_ids_txt_sha256': hashlib.sha256(have.encode()).hexdigest(), 'sampling_json_sha256': meta['sample_ids_sha256'],
       'allocation': alloc, 'seed': 20261113, 'n': len(sample),
       'sample_profile': {'phase': collections.Counter(REL[n].get('phase') for n in sample).most_common(),
                          'sponsor_class': collections.Counter(REL[n].get('sponsor_class') for n in sample).most_common(),
                          'results_year_min_max': [min(REL[n]['results_date'] for n in sample), max(REL[n]['results_date'] for n in sample)]}}
json.dump(res, open(os.path.join(OUT, 'sampling_verification.json'), 'w'), indent=1)
print(json.dumps(res)[:900])
