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
from datetime import date, datetime, timedelta, timezone
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
    """Whether a CT.gov `/version` answer can be used: a non-blank `apiVersion` and a `dataTimestamp` in
    CT.gov's own form, a whole date and time with no offset (`timestamp_problem`, the rule a local
    freeze's `--data-timestamp` follows). Live answers are checked here, so a malformed or zoned one is
    logged as `UNUSABLE_200` and the run stops (`CTGovVersionUnavailable`) rather than being frozen as
    the data version: `not-a-timestamp` stopped the build later in `date.fromisoformat`, and
    `2026-11-01T23:30:00-05:00` was cut to the wrong `as_of` day. A line an earlier acquisition logged as
    successful with such a timestamp counts, here as everywhere this is called, as a failed observation:
    the freeze's bracket check needs other observations around the study answers, and run.json
    `sources.ctgov` leaves it out. Every stored CT.gov answer carries the form (`2026-11-13T10:00:06`).
    """
    return (
        isinstance(body, dict)
        and isinstance(body.get('apiVersion'), str)
        and bool(body['apiVersion'].strip())
        and timestamp_problem(body.get('dataTimestamp'), 'dataTimestamp', offset='none') is None
    )


def data_timestamp_after_retrieval(data_timestamp: str, fetched_at: str) -> bool:
    """Whether CT.gov `dataTimestamp` `data_timestamp` is later than records retrieved at `fetched_at`
    can reflect. Both must already pass `timestamp_problem` (no offset, and UTC, respectively).

    CT.gov states no time zone for dataTimestamp, so it is compared as if UTC with the largest real UTC
    offset (14 hours) allowed: no possible pair is refused, and a data timestamp more than that after
    retrieval (a later day's refresh, or `2100-01-01T00:00:00` answered in 2026) is.
    """
    fetched_utc = datetime.fromisoformat(fetched_at).astimezone(timezone.utc).replace(tzinfo=None)
    return datetime.fromisoformat(data_timestamp) - timedelta(hours=14) > fetched_utc


def ctgov_version_record_valid(rec: Any) -> bool:
    """Whether a logged CT.gov `/version` record is a usable observation of the data version: an HTTP
    200 answer that passes `ctgov_version_valid`, with a `dataTimestamp` no later than its own retrieval
    allows (`data_timestamp_after_retrieval`).

    A valid-looking `2100-01-01T00:00:00` answered in 2026 was accepted by the bracket check, frozen,
    and became the build's default `as_of`, so records were classified against a date in the future.
    Such a record counts, everywhere this is called, as a failed observation, like a malformed one. The
    bound needs the record's `fetched_at` as a UTC timestamp, which every record acquisition or a local
    freeze logs carries, and a frozen archive holding a record without one is refused at load
    (`_read_verified_archive`); a record without one is not bounded here, as for openFDA actions
    (`evidence._retrieved_day`).
    """
    if not isinstance(rec, dict) or rec.get('status') != 200 or not ctgov_version_valid(rec.get('body')):
        return False
    fetched_at = rec.get('fetched_at')
    if timestamp_problem(fetched_at, 'fetched_at', offset='utc') is not None:
        return True
    return not data_timestamp_after_retrieval(rec['body']['dataTimestamp'], fetched_at)


# Every part of a CT.gov v2 study record the build reads, with the type it must have when present
# (`None` counts as absent, as the readers' `or {}` treat it). A part of another type made a reader's
# `.get` raise and stopped an offline build (`statusModule: ["bad"]`), or was read as absent. The
# interventions and references modules are checked per route instead (`drug_planning_problem`,
# `registry_references_problem`), and a non-boolean `hasResults` is insufficient evidence by design.
_STR, _INT, _BOOL = (str,), (int,), (bool,)
CTGOV_STUDY_PARTS: tuple[tuple[tuple[str, ...], type, tuple[tuple[str, tuple[type, ...]], ...]], ...] = (
    (('protocolSection', 'identificationModule'), dict, (('briefTitle', _STR), ('officialTitle', _STR))),
    (('protocolSection', 'statusModule'), dict,
     (('overallStatus', _STR), ('resultsFirstSubmitDate', _STR), ('resultsFirstSubmitQcDate', _STR))),
    (('protocolSection', 'statusModule', 'primaryCompletionDateStruct'), dict, (('date', _STR), ('type', _STR))),
    (('protocolSection', 'statusModule', 'completionDateStruct'), dict, (('date', _STR), ('type', _STR))),
    (('protocolSection', 'statusModule', 'resultsFirstPostDateStruct'), dict, (('date', _STR), ('type', _STR))),
    (('protocolSection', 'designModule'), dict, (('studyType', _STR),)),
    (('protocolSection', 'designModule', 'enrollmentInfo'), dict, (('count', _INT), ('type', _STR))),
    (('protocolSection', 'designModule', 'phases'), list, ()),
    (('protocolSection', 'conditionsModule'), dict, ()),
    (('protocolSection', 'conditionsModule', 'conditions'), list, ()),
    (('protocolSection', 'sponsorCollaboratorsModule'), dict, ()),
    (('protocolSection', 'sponsorCollaboratorsModule', 'leadSponsor'), dict, (('name', _STR), ('class', _STR))),
    (('protocolSection', 'sponsorCollaboratorsModule', 'collaborators'), list, ()),
    (('protocolSection', 'oversightModule'), dict, (('isFdaRegulatedDrug', _BOOL),)),
    (('resultsSection',), dict, ()),
)
# List parts whose entries the build reads, with the entry type and the entry fields read.
CTGOV_STUDY_LIST_ENTRIES: dict[tuple[str, ...], tuple[type, tuple[tuple[str, tuple[type, ...]], ...]]] = {
    ('protocolSection', 'designModule', 'phases'): (str, ()),
    ('protocolSection', 'conditionsModule', 'conditions'): (str, ()),
    ('protocolSection', 'sponsorCollaboratorsModule', 'collaborators'): (dict, (('name', _STR), ('class', _STR))),
}


