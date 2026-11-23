"""Resumable, rate-limited cross-source acquisition and frozen-cache verification.

Acquires and freezes evidence across three public endpoints with zero credentials required:
  1. ClinicalTrials.gov API v2 (`/api/v2/version` and `/api/v2/studies/{nct_id}`)
  2. Literature: Europe PMC REST citation search by NCT ID and NCBI E-utilities PubMed `esummary`
     for PMIDs listed in `protocolSection.referencesModule.references`
  3. openFDA Drugs@FDA (`/drug/drugsfda.json`) NDA/BLA records mentioning the NCT ID or linked
     investigational drug candidates

All live requests go through `Throttle`, enforcing `<= 2` requests per second per host
(`min_interval >= 0.5` seconds; default `0.55` seconds). Responses are logged append-only under
`<cache>/raw/*.jsonl` so interrupted runs resume without re-fetching settled answers (`200`/`404`).
`freeze_cache` writes canonical key-sorted `.jsonl.gz` archives (`mtime=0`), per-archive
`.meta.json` sidecars, and `<cache>/manifest.json`. Offline builds call `load_frozen_cache`
exclusively and refuse partial, unfrozen, missing, or sha256-mismatched caches.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

CTGOV_API = 'https://clinicaltrials.gov/api/v2'
EPMC_REST = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
NCBI_EUTILS = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi'
OPENFDA_DRUGSFDA = 'https://api.fda.gov/drug/drugsfda.json'

USER_AGENT = 'civicsample-drug-disclosure/1.0'
MAX_REQUESTS_PER_SECOND = 2.0
MIN_FLOOR_SECONDS = 0.5
MIN_INTERVAL_SECONDS = 0.55
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
FINAL_STATUSES = frozenset({200, 404})
MAX_ATTEMPTS = 4
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
UNUSABLE_200 = 'unusable_200'

CACHE_SCHEMA_VERSION = 1
SOURCES: dict[str, str] = {
    'ctgov': 'ClinicalTrials.gov API v2 (/api/v2/version and /api/v2/studies/{nct_id})',
    'literature': 'Europe PMC REST search and NCBI E-utilities PubMed esummary',
    'openfda': 'openFDA Drugs@FDA (/drug/drugsfda.json)',
}
STAGES = ('ctgov', 'literature', 'openfda', 'freeze')

NCT_RE = re.compile(r'^NCT\d{8}$')
PMID_RE = re.compile(r'^\d{1,10}$')
PLACEBO_TERMS = frozenset({
    'placebo', 'matching placebo', 'oral placebo', 'iv placebo', 'subcutaneous placebo',
    'sham', 'sham procedure', 'vehicle', 'saline', 'normal saline', '0.9% sodium chloride',
    'standard of care', 'usual care', 'best supportive care', 'observation', 'no intervention',
    'control', 'active comparator',
})

Response = tuple[int | None, dict[str, str], bytes, str | None]
Get = Callable[[str, dict[str, Any] | None], Response]


class CacheIntegrityError(ValueError):
    """Raised when a disclosure cache is missing, partial, unfrozen, or fails hash verification."""


@dataclass(frozen=True)
class FrozenCache:
    """Verified offline snapshot of frozen acquisition archives and manifest metadata."""

    cache_dir: Path
    manifest: dict[str, Any]
    requested_nct_ids: list[str]
    ctgov: dict[str, dict[str, Any]]
    ctgov_meta: dict[str, Any]
    literature: dict[str, dict[str, Any]]
    literature_meta: dict[str, Any] | None
    openfda: dict[str, dict[str, Any]]
    openfda_meta: dict[str, Any] | None


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(',', ':'), sort_keys=True) + '\n').encode('utf-8')


def write_atomic(path: Path, data: bytes) -> None:
    """Write beside `path` under a `.part` name and atomically replace `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.part')
    tmp.write_bytes(data)
    tmp.replace(path)


def read_nct_list(path: Path) -> list[str]:
    """Read distinct NCT IDs (one per line, `#` comments allowed); raise ValueError on bad/dup IDs."""
    ids = [line.strip() for line in path.read_text(encoding='utf-8').split('\n')
           if line.strip() and not line.strip().startswith('#')]
    bad = [item for item in ids if not NCT_RE.match(item)]
    if bad:
        raise ValueError(f'{path.name}: invalid NCT ID(s): {bad[:5]}')
    if len(set(ids)) != len(ids):
        raise ValueError(f'{path.name}: duplicate NCT IDs are not permitted')
    if not ids:
        raise ValueError(f'{path.name}: contains no NCT IDs')
    return sorted(ids)


