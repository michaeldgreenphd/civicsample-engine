#!/usr/bin/env python3
"""Check this week's site files against the site's own contract before the push.

READS   <site>/tests/record_contract.json   every study-record field the site
                                            reads, in classes, and its layout
                                            section (src/site_layout.py)
        <site>/tests/data_budget.json       the part count, the size budget and
                                            the optional per-class budgets
                                            ("classes")
        <site>/data/                        this week's dataset, as staged: the
                                            demographics.part*.json.gz and, for a
                                            split dataset, its studies_tab parts,
                                            detail shards and run.json
        <site>/history.json, <site>/snapshots/
                                            what the "View snapshot" menu offers:
                                            the latest date (data/), the complete
                                            snapshots and the monthly aggregates
                                            (scripts/prune_snapshots.py)
        the <site> working tree             its size, as GitHub Pages will publish it
WRITES  --report (JSON: what was checked, the sizes, every error and warning),
        and ::error:: / ::warning:: lines for the Actions log
EXITS   1 when the push must not happen:
          - the contract or the budget cannot be read, or the contract's
            layout section names a layout version this check does not know;
          - a part is missing, is not gzipped JSON, has a wrong header, or comes
            from another run than part 1 (extracted_at, pipeline_commit);
          - an nct_id appears twice;
          - a record lacks a path the contract lists (a key may hold null or
            an empty list, which is how a record says "none"; it may not be
            absent);
          - a file is over GitHub's 100 MiB per-file push limit, or the
            published tree is over GitHub Pages' 1 GB limit.
        Parts with no `layout` block are inline: they carry every class, and
        the rules above are the whole check. When core part 1 carries a layout
        (the split), the parts carry the core class, and it also stops the
        push when:
          - the site's contract does not turn that layout on (the handshake),
            the parts disagree about it, or it is not the layout the contract
            and the part count give (studies_tab files, detail shards, key);
          - a core record's nct_id is not NCT + 8 digits;
          - studies_tab.part1..K are not exactly there, or one is not this
            run's (stamps), has a wrong header (class, part, total_parts), does
            not hold exactly core part K's records, or has an entry that lacks
            a studies_tab path;
          - detail/ does not hold exactly shards 0..N-1, or a shard is not this
            run's, has a wrong header (class, shard, shards, key), holds a
            record of another shard or of no core part, or has an entry that
            lacks a detail path (study_sites is written whole there, so with
            its country too); or a core record has no detail entry;
          - data/run.json does not give this run's stamps, part count, record
            count and layout.
        With --latest (the weekly job passes its date), or whenever the site
        has a history.json, it also stops the push when what the snapshot
        menu offers is not there (check_history):
          - history.json cannot be read, or its "latest" is not --latest (the
            date data/ serves), is not listed, or is not its newest date;
          - data/run.json does not date its run as the latest (its
            snapshot_date, else the date of its extracted_at, as the site
            reads it): the site would then ask snapshots/<latest>/, which is
            not there;
          - a date it lists other than the latest has no snapshots/<date>/
            with a readable dashboard-summary.json;
          - an aggregate it names still holds dataset files, or the archive
            file it names is missing, unreadable, from another run than its
            summary, or not an entry for exactly its summary's recentStudies;
          - a complete snapshot lacks a file of its dataset (whole parts, or
            core, studies_tab and detail files and run.json), or a file, its
            summary, its sex/gender pair or its methods text carries another
            run's stamps or a wrong header (src/dataset_folder.py).
        Size over the site's budgets only warns: 20 MiB a part today, and the
        optional per-class budgets for the studies_tab and detail files. That
        is the owner's policy: block missing fields; for size, warn until a
        hard limit, and the hard limits are the ones a push or a Pages deploy
        would hit.
INVOKED by .github/workflows/extract.yml in the publish step, after the files
        are staged in the site checkout and before git commit / git push. A
        failure leaves the site on last week's data and skips the steps that
        publish to the site after it (the sex/gender audit, the sponsor
        bridge). The week's full records and raw sex/gender measures are on
        their releases by then; those steps run before the site checkout.

The path rules are the site's (tests/study_record_contract.test.mjs there):
'status' is a key, 'race.reported' a key inside an object, and
'study_sites[].country' a key on every item of a list.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from datetime import datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import archive_records  # noqa: E402
from src import dataset_folder as df  # noqa: E402
from src import site_layout as sl  # noqa: E402

# GitHub rejects a push with a file over 100 MiB. (The site serves its parts
# from GitHub Pages; the 20 MiB per-part figure in its budget is a budget,
# and only warns.)
PART_HARD_LIMIT_BYTES = 100 * 1024 * 1024
# GitHub Pages refuses to publish a site larger than 1 GB.
TREE_HARD_LIMIT_BYTES = 1_000_000_000
TREE_WARN_BYTES = 900_000_000
MAX_REPORTED = 20
EXAMPLES_PER_PATH = 3
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
HISTORY_FILE = "history.json"
_ABSENT = object()


def segments(path: str) -> list[tuple[str, bool]]:
    """'race.raw_categories[].omb_category' -> [(race, False), (raw_categories, True), (omb_category, False)]."""
    return [(s[:-2], True) if s.endswith("[]") else (s, False) for s in path.split(".")]


# Why a record fails a contract path, or None (src/site_layout.py, shared with
# the archive writer).
missing = sl.missing


def tree_size(root: str) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            p = os.path.join(dirpath, name)
            if not os.path.islink(p):
                total += os.path.getsize(p)
    return total


def read_gz_json(path: str) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


# The dataset files in a site folder, under either layout (src/dataset_folder.py).
dataset_files = df.present_files


def class_files(folder: str) -> list[str]:
    """The split layout's own files in a folder: studies_tab parts and detail/."""
    return [f for f in dataset_files(folder) if not sl.CORE_PART_RE.fullmatch(f) and f != sl.RUN_FILE]


