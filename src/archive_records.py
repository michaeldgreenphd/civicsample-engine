"""archive_records.json.gz: a monthly aggregate's own study records.

When a complete snapshot leaves the complete window and becomes its month's
aggregate, the site keeps its dashboard-summary.json: its charts are the whole
dataset's, and its Studies table lists the summary's recentStudies rows (500).
Those rows are summaries, and a pop-up needs the record. So before the
folder's dataset files are deleted, scripts/prune_snapshots.py writes this
file from the folder's OWN records (its whole-record parts, or core +
studies_tab + detail for a split folder): the aggregate's pop-ups then show
that week's values, never another week's (owner decisions 2c and 21a).

    {"source_extracted_at":    the run's extracted_at,
     "source_pipeline_commit": the run's pipeline_commit, or null when its
                               parts carry none (every run before 2026-09),
     "class": "archive",
     "data": {nct_id: the record projected onto every field the live site
              contract lists, its classes and its optional paths, keys
              sorted, ...}}   (in the summary's recentStudies order)

Its keys are the contract's layout.headers.archive, and its name its
layout.archive.file. It covers exactly the summary's recentStudies ids, and its
stamps are the stamps the site compares with the summary's: extracted_at
always, pipeline_commit when the summary has one. history.json names it,
"archives": {"<date>": {"kind": "aggregate", "detail": "archive_records.json.gz"}},
and scripts/check_site_contract.py checks it with problems() before every push.
"""
from __future__ import annotations

import gzip
import json
import os
from typing import Any

from src import dataset_folder as df
from src import site_layout as sl


class ArchiveError(ValueError):
    """An aggregate's archive file cannot be written from what the folder holds."""


def _eg(items: list[Any], n: int = 3) -> str:
    return ", ".join(map(str, items[:n])) + (" …" if len(items) > n else "")


