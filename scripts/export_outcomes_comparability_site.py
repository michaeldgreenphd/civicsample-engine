#!/usr/bin/env python3
"""Export a verified outcomes-comparability build to site files with atomic staging.

  python3 scripts/export_outcomes_comparability_site.py BUILD_DIR OUT_DIR --holdout-index INDEX

`--holdout-index` (required) is the site's outcomes index (data/outcomes/index.json). The export is
refused, `OUT_DIR` untouched, when the build's run.json has no valid `holdout_exclusion` stamp (a build
made before the in_audit holdout exclusion), when the stamp's index sha256 is not this index's (the
index changed since the build: rebuild), or when any file of the build, `records.json` included, names a
holdout id (`src/holdout_exclusion.py`). The site `run.json` carries the stamp (sha256 and counts only).

Reads only `BUILD_DIR` (offline; zero network calls), verifies every file listed in
`BUILD_DIR/run.json` by raw byte SHA-256 and runs `comparability_build.check()` before selecting
anything. Refuses tampered, partial, or inconsistent builds non-zero, and any build whose producer
`commit` is unknown or whose `uncommitted_changes` is null (unverifiable); `true` (a dirty working
tree, recorded honestly) and `false` are both exported.

Site files written (`SITE_SCHEMA_VERSION = 1`, staged atomically and verified by SHA-256):
  - `run.json`: site manifest carrying `run_id = <build_run_id>-<site_content_digest[:8]>`, `build_run_id`,
    `build_files`, `vocabularies`, `scope`, `history_archive`, and byte SHA-256s of all site files.
  - `summary.json`: the build's `summary.json` unchanged apart from `run_id` (browser computes no
    aggregate).
  - `index.json`: one slim row per included trial reconciled against `summary.json` counts.
  - `records.json`: per-trial comparability and outcome-history records stamped with site `run_id`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from outcomes import comparability_build as B  # noqa: E402
from src import holdout_exclusion as HX  # noqa: E402

SITE_SCHEMA_VERSION = 1
BUILD_SCHEMA_VERSION = 1
SITE_FILES = ('summary.json', 'index.json', 'records.json')


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def dump(obj: Any) -> bytes:
    """Compact key-sorted UTF-8 JSON bytes with final newline."""
    return (json.dumps(obj, ensure_ascii=False, separators=(',', ':'), sort_keys=True) + '\n').encode('utf-8')


def read_build(build: Path) -> dict[str, Any]:
    """Read and verify `BUILD_DIR` against its `run.json` SHA-256s and producer contract."""
    run_path = build / 'run.json'
    if not run_path.is_file():
        raise ValueError(f'{build}: missing run.json; not an outcomes-comparability build')
    try:
        run = json.loads(run_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'{run_path}: unreadable JSON ({exc})') from exc
    if not isinstance(run, dict) or run.get('schema_version') != BUILD_SCHEMA_VERSION:
        raise ValueError(f"run.json: unsupported build schema_version {run.get('schema_version') if isinstance(run, dict) else None!r}")
    listed = run.get('files')
    if not isinstance(listed, dict) or not listed:
        raise ValueError('run.json lists no files with SHA-256 digests')

    files: dict[str, Any] = {'run.json': run}
    for name, digest in sorted(listed.items()):
        fp = build / name
        if not fp.is_file():
            raise ValueError(f'{name}: listed in run.json and missing from {build}')
        raw = fp.read_bytes()
        if sha256_bytes(raw) != digest:
            raise ValueError(f'{name}: on-disk bytes do not match SHA-256 in run.json')
        try:
            parsed = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f'{name}: invalid JSON ({exc})') from exc
        if parsed.get('run_id') != run.get('run_id'):
            raise ValueError(f"{name}: belongs to another run ({parsed.get('run_id')!r})")
        files[name] = parsed

    problems = B.check(files)
    if problems:
        raise ValueError('build failed producer contract check: ' + '; '.join(problems[:5]))
    return files


def site_run_id(run: dict[str, Any], site_files: dict[str, Any]) -> str:
    """`build_run_id` with an 8-hex digest of the selected site content appended.

    The digest covers every site file as `select` builds it (`run.json` included, which carries the
    build's file SHA-256s as `build_files`), leaving out only the `run_id` each file is then stamped
    with. The id therefore follows from the exported bytes, not only from the build: re-exporting an
    unchanged build after an exporter change (`_slim_index_row`, `select`) gives a new id, and the
    same build exported by the same code keeps its id.
    """
    content = {name: {k: v for k, v in obj.items() if k != 'run_id'} for name, obj in site_files.items()}
    return f"{run['run_id']}-{sha256_bytes(dump(content))[:8]}"


def _metric_numerator(summary: dict[str, Any], metric_id: str) -> int:
    for m in summary['metrics']:
        if m['id'] == metric_id:
            return int(m['numerator'])
    raise ValueError(f'summary.json lacks metric {metric_id!r}')


def _slim_index_row(rec: dict[str, Any]) -> dict[str, Any]:
    """Build one slim searchable trial row from a full comparability record."""
    funnel = rec.get('denominator_funnel') or {}
    enr = funnel.get('registered_enrollment') or {}
    p1 = funnel.get('period1_started') or {}
    bt = funnel.get('baseline_total') or {}
    docs = rec.get('posted_documents') or {}
    arm_al = rec.get('arm_alignment') or {}
    est = rec.get('estimand_signals') or {}
    cs = rec.get('contrast_structure') or {}
    oh = rec.get('outcome_history') or {}
    p2r = oh.get('protocol_to_results') or {}
    primaries = rec.get('primary_measures') or []
    first_pm = primaries[0] if primaries else {}

    return {
        'nct_id': rec['nct_id'],
        'brief_title': rec.get('brief_title'),
        'results_status': rec['results_status'],
        'registered_enrollment': enr.get('count'),
        'registered_enrollment_type': enr.get('type'),
        'period1_started': p1.get('value'),
        'period1_started_state': p1.get('state'),
        'baseline_total': bt.get('value'),
        'baseline_total_method': bt.get('method'),
        'baseline_minus_period1_started': funnel.get('baseline_minus_period1_started'),
        'n_primary_measures': len(primaries) if rec['results_status'] == 'posted' and rec.get('primary_measures') is not None else None,
        'first_primary_analysed_total': first_pm.get('analysed_total') if primaries else None,
        'first_primary_analysed_minus_baseline_total': first_pm.get('analysed_minus_baseline_total') if primaries else None,
        'first_primary_analysed_over_period1_started': first_pm.get('analysed_over_period1_started') if primaries else None,
        'has_posted_protocol': docs.get('has_posted_protocol') if rec.get('posted_documents') is not None else None,
        'has_posted_sap': docs.get('has_posted_sap') if rec.get('posted_documents') is not None else None,
        'has_posted_icf': docs.get('has_posted_icf') if rec.get('posted_documents') is not None else None,
        'extra_baseline_groups_outside_primary': (
            arm_al.get('extra_baseline_groups_outside_primary') if rec.get('arm_alignment') is not None else None
        ),
        'has_shared_arm_contrasts': (
            cs.get('has_shared_arm_contrasts') if rec.get('contrast_structure') is not None else None
        ),
        'missing_data_state': (
            (est.get('missing_data_signal') or {}).get('state') if rec.get('estimand_signals') is not None else None
        ),
        'intercurrent_event_state': (
            (est.get('intercurrent_event_signal') or {}).get('state') if rec.get('estimand_signals') is not None else None
        ),
        'protocol_to_results_has_discrepancy': p2r.get('has_discrepancy'),
        'version_history_state': oh.get('version_history_state'),
    }


def select(build: dict[str, Any]) -> dict[str, Any]:
    """Select and reconcile site artifacts from a verified comparability build."""
    run = build['run.json']
    # Only a build with verifiable provenance is published: its producing commit must be known and its
    # working-tree state read (True, a dirty tree recorded honestly, or False). A null commit or a null
    # (unverifiable) `uncommitted_changes` would publish numbers no one can trace to a revision.
    prod = run.get('producer') or {}
    if not (isinstance(prod.get('commit'), str) and B.GIT_COMMIT.match(prod['commit'])) or not isinstance(
        prod.get('uncommitted_changes'), bool
    ):
        raise ValueError(
            f"build producer commit {prod.get('commit')!r} with uncommitted_changes {prod.get('uncommitted_changes')!r}: "
            'a build whose producing commit or working-tree state is unknown is not exported'
        )
    holdout_problems = HX.stamp_problems(run.get(HX.STAMP_KEY))
    if holdout_problems:
        raise ValueError(holdout_problems[0])
    summary = build['summary.json']
    records_map = build['records.json']['records']

    rows = [_slim_index_row(records_map[nct]) for nct in sorted(records_map)]

    # Reconcile slim index tallies against the build's summary.json counts.
    if len(rows) != summary['denominators']['requested_trials']['n']:
        raise ValueError('index row count disagrees with summary.json requested_trials')
    if sum(1 for r in rows if r['results_status'] == 'posted') != _metric_numerator(summary, 'results_posted'):
        raise ValueError('index posted-results count disagrees with summary.json results_posted')
    if sum(1 for r in rows if r['has_posted_protocol'] is True) != _metric_numerator(summary, 'has_posted_protocol'):
        raise ValueError('index protocol count disagrees with summary.json has_posted_protocol')
    if sum(1 for r in rows if r['has_posted_sap'] is True) != _metric_numerator(summary, 'has_posted_sap'):
        raise ValueError('index SAP count disagrees with summary.json has_posted_sap')
    if sum(1 for r in rows if r['extra_baseline_groups_outside_primary'] is True) != _metric_numerator(
        summary, 'extra_baseline_groups_outside_primary'
    ):
        raise ValueError('index extra_baseline_groups_outside_primary count disagrees with summary.json')
    if sum(1 for r in rows if r['has_shared_arm_contrasts'] is True) != _metric_numerator(
        summary, 'trials_with_shared_arm_contrasts'
    ):
        raise ValueError('index has_shared_arm_contrasts count disagrees with summary.json')

    stamp = {'schema_version': SITE_SCHEMA_VERSION}
    site_files: dict[str, Any] = {
        'summary.json': {**summary, **stamp},
        'index.json': {
            **stamp,
            'method_version': summary['method_version'],
            'outcome_history_version': summary['outcome_history_version'],
            'rows': rows,
        },
        'records.json': {
            **build['records.json'],
            **stamp,
        },
    }
    site_files['run.json'] = {
        **stamp,
        'build_run_id': run['run_id'],
        'build_schema_version': run['schema_version'],
        'generated_at': run['generated_at'],
        'scheduled': False,
        'producer': run['producer'],
        HX.STAMP_KEY: HX.stamp_for_site(run[HX.STAMP_KEY]),
        'method': run['method'],
        'acquisition': {
            k: run['acquisition'].get(k)
            for k in ('source', 'mode', 'retrieved_at', 'api_version', 'data_timestamp', 'n_requested', 'n_records', 'sha256_uncompressed')
        },
        'history_archive': run['history_archive'],
        'scope': run['scope'],
        'n_records': run['n_records'],
        'vocabularies': run['vocabularies'],
        'limitations': run['limitations'],
        'build_files': run['files'],
    }
    # Stamp the site run id last: it is a digest of the content selected above.
    s_run_id = site_run_id(run, site_files)
    for obj in site_files.values():
        obj['run_id'] = s_run_id
    return site_files


def export(build_dir: Path, out_dir: Path, holdout_index: HX.HoldoutIndex) -> dict[str, Any]:
    """Verify `build_dir`, stage site files into a temporary sibling of `out_dir`, verify written
    SHA-256 digests, and swap into `out_dir` atomically.

    Before anything is staged, the build's `holdout_exclusion` stamp must be valid and made under
    `holdout_index` (`HX.require_matching_stamp`) and no file of the build may name one of its holdout
    ids (`HX.refuse_holdout_mentions`, defence in depth); either refusal leaves `out_dir` untouched.
    """
    build = read_build(build_dir)
    HX.require_matching_stamp(build['run.json'], holdout_index)
    HX.refuse_holdout_mentions(build_dir, holdout_index)
    files = select(build)
    out_dir = out_dir.resolve()
    expected_names = set(SITE_FILES) | {'run.json'}
    refusal = B.replace_refusal(out_dir, expected_names, id_prefix='outcomes-comparability-')
    if refusal:
        raise ValueError(f'{refusal}; refusing to replace it')

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=out_dir.name + '.staging.', dir=out_dir.parent))
    try:
        digests: dict[str, str] = {}
        for name in SITE_FILES:
            raw = dump(files[name])
            target_file = stage / name
            target_file.write_bytes(raw)
            written_sha = sha256_bytes(target_file.read_bytes())
            if written_sha != sha256_bytes(raw):
                raise ValueError(f'{name}: staged bytes failed SHA-256 read-back check')
            digests[name] = written_sha

        manifest = {**files['run.json'], 'files': digests}
        (stage / 'run.json').write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + '\n',
            encoding='utf-8',
        )

        old: Path | None = None
        if out_dir.exists():
            old = Path(tempfile.mkdtemp(prefix=out_dir.name + '.replaced.', dir=out_dir.parent))
            out_dir.rename(old / out_dir.name)
        try:
            stage.rename(out_dir)
        except OSError:
            if old is not None and not out_dir.exists():
                (old / out_dir.name).rename(out_dir)
                old.rmdir()
            raise
        if old is not None:
            shutil.rmtree(old)
    finally:
        if stage.exists():
            shutil.rmtree(stage)

    # Post-swap verification: confirm every exported file in `out_dir` matches manifest SHA-256.
    for name, expected_hex in sorted(manifest['files'].items()):
        actual_hex = sha256_bytes((out_dir / name).read_bytes())
        if actual_hex != expected_hex:
            raise ValueError(f'{name}: post-swap SHA-256 mismatch in {out_dir}')
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('build', type=Path, help='directory written by scripts/build_outcomes_comparability.py')
    ap.add_argument('out', type=Path, help='destination site directory, e.g. data/outcomes_comparability')
    ap.add_argument('--holdout-index', type=Path, required=True,
                    help="the site's outcomes index (data/outcomes/index.json) the build must have been made under; required")
    args = ap.parse_args(argv)
    try:
        holdout_index = HX.load_holdout_index(args.holdout_index)
        manifest = export(args.build, args.out, holdout_index)
    except (ValueError, HX.HoldoutIndexError) as err:
        print(f'REFUSED: {err}', file=sys.stderr)
        return 1
    total = 0
    for name in sorted(p.name for p in args.out.iterdir()):
        size = (args.out / name).stat().st_size
        total += size
        print(f'{size:>10,}  {name}')
    print(f'{total:>10,}  total; wrote {args.out} ({manifest["run_id"]})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
