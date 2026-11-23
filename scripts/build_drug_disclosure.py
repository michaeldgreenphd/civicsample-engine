#!/usr/bin/env python3
"""Acquire, freeze, and build Drug Trial Cross-Source Evidence & Disclosure Timeline artifacts.

Usage:
  # 1a. Live rate-limited (<= 2 req/s per host), resumable acquisition + freeze:
  python3 scripts/build_drug_disclosure.py acquire --cache build/disclosure_cache --nct-list ids.txt

  # 1b. Or freeze already-downloaded local CT.gov v2 `.json`/`.json.gz`/`.jsonl.gz` records offline,
  #     stating when they were retrieved and the CT.gov data timestamp they reflect (no defaults):
  python3 scripts/build_drug_disclosure.py freeze-local --records /path/to/records --cache build/disclosure_cache \
      --fetched-at 2026-11-13T22:32:29+00:00 --data-timestamp 2026-11-13T10:00:06

  # 2. Offline deterministic build from frozen sha256-verified cache:
  python3 scripts/build_drug_disclosure.py build --cache build/disclosure_cache --out build/disclosure
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from disclosure import acquire as A  # noqa: E402
from disclosure import artifacts as ART  # noqa: E402

PRODUCER_SCRIPTS = (
    'scripts/build_drug_disclosure.py',
    'scripts/check_drug_disclosure_contract.py',
    'scripts/export_drug_disclosure_site.py',
)

# Repository directories whose modules count as producing code.
PRODUCER_DIRS: tuple[str, ...] = ('disclosure', 'src', 'sponsors', 'scripts')


def _loaded_repo_modules(root: Path) -> dict[str, Path]:
    """Module name -> resolved source file for every loaded module under `PRODUCER_DIRS`."""
    root_resolved = root.resolve()
    loaded: dict[str, Path] = {}
    for name, module in list(sys.modules.items()):
        fname = getattr(module, '__file__', None)
        if not isinstance(fname, str) or not fname.endswith('.py'):
            continue  # builtins, extension modules, namespace packages
        path = Path(fname).resolve()
        if not path.is_relative_to(root_resolved):
            continue
        if path.relative_to(root_resolved).parts[0] in PRODUCER_DIRS:
            loaded[name] = path
    return loaded


def _imported_module_names(path: Path, package: str | None) -> set[str]:
    """Every module name an import statement in `path` can bind, parent packages included.

    `from pkg import name` yields both `pkg` and `pkg.name` because `name` may be a submodule;
    names that are not loaded modules are simply never matched. Relative imports resolve against
    `package`; imports inside functions and `try` blocks count, since the build may run them.
    """
    tree = ast.parse(path.read_bytes(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ''
            if node.level:
                if not package:
                    continue  # a relative import outside a package cannot run
                base = importlib.util.resolve_name('.' * node.level + base, package)
            targets = [base] + [f'{base}.{alias.name}' for alias in node.names]
        for target in targets:
            parts = target.split('.')
            names.update('.'.join(parts[: i + 1]) for i in range(len(parts)))
    return names


def _import_closure(root: Path) -> list[str]:
    """Sorted paths, relative to `root`, of the repo modules this script's imports reach.

    Method: the repo-local entries of `sys.modules` (files under `PRODUCER_DIRS`) are the modules
    actually loaded. Starting from this script, follow each file's import statements to the loaded
    modules they name. Following imports rather than taking every repo-local entry keeps the list
    independent of what else the process loaded (a test session imports many unrelated modules).
    The closure reaches `disclosure/` and the `sponsors/` attribution modules (`adapter`,
    `audit_states`, `sponsor_roles`) whenever pandas let them load; without pandas the stdlib
    mirror in `disclosure/timeline.py` runs instead and they are, correctly, absent.
    """
    loaded = _loaded_repo_modules(root)
    entry = Path(__file__).resolve()
    seen: set[Path] = set()
    pending: list[tuple[Path, str | None]] = [(entry, __package__)]
    while pending:
        path, package = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        for name in _imported_module_names(path, package):
            dep = loaded.get(name)
            if dep is not None and dep not in seen:
                pending.append((dep, getattr(sys.modules[name], '__package__', None)))
    root_resolved = root.resolve()
    # relative_to raises if `root` is not the checkout this script was imported from.
    return sorted(p.relative_to(root_resolved).as_posix() for p in seen)


def producer_paths(root: Path = ROOT) -> list[str]:
    """Every producing code and curated CSV path hashed into `producer.source_sha256`.

    The union of the build's import closure (`_import_closure`, which adds the `sponsors/`
    modules the build executes) and the files this family always hashed: every file under
    `disclosure/` (its modules, curated CSVs and README) and the three CLI scripts, of which the
    contract and export scripts run after the build rather than inside it. `source_digest` and
    the `git status` dirty check in `producer_metadata` use this one list. Call it after the build
    modules are imported, so imports made at run time are loaded.
    """
    pkg_files = [
        str(p.relative_to(root).as_posix())
        for p in (root / 'disclosure').rglob('*')
        if p.is_file() and '__pycache__' not in p.parts and not p.name.endswith('.pyc')
    ]
    scripts = [rel for rel in PRODUCER_SCRIPTS if (root / rel).is_file()]
    return sorted(set(pkg_files) | set(scripts) | set(_import_closure(root)))


def source_digest(root: Path = ROOT, paths: list[str] | None = None) -> str:
    """Deterministic sha256 over the producing files (default `producer_paths()`) read from `root`."""
    h = hashlib.sha256()
    for rel in producer_paths() if paths is None else paths:
        h.update(rel.encode('utf-8') + b'\0' + (root / rel).read_bytes() + b'\0')
    return h.hexdigest()


def _git_stdout(root: Path, *args: str) -> str | None:
    """stdout of `git <args>` run in `root`, or None when git is missing or the command fails."""
    try:
        proc = subprocess.run(['git', *args], cwd=root, capture_output=True, text=True)
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def producer_metadata(root: Path = ROOT) -> dict[str, Any]:
    """Producer repository, HEAD commit (when git metadata is reachable), and `source_sha256`.

    The digest and the `git status` dirty check cover the same list (`producer_paths`), checked
    in `root`; the dirty check also names the `disclosure` directory, so a deleted tracked file
    there counts as an uncommitted change.

    Git provenance is recorded only when it is known. If git is missing, `rev-parse` fails or does
    not print a 40-hex commit, or `status` fails, both `commit` and `uncommitted_changes` are None,
    never a commit beside an unknown dirty state and never False. The build may still be written
    for local diagnostics; the site exporter refuses it. A dirty tree is recorded as True.
    """
    paths = producer_paths(root)
    head = _git_stdout(root, 'rev-parse', 'HEAD')
    commit: str | None = head.strip() if head is not None and ART.GIT_COMMIT_RE.match(head.strip()) else None
    dirty: bool | None = None
    if commit is not None:
        # The `disclosure` directory pathspec also reports a tracked file deleted from the working
        # tree, which `paths` (files present on disk) cannot name; such a deletion changes the digest.
        status = _git_stdout(root, 'status', '--porcelain', '--', 'disclosure', *paths)
        if status is None:
            commit = None
        else:
            dirty = bool(status.strip())
    return {
        'repository': 'civicsample-engine',
        'commit': commit,
        'uncommitted_changes': dirty,
        'source_sha256': source_digest(root, paths),
    }


FETCHED_AT_HELP = (
    'ISO 8601 time the local records were retrieved from ClinicalTrials.gov '
    '(stamped as the fetched_at of each record; there is no default)'
)
DATA_TIMESTAMP_HELP = (
    'ISO 8601 CT.gov dataTimestamp the local records reflect (run.json sources.ctgov.data_timestamp '
    'and the default --as-of; there is no default)'
)


def _resolve_nct_ids(nct_list: Path | None, nct_ids: list[str] | None) -> list[str]:
    collected: list[str] = []
    if nct_list is not None:
        collected.extend(A.read_nct_list(nct_list))
    if nct_ids:
        for item in nct_ids:
            s = item.strip()
            if not A.NCT_RE.match(s):
                raise ValueError(f'invalid --nct-id {s!r}')
            collected.append(s)
    if not collected:
        raise ValueError('specify --nct-list and/or at least one --nct-id')
    return sorted(set(collected))


def cmd_acquire(args: argparse.Namespace) -> int:
    try:
        ids = _resolve_nct_ids(args.nct_list, args.nct_id)
        throttle = A.Throttle(min_interval=args.min_interval)
    except ValueError as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        return 1

    get = A.requests_get()
    stages = args.stage or list(A.STAGES)
    cache_dir: Path = args.cache

    ctgov_log = A.ResponseLog(A.raw_log_path(cache_dir, 'ctgov'))
    if 'ctgov' in stages:
        try:
            res_ct = A.fetch_ctgov_studies(ids, ctgov_log, get, throttle)
        except A.CTGovVersionUnavailable as exc:
            print(f'REFUSED: {exc}', file=sys.stderr)
            return 1
        print(f'ctgov: {res_ct}')

    studies_by_nct = A.studies_from_ctgov_records(ctgov_log.records, ids)

    if 'literature' in stages:
        lit_log = A.ResponseLog(A.raw_log_path(cache_dir, 'literature'))
        res_lit = A.fetch_literature(ids, studies_by_nct, lit_log, get, throttle)
        print(f'literature: {res_lit}')

    if 'openfda' in stages:
        fda_log = A.ResponseLog(A.raw_log_path(cache_dir, 'openfda'))
        res_fda = A.fetch_openfda(ids, studies_by_nct, fda_log, get, throttle)
        print(f'openfda: {res_fda}')

    if 'freeze' in stages:
        try:
            manifest = A.freeze_cache(cache_dir, ids)
        except ValueError as exc:
            print(f'REFUSED: {exc}', file=sys.stderr)
            return 1
        print(
            f"freeze: {manifest['n_requested_nct_ids']} requested NCT IDs; "
            f"archives={sorted(manifest['archives'])} -> {cache_dir}"
        )
    return 0


def cmd_freeze_local(args: argparse.Namespace) -> int:
    try:
        wanted = A.read_nct_list(args.nct_list) if args.nct_list else None
        manifest = A.ingest_local_ctgov_records(
            args.records,
            args.cache,
            nct_ids=wanted,
            limit=args.limit,
            fetched_at=args.fetched_at,
            data_timestamp=args.data_timestamp,
        )
    except (OSError, ValueError) as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        return 1
    print(
        f"freeze-local: froze {manifest['n_requested_nct_ids']} CT.gov v2 study record(s) "
        f"into {args.cache} (sha256={manifest['archives']['ctgov']['sha256_uncompressed'][:12]})"
    )
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    try:
        # Checked before anything else, so a typo cannot seed a cache from --records first.
        if args.generated_at is not None:
            gen_problem = ART.generated_at_problem(args.generated_at)
            if gen_problem:
                raise ValueError(gen_problem)
        subset_ids = A.read_nct_list(args.nct_list) if args.nct_list else None
        if args.records is not None and not (args.cache / 'manifest.json').exists():
            if args.fetched_at is None or args.data_timestamp is None:
                raise ValueError(
                    '--records seeds the cache from local files, so --fetched-at and --data-timestamp '
                    'are required: when the records were retrieved and the CT.gov data timestamp they reflect'
                )
            A.ingest_local_ctgov_records(
                args.records,
                args.cache,
                nct_ids=subset_ids,
                limit=args.limit,
                fetched_at=args.fetched_at,
                data_timestamp=args.data_timestamp,
            )
        frozen = A.load_frozen_cache(args.cache)
        as_of_date = date.fromisoformat(args.as_of) if args.as_of else None
        files = ART.build_artifacts(
            frozen,
            reviews_path=args.reviews,
            statements_path=args.statements,
            sponsor_rules_path=args.sponsor_rules,
            producer=producer_metadata(),
            generated_at=args.generated_at or A.now_utc(),
            as_of=as_of_date,
            requested_nct_ids=subset_ids,
        )
    except (OSError, ValueError) as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        return 1

    problems = ART.check_contract(files)
    if problems:
        print(f'CONTRACT FAILED ({len(problems)} problem(s)); nothing written:', file=sys.stderr)
        for prob in problems[:50]:
            print(f'  {prob}', file=sys.stderr)
        return 1

    try:
        ART.write_artifacts(files, args.out)
    except ValueError as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        return 1

    run = files['run.json']
    den = files['summary.json']['denominators']
    print(
        f"{run['run_id']}: {den['included_trials']['n']} trials "
        f"({den['retrieved_trials']['n']} retrieved, {den['ctgov_posted_trials']['n']} CT.gov posted), "
        f"{den['evidence_links']['n']} evidence links -> {args.out}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='command', required=True)

    p_acq = sub.add_parser('acquire', help='Acquire and freeze cross-source responses (<= 2 req/s)')
    p_acq.add_argument('--cache', type=Path, default=ROOT / 'build' / 'disclosure_cache')
    p_acq.add_argument('--nct-list', type=Path, help='File containing NCT IDs (one per line)')
    p_acq.add_argument('--nct-id', action='append', help='Individual NCT ID (repeatable)')
    p_acq.add_argument('--stage', action='append', choices=A.STAGES, help='Subset of stages to run')
    p_acq.add_argument('--min-interval', type=float, default=A.MIN_INTERVAL_SECONDS,
                       help='Minimum seconds between requests per host (>= 0.5)')

    p_loc = sub.add_parser('freeze-local', help='Freeze local CT.gov v2 study JSON/JSON.gz/JSONL.gz records')
    p_loc.add_argument('--records', type=Path, required=True,
                       help='Directory of CT.gov v2 study files or a .json/.json.gz/.jsonl/.jsonl.gz file')
    p_loc.add_argument('--cache', type=Path, default=ROOT / 'build' / 'disclosure_cache')
    p_loc.add_argument('--nct-list', type=Path, help='Optional allowlist of NCT IDs')
    p_loc.add_argument('--limit', type=int, help='Optional cap on number of local study records ingested')
    p_loc.add_argument('--fetched-at', required=True, help=FETCHED_AT_HELP)
    p_loc.add_argument('--data-timestamp', required=True, help=DATA_TIMESTAMP_HELP)

    p_bld = sub.add_parser('build', help='Offline deterministic build from frozen cache')
    p_bld.add_argument('--cache', type=Path, default=ROOT / 'build' / 'disclosure_cache')
    p_bld.add_argument('--out', type=Path, default=ROOT / 'build' / 'disclosure')
    p_bld.add_argument('--records', type=Path,
                       help='Optional local CT.gov v2 records to freeze into --cache when manifest is absent')
    p_bld.add_argument('--limit', type=int, help='Optional limit when seeding from --records')
    p_bld.add_argument('--fetched-at', help='Required with --records when seeding: ' + FETCHED_AT_HELP)
    p_bld.add_argument('--data-timestamp', help='Required with --records when seeding: ' + DATA_TIMESTAMP_HELP)
    p_bld.add_argument('--nct-list', type=Path, help='Optional subset of cached NCT IDs to include')
    p_bld.add_argument('--as-of', help='Reference date YYYY-MM-DD for 365-day window evaluation '
                       "(default: the date of the cache's CT.gov data timestamp; required when the "
                       "cache's version records span several data timestamps)")
    p_bld.add_argument('--generated-at', help='Pin ISO 8601 timestamp for byte-identical run.json')
    p_bld.add_argument('--reviews', type=Path, default=ROOT / 'disclosure' / 'reviews.csv')
    p_bld.add_argument('--statements', type=Path, default=ROOT / 'disclosure' / 'documentary_statements.csv')
    p_bld.add_argument('--sponsor-rules', type=Path, default=ROOT / 'sponsors' / 'company_aliases.csv')

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == 'acquire':
        return cmd_acquire(args)
    if args.command == 'freeze-local':
        return cmd_freeze_local(args)
    return cmd_build(args)


if __name__ == '__main__':
    sys.exit(main())
