#!/usr/bin/env python3
"""
Generate a pilot target list of the 50 largest clinical trials by enrollment.

Reads the week's full records (`data/demographics.json`, src.full_records;
pass --demographics for another week's file), sorts trials by participant
count descending, and writes the top 50 to
`data/pilot_clinical_trials_targets.csv` for use as input to an external
literature-search pilot (Google Colab).

The records carry the trial-level fields used here: `nct_id`, `enrollment`
(int), `primary_condition`, `conditions`, `intervention_model_description`,
`intervention_model`. The site's `demographics.part*.json.gz` are not read:
they are about to drop `intervention_model_description`, which would silently
turn every Intervention cell into the model name.

Output columns (exact headers, in order):
  NCT Number, Condition, Intervention, Total Participants
"""

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import full_records  # noqa: E402

OUTPUT_CSV = "data/pilot_clinical_trials_targets.csv"
TOP_N = 50


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


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Write the pilot target list.")
    ap.add_argument("--demographics", default=full_records.DEFAULT_PATH,
                    help="the week's full records (default: %(default)s)")
    a = ap.parse_args(argv)
    print(f"Loading the full records from {a.demographics}")
    trials, _, _ = full_records.load(a.demographics)
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
