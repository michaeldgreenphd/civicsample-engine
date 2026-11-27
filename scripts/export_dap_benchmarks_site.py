#!/usr/bin/env python3
"""Export a verified DAP site-mix benchmark build to static site files.

  python3 scripts/export_dap_benchmarks_site.py BUILD_DIR OUT_DIR --holdout-index INDEX
  python3 scripts/export_dap_benchmarks_site.py build/dap ../clinical-trial-populations/data/dap \
      --holdout-index ../clinical-trial-populations/data/outcomes/index.json

``--holdout-index`` (required) is the site's outcomes index. The export is
refused, ``OUT_DIR`` untouched, when the build's run.json has no valid
``holdout_exclusion`` stamp (a build made before the in_audit holdout exclusion),
when the stamp's index sha256 is not this index's (the index changed since the
build: rebuild), or when any file of the build, ``trials.jsonl.gz`` included,
names a holdout id (``src/holdout_exclusion.py``). The site ``run.json`` carries
the stamp (the index sha256 and counts only).

Reads only ``BUILD_DIR``, verifies every file against ``run.json`` checksums and
``dap.artifacts.check_contract``, projects ``summary.json``, ``strata.json``,
and ``sponsors.json`` (leaving per-trial ``trials.jsonl.gz`` with the engine
build), stages atomically into a sibling directory, verifies written SHA-256
digests, and swaps ``OUT_DIR`` into place. The site ``run.json`` records this
exporter's own provenance (``exporter``: commit, uncommitted changes and source
SHA-256 over its import closure, from local ``git``), and the site ``run_id``
is derived from the exported content. Never scheduled; makes no network calls.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT: Path = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import build_dap_benchmarks  # noqa: E402
from dap import UNIT_BASIS, artifacts  # noqa: E402
from src import holdout_exclusion as HX  # noqa: E402

SITE_SCHEMA_VERSION: int = 1
BUILD_SCHEMA_VERSION: int = 1
SITE_FILES: tuple[str, ...] = ("summary.json", "strata.json", "sponsors.json")
# Sidecar provenance the build verified (benchmarks.check_condition_mesh_provenance)
# and recorded in run.json inputs.condition_mesh; carried to the site manifest so
# the published files name the extraction run their MeSH descriptors came from.
MESH_PROVENANCE_KEYS: tuple[str, ...] = (
    "snapshot_date",
    "extracted_at",
    "pipeline_commit",
    "provenance_check",
    "provenance_reason",
    "n_cohort_trials",
    "n_lines_outside_cohort",
)


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


def site_run_id(build_run: dict[str, Any], site_files: dict[str, dict[str, Any]]) -> str:
    """Build run_id suffixed with an 8-hex digest of the selected site content.

    The digest covers every site file as ``select_site_files`` builds it,
    ``run.json`` included (which carries the build's file SHA-256s as
    ``build_files`` and the ``exporter`` provenance), leaving out only the
    ``run_id`` each file is then stamped with. Re-exporting an unchanged build
    with changed exporter code or from another commit therefore gives a new id,
    and the same build exported by the same code keeps its id.
    """
    content = {name: {k: v for k, v in obj.items() if k != "run_id"} for name, obj in site_files.items()}
    return f"{build_run['run_id']}-{sha256_bytes(artifacts.dump(content))[:8]}"


def exporter_info() -> dict[str, Any]:
    """This exporter's provenance: commit, uncommitted changes and source SHA-256 over its import closure.

    Same method as the build's ``producer`` block (``build_dap_benchmarks.producer_info``),
    started from this script, so the site files name the code that projected them.
    """
    return build_dap_benchmarks.producer_info(ROOT, entry=Path(__file__))


def require_git_provenance(block: Any, where: str) -> None:
    """Refuse a provenance block that cannot name its commit or say whether its files were modified.

    ``build_dap_benchmarks.producer_info`` records ``null`` (never ``false``)
    when git is unavailable or a git command fails. Such a build may be kept for
    local diagnostics but is not published. ``uncommitted_changes: true`` is
    honest provenance (a dirty tree, recorded as such) and is accepted.
    """
    block = block if isinstance(block, dict) else {}
    commit, dirty = block.get("commit"), block.get("uncommitted_changes")
    if not isinstance(commit, str) or not commit or not isinstance(dirty, bool):
        raise ValueError(
            f"{where}: commit {commit!r}, uncommitted_changes {dirty!r}: no readable git provenance; "
            "only builds made in a git checkout, exported from one, are published"
        )


def require_weekly_source_stamps(study_in: dict[str, Any]) -> None:
    """Refuse a weekly build whose source container cannot name its extraction time and commit.

    The site manifest publishes `inputs.study_records.extracted_at` and `pipeline_commit`, the stamps of
    the `demographics.json` container the build read. `src.utils.pipeline_commit` writes null outside a
    checkout, and the build records the null (`benchmarks.weekly_source_stamp_problem`), but such a
    build is not published: the site's numbers could not be traced to the extraction and code that
    produced their input. With a condition_mesh sidecar the build already requires both stamps.
    """
    extracted_at, pipeline_commit = study_in.get("extracted_at"), study_in.get("pipeline_commit")
    problem = artifacts.weekly_source_stamp_problem(extracted_at, pipeline_commit)
    if problem or extracted_at is None or pipeline_commit is None:
        raise ValueError(
            f"run.json: inputs.study_records extracted_at {extracted_at!r}, pipeline_commit "
            f"{pipeline_commit!r}: a weekly build whose source container cannot name its extraction time "
            "and producing commit is not published"
        )


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

    # run_id is stamped last, from the content (site_run_id).
    site_objs: dict[str, dict[str, Any]] = {
        "summary.json": {**summary, "schema_version": SITE_SCHEMA_VERSION, "run_id": None},
        "strata.json": {**strata, "schema_version": SITE_SCHEMA_VERSION, "run_id": None},
        "sponsors.json": {**sponsors, "schema_version": SITE_SCHEMA_VERSION, "run_id": None},
    }

    inputs = run.get("inputs") or {}
    study_in = inputs.get("study_records") or {}
    # A --limit build covers only the first N input records and is diagnostic
    # only; exported, it would replace the site with a partial cohort under a
    # manifest that looks complete. load_study_records records limit_applied on
    # every input (null when no limit was given), so a missing key is refused too.
    if "limit_applied" not in study_in or study_in["limit_applied"] is not None:
        raise ValueError(
            f"run.json: inputs.study_records.limit_applied is {study_in.get('limit_applied', '<missing>')!r}; "
            "a --limit build is diagnostic only and is not exported to the site (rebuild without --limit)"
        )
    require_git_provenance(run.get("producer"), "run.json: producer")
    if study_in.get("input_format") == "full_records":
        require_weekly_source_stamps(study_in)
    exporter = exporter_info()
    require_git_provenance(exporter, "exporter")
    mesh_in = inputs.get("condition_mesh")
    ont_in = inputs.get("condition_ontology") or {}
    sp_in = inputs.get("sponsor_rules") or {}
    site_mesh: dict[str, Any] | None = None
    if isinstance(mesh_in, dict):
        # A build whose run.json lacks these keys predates the sidecar checks:
        # its sidecar was never verified against its records, so it is refused
        # rather than published with the provenance silently missing.
        missing = [k for k in MESH_PROVENANCE_KEYS if k not in mesh_in]
        if missing:
            raise ValueError(
                f"run.json: inputs.condition_mesh lacks {missing}; the sidecar was not "
                "provenance-checked, rebuild with the current build_dap_benchmarks.py"
            )
        site_mesh = {
            "n_lines": mesh_in.get("n_lines"),
            "sha256": mesh_in.get("sha256"),
            **{k: mesh_in[k] for k in MESH_PROVENANCE_KEYS},
        }

    holdout_problems = HX.stamp_problems(run.get(HX.STAMP_KEY))
    if holdout_problems:
        raise ValueError(holdout_problems[0])
    site_manifest: dict[str, Any] = {
        "schema_version": SITE_SCHEMA_VERSION,
        "run_id": None,
        "build_run_id": run["run_id"],
        "build_schema_version": run["schema_version"],
        "generated_at": run["generated_at"],
        "scheduled": False,
        "method_version": run["method_version"],
        "unit_basis": UNIT_BASIS,
        "producer": run["producer"],
        "exporter": exporter,
        HX.STAMP_KEY: HX.stamp_for_site(run[HX.STAMP_KEY]),
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
            "condition_mesh": site_mesh,
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
    selected = {"run.json": site_manifest, **site_objs}
    s_run_id = site_run_id(run, selected)
    for obj in selected.values():
        obj["run_id"] = s_run_id
    return selected


def export_site(build_dir: Path, out_dir: Path, holdout_index: HX.HoldoutIndex) -> dict[str, Any]:
    """Verify ``build_dir``, stage site projection, verify checksums, and swap ``out_dir``.

    Before anything is staged, the build's ``holdout_exclusion`` stamp must be
    valid and made under ``holdout_index`` (``HX.require_matching_stamp``) and no
    file of the build may name one of its holdout ids
    (``HX.refuse_holdout_mentions``, defence in depth); either refusal raises
    ``HX.HoldoutIndexError`` and leaves ``out_dir`` untouched.
    """
    loaded = read_verified_build(build_dir)
    HX.require_matching_stamp(loaded["run.json"], holdout_index)
    HX.refuse_holdout_mentions(build_dir, holdout_index)
    selected = select_site_files(loaded)
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
    ap.add_argument(
        "--holdout-index",
        type=Path,
        required=True,
        help="The site's outcomes index (data/outcomes/index.json) the build must have been made under; required",
    )
    args = ap.parse_args(argv)

    try:
        holdout_index = HX.load_holdout_index(args.holdout_index)
        manifest = export_site(args.build, args.out, holdout_index)
    except (ValueError, OSError, HX.HoldoutIndexError) as exc:
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
