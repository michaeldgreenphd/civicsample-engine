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
import math
import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal
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

# Version 2: freeze_cache checks that CT.gov version observations bracket the study answers
# (ctgov_version_bracket_problem). The check needs the raw log's line order, which the frozen archive
# (sorted by key) does not keep, so it cannot be re-run on load. A version-1 cache was frozen without
# it and is refused; its raw logs are still in the cache, so `acquire --nct-list FILE --stage ctgov
# --stage freeze`, with FILE listing the old manifest's requested_nct_ids, adds the missing
# observation and freezes it again.
CACHE_SCHEMA_VERSION = 2
SOURCES: dict[str, str] = {
    'ctgov': 'ClinicalTrials.gov API v2 (/api/v2/version and /api/v2/studies/{nct_id})',
    'literature': 'Europe PMC REST search and NCBI E-utilities PubMed esummary',
    'openfda': 'openFDA Drugs@FDA (/drug/drugsfda.json)',
}
STAGES = ('ctgov', 'literature', 'openfda', 'freeze')

# ASCII digits and \Z: \d also matches other scripts' digits, and $ allows a trailing newline.
NCT_RE = re.compile(r'^NCT[0-9]{8}\Z')
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


class CTGovVersionUnavailable(RuntimeError):
    """Raised when a CT.gov `/version` observation bracketing an acquisition run fails."""


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


