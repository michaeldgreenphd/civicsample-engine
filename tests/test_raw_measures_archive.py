"""The permanent raw sex/gender measure archive takes only a whole file of this run.

The archive step uploads data/sex_gender_raw_measures.jsonl.gz to the
sex-gender-raw-measures release with --clobber, which replaces a same-day
asset. So:
- it runs only when the extraction succeeded (a failed or killed extraction
  can leave a partial file), and not only when the site push does;
- scripts/check_raw_measures.py refuses a truncated, malformed, empty,
  mixed-run or wrong-date file, checked here on files the real writer
  (src/extract_all.write_raw_measures) produces;
- the step's own run block, under bash -e with a stub gh, uploads a whole
  file and uploads nothing otherwise.
"""
from __future__ import annotations

import gzip
import os
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import check_raw_measures as crm  # noqa: E402
from src.extract_all import RAW_MEASURES_FILENAME, write_raw_measures  # noqa: E402

WORKFLOW = open(os.path.join(ROOT, ".github", "workflows", "extract.yml")).read()
DATE = "2026-10-04"
STAMPS = {"extracted_at": "2026-10-04T11:02:41.118204+00:00", "pipeline_commit": "3d2ded6"}


def _records(n: int = 3, **override: object) -> list[dict]:
    return [{"nct_id": f"NCT{i:08d}", "enrollment": 10 * i, "snapshot_date": DATE, **STAMPS,
             "measures": [{"title": "Sex: Female, Male"}], **override} for i in range(1, n + 1)]


def _write(tmp_path: pathlib.Path, records: list[dict]) -> pathlib.Path:
    path = tmp_path / "data" / RAW_MEASURES_FILENAME
    write_raw_measures(records, path)
    return path


def _step(name: str) -> str:
    m = re.search(rf"\n      - name: {re.escape(name)}\n(.*?)(?=\n      - name: |\Z)", WORKFLOW, re.S)
    assert m, f"extract.yml lost the step {name!r}"
    return m.group(1)


ARCHIVE = "Archive the retained sex/gender raw measures permanently"


def test_a_whole_file_of_this_run_passes(tmp_path: pathlib.Path) -> None:
    assert crm.check(str(_write(tmp_path, _records(5))), DATE) == (5, [])


def test_a_file_cut_short_mid_write_is_refused(tmp_path: pathlib.Path) -> None:
    # Large enough that a checker reading only a first stretch of the file
    # would never reach the cut.
    path = _write(tmp_path, _records(30_000))
    whole = path.read_bytes()
    path.write_bytes(whole[: len(whole) // 2])
    _, errors = crm.check(str(path), DATE)
    assert any("not a whole gzip file" in e for e in errors), errors
    path.write_bytes(whole[:-4])  # everything but the trailer's length field
    _, errors = crm.check(str(path), DATE)
    assert errors, "a file missing its gzip trailer passed"
    path.write_bytes(whole[:-1])
    records, errors = crm.check(str(path), DATE)
    assert errors, "a file one byte short passed"


@pytest.mark.parametrize("content,why", [
    (None, "not a whole gzip file"),
    (b"not gzip at all", "not a whole gzip file"),
    (gzip.compress(b""), "holds no records"),
    (gzip.compress(b'{"nct_id": "NCT00000001"\n'), "line 1 is not JSON"),
    (gzip.compress(b'["NCT00000001"]\n'), "line 1 is not a record with an nct_id"),
    (gzip.compress(b'{"enrollment": 3}\n'), "line 1 is not a record with an nct_id"),
    (gzip.compress(b"\xff\xfe\n"), "is not UTF-8 text"),
])
def test_a_missing_empty_or_malformed_file_is_refused(tmp_path: pathlib.Path, content: bytes | None, why: str) -> None:
    path = tmp_path / RAW_MEASURES_FILENAME
    if content is not None:
        path.write_bytes(content)
    _, errors = crm.check(str(path), DATE)
    assert any(why in e for e in errors), errors


def test_another_dates_file_is_refused(tmp_path: pathlib.Path) -> None:
    _, errors = crm.check(str(_write(tmp_path, _records(2, snapshot_date="2026-09-27"))), DATE)
    assert errors and all("not this run's 2026-10-04" in e for e in errors), errors


def test_a_file_mixing_two_runs_is_refused(tmp_path: pathlib.Path) -> None:
    recs = _records(3)
    recs[2]["extracted_at"] = "2026-10-04T15:40:00+00:00"
    _, errors = crm.check(str(_write(tmp_path, recs)), DATE)
    assert any("NCT00000003: from another run" in e for e in errors), errors


@pytest.mark.parametrize("missing", ["extracted_at", "pipeline_commit"])
def test_a_file_without_its_run_stamps_is_refused(tmp_path: pathlib.Path, missing: str) -> None:
    recs = _records(2)
    for r in recs:
        r.pop(missing)
    _, errors = crm.check(str(_write(tmp_path, recs)), DATE)
    assert any("extracted_at or pipeline_commit is missing" in e for e in errors), errors


def test_a_duplicate_trial_is_refused(tmp_path: pathlib.Path) -> None:
    recs = _records(2)
    recs.append(dict(recs[0]))
    _, errors = crm.check(str(_write(tmp_path, recs)), DATE)
    assert errors == ["NCT00000001 appears twice"]


def test_the_command_line_exits_1_with_error_lines(tmp_path: pathlib.Path) -> None:
    path = _write(tmp_path, _records(2, snapshot_date="2026-09-27"))
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "check_raw_measures.py"), str(path),
                        "--snapshot-date", DATE], capture_output=True, text=True)
    assert r.returncode == 1 and "::error::NCT00000001: snapshot_date" in r.stdout
    ok = _write(tmp_path, _records(2))
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "check_raw_measures.py"), str(ok),
                        "--snapshot-date", DATE], capture_output=True, text=True)
    assert r.returncode == 0 and "ok; 2 records" in r.stdout


