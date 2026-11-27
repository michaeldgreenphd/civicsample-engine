#!/usr/bin/env python3
"""Export a verified feasibility-benchmarks build to compact site-consumable JSON files.

  python3 scripts/export_feasibility_site.py BUILD_DIR OUT_DIR --holdout-index INDEX
  python3 scripts/export_feasibility_site.py build/feasibility ../clinical-trial-populations/data/feasibility \
      --holdout-index ../clinical-trial-populations/data/outcomes/index.json

Runs by hand, makes no network call, and commits nothing.

`--holdout-index` (required) is the site's outcomes index. The export is refused, `OUT_DIR` untouched,
when the build's run.json has no valid `holdout_exclusion` stamp (a build made before the in_audit
holdout exclusion), when the stamp's index sha256 is not this index's (the index changed since the
build: rebuild), or when any file of the build, `trials.jsonl.gz` and so every stopped-trial row
included, names a holdout id (`src/holdout_exclusion.py`). The site `run.json` carries the stamp (the
index sha256 and counts only).

Verification & safety guarantees:
1. Full build check first: Loads `BUILD_DIR` and runs `feasibility.artifacts.check_contract`
   (including SHA-256 verification of every file in `BUILD_DIR/run.json` and `run_identity`
   recomputation). Edited, truncated, or mixed builds are refused non-zero.
2. Selection, never a second count: `summary.json`, `strata.json`, and `sponsors.json` carry the
   build's exact numbers with only `run_id` updated to `site_run_id`. `stopped_trials.json` exports
   slim stopped-study signal rows (`TERMINATED`, `WITHDRAWN`, `SUSPENDED`) whose row count and
   per-category tallies are checked 1:1 against `summary.json` before anything is written; full
   `trials.jsonl.gz` remains with the producer build.
3. Atomic directory swap & replacement guard: Stages in a temporary sibling directory and swaps
   into `OUT_DIR`. Refuses any existing non-empty `OUT_DIR` that is not already a complete
   `feasibility-*` site export, preventing accidental deletion of unrelated directories.
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

from feasibility import artifacts  # noqa: E402
from src import holdout_exclusion as HX  # noqa: E402

SITE_SCHEMA_VERSION = 1
SITE_FILES = (
    "summary.json",
    "strata.json",
    "sponsors.json",
    "stopped_trials.json",
)


def sha256_bytes(data: bytes) -> str:
    """Return hex SHA-256 digest of `data`."""
    return hashlib.sha256(data).hexdigest()


def dump_compact(obj: Any) -> bytes:
    """Canonical compact key-sorted JSON bytes with trailing newline."""
    return artifacts.dump_canonical(obj)


def site_run_id(build_run: dict[str, Any]) -> str:
    """Append an 8-hex digest of the verified build's file SHA-256 map to `build_run['run_id']`."""
    digest = sha256_bytes(json.dumps(sorted(build_run["files"].items())).encode("utf-8"))
    return f"{build_run['run_id']}-{digest[:8]}"


def read_verified_build(build_dir: Path) -> dict[str, Any]:
    """Load `build_dir` and enforce full producer contract + SHA-256 verification."""
    try:
        parsed_family, raw_bytes = artifacts.load_dir(build_dir)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{build_dir}: cannot read build ({exc})") from exc

    # An exception check_contract did not anticipate is a refusal like any contract problem (main
    # prints it and exits 1); it is raised before anything is staged, so nothing is written.
    try:
        problems = artifacts.check_contract(parsed_family, raw_bytes)
    except Exception as exc:  # noqa: BLE001 - any unanticipated shape refuses the export
        raise ValueError(
            f"{build_dir}: contract check could not complete ({type(exc).__name__}: {exc})"
        ) from exc
    if problems:
        raise ValueError(
            f"{build_dir}: build failed contract verification ({len(problems)} problem(s): {problems[0]})"
        )
    return parsed_family


