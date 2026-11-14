"""Draw the seeded stratified sample. Run: python3 -I draw_sample.py DEMOGRAPHICS.json.gz OUTDIR

Method (stated so it can be reproduced exactly):
- Strata: the record's own `study_type`.
- Allocation: proportional, n_h = N_h / N * 2000, rounded by largest remainder so the total is 2000.
- Within each stratum, NCT IDs are sorted ascending (so file order does not matter), then
  random.Random(SEED) -- ONE generator shared across strata, strata visited in sorted name order --
  draws rng.sample(sorted_ids, n_h).
- Writes sample_ids.txt (sorted), sample_frame.json (the sampled records' fields this audit uses,
  copied verbatim from the release) and sampling.json (counts).
"""
import gzip, json, sys, random, collections, hashlib, os

SEED = 20261113
N_SAMPLE = 2000
KEEP = ['nct_id', 'study_type', 'phase', 'sponsor_class', 'results_date', 'start_date',
        'primary_completion_date', 'enrollment', 'enrollment_type', 'status', 'race', 'ethnicity',
        'sex', 'primary_endpoint', 'secondary_outcomes', 'primary_condition', 'secondary_condition',
        'condition_classifications', 'conditions']

src, out = sys.argv[1], sys.argv[2]
h = hashlib.sha256()
with open(src, 'rb') as f:
    for chunk in iter(lambda: f.read(1 << 20), b''):
        h.update(chunk)
with gzip.open(src, 'rt', encoding='utf-8') as f:
    d = json.load(f)
recs = d['data']
by = collections.defaultdict(list)
for r in recs:
    by[str(r.get('study_type'))].append(r['nct_id'])
N = len(recs)
assert len({r['nct_id'] for r in recs}) == N, 'duplicate nct_id'
quota = {k: len(v) / N * N_SAMPLE for k, v in by.items()}
alloc = {k: int(q) for k, q in quota.items()}
short = N_SAMPLE - sum(alloc.values())
for k in sorted(quota, key=lambda k: (-(quota[k] - int(quota[k])), k))[:short]:
    alloc[k] += 1
rng = random.Random(SEED)
chosen = {}
for k in sorted(by):
    ids = sorted(by[k])
    chosen[k] = rng.sample(ids, alloc[k])
sample = sorted(i for v in chosen.values() for i in v)
assert len(sample) == N_SAMPLE == len(set(sample))
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, 'sample_ids.txt'), 'w') as f:
    f.write('\n'.join(sample) + '\n')
sset = set(sample)
frame = {r['nct_id']: {k: r.get(k) for k in KEEP} for r in recs if r['nct_id'] in sset}
with open(os.path.join(out, 'sample_frame.json'), 'w') as f:
    json.dump(frame, f, sort_keys=True)
meta = {'seed': SEED, 'source_sha256': h.hexdigest(), 'extracted_at': d.get('extracted_at'),
        'pipeline_commit': d.get('pipeline_commit'), 'population_n': N,
        'population_by_study_type': {k: len(v) for k, v in sorted(by.items())},
        'exact_quota': {k: round(q, 3) for k, q in sorted(quota.items())},
        'sample_by_study_type': dict(sorted(alloc.items())), 'sample_n': len(sample),
        'sample_ids_sha256': hashlib.sha256(('\n'.join(sample) + '\n').encode()).hexdigest()}
with open(os.path.join(out, 'sampling.json'), 'w') as f:
    json.dump(meta, f, indent=1, sort_keys=True)
print(json.dumps(meta, indent=1, sort_keys=True))
r = frame[sample[0]]
print(json.dumps({k: r[k] for k in ('race', 'ethnicity', 'sex', 'primary_endpoint', 'secondary_outcomes', 'primary_condition', 'condition_classifications')}, indent=1)[:2500])
