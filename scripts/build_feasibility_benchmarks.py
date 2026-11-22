#!/usr/bin/env python3
"""Build operational feasibility, accrual velocity, and early-termination benchmarks (manual).

Examples:
  python3 scripts/build_feasibility_benchmarks.py --demographics data/demographics.json.gz \
      --condition-mesh data/condition_mesh.jsonl.gz --out build/feasibility
  python3 scripts/build_feasibility_benchmarks.py --records-dir /path/to/ctgov_v2_records \
      --out build/feasibility
  python3 scripts/build_feasibility_benchmarks.py \
      --archive docs/audits/outcomes_results_structure/first_pass/cache/raw_responses.jsonl.gz \
      --out build/feasibility

Never scheduled by `.github/workflows/extract.yml`. Refuses site-projected
`demographics.part*.json.gz` files, validates the staged family with `feasibility.artifacts.check_contract`
before writing, and stages atomically into `--out`.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from feasibility import artifacts, benchmarks, termination  # noqa: E402
from src import full_records  # noqa: E402

DEFAULT_REVIEWS_PATH = ROOT / "feasibility" / "termination_reviews.csv"
DEFAULT_RULES_PATH = ROOT / "sponsors" / "company_aliases.csv"
DEFAULT_OUT_PATH = ROOT / "build" / "feasibility"

_PART_RE = re.compile(r"demographics\.part\d*\.json(?:\.gz)?$", re.IGNORECASE)


def now_utc_iso() -> str:
    """Current UTC wall-clock timestamp in ISO-8601 seconds format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def producer_paths(root: Path = ROOT) -> list[str]:
    """Sorted repository-relative paths of all producing code and curated review CSVs."""
    pkg_files = [
        str(p.relative_to(root).as_posix())
        for p in (root / "feasibility").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    ]
    scripts_list = [
        "scripts/build_feasibility_benchmarks.py",
        "scripts/check_feasibility_contract.py",
        "scripts/export_feasibility_site.py",
    ]
    present_scripts = [s for s in scripts_list if (root / s).is_file()]
    return sorted(set(pkg_files + present_scripts))


def source_digest(root: Path = ROOT) -> str:
    """SHA-256 over all producing files on disk, path by path."""
    digest = hashlib.sha256()
    for rel in producer_paths(root):
        digest.update(rel.encode("utf-8") + b"\0" + (root / rel).read_bytes() + b"\0")
    return digest.hexdigest()


