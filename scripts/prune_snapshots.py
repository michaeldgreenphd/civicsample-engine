#!/usr/bin/env python3
"""Archive last week's dataset, then prune the site's snapshots to the retention policy.

READS   <site>/history.json      "dates" (the dashboard's "View snapshot" menu),
                                 "latest" (the date data/ serves; a file written
                                 before that key existed has only dates, and its
                                 newest is the latest) and "archives"
        <site>/data/             last week's dataset, before this run's replaces it
        <site>/snapshots/<date>/ every archived week
        <site>/tests/record_contract.json (or --contract): the fields an
                                 aggregate's archive_records.json.gz keeps
WRITES  <site>/snapshots/<last week's date>/   last week's dataset, copied out of
                                 data/ when the policy keeps it
        <site>/snapshots/<date>/archive_records.json.gz   before a complete
                                 snapshot is slimmed into its month's aggregate
        and deletes what the policy does not keep;
        <site>/history.json      rewritten whole, atomically: dates, latest, archives
        --report                 JSON: what it archived, kept, slimmed and removed
INVOKED by .github/workflows/extract.yml in the publish step as
        python3 scripts/prune_snapshots.py --site site --latest "$DATE" --report ...
        BEFORE this week's files replace data/, so last week's are still there
        to archive. By hand: --dry-run changes nothing and says what it would do.
EXITS   1, having changed nothing, when history.json cannot be read, --latest
        is not a date, or a snapshot or a listed date is newer than the latest;
        1, with an ::error:: line and history.json not rewritten, when a copy
        fails part way or differs from its source (the partial copy is
        removed, and nothing else has been changed yet) or a file cannot be
        deleted. A week it
        cannot archive, or a snapshot it cannot slim (a file missing, another
        run's stamps, a study its summary lists missing from its records), is
        reported, warned and left as it is: nothing is deleted on a doubt.

The policy (owner decisions 19b, 20a and 21a, 2026-10-05):

  The latest week lives in data/ only. The publish step no longer copies it
  into snapshots/<date>/ as well (that copy was 157 MB, byte for byte data/'s);
  history.json's "latest" says which date data/ serves, and the site serves
  that date from data/. When the next week is published, this script copies
  the outgoing week out of data/ into snapshots/<its date>/, if the policy
  keeps it, before anything replaces data/: every file of its dataset as its
  layout names them (src/dataset_folder.py: the whole-record parts, or the
  core parts, studies_tab parts and detail shards, and run.json), its
  dashboard-summary.json, and the sex/gender table, meta and methods text when
  they are that run's. Every file's stamps are checked first, and so is the
  run's date: run.json's snapshot_date (written from the job's date since
  2026-10) must be the outgoing date exactly; without one, the pull's UTC day
  must be that date or the next. Every copy is compared with its source after.

  Three complete snapshots, about two weeks apart. Time is cut into
  fortnights counted from EPOCH, a fixed Sunday. A fortnight's representative
  is its earliest week with a complete snapshot; the representatives of the
  three most recent fortnights that have one are kept complete. A week that
  is a fortnight's earliest stays its representative whatever is published
  after it, so it is kept from the day it leaves data/ until three newer
  fortnights have theirs (four to six weeks): the set moves by at most one
  snapshot a week, and no week that will qualify is deleted before it can be
  kept. (Until 2026-10 the spacing was measured from the newest date, which
  moves every week, so each run deleted last week's snapshot, the one that
  would have qualified a week later: 14 weekly snapshots went that way between
  July and October 2026.) A visitor can open four complete datasets: the
  latest, from data/, and these three, which reach back five to six weeks.

  One monthly aggregate for every older month. A month none of whose weeks is
  kept complete, nor is the latest, keeps its newest snapshot as an aggregate:
  dashboard-summary.json, from which the dashboard draws every chart, and
  archive_records.json.gz, its 500 recent studies' own records for their
  pop-ups (src/archive_records.py). The archive file is written from the
  folder's own records, and checked, before anything is deleted from it; a
  folder whose file cannot be written stays complete, and is warned about.
  Then everything but those two files goes. After three or more Sundays that
  publish nothing (the registry down, or the gate blocking each week), the
  outgoing week can be the only week of such a month: it is archived out of
  data/ like a kept week, and slimmed into its month's aggregate in the same
  run, so no month is left without a snapshot.

  The full files of every week stay in the site repository's git history,
  which is never rewritten (and from 2026-08-28 on, each week's full records
  are on its data-<date> release too). To get a week's files back, in a full
  clone of the site repository (a shallow one lacks the older commits):
    any week the site served, archived or not (a fortnight's second week never
    is: it lived in data/ only), as its weekly run published it into data/,
    into a folder <dir>:
      c=$(git log -1 --format=%H --grep='^Update demographics data <date>')
      mkdir -p <dir> && git archive "$c" data | tar -x -C <dir> --strip-components=1
    a snapshot folder that was slimmed or deleted, back in place:
      c=$(git log -1 --format=%H --diff-filter=D -- snapshots/<date>/demographics.part1.json.gz)
      git restore --source="$c^" -- snapshots/<date>/
  Either way a split week's studies_tab parts, detail shards and run.json
  come back with it. (The second alone finds no commit for a week that never
  had a folder.)

  Everything else is deleted, and history.json is rewritten from what is on
  disk afterwards: "dates", a list of date strings as before (the latest and
  every snapshot left), so it never offers a date whose folder is gone;
  "latest"; and "archives", every aggregate's entry, {"kind": "aggregate"},
  with "detail": "archive_records.json.gz" when its archive file checks. A
  snapshots/<latest>/ folder is a copy of what data/ serves, and goes.

EPOCH and COMPLETE_SNAPSHOTS are the policy: a change to either re-anchors or
resizes the window, and deletes snapshots the old values kept.
"""
from __future__ import annotations

