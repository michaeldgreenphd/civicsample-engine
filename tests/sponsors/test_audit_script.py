"""End-to-end: build_sponsor_bridge.py + sponsor_audit.py over three synthetic
weeks, checking the loop's contracts — every audit row stamped rules_sha256
(A4), bridge size measured (A2), inbox_open's first_seen persisting across
weeks (2.4), and match_changes / label_changes carrying a cause that tells a
registry edit from a rules change (2.3, P1)."""
import gzip
import hashlib
import json
import os
import subprocess
import sys

import pandas as pd

from conftest import ROOT

RULES_V1 = ("canonical,rule_type,pattern,status,shared,note\n"
            "Pfizer,exact_normalized,pfizer,attribute,no,\"test: Pfizer\"\n"
            "Zeta Bio,exact_literal,Zeta Bio,attribute,no,\"test: Zeta\"\n")
RULES_V2 = RULES_V1 + "Beta Pharma,exact_literal,Beta Pharma,attribute,no,\"test: Beta added week 3\"\n"


def rec(nct, lead, cls="INDUSTRY", collabs=()):
    return {"nct_id": nct, "lead_sponsor_name": lead, "sponsor_class": cls,
            "collaborators": [{"name": n, "class": c} for n, c in collabs]}


WEEK1 = [rec("NCT1", "Pfizer Inc."),
         rec("NCT2", "Beta Pharma"),
         rec("NCT3", "Acme Labs", collabs=[("Beta Pharma", "INDUSTRY")]),
         rec("NCT4", "", cls="OTHER"),                 # blank lead: counted, never an inbox line
         rec("NCT5", "Zeta Bio")]
WEEK2 = [rec("NCT1", "Pfizer Inc."),
         rec("NCT2", "Beta Pharma"),
         rec("NCT3", "Pfizer Inc.", collabs=[("Beta Pharma", "INDUSTRY")]),  # registry edit: lead changed
         rec("NCT4", "", cls="OTHER")]                                        # NCT5 withdrawn: Zeta Bio gone