# Every CT.gov date the build reads, under `statusModule`: the milestones, the 365-day window and the
# CT.gov results link's date read each one through `ctgov_date_span`'s reader.
CTGOV_STUDY_DATES: tuple[tuple[str, ...], ...] = (
    ('primaryCompletionDateStruct', 'date'),
    ('completionDateStruct', 'date'),
    ('resultsFirstSubmitDate',),
    ('resultsFirstSubmitQcDate',),
    ('resultsFirstPostDateStruct', 'date'),
)


def ctgov_date_span(value: str) -> tuple[date, date] | None:
    """The first and last day that CT.gov date `value` can denote, read as the build reads it
    (`timeline.parse_partial_date`: `YYYY`, `YYYY-MM` or `YYYY-MM-DD` in the years 1800 to 2200, surrounding
    blanks stripped), or None when no date is read from it.

    The study check and the retrieval bound use this reader rather than a pattern of their own. The
    bound's pattern skipped `2100` and ` 2100-01-01`, which the build reads as days in 2100, so a
    posting date in 2100 passed in a record fetched in 2026 while `2100-01-01` was refused.
    """
    # Imported when called: `timeline` imports this module.
    from disclosure.timeline import parse_partial_date

    parsed = parse_partial_date(value)
    return None if parsed is None else (parsed.earliest, parsed.latest)


def _status_value(status_mod: dict[str, Any], path: tuple[str, ...]) -> Any:
    """The value at `path` under a study's `statusModule`, or None where any part of it is absent."""
    value: Any = status_mod
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _value_is(value: Any, types: tuple[type, ...]) -> bool:
    """`isinstance`, except that a bool is not an int (`True` is not an enrollment count)."""
    return isinstance(value, types) and not (isinstance(value, bool) and bool not in types)


def ctgov_study_problem(body: Any) -> str | None:
    """Why `body` is not a CT.gov v2 study record the build can read, or None when it is.

    It needs a `protocolSection` object with an `identificationModule` object whose `nctId` is an NCT
    ID, and every part in `CTGOV_STUDY_PARTS` (and every entry in `CTGOV_STUDY_LIST_ENTRIES`) must have
    its type when present. Every populated date in `CTGOV_STUDY_DATES` must be one the build reads
    (`ctgov_date_span`). `hasResults: false` with a non-empty `resultsSection`, or with a results
    posting date, is refused as contradictory. All 3,371 distinct trials and all 75,811 study entries
    in the stored local files pass.
    """
    if not isinstance(body, dict) or not isinstance(body.get('protocolSection'), dict):
        return 'no protocolSection object'
    ident = body['protocolSection'].get('identificationModule')
    if not isinstance(ident, dict):
        return 'no identificationModule object'
    nct = ident.get('nctId')
    if not (isinstance(nct, str) and NCT_RE.match(nct)):
        return f'identificationModule.nctId {nct!r} is not an NCT ID'
    for path, part_type, fields in CTGOV_STUDY_PARTS:
        value: Any = body
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if value is None:
            continue
        where = '.'.join(path)
        if not isinstance(value, part_type):
            return f'{where} is {type(value).__name__}, not {part_type.__name__}'
        for field, types in fields:
            if value.get(field) is not None and not _value_is(value[field], types):
                return f'{where}.{field} is {type(value[field]).__name__}, not {types[0].__name__}'
        if path in CTGOV_STUDY_LIST_ENTRIES:
            entry_type, entry_fields = CTGOV_STUDY_LIST_ENTRIES[path]
            for i, entry in enumerate(value):
                if not isinstance(entry, entry_type):
                    return f'{where}[{i}] is {type(entry).__name__}, not {entry_type.__name__}'
                for field, types in entry_fields:
                    if entry.get(field) is not None and not _value_is(entry[field], types):
                        return f'{where}[{i}].{field} is {type(entry[field]).__name__}, not {types[0].__name__}'
    # A populated date the build cannot read was read as no date. With `hasResults: false`, a
    # `resultsFirstSubmitDate` of `not-a-date` or `2025-99-99` became "no posted or submitted results"
    # and an `UNREPORTED_PAST_365D` window, a completed negative resting on unreadable evidence. Blank
    # text reads as absent, as for the posting date below. Every populated date in the 75,811 stored
    # study entries is `YYYY-MM` or `YYYY-MM-DD`.
    status_mod = body['protocolSection'].get('statusModule') or {}
    for date_path in CTGOV_STUDY_DATES:
        raw = _status_value(status_mod, date_path)
        if isinstance(raw, str) and raw.strip() and ctgov_date_span(raw) is None:
            return f"statusModule.{'.'.join(date_path)} {raw!r} is not a date (YYYY, YYYY-MM or YYYY-MM-DD, 1800-2200)"
    # Every module of a results section is an object (null reads as absent, as above): `{"participantFlowModule":
    # "broken"}` was read as posted results, so a verified CT.gov link and `verified_link` rested on a value
    # nothing could read. All 3,812 stored results sections hold only objects.
    results = body.get('resultsSection')
    if isinstance(results, dict):
        for key, module in results.items():
            if module is not None and not isinstance(module, dict):
                return f'resultsSection.{key} is {type(module).__name__}, not dict'
    # `hasResults: false` beside posted results contradicts itself: coverage read the flag as the
    # registry's negative while the record holds a results section, or a results posting date that the
    # 365-day window reads as posted (`POSTED_WITHIN_365D` beside an unreported CT.gov route). #106 and
    # #107 refuse the section case too. None of the stored records has either shape: the 207 distinct
    # trials with the flag false carry no section and no posting date.
    if body.get('hasResults') is False:
        if isinstance(body.get('resultsSection'), dict) and body['resultsSection']:
            return 'hasResults is false but resultsSection is non-empty'
        post_date = (status_mod.get('resultsFirstPostDateStruct') or {}).get('date')
        if isinstance(post_date, str) and post_date.strip():
            return 'hasResults is false but statusModule.resultsFirstPostDateStruct has a date'
    return None