def _said(layout: Any) -> str:
    return "none" if layout is _ABSENT else repr(layout)


def _eg(items: list[Any], n: int = 3) -> str:
    return ", ".join(map(str, items[:n])) + (" …" if len(items) > n else "")


class Tally:
    """Counts of one kind of fault with a few examples, so one fault in many records is one line."""

    def __init__(self) -> None:
        self.kinds: dict[str, dict[str, Any]] = {}

    def add(self, kind: str, example: str) -> None:
        entry = self.kinds.setdefault(kind, {"count": 0, "examples": []})
        entry["count"] += 1
        if len(entry["examples"]) < EXAMPLES_PER_PATH:
            entry["examples"].append(example)

    def lines(self) -> list[str]:
        return [f"{kind}: {e['count']:,}; e.g. {', '.join(e['examples'])}" for kind, e in self.kinds.items()]


class Findings:
    """One check's findings and report: errors block the push, warnings do not."""

    def __init__(self, site: str) -> None:
        self.site = site
        self.errors: list[str] = []
        self.warnings: list[str] = []
        # A missing path is counted per path, so one field gone from every
        # record reads as that, and every failing path is named once.
        self.missing_by_path: dict[str, dict[str, Any]] = {}
        self.faults = Tally()          # record-level faults of a split dataset: they block
        self.fat = Tally()             # fields a class's files carry but the class does not read: warn
        self.report: dict[str, Any] = {"site": site, "errors": self.errors, "warnings": self.warnings,
                                       "missing_by_path": self.missing_by_path, "layout_version": None,
                                       "classes": {}, "history": None}

    def err(self, msg: str) -> None:
        if len(self.errors) < MAX_REPORTED:
            self.errors.append(msg)

    def need(self, record: Any, paths: list[str], where: str, nct: Any) -> None:
        for path in paths:
            why = missing(record, path)
            if why:
                entry = self.missing_by_path.setdefault(path, {"count": 0, "why": why, "examples": []})
                entry["count"] += 1
                if len(entry["examples"]) < EXAMPLES_PER_PATH:
                    entry["examples"].append(f"{nct or '(no nct_id)'} in {where}")

    def finish(self) -> dict[str, Any]:
        for path, entry in self.missing_by_path.items():
            n = entry["count"]
            self.errors.append(f"{path}: missing in {n:,} record{'' if n == 1 else 's'} ({entry['why']}); "
                               f"e.g. {', '.join(entry['examples'])}")
        self.errors.extend(self.faults.lines())
        self.warnings.extend(f"{line} (a field the class does not read should not be published)"
                             for line in self.fat.lines())
        self.report["error_count"] = len(self.errors)
        self.report["ok"] = not self.errors
        return self.report


