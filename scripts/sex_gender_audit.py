#!/usr/bin/env python3
"""Weekly sex/gender curation audit — the inbox nothing may silently drop from.

READS   data/sex_gender_parsed.csv.gz and data/sex_gender_parsed_meta.json (this
        week's table, scripts/build_sex_gender_table.py), the retained raw
        measures data/sex_gender_raw_measures.jsonl.gz (the label inventory with
        its level and an example trial), and last week's committed artifacts in
        --prior-dir (label_buckets.csv, inbox_unrecognized.csv, status_counts.json).
WRITES  under --out-dir (default data/sex_gender_audit/), every CSV row stamped
        parser_rules_version:
        label_buckets.csv        every distinct (level, label) seen this week with its
                                 bucket and reason; next week's prior
        inbox_unrecognized.csv   every label canon_bucket() maps to nothing with reason
                                 "unrecognized": n_trials desc, example NCT, first_seen,
                                 weeks_open (first_seen persists week to week)
        inbox_exits.csv          labels that left the inbox since last week, with cause
        bucket_changes.csv       labels whose bucket differs from last week, with cause
        status_counts.json       the five-state table, outcomes, structural checks and
                                 baseline drift, each with last week's value and the delta
        audit_summary.json       counts for the run summary
INVOKED by .github/workflows/extract.yml after the site publish. Run from the
        repo root. Reads every prior artifact BEFORE writing.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import sex_gender_parser as sgp  # noqa: E402
from src import sex_gender_table as sgt  # noqa: E402
from src.utils import pipeline_commit  # noqa: E402

INVENTORY_COLUMNS = ["level", "label", "bucket", "reason", "n_occurrences", "n_trials", "example_nct"]


def read_csv_if(path: str | None) -> pd.DataFrame | None:
    if path and os.path.exists(path):
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    return None


def read_json_if(path: str | None) -> dict | None:
    if path and os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    return None


def inventory_from_raw(path: str) -> pd.DataFrame:
    """One row per distinct (level, label) across the selected measures of every
    trial, mirroring the bundle's label_vocabulary_audit layout."""
    occ: dict = defaultdict(int)
    trials: dict = defaultdict(set)
    example: dict = {}
    with gzip.open(path, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            nct = raw.get("nct_id")
            for m in raw.get("measures") or []:
                for cl in (m.get("classes") or []):
                    if not isinstance(cl, dict):
                        continue
                    if cl.get("title") is not None:
                        k = ("class", str(cl["title"]))
                        occ[k] += 1; trials[k].add(nct); example.setdefault(k, nct)
                    for cat in (cl.get("categories") or []):
                        if isinstance(cat, dict) and cat.get("title") is not None:
                            k = ("category", str(cat["title"]))
                            occ[k] += 1; trials[k].add(nct); example.setdefault(k, nct)
    rows = [{"level": lv, "label": lb, "bucket": sgp.canon_bucket(lb), "reason": sgp.canon_unmapped_reason(lb),
             "n_occurrences": occ[(lv, lb)], "n_trials": len(trials[(lv, lb)]), "example_nct": example[(lv, lb)]}
            for (lv, lb) in occ]
    return pd.DataFrame(rows, columns=INVENTORY_COLUMNS)


def inventory_from_table(df: pd.DataFrame) -> pd.DataFrame:
    """Fallback when no raw measures were retained: labels from the table's
    label trails. Level is unknown ("table") and an example NCT is any carrier."""
    occ: dict = defaultdict(int)
    trials: dict = defaultdict(set)
    example: dict = {}
    for col in ("unknown_labels", "gender_diverse_labels", "ambiguous_labels", "unmapped_labels"):
        if col not in df.columns:
            continue
        for nct, cell in zip(df["nct_id"], df[col]):
            if not isinstance(cell, str) or not cell:
                continue
            for lb in cell.split("; "):
                if lb:
                    k = ("table", lb)
                    occ[k] += 1; trials[k].add(nct); example.setdefault(k, nct)
    rows = [{"level": lv, "label": lb, "bucket": sgp.canon_bucket(lb), "reason": sgp.canon_unmapped_reason(lb),
             "n_occurrences": occ[(lv, lb)], "n_trials": len(trials[(lv, lb)]), "example_nct": example[(lv, lb)]}
            for (lv, lb) in occ]
    return pd.DataFrame(rows, columns=INVENTORY_COLUMNS)


def _delta(now: dict, prior: dict | None) -> dict:
    out = {}
    for k, v in now.items():
        if isinstance(v, dict):
            out[k] = _delta(v, (prior or {}).get(k) if isinstance((prior or {}).get(k), dict) else None)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            p = (prior or {}).get(k)
            out[k] = {"now": v, "prior": p, "delta": (v - p) if isinstance(p, (int, float)) else None}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="data/sex_gender_parsed.csv.gz")
    ap.add_argument("--meta", default="data/sex_gender_parsed_meta.json")
    ap.add_argument("--raw-measures", default="data/sex_gender_raw_measures.jsonl.gz")
    ap.add_argument("--prior-dir", default="data/sex_gender_audit", help="last week's committed audit")
    ap.add_argument("--snapshot-date", default=None)
    ap.add_argument("--out-dir", default="data/sex_gender_audit")
    a = ap.parse_args()

    df = pd.read_csv(a.table, dtype=str, keep_default_na=False)
    meta = read_json_if(a.meta) or {}
    rules = sgp.PARSER_RULES_VERSION
    snapshot_date = a.snapshot_date or meta.get("snapshot_date") or (df["snapshot_date"].iloc[0] if len(df) else None) \
        or datetime.now(timezone.utc).date().isoformat()
    snap = date.fromisoformat(snapshot_date)

    # ── Read every prior artifact BEFORE writing (the out dir is usually the prior dir) ──
    prior_buckets = read_csv_if(os.path.join(a.prior_dir, "label_buckets.csv"))
    prior_inbox = read_csv_if(os.path.join(a.prior_dir, "inbox_unrecognized.csv"))
    prior_status = read_json_if(os.path.join(a.prior_dir, "status_counts.json"))
    prior_rules = (prior_status or {}).get("parser_rules_version") or (
        prior_buckets["parser_rules_version"].iloc[0]
        if prior_buckets is not None and "parser_rules_version" in prior_buckets.columns and len(prior_buckets) else None)
    rules_changed = (prior_rules != rules) if prior_rules else None

    # ── Label inventory ──
    if a.raw_measures and os.path.exists(a.raw_measures):
        inv = inventory_from_raw(a.raw_measures)
        inv_source = a.raw_measures
    else:
        inv = inventory_from_table(df)
        inv_source = a.table + " (label trails; no raw measures retained)"
    inv = inv.sort_values(["n_trials", "label"], ascending=[False, True]).reset_index(drop=True)

    # ── Inbox: unrecognized labels, cumulative first_seen ──
    first_seen: dict = {}
    if prior_inbox is not None and {"level", "label", "first_seen"} <= set(prior_inbox.columns):
        first_seen = {(lv, lb): fs for lv, lb, fs in zip(prior_inbox["level"], prior_inbox["label"], prior_inbox["first_seen"])}
    inbox = inv[inv["reason"] == "unrecognized"].copy()
    inbox["first_seen"] = [first_seen.get((lv, lb), snapshot_date) for lv, lb in zip(inbox["level"], inbox["label"])]
    inbox["weeks_open"] = inbox["first_seen"].map(lambda d: (snap - date.fromisoformat(d)).days // 7)
    inbox = inbox.sort_values(["n_trials", "label"], ascending=[False, True]).reset_index(drop=True)

    # ── Exits: every prior inbox label that is not in this week's inbox, with cause ──
    exits = []
    if prior_inbox is not None and {"level", "label"} <= set(prior_inbox.columns):
        now_inbox = set(zip(inbox["level"], inbox["label"]))
        now_inv = {(lv, lb): b for lv, lb, b in zip(inv["level"], inv["label"], inv["bucket"])}
        for _, r in prior_inbox.iterrows():
            k = (r["level"], r["label"])
            if k in now_inbox:
                continue
            if k in now_inv:
                cause = ("mapped_by_rules_change" if rules_changed else
                         ("mapped_unknown_cause" if now_inv[k] else "reason_changed"))
                after = now_inv[k]
            else:
                cause, after = "no_longer_in_registry", None
            exits.append({"level": r["level"], "label": r["label"], "prior_n_trials": r.get("n_trials"),
                          "bucket_after": after, "cause": cause, "first_seen": r.get("first_seen")})
    exits_df = pd.DataFrame(exits, columns=["level", "label", "prior_n_trials", "bucket_after", "cause", "first_seen"])

    # ── Bucket changes vs last week, with cause ──
    changes = []
    if prior_buckets is not None and {"level", "label", "bucket"} <= set(prior_buckets.columns):
        prior_map = {(lv, lb): b for lv, lb, b in zip(prior_buckets["level"], prior_buckets["label"], prior_buckets["bucket"])}
        for lv, lb, b, n in zip(inv["level"], inv["label"], inv["bucket"], inv["n_trials"]):
            k = (lv, lb)
            if k in prior_map and prior_map[k] != b:
                changes.append({"level": lv, "label": lb, "prior_bucket": prior_map[k], "new_bucket": b, "n_trials": n,
                                "cause": "rules_version_change" if rules_changed else
                                         ("unknown_no_prior_version" if rules_changed is None else "unknown_module_change")})
    changes_df = pd.DataFrame(changes, columns=["level", "label", "prior_bucket", "new_bucket", "n_trials", "cause"])
    changes_df = changes_df.sort_values(["n_trials", "label"], ascending=[False, True]).reset_index(drop=True)

    # ── Status counts with drift against last week ──
    counts = meta.get("status_counts") or {}
    status_doc = {
        "snapshot_date": snapshot_date,
        "parser_rules_version": rules,
        "parser_module_version": sgp.__version__,
        "counts": counts,
        "vs_prior": _delta(counts, (prior_status or {}).get("counts")),
        "prior_snapshot_date": (prior_status or {}).get("snapshot_date"),
        "prior_parser_rules_version": prior_rules,
        "structural_checks": meta.get("structural_checks"),
        "structural_checks_all_pass": meta.get("structural_checks_all_pass"),
        "baseline": meta.get("baseline"),
    }

    # ── Write ──
    os.makedirs(a.out_dir, exist_ok=True)
    for name, frame in [("label_buckets.csv", inv), ("inbox_unrecognized.csv", inbox),
                        ("inbox_exits.csv", exits_df), ("bucket_changes.csv", changes_df)]:
        frame = frame.copy(); frame["parser_rules_version"] = rules
        frame.to_csv(os.path.join(a.out_dir, name), index=False)
    with open(os.path.join(a.out_dir, "status_counts.json"), "w") as fh:
        json.dump(status_doc, fh, indent=2, default=str)

    summary = {
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_date": snapshot_date,
        "pipeline_commit": pipeline_commit(),
        "source_extracted_at": meta.get("source_extracted_at"),
        "parser_rules_version": rules,
        "parser_module_version": sgp.__version__,
        "prior_parser_rules_version": prior_rules,
        "rules_changed_vs_prior": rules_changed,
        "inventory_source": inv_source,
        "n_rows": int(len(df)),
        "n_labels": int(len(inv)),
        "n_labels_by_reason": inv["reason"].value_counts().to_dict(),
        "inbox_size": int(len(inbox)),
        "inbox_new_this_week": int((inbox["first_seen"] == snapshot_date).sum()) if len(inbox) else 0,
        "inbox_top": (inbox.iloc[0].to_dict() if len(inbox) else None),
        "inbox_oldest": (inbox.sort_values(["weeks_open", "n_trials"], ascending=[False, False]).iloc[0].to_dict()
                         if len(inbox) else None),
        "n_inbox_exits": int(len(exits_df)),
        "inbox_exit_causes": exits_df["cause"].value_counts().to_dict() if len(exits_df) else {},
        "n_bucket_changes": int(len(changes_df)),
        "bucket_change_causes": changes_df["cause"].value_counts().to_dict() if len(changes_df) else {},
        "status_counts": counts.get("status"),
        "structural_checks_all_pass": meta.get("structural_checks_all_pass"),
        "baseline_drift_lines": len((meta.get("baseline") or {}).get("drift") or []),
        "had_prior": {"label_buckets": prior_buckets is not None, "inbox": prior_inbox is not None,
                      "status_counts": prior_status is not None},
    }
    with open(os.path.join(a.out_dir, "audit_summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2, default=str)
    print(json.dumps({k: v for k, v in summary.items() if k not in ("inbox_top", "inbox_oldest")}, indent=2, default=str))
    if summary["inbox_top"]:
        print("inbox top:", summary["inbox_top"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
