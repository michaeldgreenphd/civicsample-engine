"""The publish step's check against the site's own contract.

scripts/check_site_contract.py reads the site's tests/record_contract.json
and tests/data_budget.json and the staged dataset, and decides whether the
weekly push may happen. These tests build small site checkouts and pin the
rules.

Inline parts (no layout block: every file published before the split, and
what the engine writes while the site's switch is off), on a contract with
nested and list paths, a 2-part budget and two parts:

- a record missing a listed path blocks; a null or empty value does not, and
  an optional path need not be there;
- a null or non-object parent counts as missing, as in the site's own test;
- a malformed part, an empty part, a part from another run, bad stamps, a
  duplicate nct_id or the wrong set of parts blocks;
- a missing path is reported once per path with a count;
- over the site's budget only warns; over GitHub's per-file push limit or
  GitHub Pages' size limit blocks;
- an unreadable contract or budget, or a layout version the check does not
  know, blocks, and every outcome writes a report; split-layout files left
  beside whole parts only warn.

A split dataset (layout version 1), cut by scripts/split_data.py itself from
a contract with the split on, in data/ and archived complete in
snapshots/<date>/ (history.json listing both), passes; each blocking rule has
its own case: the handshake with the contract and a contract the layout
cannot follow, the layout block, core records, the studies_tab parts and the
detail shards (among them a file that is not gzipped JSON or not an object,
and an entry that is not an object), run.json, the archived copy (a folder
gone, a shard or run.json missing, a stale shard or studies_tab part, a file
or its summary from another run) and a --latest that is not a date.
Per-class budgets and re-fattened entries only warn.

history.json, on a site as scripts/prune_snapshots.py leaves it (real
datasets, tests/snapshot_helpers.py): it passes; a latest that is not
--latest, not listed or not the newest, a date listed twice or with no
folder or no summary, an aggregate still holding its dataset, an archive file
missing, unreadable, from another run or not covering exactly its summary's
recentStudies, and a complete snapshot missing a file or carrying another
run's stamps (a part, its summary, its sex/gender meta, its methods text)
each block; a folder for the latest date, an unlisted folder, an archive file
history.json does not name and stray files in an aggregate only warn.

extract.yml: retention runs before data/ is replaced and the check after
staging and before the commit and push; nothing writes snapshots/<this
week>/, the sponsor step included; the publish step's own run block is run
under bash -e, with real git on a site repository and stubs for the check,
prune and push, for a whole week, a split week, the rollback from split to
whole parts and a same-day re-run that switches layout, and stages exactly
the dataset's additions and removals; with no dataset to stage it stops
before staging anything; with the real prune and check it archives last
week's dataset, writes history.json and pushes; the run summary's jq reads
the keys the report carries. The raw-measure archive does not depend on the
site push (what it takes is tests/test_raw_measures_archive.py).
"""
from __future__ import annotations

import copy
import gzip
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import Any

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "check_site_contract.py")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_site_contract as csc  # noqa: E402
import prune_snapshots as ps  # noqa: E402
import snapshot_helpers as sh  # noqa: E402
import split_helpers  # noqa: E402
from src import archive_records  # noqa: E402

WORKFLOW = open(os.path.join(ROOT, ".github", "workflows", "extract.yml")).read()

CONTRACT = {
    "classes": {
        "core": ["nct_id", "race.reported", "countries[].country"],
        "studies_tab": ["references[].pmid", "race.raw_categories[].omb_category"],
        "detail": ["status"],
    },
    "optional": ["references[].title"],
    "item_names": {"references": ["ref"]},
    "not_record_fields": {},
}
STAMPS = {"extracted_at": "2026-09-27T11:49:53.609081+00:00", "pipeline_commit": "12b9b652ad5fdf3b05653b30ecf47297ae87a479"}


def _record(n: int) -> dict:
    return {"nct_id": f"NCT{n:08d}", "race": {"reported": True, "raw_categories": [{"omb_category": "white"}]},
            "countries": [{"country": "France"}], "references": [{"pmid": "1"}], "status": "COMPLETED"}


def _site(tmp_path: pathlib.Path, records: tuple[list[dict], list[dict]] | None = None,
          budget: dict | None = None, contract: dict | None = None) -> pathlib.Path:
    site = tmp_path / "site"
    (site / "tests").mkdir(parents=True)
    (site / "data").mkdir()
    (site / "tests" / "record_contract.json").write_text(json.dumps(contract or CONTRACT))
    (site / "tests" / "data_budget.json").write_text(json.dumps(
        budget or {"part_count": 2, "part_gzip_max_bytes": 20 * 1024 * 1024, "total_gzip_max_bytes": 40 * 1024 * 1024}))
    records = records or ([_record(1), _record(2)], [_record(3)])
    for i, recs in enumerate(records, start=1):
        _write_part(site, i, {**STAMPS, "part": i, "total_parts": len(records), "data": recs})
    return site


def _write_part(site: pathlib.Path, i: int, body: dict) -> None:
    with gzip.open(site / "data" / f"demographics.part{i}.json.gz", "wt") as f:
        json.dump(body, f)


def _archive_copy(site: pathlib.Path, day: str, stamps: dict | None = None) -> pathlib.Path:
    """data/'s dataset archived as snapshots/<day>/, as prune_snapshots.py
    archives it: its dataset files and a summary of its run."""
    folder = site / "snapshots" / day
    folder.mkdir(parents=True)
    for rel in csc.dataset_files(str(site / "data")):
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(site / "data" / rel, folder / rel)
    (folder / "dashboard-summary.json").write_text(json.dumps({**(stamps or STAMPS), "recentStudies": []}))
    return folder


def _history(site: pathlib.Path, dates: list[str], latest: str, archives: dict | None = None) -> None:
    (site / "history.json").write_text(json.dumps({"dates": dates, "latest": latest, "archives": archives or {}}))


