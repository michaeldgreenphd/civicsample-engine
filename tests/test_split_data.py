"""scripts/split_data.py: the week's full records cut into the site's dataset files.

The split follows the site's own contract (tests/record_contract.json there),
which the weekly job reads from its site checkout: its classes, its optional
paths and its layout section's switch. These tests pin:

- the switch: with no layout section, or layout.enabled false, the 8
  whole-record parts come out byte for byte as split_data.py wrote them before
  the layout existed (tests/fixtures/split_data_before_layout.py is that
  script, verbatim); with version 1 and enabled true, the split; with any other
  version, a malformed switch, or (split on) a contract the split cannot follow,
  a non-zero exit with nothing written;
- the split layout: the file set, every header, the same run stamps in every
  file, studies_tab part K holding exactly core part K's records, every record
  once in its own detail shard (the site's shard test vectors), study_sites
  whole in the shards and country-only in core, unread fields gone, null kept,
  each file's entries equal to what the site's own reference projection
  (tests/split_fixture.mjs there) gives, and every record's entries merging
  back to the record as the contract reads it;
- run.json: the stamps, total_parts and studies, plus the layout and the gzip
  bytes per class, and no list of files;
- the full records are never written or deleted; a switch either way leaves
  nothing of the other layout, and nothing else is removed;
- the weekly job cuts the dataset after the site checkout, from the site's
  contract, whatever the full-record release did, and the run summary reports
  its size per class.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import pathlib
import re
import runpy
import shutil
import subprocess
import sys
import types
from collections.abc import Callable
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import split_helpers as h  # noqa: E402  (puts the repo root and scripts/ on sys.path)
import split_data  # noqa: E402
from src import site_layout as sl  # noqa: E402

ROOT = h.ROOT
SCRIPT = os.path.join(ROOT, "scripts", "split_data.py")
BEFORE = os.path.join(h.TESTS, "fixtures", "split_data_before_layout.py")
# scripts/split_data.py at engine main adba050, the last version before the layout.
BEFORE_SHA256 = "936e9d1cf150d382986a3ee38862983ec959b1d8c798db4aa75256e06bdb904f"
PARTS = [f"demographics.part{k}.json.gz" for k in range(1, 9)]
WORKFLOW = open(os.path.join(ROOT, ".github", "workflows", "extract.yml")).read()


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(folder: pathlib.Path) -> list[str]:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())


def _run(tmp_path: pathlib.Path, contract: dict[str, Any], records: list[dict[str, Any]],
         out: pathlib.Path) -> subprocess.CompletedProcess[str]:
    (tmp_path / "c.json").write_text(json.dumps(contract))
    h.write_full(tmp_path / "demographics.json", records)
    return subprocess.run([sys.executable, SCRIPT, "--contract", str(tmp_path / "c.json"),
                           "--demographics", str(tmp_path / "demographics.json"), "--out-dir", str(out)],
                          capture_output=True, text=True)


# ── switched off: today's parts, byte for byte ──────────────────────────────

@pytest.mark.parametrize("n", [16, 20], ids=["8-full-parts", "an-empty-last-part"])
def test_whole_parts_are_byte_for_byte_what_the_script_wrote_before_the_layout(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    assert _sha(pathlib.Path(BEFORE)) == BEFORE_SHA256, "the reference script was edited"
    records = h.whole_records(h.ids(n))
    # gzip writes the time into its header; both runs get the same one.
    monkeypatch.setattr(gzip, "time", types.SimpleNamespace(time=lambda: 1_760_158_329.0))
    old = tmp_path / "old"
    (old / "data").mkdir(parents=True)
    h.write_full(old / "data" / "demographics.json", records)
    monkeypatch.chdir(old)
    runpy.run_path(BEFORE, run_name="__main__")
    new = tmp_path / "new"
    new.mkdir()
    h.write_full(new / "demographics.json", records)
    (new / "c.json").write_text(json.dumps(h.without_layout(h.site_contract())))
    assert split_data.main(["--contract", str(new / "c.json"), "--demographics", str(new / "demographics.json"),
                            "--out-dir", str(new / "dataset")]) == 0
    assert _files(new / "dataset") == sorted(PARTS + ["run.json"])
    for name in PARTS:
        assert (new / "dataset" / name).read_bytes() == (old / "data" / name).read_bytes(), f"{name} differs"
    before = json.loads((old / "data" / "run.json").read_text())
    after = json.loads((new / "dataset" / "run.json").read_text())
    assert {k: after[k] for k in before} == before == {**h.STAMPS, "total_parts": 8, "studies": n}


def _no_enabled_key(contract: dict[str, Any]) -> dict[str, Any]:
    del contract["layout"]["enabled"]
    return contract


@pytest.mark.parametrize("contract", [
    lambda: h.without_layout(h.site_contract()),
    lambda: h.site_contract(enabled=False),
    lambda: _no_enabled_key(h.site_contract()),
], ids=["no-layout-section", "layout-off", "no-enabled-key"])
def test_without_the_switch_the_parts_carry_the_whole_records(tmp_path: pathlib.Path,
                                                              contract: Callable[[], dict[str, Any]]) -> None:
    records = h.whole_records(h.ids(16))
    out = h.split(tmp_path, copy.deepcopy(records), contract())
    assert _files(out) == sorted(PARTS + ["run.json"]), "a whole week wrote class files"
    parts = [h.read_gz(out / name) for name in PARTS]
    assert all(list(p) == ["extracted_at", "pipeline_commit", "part", "total_parts", "data"] for p in parts)
    assert [r for p in parts for r in p["data"]] == records, "the parts do not carry the whole records"
    run = json.loads((out / "run.json").read_text())
    assert run == {**h.STAMPS, "total_parts": 8, "studies": 16,
                   "gzip_bytes": {"inline": sum((out / name).stat().st_size for name in PARTS)}}


# ── what refuses, and leaves the dataset folder as it was ───────────────────

def _last_week(out: pathlib.Path) -> dict[str, str]:
    out.mkdir(parents=True, exist_ok=True)
    (out / "demographics.part1.json.gz").write_bytes(b"last week's part")
    (out / "run.json").write_text('{"extracted_at": "last week"}')
    return {p: _sha(out / p) for p in _files(out)}


def _refused(tmp_path: pathlib.Path, r: subprocess.CompletedProcess[str], out: pathlib.Path,
             before: dict[str, str], message: str) -> None:
    assert r.returncode != 0, r.stdout
    assert message in r.stderr and "nothing written" in r.stderr, r.stderr
    assert {p: _sha(out / p) for p in _files(out)} == before, "a refused run changed the dataset folder"
    assert not (tmp_path / "dataset.partial").exists()


@pytest.mark.parametrize("enabled", [True, False], ids=["on", "off"])
@pytest.mark.parametrize("damage,message", [
    (lambda c: c["layout"].update(version=2), "version 2"),
    (lambda c: c["layout"].update(version="1"), "version '1'"),
    (lambda c: c["layout"].update(version=True), "version True"),
    (lambda c: c["layout"].pop("version"), "version None"),
    (lambda c: c.update(layout=[1]), "not an object"),
    (lambda c: c["layout"].update(enabled="true"), "not true or false"),
], ids=["version-2", "version-text", "version-true", "no-version", "not-an-object", "enabled-text"])
def test_a_layout_this_engine_does_not_know_refuses_and_writes_nothing(
        tmp_path: pathlib.Path, enabled: bool, damage: Callable[[dict[str, Any]], object], message: str) -> None:
    contract = h.site_contract(enabled)
    damage(contract)
    out = tmp_path / "dataset"
    before = _last_week(out)
    _refused(tmp_path, _run(tmp_path, contract, h.whole_records(h.ids(16)), out), out, before, message)


@pytest.mark.parametrize("damage,message", [
    (lambda c, r: c["classes"]["detail"].append("references[].type"), "one class must hold it"),
    (lambda c, r: c["classes"]["detail"].append("nct_id"), "listed twice"),
    (lambda c, r: c["classes"]["core"].append("Brief Title"), "malformed path"),
    (lambda c, r: c["classes"].pop("detail"), "not ['core', 'studies_tab', 'detail']"),
    (lambda c, r: c["layout"]["optional_class"].update({"references[].title": "detail"}), "shares its container"),
    (lambda c, r: c["layout"]["optional_class"].pop("official_title"), "exactly the optional paths"),
    (lambda c, r: c["layout"]["files"].update(detail="detail/shard{n}.json.gz"), "this engine writes"),
    (lambda c, r: c["layout"]["headers"]["detail"].append("checksum"), "this engine writes"),
    (lambda c, r: c["layout"]["detail"].update(key="nct_mod"), "this engine shards by"),
    (lambda c, r: c["layout"]["detail"].update(shards=0), "not a whole number"),
    (lambda c, r: c["layout"]["detail"]["vectors"][4].update(shard=17), "test vector"),
    (lambda c, r: c["layout"]["detail"]["whole_lists"].append("countries"), "does not read"),
    (lambda c, r: r[5].update(nct_id="NCT123"), "not NCT followed by 8 digits"),
    (lambda c, r: r[5].update(nct_id=None), "not NCT followed by 8 digits"),
    (lambda c, r: r[5].update(nct_id=r[2]["nct_id"]), "appears in more than one record"),
], ids=["a-field-in-two-sidecars", "a-path-listed-twice", "a-malformed-path", "a-class-missing",
        "an-optional-path-misplaced", "an-optional-path-unplaced", "other-file-names", "other-headers",
        "another-shard-key", "no-shards", "a-test-vector-disagrees", "a-whole-list-detail-does-not-read",
        "a-short-nct-id", "no-nct-id", "a-trial-twice"])
def test_split_on_a_contract_or_records_it_cannot_follow_refuses_and_writes_nothing(
        tmp_path: pathlib.Path, damage: Callable[[dict[str, Any], list[dict[str, Any]]], object],
        message: str) -> None:
    contract, records = h.site_contract(enabled=True), h.whole_records(h.ids(16))
    damage(contract, records)
    out = tmp_path / "dataset"
    before = _last_week(out)
    _refused(tmp_path, _run(tmp_path, contract, records, out), out, before, message)


def test_a_record_that_would_not_merge_back_whole_refuses_and_writes_nothing(
        tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The split checks every record: a projection that lost a field the site
    reads (here, a stand-in fault that drops one) stops it before anything moves."""
    real = sl.Plan.sidecar

    def lossy(self: sl.Plan, record: dict[str, Any], cls: str) -> dict[str, Any]:
        entry = real(self, record, cls)
        if cls == "detail" and record["nct_id"] == "NCT00663858":
            entry.pop("lead_sponsor_name")
        return entry

    monkeypatch.setattr(sl.Plan, "sidecar", lossy)
    out = tmp_path / "dataset"
    before = _last_week(out)
    (tmp_path / "c.json").write_text(json.dumps(h.site_contract(enabled=True)))
    h.write_full(tmp_path / "demographics.json", h.whole_records(h.ids(16)))
    with pytest.raises(SystemExit, match=r"NCT00663858: .* do not merge back .*\(they differ in lead_sponsor_name\); nothing written"):
        split_data.main(["--contract", str(tmp_path / "c.json"), "--demographics", str(tmp_path / "demographics.json"),
                         "--out-dir", str(out)])
    assert {p: _sha(out / p) for p in _files(out)} == before and not (tmp_path / "dataset.partial").exists()


