"""prune_snapshots.py retention, including the sponsors/ subdirectory that
rides with the demographics parts (a monthly, aggregate-only snapshot keeps
its summary and loses both)."""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import prune_snapshots as ps  # noqa: E402


def make_snapshot(root, date, with_parts=True, with_sponsors=True):
    d = os.path.join(root, "snapshots", date)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "dashboard-summary.json"), "w").write("{}")
    if with_parts:
        for i in (1, 2):
            open(os.path.join(d, f"demographics.part{i}.json.gz"), "wb").write(b"x")
    if with_sponsors:
        os.makedirs(os.path.join(d, "sponsors"), exist_ok=True)
        open(os.path.join(d, "sponsors", "bridge.csv.gz"), "wb").write(b"x")
        open(os.path.join(d, "sponsors", "bridge_meta.json"), "w").write("{}")
    return d


DATES = ["2026-03-01", "2026-03-15", "2026-04-05", "2026-06-07", "2026-06-21",
         "2026-07-05", "2026-07-19", "2026-08-02", "2026-08-16", "2026-08-30"]


def test_keep_policy_is_four_biweekly_then_one_per_month():
    biweekly, monthly = ps.compute_keep(DATES)
    assert len(biweekly) == 4 and max(biweekly) == "2026-08-30"
    assert monthly and monthly.isdisjoint(biweekly)
    assert {d[:7] for d in monthly}.isdisjoint({d[:7] for d in biweekly})


def test_monthly_snapshot_loses_parts_and_the_sponsors_bridge(tmp_path, monkeypatch):
    root = str(tmp_path)
    for d in DATES:
        make_snapshot(root, d)
    json.dump({"dates": DATES}, open(os.path.join(root, "history.json"), "w"))
    monkeypatch.setattr(ps, "SNAPSHOT_DIR", os.path.join(root, "snapshots"))
    monkeypatch.setattr(ps, "HISTORY_FILE", os.path.join(root, "history.json"))
    monkeypatch.setattr(sys, "argv", ["prune_snapshots.py"])

    biweekly, monthly = ps.compute_keep(DATES)
    ps.main()

    kept = set(os.listdir(os.path.join(root, "snapshots")))
    assert kept == biweekly | monthly
    for d in biweekly:                                    # full snapshots keep everything
        assert os.path.exists(os.path.join(root, "snapshots", d, "demographics.part1.json.gz"))
        assert os.path.exists(os.path.join(root, "snapshots", d, "sponsors", "bridge.csv.gz"))
    for d in monthly:                                     # aggregate-only: summary survives, bulk does not
        sdir = os.path.join(root, "snapshots", d)
        assert os.path.exists(os.path.join(sdir, "dashboard-summary.json"))
        assert not os.path.exists(os.path.join(sdir, "demographics.part1.json.gz"))
        assert not os.path.isdir(os.path.join(sdir, "sponsors"))
    assert json.load(open(os.path.join(root, "history.json")))["dates"] == sorted(biweekly | monthly)


def test_dry_run_changes_nothing(tmp_path, monkeypatch):
    root = str(tmp_path)
    for d in DATES:
        make_snapshot(root, d)
    json.dump({"dates": DATES}, open(os.path.join(root, "history.json"), "w"))
    monkeypatch.setattr(ps, "SNAPSHOT_DIR", os.path.join(root, "snapshots"))
    monkeypatch.setattr(ps, "HISTORY_FILE", os.path.join(root, "history.json"))
    monkeypatch.setattr(sys, "argv", ["prune_snapshots.py", "--dry-run"])
    ps.main()
    assert set(os.listdir(os.path.join(root, "snapshots"))) == set(DATES)
    assert os.path.isdir(os.path.join(root, "snapshots", DATES[0], "sponsors"))
    assert json.load(open(os.path.join(root, "history.json")))["dates"] == DATES
