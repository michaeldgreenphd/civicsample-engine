"""Summarise cache/fetch_manifest.jsonl (one line per HTTP request made by this audit's scripts).

Run: python3 -I scripts/manifest_summary.py OUTDIR
Writes cache/fetch_manifest_summary.json: requests by purpose group and status, first/last fetch,
failures, and the sha256 and line count of each cache file.
"""
import sys, os, json, gzip, hashlib, collections

OUT = sys.argv[1]
lines = [json.loads(l) for l in open(os.path.join(OUT, 'cache', 'fetch_manifest.jsonl'))]
by = collections.Counter((l['purpose'].split(':')[0], l['status']) for l in lines)
files = {}
for name in sorted(os.listdir(os.path.join(OUT, 'cache'))):
    if name.endswith('.jsonl.gz'):
        p = os.path.join(OUT, 'cache', name)
        with gzip.open(p, 'rt', encoding='utf-8') as f:
            n = sum(1 for _ in f)
        files[name] = {'responses': n, 'sha256': hashlib.sha256(open(p, 'rb').read()).hexdigest(), 'bytes': os.path.getsize(p)}
summ = {'requests': len(lines), 'by_purpose_and_status': {f'{k[0]}|{k[1]}': v for k, v in sorted(by.items(), key=str)},
        'failures': [l for l in lines if l.get('status') != 200], 'first_fetch': min(l['fetched_at'] for l in lines),
        'last_fetch': max(l['fetched_at'] for l in lines), 'cache_files': files,
        'not_in_manifest': '12 exploratory count requests were made by hand at about 2026-11-13T23:18Z before ctgov.py existed; '
                           'each was re-issued through the cache by fetch_counts.py and returned the same count. '
                           'The first audit\'s 40 study requests and 1 version request are in its own cache/fetch_manifest.json.'}
json.dump(summ, open(os.path.join(OUT, 'cache', 'fetch_manifest_summary.json'), 'w'), indent=1)
print(json.dumps(summ, indent=1)[:1800])