def test_switched_off_a_contract_the_split_could_not_follow_only_warns(tmp_path: pathlib.Path) -> None:
    contract = h.site_contract(enabled=False)
    contract["classes"]["detail"].append("references[].type")
    r = _run(tmp_path, contract, h.whole_records(h.ids(16)), tmp_path / "dataset")
    assert r.returncode == 0, r.stderr
    assert "::warning::" in r.stdout and "one class must hold it" in r.stdout
    assert _files(tmp_path / "dataset") == sorted(PARTS + ["run.json"])


# ── switched on: the split layout ───────────────────────────────────────────

def _copy_path(src: Any, dst: dict[str, Any], segs: list[tuple[str, bool]]) -> None:
    (key, each), rest = segs[0], segs[1:]
    if not isinstance(src, dict) or key not in src:
        return
    value = src[key]
    if not rest:
        dst[key] = value
    elif each:
        if not isinstance(value, list):
            dst[key] = value
            return
        if not isinstance(dst.get(key), list):
            dst[key] = [{} for _ in value]
        for item, into in zip(value, dst[key]):
            _copy_path(item, into, rest)
    elif not isinstance(value, dict):
        dst[key] = value
    else:
        if not isinstance(dst.get(key), dict):
            dst[key] = {}
        _copy_path(value, dst[key], rest)


