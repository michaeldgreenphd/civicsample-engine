"""scripts/prune_snapshots.py: the retention policy over chained weekly runs,
and what one run does to a site checkout.

The rule it replaces measured the 14-day spacing from the newest date, which
moves every week, so every weekly run deleted last week's snapshot, the one
that would have qualified a week later (14 snapshots between July and October
2026). Its tests called it once, on dates already 14 days apart, and never
saw a weekly cadence. These run it week after week, on site checkouts with
real datasets cut by scripts/split_data.py (tests/snapshot_helpers.py), the
way the publish step runs it: retention while data/ still holds last week's
dataset, then this week's files into data/.

- EPOCH (a Sunday), fortnight 20 starting 2026-10-11, three complete
  snapshots and the aggregate keep-list are pinned; empty input plans
  nothing and raises nothing.
- From the site's real 2026-10-04 state, 16 weekly runs: three complete
  snapshots after every run, the set moving by at most one a run, every
  fortnight's first week archived the run after it and kept until three newer
  fortnights have theirs, no date gone and back, one folder per month kept for
  good, and history.json naming exactly what is there.
- The real runs since 2026-07-03 (a missed week, a Friday and a Tuesday run):
  the 2026-09-27 regression, 09-27 archived and kept, July and September kept.
- Four Sundays that publish nothing: the outgoing week, the only one of its
  month, is archived and slimmed into the month's aggregate, not lost.
- A same-day re-run archives nothing and changes nothing.
- Archiving copies every file of the outgoing dataset (whole parts; core,
  studies_tab, detail and run.json), its summary, sex/gender pair and methods
  text byte for byte, and never the March details, industry_sponsors.json or
  the sponsor bridge; it refuses a dataset with a file from another run, a
  summary from another run, or a run of another day (run.json's
  snapshot_date exactly, else the pull's day or the next), and leaves an
  existing folder alone.
- Slimming writes the archive file from the folder's own records first (whole
  or split, equal to the record projected onto the contract), covering
  exactly its recentStudies, then keeps only it and the summary; a folder
  whose summary lists a study its records lack, or a run without a readable
  contract, keeps its files. A slim cut short is finished on the next run.
- history.json: dates (strings), latest and archives, from what is on disk.
  A listed date with no folder goes; so does snapshots/<latest>/. A
  history.json it cannot read, or a snapshot newer than the latest, stops it
  before it changes anything; a dry run changes nothing.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import prune_snapshots as ps  # noqa: E402
import snapshot_helpers as sh  # noqa: E402
import split_helpers as h  # noqa: E402
from src import archive_records  # noqa: E402
from src import site_layout as sl  # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts", "prune_snapshots.py")
WORKFLOW = open(os.path.join(ROOT, ".github", "workflows", "extract.yml")).read()
# The site on 2026-10-04: three summary-only months, four complete folders,
# 10-04's a copy of data/ (the publish step wrote one every week until now).
REAL_AGGREGATES = ["2026-02-22", "2026-03-29", "2026-04-26"]
REAL_COMPLETE = ["2026-05-31", "2026-06-14", "2026-08-02", "2026-10-04"]


def sundays(first: str, n: int) -> list[str]:
    start = date.fromisoformat(first)
    return [str(start + timedelta(days=7 * i)) for i in range(n)]


def publish(tmp: pathlib.Path, site: pathlib.Path, day: str, split: bool = False) -> ps.Outcome:
    """One weekly publish as extract.yml orders it: retention while data/ holds
    last week's dataset, then this week's files into data/."""
    out = ps.run(str(site), day, None, dry=False)
    assert out is not None
    sh.write_week(tmp, site / "data", day, split)
    return out


def kinds(site: pathlib.Path) -> dict[str, str]:
    folders, _ = ps.scan(str(site / "snapshots"))
    return {d: f.kind for d, f in folders.items()}


def tree_digest(root: pathlib.Path) -> str:
    """Every path and byte under root, as one digest."""
    digest = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        digest.update(str(p.relative_to(root)).encode())
        if p.is_file():
            digest.update(p.read_bytes())
    return digest.hexdigest()


# ── the policy ──────────────────────────────────────────────────────────────

def test_the_anchor_and_the_window_are_pinned() -> None:
    """Moving EPOCH re-anchors every fortnight and deletes what the old anchor
    kept; resizing the window or the keep-list deletes snapshots too."""
    assert ps.EPOCH == date(2026, 1, 4) and ps.EPOCH.weekday() == 6, "EPOCH must stay Sunday 2026-01-04"
    assert ps.fortnight("2026-10-11") == 20 and ps.fortnight("2026-10-18") == 20
    assert ps.fortnight("2026-10-04") == 19 and ps.fortnight("2026-09-27") == 19
    assert ps.COMPLETE_SNAPSHOTS == 3, "owner decision 19b: three complete snapshots"
    assert ps.AGGREGATE_KEEP == {"dashboard-summary.json", "archive_records.json.gz"}


def test_empty_input_plans_nothing() -> None:
    assert ps.plan_retention([], [], "2026-10-11") == ps.Plan([], [])
    assert ps.plan_retention([], ["2026-02-22"], "2026-10-11") == ps.Plan([], ["2026-02-22"])


def test_a_fortnight_is_kept_by_its_earliest_week_whatever_comes_after() -> None:
    plan = ps.plan_retention(["2026-09-27", "2026-10-04", "2026-10-11"], [], "2026-10-18")
    assert plan.complete == ["2026-09-27", "2026-10-11"], "10-04 shares 09-27's fortnight"
    plan = ps.plan_retention(["2026-10-04", "2026-10-11"], [], "2026-10-18")
    assert plan.complete == ["2026-10-04", "2026-10-11"], "with 09-27 gone, 10-04 is fortnight 19's earliest"


# ── chained weekly runs ─────────────────────────────────────────────────────

def test_sixteen_weekly_runs_from_the_real_2026_10_04_state(tmp_path: pathlib.Path) -> None:
    site = sh.make_site(tmp_path, "2026-10-04", REAL_COMPLETE, REAL_AGGREGATES)
    runs = sundays("2026-10-11", 16)
    complete_by_run: dict[str, list[str]] = {}
    gone: set[str] = set()
    months: set[str] = {d[:7] for d in REAL_COMPLETE + REAL_AGGREGATES}
    previous: set[str] = set(REAL_COMPLETE)
    for day in runs:
        out = publish(tmp_path, site, day)
        state = kinds(site)
        complete = sorted(d for d, k in state.items() if k == ps.COMPLETE)
        complete_by_run[day] = complete
        assert len(complete) == 3, (day, complete)
        assert len(set(complete) - previous) <= 1 and len(previous - set(complete)) <= 1, (day, previous, complete)
        previous = set(complete)
        assert not gone & set(state), f"{day}: {sorted(gone & set(state))} came back"
        gone |= set(out.removed)
        assert all(k != ps.UNUSABLE for k in state.values()), state
        history = sh.read_history(site)
        assert history["latest"] == day and day not in state, "the latest lives in data/ only"
        assert history["dates"] == sorted([*state, day]) and all(isinstance(d, str) for d in history["dates"])
        aggregates = sorted(d for d, k in state.items() if k == ps.AGGREGATE)
        assert sorted(history["archives"]) == aggregates
        months |= {d[:7] for d in state}
        for d in aggregates:
            assert sorted(os.listdir(site / "snapshots" / d)) in (
                ["dashboard-summary.json"], ["archive_records.json.gz", "dashboard-summary.json"]), d
            if d not in REAL_AGGREGATES:          # slimmed by this policy: its own records came first
                assert history["archives"][d] == {"kind": "aggregate", "detail": "archive_records.json.gz"}
    # Every fortnight's first Sunday is archived the run after it and kept
    # complete until three newer fortnights have theirs: six runs.
    firsts = [d for d in runs if ps.fortnight(d) != ps.fortnight(str(date.fromisoformat(d) - timedelta(days=7)))]
    assert firsts[0] == "2026-10-11"
    for first in firsts:
        held = [day for day in runs if first in complete_by_run[day]]
        expected = [day for day in runs if 7 <= (date.fromisoformat(day) - date.fromisoformat(first)).days <= 42]
        assert held == expected, (first, held)
    # The week between two fortnights' firsts is never archived.
    seconds = [d for d in runs[:-1] if d not in firsts]
    assert not set(seconds) & set(kinds(site)) and not set(seconds) & gone
    # One folder per month for good: every month seen keeps exactly one,
    # except those the window still covers.
    state = kinds(site)
    for month in sorted(months):
        held = [d for d in state if d[:7] == month]
        window = {d[:7] for d in complete_by_run[runs[-1]]} | {runs[-1][:7]}
        assert len(held) == 1 or month in window, (month, held)
    assert [d for d, k in sorted(state.items()) if k == ps.AGGREGATE] == [
        "2026-02-22", "2026-03-29", "2026-04-26", "2026-05-31", "2026-06-14", "2026-08-02", "2026-10-25",
        "2026-11-22"]
    assert complete_by_run[runs[-1]] == ["2026-12-20", "2027-01-03", "2027-01-17"]


def test_the_2026_09_27_regression_on_the_real_runs_since_july(tmp_path: pathlib.Path) -> None:
    """The site's real runs from the 2026-07-03 prune (26 snapshots to 7) to
    2026-10-11: a missed week (08-09), a Friday run (08-28) and a Tuesday run
    (09-15). Under the old rule each run deleted the run before it; 09-27, the
    first Sunday of fortnight 19, was deleted on 10-04."""
    site = sh.make_site(tmp_path, "2026-06-28", ["2026-05-17", "2026-05-31", "2026-06-14", "2026-06-28"],
                        REAL_AGGREGATES)
    runs = ["2026-07-05", "2026-07-12", "2026-07-19", "2026-07-26", "2026-08-02", "2026-08-16", "2026-08-23",
            "2026-08-28", "2026-08-30", "2026-09-06", "2026-09-13", "2026-09-15", "2026-09-20", "2026-09-27",
            "2026-10-04", "2026-10-11"]
    archived = {}
    for day in runs:
        out = publish(tmp_path, site, day)
        archived[out.archived.get("date")] = out.archived.get("status")
    assert archived["2026-09-27"] == "archived", "the 09-27 regression: archived at the 10-04 run"
    assert archived["2026-08-28"] == "not kept" and archived["2026-09-15"] == "not kept"
    assert archived["2026-08-02"] == "archived", "after the missed week, 08-02 is fortnight 15's first"
    assert archived["2026-09-13"] == "archived"
    assert archived["2026-10-04"] == "not kept", "10-04 shares fortnight 19 with 09-27"
    state = kinds(site)
    assert sorted(d for d, k in state.items() if k == ps.COMPLETE) == ["2026-08-30", "2026-09-13", "2026-09-27"]
    assert sorted(d for d, k in state.items() if k == ps.AGGREGATE) == [
        "2026-02-22", "2026-03-29", "2026-04-26", "2026-05-31", "2026-06-28", "2026-07-19"]


def test_after_sundays_that_publish_nothing_the_outgoing_week_keeps_its_month(tmp_path: pathlib.Path) -> None:
    """11-08 to 11-29 publish nothing (the registry down, or the gate blocking
    each week), so data/ still serves 11-01 at the 12-06 run. 11-01 is the
    second week of fortnight 21, whose first, 10-25, is kept complete; but no
    kept week and not the latest is in November, so 11-01 is its month's
    aggregate: archived out of data/ and slimmed in the same run, its own
    records written first. Not archiving it would leave November with no
    snapshot at all."""
    site = sh.make_site(tmp_path, "2026-10-04", REAL_COMPLETE, REAL_AGGREGATES)
    for day in sundays("2026-10-11", 4):
        publish(tmp_path, site, day)
    before = tree_digest(site)
    dry = ps.run(str(site), "2026-12-06", None, dry=True)
    assert tree_digest(site) == before, "the dry run changed the site"
    assert dry.archived["status"] == "would archive" and dry.slimmed == ["2026-11-01"], dry.report()
    assert "demographics.part1.json.gz" in dry.stripped["2026-11-01"]
    out = publish(tmp_path, site, "2026-12-06")
    assert out.archived["status"] == "archived" and "aggregate" in out.archived["reason"], out.archived
    assert out.slimmed == ["2026-11-01"] and out.warnings == [], out.report()
    assert (out.complete, out.slimmed, out.removed) == (dry.complete, dry.slimmed, dry.removed)
    folder = site / "snapshots" / "2026-11-01"
    assert sorted(os.listdir(folder)) == ["archive_records.json.gz", "dashboard-summary.json"]
    doc = h.read_gz(folder / "archive_records.json.gz")
    assert (doc["source_extracted_at"], doc["source_pipeline_commit"]) == tuple(sh.stamps("2026-11-01").values())
    history = sh.read_history(site)
    assert "2026-11-01" in history["dates"]
    assert history["archives"]["2026-11-01"] == {"kind": "aggregate", "detail": "archive_records.json.gz"}
    months = {d[:7] for d in history["dates"]}
    assert months == {"2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-08", "2026-10", "2026-11",
                      "2026-12"}, sorted(months)


def test_a_same_day_rerun_archives_nothing_and_changes_nothing(tmp_path: pathlib.Path) -> None:
    site = sh.make_site(tmp_path, "2026-10-04", REAL_COMPLETE, REAL_AGGREGATES)
    publish(tmp_path, site, "2026-10-11")
    before = tree_digest(site / "snapshots"), sh.read_history(site)
    out = publish(tmp_path, site, "2026-10-11")
    assert out.archived == {} and out.removed == [] and out.slimmed == []
    assert (tree_digest(site / "snapshots"), sh.read_history(site)) == before


# ── archiving the outgoing week ─────────────────────────────────────────────

@pytest.mark.parametrize("split", [False, True], ids=["whole-parts", "split"])
def test_last_weeks_dataset_is_archived_whole_and_byte_for_byte(tmp_path: pathlib.Path, split: bool) -> None:
    site = sh.make_site(tmp_path, "2026-10-11", split=split,
                        history={"dates": ["2026-10-11"], "latest": "2026-10-11", "archives": {}})
    data_before = {rel: (site / "data" / rel).read_bytes() for rel in sh.files(site / "data")}
    out = publish(tmp_path, site, "2026-10-18", split=split)
    assert out.archived["status"] == "archived", out.archived
    archived = sh.files(site / "snapshots" / "2026-10-11")
    dataset = [f"demographics.part{k}.json.gz" for k in range(1, 9)] + ["run.json"]
    if split:
        dataset += [f"studies_tab.part{k}.json.gz" for k in range(1, 9)] + [f"detail/{n}.json.gz" for n in range(256)]
    assert archived == sorted(dataset + ["dashboard-summary.json", "sex_gender/methods.json",
                                         "sex_gender_parsed.csv.gz", "sex_gender_parsed_meta.json"])
    for rel in archived:
        assert (site / "snapshots" / "2026-10-11" / rel).read_bytes() == data_before[rel], rel
    assert out.archived["files"] == len(archived)
    assert sh.read_history(site) == {"dates": ["2026-10-11", "2026-10-18"], "latest": "2026-10-18", "archives": {}}
    assert not list((site / "snapshots").glob(".*")), "a partial copy was left behind"


@pytest.mark.parametrize("damage,why", [
    (lambda s: _restamp(s / "data" / "demographics.part3.json.gz", extracted_at="2026-10-04T06:00:00+00:00"),
     "demographics.part3.json.gz comes from another run"),
    (lambda s: _restamp(s / "data" / "demographics.part3.json.gz", pipeline_commit="0" * 40),
     "demographics.part3.json.gz comes from another run"),
    (lambda s: (s / "data" / "demographics.part8.json.gz").unlink(), "lacks 1 of its 8 dataset files"),
    (lambda s: (s / "data" / "demographics.part2.json.gz").write_bytes(
        (s / "data" / "demographics.part2.json.gz").read_bytes()[:-20]), "demographics.part2.json.gz is not gzipped JSON"),
    (lambda s: sh.cut_short_past_its_header(s / "data" / "demographics.part4.json.gz"),
     "demographics.part4.json.gz is not gzipped JSON"),
    (lambda s: (s / "data" / "dashboard-summary.json").write_text(json.dumps(sh.summary("2026-10-04"))),
     "dashboard-summary.json comes from another run"),
    (lambda s: (s / "data" / "run.json").write_text(json.dumps({**json.loads((s / "data" / "run.json").read_text()),
                                                                **sh.stamps("2026-10-04")})),
     "run.json says extracted_at"),
], ids=["a-part-from-another-run", "a-part-from-another-commit", "a-part-missing", "a-truncated-part",
        "a-part-cut-short-past-its-header", "a-summary-from-another-run", "run-json-from-another-run"])
def test_a_week_whose_files_are_not_one_run_is_not_archived(tmp_path: pathlib.Path,
                                                           damage: Callable[[pathlib.Path], object], why: str) -> None:
    site = sh.make_site(tmp_path, "2026-10-11", history={"dates": ["2026-10-11"], "latest": "2026-10-11"})
    damage(site)
    out = publish(tmp_path, site, "2026-10-18")
    assert out.archived["status"] == "refused" and why in out.archived["reason"], out.archived
    assert not (site / "snapshots" / "2026-10-11").exists()
    assert any("2026-10-11 was not archived" in w for w in out.warnings)
    assert sh.read_history(site)["dates"] == ["2026-10-18"], "a week that was not archived is not listed"


def test_data_holding_another_days_run_is_not_archived_under_the_latest_date(tmp_path: pathlib.Path) -> None:
    """Run by hand after this week's files were copied in, the script would
    otherwise archive this week's dataset under last week's date."""
    site = sh.make_site(tmp_path, "2026-10-18", history={"dates": ["2026-10-11"], "latest": "2026-10-11"})
    out = ps.run(str(site), "2026-10-18", None, dry=False)
    assert out.archived["status"] == "refused" and "not 2026-10-11's" in out.archived["reason"]
    assert not (site / "snapshots").exists() or not (site / "snapshots" / "2026-10-11").exists()


@pytest.mark.parametrize("extracted_at,snapshot_date,status", [
    ("2026-10-12T00:04:00+00:00", "2026-10-11", "archived"),
    ("2026-10-11T06:00:00+00:00", "2026-10-04", "refused"),
    ("2026-10-12T00:10:00+00:00", None, "archived"),
    ("2026-10-13T06:00:00+00:00", None, "refused"),
], ids=["a-run-started-before-midnight-utc", "run-json-naming-another-date",
        "before-run-json-had-a-snapshot-date-the-next-day", "before-run-json-had-a-snapshot-date-two-days-later"])
def test_the_outgoing_week_is_the_run_its_run_json_names(tmp_path: pathlib.Path, extracted_at: str,
                                                         snapshot_date: str | None, status: str) -> None:
    """data/run.json's snapshot_date is the date the run was published as (the
    weekly job's date): the week is archived under that date only, even when
    the pull ran past midnight UTC and its extracted_at says the next day. A
    run.json without one (written before 2026-10) falls back to extracted_at,
    on the day or the day after."""
    site = sh.make_site(tmp_path, "2026-10-11", history={"dates": ["2026-10-11"], "latest": "2026-10-11"})
    _restamp_week(site / "data", extracted_at, snapshot_date)
    out = publish(tmp_path, site, "2026-10-18")
    assert out.archived["status"] == status, out.archived
    if status == "archived":
        assert kinds(site) == {"2026-10-11": ps.COMPLETE} and out.warnings == [], out.warnings
    else:
        assert "not 2026-10-11's" in out.archived["reason"] and not (site / "snapshots" / "2026-10-11").exists()


def test_a_sex_gender_pair_from_another_run_stays_behind(tmp_path: pathlib.Path) -> None:
    site = sh.make_site(tmp_path, "2026-10-11", history={"dates": ["2026-10-11"], "latest": "2026-10-11"})
    meta = json.loads((site / "data" / "sex_gender_parsed_meta.json").read_text())
    (site / "data" / "sex_gender_parsed_meta.json").write_text(json.dumps({**meta, "snapshot_date": "2026-10-04"}))
    out = publish(tmp_path, site, "2026-10-18")
    assert out.archived["status"] == "archived"
    held = sh.files(site / "snapshots" / "2026-10-11")
    assert "sex_gender_parsed_meta.json" not in held and "sex_gender_parsed.csv.gz" not in held
    assert "sex_gender/methods.json" in held and any("sex/gender table of 2026-10-11" in w for w in out.warnings)


def test_the_week_between_two_fortnights_firsts_is_not_archived(tmp_path: pathlib.Path) -> None:
    site = sh.make_site(tmp_path, "2026-10-18", ["2026-10-11"],
                        history={"dates": ["2026-10-11", "2026-10-18"], "latest": "2026-10-18"})
    out = publish(tmp_path, site, "2026-10-25")
    assert out.archived == {"date": "2026-10-18", "status": "not kept", "files": 0, "bytes": 0,
                            "reason": "fortnight 20 is represented by 2026-10-11"}
    assert sorted(kinds(site)) == ["2026-10-11"]


def test_a_folder_the_old_publish_step_wrote_is_left_as_it_is(tmp_path: pathlib.Path) -> None:
    """Until this change every week was also copied into snapshots/<date>/:
    on the first run that folder is the outgoing week's archive already."""
    site = sh.make_site(tmp_path, "2026-10-04", ["2026-10-04"])
    before = tree_digest(site / "snapshots" / "2026-10-04")
    out = publish(tmp_path, site, "2026-10-11")
    assert out.archived["status"] == "already archived" and out.warnings == []
    assert tree_digest(site / "snapshots" / "2026-10-04") == before