import argparse
import filecmp
import json
import os
import re
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import archive_records  # noqa: E402
from src import dataset_folder as df  # noqa: E402
from src import site_layout as sl  # noqa: E402

SNAPSHOT_DIR = "snapshots"
HISTORY_FILE = "history.json"
DATA_DIR = "data"
CONTRACT = os.path.join("tests", "record_contract.json")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A Sunday. Never change it: every fortnight, and so every representative, is
# counted from it.
EPOCH = date(2026, 1, 4)
COMPLETE_SNAPSHOTS = 3
# What a monthly aggregate keeps.
AGGREGATE_KEEP = frozenset({df.SUMMARY_FILE, sl.ARCHIVE_FILE})
# What rides with an archived dataset besides its own files, when it is that
# run's: the site reads them for every snapshot (?sg=v2 and the methods page).
SEX_GENDER_PAIR = (df.SG_TABLE, df.SG_META)

COMPLETE, AGGREGATE, UNUSABLE = "complete", "aggregate", "unusable"


class Refused(Exception):
    """The run stops before changing anything."""


def is_date(value: Any) -> bool:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def fortnight(d: str) -> int:
    """The fortnight a date falls in, counted from EPOCH."""
    return (date.fromisoformat(d) - EPOCH).days // 14


# ── the policy ──────────────────────────────────────────────────────────────

@dataclass
class Plan:
    complete: list[str]           # snapshots kept complete
    aggregates: list[str]         # snapshots kept as their month's aggregate


def plan_retention(complete: Iterable[str], summarised: Iterable[str], latest: str) -> Plan:
    """Which snapshots the policy keeps. complete: the dates whose folder holds
    one complete run; summarised: the dates whose folder holds a summary (the
    complete ones and the aggregates); latest: the date data/ serves. Nothing
    at or after the latest is kept: data/ serves it. Pure; empty input gives an
    empty plan."""
    earliest: dict[int, str] = {}
    for d in sorted(set(complete)):
        if d < latest:
            earliest.setdefault(fortnight(d), d)
    keep = sorted(earliest[f] for f in sorted(earliest, reverse=True)[:COMPLETE_SNAPSHOTS])
    covered = {d[:7] for d in keep} | {latest[:7]}
    monthly: dict[str, str] = {}
    for d in sorted(set(summarised)):
        if d < latest and d not in keep and d[:7] not in covered:
            monthly[d[:7]] = d        # in date order, so the month's newest stays
    return Plan(keep, sorted(monthly.values()))


