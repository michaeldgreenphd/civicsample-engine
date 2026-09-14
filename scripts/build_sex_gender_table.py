#!/usr/bin/env python3
"""Tabulate the sex/gender rows into the published table, or re-parse them.

READS   data/demographics.json (the weekly pull; or --parts for the split
        data/demographics.part*.json.gz) and takes study["sex_gender"] as-is,
        OR --from-raw data/sex_gender_raw_measures.jsonl.gz to RE-PARSE every
        trial with the parser vendored in src/ (the path a rule bump takes; no
        registry pull needed).
WRITES  data/sex_gender_parsed.csv.gz        one row per trial, columns in
                                             src/sex_gender_table.py COLUMNS order
        data/sex_gender_parsed_meta.json     provenance, status counts, the
                                             structural checks, drift vs the
                                             2026-06-09 baseline (reported, never
                                             fatal on a fresh pull)
INVOKED by .github/workflows/extract.yml after split_data.py. Run from the repo
        root. --strict exits 1 when a structural check fails (CI does not use it:
        the table publishes and the failure is a ::warning:: plus an audit row).
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import sex_gender_table as sgt  # noqa: E402
from src.utils import pipeline_commit  # noqa: E402

BASELINE_PATH = os.path.join("tests", "sex_gender", "fixtures", "snapshot_baseline_2026-06-09.json")


def load_records(demographics: str | None, parts_glob: str | None):
    if demographics and os.path.exists(demographics):
        with open(demographics) as f:
            c = json.load(f)
        return c["data"], c.get("extracted_at"), c.get("pipeline_commit")
    paths = sorted(glob.glob(parts_glob or "data/demographics.part*.json.gz"))
    if not paths:
        raise SystemExit("no demographics.json and no demographics.part*.json.gz found")
    records, extracted_at, commit = [], None, None
    for p in paths:
        with gzip.open(p, "rt") as f:
            c = json.load(f)
        records.extend(c["data"])
        extracted_at = extracted_at or c.get("extracted_at")
        commit = commit or c.get("pipeline_commit")
    return records, extracted_at, commit


def rows_from_records(records) -> tuple[list, list]:
    """(rows, missing_nct_ids): the sex_gender row of every record that has one."""
    rows, missing = [], []
    for s in records:
        r = s.get("sex_gender")
        if r:
            rows.append(sgt.ordered(r))
        else:
            missing.append(s.get("nct_id"))
    return rows, missing


def rows_from_raw(path: str, snapshot_date: str | None) -> tuple[list, str | None]:
    rows, first_date = [], None
    with gzip.open(path, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                rows.append(sgt.ordered(sgt.parse_error_row(None, None, snapshot_date or sgt.today_utc())))
                continue
            first_date = first_date or raw.get("snapshot_date")
            rows.append(sgt.ordered(sgt.build_row_from_raw(
                raw, snapshot_date or raw.get("snapshot_date") or sgt.today_utc())))
    return rows, first_date


def write_table(rows: list, out_csv: str) -> None:
    df = pd.DataFrame(rows, columns=sgt.COLUMNS)
    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    df.to_csv(out_csv, index=False, compression="gzip" if out_csv.endswith(".gz") else None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demographics", default="data/demographics.json")
    ap.add_argument("--parts", default="data/demographics.part*.json.gz")
    ap.add_argument("--from-raw", default=None, help="re-parse this raw-measures jsonl.gz instead")
    ap.add_argument("--snapshot-date", default=None, help="override the stamped snapshot date (re-parse only)")
    ap.add_argument("--out", default="data/sex_gender_parsed.csv.gz")
    ap.add_argument("--meta", default="data/sex_gender_parsed_meta.json")
    ap.add_argument("--baseline", default=BASELINE_PATH)
    ap.add_argument("--write-back", default=None,
                    help="demographics.json to update in place: every study's sex_gender key becomes the "
                         "lean subset of the row built here (the re-parse path; run before split_data.py)")
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args()

    extracted_at = commit = None
    missing: list = []
    if a.from_raw:
        rows, snapshot_date = rows_from_raw(a.from_raw, a.snapshot_date)
        source = f"re-parsed from {a.from_raw}"
        snapshot_date = a.snapshot_date or snapshot_date
    else:
        records, extracted_at, commit = load_records(a.demographics, a.parts)
        rows, missing = rows_from_records(records)
        source = "study['sex_gender'] rows of the pull"
        snapshot_date = rows[0]["snapshot_date"] if rows else None

    checks = sgt.structural_checks(rows)
    counts = sgt.status_counts(rows)
    drift = []
    if os.path.exists(a.baseline):
        drift = sgt.baseline_drift(rows, json.load(open(a.baseline)))

    write_table(rows, a.out)
    written_back = None
    if a.write_back:
        by_nct = {r["nct_id"]: sgt.lean_row(r) for r in rows if r.get("nct_id")}
        with open(a.write_back) as f:
            container = json.load(f)
        hit = 0
        for s in container["data"]:
            lean = by_nct.get(s.get("nct_id"))
            if lean is not None:
                s["sex_gender"] = lean
                hit += 1
        with open(a.write_back, "w") as f:
            json.dump(container, f, indent=2)
        written_back = {"path": a.write_back, "records": len(container["data"]), "updated": hit}
        print(f"wrote back lean sex_gender rows into {a.write_back}: {hit} of {len(container['data'])} records")
    meta = {
        "written_back": written_back,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_commit": pipeline_commit() or commit,
        "source_extracted_at": extracted_at,
        "snapshot_date": snapshot_date,
        "source": source,
        "parser_rules_version": sgt.sgp.PARSER_RULES_VERSION,
        "parser_module_version": sgt.sgp.__version__,
        "n_rows": len(rows),
        "n_records_without_row": len(missing),
        "records_without_row": missing[:50],
        "columns": sgt.COLUMNS,
        "status_counts": counts,
        "structural_checks": checks,
        "structural_checks_all_pass": sgt.all_pass(checks),
        "baseline": {"path": a.baseline, "is_baseline_data": sgt.is_baseline_data({"snapshot_date": snapshot_date}),
                     "drift": drift, "exact_match": not drift},
    }
    os.makedirs(os.path.dirname(a.meta) or ".", exist_ok=True)
    with open(a.meta, "w") as f:
        json.dump(meta, f, indent=2, default=str)

    print(f"wrote {a.out}: {len(rows)} rows ({source}); rules {meta['parser_rules_version']}")
    print("status:", counts["status"])
    print("outcomes:", counts["outcomes"])
    for name, c in checks.items():
        print(f"  [{'ok' if c['pass'] else 'FAIL'}] {name} {c['detail']}")
    if drift:
        print(f"drift vs {sgt.BASELINE_SNAPSHOT_DATE} baseline ({'BASELINE DATA: this is a failure' if meta['baseline']['is_baseline_data'] else 'fresh pull: reported only'}):")
        for line in drift:
            print("   ", line)
    failed = not sgt.all_pass(checks)
    if failed:
        print("::warning::sex/gender structural check(s) failed; see sex_gender_parsed_meta.json")
    if meta["baseline"]["is_baseline_data"] and drift:
        print("::error::sex/gender table parsed from the 2026-06-09 baseline does not reproduce the baseline counts")
        return 1
    return 1 if (failed and a.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