def test_the_archive_runs_after_a_successful_extraction_only() -> None:
    assert re.search(r"\n      - name: Run extraction\n        id: extract\n", WORKFLOW), "the extraction step lost its id"
    step = _step(ARCHIVE)
    cond = re.search(r"^        if: (.+)$", step, re.M).group(1)
    assert cond == "${{ !cancelled() && steps.extract.outcome == 'success' }}", cond
    assert "hashFiles" not in cond, "a partial file from a failed extraction would be uploaded"
    assert "continue-on-error: true" in step


def test_a_refused_archive_turns_the_run_red() -> None:
    """The archive step carries on (continue-on-error); this last step is what fails the run."""
    archive = _step(ARCHIVE)
    assert re.search(r"^        id: sg_archive$", archive, re.M) and "continue-on-error: true" in archive
    gate = _step("Fail the run if the permanent raw-measure archive failed")
    assert re.search(r"^        if: \$\{\{ !cancelled\(\) && steps\.sg_archive\.outcome == 'failure' \}\}$", gate, re.M), \
        "the gate must read outcome (conclusion is 'success' under continue-on-error)"
    assert re.search(r"^          exit 1$", gate, re.M)
    order = [m.group(1) for m in re.finditer(r"\n      - name: (.+)", WORKFLOW)]
    assert order[-1] == "Fail the run if the permanent raw-measure archive failed"


def _run_archive(tmp_path: pathlib.Path, records: list[dict] | None, cut: bool = False) -> tuple[subprocess.CompletedProcess[str], str]:
    body = re.search(r"^        run: \|\n(.*)", _step(ARCHIVE), re.S | re.M).group(1)
    block = []
    for line in body.splitlines():
        if line.strip() and not line.startswith(" " * 10):
            break
        block.append(line[10:])
    assert block[-1].startswith('echo "Archived'), "the run block was not read whole"
    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True)
    (work / "scripts" / "check_raw_measures.py").write_text(
        open(os.path.join(ROOT, "scripts", "check_raw_measures.py")).read())
    if records is not None:
        path = _write(work, records)
        if cut:
            path.write_bytes(path.read_bytes()[:-20])
    stub = tmp_path / "bin"
    stub.mkdir()
    log = tmp_path / "gh.log"
    (stub / "gh").write_text('#!/bin/bash\necho "gh $*" >> "$CALLS"\n[ "$1 $2" = "release view" ] && exit 0\nexit 0\n')
    (stub / "python3").write_text(f'#!/bin/bash\nexec "{sys.executable}" "$@"\n')
    for f in ("gh", "python3"):
        (stub / f).chmod(0o755)
    (tmp_path / "runner").mkdir()
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}", "CALLS": str(log), "CURRENT_DATE": DATE,
           "RUNNER_TEMP": str(tmp_path / "runner"), "GITHUB_REPOSITORY": "owner/engine"}
    r = subprocess.run(["bash", "-e", "-c", "\n".join(block)], cwd=work, env=env, capture_output=True, text=True)
    return r, log.read_text() if log.exists() else ""


def test_the_archive_step_uploads_a_whole_file(tmp_path: pathlib.Path) -> None:
    r, calls = _run_archive(tmp_path, _records(3))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"gh release upload sex-gender-raw-measures {tmp_path}/runner/{DATE}_sex_gender_raw_measures.jsonl.gz --clobber" in calls


@pytest.mark.parametrize("records,cut", [
    (_records(300), True),
    (_records(2, snapshot_date="2026-09-27"), False),
    ([], False),
    (None, False),
], ids=["cut-short", "another-date", "empty", "missing"])
def test_the_archive_step_uploads_nothing_that_is_not_whole(tmp_path: pathlib.Path, records: list[dict] | None, cut: bool) -> None:
    r, calls = _run_archive(tmp_path, records, cut)
    assert r.returncode != 0, "the step passed, so the run would not go red"
    assert "release upload" not in calls and "release create" not in calls, calls
    assert f"::error::the raw sex/gender measures for {DATE} are not whole" in r.stdout
