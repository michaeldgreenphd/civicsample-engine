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

Required:
  --holdout-index PATH     the site's outcomes index (clinical-trial-populations data/outcomes/index.json).
                           A trial whose in_audit is true there is an audit holdout and is excluded: it is
                           dropped from --nct-list, --exclude-list and the input's frame before anything is
                           fetched or read, its records and history lines are dropped by id before any
                           per-trial parse, and run.json holdout_exclusion records how many were removed
                           (src/holdout_exclusion.py). There is no default.

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
from src import holdout_exclusion as HX  # noqa: E402

PRODUCER_PATHS = (
    'outcomes/comparability.py',
    'outcomes/outcome_history.py',
    'outcomes/comparability_build.py',
    'outcomes/COMPARABILITY.md',
    'outcomes/acquire.py',
    'scripts/build_outcomes_comparability.py',
    # The holdout exclusion decides which trials are built (and the guard supplies its id check).
    'src/holdout_exclusion.py',
    'src/holdout_guard.py',
)


def source_digest(root: Path = ROOT) -> str:
    """SHA-256 over sorted producer source files as they stand on disk."""
    h = hashlib.sha256()
    for rel in sorted(PRODUCER_PATHS):
        h.update(rel.encode('utf-8') + b'\0' + (root / rel).read_bytes() + b'\0')
    return h.hexdigest()


def producer(root: Path = ROOT) -> dict[str, Any]:
    """Current repository commit, dirty flag over `PRODUCER_PATHS`, and `source_sha256`.

    `uncommitted_changes` is True or False only when git could read both the commit and the status
    of `PRODUCER_PATHS`. When git is missing, the source has no `.git`, or either probe fails, the
    commit is None and `uncommitted_changes` is None (unverifiable), never False: a build from an
    unidentified or unverifiable source must not read as clean. Such a build may still be written for
    local diagnostics; the site exporter refuses it (it accepts True and False). Same convention as
    the feasibility and DAP builds.
    """
    commit: str | None = None
    dirty: bool | None = None
    try:
        commit_proc = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True, text=True, check=False
        )
        if commit_proc.returncode == 0 and commit_proc.stdout.strip():
            commit = commit_proc.stdout.strip()
            status_proc = subprocess.run(
                ['git', 'status', '--porcelain', '--', *PRODUCER_PATHS],
                cwd=root, capture_output=True, text=True, check=False,
            )
            if status_proc.returncode == 0:
                dirty = bool(status_proc.stdout.strip())
    except OSError:
        commit, dirty = None, None
    return {
        'repository': 'civicsample-engine',
        'commit': commit,
        'uncommitted_changes': dirty,
        'source_sha256': source_digest(root),
    }


