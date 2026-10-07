#!/usr/bin/env python3
"""Rebuild old monthly archives from the site's own git history: a one-off job.

Owner decisions 21a and 22a (2026-10-05). From 2026-10 on, every snapshot the
retention policy slims into its month's aggregate gets its own
archive_records.json.gz first (scripts/prune_snapshots.py, src/archive_records.py).
The aggregates slimmed before that have none, and two months lost their
snapshot altogether to the retention rule that #22 replaced. Their files are
still in the site repository's history, which is never rewritten, so this
job rebuilds what the policy would have kept, from each week's own files at
the site commit that published them (TARGETS):

  2026-02-22, 2026-04-26   aggregates already on the site (a summary only):
                           their archive_records.json.gz, built from the parts
                           at that commit, for exactly their summary's
                           recentStudies;
  2026-07-26, 2026-09-27   deleted by the old rule: restored as their months'
                           aggregates, exactly the files prune_snapshots.py
                           keeps in one (AGGREGATE_KEEP): the summary, copied
                           byte for byte from that commit, and the archive
                           file built from that commit's parts.

and lists them in history.json in the form prune_snapshots.py writes: the
restored dates added to "dates" (sorted), and each date's "archives" entry
{"kind": "aggregate", "detail": "archive_records.json.gz"}.

The archive file is archive_records.build's, the same projection and the
same refusal as a slim: a contract field a record has that the projection
would drop stops the job. A week whose parts carry no pipeline_commit (every
run before 2026-09) gets "source_pipeline_commit": null.

READS   <site>/history.json, <site>/snapshots/, <site>/tests/record_contract.json
        and, through git, snapshots/<date>/ at each target's commit (the site
        checkout needs that commit and the history leading to it: a full or
        blobless clone, not a shallow one)
WRITES  with --write only, and only these, never deleting anything:
          <site>/snapshots/<date>/archive_records.json.gz   (each target)
          <site>/snapshots/<date>/dashboard-summary.json    (each restored date)
          <site>/history.json
        --out DIR: the files built (at their site paths), history.json as it
        would be, and plan.json (the report); --report: the report alone
INVOKED by .github/workflows/backfill-archives.yml (workflow_dispatch; dry_run
        true by default, which uploads --out and pushes nothing), as
          python3 scripts/backfill_archives.py --site site --write --out ... --report ...
        By hand, without --write, it changes nothing in the site.
EXITS   0  the plan is sound (and, with --write, carried out and the site
           passes scripts/check_site_contract.py: the commit may be made);
        1  refused, before anything is written, when: history.json is not in
           the form prune_snapshots.py writes (dates, latest and archives:
           the first weekly run with it writes that); a commit is not in the
           site's history or does not hold the date's folder; its parts are
           not one complete run, or its summary (the commit's for a restored
           date, the site's for an existing aggregate) is not their run or not
           that date's; the projection would lose a field; a date's folder on
           the site holds anything but an aggregate's files, or a file this
           job would write that is not byte-identical and does not check;
           or the retention policy would not keep a restored date as its
           month's aggregate (or would change what else it keeps);
        1  with --write, when the written site fails check_site_contract.py:
           the files stay in the working tree, uncommitted.

A second run finds every file byte-identical and history.json unchanged, and
writes nothing; a run that stopped part way is completed. An archive file
that is already there and checks against its summary but differs from what
this run builds (the contract changed since) is left as it is, and said.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import check_site_contract as csc  # noqa: E402
import prune_snapshots as ps  # noqa: E402
from src import archive_records  # noqa: E402
from src import dataset_folder as df  # noqa: E402
from src import site_layout as sl  # noqa: E402

HISTORY_KEYS = ("dates", "latest", "archives")
# What a restored aggregate copies from its commit: everything an aggregate
# keeps but the archive file, which is built.
COPIED = tuple(sorted(ps.AGGREGATE_KEEP - {sl.ARCHIVE_FILE}))


@dataclass(frozen=True)
class Target:
    date: str
    commit: str            # the site commit whose snapshots/<date>/ holds that week's parts
    restore: bool          # True: the folder is gone, restore it; False: the aggregate is there

    @property
    def folder(self) -> str:
        return f"{ps.SNAPSHOT_DIR}/{self.date}"


# Checked against the site on 2026-10-06 (each commit's folder, its summary's
# and its eight parts' stamps): see the pull request.
TARGETS = (
    # The parts were committed by "Backfill historical snapshot directories"
    # (no summary there; the site's summary was added on 2026-07-03 from them).
    Target("2026-02-22", "cb50c082ac9bf337f965aae6ae09f51b9ba1be43", restore=False),
    Target("2026-04-26", "be0285566775308901aa9bb834695dc238eabbcc", restore=False),
    Target("2026-07-26", "433eee466ae048d8b35731acdfd3bca5684f218d", restore=True),
    Target("2026-09-27", "4a1df0a8042e42f93bfd84ce0375c4891e0d78d9", restore=True),
)


class Refused(Exception):
    """The job stops before writing anything."""


# ── the site's history, through git ─────────────────────────────────────────

def _git(site: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", site, *args], capture_output=True, text=True, check=False)


def commit_files(site: str, t: Target) -> list[str]:
    """The files of snapshots/<date>/ at the target's commit, relative to the
    folder. Refused when the commit is not in the checkout, not in the history
    of its HEAD, or does not hold the folder's core part 1 (and, for a
    restored date, its summary)."""
    r = _git(site, "cat-file", "-t", t.commit)
    if r.returncode or r.stdout.strip() != "commit":
        raise Refused(f"{t.date}: the site checkout has no commit {t.commit} (it needs the site's history: "
                      "a full or blobless clone, not a shallow one)")
    r = _git(site, "merge-base", "--is-ancestor", t.commit, "HEAD")
    if r.returncode == 1:
        raise Refused(f"{t.date}: commit {t.commit} is not in the history of the site's HEAD")
    if r.returncode:
        raise Refused(f"{t.date}: cannot tell whether commit {t.commit} is in the site's history "
                      f"({r.stderr.strip()}); a shallow clone cannot")
    r = _git(site, "ls-tree", "-r", "-z", "--name-only", t.commit, "--", t.folder + "/")
    if r.returncode:
        raise Refused(f"{t.date}: cannot list {t.folder}/ at {t.commit}: {r.stderr.strip()}")
    names = sorted(n[len(t.folder) + 1:] for n in r.stdout.split("\0") if n)
    need = [sl.core_part_name(1)] + (list(COPIED) if t.restore else [])
    absent = [n for n in need if n not in names]
    if absent:
        held = f"holds only {', '.join(names[:5])}" if names else "does not hold that folder"
        raise Refused(f"{t.date}: site commit {t.commit} {held}; it has no {', '.join(absent)} there")
    return names


def is_dataset_file(name: str) -> bool:
    """A file inspect reads as part of a dataset (dataset_folder.present_files)."""
    return bool(sl.CORE_PART_RE.fullmatch(name) or sl.STUDIES_TAB_PART_RE.fullmatch(name)
                or name == sl.RUN_FILE or name.startswith(sl.DETAIL_DIR + "/"))


def blob(site: str, t: Target, name: str, dst: str) -> None:
    """snapshots/<date>/<name> at the target's commit, into dst (a blobless
    clone fetches it here)."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as fh:
        r = subprocess.run(["git", "-C", site, "cat-file", "blob", f"{t.commit}:{t.folder}/{name}"],
                           stdout=fh, stderr=subprocess.PIPE, check=False)
    if r.returncode:
        raise Refused(f"{t.date}: cannot read {t.folder}/{name} at {t.commit}: "
                      f"{r.stderr.decode(errors='replace').strip()}")


