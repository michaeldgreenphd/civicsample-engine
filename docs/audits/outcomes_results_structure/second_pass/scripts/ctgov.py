"""Shared, polite, caching HTTP helper for the ClinicalTrials.gov API v2 (used by every fetch script).

- One request at a time, at least MIN_GAP seconds apart (0.5 s = 2 requests/second, under the 3/s asked for).
- Every response body is appended, unparsed, to a jsonl.gz cache with its URL, UTC time, HTTP status and
  sha256; a line is also appended to cache/fetch_manifest.jsonl (URL, time, status, bytes, purpose, cache
  file). A URL already in the cache is served from it and not requested again.
- Failures (non-200 after retries, network errors) are written to the manifest with status and error text
  and re-raised to the caller, which records them as retrieval failures; nothing is silently skipped.
- No API key; no environment variable is read here (urllib picks up the session proxy by itself).
"""
import gzip, json, os, time, hashlib, urllib.request, urllib.error, urllib.parse

BASE = 'https://clinicaltrials.gov/api/v2'
UA = 'outcomes-audit-script/2.0 (python-urllib)'
MIN_GAP, MAX_TRIES = 0.5, 5
_last = [0.0]


class Cache:
    def __init__(self, outdir, name):
        self.outdir = outdir
        self.rel = os.path.join('cache', name)
        self.path = os.path.join(outdir, self.rel)
        self.manifest = os.path.join(outdir, 'cache', 'fetch_manifest.jsonl')
        self.by_url = {}
        if os.path.exists(self.path):
            with gzip.open(self.path, 'rt', encoding='utf-8') as f:
                for line in f:
                    rec = json.loads(line)
                    self.by_url[rec['url']] = rec

    def get(self, url, purpose):
        """Return (parsed_json, record). Cached URLs are not re-requested."""
        if url in self.by_url:
            rec = self.by_url[url]
            return json.loads(rec['body']), rec
        last_err, status, body = None, None, None
        for attempt in range(MAX_TRIES):
            wait = MIN_GAP - (time.time() - _last[0])
            if wait > 0:
                time.sleep(wait)
            try:
                req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
                with urllib.request.urlopen(req, timeout=120) as r:
                    status, body = r.status, r.read().decode('utf-8')
                _last[0] = time.time()
                break
            except urllib.error.HTTPError as e:
                _last[0] = time.time()
                status, last_err = e.code, f'HTTP {e.code}'
                if e.code not in (429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                _last[0] = time.time()
                last_err = repr(e)
            time.sleep(min(60, 2 ** attempt * 2))
        now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        if body is None:
            with open(self.manifest, 'a') as m:
                m.write(json.dumps({'url': url, 'fetched_at': now, 'status': status, 'error': last_err,
                                    'purpose': purpose, 'cache_file': None}) + '\n')
            raise RuntimeError(f'retrieval failure: {url}: {last_err}')
        rec = {'url': url, 'status': status, 'fetched_at': now, 'purpose': purpose,
               'body_sha256': hashlib.sha256(body.encode('utf-8')).hexdigest(), 'body': body}
        with gzip.open(self.path, 'at', encoding='utf-8') as out:
            out.write(json.dumps(rec) + '\n')
        with open(self.manifest, 'a') as m:
            m.write(json.dumps({'url': url, 'fetched_at': now, 'status': status, 'bytes': len(body.encode('utf-8')),
                                'body_sha256': rec['body_sha256'], 'purpose': purpose, 'cache_file': self.rel}) + '\n')
        self.by_url[url] = rec
        return json.loads(body), rec


def studies_url(params):
    return f'{BASE}/studies?' + urllib.parse.urlencode(params)
