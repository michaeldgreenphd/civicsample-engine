"""Schema-version-1 artifact builder, archive manager, and producer contract checker for
`outcomes/comparability.py` and `outcomes/outcome_history.py`.

Artifact family (`SCHEMA_VERSION = 1`):
  - `run.json`: manifest carrying deterministic `run_identity` (`outcomes-comparability-YYYY-MM-DD-<10hex>`),
    producer revision and source digest, acquisition provenance and sha256, optional historical
    archive provenance, cohort scope, vocabularies, inspected paths, limitations, and sibling sha256s.
  - `summary.json`: cohort-level counts, state breakdowns, failure tables, and exact quantile
    summaries of continuous funnel/attrition distributions. Every count object carries `value`,
    `numerator`, `denominator`, and `definition` (no browser-side aggregation and no hardcoded
    pass/fail cutoffs).
  - `records.json`: one deterministic record per requested `NCT` ID.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from outcomes import acquire  # noqa: E402
from outcomes import comparability as C  # noqa: E402
from outcomes import outcome_history as OH  # noqa: E402

SCHEMA_VERSION = 1
API = 'https://clinicaltrials.gov/api/v2'
BATCH = 25
MIN_DELAY = 0.5
MAX_TRIES = 5

RUN_ID = re.compile(r'^outcomes-comparability-\d{4}-\d{2}-\d{2}-[0-9a-f]{10}$')
HEX64 = re.compile(r'^[0-9a-f]{64}$')
NCT = re.compile(r'^NCT\d{8}$')

PRIVATE_KEYS = frozenset({
    'reviewer', 'rationale_text', 'elapsed_minutes', 'active_minutes',
    'packet_sha256', 'raw_response', 'model_response', 'prompt',
})

LIMITATIONS = (
    'Denominator funnel differences, ratios, and arm-specific attrition rates are exact arithmetic '
    'summaries of structured registry fields; no hardcoded pass/fail cutoff is applied.',
    'OG -> BG and OG -> FG arm alignment uses conservative normalized non-empty title/description '
    'equivalence only; array position and participant count equality are never used to match arms.',
    'Missing-data, intercurrent-event, and hypothesis-margin signals scan structured primary-outcome '
    'fields in the registry record; posted PDF protocols, SAPs, and publications are not parsed.',
    'Protocol-to-results fidelity compares the current protocolSection against posted resultsSection; '
    'registration changes over time require an explicit frozen historical-versions archive.',
)


class BuildError(Exception):
    """Raised when build inputs, scope, or provenance checks fail."""


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def dump(obj: Any) -> bytes:
    """Canonical compact key-sorted UTF-8 JSON bytes with trailing newline."""
    return (json.dumps(obj, ensure_ascii=False, separators=(',', ':'), sort_keys=True) + '\n').encode('utf-8')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def read_nct_list(path: Path) -> list[str]:
    """Read distinct `NCT########` IDs from a text file (skipping blank and `#` comment lines)."""
    ids = [
        line.strip()
        for line in path.read_text(encoding='utf-8').splitlines()
        if line.strip() and not line.strip().startswith('#')
    ]
    bad = [i for i in ids if not NCT.match(i)]
    if bad:
        raise BuildError(f'{path.name}: invalid NCT ids: {bad[:5]}')
    if len(set(ids)) != len(ids):
        raise BuildError(f'{path.name}: duplicate NCT ids')
    return sorted(ids)


# ---------------------------------------------------------------- acquisition & offline loaders

def fetch(
    ids: list[str],
    get: Callable[..., Any] | None = None,
    delay: float = MIN_DELAY,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Fetch full CT.gov API v2 records for `ids` at <= 2 req/s (`delay >= 0.5s`)."""
    delay = max(delay, MIN_DELAY)
    session = requests.Session()
    session.headers['User-Agent'] = 'civicsample-outcomes-comparability'
    get_fn = get or (lambda url, params: session.get(url, params=params, timeout=120))
    lines: dict[str, bytes] = {}
    log: list[dict[str, Any]] = []
    failed: dict[str, str] = {}
    last = [0.0]

    def request(url: str, params: dict[str, Any], purpose: str) -> Any | None:
        error: str | None = None
        for attempt in range(MAX_TRIES):
            wait = delay - (time.monotonic() - last[0])
            if wait > 0:
                sleep(wait)
            entry: dict[str, Any] = {'purpose': purpose, 'requested_at': _now(), 'attempt': attempt + 1}
            log.append(entry)
            try:
                r = get_fn(url, params)
            except requests.RequestException as exc:
                last[0] = time.monotonic()
                error = f'{type(exc).__name__}: {exc}'
                entry.update(status=None, error=error)
            else:
                last[0] = time.monotonic()
                entry.update(status=r.status_code, bytes=len(r.content), sha256=sha256(r.content))
                if r.status_code == 200:
                    try:
                        return r.json()
                    except ValueError as exc:
                        error = f'response is not JSON: {exc}'
                        entry['error'] = error
                else:
                    error = f'HTTP {r.status_code}'
                    entry['error'] = error
                    if r.status_code not in (429, 500, 502, 503, 504):
                        break
            if attempt + 1 < MAX_TRIES:
                sleep(min(60.0, float(2 ** attempt * 2)))
        log[-1]['gave_up'] = error
        return None

    started = _now()
    version = request(f'{API}/version', {}, 'version')
    if not isinstance(version, dict) or not all(
        isinstance(version.get(k), str) and version[k].strip() for k in ('apiVersion', 'dataTimestamp')
    ):
        why = log[-1].get('gave_up') if version is None else f'missing apiVersion/dataTimestamp: {str(version)[:200]}'
        raise BuildError(f'registry version request failed ({why}); nothing was fetched')

    for i in range(0, len(ids), BATCH):
        batch = ids[i:i + BATCH]
        token: str | None = None
        pages = 0
        while True:
            params: dict[str, Any] = {'filter.ids': ','.join(batch), 'pageSize': 100, 'format': 'json'}
            if token:
                params['pageToken'] = token
            body = request(f'{API}/studies', params, f'studies {batch[0]}..{batch[-1]} ({len(batch)} ids)')
            if body is None:
                for n in batch:
                    if n not in lines:
                        failed[n] = f"request failed: {log[-1].get('gave_up')}"
                break
            for s in body.get('studies') or []:
                n = C.nct_id_of(s)
                if n in batch:
                    lines[n] = acquire.encode([s])
                else:
                    failed[str(n)] = 'returned by registry but not requested'
            token = body.get('nextPageToken')
            pages += 1
            if not token or pages >= 5:
                break
        for n in batch:
            if n not in lines and n not in failed:
                failed[n] = 'not returned by registry for filter.ids'

    studies_requests = [e for e in log if e['purpose'] != 'version']
    record = {
        'source': 'clinicaltrials.gov API v2',
        'mode': 'fetch',
        'endpoint': f'{API}/studies',
        'request': f'filter.ids in batches of {BATCH}, full record, format=json',
        'retrieved_from': started,
        'retrieved_to': _now(),
        'retrieved_at': started,
        'api_version': version.get('apiVersion'),
        'data_timestamp': version.get('dataTimestamp'),
        'n_requested': len(ids),
        'n_records': len(lines),
        'not_retrieved': [{'nct_id': n, 'reason': r} for n, r in sorted(failed.items())],
        'n_requests': len(log),
        'n_studies_requests': len(studies_requests),
        'n_retries': sum(1 for e in log if e['attempt'] > 1),
        'n_failed_requests': sum(1 for e in log if 'error' in e),
        'min_seconds_between_requests': delay,
        'requests': log,
    }
    return lines, record


def fetch_record(lines: dict[str, bytes], record: dict[str, Any], requested: list[str]) -> dict[str, Any]:
    raw = b''.join(lines[n] for n in sorted(lines))
    return {
        **record,
        'requested_ids': sorted(requested),
        'requested_ids_sha256': sha256('\n'.join(sorted(requested)).encode('utf-8')),
        'sha256_uncompressed': sha256(raw),
    }


