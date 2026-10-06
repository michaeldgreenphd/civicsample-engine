"""A dataset folder of the site, read without trusting it.

A dataset is one folder of the site repository: data/ for the latest run,
snapshots/<date>/ for an archived week (src/site_layout.py names its files).
scripts/prune_snapshots.py reads data/ through this module before it archives
last week's dataset out of it, and reads a snapshot through it before it
slims the snapshot into a monthly aggregate; scripts/check_site_contract.py
reads every complete snapshot history.json lists. All three ask the same
question, so they ask it here: is this folder one complete run?

inspect(folder) answers from the files' headers. Every file the engine writes
puts its header keys before "data", so a header is read from the first
kilobytes of the decompressed stream (read_head); the rest of the stream is
still decompressed to its end, which checks the gzip checksum and length, so
a truncated or damaged copy is caught without parsing its JSON. A folder is
one complete run when:

  - it holds demographics.part1..N.json.gz (N the site's part count, or core
    part 1's total_parts), each saying it is part k of N, each carrying core
    part 1's run stamps (extracted_at, pipeline_commit) and the same `layout`
    block, or none;
  - inline (no layout: whole records, every dataset published before the
    split): run.json, when there is one (folders archived before it existed
    have none), gives the same stamps and part count and no layout. A split
    file beside whole parts is a stray: no page reads it, and it is no loss;
  - split (layout version 1): studies_tab.part1..N.json.gz and
    detail/0..S-1.json.gz are all there and nothing else is, each with its
    header and the same stamps, and run.json gives the stamps, the part count
    and the layout;
  - dashboard-summary.json is there and is the same run's: its extracted_at
    is the parts', and so is its pipeline_commit when it has one (summaries
    written before 2026-09 have none, like their parts).

sex_gender_problems and methods_problems say whether the sex/gender table,
its meta and the methods text that ride with a dataset are that run's.
records() returns studies' records as the site puts them together. run_date()
reads the date of a run.json as the site reads data/run.json's.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import re
import zlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src import site_layout as sl

SUMMARY_FILE = "dashboard-summary.json"
SG_TABLE = "sex_gender_parsed.csv.gz"
SG_META = "sex_gender_parsed_meta.json"
SG_METHODS = "sex_gender/methods.json"
# How much of a decompressed file read_head parses for its header; the
# engine's headers are a few hundred bytes. A file whose header does not fit,
# or whose data comes first, is parsed whole instead.
HEAD_BYTES = 1 << 16
_CHUNK = 1 << 20
# A date as the site's runDate accepts one: YYYY-MM-DD text.
DATE_TEXT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class FolderError(ValueError):
    """A file of a dataset cannot be read as one."""


def _eg(items: list[Any], n: int = 3) -> str:
    return ", ".join(map(str, items[:n])) + (" …" if len(items) > n else "")


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def _head_of(text: str) -> dict[str, Any] | None:
    """The keys before "data" of the JSON object that text starts, or None when
    text ends before "data" is reached or does not parse that far."""
    decoder = json.JSONDecoder()
    i = _skip_ws(text, 0)
    if i >= len(text) or text[i] != "{":
        return None
    i += 1
    head: dict[str, Any] = {}
    try:
        while True:
            i = _skip_ws(text, i)
            key, i = decoder.raw_decode(text, i)
            i = _skip_ws(text, i)
            if not isinstance(key, str) or i >= len(text) or text[i] != ":":
                return None
            if key == "data":
                return head
            value, i = decoder.raw_decode(text, _skip_ws(text, i + 1))
            head[key] = value
            i = _skip_ws(text, i)
            if i >= len(text) or text[i] != ",":
                return None                  # the object ended (or broke off) before data
            i += 1
    except json.JSONDecodeError:
        return None


def read_head(path: str, verify: bool = True) -> dict[str, Any]:
    """The header of a gzipped JSON object: every key before its "data", which
    every file the engine writes puts last. With verify, the rest of the stream
    is decompressed too (not parsed), so a damaged file fails here.

    FolderError when the file is not gzipped JSON or not a JSON object."""
    try:
        with gzip.open(path, "rb") as fh:
            first = fh.read(HEAD_BYTES)
            head = _head_of(first.decode("utf-8", errors="replace"))
            if head is None:
                # A header that does not fit, data that is not last, or no
                # data at all: read the file whole and take its other keys.
                doc = json.loads((first + fh.read()).decode("utf-8"))
                if not isinstance(doc, dict):
                    raise FolderError(f"is not an object: its JSON is a {type(doc).__name__}")
                return {k: v for k, v in doc.items() if k != "data"}
            if verify:
                while fh.read(_CHUNK):
                    pass
            return head
    except FolderError:
        raise
    except (OSError, EOFError, zlib.error, UnicodeDecodeError, ValueError) as e:
        raise FolderError(f"is not gzipped JSON ({e})") from e


def read_gz_json(path: str) -> Any:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, EOFError, zlib.error, UnicodeDecodeError, ValueError) as e:
        raise FolderError(f"{os.path.basename(path)} is not gzipped JSON ({e})") from e


def read_json(path: str) -> Any:
    """A plain JSON file, or None when it is missing or cannot be read."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def read_summary(folder: str) -> dict[str, Any] | None:
    """The folder's dashboard-summary.json, or None when it is missing, cannot
    be read, or is not a JSON object."""
    summary = read_json(os.path.join(folder, SUMMARY_FILE))
    return summary if isinstance(summary, dict) else None


