"""Manual CLI builder for multi-site US catchment footprints and ACS 5-year bounds.

Invoked by hand via ``python scripts/build_multisite_catchments.py`` or ``python -m catchments``;
never scheduled. Supports ``--footprint-only`` (offline across a weekly ``demographics.json.gz``
or API v2 study file with no Census API key) and full cached-ACS multi-site benchmarking.
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

from catchments import METHOD_VERSION, SCHEMA_VERSION
from catchments.benchmark import benchmark_multisite_trial, pool_multisite_cohort
from catchments.footprint import FOOTPRINT_TIERS, classify_footprint
from census import acs, geography, trials

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


def _load_release_records(path: Path) -> tuple[list[dict[str, Any]], str | None, str | None]:
    if not path.is_file():
        raise SystemExit(f"release file not found: {path}")
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        container = json.load(fh)
    if isinstance(container, dict) and isinstance(container.get("data"), list):
        return (
            [x for x in container["data"] if isinstance(x, dict)],
            container.get("extracted_at"),
            container.get("pipeline_commit"),
        )
    if isinstance(container, dict) and isinstance(container.get("studies"), list):
        return [x for x in container["studies"] if isinstance(x, dict)], None, None
    if isinstance(container, list):
        return [x for x in container if isinstance(x, dict)], None, None
    raise SystemExit(f"{path}: unrecognized release container format")


def _pipeline_commit_or_none() -> str | None:
    try:
        from src.utils import pipeline_commit
        return pipeline_commit()
    except Exception:
        return None


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build deterministic multi-site US footprints and ACS 5-year catchment bounds."
    )
    parser.add_argument("--release", type=Path, required=True, help="Path to demographics.json[.gz] or study JSON")
    parser.add_argument("--release-tag", type=str, required=True, help="Source release tag (e.g. data-2026-11-22)")
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
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--footprint-only",
        action="store_true",
        help="Classify facility footprints across the cohort offline without ACS/boundary caches",
    )
    mode_group.add_argument(
        "--census-cache",
        type=Path,
        help="Path to cached ACS 5-year tables and cartographic boundary files (see census/README.md)",
    )
    parser.add_argument("--floor", type=int, default=50, help="Minimum qualifying trials for pooled cohort display")
    parser.add_argument("--resamples", type=int, default=2000, help="Bootstrap resamples over trials")
    parser.add_argument("--seed", type=int, default=20261123, help="Bootstrap generator seed")
    parser.add_argument("--out", type=Path, required=True, help="Output directory for generated artifacts")
    args = parser.parse_args(list(argv) if argv is not None else None)

    release_sha = _sha256_file(args.release)
    in_audit_ids: set[str] = set()
    in_audit_sha: str | None = None
    if args.in_audit_index is not None:
        in_audit_ids, in_audit_sha = load_in_audit_firewall(args.in_audit_index)
    elif not args.synthetic_ids_only:
        raise SystemExit(
            "--in-audit-index is required unless --synthetic-ids-only is explicitly set on synthetic inputs"
        )

    raw_records, source_extracted_at, source_pipeline_commit = _load_release_records(args.release)
    n_excluded_in_audit = 0
    kept_records: list[dict[str, Any]] = []
    for rec in raw_records:
        nid = _extract_nct_id(rec)
        if args.in_audit_index is None and (not nid or not _SYNTHETIC_ID_RX.fullmatch(nid)):
            raise SystemExit(
                "Non-synthetic NCT ID encountered without --in-audit-index; refusing to run without firewall"
            )
        if nid in in_audit_ids:
            n_excluded_in_audit += 1
            continue
        kept_records.append(rec)

    tier_counts: dict[str, int] = {t: 0 for t in FOOTPRINT_TIERS}
    source_counts: collections.Counter[str] = collections.Counter()
    footprint_rows: list[dict[str, Any]] = []
    n_us_50_dc_facilities = 0
    n_us_facilities_with_zip5 = 0

    for rec in kept_records:
        fp = classify_footprint(rec)
        tier_counts[fp["footprint_tier"]] = tier_counts.get(fp["footprint_tier"], 0) + 1
        source_counts[fp["location_source"]] += 1
        n_us_50_dc_facilities += fp["n_us_state_or_dc_facilities"]
        for fac in fp["facilities"]:
            if fac["jurisdiction"] == "us_state_or_dc" and fac["zip5"]:
                n_us_facilities_with_zip5 += 1
        footprint_rows.append(
            {
                "nct_id": _extract_nct_id(rec),
                "footprint_tier": fp["footprint_tier"],
                "location_source": fp["location_source"],
                "weekly_projection_omits_countryless_sites": fp["weekly_projection_omits_countryless_sites"],
                "n_facilities": fp["n_facilities"],
                "n_us_state_or_dc_facilities": fp["n_us_state_or_dc_facilities"],
                "n_us_territory_facilities": fp["n_us_territory_facilities"],
                "n_non_us_facilities": fp["n_non_us_facilities"],
                "eligible_for_us_multisite_rollup": fp["eligible_for_us_multisite_rollup"],
                "global_pooled_baseline_table": fp["global_pooled_baseline_table"],
                "us_states_and_dc": fp["us_states_and_dc"],
            }
        )

    footprint_summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "release_tag": args.release_tag,
        "n_input_records": len(raw_records),
        "n_excluded_in_audit": n_excluded_in_audit,
        "n_evaluated": len(kept_records),
        "footprint_tier_counts": tier_counts,
        "location_source_counts": dict(source_counts),
        "eligible_for_us_multisite_rollup": tier_counts["multi_site_us_only"],
        "global_pooled_baseline_table": tier_counts["mixed_us_and_international"],
        "n_us_state_or_dc_facilities": n_us_50_dc_facilities,
        "n_us_state_or_dc_facilities_with_zip5": n_us_facilities_with_zip5,
    }

    trial_benchmarks: list[dict[str, Any]] = []
    cohort_summary: dict[str, Any] | None = None

    if not args.footprint_only:
        assert args.census_cache is not None
        cache_dir = args.census_cache
        if not cache_dir.is_dir():
            raise SystemExit(f"census cache directory not found: {cache_dir}")
        vintages = sorted({hi for _, hi in trials.WINDOWS})
        levels_by_vintage: dict[int, dict[str, acs.Level]] = {}
        resolvers_by_vintage: dict[int, geography.Resolver] = {}
        all_zips = {
            fac["zip5"]
            for rec in kept_records
            for fac in classify_footprint(rec)["facilities"]
            if fac["zip5"]
        }
        for v in vintages:
            v_levels = acs.LEVELS_BY_VINTAGE.get(v, tuple(acs.LEVELS))
            levels_by_vintage[v] = {lv: acs.load(v, lv, cache_dir) for lv in v_levels}
            zcta_pub = set(levels_by_vintage[v]["zcta"].index) if "zcta" in levels_by_vintage[v] else None
            resolvers_by_vintage[v] = geography.Resolver(v, cache_dir, all_zips, zcta_pub)

        for rec in kept_records:
            fp_tier = classify_footprint(rec)["footprint_tier"]
            if fp_tier not in ("multi_site_us_only", "mixed_us_and_international", "single_us_facility"):
                continue
            _, exact_mid = trials.enrolment_midpoint(rec)
            win = trials.assign_window(exact_mid)
            v = win.get("vintage")
            lv_map = levels_by_vintage.get(v, {}) if isinstance(v, int) else {}
            res_v = resolvers_by_vintage.get(v) if isinstance(v, int) else None
            tb = benchmark_multisite_trial(rec, lv_map, res_v)
            trial_benchmarks.append(tb)

        cohort_summary = {
            dim: pool_multisite_cohort(
                trial_benchmarks,
                level="cbsa",
                dimension=dim,
                floor=args.floor,
                resamples=args.resamples,
                seed=args.seed,
            )
            for dim in ("race", "ethnicity", "sex")
        }

    run_doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pipeline_commit": _pipeline_commit_or_none(),
        "mode": "footprint_only" if args.footprint_only else "full_acs_benchmark",
        "release_tag": args.release_tag,
        "release_sha256": release_sha,
        "source_extracted_at": source_extracted_at,
        "source_pipeline_commit": source_pipeline_commit,
        "in_audit_firewall_enforced": args.in_audit_index is not None,
        "in_audit_index_sha256": in_audit_sha,
        "n_input_records": len(raw_records),
        "n_excluded_in_audit": n_excluded_in_audit,
        "n_evaluated": len(kept_records),
    }

    out_dir: Path = args.out.resolve()
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix=f".{out_dir.name}.staging.", dir=str(out_dir.parent)))
    backup_dir: Path | None = None
    try:
        (stage_dir / "run.json").write_text(json.dumps(run_doc, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        (stage_dir / "footprint_summary.json").write_text(
            json.dumps(footprint_summary, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        with gzip.open(stage_dir / "footprints.jsonl.gz", "wt", encoding="utf-8") as fh:
            for frow in footprint_rows:
                fh.write(json.dumps(frow, allow_nan=False, separators=(",", ":")) + "\n")
        if not args.footprint_only:
            with gzip.open(stage_dir / "trials.jsonl.gz", "wt", encoding="utf-8") as fh:
                for tb in trial_benchmarks:
                    fh.write(json.dumps(tb, allow_nan=False, separators=(",", ":")) + "\n")
            (stage_dir / "cohort_summary.json").write_text(
                json.dumps(cohort_summary, indent=2, allow_nan=False) + "\n", encoding="utf-8"
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