def _name_of(rec: dict[str, Any]) -> str:
    date_part = str(rec.get('retrieved_at') or '')[:10]
    sha_part = str(rec.get('sha256_uncompressed') or '')[:12]
    rec_part = sha256(dump(rec))[:8]
    return f'records-{date_part}-{sha_part}-{rec_part}.jsonl.gz'


def archive_name(lines: dict[str, bytes], record: dict[str, Any], requested: list[str]) -> str:
    return _name_of(fetch_record(lines, record, requested))


def write_archive(lines: dict[str, bytes], path: Path, record: dict[str, Any], requested: list[str]) -> dict[str, Any]:
    raw = b''.join(lines[n] for n in sorted(lines))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(raw, mtime=0))
    meta = {**fetch_record(lines, record, requested), 'archive': path.name}
    path.with_name(path.name + '.meta.json').write_text(json.dumps(meta, indent=1) + '\n', encoding='utf-8')
    return meta


def read_archive_meta(path: Path) -> dict[str, Any]:
    """Validate frozen `.jsonl.gz` archive against its `.meta.json` sidecar."""
    if not path.is_file():
        raise BuildError(f'{path}: archive file does not exist')
    meta_path = path.with_name(path.name + '.meta.json')
    if not meta_path.exists():
        raise BuildError(f'{path.name}: no metadata file beside the archive')
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    digest = hashlib.sha256()
    n = 0
    with gzip.open(path, 'rb') as fh:
        for line in fh:
            digest.update(line)
            n += bool(line.strip())
    if digest.hexdigest() != meta.get('sha256_uncompressed'):
        raise BuildError(f'{path.name}: bytes do not match recorded sha256_uncompressed')
    if n != meta.get('n_records'):
        raise BuildError(f'{path.name}: record count does not match metadata')
    ids = meta.get('requested_ids')
    if not isinstance(ids, list) or not all(isinstance(i, str) and NCT.match(i) for i in ids) or len(set(ids)) != len(ids):
        raise BuildError(f'{path.name}: metadata does not list distinct valid requested NCT ids')
    if sha256('\n'.join(sorted(ids)).encode('utf-8')) != meta.get('requested_ids_sha256'):
        raise BuildError(f'{path.name}: requested_ids do not match recorded sha256')
    if len(ids) != meta.get('n_requested'):
        raise BuildError(f'{path.name}: n_requested does not match requested_ids length')
    if not all(isinstance(meta.get(k), str) and meta[k].strip() for k in ('api_version', 'data_timestamp')):
        raise BuildError(f'{path.name}: metadata lacks registry api_version or data_timestamp')
    expected = _name_of({k: v for k, v in meta.items() if k != 'archive'})
    if path.name != expected or meta.get('archive') != path.name:
        raise BuildError(f'{path.name}: metadata does not match archive name (expected {expected})')
    return meta


def iter_archive(path: Path) -> Iterator[Any]:
    with gzip.open(path, 'rb') as fh:
        for line in fh:
            if line.strip():
                try:
                    yield json.loads(line)
                except ValueError as exc:
                    yield exc


def _unwrap_study_object(obj: Any) -> dict[str, Any]:
    """Unwrap common stored CT.gov v2 study wrappers (`{"study": ...}` or `{"body": ...}`)."""
    if not isinstance(obj, dict):
        raise BuildError('stored record is not a JSON object')
    if 'protocolSection' in obj or 'resultsSection' in obj or 'hasResults' in obj:
        return obj
    if isinstance(obj.get('study'), dict):
        return obj['study']
    if isinstance(obj.get('body'), dict):
        return obj['body']
    if isinstance(obj.get('body'), str):
        parsed = json.loads(obj['body'])
        if isinstance(parsed, dict) and ('protocolSection' in parsed or 'hasResults' in parsed):
            return parsed
    return obj


