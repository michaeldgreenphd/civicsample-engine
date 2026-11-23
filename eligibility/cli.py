"""Manual CLI builder for deterministic protocol eligibility barriers and discordance flags.

Invoked by hand via ``python scripts/build_eligibility_barriers.py`` or ``python -m eligibility``;
never scheduled. Enforces a fail-closed ``in_audit`` firewall whenever any input study carries a
non-synthetic NCT ID, verifies structural invariants and lean-vs-full cohort summary parity, and
writes provenance-stamped artifacts atomically.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from eligibility import (
    PARSER_MODULE_VERSION,
    VOCABULARY_SHA256,
    VOCABULARY_VERSION,
    all_structural_checks_pass,
    build_eligibility_row,
    lean_eligibility_row,
    structural_checks,
    summarize_cohort_barriers,
)

_SYNTHETIC_ID_RX = re.compile(r"^NCT" + r"99\d{6}$")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_in_audit_firewall(index_path: Path) -> tuple[set[str], str]:
    """Load in_audit == True NCT IDs from outcomes/index.json, failing closed on bad schema."""
    if not index_path.is_file():
        raise SystemExit(f"in_audit index file not found: {index_path}")
    digest = _sha256_file(index_path)
    doc = json.loads(index_path.read_text(encoding="utf-8"))
    candidates: list[dict[str, Any]] = []
    if isinstance(doc, dict):
        for key in ("rows", "trials", "records", "studies", "items", "data"):
            val = doc.get(key)
            if isinstance(val, list):
                candidates.extend(x for x in val if isinstance(x, dict))
    elif isinstance(doc, list):
        candidates.extend(x for x in doc if isinstance(x, dict))

    saw_in_audit_field = False
    excluded: set[str] = set()
    for row in candidates:
        if "in_audit" in row and isinstance(row["in_audit"], bool):
            saw_in_audit_field = True
            if row["in_audit"] is True:
                nid = str(row.get("nct_id") or row.get("nctId") or "").strip().upper()
                if nid:
                    excluded.add(nid)

    if not saw_in_audit_field:
        raise SystemExit(
            f"{index_path}: invalid outcomes index schema (no rows carrying boolean 'in_audit' found)"
        )
    return excluded, digest


def _extract_nct_id(record: Any) -> str:
    if not isinstance(record, dict):
        return ""
    top = record.get("nct_id") or record.get("nctId")
    if isinstance(top, str) and top.strip():
        return top.strip().upper()
    proto = record.get("protocolSection")
    if isinstance(proto, dict):
        ident = proto.get("identificationModule")
        if isinstance(ident, dict):
            nid = ident.get("nctId")
            if isinstance(nid, str) and nid.strip():
                return nid.strip().upper()
    return ""


def _load_studies_file(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise SystemExit(f"studies file not found: {path}")
    opener = gzip.open if path.name.endswith(".gz") else open
    name_no_gz = path.name[:-3] if path.name.endswith(".gz") else path.name
    if name_no_gz.endswith(".jsonl"):
        out: list[dict[str, Any]] = []
        with opener(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    obj = json.loads(line)
                    if not isinstance(obj, dict):
                        raise SystemExit(f"{path}: JSONL line is not an object")
                    out.append(obj)
        return out
    with opener(path, "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("studies", "data", "records"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
        if "protocolSection" in payload or "eligibilityModule" in payload:
            return [payload]
    raise SystemExit(f"{path}: unrecognized study container format")


def _pipeline_commit_or_none() -> str | None:
    try:
        from src.utils import pipeline_commit
        return pipeline_commit()
    except Exception:
        return None


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build deterministic eligibility-barrier rows, lean rows, and cohort summary."
    )
    parser.add_argument("--studies", type=Path, required=True, help="Path to CT.gov API v2 studies (.json/.jsonl[.gz])")
    parser.add_argument(
        "--in-audit-index",
        type=Path,
        default=None,
        help="Path to outcomes/index.json for in_audit firewall (required on non-synthetic inputs)",
    )
    parser.add_argument(
        "--synthetic-ids-only",
        action="store_true",
        help="Allow running without --in-audit-index ONLY when every record uses a synthetic NCT99000000-series ID",
    )
    parser.add_argument("--snapshot-date", type=str, default=None, help="Snapshot date YYYY-MM-DD (default: UTC today)")
    parser.add_argument("--out", type=Path, required=True, help="Output directory for generated artifacts")
    args = parser.parse_args(list(argv) if argv is not None else None)

    source_sha = _sha256_file(args.studies)
    in_audit_ids: set[str] = set()
    in_audit_sha: str | None = None
    if args.in_audit_index is not None:
        in_audit_ids, in_audit_sha = load_in_audit_firewall(args.in_audit_index)
    elif not args.synthetic_ids_only:
        raise SystemExit(
            "--in-audit-index is required unless --synthetic-ids-only is explicitly set on synthetic inputs"
        )

    snap_date = args.snapshot_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw_studies = _load_studies_file(args.studies)
    full_rows: list[dict[str, Any]] = []
    lean_rows: list[dict[str, Any]] = []
    n_excluded_in_audit = 0

    for st in raw_studies:
        nid = _extract_nct_id(st)
        if args.in_audit_index is None and (not nid or not _SYNTHETIC_ID_RX.fullmatch(nid)):
            raise SystemExit(
                "Non-synthetic NCT ID encountered without --in-audit-index; refusing to run without firewall"
            )
        if nid in in_audit_ids:
            n_excluded_in_audit += 1
            continue
        frow = build_eligibility_row(st, snapshot_date=snap_date)
        lrow = lean_eligibility_row(frow)
        full_rows.append(frow)
        lean_rows.append(lrow)

    checks_full = structural_checks(full_rows)
    checks_lean = structural_checks(lean_rows)
    if not all_structural_checks_pass(checks_full) or not all_structural_checks_pass(checks_lean):
        raise SystemExit(f"eligibility structural_checks failed: full={checks_full}, lean={checks_lean}")

    summary_full = summarize_cohort_barriers(full_rows)
    summary_lean = summarize_cohort_barriers(lean_rows)
    if summary_full != summary_lean:
        raise SystemExit("Lean-vs-full cohort barrier summary parity mismatch")

    run_doc: dict[str, Any] = {
        "parser_module_version": PARSER_MODULE_VERSION,
        "vocabulary_version": VOCABULARY_VERSION,
        "vocabulary_sha256": VOCABULARY_SHA256,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshot_date": snap_date,
        "pipeline_commit": _pipeline_commit_or_none(),
        "source_sha256": source_sha,
        "in_audit_firewall_enforced": args.in_audit_index is not None,
        "in_audit_index_sha256": in_audit_sha,
        "n_input_records": len(raw_studies),
        "n_excluded_in_audit": n_excluded_in_audit,
        "n_evaluated": len(full_rows),
        "structural_checks": checks_full,
    }

    out_dir: Path = args.out.resolve()
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix=f".{out_dir.name}.staging.", dir=str(out_dir.parent)))
    backup_dir: Path | None = None
    try:
        (stage_dir / "run.json").write_text(json.dumps(run_doc, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        (stage_dir / "eligibility_summary.json").write_text(
            json.dumps(summary_full, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        with gzip.open(stage_dir / "eligibility_barriers.jsonl.gz", "wt", encoding="utf-8") as fh:
            for frow in full_rows:
                fh.write(json.dumps(frow, allow_nan=False, separators=(",", ":")) + "\n")
        with gzip.open(stage_dir / "eligibility_lean.jsonl.gz", "wt", encoding="utf-8") as fh:
            for lrow in lean_rows:
                fh.write(json.dumps(lrow, allow_nan=False, separators=(",", ":")) + "\n")

        if out_dir.exists():
            backup_dir = out_dir.parent / f".{out_dir.name}.backup.{os.getpid()}"
            if backup_dir.exists():
                shutil.rmtree(backup_dir)
            os.replace(out_dir, backup_dir)
        os.replace(stage_dir, out_dir)
        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(backup_dir)
    except Exception:
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)
        if backup_dir is not None and backup_dir.exists() and not out_dir.exists():
            os.replace(backup_dir, out_dir)
        raise

    return 0