def ctgov_study_valid(body: Any) -> bool:
    """Whether `body` is a study record the build can read (`ctgov_study_problem`)."""
    return ctgov_study_problem(body) is None


def ctgov_results_section_posted(body: dict[str, Any]) -> bool:
    """Whether a study record's `resultsSection` holds posted results: at least one non-empty module.

    A section whose modules are all empty or null carries nothing posted, so with `hasResults: true` it is
    insufficient evidence (`available_document_insufficient`, `RESULTS_STATUS_INDETERMINATE`), never a
    verified CT.gov results link. Every one of the 3,812 stored results sections holds a non-empty module.
    """
    results = body.get('resultsSection')
    return isinstance(results, dict) and any(isinstance(m, dict) and m for m in results.values())


# The results dates ClinicalTrials.gov records when it receives, quality-checks and posts a trial's
# results. Each names a day that had begun somewhere when the record was served.
CTGOV_RESULTS_DATES: tuple[tuple[str, ...], ...] = (
    ('resultsFirstSubmitDate',),
    ('resultsFirstSubmitQcDate',),
    ('resultsFirstPostDateStruct', 'date'),
)


# Completion dates the record states as having happened (`type: ACTUAL`). An ACTUAL date names a day that
# had begun when the record was served, as a results date does; an ESTIMATED or ANTICIPATED one may lie
# ahead, and an untyped one opens no elapsed-window state (`timeline.classify_window_365d`), so neither is
# bounded. The latest ACTUAL dates in the 75,811 stored study entries are 2026-04-10 (primary
# completion) and 2026-05-19 (completion).
CTGOV_ACTUAL_DATE_STRUCTS: tuple[str, ...] = ('primaryCompletionDateStruct', 'completionDateStruct')


