"""American Community Survey pulls: which product, how it is fetched, and how tables are read.

Product choice. The benchmark needs sex by age by race for every county and ZIP Code Tabulation
Area a site can fall in. Among Census Bureau products:
  * ACS 5-year estimates are the only ACS product published for every county and ZCTA. An
    estimate pools 60 months of interviews and describes that whole period; "2020-2024" is not
    a count for 2024. Each carries a 90% margin of error.
  * ACS 1-year estimates cover only areas of 65,000 people or more, so they cannot serve ZCTAs
    or most counties; the standard 2020 1-year release was not published (experimental
    estimates only), and the 2016-2020 5-year file used revised weights for 2020 interviews.
  * The decennial census (2010 SF1, 2020 DHC) is a full count on one day with no sampling
    error; 2020 DHC adds disclosure-avoidance noise. It is not used here. It would be the better
    reference for trials enrolling close to April 2010 or April 2020, and is listed as future work.
The four windows in trials.WINDOWS are the non-overlapping 5-year files.

The API key is read from CENSUS_API_KEY and never written anywhere: recorded URLs go through
redact().
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import requests

API = 'https://api.census.gov/data/{year}/acs/acs5'
# Benchmark category -> ACS table. Race tables are "alone" groups plus Two or More Races, so
# they partition the population the way the registry's race question does. B01001I counts
# Hispanic or Latino residents of any race: ethnicity is a separate question on both sides.
TABLES: dict[str, str] = {
    'total': 'B01001', 'white': 'B01001A', 'black_african_american': 'B01001B',
    'american_indian_alaska_native': 'B01001C', 'asian': 'B01001D',
    'native_hawaiian_pacific_islander': 'B01001E', 'other': 'B01001F',
    'more_than_one_race': 'B01001G', 'hispanic_latino': 'B01001I'}
TABLE_ORDER = tuple(TABLES)
LEVELS: dict[str, str] = {
    'us': 'us:1', 'region': 'region:*', 'state': 'state:*', 'county': 'county:*',
    'cbsa': 'metropolitan statistical area/micropolitan statistical area:*',
    'zcta': 'zip code tabulation area:*'}
# ZCTA tables begin with the 2007-2011 file; the 2005-2009 window has none (the API answers
# "unsupported geography"). The benchmark is recorded as unavailable there, not substituted.
LEVELS_BY_VINTAGE = {2009: ('us', 'region', 'state', 'county', 'cbsa')}
SEXES = ('male', 'female')
# "-555555555" marks an estimate controlled to an independent count: its margin of error is zero
# by construction. Any other negative value is one of the survey's "not available" codes.
_CONTROLLED = -555555555


def redact(url: str) -> str:
    """The URL without the key, for anything that is logged or written."""
    return re.sub(r'([?&]key=)[^&]+', r'\1REDACTED', url)


def _key() -> str:
    key = os.environ.get('CENSUS_API_KEY')
    if not key:
        raise RuntimeError('CENSUS_API_KEY is not set; add it to the environment (never to a file)')
    return key


def age_span(label: str) -> tuple[float, float | None] | None:
    """'Under 5 years' -> (0, 5); '18 and 19 years' -> (18, 20); '85 years and over' -> (85, None)."""
    text = label.split('!!')[-1].strip().lower()
    if m := re.fullmatch(r'under (\d+) years?', text):
        return 0.0, float(m.group(1))
    if m := re.fullmatch(r'(\d+) (?:to|and) (\d+) years?', text):
        return float(m.group(1)), float(m.group(2)) + 1
    if m := re.fullmatch(r'(\d+) years? and over', text):
        return float(m.group(1)), None
    if m := re.fullmatch(r'(\d+) years?', text):
        return float(m.group(1)), float(m.group(1)) + 1
    return None


def bands(variables: dict[str, dict[str, Any]]) -> list[tuple[str, str, float, float | None]]:
    """(variable stem, sex, lo, hi) for every sex-by-age estimate cell, read from the table's own
    labels so a renumbered variable cannot be misread. Labels differ between files only in a
    trailing colon on each level."""
    out = []
    for name, meta in variables.items():
        if not name.endswith('E') or not meta.get('label', '').startswith('Estimate'):
            continue
        parts = [p.strip().rstrip(':').lower() for p in meta['label'].split('!!')]
        if len(parts) == 4 and parts[2] in SEXES and (span := age_span(parts[3])):
            out.append((name[:-1], parts[2], span[0], span[1]))
    out.sort(key=lambda b: (SEXES.index(b[1]), b[2]))
    per_sex = [b for b in out if b[1] == 'male']
    if not per_sex or len(out) != 2 * len(per_sex):
        raise ValueError('sex-by-age cells are not the same for both sexes')
    edges = [(b[2], b[3]) for b in per_sex]
    if edges[0][0] != 0 or edges[-1][1] is not None or any(a[1] != b[0] for a, b in zip(edges, edges[1:])):
        raise ValueError(f'age bands do not tile 0 to the open top band: {edges}')
    return out


def fetch(year: int, table: str, level: str, cache: Path, session: requests.Session | None = None) -> dict[str, Any]:
    """One whole table for one geography level, cached as served, with its provenance.

    Returns the metadata record (redacted URL, retrieval time, sha256 of the response body). A
    response that is not JSON is a failure whatever its status: the API answers a missing key
    with an HTML page and status 200."""
    cache.mkdir(parents=True, exist_ok=True)
    stem = cache / f'acs5_{year}_{table}_{level}'
    meta_path = stem.with_suffix('.meta.json')
    if meta_path.exists() and stem.with_suffix('.json.gz').exists():
        return json.loads(meta_path.read_text())
    session = session or requests.Session()
    base = API.format(year=year)
    out = {}
    for kind, url, params in (('variables', f'{base}/groups/{table}.json', {}),
                              ('data', base, {'get': f'group({table})', 'for': LEVELS[level], 'key': _key()})):
        for attempt in range(4):
            r = session.get(url, params=params, timeout=600)
            if r.status_code == 200:
                break
            time.sleep(2 ** attempt)
        try:
            body = r.json()
        except ValueError as exc:
            raise RuntimeError(f'{redact(r.url)}: status {r.status_code}, not JSON') from exc
        if r.status_code != 200:
            raise RuntimeError(f'{redact(r.url)}: status {r.status_code}')
        out[kind] = (body, redact(r.url), hashlib.sha256(r.content).hexdigest())
    payload = {'variables': out['variables'][0]['variables'], 'rows': out['data'][0]}
    with gzip.GzipFile(stem.with_suffix('.json.gz'), 'wb', mtime=0) as fh:
        fh.write(json.dumps(payload, separators=(',', ':')).encode())
    meta = {'product': 'ACS 5-year', 'window': f'{year - 4}-{year}', 'table': table, 'level': level,
            'url': out['data'][1], 'variables_url': out['variables'][1],
            'sha256_response': out['data'][2], 'sha256_variables': out['variables'][2],
            'retrieved_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'n_rows': len(out['data'][0]) - 1}
    meta_path.write_text(json.dumps(meta, indent=1) + '\n')
    return meta


class Level:
    """Every area of one level in one window: estimates and 90% margins of error as arrays
    [area, table, sex, band], on the race tables' age bands."""

    def __init__(self, geoids: list[str], names: list[str], edges: list[tuple[float, float | None]],
                 est: np.ndarray, moe: np.ndarray) -> None:
        self.index = {g: i for i, g in enumerate(geoids)}
        self.names, self.edges, self.est, self.moe = names, edges, est, moe


