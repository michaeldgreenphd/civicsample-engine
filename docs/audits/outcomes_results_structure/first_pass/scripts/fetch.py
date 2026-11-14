"""Fetch the sampled studies from ClinicalTrials.gov API v2 and cache raw responses.

Run: python3 -I fetch.py OUTDIR
Reads OUTDIR/sample_ids.txt; writes OUTDIR/cache/raw_responses.jsonl.gz (one line per HTTP
response: request metadata + the unparsed body text) and OUTDIR/cache/fetch_manifest.json.
Politeness: one request at a time, >=0.5 s between requests (<=2/s, under the 3/s limit asked
for); retry with exponential backoff on 429/5xx/network errors. Re-running skips batches already
cached, so the analysis can be re-run offline from the cache alone. No API key is used and no
environment variable is read by this script (urllib picks up the session proxy itself).
"""
import gzip, json, sys, os, time, hashlib, urllib.request, urllib.error, urllib.parse

OUT = sys.argv[1]
BASE = 'https://clinicaltrials.gov/api/v2/studies'
FIELDS = 'NCTId,HasResults,ResultsSection,OutcomesModule,StatusModule,DesignModule'
BATCH, MIN_GAP, MAX_TRIES = 50, 0.5, 6
UA = 'outcomes-audit-script/1.0 (python-urllib)'

ids = [l.strip() for l in open(os.path.join(OUT, 'sample_ids.txt')) if l.strip()]
cache = os.path.join(OUT, 'cache', 'raw_responses.jsonl.gz')
done = {}
if os.path.exists(cache):
    with gzip.open(cache, 'rt', encoding='utf-8') as f:
        for line in f:
            rec = json.loads(line)
            done[tuple(rec['ids'])] = rec

def get(url):
    last = None
    for attempt in range(MAX_TRIES):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, r.read().decode('utf-8'), attempt
        except urllib.error.HTTPError as e:
            last = f'HTTP {e.code}'
            if e.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = repr(e)
        time.sleep(min(60, 2 ** attempt * 2))
    raise RuntimeError(f'giving up on {url}: {last}')

t_last = 0.0
n_req = n_retry = 0
with gzip.open(cache, 'at', encoding='utf-8') as out:
    for i in range(0, len(ids), BATCH):
        batch = ids[i:i + BATCH]
        if tuple(batch) in done:
            continue
        q = urllib.parse.urlencode({'filter.ids': ','.join(batch), 'pageSize': BATCH, 'fields': FIELDS}, safe=',')
        url = f'{BASE}?{q}'
        wait = MIN_GAP - (time.time() - t_last)
        if wait > 0:
            time.sleep(wait)
        status, body, retries = get(url)
        t_last = time.time()
        n_req += 1; n_retry += retries
        rec = {'ids': batch, 'url': url, 'status': status, 'retries': retries,
               'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
               'body_sha256': hashlib.sha256(body.encode('utf-8')).hexdigest(), 'body': body}
        out.write(json.dumps(rec) + '\n'); out.flush()
        done[tuple(batch)] = rec
        print(i, status, len(body), retries, flush=True)

# Verify coverage from the cache itself.
got, next_tokens, times = {}, 0, []
with gzip.open(cache, 'rt', encoding='utf-8') as f:
    for line in f:
        rec = json.loads(line)
        b = json.loads(rec['body'])
        times.append(rec['fetched_at'])
        next_tokens += 1 if b.get('nextPageToken') else 0
        for s in b.get('studies', []):
            got[s['protocolSection']['identificationModule']['nctId']] = 1
missing = sorted(set(ids) - set(got)); extra = sorted(set(got) - set(ids))
try:
    with urllib.request.urlopen(urllib.request.Request('https://clinicaltrials.gov/api/v2/version', headers={'User-Agent': UA}), timeout=60) as r:
        version = json.loads(r.read().decode())
except Exception as e:  # recorded, not fatal: the cache is what the analysis reads
    version = {'error': repr(e)}
h = hashlib.sha256()
with open(cache, 'rb') as f:
    h.update(f.read())
man = {'requested': len(ids), 'returned': len(got), 'missing': missing, 'unexpected': extra,
       'responses_with_nextPageToken': next_tokens, 'requests_this_run': n_req, 'retries_this_run': n_retry,
       'first_fetch': min(times), 'last_fetch': max(times), 'api_version': version, 'fields': FIELDS,
       'cache_file': 'cache/raw_responses.jsonl.gz', 'cache_sha256': h.hexdigest()}
json.dump(man, open(os.path.join(OUT, 'cache', 'fetch_manifest.json'), 'w'), indent=1, sort_keys=True)
print(json.dumps(man, indent=1, sort_keys=True))