# ── one target ──────────────────────────────────────────────────────────────

@dataclass
class Built:
    target: Target
    stamps: tuple[Any, Any]
    parts: dict[str, int]                 # the dataset files at the commit, with their sizes
    summary: dict[str, Any]
    summary_bytes: bytes                  # the summary the archive file is checked against
    doc: dict[str, Any]
    archive: bytes
    actions: dict[str, str] = field(default_factory=dict)   # site path -> write / unchanged / kept

    def report(self) -> dict[str, Any]:
        ids = archive_records.summary_ids(self.summary)
        return {"date": self.target.date, "commit": self.target.commit,
                "kind": "restored aggregate" if self.target.restore else "archive file for an aggregate",
                "source_extracted_at": self.stamps[0], "source_pipeline_commit": self.stamps[1],
                "parts": self.parts, "parts_bytes": sum(self.parts.values()),
                "archive_bytes": len(self.archive), "archive_records": len(self.doc["data"]),
                "recent_studies": len(ids), "covered": sum(1 for nct in ids if nct in self.doc["data"]),
                "actions": self.actions}


def date_problem(summary: dict[str, Any], run: Any, d: str) -> str | None:
    """Why the run is not that date's (None when it is), as prune_snapshots.py
    decides it for the outgoing week: a snapshot_date (run.json's, else the
    summary's) must be the date exactly; without one, the pull's UTC day must
    be the date or the next."""
    for source, said in ((sl.RUN_FILE, run.get("snapshot_date") if isinstance(run, dict) else None),
                         ("summary", summary.get("snapshot_date"))):
        if isinstance(said, str) and df.DATE_TEXT.fullmatch(said):
            return None if said == d else f"its {source} says it is the {said} run, not {d}'s"
    day = ps.run_day(summary.get("extracted_at"))
    if day is None or day not in (date.fromisoformat(d), date.fromisoformat(d) + timedelta(days=1)):
        return f"its summary is the run of {summary.get('extracted_at')!r}, not {d}'s"
    return None


