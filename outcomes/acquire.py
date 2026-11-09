"""Acquire the outcomes frame from the ClinicalTrials.gov API v2, or reuse a frozen archive.

The frame is fetched independently of the weekly extraction (which keeps only trials with
results and drops the document and status fields this beta needs). A fetch is written as a
frozen archive: one study per line, sorted by NCT id, gzip-compressed, with the sha256 of the
uncompressed bytes. A later build can reuse the archive (`--archive`) so that its inputs are
pinned; the run manifest records which was used.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

API = 'https://clinicaltrials.gov/api/v2/studies'
# protocolSection, documentSection and hasResults are all the evidence layer reads; the results
# section is not fetched (assessments come from the reviewed export, not from this path).
FIELDS = 'protocolSection,documentSection,hasResults'
PAGE_SIZE = 200


def fetch(query: str, get: Callable[..., Any] | None = None, delay: float = 0.3) -> list[dict[str, Any]]:
    """Every study matching the query, following page tokens to the end."""
    session = requests.Session()
    session.headers['User-Agent'] = 'civicsample-outcomes-beta'
    get = get or (lambda url, params: session.get(url, params=params, timeout=60))
    studies, token = [], None
    while True:
        params = {'query.term': query, 'fields': FIELDS, 'pageSize': PAGE_SIZE, 'format': 'json'}
        if token:
            params['pageToken'] = token
        r = get(API, params)
        r.raise_for_status()
        body = r.json()
        studies.extend(body.get('studies') or [])
        token = body.get('nextPageToken')
        if not token:
            return studies
        time.sleep(delay)


def encode(studies: list[dict[str, Any]]) -> bytes:
    """Canonical archive bytes (uncompressed): one sorted-key JSON object per line, by NCT id."""
    def nct(s: dict[str, Any]) -> str:
        return ((s.get('protocolSection') or {}).get('identificationModule') or {}).get('nctId') or ''
    ids = [nct(s) for s in studies]
    if '' in ids or len(set(ids)) != len(ids):
        raise ValueError('every study needs a distinct NCT id')
    return b''.join(json.dumps(s, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode() + b'\n'
                    for s in sorted(studies, key=nct))


def write_archive(studies: list[dict[str, Any]], path: Path, query: str) -> dict[str, Any]:
    raw = encode(studies)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(raw, mtime=0))   # mtime 0: the same records give the same bytes
    meta = {'source': 'clinicaltrials.gov API v2', 'query': query, 'fields': FIELDS,
            'retrieved_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'n_records': len(studies), 'sha256_uncompressed': hashlib.sha256(raw).hexdigest(),
            'archive': path.name}
    path.with_name(path.name + '.meta.json').write_text(json.dumps(meta, indent=1) + '\n')
    return meta


def read_archive(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Studies and metadata from a frozen archive; refuses bytes that do not match the metadata."""
    raw = gzip.decompress(path.read_bytes())
    meta_path = path.with_name(path.name + '.meta.json')
    if not meta_path.exists():
        raise ValueError(f'{path.name}: no metadata file beside the archive')
    meta = json.loads(meta_path.read_text())
    if hashlib.sha256(raw).hexdigest() != meta.get('sha256_uncompressed'):
        raise ValueError(f'{path.name}: bytes do not match the recorded sha256')
    studies = [json.loads(line) for line in raw.splitlines() if line]
    if len(studies) != meta.get('n_records'):
        raise ValueError(f'{path.name}: record count does not match the metadata')
    return studies, meta