def slim_stopped_trial(trial: dict[str, Any]) -> dict[str, Any]:
    """Extract compact display fields for one stopped study (`TERMINATED`, `WITHDRAWN`, `SUSPENDED`)."""
    term = trial["termination"]
    return {
        "nct_id": trial["nct_id"],
        "brief_title": trial["brief_title"],
        "overall_status": trial["overall_status"],
        "phase": trial["phase"],
        "ontology_primary": trial["indication"]["ontology_primary"],
        "lead_sponsor_class": trial["sponsor"]["lead_sponsor_class"],
        "sponsor_review_state": trial["sponsor"]["review_state"],
        "lead_canonical_companies": trial["sponsor"]["lead_canonical_companies"],
        "lead_literal": trial["sponsor"]["lead_literal"],
        "footprint_tier": trial["facilities"]["footprint_tier"],
        "n_listed_facilities": trial["facilities"]["n_listed_facilities"],
        "enrollment_type": trial["enrollment"]["type"],
        "enrollment_count": trial["enrollment"]["count"],
        "why_stopped_verbatim": term["why_stopped_verbatim"],
        "category": term["category"],
        "deterministic_category": term["deterministic_category"],
        "matched_signals": term["matched_signals"],
        "classification_source": term["classification_source"],
        "reviewed_at": term["reviewed_at"],
        "stopped_study_results_state": term["stopped_study_results_state"],
    }


def select_site_files(parsed_build: dict[str, Any]) -> dict[str, Any]:
    """Construct site export JSON payloads and reconcile tallies against `summary.json`."""
    run = parsed_build["run.json"]
    summary = parsed_build["summary.json"]
    strata = parsed_build["strata.json"]
    sponsors = parsed_build["sponsors.json"]
    trials = parsed_build[artifacts.TRIALS_FILE]

    # Published files must name the code that produced them. When git was unavailable or failed, the
    # build records `commit: null` / `uncommitted_changes: null` (unverifiable, never false); such a
    # build is fine for local diagnostics but is not exported. A dirty tree recorded as `true` is
    # honest provenance and is exported with that flag.
    prod = run.get("producer") or {}
    if prod.get("commit") is None or prod.get("uncommitted_changes") is None:
        raise ValueError(
            f"run.json: producer commit {prod.get('commit')!r} / uncommitted_changes "
            f"{prod.get('uncommitted_changes')!r}: the producing revision could not be read from git, so "
            "the build is not exported to the site (rebuild from a git checkout)"
        )

    holdout_problems = HX.stamp_problems(run.get(HX.STAMP_KEY))
    if holdout_problems:
        raise ValueError(holdout_problems[0])
    s_run_id = site_run_id(run)
    stopped_rows = [slim_stopped_trial(t) for t in trials if t["termination"]["is_stopped_study"]]

    expected_stopped = summary["denominators"]["stopped_trials"]["n"]
    if len(stopped_rows) != expected_stopped:
        raise ValueError(
            f"stopped_trials count ({len(stopped_rows)}) disagrees with summary.json ({expected_stopped})"
        )

    cat_counts = Counter(r["category"] for r in stopped_rows)
    for row in summary["tables"]["termination_categories"]["rows"]:
        cat = row["id"].split(":", 1)[1]
        if cat_counts.get(cat, 0) != row["numerator"]:
            raise ValueError(
                f"stopped_trials category {cat!r} ({cat_counts.get(cat, 0)}) disagrees with summary.json ({row['numerator']})"
            )

    stamp = {"schema_version": SITE_SCHEMA_VERSION, "run_id": s_run_id}
    site_payloads: dict[str, Any] = {
        "summary.json": {**summary, "schema_version": SITE_SCHEMA_VERSION, "run_id": s_run_id},
        "strata.json": {**strata, "schema_version": SITE_SCHEMA_VERSION, "run_id": s_run_id},
        "sponsors.json": {**sponsors, "schema_version": SITE_SCHEMA_VERSION, "run_id": s_run_id},
        "stopped_trials.json": {
            **stamp,
            "termination_rules_version": run["termination_rules_version"],
            "n_stopped_trials": len(stopped_rows),
            "stopped_trials": stopped_rows,
        },
        "run.json": {
            **stamp,
            "build_run_id": run["run_id"],
            "build_schema_version": run["schema_version"],
            "generated_at": run["generated_at"],
            "scheduled": False,
            "method_version": run["method_version"],
            "termination_rules_version": run["termination_rules_version"],
            "producer": run["producer"],
            HX.STAMP_KEY: HX.stamp_for_site(run[HX.STAMP_KEY]),
            "parameters": run["parameters"],
            "vocabularies": run["vocabularies"],
            "inputs": {
                "records": {
                    k: run["inputs"]["records"].get(k)
                    for k in ("mode", "source", "n_records", "sha256", "source_extracted_at", "source_pipeline_commit")
                },
                "sponsor_rules_sha256": run["inputs"]["sponsor_rules"]["sha256"],
                # The curated overrides change published classifications, and two review files can
                # have the same row count; their digest (checked 64-hex by the contract) names the
                # file used. `build_files` does not cover it: run.json is outside its own hashes.
                "termination_reviews_sha256": run["inputs"]["termination_reviews"]["sha256"],
                "termination_reviews_n_rows": run["inputs"]["termination_reviews"]["n_rows"],
                "condition_mesh": run["inputs"].get("condition_mesh"),
            },
            "reviews": {
                "n_rows": run["reviews"]["n_rows"],
                "n_applied": run["reviews"]["n_applied"],
                "n_orphaned": len(run["reviews"]["orphaned_reviews"]),
            },
            "n_trials": run["n_trials"],
            "n_stopped_trials": len(stopped_rows),
            "build_files": run["files"],
        },
    }
    return site_payloads


