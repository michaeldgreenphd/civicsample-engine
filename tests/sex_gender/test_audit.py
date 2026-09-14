"""The weekly sex/gender curation audit (scripts/sex_gender_audit.py).

Pins: the `consequential` flag comes from the layout the parser chose for each
measure (class titles in a customized table, first-class category titles in a
standard table) and nothing else; the inbox is sorted consequential first, then
by trials; a label a curator marks resolved leaves the inbox on the next run and
appears in inbox_exits.csv with the note; nothing leaves label_buckets.csv; a
rules-version change is the cause logged on a bucket change.
"""
import gzip
import json
import os
import subprocess
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from src import sex_gender_table as sgt  # noqa: E402
import sex_gender_audit as audit  # noqa: E402
from build_sex_gender_table import write_table  # noqa: E402

SNAP = "2026-09-14"

STANDARD = {"title": "Sex/Gender, Customized", "paramType": "COUNT_OF_PARTICIPANTS",
            "classes": [{"title": "Sex/Gender",                       # container: never counted
                         "categories": [{"title": "Female", "measurements": [{"value": "4"}]},
                                        {"title": "Male", "measurements": [{"value": "5"}]},
                                        {"title": "Zorblat", "measurements": [{"value": "1"}]}]}]}   # counted position, unrecognized
CUSTOMIZED = {"title": "Sex/Gender, Customized", "paramType": "COUNT_OF_PARTICIPANTS",
              "classes": [{"title": "Female", "categories": [{"measurements": [{"value": "7"}]}]},
                          {"title": "Wibble", "categories": [{"title": "Count", "measurements": [{"value": "2"}]}]}]}  # class = counted; "Count" = container


def _raw(nct, measures, enrollment=20):
    return {"nct_id": nct, "enrollment": enrollment, "measures": measures, "snapshot_date": SNAP}


def _write_inputs(d, raws):
    raw_path = os.path.join(d, "raw.jsonl.gz")
    with gzip.open(raw_path, "wt") as fh:
        for r in raws:
            fh.write(json.dumps(r) + "\n")
    rows = [sgt.ordered(sgt.build_row_from_raw(r, SNAP)) for r in raws]
    table = os.path.join(d, "parsed.csv.gz")
    write_table(rows, table)
    meta = os.path.join(d, "meta.json")
    json.dump({"snapshot_date": SNAP, "status_counts": sgt.status_counts(rows),
               "structural_checks": sgt.structural_checks(rows), "structural_checks_all_pass": True,
               "baseline": {"drift": []}}, open(meta, "w"))
    return raw_path, table, meta


def _run(d, raw_path, table, meta, prior_dir, out_dir, snapshot=SNAP):
    cmd = [sys.executable, os.path.join(ROOT, "scripts", "sex_gender_audit.py"), "--table", table, "--meta", meta,
           "--raw-measures", raw_path, "--prior-dir", prior_dir, "--out-dir", out_dir, "--snapshot-date", snapshot]
    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return json.load(open(os.path.join(out_dir, "audit_summary.json")))


def test_count_positions_follow_the_parsed_layout():
    assert audit.count_positions(STANDARD) == {("category", "Female"), ("category", "Male"), ("category", "Zorblat")}
    assert audit.count_positions(CUSTOMIZED) == {("class", "Female"), ("class", "Wibble")}


