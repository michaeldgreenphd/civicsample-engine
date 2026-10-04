"""The publish step's check against the site's own contract.

scripts/check_site_contract.py reads the site's tests/record_contract.json
and tests/data_budget.json and the staged parts, and decides whether the
weekly push may happen. These tests build a small site checkout (a contract
with nested and list paths, a 2-part budget, two parts) and pin the rules:

- a record missing a listed path blocks; a null or empty value does not, and
  an optional path need not be there;
- a null or non-object parent counts as missing, as in the site's own test;
- a malformed part, an empty part, a part from another run, bad stamps, a
  duplicate nct_id or the wrong set of parts blocks;
- a missing path is reported once per path with a count;
- over the site's budget only warns; over GitHub's per-file push limit or
  GitHub Pages' size limit blocks;
- an unreadable contract or budget blocks, and every outcome writes a report;
- extract.yml runs the check after staging and before the commit and push
  (the publish step is run under bash -e with stub git and check), and the
  run summary's jq reads the keys the report carries;
- the raw-measure archive does not depend on the site push (what it takes
  is tests/test_raw_measures_archive.py).
"""
from __future__ import annotations

import gzip
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "check_site_contract.py")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import check_site_contract as csc  # noqa: E402

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


def _step(name: str) -> str:
    m = re.search(rf"\n      - name: {re.escape(name)}\n(.*?)(?=\n      - name: |\Z)", WORKFLOW, re.S)
    assert m, f"extract.yml lost the step {name!r}"
    return m.group(1)


CHECK_LINE = 'python3 ../scripts/check_site_contract.py --site . --report "$RUNNER_TEMP/site_check.json"'


def test_the_publish_step_runs_the_check_after_staging_and_before_the_push() -> None:
    step = _step("Publish artifacts, archive snapshot, and push to the site")
    lines = [line.strip() for line in step.splitlines()]
    assert CHECK_LINE in lines, "the check line was changed (an || or a wrapper makes it advisory)"
    check = lines.index(CHECK_LINE)
    assert lines.index("git add -A snapshots/ history.json") < check, "the check runs before everything is staged"
    assert lines.index("python3 ../scripts/prune_snapshots.py") < check, "the check runs before pruning settles the tree"
    assert check < lines.index('git commit -m "Update demographics data $DATE$REPLACED" || exit 0') < lines.index("git push")
    assert "set +e" not in step and "continue-on-error" not in step, "the publish step no longer stops on a failed check"


@pytest.mark.parametrize("check_rc,pushed", [(1, False), (0, True)])
def test_a_failed_check_stops_the_publish_step_before_commit_and_push(tmp_path: pathlib.Path, check_rc: int, pushed: bool) -> None:
    """The step's own run block, under bash -e, with stub git/cp/jq/mv/python3."""
    body = re.search(r"^        run: \|\n(.*)", _step("Publish artifacts, archive snapshot, and push to the site"), re.S | re.M).group(1)
    block = []
    for line in body.splitlines():
        if line.strip() and not line.startswith(" " * 10):
            break  # the run block ends where its indentation does
        block.append(line[10:])
    body = "\n".join(block)
    assert body.rstrip().endswith("git push"), "the run block was not read whole"
    stub = tmp_path / "bin"
    stub.mkdir()
    log = tmp_path / "calls.log"
    for name, script in {
        "git": 'echo "git $*" >> "$CALLS"',
        "cp": 'echo "cp $*" >> "$CALLS"',
        "mv": "exit 0",
        "jq": "echo '{}'",
        "python3": 'echo "python3 $*" >> "$CALLS"; case "$*" in *check_site_contract.py*) exit "$CHECK_RC";; esac; exit 0',
    }.items():
        (stub / name).write_text(f"#!/bin/bash\n{script}\n")
        (stub / name).chmod(0o755)
    (tmp_path / "site").mkdir()
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}", "CALLS": str(log), "CHECK_RC": str(check_rc),
           "CURRENT_DATE": "2026-10-04", "RUNNER_TEMP": str(tmp_path)}
    r = subprocess.run(["bash", "-e", "-c", body], cwd=tmp_path, env=env, capture_output=True, text=True)
    calls = log.read_text()
    assert "check_site_contract.py" in calls
    assert (r.returncode == 0) is pushed, r.stdout + r.stderr
    assert ("git commit" in calls) is pushed and ("git push" in calls) is pushed, calls


def test_the_run_summary_reads_what_the_report_carries(tmp_path: pathlib.Path) -> None:
    jq = shutil.which("jq")
    if not jq:
        pytest.skip("jq is not installed here (the runner has it)")
    summary = _step("Write run summary")
    m = re.search(r"jq -r '([^']*)' \"\$RUNNER_TEMP/site_check\.json\"", summary)
    assert m, "the run summary lost the site check row"
    program = m.group(1)
    assert '[ -f "$RUNNER_TEMP/site_check.json" ]' in summary and '--report "$RUNNER_TEMP/site_check.json"' in WORKFLOW
    _, report = _check(_site(tmp_path))
    read = set(re.findall(r"\.([a-z_]+)", program))
    assert read <= set(report), f"the summary reads keys the report lacks: {read - set(report)}"
    big = tmp_path / "big.json"
    big.write_text(json.dumps({**report, "records": 80210, "parts_gzip_bytes": 152030874,
                               "total_budget_bytes": 160000000, "tree_bytes": 863357580}))
    row = subprocess.run([jq, "-r", program, str(big)], capture_output=True, text=True).stdout.strip()
    assert row == "passed: 80210 records; parts 152 MB of a 160 MB budget; site 863 MB; 0 errors, 0 warnings"
    failing = tmp_path / "fail.json"
    failing.write_text(json.dumps({"ok": False, "errors": ["x"], "warnings": []}))
    row = subprocess.run([jq, "-r", program, str(failing)], capture_output=True, text=True).stdout.strip()
    assert row.startswith("FAILED, nothing published") and row.endswith("1 errors, 0 warnings")


def test_the_raw_measure_archive_does_not_depend_on_the_site_push() -> None:
    order = [m.group(1) for m in re.finditer(r"\n      - name: (.+)", WORKFLOW)]
    archive = order.index("Archive the retained sex/gender raw measures permanently")
    assert archive < order.index("Check out the site repository") < order.index("Publish artifacts, archive snapshot, and push to the site")
    step = _step("Archive the retained sex/gender raw measures permanently")
    assert "!cancelled()" in step and "steps.extract.outcome == 'success'" in step
    assert "continue-on-error: true" in step
