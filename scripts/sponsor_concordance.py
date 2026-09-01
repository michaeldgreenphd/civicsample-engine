#!/usr/bin/env python3
"""Per-trial concordance of our adapter vs the AACT 2026-06-19 fixture (A1).

READS   the weekly pull (or parts), sponsors/company_aliases.csv,
        tests/sponsors/fixtures/sponsors_fixture_20260619.csv.gz
WRITES  a report to stdout and --out JSON; disagreement rows to --out-dir
Run from the repo root. Not part of the weekly job; run for reviews.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from build_sponsor_bridge import load_records  # noqa: E402
from sponsors import sponsor_roles as sr  # noqa: E402
from sponsors.adapter import records_to_frame  # noqa: E402
from sponsors.concordance import cohort_count_table, concordance  # noqa: E402

FIXTURE = "tests/sponsors/fixtures/sponsors_fixture_20260619.csv.gz"
COMPANIES = ["Pfizer", "Merck & Co", "Merck KGaA", "Bristol-Myers Squibb"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demographics", default="data/demographics.json")
    ap.add_argument("--parts", default="data/demographics.part*.json.gz")
    ap.add_argument("--rules", default="sponsors/company_aliases.csv")
    ap.add_argument("--fixture", default=FIXTURE)
    ap.add_argument("--out-dir", default=None)
    a = ap.parse_args()

    records, extracted_at = load_records(a.demographics, a.parts)
    pull_frame, log = records_to_frame(records)
    rules = sr.load_rules(a.rules)
    pull_index = sr.build_index(pull_frame, rules)

    fixture = pd.read_csv(a.fixture, sep="|", dtype=str)
    fixture_index = sr.build_index(fixture, rules)

    c = concordance(pull_index, fixture_index)
    shared = set(pull_index.nct_id) & set(fixture_index.nct_id)
    table_pull = cohort_count_table(pull_index[pull_index.nct_id.isin(shared)], COMPANIES)
    table_fix = cohort_count_table(fixture_index[fixture_index.nct_id.isin(shared)], COMPANIES)
    table_full = cohort_count_table(pull_index, COMPANIES)

    total = c["agree"] + c["pull_only"]["n_pairs"] + c["fixture_only"]["n_pairs"]
    print(f"pull extracted_at: {extracted_at}")
    print(f"shared trials (pull ∩ fixture): {c['n_shared_trials']}")
    print(f"(nct_id, canonical, role) pairs: agree {c['agree']} | pull-only {c['pull_only']['n_pairs']} "
          f"| fixture-only {c['fixture_only']['n_pairs']}  (agreement {100 * c['agree'] / max(total, 1):.2f}%)")
    for side in ("pull_only", "fixture_only"):
        print(f"\n{side}: causes {c[side]['causes']}")
        for t in c[side]["top_literals"]:
            print(f"   {t['n_trials']:5d}  {t['literal']}")
    print("\nCohort-scoped count table (shared trials only): pull vs fixture")
    print(table_pull.merge(table_fix, on="company", suffixes=("_pull", "_fixture")).to_string(index=False))
    print("\nFull-pull count table (our whole posted-results universe):")
    print(table_full.to_string(index=False))
    print("\nadapter log:")
    for line in log.lines():
        print("  ", line)

    if a.out_dir:
        os.makedirs(a.out_dir, exist_ok=True)
        c["pull_only"]["rows"].to_csv(os.path.join(a.out_dir, "pull_only.csv"), index=False)
        c["fixture_only"]["rows"].to_csv(os.path.join(a.out_dir, "fixture_only.csv"), index=False)
        out = {k: v for k, v in c.items() if k not in ("pull_only", "fixture_only")}
        out["pull_only"] = {k: v for k, v in c["pull_only"].items() if k != "rows"}
        out["fixture_only"] = {k: v for k, v in c["fixture_only"].items() if k != "rows"}
        out["count_table_cohort"] = table_pull.merge(table_fix, on="company", suffixes=("_pull", "_fixture")).to_dict("records")
        out["count_table_full_pull"] = table_full.to_dict("records")
        out["adapter_log"] = log.to_dict()
        out["pull_extracted_at"] = extracted_at
        with open(os.path.join(a.out_dir, "concordance.json"), "w") as f:
            json.dump(out, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
