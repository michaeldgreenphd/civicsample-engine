#!/usr/bin/env python3
"""Cut a verified drug-disclosure build down to lean static site files with atomic staging.

Usage:
  python3 scripts/export_drug_disclosure_site.py build/disclosure /tmp/site_disclosure

Verifies the input build against its `run.json` sha256 manifest and producer contract before
selecting any file. `summary.json` is carried forward unchanged apart from `run_id` so the site
computes zero aggregates. Set-aside rule rejections go to `evidence_set_aside.json` for on-demand
loading. Writes via a sibling `.staging.` directory and refuses to overwrite any non-empty
directory that is not already a complete `disclosure-` export.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from disclosure import artifacts as ART  # noqa: E402
from disclosure import evidence as E  # noqa: E402

SITE_SCHEMA_VERSION = 1
SITE_FILES = (
    'summary.json',
    'trials.json',
    'evidence.json',
    'evidence_set_aside.json',
    'sponsors.json',
)

SOURCE_SUMMARY_KEYS = (
    'source',
    'n_records',
    'first_fetched_at',
    'last_fetched_at',
    'api_version',
    'data_timestamp',
    'data_timestamp_first',
    'data_timestamp_last',
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dump(obj: Any) -> bytes:
    return ART.dump(obj)


class _StringTable:
    """Deduplicated string table preserving deterministic order of first appearance."""

    def __init__(self) -> None:
        self.values: list[str] = []
        self._idx: dict[str, int] = {}

    def index(self, text: str) -> int:
        if text not in self._idx:
            self._idx[text] = len(self.values)
            self.values.append(text)
        return self._idx[text]


def read_verified_build(build_dir: Path) -> dict[str, Any]:
    """Load `build_dir` and enforce full manifest sha256 and contract verification."""
    files, raw_hashes = ART.load_dir(build_dir)
    problems = ART.check_contract(files, raw_file_hashes=raw_hashes)
    if problems:
        raise ValueError(f'{build_dir} failed contract check ({len(problems)} problem(s)): {problems[0]}')
    return files


def site_run_id(run: dict[str, Any]) -> str:
    """Append an 8-hex digest of the build's file manifest to `run['run_id']`."""
    digest = sha256_bytes(json.dumps(sorted(run['files'].items())).encode('utf-8'))
    return f"{run['run_id']}-{digest[:8]}"


def _slim_evidence_file(links: list[dict[str, Any]]) -> dict[str, Any]:
    reasons = _StringTable()
    records: list[dict[str, Any]] = []
    for lnk in links:
        records.append({
            'link_id': lnk['link_id'],
            'nct_id': lnk['nct_id'],
            'source_route': lnk['source_route'],
            'target_id_type': lnk['target_id_type'],
            'target_id': lnk['target_id'],
            'citation_type': lnk['citation_type'],
            'publication_or_action_date': lnk['publication_or_action_date'],
            'date_precision': lnk['date_precision'],
            'verbatim_excerpt': lnk['verbatim_excerpt'],
            'source_sha256': lnk['source_sha256'],
            'locator': lnk['locator'],
            'automated_validation': lnk['automated_validation'],
            'validation_reason_indices': [reasons.index(r) for r in lnk['validation_reasons']],
            'review_status': lnk['review_status'],
            'review': lnk['review'],
        })
    return {
        'reasons': reasons.values,
        'records': records,
    }


