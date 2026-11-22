"""Build a slim, line-oriented copy of the full release so later steps do not re-parse 1 GB of JSON.

Run: python3 -I scripts/build_slim.py RELEASE_GZ OUTDIR
Reads the release `{"extracted_at","pipeline_commit","data":[...]}` and writes
OUTDIR/release_slim.jsonl.gz (one record per trial, only the fields this audit uses, values verbatim)
and OUTDIR/release_meta.json (container stamps, record count, file sha256, key inventory).
Nothing is recoded here: `race_reported`/`ethnicity_reported`/`sex_reported` are the record's own
`<dim>.reported` booleans (None when the block is absent, never coerced to False).
"""
import gzip, json, sys, hashlib, os, collections

src, out = sys.argv[1], sys.argv[2]
h = hashlib.sha256()
with open(src, 'rb') as f:
    for chunk in iter(lambda: f.read(1 << 20), b''):
        h.update(chunk)
with gzip.open(src, 'rt', encoding='utf-8') as f:
    doc = json.load(f)
data = doc['data']
keys = collections.Counter()
KEEP = ['nct_id', 'study_type', 'phase', 'allocation', 'sponsor_class', 'conditions', 'primary_condition',
        'secondary_condition', 'condition_classifications', 'primary_endpoint', 'secondary_outcomes',
        'enrollment', 'enrollment_type', 'results_date', 'primary_completion_date', 'start_date', 'status',
        'title', 'brief_title', 'interventions', 'intervention_types', 'masking', 'intervention_model',
        'primary_purpose', 'lead_sponsor_name', 'primary_outcome_time_frame', 'primary_outcome_description', 'keywords', 'completion_date', 'last_update']
with gzip.open(os.path.join(out, 'release_slim.jsonl.gz'), 'wt', encoding='utf-8') as o:
    for r in data:
        keys.update(r.keys())
        s = {k: r.get(k) for k in KEEP if k in r}
        for dim in ('race', 'ethnicity', 'sex'):
            blk = r.get(dim)
            s[dim + '_reported'] = blk.get('reported') if isinstance(blk, dict) else None
        o.write(json.dumps(s, separators=(',', ':')) + '\n')
meta = {'source': src, 'source_sha256': h.hexdigest(), 'extracted_at': doc.get('extracted_at'),
        'pipeline_commit': doc.get('pipeline_commit'), 'n_records': len(data),
        'n_unique_nct': len({r.get('nct_id') for r in data}),
        'container_keys': [k for k in doc if k != 'data'], 'record_key_counts': dict(sorted(keys.items()))}
json.dump(meta, open(os.path.join(out, 'release_meta.json'), 'w'), indent=1)
print(json.dumps(meta, indent=1))
