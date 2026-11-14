"""Acquire official FDA records and documents for the device inventory, politely and resumably.

Three sources, all public and none needing a credential:

  openFDA               api.fda.gov device/510k.json (which also carries De Novo numbers) and
                        device/pma.json: applicant, decision date and code, product code, the
                        510(k) Statement-or-Summary flag. It exposes no predicate and no study
                        identifier; that absence is recorded as such.
  FDA database pages    the 510(k), De Novo and PMA release pages on www.accessdata.fda.gov. Each
                        lists the documents FDA publishes for the submission, with FDA's own
                        label ("Summary", "Statement", "Decision Summary", "Classification Order",
                        "Approval Order", "Summary of Safety and Effectiveness", "Labeling").
                        This is how documents are discovered; a URL is not guessed when the page
                        answers.
  FDA documents         the PDFs those pages link to, under www.accessdata.fda.gov/cdrh_docs/.
                        URL patterns are used only as a fallback and are recorded as such.

Everything fetched is frozen: API and page responses in an append-only JSONL log that is then
written as a sorted, byte-stable archive with its sha256 (as outcomes/acquire.py does), documents
content-addressed by sha256 with one manifest line per attempt. A rerun never re-downloads a URL
whose hash is recorded, never re-asks a question whose answer is logged, and retries only what
failed transiently. A 404 is a finding and is recorded as one; it is not an error.

Nothing in this module runs on a schedule. Network access happens only in the functions that take
a `get` callable; tests pass a stub.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

OPENFDA = 'https://api.fda.gov/device'
ACCESSDATA = 'https://www.accessdata.fda.gov'
DATABASE_PAGES = ACCESSDATA + '/scripts/cdrh/cfdocs'
CDRH_DOCS = ACCESSDATA + '/cdrh_docs'
# A generic client name: requests carry nothing that identifies a person or a project.
USER_AGENT = 'device-evidence-research/1.0'
# At most two requests a second to any one host: 0.55 s between request starts.
MIN_INTERVAL_SECONDS = 0.55
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 4
MAX_DOCUMENT_BYTES = 120 * 1024 * 1024
OPENFDA_BATCH = 50
# openFDA repeats, on every record, the registration and FEI numbers of every establishment listed
# under the product code. They describe the product code, not the submission, and are dropped.
OPENFDA_DROPPED = ('registration_number', 'fei_number')
DOCUMENT_OUTCOMES = ('retrieved', 'not_found', 'not_a_pdf', 'too_large', 'http_error', 'network_error',
                     'skipped_disk_budget')

Response = tuple[int | None, dict[str, str], bytes, str | None]
Get = Callable[[str, dict[str, Any] | None], Response]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Throttle:
    """Spaces request starts per host, and owns the sleeping (so that a test can run without
    waiting). One instance is shared by everything that talks to a host."""

    def __init__(self, min_interval: float = MIN_INTERVAL_SECONDS, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.min_interval, self._sleep, self._clock = min_interval, sleep, clock
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


def requests_get(timeout: float = 120.0) -> Get:
    """The real transport: one requests.Session honouring the environment's proxy settings."""
    import requests
    session = requests.Session()
    session.headers['User-Agent'] = USER_AGENT

    def get(url: str, params: dict[str, Any] | None) -> Response:
        try:
            r = session.get(url, params=params, timeout=timeout, stream=True)
            body = bytearray()
            for chunk in r.iter_content(1 << 16):
                body += chunk
                if len(body) > MAX_DOCUMENT_BYTES:
                    r.close()
                    return r.status_code, dict(r.headers), b'', 'too_large'
            return r.status_code, dict(r.headers), bytes(body), None
        except requests.RequestException as exc:
            return None, {}, b'', f'{type(exc).__name__}: {exc}'[:300]
    return get