def _site_project(record: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    """The site's reference projection (tests/split_fixture.mjs project() there),
    ported line by line: every listed path the record has, lists item by item."""
    out: dict[str, Any] = {}
    for path in dict.fromkeys(paths):
        _copy_path(record, out, [(s[:-2], True) if s.endswith("[]") else (s, False) for s in path.split(".")])
    return out


def _site_class_paths(contract: dict[str, Any]) -> dict[str, list[str]]:
    """split_fixture.mjs classPaths: each class's paths and its optional ones;
    detail adds every contract path of its whole lists."""
    classes, layout = contract["classes"], contract["layout"]
    out = {c: classes[c] + [p for p, k in layout["optional_class"].items() if k == c] for c in sl.CLASSES}
    out["detail"] += [p for c in sl.CLASSES for p in classes[c]
                      if any(p.startswith(f"{name}[].") for name in layout["detail"]["whole_lists"])]
    return out


UNREAD = ["keywords", "raceBreakdown", "sexBreakdown", "ethnicityBreakdown", "start_to_report_days",
          "time_perspective", "masking_description"]


def test_the_split_writes_each_class_in_its_own_files(tmp_path: pathlib.Path) -> None:
    contract = h.site_contract(enabled=True)
    records = h.whole_records(h.ids(24))
    out = h.split(tmp_path, copy.deepcopy(records), contract)
    shards = 256
    layout = {"version": 1, "studies_tab": {"files": 8}, "detail": {"shards": shards, "key": "nct_number_mod"}}
    assert _files(out) == sorted(PARTS + [f"studies_tab.part{k}.json.gz" for k in range(1, 9)]
                                 + [f"detail/{n}.json.gz" for n in range(shards)] + ["run.json"])

    core = [h.read_gz(out / name) for name in PARTS]
    for k, part in enumerate(core, start=1):
        assert list(part) == ["extracted_at", "pipeline_commit", "part", "total_parts", "layout", "data"]
        assert {key: part[key] for key in list(part)[:-1]} == {**h.STAMPS, "part": k, "total_parts": 8, "layout": layout}
    part_ids = [[r["nct_id"] for r in part["data"]] for part in core]
    assert [nct for ids in part_ids for nct in ids] == [r["nct_id"] for r in records], "core lost the record order"
    assert [len(ids) for ids in part_ids] == [3] * 8, "core is not cut in the same positional slices"

    tab: dict[str, Any] = {}
    for k in range(1, 9):
        body = h.read_gz(out / f"studies_tab.part{k}.json.gz")
        assert list(body) == ["extracted_at", "pipeline_commit", "class", "part", "total_parts", "data"]
        assert {key: body[key] for key in list(body)[:-1]} == {**h.STAMPS, "class": "studies_tab", "part": k, "total_parts": 8}
        assert list(body["data"]) == part_ids[k - 1], f"studies_tab part {k} does not hold core part {k}'s records"
        tab.update(body["data"])

    detail: dict[str, Any] = {}
    home: dict[str, int] = {}
    for n in range(shards):
        body = h.read_gz(out / f"detail/{n}.json.gz")
        assert list(body) == ["extracted_at", "pipeline_commit", "class", "shard", "shards", "key", "data"]
        assert {key: body[key] for key in list(body)[:-1]} == {**h.STAMPS, "class": "detail", "shard": n,
                                                               "shards": shards, "key": "nct_number_mod"}
        for nct, entry in body["data"].items():
            assert int(nct[3:]) % shards == n and nct not in home
            home[nct] = n
            detail[nct] = entry
    assert sorted(home) == sorted(r["nct_id"] for r in records), "not every record has exactly one detail entry"
    assert (home["NCT01975376"], home["NCT00663858"], home["NCT01174160"]) == (80, 50, 144), "the site's test vectors"

    paths = _site_class_paths(contract)
    every = [p for c in sl.CLASSES for p in contract["classes"][c]] + contract["optional"]
    for part in core:
        for row in part["data"]:
            record = next(r for r in records if r["nct_id"] == row["nct_id"])
            nct = record["nct_id"]
            # Each file holds what the site's own reference projection gives.
            assert row == _site_project(record, paths["core"]), nct
            assert tab[nct] == _site_project(record, paths["studies_tab"]), nct
            assert detail[nct] == _site_project(record, paths["detail"]), nct
            # And together they are the record as the contract reads it.
            assert sl.merge_study(row, tab[nct], detail[nct]) == _site_project(record, every), nct
            assert "nct_id" not in tab[nct] and "nct_id" not in detail[nct]
            for key in UNREAD:
                assert key in record and key not in row and key not in tab[nct] and key not in detail[nct]
            assert "status" not in row and "references" not in row and "secondary_outcomes" not in row
            assert all(set(s) == {"country"} for s in row["study_sites"]), "core carries more of a site than its country"
            assert detail[nct]["study_sites"] == record["study_sites"], "a shard's study_sites is not whole"
            assert all(set(o) == {"measure", "time_frame"} for o in detail[nct]["secondary_outcomes"])
            assert all("type" not in ref for ref in tab[nct]["references"])
    # The records did carry the unread fields the files leave out.
    assert "description" in records[0]["secondary_outcomes"][0] and "type" in records[0]["references"][0]
    first, second = records[0]["nct_id"], records[1]["nct_id"]
    assert core[0]["data"][0]["official_title"] == "An official title", "an optional core path was dropped"
    assert core[0]["data"][0]["brief_title"] == "Café study – naïve participants"
    assert detail[second]["status"] is None and detail[second]["why_stopped"] is None, "null was not kept"
    assert detail[first]["collaborators"] == records[0]["collaborators"]
    assert tab[first]["references"][0]["title"] == "A randomized trial", "an optional studies_tab path was dropped"

    run = json.loads((out / "run.json").read_text())
    sizes = {"core": sum((out / p).stat().st_size for p in PARTS),
             "studies_tab": sum((out / f"studies_tab.part{k}.json.gz").stat().st_size for k in range(1, 9)),
             "detail": sum((out / f"detail/{n}.json.gz").stat().st_size for n in range(shards))}
    assert run == {**h.STAMPS, "total_parts": 8, "studies": 24, "layout": layout, "gzip_bytes": sizes}


def test_the_split_files_are_the_same_bytes_for_the_same_run(tmp_path: pathlib.Path) -> None:
    contract, records = h.site_contract(enabled=True), h.whole_records(h.ids(16))
    a = h.split(tmp_path / "a", copy.deepcopy(records), contract)
    b = h.split(tmp_path / "b", copy.deepcopy(records), contract)
    assert _files(a) == _files(b)
    assert all((a / p).read_bytes() == (b / p).read_bytes() for p in _files(a))


def test_a_contract_may_name_another_shard_count(tmp_path: pathlib.Path) -> None:
    contract = h.site_contract(enabled=True)
    contract["layout"]["detail"]["shards"] = 128
    out = h.split(tmp_path, h.whole_records(h.ids(16)), contract)
    assert sorted(os.listdir(out / "detail"), key=lambda n: int(n.split(".")[0])) == [f"{n}.json.gz" for n in range(128)]
    assert h.read_gz(out / "demographics.part1.json.gz")["layout"]["detail"] == {"shards": 128, "key": "nct_number_mod"}
    assert "NCT01174160" in h.read_gz(out / "detail" / "16.json.gz")["data"], "the 128-shard test vector"


# ── what it never touches, and what it cleans up ────────────────────────────

@pytest.mark.parametrize("enabled", [False, True], ids=["whole", "split"])
@pytest.mark.parametrize("name", ["demographics.json", "demographics.json.gz"])
def test_the_full_records_are_never_written_or_deleted(tmp_path: pathlib.Path, enabled: bool, name: str) -> None:
    data = tmp_path / "data"
    data.mkdir()
    full = data / name
    h.write_full(full, h.whole_records(h.ids(16)))
    before = _sha(full)
    (tmp_path / "c.json").write_text(json.dumps(h.site_contract(enabled)))
    for _ in range(2):          # the second run replaces the first's outputs, in the input's own folder
        assert split_data.main(["--contract", str(tmp_path / "c.json"), "--demographics", str(full),
                                "--out-dir", str(data)]) == 0
        assert full.exists() and _sha(full) == before, "split_data.py changed the full records"


def test_a_switch_either_way_leaves_nothing_of_the_other_layout_and_nothing_else_goes(tmp_path: pathlib.Path) -> None:
    records = h.whole_records(h.ids(16))
    out = tmp_path / "dataset"
    out.mkdir()
    others = {"dashboard-summary.json": b"{}", "details.part1.json.gz": b"the frozen March details",
              "detail.txt": b"not the detail folder", "notes/detail/0.json.gz": b"elsewhere"}
    for rel, body in others.items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_bytes(body)
    (out / "demographics.part9.json.gz").write_bytes(b"a part from a longer week")
    (out / "detail").mkdir()
    (out / "detail" / "999.json.gz").write_bytes(b"a shard from a larger count")
    (tmp_path / "c.json").write_text("{}")
    h.write_full(tmp_path / "demographics.json", records)

    def run(contract: dict[str, Any]) -> list[str]:
        (tmp_path / "c.json").write_text(json.dumps(contract))
        assert split_data.main(["--contract", str(tmp_path / "c.json"), "--demographics",
                                str(tmp_path / "demographics.json"), "--out-dir", str(out)]) == 0
        for rel, body in others.items():
            assert (out / rel).read_bytes() == body, f"{rel} was touched"
        return [p for p in _files(out) if p not in others]

    split_files = sorted(PARTS + [f"studies_tab.part{k}.json.gz" for k in range(1, 9)]
                         + [f"detail/{n}.json.gz" for n in range(256)] + ["run.json"])
    assert run(h.site_contract(enabled=True)) == split_files
    assert run(h.site_contract(enabled=False)) == sorted(PARTS + ["run.json"]), "the rollback left split files"
    assert run(h.site_contract(enabled=True)) == split_files


# ── the weekly job ──────────────────────────────────────────────────────────

def _step(name: str) -> str:
    m = re.search(rf"\n      - name: {re.escape(name)}\n(.*?)(?=\n      - name: |\Z)", WORKFLOW, re.S)
    assert m, f"extract.yml lost the step {name!r}"
    return m.group(1)


SPLIT_STEP = "Cut the week's records into the site's dataset files"
PUBLISH_STEP = "Publish artifacts, archive snapshot, and push to the site"


def test_the_weekly_job_cuts_the_dataset_after_the_site_checkout_from_the_sites_contract() -> None:
    order = [m.group(1) for m in re.finditer(r"\n      - name: (.+)", WORKFLOW)]
    split = order.index(SPLIT_STEP)
    assert order.index("Keep the week's full records on a permanent GitHub Release") < order.index("Check out the site repository")
    assert order.index("Check out the site repository") == split - 1, "the split must read the site checkout's contract"
    assert split + 1 == order.index(PUBLISH_STEP)
    step = _step(SPLIT_STEP)
    assert ("python3 scripts/split_data.py --contract site/tests/record_contract.json "
            "--demographics data/demographics.json --out-dir data/dataset") in step
    # Owner decision 17a: the site publish does not wait on the full-record
    # release, as before the split; neither step has a condition.
    for name in (SPLIT_STEP, PUBLISH_STEP):
        assert not re.search(r"^        if:", _step(name), re.M), f"{name} gained a condition"
    assert "split_data.py" not in _step("Generate dashboard artifacts")
    assert len(re.findall(r"python3? scripts/split_data\.py", WORKFLOW)) == 1, "the split runs more than once"


def test_the_weekly_job_publishes_the_dataset_folder_to_data_and_to_the_snapshot() -> None:
    lines = [line.strip() for line in _step(PUBLISH_STEP).splitlines()]
    rm_data = "rm -f site/data/demographics.part*.json.gz site/data/studies_tab.part*.json.gz site/data/run.json"
    assert lines.index(rm_data) < lines.index("rm -rf site/data/detail") < lines.index("cp -R data/dataset/. site/data/")
    rm_snap = ('rm -f "snapshots/$DATE"/demographics.part*.json.gz "snapshots/$DATE"/studies_tab.part*.json.gz '
               '"snapshots/$DATE/run.json"')
    assert lines.index(rm_snap) < lines.index('rm -rf "snapshots/$DATE/detail"') \
        < lines.index('cp -R ../data/dataset/. "snapshots/$DATE/"')
    step = _step(PUBLISH_STEP)
    assert "details" not in " ".join(line for line in lines if line.startswith("rm ")), \
        "a removal could reach the frozen data/details.part*.json.gz"
    assert not re.search(r"git add (-A |--all )?(-- )?data/?$", step, re.M), "a blanket git add over data/"


def test_the_run_summary_reports_the_dataset_size_per_class(tmp_path: pathlib.Path) -> None:
    jq = shutil.which("jq")
    if not jq:
        pytest.skip("jq is not installed here (the runner has it)")
    summary = _step("Write run summary")
    m = re.search(r"jq -r '([^']*)' data/dataset/run\.json", summary)
    assert m, "the run summary lost the dataset row"
    records = h.whole_records(h.ids(16))
    rows = {}
    for enabled in (True, False):
        out = h.split(tmp_path / str(enabled), copy.deepcopy(records), h.site_contract(enabled))
        rows[enabled] = subprocess.run([jq, "-r", m.group(1), str(out / "run.json")],
                                       capture_output=True, text=True, check=True).stdout.strip()
    assert re.fullmatch(r"split \(layout 1, 256 detail shards\): core [\d.]+ MB, studies_tab [\d.]+ MB, detail [\d.]+ MB",
                        rows[True]), rows[True]
    assert re.fullmatch(r"whole records \(no layout\): inline [\d.]+ MB", rows[False]), rows[False]
    big = tmp_path / "big.json"
    big.write_text(json.dumps({"layout": sl.header_layout(8, 256),
                               "gzip_bytes": {"core": 18194978, "studies_tab": 24242010, "detail": 62601234}}))
    row = subprocess.run([jq, "-r", m.group(1), str(big)], capture_output=True, text=True, check=True).stdout.strip()
    assert row == "split (layout 1, 256 detail shards): core 18.2 MB, studies_tab 24.2 MB, detail 62.6 MB"