def timestamp_problem(value: Any, label: str, *, offset: Literal['utc', 'none']) -> str | None:
    """Why `value` is not a whole ISO 8601 date and time in the stated form, or None when it is.

    `datetime.fromisoformat` alone accepts a bare date (`2026-11-01`), which names no instant, so a
    date and an hour:minute time are required and the whole value must parse. `offset='utc'` also
    requires a zero UTC offset (`Z` or `+00:00`): acquisition and generation times are stamped in UTC.
    The offset is checked as text, not only as a number: RFC 3339 uses `-00:00` to say the local
    offset is unknown, so it is refused although `fromisoformat` reads it as zero.
    `offset='none'` requires no offset at all, the form of CT.gov's own `dataTimestamp`
    (`2026-11-13T10:00:06`). Data timestamps are compared as strings and cut to their first ten
    characters for `as_of`, so a zoned value (`2026-11-01T23:30:00-05:00`, which is 2 November in
    UTC) would give the wrong day and break the ordering; it is refused rather than converted,
    because CT.gov never states one.
    """
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}.*', value):
        return f'{label} must be a whole ISO 8601 date and time, got {value!r}'
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return f'{label} must be a whole ISO 8601 date and time, got {value!r}'
    if offset == 'utc' and (
        parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
        or not re.search(r'(?:Z|\+00:00)\Z', value)
    ):
        return f'{label} must carry a UTC offset (Z or +00:00), got {value!r}'
    if offset == 'none' and parsed.tzinfo is not None:
        return f"{label} must carry no UTC offset, as CT.gov's dataTimestamp does not, got {value!r}"
    return None


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
        # Finite as well as bounded: NaN compares false with everything, so `nan < floor` passed and
        # every later `remaining > 0` was false, sending requests with no wait at all; infinity
        # would wait forever.
        if (
            isinstance(min_interval, bool)
            or not isinstance(min_interval, (int, float))
            or not math.isfinite(min_interval)
            or min_interval < MIN_FLOOR_SECONDS
        ):
            raise ValueError(
                f'min_interval ({min_interval}) must be a finite number >= {MIN_FLOOR_SECONDS}s '
                '(<= 2 requests/second)'
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


def ctgov_study_answers(body: Any, nct_id: str) -> bool:
    """Whether `body` is a valid CT.gov study record for `nct_id` itself.

    `/studies/{nct_id}` can answer with a structurally valid record whose own `nctId` differs (for
    example an obsolete ID answered under the trial's current one). Its titles, results and references
    would be published under the requested ID, so such an answer is not settled at acquisition and a
    frozen cache holding one is refused (`ctgov_studies_answered_for_another_trial`).
    """
    return ctgov_study_valid(body) and body['protocolSection']['identificationModule']['nctId'] == nct_id


def ctgov_studies_answered_for_another_trial(ctgov_records: dict[str, dict[str, Any]]) -> list[str]:
    """`ctgov/study/{id}` keys whose HTTP 200 record is a study for a different NCT ID, sorted."""
    out: list[str] = []
    for key, rec in ctgov_records.items():
        if not key.startswith('ctgov/study/') or not isinstance(rec, dict) or rec.get('status') != 200:
            continue
        wanted = key[len('ctgov/study/'):]
        if ctgov_study_valid(rec.get('body')) and not ctgov_study_answers(rec.get('body'), wanted):
            out.append(key)
    return sorted(out)


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
# Units: mass (`mcg`, `ug`, `ng`, `mg`, `g`, and `g` after a micro sign U+00B5 or Greek mu U+03BC),
# volume (`ml`), activity (`iu`, `unit`/`units`, `u`) and amount (`mEq`, `mmol`, `nmol`, `pmol`); a body
# surface area denominator may be written `m2` or with a superscript two (`m²`). Each alternative must end at a
# non-word character (the closing lookahead `(?=\W|$)`, which unlike `\b` also holds after `%`
# before a space), so a unit is stripped only as a whole token right after a number: `mg/mL` goes
# whole rather than as `mg` leaving `ml`, and `D3` in `Vitamin D3`, the `U` of `Insulin U-100` or a
# name such as `Unituxin` are never touched. Dosage-form and route words go next (see below).
DRUG_STRENGTH_RE = re.compile(
    r'\b\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)*\s*'
    r'(?:%|(?:units?|mmol|nmol|pmol|meq|mcg|[\u00b5\u03bc]g|ug|ng|mg|ml|iu|g|u)'
    r'(?:\s*/\s*(?:dose|day|hour|min|hr|h|mcg|[\u00b5\u03bc]g|kg|m2|m\u00b2|mg|ml|dl|ug|g|l))*)'
    r'(?=\W|$)',
    re.IGNORECASE,
)


# Dosage-form, route and release words removed as whole tokens. The openFDA query matches the term as a
# quoted phrase in name fields only (`OPENFDA_DRUG_FIELDS`). Drugs@FDA records the dosage form and
# route separately (`products.dosage_form`, `products.route`), so `"metformin tablet"` or
# `"pembrolizumab iv infusion"` misses an application whose names are METFORMIN HYDROCHLORIDE or
# KEYTRUDA / PEMBROLIZUMAB. When the removed words sit at either end of the label, the shorter phrase
# matches every field value the longer one did, so the candidate set can only widen. A form word in the
# middle (`eribulin injection halaven` -> `eribulin halaven`; 8 of the 63 labels that change among the
# stored CT.gov records) gives a different phrase. The list is closed and limited to words that name a
# form, route or release profile, not an ingredient; a label made only of such words (`Tablets`, one of
# the stored labels) names no ingredient and gives no query term.
DOSAGE_FORM_WORDS: frozenset[str] = frozenset({
    # forms
    'tablet', 'tablets', 'capsule', 'capsules', 'caplet', 'caplets', 'pill', 'pills', 'cream', 'ointment',
    'gel', 'lotion', 'foam', 'paste', 'patch', 'patches', 'film', 'solution', 'suspension', 'emulsion',
    'syrup', 'elixir', 'drop', 'drops', 'spray', 'inhaler', 'inhalation', 'aerosol', 'powder', 'granules',
    'sachet', 'injection', 'injections', 'injectable', 'infusion', 'suppository', 'suppositories', 'implant',
    'depot', 'vial', 'prefilled', 'pre-filled', 'syringe', 'autoinjector', 'auto-injector', 'pen',
    'concentrate', 'lyophilized',
    # routes
    'intravenous', 'intravenously', 'subcutaneous', 'subcutaneously', 'intramuscular', 'oral', 'orally',
    'topical', 'transdermal', 'ophthalmic', 'nasal', 'intranasal',
    'vaginal', 'rectal', 'sublingual', 'buccal', 'intravitreal', 'intrathecal', 'inhaled',
    # release profile and coating
    'extended-release', 'delayed-release', 'sustained-release', 'controlled-release', 'modified-release',
    'immediate-release', 'extended', 'delayed', 'sustained', 'controlled', 'modified', 'immediate',
    'release', 'orally-disintegrating', 'chewable',
    'dispersible', 'effervescent', 'coated', 'film-coated', 'enteric', 'enteric-coated',
})
# Route and release abbreviations are also compound-code prefixes (`SR 96669`, `CR 845`), so one is
# removed only when the next token does not start with a digit: `Glucophage XR` and `Daratumumab SC`
# lose it, `SR 96669` keeps it.
DOSAGE_FORM_ABBREVIATIONS: frozenset[str] = frozenset({
    'iv', 'sc', 'sq', 'im', 'er', 'xr', 'sr', 'xl', 'cr', 'ir', 'dr', 'odt',
})


# Dosing schedules and dose levels written into a label (`Tiotropium 18µg once daily`, `Metformin 500 mg
# bid`, `Exenatide Once Weekly`, `Empagliflozin high dose`, `PF-07321332 Dose 3`, `Low-dose cytarabine`)
# are not part of any name Drugs@FDA indexes, so the quoted phrase `tiotropium once daily` matches no
# application and the route would report a completed search with no candidate. They are removed only
# where they stand at either end of the normalized label, repeatedly, so the phrase left is a part of
# the original at its boundary and can only widen what it matched (`Pronamel Daily Protection` keeps its
# `daily`). Each alternative is closed: a schedule word or abbreviation, a `once/twice ... a day` form,
# `every N weeks`, `for N days`, or a dose-level qualifier immediately followed by `dose`. Abbreviations
# (`bid`, `qd`, `q3w`) are removed at the end only, where no compound code can follow them. Dose levels
# in the middle of a label (`of the mid dose vax-31`) and design text (`as formulation a`) are kept.
_SCHEDULE_PHRASES = (
    r'(?:once|twice|thrice|\d+\s+times?)\s+(?:a|per|each|every)\s+(?:day|week|month|night)'
    r'|(?:once|twice|thrice)\s+(?:daily|weekly|monthly|nightly)'
    r'|(?:every|each)\s+(?:other\s+)?(?:\d+\s+)?(?:days?|weeks?|months?|hours?)'
    r'|for\s+\d+\s+(?:days?|weeks?|months?)'
    r'|daily|weekly|biweekly|monthly|nightly'
)
_SCHEDULE_ABBREVIATIONS = r'bid|tid|qid|qd|qod|qam|qpm|qhs|qw|q\d+(?:h|hr|hrs|d|w|wk|wks)'
_DOSE_LEVEL_WORDS = (
    r'high|higher|low|lower|mid|medium|middle|intermediate|standard|normal|single|multiple|fixed|loading'
    r'|maintenance|induction|therapeutic|supra-therapeutic'
)
DRUG_TERM_TRAILING_QUALIFIER_RE = re.compile(
    rf'(?:^|\s)(?:{_SCHEDULE_PHRASES}|{_SCHEDULE_ABBREVIATIONS}'
    rf'|(?:(?:{_DOSE_LEVEL_WORDS})[- ])?doses?(?:\s+level)?(?:\s+(?:\d+|[a-e]))?)$'
)
DRUG_TERM_LEADING_QUALIFIER_RE = re.compile(
    rf'^(?:{_SCHEDULE_PHRASES}|(?:{_DOSE_LEVEL_WORDS})[- ]doses?(?:\s+of)?)(?:\s+|$)'
)


def _strip_end_qualifiers(term: str) -> str:
    """Remove schedule and dose-level qualifiers standing at either end of a normalized term (see above).

    A separator hyphen left at an end (`ir-cpi - dose 1` -> `ir-cpi`) goes too; hyphens inside a word
    (`ir-cpi`, `pf-07321332`) are kept.
    """
    while True:
        shorter = DRUG_TERM_LEADING_QUALIFIER_RE.sub('', DRUG_TERM_TRAILING_QUALIFIER_RE.sub('', term))
        shorter = re.sub(r'^(?:-\s+)+|(?:\s+-)+$', '', shorter.strip()).strip()
        if shorter == term:
            return term
        term = shorter


def normalize_drug_term(raw: str) -> str | None:
    """Normalize an intervention drug label for candidate openFDA lookup; return None for placebos.

    Parenthesised text, strengths (`DRUG_STRENGTH_RE`) and punctuation are removed, then the
    dosage-form, route and release words in `DOSAGE_FORM_WORDS` and, unless a number follows them,
    the abbreviations in `DOSAGE_FORM_ABBREVIATIONS` (see the notes there), and last any schedule or
    dose-level qualifier at either end (`_strip_end_qualifiers`).
    """
    cleaned = re.sub(r'\([^)]*\)', ' ', raw or '')
    cleaned = DRUG_STRENGTH_RE.sub(' ', cleaned)
    cleaned = re.sub(r'[^A-Za-z0-9\- ]+', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip().lower()
    tokens = [t for t in cleaned.split(' ') if t]
    cleaned = ' '.join(
        t for i, t in enumerate(tokens)
        if t not in DOSAGE_FORM_WORDS
        and not (t in DOSAGE_FORM_ABBREVIATIONS and not (tokens[i + 1:i + 2] or [''])[0][:1].isdigit())
    )
    cleaned = _strip_end_qualifiers(cleaned)
    if len(cleaned) < 3 or cleaned in PLACEBO_TERMS:
        return None
    if any(cleaned.startswith(prefix) for prefix in ('placebo ', 'sham ', 'matching placebo')):
        return None
    return cleaned


# Separators between the components of a combination label (`Nivolumab + Ipilimumab`,
# `Nivolumab/Relatlimab`, `Fluticasone Propionate and Salmeterol`, `Cisplatin or Carboplatin`).
# Drugs@FDA may index each component as its own `products.active_ingredients.name`, or under its own
# application, so the combined phrase alone can miss them. A component with no letter left (`000`
# from `Ro 09-1978/000`) names no drug and gives no term. Labels are split only after parenthesised
# text and strengths are removed, so a strength's `/` (`5 mg/mL`, `320/9.6 mcg`) never splits one. A
# comma is not a separator: it occurs inside chemical names (`2,4-...`).
DRUG_COMPONENT_SEPARATOR_RE = re.compile(r'\s*(?:\+|/|;|&|\bplus\b|\band\b|\bor\b|\bwith\b)\s*', re.IGNORECASE)


def drug_label_terms(raw: str) -> list[str]:
    """Query terms for one intervention label: the whole normalized label, plus each component of a
    combination label (see `DRUG_COMPONENT_SEPARATOR_RE`) that normalizes to a term of its own."""
    terms: list[str] = []
    whole = normalize_drug_term(raw)
    if whole:
        terms.append(whole)
    stripped = DRUG_STRENGTH_RE.sub(' ', re.sub(r'\([^)]*\)', ' ', raw or ''))
    parts = [p for p in DRUG_COMPONENT_SEPARATOR_RE.split(stripped) if p and p.strip()]
    if len(parts) > 1:
        for part in parts:
            term = normalize_drug_term(part)
            if term and any(c.isalpha() for c in term) and term not in terms:
                terms.append(term)
    return terms


def drug_planning_problem(study: dict[str, Any] | None) -> str | None:
    """Why a study's openFDA drug queries cannot be planned from its CT.gov record, or None when they can.

    `extract_drug_candidates` returns an empty list both for a trial that registers no drug
    intervention and for a record it could not read, so coverage asks this first. A missing record, or
    an interventions module, list or entry of the wrong type, means the planned queries are unknown. A
    record with no `armsInterventionsModule` (or no `interventions` key) at all is read as registering
    no interventions: 26 of the 3,371 distinct trials in the stored unprojected CT.gov records (all
    interventional) have no such module, so its absence is a real registry state and cannot be told
    from a field-projected record that dropped it.
    """
    if not isinstance(study, dict):
        return 'no CT.gov study record was retrieved'
    ps = study.get('protocolSection')
    if not isinstance(ps, dict):
        return 'protocolSection is not an object'
    if 'armsInterventionsModule' not in ps:
        return None
    arms = ps['armsInterventionsModule']
    if not isinstance(arms, dict):
        return 'armsInterventionsModule is not an object'
    if 'interventions' not in arms:
        return None
    interventions = arms['interventions']
    if not isinstance(interventions, list):
        return 'armsInterventionsModule.interventions is not a list'
    for i, item in enumerate(interventions):
        if not isinstance(item, dict):
            return f'interventions[{i}] is not an object'
        if not isinstance(item.get('type'), str) or not isinstance(item.get('name'), str):
            return f'interventions[{i}] lacks a string type and name'
        others = item.get('otherNames', [])
        if not isinstance(others, list) or not all(isinstance(o, str) for o in others):
            return f'interventions[{i}].otherNames is not a list of strings'
    return None


def extract_drug_candidates(study: dict[str, Any] | None) -> list[str]:
    """Extract sorted distinct normalized drug/biologic candidate terms from a CT.gov v2 study."""
    if not isinstance(study, dict):
        return []
    # Read type-safely: a malformed module or list plans nothing here, and coverage reports the route
    # incomplete through `drug_planning_problem` rather than as a search over zero candidates.
    ps = study.get('protocolSection')
    arms = ps.get('armsInterventionsModule') if isinstance(ps, dict) else None
    interventions = arms.get('interventions') if isinstance(arms, dict) else None
    if not isinstance(interventions, list):
        return []
    found: set[str] = set()
    for item in interventions:
        if not isinstance(item, dict):
            continue
        itype = str(item.get('type') or '').upper()
        if itype not in ('DRUG', 'BIOLOGICAL', 'COMBINATION_PRODUCT'):
            continue
        found.update(drug_label_terms(str(item.get('name') or '')))
        # Every registered alias is searched: the generic ingredient Drugs@FDA indexes may be any of
        # them (a label's first aliases are often brand or formulation names), and a capped list would
        # still let coverage report the planned drug search as complete.
        others = item.get('otherNames') or []
        if isinstance(others, list):
            for other in others:
                if isinstance(other, str):
                    found.update(drug_label_terms(other))
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
    run_stamp: str | None = None,
) -> dict[str, int]:
    """Fetch `/api/v2/studies/{nct_id}` for every requested NCT ID, observing `/api/v2/version`
    before the first study request and after the last.

    Each run records its own two version observations, keyed `ctgov/version/{run_stamp}/begin` and
    `.../end` (names that sort in time order when both share a `fetched_at` second), with
    `run_stamp` this run's start time in UTC to the microsecond unless a caller (a test) names one.
    Study answers settled by an earlier run are reused, but the version is never reused: a run that
    resumes after a CT.gov data refresh, on the same UTC day or later, or that spans one, records
    the new `dataTimestamp` beside the old, so the cache's range of data timestamps shows the mix
    and the build requires `--as-of` (see `artifacts._ctgov_version_info`). Earlier versions keyed
    one record per UTC day reused the morning's version for a resume later that day, attributing
    studies fetched after a refresh to the stale timestamp.
    """
    stamp = run_stamp or datetime.now(timezone.utc).isoformat(timespec='microseconds')

    def observe_version(phase: str) -> dict[str, Any] | None:
        return logged_json(
            log,
            get,
            throttle,
            f'ctgov/version/{stamp}/{phase}',
            f'{CTGOV_API}/version',
            None,
            tag={'run_stamp': stamp, 'phase': phase},
            valid=ctgov_version_valid,
        )

    def observe_version_or_raise(phase: str) -> None:
        rec = observe_version(phase)
        if rec and rec.get('status') == 200 and ctgov_version_valid(rec.get('body')):
            return
        status = rec.get('status') if rec else None
        if phase == 'begin':
            raise CTGovVersionUnavailable(
                f'the CT.gov /version request before the study requests failed (status {status!r}); '
                'no study was requested; run the ctgov stage again'
            )
        raise CTGovVersionUnavailable(
            f'the CT.gov /version request after the study requests failed (status {status!r}); the study '
            'answers are logged; run the ctgov stage again to observe the version after them before freezing'
        )

    # Without the observation before them no study is requested, so a failed begin leaves nothing
    # unbracketed. A failed end raises after the study answers are logged; the next run's
    # observations, all later than them, bound them instead (see ctgov_version_bracket_problem).
    observe_version_or_raise('begin')
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
            # The record must be the requested trial's own (`ctgov_study_answers`): an answer for
            # another NCT ID is logged as unusable and requested again on the next run.
            valid=lambda body, wanted=nct_id: ctgov_study_answers(body, wanted),
        )
        if rec is not None and rec.get('status') in FINAL_STATUSES:
            answered += 1
        else:
            unanswered += 1
    observe_version_or_raise('end')
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
    anything is written, never recorded as a CT.gov 404 that no request returned. A trial present
    more than once must have identical records; differing snapshots of one trial are refused, not
    resolved by file or line order.

    The code cannot know when local files were obtained, so both stamps are required and have no
    default (a fixed default would claim that date for every local snapshot):
      * `fetched_at`: when the records were retrieved from ClinicalTrials.gov, as stated by the
        person freezing them; stamped on each record and so the archive's first/last_fetched_at.
      * `data_timestamp`: the CT.gov `dataTimestamp` (data refresh) the records reflect; recorded
        in the local version record, reported as run.json `sources.ctgov.data_timestamp`, and the
        build's `as_of` when `--as-of` is not given.
    `frozen_at` is when the cache was frozen: the current time unless pinned (tests pin it).

    `cache_dir` must be new or empty. Ingesting into an existing cache would keep its earlier records
    beside the local ones: a later `ctgov/version/*` record would win the build's data timestamp (and
    so its default `as_of`), and earlier literature or openFDA logs would be frozen with records they
    were never acquired for. Live `acquire` keeps resuming an existing cache; only this path refuses.
    """
    # A limit of 0 or less used to freeze one record anyway (the cap was checked only after a record
    # was kept), so it must be a positive whole number when given.
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError(f'limit must be a positive whole number of records, got {limit!r}')
    # fetched_at is an acquisition instant, stamped in UTC like live acquisition's; data_timestamp
    # follows CT.gov's dataTimestamp, a whole date and time with no offset (see timestamp_problem).
    for problem in (
        timestamp_problem(fetched_at, 'fetched_at', offset='utc'),
        timestamp_problem(data_timestamp, 'data_timestamp', offset='none'),
    ):
        if problem:
            raise ValueError(problem)
    if cache_dir.exists() and (not cache_dir.is_dir() or any(cache_dir.iterdir())):
        raise CacheIntegrityError(
            f'{cache_dir}: a local freeze needs a new or empty cache directory, so no earlier CT.gov version, '
            'study, literature or openFDA record is frozen with the local records; use another --cache'
        )
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
    first_seen_in: dict[str, Path] = {}
    # A trial appearing twice is kept only when every copy is byte-identical in canonical JSON (the
    # same record shipped in two files). Two different snapshots of one trial would otherwise be
    # resolved by file or line order, and the one kept would be stamped with the caller's
    # `fetched_at`/`data_timestamp` whether or not it is the snapshot those describe.
    conflicting: dict[str, tuple[Path, Path]] = {}
    # `--limit N` (without an NCT list) selects the first N distinct trials in file and line order,
    # but every file is still read to the end: a differing copy of a selected trial may come after
    # the Nth new ID, and stopping there would freeze whichever snapshot sorted first. Trials beyond
    # the limit are only skipped, never compared or frozen.
    limit_reached = False
    for file_path in files:
        for st in _iter_studies_from_local_file(file_path):
            if not ctgov_study_valid(st):
                continue
            nct_id = st['protocolSection']['identificationModule']['nctId']
            if wanted is not None and nct_id not in wanted:
                continue
            if nct_id not in studies:
                if limit_reached:
                    continue
                studies[nct_id] = st
                first_seen_in[nct_id] = file_path
                limit_reached = limit is not None and wanted is None and len(studies) >= limit
            elif canonical_json_bytes(st) != canonical_json_bytes(studies[nct_id]):
                conflicting.setdefault(nct_id, (first_seen_in[nct_id], file_path))
    if conflicting:
        first_id = sorted(conflicting)[0]
        first_a, first_b = conflicting[first_id]
        raise ValueError(
            f'{source_path}: {len(conflicting)} NCT ID(s) appear more than once with different records '
            f'(first: {first_id} in {first_a.name} and {first_b.name}); keep one snapshot per trial, '
            'the one the stated --fetched-at and --data-timestamp describe'
        )

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


def ctgov_version_bracket_problem(ctgov_lines: list[dict[str, Any]]) -> str | None:
    """Why the CT.gov version observations in a log do not bracket its study answers, or None.

    `ctgov_lines` is raw/ctgov.jsonl in file order. The log is append-only, so its order is the order
    in which answers arrived, exact where `fetched_at` (to the second) is not; the latest line for a
    key is that key's answer. A frozen cache's data timestamps must cover every study answer it holds:
    some valid `/version` answer must come before the first final (200 or 404) study answer and some
    after the last. CT.gov's `dataTimestamp` only moves forward, so if those two agree no refresh
    fell between them, and if they differ the cache's range shows it (and the build requires
    `--as-of`). A failed version request is ignored, so a run whose observation after its study
    requests failed leaves its answers unbounded above until a later run observes the version.
    Study records from a local freeze (`tag.origin == 'local_records'`) are not acquisitions: their
    data timestamp is the one stated for them, so they need no bracket, also when the cache is
    frozen again after later stages.
    """
    last_line: dict[str, int] = {}
    for idx, rec in enumerate(ctgov_lines):
        if isinstance(rec, dict) and isinstance(rec.get('key'), str):
            last_line[rec['key']] = idx
    study_lines = [
        idx for key, idx in last_line.items()
        if key.startswith('ctgov/study/')
        and ctgov_lines[idx].get('status') in FINAL_STATUSES
        and (ctgov_lines[idx].get('tag') or {}).get('origin') != 'local_records'
    ]
    if not study_lines:
        return None
    version_lines = [
        idx for key, idx in last_line.items()
        if key.startswith('ctgov/version/')
        and ctgov_lines[idx].get('status') == 200
        and ctgov_version_valid(ctgov_lines[idx].get('body'))
    ]
    if not version_lines:
        return 'no successful CT.gov /version observation is logged; run the ctgov stage again'
    if min(version_lines) > min(study_lines):
        return (
            'no successful CT.gov /version observation was logged before the first study answer, so the '
            'data version it came from is unknown; re-acquire into a new cache'
        )
    if max(version_lines) < max(study_lines):
        return (
            'no successful CT.gov /version observation was logged after the last study answer; run the '
            'ctgov stage again to observe the version after it'
        )
    return None


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

    Refuses when any requested NCT ID has no CT.gov study response record in raw/ctgov.jsonl, when
    an HTTP 200 study response is a record for a different NCT ID, when the CT.gov version
    observations do not bracket the study answers (`ctgov_version_bracket_problem`), or when a
    drug-query record the build will read in raw/openfda.jsonl was made with an earlier query form.
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
    foreign = ctgov_studies_answered_for_another_trial(ctgov_log.records)
    if foreign:
        raise CacheIntegrityError(
            f'{cache_dir}: {len(foreign)} CT.gov study response(s) in raw/ctgov.jsonl are records for a '
            f'different NCT ID than the one requested ({foreign[:3]}); run the ctgov stage again before freezing'
        )
    bracket = ctgov_version_bracket_problem(read_jsonl(ctgov_raw))
    if bracket:
        raise CacheIntegrityError(f'{cache_dir}: {bracket}')
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
    response record (`ctgov_ids_without_study_record`), no HTTP 200 study response may be a record
    for a different NCT ID (`ctgov_studies_answered_for_another_trial`), and every openFDA drug-query
    record the build will read must have been made with today's query form (`stale_openfda_drug_queries`).
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
        found = manifest.get('schema_version') if isinstance(manifest, dict) else None
        hint = ''
        if found == 1:
            # `acquire` needs the NCT IDs, which only this (refused) manifest still lists.
            old_ids = manifest.get('requested_nct_ids')
            n_ids = len(old_ids) if isinstance(old_ids, list) else 'the'
            hint = (
                '; it was frozen before the CT.gov version-bracket check. Its raw logs are still in the '
                f"cache: write this manifest's {n_ids} requested_nct_ids to a file, one per line, and run "
                # The script is not executable (mode 0644), and the path is quoted for the shell.
                f'`python3 scripts/build_drug_disclosure.py acquire --cache {shlex.quote(str(cache_dir))} '
                '--nct-list FILE --stage ctgov --stage freeze` from the repository root to observe the CT.gov '
                'version again and freeze it again'
            )
        raise CacheIntegrityError(
            f'{manifest_path}: unsupported schema_version {found!r} (expected {CACHE_SCHEMA_VERSION}){hint}'
        )

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
    foreign = ctgov_studies_answered_for_another_trial(loaded['ctgov'][0])
    if foreign:
        raise CacheIntegrityError(
            f'{cache_dir}: {len(foreign)} CT.gov study response(s) in ctgov.jsonl.gz are records for a '
            f'different NCT ID than the one requested ({foreign[:3]}); run `acquire --stage ctgov --stage '
            'freeze` to request them again'
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
