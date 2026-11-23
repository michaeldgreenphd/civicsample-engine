#!/usr/bin/env python3
"""Build the trial comparability, estimand, attrition & outcome-fidelity artifacts (manual only).

Examples:
  python3 scripts/build_outcomes_comparability.py --records-dir PATH --snapshot-date YYYY-MM-DD \
      --out build/outcomes_comparability
  python3 scripts/build_outcomes_comparability.py --archive build/outcomes_comparability_cache/NAME.jsonl.gz \
      --out build/outcomes_comparability
  python3 scripts/build_outcomes_comparability.py --responses-cache PATH --out build/outcomes_comparability
  python3 scripts/build_outcomes_comparability.py --fetch --nct-list ids.txt --out build/outcomes_comparability

Optional flags:
  --history-archive PATH   frozen jsonl.gz keyed by nct_id with historical primaryOutcomes
  --nct-list FILE          subset/target NCT IDs to include
  --exclude-list FILE      frame NCT IDs excluded from this build (requires --exclude-reason)
  --exclude-reason TEXT    explicit reason for excluded frame records
  --snapshot-date DATE     YYYY-MM-DD date the local records were retrieved: required for
                           --records-dir, and for --responses-cache when any line lacks fetched_at
                           (registry post dates are never used in its place)

Runs comparability_build.check() before writing anything; stages atomically so a failed run never
corrupts or partially overwrites an existing output directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from outcomes import comparability_build as B  # noqa: E402
from outcomes import outcome_history as OH  # noqa: E402

PRODUCER_PATHS = (
    'outcomes/comparability.py',
    'outcomes/outcome_history.py',
    'outcomes/comparability_build.py',
    'outcomes/COMPARABILITY.md',
    'outcomes/acquire.py',
    'scripts/build_outcomes_comparability.py',
)


def source_digest(root: Path = ROOT) -> str:
    """SHA-256 over sorted producer source files as they stand on disk."""
    h = hashlib.sha256()
    for rel in sorted(PRODUCER_PATHS):
        h.update(rel.encode('utf-8') + b'\0' + (root / rel).read_bytes() + b'\0')
    return h.hexdigest()


def producer(root: Path = ROOT) -> dict[str, Any]:
    """Current repository commit, dirty flag over `PRODUCER_PATHS`, and `source_sha256`."""
    commit_proc = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True, text=True, check=False
    )
    status_proc = subprocess.run(
        ['git', 'status', '--porcelain', '--', *PRODUCER_PATHS],
        cwd=root, capture_output=True, text=True, check=False,
    )
    commit = commit_proc.stdout.strip() if commit_proc.returncode == 0 and commit_proc.stdout.strip() else None
    dirty = bool(status_proc.stdout.strip()) if status_proc.returncode == 0 else False
    return {
        'repository': 'civicsample-engine',
        'commit': commit,
        'uncommitted_changes': dirty,
        'source_sha256': source_digest(root),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--fetch', action='store_true', help='fetch full CT.gov API v2 records for --nct-list')
    src.add_argument('--archive', type=Path, help='reuse a frozen .jsonl.gz archive verified against .meta.json')
    src.add_argument('--records-dir', type=Path, help='load stored per-trial .json.gz / .json CT.gov v2 study records')
    src.add_argument('--responses-cache', type=Path, help='load frozen CT.gov API v2 batch responses .jsonl.gz')

    ap.add_argument('--nct-list', type=Path, help='text file of included NCT IDs (one per line)')
    ap.add_argument('--exclude-list', type=Path, help='text file of excluded frame NCT IDs')
    ap.add_argument('--exclude-reason', help='stated reason for --exclude-list')
    ap.add_argument('--history-archive', type=Path, help='optional frozen jsonl.gz of historical primaryOutcomes')
    ap.add_argument(
        '--snapshot-date',
        help='YYYY-MM-DD retrieval date; required for --records-dir, and for --responses-cache lines without fetched_at',
    )
    ap.add_argument('--out', type=Path, default=ROOT / 'build' / 'outcomes_comparability')
    args = ap.parse_args(argv)

    if (args.exclude_list is None) != (args.exclude_reason is None):
        ap.error('--exclude-list and --exclude-reason must be supplied together')
    if args.exclude_list and (args.fetch or not args.nct_list):
        ap.error('--exclude-list requires a local source (--archive, --records-dir, or --responses-cache) and --nct-list')
    if args.snapshot_date is not None and B.calendar_date(args.snapshot_date) is None:
        ap.error(f'--snapshot-date {args.snapshot_date!r} is not a real YYYY-MM-DD calendar date')

    try:
        # Validate optional historical archive upfront before making any network calls.
        history_snapshots: dict[str, dict[str, Any]] | None = None
        history_meta: dict[str, Any] | None = None
        if args.history_archive is not None:
            history_snapshots, history_meta = OH.load_history_archive(args.history_archive)

        studies_iter: Any
        if args.fetch:
            if not args.nct_list:
                ap.error('--fetch requires --nct-list')
            requested = B.read_nct_list(args.nct_list)
            if not requested:
                raise B.BuildError(f'{args.nct_list.name} lists no NCT ids; nothing to fetch')
            lines, rec = B.fetch(requested)
            cache_path = ROOT / 'build' / 'outcomes_comparability_cache' / B.archive_name(lines, rec, requested)
            if cache_path.exists():
                meta = B.read_archive_meta(cache_path)
                expected = json.loads(json.dumps({**B.fetch_record(lines, rec, requested), 'archive': cache_path.name}))
                if meta != expected:
                    raise B.BuildError(
                        f'{cache_path.name} already exists with differing metadata; move it aside and retry'
                    )
            else:
                meta = B.write_archive(lines, cache_path, rec, requested)
            del lines
            studies_iter = B.iter_archive(cache_path)
            frame_ids = list(meta.get('requested_ids') or requested)
            source_label = 'fetch'
        elif args.archive is not None:
            meta = B.read_archive_meta(args.archive)
            frame_ids = list(meta.get('requested_ids') or [])
            requested = B.read_nct_list(args.nct_list) if args.nct_list else frame_ids
            if not requested:
                raise B.BuildError('archive metadata lists no requested ids; supply --nct-list')
            studies_iter = B.iter_archive(args.archive)
            source_label = 'archive'
        elif args.records_dir is not None:
            loaded_studies, meta = B.load_records_dir(args.records_dir, snapshot_date=args.snapshot_date)
            frame_ids = list(meta['requested_ids'])
            requested = B.read_nct_list(args.nct_list) if args.nct_list else frame_ids
            if not requested:
                raise B.BuildError('--nct-list is empty')
            studies_iter = loaded_studies
            source_label = 'records_dir'
        else:
            loaded_studies, meta = B.load_responses_cache(args.responses_cache, snapshot_date=args.snapshot_date)
            frame_ids = list(meta['requested_ids'])
            requested = B.read_nct_list(args.nct_list) if args.nct_list else frame_ids
            if not requested:
                raise B.BuildError('--nct-list is empty')
            studies_iter = loaded_studies
            source_label = 'responses_cache'

        not_retrieved = {x['nct_id']: x['reason'] for x in meta.get('not_retrieved') or []}
        excluded = B.read_nct_list(args.exclude_list) if args.exclude_list else None
        scope = B.build_scope(frame_ids, requested, excluded, args.exclude_reason)
        records, problems = B.build_records(
            studies_iter,
            requested,
            not_retrieved,
            history_snapshots=history_snapshots,
            excluded=frozenset(excluded or ()),
        )
        parameters: dict[str, Any] = {
            'source': source_label,
            'nct_list': args.nct_list.name if args.nct_list else None,
            'nct_list_sha256': B.sha256(args.nct_list.read_bytes()) if args.nct_list else None,
            'n_requested': len(requested),
            'exclude_list': args.exclude_list.name if args.exclude_list else None,
            'exclude_list_sha256': B.sha256(args.exclude_list.read_bytes()) if args.exclude_list else None,
            'exclude_reason': args.exclude_reason,
            'history_archive': args.history_archive.name if args.history_archive else None,
            'batch_size': B.BATCH,
            'min_seconds_between_requests': B.MIN_DELAY,
        }
        files = B.build(
            records,
            problems,
            meta,
            producer(),
            parameters,
            scope,
            history_archive_meta=history_meta,
        )
    except (B.BuildError, OH.HistoryArchiveError) as exc:
        print(f'refused: {exc}', file=sys.stderr)
        return 1

    contract_problems = B.check(files)
    if contract_problems:
        print('contract problems; nothing written:', *contract_problems, sep='\n  ', file=sys.stderr)
        return 1

    try:
        B.write(files, args.out)
    except B.BuildError as exc:
        print(f'refused: {exc}', file=sys.stderr)
        return 1

    den = files['summary.json']['denominators']
    sc = files['run.json']['scope']
    print(f"scope: frame {sc['frame']['n']}, included {sc['included']['n']}, excluded {sc['excluded']['n']}")
    print(
        f"{files['run.json']['run_id']}: {den['requested_trials']['n']} requested, "
        f"{den['retrieved_trials']['n']} retrieved, {den['posted_results_trials']['n']} posted results, "
        f"{den['primary_measures']['n']} primary measures -> {args.out}"
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