# ── slimming into a monthly aggregate ───────────────────────────────────────

@pytest.mark.parametrize("split", [False, True], ids=["whole-parts", "split"])
def test_a_slimmed_snapshot_keeps_its_own_records_for_exactly_its_recent_studies(tmp_path: pathlib.Path,
                                                                                 split: bool) -> None:
    site = sh.make_site(tmp_path, "2026-10-04", ["2026-05-31", "2026-06-14", "2026-08-02", "2026-10-04"],
                        split=split)
    (site / "snapshots" / "2026-05-31" / "industry_sponsors.json").write_text("{}")
    (site / "snapshots" / "2026-05-31" / "sponsors").mkdir()
    (site / "snapshots" / "2026-05-31" / "sponsors" / "bridge.csv.gz").write_bytes(b"x")
    out = publish(tmp_path, site, "2026-10-11", split=split)
    assert out.slimmed == ["2026-05-31"] and out.removed == []
    folder = site / "snapshots" / "2026-05-31"
    assert sorted(os.listdir(folder)) == ["archive_records.json.gz", "dashboard-summary.json"]
    doc = h.read_gz(folder / "archive_records.json.gz")
    assert list(doc) == ["source_extracted_at", "source_pipeline_commit", "class", "data"]
    assert (doc["source_extracted_at"], doc["source_pipeline_commit"]) == tuple(sh.stamps("2026-05-31").values())
    assert doc["class"] == "archive" and list(doc["data"]) == sh.IDS
    spec = sl.record_spec(sh.contract(split))
    for record in h.whole_records(sh.IDS):
        assert doc["data"][record["nct_id"]] == sl.project_object(record, spec), record["nct_id"]
        assert list(doc["data"][record["nct_id"]]) == sorted(doc["data"][record["nct_id"]])
    assert archive_records.problems(str(folder / "archive_records.json.gz"),
                                    json.loads((folder / "dashboard-summary.json").read_text())) == []
    assert sh.read_history(site)["archives"]["2026-05-31"] == {"kind": "aggregate", "detail": "archive_records.json.gz"}


