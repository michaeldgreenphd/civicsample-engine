"""prune_snapshots.py retention, including what rides with the demographics
parts: the sponsors/ bridge, the sex/gender table, and a split week's
Studies-tab files, detail shards and run.json. A monthly, aggregate-only
snapshot keeps its summary (and industry_sponsors.json) and loses all of
them."""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import prune_snapshots as ps  # noqa: E402


SPLIT_FILES = ["studies_tab.part1.json.gz", "studies_tab.part2.json.gz", "run.json",
               os.path.join("detail", "0.json.gz"), os.path.join("detail", "255.json.gz")]


def make_snapshot(root: str, date: str, with_parts: bool = True, with_sponsors: bool = True,
                  split: bool = False) -> str:
    d = os.path.join(root, "snapshots", date)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "dashboard-summary.json"), "w").write("{}")
    open(os.path.join(d, "industry_sponsors.json"), "w").write("{}")
    if split:
        os.makedirs(os.path.join(d, "detail"), exist_ok=True)
        for f in SPLIT_FILES:
            open(os.path.join(d, f), "wb").write(b"x")
    if with_parts:
        for i in (1, 2):
            open(os.path.join(d, f"demographics.part{i}.json.gz"), "wb").write(b"x")
        # the per-trial sex/gender table rides with the parts
        open(os.path.join(d, "sex_gender_parsed.csv.gz"), "wb").write(b"x")
        open(os.path.join(d, "sex_gender_parsed_meta.json"), "w").write("{}")
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
        assert os.path.exists(os.path.join(root, "snapshots", d, "sex_gender_parsed.csv.gz"))
    for d in monthly:                                     # aggregate-only: summary survives, bulk does not
        sdir = os.path.join(root, "snapshots", d)
        assert os.path.exists(os.path.join(sdir, "dashboard-summary.json"))
        assert not os.path.exists(os.path.join(sdir, "demographics.part1.json.gz"))
        assert not os.path.isdir(os.path.join(sdir, "sponsors"))
        assert not os.path.exists(os.path.join(sdir, "sex_gender_parsed.csv.gz"))
        assert not os.path.exists(os.path.join(sdir, "sex_gender_parsed_meta.json"))
    assert json.load(open(os.path.join(root, "history.json")))["dates"] == sorted(biweekly | monthly)


def test_a_split_week_that_becomes_monthly_loses_its_class_files(tmp_path, monkeypatch) -> None:
    """A split week's Studies-tab files, detail shards and run.json ride with its
    parts: left behind in an aggregate, the shards alone would grow the Pages
    tree by ~62 MB a month toward the 1 GB limit."""
    root = str(tmp_path)
    for d in DATES:
        make_snapshot(root, d, split=True)
    json.dump({"dates": DATES}, open(os.path.join(root, "history.json"), "w"))
    monkeypatch.setattr(ps, "SNAPSHOT_DIR", os.path.join(root, "snapshots"))
    monkeypatch.setattr(ps, "HISTORY_FILE", os.path.join(root, "history.json"))
    monkeypatch.setattr(sys, "argv", ["prune_snapshots.py"])

    biweekly, monthly = ps.compute_keep(DATES)
    ps.main()

    assert monthly, "the fixture must exercise the monthly tier"
    for d in biweekly:                                    # full snapshots keep every class file
        for f in SPLIT_FILES:
            assert os.path.exists(os.path.join(root, "snapshots", d, f)), (d, f)
    for d in monthly:                                     # aggregates keep the summary and industry file only
        sdir = os.path.join(root, "snapshots", d)
        assert sorted(os.listdir(sdir)) == ["dashboard-summary.json", "industry_sponsors.json"], (d, os.listdir(sdir))


def test_a_monthly_snapshot_without_a_summary_keeps_its_split_files(tmp_path, monkeypatch) -> None:
    root = str(tmp_path)
    for d in DATES:
        make_snapshot(root, d, split=True)
    biweekly, monthly = ps.compute_keep(DATES)
    victim = sorted(monthly)[0]
    os.remove(os.path.join(root, "snapshots", victim, "dashboard-summary.json"))
    json.dump({"dates": DATES}, open(os.path.join(root, "history.json"), "w"))
    monkeypatch.setattr(ps, "SNAPSHOT_DIR", os.path.join(root, "snapshots"))
    monkeypatch.setattr(ps, "HISTORY_FILE", os.path.join(root, "history.json"))
    monkeypatch.setattr(sys, "argv", ["prune_snapshots.py"])
    ps.main()
    for f in SPLIT_FILES:
        assert os.path.exists(os.path.join(root, "snapshots", victim, f)), f


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