class Throttle:
    """Per-host rate limiter enforcing `<= 2` requests/second (`min_interval >= 0.5` seconds)."""

    def __init__(
        self,
        min_interval: float = MIN_INTERVAL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min_interval < MIN_FLOOR_SECONDS:
            raise ValueError(
                f'min_interval ({min_interval}) must be >= {MIN_FLOOR_SECONDS}s (<= 2 requests/second)'
            )
        self.min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = urlsplit(url).netloc
        last = self._last.get(host)
        if last is not None:
            remaining = self.min_interval - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last[host] = self._clock()

    def back_off(self, seconds: float) -> None:
        self._sleep(seconds)


def requests_get(timeout: float = 90.0) -> Get:
    """HTTP GET transport using `requests.Session` when available, else stdlib `urllib.request`."""
    try:
        import requests
    except ModuleNotFoundError:
        import urllib.error
        import urllib.parse
        import urllib.request

        def get_stdlib(url: str, params: dict[str, Any] | None) -> Response:
            full_url = url
            if params:
                qs = urllib.parse.urlencode(params)
                sep = '&' if '?' in url else '?'
                full_url = f'{url}{sep}{qs}'
            req = urllib.request.Request(full_url, headers={'User-Agent': USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = resp.read(MAX_RESPONSE_BYTES + 1)
                    if len(data) > MAX_RESPONSE_BYTES:
                        return resp.status, dict(resp.headers.items()), b'', 'response_too_large'
                    return resp.status, dict(resp.headers.items()), data, None
            except urllib.error.HTTPError as exc:
                body = exc.read(MAX_RESPONSE_BYTES) if exc.fp is not None else b''
                return exc.code, dict(exc.headers.items()) if exc.headers else {}, body, None
            except (OSError, urllib.error.URLError) as exc:
                return None, {}, b'', f'{type(exc).__name__}: {exc}'[:300]

        return get_stdlib

    session = requests.Session()
    session.headers['User-Agent'] = USER_AGENT

    def get(url: str, params: dict[str, Any] | None) -> Response:
        try:
            resp = session.get(url, params=params, timeout=timeout, stream=True)
            buf = bytearray()
            for chunk in resp.iter_content(1 << 16):
                buf += chunk
                if len(buf) > MAX_RESPONSE_BYTES:
                    resp.close()
                    return resp.status_code, dict(resp.headers), b'', 'response_too_large'
            return resp.status_code, dict(resp.headers), bytes(buf), None
        except requests.RequestException as exc:
            return None, {}, b'', f'{type(exc).__name__}: {exc}'[:300]

    return get


def polite_get(
    get: Get,
    throttle: Throttle,
    url: str,
    params: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Rate-limited GET with exponential backoff on 429/5xx and transient network errors."""
    resp: Response = (None, {}, b'', 'not_attempted')
    for attempt in range(1, MAX_ATTEMPTS + 1):
        throttle.wait(url)
        resp = get(url, params)
        status, _, _, err = resp
        if err == 'response_too_large' or (status is not None and status not in RETRY_STATUSES):
            return resp, attempt
        if attempt < MAX_ATTEMPTS:
            throttle.back_off(2.0 ** attempt)
    return resp, MAX_ATTEMPTS


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read append-only JSONL splitting strictly on `\\n` (`0x0A`) so U+2028/U+2029/VT/FF inside
    `ensure_ascii=False` JSON strings never split a record mid-line."""
    if not path.exists():
        return []
    raw = path.read_bytes()
    if not raw:
        return []
    keep = raw[:raw.rfind(b'\n') + 1]
    if len(keep) != len(raw):
        path.write_bytes(keep)
    return [
        json.loads(raw_line.rstrip(b'\r').decode('utf-8'))
        for raw_line in keep.split(b'\n')
        if raw_line.strip()
    ]


class ResponseLog:
    """Append-only JSONL log keyed by logical request key; latest record wins on resume."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: dict[str, dict[str, Any]] = {rec['key']: rec for rec in read_jsonl(path)}

    def get(self, key: str) -> dict[str, Any] | None:
        return self.records.get(key)

    def settled(self, key: str) -> dict[str, Any] | None:
        rec = self.records.get(key)
        return rec if rec is not None and rec.get('status') in FINAL_STATUSES else None

    def put(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n'
        with self.path.open('a', encoding='utf-8') as fh:
            fh.write(line)
        self.records[record['key']] = record


def logged_json(
    log: ResponseLog,
    get: Get,
    throttle: Throttle,
    key: str,
    url: str,
    params: dict[str, Any] | None = None,
    tag: dict[str, Any] | None = None,
    valid: Callable[[Any], bool] | None = None,
) -> dict[str, Any] | None:
    """Return settled cached JSON record or fetch once, validating 200 payload shape."""
    wanted_params = params or {}
    held = log.settled(key)
    if held is not None and held.get('params', {}) == wanted_params:
        if valid is None or held.get('status') != 200 or valid(held.get('body')):
            return held
    (status, _, body, error), attempts = polite_get(get, throttle, url, wanted_params)
    if status is None:
        rec_err: dict[str, Any] = {
            'key': key,
            'url': url,
            'params': wanted_params,
            'tag': tag or {},
            'fetched_at': now_utc(),
            'status': None,
            'http_status': None,
            'attempts': attempts,
            'response_sha256': sha256_bytes(b''),
            'body': None,
            'error': error or 'transport_or_timeout_error',
        }
        log.put(rec_err)
        return rec_err
    parsed: Any = None
    body_sha = sha256_bytes(body) if body else sha256_bytes(b'')
    if body:
        try:
            parsed = json.loads(body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            error = error or 'invalid_json_body'
    logged_status: int | str = status
    if status == 200 and parsed is None:
        logged_status = UNUSABLE_200
        error = error or 'empty_or_invalid_json_on_200'
    elif status == 200 and valid is not None and not valid(parsed):
        logged_status = UNUSABLE_200
        error = error or 'invalid_answer_shape_on_200'
    rec: dict[str, Any] = {
        'key': key,
        'url': url,
        'params': wanted_params,
        'tag': tag or {},
        'fetched_at': now_utc(),
        'status': logged_status,
        'http_status': status,
        'attempts': attempts,
        'response_sha256': body_sha,
        'body': parsed,
        'error': error,
    }
    log.put(rec)
    return rec


# ---------------------------------------------------------------- validators & query planners

def ctgov_version_valid(body: Any) -> bool:
    return (
        isinstance(body, dict)
        and isinstance(body.get('apiVersion'), str)
        and bool(body['apiVersion'].strip())
        and isinstance(body.get('dataTimestamp'), str)
        and bool(body['dataTimestamp'].strip())
    )


def ctgov_study_valid(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    ps = body.get('protocolSection')
    if not isinstance(ps, dict):
        return False
    ident = ps.get('identificationModule')
    if not isinstance(ident, dict):
        return False
    nct = ident.get('nctId')
    return isinstance(nct, str) and NCT_RE.match(nct) is not None


def epmc_search_valid(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    hit_count = body.get('hitCount')
    result_list = body.get('resultList')
    if isinstance(hit_count, bool) or not isinstance(hit_count, int) or hit_count < 0:
        return False
    if not isinstance(result_list, dict):
        return False
    results = result_list.get('result')
    if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
        return False
    return hit_count >= len(results)


def pubmed_esummary_valid(body: Any, pmid: str) -> bool:
    if not isinstance(body, dict):
        return False
    res = body.get('result')
    if not isinstance(res, dict):
        return False
    entry = res.get(pmid)
    return isinstance(entry, dict)


def openfda_drugsfda_valid(body: Any) -> bool:
    """openFDA answers 0 matches with HTTP 404; HTTP 200 must carry `results` and `meta.results.total`."""
    if not isinstance(body, dict):
        return False
    results = body.get('results')
    if not isinstance(results, list) or not results or not all(isinstance(r, dict) for r in results):
        return False
    meta_results = (body.get('meta') or {}).get('results') if isinstance(body.get('meta'), dict) else None
    total = meta_results.get('total') if isinstance(meta_results, dict) else None
    if isinstance(total, bool) or not isinstance(total, int) or total < len(results):
        return False
    return True


# A strength is a number, or a combination product's `a/b` numbers (`320/9.6 mcg`), followed by a
# unit and any `/unit` denominators (`5 mg/mL`, `2 mg/kg`, `75 mg/m2`, `5 mcg/kg/min`), or by `%`.
# Units: mass (`mcg`, `ug`, `mg`, `g`, and `g` after a micro sign U+00B5 or Greek mu U+03BC), volume
# (`ml`), activity (`iu`, `unit`/`units`, `u`) and amount (`mEq`, `mmol`). Each alternative must end at a
# non-word character (the closing lookahead `(?=\W|$)`, which unlike `\b` also holds after `%`
# before a space), so a unit is stripped only as a whole token right after a number: `mg/mL` goes
# whole rather than as `mg` leaving `ml`, and `D3` in `Vitamin D3`, the `U` of `Insulin U-100` or a
# name such as `Unituxin` are never touched. Dosage-form words (`cream`, `tablet`) are kept.
DRUG_STRENGTH_RE = re.compile(
    r'\b\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)*\s*'
    r'(?:%|(?:units?|mmol|meq|mcg|[\u00b5\u03bc]g|ug|mg|ml|iu|g|u)'
    r'(?:\s*/\s*(?:dose|day|hour|min|hr|h|mcg|[\u00b5\u03bc]g|kg|m2|mg|ml|dl|ug|g|l))*)'
    r'(?=\W|$)',
    re.IGNORECASE,
)


def normalize_drug_term(raw: str) -> str | None:
    """Normalize an intervention drug label for candidate openFDA lookup; return None for placebos."""
    cleaned = re.sub(r'\([^)]*\)', ' ', raw or '')
    cleaned = DRUG_STRENGTH_RE.sub(' ', cleaned)
    cleaned = re.sub(r'[^A-Za-z0-9\- ]+', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip().lower()
    if len(cleaned) < 3 or cleaned in PLACEBO_TERMS:
        return None
    if any(cleaned.startswith(prefix) for prefix in ('placebo ', 'sham ', 'matching placebo')):
        return None
    return cleaned


def extract_drug_candidates(study: dict[str, Any] | None) -> list[str]:
    """Extract sorted distinct normalized drug/biologic candidate terms from a CT.gov v2 study."""
    if not isinstance(study, dict):
        return []
    ps = study.get('protocolSection') or {}
    arms = ps.get('armsInterventionsModule') or {}
    interventions = arms.get('interventions') or []
    if not isinstance(interventions, list):
        return []
    found: set[str] = set()
    for item in interventions:
        if not isinstance(item, dict):
            continue
        itype = str(item.get('type') or '').upper()
        if itype not in ('DRUG', 'BIOLOGICAL', 'COMBINATION_PRODUCT'):
            continue
        primary = normalize_drug_term(str(item.get('name') or ''))
        if primary:
            found.add(primary)
        others = item.get('otherNames') or []
        if isinstance(others, list):
            for other in others[:2]:
                if isinstance(other, str):
                    norm_other = normalize_drug_term(other)
                    if norm_other:
                        found.add(norm_other)
    return sorted(found)


def drug_query_key(drug_norm: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', drug_norm).strip('-')[:40] or 'drug'
    digest = sha256_bytes(drug_norm.encode('utf-8'))[:8]
    return f'openfda/drug/{slug}-{digest}'


# Drugs@FDA fields a drug term may appear in; generic and brand names often differ, so a match in
# any one field is enough.
OPENFDA_DRUG_FIELDS = (
    'openfda.generic_name',
    'openfda.brand_name',
    'products.brand_name',
    'products.active_ingredients.name',
)


def drug_query_params(drug_norm: str) -> dict[str, str]:
    """openFDA `search` matching `drug_norm` as a phrase in ANY of `OPENFDA_DRUG_FIELDS`.

    The clauses are joined with an explicit ` OR `, not `+`. The HTTP client form-encodes the
    value, so a space is sent as `+` (which openFDA decodes back to a space) while a literal `+`
    is sent as `%2B` and reaches the query parser as `+`, the "required clause" prefix. The old
    `field:"x"+field:"x"` form therefore required every field after the first to match, and an
    application naming the drug in only one or two fields was missed. Changing this string
    changes the request, so `stale_openfda_drug_queries` refuses records made with the old form.
    """
    quoted = '"' + drug_norm.replace('"', ' ') + '"'
    search = ' OR '.join(f'{field}:{quoted}' for field in OPENFDA_DRUG_FIELDS)
    return {'search': search, 'limit': '100'}


def stale_openfda_drug_queries(
    openfda_records: dict[str, dict[str, Any]],
    ctgov_records: dict[str, dict[str, Any]],
    nct_ids: Iterable[str],
) -> list[str]:
    """Keys of the drug-query records the build will read whose request differs from today's form.

    The build reads `drug_query_key(drug)` for each candidate term of each requested trial. A record
    there made with an earlier query form (`params` != `drug_query_params(drug)`) answers a different
    question, so a cache holding one is refused at freeze and load rather than read as this query's
    answer. Records for terms no requested trial has any more are never read and are ignored; a
    missing record is left to coverage, which reports the query as not answered. `acquire` re-fetches
    stale records on its own, since `logged_json` reuses a record only when its params match.
    """
    ids = sorted(set(nct_ids))
    stale: set[str] = set()
    for study in studies_from_ctgov_records(ctgov_records, ids).values():
        for drug in extract_drug_candidates(study):
            key = drug_query_key(drug)
            rec = openfda_records.get(key)
            if rec is not None and rec.get('params') != drug_query_params(drug):
                stale.add(key)
    return sorted(stale)


def extract_registry_references(study: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return normalized entries from `protocolSection.referencesModule.references`."""
    if not isinstance(study, dict):
        return []
    ps = study.get('protocolSection') or {}
    rm = ps.get('referencesModule') or {}
    refs = rm.get('references') or []
    if not isinstance(refs, list):
        return []
    out: list[dict[str, Any]] = []
    for idx, ref in enumerate(refs):
        if not isinstance(ref, dict):
            continue
        pmid_raw = ref.get('pmid')
        pmid = str(pmid_raw).strip() if pmid_raw is not None and str(pmid_raw).strip() else None
        ref_type_raw = ref.get('type')
        ref_type = str(ref_type_raw).strip().upper() if isinstance(ref_type_raw, str) and ref_type_raw.strip() else None
        citation_raw = ref.get('citation')
        citation = str(citation_raw).strip() if isinstance(citation_raw, str) and citation_raw.strip() else None
        retractions = ref.get('retractions') if isinstance(ref.get('retractions'), list) else []
        out.append({
            'index': idx,
            'pmid': pmid,
            'type': ref_type,
            'citation': citation,
            'retractions': retractions,
        })
    return out


# ---------------------------------------------------------------- acquisition stages

def raw_log_path(cache_dir: Path, source: str) -> Path:
    return cache_dir / 'raw' / f'{source}.jsonl'


def archive_path(cache_dir: Path, source: str) -> Path:
    return cache_dir / f'{source}.jsonl.gz'


def fetch_ctgov_studies(
    nct_ids: list[str],
    log: ResponseLog,
    get: Get,
    throttle: Throttle,
    day: str | None = None,
) -> dict[str, int]:
    """Fetch `/api/v2/version` and `/api/v2/studies/{nct_id}` for every requested NCT ID."""
    stamp_day = day or now_utc()[:10]
    logged_json(
        log,
        get,
        throttle,
        f'ctgov/version/{stamp_day}',
        f'{CTGOV_API}/version',
        None,
        tag={'day': stamp_day},
        valid=ctgov_version_valid,
    )
    answered = 0
    unanswered = 0
    for nct_id in sorted(set(nct_ids)):
        if not NCT_RE.match(nct_id):
            raise ValueError(f'invalid NCT ID: {nct_id!r}')
        rec = logged_json(
            log,
            get,
            throttle,
            f'ctgov/study/{nct_id}',
            f'{CTGOV_API}/studies/{nct_id}',
            None,
            tag={'nct_id': nct_id},
            valid=ctgov_study_valid,
        )
        if rec is not None and rec.get('status') in FINAL_STATUSES:
            answered += 1
        else:
            unanswered += 1
    return {'studies_answered': answered, 'studies_unanswered': unanswered}


def studies_from_ctgov_records(ctgov_records: dict[str, dict[str, Any]], nct_ids: Iterable[str]) -> dict[str, dict[str, Any] | None]:
    """Extract `{nct_id: study_dict_or_None}` for settled 200 study responses matching their ID."""
    out: dict[str, dict[str, Any] | None] = {}
    for nct_id in sorted(set(nct_ids)):
        rec = ctgov_records.get(f'ctgov/study/{nct_id}')
        body = rec.get('body') if rec and rec.get('status') == 200 else None
        own_id = (
            (((body or {}).get('protocolSection') or {}).get('identificationModule') or {}).get('nctId')
            if isinstance(body, dict)
            else None
        )
        out[nct_id] = body if isinstance(body, dict) and own_id == nct_id else None
    return out


def fetch_literature(
    nct_ids: list[str],
    studies_by_nct: dict[str, dict[str, Any] | None],
    log: ResponseLog,
    get: Get,
    throttle: Throttle,
) -> dict[str, int]:
    """Search Europe PMC by NCT ID and resolve registry-cited PMIDs via NCBI E-utilities."""
    epmc_answered = 0
    epmc_unanswered = 0
    for nct_id in sorted(set(nct_ids)):
        params = {'query': f'"{nct_id}"', 'format': 'json', 'pageSize': '100', 'resultType': 'core'}
        rec = logged_json(
            log,
            get,
            throttle,
            f'literature/epmc_nct/{nct_id}',
            EPMC_REST,
            params,
            tag={'nct_id': nct_id, 'route': 'epmc_nct'},
            valid=epmc_search_valid,
        )
        if rec is not None and rec.get('status') in FINAL_STATUSES:
            epmc_answered += 1
        else:
            epmc_unanswered += 1

    pmids: set[str] = set()
    for nct_id in sorted(set(nct_ids)):
        for ref in extract_registry_references(studies_by_nct.get(nct_id)):
            pmid = ref.get('pmid')
            if isinstance(pmid, str) and PMID_RE.match(pmid):
                pmids.add(pmid)

    pmid_answered = 0
    pmid_unanswered = 0
    for pmid in sorted(pmids):
        params = {'db': 'pubmed', 'id': pmid, 'retmode': 'json'}
        rec = logged_json(
            log,
            get,
            throttle,
            f'literature/pubmed_pmid/{pmid}',
            NCBI_EUTILS,
            params,
            tag={'pmid': pmid, 'route': 'pubmed_esummary'},
            valid=lambda body, p=pmid: pubmed_esummary_valid(body, p),
        )
        if rec is not None and rec.get('status') in FINAL_STATUSES:
            pmid_answered += 1
        else:
            pmid_unanswered += 1

    return {
        'epmc_answered': epmc_answered,
        'epmc_unanswered': epmc_unanswered,
        'pmid_answered': pmid_answered,
        'pmid_unanswered': pmid_unanswered,
    }


def fetch_openfda(
    nct_ids: list[str],
    studies_by_nct: dict[str, dict[str, Any] | None],
    log: ResponseLog,
    get: Get,
    throttle: Throttle,
) -> dict[str, int]:
    """Query openFDA Drugs@FDA by NCT ID and by each trial's drug/biologic candidate terms."""
    nct_answered = 0
    nct_unanswered = 0
    drug_to_ncts: dict[str, list[str]] = {}
    for nct_id in sorted(set(nct_ids)):
        params = {'search': f'"{nct_id}"', 'limit': '100'}
        rec = logged_json(
            log,
            get,
            throttle,
            f'openfda/nct/{nct_id}',
            OPENFDA_DRUGSFDA,
            params,
            tag={'nct_id': nct_id, 'query_kind': 'nct'},
            valid=openfda_drugsfda_valid,
        )
        if rec is not None and rec.get('status') in FINAL_STATUSES:
            nct_answered += 1
        else:
            nct_unanswered += 1
        for drug in extract_drug_candidates(studies_by_nct.get(nct_id)):
            drug_to_ncts.setdefault(drug, []).append(nct_id)

    drug_answered = 0
    drug_unanswered = 0
    for drug, linked_ncts in sorted(drug_to_ncts.items()):
        rec = logged_json(
            log,
            get,
            throttle,
            drug_query_key(drug),
            OPENFDA_DRUGSFDA,
            drug_query_params(drug),
            tag={'drug_norm': drug, 'nct_ids': sorted(set(linked_ncts)), 'query_kind': 'drug_candidate'},
            valid=openfda_drugsfda_valid,
        )
        if rec is not None and rec.get('status') in FINAL_STATUSES:
            drug_answered += 1
        else:
            drug_unanswered += 1

    return {
        'nct_queries_answered': nct_answered,
        'nct_queries_unanswered': nct_unanswered,
        'drug_queries_answered': drug_answered,
        'drug_queries_unanswered': drug_unanswered,
    }


# ---------------------------------------------------------------- local offline seeding

def _iter_studies_from_local_file(path: Path) -> Iterable[dict[str, Any]]:
    """Yield raw CT.gov v2 study dicts from `.json`, `.json.gz`, `.jsonl`, or `.jsonl.gz`."""
    name = path.name
    is_gz = name.endswith('.gz')
    is_jsonl = '.jsonl' in name
    raw = gzip.decompress(path.read_bytes()) if is_gz else path.read_bytes()
    if is_jsonl:
        for raw_line in raw.split(b'\n'):
            if not raw_line.strip():
                continue
            obj = json.loads(raw_line.rstrip(b'\r').decode('utf-8'))
            if isinstance(obj, dict) and 'protocolSection' in obj:
                yield obj
            elif isinstance(obj, dict) and isinstance(obj.get('body'), str):
                inner = json.loads(obj['body'])
                if isinstance(inner, dict) and isinstance(inner.get('studies'), list):
                    for st in inner['studies']:
                        if isinstance(st, dict) and 'protocolSection' in st:
                            yield st
            elif isinstance(obj, dict) and isinstance(obj.get('body'), dict):
                body = obj['body']
                if 'protocolSection' in body:
                    yield body
                elif isinstance(body.get('studies'), list):
                    for st in body['studies']:
                        if isinstance(st, dict) and 'protocolSection' in st:
                            yield st
        return

    parsed = json.loads(raw.decode('utf-8'))
    if isinstance(parsed, dict) and 'protocolSection' in parsed:
        yield parsed
    elif isinstance(parsed, dict) and isinstance(parsed.get('studies'), list):
        for st in parsed['studies']:
            if isinstance(st, dict) and 'protocolSection' in st:
                yield st
    elif isinstance(parsed, list):
        for st in parsed:
            if isinstance(st, dict) and 'protocolSection' in st:
                yield st


def ingest_local_ctgov_records(
    source_path: Path,
    cache_dir: Path,
    *,
    fetched_at: str,
    data_timestamp: str,
    nct_ids: list[str] | None = None,
    limit: int | None = None,
    frozen_at: str | None = None,
) -> dict[str, Any]:
    """Freeze local CT.gov v2 study files into `cache_dir` with sha256 manifest verification.

    Makes zero network calls. `literature` and `openfda` archives remain absent so their coverage
    routes report `not_searched` rather than falsely claiming `no_candidate_in_completed_scope`.
    Every ID in `nct_ids` must be present in the local files: an absent one is refused before
    anything is written, never recorded as a CT.gov 404 that no request returned.

    The code cannot know when local files were obtained, so both stamps are required and have no
    default (a fixed default would claim that date for every local snapshot):
      * `fetched_at`: when the records were retrieved from ClinicalTrials.gov, as stated by the
        person freezing them; stamped on each record and so the archive's first/last_fetched_at.
      * `data_timestamp`: the CT.gov `dataTimestamp` (data refresh) the records reflect; recorded
        in the local version record, reported as run.json `sources.ctgov.data_timestamp`, and the
        build's `as_of` when `--as-of` is not given.
    `frozen_at` is when the cache was frozen: the current time unless pinned (tests pin it).
    """
    for label, stamp in (('fetched_at', fetched_at), ('data_timestamp', data_timestamp)):
        try:
            datetime.fromisoformat(stamp)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'{label} must be an ISO 8601 timestamp, got {stamp!r}') from exc
    if not source_path.exists():
        raise FileNotFoundError(f'{source_path}: local CT.gov source path does not exist')
    files: list[Path]
    if source_path.is_dir():
        files = sorted(
            p for p in source_path.iterdir()
            if p.is_file() and (p.name.endswith('.json') or p.name.endswith('.json.gz')
                                or p.name.endswith('.jsonl') or p.name.endswith('.jsonl.gz'))
        )
    else:
        files = [source_path]
    if not files:
        raise ValueError(f'{source_path}: contains no JSON or JSONL files')

    wanted = set(nct_ids) if nct_ids is not None else None
    studies: dict[str, dict[str, Any]] = {}
    for file_path in files:
        for st in _iter_studies_from_local_file(file_path):
            if not ctgov_study_valid(st):
                continue
            nct_id = st['protocolSection']['identificationModule']['nctId']
            if wanted is not None and nct_id not in wanted:
                continue
            if nct_id not in studies:
                studies[nct_id] = st
                if limit is not None and wanted is None and len(studies) >= limit:
                    break
        if limit is not None and wanted is None and len(studies) >= limit:
            break

    requested = sorted(wanted) if wanted is not None else sorted(studies)
    if not requested:
        raise ValueError(f'{source_path}: yielded zero valid CT.gov v2 study records')
    # Absence from local files says nothing about ClinicalTrials.gov, so it is refused rather than
    # logged as an HTTP 404 (which coverage would report as the registry answering "no record").
    absent = [i for i in requested if i not in studies]
    if absent:
        raise ValueError(
            f'{source_path}: {len(absent)} requested NCT ID(s) are not in the local CT.gov records '
            f'({absent[:5]}); supply their records, drop them from the NCT list, or acquire them from CT.gov'
        )

    log = ResponseLog(raw_log_path(cache_dir, 'ctgov'))
    ver_body = {'apiVersion': '2.0.local', 'dataTimestamp': data_timestamp}
    ver_bytes = canonical_json_bytes(ver_body)
    day = fetched_at[:10]
    log.put({
        'key': f'ctgov/version/{day}',
        'url': f'{CTGOV_API}/version',
        'params': {},
        'tag': {'day': day, 'origin': 'local_records'},
        'fetched_at': fetched_at,
        'status': 200,
        'http_status': 200,
        'attempts': 0,
        'response_sha256': sha256_bytes(ver_bytes),
        'body': ver_body,
        'error': None,
    })
    for nct_id in requested:
        st = studies[nct_id]
        st_bytes = canonical_json_bytes(st)
        log.put({
            'key': f'ctgov/study/{nct_id}',
            'url': f'{CTGOV_API}/studies/{nct_id}',
            'params': {},
            'tag': {'nct_id': nct_id, 'origin': 'local_records'},
            'fetched_at': fetched_at,
            'status': 200,
            'http_status': 200,
            'attempts': 0,
            'response_sha256': sha256_bytes(st_bytes),
            'body': st,
            'error': None,
        })
    return freeze_cache(cache_dir, requested, frozen_at=frozen_at)


# ---------------------------------------------------------------- freeze & strict verification

def encode_archive_records(records: Iterable[dict[str, Any]]) -> bytes:
    """Canonical uncompressed JSONL archive bytes sorted by unique `key`."""
    ordered = sorted(records, key=lambda r: str(r['key']))
    if len({r['key'] for r in ordered}) != len(ordered):
        raise ValueError('duplicate keys in archive records')
    return b''.join(canonical_json_bytes(rec) for rec in ordered)


def freeze_source(log: ResponseLog, out_archive: Path, source: str, frozen_at: str) -> dict[str, Any]:
    """Write `<source>.jsonl.gz` (mtime=0) and `<source>.jsonl.gz.meta.json` atomically."""
    raw = encode_archive_records(log.records.values())
    compressed = gzip.compress(raw, mtime=0)
    times = sorted(str(r['fetched_at']) for r in log.records.values() if r.get('fetched_at'))
    meta: dict[str, Any] = {
        'source': source,
        'description': SOURCES[source],
        'archive': out_archive.name,
        'n_records': len(log.records),
        'sha256_uncompressed': sha256_bytes(raw),
        'sha256_compressed': sha256_bytes(compressed),
        'first_fetched_at': times[0] if times else None,
        'last_fetched_at': times[-1] if times else None,
        'frozen_at': frozen_at,
    }
    write_atomic(out_archive, compressed)
    meta_bytes = (json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True) + '\n').encode('utf-8')
    write_atomic(out_archive.with_name(out_archive.name + '.meta.json'), meta_bytes)
    return meta


def ctgov_ids_without_study_record(
    nct_ids: Iterable[str],
    ctgov_records: dict[str, dict[str, Any]],
) -> list[str]:
    """Requested NCT IDs with no `ctgov/study/{id}` record in `ctgov_records`, sorted.

    Any logged lookup counts, whatever its status: a 404 is `document_unavailable` and a transport
    failure (status None) is `search_incomplete_or_blocked`. An ID with no record was never looked
    up, so a cache whose manifest lists it describes a frame its CT.gov log does not cover (a raw
    log acquired for another NCT list). The build would emit such a trial as `not_searched`, so the
    cache is refused instead.
    """
    return sorted(i for i in set(nct_ids) if f'ctgov/study/{i}' not in ctgov_records)


def freeze_cache(
    cache_dir: Path,
    requested_nct_ids: Iterable[str],
    *,
    frozen_at: str | None = None,
) -> dict[str, Any]:
    """Freeze all acquired source logs under `cache_dir` and write `manifest.json` sidecar.

    Refuses when any requested NCT ID has no CT.gov study response record in raw/ctgov.jsonl, or
    when a drug-query record the build will read in raw/openfda.jsonl was made with an earlier
    query form.
    """
    ids = sorted(set(requested_nct_ids))
    if not ids or any(not NCT_RE.match(i) for i in ids):
        raise ValueError('freeze_cache requires at least one valid NCT ID')
    ctgov_raw = raw_log_path(cache_dir, 'ctgov')
    if not ctgov_raw.exists():
        raise CacheIntegrityError(f'{cache_dir}: cannot freeze cache without raw/ctgov.jsonl')
    ctgov_log = ResponseLog(ctgov_raw)
    unlooked = ctgov_ids_without_study_record(ids, ctgov_log.records)
    if unlooked:
        raise CacheIntegrityError(
            f'{cache_dir}: {len(unlooked)} requested NCT ID(s) have no CT.gov study response in '
            f'raw/ctgov.jsonl ({unlooked[:5]}); run the ctgov stage for them before freezing'
        )
    openfda_raw = raw_log_path(cache_dir, 'openfda')
    openfda_log = ResponseLog(openfda_raw) if openfda_raw.exists() else None
    stale = stale_openfda_drug_queries(openfda_log.records, ctgov_log.records, ids) if openfda_log is not None else []
    if stale:
        raise CacheIntegrityError(
            f'{cache_dir}: {len(stale)} openFDA drug-query record(s) in raw/openfda.jsonl were made with '
            f'an earlier query form ({stale[:3]}); run the openfda stage again before freezing'
        )

    stamp = frozen_at or now_utc()
    archives_meta: dict[str, dict[str, Any]] = {}
    for source in SOURCES:
        rpath = raw_log_path(cache_dir, source)
        if rpath.exists():
            preloaded = {'ctgov': ctgov_log, 'openfda': openfda_log}.get(source)
            log = preloaded if preloaded is not None else ResponseLog(rpath)
            meta = freeze_source(log, archive_path(cache_dir, source), source, stamp)
            archives_meta[source] = meta

    manifest: dict[str, Any] = {
        'schema_version': CACHE_SCHEMA_VERSION,
        'frozen_at': stamp,
        'max_requests_per_second': MAX_REQUESTS_PER_SECOND,
        'min_interval_seconds': MIN_INTERVAL_SECONDS,
        'requested_nct_ids': ids,
        'n_requested_nct_ids': len(ids),
        'requested_nct_ids_sha256': sha256_bytes('\n'.join(ids).encode('utf-8')),
        'archives': archives_meta,
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + '\n').encode('utf-8')
    write_atomic(cache_dir / 'manifest.json', manifest_bytes)
    return manifest


def _read_verified_archive(
    path: Path,
    expected_manifest_entry: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Verify `.jsonl.gz` against both its `.meta.json` sidecar and `manifest.json` entry."""
    if not path.is_file():
        raise CacheIntegrityError(f'{path.name}: listed in manifest.json but missing from cache')
    meta_path = path.with_name(path.name + '.meta.json')
    if not meta_path.is_file():
        raise CacheIntegrityError(f'{path.name}: missing .meta.json sidecar')
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise CacheIntegrityError(f'{meta_path.name}: unreadable JSON sidecar ({exc})') from exc

    for key in ('archive', 'n_records', 'sha256_compressed', 'sha256_uncompressed', 'source'):
        if meta.get(key) != expected_manifest_entry.get(key):
            raise CacheIntegrityError(
                f'{path.name}: manifest.json and {meta_path.name} disagree on {key!r}'
            )

    compressed = path.read_bytes()
    if sha256_bytes(compressed) != meta.get('sha256_compressed'):
        raise CacheIntegrityError(f'{path.name}: compressed sha256 mismatch against manifest/meta')
    try:
        raw = gzip.decompress(compressed)
    except (OSError, EOFError) as exc:
        raise CacheIntegrityError(f'{path.name}: corrupt gzip stream ({exc})') from exc
    if sha256_bytes(raw) != meta.get('sha256_uncompressed'):
        raise CacheIntegrityError(f'{path.name}: uncompressed sha256 mismatch against manifest/meta')

    records: dict[str, dict[str, Any]] = {}
    lines = [ln.rstrip(b'\r') for ln in raw.split(b'\n') if ln.strip()]
    for line in lines:
        try:
            rec = json.loads(line.decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CacheIntegrityError(f'{path.name}: malformed JSONL record ({exc})') from exc
        if not isinstance(rec, dict) or not isinstance(rec.get('key'), str):
            raise CacheIntegrityError(f'{path.name}: record lacks string key')
        if rec['key'] in records:
            raise CacheIntegrityError(f"{path.name}: duplicate key {rec['key']!r}")
        records[rec['key']] = rec

    if len(records) != meta.get('n_records'):
        raise CacheIntegrityError(
            f"{path.name}: decoded {len(records)} record(s), expected {meta.get('n_records')}"
        )
    return records, meta


def load_frozen_cache(cache_dir: Path) -> FrozenCache:
    """Strictly verify and load a frozen disclosure cache for offline build execution.

    Besides the archive hashes, every NCT ID the manifest requests must have a CT.gov study
    response record (`ctgov_ids_without_study_record`), and every openFDA drug-query record the
    build will read must have been made with today's query form (`stale_openfda_drug_queries`).
    """
    if not cache_dir.is_dir():
        raise CacheIntegrityError(f'{cache_dir}: cache directory does not exist')

    part_files = sorted(str(p.relative_to(cache_dir)) for p in cache_dir.rglob('*.part'))
    if part_files:
        raise CacheIntegrityError(
            f'{cache_dir}: partial `.part` file(s) present ({part_files[:3]}); cache is incomplete'
        )

    manifest_path = cache_dir / 'manifest.json'
    if not manifest_path.is_file():
        raise CacheIntegrityError(f'{cache_dir}: missing manifest.json sidecar (run freeze first)')
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise CacheIntegrityError(f'{manifest_path}: unreadable manifest JSON ({exc})') from exc
    if not isinstance(manifest, dict) or manifest.get('schema_version') != CACHE_SCHEMA_VERSION:
        raise CacheIntegrityError(f'{manifest_path}: unsupported schema_version')

    ids = manifest.get('requested_nct_ids')
    if (
        not isinstance(ids, list)
        or not ids
        or not all(isinstance(i, str) and NCT_RE.match(i) for i in ids)
        or len(set(ids)) != len(ids)
        or ids != sorted(ids)
    ):
        raise CacheIntegrityError('manifest.json: requested_nct_ids must be a non-empty sorted list of distinct NCT IDs')
    if manifest.get('n_requested_nct_ids') != len(ids):
        raise CacheIntegrityError('manifest.json: n_requested_nct_ids does not match requested_nct_ids')
    if sha256_bytes('\n'.join(ids).encode('utf-8')) != manifest.get('requested_nct_ids_sha256'):
        raise CacheIntegrityError('manifest.json: requested_nct_ids_sha256 mismatch')

    archives = manifest.get('archives')
    if not isinstance(archives, dict) or 'ctgov' not in archives:
        raise CacheIntegrityError('manifest.json: must list at least the frozen `ctgov` archive')
    unknown_sources = set(archives) - set(SOURCES)
    if unknown_sources:
        raise CacheIntegrityError(f'manifest.json: unknown archive source(s): {sorted(unknown_sources)}')

    on_disk_archives = {p.name for p in cache_dir.glob('*.jsonl.gz')}
    expected_archive_names = {f'{src}.jsonl.gz' for src in archives}
    unlisted = sorted(on_disk_archives - expected_archive_names)
    if unlisted:
        raise CacheIntegrityError(f'{cache_dir}: unlisted archive(s) on disk not in manifest.json: {unlisted}')

    for source in SOURCES:
        rpath = raw_log_path(cache_dir, source)
        if rpath.exists():
            if source not in archives:
                raise CacheIntegrityError(
                    f'{cache_dir}: raw/{source}.jsonl exists but {source} was not frozen in manifest.json'
                )
            raw_bytes = rpath.read_bytes()
            if raw_bytes and not raw_bytes.endswith(b'\n'):
                raise CacheIntegrityError(f'raw/{source}.jsonl has an interrupted trailing line; re-freeze required')
            unfrozen_digest = sha256_bytes(encode_archive_records(ResponseLog(rpath).records.values()))
            if unfrozen_digest != archives[source].get('sha256_uncompressed'):
                raise CacheIntegrityError(
                    f'raw/{source}.jsonl differs from frozen {source}.jsonl.gz; run freeze before building'
                )

    loaded: dict[str, tuple[dict[str, dict[str, Any]], dict[str, Any] | None]] = {
        src: ({}, None) for src in SOURCES
    }
    for source, entry in sorted(archives.items()):
        if not isinstance(entry, dict):
            raise CacheIntegrityError(f'manifest.json: invalid archive entry for {source!r}')
        loaded[source] = _read_verified_archive(archive_path(cache_dir, source), entry)

    assert loaded['ctgov'][1] is not None
    unlooked = ctgov_ids_without_study_record(ids, loaded['ctgov'][0])
    if unlooked:
        raise CacheIntegrityError(
            f'{cache_dir}: {len(unlooked)} requested NCT ID(s) in manifest.json have no CT.gov study '
            f'response in ctgov.jsonl.gz ({unlooked[:5]}); the cache does not cover its own frame'
        )
    stale = stale_openfda_drug_queries(loaded['openfda'][0], loaded['ctgov'][0], ids)
    if stale:
        raise CacheIntegrityError(
            f'{cache_dir}: {len(stale)} openFDA drug-query record(s) in openfda.jsonl.gz were made with an '
            f'earlier query form ({stale[:3]}); run `acquire --stage openfda --stage freeze` to refresh them'
        )
    return FrozenCache(
        cache_dir=cache_dir,
        manifest=manifest,
        requested_nct_ids=ids,
        ctgov=loaded['ctgov'][0],
        ctgov_meta=loaded['ctgov'][1],
        literature=loaded['literature'][0],
        literature_meta=loaded['literature'][1],
        openfda=loaded['openfda'][0],
        openfda_meta=loaded['openfda'][1],
    )
