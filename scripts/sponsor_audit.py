#!/usr/bin/env python3
"""Weekly sponsor curation audit — the inbox nothing may silently drop from.

READS   the weekly pull (data/demographics.json or the parts), the rules,
        data/sponsors/bridge.csv.gz (this week's bridge),
        --prior-bridge (last week's bridge from the site checkout, optional),
        --prior-unmatched (last week's unmatched_all.csv, optional)
WRITES  data/sponsor_audit/unmatched_all.csv        every unreviewed literal
        data/sponsor_audit/inbox_new_unmatched.csv  unreviewed literals not seen last week
        data/sponsor_audit/match_changes.csv        literals whose attribution changed
        data/sponsor_audit/audit_summary.json       counts for the run summary
Every CSV row carries rules_sha256 (amendment A4). Run from the repo root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from build_sponsor_bridge import load_records  # noqa: E402
from sponsors import sponsor_roles as sr  # noqa: E402
from sponsors.adapter import records_to_frame  # noqa: E402
from sponsors.audit_states import partition, summarize_literals  # noqa: E402


def literal_canonicals(bridge: pd.DataFrame) -> pd.DataFrame:
    """literal -> sorted canonical set + shared flag, one row per literal."""
    if bridge.empty:
        return pd.DataFrame(columns=["literal_name", "canonicals", "shared"])
    g = bridge.groupby("literal_name").agg(
        canonicals=("canonical", lambda c: "|".join(sorted(set(c)))),
        shared=("shared", lambda s: "yes" if (s == "yes").any() else "no"))
    return g.reset_index()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demographics", default="data/demographics.json")
    ap.add_argument("--parts", default="data/demographics.part*.json.gz")
    ap.add_argument("--rules", default="sponsors/company_aliases.csv")
    ap.add_argument("--bridge", default="data/sponsors/bridge.csv.gz")
    ap.add_argument("--prior-bridge", default=None)
    ap.add_argument("--prior-unmatched", default=None)
    ap.add_argument("--out-dir", default="data/sponsor_audit")
    a = ap.parse_args()

    rules_sha = hashlib.sha256(open(a.rules, "rb").read()).hexdigest()
    records, extracted_at = load_records(a.demographics, a.parts)
    frame, _ = records_to_frame(records)
    rules = sr.load_rules(a.rules)
    bridge = pd.read_csv(a.bridge, dtype=str)
    states = partition(frame, rules, index=bridge)

    # Read every prior artifact BEFORE writing anything (stable paths).
    prior_unmatched = set()
    if a.prior_unmatched and os.path.exists(a.prior_unmatched):
        prior_unmatched = set(pd.read_csv(a.prior_unmatched, dtype=str)["name"])
    prior_bridge = None
    if a.prior_bridge and os.path.exists(a.prior_bridge):
        prior_bridge = pd.read_csv(a.prior_bridge, dtype=str)

    unmatched_all = summarize_literals(frame, states["unreviewed"])
    inbox = unmatched_all[~unmatched_all["name"].isin(prior_unmatched)].reset_index(drop=True)

    # Match changes vs the prior bridge, with a cause hint per literal.
    changes = []
    if prior_bridge is not None:
        now = literal_canonicals(bridge).set_index("literal_name")
        before = literal_canonicals(prior_bridge).set_index("literal_name")
        current_literals = set(frame["name"])
        for lit in sorted(set(now.index) | set(before.index)):
            n, b = (now.loc[lit] if lit in now.index else None), (before.loc[lit] if lit in before.index else None)
            if n is not None and b is not None and n.canonicals == b.canonicals and n.shared == b.shared:
                continue
            if n is None:
                change, cause = "dropped_match", ("literal_absent_from_pull" if lit not in current_literals else "rules_change")
            elif b is None:
                change, cause = "new_match", ("literal_new_in_pull" if lit not in set(prior_bridge.literal_name) else "rules_change")
            else:
                change, cause = "canonicals_changed", "rules_change"
            n_trials = int(frame.loc[frame["name"] == lit, "nct_id"].nunique())
            changes.append({"literal_name": lit, "change": change, "cause_hint": cause,
                            "before": None if b is None else b.canonicals,
                            "after": None if n is None else n.canonicals,
                            "n_trials": n_trials})
    changes_df = pd.DataFrame(changes, columns=["literal_name", "change", "cause_hint", "before", "after", "n_trials"])
    changes_df = changes_df.sort_values(["n_trials", "literal_name"], ascending=[False, True]).reset_index(drop=True)

    os.makedirs(a.out_dir, exist_ok=True)
    for name, df in [("unmatched_all.csv", unmatched_all), ("inbox_new_unmatched.csv", inbox),
                     ("match_changes.csv", changes_df)]:
        df = df.copy()
        df["rules_sha256"] = rules_sha
        df.to_csv(os.path.join(a.out_dir, name), index=False)

    summary = {
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "source_extracted_at": extracted_at,
        "rules_sha256": rules_sha,
        "n_literals": int(sum(len(v) for v in states.values())),
        "n_attributed": len(states["attributed"]),
        "n_reviewed_excluded": len(states["reviewed_excluded"]),
        "n_unreviewed": len(states["unreviewed"]),
        "inbox_size": int(len(inbox)),
        "inbox_top": (inbox.iloc[0].to_dict() if len(inbox) else None),
        "n_match_changes": int(len(changes_df)),
        "had_prior_bridge": prior_bridge is not None,
        "had_prior_unmatched": bool(prior_unmatched),
    }
    with open(os.path.join(a.out_dir, "audit_summary.json"), "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(json.dumps({k: v for k, v in summary.items() if k != "inbox_top"}, indent=2))
    if summary["inbox_top"]:
        print("inbox top:", summary["inbox_top"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