def check(site: str, part_hard_limit: int = PART_HARD_LIMIT_BYTES,
          tree_hard_limit: int = TREE_HARD_LIMIT_BYTES, tree_warn: int = TREE_WARN_BYTES,
          latest: str | None = None) -> dict[str, Any]:
    f = Findings(site)
    report, warnings = f.report, f.warnings
    try:
        with open(os.path.join(site, "tests", "record_contract.json"), encoding="utf-8") as fh:
            contract = json.load(fh)
        with open(os.path.join(site, "tests", "data_budget.json"), encoding="utf-8") as fh:
            budget = json.load(fh)
        paths = [p for cls in contract["classes"].values() for p in cls]
        part_count = int(budget["part_count"])
        part_budget = int(budget["part_gzip_max_bytes"])
        total_budget = int(budget["total_gzip_max_bytes"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        f.err(f"cannot read the site's record contract or data budget: {e}")
        return f.finish()
    report.update({"contract_paths": len(paths), "part_count": part_count,
                   "part_budget_bytes": part_budget, "total_budget_bytes": total_budget})
    switch: sl.Switch | None = None
    try:
        switch = sl.read_switch(contract)
    except sl.LayoutError as e:
        f.err(f"the site's record contract: {e}")

    data_dir = os.path.join(site, "data")
    present = sorted((n for n in os.listdir(data_dir) if sl.CORE_PART_RE.fullmatch(n)),
                     key=lambda n: int(sl.CORE_PART_RE.fullmatch(n).group(1)))
    expected = [sl.core_part_name(i) for i in range(1, part_count + 1)]
    if present != expected:
        f.err(f"data/ holds {present}, not the {part_count} parts the site fetches")

    sizes = {n: os.path.getsize(os.path.join(data_dir, n)) for n in present}
    total = sum(sizes.values())
    report["parts_gzip_bytes"] = total
    for n, size in sizes.items():
        if size > part_hard_limit:
            f.err(f"{n} is {size:,} bytes, over GitHub's per-file push limit of {part_hard_limit:,}")
        elif size > part_budget:
            warnings.append(f"{n} is {size:,} bytes, over the site's per-part budget of {part_budget:,}")
    if total > total_budget:
        warnings.append(f"the parts total {total:,} bytes, over the site's budget of {total_budget:,}")

    # Core part 1's header says how the records are laid out: no layout block
    # means inline, every class on the records (every file published before
    # the split); layout version 1 means split.
    mode: str | None = None              # "inline", "split", or "unknown" (already an error)
    layout: Any = _ABSENT
    plan: sl.Plan | None = None
    check_paths = paths
    first: dict[str, Any] | None = None
    seen: set[Any] = set()
    part_ids: dict[int, list[Any]] = {}
    records = 0
    for i, name in enumerate(expected, start=1):
        if name not in sizes:
            continue
        try:
            part = read_gz_json(os.path.join(data_dir, name))
        except (OSError, ValueError, EOFError) as e:
            f.err(f"{name} is not gzipped JSON: {e}")
            continue
        if not isinstance(part, dict):
            f.err(f"{name} is not a part: its JSON is a {type(part).__name__}, not an object")
            continue
        if mode is None:
            layout = part.get("layout", _ABSENT)
            if layout is _ABSENT:
                mode = "inline"
            elif isinstance(layout, dict) and type(layout.get("version")) is int \
                    and layout["version"] == sl.LAYOUT_VERSION:
                mode = "split"
                try:
                    plan = sl.Plan(contract)
                    check_paths = plan.required["core"]
                except sl.LayoutError as e:
                    f.err(f"the parts are split, and the site's record contract cannot be checked against "
                          f"the split layout: {e}")
                    mode, check_paths = "unknown", []
            else:
                f.err(f"{name} carries layout {layout!r}; this check knows inline parts (no layout) "
                      f"and layout version {sl.LAYOUT_VERSION} only")
                mode, check_paths = "unknown", []
        elif part.get("layout", _ABSENT) != layout:
            f.err(f"{name} carries layout {_said(part.get('layout', _ABSENT))}, not {_said(layout)} as the first "
                  "part does; the parts of one dataset share one layout")
        if not isinstance(part.get("data"), list) or not part["data"]:
            f.err(f"{name} has no records")
            continue
        head = {k: part.get(k) for k in ("extracted_at", "pipeline_commit", "part", "total_parts")}
        if head["part"] != i or head["total_parts"] != part_count:
            f.err(f"{name} says it is part {head['part']} of {head['total_parts']}, not {i} of {part_count}")
        if first is None:
            try:
                datetime.fromisoformat(str(head["extracted_at"]).replace("Z", "+00:00"))
            except ValueError:
                f.err(f"{name}: extracted_at is not a timestamp ({head['extracted_at']!r})")
            if not re.fullmatch(r"[0-9a-f]{7,40}", str(head["pipeline_commit"] or "")):
                f.err(f"{name}: pipeline_commit is not a commit id ({head['pipeline_commit']!r})")
            first = head
        elif (head["extracted_at"], head["pipeline_commit"]) != (first["extracted_at"], first["pipeline_commit"]):
            f.err(f"{name} comes from another run than part 1")
        ids: list[Any] = []
        for record in part["data"]:
            records += 1
            nct = record.get("nct_id") if isinstance(record, dict) else None
            if nct in seen:
                f.err(f"{nct} appears in more than one record ({name})")
            seen.add(nct)
            ids.append(nct)
            if mode == "split" and plan is not None:
                if not isinstance(nct, str) or not sl.NCT_RE.fullmatch(nct):
                    f.faults.add("core records whose nct_id is not NCT + 8 digits, so no detail shard can hold them",
                                 f"{nct!r} in {name}")
                if isinstance(record, dict):
                    for key in plan.stray_keys(record, "core"):
                        f.fat.add(f"core records carry {key}, which core does not read", str(nct))
            f.need(record, check_paths, name, nct)
        part_ids[i] = ids
        del part
    report["records"] = records

    if mode == "split" and plan is not None and first is not None:
        report["layout_version"] = sl.LAYOUT_VERSION
        check_split(f, switch, plan, layout, first, part_count, part_ids, sizes, budget, part_hard_limit)
    elif mode == "inline":
        report["classes"] = {"inline": {"files": len(sizes), "gzip_bytes": total, "records": records}}
        # The publish step removes a split week's files before it copies whole
        # parts in; any left over are files no page reads.
        stale = class_files(data_dir)
        if stale:
            warnings.append(f"data/ holds split-layout files beside whole-record parts, which no page reads: "
                            f"{_eg(stale)}")
        if switch is not None and switch.split:
            warnings.append("the site's contract turns the split layout on, but this week's parts carry whole records")
        check_inline_run(f, data_dir, first, part_count, records)

    if latest is not None or os.path.exists(os.path.join(site, HISTORY_FILE)):
        check_history(f, latest, part_count)

    tree = tree_size(site)
    report["tree_bytes"] = tree
    if tree > tree_hard_limit:
        f.err(f"the site would publish {tree:,} bytes, over GitHub Pages' limit of {tree_hard_limit:,}")
    elif tree > tree_warn:
        warnings.append(f"the site would publish {tree:,} bytes, close to GitHub Pages' limit of {tree_hard_limit:,}")
    return f.finish()


def check_split(f: Findings, switch: sl.Switch | None, plan: sl.Plan, layout: dict[str, Any], first: dict[str, Any],
                part_count: int, part_ids: dict[int, list[Any]], core_sizes: dict[str, int], budget: dict[str, Any],
                part_hard_limit: int) -> None:
    """A split dataset's other files: its studies_tab parts, its detail shards
    and run.json; and the per-class sizes."""
    site, report, warnings = f.site, f.report, f.warnings
    data_dir = os.path.join(site, "data")
    stamps = (first["extracted_at"], first["pipeline_commit"])

    # The handshake: the engine publishes a layout only while the site's
    # contract declares it and the owner has turned it on.
    if switch is None or not switch.split:
        f.err(f"the parts carry layout version {sl.LAYOUT_VERSION}, but the site's contract does not turn it on "
              "(its layout section needs \"version\": 1 and \"enabled\": true), so the site's pages may not read it")
    want = sl.header_layout(part_count, plan.shards)
    if layout != want:
        f.err(f"core part 1's layout is {layout!r}; the site's contract and part count give {want!r}")
    detail_block = layout.get("detail") if isinstance(layout.get("detail"), dict) else {}
    shards = detail_block.get("shards")
    if type(shards) is not int or not 1 <= shards <= sl.MAX_DETAIL_SHARDS:
        shards = None                     # reported above; the shards cannot be checked

    classes: dict[str, dict[str, Any]] = {
        "core": {"files": len(core_sizes), "gzip_bytes": sum(core_sizes.values()), "records": report["records"],
                 "file_budget": report["part_budget_bytes"], "total_budget": report["total_budget_bytes"]}}
    report["classes"] = classes
    class_budgets = budget.get("classes")
    if class_budgets is None:
        warnings.append("the site's data_budget.json has no per-class budgets (\"classes\"); "
                        "the studies_tab and detail sizes are reported only")
    elif not isinstance(class_budgets, dict):
        warnings.append("the site's data_budget.json \"classes\" is not an object; "
                        "the studies_tab and detail sizes are reported only")
        class_budgets = None

    def sized(cls: str, files: dict[str, int], records: int) -> None:
        """The class's sizes: over 100 MiB a file blocks; over its budget warns."""
        file_budget = total_budget = None
        entry = (class_budgets or {}).get(cls)
        if class_budgets is not None and entry is None:
            warnings.append(f"the site's data_budget.json names no budget for {cls}; its size is reported only")
        elif entry is not None:
            try:
                file_budget, total_budget = int(entry["file_gzip_max_bytes"]), int(entry["total_gzip_max_bytes"])
            except (KeyError, TypeError, ValueError) as e:
                warnings.append(f"the site's data_budget.json budget for {cls} cannot be read ({e!r}); "
                                "its size is reported only")
        total = sum(files.values())
        classes[cls] = {"files": len(files), "gzip_bytes": total, "records": records,
                        "file_budget": file_budget, "total_budget": total_budget}
        for name, size in files.items():
            if size > part_hard_limit:
                f.err(f"{name} is {size:,} bytes, over GitHub's per-file push limit of {part_hard_limit:,}")
            elif file_budget is not None and size > file_budget:
                warnings.append(f"{name} is {size:,} bytes, over the site's {cls} budget of {file_budget:,} a file")
        if total_budget is not None and total > total_budget:
            warnings.append(f"the {cls} files total {total:,} bytes, over the site's {cls} budget of {total_budget:,}")

    def opened(name: str, header: dict[str, Any]) -> dict[str, Any] | None:
        """The file's entries once its stamps and header are checked; None when it cannot be read."""
        try:
            doc = read_gz_json(os.path.join(data_dir, name))
        except (OSError, ValueError, EOFError) as e:
            f.err(f"{name} is not gzipped JSON: {e}")
            return None
        if not isinstance(doc, dict):
            f.err(f"{name} is not a {header['class']} file: its JSON is a {type(doc).__name__}, not an object")
            return None
        if (doc.get("extracted_at"), doc.get("pipeline_commit")) != stamps:
            f.err(f"{name} comes from another run than core part 1 "
                  f"({doc.get('extracted_at')!r}, {doc.get('pipeline_commit')!r})")
        got = {k: doc.get(k) for k in header}
        if got != header:
            f.err(f"{name} says {got}, not {header}")
        if not isinstance(doc.get("data"), dict):
            f.err(f"{name} holds no object keyed by nct_id")
            return None
        return doc["data"]

    def entry_checks(cls: str, nct: str, entry: Any, name: str) -> None:
        if not isinstance(entry, dict):
            f.faults.add(f"{cls} entries that are not objects", f"{nct} in {name}")
            return
        for key in plan.stray_keys(entry, cls):
            f.fat.add(f"{cls} entries carry {key}, which {cls} does not read", nct)
        f.need(entry, plan.required[cls], name, nct)

    # studies_tab part K: exactly core part K's records.
    tab_names = [sl.studies_tab_part_name(k) for k in range(1, part_count + 1)]
    tab_present = sorted((n for n in os.listdir(data_dir) if sl.STUDIES_TAB_PART_RE.fullmatch(n)),
                         key=lambda n: int(sl.STUDIES_TAB_PART_RE.fullmatch(n).group(1)))
    if tab_present != tab_names:
        f.err(f"data/ holds studies_tab parts {tab_present}, not {tab_names}")
    tab_sizes = {n: os.path.getsize(os.path.join(data_dir, n)) for n in tab_names if n in tab_present}
    tab_records = 0
    for k, name in enumerate(tab_names, start=1):
        if name not in tab_sizes:
            continue
        entries = opened(name, {"class": "studies_tab", "part": k, "total_parts": part_count})
        if entries is None:
            continue
        tab_records += len(entries)
        if k in part_ids:
            ids = part_ids[k]
            id_set = set(ids)
            absent = [nct for nct in ids if nct not in entries]
            foreign = [nct for nct in entries if nct not in id_set]
            if absent or foreign:
                f.err(f"{name} does not hold exactly core part {k}'s records: {len(absent):,} of them have no "
                      f"entry ({_eg(absent)}), {len(foreign):,} entries are not theirs ({_eg(foreign)})")
        for nct, entry in entries.items():
            entry_checks("studies_tab", nct, entry, name)
        del entries
    sized("studies_tab", tab_sizes, tab_records)

    # detail: every record exactly once, in shard int(nct_id[3:]) % N.
    on_disk = {n for n in dataset_files(data_dir) if n == sl.DETAIL_DIR or n.startswith(sl.DETAIL_DIR + "/")}
    detail_sizes: dict[str, int] = {}
    where: dict[str, int] = {}
    if shards is not None:
        shard_names = [sl.detail_shard_name(n) for n in range(shards)]
        absent_shards = [n for n in shard_names if n not in on_disk]
        strays = sorted(on_disk - set(shard_names))
        if absent_shards:
            f.err(f"data/ lacks {len(absent_shards):,} of the {shards} detail shards ({_eg(absent_shards)})")
        if strays:
            f.err(f"data/{sl.DETAIL_DIR}/ holds files the layout does not name: {_eg(strays, 5)}")
        detail_sizes = {n: os.path.getsize(os.path.join(data_dir, n)) for n in shard_names if n in on_disk}
        all_ids = {nct for ids in part_ids.values() for nct in ids}
        read_all = not absent_shards and len(part_ids) == part_count
        for n, name in enumerate(shard_names):
            if name not in detail_sizes:
                continue
            entries = opened(name, {"class": "detail", "shard": n, "shards": shards, "key": sl.SHARD_KEY})
            if entries is None:
                read_all = False
                continue
            for nct, entry in entries.items():
                try:
                    home: int | None = sl.shard_of(nct, shards)
                except sl.LayoutError:
                    home = None
                if home != n:
                    f.faults.add(f"detail entries outside their shard (int(nct_id[3:]) % {shards})", f"{nct} in {name}")
                if nct in where:
                    f.faults.add("records with an entry in more than one detail shard", f"{nct} in {where[nct]} and {n}")
                where[nct] = n
                if nct not in all_ids:
                    f.faults.add("detail entries for no core record", f"{nct} in {name}")
                entry_checks("detail", nct, entry, name)
            del entries
        if read_all:
            no_entry = sorted(str(nct) for nct in all_ids if nct not in where)
            if no_entry:
                f.err(f"{len(no_entry):,} core records have no detail entry ({_eg(no_entry)})")
    sized("detail", detail_sizes, len(where))

    # run.json: the site keys this run's files by its stamps.
    try:
        with open(os.path.join(data_dir, sl.RUN_FILE), encoding="utf-8") as fh:
            run = json.load(fh)
        if not isinstance(run, dict):
            raise ValueError(f"its JSON is a {type(run).__name__}, not an object")
    except (OSError, ValueError) as e:
        f.err(f"data/{sl.RUN_FILE} cannot be read ({e}); the site keys this run's files by it")
    else:
        want_run = {"extracted_at": stamps[0], "pipeline_commit": stamps[1], "total_parts": part_count,
                    "studies": report["records"], "layout": layout}
        for key, value in want_run.items():
            if run.get(key) != value:
                f.err(f"data/{sl.RUN_FILE} says {key} {run.get(key)!r}; the dataset says {value!r}")
        on_disk_bytes = {cls: classes[cls]["gzip_bytes"] for cls in sl.CLASSES}
        if run.get("gzip_bytes") != on_disk_bytes:
            warnings.append(f"data/{sl.RUN_FILE} gives gzip_bytes {run.get('gzip_bytes')!r}; "
                            f"the files are {on_disk_bytes!r}")


def check_inline_run(f: Findings, data_dir: str, first: dict[str, Any] | None, part_count: int,
                     records: int) -> None:
    """A whole-record week's data/run.json must be this run's: the site keys the
    run's files by its extracted_at, so a run.json left from another extraction
    (with the right date) would let returning browsers reuse that run's cached
    parts. Same stamps as part 1, its part count and record count, and no
    layout. Whether it is there at all is check_data_run's to say."""
    path = os.path.join(data_dir, sl.RUN_FILE)
    if first is None or not os.path.exists(path):
        return
    run = df.read_json(path)
    if not isinstance(run, dict):
        f.err(f"data/{sl.RUN_FILE} cannot be read as a JSON object; the site keys this run's files by it")
        return
    want = {"extracted_at": first["extracted_at"], "pipeline_commit": first["pipeline_commit"],
            "total_parts": part_count, "studies": records}
    for key, value in want.items():
        if run.get(key) != value:
            f.err(f"data/{sl.RUN_FILE} says {key} {run.get(key)!r}; the parts say {value!r}")
    if "layout" in run:
        f.err(f"data/{sl.RUN_FILE} carries a layout ({run['layout']!r}), but this week's parts carry whole records")


def _is_date(value: Any) -> bool:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def check_history(f: Findings, latest: str | None, part_count: int) -> None:
    """history.json against the tree: every date the "View snapshot" menu
    offers must open. latest is the date data/ serves (the weekly job's date;
    None: history.json's own). A complete snapshot is checked from its files'
    headers, every file decompressed to its end: its records were checked
    against the contract when data/ published them, and it is their copy."""
    site, warnings = f.site, f.warnings
    if latest is not None and not _is_date(latest):
        f.err(f"--latest {latest!r} is not a date (YYYY-MM-DD)")
        return
    path = os.path.join(site, HISTORY_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            history = json.load(fh)
    except (OSError, ValueError) as e:
        f.err(f"{HISTORY_FILE} cannot be read ({e}); the snapshot menu reads it")
        return
    dates = history.get("dates") if isinstance(history, dict) else None
    if not isinstance(dates, list) or any(not _is_date(d) for d in dates):
        f.err(f"{HISTORY_FILE} has no list of YYYY-MM-DD dates (dates: {_eg(dates if isinstance(dates, list) else [dates])})")
        return
    if len(set(dates)) != len(dates):
        f.err(f"{HISTORY_FILE} lists a date twice")
    named = history.get("latest")
    want = latest if latest is not None else named
    if named != want:
        f.err(f"{HISTORY_FILE} says latest {named!r}, but data/ serves {latest}")
    if not _is_date(want):
        f.err(f"{HISTORY_FILE} names no latest date (data/'s)")
        return
    check_data_run(f, want)
    if want not in dates:
        f.err(f"{HISTORY_FILE} does not list the latest date, {want}")
    if dates and max(dates) > want:
        f.err(f"{HISTORY_FILE} lists {max(dates)}, newer than the latest date, {want}")
    archives = history.get("archives", {})
    if not isinstance(archives, dict):
        f.err(f"{HISTORY_FILE} archives is not an object")
        archives = {}
    snapshots = os.path.join(site, "snapshots")
    record: dict[str, Any] = {"latest": want, "dates": len(dates), "complete": [], "aggregates": [], "archive_files": []}
    f.report["history"] = record
    for d in sorted(archives):
        if d not in dates or d == want:
            f.err(f"{HISTORY_FILE} archives names {d!r}, which is not a listed archived date")
    for d in sorted(set(dates)):
        if d == want:
            continue
        folder = os.path.join(snapshots, d)
        summary = df.read_summary(folder)
        if not os.path.isdir(folder):
            f.err(f"{HISTORY_FILE} lists {d}, but snapshots/{d}/ is not there")
            continue
        if summary is None:
            f.err(f"snapshots/{d}/ has no readable {df.SUMMARY_FILE}, which the menu needs for every date")
            continue
        entry = archives.get(d)
        if entry is not None:
            check_aggregate(f, d, folder, summary, entry, record)
            continue
        record["complete"].append(d)
        ds = df.inspect(folder, part_count)
        for problem in ds.problems:
            f.err(f"snapshots/{d}/ is listed as a complete snapshot but {problem}")
        if ds.stamps is not None:
            for problem in df.sex_gender_problems(folder, ds.stamps, d) + df.methods_problems(folder, ds.stamps, d):
                f.err(f"snapshots/{d}/: {problem}")
        if ds.strays:
            warnings.append(f"snapshots/{d}/ holds split-layout files beside whole-record parts, which no page reads: "
                            f"{_eg(ds.strays)}")
    if os.path.isdir(os.path.join(snapshots, want)):
        warnings.append(f"snapshots/{want}/ is there, a copy of the latest date, which data/ serves")
    unlisted = sorted(n for n in (os.listdir(snapshots) if os.path.isdir(snapshots) else [])
                      if os.path.isdir(os.path.join(snapshots, n)) and n not in dates)
    if unlisted:
        warnings.append(f"snapshots/ holds folders {HISTORY_FILE} does not list, which no page offers: {_eg(unlisted)}")


def check_data_run(f: Findings, want: str) -> None:
    """data/run.json must be the run of the date data/ serves. The site reads
    that date from data/ only when data/run.json gives it (runDate:
    snapshot_date, else the date of extracted_at); otherwise it asks
    snapshots/<date>/, which the latest date does not have, and the date
    fails for every visitor that week."""
    run = df.read_json(os.path.join(f.site, "data", sl.RUN_FILE))
    if not isinstance(run, dict):
        f.err(f"data/{sl.RUN_FILE} cannot be read as a JSON object, so the site would not serve {want} from data/ "
              f"and would ask snapshots/{want}/ for it")
        return
    said = df.run_date(run)
    if said != want:
        f.err(f"data/{sl.RUN_FILE} dates its run {said!r}, not {want} (its snapshot_date, else the date of its "
              f"extracted_at), so the site would not serve {want} from data/ and would ask snapshots/{want}/ for it")


def check_aggregate(f: Findings, d: str, folder: str, summary: dict[str, Any], entry: Any,
                    record: dict[str, Any]) -> None:
    """A monthly aggregate history.json names: its summary, and the archive file
    it names, which must be its summary's run's own records."""
    record["aggregates"].append(d)
    if not isinstance(entry, dict) or entry.get("kind") != "aggregate":
        f.err(f"{HISTORY_FILE} archives entry for {d} is {entry!r}, not {{\"kind\": \"aggregate\", ...}}")
        return
    held = df.present_files(folder)
    if held:
        f.err(f"{HISTORY_FILE} names {d} an aggregate, but snapshots/{d}/ still holds dataset files ({_eg(held)})")
    detail = entry.get("detail")
    if detail is None:
        if os.path.exists(os.path.join(folder, sl.ARCHIVE_FILE)):
            f.warnings.append(f"snapshots/{d}/{sl.ARCHIVE_FILE} is there, but {HISTORY_FILE} does not name it")
    elif detail != sl.ARCHIVE_FILE:
        f.err(f"{HISTORY_FILE} names {detail!r} as the archive file of {d}; the layout's is {sl.ARCHIVE_FILE}")
    elif not os.path.exists(os.path.join(folder, detail)):
        f.err(f"{HISTORY_FILE} names snapshots/{d}/{detail}, which is not there")
    else:
        found = archive_records.problems(os.path.join(folder, detail), summary)
        for problem in found:
            f.err(f"snapshots/{d}/: {problem}")
        if not found:
            record["archive_files"].append(d)
    others = sorted(set(os.listdir(folder)) - {df.SUMMARY_FILE, sl.ARCHIVE_FILE} - set(held))
    if others:
        f.warnings.append(f"snapshots/{d}/ is an aggregate but also keeps {_eg(others)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check this week's site files against the site's contract.")
    ap.add_argument("--site", required=True, help="the site checkout, with this week's files staged")
    ap.add_argument("--report", default=None, help="write the report here as JSON")
    ap.add_argument("--latest", default=None, metavar="DATE",
                    help="the date data/ serves (the weekly job's date): history.json's latest must name it, "
                         "and every date it lists must open")
    ap.add_argument("--part-hard-limit-bytes", type=int, default=PART_HARD_LIMIT_BYTES)
    ap.add_argument("--tree-hard-limit-bytes", type=int, default=TREE_HARD_LIMIT_BYTES)
    ap.add_argument("--tree-warn-bytes", type=int, default=TREE_WARN_BYTES)
    a = ap.parse_args(argv)
    report = check(a.site, a.part_hard_limit_bytes, a.tree_hard_limit_bytes, a.tree_warn_bytes, a.latest)
    if a.report:
        with open(a.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    for w in report["warnings"]:
        print(f"::warning::{w}")
    for e in report["errors"]:
        print(f"::error::{e}")
    mb = lambda b: f"{(b or 0) / 1e6:.1f} MB"  # noqa: E731
    classes = report.get("classes") or {}
    split = "; ".join(f"{cls} {mb(c['gzip_bytes'])} in {c['files']} files"
                      for cls, c in classes.items() if cls in sl.SIDECARS)
    print(f"site contract check: {'ok' if report['ok'] else 'FAILED'}; {report.get('records', 0):,} records, "
          f"parts {mb(report.get('parts_gzip_bytes'))} of {mb(report.get('total_budget_bytes'))} budget, "
          f"tree {mb(report.get('tree_bytes'))}" + (f"; split: {split}" if split else ""))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
