#!/usr/bin/env python3
"""
Generate a pilot target list of the 50 largest clinical trials by enrollment.

Reads every `data/demographics.part*.json.gz` shard, sorts trials by
participant count descending, and writes the top 50 to
`data/pilot_clinical_trials_targets.csv` for use as input to an external
literature-search pilot (Google Colab).

Schema notes (discovered from `data/details.part1.json.gz` and the
`demographics` shards):
  - `details.part*.json.gz` records contain only narrative fields
    (study_sites, outcome descriptions, intervention_model_description) —
    they do NOT carry enrollment or condition.
  - `demographics.part*.json.gz` records carry the trial-level metadata:
    `nct_id`, `enrollment` (int), `primary_condition`, `conditions`,
    `intervention_model_description`, `intervention_model`.

Output columns (exact headers, in order):
  NCT Number, Condition, Intervention, Total Participants
"""

import csv
import glob
import gzip
import json
import os
import sys

DEMOGRAPHICS_GLOB = "data/demographics.part*.json.gz"
OUTPUT_CSV = "data/pilot_clinical_trials_targets.csv"
TOP_N = 50


def load_trials(pattern: str) -> list[dict]:
    paths = sorted(glob.glob(pattern))
    if not paths:
        print(f"Error: no shards matched {pattern}", file=sys.stderr)
        sys.exit(1)
    trials: list[dict] = []
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            obj = json.load(f)
        records = obj.get("data", [])
        print(f"  {os.path.basename(path)}: {len(records):,} records")
        trials.extend(records)
    return trials


def pick_condition(rec: dict) -> str:
    primary = (rec.get("primary_condition") or "").strip()
    if primary:
        return primary
    conditions = rec.get("conditions") or []
    if isinstance(conditions, list) and conditions:
        return str(conditions[0]).strip()
    secondary = (rec.get("secondary_condition") or "").strip()
    return secondary


def pick_intervention(rec: dict) -> str:
    desc = (rec.get("intervention_model_description") or "").strip()
    if desc:
        return desc
    model = (rec.get("intervention_model") or "").strip()
    return model


def main():
    print(f"Loading shards from {DEMOGRAPHICS_GLOB}")
    trials = load_trials(DEMOGRAPHICS_GLOB)
    print(f"Loaded {len(trials):,} total trial records\n")

    eligible = [t for t in trials if isinstance(t.get("enrollment"), int) and t.get("nct_id")]
    print(f"Eligible (have nct_id + integer enrollment): {len(eligible):,}")

    eligible.sort(key=lambda r: r["enrollment"], reverse=True)
    top = eligible[:TOP_N]
    print(f"Top {TOP_N} enrollment range: {top[0]['enrollment']:,} → {top[-1]['enrollment']:,}\n")

    os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["NCT Number", "Condition", "Intervention", "Total Participants"])
        for rec in top:
            writer.writerow([
                rec["nct_id"],
                pick_condition(rec),
                pick_intervention(rec),
                rec["enrollment"],
            ])

    print(f"Wrote {len(top)} rows to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