def run(args):
    r = subprocess.run([sys.executable] + args, cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout
    return r


def week(tmp, name, records, rules_text, snapshot, prior=None):
    d = tmp / name
    d.mkdir()
    demo = d / "demographics.json"
    demo.write_text(json.dumps({"extracted_at": f"{snapshot}T06:00:00+00:00", "data": records}))
    rules = d / "rules.csv"
    rules.write_text(rules_text)
    run(["scripts/build_sponsor_bridge.py", "--demographics", str(demo), "--rules", str(rules),
         "--out-dir", str(d / "sponsors")])
    args = ["scripts/sponsor_audit.py", "--demographics", str(demo), "--rules", str(rules),
            "--bridge", str(d / "sponsors" / "bridge.csv.gz"), "--snapshot-date", snapshot,
            "--out-dir", str(d / "audit")]
    if prior is not None:
        args += ["--prior-bridge", str(prior / "sponsors" / "bridge.csv.gz"),
                 "--prior-meta", str(prior / "sponsors" / "bridge_meta.json"),
                 "--prior-literals", str(prior / "audit" / "literals_all.csv.gz"),
                 "--prior-open", str(prior / "audit" / "inbox_open.csv.gz"),
                 "--prior-trials", str(prior / "audit" / "trial_sponsors.csv.gz")]
    run(args)
    return d


def csv(d, name):
    return pd.read_csv(d / "audit" / name, dtype=str, keep_default_na=False)


def test_three_week_loop(tmp_path):
    sha1 = hashlib.sha256(RULES_V1.encode()).hexdigest()
    sha2 = hashlib.sha256(RULES_V2.encode()).hexdigest()

    # ── week 1: no priors ──
    w1 = week(tmp_path, "w1", WEEK1, RULES_V1, "2026-09-06")
    meta = json.load(open(w1 / "sponsors" / "bridge_meta.json"))
    assert meta["rules_sha256"] == sha1                                   # A4
    assert meta["bridge_bytes_gz"] == os.path.getsize(w1 / "sponsors" / "bridge.csv.gz")  # A2
    assert meta["n_rows"] == 2 and meta["adapter_log"]["n_empty_lead_names"] == 1
    for f in ["literals_all.csv.gz", "inbox_new_unmatched.csv",
              "inbox_open.csv.gz", "match_changes.csv", "label_changes.csv"]:
        df = csv(w1, f)
        assert "rules_sha256" in df.columns and (df["rules_sha256"] == sha1).all() if len(df) else True
    lits = csv(w1, "literals_all.csv.gz").set_index("name")
    assert lits.loc["Pfizer Inc.", "state"] == "attributed" and lits.loc["Pfizer Inc.", "canonicals"] == "Pfizer"
    assert lits.loc["", "state"] == "unreviewed"                          # the blank literal is not hidden
    open1 = csv(w1, "inbox_open.csv.gz")
    assert set(open1["name"]) == {"Acme Labs", "Beta Pharma"}             # blank is not an inbox line
    assert set(open1["first_seen"]) == {"2026-09-06"} and set(open1["weeks_open"]) == {"0"}
    s1 = json.load(open(w1 / "audit" / "audit_summary.json"))
    assert s1["n_blank_literal_trials"] == 1 and s1["n_match_changes"] == 0
    assert s1["had_prior"] == {k: False for k in ["bridge", "meta", "literals", "open", "trials"]}
    with gzip.open(w1 / "audit" / "trial_sponsors.csv.gz", "rt") as fh:
        assert len(fh.read().splitlines()) == 1 + 6                       # header + every sponsor row

    # ── week 2: registry edits only (rules unchanged) ──
    w2 = week(tmp_path, "w2", WEEK2, RULES_V1, "2026-09-13", prior=w1)
    s2 = json.load(open(w2 / "audit" / "audit_summary.json"))
    assert s2["rules_changed_vs_prior"] is False
    mc = csv(w2, "match_changes.csv").set_index("literal_name")
    assert mc.loc["Zeta Bio", "change"] == "dropped_match" and mc.loc["Zeta Bio", "cause"] == "registry_edit"
    assert "Acme Labs" not in mc.index                                    # unmatched before and after: not a match change
    lc = csv(w2, "label_changes.csv")
    added = lc[(lc.change == "added")].set_index(["nct_id", "canonical", "role"])
    removed = lc[(lc.change == "removed")].set_index(["nct_id", "canonical", "role"])
    assert added.loc[("NCT3", "Pfizer", "lead"), "cause"] == "registry_edit"
    assert removed.loc[("NCT5", "Zeta Bio", "lead"), "cause"] == "registry_edit"
    open2 = csv(w2, "inbox_open.csv.gz").set_index("name")
    assert open2.loc["Beta Pharma", "first_seen"] == "2026-09-06" and open2.loc["Beta Pharma", "weeks_open"] == "1"
    assert "Acme Labs" not in open2.index                                 # left the registry, so left the inbox
    assert list(csv(w2, "inbox_new_unmatched.csv")["name"]) == []         # the blank literal must not re-enter as "new"

    # ── week 3: rules change only (registry unchanged) ──
    w3 = week(tmp_path, "w3", WEEK2, RULES_V2, "2026-09-20", prior=w2)
    s3 = json.load(open(w3 / "audit" / "audit_summary.json"))
    assert s3["rules_changed_vs_prior"] is True and s3["rules_sha256"] == sha2
    mc = csv(w3, "match_changes.csv").set_index("literal_name")
    assert mc.loc["Beta Pharma", "change"] == "new_match" and mc.loc["Beta Pharma", "cause"] == "rules_change"
    lc = csv(w3, "label_changes.csv").set_index(["nct_id", "canonical", "role"])
    assert lc.loc[("NCT2", "Beta Pharma", "lead"), "cause"] == "rules_change"
    assert lc.loc[("NCT3", "Beta Pharma", "collaborator"), "cause"] == "rules_change"
    assert s3["label_change_causes"] == {"rules_change": 2}
    assert list(csv(w3, "inbox_open.csv.gz")["name"]) == []                  # Beta Pharma left by a rule; nothing else open
    assert (csv(w3, "literals_all.csv.gz")["rules_sha256"] == sha2).all()
