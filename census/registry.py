"""Registry text and locations for the cohort, fetched from ClinicalTrials.gov and frozen.

The weekly release keeps each site's ZIP but not the city coordinates, the per-location status
or the description text the single-site tier reads. They are fetched by NCT id and written as a
byte-stable archive with its sha256, like outcomes/acquire.py, so a later build can pin them.
This is the registration as it stands at retrieval: earlier versions of a record, where a site
list may have differed, are not examined.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

API = 'https://clinicaltrials.gov/api/v2/studies'
FIELDS = 'NCTId,DescriptionModule,OfficialTitle,BriefTitle,ContactsLocationsModule,StatusModule,EnrollmentInfo'
# The posted baseline tables, fetched only for trials whose race table is titled with ethnicity
# (see ethnicity_rows_in_race_tables). Kept as its own archive with its own sha256.
BASELINE_FIELDS = 'NCTId,BaselineCharacteristicsModule'
BATCH = 100


def fetch(ids: list[str], delay: float = 0.25, fields: str = FIELDS) -> list[dict[str, Any]]:
    session = requests.Session()
    session.headers['User-Agent'] = 'civicsample-census-benchmarks'
    out: list[dict[str, Any]] = []
    for i in range(0, len(ids), BATCH):
        params = {'filter.ids': ','.join(ids[i:i + BATCH]), 'fields': fields, 'pageSize': BATCH, 'format': 'json'}
        for attempt in range(5):
            r = session.get(API, params=params, timeout=120)
            if r.status_code == 200:
                break
            time.sleep(2 ** attempt)
        r.raise_for_status()
        out.extend(r.json().get('studies') or [])
        time.sleep(delay)
    return out


def _nct(study: dict[str, Any]) -> str:
    return ((study.get('protocolSection') or {}).get('identificationModule') or {}).get('nctId') or ''


def write_archive(studies: list[dict[str, Any]], path: Path, requested: int, fields: str = FIELDS) -> dict[str, Any]:
    raw = b''.join(json.dumps(s, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode() + b'\n'
                   for s in sorted(studies, key=_nct))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(raw, mtime=0))
    meta = {'source': 'clinicaltrials.gov API v2', 'fields': fields, 'n_requested': requested,
            'n_records': len(studies), 'sha256_uncompressed': hashlib.sha256(raw).hexdigest(),
            'retrieved_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'archive': path.name}
    path.with_name(path.name + '.meta.json').write_text(json.dumps(meta, indent=1) + '\n')
    return meta


def read_archive(path: Path, fields: str = FIELDS) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """{nct: study} and metadata; refuses bytes that do not match the recorded sha256."""
    raw = gzip.decompress(path.read_bytes())
    meta = json.loads(path.with_name(path.name + '.meta.json').read_text())
    if hashlib.sha256(raw).hexdigest() != meta.get('sha256_uncompressed') or meta.get('fields') != fields:
        raise ValueError(f'{path.name}: bytes or field set do not match the recorded metadata')
    return {_nct(s): s for s in map(json.loads, raw.splitlines())}, meta


def site_facts(study: dict[str, Any] | None) -> dict[str, Any]:
    """What the live registration adds for one trial: text for the tier, the city point, how
    many locations it lists today and where the only one is, and the enrolment type."""
    p = (study or {}).get('protocolSection') or {}
    locs = (p.get('contactsLocationsModule') or {}).get('locations') or []
    d = p.get('descriptionModule') or {}
    ident = p.get('identificationModule') or {}
    text = ' \n '.join(x for x in (ident.get('officialTitle'), ident.get('briefTitle'),
                                   d.get('briefSummary'), d.get('detailedDescription')) if x)
    point = (locs[0].get('geoPoint') or {}) if len(locs) == 1 else {}
    enrol = (p.get('designModule') or {}).get('enrollmentInfo') or {}
    # Where the one listed facility is today, so the build can tell whether it is still the
    # site the release recorded before it uses today's city point beside the release's ZIP.
    place = {k: locs[0].get(k) for k in ('city', 'state', 'zip', 'country')} if len(locs) == 1 else None
    return {'found': study is not None, 'text': text, 'n_locations_now': len(locs), 'place_now': place,
            'point': (point['lat'], point['lon']) if 'lat' in point and 'lon' in point else None,
            'location_status': locs[0].get('status') if len(locs) == 1 else None,
            'enrollment_type': enrol.get('type'), 'enrollment': enrol.get('count')}


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(',', ''))
    except (TypeError, ValueError):
        return None


def ethnicity_rows_in_race_tables(study: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """Ethnicity rows standing among race rows in a posted baseline table, as the registry has them.

    The weekly extractor reads a race table row by row and leaves a purely ethnicity row
    ("Hispanic", "Not Hispanic or Latino") to the ethnicity pipeline. The release therefore
    carries no trace of that row in the race block, and a table that classified people as
    White / Black / Hispanic arrives looking like a race table whose Hispanic participants are
    "unknown". Only the registry's own table shows the row, so it is read here.

    Uses the extractor's own two tests (which measure is a race table; which label is
    ethnicity-only), so this names exactly the rows the extractor dropped. A table whose rows
    are all ethnicity is not a race table with an ethnicity row and is left out. Returns None
    when no record was retrieved: not looked at is not the same as none found."""
    if study is None:
        return None
    from src.race_extractor import _is_ethnicity_only_label, is_race_table   # the pipeline's own rules
    found: list[dict[str, Any]] = []
    for measure in ((study.get('resultsSection') or {}).get('baselineCharacteristicsModule') or {}).get('measures') or []:
        if not is_race_table(measure.get('title') or ''):
            continue
        rows: list[tuple[str, float | None]] = []
        for cls in measure.get('classes') or []:
            for cat in cls.get('categories') or [{}]:
                label = (cat.get('title') or cls.get('title') or '').strip()
                counts = [n for n in (_number(m.get('value')) for m in cat.get('measurements') or []) if n is not None]
                if label:
                    rows.append((label, max(counts) if counts else None))   # the largest group is the total
        ethnic = [(label, n) for label, n in rows if _is_ethnicity_only_label(label)]
        if ethnic and len(ethnic) < len(rows):
            found += [{'measure_title': measure.get('title'), 'label': label, 'largest_count': n} for label, n in ethnic]
    return found
