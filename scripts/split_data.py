#!/usr/bin/env python3
"""Cut the week's full records into the files the site serves for one dataset.

READS   --contract: the site's own tests/record_contract.json (the weekly job
        passes site/tests/record_contract.json from its site checkout): the
        study-record fields the site reads, in classes, and its `layout`
        section, whose `enabled` is the owner's switch for the split.
        --demographics (default data/demographics.json): the week's full study
        records, read through src/full_records.py (plain or gzipped). This
        script never writes or deletes it; the weekly job keeps it, gzipped,
        on the week's data-* release.
WRITES  --out-dir (default data/dataset): the files of one dataset, as a site
        folder (data/, snapshots/<date>/) holds them:
          - while the contract does not turn the split on (no layout section,
            or layout.enabled false): demographics.part1..8.json.gz, the whole
            records in 8 positional slices, byte for byte as this script wrote
            them before the split existed;
          - with layout version 1 and layout.enabled true (src/site_layout.py):
            demographics.part1..8.json.gz with the core fields and a `layout`
            block in each header, studies_tab.part1..8.json.gz aligned with
            them, and detail/0..N-1.json.gz (N = 256 unless the contract names
            another count). A field in no class is not published;
          - run.json: the run's stamps (the site keys its data cache by
            extracted_at), with --snapshot-date its snapshot_date, total_parts,
            studies, the layout block when split, and the gzip bytes per class
            ("inline" for whole-record parts). No list of files: their names
            follow from the layout. snapshot_date is the date the site serves
            the run as: the weekly job passes its own date, which
            history.json names as the latest, and the site reads that date
            from data/ only when data/run.json gives it (its snapshot_date,
            else the date of its extracted_at, which is the next day for a
            pull that runs past midnight UTC).
        The new files are written beside --out-dir and moved in only when all
        of them are; the previous ones (either layout's names, and only those)
        are removed then, so a switch either way leaves nothing stale.
EXITS   non-zero, with --out-dir untouched, when --snapshot-date is not a
        date, the contract cannot be read or names a layout version this
        engine does not know; when the records
        cannot be read or carry no extracted_at; and, split on, when the
        contract's classes cannot be split (src/site_layout.py says why), an
        nct_id is not NCT + 8 digits or appears twice, or a record would not
        merge back whole. Size never stops it: scripts/check_site_contract.py
        decides at publish.
INVOKED by .github/workflows/extract.yml right after the site checkout, with
        --snapshot-date "$CURRENT_DATE"; the publish step copies --out-dir
        into site/data only (scripts/prune_snapshots.py archives it into
        snapshots/<date>/ when the next week's run replaces it).

The 8 slices were first sized for a CDN's per-file limit. The site serves
every file from GitHub Pages now, whose 100 MiB per-file push limit the
publish check enforces; the slices stay because the site fetches the parts by
these names. The owner turns the split on in the site's contract once the
site's reader has been live through a weekly run, and turns it off (the
rollback) the same way.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import shutil
import sys
import time
from datetime import date
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import full_records  # noqa: E402
from src import site_layout as sl  # noqa: E402

NUM_PARTS = 8
SEPARATORS = (",", ":")
DEFAULT_OUT_DIR = os.path.join("data", "dataset")


def is_date(value: str) -> bool:
    """YYYY-MM-DD, and a real day (date.fromisoformat alone also takes 20261011)."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def load_contract(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            contract = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit(f"{path}: cannot read the site's record contract ({e}); nothing written")
    if not isinstance(contract, dict):
        raise SystemExit(f"{path}: the site's record contract is not a JSON object; nothing written")
    return contract


def part_bounds(total: int, parts: int = NUM_PARTS) -> list[tuple[int, int]]:
    """Positional slices, as before the split: ceil(total / parts) records each."""
    chunk = math.ceil(total / parts)
    return [(i * chunk, min(i * chunk + chunk, total)) for i in range(parts)]


def owned(name: str) -> bool:
    """A name this script writes in a dataset folder, under either layout."""
    return bool(sl.CORE_PART_RE.fullmatch(name) or sl.STUDIES_TAB_PART_RE.fullmatch(name)) \
        or name in (sl.RUN_FILE, sl.DETAIL_DIR)


def remove_owned(out_dir: str) -> list[str]:
    """Remove the names this script writes (either layout's) from out_dir, and nothing else."""
    removed: list[str] = []
    for name in sorted(os.listdir(out_dir)):
        if not owned(name):
            continue
        path = os.path.join(out_dir, name)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        removed.append(name)
    return removed