def producer_metadata(root: Path = ROOT) -> dict[str, Any]:
    """Git revision, dirty state (when git is accessible), and deterministic code digest."""
    paths = producer_paths(root)
    commit: str | None = None
    dirty: bool | None = None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            commit = proc.stdout.strip()
            st = subprocess.run(
                ["git", "status", "--porcelain", "--", *paths],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            if st.returncode == 0:
                dirty = bool(st.stdout.strip())
    except OSError:
        commit = None
        dirty = None

    return {
        "repository": "civicsample-engine",
        "commit": commit,
        "uncommitted_changes": dirty,
        "source_sha256": source_digest(root),
    }


def _extract_studies_from_json_obj(obj: Any) -> list[dict[str, Any]]:
    """Unwrap one study or a batch of studies from direct CT.gov v2 JSON or cached envelopes."""
    if not isinstance(obj, dict):
        raise ValueError("expected JSON object for study record or envelope")
    if "protocolSection" in obj or "nct_id" in obj:
        return [obj]
    if isinstance(obj.get("study"), dict):
        return [obj["study"]]
    if isinstance(obj.get("studies"), list):
        return [s for s in obj["studies"] if isinstance(s, dict)]
    if "body" in obj:
        body = obj["body"]
        if isinstance(body, str):
            body = json.loads(body)
        if isinstance(body, dict):
            if "protocolSection" in body:
                return [body]
            if isinstance(body.get("studies"), list):
                return [s for s in body["studies"] if isinstance(s, dict)]
        return []
    raise ValueError(f"unrecognized study JSON shape with keys {sorted(obj.keys())[:8]}")


def load_studies_input(
    *,
    demographics_path: Path | None,
    records_dir: Path | None,
    archive_path: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load studies and return `(studies, records_input_metadata)`."""
    if records_dir is not None:
        if not records_dir.is_dir():
            raise ValueError(f"{records_dir}: --records-dir is not an existing directory")
        files = sorted(
            p
            for p in records_dir.iterdir()
            if p.is_file() and (p.name.endswith(".json") or p.name.endswith(".json.gz"))
        )
        if not files:
            raise ValueError(f"{records_dir}: holds no .json or .json.gz study files")
        dir_hash = hashlib.sha256()
        studies: list[dict[str, Any]] = []
        for fp in files:
            raw = fp.read_bytes()
            dir_hash.update(fp.name.encode("utf-8") + b"\0" + raw + b"\0")
            text = gzip.decompress(raw).decode("utf-8") if fp.name.endswith(".gz") else raw.decode("utf-8")
            obj = json.loads(text)
            studies.extend(_extract_studies_from_json_obj(obj))
        if not studies:
            raise ValueError(f"{records_dir}: decoded zero study records")
        return studies, {
            "mode": "records_dir",
            "source": records_dir.name,
            "n_files": len(files),
            "n_records": len(studies),
            "sha256": dir_hash.hexdigest(),
            "source_extracted_at": None,
            "source_pipeline_commit": None,
        }

    if archive_path is not None:
        if not archive_path.is_file():
            raise ValueError(f"{archive_path}: --archive file does not exist")
        if _PART_RE.search(archive_path.name):
            raise ValueError(
                f"{archive_path}: site-projected demographics.part*.json.gz is not a full-record archive"
            )
        raw_bytes = archive_path.read_bytes()
        file_sha = hashlib.sha256(raw_bytes).hexdigest()
        uncompressed = (
            gzip.decompress(raw_bytes) if archive_path.name.endswith(".gz") else raw_bytes
        )
        studies = []
        # Split strictly on ASCII LF (0x0A), never str.splitlines(), so U+2028 / U+2029 / \x85
        # inside ensure_ascii=False JSON string values are never split mid-string.
        for line_no, raw_line in enumerate(uncompressed.split(b"\n"), start=1):
            if not raw_line.strip():
                continue
            try:
                obj = json.loads(raw_line.rstrip(b"\r").decode("utf-8"))
            except ValueError as exc:
                raise ValueError(f"{archive_path.name}:{line_no}: invalid JSON line") from exc
            studies.extend(_extract_studies_from_json_obj(obj))
        if not studies:
            raise ValueError(f"{archive_path}: archive holds zero study records")
        return studies, {
            "mode": "jsonl_archive",
            "source": archive_path.name,
            "n_records": len(studies),
            "sha256": file_sha,
            "source_extracted_at": None,
            "source_pipeline_commit": None,
        }

    target = demographics_path or Path(full_records.DEFAULT_PATH)
    if _PART_RE.search(target.name):
        raise ValueError(
            f"{target}: refusing site-projected demographics.part*.json.gz; pass full records instead"
        )
    try:
        records, extracted_at, pipe_commit = full_records.load(target)
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc
    file_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    return records, {
        "mode": "weekly_full_records",
        "source": target.name,
        "n_records": len(records),
        "sha256": file_sha,
        "source_extracted_at": extracted_at,
        "source_pipeline_commit": pipe_commit,
    }


def load_condition_mesh_sidecar(
    path: Path | None,
) -> tuple[dict[str, dict[str, Any]] | None, dict[str, Any] | None]:
    """Load optional `condition_mesh.jsonl.gz` sidecar keyed by `nct_id`."""
    if path is None:
        return None, None
    if not path.is_file():
        raise ValueError(f"{path}: --condition-mesh file does not exist")
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    uncompressed = gzip.decompress(raw) if path.name.endswith(".gz") else raw
    by_nct: dict[str, dict[str, Any]] = {}
    for line_no, raw_line in enumerate(uncompressed.split(b"\n"), start=1):
        if not raw_line.strip():
            continue
        obj = json.loads(raw_line.rstrip(b"\r").decode("utf-8"))
        nct = obj.get("nct_id")
        if not isinstance(nct, str) or not nct.strip():
            raise ValueError(f"{path.name}:{line_no}: missing nct_id in condition_mesh sidecar")
        if nct in by_nct:
            raise ValueError(f"{path.name}:{line_no}: duplicate nct_id {nct} in condition_mesh sidecar")
        by_nct[nct] = obj
    return by_nct, {
        "file": path.name,
        "n_rows": len(by_nct),
        "sha256": sha,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument(
        "--demographics",
        type=Path,
        help="Weekly full-record demographics.json or demographics.json.gz (via src/full_records.py).",
    )
    src.add_argument(
        "--records-dir",
        type=Path,
        help="Directory of full ClinicalTrials.gov API v2 .json or .json.gz study records.",
    )
    src.add_argument(
        "--archive",
        "--records-jsonl",
        dest="archive",
        type=Path,
        help="Frozen .jsonl or .jsonl.gz archive of ClinicalTrials.gov API v2 study records.",
    )
    ap.add_argument(
        "--condition-mesh",
        type=Path,
        help="Optional condition_mesh.jsonl.gz sidecar file.",
    )
    ap.add_argument(
        "--reviews",
        type=Path,
        default=DEFAULT_REVIEWS_PATH,
        help="Curated human review override CSV (default: feasibility/termination_reviews.csv).",
    )
    ap.add_argument(
        "--rules",
        type=Path,
        default=DEFAULT_RULES_PATH,
        help="Curated sponsor alias rules CSV (default: sponsors/company_aliases.csv, read-only).",
    )
    ap.add_argument(
        "--min-cell-trials",
        type=int,
        default=benchmarks.DEFAULT_MIN_CELL_TRIALS,
        help="Minimum trials/eligible observations required before showing cell percentiles (default: 5).",
    )
    ap.add_argument(
        "--generated-at",
        help="Optional ISO-8601 UTC timestamp to pin for deterministic byte-identical rebuilds.",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_PATH,
        help="Output artifact directory (default: build/feasibility).",
    )
    args = ap.parse_args(argv)

    if args.min_cell_trials < 1:
        print("REFUSED: --min-cell-trials must be >= 1", file=sys.stderr)
        return 1

    try:
        reviews = termination.load_termination_reviews(args.reviews)
        if not args.rules.is_file():
            raise ValueError(f"{args.rules}: sponsor rules CSV not found")
        studies, records_meta = load_studies_input(
            demographics_path=args.demographics,
            records_dir=args.records_dir,
            archive_path=args.archive,
        )
        mesh_by_nct, mesh_meta = load_condition_mesh_sidecar(args.condition_mesh)

        trials, sponsor_meta, review_meta = benchmarks.build_trial_records(
            studies,
            reviews=reviews,
            rules_path=args.rules,
            condition_mesh_by_nct=mesh_by_nct,
        )

        inputs_meta: dict[str, Any] = {
            "records": records_meta,
            "sponsor_rules": {
                "file": args.rules.name,
                "sha256": sponsor_meta["rules_sha256"],
            },
            "termination_reviews": {
                "file": args.reviews.name,
                "n_rows": len(reviews),
                "sha256": hashlib.sha256(args.reviews.read_bytes()).hexdigest(),
            },
            "condition_mesh": mesh_meta,
        }

        parsed_family, raw_bytes = artifacts.build_artifact_family(
            trials=trials,
            sponsor_meta=sponsor_meta,
            review_meta=review_meta,
            inputs_meta=inputs_meta,
            producer_meta=producer_metadata(),
            generated_at=args.generated_at or now_utc_iso(),
            min_cell_trials=args.min_cell_trials,
        )

        problems = artifacts.check_contract(parsed_family, raw_bytes)
        if problems:
            print(
                f"CONTRACT FAILED ({len(problems)} problem(s)); nothing written:",
                *problems[:40],
                sep="\n  ",
                file=sys.stderr,
            )
            return 1

        artifacts.write_artifact_family(raw_bytes, args.out)
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    run = parsed_family["run.json"]
    den = parsed_family["summary.json"]["denominators"]
    print(
        f"{run['run_id']}: {den['trials']['n']} trials "
        f"({den['resolved_trials']['n']} resolved, {den['stopped_trials']['n']} stopped, "
        f"{den['actual_enrollment_trials']['n']} ACTUAL enrollment) -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