def ctgov_results_date_after(body: dict[str, Any], fetched_at: str) -> str | None:
    """The first results date (`CTGOV_RESULTS_DATES`), ACTUAL completion date (`CTGOV_ACTUAL_DATE_STRUCTS`)
    or QC review cycle date in study `body` that starts after a record retrieved at `fetched_at` (a UTC
    timestamp) can hold, as `<path> (<date>)`, else None.

    `body` must pass `ctgov_study_problem`, which refuses a populated date the build cannot read. CT.gov
    states no time zone for these days, so the bound is the calendar day at `fetched_at` in the zone
    furthest ahead of UTC (+14 hours), as for `dataTimestamp` (`data_timestamp_after_retrieval`): no real
    record is refused. A date is after the bound when the first day it can denote is (`ctgov_date_span`,
    the build's own reader), so a month or year containing the bound is not after it.
    """
    bound = (datetime.fromisoformat(fetched_at).astimezone(timezone.utc) + timedelta(hours=14)).date()
    status_mod = body['protocolSection'].get('statusModule') or {}
    for path in CTGOV_RESULTS_DATES:
        value = _status_value(status_mod, path)
        span = ctgov_date_span(value) if isinstance(value, str) else None
        if span is not None and span[0] > bound:
            return f"statusModule.{'.'.join(path)} ({value.strip()})"
    # On 05a8005 an ACTUAL primary completion or completion in 2100, in a record fetched in 2026, was published
    # as a completion milestone and fed every completion-based interval.
    for struct_name in CTGOV_ACTUAL_DATE_STRUCTS:
        struct = status_mod.get(struct_name)
        value = struct.get('date') if isinstance(struct, dict) and struct.get('type') == 'ACTUAL' else None
        span = ctgov_date_span(value) if isinstance(value, str) else None
        if span is not None and span[0] > bound:
            return f"statusModule.{struct_name}.date ({value.strip()}, ACTUAL)"
    # A QC cycle's release, unrelease or reset is an event of the same results review, so it too names a day
    # that had begun when the record was served: on 71f99b9 a `releaseDate` of 2100-01-01 in a record
    # fetched in 2026 was published as a cycle. The timeline's own path and fields are read (imported when
    # called: `timeline` imports this module), and a malformed history is left to the timeline to report.
    # The latest stored cycle date is 2026-10-30.
    from disclosure.timeline import SUBMISSION_INFO_DATES, SUBMISSION_INFOS_PATH

    node: Any = body
    for key in SUBMISSION_INFOS_PATH:
        node = node.get(key) if isinstance(node, dict) else None
    for i, entry in enumerate(node if isinstance(node, list) else []):
        for field in SUBMISSION_INFO_DATES:
            value = entry.get(field) if isinstance(entry, dict) else None
            span = ctgov_date_span(value) if isinstance(value, str) else None
            if span is not None and span[0] > bound:
                return f"{'.'.join(SUBMISSION_INFOS_PATH)}[{i}].{field} ({value.strip()})"
    return None


def ctgov_study_record_problem(rec: Any) -> str | None:
    """Why a logged CT.gov study record is not one the build can read, or None when it is: an HTTP 200
    answer whose body passes `ctgov_study_problem` and holds no results date after its own retrieval
    (`ctgov_results_date_after`).

    A valid-looking `resultsFirstPostDateStruct.date` of `2100-01-01` in a record fetched in 2026 was
    published on the verified CT.gov results link and as `results_first_posted`, and with an earlier
    submission date the 365-day window read the results as `PENDING_SUBMITTED_UNPOSTED` while coverage
    reported them posted. A record contradicting its own retrieval is read as a failed lookup, like
    one contradicting itself (`hasResults: false` beside posted results). The bound needs the record's
    `fetched_at` as a UTC timestamp, which every record acquisition or a local freeze logs carries, and
    a frozen archive holding a record without one is refused at load (`_read_verified_archive`); a
    record without one is not bounded here, as for openFDA actions (`evidence._retrieved_day`).
    """
    if not isinstance(rec, dict) or rec.get('status') != 200:
        return 'not an HTTP 200 answer'
    problem = ctgov_study_problem(rec.get('body'))
    if problem is not None:
        return problem
    fetched_at = rec.get('fetched_at')
    if timestamp_problem(fetched_at, 'fetched_at', offset='utc') is not None:
        return None
    late = ctgov_results_date_after(rec['body'], fetched_at)
    return f'{late} is after the record was retrieved ({fetched_at})' if late else None


def ctgov_study_record_valid(rec: Any) -> bool:
    """Whether a logged CT.gov study record is one the build can read (`ctgov_study_record_problem`)."""
    return ctgov_study_record_problem(rec) is None


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


def _nonblank_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


# The Europe PMC identifier fields `evidence._epmc_target` reads, in its order of preference.
EPMC_IDENTIFIER_FIELDS = ('pmid', 'pmcid', 'doi', 'id')


def _epmc_hit_readable(hit: Any) -> bool:
    """Whether a Europe PMC result can be inspected: an object with an identifier (`id`, which the
    `resultType=core` search returns on every record, or a `pmid`, `pmcid` or `doi`) and a title, and
    every identifier field it carries (`EPMC_IDENTIFIER_FIELDS`, other than an explicit null) a string.

    A hit without them gave no record to look into: it became the placeholder target `epmc:<index>`, was
    set aside for carrying no NCT token, and the route read `no_candidate_in_completed_scope`, a
    completed negative that `unreported_across_completed_routes` builds on. The whole answer is now
    unusable instead, so the route stays incomplete (the conservative side if a real record lacks one).
    A mistyped identifier beside a readable one is refused too: `_epmc_target` turned a numeric `pmid`
    into text and preferred it, so an exact NCT token made the route a verified link to an identifier
    the source never wrote as one.
    """
    return (
        isinstance(hit, dict)
        and any(_nonblank_str(hit.get(k)) for k in EPMC_IDENTIFIER_FIELDS)
        and all(hit.get(k) is None or isinstance(hit[k], str) for k in EPMC_IDENTIFIER_FIELDS)
        and _nonblank_str(hit.get('title'))
    )


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
    if not isinstance(results, list) or not all(_epmc_hit_readable(r) for r in results):
        return False
    return hit_count >= len(results)