def main(argv: list[str] | None = None) -> int:
    """Build the comparability artifact family; in_audit holdouts are excluded from every universe.

    Drop, never refuse, for every entry point that names trials (`--nct-list`, `--exclude-list`, the
    archive's or cache's frame, a records directory): a holdout id is removed and counted, so the
    stamp reports how many left the universe; a universe left empty is refused. The holdout index is
    read before anything else, the history archive included.
    """
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
    ap.add_argument(
        '--holdout-index', type=Path, required=True,
        help="the site's outcomes index (data/outcomes/index.json); in_audit trials are excluded. Required",
    )
    args = ap.parse_args(argv)

    if (args.exclude_list is None) != (args.exclude_reason is None):
        ap.error('--exclude-list and --exclude-reason must be supplied together')
    if args.exclude_list and (args.fetch or not args.nct_list):
        ap.error('--exclude-list requires a local source (--archive, --records-dir, or --responses-cache) and --nct-list')
    if args.snapshot_date is not None and B.calendar_date(args.snapshot_date) is None:
        ap.error(f'--snapshot-date {args.snapshot_date!r} is not a real YYYY-MM-DD calendar date')

    try:
        exclusion = HX.HoldoutExclusion(HX.load_holdout_index(args.holdout_index))
    except HX.HoldoutIndexError as exc:
        print(f'refused: {exc}', file=sys.stderr)
        return 1
    archive_dropped = [0]  # archive lines dropped as holdouts (`B.iter_archive`)

    def listed(path: Path) -> list[str]:
        """An NCT list without its holdouts, which are counted as removed from the universe."""
        return exclusion.exclude_ids(B.read_nct_list(path))

    try:
        # Validate optional historical archive upfront before making any network calls.
        history_snapshots: dict[str, dict[str, Any]] | None = None
        history_meta: dict[str, Any] | None = None
        if args.history_archive is not None:
            history_snapshots, history_meta = OH.load_history_archive(args.history_archive, exclusion)

        studies_iter: Any
        if args.fetch:
            if not args.nct_list:
                ap.error('--fetch requires --nct-list')
            requested = listed(args.nct_list)
            if not requested and exclusion.n_excluded:
                raise B.BuildError(f'{args.nct_list.name} lists only in_audit holdout trials, which are excluded')
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
            studies_iter = B.iter_archive(cache_path, exclusion)
            frame_ids = exclusion.exclude_ids(meta.get('requested_ids') or requested)
            source_label = 'fetch'
        elif args.archive is not None:
            meta = B.read_archive_meta(args.archive)
            # An archive acquired before the exclusion may list and hold holdouts: they leave its frame
            # here (counted) and its lines are dropped by id as they are read (`B.iter_archive`).
            frame_ids = exclusion.exclude_ids(meta.get('requested_ids') or [])
            requested = listed(args.nct_list) if args.nct_list else frame_ids
            if not requested and exclusion.n_excluded:
                raise B.BuildError('every requested trial is an in_audit holdout trial, which is excluded')
            if not requested:
                raise B.BuildError('archive metadata lists no requested ids; supply --nct-list')
            studies_iter = B.iter_archive(args.archive, exclusion, archive_dropped)
            source_label = 'archive'
        elif args.records_dir is not None:
            loaded_studies, meta = B.load_records_dir(
                args.records_dir, snapshot_date=args.snapshot_date, exclusion=exclusion
            )
            frame_ids = list(meta['requested_ids'])
            requested = listed(args.nct_list) if args.nct_list else frame_ids
            if not requested:
                raise B.BuildError('--nct-list is empty (or lists only in_audit holdout trials)')
            studies_iter = loaded_studies
            source_label = 'records_dir'
        else:
            loaded_studies, meta = B.load_responses_cache(
                args.responses_cache, snapshot_date=args.snapshot_date, exclusion=exclusion
            )
            frame_ids = list(meta['requested_ids'])
            requested = listed(args.nct_list) if args.nct_list else frame_ids
            if not requested:
                raise B.BuildError('--nct-list is empty (or lists only in_audit holdout trials)')
            studies_iter = loaded_studies
            source_label = 'responses_cache'

        # A frame or not_retrieved list written before the exclusion may still name a holdout.
        not_retrieved = {
            x['nct_id']: x['reason'] for x in meta.get('not_retrieved') or [] if not exclusion.is_holdout(x['nct_id'])
        }
        excluded = listed(args.exclude_list) if args.exclude_list else None
        scope = B.build_scope(frame_ids, requested, excluded, args.exclude_reason)
        records, problems = B.build_records(
            studies_iter,
            requested,
            not_retrieved,
            history_snapshots=history_snapshots,
            excluded=frozenset(excluded or ()),
        )
        if source_label == 'archive':
            # The archive's own metadata described the frame as frozen; what is published describes
            # the frame built: the holdouts' ids and lines are left out of its lists and counts (the
            # hashes still identify the archive file as given).
            meta = {
                **meta,
                'requested_ids': frame_ids,
                'n_requested': len(frame_ids),
                'not_retrieved': [{'nct_id': n, 'reason': r} for n, r in sorted(not_retrieved.items())],
                'n_records': meta['n_records'] - archive_dropped[0],
            }
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
            holdout_exclusion=exclusion.stamp(),
        )
    except (B.BuildError, OH.HistoryArchiveError, HX.HoldoutIndexError) as exc:
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

    prod = files['run.json']['producer']
    if prod['commit'] is None or prod['uncommitted_changes'] is None:
        print(
            f"note: producer commit {prod['commit']!r}, uncommitted_changes {prod['uncommitted_changes']!r}; "
            'written for local use only, the site exporter refuses a build whose commit or working-tree '
            'state is unknown',
            file=sys.stderr,
        )
    den = files['summary.json']['denominators']
    sc = files['run.json']['scope']
    print(f"scope: frame {sc['frame']['n']}, included {sc['included']['n']}, excluded {sc['excluded']['n']}")
    print(f"holdouts: {exclusion.n_excluded} in_audit holdout trial(s) excluded "
          f"({exclusion.index.n_holdouts} in the index)")
    print(
        f"{files['run.json']['run_id']}: {den['requested_trials']['n']} requested, "
        f"{den['retrieved_trials']['n']} retrieved, {den['posted_results_trials']['n']} posted results, "
        f"{den['primary_measures']['n']} primary measures -> {args.out}"
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