def select_site_files(build: dict[str, Any]) -> dict[str, Any]:
    """Construct site export payloads and verify per-trial tallies against `summary.json`."""
    run = build['run.json']
    summary = build['summary.json']
    studies_by_id = {s['nct_id']: s for s in build['studies.json']['studies']}
    cov_by_id = {c['nct_id']: c for c in build['coverage.json']['trials']}
    tl_by_id = {t['nct_id']: t for t in build['timelines.json']['timelines']}
    links = build['links.json']['links']

    links_by_nct: dict[str, list[dict[str, Any]]] = {}
    for lnk in links:
        links_by_nct.setdefault(lnk['nct_id'], []).append(lnk)

    route_order = list(run['vocabularies']['coverage_routes'])
    av_order = list(E.AUTOMATED_VALIDATION)
    coverage_reasons = _StringTable()

    trial_rows: list[dict[str, Any]] = []
    summed_av = Counter()
    for nct_id in sorted(studies_by_id):
        st = studies_by_id[nct_id]
        cov = cov_by_id[nct_id]
        tl = tl_by_id[nct_id]
        own_links = links_by_nct.get(nct_id, [])
        av_counter = Counter(x['automated_validation'] for x in own_links)
        summed_av.update(av_counter)
        cells = {c['route']: c for c in cov['routes']}
        trial_rows.append({
            'nct_id': nct_id,
            'registry_state': st['registry_state'],
            'brief_title': st['brief_title'],
            'overall_status': st['overall_status'],
            'study_type': st['study_type'],
            'phases': st['phases'],
            'conditions': st['conditions'],
            'drug_candidates': st['drug_candidates'],
            'enrollment_count': st['enrollment_count'],
            'enrollment_type': st['enrollment_type'],
            'has_results': st['has_results'],
            'sponsor': tl['sponsor'],
            'milestones': tl['milestones'],
            'intervals': tl['intervals'],
            'qc_lag': tl['qc_lag'],
            'submission_infos': tl['submission_infos'],
            'window_365d': tl['window_365d'],
            'discordance': tl['discordance'],
            'coverage_overall': cov['overall']['state'],
            'coverage_overall_routes': cov['overall']['routes'],
            'coverage': [[cells[r]['state'], coverage_reasons.index(cells[r]['reason'])] for r in route_order],
            'evidence_counts_by_validation': [av_counter.get(av, 0) for av in av_order],
        })

    # Verify per-trial tallies sum identically to `summary.json`
    summary_av_table = summary['tables']['links_by_automated_validation']
    for av in av_order:
        expected_n = summary_av_table[av]['numerator']
        if summed_av.get(av, 0) != expected_n:
            raise ValueError(
                f'tally mismatch on {av}: trial rows sum to {summed_av.get(av, 0)}, summary.json has {expected_n}'
            )
    if sum(summed_av.values()) != summary['denominators']['evidence_links']['n']:
        raise ValueError('trial evidence counts do not sum to summary.json evidence_links denominator')

    s_rid = site_run_id(run)
    stamp = {'schema_version': SITE_SCHEMA_VERSION, 'run_id': s_rid}

    shown = [x for x in links if x['automated_validation'] != 'rejected_by_rule']
    set_aside = [x for x in links if x['automated_validation'] == 'rejected_by_rule']

    raw_sources = run.get('sources') or {}
    slim_sources = {
        name: ({k: src.get(k) for k in SOURCE_SUMMARY_KEYS if k in src} if isinstance(src, dict) else None)
        for name, src in sorted(raw_sources.items())
    }
    curated_in = ((run.get('inputs') or {}).get('curated') or {})

    site_files: dict[str, Any] = {
        'summary.json': {**summary, 'run_id': s_rid},
        'trials.json': {
            **stamp,
            'as_of': run['as_of'],
            'coverage_routes': route_order,
            'coverage_reasons': coverage_reasons.values,
            'automated_validation_order': av_order,
            'trials': trial_rows,
        },
        'evidence.json': {**stamp, **_slim_evidence_file(shown)},
        'evidence_set_aside.json': {**stamp, **_slim_evidence_file(set_aside)},
        'sponsors.json': {**build['sponsors.json'], 'run_id': s_rid},
    }
    site_files['run.json'] = {
        **stamp,
        'build_run_id': run['run_id'],
        'build_run_identity': run['run_identity'],
        'build_schema_version': run['schema_version'],
        'generated_at': run['generated_at'],
        'as_of': run['as_of'],
        'as_of_source': run['as_of_source'],
        'scheduled': False,
        'method_version': run['method_version'],
        'producer': run['producer'],
        'counting_units': run['counting_units'],
        'vocabularies': run['vocabularies'],
        'sources': slim_sources,
        'curated': {
            name: {'n_rows': meta.get('n_rows'), 'sha256': meta.get('sha256')}
            for name, meta in sorted(curated_in.items())
        },
        'reviews': {
            'n_rows': run['reviews']['n_rows'],
            'n_applied': run['reviews']['n_applied'],
            'n_orphaned': len(run['reviews']['orphaned_link_ids']),
            'n_out_of_scope': len(run['reviews']['out_of_scope_link_ids']),
        },
        'records': {
            'trials.json': len(trial_rows),
            'evidence.json': len(shown),
            'evidence_set_aside.json': len(set_aside),
        },
        'build_files': run['files'],
    }
    return site_files


def export_site(build_dir: Path, out_dir: Path) -> dict[str, Any]:
    """Verify `build_dir`, stage site files beside `out_dir`, and atomically swap `out_dir`."""
    site_files = select_site_files(read_verified_build(build_dir))
    expected_names = set(SITE_FILES) | {'run.json'}
    refusal = ART.replace_refusal(out_dir, expected_names, run_prefix='disclosure-')
    if refusal:
        raise ValueError(f'{refusal}; refusing to replace it')

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=out_dir.name + '.staging.', dir=out_dir.parent))
    try:
        digests: dict[str, str] = {}
        for name in SITE_FILES:
            raw = dump(site_files[name])
            (stage / name).write_bytes(raw)
            digests[name] = sha256_bytes(raw)
        manifest = {**site_files['run.json'], 'files': digests}
        manifest_text = json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + '\n'
        (stage / 'run.json').write_text(manifest_text, encoding='utf-8')

        old: Path | None = None
        if out_dir.exists():
            old = Path(tempfile.mkdtemp(prefix=out_dir.name + '.replaced.', dir=out_dir.parent))
            out_dir.rename(old / out_dir.name)
        try:
            stage.rename(out_dir)
        except OSError:
            if old is not None and (old / out_dir.name).exists():
                (old / out_dir.name).rename(out_dir)
                old.rmdir()
            raise
        if old is not None:
            shutil.rmtree(old)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('build', type=Path, help='Verified drug-disclosure build directory')
    ap.add_argument('out', type=Path, help='Target site export directory (e.g. data/disclosure)')
    args = ap.parse_args(argv)
    try:
        manifest = export_site(args.build, args.out)
    except (OSError, ValueError) as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        return 1

    total = 0
    for name in sorted(p.name for p in args.out.iterdir()):
        sz = (args.out / name).stat().st_size
        total += sz
        print(f'{sz:>10,}  {name}')
    print(f'{total:>10,}  total; wrote {args.out} ({manifest["run_id"]})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