def test_a_week_gives_the_same_archive_file_whole_or_split(tmp_path: pathlib.Path) -> None:
    """The same records give the same bytes, whichever layout they were cut in:
    a split record comes back core fields first, so every entry is written
    with its keys sorted."""
    made = []
    for split in (False, True):
        site = sh.make_site(tmp_path / str(split), "2026-10-04", ["2026-05-31", "2026-06-14", "2026-08-02", "2026-10-04"],
                            split=split)
        publish(tmp_path / str(split), site, "2026-10-11", split=split)
        made.append((site / "snapshots" / "2026-05-31" / "archive_records.json.gz").read_bytes())
    assert made[0] == made[1]


def test_a_snapshot_whose_summary_lists_a_study_its_records_lack_stays_complete(tmp_path: pathlib.Path) -> None:
    site = sh.make_site(tmp_path, "2026-10-04", REAL_COMPLETE)
    path = site / "snapshots" / "2026-05-31" / "dashboard-summary.json"
    summary = json.loads(path.read_text())
    summary["recentStudies"].append({"nct_id": "NCT09999999"})
    path.write_text(json.dumps(summary))
    out = publish(tmp_path, site, "2026-10-11")
    assert out.slimmed == [] and "2026-05-31" in out.complete
    assert any("snapshots/2026-05-31/ stays complete" in w and "NCT09999999" in w for w in out.warnings)
    assert (site / "snapshots" / "2026-05-31" / "demographics.part1.json.gz").exists()
    assert not (site / "snapshots" / "2026-05-31" / "archive_records.json.gz").exists()