# ── what is on disk ─────────────────────────────────────────────────────────

@dataclass
class Folder:
    date: str
    kind: str                                  # complete, aggregate or unusable
    dataset: df.Dataset | None = None          # what it holds, when it holds dataset files
    why: str = ""                              # why it is unusable
    pending: list[str] | None = None           # dry run: the files an archive would copy in


def classify(d: str, path: str) -> Folder:
    """A snapshot folder: complete (one whole run, with its summary, and its
    sex/gender files and methods text that run's when it has them); aggregate
    (a summary and no dataset files, or a summary and an archive file that
    checks beside what a slim cut short left); or unusable (anything else,
    such as a damaged dataset, or dataset files without core part 1: left as
    it is, so nothing that might be recovered is deleted)."""
    summary = df.read_summary(path)
    archive = os.path.join(path, sl.ARCHIVE_FILE)

    def slim_cut_short() -> bool:
        # The archive file is written (and checked) before anything is
        # deleted, so with it in place the folder is an aggregate whose strip
        # did not finish.
        return summary is not None and os.path.exists(archive) and not archive_records.problems(archive, summary)

    if os.path.exists(os.path.join(path, sl.core_part_name(1))):
        ds = df.inspect(path)
        problems = list(ds.problems)
        if ds.stamps is not None:
            problems += df.sex_gender_problems(path, ds.stamps) + df.methods_problems(path, ds.stamps)
        if ds.complete and not problems:
            return Folder(d, COMPLETE, ds)
        if slim_cut_short():
            return Folder(d, AGGREGATE)
        return Folder(d, UNUSABLE, ds, "; ".join(problems))
    if summary is None:
        return Folder(d, UNUSABLE, None, f"no {sl.core_part_name(1)} and no readable {df.SUMMARY_FILE}")
    held = df.present_files(path)
    if held and not slim_cut_short():
        # Part 1 gone and the rest there (a partial restore or a clean-up by
        # hand) is a damaged dataset, not an aggregate: stripping it would
        # delete files no archive file stands in for.
        return Folder(d, UNUSABLE, None,
                      f"it holds {len(held):,} dataset files ({', '.join(held[:3])}{' …' if len(held) > 3 else ''}) "
                      f"without {sl.core_part_name(1)}, and no {sl.ARCHIVE_FILE} that checks")
    return Folder(d, AGGREGATE)


def scan(snapshot_dir: str) -> tuple[dict[str, Folder], list[str]]:
    """Every dated folder under snapshots/, classified; and the other names there."""
    folders: dict[str, Folder] = {}
    others: list[str] = []
    if not os.path.isdir(snapshot_dir):
        return folders, others
    for name in sorted(os.listdir(snapshot_dir)):
        path = os.path.join(snapshot_dir, name)
        if is_date(name) and os.path.isdir(path) and not os.path.islink(path):
            folders[name] = classify(name, path)
        else:
            others.append(name)
    return folders, others


def read_history(path: str) -> dict[str, Any]:
    """history.json as written, or an empty one when there is none. Refused when
    it cannot be read or its latest is not a date."""
    if not os.path.exists(path):
        return {"dates": []}
    try:
        with open(path, encoding="utf-8") as fh:
            history = json.load(fh)
    except (OSError, ValueError) as e:
        raise Refused(f"{path} cannot be read ({e}); nothing was changed")
    if not isinstance(history, dict) or not isinstance(history.get("dates", []), list):
        raise Refused(f"{path} is not a history file (an object with a list of dates); nothing was changed")
    if "latest" in history and not is_date(history["latest"]):
        raise Refused(f"{path} says latest {history['latest']!r}, which is not a date; nothing was changed")
    return history