def _check(site: pathlib.Path, *args: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    report = site.parent / "report.json"
    report.unlink(missing_ok=True)
    r = subprocess.run([sys.executable, SCRIPT, "--site", str(site), "--report", str(report), *args],
                       capture_output=True, text=True)
    return r, json.loads(report.read_text())


def test_a_site_that_keeps_its_contract_passes(tmp_path: pathlib.Path) -> None:
    r, report = _check(_site(tmp_path))
    assert r.returncode == 0, r.stdout
    assert report["ok"] and report["records"] == 3 and report["errors"] == [] and report["warnings"] == []
    assert "site contract check: ok" in r.stdout


def test_null_and_empty_values_are_present_and_optional_paths_may_be_absent(tmp_path: pathlib.Path) -> None:
    rec = _record(1)
    rec["status"] = None
    rec["countries"] = []
    rec["references"] = [{"pmid": "1"}]          # no title: optional
    r, report = _check(_site(tmp_path, records=([rec], [_record(2)])))
    assert r.returncode == 0, report["errors"]


@pytest.mark.parametrize("damage,path", [
    (lambda rec: rec.pop("status"), "status"),
    (lambda rec: rec["race"].pop("reported"), "race.reported"),
    (lambda rec: rec["countries"].append({}), "countries[].country"),
    (lambda rec: rec["race"]["raw_categories"][0].pop("omb_category"), "race.raw_categories[].omb_category"),
    (lambda rec: rec.update(references={"pmid": "1"}), "references[].pmid"),
])
def test_a_record_missing_a_listed_path_blocks_the_push(tmp_path: pathlib.Path, damage: Callable[[dict], object], path: str) -> None:
    rec = _record(3)
    damage(rec)
    r, report = _check(_site(tmp_path, records=([_record(1), _record(2)], [rec])))
    assert r.returncode == 1
    assert any(e.startswith(f"{path}: missing in 1 record") and "NCT00000003 in demographics.part2.json.gz" in e
               for e in report["errors"]), report["errors"]
    assert report["missing_by_path"][path]["count"] == 1
    assert f"::error::{path}: missing in 1 record" in r.stdout


@pytest.mark.parametrize("damage,path,why", [
    (lambda rec: rec.update(race=None), "race.reported", "no reported"),
    (lambda rec: rec.update(race=[]), "race.reported", "no reported"),
    (lambda rec: rec.update(countries=None), "countries[].country", "countries is not a list"),
    (lambda rec: rec.update(countries={"country": "France"}), "countries[].country", "countries is not a list"),
])
def test_a_null_or_wrong_shaped_parent_counts_as_missing(tmp_path: pathlib.Path, damage: Callable[[dict], object],
                                                          path: str, why: str) -> None:
    rec = _record(3)
    damage(rec)
    r, report = _check(_site(tmp_path, records=([_record(1)], [rec])))
    assert r.returncode == 1
    assert report["missing_by_path"][path]["why"] == why


def test_the_path_rules_are_the_site_tests_own() -> None:
    """The cases the site's study_record_contract.test.mjs pins for its missing()."""
    r = {"a": {"b": []}, "c": [{"d": None}, {"d": 1}], "e": None}
    assert csc.missing(r, "a.b") is None
    assert csc.missing(r, "a.b[].x") is None, "an empty list has nothing to check"
    assert csc.missing(r, "c[].d") is None, "null is a value"
    assert csc.missing(r, "e") is None
    assert csc.missing(r, "f") == "no f"
    assert csc.missing({"c": [{"d": 1}, {}]}, "c[].d") == "no d"
    assert csc.missing({"c": {}}, "c[].d") == "c is not a list"
    assert csc.missing({"e": None}, "e.x") == "no x", "a null parent is missing its keys"


def test_a_field_gone_from_every_record_is_one_error_with_its_count(tmp_path: pathlib.Path) -> None:
    recs = [_record(i) for i in range(1, 7)]
    for rec in recs:
        rec.pop("status")
    r, report = _check(_site(tmp_path, records=(recs[:3], recs[3:])))
    assert r.returncode == 1
    status_errors = [e for e in report["errors"] if e.startswith("status:")]
    assert status_errors == [status_errors[0]] and "missing in 6 records" in status_errors[0]
    assert report["error_count"] == len(report["errors"]) == 1


def test_a_part_that_is_not_gzipped_json_blocks(tmp_path: pathlib.Path) -> None:
    site = _site(tmp_path)
    (site / "data" / "demographics.part2.json.gz").write_bytes(b"not gzip")
    r, report = _check(site)
    assert r.returncode == 1 and any("part2.json.gz is not gzipped JSON" in e for e in report["errors"])


def test_a_part_that_is_json_but_not_an_object_blocks_with_a_report(tmp_path: pathlib.Path) -> None:
    site = _site(tmp_path)
    with gzip.open(site / "data" / "demographics.part2.json.gz", "wt") as f:
        json.dump([_record(3)], f)
    r, report = _check(site)
    assert r.returncode == 1 and any("part2.json.gz is not a part" in e for e in report["errors"]), r.stderr


@pytest.mark.parametrize("data", [[], None, {"NCT00000003": {}}], ids=["empty", "absent", "not-a-list"])
def test_a_part_with_no_records_blocks(tmp_path: pathlib.Path, data: object) -> None:
    site = _site(tmp_path)
    body = {**STAMPS, "part": 2, "total_parts": 2}
    if data is not None:
        body["data"] = data
    _write_part(site, 2, body)
    r, report = _check(site)
    assert r.returncode == 1 and any("demographics.part2.json.gz has no records" in e for e in report["errors"])


def test_an_empty_week_blocks(tmp_path: pathlib.Path) -> None:
    site = _site(tmp_path)
    for i in (1, 2):
        _write_part(site, i, {**STAMPS, "part": i, "total_parts": 2, "data": []})
    r, report = _check(site)
    assert r.returncode == 1 and report["records"] == 0


@pytest.mark.parametrize("change,message", [
    ({"extracted_at": "2026-09-20T11:05:33+00:00"}, "comes from another run than part 1"),
    ({"pipeline_commit": "0000000"}, "comes from another run than part 1"),
    ({"part": 1}, "says it is part 1 of 2, not 2 of 2"),
    ({"total_parts": 3}, "says it is part 2 of 3, not 2 of 2"),
])
def test_a_part_from_another_run_or_with_a_wrong_header_blocks(tmp_path: pathlib.Path, change: dict, message: str) -> None:
    site = _site(tmp_path)
    _write_part(site, 2, {**STAMPS, "part": 2, "total_parts": 2, "data": [_record(3)], **change})
    r, report = _check(site)
    assert r.returncode == 1 and any(message in e for e in report["errors"]), report["errors"]


@pytest.mark.parametrize("stamps,message", [
    ({"extracted_at": None}, "extracted_at is not a timestamp"),
    ({"extracted_at": "last Sunday"}, "extracted_at is not a timestamp"),
    ({"pipeline_commit": ""}, "pipeline_commit is not a commit id"),
])
def test_part_one_must_carry_real_stamps(tmp_path: pathlib.Path, stamps: dict, message: str) -> None:
    site = _site(tmp_path)
    for i, recs in ((1, [_record(1)]), (2, [_record(2)])):
        _write_part(site, i, {**STAMPS, **stamps, "part": i, "total_parts": 2, "data": recs})
    r, report = _check(site)
    assert r.returncode == 1 and any(message in e for e in report["errors"]), report["errors"]


def test_a_trial_in_two_records_blocks(tmp_path: pathlib.Path) -> None:
    r, report = _check(_site(tmp_path, records=([_record(1)], [_record(1)])))
    assert r.returncode == 1 and any("NCT00000001 appears in more than one record" in e for e in report["errors"])


def test_a_missing_or_extra_part_blocks(tmp_path: pathlib.Path) -> None:
    site = _site(tmp_path)
    (site / "data" / "demographics.part2.json.gz").unlink()
    r, report = _check(site)
    assert r.returncode == 1 and any("not the 2 parts the site fetches" in e for e in report["errors"])
    site = _site(tmp_path / "again")
    _write_part(site, 3, {**STAMPS, "part": 3, "total_parts": 3, "data": [_record(9)]})
    r, report = _check(site)
    assert r.returncode == 1 and any("not the 2 parts the site fetches" in e for e in report["errors"])


def test_over_the_size_budget_only_warns(tmp_path: pathlib.Path) -> None:
    site = _site(tmp_path, budget={"part_count": 2, "part_gzip_max_bytes": 10, "total_gzip_max_bytes": 20})
    r, report = _check(site)
    assert r.returncode == 0, report["errors"]
    assert any("over the site's per-part budget" in w for w in report["warnings"])
    assert any("over the site's budget" in w for w in report["warnings"])
    assert "::warning::" in r.stdout


def test_over_githubs_per_file_push_limit_blocks(tmp_path: pathlib.Path) -> None:
    assert csc.PART_HARD_LIMIT_BYTES == 100 * 1024 * 1024, "the hard limit is GitHub's 100 MiB per-file push limit"
    r, report = _check(_site(tmp_path), "--part-hard-limit-bytes", "10")
    assert r.returncode == 1 and any("over GitHub's per-file push limit" in e for e in report["errors"])


def test_the_published_site_size_warns_near_and_blocks_over_the_pages_limit(tmp_path: pathlib.Path) -> None:
    site = _site(tmp_path)
    (site / "snapshots").mkdir()
    (site / "snapshots" / "big.bin").write_bytes(b"x" * 5000)
    (site / ".git").mkdir()
    (site / ".git" / "pack").write_bytes(b"x" * 100000)          # not published
    r, report = _check(site, "--tree-warn-bytes", "4000", "--tree-hard-limit-bytes", "90000")
    assert r.returncode == 0 and any("close to GitHub Pages' limit" in w for w in report["warnings"])
    assert report["tree_bytes"] < 90000, "the .git directory was counted"
    r, report = _check(site, "--tree-hard-limit-bytes", "4000")
    assert r.returncode == 1 and any("over GitHub Pages' limit" in e for e in report["errors"])


@pytest.mark.parametrize("broken", ["record_contract.json", "data_budget.json"])
def test_an_unreadable_contract_or_budget_blocks(tmp_path: pathlib.Path, broken: str) -> None:
    site = _site(tmp_path)
    (site / "tests" / broken).write_text("{not json")
    r, report = _check(site)
    assert r.returncode == 1 and any("cannot read the site's record contract or data budget" in e for e in report["errors"])
    (site / "tests" / broken).unlink()
    r, report = _check(site)
    assert r.returncode == 1 and any("cannot read the site's record contract or data budget" in e for e in report["errors"])




def _read(path: pathlib.Path) -> Any:
    with gzip.open(path, "rt") as f:
        return json.load(f)


def _write(path: pathlib.Path, body: Any) -> None:
    with gzip.open(path, "wt") as f:
        json.dump(body, f)


# ── inline parts and the contract's layout section ──────────────────────────

def test_a_contract_layout_this_check_does_not_know_blocks_even_with_inline_parts(tmp_path: pathlib.Path) -> None:
    r, report = _check(_site(tmp_path, contract={**CONTRACT, "layout": {"version": 2, "enabled": False}}))
    assert r.returncode == 1 and any("layout section is version 2" in e for e in report["errors"]), report["errors"]


def test_inline_parts_under_a_layout_that_is_off_pass(tmp_path: pathlib.Path) -> None:
    r, report = _check(_site(tmp_path, contract={**CONTRACT, "layout": {"version": 1, "enabled": False}}))
    assert r.returncode == 0 and report["warnings"] == [], report
    assert report["layout_version"] is None and report["classes"]["inline"]["files"] == 2


def test_split_files_beside_inline_parts_and_a_switch_left_on_only_warn(tmp_path: pathlib.Path) -> None:
    site = _site(tmp_path, contract={**CONTRACT, "layout": {"version": 1, "enabled": True}})
    _archive_copy(site, "2026-10-11")
    (site / "snapshots" / "2026-10-11" / "studies_tab.part1.json.gz").write_bytes(b"x")
    (site / "data" / "detail").mkdir()
    (site / "data" / "detail" / "0.json.gz").write_bytes(b"x")
    _history(site, ["2026-10-11", "2026-10-18"], "2026-10-18")
    r, report = _check(site, "--latest", "2026-10-18")
    assert r.returncode == 0, report["errors"]
    assert any("data/ holds split-layout files" in w and "detail/0.json.gz" in w for w in report["warnings"])
    assert any("snapshots/2026-10-11/ holds split-layout files" in w for w in report["warnings"])
    assert any("turns the split layout on, but this week's parts carry whole records" in w for w in report["warnings"])


# ── a split dataset ─────────────────────────────────────────────────────────

SPLIT_CONTRACT = {
    "classes": {
        "core": ["nct_id", "race.reported", "countries[].country", "study_sites[].country"],
        "studies_tab": ["references[].pmid", "race.raw_categories[].omb_category"],
        "detail": ["status", "study_sites[].facility"],
    },
    "optional": ["references[].title"],
    "item_names": {"references": ["ref"], "study_sites": ["site"]},
    "not_record_fields": {},
    "layout": {"version": 1, "enabled": True,
               "detail": {"key": "nct_number_mod", "shards": 4, "whole_lists": ["study_sites"]}},
}
SPLIT_BUDGET = {"part_count": 8, "part_gzip_max_bytes": 20 * 1024 * 1024, "total_gzip_max_bytes": 160_000_000,
                "classes": {"studies_tab": {"file_gzip_max_bytes": 3_600_000, "total_gzip_max_bytes": 28_500_000},
                            "detail": {"file_gzip_max_bytes": 400_000, "total_gzip_max_bytes": 71_500_000}}}
DATE = "2026-10-11"              # an archived complete snapshot
LATEST = "2026-10-18"            # the date data/ serves
CORE_PARTS = [f"demographics.part{k}.json.gz" for k in range(1, 9)]


def _split_record(n: int) -> dict:
    rec = _record(n)
    rec["study_sites"] = [{"facility": f"Clinic {n}", "country": "France"}]
    rec["keywords"] = ["a field no class reads"]
    return rec


def _split_site(tmp_path: pathlib.Path, contract: dict | None = None, budget: dict | None = None,
                records: list[dict] | None = None, site_contract: dict | None = None) -> pathlib.Path:
    """A site checkout holding a dataset that scripts/split_data.py cut from
    `contract`, in data/ (served as LATEST) and archived complete as
    snapshots/DATE/ with its summary, history.json listing both; the site's
    contract is `site_contract` (default: the same)."""
    contract = copy.deepcopy(contract or SPLIT_CONTRACT)
    dataset = split_helpers.split(tmp_path / "engine", records or [_split_record(n) for n in range(1, 17)],
                                  contract, stamps=STAMPS)
    site = tmp_path / "site"
    (site / "tests").mkdir(parents=True)
    (site / "tests" / "record_contract.json").write_text(json.dumps(site_contract or contract))
    (site / "tests" / "data_budget.json").write_text(json.dumps(budget or SPLIT_BUDGET))
    shutil.copytree(dataset, site / "data")
    shutil.copytree(dataset, site / "snapshots" / DATE)
    (site / "snapshots" / DATE / "dashboard-summary.json").write_text(json.dumps({**STAMPS, "recentStudies": []}))
    _history(site, [DATE, LATEST], LATEST)
    (site / "data" / "details.part1.json.gz").write_bytes(b"the frozen March details: not a dataset file")
    return site


def _both(site: pathlib.Path, change: Callable[[pathlib.Path], object]) -> None:
    """Apply a change to the dataset in data/ and in its archived copy."""
    for folder in (site / "data", site / "snapshots" / DATE):
        change(folder)


def _check_split(site: pathlib.Path, *args: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    return _check(site, "--latest", LATEST, *args)


def test_a_split_dataset_that_keeps_its_contract_passes(tmp_path: pathlib.Path) -> None:
    site = _split_site(tmp_path)
    r, report = _check_split(site)
    assert r.returncode == 0, report["errors"]
    assert report["ok"] and report["errors"] == [] and report["warnings"] == [], report
    assert report["layout_version"] == 1 and report["records"] == 16
    classes = report["classes"]
    assert {c: (classes[c]["files"], classes[c]["records"]) for c in classes} == \
        {"core": (8, 16), "studies_tab": (8, 16), "detail": (4, 16)}
    assert classes["detail"]["gzip_bytes"] == sum(p.stat().st_size for p in (site / "data" / "detail").iterdir())
    assert report["history"] == {"latest": LATEST, "dates": 2, "complete": [DATE], "aggregates": [], "archive_files": []}
    assert "split: studies_tab" in r.stdout and "detail" in r.stdout


def test_the_engines_split_of_the_sites_own_contract_passes(tmp_path: pathlib.Path) -> None:
    """The site's real classes, whole lists and 256 shards, on the extractor's records."""
    contract = split_helpers.site_contract(enabled=True)
    records = split_helpers.whole_records(split_helpers.ids(16))
    site = _split_site(tmp_path, contract=contract, records=records)
    r, report = _check_split(site)
    assert r.returncode == 0, report["errors"]
    assert report["errors"] == [] and report["classes"]["detail"]["files"] == 256
    assert report["contract_paths"] == sum(len(v) for v in contract["classes"].values())


def _layout(site: pathlib.Path) -> dict:
    return _read(site / "data" / "demographics.part1.json.gz")["layout"]


def _set_layout(layout: dict | None) -> Callable[[pathlib.Path], object]:
    def change(folder: pathlib.Path) -> None:
        for name in CORE_PARTS:
            body = _read(folder / name)
            if layout is None:
                body.pop("layout")
            else:
                body["layout"] = layout
            _write(folder / name, body)
    return change


def _first_id(site: pathlib.Path, rel: str) -> str:
    return next(iter(_read(site / "data" / rel)["data"]))


def _move_entry(src: int, dst: int, keep: bool) -> Callable[[pathlib.Path], object]:
    def change(folder: pathlib.Path) -> None:
        a, b = _read(folder / f"detail/{src}.json.gz"), _read(folder / f"detail/{dst}.json.gz")
        nct = next(iter(a["data"]))
        b["data"][nct] = a["data"][nct] if keep else a["data"].pop(nct)
        _write(folder / f"detail/{src}.json.gz", a)
        _write(folder / f"detail/{dst}.json.gz", b)
    return change


def _gz_change(rel: str, change: Callable[[Any], object]) -> Callable[[pathlib.Path], object]:
    def apply(folder: pathlib.Path) -> None:
        body = _read(folder / rel)
        change(body)
        _write(folder / rel, body)
    return apply


def _run_json(change: Callable[[dict], object]) -> Callable[[pathlib.Path], object]:
    def apply(folder: pathlib.Path) -> None:
        run = json.loads((folder / "run.json").read_text())
        change(run)
        (folder / "run.json").write_text(json.dumps(run))
    return apply


def _first_entry(body: dict) -> dict:
    return next(iter(body["data"].values()))


# Each blocking rule of a split dataset: (change to the site, a phrase the error says).
SPLIT_BLOCKS: dict[str, tuple[Callable[[pathlib.Path], object], str]] = {
    "the contract does not turn the split on": (
        lambda s: (s / "tests" / "record_contract.json").write_text(
            json.dumps({**SPLIT_CONTRACT, "layout": {**SPLIT_CONTRACT["layout"], "enabled": False}})),
        "does not turn it on"),
    "the contract has no layout section": (
        lambda s: (s / "tests" / "record_contract.json").write_text(
            json.dumps({k: v for k, v in SPLIT_CONTRACT.items() if k != "layout"})),
        "does not turn it on"),
    "the parts carry a layout version the check does not know": (
        lambda s: _both(s, _set_layout({**_layout(s), "version": 2})), "this check knows inline parts"),
    "a part without the layout": (
        lambda s: _both(s, _gz_change("demographics.part3.json.gz", lambda b: b.pop("layout"))),
        "carries layout none"),
    "a part with another layout": (
        lambda s: _both(s, _gz_change("demographics.part2.json.gz",
                                      lambda b: b["layout"]["detail"].update(shards=8))),
        "the parts of one dataset share one layout"),
    "studies_tab files that are not the part count": (
        lambda s: _both(s, _set_layout({**_layout(s), "studies_tab": {"files": 7}})),
        "the site's contract and part count give"),
    "a shard count the contract does not give": (
        lambda s: (s / "tests" / "record_contract.json").write_text(json.dumps(
            {**SPLIT_CONTRACT, "layout": {**SPLIT_CONTRACT["layout"],
                                          "detail": {**SPLIT_CONTRACT["layout"]["detail"], "shards": 8}}})),
        "the site's contract and part count give"),
    "another shard key": (
        lambda s: _both(s, _set_layout({**_layout(s), "detail": {"shards": 4, "key": "nct_mod"}})),
        "the site's contract and part count give"),
    "a site contract the split layout cannot follow": (
        lambda s: (s / "tests" / "record_contract.json").write_text(json.dumps(
            {**SPLIT_CONTRACT, "classes": {**SPLIT_CONTRACT["classes"],
                                           "detail": SPLIT_CONTRACT["classes"]["detail"] + ["references[].type"]}})),
        "the parts are split, and the site's record contract cannot be checked against the split layout"),
    "a core record without a core field": (
        lambda s: _both(s, _gz_change("demographics.part2.json.gz", lambda b: b["data"][0]["study_sites"][0].pop("country"))),
        "study_sites[].country: missing in 1 record"),
    "a core record whose nct_id has no shard": (
        lambda s: _both(s, _gz_change("demographics.part2.json.gz", lambda b: b["data"][0].update(nct_id="NCT123"))),
        "nct_id is not NCT + 8 digits"),
    "a studies_tab part missing": (
        lambda s: _both(s, lambda f: (f / "studies_tab.part3.json.gz").unlink()), "data/ holds studies_tab parts"),
    "an extra studies_tab part": (
        lambda s: _both(s, lambda f: shutil.copy(f / "studies_tab.part8.json.gz", f / "studies_tab.part9.json.gz")),
        "data/ holds studies_tab parts"),
    "a studies_tab part from another run": (
        lambda s: _both(s, _gz_change("studies_tab.part3.json.gz", lambda b: b.update(extracted_at="2026-10-04T06:00:00+00:00"))),
        "studies_tab.part3.json.gz comes from another run than core part 1"),
    "a studies_tab part that says it is another": (
        lambda s: _both(s, _gz_change("studies_tab.part4.json.gz", lambda b: b.update(part=5))),
        "studies_tab.part4.json.gz says"),
    "a studies_tab part of another class": (
        lambda s: _both(s, _gz_change("studies_tab.part4.json.gz", lambda b: b.update({"class": "detail"}))),
        "studies_tab.part4.json.gz says"),
    "a studies_tab part whose data is a list": (
        lambda s: _both(s, _gz_change("studies_tab.part5.json.gz", lambda b: b.update(data=list(b["data"].values())))),
        "holds no object keyed by nct_id"),
    "a studies_tab part that is not gzipped JSON": (
        lambda s: _both(s, lambda f: (f / "studies_tab.part3.json.gz").write_bytes(b"not gzip")),
        "studies_tab.part3.json.gz is not gzipped JSON"),
    "a studies_tab part whose JSON is not an object": (
        lambda s: _both(s, lambda f: _write(f / "studies_tab.part4.json.gz", list(_read(f / "studies_tab.part4.json.gz")))),
        "studies_tab.part4.json.gz is not a studies_tab file: its JSON is a list"),
    "a studies_tab entry that is not an object": (
        lambda s: _both(s, _gz_change("studies_tab.part1.json.gz", lambda b: b["data"].update({next(iter(b["data"])): None}))),
        "studies_tab entries that are not objects"),
    "a studies_tab part without one of its core part's records": (
        lambda s: _both(s, _gz_change("studies_tab.part2.json.gz", lambda b: b["data"].pop(next(iter(b["data"]))))),
        "does not hold exactly core part 2's records"),
    "a studies_tab part holding another part's record": (
        lambda s: _both(s, _gz_change("studies_tab.part2.json.gz", lambda b: b["data"].update(NCT00000001={"references": []}))),
        "does not hold exactly core part 2's records"),
    "a studies_tab entry without a studies_tab field": (
        lambda s: _both(s, _gz_change("studies_tab.part6.json.gz", lambda b: _first_entry(b).pop("references"))),
        "references[].pmid: missing in 1 record"),
    "a detail shard missing": (
        lambda s: _both(s, lambda f: (f / "detail" / "2.json.gz").unlink()), "lacks 1 of the 4 detail shards"),
    "a stray file in detail/": (
        lambda s: _both(s, lambda f: (f / "detail" / "index.json").write_text("{}")), "files the layout does not name"),
    "a stray folder in detail/": (
        lambda s: _both(s, lambda f: (f / "detail" / "old").mkdir() or shutil.copy(f / "detail" / "0.json.gz", f / "detail" / "old" / "0.json.gz")),
        "files the layout does not name"),
    "a detail shard from another run": (
        lambda s: _both(s, _gz_change("detail/1.json.gz", lambda b: b.update(pipeline_commit="0000000"))),
        "detail/1.json.gz comes from another run than core part 1"),
    "a detail shard that says it is another": (
        lambda s: _both(s, _gz_change("detail/1.json.gz", lambda b: b.update(shard=2))), "detail/1.json.gz says"),
    "a detail shard with another count": (
        lambda s: _both(s, _gz_change("detail/1.json.gz", lambda b: b.update(shards=8))), "detail/1.json.gz says"),
    "a detail shard with another key": (
        lambda s: _both(s, _gz_change("detail/1.json.gz", lambda b: b.update(key="nct_mod"))), "detail/1.json.gz says"),
    "a truncated detail shard": (
        lambda s: _both(s, lambda f: (f / "detail" / "1.json.gz").write_bytes(
            (f / "detail" / "1.json.gz").read_bytes()[:-12])),
        "detail/1.json.gz is not gzipped JSON"),
    "a detail shard whose JSON is not an object": (
        lambda s: _both(s, lambda f: _write(f / "detail" / "1.json.gz", list(_read(f / "detail" / "1.json.gz")))),
        "detail/1.json.gz is not a detail file: its JSON is a list"),
    "a record in another record's shard": (
        lambda s: _both(s, _move_entry(1, 2, keep=False)), "detail entries outside their shard"),
    "a record in two shards": (
        lambda s: _both(s, _move_entry(1, 2, keep=True)), "an entry in more than one detail shard"),
    "a core record without a detail entry": (
        lambda s: _both(s, _gz_change("detail/3.json.gz", lambda b: b["data"].pop(next(iter(b["data"]))))),
        "core records have no detail entry"),
    "a detail entry for no core record": (
        lambda s: _both(s, _gz_change("detail/0.json.gz", lambda b: b["data"].update(
            NCT09999996={"status": "COMPLETED", "study_sites": []}))),
        "detail entries for no core record"),
    "a detail entry without a detail field": (
        lambda s: _both(s, _gz_change("detail/0.json.gz", lambda b: _first_entry(b).pop("status"))),
        "status: missing in 1 record"),
    "a detail entry that is not an object": (
        lambda s: _both(s, _gz_change("detail/0.json.gz", lambda b: b["data"].update({next(iter(b["data"])): None}))),
        "detail entries that are not objects"),
    "a detail entry whose sites lost their country": (
        lambda s: _both(s, _gz_change("detail/0.json.gz", lambda b: _first_entry(b)["study_sites"][0].pop("country"))),
        "study_sites[].country: missing in 1 record"),
    "no run.json": (lambda s: _both(s, lambda f: (f / "run.json").unlink()), "data/run.json cannot be read"),
    "run.json from another run": (
        lambda s: _both(s, _run_json(lambda r: r.update(extracted_at="2026-10-04T06:00:00+00:00"))),
        "data/run.json says extracted_at"),
    "run.json from another commit": (
        lambda s: _both(s, _run_json(lambda r: r.update(pipeline_commit="0000000"))),
        "data/run.json says pipeline_commit"),
    "run.json with another record count": (
        lambda s: _both(s, _run_json(lambda r: r.update(studies=15))), "data/run.json says studies"),
    "run.json with another part count": (
        lambda s: _both(s, _run_json(lambda r: r.update(total_parts=7))), "data/run.json says total_parts"),
    "run.json without the layout": (
        lambda s: _both(s, _run_json(lambda r: r.pop("layout"))), "data/run.json says layout"),
    "no snapshot folder": (lambda s: shutil.rmtree(s / "snapshots" / DATE),
                           f"history.json lists {DATE}, but snapshots/{DATE}/ is not there"),
    "a snapshot without a shard": (
        lambda s: (s / "snapshots" / DATE / "detail" / "3.json.gz").unlink(),
        f"snapshots/{DATE}/ is listed as a complete snapshot but lacks 1 of its 20 dataset files (detail/3.json.gz)"),
    "a snapshot without run.json": (
        lambda s: (s / "snapshots" / DATE / "run.json").unlink(), "but no run.json"),
    "a snapshot with a stale shard": (
        lambda s: shutil.copy(s / "snapshots" / DATE / "detail" / "0.json.gz", s / "snapshots" / DATE / "detail" / "4.json.gz"),
        "holds dataset files its layout does not name (detail/4.json.gz)"),
    "a snapshot with a stale studies_tab part": (
        lambda s: shutil.copy(s / "snapshots" / DATE / "studies_tab.part1.json.gz",
                              s / "snapshots" / DATE / "studies_tab.part9.json.gz"),
        "holds dataset files its layout does not name (studies_tab.part9.json.gz)"),
    "a snapshot file from another run": (
        lambda s: _gz_change("studies_tab.part1.json.gz", lambda b: b.update(extracted_at="2026-10-04T06:00:00+00:00"))(
            s / "snapshots" / DATE),
        "studies_tab.part1.json.gz comes from another run than core part 1"),
    "a truncated snapshot shard": (
        lambda s: (s / "snapshots" / DATE / "detail" / "2.json.gz").write_bytes(
            (s / "snapshots" / DATE / "detail" / "2.json.gz").read_bytes()[:-12]),
        "detail/2.json.gz is not gzipped JSON"),
    "a snapshot summary from another run": (
        lambda s: (s / "snapshots" / DATE / "dashboard-summary.json").write_text(
            json.dumps({**STAMPS, "extracted_at": "2026-10-04T06:00:00+00:00"})),
        "dashboard-summary.json comes from another run"),
}


@pytest.mark.parametrize("name", list(SPLIT_BLOCKS))
def test_each_split_layout_rule_blocks_the_push(tmp_path: pathlib.Path, name: str) -> None:
    damage, message = SPLIT_BLOCKS[name]
    site = _split_site(tmp_path)
    damage(site)
    r, report = _check_split(site)
    assert r.returncode == 1, f"{name} did not block"
    assert any(message in e for e in report["errors"]), report["errors"]
    assert "::error::" in r.stdout and not report["ok"]


def test_a_latest_that_is_not_a_date_blocks(tmp_path: pathlib.Path) -> None:
    """--latest is the date data/ serves, a YYYY-MM-DD date and nothing else."""
    site = _split_site(tmp_path)
    for bad in (f"../{LATEST}", "2026-10-32", "latest"):
        r, report = _check(site, "--latest", bad)
        assert r.returncode == 1
        assert report["errors"] == [f"--latest {bad!r} is not a date (YYYY-MM-DD)"], report["errors"]
    r, report = _check(site, "--latest", LATEST)
    assert r.returncode == 0, report["errors"]


def test_a_split_file_over_githubs_per_file_push_limit_blocks(tmp_path: pathlib.Path) -> None:
    site = _split_site(tmp_path)
    r, report = _check_split(site, "--part-hard-limit-bytes", "10")
    assert r.returncode == 1
    for name in ("demographics.part1.json.gz", "studies_tab.part1.json.gz", "detail/0.json.gz"):
        assert any(e.startswith(f"{name} is") and "per-file push limit" in e for e in report["errors"]), name


def test_over_the_per_class_budgets_only_warns(tmp_path: pathlib.Path) -> None:
    tight = {**SPLIT_BUDGET, "classes": {"studies_tab": {"file_gzip_max_bytes": 10, "total_gzip_max_bytes": 20},
                                         "detail": {"file_gzip_max_bytes": 10, "total_gzip_max_bytes": 20}}}
    r, report = _check_split(_split_site(tmp_path, budget=tight))
    assert r.returncode == 0, report["errors"]
    for phrase in ("over the site's studies_tab budget of 10 a file", "the studies_tab files total",
                   "over the site's detail budget of 10 a file", "the detail files total"):
        assert any(phrase in w for w in report["warnings"]), phrase
    assert report["classes"]["detail"]["file_budget"] == 10


def test_without_per_class_budgets_the_sizes_are_reported_only(tmp_path: pathlib.Path) -> None:
    budget = {k: v for k, v in SPLIT_BUDGET.items() if k != "classes"}
    r, report = _check_split(_split_site(tmp_path, budget=budget))
    assert r.returncode == 0 and any("no per-class budgets" in w for w in report["warnings"])
    assert report["classes"]["studies_tab"]["total_budget"] is None and report["classes"]["studies_tab"]["gzip_bytes"] > 0


def test_fields_a_class_does_not_read_only_warn(tmp_path: pathlib.Path) -> None:
    site = _split_site(tmp_path)
    _both(site, _gz_change("studies_tab.part1.json.gz", lambda b: [e.update(keywords=["x"]) for e in b["data"].values()]))
    _both(site, _gz_change("demographics.part1.json.gz", lambda b: b["data"][0].update(status="COMPLETED")))
    _both(site, _run_json(lambda r: r["gzip_bytes"].update(core=1)))
    r, report = _check_split(site)
    assert r.returncode == 0, report["errors"]
    assert any(w.startswith("studies_tab entries carry keywords") and ": 2;" in w for w in report["warnings"])
    assert any(w.startswith("core records carry status") for w in report["warnings"])
    assert any("data/run.json gives gzip_bytes" in w for w in report["warnings"])


def test_the_frozen_march_files_beside_a_split_dataset_are_not_dataset_files(tmp_path: pathlib.Path) -> None:
    site = _split_site(tmp_path)
    assert (site / "data" / "details.part1.json.gz").exists()
    assert "details.part1.json.gz" not in csc.dataset_files(str(site / "data"))
    r, report = _check_split(site)
    assert r.returncode == 0, report["errors"]


# ── history.json and the archived snapshots ─────────────────────────────────

RETAINED = "2026-10-11"
SLIMMED = "2026-05-31"


def _retained_site(tmp_path: pathlib.Path) -> pathlib.Path:
    """The site of 2026-10-04 after the next week's publish, as the publish
    step leaves it: retention run (May's 05-31 slimmed into an aggregate with
    its own records; 06-14, 08-02 and 10-04 kept complete), then the 2026-10-11
    week in data/. Real datasets (tests/snapshot_helpers.py)."""
    site = sh.make_site(tmp_path, "2026-10-04", ["2026-05-31", "2026-06-14", "2026-08-02", "2026-10-04"],
                        ["2026-02-22", "2026-03-29", "2026-04-26"])
    assert ps.run(str(site), RETAINED, None, dry=False) is not None
    sh.write_week(tmp_path, site / "data", RETAINED)
    return site


def test_a_site_as_retention_leaves_it_passes(tmp_path: pathlib.Path) -> None:
    site = _retained_site(tmp_path)
    r, report = _check(site, "--latest", RETAINED)
    assert r.returncode == 0, report["errors"]
    assert report["warnings"] == [], report["warnings"]
    assert report["history"] == {"latest": RETAINED, "dates": 8, "complete": ["2026-06-14", "2026-08-02", "2026-10-04"],
                                 "aggregates": ["2026-02-22", "2026-03-29", "2026-04-26", SLIMMED],
                                 "archive_files": [SLIMMED]}
    r, report = _check(site)
    assert r.returncode == 0 and report["history"]["latest"] == RETAINED, "without --latest, its own latest"


def _history_change(change: Callable[[dict], object]) -> Callable[[pathlib.Path], object]:
    def apply(site: pathlib.Path) -> None:
        history = json.loads((site / "history.json").read_text())
        change(history)
        (site / "history.json").write_text(json.dumps(history))
    return apply


def _archive_change(change: Callable[[dict], object]) -> Callable[[pathlib.Path], object]:
    def apply(site: pathlib.Path) -> None:
        path = site / "snapshots" / SLIMMED / "archive_records.json.gz"
        doc = _read(path)
        change(doc)
        _write(path, doc)
    return apply


def _json_change(rel: str, change: Callable[[dict], object]) -> Callable[[pathlib.Path], object]:
    def apply(site: pathlib.Path) -> None:
        body = json.loads((site / rel).read_text())
        change(body)
        (site / rel).write_text(json.dumps(body))
    return apply


def _first_key(d: dict) -> str:
    return next(iter(d))


OTHER_RUN = sh.stamps("2026-06-14")
# Each blocking rule of history.json and the archived snapshots: (change, a phrase the error says).
HISTORY_BLOCKS: dict[str, tuple[Callable[[pathlib.Path], object], str]] = {
    "no history.json": (lambda s: (s / "history.json").unlink(), "history.json cannot be read"),
    "a latest that is not the date data/ serves": (
        _history_change(lambda h: h.update(latest="2026-10-04")), "says latest '2026-10-04', but data/ serves 2026-10-11"),
    "a history.json without a latest (the old format)": (
        _history_change(lambda h: h.pop("latest")), "says latest None, but data/ serves 2026-10-11"),
    "the latest not listed": (_history_change(lambda h: h["dates"].remove(RETAINED)), "does not list the latest date"),
    "a date newer than the latest": (
        _history_change(lambda h: h["dates"].append("2026-10-18")), "lists 2026-10-18, newer than the latest date"),
    "a date listed twice": (_history_change(lambda h: h["dates"].append("2026-06-14")), "lists a date twice"),
    "a date that is not a date string": (_history_change(lambda h: h["dates"].append(20261011)),
                                         "has no list of YYYY-MM-DD dates"),
    "a listed date whose folder is gone": (lambda s: shutil.rmtree(s / "snapshots" / "2026-06-14"),
                                           "lists 2026-06-14, but snapshots/2026-06-14/ is not there"),
    "a listed folder without its summary": (
        lambda s: (s / "snapshots" / "2026-02-22" / "dashboard-summary.json").unlink(),
        "snapshots/2026-02-22/ has no readable dashboard-summary.json"),
    "an archives entry for a date not listed": (
        _history_change(lambda h: h["archives"].update({"2026-07-26": {"kind": "aggregate"}})),
        "archives names '2026-07-26', which is not a listed archived date"),
    "an archives entry of another kind": (
        _history_change(lambda h: h["archives"].update({"2026-06-14": {"kind": "complete"}})),
        "archives entry for 2026-06-14"),
    "an aggregate that still holds dataset files": (
        lambda s: shutil.copy(s / "snapshots" / "2026-06-14" / "demographics.part1.json.gz", s / "snapshots" / SLIMMED),
        f"names {SLIMMED} an aggregate, but snapshots/{SLIMMED}/ still holds dataset files"),
    "an archive file named but not there": (
        lambda s: (s / "snapshots" / SLIMMED / "archive_records.json.gz").unlink(), "which is not there"),
    "an archive file that is not gzipped JSON": (
        lambda s: (s / "snapshots" / SLIMMED / "archive_records.json.gz").write_bytes(b"not gzip"),
        "archive_records.json.gz is not gzipped JSON"),
    "an archive file from another run": (
        _archive_change(lambda d: d.update(source_extracted_at=OTHER_RUN["extracted_at"])), "comes from another run"),
    "an archive file from another commit": (
        _archive_change(lambda d: d.update(source_pipeline_commit=OTHER_RUN["pipeline_commit"])), "comes from another run"),
    "an archive file missing a study": (
        _archive_change(lambda d: d["data"].pop(_first_key(d["data"]))),
        "does not cover exactly its summary's 16 recentStudies: 1 have no entry"),
    "an archive file with a study too many": (
        _archive_change(lambda d: d["data"].update(NCT09999999={"nct_id": "NCT09999999"})),
        "does not cover exactly its summary's 16 recentStudies: 0 have no entry (), 1 entries are not theirs"),
    "an archive file of another class": (_archive_change(lambda d: d.update({"class": "detail"})), "says class 'detail'"),
    "an archive file with another header": (_archive_change(lambda d: d.update(extracted_at="x")), "carries ["),
    "an archive entry that is another study's record": (
        _archive_change(lambda d: d["data"].update({_first_key(d["data"]): {"nct_id": "NCT00000001"}})),
        "are not that study's record"),
    "a complete snapshot missing a part": (
        lambda s: (s / "snapshots" / "2026-08-02" / "demographics.part4.json.gz").unlink(),
        "snapshots/2026-08-02/ is listed as a complete snapshot but lacks 1 of its 8 dataset files"),
    "a complete snapshot with a part too many": (
        lambda s: shutil.copy(s / "snapshots" / "2026-08-02" / "demographics.part8.json.gz",
                              s / "snapshots" / "2026-08-02" / "demographics.part9.json.gz"),
        "holds dataset files its layout does not name (demographics.part9.json.gz)"),
    "a complete snapshot part from another run": (
        lambda s: _gz_change("demographics.part2.json.gz", lambda b: b.update(OTHER_RUN))(s / "snapshots" / "2026-08-02"),
        "demographics.part2.json.gz comes from another run than core part 1"),
    "a truncated complete snapshot part": (
        lambda s: (s / "snapshots" / "2026-08-02" / "demographics.part6.json.gz").write_bytes(
            (s / "snapshots" / "2026-08-02" / "demographics.part6.json.gz").read_bytes()[:-30]),
        "demographics.part6.json.gz is not gzipped JSON"),
    "a complete snapshot whose run.json is another run's": (
        _json_change("snapshots/2026-08-02/run.json", lambda b: b.update(OTHER_RUN)), "run.json says extracted_at"),
    "a complete snapshot summary from another run": (
        _json_change("snapshots/2026-08-02/dashboard-summary.json", lambda b: b.update(OTHER_RUN)),
        "dashboard-summary.json comes from another run"),
    "a complete snapshot sex/gender meta from another run": (
        _json_change("snapshots/2026-08-02/sex_gender_parsed_meta.json",
                     lambda b: b.update(source_extracted_at=OTHER_RUN["extracted_at"])),
        "sex_gender_parsed_meta.json comes from another run"),
    "a complete snapshot sex/gender table of another size": (
        _json_change("snapshots/2026-08-02/sex_gender_parsed_meta.json", lambda b: b.update(n_rows=15)),
        "sex_gender_parsed.csv.gz has 16 rows; its meta counts 15"),
    "a complete snapshot methods text of another snapshot": (
        _json_change("snapshots/2026-08-02/sex_gender/methods.json", lambda b: b.update(snapshot_date="2026-06-14")),
        "methods.json is the '2026-06-14' snapshot's, not 2026-08-02's"),
}


@pytest.mark.parametrize("name", list(HISTORY_BLOCKS))
def test_each_history_and_snapshot_rule_blocks_the_push(tmp_path: pathlib.Path, name: str) -> None:
    damage, message = HISTORY_BLOCKS[name]
    site = _retained_site(tmp_path)
    damage(site)
    r, report = _check(site, "--latest", RETAINED)
    assert r.returncode == 1, f"{name} did not block"
    assert any(message in e for e in report["errors"]), report["errors"]


HISTORY_WARNS: dict[str, tuple[Callable[[pathlib.Path], object], str]] = {
    "a folder for the latest date": (
        lambda s: shutil.copytree(s / "snapshots" / "2026-10-04", s / "snapshots" / RETAINED),
        f"snapshots/{RETAINED}/ is there, a copy of the latest date"),
    "a folder history.json does not list": (
        lambda s: shutil.copytree(s / "snapshots" / "2026-08-02", s / "snapshots" / "2026-09-13"),
        "snapshots/ holds folders history.json does not list, which no page offers: 2026-09-13"),
    "an archive file history.json does not name": (
        _history_change(lambda h: h["archives"][SLIMMED].pop("detail")),
        f"snapshots/{SLIMMED}/archive_records.json.gz is there, but history.json does not name it"),
    "another file in an aggregate": (
        lambda s: (s / "snapshots" / "2026-02-22" / "industry_sponsors.json").write_text("{}"),
        "snapshots/2026-02-22/ is an aggregate but also keeps industry_sponsors.json"),
}


@pytest.mark.parametrize("name", list(HISTORY_WARNS))
def test_what_wastes_space_but_opens_only_warns(tmp_path: pathlib.Path, name: str) -> None:
    change, message = HISTORY_WARNS[name]
    site = _retained_site(tmp_path)
    change(site)
    r, report = _check(site, "--latest", RETAINED)
    assert r.returncode == 0, report["errors"]
    assert any(message in w for w in report["warnings"]), report["warnings"]


def test_an_archive_file_is_checked_against_its_own_summarys_run(tmp_path: pathlib.Path) -> None:
    """The site compares an archive file's source stamps with its summary's: a
    summary without a pipeline_commit (every run before 2026-09) takes any."""
    summary = {"extracted_at": "2026-05-31T09:00:28.493807", "recentStudies": [{"nct_id": "NCT00000001"}]}
    doc = {"source_extracted_at": summary["extracted_at"], "source_pipeline_commit": None, "class": "archive",
           "data": {"NCT00000001": {"nct_id": "NCT00000001"}}}
    path = tmp_path / "archive_records.json.gz"
    for commit in (None, "abc1234"):
        _write(path, {**doc, "source_pipeline_commit": commit})
        assert archive_records.problems(str(path), summary) == [], commit
    _write(path, doc)
    assert archive_records.problems(str(path), {**summary, "pipeline_commit": "abc1234"})


# ── extract.yml ─────────────────────────────────────────────────────────────

def _step(name: str) -> str:
    m = re.search(rf"\n      - name: {re.escape(name)}\n(.*?)(?=\n      - name: |\Z)", WORKFLOW, re.S)
    assert m, f"extract.yml lost the step {name!r}"
    return m.group(1)


PUBLISH = "Publish artifacts, archive snapshot, and push to the site"
CHECK_LINE = 'python3 ../scripts/check_site_contract.py --site . --latest "$DATE" --report "$RUNNER_TEMP/site_check.json"'
PRUNE_LINE = 'python3 scripts/prune_snapshots.py --site site --latest "$DATE" --report "$RUNNER_TEMP/retention.json"'


def test_the_publish_step_runs_the_check_after_staging_and_before_the_push() -> None:
    step = _step(PUBLISH)
    lines = [line.strip() for line in step.splitlines()]
    assert CHECK_LINE in lines, "the check line was changed (an || or a wrapper makes it advisory)"
    check = lines.index(CHECK_LINE)
    assert lines.index("git add -A -- $DATASET") < check, "the check runs before the dataset is staged"
    assert lines.index("git add -A snapshots/ history.json") < check, "the check runs before everything is staged"
    assert lines.index(PRUNE_LINE) < check, "the check runs before retention settles the tree"
    assert check < lines.index('git commit -m "Update demographics data $DATE$REPLACED" || exit 0') < lines.index("git push")
    assert "set +e" not in step and "continue-on-error" not in step, "the publish step no longer stops on a failed check"


def test_retention_runs_before_data_is_replaced_and_nothing_writes_this_weeks_snapshot() -> None:
    """Last week's dataset is archived out of data/ by the retention script, so
    it must run before this week's files replace it. The week itself lives in
    data/ only: no step copies it into snapshots/<date>/, the sponsor bridge
    included, and history.json is written by the retention script alone."""
    lines = [line.strip() for line in _step(PUBLISH).splitlines()]
    prune = lines.index(PRUNE_LINE)
    assert lines.index('test -n "$(find data/dataset -type f -print -quit)"') < prune, "retention runs without a dataset"
    assert prune < lines.index("rm -f site/data/demographics.part*.json.gz site/data/studies_tab.part*.json.gz "
                               "site/data/run.json")
    assert prune < lines.index("cp -R data/dataset/. site/data/")
    for name in (PUBLISH, "Publish sponsor bridge to the site", "Publish sex/gender audit to the site"):
        assert not re.search(r"snapshots/\$\{?(DATE|CURRENT_DATE)", _step(name)), f"{name} writes this week's snapshot"
    assert "jq --arg" not in _step(PUBLISH) and "history.json.tmp" not in _step(PUBLISH)
    assert len(re.findall(r"^\s*python3 \S*prune_snapshots\.py", WORKFLOW, re.M)) == 1


def _publish_block() -> str:
    body = re.search(r"^        run: \|\n(.*)", _step(PUBLISH), re.S | re.M).group(1)
    block = []
    for line in body.splitlines():
        if line.strip() and not line.startswith(" " * 10):
            break  # the run block ends where its indentation does
        block.append(line[10:])
    text = "\n".join(block)
    assert text.rstrip().endswith("git push"), "the run block was not read whole"
    return text


def _whole_week(tag: str) -> dict[str, str]:
    return {**{f"demographics.part{k}.json.gz": f"{tag} part {k}" for k in (1, 2)}, "run.json": f"{tag} run"}


def _split_week(tag: str) -> dict[str, str]:
    return {**_whole_week(tag), **{f"studies_tab.part{k}.json.gz": f"{tag} tab {k}" for k in (1, 2)},
            **{f"detail/{n}.json.gz": f"{tag} shard {n}" for n in range(4)}}


def _publish(tmp_path: pathlib.Path, last_week: dict[str, str], this_week: dict[str, str], check_rc: int = 0,
             last_snapshot: str | None = "2026-10-04",
             history: str = '{"dates": ["2026-10-04"]}') -> tuple[subprocess.CompletedProcess[str], str, pathlib.Path]:
    """Run the publish step's own block under bash -e: real files, real git and
    real jq in a site repository whose last commit holds last week's dataset (in
    data/, and in snapshots/<last_snapshot>/ as the publish step before this
    change wrote it), stubs for the check, retention and push."""
    real_git = shutil.which("git")
    assert real_git, "git is not installed"
    if not shutil.which("jq"):
        pytest.skip("jq is not installed here (the runner has it)")
    engine = tmp_path / "engine"
    (engine / "data" / "dataset").mkdir(parents=True)          # there even when this week's is empty
    files = {f"data/dataset/{rel}": body for rel, body in this_week.items()}
    files.update({"data/dashboard-summary.json": "{}", "data/industry_sponsors.json": "{}",
                  "data/sex_gender_parsed.csv.gz": "table", "data/sex_gender_parsed_meta.json": "{}",
                  "data/sex_gender/methods.json": "{}", "data/sex_gender/methods.md": "methods",
                  "condition_ontology.json": "{}"})
    site_files = {f"data/{rel}": body for rel, body in last_week.items()}
    if last_snapshot:
        site_files.update({f"snapshots/{last_snapshot}/{rel}": body for rel, body in last_week.items()})
    # A monthly aggregate: the site has tracked snapshots/ since February.
    site_files.update({"data/details.part1.json.gz": "the frozen March details", "data/dashboard-summary.json": "{}",
                       "data/geo/active_run.json": "{}", "history.json": history,
                       "snapshots/2026-04-26/dashboard-summary.json": "{}"})
    for root, tree in ((engine, files), (engine / "site", site_files)):
        for rel, body in tree.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(body)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "HOME": str(tmp_path)}
    site = engine / "site"
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "last week"]):
        subprocess.run([real_git, *args], cwd=site, env=env, check=True)
    (site / "data" / "stray.txt").write_text("an untracked file the publish must not stage")
    stub = tmp_path / "bin"
    stub.mkdir()
    log = tmp_path / "calls.log"
    for name, script in {
        "git": f'echo "git $*" >> "$CALLS"; case "$1" in push) exit 0;; esac; exec "{real_git}" "$@"',
        "python3": 'echo "python3 $*" >> "$CALLS"; case "$*" in *check_site_contract.py*) exit "$CHECK_RC";; esac; exit 0',
    }.items():
        (stub / name).write_text(f"#!/bin/bash\n{script}\n")
        (stub / name).chmod(0o755)
    env.update({"PATH": f"{stub}:{os.environ['PATH']}", "CALLS": str(log), "CHECK_RC": str(check_rc),
                "CURRENT_DATE": DATE, "RUNNER_TEMP": str(tmp_path)})
    r = subprocess.run(["bash", "-e", "-c", _publish_block()], cwd=engine, env=env, capture_output=True, text=True)
    return r, log.read_text() if log.exists() else "", site