def test_without_the_sites_contract_nothing_is_slimmed(tmp_path: pathlib.Path) -> None:
    site = sh.make_site(tmp_path, "2026-10-04", REAL_COMPLETE)
    (site / "tests" / "record_contract.json").unlink()
    out = publish(tmp_path, site, "2026-10-11")
    assert out.slimmed == [] and (site / "snapshots" / "2026-05-31" / "demographics.part1.json.gz").exists()
    assert any("record contract cannot be read" in w for w in out.warnings)


def test_an_aggregate_keeps_only_its_summary_and_archive_file(tmp_path: pathlib.Path) -> None:
    site = sh.make_site(tmp_path, "2026-10-04", ["2026-06-14", "2026-08-02", "2026-10-04"], ["2026-04-26"])
    folder = site / "snapshots" / "2026-04-26"
    (folder / "industry_sponsors.json").write_text("{}")
    (folder / "sponsors").mkdir()
    (folder / "sponsors" / "bridge.csv.gz").write_bytes(b"x")
    (folder / "notes.txt").write_text("an unknown file")
    publish(tmp_path, site, "2026-10-11")
    assert sorted(os.listdir(folder)) == ["dashboard-summary.json"]
    assert sh.read_history(site)["archives"]["2026-04-26"] == {"kind": "aggregate"}


