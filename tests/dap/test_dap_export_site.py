"""Offline tests for scripts/export_dap_benchmarks_site.py atomic staging and refusals."""
from __future__ import annotations

import copy
import gzip
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT: Path = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

_FX_SPEC = importlib.util.spec_from_file_location(
    "dap_fixture", Path(__file__).resolve().parent / "fixture.py"
)
assert _FX_SPEC is not None and _FX_SPEC.loader is not None
fx = sys.modules.setdefault("dap_fixture", importlib.util.module_from_spec(_FX_SPEC))
if not hasattr(fx, "make_raw_ctgov_study"):
    _FX_SPEC.loader.exec_module(fx)

import build_dap_benchmarks as cli_build  # noqa: E402
import export_dap_benchmarks_site as site_export  # noqa: E402
from dap import artifacts  # noqa: E402
from src import condition_mesh as cmesh  # noqa: E402
from src import holdout_exclusion as HX  # noqa: E402

# The fictional holdout index every CLI call is given (tests/fixtures/holdout_index/index.json). Its
# holdouts (NCT99777701, NCT99777702) are in no DAP fixture, so the exclusion removes nothing and
# every existing expectation holds; test_dap_holdouts.py adds a holdout.
HOLDOUT_INDEX: Path = ROOT / "tests" / "fixtures" / "holdout_index" / "index.json"
HOLDOUT_ARGS: list[str] = ["--holdout-index", str(HOLDOUT_INDEX)]
HOLDOUT = HX.load_holdout_index(HOLDOUT_INDEX)