def _committed(site: pathlib.Path) -> dict[str, str]:
    """What the publish commit changed: path -> A, M or D."""
    out = subprocess.run(["git", "show", "--no-renames", "--name-status", "--format=", "HEAD"], cwd=site, capture_output=True,
                         text=True, check=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull}).stdout
    return {line.split("\t")[1]: line.split("\t")[0] for line in out.splitlines() if line.strip()}


def _dataset_changes(changes: dict[str, str], folder: str) -> dict[str, str]:
    pattern = re.compile(rf"{re.escape(folder)}/(demographics\.part\d+\.json\.gz|studies_tab\.part\d+\.json\.gz|detail/.*|run\.json)")
    return {p.split("/", 2 if folder.startswith("snapshots") else 1)[-1]: s
            for p, s in changes.items() if pattern.fullmatch(p)}


def _expect(last: dict[str, str], now: dict[str, str]) -> dict[str, str]:
    """The dataset changes a commit should make, going from last week's files to this week's."""
    out = {rel: ("M" if rel in last else "A") for rel in now if last.get(rel) != now[rel]}
    out.update({rel: "D" for rel in last if rel not in now})
    return out


@pytest.mark.parametrize("last,now", [
    (_whole_week("last"), _whole_week("this")),
    (_whole_week("last"), _split_week("this")),
    (_split_week("last"), _whole_week("this")),
    (_split_week("last"), _split_week("this")),
], ids=["whole-week", "split-turned-on", "rollback-to-whole-parts", "split-week"])
def test_the_publish_step_stages_exactly_the_datasets_additions_and_removals(
        tmp_path: pathlib.Path, last: dict[str, str], now: dict[str, str]) -> None:
    r, calls, site = _publish(tmp_path, last, now)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"prune_snapshots.py --site site --latest {DATE}" in calls and f"--latest {DATE}" in calls
    assert "git push" in calls
    changes = _committed(site)
    assert _dataset_changes(changes, "data") == _expect(last, now)
    assert not any(p.startswith("snapshots/") for p in changes), "the step wrote a snapshot (retention is stubbed)"
    assert not (site / "snapshots" / DATE).exists(), "this week was copied into snapshots/ too"
    assert "data/details.part1.json.gz" not in changes and (site / "data" / "details.part1.json.gz").exists()
    assert "data/stray.txt" not in changes
    status = subprocess.run(["git", "status", "--porcelain"], cwd=site, capture_output=True, text=True).stdout
    assert status.strip() == "?? data/stray.txt", status
    on_disk = sorted(str(p.relative_to(site / "data")) for p in (site / "data").rglob("*")
                     if p.is_file() and re.fullmatch(r"(demographics|studies_tab)\.part\d+\.json\.gz|detail/.*|run\.json",
                                                     str(p.relative_to(site / "data"))))
    assert on_disk == sorted(now), "data/ does not hold exactly this week's dataset"