def build(site: str, t: Target, contract: Any, work: str) -> Built:
    """The target's archive file, built from its commit's parts in work/.
    Refused when the commit's folder is not one complete run of that date
    whose summary is the one the archive file answers to."""
    names = commit_files(site, t)
    folder = os.path.join(work, t.date)
    parts: dict[str, int] = {}
    for name in names:
        if is_dataset_file(name):
            blob(site, t, name, os.path.join(folder, name))
            parts[name] = os.path.getsize(os.path.join(folder, name))
    summary_path = os.path.join(folder, df.SUMMARY_FILE)
    if t.restore:
        blob(site, t, df.SUMMARY_FILE, summary_path)
    else:
        # The aggregate's summary is the site's: the archive file answers to it.
        shutil.copyfile(os.path.join(site, t.folder, df.SUMMARY_FILE), summary_path)
    print(f"  {t.date}: {len(parts)} dataset files at {t.commit[:10]}, {sum(parts.values()) / 1e6:.1f} MB; "
          "checking they are one run", flush=True)
    ds = df.inspect(folder)
    if not ds.complete or ds.stamps is None or ds.summary is None:
        raise Refused(f"{t.date}: {t.folder}/ at {t.commit} with {'its' if t.restore else 'the site'}'s summary "
                      f"is not one complete run: {'; '.join(ds.problems)}")
    run = df.read_json(os.path.join(folder, sl.RUN_FILE)) if sl.RUN_FILE in ds.files else None
    why = date_problem(ds.summary, run, t.date)
    if why:
        raise Refused(f"{t.date}: {t.folder}/ at {t.commit} is not that date's run: {why}")
    print(f"  {t.date}: run {ds.stamps[0]} (pipeline_commit {ds.stamps[1]}); building its archive file", flush=True)
    try:
        doc = archive_records.build(ds, contract)
    except archive_records.ArchiveError as e:
        raise Refused(f"{t.date}: its archive file cannot be built: {e}") from e
    data = archive_records.encode(doc)
    check = os.path.join(folder, sl.ARCHIVE_FILE)
    with open(check, "wb") as fh:
        fh.write(data)
    found = archive_records.problems(check, ds.summary)
    if found:
        raise Refused(f"{t.date}: the archive file built does not check: {'; '.join(found)}")
    with open(summary_path, "rb") as fh:
        summary_bytes = fh.read()
    return Built(t, ds.stamps, parts, ds.summary, summary_bytes, doc, data)


# ── the site as it is ───────────────────────────────────────────────────────

