#!/usr/bin/env python3
"""Advance the geography tab to a new contract run — the deliberate act.

    python3 scripts/geo/advance_run.py /path/to/geo_rep_YYYY-MM-DD_runNNN

What it does, in order:
  1. refuses a run whose RUN_INFO.csv status is not `final`
  2. hash-verifies the 12 contract files against the run's own MANIFEST.csv
  3. stages them byte-for-byte into data/geo/<run_id>/
  4. rewrites data/geo/active_run.json (the pin)
  5. regenerates tests/run_expectations.json from the newly staged table and
     prints an old→new summary of every expectation that moved

It deliberately does NOT commit anything. Review the diff — especially the
expectations summary and the caveat prose the tests will now check — then
commit and open a PR; the geo-contract-tests workflow re-runs the acceptance
tests on the new pin. Nothing reads geo_rep_LATEST.txt.

The expectations file records what THIS run's numbers are so the acceptance
tests can assert the app renders them faithfully. It is generated from the
same table the app reads, so it cannot catch a pipeline regression by itself —
that is the pipeline's own cross-check suite's job (a_/b_cross_checks, b2/b3
in the run directory). What it does catch: the app drifting from the table,
and any value change being silently invisible in review.
"""
import csv
import hashlib
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

CONTRACT_FILES = [
    # the long table is located by pattern, like geo_reader.R does
    "geo_dictionary.csv", "metric_dictionary.csv", "column_dictionary.csv",
    "d2_display_rule_vocabulary.csv", "d3_blank_inventory.csv",
    "b1_view_definitions.csv", "b1_forbidden_operations.csv",
    "c1_flag_registry.csv", "g1_unit_drivers.csv",
    "geo_reader.R", "CONTRACT.md",
]


def die(msg):
    sys.exit("advance_run: " + msg)


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_expectations(run_dir, long_name):
    """Derive the run-pinned numbers the acceptance tests assert."""
    long = read_csv(run_dir / long_name)
    views = read_csv(run_dir / "b1_view_definitions.csv")

    def view_rows(d):
        ests = d["estimands"].split("|")
        out = [r for r in long
               if r["geo_level"] == d["geo_level"]
               and r["estimand"] in ests
               and r["metric"] == d["metric"]]
        if d["include_gate_failures"] != "TRUE":
            out = [r for r in out if r["gate_status"] == "pass"]
        if d["include_withheld"] != "TRUE":
            out = [r for r in out if r["display_rule"] != "withheld"]
        return out

    exp = {"views": {}}
    for d in views:
        rows = view_rows(d)
        exp["views"][d["view_name"]] = {
            "rows": len(rows),
            "geographies": len({r["geo_code"] for r in rows}),
        }
    exp["long_table_rows"] = len(long)
    exp["withheld_rows"] = sum(1 for r in long if r["display_rule"] == "withheld")
    exp["blank_value_rows"] = sum(1 for r in long if r["value"] == "")
    exp["gate_pass_rows"] = sum(1 for r in long if r["gate_status"] == "pass")

    # the states absent from the descriptive spine, for the prose tripwire
    geo_dict = read_csv(run_dir / "geo_dictionary.csv")
    spine = {r["geo_code"] for r in long
             if r["geo_level"] == "us_state" and r["gate_status"] == "pass"}
    exp["absent_us_units"] = sorted(
        r["geo_name"] for r in geo_dict
        if r["geo_level"] == "us_state" and r["geo_code"] not in spine)
    return exp


def main():
    if len(sys.argv) != 2:
        die("usage: advance_run.py /path/to/run_dir")
    run_dir = Path(sys.argv[1]).resolve()
    if not run_dir.is_dir():
        die(f"{run_dir} is not a directory")

    run_info = {r["key"]: r["value"] for r in read_csv(run_dir / "RUN_INFO.csv")}
    if run_info.get("status") != "final":
        die(f"run status is '{run_info.get('status')}', not 'final' — refusing "
            "to advance the app to a wip run")
    run_id, snapshot_date = run_info["run_id"], run_info["snapshot_date"]

    longs = sorted(run_dir.glob("geo_representation_long_*.csv"))
    if len(longs) != 1:
        die(f"expected exactly one geo_representation_long_*.csv, found {len(longs)}")
    long_name = longs[0].name
    files = [long_name] + CONTRACT_FILES

    manifest = {r["filename"]: r["sha256"] for r in read_csv(run_dir / "MANIFEST.csv")}
    for f in files:
        if f not in manifest:
            die(f"{f} has no MANIFEST.csv entry")
        got = sha256(run_dir / f)
        if got != manifest[f]:
            die(f"hash mismatch for {f}: manifest {manifest[f][:12]}…, file {got[:12]}…")
    print(f"hash-verified {len(files)} contract files against MANIFEST.csv")

    dest = REPO / "data" / "geo" / run_id
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(run_dir / f, dest / f)
    print(f"staged -> {dest.relative_to(REPO)}")

    pin_path = REPO / "data" / "geo" / "active_run.json"
    old_pin = json.loads(pin_path.read_text()) if pin_path.exists() else {}
    pin_path.write_text(json.dumps({
        "run_id": run_id,
        "snapshot_date": snapshot_date,
        "run_dir": f"data/geo/{run_id}",
        "long_table": long_name,
        "note": "Pinned by scripts/geo/advance_run.py. Advancing runs is a "
                "deliberate act: run the script, review the expectations diff, "
                "update any caveat prose the tests flag, and merge the PR only "
                "with geo-contract-tests green. Nothing reads geo_rep_LATEST.txt.",
    }, indent=2) + "\n")

    exp_path = REPO / "tests" / "run_expectations.json"
    old_exp = json.loads(exp_path.read_text()) if exp_path.exists() else None
    exp = compute_expectations(dest, long_name)
    exp["run_id"] = run_id
    exp_path.write_text(json.dumps(exp, indent=2) + "\n")

    print(f"\npin: {old_pin.get('run_id', '(none)')} -> {run_id} "
          f"(snapshot {snapshot_date})")
    if old_exp:
        def walk(o, n, prefix=""):
            for k in sorted(set(o) | set(n)):
                ov, nv = o.get(k), n.get(k)
                if isinstance(ov, dict) or isinstance(nv, dict):
                    walk(ov or {}, nv or {}, prefix + k + ".")
                elif ov != nv:
                    print(f"  {prefix + k}: {ov} -> {nv}")
        print("expectation changes (review each — the dashboard will say these):")
        walk(old_exp, exp)
    print("\nNext: git diff, update any caveat text the prose tests flag, "
          "run `npm test`, commit, open a PR.")
    if old_pin.get("run_id") and old_pin["run_id"] != run_id:
        print(f"The previous run directory data/geo/{old_pin['run_id']} is still "
              "staged; remove it in the same PR unless you want both retained.")


if __name__ == "__main__":
    main()