def write_whole(records: list[dict[str, Any]], stamps: dict[str, Any], out_dir: str) -> dict[str, int]:
    """The 8 whole-record parts, written exactly as split_data.py wrote them
    before the split: the same slices, header, separators, gzip level and
    file names (gzip keeps the name in its header). Returns their gzip bytes."""
    total = len(records)
    size = 0
    for i, (start, end) in enumerate(part_bounds(total)):
        part_data = {
            'extracted_at': stamps['extracted_at'],
            'pipeline_commit': stamps['pipeline_commit'],
            'part': i + 1,
            'total_parts': NUM_PARTS,
            'data': records[start:end]
        }
        path = os.path.join(out_dir, sl.core_part_name(i + 1))
        with gzip.open(path, 'wt', compresslevel=9) as f:
            json.dump(part_data, f, separators=(',', ':'))
        size += os.path.getsize(path)
        print(f"  Part {i + 1}: studies {start}–{end - 1} ({end - start} studies): "
              f"{os.path.getsize(path) / 1e6:.2f} MB")
    return {"inline": size}


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=SEPARATORS)


def write_gz(path: str, text: str) -> int:
    """Gzip ASCII JSON text to path (level 9, no name or time in the header, so
    the same content gives the same bytes); return the file's size."""
    data = gzip.compress(text.encode("ascii"), compresslevel=9, mtime=0)
    with open(path, "wb") as f:
        f.write(data)
    return len(data)