def test_a_same_day_rerun_that_switches_layout_leaves_nothing_of_the_earlier_run(tmp_path: pathlib.Path) -> None:
    """history.json already serves the date from data/: the re-run replaces
    data/'s files, says so, and writes no snapshot of either run."""
    last, now = _split_week("earlier today"), _whole_week("this")
    r, calls, site = _publish(tmp_path, last, now, last_snapshot=None,
                              history=json.dumps({"dates": ["2026-10-04", DATE], "latest": DATE}))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"history.json already serves {DATE} from data/" in r.stdout
    changes = _committed(site)
    assert _dataset_changes(changes, "data") == _expect(last, now)
    assert not any(p.startswith("snapshots/") for p in changes) and not (site / "snapshots" / DATE).exists()
    log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=site, capture_output=True, text=True).stdout.strip()
    assert log == f"Update demographics data {DATE} (replaces earlier {DATE} data)"


def test_with_no_dataset_to_stage_the_publish_step_stops_before_staging_anything(tmp_path: pathlib.Path) -> None:
    """An empty data/dataset/ and no dataset file tracked in the site leave the
    list of dataset paths empty, and `git add -A --` with no path stages the
    whole site checkout, stray files included. The step stops before that, and
    before retention archives or deletes anything."""
    r, calls, site = _publish(tmp_path, {}, {})
    assert r.returncode != 0, r.stdout + r.stderr
    assert "git add" not in calls and "git commit" not in calls and "git push" not in calls, calls
    assert "check_site_contract.py" not in calls and "prune_snapshots.py" not in calls
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=site, capture_output=True, text=True,
                            check=True).stdout
    assert staged == "", f"the step staged {staged!r}"
    log = subprocess.run(["git", "log", "--format=%s"], cwd=site, capture_output=True, text=True).stdout.split("\n")
    assert log[0] == "last week"