def read_history(site: str) -> dict[str, Any]:
    """history.json, which must be in the form prune_snapshots.py writes."""
    try:
        history = ps.read_history(os.path.join(site, ps.HISTORY_FILE))
    except ps.Refused as e:
        raise Refused(str(e)) from e
    if set(history) != set(HISTORY_KEYS) or not ps.is_date(history.get("latest")) \
            or not isinstance(history.get("archives"), dict) \
            or not all(ps.is_date(d) for d in history["dates"]):
        raise Refused(f"history.json carries {sorted(history)}, not the {list(HISTORY_KEYS)} that "
                      "scripts/prune_snapshots.py writes: the first weekly run with it writes that form, "
                      "so run this after it")
    return history


def existing_archive(path: str, built: Built) -> str:
    """write, unchanged or kept, for an archive file at path. Refused when one
    is there that is not this run's bytes and does not check."""
    if not os.path.exists(path):
        return "write"
    with open(path, "rb") as fh:
        if fh.read() == built.archive:
            return "unchanged"
    found = archive_records.problems(path, built.summary)
    if found:
        raise Refused(f"{built.target.date}: {built.target.folder}/{sl.ARCHIVE_FILE} is there and does not "
                      f"check ({'; '.join(found)}); it is left for the owner")
    return "kept"


def check_site_folder(site: str, t: Target, history: dict[str, Any]) -> None:
    """The site's snapshots/<date>/ can take what this job writes: an existing
    aggregate's holds an aggregate's files only, is listed, and is named an
    aggregate; a restored date's is absent or holds an aggregate's files only.
    Refused otherwise (so a complete snapshot, or anything unexpected, is never
    touched)."""
    path = os.path.join(site, t.folder)
    if t.date >= history["latest"]:
        raise Refused(f"{t.date} is not older than the latest date, {history['latest']}")
    if not os.path.isdir(path):
        if t.restore:
            return
        raise Refused(f"{t.date}: {t.folder}/ is not on the site; it is an aggregate this job adds a file to")
    held = sorted(os.listdir(path))
    stray = [n for n in held if n not in ps.AGGREGATE_KEEP or not os.path.isfile(os.path.join(path, n))]
    if stray or df.present_files(path):
        raise Refused(f"{t.date}: {t.folder}/ holds {', '.join(stray[:5])}, not an aggregate's files only; "
                      "it is left as it is")
    if not t.restore:
        entry = history["archives"].get(t.date)
        if t.date not in history["dates"] or not isinstance(entry, dict) or entry.get("kind") != "aggregate" \
                or set(entry) - {"kind", "detail"}:
            raise Refused(f"{t.date}: history.json does not list {t.date} as an aggregate (archives: {entry!r})")
        if df.read_summary(path) is None:
            raise Refused(f"{t.date}: {t.folder}/{df.SUMMARY_FILE} cannot be read")


def plan_site(site: str, built: Built) -> None:
    """What the job does in the target's folder: built.actions."""
    t = built.target
    path = os.path.join(site, t.folder)
    if t.restore:
        for name in COPIED:
            target = os.path.join(path, name)
            if not os.path.exists(target):
                built.actions[f"{t.folder}/{name}"] = "write"
                continue
            with open(target, "rb") as fh:
                same = fh.read() == built.summary_bytes if name == df.SUMMARY_FILE else False
            if not same:
                raise Refused(f"{t.date}: {t.folder}/{name} is there and is not the one at {t.commit}; "
                              "it is left for the owner")
            built.actions[f"{t.folder}/{name}"] = "unchanged"
    built.actions[f"{t.folder}/{sl.ARCHIVE_FILE}"] = existing_archive(os.path.join(path, sl.ARCHIVE_FILE), built)