def load_records_dir(records_dir: Path, snapshot_date: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load stored per-trial CT.gov v2 `.json.gz` / `.json` files from `records_dir` deterministically."""
    if not records_dir.is_dir():
        raise BuildError(f'{records_dir}: not a directory')
    candidates = sorted(
        p for p in records_dir.iterdir()
        if p.is_file() and (p.name.endswith('.json.gz') or (p.name.endswith('.json') and not p.name.endswith('.meta.json')))
    )
    if not candidates:
        raise BuildError(f'{records_dir}: contains no .json.gz or .json study files')

    studies_by_nct: dict[str, dict[str, Any]] = {}
    dates_seen: list[str] = []
    for p in candidates:
        try:
            raw_bytes = gzip.decompress(p.read_bytes()) if p.name.endswith('.gz') else p.read_bytes()
            raw_obj = json.loads(raw_bytes.decode('utf-8'))
            study = _unwrap_study_object(raw_obj)
        except (OSError, EOFError, ValueError, BuildError) as exc:
            raise BuildError(f'{p.name}: could not read study JSON ({exc})') from exc
        nct = C.nct_id_of(study)
        if not nct or not NCT.match(nct):
            raise BuildError(f'{p.name}: missing or invalid protocolSection.identificationModule.nctId')
        if nct in studies_by_nct:
            raise BuildError(f'{p.name}: duplicate study record for {nct}')
        studies_by_nct[nct] = study
        # Collect lastUpdatePostDate or resultsFirstPostDate as deterministic fallback date when no
        # explicit snapshot_date is supplied, so two builds on the same directory share one date.
        status_mod = ((study.get('protocolSection') or {}).get('statusModule') or {})
        for key in ('lastUpdatePostDateStruct', 'resultsFirstPostDateStruct'):
            dt = (status_mod.get(key) or {}).get('date')
            if isinstance(dt, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', dt):
                dates_seen.append(dt)

    ordered_ids = sorted(studies_by_nct)
    ordered_studies = [studies_by_nct[n] for n in ordered_ids]
    canonical_bytes = acquire.encode(ordered_studies)
    inferred_date = snapshot_date or (max(dates_seen) if dates_seen else '1970-01-01')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', inferred_date):
        raise BuildError(f'snapshot_date {inferred_date!r} must be YYYY-MM-DD')

    meta = {
        'source': 'local CT.gov API v2 records directory',
        'mode': 'records_dir',
        'endpoint': None,
        'request': None,
        'retrieved_from': None,
        'retrieved_to': None,
        'retrieved_at': f'{inferred_date}T00:00:00+00:00',
        'api_version': 'local_records_dir',
        'data_timestamp': inferred_date,
        'n_requested': len(ordered_ids),
        'n_records': len(ordered_ids),
        'not_retrieved': [],
        'requested_ids': ordered_ids,
        'requested_ids_sha256': sha256('\n'.join(ordered_ids).encode('utf-8')),
        'sha256_uncompressed': sha256(canonical_bytes),
        'archive': records_dir.name,
    }
    return ordered_studies, meta


def load_responses_cache(cache_path: Path, snapshot_date: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load CT.gov API v2 studies from a frozen HTTP batch responses `.jsonl.gz` cache."""
    if not cache_path.is_file():
        raise BuildError(f'{cache_path}: responses cache file does not exist')
    studies_by_nct: dict[str, dict[str, Any]] = {}
    fetched_dates: list[str] = []
    try:
        with gzip.open(cache_path, 'rt', encoding='utf-8') as fh:
            for line_no, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                outer = json.loads(line)
                if isinstance(outer, dict) and isinstance(outer.get('fetched_at'), str):
                    if re.match(r'^\d{4}-\d{2}-\d{2}', outer['fetched_at']):
                        fetched_dates.append(outer['fetched_at'][:10])
                if isinstance(outer, dict) and 'body' in outer:
                    body = json.loads(outer['body']) if isinstance(outer['body'], str) else outer['body']
                    batch_studies = body.get('studies') or [] if isinstance(body, dict) else []
                elif isinstance(outer, dict) and ('protocolSection' in outer or 'hasResults' in outer):
                    batch_studies = [outer]
                else:
                    raise BuildError(f'{cache_path.name} line {line_no}: unrecognized cache line shape')
                for s in batch_studies:
                    nct = C.nct_id_of(s)
                    if not nct or not NCT.match(nct):
                        raise BuildError(f'{cache_path.name} line {line_no}: study lacks valid NCT id')
                    if nct in studies_by_nct:
                        raise BuildError(f'{cache_path.name}: duplicate study {nct}')
                    studies_by_nct[nct] = s
    except (OSError, EOFError, ValueError) as exc:
        raise BuildError(f'{cache_path.name}: could not read responses cache ({exc})') from exc

    if not studies_by_nct:
        raise BuildError(f'{cache_path.name}: contains no studies')

    ordered_ids = sorted(studies_by_nct)
    ordered_studies = [studies_by_nct[n] for n in ordered_ids]
    canonical_bytes = acquire.encode(ordered_studies)

    if snapshot_date is None and not fetched_dates:
        # Derive deterministic date from latest record post date when cache lines lack `fetched_at`.
        post_dates: list[str] = []
        for s in ordered_studies:
            sm = ((s.get('protocolSection') or {}).get('statusModule') or {})
            for k in ('lastUpdatePostDateStruct', 'resultsFirstPostDateStruct'):
                dt = (sm.get(k) or {}).get('date')
                if isinstance(dt, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', dt):
                    post_dates.append(dt)
        inferred_date = max(post_dates) if post_dates else '1970-01-01'
    else:
        inferred_date = snapshot_date or max(fetched_dates)

    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', inferred_date):
        raise BuildError(f'snapshot_date {inferred_date!r} must be YYYY-MM-DD')

    meta = {
        'source': 'frozen CT.gov API v2 responses cache',
        'mode': 'responses_cache',
        'endpoint': None,
        'request': None,
        'retrieved_from': None,
        'retrieved_to': None,
        'retrieved_at': f'{inferred_date}T00:00:00+00:00',
        'api_version': 'cached_v2',
        'data_timestamp': inferred_date,
        'n_requested': len(ordered_ids),
        'n_records': len(ordered_ids),
        'not_retrieved': [],
        'requested_ids': ordered_ids,
        'requested_ids_sha256': sha256('\n'.join(ordered_ids).encode('utf-8')),
        'sha256_uncompressed': sha256(canonical_bytes),
        'source_file_sha256': sha256(cache_path.read_bytes()),
        'archive': cache_path.name,
    }
    return ordered_studies, meta


# ---------------------------------------------------------------- scope, records, summary

def build_scope(
    frame: list[str],
    requested: list[str],
    excluded: list[str] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Verify every frame record is either requested or explicitly excluded with a stated reason."""
    frame_set = set(frame)
    want = set(requested)
    out = sorted(excluded or [])
    if excluded is not None:
        if not (reason or '').strip():
            raise BuildError('an exclude list requires a non-empty --exclude-reason')
        if len(set(out)) != len(out):
            raise BuildError('duplicate NCT ids in exclude list')
        if want & set(out):
            raise BuildError(f'ids both requested and excluded: {sorted(want & set(out))[:5]}')
        if set(out) - frame_set:
            raise BuildError(f'excluded ids not in frame: {sorted(set(out) - frame_set)[:5]}')
        left = frame_set - want - set(out)
        if left:
            raise BuildError(f'{len(left)} frame records are neither requested nor excluded: {sorted(left)[:5]}')
    elif frame_set - want:
        left = frame_set - want
        raise BuildError(
            f'{len(left)} frame records are not requested and no exclude list states why: {sorted(left)[:5]}'
        )
    return {
        'frame': {'n': len(frame_set), 'definition': 'Registry records held by the input source (the frame).'},
        'included': {
            'n': len(want),
            'definition': 'Trials covered by this build (one record each in records.json).',
        },
        'excluded': {
            'n': len(out),
            'reason': reason.strip() if out and reason else None,
            'nct_ids': out,
            'definition': 'Frame records left out of this build; excluded from records.json and all counts.',
        },
    }


def build_records(
    studies: Iterable[Any],
    requested: list[str],
    not_retrieved: dict[str, str],
    history_snapshots: dict[str, dict[str, Any]] | None = None,
    excluded: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build one comparability & outcome-history record per requested NCT ID."""
    records: dict[str, Any] = {}
    problems: dict[str, Any] = {'unkeyed_lines': [], 'unrequested_records': []}
    wanted = set(requested)

    for i, s in enumerate(studies):
        if isinstance(s, Exception):
            problems['unkeyed_lines'].append({'line': i, 'reason': f'{type(s).__name__}: {s}'})
            continue
        rec = C.inspect_study(s, history_snapshots=history_snapshots)
        n = rec['nct_id']
        if n is None:
            problems['unkeyed_lines'].append({'line': i, 'reason': 'no NCT id in protocolSection.identificationModule'})
        elif n in excluded:
            continue
        elif n not in wanted:
            problems['unrequested_records'].append(n)
        elif n in records:
            raise BuildError(f'{n}: duplicate study record in input')
        else:
            records[n] = rec

    for n in requested:
        if n not in records:
            reason = not_retrieved.get(n) or (
                'archive line could not be read' if problems['unkeyed_lines'] else 'not in the archive'
            )
            records[n] = C.not_retrieved_record(n, reason, history_snapshots=history_snapshots)

    return dict(sorted(records.items())), problems


def count_item(
    cid: str,
    label: str,
    numerator: int,
    denominator: str,
    denominators: dict[str, dict[str, Any]],
    definition: str,
) -> dict[str, Any]:
    """Canonical summary count object carrying `value`, `numerator`, `denominator`, and `definition`."""
    den_n = denominators[denominator]['n']
    return {
        'id': cid,
        'label': label,
        'value': numerator,
        'numerator': numerator,
        'denominator': denominator,
        'denominator_n': den_n,
        'proportion': round(numerator / den_n, 6) if den_n > 0 else None,
        'definition': definition,
    }


def _quantile(sorted_vals: list[float], q: float) -> float | None:
    """Exact linear-interpolated quantile over pre-sorted finite numbers; `None` when empty."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return round(sorted_vals[0], 6)
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return round(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac, 6)


def _distribution_summary(
    dist_id: str,
    label: str,
    values: list[int | float | None],
    denominator: str,
    denominators: dict[str, dict[str, Any]],
    definition: str,
) -> dict[str, Any]:
    """Summarize a continuous metric without hardcoded pass/fail cutoffs."""
    finite = sorted(float(v) for v in values if v is not None)
    n_eval = len(finite)
    n_miss = denominators[denominator]['n'] - n_eval
    return {
        'id': dist_id,
        'label': label,
        'definition': definition,
        'evaluable': count_item(
            f'{dist_id}_evaluable',
            f'{label}: evaluable',
            n_eval,
            denominator,
            denominators,
            f'Items in {denominator} with finite numeric operands for {dist_id}.',
        ),
        'not_evaluable': count_item(
            f'{dist_id}_not_evaluable',
            f'{label}: not evaluable',
            n_miss,
            denominator,
            denominators,
            f'Items in {denominator} where one or more operands for {dist_id} are absent or non-positive.',
        ),
        'quantiles': {
            'min': _quantile(finite, 0.0),
            'p25': _quantile(finite, 0.25),
            'median': _quantile(finite, 0.50),
            'p75': _quantile(finite, 0.75),
            'max': _quantile(finite, 1.0),
        },
    }


def summarize(
    records: dict[str, Any],
    problems: dict[str, Any],
    scope: dict[str, Any],
) -> dict[str, Any]:
    """Compute schema-1 `summary.json` where every count carries `value`, `numerator`, `denominator`,
    and `definition`."""
    recs = list(records.values())
    retrieved = [r for r in recs if r['results_status'] != 'not_retrieved']
    posted = [r for r in recs if r['results_status'] == 'posted']
    with_prim = [r for r in posted if r['primary_measures']]
    hist_eval = [
        r for r in recs
        if (r.get('outcome_history') or {}).get('version_history_state') == 'evaluated'
    ]

    all_prim_measures = [pm for r in posted for pm in (r['primary_measures'] or [])]
    all_og_bg = [a for pm in all_prim_measures for a in pm['og_to_bg_alignment']['og_alignments']]
    all_og_fg = [a for pm in all_prim_measures for a in pm['og_to_fg_alignment']['og_alignments']]
    all_analyses = [a for pm in all_prim_measures for a in pm['analyses']]
    pairwise_analyses = [a for a in all_analyses if a['group_ids_cardinality_state'] == 'pairwise_two_arm']
    proto_res_alignments = [
        al
        for r in posted
        for al in (((r.get('outcome_history') or {}).get('protocol_to_results') or {}).get('alignments') or [])
    ]
    hist_diffs = [
        d
        for r in hist_eval
        for d in (((r.get('outcome_history') or {}).get('version_history') or {}).get('diffs') or [])
    ]

    denominators: dict[str, dict[str, Any]] = {
        'frame_records': {
            'n': scope['frame']['n'],
            'definition': scope['frame']['definition'],
        },
        'requested_trials': {
            'n': len(recs),
            'definition': 'Trials covered by this build (one record each in records.json).',
        },
        'retrieved_trials': {
            'n': len(retrieved),
            'definition': 'Requested trials whose study record was retrieved.',
        },
        'posted_results_trials': {
            'n': len(posted),
            'definition': 'Retrieved trials with posted results (hasResults is true and resultsSection is non-empty).',
        },
        'trials_with_primary_measure': {
            'n': len(with_prim),
            'definition': 'Posted-results trials with at least one posted PRIMARY outcome measure.',
        },
        'history_evaluated_trials': {
            'n': len(hist_eval),
            'definition': 'Requested trials evaluated against a frozen historical version snapshot.',
        },
        'primary_measures': {
            'n': len(all_prim_measures),
            'definition': 'Posted PRIMARY outcome measures across all posted-results trials.',
        },
        'primary_measure_ogs': {
            'n': len(all_og_bg),
            'definition': 'Non-Total outcome groups (OGs) across all posted PRIMARY outcome measures.',
        },
        'primary_analyses': {
            'n': len(all_analyses),
            'definition': 'Statistical analysis entries (analyses[]) across all posted PRIMARY outcome measures.',
        },
        'pairwise_two_arm_analyses': {
            'n': len(pairwise_analyses),
            'definition': 'Primary-measure analyses naming exactly two distinct resolved outcome groups.',
        },
        'protocol_to_results_items': {
            'n': len(proto_res_alignments),
            'definition': 'Alignment items pairing protocolSection primaryOutcomes with posted PRIMARY measures.',
        },
        'history_diff_items': {
            'n': len(hist_diffs),
            'definition': 'Outcome-history diff items across trials evaluated against a historical version snapshot.',
        },
    }

    def n_where(pool: list[dict[str, Any]], pred: Callable[[dict[str, Any]], bool]) -> int:
        return sum(1 for x in pool if pred(x))

    metrics = [
        count_item(
            'trials_retrieved',
            'Trials retrieved',
            len(retrieved),
            'requested_trials',
            denominators,
            'Requested trials whose registry record was retrieved.',
        ),
        count_item(
            'results_posted',
            'Posted results section present',
            len(posted),
            'retrieved_trials',
            denominators,
            'hasResults is true and resultsSection is a non-empty object.',
        ),
        count_item(
            'has_posted_protocol',
            'Posted study protocol PDF',
            n_where(retrieved, lambda r: bool((r['posted_documents'] or {}).get('has_posted_protocol'))),
            'retrieved_trials',
            denominators,
            'At least one documentSection.largeDocumentModule.largeDocs entry has hasProtocol == true.',
        ),
        count_item(
            'has_posted_sap',
            'Posted statistical analysis plan (SAP) PDF',
            n_where(retrieved, lambda r: bool((r['posted_documents'] or {}).get('has_posted_sap'))),
            'retrieved_trials',
            denominators,
            'At least one documentSection.largeDocumentModule.largeDocs entry has hasSap == true.',
        ),
        count_item(
            'has_posted_icf',
            'Posted informed consent form (ICF) PDF',
            n_where(retrieved, lambda r: bool((r['posted_documents'] or {}).get('has_posted_icf'))),
            'retrieved_trials',
            denominators,
            'At least one documentSection.largeDocumentModule.largeDocs entry has hasIcf == true.',
        ),
        count_item(
            'enrollment_actual',
            'Registered enrollment marked ACTUAL',
            n_where(
                retrieved,
                lambda r: ((r['denominator_funnel'] or {}).get('registered_enrollment') or {}).get('type') == 'ACTUAL'
                and ((r['denominator_funnel'] or {}).get('registered_enrollment') or {}).get('state') == 'ok',
            ),
            'retrieved_trials',
            denominators,
            'protocolSection.designModule.enrollmentInfo carries a numeric count with type == "ACTUAL".',
        ),
        count_item(
            'enrollment_estimated',
            'Registered enrollment marked ESTIMATED',
            n_where(
                retrieved,
                lambda r: ((r['denominator_funnel'] or {}).get('registered_enrollment') or {}).get('type') == 'ESTIMATED'
                and ((r['denominator_funnel'] or {}).get('registered_enrollment') or {}).get('state') == 'ok',
            ),
            'retrieved_trials',
            denominators,
            'protocolSection.designModule.enrollmentInfo carries a numeric count with type == "ESTIMATED".',
        ),
        count_item(
            'period1_started_ok',
            'Period 1 STARTED participant sum formed',
            n_where(posted, lambda r: ((r['denominator_funnel'] or {}).get('period1_started') or {}).get('state') == 'ok'),
            'posted_results_trials',
            denominators,
            'participantFlowModule.periods[0] STARTED milestone has numeric numSubjects for every non-Total flow group.',
        ),
        count_item(
            'baseline_total_ok',
            'Baseline Total participant count formed',
            n_where(posted, lambda r: ((r['denominator_funnel'] or {}).get('baseline_total') or {}).get('state') == 'ok'),
            'posted_results_trials',
            denominators,
            'baselineCharacteristicsModule Participants denominator row forms a numeric total via Rule T.',
        ),
        count_item(
            'extra_baseline_groups_outside_primary',
            'Extra baseline cohorts outside primary measures',
            n_where(posted, lambda r: bool((r['arm_alignment'] or {}).get('extra_baseline_groups_outside_primary'))),
            'posted_results_trials',
            denominators,
            'At least one non-Total baseline group has no aligned or pooled counterpart in any posted PRIMARY measure.',
        ),
        count_item(
            'trials_with_shared_arm_contrasts',
            'Trials with non-independent shared-arm primary contrasts',
            n_where(posted, lambda r: bool((r['contrast_structure'] or {}).get('has_shared_arm_contrasts'))),
            'posted_results_trials',
            denominators,
            'At least one posted PRIMARY measure has two or more pairwise contrasts sharing a common OG arm.',
        ),
        count_item(
            'primary_measures_with_participant_denoms',
            'Primary measures with complete participant denominators',
            n_where(all_prim_measures, lambda pm: pm['denom_state'] == 'ok'),
            'primary_measures',
            denominators,
            'PRIMARY measure denoms[] carries a Participants row with numeric counts for every non-Total OG.',
        ),
        count_item(
            'primary_measures_with_class_denom_deviation',
            'Primary measures with row-specific classes[].denoms deviations',
            n_where(all_prim_measures, lambda pm: pm['has_class_denom_deviation'] is True),
            'primary_measures',
            denominators,
            'At least one classes[].denoms row differs in unit or per-OG count from measure-level denoms.',
        ),
        count_item(
            'primary_measures_with_shared_arm_contrasts',
            'Primary measures with shared-arm pairwise contrasts',
            n_where(all_prim_measures, lambda pm: pm['contrast_structure']['has_shared_arm_contrasts'] is True),
            'primary_measures',
            denominators,
            'Within one PRIMARY measure, two or more pairwise analyses reuse a common comparator or experimental OG.',
        ),
        count_item(
            'pairwise_contrasts_attrition_from_started_computable',
            'Pairwise 2-arm contrasts with exact attrition from Period 1 STARTED',
            n_where(
                pairwise_analyses,
                lambda a: bool(
                    ((a.get('differential_attrition') or {}).get('from_period1_started') or {}).get('computable')
                ),
            ),
            'pairwise_two_arm_analyses',
            denominators,
            '2-arm contrast where both OGs have exact_unique_title_match to distinct FG groups with positive STARTED counts.',
        ),
        count_item(
            'pairwise_contrasts_attrition_from_baseline_computable',
            'Pairwise 2-arm contrasts with exact attrition from Baseline',
            n_where(
                pairwise_analyses,
                lambda a: bool(
                    ((a.get('differential_attrition') or {}).get('from_baseline') or {}).get('computable')
                ),
            ),
            'pairwise_two_arm_analyses',
            denominators,
            '2-arm contrast where both OGs have exact_unique_title_match to distinct non-Total BG groups with positive counts.',
        ),
        count_item(
            'protocol_to_results_discrepancy_trials',
            'Posted-results trials with protocol-vs-results primary discrepancy',
            n_where(
                posted,
                lambda r: (((r.get('outcome_history') or {}).get('protocol_to_results') or {}).get('has_discrepancy')) is True,
            ),
            'posted_results_trials',
            denominators,
            'At least one primary outcome alignment between protocolSection and resultsSection is not verbatim match.',
        ),
    ]

    def _breakdown(
        table_id: str,
        vocab: tuple[str, ...],
        pool: list[Any],
        getter: Callable[[Any], str | None],
        denominator: str,
        definitions_by_state: dict[str, str],
    ) -> dict[str, Any]:
        return {
            'unit_denominator': denominator,
            'counts': {
                s: count_item(
                    f'{table_id}:{s}',
                    s,
                    sum(1 for item in pool if getter(item) == s),
                    denominator,
                    denominators,
                    definitions_by_state[s],
                )
                for s in vocab
            },
        }

    align_defs = {
        'exact_unique_title_match': 'Unique 1-to-1 match on normalized non-empty title/description.',
        'ambiguous_duplicate_titles': 'Duplicate group titles/descriptions prevent unique 1-to-1 resolution.',
        'pooled_or_subset_suspected': 'No exact match; pooling/subset cue or embedded arm title detected.',
        'no_title_match': 'Target module has groups, with no title/description match or pooling cue.',
        'module_groups_absent': 'Target module is absent or carries zero non-Total groups.',
    }
    md_defs = {
        'mmrm': 'Mixed-effects model for repeated measures (MMRM) alone named in structured primary fields.',
        'multiple_imputation': 'Multiple imputation / reference-based imputation alone named.',
        'locf': 'Last observation carried forward (LOCF) alone named.',
        'bocf': 'Baseline observation carried forward (BOCF) alone named.',
        'wocf': 'Worst observation carried forward (WOCF) alone named.',
        'observed_cases_only': 'Observed/complete/available cases without imputation alone named.',
        'multiple_methods_named': 'Two or more distinct missing-data methods named across structured primary fields.',
        'not_stated': 'No missing-data method detected in structured primary outcome fields.',
    }
    ice_defs = {
        'rescue_excluded_or_censored': 'Post-rescue or post-discontinuation measurements excluded/censored (or hypothetical estimand).',
        'treatment_policy_regardless_of_rescue_or_discontinuation': 'Treatment-policy strategy regardless of rescue or treatment discontinuation.',
        'both_co_reported': 'Both rescue-excluded/hypothetical and treatment-policy signals co-reported.',
        'not_stated': 'No intercurrent-event or rescue-medication signal detected in structured primary fields.',
    }
    hyp_defs = {
        'superiority_explicit': 'Explicit SUPERIORITY enum, or legacy SUPERIORITY_OR_OTHER disambiguated by superiority text.',
        'non_inferiority_with_margin': 'NON_INFERIORITY (or disambiguated legacy enum) with a numeric margin stated.',
        'non_inferiority_missing_margin': 'NON_INFERIORITY (or disambiguated legacy enum) with no numeric margin found in structured text.',
        'equivalence_with_margin': 'EQUIVALENCE (or disambiguated legacy enum) with a numeric margin/interval stated.',
        'equivalence_missing_margin': 'EQUIVALENCE (or disambiguated legacy enum) with no numeric margin found in structured text.',
        'ambiguous_legacy_enum': 'Undisambiguated SUPERIORITY_OR_OTHER(_LEGACY) or NON_INFERIORITY_OR_EQUIVALENCE(_LEGACY).',
        'other_explicit': 'Explicit OTHER hypothesis enum.',
        'not_stated': 'Hypothesis type not stated in nonInferiorityType or nonInferiorityComment.',
    }
    card_defs = {
        'empty': 'analyses[].groupIds carries zero valid group identifiers.',
        'single_arm': 'analyses[].groupIds references exactly one distinct group identifier.',
        'pairwise_two_arm': 'analyses[].groupIds references exactly two distinct group identifiers.',
        'multi_arm_joint': 'analyses[].groupIds references three or more distinct group identifiers.',
    }
    p2r_defs = {
        'verbatim_measure_and_timeframe_match': 'Normalized non-empty measure title and timeFrame match between protocol and results.',
        'timeframe_differing': 'Normalized measure title matches while timeFrame differs.',
        'measure_title_differing': 'Shared clinical construct tokens with differing measure wording.',
        'registered_primary_missing_in_results': 'Protocol primaryOutcome has no matching posted PRIMARY outcome measure.',
        'results_primary_unlisted_in_protocol': 'Posted PRIMARY outcome measure has no matching protocol primaryOutcome.',
    }
    vh_state_defs = {
        'history_archive_not_provided': 'Build ran without a historical study-versions archive.',
        'trial_not_in_history_archive': 'Historical archive was supplied, without a snapshot for this NCT ID.',
        'evaluated': 'Historical snapshot compared against current protocolSection.outcomesModule.',
        'parse_failed': 'Historical or current outcomesModule could not be parsed.',
    }
    hd_defs = {
        'unchanged': 'Historical primary outcome retained with identical normalized measure and timeFrame.',
        'primary_added': 'Current primary outcome absent from historical primary and secondary outcomes.',
        'primary_dropped': 'Historical primary outcome absent from current primary and secondary outcomes.',
        'primary_demoted_to_secondary': 'Historical primary outcome moved to current secondaryOutcomes.',
        'secondary_promoted_to_primary': 'Historical secondary outcome moved to current primaryOutcomes.',
        'timeframe_changed': 'Historical primary outcome retained same measure title with changed timeFrame.',
        'measure_reworded': 'Historical primary outcome paired with reworded current primary sharing clinical construct.',
    }
    flow_defs = {
        'ok': 'Numeric Period 1 STARTED participant counts summed across non-Total flow groups.',
        'flow_absent': 'resultsSection.participantFlowModule is absent or empty.',
        'unit_not_participants': 'Flow counts are reported only in non-participant units (e.g. eyes, lesions).',
        'no_periods': 'participantFlowModule has an empty periods list.',
        'no_started_milestone': 'First period has no STARTED milestone.',
        'no_flow_groups': 'No non-Total flow groups exist to sum over.',
        'non_numeric_or_missing_num_subjects': 'At least one non-Total flow group lacks a numeric STARTED numSubjects.',
        'no_posted_results': 'Trial has no posted results.',
    }

    state_breakdowns = {
        'period1_started_states': _breakdown(
            'period1_started',
            C.FLOW_STARTED_STATES,
            posted,
            lambda r: ((r['denominator_funnel'] or {}).get('period1_started') or {}).get('state'),
            'posted_results_trials',
            flow_defs,
        ),
        'og_to_bg_alignment': _breakdown(
            'og_to_bg',
            C.ARM_ALIGNMENT_STATES,
            all_og_bg,
            lambda a: a['state'],
            'primary_measure_ogs',
            align_defs,
        ),
        'og_to_fg_alignment': _breakdown(
            'og_to_fg',
            C.ARM_ALIGNMENT_STATES,
            all_og_fg,
            lambda a: a['state'],
            'primary_measure_ogs',
            align_defs,
        ),
        'trial_missing_data_signal': _breakdown(
            'trial_missing_data',
            C.MISSING_DATA_STATES,
            posted,
            lambda r: ((r['estimand_signals'] or {}).get('missing_data_signal') or {}).get('state'),
            'posted_results_trials',
            md_defs,
        ),
        'measure_missing_data_signal': _breakdown(
            'measure_missing_data',
            C.MISSING_DATA_STATES,
            all_prim_measures,
            lambda pm: pm['missing_data_signal']['state'],
            'primary_measures',
            md_defs,
        ),
        'trial_intercurrent_event_signal': _breakdown(
            'trial_ice',
            C.INTERCURRENT_EVENT_STATES,
            posted,
            lambda r: ((r['estimand_signals'] or {}).get('intercurrent_event_signal') or {}).get('state'),
            'posted_results_trials',
            ice_defs,
        ),
        'analysis_hypothesis_clarity': _breakdown(
            'hypothesis_clarity',
            C.HYPOTHESIS_CLARITY_STATES,
            all_analyses,
            lambda a: a['hypothesis_clarity']['state'],
            'primary_analyses',
            hyp_defs,
        ),
        'analysis_group_ids_cardinality': _breakdown(
            'cardinality',
            C.CARDINALITY_STATES,
            all_analyses,
            lambda a: a['group_ids_cardinality_state'],
            'primary_analyses',
            card_defs,
        ),
        'protocol_to_results_alignment': _breakdown(
            'protocol_to_results',
            OH.PROTOCOL_TO_RESULTS_STATES,
            proto_res_alignments,
            lambda al: al['state'],
            'protocol_to_results_items',
            p2r_defs,
        ),
        'version_history_record_state': _breakdown(
            'version_history_state',
            OH.VERSION_HISTORY_STATES,
            recs,
            lambda r: (r.get('outcome_history') or {}).get('version_history_state'),
            'requested_trials',
            vh_state_defs,
        ),
        'version_history_diffs': _breakdown(
            'history_diff',
            OH.HISTORY_DIFF_STATES,
            hist_diffs,
            lambda d: d['state'],
            'history_diff_items',
            hd_defs,
        ),
    }

    continuous_distributions = {
        'baseline_minus_period1_started': _distribution_summary(
            'baseline_minus_period1_started',
            'Baseline Total minus Period 1 STARTED participants',
            [(r['denominator_funnel'] or {}).get('baseline_minus_period1_started') for r in posted],
            'posted_results_trials',
            denominators,
            'Signed participant difference (baseline_total - period1_started) on posted-results trials.',
        ),
        'analysed_minus_baseline_total': _distribution_summary(
            'analysed_minus_baseline_total',
            'Primary measure analysed participants minus Baseline Total',
            [pm['analysed_minus_baseline_total'] for pm in all_prim_measures],
            'primary_measures',
            denominators,
            'Signed participant difference (analysed_total - baseline_total) across posted PRIMARY measures.',
        ),
        'analysed_over_period1_started': _distribution_summary(
            'analysed_over_period1_started',
            'Primary measure analysed participants divided by Period 1 STARTED',
            [pm['analysed_over_period1_started'] for pm in all_prim_measures],
            'primary_measures',
            denominators,
            'Ratio (analysed_total / period1_started) across posted PRIMARY measures when period1_started > 0.',
        ),
        'abs_differential_attrition_from_started': _distribution_summary(
            'abs_differential_attrition_from_started',
            'Absolute differential attrition between arms from Period 1 STARTED',
            [
                ((a.get('differential_attrition') or {}).get('from_period1_started') or {}).get(
                    'abs_differential_attrition'
                )
                for a in pairwise_analyses
            ],
            'pairwise_two_arm_analyses',
            denominators,
            'Absolute between-arm attrition difference |rate_A - rate_B| relative to Period 1 STARTED.',
        ),
        'abs_differential_attrition_from_baseline': _distribution_summary(
            'abs_differential_attrition_from_baseline',
            'Absolute differential attrition between arms from Baseline',
            [
                ((a.get('differential_attrition') or {}).get('from_baseline') or {}).get(
                    'abs_differential_attrition'
                )
                for a in pairwise_analyses
            ],
            'pairwise_two_arm_analyses',
            denominators,
            'Absolute between-arm attrition difference |rate_A - rate_B| relative to Baseline participant counts.',
        ),
    }

    frame_included_n = scope['frame']['n'] - scope['excluded']['n']
    scope_summary = {
        'definition': 'Which frame records this build covers. Every count in summary.json names its denominator.',
        'counts': {
            'included': count_item(
                'scope_frame_included',
                'Frame records included in this build',
                frame_included_n,
                'frame_records',
                denominators,
                'Frame records included in the requested trial list.',
            ),
            'excluded': count_item(
                'scope_frame_excluded',
                'Frame records excluded from this build',
                scope['excluded']['n'],
                'frame_records',
                denominators,
                scope['excluded']['definition'],
            ),
        },
        'excluded_reason': scope['excluded']['reason'],
    }

    tables = {
        'retrieval_failures': {
            'count': count_item(
                'retrieval_failures',
                'Requested trials not retrieved',
                n_where(recs, lambda r: r['results_status'] == 'not_retrieved'),
                'requested_trials',
                denominators,
                'Requested trials whose study record was not returned or could not be read.',
            ),
            'rows': [
                {'nct_id': r['nct_id'], 'reason': r['retrieval']['reason']}
                for r in recs
                if r['results_status'] == 'not_retrieved'
            ],
            'unkeyed_archive_lines': problems['unkeyed_lines'],
            'unrequested_records': problems['unrequested_records'],
        },
        'parsing_failures': {
            'count': count_item(
                'parsing_failures',
                'Retrieved trials with at least one section parse failure',
                n_where(retrieved, lambda r: r['parse']['status'] != 'ok'),
                'retrieved_trials',
                denominators,
                'Retrieved trials where a section could not be parsed; failed sections stay None, never zero.',
            ),
            'rows': [
                {'nct_id': r['nct_id'], 'section': f['section'], 'reason': f['reason']}
                for r in retrieved
                for f in r['parse']['failures']
            ],
        },
    }

    return {
        'scope': scope_summary,
        'denominators': denominators,
        'metrics': metrics,
        'state_breakdowns': state_breakdowns,
        'continuous_distributions': continuous_distributions,
        'tables': tables,
    }


def run_identity(run: dict[str, Any], files: dict[str, Any]) -> str:
    """10-hex deterministic content digest over `run.json` (minus `run_id`, `generated_at`, `files`)
    and every sibling file (minus `run_id`)."""
    body = {k: v for k, v in run.items() if k not in ('run_id', 'generated_at', 'files')}
    content = {
        name: sha256(dump({k: v for k, v in obj.items() if k != 'run_id'}))
        for name, obj in sorted(files.items())
    }
    return sha256(dump([body, content]))[:10]


def build(
    records: dict[str, Any],
    problems: dict[str, Any],
    acquisition: dict[str, Any],
    producer: dict[str, Any],
    parameters: dict[str, Any],
    scope: dict[str, Any],
    history_archive_meta: dict[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Assemble `{filename: parsed_object}` for the comparability artifact family (`schema_version = 1`)."""
    retrieved_at = str(acquisition.get('retrieved_at') or '')[:10]
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', retrieved_at):
        raise BuildError('acquisition record has no YYYY-MM-DD retrieved_at date')

    spec_path = ROOT / 'outcomes' / 'COMPARABILITY.md'
    spec_sha = sha256(spec_path.read_bytes()) if spec_path.is_file() else None

    hist_block = history_archive_meta or {
        'provided': False,
        'state': 'history_archive_not_provided',
        'file': None,
        'sha256_compressed': None,
        'sha256_uncompressed': None,
        'meta_verified': False,
        'n_records': None,
    }

    files: dict[str, Any] = {
        'summary.json': summarize(records, problems, scope),
        'records.json': {'records': records},
    }
    for obj in files.values():
        obj.update(
            schema_version=SCHEMA_VERSION,
            method_version=C.METHOD_VERSION,
            outcome_history_version=OH.METHOD_VERSION,
        )

    run: dict[str, Any] = {
        'schema_version': SCHEMA_VERSION,
        'manual_run': True,
        'scheduled': False,
        'producer': producer,
        'method': {
            'version': C.METHOD_VERSION,
            'outcome_history_version': OH.METHOD_VERSION,
            'spec': 'outcomes/COMPARABILITY.md',
            'spec_sha256': spec_sha,
        },
        'acquisition': {
            k: acquisition.get(k)
            for k in (
                'source', 'mode', 'endpoint', 'request', 'retrieved_from', 'retrieved_to',
                'retrieved_at', 'api_version', 'data_timestamp', 'n_requested', 'n_records',
                'requested_ids_sha256', 'sha256_uncompressed', 'source_file_sha256', 'archive',
            )
        },
        'history_archive': hist_block,
        'parameters': parameters,
        'scope': scope,
        'n_records': len(records),
        'vocabularies': {
            'results_statuses': list(C.RESULTS_STATUSES),
            'flow_started_states': list(C.FLOW_STARTED_STATES),
            'baseline_total_states': list(C.BASELINE_TOTAL_STATES),
            'measure_denom_states': list(C.MEASURE_DENOM_STATES),
            'arm_alignment_states': list(C.ARM_ALIGNMENT_STATES),
            'missing_data_states': list(C.MISSING_DATA_STATES),
            'intercurrent_event_states': list(C.INTERCURRENT_EVENT_STATES),
            'hypothesis_clarity_states': list(C.HYPOTHESIS_CLARITY_STATES),
            'cardinality_states': list(C.CARDINALITY_STATES),
            'protocol_to_results_states': list(OH.PROTOCOL_TO_RESULTS_STATES),
            'version_history_states': list(OH.VERSION_HISTORY_STATES),
            'history_diff_states': list(OH.HISTORY_DIFF_STATES),
        },
        'fields_inspected': list(C.FIELDS_INSPECTED),
        'limitations': list(LIMITATIONS),
    }

    rid = f'outcomes-comparability-{retrieved_at}-{run_identity(run, files)}'
    for obj in files.values():
        obj['run_id'] = rid
    run.update(run_id=rid, generated_at=generated_at or _now())
    run['files'] = {name: sha256(dump(obj)) for name, obj in sorted(files.items())}
    return {'run.json': run, **files}


# ---------------------------------------------------------------- producer contract checker

def _walk_keys(obj: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.add(k)
            found |= _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= _walk_keys(v)
    return found


def _iter_count_objects(node: Any, where: str = 'summary.json') -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield every count object in `summary.json` (objects carrying `numerator` or `value` + `denominator`)."""
    if isinstance(node, dict):
        if any(k in node for k in ('numerator', 'denominator')) or (
            'value' in node and 'definition' in node and 'quantiles' not in node
        ):
            yield where, node
            return
        for k, v in node.items():
            yield from _iter_count_objects(v, f'{where}.{k}')
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _iter_count_objects(v, f'{where}[{i}]')


def check(files: dict[str, Any]) -> list[str]:
    """Validate a comparability artifact family against all schema, provenance, and domain invariants."""
    p: list[str] = []
    run = files.get('run.json')
    if not isinstance(run, dict):
        return ['run.json is missing']
    if run.get('schema_version') != SCHEMA_VERSION:
        p.append(f"run.json schema_version {run.get('schema_version')!r} is not {SCHEMA_VERSION}")
    if not isinstance(run.get('run_id'), str) or not RUN_ID.match(run['run_id']):
        p.append('run.json run_id is malformed')
    if run.get('manual_run') is not True or run.get('scheduled') is not False:
        p.append('run.json must state manual_run: true and scheduled: false')
    method = run.get('method') or {}
    if method.get('version') != C.METHOD_VERSION or method.get('outcome_history_version') != OH.METHOD_VERSION:
        p.append('run.json method version mismatch')
    prod = run.get('producer') or {}
    if not HEX64.match(str(prod.get('source_sha256') or '')):
        p.append('run.json producer lacks a 64-hex source_sha256')
    acq = run.get('acquisition') or {}
    if not HEX64.match(str(acq.get('sha256_uncompressed') or '')):
        p.append('run.json acquisition lacks a 64-hex sha256_uncompressed')

    listed = run.get('files') or {}
    expected_siblings = {'summary.json', 'records.json'}
    if set(listed) != expected_siblings or set(files) != ({'run.json'} | expected_siblings):
        p.append('artifact family must contain exactly run.json, summary.json, and records.json')
        return p

    for path in sorted(listed):
        if sha256(dump(files[path])) != listed[path]:
            p.append(f'{path}: sha256 does not match run.json')
        if (
            files[path].get('run_id') != run.get('run_id')
            or files[path].get('schema_version') != SCHEMA_VERSION
            or files[path].get('method_version') != C.METHOD_VERSION
        ):
            p.append(f'{path}: run_id, schema_version, or method_version disagrees with run.json')

    if isinstance(run.get('run_id'), str) and run['run_id'][-10:] != run_identity(run, {k: files[k] for k in listed}):
        p.append('run.json run_id does not follow from the content of the family')

    leaked = _walk_keys(files) & set(PRIVATE_KEYS)
    if leaked:
        p.append(f'private keys leaked in artifact family: {sorted(leaked)}')

    scope = run.get('scope')
    if not isinstance(scope, dict) or set(scope) != {'frame', 'included', 'excluded'}:
        p.append('run.json lacks valid scope (frame, included, excluded)')
        return p

    records = files['records.json'].get('records')
    if not isinstance(records, dict):
        p.append('records.json lacks a records object')
        return p
    summary = files['summary.json']
    if not isinstance(summary, dict):
        p.append('summary.json is not an object')
        return p

    if run.get('n_records') != len(records) or scope['included'].get('n') != len(records):
        p.append('record count disagrees between run.json, scope.included, and records.json')
    ex = scope['excluded']
    if ex.get('n') != len(ex.get('nct_ids') or []) or (ex.get('n', 0) > 0 and not ex.get('reason')):
        p.append('scope.excluded must carry matching nct_ids and a stated reason')
    if set(ex.get('nct_ids') or []) & set(records):
        p.append('an excluded NCT id appears in records.json')

    hist_meta = run.get('history_archive') or {}
    hist_provided = bool(hist_meta.get('provided'))
    if hist_provided and not HEX64.match(str(hist_meta.get('sha256_uncompressed') or '')):
        p.append('run.json history_archive is marked provided without sha256_uncompressed')

    # Domain invariants across every record.
    for n, r in records.items():
        if not NCT.match(n) or r.get('nct_id') != n:
            p.append(f'{n}: record key and nct_id disagree')
        if r.get('method_version') != C.METHOD_VERSION:
            p.append(f'{n}: unexpected method_version {r.get("method_version")!r}')
        st = r.get('results_status')
        if st not in C.RESULTS_STATUSES:
            p.append(f'{n}: unknown results_status {st!r}')

        oh = r.get('outcome_history') or {}
        vh_state = oh.get('version_history_state')
        if vh_state not in OH.VERSION_HISTORY_STATES:
            p.append(f'{n}: invalid version_history_state {vh_state!r}')
        if not hist_provided and (vh_state != 'history_archive_not_provided' or (oh.get('version_history') or {}).get('diffs') is not None):
            p.append(f'{n}: history archive not provided, yet version_history_state is not history_archive_not_provided')

        if st != 'posted':
            continue

        arm_al = r.get('arm_alignment')
        if arm_al is not None:
            if arm_al.get('extra_baseline_groups_outside_primary') != bool(arm_al.get('extra_baseline_groups')):
                p.append(f'{n}: extra_baseline_groups_outside_primary disagrees with extra_baseline_groups list')

        for pm in r.get('primary_measures') or []:
            if pm.get('denom_state') not in C.MEASURE_DENOM_STATES:
                p.append(f'{n}: invalid denom_state on primary measure')
            for side in ('og_to_bg_alignment', 'og_to_fg_alignment'):
                for oa in (pm.get(side) or {}).get('og_alignments') or []:
                    if oa.get('state') not in C.ARM_ALIGNMENT_STATES:
                        p.append(f'{n}: invalid arm alignment state {oa.get("state")!r}')
                    if oa.get('state') == 'exact_unique_title_match' and not oa.get('matched_target_id'):
                        p.append(f'{n}: exact_unique_title_match without matched_target_id')
                    if oa.get('state') != 'exact_unique_title_match' and oa.get('matched_target_id') is not None:
                        p.append(f'{n}: non-exact alignment state carries matched_target_id')

            cs = pm.get('contrast_structure') or {}
            shared = cs.get('shared_arm_contrast_groups') or []
            if cs.get('has_shared_arm_contrasts') != bool(shared):
                p.append(f'{n}: has_shared_arm_contrasts disagrees with shared_arm_contrast_groups')
            for sg in shared:
                if len(sg.get('analysis_indices') or []) < 2 or sg.get('non_independent') is not True:
                    p.append(f'{n}: invalid shared_arm_contrast_groups entry')

            for a in pm.get('analyses') or []:
                if a.get('group_ids_cardinality_state') not in C.CARDINALITY_STATES:
                    p.append(f'{n}: invalid group_ids_cardinality_state')
                hc = a.get('hypothesis_clarity') or {}
                h_state = hc.get('state')
                if h_state not in C.HYPOTHESIS_CLARITY_STATES:
                    p.append(f'{n}: invalid hypothesis_clarity state {h_state!r}')
                raw_ni = hc.get('raw_non_inferiority_type')
                if (
                    raw_ni in (
                        'SUPERIORITY_OR_OTHER', 'SUPERIORITY_OR_OTHER_LEGACY',
                        'NON_INFERIORITY_OR_EQUIVALENCE', 'NON_INFERIORITY_OR_EQUIVALENCE_LEGACY',
                    )
                    and hc.get('disambiguated_from_legacy_enum') is None
                    and h_state != 'ambiguous_legacy_enum'
                ):
                    p.append(f'{n}: undisambiguated legacy hypothesis enum {raw_ni} not marked ambiguous_legacy_enum')
                if h_state in ('non_inferiority_with_margin', 'equivalence_with_margin') and not hc.get('margin_stated'):
                    p.append(f'{n}: {h_state} claimed without margin_stated')

            md = pm.get('missing_data_signal') or {}
            if md.get('state') not in C.MISSING_DATA_STATES:
                p.append(f'{n}: invalid missing_data_signal state')
            if md.get('state') != 'not_stated' and not md.get('evidence'):
                p.append(f'{n}: missing_data_signal {md.get("state")} lacks path+substring evidence')

            ice = pm.get('intercurrent_event_signal') or {}
            if ice.get('state') not in C.INTERCURRENT_EVENT_STATES:
                p.append(f'{n}: invalid intercurrent_event_signal state')
            if ice.get('state') != 'not_stated' and not ice.get('evidence'):
                p.append(f'{n}: intercurrent_event_signal {ice.get("state")} lacks path+snippet evidence')

    # Verify every count object in summary.json carries value, numerator, denominator, definition.
    den = summary.get('denominators') or {}
    if not isinstance(den, dict) or not den:
        p.append('summary.json lacks denominators')
        return p
    for d_name, d_obj in den.items():
        if not isinstance(d_obj, dict) or not isinstance(d_obj.get('n'), int) or d_obj['n'] < 0 or not d_obj.get('definition'):
            p.append(f'denominator {d_name}: requires non-negative int n and non-empty definition')

    count_objs = list(_iter_count_objects(summary))
    if not count_objs:
        p.append('summary.json carries no count objects')
    for where, cobj in count_objs:
        for req_k in ('value', 'numerator', 'denominator', 'definition'):
            if req_k not in cobj:
                p.append(f'{where}: missing required key {req_k!r}')
        d_ref = cobj.get('denominator')
        num_v = cobj.get('numerator')
        val_v = cobj.get('value')
        defn = cobj.get('definition')
        if d_ref not in den:
            p.append(f'{where}: unknown denominator {d_ref!r}')
        else:
            max_n = den[d_ref]['n']
            if not isinstance(num_v, int) or isinstance(num_v, bool) or not (0 <= num_v <= max_n):
                p.append(f'{where}: numerator {num_v!r} outside 0..{max_n}')
            if val_v != num_v:
                p.append(f'{where}: value {val_v!r} disagrees with numerator {num_v!r}')
        if not isinstance(defn, str) or not defn.strip():
            p.append(f'{where}: empty or missing definition')

    # Exact recount parity of summary.json from records.json + scope.
    rf = ((summary.get('tables') or {}).get('retrieval_failures') or {})
    recount = summarize(
        records,
        {
            'unkeyed_lines': rf.get('unkeyed_archive_lines', []),
            'unrequested_records': rf.get('unrequested_records', []),
        },
        scope,
    )
    for key in ('scope', 'denominators', 'metrics', 'state_breakdowns', 'continuous_distributions', 'tables'):
        if recount[key] != summary.get(key):
            p.append(f'summary.json {key} does not match recount from records.json')

    return p


# ---------------------------------------------------------------- atomic staging & write

def replace_refusal(out: Path, expected_names: set[str], id_prefix: str = 'outcomes-comparability-') -> str | None:
    """Refuse to overwrite `out` unless it is absent, empty, or already a complete family with
    matching `run_id` prefix."""
    if not out.exists() and not out.is_symlink():
        return None
    if out.is_symlink() or not out.is_dir():
        return f'{out} is not a directory'
    entries = list(out.iterdir())
    if not entries:
        return None
    odd = sorted(p.name for p in entries if p.is_symlink() or not p.is_file() or p.name not in expected_names)
    if odd:
        return f'{out} holds unexpected entries ({odd[:5]})'
    missing = sorted(expected_names - {p.name for p in entries})
    if missing:
        return f'{out} lacks {missing[:5]} (not a complete comparability family)'
    try:
        run = json.loads((out / 'run.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return f'{out}/run.json is not readable JSON'
    rid = run.get('run_id') if isinstance(run, dict) else None
    if not (isinstance(rid, str) and rid.startswith(id_prefix)):
        return f'{out}/run.json does not name an {id_prefix}* run'
    return None


def write(files: dict[str, Any], out: Path) -> None:
    """Atomically stage and swap `run.json`, `summary.json`, and `records.json` into `out`."""
    out = out.resolve()
    refusal = replace_refusal(out, {'run.json', 'summary.json', 'records.json'})
    if refusal:
        raise BuildError(f'{refusal}; refusing to replace it')
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=out.name + '.staging.', dir=out.parent))
    try:
        for name, obj in files.items():
            if name != 'run.json':
                (stage / name).write_bytes(dump(obj))
        (stage / 'run.json').write_text(
            json.dumps(files['run.json'], indent=1, ensure_ascii=False, sort_keys=True) + '\n',
            encoding='utf-8',
        )
        old: Path | None = None
        if out.exists():
            old = Path(tempfile.mkdtemp(prefix=out.name + '.replaced.', dir=out.parent))
            out.rename(old / out.name)
        try:
            stage.rename(out)
        except BaseException:
            if old is not None and not out.exists():
                (old / out.name).rename(out)
                old.rmdir()
            raise
        if old is not None:
            shutil.rmtree(old)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def load_dir(out: Path) -> dict[str, Any]:
    return {
        name: json.loads((out / name).read_text(encoding='utf-8'))
        for name in ('run.json', 'summary.json', 'records.json')
        if (out / name).is_file()
    }
