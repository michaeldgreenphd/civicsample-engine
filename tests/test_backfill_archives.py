"""scripts/backfill_archives.py: old archives rebuilt from the site's git history.

Each test runs on a site checkout that is a real git repository with the
history the real site has, in miniature (snapshot_helpers' weeks, cut by
scripts/split_data.py):

  commit FEB    snapshots/2026-02-22/: the eight whole-record parts only, with
                no pipeline_commit in their headers (cb50c082 on the site);
  commit JUL    snapshots/2026-07-26/: a week with its summary (433eee4);
  commit SEP    snapshots/2026-09-27/: a week with its summary and sex/gender
                pair (4a1df0a);
  then          02-22 slimmed to its summary, 07-26 and 09-27 deleted (the old
                retention rule), and today's site: data/ serving 2026-10-04,
                08-02 complete, 03-29 an aggregate, history.json as
                scripts/prune_snapshots.py writes it.

- It builds 02-22's archive file from the FEB parts (source_pipeline_commit
  null) and restores 07-26 and 09-27 as aggregates holding exactly an
  aggregate's files, the summary byte for byte from its commit; every archive
  file is archive_records.build's for that week's own folder, byte for byte,
  covering exactly its recentStudies; history.json lists them as prune writes
  it, and the result passes the publish gate.
- Retention keeps the restored months as aggregates on this run and on the
  next three weekly runs, and changes nothing else it keeps.
- It refuses, writing nothing: a commit that does not hold the folder, one not
  in the site's history, parts from two runs, a run of another date, a
  projection that loses a field, a history.json not in prune's form, a
  complete snapshot in the way, a restored date retention would delete or
  that would change what else retention keeps.
- A second run writes nothing; a run that stopped part way is completed.
- Without --write nothing in the site changes; the workflow pushes only when
  dry_run is unchecked, and shares the weekly extract's concurrency group.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Any

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import backfill_archives as ba  # noqa: E402
import prune_snapshots as ps  # noqa: E402
import snapshot_helpers as sh  # noqa: E402
from src import archive_records  # noqa: E402
from src import dataset_folder as df  # noqa: E402
from src import site_layout as sl  # noqa: E402

FEB, MAR, JUL, AUG, SEP, LATEST = "2026-02-22", "2026-03-29", "2026-07-26", "2026-08-02", "2026-09-27", "2026-10-04"
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: pathlib.Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _without_commit(folder: pathlib.Path) -> None:
    """A week as the runs before 2026-09 published it: no pipeline_commit in its
    parts or summary, and no run.json."""
    for part in folder.glob("demographics.part*.json.gz"):
        body = json.loads(gzip.decompress(part.read_bytes()))
        body.pop("pipeline_commit")
        part.write_bytes(gzip.compress(json.dumps(body).encode(), mtime=0))
    (folder / "run.json").unlink(missing_ok=True)
    summary = folder / "dashboard-summary.json"
    if summary.exists():
        doc = json.loads(summary.read_text())
        doc.pop("pipeline_commit")
        summary.write_text(json.dumps(doc))


def _laid_out(path: pathlib.Path) -> None:
    """A summary in bytes json.dumps would not give back (indented, a
    trailing newline), so "byte for byte" can tell a copy from a re-dump."""
    path.write_text(json.dumps(json.loads(path.read_text()), indent=1) + "\n")


def _build_template(tmp: pathlib.Path) -> tuple[pathlib.Path, dict[str, str]]:
    site = tmp / "site"
    site.mkdir()
    _git(site, "init", "-q", "-b", "main")
    _git(site, "config", "user.email", "t@example.org")
    _git(site, "config", "user.name", "t")
    commits: dict[str, str] = {}
    snaps = site / "snapshots"
    sh.write_week(tmp, snaps / FEB, FEB, extras=False)
    _without_commit(snaps / FEB)
    (snaps / FEB / "dashboard-summary.json").unlink()
    commits[FEB] = _commit(site, "Backfill historical snapshot directories")
    sh.write_week(tmp, snaps / JUL, JUL, extras=False)
    _without_commit(snaps / JUL)
    _laid_out(snaps / JUL / "dashboard-summary.json")
    commits[JUL] = _commit(site, f"Update demographics data {JUL}")
    sh.write_week(tmp, snaps / SEP, SEP)
    _laid_out(snaps / SEP / "dashboard-summary.json")
    commits[SEP] = _commit(site, f"Update demographics data {SEP}")
    # The old rule: February slimmed to its summary, July and September deleted.
    for p in (snaps / FEB).iterdir():
        p.unlink()
    summary = sh.summary(FEB)
    summary.pop("pipeline_commit")
    (snaps / FEB / "dashboard-summary.json").write_text(json.dumps(summary))
    shutil.rmtree(snaps / JUL)
    shutil.rmtree(snaps / SEP)
    _commit(site, "Monthly snapshots become aggregate archives")
    # Today's site, with history.json as the retention script writes it.
    base = sh.make_site(tmp, LATEST, complete=[AUG], aggregates=[MAR], name="base")
    shutil.copytree(base, site, dirs_exist_ok=True)
    (site / "history.json").write_text(json.dumps({"dates": sorted([FEB, MAR, AUG, LATEST])}))
    out = ps.run(str(site), LATEST, None, dry=False)
    assert out is not None and not out.warnings, out and out.warnings
    _commit(site, f"Update demographics data {LATEST}")
    return site, commits


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> tuple[pathlib.Path, dict[str, str]]:
    return _build_template(tmp_path_factory.mktemp("backfill"))


@pytest.fixture
def site(template: tuple[pathlib.Path, dict[str, str]], tmp_path: pathlib.Path) -> pathlib.Path:
    dst = tmp_path / "site"
    shutil.copytree(template[0], dst, symlinks=True)
    return dst


@pytest.fixture
def commits(template: tuple[pathlib.Path, dict[str, str]]) -> dict[str, str]:
    return template[1]


def targets(commits: dict[str, str]) -> list[ba.Target]:
    return [ba.Target(FEB, commits[FEB], restore=False), ba.Target(JUL, commits[JUL], restore=True),
            ba.Target(SEP, commits[SEP], restore=True)]


def digest(root: pathlib.Path) -> str:
    """Every path and byte of the working tree (not .git)."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if rel.parts[0] == ".git":
            continue
        h.update(str(rel).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def history(site: pathlib.Path) -> dict[str, Any]:
    return json.loads((site / "history.json").read_text())


def at_commit(site: pathlib.Path, commit: str, path: str) -> bytes:
    return subprocess.run(["git", "-C", str(site), "show", f"{commit}:{path}"], check=True,
                          capture_output=True).stdout


def own_archive(site: pathlib.Path, tmp: pathlib.Path, commit: str, d: str, summary: bytes) -> bytes:
    """What prune_snapshots.py would have written slimming that week's folder:
    archive_records.build over the commit's own files and that summary."""
    folder = tmp / f"own-{d}"
    folder.mkdir()
    subprocess.run(f"git -C '{site}' archive {commit} snapshots/{d} | tar -x -C '{folder}' --strip-components=2",
                   shell=True, check=True)
    (folder / "dashboard-summary.json").write_bytes(summary)
    contract = json.loads((site / "tests" / "record_contract.json").read_text())
    return archive_records.encode(archive_records.build(df.inspect(str(folder)), contract))


# ── what it builds ──────────────────────────────────────────────────────────

def test_it_builds_each_archive_from_its_own_commit_and_lists_it(site: pathlib.Path, commits: dict[str, str],
                                                                 tmp_path: pathlib.Path) -> None:
    report = ba.run(str(site), targets(commits), write=True)
    assert report["ok"] and report["gate"]["ok"], report["gate"]
    snaps = site / "snapshots"
    for d in (JUL, SEP):
        assert sorted(os.listdir(snaps / d)) == sorted(ps.AGGREGATE_KEEP), d
        assert (snaps / d / "dashboard-summary.json").read_bytes() == \
            at_commit(site, commits[d], f"snapshots/{d}/dashboard-summary.json"), "the summary, byte for byte"
    assert not (snaps / SEP / "sex_gender_parsed_meta.json").exists(), "an aggregate keeps no sex/gender pair"
    assert sorted(os.listdir(snaps / FEB)) == sorted(ps.AGGREGATE_KEEP)
    for d in (FEB, JUL, SEP):
        summary_bytes = (snaps / d / "dashboard-summary.json").read_bytes()
        archive = snaps / d / sl.ARCHIVE_FILE
        assert archive.read_bytes() == own_archive(site, tmp_path, commits[d], d, summary_bytes), d
        summary = json.loads(summary_bytes)
        assert archive_records.problems(str(archive), summary) == []
        doc = json.loads(gzip.decompress(archive.read_bytes()))
        assert list(doc["data"]) == [r["nct_id"] for r in summary["recentStudies"]]
        assert doc["source_extracted_at"] == sh.stamps(d)["extracted_at"]
    assert report["changed"] == sorted([f"snapshots/{FEB}/{sl.ARCHIVE_FILE}", f"snapshots/{JUL}/{sl.ARCHIVE_FILE}",
                                        f"snapshots/{JUL}/dashboard-summary.json",
                                        f"snapshots/{SEP}/{sl.ARCHIVE_FILE}",
                                        f"snapshots/{SEP}/dashboard-summary.json"]) + ["history.json"]
    by_date = {t["date"]: t for t in report["targets"]}
    assert all(t["archive_records"] == t["recent_studies"] == t["covered"] == sh.RECORDS for t in by_date.values())


def test_the_02_22_parts_carry_no_commit_so_its_archive_names_none(site: pathlib.Path,
                                                                   commits: dict[str, str]) -> None:
    head = json.loads(gzip.decompress(at_commit(site, commits[FEB], f"snapshots/{FEB}/demographics.part1.json.gz")))
    assert "pipeline_commit" not in head
    ba.run(str(site), targets(commits), write=True)
    doc = json.loads(gzip.decompress((site / "snapshots" / FEB / sl.ARCHIVE_FILE).read_bytes()))
    assert "source_pipeline_commit" in doc and doc["source_pipeline_commit"] is None
    sep = json.loads(gzip.decompress((site / "snapshots" / SEP / sl.ARCHIVE_FILE).read_bytes()))
    assert sep["source_pipeline_commit"] == sh.stamps(SEP)["pipeline_commit"]


def test_history_lists_them_in_the_form_prune_writes(site: pathlib.Path, commits: dict[str, str]) -> None:
    ba.run(str(site), targets(commits), write=True)
    raw = (site / "history.json").read_text()
    doc = json.loads(raw)
    assert list(doc) == ["dates", "latest", "archives"]
    assert doc["dates"] == [FEB, MAR, JUL, AUG, SEP, LATEST] == sorted(doc["dates"])
    assert doc["latest"] == LATEST
    detail = {"kind": "aggregate", "detail": sl.ARCHIVE_FILE}
    assert doc["archives"] == {FEB: detail, MAR: {"kind": "aggregate"}, JUL: detail, SEP: detail}
    assert list(doc["archives"]) == sorted(doc["archives"])
    assert raw == json.dumps(doc, indent=2) + "\n", "prune's write_history bytes"
    # And prune, run now, would write the same history.json and delete nothing.
    out = ps.run(str(site), LATEST, None, dry=True)
    assert out is not None and out.removed == [] and out.slimmed == [] and not out.stripped
    assert ps.history_doc(str(site), LATEST, {**{d: ps.AGGREGATE for d in (FEB, MAR, JUL, SEP)}, AUG: ps.COMPLETE},
                          ps.Outcome(LATEST, LATEST, True)) == doc


def test_history_doc_sorts_dates_and_archives() -> None:
    hist = {"dates": ["2026-10-04", "2026-02-22"], "latest": "2026-10-04",
            "archives": {"2026-03-29": {"kind": "aggregate"}}}
    doc = ba.history_doc(hist, [ba.Target("2026-09-27", "x", True), ba.Target("2026-02-22", "y", False)])
    assert doc["dates"] == ["2026-02-22", "2026-09-27", "2026-10-04"]
    assert list(doc["archives"]) == ["2026-02-22", "2026-03-29", "2026-09-27"]


# ── retention ───────────────────────────────────────────────────────────────

def test_the_next_three_weekly_runs_keep_the_restored_months_and_nothing_else_changes(
        site: pathlib.Path, commits: dict[str, str], tmp_path: pathlib.Path) -> None:
    control = tmp_path / "control"
    shutil.copytree(site, control, symlinks=True)
    ba.run(str(site), targets(commits), write=True)
    restored = {d: {n: (site / "snapshots" / d / n).read_bytes() for n in ps.AGGREGATE_KEEP} for d in (FEB, JUL, SEP)}
    for day in ("2026-10-11", "2026-10-18", "2026-10-25"):
        for s in (site, control):
            assert ps.run(str(s), day, None, dry=False) is not None
            sh.write_week(tmp_path, s / "data", day)
        kinds = {d: f.kind for d, f in ps.scan(str(site / "snapshots"))[0].items()}
        before = {d: f.kind for d, f in ps.scan(str(control / "snapshots"))[0].items()}
        assert kinds == {**before, JUL: ps.AGGREGATE, SEP: ps.AGGREGATE}, day
        hist = history(site)
        assert {JUL, SEP, FEB} <= set(hist["dates"]), day
        for d in (FEB, JUL, SEP):
            assert hist["archives"][d] == {"kind": "aggregate", "detail": sl.ARCHIVE_FILE}, (day, d)
            assert {n: (site / "snapshots" / d / n).read_bytes() for n in ps.AGGREGATE_KEEP} == restored[d], (day, d)
        other = {k: v for k, v in hist["archives"].items() if k not in (FEB, JUL, SEP)}
        assert other == {k: v for k, v in history(control)["archives"].items() if k != FEB}, day


@pytest.mark.parametrize("restore, why", [
    ("2026-08-09", "would not keep"),       # August is kept complete (08-02): prune would delete it
    ("2026-03-30", "would change"),         # newer than 03-29: March's aggregate would move
])
def test_a_date_retention_would_not_keep_as_it_is_is_refused(site: pathlib.Path, commits: dict[str, str],
                                                             restore: str, why: str) -> None:
    before = digest(site)
    with pytest.raises(ba.Refused, match=why):
        ba.run(str(site), [ba.Target(restore, commits[JUL], restore=True)], write=True)
    assert digest(site) == before


# ── refusals ────────────────────────────────────────────────────────────────

def test_a_commit_without_the_folder_is_refused(site: pathlib.Path, commits: dict[str, str]) -> None:
    before = digest(site)
    with pytest.raises(ba.Refused, match="does not hold that folder"):
        ba.run(str(site), [ba.Target(JUL, commits[FEB], restore=True)], write=True)
    with pytest.raises(ba.Refused, match="no dashboard-summary.json"):
        # The FEB commit holds the parts but no summary: a restore needs one.
        ba.run(str(site), [ba.Target(FEB, commits[FEB], restore=True)], write=True)
    assert digest(site) == before


def test_a_commit_not_in_the_sites_history_is_refused(site: pathlib.Path, commits: dict[str, str]) -> None:
    _git(site, "checkout", "-q", "-b", "elsewhere", commits[JUL])
    (site / "stray.txt").write_text("x")
    side = _commit(site, "a commit main never had")
    _git(site, "checkout", "-q", "main")
    before = digest(site)
    with pytest.raises(ba.Refused, match="not in the history"):
        ba.run(str(site), [ba.Target(JUL, side, restore=True)], write=True)
    with pytest.raises(ba.Refused, match="has no commit"):
        ba.run(str(site), [ba.Target(JUL, "0" * 40, restore=True)], write=True)
    assert digest(site) == before


def _commit_week(site: pathlib.Path, tmp: pathlib.Path, folder_date: str, run_date: str,
                 change: Any = None) -> str:
    """A commit on main holding snapshots/<folder_date>/ with the week of
    run_date (changed by change), then one removing it again."""
    folder = site / "snapshots" / folder_date
    sh.write_week(tmp, folder, run_date, extras=False)
    if change:
        change(folder)
    sha = _commit(site, "a week")
    shutil.rmtree(folder)
    _commit(site, "gone again")
    return sha


def _restamp_part3(folder: pathlib.Path) -> None:
    part = folder / "demographics.part3.json.gz"
    body = json.loads(gzip.decompress(part.read_bytes()))
    body["extracted_at"] = "2026-07-26T09:00:00+00:00"
    part.write_bytes(gzip.compress(json.dumps(body).encode(), mtime=0))


def test_parts_that_are_not_one_run_are_refused(site: pathlib.Path, tmp_path: pathlib.Path) -> None:
    sha = _commit_week(site, tmp_path, JUL, JUL, _restamp_part3)
    before = digest(site)
    with pytest.raises(ba.Refused, match="not one complete run.*part3"):
        ba.run(str(site), [ba.Target(JUL, sha, restore=True)], write=True)
    assert digest(site) == before


def test_a_run_of_another_date_is_refused(site: pathlib.Path, tmp_path: pathlib.Path) -> None:
    sha = _commit_week(site, tmp_path, JUL, "2026-07-19")
    with pytest.raises(ba.Refused, match="not that date's run.*2026-07-19 run"):
        ba.run(str(site), [ba.Target(JUL, sha, restore=True)], write=True)


def test_an_aggregate_summary_of_another_run_than_the_parts_is_refused(site: pathlib.Path,
                                                                       commits: dict[str, str]) -> None:
    summary = site / "snapshots" / FEB / "dashboard-summary.json"
    doc = json.loads(summary.read_text())
    doc["extracted_at"] = "2026-02-22T09:00:00"
    summary.write_text(json.dumps(doc))
    with pytest.raises(ba.Refused, match="another run"):
        ba.run(str(site), [ba.Target(FEB, commits[FEB], restore=False)], write=True)
    assert not (site / "snapshots" / FEB / sl.ARCHIVE_FILE).exists()


def test_a_projection_that_loses_a_field_is_refused_and_writes_nothing(
        site: pathlib.Path, commits: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    real = sl.project_object

    def lossy(obj: dict, spec: dict) -> dict:
        out = real(obj, spec)
        out.pop("status", None)
        return out

    monkeypatch.setattr(sl, "project_object", lossy)
    before = digest(site)
    with pytest.raises(ba.Refused, match="drops contract fields they have: status"):
        ba.run(str(site), targets(commits), write=True)
    assert digest(site) == before


def test_a_history_not_in_prunes_form_is_refused(site: pathlib.Path, commits: dict[str, str]) -> None:
    (site / "history.json").write_text(json.dumps({"dates": [FEB, MAR, AUG, LATEST]}))
    before = digest(site)
    with pytest.raises(ba.Refused, match="first weekly run with it"):
        ba.run(str(site), targets(commits), write=True)
    assert digest(site) == before


def test_a_complete_snapshot_is_never_touched(site: pathlib.Path, commits: dict[str, str]) -> None:
    before = digest(site)
    with pytest.raises(ba.Refused, match="not an aggregate's files only"):
        ba.run(str(site), [ba.Target(AUG, commits[JUL], restore=True)], write=True)
    with pytest.raises(ba.Refused, match="not an aggregate's files only"):
        ba.run(str(site), [ba.Target(AUG, commits[JUL], restore=False)], write=True)
    assert digest(site) == before


def test_a_file_in_the_way_that_does_not_check_is_refused(site: pathlib.Path, commits: dict[str, str]) -> None:
    (site / "snapshots" / FEB / sl.ARCHIVE_FILE).write_bytes(gzip.compress(b'{"data": {}}', mtime=0))
    before = digest(site)
    with pytest.raises(ba.Refused, match="does not check"):
        ba.run(str(site), targets(commits), write=True)
    assert digest(site) == before


def test_a_restored_summary_that_differs_from_its_commits_is_refused(site: pathlib.Path,
                                                                     commits: dict[str, str]) -> None:
    (site / "snapshots" / JUL).mkdir()
    (site / "snapshots" / JUL / "dashboard-summary.json").write_text(json.dumps(sh.summary(JUL)))
    with pytest.raises(ba.Refused, match="is not the one at"):
        ba.run(str(site), targets(commits), write=True)


def test_an_archive_that_checks_but_differs_is_kept_and_said(site: pathlib.Path, commits: dict[str, str]) -> None:
    ba.run(str(site), targets(commits), write=True)
    path = site / "snapshots" / FEB / sl.ARCHIVE_FILE
    doc = json.loads(gzip.decompress(path.read_bytes()))
    first = next(iter(doc["data"]))
    doc["data"][first] = {"nct_id": first}
    path.write_bytes(archive_records.encode(doc))
    kept = path.read_bytes()
    report = ba.run(str(site), targets(commits), write=True)
    assert path.read_bytes() == kept and report["changed"] == []
    assert next(t for t in report["targets"] if t["date"] == FEB)["actions"] == {
        f"snapshots/{FEB}/{sl.ARCHIVE_FILE}": "kept"}


# ── re-runs ─────────────────────────────────────────────────────────────────

def test_a_second_run_changes_nothing(site: pathlib.Path, commits: dict[str, str]) -> None:
    ba.run(str(site), targets(commits), write=True)
    _commit(site, "the backfill")
    before = digest(site)
    report = ba.run(str(site), targets(commits), write=True)
    assert report["ok"] and report["changed"] == [] and not report["history"]["changed"]
    assert all(set(t["actions"].values()) == {"unchanged"} for t in report["targets"])
    assert digest(site) == before
    assert _git(site, "status", "--porcelain") == "", "nothing to commit"


def test_a_run_that_stopped_part_way_is_completed(site: pathlib.Path, commits: dict[str, str],
                                                  tmp_path: pathlib.Path) -> None:
    whole = tmp_path / "whole"
    shutil.copytree(site, whole, symlinks=True)
    ba.run(str(whole), targets(commits), write=True)
    # Stopped part way: 02-22's file written, 07-26 left with its summary
    # only, 09-27 not begun, history.json not yet rewritten.
    shutil.copyfile(whole / "snapshots" / FEB / sl.ARCHIVE_FILE, site / "snapshots" / FEB / sl.ARCHIVE_FILE)
    (site / "snapshots" / JUL).mkdir()
    shutil.copyfile(whole / "snapshots" / JUL / "dashboard-summary.json", site / "snapshots" / JUL / "dashboard-summary.json")
    report = ba.run(str(site), targets(commits), write=True)
    assert report["ok"]
    actions = {p: a for t in report["targets"] for p, a in t["actions"].items()}
    assert actions[f"snapshots/{FEB}/{sl.ARCHIVE_FILE}"] == "unchanged"
    assert actions[f"snapshots/{JUL}/dashboard-summary.json"] == "unchanged"
    assert actions[f"snapshots/{JUL}/{sl.ARCHIVE_FILE}"] == "write"
    assert digest(site) == digest(whole)


# ── dry run and the workflow ────────────────────────────────────────────────

def test_without_write_nothing_in_the_site_changes(site: pathlib.Path, commits: dict[str, str],
                                                   tmp_path: pathlib.Path) -> None:
    before = digest(site)
    out = tmp_path / "out"
    report = ba.run(str(site), targets(commits), write=False, out=str(out))
    assert digest(site) == before and report["gate"] is None
    assert len(report["changed"]) == 6
    assert sh.files(out) == sorted([f"snapshots/{d}/{sl.ARCHIVE_FILE}" for d in (FEB, JUL, SEP)]
                                   + [f"snapshots/{d}/dashboard-summary.json" for d in (JUL, SEP)]
                                   + ["history.json", "plan.json"])
    assert ba.main(["--site", str(site)]) == 1, "the real targets' commits are not in this repository"


def test_a_gate_failure_fails_the_run(site: pathlib.Path, commits: dict[str, str]) -> None:
    (site / "data" / "run.json").unlink()
    report = ba.run(str(site), targets(commits), write=True)
    assert not report["ok"] and any("run.json" in e for e in report["gate"]["errors"])


def test_the_targets_are_the_owners() -> None:
    assert [(t.date, t.commit, t.restore) for t in ba.TARGETS] == [
        ("2026-02-22", "cb50c082ac9bf337f965aae6ae09f51b9ba1be43", False),
        ("2026-04-26", "be0285566775308901aa9bb834695dc238eabbcc", False),
        ("2026-07-26", "433eee466ae048d8b35731acdfd3bca5684f218d", True),
        ("2026-09-27", "4a1df0a8042e42f93bfd84ce0375c4891e0d78d9", True)]
    assert ba.COPIED == ("dashboard-summary.json",), "an aggregate keeps its summary and the archive file only"


def _steps(text: str) -> list[str]:
    return re.split(r"\n      - ", text.split("\n    steps:\n", 1)[1])


def _concurrency(text: str) -> str:
    m = re.search(r"^concurrency:\n  group: (\S+)\n  cancel-in-progress: false$", text, re.M)
    assert m, "a workflow-level concurrency group that never cancels a run in progress"
    return m.group(1)


def test_the_workflow_pushes_only_when_dry_run_is_unchecked() -> None:
    text = open(os.path.join(WORKFLOWS, "backfill-archives.yml")).read()
    on = text.split("\non:\n", 1)[1].split("\n\n", 1)[0] + "\n"
    assert re.fullmatch(r"  workflow_dispatch:\n    inputs:\n      dry_run:\n.*", on, re.S), "dispatch only"
    assert re.search(r"dry_run:\n(        .*\n)*?        type: boolean\n(        .*\n)*?        default: true\n", on)
    steps = _steps(text)
    pushing = [s for s in steps if re.search(r"\bgit (push|commit)\b", s)]
    assert len(pushing) == 1 and pushing[0].startswith("name: Commit and push to the site\n        if: ${{ !inputs.dry_run }}\n")
    assert "--force" not in text and not re.search(r"git push.* -f\b", text)
    assert not re.search(r"\b(git rm|rm -|rmdir)\b", text), "it never deletes"
    assert "--diff-filter=DR" in pushing[0], "a staged deletion stops the push"
    site_checkout = next(s for s in steps if "repository: michaeldgreenphd/clinical-trial-populations" in s)
    assert "persist-credentials: ${{ !inputs.dry_run }}" in site_checkout, "a dry run keeps no push credentials"
    assert "fetch-depth: 0" in site_checkout and "filter: blob:none" in site_checkout
    build = next(s for s in steps if "backfill_archives.py" in s)
    assert "--write" in build and "if:" not in build.split("run:")[0]
    upload = next(s for s in steps if "actions/upload-artifact" in s)
    assert steps.index(build) < steps.index(upload) < steps.index(pushing[0])


def test_the_workflow_shares_the_weekly_extracts_concurrency_group() -> None:
    backfill = open(os.path.join(WORKFLOWS, "backfill-archives.yml")).read()
    extract = open(os.path.join(WORKFLOWS, "extract.yml")).read()
    assert _concurrency(backfill) == _concurrency(extract) == "site-publish"
