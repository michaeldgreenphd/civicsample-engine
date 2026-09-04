"""Phase 2.5: a rules conflict must fail the bridge build without touching
the previously published bridge. The workflow guarantees ordering (the
demographics publish and the site's bridge copy both precede the build);
this test pins the script-level contract: on a conflict, build_index raises
BEFORE anything is written, so the site keeps last week's bridge.
"""
import gzip
import json
import os
import subprocess
import sys

from conftest import ROOT

HEADER = "canonical,rule_type,pattern,status,shared,note\n"


def test_conflicting_rules_leave_previous_bridge_untouched(tmp_path):
    # last week's published bridge, as the site holds it
    site = tmp_path / "site" / "data" / "sponsors"
    site.mkdir(parents=True)
    prior_bridge = b"nct_id,canonical,entity,role,literal_name,agency_class,match_rule,shared\nNCT1,Pfizer,pfizer,lead,Pfizer,INDUSTRY,exact_literal,no\n"
    (site / "bridge.csv.gz").write_bytes(gzip.compress(prior_bridge))
    (site / "bridge_meta.json").write_text(json.dumps({"source_extracted_at": "2026-08-23T06:00:00+00:00", "rules_sha256": "old"}))

    # this week's pull + a conflicting rules file (two companies, one literal, not shared)
    demo = tmp_path / "demographics.json"
    demo.write_text(json.dumps({"extracted_at": "2026-08-30T06:00:00+00:00", "data": [
        {"nct_id": "NCT1", "lead_sponsor_name": "Acme", "sponsor_class": "INDUSTRY", "collaborators": []}]}))
    rules = tmp_path / "rules.csv"
    rules.write_text(HEADER + "Pfizer,exact_literal,Acme,attribute,no,t\nMerck & Co,exact_literal,Acme,attribute,no,t\n")
    out = tmp_path / "out"

    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "build_sponsor_bridge.py"),
                        "--demographics", str(demo), "--rules", str(rules), "--out-dir", str(out)],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode != 0, "a rules conflict must fail the build"
    assert "conflicting rules for literal 'Acme'" in r.stderr
    assert not (out / "bridge.csv.gz").exists() and not (out / "bridge_meta.json").exists(), \
        "nothing may be written on a conflict"
    # the site's previous bridge and its meta are byte-identical -> the tab's stale marker can fire
    assert gzip.decompress((site / "bridge.csv.gz").read_bytes()) == prior_bridge
    assert json.loads((site / "bridge_meta.json").read_text())["source_extracted_at"] == "2026-08-23T06:00:00+00:00"