@pytest.mark.parametrize("split", [False, True], ids=["whole-parts", "split"])
def test_dataset_files_without_core_part_1_are_left_as_they_are(tmp_path: pathlib.Path, split: bool) -> None:
    """Core part 1 gone and the rest of the dataset there (a partial restore or
    a clean-up by hand), with no archive file that checks: not an aggregate
    whose strip was cut short, so nothing in it is deleted. It is left as it
    is, warned about and not listed, like any damaged dataset. So is a summary
    folder holding a stray dataset file."""
    site = sh.make_site(tmp_path, "2026-10-11", ["2026-06-14", "2026-09-13", "2026-09-27", "2026-10-04"],
                        ["2026-02-22"], split=split)
    damaged = site / "snapshots" / "2026-06-14"
    (damaged / "demographics.part1.json.gz").unlink()
    stray = site / "snapshots" / "2026-02-22"
    (stray / "detail").mkdir()
    (stray / "detail" / "0.json.gz").write_bytes(b"x")
    before = {d: tree_digest(d) for d in (damaged, stray)}
    out = ps.run(str(site), "2026-10-18", None, dry=False)
    assert {d: tree_digest(d) for d in (damaged, stray)} == before, "a folder holding dataset files was touched"
    assert kinds(site)["2026-06-14"] == kinds(site)["2026-02-22"] == ps.UNUSABLE
    assert not {"2026-06-14", "2026-02-22"} & (set(out.stripped) | set(out.removed) | set(out.aggregates))
    assert not {"2026-06-14", "2026-02-22"} & set(sh.read_history(site)["dates"])
    for d in ("2026-06-14", "2026-02-22"):
        assert any(f"snapshots/{d}/ is left as it is" in w and "without demographics.part1.json.gz" in w
                   for w in out.warnings), out.warnings


