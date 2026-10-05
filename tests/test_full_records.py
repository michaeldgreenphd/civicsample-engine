"""The week's full records, and who reads them.

Once the site's contract turns the split layout on, the site's files carry
only the fields the dashboard reads (scripts/split_data.py). Every engine
script that needs anything else (sponsors, status, ages, reference counts,
intervention descriptions) reads the full records through
src/full_records.py, which never falls back to the parts, and the weekly job
keeps those records on a data-* release that is never pruned. These tests
pin both halves:

- src.full_records.load reads the plain and gzipped file, keeps the
  provenance stamps, and exits (never falls back) when the file is missing
  or empty, even with parts lying next to it;
- generate_mobile_data, generate_industry_sponsors, generate_pilot_targets,
  the sponsor bridge loader and the sex/gender side-by-side read the full
  records and refuse to run without them, and the sex/gender table refuses
  parts cut in the split layout;
- extract.yml passes the full file to the generators, prunes no data-*
  release, retries a failed release, keeps the files as an artifact when it
  still fails, and turns the run red. The release step is run for real
  against a stub `gh` that logs its calls;
- the full records are compressed and released whenever the extraction
  succeeded, a failed sex/gender table build included, while that failure
  still skips the site publish (the step conditions are evaluated over the
  workflow as written), and the table build's write-back leaves the file
  whole when it fails.
"""
from __future__ import annotations

import gzip
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import full_records  # noqa: E402

STAMPS = {"extracted_at": "2026-09-27T11:49:53+00:00", "pipeline_commit": "12b9b65"}


def _study(i: int) -> dict:
    """A mock API study with sex and race baseline tables."""
    from validate_fixes import _make_study, _make_measure, _make_class, _make_category
    return _make_study(f"NCT0000000{i}", f"study {i}", [
        _make_measure("Sex: Female, Male", [_make_class("", [_make_category("Female", 40 + i), _make_category("Male", 35)])]),
        _make_measure("Race (NIH/OMB)", [_make_class("", [_make_category("White", 50 + i), _make_category("Black or African American", 25)])]),
    ], total_participants=75 + i)


def _records(n: int = 3) -> list[dict]:
    """Whole study records, built by the extractor from mock API studies."""
    from src.extract_all import extract_demographics_from_study
    return [extract_demographics_from_study(_study(i), snapshot_date="2026-09-27") for i in range(n)]


def _write_container(path: pathlib.Path, records: list[dict], gz: bool = False) -> None:
    body = json.dumps({**STAMPS, "data": records})
    if gz:
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(body)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)


def _write_parts(data_dir: pathlib.Path, records: list[dict]) -> None:
    for i in (1, 2):
        with gzip.open(os.path.join(data_dir, f"demographics.part{i}.json.gz"), "wt") as f:
            json.dump({**STAMPS, "part": i, "total_parts": 2, "data": records}, f)


def _run(script: str, *args: str, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, os.path.join(ROOT, "scripts", script), *args],
                          cwd=cwd, capture_output=True, text=True)


# ── src.full_records ────────────────────────────────────────────────────────

@pytest.mark.parametrize("gz", [False, True])
def test_load_reads_the_full_records_and_keeps_their_stamps(tmp_path: pathlib.Path, gz: bool) -> None:
    path = tmp_path / ("demographics.json.gz" if gz else "demographics.json")
    recs = _records()
    _write_container(path, recs, gz=gz)
    records, extracted_at, commit = full_records.load(str(path))
    assert [r["nct_id"] for r in records] == [r["nct_id"] for r in recs]
    assert (extracted_at, commit) == (STAMPS["extracted_at"], STAMPS["pipeline_commit"])
    assert "lead_sponsor_name" in records[0] and "study_sites" in records[0]


