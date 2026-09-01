#!/usr/bin/env python3
"""Cohort-ranked curation batch (phase Part 3).

The top-N unmatched INDUSTRY lead literals ranked within OUR posted-results
cohort, with trial counts, roles, and the precomputed normalized pattern —
a ready-to-curate list that comes back as a rules PR.

READS   the weekly pull (or parts), the rules
WRITES  --out (default data/sponsor_audit/curation_batches/<date>_industry_leads_top<N>.csv)
Run from the repo root.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from build_sponsor_bridge import load_records  # noqa: E402
from sponsors import sponsor_roles as sr  # noqa: E402
from sponsors.adapter import records_to_frame  # noqa: E402
from sponsors.audit_states import partition  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demographics", default="data/demographics.json")
    ap.add_argument("--parts", default="data/demographics.part*.json.gz")
    ap.add_argument("--rules", default="sponsors/company_aliases.csv")
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    rules_sha = hashlib.sha256(open(a.rules, "rb").read()).hexdigest()
    records, extracted_at = load_records(a.demographics, a.parts)
    frame, _ = records_to_frame(records)
    rules = sr.load_rules(a.rules)
    states = partition(frame, rules)
    unreviewed = states["unreviewed"]

    leads = frame[(frame.lead_or_collaborator == "lead") & (frame.agency_class == "INDUSTRY")
                  & frame["name"].isin(unreviewed)]
    n_lead = leads.groupby("name").nct_id.nunique().rename("n_lead_trials")
    n_any = frame[frame["name"].isin(n_lead.index)].groupby("name").nct_id.nunique().rename("n_any_trials")
    roles = (frame[frame["name"].isin(n_lead.index)].groupby("name")["lead_or_collaborator"]
             .agg(lambda r: ",".join(sorted(set(r)))).rename("roles"))
    out = (pd.concat([n_lead, n_any, roles], axis=1).reset_index()
           .sort_values(["n_lead_trials", "name"], ascending=[False, True]).head(a.top).reset_index(drop=True))
    out.insert(0, "rank", range(1, len(out) + 1))
    out["normalized_pattern"] = out["name"].map(sr.normalize)
    out["suggested_rule_type"] = "exact_normalized"
    out["parsed_parent"] = out["name"].map(lambda n: sr.parsed_parent(n) or "")
    out["draft_rule_line"] = out.apply(
        lambda r: f'<canonical>,exact_normalized,"{r.normalized_pattern}",attribute,no,"<initials> <date>: <why>"', axis=1)
    out["rules_sha256"] = rules_sha
    out["cohort_extracted_at"] = extracted_at

    snapshot = (extracted_at or "")[:10] or "undated"
    path = a.out or f"data/sponsor_audit/curation_batches/{snapshot}_industry_leads_top{a.top}.csv"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    out.to_csv(path, index=False)
    print(f"wrote {path}: {len(out)} literals; lead trials covered: {int(out.n_lead_trials.sum())}")
    print(out[["rank", "name", "n_lead_trials", "n_any_trials", "roles"]].head(15).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