def summary_ids(summary: Any) -> list[str]:
    """The nct_ids of a summary's recentStudies rows, in order.

    ArchiveError when it lists none, a row has no nct_id, or one is listed twice."""
    rows = summary.get("recentStudies") if isinstance(summary, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ArchiveError(f"its {df.SUMMARY_FILE} lists no recentStudies")
    ids = [row.get("nct_id") if isinstance(row, dict) else None for row in rows]
    bad = [i for i, nct in enumerate(ids) if not isinstance(nct, str) or not nct]
    if bad:
        raise ArchiveError(f"{len(bad)} recentStudies rows of its {df.SUMMARY_FILE} carry no nct_id (rows {_eg(bad)})")
    if len(set(ids)) != len(ids):
        raise ArchiveError(f"its {df.SUMMARY_FILE} lists a study twice in recentStudies")
    return ids


def build(dataset: df.Dataset, contract: Any) -> dict[str, Any]:
    """The archive file of a folder that holds one complete run: its summary's
    recentStudies, each projected from the folder's own record onto every field
    the contract lists.

    ArchiveError when the folder is not one complete run, its summary lists no
    studies, a study is not in its records, or the contract cannot be read."""
    if not dataset.complete or dataset.stamps is None:
        raise ArchiveError(f"{dataset.folder} is not one complete run: {'; '.join(dataset.problems)}")
    ids = summary_ids(dataset.summary)
    try:
        spec = sl.record_spec(contract)
        sl.archive_file_name(contract)
    except sl.LayoutError as e:
        raise ArchiveError(f"the site's record contract: {e}") from e
    try:
        found = df.records(dataset, ids)
    except (df.FolderError, sl.LayoutError) as e:
        raise ArchiveError(str(e)) from e
    absent = [nct for nct in ids if nct not in found]
    if absent:
        raise ArchiveError(f"{len(absent)} of the {len(ids)} recentStudies of {dataset.folder} are not in its "
                           f"records ({_eg(absent)})")
    data = {nct: _canonical(sl.project_object(found[nct], spec)) for nct in ids}
    lost = projection_losses(found, data, required_paths(contract))
    if lost:
        raise ArchiveError(f"the projection of {dataset.folder}'s records drops contract fields they have: "
                           + "; ".join(f"{p} in {len(ncts)} records ({_eg(ncts)})" for p, ncts in sorted(lost.items())))
    return {"source_extracted_at": dataset.stamps[0], "source_pipeline_commit": dataset.stamps[1],
            "class": sl.ARCHIVE_CLASS, "data": data}


def required_paths(contract: Any) -> list[str]:
    """Every path the contract's classes list (its optional paths aside)."""
    classes = contract.get("classes") if isinstance(contract, dict) else None
    if not isinstance(classes, dict):
        raise ArchiveError("the site's record contract lists no classes")
    return [p for c in sl.CLASSES for p in classes.get(c, []) if isinstance(p, str)]


def projection_losses(source: dict[str, dict[str, Any]], archived: dict[str, Any],
                      paths: list[str]) -> dict[str, list[str]]:
    """The contract paths an archived record lacks although its source record
    has them, with the studies: the slim deletes the source, so a value the
    projection dropped would be lost. A path the source record lacks too is
    absence, kept as absence: a week published before a field existed (the
    sex_gender block, from 2026-08) never had it, and refusing its archive
    would keep the folder complete for good without restoring anything."""
    lost: dict[str, list[str]] = {}
    for nct, entry in archived.items():
        record = source.get(nct)
        for path in paths:
            if sl.missing(entry, path) is not None and sl.missing(record, path) is None:
                lost.setdefault(path, []).append(nct)
    return lost


def _canonical(value: Any) -> Any:
    """Every object's keys in sorted order, lists as they are: a split folder's
    record comes back core fields first (merge_study), a whole part's in the
    extractor's order, and the same record must give the same bytes either way."""
    if isinstance(value, dict):
        return {k: _canonical(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value


def write(doc: dict[str, Any], path: str, summary: Any) -> int:
    """Write the archive file beside path, check it against the summary, and
    only then move it into place. Level 9 and no time in the gzip header, so
    the same records give the same bytes. Returns its size.

    ArchiveError, with nothing moved into place, when the written file does
    not check."""
    tmp = path + ".partial"
    data = gzip.compress(json.dumps(doc, separators=(",", ":")).encode("ascii"), compresslevel=9, mtime=0)
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        found = problems(tmp, summary)
        if found:
            raise ArchiveError(f"the archive file written for {os.path.dirname(path)} does not check: "
                               f"{'; '.join(found)}")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return len(data)


def problems(path: str, summary: Any) -> list[str]:
    """Why the file at path is not this summary's archive file; [] when it is.
    It must be gzipped JSON with exactly the archive header's keys, class
    "archive", the summary's run (source_extracted_at; source_pipeline_commit
    too when the summary has one), and an entry for exactly its recentStudies
    ids, each an object (whose nct_id, when it has one, is its key)."""
    try:
        doc = df.read_gz_json(path)
    except df.FolderError as e:
        return [str(e)]
    name = os.path.basename(path)
    if not isinstance(doc, dict):
        return [f"{name} is not an archive file: its JSON is a {type(doc).__name__}"]
    out: list[str] = []
    keys = list(sl.HEADERS[sl.ARCHIVE_CLASS])
    if sorted(doc) != sorted(keys):
        out.append(f"{name} carries {sorted(doc)}, not {keys}")
    if doc.get("class") != sl.ARCHIVE_CLASS:
        out.append(f"{name} says class {doc.get('class')!r}, not {sl.ARCHIVE_CLASS!r}")
    want_at = summary.get("extracted_at") if isinstance(summary, dict) else None
    want_commit = summary.get("pipeline_commit") if isinstance(summary, dict) else None
    if doc.get("source_extracted_at") != want_at or (want_commit is not None
                                                     and doc.get("source_pipeline_commit") != want_commit):
        out.append(f"{name} comes from another run (source_extracted_at {doc.get('source_extracted_at')!r}, "
                   f"source_pipeline_commit {doc.get('source_pipeline_commit')!r}) than its summary "
                   f"({want_at!r}, {want_commit!r})")
    data = doc.get("data")
    if not isinstance(data, dict):
        return out + [f"{name} holds no object keyed by nct_id"]
    try:
        ids = summary_ids(summary)
    except ArchiveError as e:
        return out + [f"{name} cannot be checked: {e}"]
    id_set = set(ids)
    absent = [nct for nct in ids if nct not in data]
    extra = [nct for nct in data if nct not in id_set]
    if absent or extra:
        out.append(f"{name} does not cover exactly its summary's {len(ids)} recentStudies: {len(absent)} have no "
                   f"entry ({_eg(absent)}), {len(extra)} entries are not theirs ({_eg(extra)})")
    wrong = [nct for nct, entry in data.items()
             if not isinstance(entry, dict) or entry.get("nct_id", nct) != nct]
    if wrong:
        out.append(f"{len(wrong)} entries of {name} are not that study's record ({_eg(wrong)})")
    return out