def test_with_the_real_retention_and_check_the_publish_step_archives_last_week_and_pushes(
        tmp_path: pathlib.Path) -> None:
    """The publish block under bash -e with the real retention script and the
    real check, on a site whose latest week, 2026-10-04, is served from data/
    with no folder: it is archived out of data/ before this week's files land,
    whole and with its summary, sex/gender pair and methods text; history.json
    names 2026-10-11 the latest; nothing is copied into snapshots/2026-10-11/;
    the check passes and the commit is pushed."""
    real_git, jq = shutil.which("git"), shutil.which("jq")
    if not jq:
        pytest.skip("jq is not installed here (the runner has it)")
    assert real_git, "git is not installed"
    engine = tmp_path / "engine"
    site = sh.make_site(engine, "2026-10-04", history={"dates": ["2026-10-04"], "latest": "2026-10-04", "archives": {}})
    for name in ("scripts", "src"):
        (engine / name).symlink_to(os.path.join(ROOT, name))
    shutil.copytree(sh.cut(engine, DATE), engine / "data" / "dataset")
    week = sh.write_week(tmp_path, tmp_path / "week", DATE)
    for rel in ("dashboard-summary.json", "sex_gender_parsed.csv.gz", "sex_gender_parsed_meta.json",
                "sex_gender/methods.json"):
        (engine / "data" / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(week / rel, engine / "data" / rel)
    (engine / "data" / "sex_gender" / "methods.md").write_text("methods")
    (engine / "data" / "industry_sponsors.json").write_text("{}")
    (engine / "condition_ontology.json").write_text("{}")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "HOME": str(tmp_path)}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "last week"]):
        subprocess.run([real_git, *args], cwd=site, env=env, check=True)
    last_week = {rel: (site / "data" / rel).read_bytes() for rel in sh.files(site / "data")}
    stub = tmp_path / "bin"
    stub.mkdir()
    log = tmp_path / "calls.log"
    for name, script in {
        "git": f'echo "git $*" >> "$CALLS"; case "$1" in push) exit 0;; esac; exec "{real_git}" "$@"',
        "python3": f'echo "python3 $*" >> "$CALLS"; exec "{sys.executable}" "$@"',
    }.items():
        (stub / name).write_text(f"#!/bin/bash\n{script}\n")
        (stub / name).chmod(0o755)
    env.update({"PATH": f"{stub}:{os.environ['PATH']}", "CALLS": str(log), "CURRENT_DATE": DATE,
                "RUNNER_TEMP": str(tmp_path)})
    r = subprocess.run(["bash", "-e", "-c", _publish_block()], cwd=engine, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "git push" in log.read_text()
    changes = _committed(site)
    archived = sorted(p.split("/", 2)[2] for p, st in changes.items() if p.startswith("snapshots/2026-10-04/") and st == "A")
    assert archived == sorted([f"demographics.part{k}.json.gz" for k in range(1, 9)] + [
        "run.json", "dashboard-summary.json", "sex_gender_parsed.csv.gz", "sex_gender_parsed_meta.json",
        "sex_gender/methods.json"])
    for rel in archived:
        assert (site / "snapshots" / "2026-10-04" / rel).read_bytes() == last_week[rel], rel
    assert not any(p.startswith(f"snapshots/{DATE}/") for p in changes) and not (site / "snapshots" / DATE).exists()
    assert json.loads((site / "history.json").read_text()) == {"dates": ["2026-10-04", DATE], "latest": DATE, "archives": {}}
    assert (site / "data" / "run.json").read_bytes() == (engine / "data" / "dataset" / "run.json").read_bytes()
    assert json.loads((tmp_path / "retention.json").read_text())["archived"]["status"] == "archived"
    check = json.loads((tmp_path / "site_check.json").read_text())
    assert check["ok"] and check["history"]["complete"] == ["2026-10-04"], check["errors"]
    subject = subprocess.run([real_git, "log", "-1", "--format=%s"], cwd=site, capture_output=True, text=True).stdout
    assert subject.strip() == f"Update demographics data {DATE}"


@pytest.mark.parametrize("check_rc,pushed", [(1, False), (0, True)])
def test_a_failed_check_stops_the_publish_step_before_commit_and_push(tmp_path: pathlib.Path, check_rc: int,
                                                                     pushed: bool) -> None:
    r, calls, site = _publish(tmp_path, _whole_week("last"), _split_week("this"), check_rc=check_rc)
    assert "check_site_contract.py" in calls
    assert (r.returncode == 0) is pushed, r.stdout + r.stderr
    assert ("git commit" in calls) is pushed and ("git push" in calls) is pushed, calls
    log = subprocess.run(["git", "log", "--format=%s"], cwd=site, capture_output=True, text=True).stdout.split("\n")
    assert (log[0] == f"Update demographics data {DATE}") is pushed


def test_the_run_summary_reads_what_the_report_carries(tmp_path: pathlib.Path) -> None:
    jq = shutil.which("jq")
    if not jq:
        pytest.skip("jq is not installed here (the runner has it)")
    summary = _step("Write run summary")
    m = re.search(r"jq -r '([^']*)' \"\$RUNNER_TEMP/site_check\.json\"", summary)
    assert m, "the run summary lost the site check row"
    program = m.group(1)
    assert '[ -f "$RUNNER_TEMP/site_check.json" ]' in summary and '--report "$RUNNER_TEMP/site_check.json"' in WORKFLOW
    for site in (_site(tmp_path / "inline"), _split_site(tmp_path / "split")):
        _, report = _check(site, "--latest", LATEST)
        read = set(re.findall(r"\.([a-z_]+)", program))
        assert read <= set(report), f"the summary reads keys the report lacks: {read - set(report)}"
    big = tmp_path / "big.json"
    big.write_text(json.dumps({**report, "records": 80320, "parts_gzip_bytes": 18194978,
                               "total_budget_bytes": 160000000, "tree_bytes": 712312000}))
    row = subprocess.run([jq, "-r", program, str(big)], capture_output=True, text=True).stdout.strip()
    assert row == "passed: 80320 records; parts 18 MB of a 160 MB budget; site 712 MB; 0 errors, 0 warnings"
    failing = tmp_path / "fail.json"
    failing.write_text(json.dumps({"ok": False, "errors": ["x"], "warnings": []}))
    row = subprocess.run([jq, "-r", program, str(failing)], capture_output=True, text=True).stdout.strip()
    assert row.startswith("FAILED, nothing published") and row.endswith("1 errors, 0 warnings")


def test_the_raw_measure_archive_does_not_depend_on_the_site_push() -> None:
    order = [m.group(1) for m in re.finditer(r"\n      - name: (.+)", WORKFLOW)]
    archive = order.index("Archive the retained sex/gender raw measures permanently")
    assert archive < order.index("Check out the site repository") < order.index(PUBLISH)
    step = _step("Archive the retained sex/gender raw measures permanently")
    assert "!cancelled()" in step and "steps.extract.outcome == 'success'" in step
    assert "continue-on-error: true" in step
