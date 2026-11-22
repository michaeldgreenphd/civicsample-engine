"""Inspect the demographics release (untrusted data: run with python -I)."""
import gzip, json, sys, collections
p = sys.argv[1]
with gzip.open(p, 'rt', encoding='utf-8') as f:
    d = json.load(f)
print({k: (v if not isinstance(v, list) else len(v)) for k, v in d.items()})
recs = d['data']
r = recs[0]
print(sorted(r.keys()))
print(json.dumps(r, indent=1)[:3500])
c = collections.Counter(x.get('study_type') for x in recs)
print(c)
print(collections.Counter(type(x.get('race')).__name__ for x in recs))
print(collections.Counter(str((x.get('race') or {}).get('reported')) for x in recs))
print(collections.Counter(str((x.get('ethnicity') or {}).get('reported')) for x in recs))
print(len(set(x['nct_id'] for x in recs)))
