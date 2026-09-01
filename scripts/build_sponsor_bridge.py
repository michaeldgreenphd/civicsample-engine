#!/usr/bin/env python3
"""Build the sponsor bridge table for the dashboard's company filter.

READS   data/demographics.json (the weekly pull; or --parts to read the
        split data/demographics.part*.json.gz instead), sponsors/company_aliases.csv
WRITES  data/sponsors/bridge.csv.gz   one row per (nct_id, canonical, entity, role)
        data/sponsors/bridge_meta.json provenance + adapter mismatch log
INVOKED by .github/workflows/extract.yml after the demographics publish
        (a rules conflict raises here and fails the run — after the site
        already has its data). Run from the repo root.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sponsors import sponsor_roles as sr  # noqa: E402
from sponsors.adapter import (AACT_LOADER_COMMIT, AACT_SOURCE_FILES,  # noqa: E402
                              records_to_frame)

BRIDGE_COLUMNS = ["nct_id", "canonical", "entity", "role", "literal_name",
                  "agency_class", "match_rule", "shared"]
MODULE_VERSION = "v3.1"


def sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def pipeline_commit() -> str | None:
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return None


def load_records(demographics: str | None, parts_glob: str | None):
    """Yield (records, extracted_at). Full JSON if present, else the parts."""
    if demographics and os.path.exists(demographics):
        with open(demographics) as f:
            c = json.load(f)
        return c["data"], c.get("extracted_at")
    paths = sorted(glob.glob(parts_glob or "data/demographics.part*.json.gz"))
    if not paths:
        raise SystemExit("no demographics.json and no demographics.part*.json.gz found")
    records, extracted_at = [], None
    for p in paths:
        with gzip.open(p, "rt") as f:
            c = json.load(f)
        extracted_at = extracted_at or c.get("extracted_at")
        records.extend(c["data"])
    return records, extracted_at


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demographics", default="data/demographics.json")
    ap.add_argument("--parts", default="data/demographics.part*.json.gz")
    ap.add_argument("--rules", default="sponsors/company_aliases.csv")
    ap.add_argument("--out-dir", default="data/sponsors")
    a = ap.parse_args()

    records, extracted_at = load_records(a.demographics, a.parts)
    frame, log = records_to_frame(records)
    rules = sr.load_rules(a.rules)
    index = sr.build_index(frame, rules)          # raises on rule conflicts (invariant 6)
    index = index[BRIDGE_COLUMNS]

    os.makedirs(a.out_dir, exist_ok=True)
    bridge_path = os.path.join(a.out_dir, "bridge.csv.gz")
    index.to_csv(bridge_path, index=False, compression="gzip")

    meta = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source_extracted_at": extracted_at,
        "pipeline_commit": pipeline_commit(),
        "rules_sha256": sha256_file(a.rules),
        "module_version": MODULE_VERSION,
        "aact_loader_commit": AACT_LOADER_COMMIT,
        "aact_source_files": AACT_SOURCE_FILES,
        "n_rows": int(len(index)),
        "n_companies": int(index.canonical.nunique()),
        "n_trials": int(index.nct_id.nunique()),
        "n_shared_rows": int((index.shared == "yes").sum()),
        "bridge_bytes_gz": os.path.getsize(bridge_path),
        "adapter_log": log.to_dict(),
    }
    with open(os.path.join(a.out_dir, "bridge_meta.json"), "w") as f:
        json.dump(meta, f, indent=2)

    print(f"bridge: {meta['n_rows']} rows, {meta['n_companies']} companies, "
          f"{meta['n_trials']} trials, {meta['bridge_bytes_gz'] / 1024:.0f} KB gz")
    for line in log.lines():
        print("adapter:", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