def run_date(run: Any) -> str | None:
    """The date of the run a run.json describes, read as the site reads
    data/run.json (resolveDataCacheVersion and runDate in its app.js): None
    unless its extracted_at is a timestamp (the site ignores a run.json whose
    stamp it cannot parse); then its snapshot_date when that is YYYY-MM-DD
    text, else the first ten characters of its extracted_at. The site serves
    the latest date from data/ only when this is that date, and asks
    snapshots/<date>/ otherwise."""
    at = run.get("extracted_at") if isinstance(run, dict) else None
    if not isinstance(at, str):
        return None
    try:
        datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None
    said = run.get("snapshot_date")
    if isinstance(said, str) and DATE_TEXT.fullmatch(said):
        return said
    return at[:10] if DATE_TEXT.fullmatch(at[:10]) else None


def present_files(folder: str) -> list[str]:
    """The dataset files in a folder, under either layout, relative to it: the
    core parts, the studies_tab parts, run.json, and every file under detail/
    (so a stray one there is seen). The frozen data/details.part*.json.gz and
    every other file are not dataset files."""
    if not os.path.isdir(folder):
        return []
    out = [n for n in os.listdir(folder)
           if sl.CORE_PART_RE.fullmatch(n) or sl.STUDIES_TAB_PART_RE.fullmatch(n) or n == sl.RUN_FILE]
    detail = os.path.join(folder, sl.DETAIL_DIR)
    if os.path.isdir(detail) and not os.path.islink(detail):
        for dirpath, _, filenames in os.walk(detail):
            rel = os.path.relpath(dirpath, folder).replace(os.sep, "/")
            out += [f"{rel}/{name}" for name in filenames]
    elif os.path.lexists(detail):
        out.append(sl.DETAIL_DIR)
    return sorted(out)


@dataclass
class Dataset:
    """What inspect found in a folder."""
    folder: str
    part_count: int = 0
    layout: dict[str, Any] | None = None           # None: inline
    stamps: tuple[Any, Any] | None = None          # core part 1's (extracted_at, pipeline_commit)
    files: list[str] = field(default_factory=list)       # its dataset files, relative, "/"-separated
    problems: list[str] = field(default_factory=list)    # why it is not one complete run
    strays: list[str] = field(default_factory=list)      # split files beside whole parts: no loss
    summary: dict[str, Any] | None = None

    @property
    def complete(self) -> bool:
        return self.stamps is not None and not self.problems


