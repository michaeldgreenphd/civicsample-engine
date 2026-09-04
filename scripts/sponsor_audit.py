#!/usr/bin/env python3
"""Weekly sponsor curation audit — the inbox nothing may silently drop from.

READS   the weekly pull (data/demographics.json or the parts), the rules,
        data/sponsors/bridge.csv.gz (this week's bridge) and, when present,
        last week's artifacts: --prior-bridge / --prior-meta (the site's copy
        before overwrite), --prior-literals, --prior-open, --prior-trials
        (this repo's committed audit)
WRITES  under --out-dir (default data/sponsor_audit/), every CSV row stamped
        rules_sha256 (A4):
        literals_all.csv.gz       every literal with its review state (+ canonicals, shared);
                                  the unreviewed state is state == "unreviewed"
        inbox_new_unmatched.csv   unreviewed literals not seen last week (small; plain CSV
                                  so a curator can open it straight from GitHub)
        inbox_open.csv.gz         every open unreviewed literal: first_seen, weeks_open (2.4)
        match_changes.csv         literal-level attribution changes + cause (2.3)
        label_changes.csv         TRIAL-level (nct_id, canonical, role) changes + cause (P1)
        trial_sponsors.csv.gz     this week's per-trial literals (next week's prior)
        audit_summary.json        counts for the run summary
Run from the repo root. Reads every prior artifact BEFORE writing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from build_sponsor_bridge import load_records, pipeline_commit  # noqa: E402
from sponsors import sponsor_roles as sr  # noqa: E402
from sponsors.adapter import records_to_frame  # noqa: E402
from sponsors.audit_states import (canonicals_by_literal, partition,  # noqa: E402
                                   rule_matches, summarize_literals)

KEY = ["nct_id", "canonical", "role"]


def read_if(path, **kw):
    """Read a prior CSV as strings. keep_default_na=False so an empty literal
    ("") and names like "NA" round-trip instead of turning into NaN."""
    if path and os.path.exists(path):
        return pd.read_csv(path, dtype=str, keep_default_na=False, **kw)
    return None


def cause_for_literal(present_prior, present_now, rules_changed):
    """Why a literal's attribution differs from last week's.

    registry_edit     the literal appeared in, or vanished from, the pull
                      (a ClinicalTrials.gov edit); the rules did not change
    rules_change      the literal was in both pulls and company_aliases.csv
                      changed in between
    both              its presence changed AND the rules changed — either
                      could explain it; the reviewer decides
    unknown           present both weeks, rules unchanged: nothing in the
                      inputs explains it — a module/adapter change; investigate
    unknown_no_prior  last week's literal list or rules hash was unavailable
    """
    if present_prior is None or rules_changed is None:
        return "unknown_no_prior"
    if present_prior != present_now:
        return "both" if rules_changed else "registry_edit"
    return "rules_change" if rules_changed else "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demographics", default="data/demographics.json")
    ap.add_argument("--parts", default="data/demographics.part*.json.gz")
    ap.add_argument("--rules", default="sponsors/company_aliases.csv")
    ap.add_argument("--bridge", default="data/sponsors/bridge.csv.gz")
    ap.add_argument("--prior-bridge", default=None)
    ap.add_argument("--prior-meta", default=None)
    ap.add_argument("--prior-literals", default=None, help="last week's literals_all.csv.gz")
    ap.add_argument("--prior-open", default=None, help="last week's inbox_open.csv.gz")
    ap.add_argument("--prior-trials", default=None)
    ap.add_argument("--snapshot-date", default=None, help="YYYY-MM-DD; default = date of source_extracted_at")
    ap.add_argument("--out-dir", default="data/sponsor_audit")
    a = ap.parse_args()

    rules_sha = hashlib.sha256(open(a.rules, "rb").read()).hexdigest()
    records, extracted_at = load_records(a.demographics, a.parts)
    snapshot_date = a.snapshot_date or (extracted_at or datetime.now(timezone.utc).isoformat())[:10]
    snap = date.fromisoformat(snapshot_date)
    frame, _ = records_to_frame(records)
    rules = sr.load_rules(a.rules)
    bridge = pd.read_csv(a.bridge, dtype=str, keep_default_na=False)
    states = partition(frame, rules)                       # rule-derived, same decision as the bridge
    now_lc = canonicals_by_literal(rule_matches(frame, rules))
    state_of = {}
    for st, names in states.items():
        for n in names:
            state_of[n] = st

    # ── Read every prior artifact BEFORE writing (stable paths) ──
    prior_bridge = read_if(a.prior_bridge)
    prior_meta = json.load(open(a.prior_meta)) if a.prior_meta and os.path.exists(a.prior_meta) else None
    prior_literals = read_if(a.prior_literals)
    prior_open = read_if(a.prior_open)
    prior_trials = read_if(a.prior_trials)
    # Last week's rules hash: the site's bridge_meta first, else the sha every
    # committed audit row carries (A4).
    prior_sha = (prior_meta.get("rules_sha256") if prior_meta else None) or (
        prior_literals["rules_sha256"].iloc[0]
        if prior_literals is not None and "rules_sha256" in prior_literals.columns and len(prior_literals)
        else None)
    rules_changed = (prior_sha != rules_sha) if prior_sha else None
    prior_universe = set(prior_literals["name"]) if prior_literals is not None else None
    prior_unmatched = (set(prior_literals.loc[prior_literals.state == "unreviewed", "name"])
                       if prior_literals is not None else set())
    # Last week's literal -> canonicals: the committed literals_all (rule-derived)
    # first, else re-derived from the site's prior bridge (per-trial dedupe can
    # hide a literal there, so it is the fallback, not the source of truth).
    if prior_literals is not None and "canonicals" in prior_literals.columns:
        pl = prior_literals[prior_literals["canonicals"] != ""]
        before = pd.DataFrame({"canonicals": pl["canonicals"].values,
                               "shared": (pl["shared"].values if "shared" in pl.columns else "no")},
                              index=pd.Index(pl["name"].values, name="name"))
    elif prior_bridge is not None:
        before = canonicals_by_literal(prior_bridge, name_col="literal_name")
    else:
        before = None

    # ── literals_all / inbox_new ──
    lits = summarize_literals(frame, set(frame["name"]))
    lits["state"] = lits["name"].map(state_of)
    lits["canonicals"] = lits["name"].map(lambda n: now_lc.canonicals.get(n, "") if n in now_lc.index else "")
    lits["shared"] = lits["name"].map(lambda n: now_lc.shared.get(n, "no") if n in now_lc.index else "no")
    unreviewed = lits[lits.state == "unreviewed"].drop(columns=["state", "canonicals", "shared"]).reset_index(drop=True)
    # An empty literal ("" — a record with no lead name, counted by the adapter)
    # is unreviewed but not curatable: no rule can name it. It stays in
    # literals_all and is counted in the summary, but never occupies an
    # inbox line.
    blank = unreviewed["name"].str.strip() == ""
    n_blank_trials = int(unreviewed.loc[blank, "n_trials"].astype(int).sum())
    curatable = unreviewed[~blank]
    inbox = curatable[~curatable["name"].isin(prior_unmatched)].reset_index(drop=True)

    # ── inbox_open: first_seen persists across weeks; an item leaves only by a rule ──
    first_seen = {}
    if prior_open is not None:
        first_seen = dict(zip(prior_open["name"], prior_open["first_seen"]))
    inbox_open = curatable.copy()
    inbox_open["first_seen"] = inbox_open["name"].map(lambda n: first_seen.get(n, snapshot_date))
    inbox_open["weeks_open"] = inbox_open["first_seen"].map(lambda d: (snap - date.fromisoformat(d)).days // 7)
    inbox_open = inbox_open.sort_values(["n_trials", "name"], ascending=[False, True]).reset_index(drop=True)

    # ── match_changes (literal level) with cause ──
    changes = []
    if before is not None:
        now_lits = set(frame["name"])
        for lit in sorted(set(now_lc.index) | set(before.index)):
            n = now_lc.loc[lit] if lit in now_lc.index else None
            b = before.loc[lit] if lit in before.index else None
            if n is not None and b is not None and n.canonicals == b.canonicals and n.shared == b.shared:
                continue
            change = "dropped_match" if n is None else ("new_match" if b is None else "canonicals_changed")
            present_prior = (lit in prior_universe) if prior_universe is not None else None
            cause = cause_for_literal(present_prior, lit in now_lits, rules_changed)
            changes.append({"literal_name": lit, "change": change, "cause": cause,
                            "before": None if b is None else b.canonicals,
                            "after": None if n is None else n.canonicals,
                            "n_trials": int(frame.loc[frame["name"] == lit, "nct_id"].nunique())})
    changes_df = pd.DataFrame(changes, columns=["literal_name", "change", "cause", "before", "after", "n_trials"])
    changes_df = changes_df.sort_values(["n_trials", "literal_name"], ascending=[False, True]).reset_index(drop=True)

    # ── label_changes (trial level, P1): every (nct_id, canonical, role) added or removed ──
    trial_now = frame[["nct_id", "lead_or_collaborator", "name"]].rename(columns={"lead_or_collaborator": "role"})
    labels = []
    if prior_bridge is not None:
        now_pairs = bridge.drop_duplicates(KEY).set_index(KEY)["literal_name"]
        prev_pairs = prior_bridge.drop_duplicates(KEY).set_index(KEY)["literal_name"]
        now_set, prev_set = set(now_pairs.index), set(prev_pairs.index)
        now_trial_lits = trial_now.groupby(["nct_id", "role"])["name"].agg(set)
        prev_trial_lits = (prior_trials.rename(columns={"lead_or_collaborator": "role"})
                           .groupby(["nct_id", "role"])["name"].agg(set)) if prior_trials is not None else None
        for pair in sorted(now_set - prev_set):
            lit = now_pairs[pair]
            had = (lit in prev_trial_lits.get((pair[0], pair[2]), set())) if prev_trial_lits is not None else None
            cause = ("unknown_no_prior" if had is None else
                     ("rules_change" if had and rules_changed else ("registry_edit" if not had else "unknown")))
            labels.append({"nct_id": pair[0], "canonical": pair[1], "role": pair[2], "change": "added",
                           "literal_before": None, "literal_after": lit, "cause": cause})
        for pair in sorted(prev_set - now_set):
            lit = prev_pairs[pair]
            still = lit in now_trial_lits.get((pair[0], pair[2]), set())
            cause = "rules_change" if (still and rules_changed) else ("registry_edit" if not still else "unknown")
            labels.append({"nct_id": pair[0], "canonical": pair[1], "role": pair[2], "change": "removed",
                           "literal_before": lit, "literal_after": None, "cause": cause})
    labels_df = pd.DataFrame(labels, columns=["nct_id", "canonical", "role", "change", "literal_before", "literal_after", "cause"])

    # ── write ──
    os.makedirs(a.out_dir, exist_ok=True)
    # The two per-literal files are gzipped: they carry a row for every name in
    # the pull (~15k) every week, and the site's audit tab already decompresses
    # gzip (app.js). The small, human-opened files stay plain CSV.
    for name, df in [("literals_all.csv.gz", lits), ("inbox_new_unmatched.csv", inbox),
                     ("inbox_open.csv.gz", inbox_open), ("match_changes.csv", changes_df),
                     ("label_changes.csv", labels_df)]:
        df = df.copy(); df["rules_sha256"] = rules_sha
        df.to_csv(os.path.join(a.out_dir, name), index=False,
                  compression="gzip" if name.endswith(".gz") else None)
    trial_now.sort_values(["nct_id", "role", "name"]).to_csv(
        os.path.join(a.out_dir, "trial_sponsors.csv.gz"), index=False, compression="gzip")

    oldest = inbox_open.sort_values(["weeks_open", "n_trials"], ascending=[False, False]).head(1)
    ind = inbox_open[inbox_open.agency_class == "INDUSTRY"]
    oldest_ind = ind.sort_values(["weeks_open", "n_trials"], ascending=[False, False]).head(1)
    summary = {
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_date": snapshot_date,
        "pipeline_commit": pipeline_commit(),
        "source_extracted_at": extracted_at,
        "rules_sha256": rules_sha,
        "rules_changed_vs_prior": rules_changed,
        "n_literals": int(len(lits)),
        "n_attributed": len(states["attributed"]),
        "n_reviewed_excluded": len(states["reviewed_excluded"]),
        "n_unreviewed": len(states["unreviewed"]),
        "n_blank_literal_trials": n_blank_trials,
        "inbox_size": int(len(inbox)),
        "inbox_top": (inbox.iloc[0].to_dict() if len(inbox) else None),
        "inbox_open_count": int(len(inbox_open)),
        "inbox_open_industry_count": int(len(ind)),
        "inbox_open_oldest": (oldest.iloc[0].to_dict() if len(oldest) else None),
        "inbox_open_oldest_industry": (oldest_ind.iloc[0].to_dict() if len(oldest_ind) else None),
        "n_match_changes": int(len(changes_df)),
        "match_change_causes": changes_df["cause"].value_counts().to_dict() if len(changes_df) else {},
        "n_label_changes": int(len(labels_df)),
        "label_change_causes": labels_df["cause"].value_counts().to_dict() if len(labels_df) else {},
        "had_prior": {"bridge": prior_bridge is not None, "meta": prior_meta is not None,
                      "literals": prior_literals is not None, "open": prior_open is not None,
                      "trials": prior_trials is not None},
    }
    with open(os.path.join(a.out_dir, "audit_summary.json"), "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(json.dumps({k: v for k, v in summary.items() if not k.startswith("inbox_top") and "oldest" not in k}, indent=2, default=str))
    if summary["inbox_open_oldest_industry"]:
        print("oldest open INDUSTRY:", summary["inbox_open_oldest_industry"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