def test_load_never_falls_back_to_the_site_parts(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The parts where an old fallback would look for them: data/ under the cwd.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    _write_parts(tmp_path / "data", _records())
    with pytest.raises(SystemExit) as e:
        full_records.load()
    msg = str(e.value)
    assert "full study records not found" in msg
    assert "gh release download data-" in msg and "-R michaeldgreenphd/civicsample-engine" in msg
    assert "--demographics demographics.json.gz" in msg


def test_load_refuses_a_file_with_no_records(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "demographics.json"
    _write_container(path, [])
    with pytest.raises(SystemExit, match="no study records"):
        full_records.load(str(path))


# ── the readers ─────────────────────────────────────────────────────────────

def test_mobile_summary_is_built_from_the_full_records(tmp_path: pathlib.Path) -> None:
    (tmp_path / "data").mkdir()
    recs = _records(3)
    # Values the trimmed parts will not carry, so the summary can only have
    # them from the full records.
    recs[1]["min_age"], recs[1]["max_age"] = "18 Years", "65 Years"
    recs[1]["references"] = [{"pmid": "123", "citation": "c", "type": "RESULT", "source": "ctgov"},
                             {"pmid": "456", "citation": "d", "type": "RESULT", "source": "ctgov"}]
    _write_container(tmp_path / "data" / "demographics.json", recs)
    out = tmp_path / "summary.json"
    r = _run("generate_mobile_data.py", "--demographics", "data/demographics.json",
             "--table", "data/absent.csv.gz", "--out", str(out), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    summary = json.loads(out.read_text())
    assert summary["totalStudies"] == 3
    assert summary["extracted_at"] == STAMPS["extracted_at"] and summary["pipeline_commit"] == STAMPS["pipeline_commit"]
    rows = {r["nct_id"]: r for r in summary["recentStudies"]}
    row = rows[recs[1]["nct_id"]]
    assert row["status"] == "COMPLETED"
    assert (row["min_age"], row["max_age"]) == ("18 Years", "65 Years")
    assert row["reference_count"] == 2


@pytest.mark.parametrize("script,args,output", [
    ("generate_mobile_data.py", ["--out", "data/dashboard-summary.json"], "data/dashboard-summary.json"),
    ("generate_industry_sponsors.py", ["data"], "data/industry_sponsors.json"),
    ("generate_pilot_targets.py", [], "data/pilot_clinical_trials_targets.csv"),
])
def test_the_generators_refuse_to_run_on_the_site_parts_alone(tmp_path: pathlib.Path, script: str, args: list[str], output: str) -> None:
    (tmp_path / "data").mkdir()
    _write_parts(tmp_path / "data", _records())
    r = _run(script, *args, cwd=tmp_path)
    assert r.returncode != 0, f"{script} ran without the full records:\n{r.stdout}"
    assert "full study records not found" in (r.stdout + r.stderr)
    assert not (tmp_path / output).exists(), f"{script} wrote {output} anyway"


def test_industry_sponsors_reads_the_file_it_is_given(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    import generate_industry_sponsors as gis
    seen = []
    monkeypatch.setattr(gis.full_records, "load", lambda p: seen.append(p) or (_ for _ in ()).throw(SystemExit("stop")))
    with pytest.raises(SystemExit):
        gis.main([str(tmp_path), "--demographics", "elsewhere/demographics.json.gz"])
    assert seen == ["elsewhere/demographics.json.gz"]
    seen.clear()
    with pytest.raises(SystemExit):
        gis.main([str(tmp_path)])
    assert seen == [os.path.join(str(tmp_path), "demographics.json")]


def test_sponsor_loader_reads_the_full_records_and_never_the_parts(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from build_sponsor_bridge import load_records
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    _write_parts(tmp_path / "data", _records())
    with pytest.raises(SystemExit, match="full study records not found"):
        load_records("data/demographics.json")
    _write_container(tmp_path / "data" / "demographics.json", _records(2))
    records, extracted_at = load_records("data/demographics.json")
    assert len(records) == 2 and extracted_at == STAMPS["extracted_at"]


RULES = os.path.join(ROOT, "sponsors", "company_aliases.csv")


@pytest.mark.parametrize("script,args", [
    ("build_sponsor_bridge.py", ["--rules", RULES, "--out-dir", "out"]),
    ("sponsor_audit.py", ["--rules", RULES, "--snapshot-date", "2026-09-27"]),
    ("sponsor_concordance.py", ["--rules", RULES]),
    ("sponsor_curation_batch.py", ["--rules", RULES, "--out", "out.csv"]),
    ("sex_gender_side_by_side.py", ["--out-dir", "out"]),
])
def test_the_sponsor_and_status_readers_refuse_the_site_parts(tmp_path: pathlib.Path, script: str, args: list[str]) -> None:
    """These read lead_sponsor_name, collaborators or status, which the parts will drop."""
    (tmp_path / "data").mkdir()
    _write_parts(tmp_path / "data", _records())
    r = _run(script, *args, cwd=tmp_path)
    assert r.returncode != 0, f"{script} ran on the parts:\n{r.stdout}"
    assert "full study records not found" in (r.stdout + r.stderr), r.stderr[-400:]


# ── the sex/gender write-back ───────────────────────────────────────────────
# The weekly job's table build rewrites data/demographics.json in place
# (--write-back) and then compresses and releases it even when the build
# fails, so a failed build must leave the file whole.

def _pull(data_dir: pathlib.Path, n: int = 3) -> pathlib.Path:
    """A week's pull as the extraction leaves it: demographics.json with the
    extractor's lean sex_gender rows, and the retained raw measures, stamped
    alike."""
    from src import sex_gender_table as sgt
    from src.extract_all import extract_demographics_from_study
    studies = [_study(i) for i in range(n)]
    _write_container(data_dir / "demographics.json",
                     [extract_demographics_from_study(s, snapshot_date="2026-09-27") for s in studies])
    with gzip.open(data_dir / "sex_gender_raw_measures.jsonl.gz", "wt") as f:
        for s in studies:
            f.write(json.dumps({**sgt.select_raw_measures(s), "snapshot_date": "2026-09-27", **STAMPS}) + "\n")
    return data_dir / "demographics.json"


def _build_table(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """scripts/build_sex_gender_table.py with the weekly job's arguments, in-process."""
    import build_sex_gender_table
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["build_sex_gender_table.py", "--from-raw", "data/sex_gender_raw_measures.jsonl.gz",
                                      "--snapshot-date", "2026-09-27", "--write-back", "data/demographics.json", "--strict"])
    return build_sex_gender_table.main()


def test_a_strict_failure_leaves_the_full_records_whole_and_written_back(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--strict exits 1 only after the write-back, so what the job then
    releases is the whole week: every record, every field, the rebuilt rows."""
    import build_sex_gender_table
    from src import sex_gender_table as sgt
    (tmp_path / "data").mkdir()
    path = _pull(tmp_path / "data")
    before = json.loads(path.read_text())
    monkeypatch.setattr(build_sex_gender_table.sgt, "structural_checks", lambda rows: {"forced": {"pass": False, "detail": "test"}})
    assert _build_table(tmp_path, monkeypatch) == 1
    after = json.loads(path.read_text())
    assert {k: v for k, v in after.items() if k != "data"} == {k: v for k, v in before.items() if k != "data"}
    assert [r["nct_id"] for r in after["data"]] == [r["nct_id"] for r in before["data"]]
    for b, a in zip(before["data"], after["data"]):
        assert {k: v for k, v in a.items() if k != "sex_gender"} == {k: v for k, v in b.items() if k != "sex_gender"}
        assert list(a["sex_gender"]) == sgt.LEAN_COLUMNS
        # the rebuild from the raw measures reproduces the extractor's row
        assert a["sex_gender"] == b["sex_gender"]
    meta = json.loads((tmp_path / "data" / "sex_gender_parsed_meta.json").read_text())
    assert meta["structural_checks_all_pass"] is False and meta["written_back"]["updated"] == 3
    assert not (tmp_path / "data" / "demographics.json.tmp").exists()


def test_a_failed_write_back_leaves_the_pulls_own_file(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A full disk (or any error) mid-dump leaves data/demographics.json as the
    extraction wrote it, never truncated, and no temporary file behind."""
    import build_sex_gender_table
    (tmp_path / "data").mkdir()
    path = _pull(tmp_path / "data")
    before = path.read_bytes()

    def full_disk(obj: object, f: object, **kw: object) -> None:
        f.write('{"extracted_at": "2026-')
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(build_sex_gender_table.json, "dump", full_disk)
    with pytest.raises(OSError, match="No space left"):
        _build_table(tmp_path, monkeypatch)
    assert path.read_bytes() == before
    assert not (tmp_path / "data" / "demographics.json.tmp").exists()


# ── extract.yml ─────────────────────────────────────────────────────────────

WORKFLOW = open(os.path.join(ROOT, ".github", "workflows", "extract.yml")).read()


def _step(name: str) -> str:
    m = re.search(rf"\n      - name: {re.escape(name)}\n(.*?)(?=\n      - name: |\Z)", WORKFLOW, re.S)
    assert m, f"extract.yml lost the step {name!r}"
    return m.group(1)


def _steps() -> list[dict]:
    """extract.yml's named steps in order: name, id, if, continue-on-error."""
    out = []
    for m in re.finditer(r"\n      - name: ([^\n]+)\n(.*?)(?=\n      - name: |\Z)", WORKFLOW, re.S):
        body = m.group(2)
        cond = re.search(r"^        if: (.+)$", body, re.M)
        sid = re.search(r"^        id: (\S+)$", body, re.M)
        out.append({"name": m.group(1), "id": sid.group(1) if sid else None, "if": cond.group(1) if cond else None,
                    "continue_on_error": re.search(r"^        continue-on-error: true$", body, re.M) is not None})
    return out


def _runs(cond: str | None, outcomes: dict[str, str], job_failed: bool, env: dict[str, str]) -> bool:
    """A step's if:, as GitHub evaluates it, for the expressions extract.yml
    uses. Without a status function the condition is success() && (cond), so
    a step with no if: is skipped once any earlier step has failed."""
    expr = (cond or "success()").strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2].strip()
    if not re.search(r"\b(always|cancelled|success|failure)\(\)", expr):
        expr = f"success() && ({expr})"
    py = (expr.replace("&&", " and ").replace("||", " or ").replace("!cancelled()", "True")
          .replace("always()", "True").replace("success()", str(not job_failed)).replace("failure()", str(job_failed)))
    py = re.sub(r"steps\.(\w+)\.outcome", lambda m: repr(outcomes.get(m.group(1), "skipped")), py)
    py = re.sub(r"env\.(\w+)", lambda m: repr(env.get(m.group(1), "")), py)
    words = set(re.findall(r"[A-Za-z_]\w*", re.sub(r"'[^']*'", "", py)))
    assert words <= {"and", "or", "not", "True", "False"}, f"an if: this evaluator does not cover: {cond}"
    return bool(eval(py))  # noqa: S307 - only the literals and operators checked above


def _simulate(failing: set[str], drive: bool = False) -> dict[str, str]:
    """Each step's outcome by name ('success', 'failure', 'skipped') when the
    steps named in failing fail and every other step that runs succeeds."""
    outcomes: dict[str, str] = {}
    result: dict[str, str] = {}
    job_failed = False
    for step in _steps():
        ran = _runs(step["if"], outcomes, job_failed, {"GDRIVE_ENABLED": "true" if drive else "false"})
        outcome = ("failure" if step["name"] in failing else "success") if ran else "skipped"
        result[step["name"]] = outcome
        if step["id"]:
            outcomes[step["id"]] = outcome
        job_failed = job_failed or (outcome == "failure" and not step["continue_on_error"])
    return result


EXTRACT = "Run extraction"
BUILD = "Build the sex/gender table (full record) and write the lean rows back"
COMPRESS = "Compress CT.gov Data for Drive"
DRIVE = ("Upload CT.gov Data to Google Drive via OAuth", "Upload retained sex/gender raw measures to Google Drive")
RELEASE = "Keep the week's full records on a permanent GitHub Release"
ARTIFACT = "Keep the full records as a workflow artifact if the release failed"
RAW_ARCHIVE = "Archive the retained sex/gender raw measures permanently"
SUMMARY = "Write run summary"
RELEASE_GATE = "Fail the run if the week's full-record release failed"
SETUP = ("Set current date", "Set up Python", "Install dependencies")


def _ran(outcomes: dict[str, str]) -> set[str]:
    return {name for name, o in outcomes.items() if o != "skipped"}


@pytest.mark.parametrize("drive", [False, True], ids=["no-drive", "drive"])
def test_a_failed_sex_gender_build_keeps_the_full_records_and_publishes_nothing(drive: bool) -> None:
    """The build's --strict failure stops the site publish and everything
    after it, but the week's full records are still compressed and released
    (a re-run pulls the registry again and cannot recover the week). Exactly
    these steps run; any other step added later that runs after a failed
    build has to be added here deliberately."""
    out = _simulate({BUILD}, drive=drive)
    keep = {COMPRESS, RELEASE, RAW_ARCHIVE, *(DRIVE if drive else ())}
    assert _ran(out) == {*SETUP, EXTRACT, BUILD, *keep, SUMMARY}, sorted(_ran(out))
    assert all(out[s] == "success" for s in keep)
    assert out["Check out the site repository"] == "skipped"
    assert out["Publish artifacts, archive snapshot, and push to the site"] == "skipped"
    # ...and a release that then fails still leaves the artifact and a red run
    out = _simulate({BUILD, RELEASE}, drive=drive)
    assert out[ARTIFACT] == "success" and out[RELEASE_GATE] == "success"
    assert out["Publish artifacts, archive snapshot, and push to the site"] == "skipped"


def test_a_failed_extraction_releases_nothing() -> None:
    """A failed or killed extraction can leave a partial file; nothing of it is
    compressed, released or archived (unchanged by keying on the extraction)."""
    out = _simulate({EXTRACT}, drive=True)
    assert _ran(out) == {*SETUP, EXTRACT, SUMMARY}, sorted(_ran(out))


def test_a_clean_run_runs_every_step_but_the_failure_paths() -> None:
    out = _simulate(set(), drive=True)
    assert {n for n, o in out.items() if o == "skipped"} == {ARTIFACT, RELEASE_GATE,
                                                            "Fail the run if the permanent raw-measure archive failed"}


def test_the_full_file_is_compressed_whenever_the_extraction_succeeded() -> None:
    order = [s["name"] for s in _steps()]
    assert order.index(EXTRACT) < order.index(BUILD) < order.index(COMPRESS) < order.index(RELEASE)
    assert order.index(COMPRESS) < order.index(DRIVE[0]) and order.index(COMPRESS) < order.index(DRIVE[1])
    compress = _step(COMPRESS)
    assert "if: ${{ !cancelled() && steps.extract.outcome == 'success' }}" in compress, \
        "a failed sex/gender table build would skip the full-record release again"
    assert "run: gzip -k data/demographics.json" in compress
    assert "id: extract" in _step(EXTRACT)
    assert "steps.compress_full.outcome == 'success'" in _step(DRIVE[0])
    assert "steps.extract.outcome == 'success'" in _step(DRIVE[1])


def _release_block() -> str:
    step = _step("Keep the week's full records on a permanent GitHub Release")
    m = re.search(r"\n        run: \|\n(.*)", step, re.S)
    assert m, "the release step lost its run block"
    return "\n".join(line[10:] for line in m.group(1).splitlines())


def _run_release(tmp_path: pathlib.Path, view_rc: int, fail_first: int) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the release step under bash -e with stub gh and sleep on PATH.

    gh exits view_rc for `release view`, and fails its first fail_first
    create/upload calls before succeeding."""
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "gh").write_text(
        '#!/bin/bash\necho "$@" >> "$GH_LOG"\n'
        'if [ "$2" = "view" ]; then exit "$VIEW_RC"; fi\n'
        'n=$(cat "$GH_COUNT" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$GH_COUNT"\n'
        'if [ "$n" -le "$FAIL_FIRST" ]; then exit 1; fi\nexit 0\n')
    (stub / "sleep").write_text("#!/bin/bash\nexit 0\n")
    for f in ("gh", "sleep"):
        (stub / f).chmod(0o755)
    (tmp_path / "extraction_log.txt").write_text("log")
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}", "GH_LOG": str(tmp_path / "gh.log"),
           "GH_COUNT": str(tmp_path / "gh.count"), "VIEW_RC": str(view_rc), "FAIL_FIRST": str(fail_first),
           "CURRENT_DATE": "2026-09-27", "GITHUB_REPOSITORY": "o/r", "GITHUB_SHA": "abc"}
    r = subprocess.run(["bash", "-e", "-c", _release_block()], cwd=tmp_path, env=env,
                       capture_output=True, text=True)
    calls = (tmp_path / "gh.log").read_text().splitlines()
    return r, [c for c in calls if not c.startswith("release view")]


@pytest.mark.parametrize("view_rc", [0, 1], ids=["release-exists", "new-release"])
@pytest.mark.parametrize("fail_first,ok", [(0, True), (2, True), (3, False)], ids=["ok", "retried", "failed"])
def test_the_release_step_keeps_the_full_records_or_fails(tmp_path: pathlib.Path, view_rc: int, fail_first: int, ok: bool) -> None:
    r, calls = _run_release(tmp_path, view_rc, fail_first)
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    assert len(calls) == min(fail_first + 1, 3), calls
    for call in calls:
        assert ("release upload" if view_rc == 0 else "release create") in call
        assert "data/demographics.json.gz" in call.split(), f"a release call without the full records: {call}"
    if not ok:
        assert "artifact full-records-2026-09-27" in r.stdout


def test_the_weekly_job_never_deletes_a_release() -> None:
    assert not re.search(r"release\s+delete|-X\s*DELETE|--method\s+DELETE", WORKFLOW), "extract.yml deletes releases"


def test_the_release_runs_before_the_site_and_is_kept_when_it_fails() -> None:
    order = [m.group(1) for m in re.finditer(r"\n      - name: (.+)", WORKFLOW)]
    release = order.index("Keep the week's full records on a permanent GitHub Release")
    assert release < order.index("Check out the site repository"), "a site-side failure could skip the release"
    assert release > order.index("Compress CT.gov Data for Drive")
    step = _step("Keep the week's full records on a permanent GitHub Release")
    assert "id: data_release" in step and "continue-on-error: true" in step
    assert "steps.compress_full.outcome == 'success'" in step, "the release depends on more than the full file"
    assert "id: compress_full" in _step("Compress CT.gov Data for Drive")
    artifact = _step("Keep the full records as a workflow artifact if the release failed")
    assert "steps.data_release.outcome == 'failure'" in artifact and "actions/upload-artifact" in artifact
    for f in ("data/demographics.json.gz", "data/sex_gender_raw_measures.jsonl.gz", "extraction_log.txt.gz"):
        assert f in artifact
    assert order.index("Keep the full records as a workflow artifact if the release failed") == release + 1
    gate = _step("Fail the run if the week's full-record release failed")
    assert "steps.data_release.outcome == 'failure'" in gate and "exit 1" in gate
    assert order.index("Fail the run if the week's full-record release failed") > order.index("Write run summary"), \
        "the gate must come after the independent steps"


def test_the_weekly_job_builds_the_summaries_from_the_full_records() -> None:
    step = _step("Generate dashboard artifacts")
    assert "generate_mobile_data.py --demographics data/demographics.json" in step
    assert "generate_industry_sponsors.py --demographics data/demographics.json" in step
    split = _step("Cut the week's records into the site's dataset files")
    assert "--demographics data/demographics.json" in split, "the split reads something other than the full records"


def test_the_sex_gender_table_refuses_parts_cut_in_the_split_layout(tmp_path: pathlib.Path) -> None:
    """--parts on split core parts would tabulate rows missing the fields core does not carry."""
    import split_helpers
    contract = split_helpers.site_contract(enabled=True)
    dataset = split_helpers.split(tmp_path / "cut", split_helpers.whole_records(split_helpers.ids(16)), contract)
    (tmp_path / "data").mkdir()
    for p in dataset.glob("demographics.part*.json.gz"):
        (tmp_path / "data" / p.name).write_bytes(p.read_bytes())
    r = _run("build_sex_gender_table.py", "--parts", "data/demographics.part*.json.gz", "--out", "t.csv.gz",
             "--meta", "m.json", cwd=tmp_path)
    assert r.returncode != 0 and "split layout" in (r.stdout + r.stderr), r.stdout + r.stderr
    assert not (tmp_path / "t.csv.gz").exists() and not (tmp_path / "m.json").exists()