def _expected(part_count: int, layout: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Each dataset file a layout names, with the header keys it must carry
    (besides the run stamps)."""
    out: dict[str, dict[str, Any]] = {}
    for k in range(1, part_count + 1):
        head: dict[str, Any] = {"part": k, "total_parts": part_count}
        if layout is not None:
            head["layout"] = layout
        out[sl.core_part_name(k)] = head
    if layout is None:
        return out
    for k in range(1, part_count + 1):
        out[sl.studies_tab_part_name(k)] = {"class": "studies_tab", "part": k, "total_parts": part_count}
    shards = layout["detail"]["shards"]
    for n in range(shards):
        out[sl.detail_shard_name(n)] = {"class": "detail", "shard": n, "shards": shards, "key": sl.SHARD_KEY}
    return out


def inspect(folder: str, part_count: int | None = None, verify: bool = True) -> Dataset:
    """The dataset a folder holds, and every reason it is not one complete run.

    part_count is the number of core parts the site fetches; None takes core
    part 1's total_parts. verify decompresses every file to its end."""
    ds = Dataset(folder=folder)
    ds.summary = read_summary(folder)
    present = set(present_files(folder))
    first = sl.core_part_name(1)
    if first not in present:
        ds.problems.append(f"no {first}" if present else "no dataset files")
        return ds
    try:
        head1 = read_head(os.path.join(folder, first), verify)
    except FolderError as e:
        ds.problems.append(f"{first} {e}")
        return ds
    n = part_count if part_count is not None else head1.get("total_parts")
    if type(n) is not int or n < 1:
        ds.problems.append(f"{first} gives no part count (total_parts {head1.get('total_parts')!r})")
        return ds
    ds.part_count = n
    if not isinstance(head1.get("extracted_at"), str) or not head1["extracted_at"]:
        ds.problems.append(f"{first} carries no extracted_at, so nothing can say which run it is")
        return ds
    ds.stamps = (head1.get("extracted_at"), head1.get("pipeline_commit"))
    layout = head1.get("layout")
    if layout is not None:
        detail = layout.get("detail") if isinstance(layout, dict) else None
        shards = detail.get("shards") if isinstance(detail, dict) else None
        if type(shards) is not int or not 1 <= shards <= sl.MAX_DETAIL_SHARDS or layout != sl.header_layout(n, shards):
            ds.problems.append(f"{first} carries layout {layout!r}, not layout version {sl.LAYOUT_VERSION} "
                               f"for {n} parts")
            return ds
        ds.layout = layout
    expected = _expected(n, ds.layout)
    missing: list[str] = []
    for name, want in expected.items():
        if name not in present:
            missing.append(name)
            continue
        try:
            head = head1 if name == first else read_head(os.path.join(folder, name), verify)
        except FolderError as e:
            ds.problems.append(f"{name} {e}")
            continue
        if (head.get("extracted_at"), head.get("pipeline_commit")) != ds.stamps:
            ds.problems.append(f"{name} comes from another run than core part 1 "
                               f"({head.get('extracted_at')!r}, {head.get('pipeline_commit')!r})")
        got = {k: head.get(k) for k in want}
        if got != want:
            ds.problems.append(f"{name} says {got}, not {want}")
        elif ds.layout is None and "layout" in head:
            ds.problems.append(f"{name} carries a layout; core part 1 carries none")
    if missing:
        ds.problems.append(f"lacks {len(missing):,} of its {len(expected):,} dataset files ({_eg(missing)})")
    others = sorted(present - set(expected) - {sl.RUN_FILE})
    extra_parts = [name for name in others if sl.CORE_PART_RE.fullmatch(name)]
    if ds.layout is None and not extra_parts:
        # Split files beside whole parts: no page reads them.
        ds.strays = others
    elif others:
        ds.problems.append(f"holds dataset files its layout does not name ({_eg(others, 5)})")
    ds.files = [name for name in expected if name in present]
    _check_run(ds, present)
    _check_summary(ds)
    return ds


def _check_run(ds: Dataset, present: set[str]) -> None:
    """run.json: the run's stamps, part count and layout. A split dataset must
    have one; an inline one may (folders archived before it existed have none)."""
    if sl.RUN_FILE not in present:
        if ds.layout is not None:
            ds.problems.append(f"no {sl.RUN_FILE}, which a split dataset's files are keyed by")
        return
    ds.files.append(sl.RUN_FILE)
    run = read_json(os.path.join(ds.folder, sl.RUN_FILE))
    if not isinstance(run, dict):
        ds.problems.append(f"{sl.RUN_FILE} cannot be read as a JSON object")
        return
    assert ds.stamps is not None
    want = {"extracted_at": ds.stamps[0], "pipeline_commit": ds.stamps[1], "total_parts": ds.part_count,
            "layout": ds.layout}
    for key, value in want.items():
        if run.get(key) != value:
            ds.problems.append(f"{sl.RUN_FILE} says {key} {run.get(key)!r}; the parts say {value!r}")


def _check_summary(ds: Dataset) -> None:
    """dashboard-summary.json: there, and the parts' run."""
    if ds.summary is None:
        ds.problems.append(f"no readable {SUMMARY_FILE}")
        return
    assert ds.stamps is not None
    if ds.summary.get("extracted_at") != ds.stamps[0]:
        ds.problems.append(f"{SUMMARY_FILE} comes from another run (extracted_at "
                           f"{ds.summary.get('extracted_at')!r}; the parts' is {ds.stamps[0]!r})")
    commit = ds.summary.get("pipeline_commit")
    if commit is not None and commit != ds.stamps[1]:
        ds.problems.append(f"{SUMMARY_FILE} comes from another commit ({commit!r}; the parts' is {ds.stamps[1]!r})")


def sex_gender_problems(folder: str, stamps: tuple[Any, Any], date: str | None = None) -> list[str]:
    """Why the folder's sex/gender table and meta are not the dataset's run's
    ([] when they are, or when neither is there). The table carries no stamp,
    so, as the site checks it: as many rows as the meta counts, each with the
    meta's snapshot_date."""
    meta_path, table_path = os.path.join(folder, SG_META), os.path.join(folder, SG_TABLE)
    has_meta, has_table = os.path.exists(meta_path), os.path.exists(table_path)
    if not has_meta and not has_table:
        return []
    if not (has_meta and has_table):
        return [f"{SG_META if has_meta else SG_TABLE} is there without {SG_TABLE if has_meta else SG_META}"]
    meta = read_json(meta_path)
    if not isinstance(meta, dict):
        return [f"{SG_META} cannot be read as a JSON object"]
    out: list[str] = []
    if meta.get("source_extracted_at") != stamps[0]:
        out.append(f"{SG_META} comes from another run (source_extracted_at {meta.get('source_extracted_at')!r}; "
                   f"the parts' extracted_at is {stamps[0]!r})")
    source_commit = meta.get("source_pipeline_commit")
    if source_commit is not None and stamps[1] is not None and source_commit != stamps[1]:
        out.append(f"{SG_META} comes from another commit ({source_commit!r}; the parts' is {stamps[1]!r})")
    if date is not None and meta.get("snapshot_date") != date:
        out.append(f"{SG_META} is the {meta.get('snapshot_date')!r} snapshot's, not {date}'s")
    try:
        with gzip.open(table_path, "rt", encoding="utf-8", newline="") as fh:
            rows = 0
            off = 0
            for row in csv.DictReader(fh):
                rows += 1
                if row.get("snapshot_date") != meta.get("snapshot_date"):
                    off += 1
    except (OSError, EOFError, zlib.error, UnicodeDecodeError, csv.Error) as e:
        return out + [f"{SG_TABLE} cannot be read ({e})"]
    if isinstance(meta.get("n_rows"), int) and rows != meta["n_rows"]:
        out.append(f"{SG_TABLE} has {rows:,} rows; its meta counts {meta['n_rows']:,}")
    if off:
        out.append(f"{off:,} rows of {SG_TABLE} carry another snapshot_date than its meta's "
                   f"{meta.get('snapshot_date')!r}")
    return out


def methods_problems(folder: str, stamps: tuple[Any, Any], date: str | None = None) -> list[str]:
    """Why the folder's sex/gender methods text is not the dataset's run's
    ([] when it is, or when there is none)."""
    path = os.path.join(folder, SG_METHODS)
    if not os.path.exists(path):
        return []
    methods = read_json(path)
    if not isinstance(methods, dict):
        return [f"{SG_METHODS} cannot be read as a JSON object"]
    out: list[str] = []
    if methods.get("source_extracted_at") != stamps[0]:
        out.append(f"{SG_METHODS} comes from another run (source_extracted_at "
                   f"{methods.get('source_extracted_at')!r}; the parts' extracted_at is {stamps[0]!r})")
    if date is not None and methods.get("snapshot_date") != date:
        out.append(f"{SG_METHODS} is the {methods.get('snapshot_date')!r} snapshot's, not {date}'s")
    return out


def _data_of(path: str, kind: type) -> Any:
    doc = read_gz_json(path)
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, kind):
        raise FolderError(f"{os.path.basename(path)} holds no {'list' if kind is list else 'object'} of records")
    return data


