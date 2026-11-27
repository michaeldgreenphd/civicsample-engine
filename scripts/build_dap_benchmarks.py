#!/usr/bin/env python3
"""Build Multi-Site Diversity Action Plan (FDORA) & geographic site-mix benchmarks.

Manual offline build (never scheduled; makes zero network calls):

  python3 scripts/build_dap_benchmarks.py --demographics demographics.json.gz --out build/dap --holdout-index INDEX
  python3 scripts/build_dap_benchmarks.py --records-dir /path/to/ctgov_studies --out build/dap --holdout-index INDEX
  python3 scripts/build_dap_benchmarks.py --records ctgov_responses.jsonl.gz --out build/dap --holdout-index INDEX

``--holdout-index`` (required, no default) is the site's outcomes index
(clinical-trial-populations ``data/outcomes/index.json``). A trial whose
``in_audit`` is true there is an audit holdout and is excluded: its row of the
snapshot (and its condition_mesh line) is removed by id before any other
processing, so no stratum, count, sponsor table or per-trial row includes it,
and run.json ``holdout_exclusion`` records how many were removed
(``src/holdout_exclusion.py``).

Raw inputs must be complete API v2 records (no ``fields=`` projection); the
audit caches under ``docs/audits/outcomes_results_structure/`` are projected
and are refused. Writes nothing unless the staged artifact family passes ``dap.artifacts.check_contract``.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT: Path = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dap import artifacts, benchmarks  # noqa: E402
from src import full_records  # noqa: E402
from src import holdout_exclusion as HX  # noqa: E402


# Repository directories whose modules count as producing code.
PRODUCER_DIRS: tuple[str, ...] = ("dap", "src", "sponsors", "scripts")


def _loaded_repo_modules(root: Path) -> dict[str, Path]:
    """Module name -> resolved source file for every loaded module under ``PRODUCER_DIRS``."""
    root_resolved = root.resolve()
    loaded: dict[str, Path] = {}
    for name, module in list(sys.modules.items()):
        fname = getattr(module, "__file__", None)
        if not isinstance(fname, str) or not fname.endswith(".py"):
            continue  # builtins, extension modules, namespace packages
        path = Path(fname).resolve()
        if not path.is_relative_to(root_resolved):
            continue
        if path.relative_to(root_resolved).parts[0] in PRODUCER_DIRS:
            loaded[name] = path
    return loaded


def _imported_module_names(path: Path, package: str | None) -> set[str]:
    """Every module name an import statement in ``path`` can bind, parent packages included.

    ``from pkg import name`` yields both ``pkg`` and ``pkg.name`` because
    ``name`` may be a submodule; names that are not loaded modules are simply
    never matched. Relative imports resolve against ``package``; imports
    inside functions count, since the build may run them.
    """
    tree = ast.parse(path.read_bytes(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                if not package:
                    continue  # a relative import outside a package cannot run
                base = importlib.util.resolve_name("." * node.level + base, package)
            targets = [base] + [f"{base}.{alias.name}" for alias in node.names]
        for target in targets:
            parts = target.split(".")
            names.update(".".join(parts[: i + 1]) for i in range(len(parts)))
    return names


def producer_paths(root: Path = ROOT, entry: Path | None = None) -> list[str]:
    """Sorted paths, relative to ``root`` (the checkout imported from), of the files the build imported.

    ``entry`` names another loaded script to start from instead of this one
    (``export_dap_benchmarks_site.py`` records its own closure this way).

    Method: the repo-local entries of ``sys.modules`` (files under
    ``PRODUCER_DIRS``) are the modules actually loaded. Starting from this
    script, follow the import statements of each file to the loaded modules
    they name, so the list is this build's import closure: the dap package,
    ``src`` full-record loading, race/ethnicity extractors, sex/gender table
    and parser, condition MeSH, and the ``sponsors`` attribution modules.
    Following imports rather than taking every repo-local entry keeps the list
    independent of what else the process loaded (a test session imports many
    unrelated modules), so the digest depends only on the producing code.
    Call it after the build has run, so imports made at run time are loaded.
    """
    loaded = _loaded_repo_modules(root)
    start = Path(__file__).resolve() if entry is None else entry.resolve()
    # The start file's package resolves its relative imports (scripts have none).
    start_package = next(
        (getattr(sys.modules[name], "__package__", None) for name, path in loaded.items() if path == start),
        None,
    )
    seen: set[Path] = set()
    pending: list[tuple[Path, str | None]] = [(start, start_package)]
    while pending:
        path, package = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        for name in _imported_module_names(path, package):
            dep = loaded.get(name)
            if dep is not None and dep not in seen:
                pending.append((dep, getattr(sys.modules[name], "__package__", None)))
    root_resolved = root.resolve()
    # relative_to raises if ``root`` is not the checkout the script was imported from.
    return sorted(p.relative_to(root_resolved).as_posix() for p in seen)


def source_digest(root: Path = ROOT, paths: list[str] | None = None) -> str:
    """Deterministic SHA-256 over the producing files (default ``producer_paths()``) read from ``root``."""
    h = hashlib.sha256()
    for rel in producer_paths() if paths is None else paths:
        h.update(rel.encode("utf-8") + b"\0" + (root / rel).read_bytes() + b"\0")
    return h.hexdigest()


def _git_stdout(args: list[str], root: Path) -> str | None:
    """Stdout of ``git <args>`` run in ``root``, or ``None`` when git is missing or the command fails."""
    try:
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    except OSError:
        return None  # no git binary (or not runnable): provenance unknown, not an error
    return proc.stdout if proc.returncode == 0 else None


def producer_info(root: Path = ROOT, entry: Path | None = None) -> dict[str, Any]:
    """Git commit, working-tree cleanliness of producing paths, and code SHA-256.

    The digest and the ``git status`` dirty check cover the same list: the
    import closure of this build (or of ``entry``; see ``producer_paths``),
    checked in ``root``. When git is unavailable or a git command fails,
    ``commit`` and/or ``uncommitted_changes`` are ``None``, never a guessed
    value (in particular never ``False``). Such a build is still written for
    local diagnostics; ``export_dap_benchmarks_site.py`` refuses to publish it.
    """
    paths = producer_paths(entry=entry)
    rev = _git_stdout(["rev-parse", "HEAD"], root)
    commit = rev.strip() if rev is not None and rev.strip() else None
    dirty: bool | None = None
    if commit is not None:
        status = _git_stdout(["status", "--porcelain", "--", *paths], root)
        if status is not None:
            dirty = bool(status.strip())
    return {
        "repository": "civicsample-engine",
        "commit": commit,
        "uncommitted_changes": dirty,
        "source_sha256": source_digest(root, paths),
    }


def now_utc_iso() -> str:
    """UTC ISO-8601 timestamp to second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for building DAP site-mix benchmark artifacts."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src_group = ap.add_mutually_exclusive_group()
    src_group.add_argument(
        "--demographics",
        type=Path,
        help="Weekly full-record JSON or JSON.GZ file (src/full_records.py format)",
    )
    src_group.add_argument(
        "--records",
        type=Path,
        help="Path to raw CT.gov v2 study JSON(.gz), JSONL(.gz), or directory of study files",
    )
    src_group.add_argument(
        "--records-dir",
        type=Path,
        help="Directory of raw CT.gov v2 .json or .json.gz study documents",
    )
    ap.add_argument(
        "--condition-mesh",
        type=Path,
        default=None,
        help=(
            "Optional condition_mesh.jsonl.gz sidecar (src/condition_mesh.py); must cover every "
            "input trial, come from one extraction run and, for weekly full records "
            "(--demographics), carry their extracted_at and pipeline_commit on every line"
        ),
    )
    ap.add_argument(
        "--ontology",
        type=Path,
        default=benchmarks.DEFAULT_ONTOLOGY_PATH,
        help="Path to condition_ontology.json",
    )
    ap.add_argument(
        "--sponsor-rules",
        type=Path,
        default=benchmarks.DEFAULT_SPONSOR_RULES_PATH,
        help="Read-only curated sponsor alias rules CSV (sponsors/company_aliases.csv)",
    )
    ap.add_argument(
        "--min-cell-trials",
        type=int,
        default=benchmarks.DEFAULT_MIN_CELL_TRIALS,
        help="Minimum qualifying trials per cell before publishing percentiles (default: 5)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional cap on first N input study records (diagnostic runs only)",
    )
    ap.add_argument(
        "--generated-at",
        type=str,
        default=None,
        help="Pin UTC ISO-8601 build timestamp (YYYY-MM-DDTHH:MM:SS+00:00) for byte-identical builds",
    )
    ap.add_argument(
        "--snapshot-date",
        type=str,
        default=None,
        help=(
            "YYYY-MM-DD registry snapshot date. Raw CT.gov input: stamped on the parsed sex/gender rows "
            "(default: the --generated-at date). Weekly full records: the pull's date, required unless "
            "the rows or the --condition-mesh sidecar carry it, and must match them"
        ),
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=ROOT / "build" / "dap",
        help="Output directory for the DAP benchmark artifact family (default: build/dap)",
    )
    ap.add_argument(
        "--holdout-index",
        type=Path,
        required=True,
        help=(
            "The site's outcomes index (clinical-trial-populations data/outcomes/index.json); trials whose "
            "in_audit is true are audit holdouts and are excluded from the build. Required; no default"
        ),
    )
    args = ap.parse_args(argv)

    # The holdout index is read first: a malformed one refuses the build before any input is read.
    try:
        exclusion = HX.HoldoutExclusion(HX.load_holdout_index(args.holdout_index))
    except HX.HoldoutIndexError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    if args.demographics is not None:
        input_path = args.demographics
        source_kind = "full_records"
    elif args.records is not None:
        input_path = args.records
        source_kind = "auto"
    elif args.records_dir is not None:
        input_path = args.records_dir
        source_kind = "auto"
    else:
        input_path = Path(full_records.DEFAULT_PATH)
        source_kind = "full_records"

    generated_at = args.generated_at or now_utc_iso()
    try:
        generated_date = artifacts.utc_timestamp_date(generated_at)  # refuse a bad value before building
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    if args.snapshot_date is not None:
        problem = artifacts.snapshot_date_problem(args.snapshot_date)  # refuse before building
        if problem:
            print(f"refused: {problem}", file=sys.stderr)
            return 1

    try:
        records, study_meta = benchmarks.load_study_records(
            input_path,
            source_kind=source_kind,
            limit=args.limit,
            exclusion=exclusion,
        )
        mesh_map, mesh_meta = benchmarks.load_condition_mesh(args.condition_mesh, exclusion)
        mesh_meta = benchmarks.check_condition_mesh_provenance(mesh_map, mesh_meta, records, study_meta)
        # Weekly full records: the pull's own date (rows or sidecar), never the build date.
        snapshot_date = artifacts.resolve_snapshot_date(
            args.snapshot_date,
            default=generated_date,
            records=records,
            study_meta=study_meta,
            mesh_meta=mesh_meta,
        )
        result = benchmarks.build_benchmarks(
            records,
            mesh_sidecar=mesh_map,
            has_mesh_sidecar=args.condition_mesh is not None,
            ontology_path=args.ontology,
            sponsor_rules_path=args.sponsor_rules,
            min_cell_trials=args.min_cell_trials,
            snapshot_date=snapshot_date,
        )
        built = artifacts.build_artifact_family(
            result,
            study_input_meta=study_meta,
            mesh_input_meta=mesh_meta,
            producer=producer_info(ROOT),
            min_cell_trials=args.min_cell_trials,
            generated_at=generated_at,
            snapshot_date=snapshot_date,
            holdout_exclusion=exclusion.stamp(),
        )
    except (ValueError, OSError, KeyError, HX.HoldoutIndexError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except SystemExit as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    in_memory_loaded: dict[str, Any] = {
        "run.json": built["run.json"],
        "summary.json": built["summary.json"],
        "strata.json": built["strata.json"],
        "sponsors.json": built["sponsors.json"],
        "trial_rows": built["trial_rows"],
        "_bytes": built["_bytes"],
        "_extra_files": [],
    }
    problems = artifacts.check_contract(in_memory_loaded)
    if problems:
        print(f"contract problems ({len(problems)}); nothing written:", file=sys.stderr)
        for prob in problems[:50]:
            print(f"  {prob}", file=sys.stderr)
        return 1

    try:
        artifacts.write_artifact_family(built, args.out)
    except (ValueError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    den = built["summary.json"]["denominators"]
    print(
        f"{built['run.json']['run_id']}: {den['all_trials']['n']} trials "
        f"({den['posted_results_trials']['n']} posted, {den['us_site_trials']['n']} US-site), "
        f"{den['all_strata']['n']} strata, "
        f"{len(built['sponsors.json']['current_owner'])} canonical sponsors -> {args.out}"
    )
    print(
        f"holdouts: {exclusion.n_excluded} in_audit holdout trial(s) excluded from the input "
        f"({exclusion.index.n_holdouts} in the index)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