def retention(site: str, history: dict[str, Any], targets: list[Target]) -> dict[str, Any]:
    """The policy (prune_snapshots.plan_retention) over the site's folders with
    the restored dates added, at the latest date and at the next weekly run:
    every target must be kept as its month's aggregate, and nothing else the
    policy keeps may change. Refused otherwise."""
    folders, _ = ps.scan(os.path.join(site, ps.SNAPSHOT_DIR))
    complete = [d for d, f in folders.items() if f.kind == ps.COMPLETE]
    summarised = [d for d, f in folders.items() if f.kind != ps.UNUSABLE]
    added = [t.date for t in targets if t.date not in summarised]
    latest = history["latest"]
    after = str(date.fromisoformat(latest) + timedelta(days=7))
    out: dict[str, Any] = {}
    for when, comp in ((latest, complete), (after, complete + [latest])):
        summ = summarised + ([latest] if when == after else [])
        before = ps.plan_retention(comp, summ, when)
        with_them = ps.plan_retention(comp, summ + added, when)
        out[when] = {"complete": with_them.complete, "aggregates": with_them.aggregates}
        lost = [t.date for t in targets if t.date not in with_them.aggregates]
        if lost:
            raise Refused(f"the retention policy run on {when} would not keep {', '.join(lost)} as its month's "
                          f"aggregate (it keeps {with_them.complete} complete and {with_them.aggregates}); "
                          "nothing is restored that the next weekly run would delete")
        if with_them.complete != before.complete or sorted(set(with_them.aggregates) - set(added)) != before.aggregates:
            raise Refused(f"restoring {', '.join(added)} would change what the retention policy keeps on {when}: "
                          f"{before} becomes {with_them}")
    return out


def history_doc(history: dict[str, Any], targets: list[Target]) -> dict[str, Any]:
    """history.json with the targets listed, in prune_snapshots.py's form:
    dates sorted, the latest, and archives keyed in date order."""
    archives = dict(history["archives"])
    for t in targets:
        archives[t.date] = {"kind": "aggregate", "detail": sl.ARCHIVE_FILE}
    return {"dates": sorted(set(history["dates"]) | {t.date for t in targets}),
            "latest": history["latest"],
            "archives": {d: archives[d] for d in sorted(archives)}}


def history_bytes(doc: dict[str, Any]) -> bytes:
    """The bytes prune_snapshots.write_history writes for doc."""
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


# ── writing ─────────────────────────────────────────────────────────────────