def records(ds: Dataset, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Those studies' records as the site puts them together: an inline
    dataset's whole records; a split one's core record with its studies_tab
    and detail entries merged over it (site_layout.merge_study). A study the
    dataset does not hold is left out. Reads one file at a time.

    FolderError when the dataset is not one complete run, a file cannot be
    read, a study is in two records, or a split study lacks an entry."""
    if not ds.complete:
        raise FolderError(f"{ds.folder} is not one complete run: {'; '.join(ds.problems)}")
    wanted = set(ids)
    found: dict[str, dict[str, Any]] = {}
    part_of: dict[str, int] = {}
    for k in range(1, ds.part_count + 1):
        for record in _data_of(os.path.join(ds.folder, sl.core_part_name(k)), list):
            nct = record.get("nct_id") if isinstance(record, dict) else None
            if nct in wanted:
                if nct in found:
                    raise FolderError(f"{nct} is in two records of {ds.folder}")
                found[nct] = record
                part_of[nct] = k
    if ds.layout is None:
        return found
    shards = ds.layout["detail"]["shards"]
    by_part: dict[int, list[str]] = defaultdict(list)
    by_shard: dict[int, list[str]] = defaultdict(list)
    for nct in found:
        by_part[part_of[nct]].append(nct)
        by_shard[sl.shard_of(nct, shards)].append(nct)
    entries: dict[str, list[Any]] = {nct: [] for nct in found}
    for files, groups in ((sl.studies_tab_part_name, by_part), (sl.detail_shard_name, by_shard)):
        for i, ncts in sorted(groups.items()):
            name = files(i)
            data = _data_of(os.path.join(ds.folder, name), dict)
            for nct in ncts:
                if nct not in data:
                    raise FolderError(f"{name} of {ds.folder} has no entry for {nct}")
                entries[nct].append(data[nct])
    return {nct: sl.merge_study(record, *entries[nct]) for nct, record in found.items()}