def polite_get(get: Get, throttle: Throttle, url: str, params: dict[str, Any] | None = None) -> tuple[Response, int]:
    """One logical request: throttled, retried with backoff (2, 4, 8 s) on 429/5xx and on network
    failure, never on 404 or other 4xx (those are answers). Returns the last response and the
    number of attempts made."""
    resp: Response = (None, {}, b'', 'not attempted')
    for attempt in range(1, MAX_ATTEMPTS + 1):
        throttle.wait(url)
        resp = get(url, params)
        status, _, _, error = resp
        if error == 'too_large' or (status is not None and status not in RETRY_STATUSES):
            return resp, attempt
        if attempt < MAX_ATTEMPTS:
            throttle.back_off(2.0 ** attempt)
    return resp, MAX_ATTEMPTS


# ---------------------------------------------------------------- frozen response logs

def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Lines of an append-only JSONL file. A final line without its newline is a write that was
    interrupted (the process was killed mid-append): it was never recorded, so it is cut off the
    file, with a message, and whatever it described is simply asked or fetched again."""
    if not path.exists():
        return []
    raw = path.read_bytes()
    keep = raw[:raw.rfind(b'\n') + 1]
    if len(keep) != len(raw):
        print(f'{path.name}: dropped an interrupted final line of {len(raw) - len(keep)} bytes', flush=True)
        path.write_bytes(keep)
    return [json.loads(line) for line in keep.decode('utf-8').splitlines() if line.strip()]


def write_atomic(path: Path, data: bytes) -> None:
    """Write beside the target and rename, so that a killed process never leaves a partial file
    under a final name (a content-addressed name over truncated bytes would be believed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.part')
    tmp.write_bytes(data)
    tmp.replace(path)


# Answers that settle a question: the record, or the host saying it has none. Any other status is
# logged (it is what happened) but asked again on a rerun: on 2026-11-13 the FDA database pages
# answered four of 1,431 requests with a bare HTTP 400 and served the same pages minutes later.
FINAL_STATUSES = frozenset({200, 404})
UNUSABLE_200 = 'unusable_200'   # HTTP 200 with an empty or unparsable body: logged, not final, not an answer
NOT_THE_RECORD = 'not_the_record'   # HTTP 200 from a database page that does not name the number asked for


class ResponseLog:
    """Append-only JSONL of answered requests, keyed by the caller; the latest line for a key is
    its answer. A response that never arrived (network failure after every retry) is not logged.
    An answer outside FINAL_STATUSES is logged and asked again on the next run."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: dict[str, dict[str, Any]] = {rec['key']: rec for rec in read_jsonl(path)}

    def get(self, key: str) -> dict[str, Any] | None:
        return self.records.get(key)

    def settled(self, key: str) -> dict[str, Any] | None:
        """The logged answer when it is final (FINAL_STATUSES), else None: ask (again)."""
        rec = self.records.get(key)
        return rec if rec is not None and rec.get('status') in FINAL_STATUSES else None

    def put(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
        self.records[record['key']] = record


def encode_archive(records: Iterable[dict[str, Any]]) -> bytes:
    """Canonical archive bytes: one sorted-key JSON object per line, ordered by key."""
    recs = sorted(records, key=lambda r: r['key'])
    if len({r['key'] for r in recs}) != len(recs):
        raise ValueError('every archived response needs a distinct key')
    return b''.join(json.dumps(r, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode() + b'\n'
                    for r in recs)


def freeze(log: ResponseLog, archive: Path, source: str) -> dict[str, Any]:
    """Write the log as a frozen archive (gzip, mtime 0) with a metadata file naming its sha256.
    The build reads only frozen archives, so its inputs are pinned by hash."""
    raw = encode_archive(log.records.values())
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(gzip.compress(raw, mtime=0))
    times = sorted(r['fetched_at'] for r in log.records.values() if r.get('fetched_at'))
    meta = {'source': source, 'archive': archive.name, 'n_records': len(log.records),
            'sha256_uncompressed': sha256(raw), 'first_fetched_at': times[0] if times else None,
            'last_fetched_at': times[-1] if times else None, 'frozen_at': now()}
    archive.with_name(archive.name + '.meta.json').write_text(json.dumps(meta, indent=1) + '\n')
    return meta


def read_archive(archive: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """{key: record} and metadata from a frozen archive; refuses bytes that do not match."""
    meta_path = archive.with_name(archive.name + '.meta.json')
    if not meta_path.exists():
        raise ValueError(f'{archive.name}: no metadata file beside the archive')
    meta = json.loads(meta_path.read_text())
    raw = gzip.decompress(archive.read_bytes())
    if sha256(raw) != meta.get('sha256_uncompressed'):
        raise ValueError(f'{archive.name}: bytes do not match the recorded sha256')
    records = {}
    for line in raw.splitlines():
        if line:
            rec = json.loads(line)
            records[rec['key']] = rec
    if len(records) != meta.get('n_records'):
        raise ValueError(f'{archive.name}: record count does not match the metadata')
    return records, meta


def logged_json(log: ResponseLog, get: Get, throttle: Throttle, key: str, url: str,
                params: dict[str, Any] | None = None, tag: dict[str, Any] | None = None,
                transform: Callable[[Any], Any] | None = None,
                valid: Callable[[Any], bool] | None = None) -> dict[str, Any] | None:
    """The logged answer to a JSON request, fetching it once if it is not logged yet. None when no
    answer arrived (the caller records the route as incomplete). `valid`, when given, says whether
    a parsed 200 body has the shape an answer must have; one that does not is no answer.

    A logged answer is this request's answer only if it was logged for these very parameters: a
    key can outlive the request it was made for (an inventory revision that changes an interior
    number of a batch keeps its first, last and length), and a different request is asked again,
    its answer becoming the latest under the key."""
    rec = log.settled(key)
    if rec is not None and rec.get('params', {}) == (params or {}):
        return rec
    (status, _, body, error), attempts = polite_get(get, throttle, url, params)
    if status is None:
        return log.get(key)     # an earlier non-final answer, if any, is still what is known
    parsed: Any = None
    if body:
        try:
            parsed = json.loads(body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            error = error or 'response is not JSON'
    if transform is not None and parsed is not None:
        parsed = transform(parsed)
    if status == 200 and parsed is None:
        # A 200 whose body is empty or not JSON (a proxy page, a cut-off response) is no answer.
        # It is logged under its own status, which no reader takes for 200 and which is not final,
        # so the route reads incomplete and the next run asks again. The HTTP status is kept.
        status, error = UNUSABLE_200, error or 'empty response body'
    elif status == 200 and valid is not None and not valid(parsed):
        # A 200 that parses but lacks the shape of an answer (an error envelope, say) is no answer
        # either, for the same reason.
        status, error = UNUSABLE_200, error or 'response lacks the structure of an answer'
    rec = {'key': key, 'url': url, 'params': params or {}, 'tag': tag or {}, 'fetched_at': now(), 'status': status,
           'http_status': 200 if status == UNUSABLE_200 else status,
           'attempts': attempts, 'body': parsed, 'error': error}
    log.put(rec)
    return rec


# ---------------------------------------------------------------- openFDA metadata

def _strip_openfda(body: Any) -> Any:
    for r in (body or {}).get('results') or []:
        for k in OPENFDA_DROPPED:
            (r.get('openfda') or {}).pop(k, None)
    return body


def openfda_requests(submission_numbers: list[str]) -> list[dict[str, Any]]:
    """The openFDA requests that cover every submission number.

    510(k) and De Novo numbers are both served by device/510k.json (field k_number), asked for
    OPENFDA_BATCH at a time. A PMA is asked for one number at a time from device/pma.json, which
    returns the original and every supplement; the record whose supplement_number matches the
    listed one ('' for an original) is the submission's record.
    """
    reqs: list[dict[str, Any]] = []
    k = sorted(sn for sn in submission_numbers if sn.startswith(('K', 'DEN', 'BK')))
    for i in range(0, len(k), OPENFDA_BATCH):
        chunk = k[i:i + OPENFDA_BATCH]
        reqs.append({'key': f'openfda/510k/{chunk[0]}..{chunk[-1]}/{len(chunk)}', 'url': f'{OPENFDA}/510k.json',
                     'params': {'search': 'k_number:(' + ' OR '.join(chunk) + ')', 'limit': str(len(chunk))},
                     'numbers': chunk})
    for sn in sorted(n for n in submission_numbers if n.startswith('P')):
        base = sn.split('/')[0]
        reqs.append({'key': f'openfda/pma/{base}', 'url': f'{OPENFDA}/pma.json',
                     'params': {'search': f'pma_number:{base}', 'limit': '1000'}, 'numbers': [sn]})
    return reqs


def openfda_answer(body: Any) -> bool:
    """A 200 from openFDA answers only with a list of results; no match is a 404, so a 200 without
    the list (an error envelope) is no answer about any number it covers."""
    return isinstance(body, dict) and isinstance(body.get('results'), list)


def fetch_openfda(submission_numbers: list[str], log: ResponseLog, get: Get, throttle: Throttle) -> dict[str, int]:
    done = failed = 0
    for q in openfda_requests(submission_numbers):
        rec = logged_json(log, get, throttle, q['key'], q['url'], q['params'], tag={'numbers': q['numbers']},
                          transform=_strip_openfda, valid=openfda_answer)
        done, failed = done + (rec is not None), failed + (rec is None)
    return {'requests_answered': done, 'requests_without_answer': failed}


def openfda_by_submission(records: dict[str, dict[str, Any]], submission_numbers: list[str]) -> dict[str, dict[str, Any]]:
    """{submission number: {'state', 'reason', 'record'}} from the logged openFDA answers.

    state: found | no_record (the endpoint answered and does not hold the number) |
           no_answer (the request for it was never answered) | error (answered with an HTTP error
           other than openFDA's 404 "no matches").
    """
    out: dict[str, dict[str, Any]] = {}
    for q in openfda_requests(submission_numbers):
        rec = records.get(q['key'])
        other_request = rec is not None and rec.get('params') != q['params']
        results = ((rec or {}).get('body') or {}).get('results') or []
        for sn in q['numbers']:
            if rec is None or other_request:
                # An answer logged under this key for different parameters (another batch that
                # shares its first and last number and its length) is not this request's answer.
                out[sn] = {'state': 'no_answer',
                           'reason': ('the answer logged for this batch was for a different request'
                                      if other_request else 'the openFDA request covering this number was not answered'),
                           'record': None}
                continue
            # openFDA answers a search with no matches with HTTP 404 and an error body.
            if rec['status'] not in (200, 404):
                out[sn] = {'state': 'error', 'reason': f"openFDA answered HTTP {rec['status']}", 'record': None}
                continue
            if rec['status'] == 200 and not openfda_answer(rec.get('body')):
                # Logged before answers were checked for shape: still no answer about this number.
                out[sn] = {'state': 'error', 'reason': 'openFDA answered HTTP 200 without a list of results',
                           'record': None}
                continue
            if sn.startswith('P'):
                base, _, supp = sn.partition('/')
                match = [r for r in results if r.get('pma_number') == base and (r.get('supplement_number') or '') == supp]
            else:
                match = [r for r in results if r.get('k_number') == sn]
            if len(match) == 1:
                out[sn] = {'state': 'found', 'reason': 'one openFDA record carries this number', 'record': match[0],
                           'fetched_at': rec['fetched_at'],
                           'n_pma_supplements': (sum(1 for r in results if r.get('supplement_number'))
                                                 if sn.startswith('P') else None)}
            else:
                out[sn] = {'state': 'no_record' if not match else 'error',
                           'reason': ('openFDA holds no record with this number' if not match
                                      else f'{len(match)} openFDA records carry this number'),
                           'record': None, 'fetched_at': rec['fetched_at']}
    return out


def official_metadata(found: dict[str, Any]) -> dict[str, Any]:
    """The fields the inventory lacks, from one openFDA record, under one set of names for both
    endpoints. `predicate` is always absent: openFDA does not expose it."""
    r = found.get('record')
    if not r:
        return {'available': False, 'reason': found.get('reason')}
    o = r.get('openfda') or {}
    return {
        'available': True, 'source': 'openFDA', 'retrieved_at': found.get('fetched_at'),
        'applicant': r.get('applicant') or None, 'decision_date': r.get('decision_date') or None,
        'date_received': r.get('date_received') or None, 'decision_code': r.get('decision_code') or None,
        'decision_description': r.get('decision_description') or None,
        'product_code': r.get('product_code') or None,
        'device_name': r.get('device_name') or r.get('trade_name') or None,
        'generic_name': r.get('generic_name') or o.get('device_name') or None,
        'advisory_committee': r.get('advisory_committee_description') or None,
        # 'Summary', 'Statement' or '' on 510(k) records; the field does not exist for a PMA.
        'statement_or_summary': (r.get('statement_or_summary') or None) if 'k_number' in r else None,
        'clearance_type': r.get('clearance_type') or None, 'third_party_review': r.get('third_party_flag') or None,
        'regulation_number': o.get('regulation_number') or None, 'device_class': o.get('device_class') or None,
        'supplement_type': r.get('supplement_type') or None, 'supplement_reason': r.get('supplement_reason') or None,
        'approval_order_statement': r.get('ao_statement') or None,
        'n_pma_supplements_on_record': found.get('n_pma_supplements'),
        'predicate': None, 'predicate_absent_reason': 'not_exposed_by_openfda',
    }


# ---------------------------------------------------------------- FDA database pages

_DOC_LINK = re.compile(r'<a\s[^>]*?href\s*=\s*"(https?://www\.accessdata\.fda\.gov/cdrh_docs/[^"]+)"[^>]*>([^<]*)', re.I)


def database_page_url(submission_number: str) -> tuple[str, dict[str, str]]:
    """The FDA release-database page for a submission number, by pathway prefix."""
    sn = submission_number.strip().upper()
    if sn.startswith('DEN'):
        return f'{DATABASE_PAGES}/cfpmn/denovo.cfm', {'id': sn}
    if sn.startswith('P'):
        return f'{DATABASE_PAGES}/cfpma/pma.cfm', {'id': sn.replace('/', '')}
    return f'{DATABASE_PAGES}/cfpmn/pmn.cfm', {'ID': sn}


def _page_field(html: str, label: str) -> str | None:
    """The value of one labelled row (<TH>label</TH><TD>value</TD>) on an FDA database page, upper
    case without spaces; '' when the row is absent; None when the page gives it two different values."""
    values = {re.sub(r'\s+', '', v).upper()
              for v in re.findall(r'<TH[^>]*>\s*' + re.escape(label) + r'\s*</TH>\s*<TD[^>]*>([^<]*)', html, re.IGNORECASE)}
    if len(values) > 1:
        return None
    return values.pop() if values else ''


def page_names_the_submission(submission_number: str, html: str) -> bool:
    """Whether a database page is the record of this very number, read from the labelled row in
    which FDA's record pages state it; the number appearing elsewhere (an error page that echoes
    the request, a link to another record) does not count.

    510(k) pages state it in a "510(k) Number" row and De Novo pages in a "De Novo Number" row. A
    PMA page uses two rows, "PMA Number" (P140011) and "Supplement Number" (S008; absent on an
    original PMA's page), so the base number alone also appears on the original's page and on
    every other supplement's: the PMA Number must be the base and the Supplement Number must be
    the supplement asked for, or absent for an original. Checked on 2026-11-14 against all 1,431
    pages fetched on 2026-11-13 (1,376 510(k), 37 De Novo, 18 PMA), whose rows all read this way.
    """
    sn = submission_number.strip().upper()
    base, _, supp = sn.partition('/')
    if base.startswith('DEN'):
        return _page_field(html, 'De Novo Number') == base
    if not base.startswith('P'):
        return _page_field(html, '510(k) Number') == base
    pma, supplement = _page_field(html, 'PMA Number'), _page_field(html, 'Supplement Number')
    return pma == base and supplement == supp


def parse_document_links(html: str) -> list[dict[str, str]]:
    """Every link on a database page that points under /cdrh_docs/, with FDA's label for it.
    http is rewritten to https (the pages still print http for the reviews directory)."""
    out, seen = [], set()
    for url, label in _DOC_LINK.findall(html):
        url = re.sub(r'^http://', 'https://', url.strip())
        if url.lower() not in seen:
            seen.add(url.lower())
            out.append({'url': url, 'official_label': re.sub(r'\s+', ' ', label).strip()})
    return out


def fetch_database_pages(submission_numbers: list[str], log: ResponseLog, pages_dir: Path, get: Get,
                         throttle: Throttle, progress: Callable[[str], None] | None = None) -> dict[str, int]:
    """One page per number; the HTML is kept gzip-compressed under pages_dir and the parsed links
    in the log. A page that answers 200 without naming the number is logged as `not_the_record`."""
    answered = unanswered = 0
    for sn in submission_numbers:
        key = f'fda_page/{sn}'
        if log.settled(key) is not None:
            answered += 1
            continue
        url, params = database_page_url(sn)
        (status, _, body, error), attempts = polite_get(get, throttle, url, params)
        if status is None:
            unanswered += 1
            continue
        html = body.decode('utf-8', 'replace')
        names_it = page_names_the_submission(sn, html)
        logged = status
        if status == 200 and not names_it:
            # A generic or error page served with 200, or another submission's page, does not say
            # which documents FDA holds for this number. It is logged under its own status, which
            # is not final, so the next run asks again and coverage reads the search incomplete
            # rather than taking 404s on guessed addresses for "FDA holds no document".
            logged, error = NOT_THE_RECORD, error or 'the page does not name the submission number requested'
        write_atomic(pages_dir / (sn.replace('/', '_') + '.html.gz'), gzip.compress(body, mtime=0))
        log.put({'key': key, 'url': url, 'params': params, 'fetched_at': now(), 'status': logged, 'http_status': status,
                 'attempts': attempts, 'error': error, 'sha256': sha256(body), 'bytes': len(body),
                 'page_names_the_submission': names_it,
                 'body': {'document_links': parse_document_links(html) if logged == 200 else []}})
        answered += 1
        if progress:
            progress(sn)
    return {'pages_answered': answered, 'pages_without_answer': unanswered}


# ---------------------------------------------------------------- documents

def cdrh_docs_dir(submission_number: str) -> str:
    """The cdrh_docs directory FDA files a number under, from the two digits after its prefix:
    'pdf' for 1976-2001 receipts, 'pdf2'..'pdf9' for 2002-2009, 'pdf10' onwards after. Verified
    against live documents on 2026-11-13 (K955841, K003301, K033840, K082483, K092967, K233662);
    the inventory's own pdf_url column pads the year to two digits and so misses 2002-2009."""
    yy = int(re.sub(r'^[A-Z]+', '', submission_number.upper())[:2])
    if yy >= 76 or yy <= 1:
        return 'pdf'
    return f'pdf{yy}'


def document_id(submission_number: str, url: str) -> str:
    """Stable id: the submission number and the source URL (case-folded; the host serves paths
    case-insensitively). The same URL fetched again is the same document, possibly a new version."""
    return f"doc:{submission_number.replace('/', '_')}:{sha256(url.lower().encode())[:12]}"


def pattern_urls(submission_number: str) -> list[dict[str, str]]:
    """Fallback URLs by pattern, tried only for what the database page did not already list.

    510(k): the summary/statement file and the reviews file. De Novo: the classification order and
    the decision summary under reviews/. PMA: A (approval order), B (summary of safety and
    effectiveness), C (labeling) and the bare number (pre-2002 PMAs were published as one file).
    These are recollections that the first live requests confirmed for some numbers; a pattern
    that returns 404 is recorded as not found, never as proof that no document exists.
    """
    sn = submission_number.upper()
    d, flat = cdrh_docs_dir(sn), sn.replace('/', '')
    if sn.startswith('P'):
        names = [(f'{d}/{flat}A.pdf', 'Approval Order'), (f'{d}/{flat}B.pdf', 'Summary of Safety and Effectiveness'),
                 (f'{d}/{flat}C.pdf', 'Labeling'), (f'{d}/{flat}.pdf', '')]
    elif sn.startswith('DEN'):
        names = [(f'{d}/{flat}.pdf', 'Classification Order'), (f'reviews/{flat}.pdf', 'Decision Summary')]
    else:
        names = [(f'{d}/{flat}.pdf', ''), (f'reviews/{flat}.pdf', 'Decision Summary')]
    return [{'url': f'{CDRH_DOCS}/{n}', 'official_label': label} for n, label in names]


def document_plan(submission_number: str, page: dict[str, Any] | None,
                  parent_page: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The documents to try for one submission, each with how it was discovered.

    Links on the submission's own database page come first. Pattern URLs are added when the page
    listed nothing, was not answered, or (De Novo and PMA only, where the substantive document is
    the point) for any pattern the page did not list. A PMA supplement with no document of its
    own also gets the ORIGINAL PMA's documents, marked scope 'parent_submission': they belong to
    the original approval, not to the supplement, and are never evidence at supplement level.
    """
    plan: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(url: str, label: str, discovery: str, scope: str) -> None:
        if url.lower() not in seen:
            seen.add(url.lower())
            plan.append({'document_id': document_id(submission_number, url), 'submission_number': submission_number,
                         'url': url, 'official_label': label or None, 'discovery': discovery, 'scope': scope})
    links = ((page or {}).get('body') or {}).get('document_links') or []
    for link in links:
        add(link['url'], link['official_label'], 'fda_database_page', 'this_submission')
    if not links or submission_number.upper().startswith(('DEN', 'P')):
        for p in pattern_urls(submission_number):
            add(p['url'], p['official_label'], 'url_pattern', 'this_submission')
    for link in ((parent_page or {}).get('body') or {}).get('document_links') or []:
        add(link['url'], link['official_label'], 'fda_database_page_of_original_pma', 'parent_submission')
    return plan


class Manifest:
    """documents/manifest.jsonl: one line per attempt on a document URL, newest last. The latest
    line for a document id is its current state; earlier lines are its history."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lines: list[dict[str, Any]] = read_jsonl(path)

    def latest(self) -> dict[str, dict[str, Any]]:
        return {line['document_id']: line for line in self.lines}

    def append(self, line: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + '\n')
        self.lines.append(line)


def settled(line: dict[str, Any] | None) -> bool:
    """True when a document need not be requested again: its bytes are held under a recorded
    hash, the host answered that it has no such file, or the file is over the size limit.
    Transient failures are not settled, and neither is `not_a_pdf`: an HTML page served with 200
    in place of the file (an error or proxy page), or such a page left on disk by an earlier
    fetch, says nothing lasting about the file, so the address is asked again."""
    return bool(line) and line['outcome'] in ('retrieved', 'not_found', 'too_large')


def recover_local(local_dir: Path, url_of: dict[str, str], manifest: Manifest, store: Path) -> int:
    """Register PDFs an earlier fetch left on disk (named <YYYY-MM-DD>_<number>.pdf) and copy them
    into the content-addressed store, so they are not downloaded again.

    Assumption, from the script that made them: each file was fetched from that row's pdf_url on
    the date in its name. The HTTP status of that fetch was not recorded and is stored as absent.
    A file that is not a PDF (an error page saved under a .pdf name) is recorded as `not_a_pdf`
    with no size or hash and is not copied into the store; each file is registered once.
    """
    latest, n = manifest.latest(), 0
    registered = {(line['document_id'], line.get('local_source')) for line in manifest.lines
                  if line.get('origin') == 'local_recovered'}
    for path in sorted(local_dir.glob('*.pdf')):
        m = re.fullmatch(r'(\d{4}-\d{2}-\d{2})_([A-Z]+\d{6})\.pdf', path.name)
        if not m or m.group(2) not in url_of:
            continue
        sn, url = m.group(2), url_of[m.group(2)]
        did = document_id(sn, url)
        if settled(latest.get(did)) or (did, path.name) in registered:
            continue
        raw = path.read_bytes()
        is_pdf = raw[:5] == b'%PDF-'
        digest = sha256(raw) if is_pdf else None
        if is_pdf and not (store / f'{digest}.pdf').exists():
            write_atomic(store / f'{digest}.pdf', raw)
        manifest.append({'document_id': did, 'submission_number': sn, 'url': url, 'official_label': None,
                         'discovery': 'inventory_pdf_url', 'scope': 'this_submission', 'origin': 'local_recovered',
                         'local_source': path.name, 'retrieved_at': m.group(1), 'retrieved_at_precision': 'date',
                         'http_status': None, 'http_status_absent_reason': 'not_recorded_by_the_earlier_fetch',
                         'outcome': 'retrieved' if is_pdf else 'not_a_pdf', 'bytes': len(raw) if is_pdf else None,
                         'sha256': digest, 'content_type': None, 'last_modified': None, 'etag': None, 'attempts': 0,
                         'error': None})
        n += 1
    return n


def store_bytes(store: Path) -> int:
    return sum(p.stat().st_size for p in store.glob('*.pdf')) if store.exists() else 0


def fetch_documents(plan: list[dict[str, Any]], manifest: Manifest, store: Path, get: Get, throttle: Throttle,
                    progress: Callable[[dict[str, Any]], None] | None = None,
                    store_budget_bytes: int | None = None) -> dict[str, int]:
    """Try every planned document that is not settled. Bytes are stored once under their sha256;
    the manifest line records status, size, hash, the server's Last-Modified and ETag (the
    source's own version markers) and any failure. Nothing is raised for a missing document.

    With a `store_budget_bytes`, acquisition stops requesting once the store holds that much:
    every remaining document gets a `skipped_disk_budget` line (not settled, so a later run with
    room fetches it) and its submission's coverage reads search incomplete, with that reason.
    """
    latest = manifest.latest()
    counts = {k: 0 for k in DOCUMENT_OUTCOMES}
    counts['already_settled'] = 0
    held = store_bytes(store) if store_budget_bytes is not None else 0
    for item in plan:
        if settled(latest.get(item['document_id'])):
            counts['already_settled'] += 1
            continue
        if store_budget_bytes is not None and held >= store_budget_bytes:
            if (latest.get(item['document_id']) or {}).get('outcome') != 'skipped_disk_budget':
                manifest.append({**item, 'origin': 'fda_fetch', 'retrieved_at': now(), 'retrieved_at_precision': 'second',
                                 'http_status': None, 'http_status_absent_reason': 'not_requested', 'bytes': None,
                                 'sha256': None, 'content_type': None, 'last_modified': None, 'etag': None,
                                 'attempts': 0, 'outcome': 'skipped_disk_budget',
                                 'error': f'document store reached its budget of {store_budget_bytes} bytes'})
            counts['skipped_disk_budget'] += 1
            continue
        (status, headers, body, error), attempts = polite_get(get, throttle, item['url'])
        h = {k.lower(): v for k, v in headers.items()}
        line = {**item, 'origin': 'fda_fetch', 'retrieved_at': now(), 'retrieved_at_precision': 'second',
                'http_status': status, 'bytes': len(body) if body else None, 'sha256': None,
                'content_type': h.get('content-type'), 'last_modified': h.get('last-modified'), 'etag': h.get('etag'),
                'attempts': attempts, 'error': error}
        if error == 'too_large':
            line['outcome'] = 'too_large'
        elif status is None:
            line['outcome'] = 'network_error'
        elif status == 404:
            line['outcome'], line['bytes'] = 'not_found', None
        elif status != 200:
            line['outcome'] = 'http_error'
        elif body[:5] != b'%PDF-':
            line['outcome'] = 'not_a_pdf'
        else:
            digest = sha256(body)
            target = store / f'{digest}.pdf'
            if not target.exists():
                write_atomic(target, body)
                # Only bytes newly stored count against the budget: the store is addressed by
                # content, so a second address serving the same file adds nothing to it.
                held += len(body)
            line['outcome'], line['sha256'] = 'retrieved', digest
        manifest.append(line)
        latest[item['document_id']] = line
        counts[line['outcome']] += 1
        if progress:
            progress(line)
    return counts
