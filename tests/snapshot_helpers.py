"""Site checkouts with real, stamped datasets, shared by the retention tests
(test_prune_snapshots.py) and the publish gate's history tests
(test_check_site_contract.py). Not a test module.

A week is a dataset cut by scripts/split_data.py itself from the extractor's
records (split_helpers), stamped with that day's run, with the files that ride
with it: its dashboard-summary.json (every study listed in recentStudies, as
the generator lists the 500 most recent), the sex/gender table and meta, and
the methods text. data/ also holds what is never archived: the frozen March
details, industry_sponsors.json and the sponsor bridge.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import pathlib
import shutil
from typing import Any

import split_helpers as h

RECORDS = 16
IDS = h.ids(RECORDS)
_CUT: dict[tuple[str, bool, str], pathlib.Path] = {}


def stamps(day: str) -> dict[str, str]:
    """The run stamps of the week published on day."""
    return {"extracted_at": f"{day}T06:12:09.454200+00:00",
            "pipeline_commit": f"{int(day.replace('-', '')):040x}"}


def contract(enabled: bool = False) -> dict[str, Any]:
    """The site's record contract (its real classes and layout section)."""
    return h.site_contract(enabled=enabled)


def cut(tmp: pathlib.Path, day: str, split: bool = False, ids: list[str] | None = None) -> pathlib.Path:
    """The dataset files of the week published on day, as split_data.py writes
    them in the weekly job (run.json's snapshot_date is day)."""
    ids = ids or IDS
    key = (day, split, ",".join(ids))
    if key not in _CUT or not _CUT[key].exists():
        _CUT[key] = h.split(tmp / f"cut-{day}-{'split' if split else 'whole'}-{len(ids)}",
                            h.whole_records(ids), contract(split), stamps=stamps(day), snapshot_date=day)
    return _CUT[key]


def summary(day: str, ids: list[str] | None = None) -> dict[str, Any]:
    """The week's summary as the generator writes it: its firstView counted
    from the week's records (the publish gate recounts data/'s)."""
    from src import first_view as fv
    ids = ids or IDS
    s = stamps(day)
    return {**s, "totalStudies": len(ids),
            "recentStudies": [{"nct_id": nct, "brief_title": f"study {i}"} for i, nct in enumerate(ids)],
            "firstView": json.loads(json.dumps(fv.first_view(h.whole_records(ids), s["extracted_at"],
                                                             s["pipeline_commit"])))}


def sex_gender_table(day: str, n: int) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["nct_id", "snapshot_date", "sex_report_status"])
    for nct in IDS[:n]:
        w.writerow([nct, day, "reported"])
    return gzip.compress(buf.getvalue().encode(), mtime=0)


def write_week(tmp: pathlib.Path, folder: pathlib.Path, day: str, split: bool = False,
               ids: list[str] | None = None, extras: bool = True) -> pathlib.Path:
    """Write the week published on day into folder (data/ or snapshots/<day>/):
    its dataset files, its summary, and, with extras, its sex/gender pair and
    methods text."""
    ids = ids or IDS
    folder.mkdir(parents=True, exist_ok=True)
    for old in list(folder.glob("demographics.part*.json.gz")) + list(folder.glob("studies_tab.part*.json.gz")):
        old.unlink()
    shutil.rmtree(folder / "detail", ignore_errors=True)
    (folder / "run.json").unlink(missing_ok=True)
    shutil.copytree(cut(tmp, day, split, ids), folder, dirs_exist_ok=True)
    (folder / "dashboard-summary.json").write_text(json.dumps(summary(day, ids)))
    if extras:
        s = stamps(day)
        (folder / "sex_gender_parsed.csv.gz").write_bytes(sex_gender_table(day, len(ids)))
        (folder / "sex_gender_parsed_meta.json").write_text(json.dumps(
            {"source_extracted_at": s["extracted_at"], "source_pipeline_commit": s["pipeline_commit"],
             "snapshot_date": day, "n_rows": len(ids)}))
        (folder / "sex_gender").mkdir(exist_ok=True)
        (folder / "sex_gender" / "methods.json").write_text(json.dumps(
            {"source_extracted_at": s["extracted_at"], "snapshot_date": day, "sections": []}))
    return folder


def make_site(tmp: pathlib.Path, latest: str, complete: list[str] = (), aggregates: list[str] = (),
              split: bool = False, history: dict[str, Any] | None = None, contract_enabled: bool | None = None,
              name: str = "site") -> pathlib.Path:
    """A site checkout: data/ holds the latest week; snapshots/<d>/ a complete
    week for each date in complete, and a summary-only aggregate for each in
    aggregates; history.json lists them all with the latest (in the format the
    publish step wrote before this change: dates only), unless given."""
    site = tmp / name
    (site / "tests").mkdir(parents=True)
    enabled = split if contract_enabled is None else contract_enabled
    (site / "tests" / "record_contract.json").write_text(json.dumps(contract(enabled)))
    (site / "tests" / "data_budget.json").write_text(json.dumps(
        {"part_count": 8, "part_gzip_max_bytes": 20 * 1024 * 1024, "total_gzip_max_bytes": 160_000_000}))
    write_week(tmp, site / "data", latest, split)
    (site / "data" / "details.part1.json.gz").write_bytes(gzip.compress(b'{"data": []}', mtime=0))
    (site / "data" / "industry_sponsors.json").write_text(json.dumps({"source_extracted_at": stamps(latest)["extracted_at"]}))
    (site / "data" / "sponsors").mkdir()
    (site / "data" / "sponsors" / "bridge.csv.gz").write_bytes(gzip.compress(b"nct_id\n", mtime=0))
    for d in complete:
        write_week(tmp, site / "snapshots" / d, d, split)
    for d in aggregates:
        (site / "snapshots" / d).mkdir(parents=True)
        (site / "snapshots" / d / "dashboard-summary.json").write_text(json.dumps(summary(d)))
    listed = sorted({*complete, *aggregates, latest})
    (site / "history.json").write_text(json.dumps(history if history is not None else {"dates": listed}, indent=2))
    return site


def cut_short_past_its_header(path: pathlib.Path) -> None:
    """A whole-record part as a real one cut short looks: one record padded
    with incompressible text until the part decompresses to four times
    dataset_folder.HEAD_BYTES, then the gzip stream cut at three quarters.
    Its header still reads from the first HEAD_BYTES; only decompressing on to
    the end of the stream finds the damage, as with a 20 MB part. (A fixture
    part of 16 records fails on its first read, and cannot show that.)"""
    import hashlib
    from src import dataset_folder as df
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        body = json.load(fh)
    body["data"][0]["padding"] = "".join(hashlib.sha256(str(i).encode()).hexdigest()
                                         for i in range(4 * df.HEAD_BYTES // 64))
    data = gzip.compress(json.dumps(body).encode(), mtime=0)
    path.write_bytes(data[: len(data) * 3 // 4])
    assert df.read_head(str(path), verify=False)["part"] == body["part"], "the header no longer reads"


def read_history(site: pathlib.Path) -> dict[str, Any]:
    return json.loads((site / "history.json").read_text())


def files(folder: pathlib.Path) -> list[str]:
    """Every file under folder, relative, sorted."""
    return sorted(str(p.relative_to(folder)).replace("\\", "/") for p in folder.rglob("*") if p.is_file())