def export_site(build_dir: Path, out_dir: Path, holdout_index: HX.HoldoutIndex) -> dict[str, Any]:
    """Verify `build_dir`, select site payloads, and atomically replace `out_dir`.

    Before anything is staged, the build's `holdout_exclusion` stamp must be valid and made under
    `holdout_index` (`HX.require_matching_stamp`) and no file of the build may name one of its holdout
    ids (`HX.refuse_holdout_mentions`, defence in depth); either refusal leaves `out_dir` untouched.
    """
    parsed = read_verified_build(build_dir)
    HX.require_matching_stamp(parsed["run.json"], holdout_index)
    HX.refuse_holdout_mentions(build_dir, holdout_index)
    site_payloads = select_site_files(parsed)

    expected_names = set(SITE_FILES) | {"run.json"}
    refusal = artifacts.replace_refusal(out_dir, expected_names, run_id_prefix="feasibility-")
    if refusal:
        raise ValueError(f"{refusal}; refusing to replace it")

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=out_dir.name + ".staging.", dir=out_dir.parent))
    try:
        digests: dict[str, str] = {}
        for name in SITE_FILES:
            raw = dump_compact(site_payloads[name])
            (stage / name).write_bytes(raw)
            digests[name] = sha256_bytes(raw)

        manifest = {**site_payloads["run.json"], "files": digests}
        (stage / "run.json").write_bytes(artifacts.dump_manifest(manifest))

        old = None
        if out_dir.exists():
            old = Path(tempfile.mkdtemp(prefix=out_dir.name + ".replaced.", dir=out_dir.parent))
            out_dir.rename(old / out_dir.name)
        try:
            stage.rename(out_dir)
        except OSError:
            if old is not None:
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
    ap.add_argument("build", type=Path, help="Verified build directory written by build_feasibility_benchmarks.py.")
    ap.add_argument("out", type=Path, help="Site target directory to replace atomically (e.g., data/feasibility).")
    ap.add_argument(
        "--holdout-index",
        type=Path,
        required=True,
        help="The site's outcomes index (data/outcomes/index.json) the build must have been made under; required.",
    )
    args = ap.parse_args(argv)

    try:
        holdout_index = HX.load_holdout_index(args.holdout_index)
        manifest = export_site(args.build, args.out, holdout_index)
    except (ValueError, HX.HoldoutIndexError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    total = 0
    for fp in sorted(args.out.iterdir()):
        sz = fp.stat().st_size
        total += sz
        print(f"{sz:>10,}  {fp.name}")
    print(f"{total:>10,}  total; exported {args.out} ({manifest['run_id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