def write_split(records: list[Any], stamps: dict[str, Any], plan: sl.Plan, out_dir: str) -> dict[str, int]:
    """Core parts, studies_tab parts and detail shards (layout version 1).

    Consumes `records`: each slot is cleared once its record is cut, so the
    full records and their projections are not all held at once. A detail
    entry is kept as its JSON text until its shard is written."""
    total = len(records)
    shards = plan.shards
    layout = sl.header_layout(NUM_PARTS, shards)
    sizes = {"core": 0, "studies_tab": 0, "detail": 0}
    shard_entries: list[list[str]] = [[] for _ in range(shards)]
    seen: set[str] = set()
    os.makedirs(os.path.join(out_dir, sl.DETAIL_DIR))
    for k, (start, end) in enumerate(part_bounds(total), start=1):
        core_rows: list[dict[str, Any]] = []
        tab: dict[str, Any] = {}
        for idx in range(start, end):
            record, records[idx] = records[idx], None
            if not isinstance(record, dict):
                raise SystemExit(f"record {idx + 1} is a {type(record).__name__}, not a study record; nothing written")
            nct = record.get("nct_id")
            try:
                n = sl.shard_of(nct, shards)
            except sl.LayoutError as e:
                raise SystemExit(f"record {idx + 1}: {e}; nothing written")
            if nct in seen:
                raise SystemExit(f"{nct} appears in more than one record; nothing written")
            seen.add(nct)
            core = plan.core(record)
            entry_tab = plan.sidecar(record, "studies_tab")
            entry_detail = plan.sidecar(record, "detail")
            # What the site puts back together must be the record as the
            # contract reads it: nothing listed lost, nothing unlisted kept.
            merged, full = sl.merge_study(core, entry_tab, entry_detail), plan.full(record)
            if merged != full:
                differ = sorted(key for key in set(merged) | set(full) if merged.get(key) != full.get(key))
                raise SystemExit(f"{nct}: its core, studies_tab and detail entries do not merge back to the record "
                                 f"the contract describes (they differ in {', '.join(differ)}); nothing written")
            core_rows.append(core)
            tab[nct] = entry_tab
            shard_entries[n].append(dumps(nct) + ":" + dumps(entry_detail))
        doc: dict[str, Any] = {**stamps, "part": k, "total_parts": NUM_PARTS, "layout": layout, "data": core_rows}
        core_bytes = write_gz(os.path.join(out_dir, sl.core_part_name(k)), dumps(doc))
        doc = {**stamps, "class": "studies_tab", "part": k, "total_parts": NUM_PARTS, "data": tab}
        tab_bytes = write_gz(os.path.join(out_dir, sl.studies_tab_part_name(k)), dumps(doc))
        del doc, core_rows, tab
        sizes["core"] += core_bytes
        sizes["studies_tab"] += tab_bytes
        print(f"  Part {k}: studies {start}–{end - 1} ({end - start} studies): core {core_bytes / 1e6:.2f} MB, "
              f"studies_tab {tab_bytes / 1e6:.2f} MB")
    shard_sizes: list[int] = []
    for n in range(shards):
        head = dumps({**stamps, "class": "detail", "shard": n, "shards": shards, "key": sl.SHARD_KEY})
        body = head[:-1] + ',"data":{' + ",".join(shard_entries[n]) + "}}"
        shard_entries[n] = []
        shard_sizes.append(write_gz(os.path.join(out_dir, sl.detail_shard_name(n)), body))
    sizes["detail"] = sum(shard_sizes)
    ordered = sorted(shard_sizes)
    largest = shard_sizes.index(ordered[-1])
    print(f"  {shards} detail shards: {sizes['detail'] / 1e6:.2f} MB; per shard min {ordered[0]:,} B, "
          f"median {ordered[len(ordered) // 2]:,} B, max {ordered[-1]:,} B ({sl.detail_shard_name(largest)})")
    return sizes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Cut the week's full records into the site's files for one dataset.")
    ap.add_argument("--contract", required=True,
                    help="the site's tests/record_contract.json (its classes and its layout switch)")
    ap.add_argument("--demographics", default=full_records.DEFAULT_PATH,
                    help="the week's full records (default %(default)s; .json or .json.gz)")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="the dataset folder to write (default %(default)s)")
    ap.add_argument("--snapshot-date", default=None, metavar="DATE",
                    help="the date the site serves this run as (the weekly job's date, history.json's latest), "
                         "written into run.json as snapshot_date")
    a = ap.parse_args(argv)
    t0 = time.monotonic()
    if a.snapshot_date is not None and not is_date(a.snapshot_date):
        raise SystemExit(f"--snapshot-date {a.snapshot_date!r} is not a date (YYYY-MM-DD); nothing written")

    # Everything that can refuse the run is read before anything is written.
    contract = load_contract(a.contract)
    try:
        switch = sl.read_switch(contract)
    except sl.LayoutError as e:
        raise SystemExit(f"{a.contract}: {e}; nothing written")
    plan: sl.Plan | None = None
    if switch.split:
        try:
            plan = sl.Plan(contract)
        except sl.LayoutError as e:
            raise SystemExit(f"{a.contract}: the split layout is on, but {e}; nothing written")
    elif switch.declared:
        # Whole parts need no classes, but a layout section the split could
        # not follow is worth knowing before the owner turns it on.
        try:
            sl.Plan(contract)
        except sl.LayoutError as e:
            print(f"::warning::{a.contract}: the split layout is off, and could not run on this contract as it "
                  f"stands: {e}")

    print(f"Loading the full records from {a.demographics}...")
    records, extracted_at, pipeline_commit = full_records.load(a.demographics)
    if not extracted_at:
        raise SystemExit(f"{a.demographics}: no extracted_at stamp, so the site's files could not say which pull "
                         "they are; nothing written")
    stamps = {"extracted_at": extracted_at, "pipeline_commit": pipeline_commit}
    total = len(records)

    # Written beside the dataset folder and moved in whole, so a run that
    # stops part way leaves the folder as it was.
    work = os.path.normpath(a.out_dir) + ".partial"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        layout: dict[str, Any] | None = None
        if plan is not None:
            print(f"Splitting {total} studies (layout version {sl.LAYOUT_VERSION}, {plan.shards} detail shards; "
                  f"written whole beside core: {', '.join(plan.whole) or 'nothing'})")
            sizes = write_split(records, stamps, plan, work)
            layout = sl.header_layout(NUM_PARTS, plan.shards)
        else:
            why = "turns it off" if switch.declared else "has no layout section"
            print(f"Splitting {total} studies into {NUM_PARTS} whole-record parts "
                  f"(the site's contract {why})")
            sizes = write_whole(records, stamps, work)
        del records
        # The run's stamps, published as run.json next to the parts. The site
        # keys every data URL by extracted_at, so a browser keeps a run's
        # files across visits and fetches new ones when a new run lands, a
        # same-day re-run included; and it serves the latest date from data/
        # only when snapshot_date (else extracted_at's date) is that date.
        run: dict[str, Any] = {**stamps}
        if a.snapshot_date is not None:
            run["snapshot_date"] = a.snapshot_date
        run.update({"total_parts": NUM_PARTS, "studies": total})
        if layout is not None:
            run["layout"] = layout
        run["gzip_bytes"] = sizes
        with open(os.path.join(work, sl.RUN_FILE), "w") as f:
            json.dump(run, f, indent=2)
        os.makedirs(a.out_dir, exist_ok=True)
        removed = remove_owned(a.out_dir)
        if removed:
            print(f"Removed the previous dataset files from {a.out_dir}: {', '.join(removed)}")
        for name in sorted(os.listdir(work)):
            os.replace(os.path.join(work, name), os.path.join(a.out_dir, name))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    summary = ", ".join(f"{cls} {size / 1e6:.1f} MB" for cls, size in sizes.items())
    print(f"✓ {total} studies into {a.out_dir}, {'split (layout 1)' if layout else 'whole-record parts'}: "
          f"{summary}; wrote {sl.RUN_FILE} ({time.monotonic() - t0:.0f} s)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
