#!/usr/bin/env python3
"""Build Multi-Site Diversity Action Plan (FDORA) & geographic site-mix benchmarks.

Manual offline build (never scheduled; makes zero network calls):

  python3 scripts/build_dap_benchmarks.py --demographics demographics.json.gz --out build/dap
  python3 scripts/build_dap_benchmarks.py --records-dir /path/to/ctgov_studies --out build/dap
  python3 scripts/build_dap_benchmarks.py --records raw_responses.jsonl.gz --out build/dap

Writes nothing unless the staged artifact family passes ``dap.artifacts.check_contract``.
"""
from __future__ import annotations

import argparse
import hashlib
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


def producer_paths(root: Path = ROOT) -> list[str]:
    """Sorted relative paths of producing code files for deterministic code digest."""
    dap_files = [
        str(p.relative_to(root).as_posix())
        for p in (root / "dap").rglob("*.py")
        if p.is_file() and "__pycache__" not in p.parts
    ]
    return sorted(dap_files + ["scripts/build_dap_benchmarks.py"])


def source_digest(root: Path = ROOT) -> str:
    """Deterministic SHA-256 over all producing module files on disk."""
    h = hashlib.sha256()
    for rel in producer_paths(root):
        h.update(rel.encode("utf-8") + b"\0" + (root / rel).read_bytes() + b"\0")
    return h.hexdigest()


def producer_info(root: Path = ROOT) -> dict[str, Any]:
    """Git commit, working-tree cleanliness of producing paths, and code SHA-256."""
    paths = producer_paths(root)
    commit_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    commit = commit_proc.stdout.strip() if commit_proc.returncode == 0 and commit_proc.stdout.strip() else None
    dirty: bool | None = None
    if commit is not None:
        status_proc = subprocess.run(
            ["git", "status", "--porcelain", "--", *paths],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if status_proc.returncode == 0:
            dirty = bool(status_proc.stdout.strip())
    return {
        "repository": "civicsample-engine",
        "commit": commit,
        "uncommitted_changes": dirty,
        "source_sha256": source_digest(root),
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
        help="YYYY-MM-DD snapshot date stamped when parsing raw sex/gender measures",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=ROOT / "build" / "dap",
        help="Output directory for the DAP benchmark artifact family (default: build/dap)",
    )
    args = ap.parse_args(argv)

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
    snapshot_date = args.snapshot_date or generated_at[:10]

    try:
        records, study_meta = benchmarks.load_study_records(
            input_path,
            source_kind=source_kind,
            limit=args.limit,
        )
        mesh_map, mesh_meta = benchmarks.load_condition_mesh(args.condition_mesh)
        mesh_meta = benchmarks.check_condition_mesh_provenance(mesh_map, mesh_meta, records, study_meta)
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
        )
    except (ValueError, OSError, KeyError) as exc:
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