@pytest.mark.parametrize("cut_short", [(1, 2, 3), (6, 7, 8)], ids=["part1-gone", "part1-left"])
def test_a_slim_cut_short_is_finished_on_the_next_run(tmp_path: pathlib.Path, cut_short: tuple[int, ...]) -> None:
    """The archive file is written and checked before the strip starts, so a
    folder that has it and has lost some parts is an aggregate mid-strip."""
    site = sh.make_site(tmp_path, "2026-10-04", REAL_COMPLETE)
    folder = site / "snapshots" / "2026-05-31"
    ds = ps.df.inspect(str(folder))
    archive_records.write(archive_records.build(ds, sh.contract()), str(folder / sl.ARCHIVE_FILE), ds.summary)
    for k in cut_short:
        (folder / f"demographics.part{k}.json.gz").unlink()
    assert kinds(site)["2026-05-31"] == ps.AGGREGATE
    publish(tmp_path, site, "2026-10-11")
    assert sorted(os.listdir(folder)) == ["archive_records.json.gz", "dashboard-summary.json"]
    assert sh.read_history(site)["archives"]["2026-05-31"]["detail"] == "archive_records.json.gz"


@pytest.mark.parametrize("lost", [5, 1], ids=["part5-lost", "part1-lost"])
def test_an_archive_file_that_does_not_check_makes_no_damaged_snapshot_an_aggregate(tmp_path: pathlib.Path,
                                                                                    lost: int) -> None:
    """A damaged dataset beside an archive file of another run (or one that
    does not cover its summary's studies) is not a slim cut short: nothing
    stands in for what the strip would delete, so the folder is left as it
    is, warned about and not listed."""
    site = sh.make_site(tmp_path, "2026-10-04", REAL_COMPLETE, REAL_AGGREGATES)
    folder = site / "snapshots" / "2026-05-31"
    (folder / f"demographics.part{lost}.json.gz").unlink()
    other = sh.stamps("2026-05-24")
    with gzip.open(folder / sl.ARCHIVE_FILE, "wt") as fh:
        json.dump({"source_extracted_at": other["extracted_at"], "source_pipeline_commit": other["pipeline_commit"],
                   "class": "archive", "data": {nct: {"nct_id": nct} for nct in sh.IDS}}, fh)
    damaged = tree_digest(folder)
    out = publish(tmp_path, site, "2026-10-11")
    assert kinds(site)["2026-05-31"] == ps.UNUSABLE
    assert tree_digest(folder) == damaged, "a damaged snapshot was stripped on an archive file that does not check"
    assert "2026-05-31" not in sh.read_history(site)["dates"] and "2026-05-31" not in out.stripped
    assert any("snapshots/2026-05-31/ is left as it is" in w for w in out.warnings), out.warnings


# ── history.json and the refusals ───────────────────────────────────────────

def test_history_lists_what_is_there_and_drops_a_date_whose_folder_is_gone(tmp_path: pathlib.Path) -> None:
    site = sh.make_site(tmp_path, "2026-10-04", REAL_COMPLETE, REAL_AGGREGATES)
    history = sh.read_history(site)
    (site / "history.json").write_text(json.dumps({"dates": history["dates"] + ["2026-09-27"]}))
    shutil.rmtree(site / "snapshots" / "2026-03-29")
    out = publish(tmp_path, site, "2026-10-11")
    assert out.dropped == ["2026-03-29", "2026-09-27"]
    written = sh.read_history(site)
    assert written == {"dates": ["2026-02-22", "2026-04-26", "2026-05-31", "2026-06-14", "2026-08-02", "2026-10-04",
                                 "2026-10-11"],
                       "latest": "2026-10-11",
                       "archives": {"2026-02-22": {"kind": "aggregate"}, "2026-04-26": {"kind": "aggregate"},
                                    "2026-05-31": {"kind": "aggregate", "detail": "archive_records.json.gz"}}}
    assert (site / "history.json").read_text().endswith("}\n") and not (site / "history.json.partial").exists()


def test_a_folder_for_the_latest_date_goes(tmp_path: pathlib.Path) -> None:
    """data/ serves the latest date; a folder for it is a copy."""
    site = sh.make_site(tmp_path, "2026-10-11", ["2026-10-11"],
                        history={"dates": ["2026-10-11"], "latest": "2026-10-11"})
    out = ps.run(str(site), "2026-10-11", None, dry=False)
    assert out.removed == ["2026-10-11"] and not (site / "snapshots" / "2026-10-11").exists()


def test_a_leftover_folder_for_the_latest_date_takes_no_fortnights_place(tmp_path: pathlib.Path) -> None:
    """The day this change is deployed, a manual re-run can follow the old
    publish step's Sunday run, which left snapshots/<latest>/ beside data/.
    That copy is not a fourth complete fortnight: the three archived ones stay
    complete, nothing is slimmed, and only the copy goes."""
    site = sh.make_site(tmp_path, "2026-10-11", ["2026-08-30", "2026-09-13", "2026-09-27", "2026-10-11"])
    out = ps.run(str(site), "2026-10-11", None, dry=False)
    assert out.complete == ["2026-08-30", "2026-09-13", "2026-09-27"], out.report()
    assert out.slimmed == [] and out.removed == ["2026-10-11"] and out.warnings == []
    assert sh.read_history(site) == {"dates": ["2026-08-30", "2026-09-13", "2026-09-27", "2026-10-11"],
                                     "latest": "2026-10-11", "archives": {}}


def test_an_unusable_folder_is_left_as_it_is_and_not_listed(tmp_path: pathlib.Path) -> None:
    """No summary; or a damaged dataset with no archive file: nothing that
    might be recovered is deleted, and the menu does not offer it."""
    site = sh.make_site(tmp_path, "2026-10-04", ["2026-06-14", "2026-08-02", "2026-10-04"])
    (site / "snapshots" / "2026-09-13").mkdir()
    (site / "snapshots" / "2026-09-13" / "notes.txt").write_text("no summary")
    (site / "snapshots" / "2026-06-14" / "demographics.part5.json.gz").unlink()
    damaged = tree_digest(site / "snapshots" / "2026-06-14")
    out = publish(tmp_path, site, "2026-10-11")
    assert (site / "snapshots" / "2026-09-13" / "notes.txt").exists()
    assert tree_digest(site / "snapshots" / "2026-06-14") == damaged, "a damaged dataset was touched"
    listed = sh.read_history(site)["dates"]
    assert "2026-09-13" not in listed and "2026-06-14" not in listed
    assert any("snapshots/2026-09-13/ is left as it is" in w for w in out.warnings)
    assert any("snapshots/2026-06-14/ is left as it is" in w and "lacks 1 of its 8" in w for w in out.warnings)


