"""Manual CLI builder for baseline chronological age tables and representation-gap audits.

Invoked by hand via ``python scripts/build_age_table.py`` or ``python -m age``; never wired
into the scheduled weekly job without explicit integration review. Enforces a fail-closed
``in_audit`` firewall whenever any input record carries a non-synthetic NCT ID.
"""
from __future__ import annotations

import argparse
import collections
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

from age import (
    METHOD_VERSION,
    PARSER_RULES_VERSION,
    RAW_SCHEMA_VERSION,
    SCHEMA_VERSION,
    VOCABULARY_SHA256,
    all_pass,
    audit_eligibility_vs_enrolled,
    build_age_row,
    build_age_row_from_raw,
    read_raw_age_jsonl,
    select_raw_age_measures,
    status_counts,
    structural_checks,
    write_age_csv,
    write_age_jsonl,
    write_raw_age_jsonl,
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
        if "protocolSection" in payload or "resultsSection" in payload:
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
        description="Build deterministic baseline age table, raw envelope, and representation-gap summary."
    )
    src_group = parser.add_mutually_exclusive_group(required=True)
    src_group.add_argument("--studies", type=Path, help="Path to CT.gov API v2 studies (.json/.jsonl[.gz])")
    src_group.add_argument("--from-raw", type=Path, help="Path to retained age_raw_measures.jsonl[.gz]")
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
    parser.add_argument("--out", type=Path, required=True, help="Output directory for generated age artifacts")
    args = parser.parse_args(list(argv) if argv is not None else None)

    source_path: Path = args.studies if args.studies is not None else args.from_raw
    source_sha = _sha256_file(source_path)

    in_audit_ids: set[str] = set()
    in_audit_sha: str | None = None
    if args.in_audit_index is not None:
        in_audit_ids, in_audit_sha = load_in_audit_firewall(args.in_audit_index)
    elif not args.synthetic_ids_only:
        raise SystemExit(
            "--in-audit-index is required unless --synthetic-ids-only is explicitly set on synthetic inputs"
        )

    snap_date = args.snapshot_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows: list[dict[str, Any]] = []
    raw_envelopes: list[dict[str, Any]] = []
    gap_records: list[dict[str, Any]] = []
    n_excluded_in_audit = 0
    n_input_records = 0

    if args.studies is not None:
        raw_studies = _load_studies_file(args.studies)
        n_input_records = len(raw_studies)
        for st in raw_studies:
            nid = _extract_nct_id(st)
            if args.in_audit_index is None and (not nid or not _SYNTHETIC_ID_RX.fullmatch(nid)):
                raise SystemExit(
                    "Non-synthetic NCT ID encountered without --in-audit-index; refusing to run without firewall"
                )
            if nid in in_audit_ids:
                n_excluded_in_audit += 1
                continue
            env = select_raw_age_measures(st)
            row = build_age_row_from_raw(env, snapshot_date=snap_date)
            gap = audit_eligibility_vs_enrolled(st, row)
            raw_envelopes.append(env)
            rows.append(row)
            gap_records.append({"nct_id": row.get("nct_id"), **gap})
    else:
        envelopes = read_raw_age_jsonl(str(args.from_raw))
        n_input_records = len(envelopes)
        for env in envelopes:
            nid = _extract_nct_id(env)
            if args.in_audit_index is None and (not nid or not _SYNTHETIC_ID_RX.fullmatch(nid)):
                raise SystemExit(
                    "Non-synthetic NCT ID encountered without --in-audit-index; refusing to run without firewall"
                )
            if nid in in_audit_ids:
                n_excluded_in_audit += 1
                continue
            row = build_age_row_from_raw(env, snapshot_date=snap_date)
            gap = audit_eligibility_vs_enrolled(env, row)
            rows.append(row)
            gap_records.append({"nct_id": row.get("nct_id"), **gap})

    checks = structural_checks(rows)
    if not all_pass(checks):
        raise SystemExit(f"age structural_checks failed: {checks}")

    gap_counts: dict[str, int] = {
        "nominally_open_65_plus": sum(1 for g in gap_records if g["nominally_open_65_plus"]),
        "older_adult_representation_gap": sum(1 for g in gap_records if g["older_adult_representation_gap"]),
        "nominally_open_75_plus": sum(1 for g in gap_records if g["nominally_open_75_plus"]),
        "older_adult_75_plus_representation_gap": sum(
            1 for g in gap_records if g["older_adult_75_plus_representation_gap"]
        ),
        "nominally_open_pediatric": sum(1 for g in gap_records if g["nominally_open_pediatric"]),
        "pediatric_representation_gap": sum(1 for g in gap_records if g["pediatric_representation_gap"]),
        "enrolled_outside_eligibility_bounds": sum(
            1 for g in gap_records if g["enrolled_outside_eligibility_bounds"]
        ),
    }

    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "parser_rules_version": PARSER_RULES_VERSION,
        "raw_schema_version": RAW_SCHEMA_VERSION,
        "vocabulary_sha256": VOCABULARY_SHA256,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshot_date": snap_date,
        "pipeline_commit": _pipeline_commit_or_none(),
        "source_mode": "studies" if args.studies is not None else "from_raw",
        "source_sha256": source_sha,
        "in_audit_firewall_enforced": args.in_audit_index is not None,
        "in_audit_index_sha256": in_audit_sha,
        "n_input_records": n_input_records,
        "n_excluded_in_audit": n_excluded_in_audit,
        "n_evaluated": len(rows),
        "counts": status_counts(rows),
        "representation_gap_counts": gap_counts,
        "structural_checks": checks,
    }

    out_dir: Path = args.out.resolve()
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix=f".{out_dir.name}.staging.", dir=str(out_dir.parent)))
    backup_dir: Path | None = None
    try:
        write_age_csv(rows, str(stage_dir / "age_parsed.csv.gz"))
        write_age_jsonl(rows, str(stage_dir / "age_parsed.jsonl.gz"))
        if raw_envelopes:
            write_raw_age_jsonl(raw_envelopes, str(stage_dir / "age_raw_measures.jsonl.gz"))
        with gzip.open(stage_dir / "age_representation_gaps.jsonl.gz", "wt", encoding="utf-8") as fh:
            for g in gap_records:
                fh.write(json.dumps(g, allow_nan=False, separators=(",", ":")) + "\n")
        (stage_dir / "age_summary.json").write_text(
            json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )

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
