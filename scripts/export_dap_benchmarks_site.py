#!/usr/bin/env python3
"""Export a verified DAP site-mix benchmark build to static site files.

  python3 scripts/export_dap_benchmarks_site.py BUILD_DIR OUT_DIR
  python3 scripts/export_dap_benchmarks_site.py build/dap ../clinical-trial-populations/data/dap

Reads only ``BUILD_DIR``, verifies every file against ``run.json`` checksums and
``dap.artifacts.check_contract``, projects ``summary.json``, ``strata.json``,
and ``sponsors.json`` (leaving per-trial ``trials.jsonl.gz`` with the engine
build), stages atomically into a sibling directory, verifies written SHA-256
digests, and swaps ``OUT_DIR`` into place. Never scheduled; makes no network
calls.
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

ROOT: Path = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dap import UNIT_BASIS, artifacts  # noqa: E402

SITE_SCHEMA_VERSION: int = 1
BUILD_SCHEMA_VERSION: int = 1
SITE_FILES: tuple[str, ...] = ("summary.json", "strata.json", "sponsors.json")


def sha256_bytes(data: bytes) -> str:
    """Return hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def read_verified_build(build_dir: Path) -> dict[str, Any]:
    """Load ``build_dir`` and refuse if any file, digest, or contract invariant fails."""
    run_path = build_dir / "run.json"
    if not run_path.is_file():
        raise ValueError(f"{build_dir}: missing run.json; not a DAP benchmark build")
    try:
        loaded = artifacts.load_dir(build_dir)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{build_dir}: unreadable artifact file ({exc})") from exc

    run = loaded.get("run.json")
    if not isinstance(run, dict):
        raise ValueError(f"{build_dir}: run.json is not a JSON object")
    if run.get("schema_version") != BUILD_SCHEMA_VERSION:
        raise ValueError(
            f"run.json: unsupported build schema_version {run.get('schema_version')!r}"
        )

    problems = artifacts.check_contract(loaded)
    if problems:
        joined = "; ".join(problems[:5])
        raise ValueError(f"{build_dir}: build failed producer contract ({len(problems)} problem(s): {joined})")
    return loaded


def site_run_id(build_run: dict[str, Any]) -> str:
    """Build run_id suffixed with an 8-hex digest of all build file hashes."""
    digest = sha256_bytes(json.dumps(sorted(build_run["files"].items())).encode("utf-8"))
    return f"{build_run['run_id']}-{digest[:8]}"


def select_site_files(loaded: dict[str, Any]) -> dict[str, Any]:
    """Project verified build artifacts onto static site files without altering counts."""
    run = loaded["run.json"]
    summary = loaded["summary.json"]
    strata = loaded["strata.json"]
    sponsors = loaded["sponsors.json"]

    # Reconcile tallies across siblings before staging.
    if len(strata["strata"]) != summary["denominators"]["all_strata"]["n"]:
        raise ValueError("strata.json count disagrees with summary.json denominators.all_strata")
    lit_states = sponsors["attribution_summary"]["literal_counts_by_review_state"]
    if sum(int(v) for v in lit_states.values()) != summary["denominators"]["sponsor_literals"]["n"]:
        raise ValueError("sponsors.json literal count disagrees with summary.json denominators.sponsor_literals")

    s_run_id = site_run_id(run)
    site_objs: dict[str, dict[str, Any]] = {
        "summary.json": {**summary, "schema_version": SITE_SCHEMA_VERSION, "run_id": s_run_id},
        "strata.json": {**strata, "schema_version": SITE_SCHEMA_VERSION, "run_id": s_run_id},
        "sponsors.json": {**sponsors, "schema_version": SITE_SCHEMA_VERSION, "run_id": s_run_id},
    }

    inputs = run.get("inputs") or {}
    study_in = inputs.get("study_records") or {}
    mesh_in = inputs.get("condition_mesh")
    ont_in = inputs.get("condition_ontology") or {}
    sp_in = inputs.get("sponsor_rules") or {}

    site_manifest: dict[str, Any] = {
        "schema_version": SITE_SCHEMA_VERSION,
        "run_id": s_run_id,
        "build_run_id": run["run_id"],
        "build_schema_version": run["schema_version"],
        "generated_at": run["generated_at"],
        "scheduled": False,
        "method_version": run["method_version"],
        "unit_basis": UNIT_BASIS,
        "producer": run["producer"],
        "parameters": run["parameters"],
        "counting_units": run["counting_units"],
        "vocabularies": run["vocabularies"],
        "limitations": run["limitations"],
        "inputs": {
            "study_records": {
                "input_format": study_in.get("input_format"),
                "n_records": study_in.get("n_records"),
                "sha256": study_in.get("sha256"),
                "extracted_at": study_in.get("extracted_at"),
                "pipeline_commit": study_in.get("pipeline_commit"),
            },
            "condition_mesh": (
                {
                    "n_lines": mesh_in.get("n_lines"),
                    "sha256": mesh_in.get("sha256"),
                }
                if isinstance(mesh_in, dict)
                else None
            ),
            "condition_ontology": {
                "version": ont_in.get("version"),
                "sha256": ont_in.get("sha256"),
            },
            "sponsor_rules": {
                "file": sp_in.get("file"),
                "sha256": sp_in.get("sha256"),
            },
        },
        "build_files": run["files"],
    }
    return {"run.json": site_manifest, **site_objs}


def export_site(build_dir: Path, out_dir: Path) -> dict[str, Any]:
    """Verify ``build_dir``, stage site projection, verify checksums, and swap ``out_dir``."""
    selected = select_site_files(read_verified_build(build_dir))
    expected_names = set(SITE_FILES) | {"run.json"}
    refusal = artifacts.replace_refusal(out_dir, expected_names)
    if refusal is not None:
        raise ValueError(f"{refusal}; refusing to replace it")

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f"{out_dir.name}.staging.", dir=out_dir.parent))
    try:
        digests: dict[str, str] = {}
        for name in SITE_FILES:
            raw = artifacts.dump(selected[name])
            (stage / name).write_bytes(raw)
            digests[name] = sha256_bytes(raw)

        manifest = {**selected["run.json"], "files": digests}
        (stage / "run.json").write_bytes(artifacts.dump_manifest(manifest))

        # Re-read staged files and verify SHA-256 digests before swapping.
        for name, expected_sha in digests.items():
            actual_sha = sha256_bytes((stage / name).read_bytes())
            if actual_sha != expected_sha:
                raise ValueError(f"staged {name} failed post-write sha256 verification")

        old: Path | None = None
        if out_dir.exists():
            old = Path(tempfile.mkdtemp(prefix=f"{out_dir.name}.replaced.", dir=out_dir.parent))
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
    """CLI entry point for exporting DAP benchmarks to a static site directory."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("build", type=Path, help="Verified build directory written by build_dap_benchmarks.py")
    ap.add_argument("out", type=Path, help="Target site directory to replace, e.g. ../clinical-trial-populations/data/dap")
    args = ap.parse_args(argv)

    try:
        manifest = export_site(args.build, args.out)
    except (ValueError, OSError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    total = 0
    for name in sorted(p.name for p in args.out.iterdir()):
        size = (args.out / name).stat().st_size
        total += size
        print(f"{size:>10,}  {name}")
    print(f"{total:>10,}  total; wrote {args.out} ({manifest['run_id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