def apply(site: str, built: Built) -> None:
    """Carry out built.actions. A restored folder is written beside the
    snapshots and moved in whole, as prune_snapshots.py archives a week."""
    t = built.target
    path = os.path.join(site, t.folder)
    if not any(a == "write" for a in built.actions.values()):
        return
    if t.restore and not os.path.exists(path):
        tmp = os.path.join(site, ps.SNAPSHOT_DIR, f".{t.date}.partial")
        try:
            shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(tmp)
            with open(os.path.join(tmp, df.SUMMARY_FILE), "wb") as fh:
                fh.write(built.summary_bytes)
            archive_records.write(built.doc, os.path.join(tmp, sl.ARCHIVE_FILE), built.summary)
            os.replace(tmp, path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return
    if built.actions.get(f"{t.folder}/{df.SUMMARY_FILE}") == "write":
        part = os.path.join(path, df.SUMMARY_FILE + ".partial")
        with open(part, "wb") as fh:
            fh.write(built.summary_bytes)
        os.replace(part, os.path.join(path, df.SUMMARY_FILE))
    if built.actions[f"{t.folder}/{sl.ARCHIVE_FILE}"] == "write":
        archive_records.write(built.doc, os.path.join(path, sl.ARCHIVE_FILE), built.summary)


def save_out(out: str, builds: list[Built], doc: dict[str, Any], report: dict[str, Any]) -> None:
    """The files built, at their site paths, history.json as it would be, and plan.json."""
    for b in builds:
        folder = os.path.join(out, b.target.folder)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, sl.ARCHIVE_FILE), "wb") as fh:
            fh.write(b.archive)
        if b.target.restore:
            with open(os.path.join(folder, df.SUMMARY_FILE), "wb") as fh:
                fh.write(b.summary_bytes)
    with open(os.path.join(out, ps.HISTORY_FILE), "wb") as fh:
        fh.write(history_bytes(doc))
    with open(os.path.join(out, "plan.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)


# ── a run ───────────────────────────────────────────────────────────────────

def run(site: str, targets: list[Target], write: bool, out: str | None = None,
        contract_path: str | None = None) -> dict[str, Any]:
    """Plan (and with write, carry out) the backfill. Returns the report;
    report["ok"] False when the written site fails the gate. Raises Refused
    before writing anything when the plan is not sound."""
    history = read_history(site)
    for t in targets:
        check_site_folder(site, t, history)
    kept = retention(site, history, targets)
    contract_file = contract_path or os.path.join(site, ps.CONTRACT)
    try:
        with open(contract_file, encoding="utf-8") as fh:
            contract = json.load(fh)
    except (OSError, ValueError) as e:
        raise Refused(f"the site's record contract cannot be read ({contract_file}: {e})") from e
    builds: list[Built] = []
    with tempfile.TemporaryDirectory(prefix="backfill-") as work:
        for t in targets:
            print(f"{t.date} from site commit {t.commit[:10]}:", flush=True)
            b = build(site, t, contract, work)
            shutil.rmtree(os.path.join(work, t.date), ignore_errors=True)
            plan_site(site, b)
            builds.append(b)
    doc = history_doc(history, targets)
    history_path = os.path.join(site, ps.HISTORY_FILE)
    with open(history_path, "rb") as fh:
        history_changed = fh.read() != history_bytes(doc)
    changed = sorted(p for b in builds for p, a in b.actions.items() if a == "write")
    if history_changed:
        changed.append(ps.HISTORY_FILE)
    report: dict[str, Any] = {"write": write, "latest": history["latest"], "targets": [b.report() for b in builds],
                              "history": {"dates_added": sorted(set(doc["dates"]) - set(history["dates"])),
                                          "archives": {t.date: doc["archives"][t.date] for t in targets},
                                          "changed": history_changed},
                              "retention": kept, "changed": changed, "gate": None, "ok": True}
    if write:
        for b in builds:
            apply(site, b)
        if history_changed:
            ps.write_history(history_path, doc)
        gate = csc.check(site, latest=history["latest"])
        report["gate"] = {"ok": gate["ok"], "errors": gate["errors"], "warnings": gate["warnings"]}
        report["ok"] = bool(gate["ok"])
    if out:
        save_out(out, builds, doc, report)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rebuild old monthly archives from the site's git history (one-off).")
    ap.add_argument("--site", required=True, help="a site checkout with its history (full or blobless clone)")
    ap.add_argument("--write", action="store_true",
                    help="write the files and history.json into the site, then run the publish gate on it")
    ap.add_argument("--out", default=None, help="put the files built, history.json and plan.json here")
    ap.add_argument("--report", default=None, help="write the report here as JSON")
    ap.add_argument("--contract", default=None,
                    help="the site's record contract (default: <site>/tests/record_contract.json)")
    a = ap.parse_args(argv)
    try:
        report = run(a.site, list(TARGETS), a.write, a.out, a.contract)
    except Refused as e:
        print(f"::error::archive backfill refused, nothing written: {e}")
        return 1
    if a.report:
        with open(a.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    print(f"Archive backfill ({'written' if a.write else 'plan only, nothing written'}), "
          f"latest {report['latest']}:")
    for t in report["targets"]:
        acts = ", ".join(f"{os.path.basename(p)} {a}" for p, a in t["actions"].items())
        print(f"  {t['date']} {t['kind']} from {t['commit'][:10]}: {t['archive_records']} records, "
              f"covers {t['covered']} of {t['recent_studies']} recentStudies, {t['archive_bytes']:,} bytes "
              f"(source_pipeline_commit {t['source_pipeline_commit']}); {acts}")
        if any(a == "kept" for a in t["actions"].values()):
            print(f"::warning::{t['date']}: the archive file there checks but differs from what this run builds "
                  "(the contract changed since?); it is left as it is")
    print(f"  history.json: {'adds ' + ', '.join(report['history']['dates_added']) if report['history']['dates_added'] else 'no dates added'}"
          f"; {'changes' if report['history']['changed'] else 'unchanged'}")
    for when, plan in report["retention"].items():
        print(f"  retention on {when}: complete {plan['complete']}, aggregates {plan['aggregates']}")
    print(f"  changed: {', '.join(report['changed']) or 'nothing'}")
    gate = report["gate"]
    if gate is not None:
        for w in gate["warnings"]:
            print(f"::warning::{w}")
        for e in gate["errors"]:
            print(f"::error::{e}")
        print(f"  site contract check: {'ok' if gate['ok'] else 'FAILED (nothing may be committed)'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
