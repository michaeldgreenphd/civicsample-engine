#!/usr/bin/env python3
"""Build the results-structure artifacts by hand (never scheduled; no workflow calls this).

  python scripts/build_results_structure.py --fetch --nct-list ids.txt \
      --out ../clinical-trial-populations/data/results_structure
  python scripts/build_results_structure.py --archive build/results_structure_cache/NAME.jsonl.gz \
      --out build/results_structure

  --fetch --nct-list FILE  fetch the full record of every listed NCT id from the ClinicalTrials.gov
                           API v2 (no key; one request at a time, at least 0.5 s apart) and freeze
                           them under build/results_structure_cache/ as a sorted, byte-stable
                           jsonl.gz with its sha256
  --archive PATH           reuse a frozen archive (its .meta.json must match its bytes); the
                           requested ids are the ones its metadata lists, unless --nct-list is given
  --exclude-list FILE      with --archive and --nct-list: the frame records this build leaves out.
                           Every record the archive was fetched for must then be in one list or
                           the other. Needs --exclude-reason.
  --exclude-reason TEXT    the one reason those records are left out; written to run.json and
                           summary.json with the frame, included and excluded counts
  --reviews PATH           human review records; default outcomes/subgroup_reviews.csv
  --out PATH               where to write run.json, summary.json and records.json; default
                           build/results_structure

Writes nothing unless the family passes results_structure_build.check. The three files are
written into a staging folder and swapped in, so a failed run leaves the previous family intact.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from outcomes import results_structure as RS  # noqa: E402
from outcomes import results_structure_build as B  # noqa: E402

DEFAULT_REVIEWS = ROOT / 'outcomes' / 'subgroup_reviews.csv'
# Every file whose code or content shapes the artifacts. outcomes/acquire.py is here because a
# fetch freezes its archive with acquire.encode: an uncommitted change there would otherwise
# produce the input while the manifest said nothing was uncommitted.
PRODUCER_PATHS = ('outcomes/results_structure.py', 'outcomes/results_structure_build.py', 'outcomes/acquire.py',
                  'outcomes/subgroup_reviews.csv', 'scripts/build_results_structure.py')


def producer() -> dict[str, object]:
    """The commit that produced the build, and whether the producing files differ from it."""
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True).stdout.strip() or None
    dirty = bool(subprocess.run(['git', 'status', '--porcelain', '--', *PRODUCER_PATHS], cwd=ROOT,
                                capture_output=True, text=True).stdout.strip())
    return {'repository': 'civicsample-engine', 'commit': commit, 'uncommitted_changes': dirty}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--fetch', action='store_true')
    src.add_argument('--archive', type=Path)
    ap.add_argument('--nct-list', type=Path)
    ap.add_argument('--exclude-list', type=Path)
    ap.add_argument('--exclude-reason')
    ap.add_argument('--reviews', type=Path, default=DEFAULT_REVIEWS)
    ap.add_argument('--out', type=Path, default=ROOT / 'build' / 'results_structure')
    args = ap.parse_args(argv)
    if (args.exclude_list is None) != (args.exclude_reason is None):
        ap.error('--exclude-list and --exclude-reason go together')
    if args.exclude_list and not (args.archive and args.nct_list):
        ap.error('--exclude-list needs --archive and --nct-list: a fetch asks only for the ids it will build')
    try:
        # The review file is read before any fetch, so a refused file costs no request.
        reviews = RS.load_reviews(args.reviews)
        reviews_file = {'file': args.reviews.name, 'sha256': hashlib.sha256(args.reviews.read_bytes()).hexdigest(),
                        'n_rows': sum(len(v) for v in reviews.values())}
        if args.fetch:
            if not args.nct_list:
                ap.error('--fetch needs --nct-list')
            requested = B.read_nct_list(args.nct_list)
            lines, record = B.fetch(requested)
            # Addressed by what was returned AND what was asked for: a later fetch never replaces
            # the archive an earlier build recorded, and a fetch for other ids never borrows its
            # metadata, even when both returned the same bytes (or both returned nothing).
            sha = hashlib.sha256(b''.join(lines[n] for n in sorted(lines))).hexdigest()
            asked = hashlib.sha256('\n'.join(sorted(requested)).encode()).hexdigest()
            path = (ROOT / 'build' / 'results_structure_cache'
                    / f"records-{record['retrieved_at'][:10]}-{sha[:12]}-{asked[:8]}.jsonl.gz")
            if path.exists():
                meta = B.read_archive_meta(path)
                if meta.get('requested_ids_sha256') != asked:
                    raise B.BuildError(f'{path.name} was written for a different list of ids; move it aside and fetch again')
            else:
                meta = B.write_archive(lines, path, record, requested)
            del lines
            print(f"fetched {meta['n_records']} of {meta['n_requested']} records in {meta['n_requests']} requests "
                  f"({meta['n_failed_requests']} failed) -> {path.relative_to(ROOT)}")
        else:
            path = args.archive
            meta = B.read_archive_meta(path)
            requested = B.read_nct_list(args.nct_list) if args.nct_list else list(meta.get('requested_ids') or [])
            if not requested:
                raise B.BuildError('the archive metadata lists no requested ids; give --nct-list')
        not_retrieved = {x['nct_id']: x['reason'] for x in meta.get('not_retrieved') or []}
        excluded = B.read_nct_list(args.exclude_list) if args.exclude_list else None
        # The frame is every id the archive was fetched for, whether or not the registry returned it.
        scope = B.build_scope(list(meta.get('requested_ids') or requested), requested, excluded, args.exclude_reason)
        records, problems = B.build_records(B.iter_archive(path), requested, not_retrieved, reviews,
                                            frozenset(excluded or ()))
        parameters = {'source': 'fetch' if args.fetch else 'archive',
                      'nct_list': args.nct_list.name if args.nct_list else None,
                      'nct_list_sha256': hashlib.sha256(args.nct_list.read_bytes()).hexdigest() if args.nct_list else None,
                      'n_requested': len(requested),
                      'exclude_list': args.exclude_list.name if args.exclude_list else None,
                      'exclude_list_sha256': hashlib.sha256(args.exclude_list.read_bytes()).hexdigest() if args.exclude_list else None,
                      'exclude_reason': args.exclude_reason, 'batch_size': B.BATCH,
                      'min_seconds_between_requests': B.MIN_DELAY}
        files = B.build(records, problems, meta, producer(), reviews_file, parameters, scope)
    except (B.BuildError, RS.ReviewError) as exc:
        print(f'refused: {exc}', file=sys.stderr)
        return 1
    found = B.check(files)
    if found:
        print('contract problems; nothing written:', *found, sep='\n  ', file=sys.stderr)
        return 1
    B.write(files, args.out)
    den = files['summary.json']['denominators']
    sc = files['run.json']['scope']
    print(f"scope: frame {sc['frame']['n']}, included {sc['included']['n']}, excluded {sc['excluded']['n']}")
    print(f"{files['run.json']['run_id']}: {den['requested']['n']} requested, {den['retrieved']['n']} retrieved, "
          f"{den['posted']['n']} with posted results; {files['summary.json']['signals']['n_signals']} signals -> {args.out}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