def run_day(extracted_at: Any) -> date | None:
    """The UTC day a run's extracted_at falls on (a stamp without a zone is UTC)."""
    try:
        moment = datetime.fromisoformat(str(extracted_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.date()


# ── a run ───────────────────────────────────────────────────────────────────

@dataclass
class Outcome:
    latest: str
    previous: str | None
    dry_run: bool
    archived: dict[str, Any] = field(default_factory=dict)
    slimmed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    stripped: dict[str, list[str]] = field(default_factory=dict)
    complete: list[str] = field(default_factory=list)
    aggregates: list[str] = field(default_factory=list)
    archive_files: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        print(f"::warning::{message}")

    def report(self) -> dict[str, Any]:
        return {"latest": self.latest, "previous_latest": self.previous, "dry_run": self.dry_run,
                "archived": self.archived, "complete": self.complete, "aggregates": self.aggregates,
                "archive_files": self.archive_files, "slimmed": self.slimmed, "removed": self.removed,
                "stripped": self.stripped, "dropped_from_history": self.dropped, "warnings": self.warnings}


def _copy_names(ds: df.Dataset, data_dir: str, prev: str, out: Outcome) -> list[str]:
    """The files of last week's dataset to archive: its dataset files and its
    summary, and the sex/gender pair and methods text when they are its run's."""
    assert ds.stamps is not None
    names = list(ds.files) + [df.SUMMARY_FILE]
    if any(os.path.exists(os.path.join(data_dir, n)) for n in SEX_GENDER_PAIR):
        problems = df.sex_gender_problems(data_dir, ds.stamps, prev)
        if problems:
            out.warn(f"the sex/gender table of {prev} is not archived with it: {'; '.join(problems)}")
        else:
            names += list(SEX_GENDER_PAIR)
    if os.path.exists(os.path.join(data_dir, df.SG_METHODS)):
        problems = df.methods_problems(data_dir, ds.stamps, prev)
        if problems:
            out.warn(f"the methods text of {prev} is not archived with it: {'; '.join(problems)}")
        else:
            names.append(df.SG_METHODS)
    return names


def archive_outgoing(site: str, prev: str, latest: str, folders: dict[str, Folder], out: Outcome) -> None:
    """Last week's dataset, still in data/, into snapshots/<prev>/, when the
    policy keeps it: complete, or as its month's aggregate (prune slims it in
    the same run). Records what happened in out.archived; adds the new folder
    to folders."""
    rec: dict[str, Any] = {"date": prev, "status": "", "reason": "", "files": 0, "bytes": 0}
    out.archived = rec
    snapshot_dir = os.path.join(site, SNAPSHOT_DIR)
    data_dir = os.path.join(site, DATA_DIR)
    if prev in folders:
        folder = folders[prev]
        if folder.kind != COMPLETE:
            rec.update(status="refused", reason=f"snapshots/{prev}/ is there but is not a complete snapshot "
                                                f"({folder.why or folder.kind}); it is left as it is")
            out.warn(f"{prev} was not archived: {rec['reason']}")
            return
        # The publish step before 2026-10 copied every week into its folder as
        # well as data/: such a week is archived already.
        rec["status"] = "already archived"
        here = df.inspect(data_dir)
        if here.complete and folder.dataset is not None and here.stamps != folder.dataset.stamps:
            out.warn(f"snapshots/{prev}/ holds the run of {folder.dataset.stamps}, data/ the run of "
                     f"{here.stamps}; the folder is kept as it is")
        return
    complete = [d for d, f in folders.items() if f.kind == COMPLETE]
    summarised = [d for d, f in folders.items() if f.kind != UNUSABLE]
    plan = plan_retention(complete + [prev], summarised + [prev], latest)
    if prev in plan.aggregates:
        # Only after Sundays that published nothing (three or more in a row):
        # the outgoing week is then the newest of a month no kept week and
        # not the latest covers, so nothing else would keep that month. It is
        # archived whole like any week, and prune slims it into the month's
        # aggregate in this run, its own records first.
        rec["reason"] = (f"kept as {prev[:7]}'s monthly aggregate: no week kept complete, and not the latest, "
                         "is in that month")
    elif prev not in plan.complete:
        rec["status"] = "not kept"
        firsts = [d for d in complete if fortnight(d) == fortnight(prev) and d < prev]
        rec["reason"] = (f"fortnight {fortnight(prev)} is represented by {min(firsts)}" if firsts
                         else "the three complete snapshots are newer")
        return
    ds = df.inspect(data_dir)
    if not ds.complete or ds.stamps is None:
        rec.update(status="refused", reason=f"data/ does not hold one complete run: {'; '.join(ds.problems)}")
        out.warn(f"{prev} was not archived: {rec['reason']}")
        return
    said = (df.read_json(os.path.join(data_dir, sl.RUN_FILE)) or {}) if sl.RUN_FILE in ds.files else {}
    named = said.get("snapshot_date") if isinstance(said, dict) else None
    if isinstance(named, str) and df.DATE_TEXT.fullmatch(named):
        # run.json names the date the run was published as (the weekly job's
        # date, since 2026-10): exactly prev's, whatever day the pull's
        # extracted_at fell on. inspect checked run.json is the parts' run.
        if named != prev:
            rec.update(status="refused", reason=f"data/run.json says it is the {named} run, not {prev}'s")
            out.warn(f"{prev} was not archived: {rec['reason']}")
            return
    else:
        # Published before run.json named its date: the pull's UTC day, or
        # the next (a run that starts just before midnight UTC).
        day = run_day(ds.stamps[0])
        if day is None or day not in (date.fromisoformat(prev), date.fromisoformat(prev) + timedelta(days=1)):
            rec.update(status="refused", reason=f"data/ holds the run of {ds.stamps[0]!r}, not {prev}'s")
            out.warn(f"{prev} was not archived: {rec['reason']}")
            return
    names = _copy_names(ds, data_dir, prev, out)
    rec["files"] = len(names)
    rec["bytes"] = sum(os.path.getsize(os.path.join(data_dir, n)) for n in names)
    if out.dry_run:
        rec["status"] = "would archive"
        folders[prev] = Folder(prev, COMPLETE, ds, pending=names)
        return
    # Copied beside the snapshots and moved in whole: a copy that stops part
    # way leaves no folder for the date.
    tmp = os.path.join(snapshot_dir, f".{prev}.partial")
    target = os.path.join(snapshot_dir, prev)
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        for name in names:
            dst = os.path.join(tmp, name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(os.path.join(data_dir, name), dst)
        differ = [n for n in names if not filecmp.cmp(os.path.join(data_dir, n), os.path.join(tmp, n), shallow=False)]
        if differ:
            raise OSError(f"the copies of {', '.join(differ[:3])} differ from data/'s")
        os.replace(tmp, target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    rec["status"] = "archived"
    folders[prev] = classify(prev, target)
    if folders[prev].kind != COMPLETE:
        out.warn(f"snapshots/{prev}/ was archived but does not read as complete: {folders[prev].why}")


def strip(path: str, dry: bool, pending: list[str] | None = None) -> list[str]:
    """Delete everything in an aggregate folder but AGGREGATE_KEEP. Returns the
    names. pending: in a dry run, the files a folder not yet archived would hold."""
    if dry and pending is not None and not os.path.isdir(path):
        return sorted({n.split("/")[0] for n in pending} - AGGREGATE_KEEP)
    names = sorted(n for n in os.listdir(path) if n not in AGGREGATE_KEEP)
    if not dry:
        for name in names:
            target = os.path.join(path, name)
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
    return names


def slim(path: str, folder: Folder, contract: Any, out: Outcome) -> bool:
    """A complete snapshot into its month's aggregate: its archive file first,
    written from its own records and checked, then the strip. False, with the
    folder untouched, when the archive file cannot be written."""
    assert folder.dataset is not None
    try:
        doc = archive_records.build(folder.dataset, contract)
        if not out.dry_run:
            archive_records.write(doc, os.path.join(path, sl.ARCHIVE_FILE), folder.dataset.summary)
    except archive_records.ArchiveError as e:
        out.warn(f"snapshots/{folder.date}/ stays complete: its archive file cannot be written ({e})")
        return False
    out.stripped[folder.date] = strip(path, out.dry_run, folder.pending)
    return True


def remove(path: str, dry: bool) -> None:
    if not dry:
        shutil.rmtree(path)


def prune(site: str, latest: str, contract: Any, out: Outcome, folders: dict[str, Folder]) -> dict[str, str]:
    """Apply the policy to the snapshot folders. Returns each kept date's kind."""
    snapshot_dir = os.path.join(site, SNAPSHOT_DIR)
    complete = [d for d, f in folders.items() if f.kind == COMPLETE]
    summarised = [d for d, f in folders.items() if f.kind != UNUSABLE]
    plan = plan_retention(complete, summarised, latest)
    kept: dict[str, str] = {}
    for d, folder in sorted(folders.items()):
        path = os.path.join(snapshot_dir, d)
        if d == latest:
            # data/ serves this date: a folder for it is a copy (or what is
            # left of an earlier run of the same day).
            out.removed.append(d)
            remove(path, out.dry_run)
        elif folder.kind == UNUSABLE:
            out.warn(f"snapshots/{d}/ is left as it is and not listed: {folder.why}")
        elif d in plan.complete:
            kept[d] = COMPLETE
        elif d in plan.aggregates:
            if folder.kind == COMPLETE:
                if slim(path, folder, contract, out):
                    out.slimmed.append(d)
                    kept[d] = AGGREGATE
                else:
                    kept[d] = COMPLETE
            else:
                stray = strip(path, out.dry_run)
                if stray:
                    out.stripped[d] = stray
                kept[d] = AGGREGATE
        else:
            out.removed.append(d)
            remove(path, out.dry_run)
    return kept


def history_doc(site: str, latest: str, kept: dict[str, str], out: Outcome) -> dict[str, Any]:
    """history.json from what is left: the latest and every kept folder that is
    there (dry run: would be), and each aggregate's entry."""
    snapshot_dir = os.path.join(site, SNAPSHOT_DIR)
    archives: dict[str, Any] = {}
    dates = [latest]
    for d, kind in sorted(kept.items()):
        path = os.path.join(snapshot_dir, d)
        if not out.dry_run and df.read_summary(path) is None:
            out.warn(f"snapshots/{d}/ has no readable {df.SUMMARY_FILE} after pruning; it is not listed")
            continue
        dates.append(d)
        if kind == COMPLETE:
            out.complete.append(d)
            continue
        out.aggregates.append(d)
        entry: dict[str, Any] = {"kind": "aggregate"}
        archive = os.path.join(path, sl.ARCHIVE_FILE)
        if os.path.exists(archive) or (out.dry_run and d in out.slimmed):
            found = [] if out.dry_run and d in out.slimmed else archive_records.problems(archive, df.read_summary(path))
            if found:
                out.warn(f"snapshots/{d}/{sl.ARCHIVE_FILE} is not named in history.json: {'; '.join(found)}")
            else:
                entry["detail"] = sl.ARCHIVE_FILE
                out.archive_files.append(d)
        archives[d] = entry
    return {"dates": sorted(dates), "latest": latest, "archives": archives}


def write_history(path: str, doc: dict[str, Any]) -> None:
    """Write history.json beside itself and move it into place."""
    tmp = path + ".partial"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def run(site: str, latest: str | None, contract_path: str | None, dry: bool) -> Outcome | None:
    """One retention run over a site checkout; None when there is nothing to
    retain (no --latest, and no history.json naming a date). Raises Refused
    before changing anything when the run cannot go ahead."""
    history_path = os.path.join(site, HISTORY_FILE)
    history = read_history(history_path)
    listed = [d for d in history.get("dates", []) if is_date(d)]
    previous = history.get("latest") or (max(listed) if listed else None)
    latest = latest or previous
    if latest is None:
        return None
    if not is_date(latest):
        raise Refused(f"--latest {latest!r} is not a date (YYYY-MM-DD)")
    folders, others = scan(os.path.join(site, SNAPSHOT_DIR))
    folderless = sorted(d for d in listed if d not in folders and d not in (previous, latest))
    newer = sorted({d for d in [*folders, *listed, previous] if d and d > latest})
    if newer:
        raise Refused(f"{', '.join(newer)} is newer than the latest, {latest}: a run's date went back? "
                      "Nothing was changed")
    contract: Any = None
    out = Outcome(latest=latest, previous=previous, dry_run=dry)
    junk = [n for n in history.get("dates", []) if not is_date(n)]
    if junk:
        out.warn(f"history.json lists {junk[:3]}, which are not dates; they are dropped")
    for name in others:
        if re.fullmatch(r"\.\d{4}-\d{2}-\d{2}\.partial", name):
            if not dry:
                shutil.rmtree(os.path.join(site, SNAPSHOT_DIR, name), ignore_errors=True)
        else:
            out.warn(f"snapshots/{name} is not a dated snapshot folder; it is left as it is")
    if previous is not None and previous != latest:
        archive_outgoing(site, previous, latest, folders, out)
    contract_file = contract_path or os.path.join(site, CONTRACT)
    try:
        with open(contract_file, encoding="utf-8") as fh:
            contract = json.load(fh)
    except (OSError, ValueError) as e:
        # Without the contract no archive file can be written, so nothing is
        # slimmed (slim refuses); the rest of the policy still applies.
        out.warn(f"the site's record contract cannot be read ({contract_file}: {e}); no snapshot is slimmed")
    kept = prune(site, latest, contract, out, folders)
    out.dropped = folderless
    doc = history_doc(site, latest, kept, out)
    if not dry:
        write_history(history_path, doc)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Archive last week's dataset and prune the site's snapshots.")
    ap.add_argument("--site", default=".", help="the site checkout (default: the current directory)")
    ap.add_argument("--latest", default=None, metavar="DATE",
                    help="the date data/ serves after this run (the weekly job passes its own date); "
                         "default: history.json's latest")
    ap.add_argument("--contract", default=None,
                    help="the site's record contract (default: <site>/tests/record_contract.json)")
    ap.add_argument("--report", default=None, help="write what was done here, as JSON")
    ap.add_argument("--dry-run", action="store_true", help="change nothing; say what would be done")
    a = ap.parse_args(argv)
    try:
        out = run(a.site, a.latest, a.contract, a.dry_run)
    except Refused as e:
        print(f"::error::snapshot retention: {e}")
        return 1
    except OSError as e:
        # A copy that failed or differs from its source (its partial copy
        # is removed), or a file that could not be deleted: the run stops
        # there, before history.json is written, and so does the publish step.
        print(f"::error::snapshot retention stopped: {e}")
        return 1
    if out is None:
        print("Snapshot retention: no --latest and no dated history.json, so there is nothing to retain")
        return 0
    if a.report:
        with open(a.report, "w", encoding="utf-8") as fh:
            json.dump(out.report(), fh, indent=2)
    rec = out.archived
    archived = f"{rec['date']}: {rec['status']}" + (f" ({rec['reason']})" if rec.get("reason") else "") if rec else "-"
    dash = lambda xs: ", ".join(xs) if xs else "-"  # noqa: E731
    print(f"Snapshot retention ({'dry run' if a.dry_run else 'applied'}), latest {out.latest} (served from data/):")
    print(f"  last week archived      {archived}")
    print(f"  complete     ({len(out.complete)}): {dash(out.complete)}")
    print(f"  aggregates   ({len(out.aggregates)}): {dash(out.aggregates)} (archive file: {dash(out.archive_files)})")
    print(f"  slimmed now  ({len(out.slimmed)}): {dash(out.slimmed)}")
    print(f"  removed now  ({len(out.removed)}): {dash(out.removed)}")
    if out.dropped:
        print(f"  no longer listed     {dash(out.dropped)} (history.json listed them; their folders were gone)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
