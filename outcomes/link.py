"""Link outcome-cohort trials to the site's participant-demographics dataset.

Reads only record-contract fields (tests/record_contract.json in the site repository: nct_id,
the sex/race/ethnicity `reported` flags, and the fields generate_industry_sponsors.build_cohort
reads) from the site's latest dataset, so the link says which outcome trials also appear in the
demographics views and whether they are in the Industry tab's own cohort. It changes neither.

Only the inline layout (whole-record parts) is read. A split layout, a missing dataset or an
unreadable part makes the link NOT_COMPUTED with the reason, never an empty link.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

from outcomes.attribution import _industry_recode


def load_site_records(site: Path, wanted: set[str]) -> tuple[dict[str, dict[str, Any]] | None, dict[str, Any]]:
    """({nct_id: record} for wanted trials, meta) or (None, meta with the reason)."""
    data = site / 'data'
    run_path = data / 'run.json'
    if not run_path.exists():
        return None, {'state': 'NOT_COMPUTED', 'reason': 'the site checkout has no data/run.json'}
    run = json.loads(run_path.read_text())
    meta = {'site_snapshot_date': run.get('snapshot_date'), 'site_extracted_at': run.get('extracted_at'),
            'site_pipeline_commit': run.get('pipeline_commit')}
    parts = sorted(data.glob('demographics.part*.json.gz'), key=lambda p: int(p.name.split('part')[1].split('.')[0]))
    if not parts or len(parts) != run.get('total_parts'):
        return None, {**meta, 'state': 'NOT_COMPUTED',
                      'reason': 'the site dataset is not in the inline layout this link reads, or its parts are incomplete'}
    found: dict[str, dict[str, Any]] = {}
    for p in parts:
        part = json.loads(gzip.decompress(p.read_bytes()))
        if part.get('extracted_at') != run.get('extracted_at'):
            return None, {**meta, 'state': 'NOT_COMPUTED', 'reason': f'{p.name} is from another run'}
        for r in part.get('data') or []:
            if r.get('nct_id') in wanted:
                found[r['nct_id']] = r
    return found, {**meta, 'state': 'COMPUTED', 'n_site_parts': len(parts)}


def link(nct_ids: list[str], site: Path | None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """({nct_id: link}, summary). link: in_dataset, sex/race/ethnicity reported, in the Industry
    demographics cohort. With no site checkout, every link is None and the summary says why."""
    if site is None:
        return {n: None for n in nct_ids}, {'state': 'NOT_COMPUTED', 'reason': 'no site checkout given (--site)'}
    records, meta = load_site_records(site, set(nct_ids))
    if records is None:
        return {n: None for n in nct_ids}, meta
    gis = _industry_recode()
    industry = {r['nct_id'] for r in gis.build_cohort(list(records.values()))}
    out = {}
    for n in nct_ids:
        r = records.get(n)
        out[n] = {'in_dataset': r is not None,
                  'sex_reported': bool((r.get('sex') or {}).get('reported')) if r else None,
                  'race_reported': bool((r.get('race') or {}).get('reported')) if r else None,
                  'ethnicity_reported': bool((r.get('ethnicity') or {}).get('reported')) if r else None,
                  'in_industry_demographics_cohort': n in industry if r else False}
    summary = {**meta, 'n_in_dataset': sum(1 for v in out.values() if v['in_dataset']),
               'n_in_industry_demographics_cohort': sum(1 for v in out.values() if v['in_industry_demographics_cohort'])}
    return out, summary