@pytest.fixture(scope="module")
def verified_build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Create a verified DAP benchmark build on disk for site export tests."""
    work = tmp_path_factory.mktemp("dap_site_fixture")
    studies_dir = fx.write_studies_dir(fx.build_fictional_cohort(), work / "studies")
    rules_path = fx.write_fictional_sponsor_rules(work / "aliases.csv")
    out = work / "build"
    rc = cli_build.main(
        [
            "--records-dir",
            str(studies_dir),
            "--sponsor-rules",
            str(rules_path),
            "--generated-at",
            "2026-11-21T12:00:00+00:00",
            "--out",
            str(out),
            *HOLDOUT_ARGS,
        ]
    )
    assert rc == 0
    return out


def test_site_export_projects_verified_files_and_preserves_summary_counts(
    verified_build: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site_dir = tmp_path / "site" / "dap"
    assert site_export.main([str(verified_build), str(site_dir), *HOLDOUT_ARGS]) == 0
    printed = capsys.readouterr().out
    assert "summary.json" in printed and "total; wrote" in printed

    written_names = {p.name for p in site_dir.iterdir()}
    assert written_names == {"run.json", "summary.json", "strata.json", "sponsors.json"}
    assert "trials.jsonl.gz" not in written_names

    site_run = json.loads((site_dir / "run.json").read_text(encoding="utf-8"))
    build_run = json.loads((verified_build / "run.json").read_text(encoding="utf-8"))
    assert site_run["build_run_id"] == build_run["run_id"]
    assert site_run["run_id"].startswith(build_run["run_id"] + "-")

    for name, digest in site_run["files"].items():
        raw = (site_dir / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
        parsed = json.loads(raw.decode("utf-8"))
        assert parsed["run_id"] == site_run["run_id"]
        assert parsed["schema_version"] == 1

    # Summary counts are identical to the build's apart from run_id
    build_summary = json.loads((verified_build / "summary.json").read_text(encoding="utf-8"))
    site_summary = json.loads((site_dir / "summary.json").read_text(encoding="utf-8"))
    assert {**site_summary, "run_id": build_summary["run_id"]} == build_summary


def test_site_manifest_carries_verified_condition_mesh_provenance(tmp_path: Path) -> None:
    studies = fx.build_fictional_cohort()
    studies_dir = fx.write_studies_dir(studies, tmp_path / "studies")
    rules_path = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")
    stamps = {
        "snapshot_date": "2026-11-21",
        "extracted_at": "2026-11-21T00:00:00+00:00",
        "pipeline_commit": "deadbeef1234" + "0" * 28,
    }
    raw = b"".join(
        (json.dumps({**cmesh.select_condition_mesh(s), **stamps}) + "\n").encode("utf-8") for s in studies
    )
    mesh_path = tmp_path / "condition_mesh.jsonl.gz"
    mesh_path.write_bytes(gzip.compress(raw, mtime=0))
    build = tmp_path / "build"
    rc = cli_build.main(
        [
            "--records-dir",
            str(studies_dir),
            "--condition-mesh",
            str(mesh_path),
            "--sponsor-rules",
            str(rules_path),
            "--generated-at",
            "2026-11-21T12:00:00+00:00",
            "--out",
            str(build),
            *HOLDOUT_ARGS,
        ]
    )
    assert rc == 0

    site_dir = tmp_path / "site" / "dap"
    site_export.export_site(build, site_dir, holdout_index=HOLDOUT)
    build_mesh = json.loads((build / "run.json").read_text(encoding="utf-8"))["inputs"]["condition_mesh"]
    site_mesh = json.loads((site_dir / "run.json").read_text(encoding="utf-8"))["inputs"]["condition_mesh"]
    assert site_mesh == {k: build_mesh[k] for k in ("n_lines", "sha256", *site_export.MESH_PROVENANCE_KEYS)}
    assert site_mesh["extracted_at"] == "2026-11-21T00:00:00+00:00"
    assert site_mesh["pipeline_commit"] == "deadbeef1234" + "0" * 28
    assert site_mesh["snapshot_date"] == "2026-11-21"
    assert site_mesh["provenance_check"] == "cohort_covered_and_one_run_only"
    assert site_mesh["n_cohort_trials"] == len(studies)

    # A build manifest without the verified provenance (older build code) is refused.
    loaded = artifacts.load_dir(build)
    unverified = copy.deepcopy(loaded)
    del unverified["run.json"]["inputs"]["condition_mesh"]["provenance_check"]
    with pytest.raises(ValueError, match="was not provenance-checked"):
        site_export.select_site_files(unverified)


def test_site_run_records_exporter_provenance_and_content_derived_id(
    verified_build: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_a = tmp_path / "a" / "dap"
    out_b = tmp_path / "b" / "dap"
    site_export.export_site(verified_build, out_a, holdout_index=HOLDOUT)
    site_export.export_site(verified_build, out_b, holdout_index=HOLDOUT)
    run_a = json.loads((out_a / "run.json").read_text(encoding="utf-8"))

    # The exporter's own provenance, over its import closure, beside the build's producer block.
    exp = run_a.get("exporter")
    assert isinstance(exp, dict), "site run.json has no exporter provenance block"
    assert set(exp) == {"repository", "commit", "uncommitted_changes", "source_sha256"}
    exporter_paths = cli_build.producer_paths(entry=ROOT / "scripts" / "export_dap_benchmarks_site.py")
    assert "scripts/export_dap_benchmarks_site.py" in exporter_paths
    assert "dap/artifacts.py" in exporter_paths
    assert "scripts/check_dap_benchmarks_contract.py" not in exporter_paths
    assert exp["source_sha256"] == cli_build.source_digest(ROOT, exporter_paths)
    assert run_a["producer"] == json.loads((verified_build / "run.json").read_text(encoding="utf-8"))["producer"]

    # Same build, same exporter: same bytes and id. The id is the digest of the content minus run_id.
    for name in ("run.json", "summary.json", "strata.json", "sponsors.json"):
        assert (out_a / name).read_bytes() == (out_b / name).read_bytes(), name
    content = {
        name: {k: v for k, v in json.loads((out_a / name).read_text(encoding="utf-8")).items() if k != "run_id"}
        for name in ("run.json", "summary.json", "strata.json", "sponsors.json")
    }
    content["run.json"].pop("files")  # added after the id, from the stamped bytes
    want = f"{run_a['build_run_id']}-{hashlib.sha256(artifacts.dump(content)).hexdigest()[:8]}"
    assert run_a["run_id"] == want

    # Changed exporter code (a different source digest) re-exports the same build under a new id.
    changed = {**exp, "source_sha256": "0" * 64}
    monkeypatch.setattr(site_export, "exporter_info", lambda: changed)
    out_c = tmp_path / "c" / "dap"
    site_export.export_site(verified_build, out_c, holdout_index=HOLDOUT)
    run_c = json.loads((out_c / "run.json").read_text(encoding="utf-8"))
    assert run_c["build_run_id"] == run_a["build_run_id"]
    assert run_c["exporter"] == changed
    assert run_c["run_id"] != run_a["run_id"]


def test_limited_diagnostic_build_is_never_exported(
    verified_build: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    studies_dir = fx.write_studies_dir(fx.build_fictional_cohort(), tmp_path / "studies")
    rules_path = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")
    limited = tmp_path / "limited"
    rc = cli_build.main(
        [
            "--records-dir",
            str(studies_dir),
            "--sponsor-rules",
            str(rules_path),
            "--limit",
            "3",
            "--generated-at",
            "2026-11-21T12:00:00+00:00",
            "--out",
            str(limited),
            *HOLDOUT_ARGS,
        ]
    )
    assert rc == 0  # a valid diagnostic build: the contract does not reject it
    run = json.loads((limited / "run.json").read_text(encoding="utf-8"))
    assert run["inputs"]["study_records"]["limit_applied"] == 3
    assert run["inputs"]["study_records"]["n_records"] == 3

    # The exporter refuses it and leaves the existing site export untouched.
    site_dir = tmp_path / "site" / "dap"
    site_export.export_site(verified_build, site_dir, holdout_index=HOLDOUT)
    before = {p.name: p.read_bytes() for p in site_dir.iterdir()}
    capsys.readouterr()
    assert site_export.main([str(limited), str(site_dir), *HOLDOUT_ARGS]) == 1
    assert "limit_applied is 3; a --limit build is diagnostic only" in capsys.readouterr().err
    assert {p.name: p.read_bytes() for p in site_dir.iterdir()} == before

    # A manifest without the key (not written by load_study_records) is refused too.
    loaded = artifacts.load_dir(verified_build)
    del loaded["run.json"]["inputs"]["study_records"]["limit_applied"]
    with pytest.raises(ValueError, match="limit_applied is '<missing>'"):
        site_export.select_site_files(loaded)


def test_build_without_git_records_null_provenance_and_is_never_exported(
    verified_build: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    studies_dir = fx.write_studies_dir(fx.build_fictional_cohort(), tmp_path / "studies")
    rules_path = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")

    class _Proc:
        def __init__(self, returncode: int, stdout: str) -> None:
            self.returncode, self.stdout = returncode, stdout

    def no_git(cmd: list[str], **_kwargs: Any) -> _Proc:
        raise FileNotFoundError("git")

    def status_fails(cmd: list[str], **_kwargs: Any) -> _Proc:
        return _Proc(0, "a" * 40 + "\n") if cmd[1] == "rev-parse" else _Proc(128, "")

    for name, fake, want in (("no_git", no_git, (None, None)), ("status_fails", status_fails, ("a" * 40, None))):
        out = tmp_path / name
        with monkeypatch.context() as m:
            m.setattr(cli_build.subprocess, "run", fake)
            rc = cli_build.main(
                [
                    "--records-dir",
                    str(studies_dir),
                    "--sponsor-rules",
                    str(rules_path),
                    "--generated-at",
                    "2026-11-21T12:00:00+00:00",
                    "--out",
                    str(out),
                    *HOLDOUT_ARGS,
                ]
            )
        # Null, never false; the build is still written for local diagnostics and passes the contract.
        assert rc == 0, name
        producer = json.loads((out / "run.json").read_text(encoding="utf-8"))["producer"]
        assert (producer["commit"], producer["uncommitted_changes"]) == want, name
        assert artifacts.check_contract(artifacts.load_dir(out)) == [], name
        # The site exporter refuses to publish it.
        with pytest.raises(ValueError, match="run.json: producer: .*no readable git provenance"):
            site_export.export_site(out, tmp_path / f"site_{name}" / "dap", holdout_index=HOLDOUT)
        assert not (tmp_path / f"site_{name}").exists()

    # The contract allows only a 40-hex commit or null, and true/false/null cleanliness.
    loaded = artifacts.load_dir(verified_build)
    for field, bad, message in (
        ("commit", "abc123", "producer.commit must be a 40-hex git commit or null (got 'abc123')"),
        ("uncommitted_changes", "no", "producer.uncommitted_changes must be true, false or null (got 'no')"),
    ):
        tampered = copy.deepcopy(loaded)
        tampered["run.json"]["producer"][field] = bad
        assert f"run.json: {message}" in artifacts.check_contract(tampered)

    # The exporter's own block: null refuses, a dirty tree (true) is accepted.
    real = site_export.exporter_info()
    monkeypatch.setattr(site_export, "exporter_info", lambda: {**real, "uncommitted_changes": None})
    with pytest.raises(ValueError, match="exporter: .*no readable git provenance"):
        site_export.export_site(verified_build, tmp_path / "site_exporter_null" / "dap", holdout_index=HOLDOUT)
    dirty = {**real, "commit": "b" * 40, "uncommitted_changes": True}
    monkeypatch.setattr(site_export, "exporter_info", lambda: dirty)
    manifest = site_export.export_site(verified_build, tmp_path / "site_dirty" / "dap", holdout_index=HOLDOUT)
    assert manifest["exporter"]["uncommitted_changes"] is True


def test_tampered_or_incomplete_build_is_refused_and_leaves_site_intact(
    verified_build: Path, tmp_path: Path
) -> None:
    site_dir = tmp_path / "site" / "dap"
    site_export.export_site(verified_build, site_dir, holdout_index=HOLDOUT)
    before = {p.name: p.read_bytes() for p in site_dir.iterdir()}

    tampered = tmp_path / "tampered_build"
    shutil.copytree(verified_build, tampered)
    s_obj = json.loads((tampered / "summary.json").read_text(encoding="utf-8"))
    s_obj["denominators"]["all_trials"]["n"] = 999
    (tampered / "summary.json").write_bytes(artifacts.dump(s_obj))

    assert site_export.main([str(tampered), str(site_dir), *HOLDOUT_ARGS]) == 1
    assert {p.name: p.read_bytes() for p in site_dir.iterdir()} == before
    assert sorted(p.name for p in site_dir.parent.iterdir()) == ["dap"]

    missing = tmp_path / "missing_build"
    shutil.copytree(verified_build, missing)
    (missing / "trials.jsonl.gz").unlink()
    with pytest.raises(ValueError, match="failed producer contract"):
        site_export.export_site(missing, tmp_path / "other_out", holdout_index=HOLDOUT)


def test_unrelated_target_directory_is_never_replaced(
    verified_build: Path, tmp_path: Path
) -> None:
    foreign = tmp_path / "foreign_data"
    foreign.mkdir()
    (foreign / "demographics.part1.json.gz").write_bytes(b"keep")
    with pytest.raises(ValueError, match="refusing to replace it"):
        site_export.export_site(verified_build, foreign, holdout_index=HOLDOUT)
    assert [p.name for p in foreign.iterdir()] == ["demographics.part1.json.gz"]

    borrowed_names = tmp_path / "borrowed"
    borrowed_names.mkdir()
    for name in ("summary.json", "strata.json", "sponsors.json"):
        (borrowed_names / name).write_text("{}", encoding="utf-8")
    (borrowed_names / "run.json").write_text('{"run_id": "devices-2026-11-21-0123456789"}', encoding="utf-8")
    with pytest.raises(ValueError, match="does not name a DAP benchmark run"):
        site_export.export_site(verified_build, borrowed_names, holdout_index=HOLDOUT)


def test_write_failure_restores_previous_site_export(
    verified_build: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site" / "dap"
    site_export.export_site(verified_build, site_dir, holdout_index=HOLDOUT)
    before = {p.name: p.read_bytes() for p in site_dir.iterdir()}

    real_dump = artifacts.dump
    calls: list[int] = []

    def failing_dump(obj: Any) -> bytes:
        calls.append(1)
        if len(calls) == 2:
            raise OSError("simulated disk full")
        return real_dump(obj)

    monkeypatch.setattr(artifacts, "dump", failing_dump)
    with pytest.raises(OSError, match="simulated disk full"):
        site_export.export_site(verified_build, site_dir, holdout_index=HOLDOUT)

    assert {p.name: p.read_bytes() for p in site_dir.iterdir()} == before
    assert sorted(p.name for p in site_dir.parent.iterdir()) == ["dap"]


@pytest.mark.parametrize("missing", ["extracted_at", "pipeline_commit"])
def test_weekly_build_without_source_stamps_is_built_but_not_published(tmp_path: Path, missing: str) -> None:
    # On 96f9856 a weekly build without the condition_mesh sidecar exported with a null source stamp.
    weekly_doc = fx.build_weekly_full_records_doc()
    weekly_doc[missing] = None
    weekly_path = tmp_path / "demographics.json.gz"
    weekly_path.write_bytes(gzip.compress(json.dumps(weekly_doc).encode("utf-8"), mtime=0))
    build = tmp_path / "build"
    rules_path = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")
    assert cli_build.main(["--demographics", str(weekly_path), "--sponsor-rules", str(rules_path),
                           "--generated-at", "2026-11-23T09:00:00Z", "--snapshot-date", "2026-11-21",
                           "--out", str(build), *HOLDOUT_ARGS]) == 0
    loaded = artifacts.load_dir(build)
    assert loaded["run.json"]["inputs"]["study_records"][missing] is None
    assert artifacts.check_contract(loaded) == []
    with pytest.raises(ValueError, match="a weekly build whose source container cannot name"):
        site_export.export_site(build, tmp_path / "site" / "dap", holdout_index=HOLDOUT)
    # The contract refuses a malformed stamp, as the build does.
    bad = copy.deepcopy(loaded)
    bad["run.json"]["inputs"]["study_records"]["pipeline_commit"] = "deadbeef1234"
    assert any("pipeline_commit 'deadbeef1234' is not a 40-hex git commit" in x for x in artifacts.check_contract(bad))


def test_weekly_build_with_both_source_stamps_is_published(tmp_path: Path) -> None:
    weekly_path = tmp_path / "demographics.json.gz"
    weekly_path.write_bytes(gzip.compress(json.dumps(fx.build_weekly_full_records_doc()).encode("utf-8"), mtime=0))
    build = tmp_path / "build"
    rules_path = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")
    assert cli_build.main(["--demographics", str(weekly_path), "--sponsor-rules", str(rules_path),
                           "--generated-at", "2026-11-23T09:00:00Z", "--snapshot-date", "2026-11-21",
                           "--out", str(build), *HOLDOUT_ARGS]) == 0
    site_export.export_site(build, tmp_path / "site" / "dap", holdout_index=HOLDOUT)
    site_run = json.loads((tmp_path / "site" / "dap" / "run.json").read_text(encoding="utf-8"))
    assert site_run["inputs"]["study_records"]["pipeline_commit"] == fx.WEEKLY_PIPELINE_COMMIT
    assert site_run["inputs"]["study_records"]["extracted_at"] == fx.WEEKLY_EXTRACTED_AT