def test_inbox_flags_consequential_labels_and_sorts_them_first(tmp_path):
    d = str(tmp_path)
    raw_path, table, meta = _write_inputs(d, [_raw("A", [STANDARD]), _raw("B", [CUSTOMIZED]), _raw("C", [STANDARD])])
    out = os.path.join(d, "audit")
    summary = _run(d, raw_path, table, meta, prior_dir=os.path.join(d, "none"), out_dir=out)
    inbox = pd.read_csv(os.path.join(out, "inbox_unrecognized.csv"), dtype=str, keep_default_na=False)
    got = {(r["level"], r["label"]): r["consequential"] for _, r in inbox.iterrows()}
    assert got[("category", "Zorblat")] == "True"          # counted position in a standard table
    assert got[("class", "Wibble")] == "True"              # class title in a customized table
    assert got[("class", "Sex/Gender")] == "False"         # container class in a standard table
    assert ("category", "Count") not in got                # structural, not unrecognized
    assert list(inbox["consequential"])[:2] == ["True", "True"] and list(inbox["consequential"])[-1] == "False"
    assert inbox.iloc[0]["label"] == "Zorblat" and inbox.iloc[0]["n_trials"] == "2"    # consequential, then trials desc
    assert summary["inbox_consequential"] == 2 and summary["inbox_size"] == 3
    buckets = pd.read_csv(os.path.join(out, "label_buckets.csv"), dtype=str, keep_default_na=False)
    assert set(buckets["label"]) >= {"Sex/Gender", "Female", "Male", "Zorblat", "Wibble", "Count"}
    assert all(inbox["first_seen"] == SNAP) and all(inbox["resolved"] == "")


def test_resolved_label_leaves_the_inbox_with_the_note_and_stays_in_label_buckets(tmp_path):
    d = str(tmp_path)
    raw_path, table, meta = _write_inputs(d, [_raw("A", [STANDARD]), _raw("B", [CUSTOMIZED])])
    week1 = os.path.join(d, "week1")
    _run(d, raw_path, table, meta, prior_dir=os.path.join(d, "none"), out_dir=week1)
    inbox = pd.read_csv(os.path.join(week1, "inbox_unrecognized.csv"), dtype=str, keep_default_na=False)
    inbox.loc[inbox["label"] == "Sex/Gender", "resolved"] = "container title; not a category"
    inbox.to_csv(os.path.join(week1, "inbox_unrecognized.csv"), index=False)      # the curator's hand edit
    week2 = os.path.join(d, "week2")
    summary = _run(d, raw_path, table, meta, prior_dir=week1, out_dir=week2, snapshot="2026-09-21")
    inbox2 = pd.read_csv(os.path.join(week2, "inbox_unrecognized.csv"), dtype=str, keep_default_na=False)
    assert "Sex/Gender" not in set(inbox2["label"])
    assert set(inbox2["first_seen"]) == {SNAP} and set(inbox2["weeks_open"]) == {"1"}   # first_seen persisted
    exits = pd.read_csv(os.path.join(week2, "inbox_exits.csv"), dtype=str, keep_default_na=False)
    assert list(exits["label"]) == ["Sex/Gender"] and exits.iloc[0]["cause"] == "resolved_by_curator"
    assert exits.iloc[0]["resolved"] == "container title; not a category"
    buckets = pd.read_csv(os.path.join(week2, "label_buckets.csv"), dtype=str, keep_default_na=False)
    assert "Sex/Gender" in set(buckets["label"])
    assert summary["resolved_this_week"] == 1 and summary["n_inbox_exits"] == 1
    assert summary["n_bucket_changes"] == 0 and summary["rules_changed_vs_prior"] is False


def test_bucket_change_is_attributed_to_a_rules_version_change(tmp_path):
    d = str(tmp_path)
    raw_path, table, meta = _write_inputs(d, [_raw("A", [STANDARD])])
    prior = os.path.join(d, "prior")
    os.makedirs(prior)
    pd.DataFrame([{"level": "category", "label": "Zorblat", "bucket": "unknown", "reason": "mapped",
                   "parser_rules_version": "older rules"}]).to_csv(os.path.join(prior, "label_buckets.csv"), index=False)
    json.dump({"parser_rules_version": "older rules", "counts": {}}, open(os.path.join(prior, "status_counts.json"), "w"))
    out = os.path.join(d, "audit")
    summary = _run(d, raw_path, table, meta, prior_dir=prior, out_dir=out)
    changes = pd.read_csv(os.path.join(out, "bucket_changes.csv"), dtype=str, keep_default_na=False)
    assert list(changes["label"]) == ["Zorblat"] and changes.iloc[0]["cause"] == "rules_version_change"
    assert changes.iloc[0]["prior_bucket"] == "unknown" and changes.iloc[0]["new_bucket"] == ""
    assert summary["rules_changed_vs_prior"] is True