def test_a_snapshot_part_cut_short_past_its_header_is_left_as_it_is(tmp_path: pathlib.Path) -> None:
    """Every file is decompressed to its end: a part whose header reads but
    whose stream was cut short is a damaged dataset, not a complete one, and
    is left as it is and not listed."""
    site = sh.make_site(tmp_path, "2026-10-04", REAL_COMPLETE, REAL_AGGREGATES)
    sh.cut_short_past_its_header(site / "snapshots" / "2026-08-02" / "demographics.part6.json.gz")
    damaged = tree_digest(site / "snapshots" / "2026-08-02")
    out = publish(tmp_path, site, "2026-10-11")
    assert kinds(site)["2026-08-02"] == ps.UNUSABLE
    assert tree_digest(site / "snapshots" / "2026-08-02") == damaged, "a damaged dataset was touched"
    assert "2026-08-02" not in sh.read_history(site)["dates"]
    assert any("snapshots/2026-08-02/ is left as it is" in w and "demographics.part6.json.gz is not gzipped JSON" in w
               for w in out.warnings), out.warnings


@pytest.mark.parametrize("change,why", [
    (lambda s: (s / "history.json").write_text("{not json"), "cannot be read"),
    (lambda s: (s / "history.json").write_text(json.dumps({"dates": ["2026-10-04"], "latest": "last week"})),
     "which is not a date"),
    (lambda s: shutil.copytree(s / "snapshots" / "2026-10-04", s / "snapshots" / "2026-10-25"), "newer than the latest"),
    (lambda s: (s / "history.json").write_text(json.dumps({"dates": ["2026-10-04", "2026-11-01"]})),
     "newer than the latest"),
], ids=["unreadable-history", "latest-not-a-date", "a-newer-folder", "a-newer-listed-date"])
def test_a_run_that_cannot_go_ahead_changes_nothing(tmp_path: pathlib.Path, change: Callable[[pathlib.Path], object],
                                                    why: str, capsys: pytest.CaptureFixture[str]) -> None:
    site = sh.make_site(tmp_path, "2026-10-04", REAL_COMPLETE, REAL_AGGREGATES)
    change(site)
    before = tree_digest(site)
    assert ps.main(["--site", str(site), "--latest", "2026-10-11"]) == 1
    assert why in capsys.readouterr().out
    assert tree_digest(site) == before


def test_a_dry_run_changes_nothing_and_says_what_a_run_would_do(tmp_path: pathlib.Path) -> None:
    site = sh.make_site(tmp_path, "2026-10-11", REAL_COMPLETE, REAL_AGGREGATES,
                        history={"dates": sorted(REAL_COMPLETE + REAL_AGGREGATES + ["2026-10-11"]),
                                 "latest": "2026-10-11"})
    before = tree_digest(site)
    report = tmp_path / "dry.json"
    assert ps.main(["--site", str(site), "--latest", "2026-10-18", "--dry-run", "--report", str(report)]) == 0
    assert tree_digest(site) == before
    dry = json.loads(report.read_text())
    assert dry["archived"]["status"] == "would archive" and dry["dry_run"] is True
    assert dry["complete"] == ["2026-08-02", "2026-10-04", "2026-10-11"]
    assert dry["slimmed"] == ["2026-05-31", "2026-06-14"] and dry["removed"] == []
    real = ps.run(str(site), "2026-10-18", None, dry=False)
    assert (real.complete, real.slimmed, real.removed) == (dry["complete"], dry["slimmed"], dry["removed"])


def test_a_dry_run_that_would_remove_a_snapshot_removes_nothing(tmp_path: pathlib.Path) -> None:
    """--dry-run is what a person runs by hand to preview a run: a plan that
    deletes a folder (10-04 shares fortnight 19 with 09-27) must leave it, and
    every other byte of the site, where it is."""
    site = sh.make_site(tmp_path, "2026-10-11", ["2026-09-13", "2026-09-27", "2026-10-04"], REAL_AGGREGATES)
    before = tree_digest(site)
    dry = ps.run(str(site), "2026-10-18", None, dry=True)
    assert dry.removed == ["2026-10-04"] and dry.archived["status"] == "would archive", dry.report()
    assert tree_digest(site) == before, "the dry run deleted or changed something"
    real = ps.run(str(site), "2026-10-18", None, dry=False)
    assert real.removed == ["2026-10-04"] and not (site / "snapshots" / "2026-10-04").exists()


def _recovery_commands() -> dict[str, list[str]]:
    """The commands prune_snapshots.py's docstring gives to get a week's files
    back, by the line that introduces them: each is a `c=$(git log ...)` line
    and the line after it."""
    lines = (ps.__doc__ or "").splitlines()
    return {lines[i - 1].strip(): [lines[i].strip(), lines[i + 1].strip()]
            for i, line in enumerate(lines) if line.strip().startswith("c=$(git log")}