def pubmed_esummary_valid(body: Any, pmid: str) -> bool:
    """Whether an NCBI esummary answer settles the lookup of `pmid`.

    NCBI's JSON esummary gives each requested ID an entry under `result`, keyed by the ID and carrying
    it as `uid` (`{"uid": "99000001", "title": ...}`); an ID it cannot resolve gets an entry with an
    `error` (`{"uid": ..., "error": "cannot get document summary"}`). An entry with neither, such as
    `{}`, holds no evidence that the PMID resolved, yet it was settled and read as
    `identifier_confirmed`, so a RESULT reference verified its route. It is now logged as unusable and
    the lookup stays incomplete. An error entry is accepted with or without `uid` (its shape has not
    been checked against the live API): it can only read as `identifier_not_resolved`, never verify.
    """
    if not isinstance(body, dict):
        return False
    res = body.get('result')
    if not isinstance(res, dict):
        return False
    entry = res.get(pmid)
    if not isinstance(entry, dict):
        return False
    error = entry.get('error')
    return entry.get('uid') == pmid or (isinstance(error, str) and bool(error.strip()))


def openfda_drugsfda_valid(body: Any) -> bool:
    """openFDA answers 0 matches with HTTP 404; HTTP 200 must carry `results` and `meta.results.total`.

    Every Drugs@FDA result names its application (`application_number`, such as `NDA021436`). One
    without it became the placeholder `app:<index>`, and an NCT token in its `notes` published a verified
    FDA link with no identifiable application, so such an answer is unusable and the search incomplete.
    """
    if not isinstance(body, dict):
        return False
    results = body.get('results')
    if not isinstance(results, list) or not results or not all(isinstance(r, dict) for r in results):
        return False
    if not all(_nonblank_str(r.get('application_number')) for r in results):
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
# A dose range (`5-10 mg`, `5 to 10 mg`, `0.5–1.0 mg/kg`) is one strength: matched only up to its
# last number, it left `prednisone 5-` or `prednisone 5 to` in the query phrase. A range is taken only
# when a unit follows it, so codes such as `5-FU` and `Ro 09-1978/000` are kept.
# One amount: plain or comma-grouped digits (`50,000`), with an optional decimal part. Used for the
# leading amount, the end of a range and every slash-separated component (`24,000/76,000/120,000 units`).
# A hyphen may join the amount to its unit (`Prednisone 10-mg`, `0.5-mg tablet`): without it the term kept
# `10-mg` or `0 5-mg`, a phrase no Drugs@FDA name carries. The unit must still follow, so `5-FU` is kept.
_DRUG_AMOUNT = r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
DRUG_STRENGTH_RE = re.compile(
    rf'\b{_DRUG_AMOUNT}(?:\s*(?:-|\u2013|\u2014|to)\s*{_DRUG_AMOUNT})?(?:\s*/\s*{_DRUG_AMOUNT})*\s*-?\s*'
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
# The words of a phrase may be joined by hyphens (`Once-weekly semaglutide`, `Metformin twice-daily`,
# `Pembrolizumab every-3-weeks`): `normalize_drug_term` keeps hyphens, so the term had kept `once-weekly`,
# a phrase no Drugs@FDA name carries. A single schedule word still needs a space or the label's edge
# before it, so `drug-daily` and `Pronamel Daily-Protection` are kept.
_SCHEDULE_GAP = r'[\s-]+'
_SCHEDULE_PHRASES = (
    rf'(?:once|twice|thrice|\d+{_SCHEDULE_GAP}times?){_SCHEDULE_GAP}(?:a|per|each|every){_SCHEDULE_GAP}'
    r'(?:day|week|month|night)'
    rf'|(?:once|twice|thrice){_SCHEDULE_GAP}(?:daily|weekly|monthly|nightly)'
    rf'|(?:every|each){_SCHEDULE_GAP}(?:other{_SCHEDULE_GAP})?(?:\d+{_SCHEDULE_GAP})?(?:days?|weeks?|months?|hours?)'
    rf'|for{_SCHEDULE_GAP}\d+{_SCHEDULE_GAP}(?:days?|weeks?|months?)'
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
    cleaned = _clean_drug_label(raw)
    if len(cleaned) < 3 or _is_placebo_term(cleaned):
        return None
    return cleaned


def _clean_drug_label(raw: str) -> str:
    """`normalize_drug_term`'s cleaning of a label, before the placebo and length tests."""
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
    return _strip_end_qualifiers(cleaned)


def _is_placebo_term(cleaned: str) -> bool:
    """Whether a cleaned label names a placebo, sham or control arm rather than a drug."""
    return cleaned in PLACEBO_TERMS or any(
        cleaned.startswith(prefix) for prefix in ('placebo ', 'sham ', 'matching placebo')
    )


def is_placebo_label(raw: str) -> bool:
    """Whether an intervention label names a placebo, sham or control arm (`PLACEBO_TERMS`), as
    `normalize_drug_term` decides it."""
    return _is_placebo_term(_clean_drug_label(raw))


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


# The intervention types whose labels plan openFDA drug queries.
DRUG_INTERVENTION_TYPES: frozenset[str] = frozenset({'DRUG', 'BIOLOGICAL', 'COMBINATION_PRODUCT'})


def is_drug_intervention_type(raw: object) -> bool:
    """Whether an intervention `type` plans openFDA drug queries. `extract_drug_candidates` and
    `drug_planning_problem` both read the type here: when they read it differently (` DRUG ` padded),
    the check took a drug as planned that extraction skipped. All 132,953 intervention entries in the
    75,811 stored local CT.gov entries carry an exact upper-case enum value."""
    return isinstance(raw, str) and raw.strip().upper() in DRUG_INTERVENTION_TYPES


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
        # A blank type could be a drug's, and a blank name gives `extract_drug_candidates` no term, so
        # either left the drug search unplanned while the route completed over the NCT query alone. No
        # intervention in the stored records has a blank string type or name.
        if not item['type'].strip() or not item['name'].strip():
            return f'interventions[{i}] has a blank type or name'
        others = item.get('otherNames', [])
        if not isinstance(others, list) or not all(isinstance(o, str) for o in others):
            return f'interventions[{i}].otherNames is not a list of strings'
        # A drug whose name and aliases all normalize to no term ("Tablets", "low dose") is searched by no
        # query, so the route completed over the NCT query alone and could read as a negative. A placebo,
        # sham or control label names no drug to search and is not a problem.
        labels = [item['name'], *others]
        if (
            is_drug_intervention_type(item['type'])
            and not any(drug_label_terms(label) for label in labels)
            and not any(is_placebo_label(label) for label in labels)
        ):
            return f'interventions[{i}] ({item["name"]!r}) names no drug term the openFDA search can query'
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
        if not is_drug_intervention_type(item.get('type')):
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


def registry_references_problem(study: dict[str, Any] | None) -> str | None:
    """Why a study's `referencesModule` cannot be read, or None when it can.

    `extract_registry_references` returns an empty list both for a record that cites nothing and for
    a module, list or entry of the wrong type, so coverage asks this first. Presence of a key is
    what counts: a record with no `referencesModule` (1,859 of the 3,371 distinct trials in the stored
    unprojected CT.gov records) or a module with no `references` key (329) cites nothing, while a
    present part of the wrong type (none in the stored records) leaves the citations unknown.

    An entry is readable only when it carries a non-blank `pmid` or `citation`, and its `pmid`,
    `citation` and `type` are strings where present (null counts as absent). An unreadable entry
    yields no link at all: one with neither field has nothing a review could read, and one with a
    mistyped field (an integer PMID) must not verify the route by coercion. All 3,449 entries in
    the stored records carry a citation string, 3,345 a PMID string too, and none has a non-string field.
    """
    if not isinstance(study, dict) or not isinstance(study.get('protocolSection'), dict):
        return 'no protocolSection object'
    ps = study['protocolSection']
    if 'referencesModule' not in ps:
        return None
    rm = ps['referencesModule']
    if not isinstance(rm, dict):
        return 'referencesModule is not an object'
    if 'references' not in rm:
        return None
    refs = rm['references']
    if not isinstance(refs, list):
        return 'referencesModule.references is not a list'
    for i, ref in enumerate(refs):
        problem = _registry_reference_entry_problem(ref)
        if problem:
            return f'referencesModule.references[{i}]{problem}'
    return None


def _registry_reference_entry_problem(ref: Any) -> str | None:
    """Why one reference entry cannot be read, as a suffix to its path, or None when it can.

    `extract_registry_references` skips every entry this names, so a malformed entry never yields a
    link: an integer PMID coerced to text could otherwise resolve on PubMed and verify the route.
    """
    if not isinstance(ref, dict):
        return ' is not an object'
    for key in ('pmid', 'citation', 'type'):
        if ref.get(key) is not None and not isinstance(ref[key], str):
            return f'.{key} is not a string'
    if not any(isinstance(ref.get(key), str) and ref[key].strip() for key in ('pmid', 'citation')):
        return ' has no citation text or PMID'
    return None


def extract_registry_references(study: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return normalized entries from `protocolSection.referencesModule.references`.

    Read type-safely (a non-object module raised `AttributeError`); a malformed part, or an entry
    `_registry_reference_entry_problem` names, gives no entry here and is reported by
    `registry_references_problem`.
    """
    if not isinstance(study, dict):
        return []
    ps = study.get('protocolSection')
    rm = ps.get('referencesModule') if isinstance(ps, dict) else None
    refs = rm.get('references') if isinstance(rm, dict) else None
    if not isinstance(refs, list):
        return []
    out: list[dict[str, Any]] = []
    for idx, ref in enumerate(refs):
        if _registry_reference_entry_problem(ref) is not None:
            # Unreadable (reported by `registry_references_problem`): a placeholder `ref:<nct>:<index>`
            # would offer a review nothing to read, and a coerced field could verify the route.
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
            # Checked against the time of validation, which the record's `fetched_at` (stamped after
            # it) does not precede, so an answer refused here would fail `ctgov_version_record_valid`.
            valid=lambda body: ctgov_version_valid(body)
            and not data_timestamp_after_retrieval(body['dataTimestamp'], now_utc()),
        )

    def observe_version_or_raise(phase: str) -> None:
        rec = observe_version(phase)
        if ctgov_version_record_valid(rec):
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
            # A results date after the answer arrived (`ctgov_results_date_after`) is unusable too,
            # checked against the time of validation, which the record's `fetched_at` does not precede.
            valid=lambda body, wanted=nct_id: ctgov_study_answers(body, wanted)
            and ctgov_results_date_after(body, now_utc()) is None,
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
        # Only a readable record that is the trial's own (`ctgov_study_answers`) and holds no results
        # date after its retrieval (`ctgov_study_record_valid`): the timeline and the sponsor context
        # read it without further type checks.
        out[nct_id] = body if ctgov_study_answers(body, nct_id) and ctgov_study_record_valid(rec) else None
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

def _is_ctgov_version_answer(obj: Any) -> bool:
    """Whether `obj` is a CT.gov `/version` answer: an object holding exactly `apiVersion` and
    `dataTimestamp`, as every one in the stored response logs does."""
    return isinstance(obj, dict) and set(obj) == {'apiVersion', 'dataTimestamp'}


def _studies_in_ctgov_answer(obj: Any) -> list[Any]:
    """The study records one CT.gov answer holds: itself for a study, the entries of a `studies` page,
    none for a `/version` answer.

    Any other value is returned as a record of its own, so that `ctgov_study_problem` refuses it by
    position. A study that lost its `protocolSection`, or a `studies` value that is not a list, used
    to be skipped, and the freeze went ahead with a smaller batch than the one supplied, with nothing
    reported (only an ID given with `--nct-list` was caught).
    """
    if isinstance(obj, dict) and 'protocolSection' not in obj and isinstance(obj.get('studies'), list):
        return list(obj['studies'])
    if _is_ctgov_version_answer(obj):
        return []
    return [obj]


def _local_log_line_failed(line: dict[str, Any]) -> bool:
    """Whether a response-log line records a request that got no answer: `status` null (a transport
    error, as `ResponseLog` writes it) or an HTTP status other than 200 (a 404, a 400). Such a line
    holds no study by design. A 200 line must hold a study, a `studies` page or a `/version` answer,
    and a missing `status` or any other value (such as `unusable_200`, an answer that could not be
    read) is refused rather than skipped."""
    if 'status' not in line:
        return False
    status = line['status']
    return status is None or (isinstance(status, int) and not isinstance(status, bool) and status != 200)


def _studies_in_local_object(obj: Any) -> Iterable[Any]:
    """The study records one object of a local file holds: a response-log line's answer when its `status`
    is 200, none when the request got no answer (`_local_log_line_failed`), the line itself for any other
    status (refused by the caller), and otherwise the object read as a CT.gov answer
    (`_studies_in_ctgov_answer`). A JSONL line and a whole `.json` file are read the same way: a `.json`
    file holding one log record was read as a study and refused for its missing `protocolSection`, and a
    failed one was refused rather than skipped."""
    if not (isinstance(obj, dict) and 'body' in obj and 'protocolSection' not in obj):
        yield from _studies_in_ctgov_answer(obj)
    elif _local_log_line_failed(obj):
        return
    elif obj.get('status') != 200:
        yield obj
    else:
        body = obj['body']
        yield from _studies_in_ctgov_answer(json.loads(body) if isinstance(body, str) else body)


def _iter_studies_from_local_file(path: Path) -> Iterable[Any]:
    """Yield raw CT.gov v2 study dicts from `.json`, `.json.gz`, `.jsonl`, or `.jsonl.gz`.

    A file, or each JSONL line, may be a study, a `studies` page, a list of studies (`.json` only), a
    CT.gov `/version` answer (which holds none), or a response-log record (an object with a `body`,
    whose answer is read the same way when its `status` is 200, and which holds none when the request
    got no answer: `_local_log_line_failed`). Everything else is yielded as a record of its own,
    whatever its shape, and so is every entry of a `studies` page or list: the caller refuses a
    malformed one rather than letting it drop out. The stored local files hold only these shapes:
    2,522 study files, a `studies` page, a list, 1,944 study lines, and log lines holding 9,933 pages,
    54 studies, 11 text-body pages, 6 `/version` answers and 33 failed (400) requests.
    """
    name = path.name
    is_gz = name.endswith('.gz')
    is_jsonl = '.jsonl' in name
    raw = gzip.decompress(path.read_bytes()) if is_gz else path.read_bytes()
    if is_jsonl:
        for raw_line in raw.split(b'\n'):
            if not raw_line.strip():
                continue
            yield from _studies_in_local_object(json.loads(raw_line.rstrip(b'\r').decode('utf-8')))
        return

    parsed = json.loads(raw.decode('utf-8'))
    if isinstance(parsed, list):
        yield from parsed
    else:
        yield from _studies_in_local_object(parsed)


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
    # Records retrieved at `fetched_at` cannot reflect a CT.gov refresh after it, and the data
    # timestamp becomes the build's default `as_of` (`data_timestamp_after_retrieval`, the rule live
    # version observations follow too).
    if data_timestamp_after_retrieval(data_timestamp, fetched_at):
        raise ValueError(
            f'data_timestamp {data_timestamp!r} is later than fetched_at {fetched_at!r}: records retrieved '
            'then cannot reflect a later CT.gov refresh'
        )
    # The cache holds the records, so they cannot have been retrieved after it was frozen: a stated
    # retrieval in 2100 for a cache frozen in 2026 published an impossible acquisition time, and its data
    # timestamp (up to 14 hours later) became the default `as_of`. The freeze time is chosen here, before
    # anything is written, and passed on to `freeze_cache`.
    frozen_at = frozen_at or now_utc()
    if datetime.fromisoformat(fetched_at) > datetime.fromisoformat(frozen_at):
        raise ValueError(
            f'fetched_at {fetched_at!r} is later than the freeze ({frozen_at!r}): records cannot be '
            'retrieved after the cache that holds them is frozen'
        )
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
    # A study record that is not a valid CT.gov v2 study (no protocolSection object, identification
    # module or NCT ID, or a part the build reads of the wrong type: `ctgov_study_problem`) is refused,
    # not skipped: skipping it froze a smaller batch than the one supplied, with nothing reported (with
    # `--nct-list` only a listed trial's absence was caught), and freezing a mistyped part froze a cache
    # the build could not read (`statusModule: ["bad"]` raised `AttributeError`).
    malformed: list[str] = []
    for file_path in files:
        for position, st in enumerate(_iter_studies_from_local_file(file_path), start=1):
            problem = ctgov_study_problem(st)
            # A results date after the stated retrieval contradicts it (`ctgov_results_date_after`).
            late = ctgov_results_date_after(st, fetched_at) if problem is None else None
            if late:
                problem = f'{late} is after the stated fetched_at ({fetched_at})'
            if problem:
                malformed.append(f'{file_path.name} study {position}: {problem}')
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
    if malformed:
        raise ValueError(
            f'{source_path}: {len(malformed)} study record(s) are not valid CT.gov v2 study records the build '
            f'can read (first: {malformed[0]}); fix or remove them rather than freeze a smaller batch'
        )
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
        if key.startswith('ctgov/version/') and ctgov_version_record_valid(ctgov_lines[idx])
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
    """Verify `.jsonl.gz` against both its `.meta.json` sidecar and `manifest.json` entry.

    The build reads the sidecar (`run.json` sources, the `as_of` fallback and bound), so every field
    it holds must equal the manifest entry, which `freeze_cache` writes from the same dict, and the
    fetch times it states must be those of the archive's own (hash-verified) records, as
    `freeze_source` computed them. An edited sidecar could otherwise publish false acquisition times.

    Every record must carry its `fetched_at` as a whole UTC timestamp (`timestamp_problem`), as every
    acquisition and `freeze-local` stamps it. The retrieval bounds downstream read it: a record without
    one, or with `fetched_at: "garbage"`, was skipped or stringified here, so a CT.gov results record
    posted `2100-01-01` built a verified link dated 2100.
    """
    if not path.is_file():
        raise CacheIntegrityError(f'{path.name}: listed in manifest.json but missing from cache')
    meta_path = path.with_name(path.name + '.meta.json')
    if not meta_path.is_file():
        raise CacheIntegrityError(f'{path.name}: missing .meta.json sidecar')
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise CacheIntegrityError(f'{meta_path.name}: unreadable JSON sidecar ({exc})') from exc

    if not isinstance(meta, dict):
        raise CacheIntegrityError(f'{meta_path.name}: sidecar is not a JSON object')
    for key in sorted(set(meta) | set(expected_manifest_entry)):
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
        if problem := timestamp_problem(rec.get('fetched_at'), 'fetched_at', offset='utc'):
            raise CacheIntegrityError(f"{path.name}: record {rec['key']!r}: {problem}")
        records[rec['key']] = rec

    if len(records) != meta.get('n_records'):
        raise CacheIntegrityError(
            f"{path.name}: decoded {len(records)} record(s), expected {meta.get('n_records')}"
        )
    times = sorted(str(r['fetched_at']) for r in records.values() if r.get('fetched_at'))
    held_times = (('first_fetched_at', times[0] if times else None), ('last_fetched_at', times[-1] if times else None))
    for key, held in held_times:
        if meta.get(key) != held:
            raise CacheIntegrityError(
                f'{path.name}: {meta_path.name} states {key} {meta.get(key)!r}, but the archive records '
                f'give {held!r}'
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
        if entry.get('frozen_at') != manifest.get('frozen_at'):
            raise CacheIntegrityError(
                f"manifest.json: the {source} archive states frozen_at {entry.get('frozen_at')!r}, but the "
                f"cache was frozen at {manifest.get('frozen_at')!r}"
            )
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