def _geoid(level: str, header: list[str], row: list[str]) -> str:
    cell = dict(zip(header, row))
    if level == 'county':
        return cell['state'] + cell['county']
    return cell[LEVELS[level].split(':')[0]]


def load(year: int, level: str, cache: Path) -> Level:
    """Read the cached tables for one level. The all-residents table has finer age bands than
    the race tables (23 against 14 per sex); it is summed onto the race tables' bands so one set
    of age weights applies to numerator and denominator alike."""
    tables = {}
    for cat, table in TABLES.items():
        with gzip.open(cache / f'acs5_{year}_{table}_{level}.json.gz') as fh:
            tables[cat] = json.load(fh)
    coarse = [b for b in bands(tables['white']['variables']) if b[1] == 'male']
    edges = [(b[2], b[3]) for b in coarse]
    header = tables['total']['rows'][0]
    geoids = [_geoid(level, header, row) for row in tables['total']['rows'][1:]]
    pos = {g: i for i, g in enumerate(geoids)}
    names = [dict(zip(header, row)).get('NAME', '') for row in tables['total']['rows'][1:]]
    est = np.full((len(geoids), len(TABLE_ORDER), 2, len(edges)), np.nan)
    moe = np.full_like(est, np.nan)
    for t, cat in enumerate(TABLE_ORDER):
        cells = bands(tables[cat]['variables'])
        hdr = tables[cat]['rows'][0]
        col = {name: i for i, name in enumerate(hdr)}
        # which coarse band each cell falls in (identity for the race tables)
        target = [next(k for k, (lo, hi) in enumerate(edges) if c[2] >= lo and (hi is None or (c[3] is not None and c[3] <= hi)))
                  for c in cells]
        for row in tables[cat]['rows'][1:]:
            i = pos.get(_geoid(level, hdr, row))
            if i is None:
                continue
            e = np.zeros((2, len(edges)))
            v = np.zeros((2, len(edges)))          # variance-like: squared margins add
            for c, k in zip(cells, target):
                raw_e, raw_m = row[col[c[0] + 'E']], row[col[c[0] + 'M']]
                s = SEXES.index(c[1])
                ev = float(raw_e) if raw_e not in (None, '') else np.nan
                mv = float(raw_m) if raw_m not in (None, '') else np.nan
                e[s, k] += ev if ev >= 0 else np.nan
                v[s, k] += 0.0 if mv == _CONTROLLED else (mv * mv if mv >= 0 else np.nan)
            est[i, t], moe[i, t] = e, np.sqrt(v)
    return Level(geoids, names, edges, est, moe)