@pytest.mark.parametrize("split", [False, True], ids=["whole-parts", "split"])
def test_the_documented_recovery_commands_bring_back_any_week(tmp_path: pathlib.Path, split: bool) -> None:
    """The commands the docstring and the README give, run as written in a git
    history of weekly publishes (each committed as the publish step commits
    it): a week served from data/ and never archived (10-18, fortnight 20's
    second week) comes back from its publish commit, whole or split; so does
    any other week; a snapshot slimmed (05-31) or deleted (10-04) comes back
    in place. The in-place command alone cannot bring back a week that never
    had a folder, which is why there are two."""
    git = shutil.which("git")
    assert git, "git is not installed"
    commands = _recovery_commands()
    assert len(commands) == 2, f"the docstring gives {len(commands)} recovery commands, not 2: {list(commands)}"
    readme = (pathlib.Path(ROOT) / "README.md").read_text()
    for pair in commands.values():
        assert all(c in readme for c in pair), f"README.md does not give {pair}"
    (anywhere, in_place) = commands.values()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "HOME": str(tmp_path)}

    def git_(*args: str) -> None:
        subprocess.run([git, "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=site, env=env, check=True,
                       capture_output=True)

    def week_files(folder: pathlib.Path) -> dict[str, bytes]:
        return {rel: (folder / rel).read_bytes() for rel in sh.files(folder)
                if re.fullmatch(r"(demographics|studies_tab)\.part\d+\.json\.gz|detail/.*|run\.json|"
                                r"dashboard-summary\.json|sex_gender_parsed.*|sex_gender/methods\.json", rel)}

    site = sh.make_site(tmp_path, "2026-10-04", REAL_COMPLETE, REAL_AGGREGATES, split=split)
    git_("init", "-q", "-b", "main")
    git_("add", "-A")
    git_("commit", "-qm", "Update demographics data 2026-10-04")
    served = {"2026-10-04": week_files(site / "data")}
    archived = {d: week_files(site / "snapshots" / d) for d in ("2026-05-31", "2026-10-04")}
    for day in sundays("2026-10-11", 6):
        publish(tmp_path, site, day, split=split)
        git_("add", "-A")
        git_("commit", "-qm", f"Update demographics data {day}")
        served[day] = week_files(site / "data")
    state = kinds(site)
    assert "2026-10-18" not in state and "2026-10-04" not in state and state["2026-05-31"] == ps.AGGREGATE

    def run(pair: list[str], day: str, into: str = "") -> subprocess.CompletedProcess[str]:
        script = "\n".join(pair).replace("<date>", day).replace("<dir>", into)
        return subprocess.run(["bash", "-e", "-c", script], cwd=site, env=env, capture_output=True, text=True)

    for day in ("2026-10-18", "2026-10-04", "2026-10-11"):
        out = tmp_path / f"back-{day}"
        r = run(anywhere, day, str(out))
        assert r.returncode == 0, r.stderr
        assert week_files(out) == served[day], f"{day} did not come back as its run published it"
    for day in ("2026-05-31", "2026-10-04"):
        r = run(in_place, day)
        assert r.returncode == 0, r.stderr
        assert week_files(site / "snapshots" / day) == archived[day], f"snapshots/{day}/ did not come back"
    r = run(in_place, "2026-10-18")
    assert r.returncode != 0 and not (site / "snapshots" / "2026-10-18").exists()


def test_with_nothing_to_retain_it_says_so(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ps.main(["--site", str(tmp_path)]) == 0
    assert "nothing to retain" in capsys.readouterr().out
    assert not (tmp_path / "history.json").exists()


def test_the_command_line_writes_its_report(tmp_path: pathlib.Path) -> None:
    site = sh.make_site(tmp_path, "2026-10-04", REAL_COMPLETE, REAL_AGGREGATES)
    report = tmp_path / "retention.json"
    r = subprocess.run([sys.executable, SCRIPT, "--site", str(site), "--latest", "2026-10-11", "--report", str(report)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    got = json.loads(report.read_text())
    assert got["archived"]["status"] == "already archived" and got["slimmed"] == ["2026-05-31"]
    assert "complete     (3): 2026-06-14, 2026-08-02, 2026-10-04" in r.stdout


def test_the_run_summary_reads_what_the_retention_report_carries(tmp_path: pathlib.Path) -> None:
    jq = shutil.which("jq")
    if not jq:
        pytest.skip("jq is not installed here (the runner has it)")
    m = re.search(r"jq -r '([^']*)' \"\$RUNNER_TEMP/retention\.json\"", WORKFLOW)
    assert m, "the run summary lost the retention row"
    assert '--report "$RUNNER_TEMP/retention.json"' in WORKFLOW
    site = sh.make_site(tmp_path, "2026-10-04", REAL_COMPLETE, REAL_AGGREGATES)
    report = tmp_path / "retention.json"
    assert ps.main(["--site", str(site), "--latest", "2026-10-11", "--report", str(report)]) == 0
    row = subprocess.run([jq, "-r", m.group(1), str(report)], capture_output=True, text=True, check=True).stdout.strip()
    assert row == ("latest 2026-10-11 (data/); last week 2026-10-04: already archived; complete: 2026-06-14, "
                   "2026-08-02, 2026-10-04; 4 monthly aggregates (1 with their own records); slimmed: 2026-05-31; "
                   "removed: none")


def _restamp(path: pathlib.Path, **stamps: str) -> None:
    with gzip.open(path, "rt") as fh:
        body = json.load(fh)
    body.update(stamps)
    with gzip.open(path, "wt") as fh:
        json.dump(body, fh)


def _restamp_week(folder: pathlib.Path, extracted_at: str, snapshot_date: str | None) -> None:
    """The whole-part week in folder, as if its pull had been stamped
    extracted_at (every file of its run), with that snapshot_date in its
    run.json (None: none, as run.json was written before 2026-10)."""
    for part in folder.glob("demographics.part*.json.gz"):
        _restamp(part, extracted_at=extracted_at)
    run = json.loads((folder / "run.json").read_text())
    run["extracted_at"] = extracted_at
    run.pop("snapshot_date", None)
    if snapshot_date is not None:
        run["snapshot_date"] = snapshot_date
    (folder / "run.json").write_text(json.dumps(run))
    for rel, key in (("dashboard-summary.json", "extracted_at"), ("sex_gender_parsed_meta.json", "source_extracted_at"),
                     ("sex_gender/methods.json", "source_extracted_at")):
        body = json.loads((folder / rel).read_text())
        body[key] = extracted_at
        (folder / rel).write_text(json.dumps(body))
