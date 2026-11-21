"""Offline tests for scripts/export_dap_benchmarks_site.py atomic staging and refusals."""
from __future__ import annotations

import hashlib
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
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_dap_benchmarks as cli_build  # noqa: E402
import export_dap_benchmarks_site as site_export  # noqa: E402
import fixture as fx  # noqa: E402
from dap import artifacts  # noqa: E402


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
        ]
    )
    assert rc == 0
    return out


def test_site_export_projects_verified_files_and_preserves_summary_counts(
    verified_build: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site_dir = tmp_path / "site" / "dap"
    assert site_export.main([str(verified_build), str(site_dir)]) == 0
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


def test_tampered_or_incomplete_build_is_refused_and_leaves_site_intact(
    verified_build: Path, tmp_path: Path
) -> None:
    site_dir = tmp_path / "site" / "dap"
    site_export.export_site(verified_build, site_dir)
    before = {p.name: p.read_bytes() for p in site_dir.iterdir()}

    tampered = tmp_path / "tampered_build"
    shutil.copytree(verified_build, tampered)
    s_obj = json.loads((tampered / "summary.json").read_text(encoding="utf-8"))
    s_obj["denominators"]["all_trials"]["n"] = 999
    (tampered / "summary.json").write_bytes(artifacts.dump(s_obj))

    assert site_export.main([str(tampered), str(site_dir)]) == 1
    assert {p.name: p.read_bytes() for p in site_dir.iterdir()} == before
    assert sorted(p.name for p in site_dir.parent.iterdir()) == ["dap"]

    missing = tmp_path / "missing_build"
    shutil.copytree(verified_build, missing)
    (missing / "trials.jsonl.gz").unlink()
    with pytest.raises(ValueError, match="failed producer contract"):
        site_export.export_site(missing, tmp_path / "other_out")


def test_unrelated_target_directory_is_never_replaced(
    verified_build: Path, tmp_path: Path
) -> None:
    foreign = tmp_path / "foreign_data"
    foreign.mkdir()
    (foreign / "demographics.part1.json.gz").write_bytes(b"keep")
    with pytest.raises(ValueError, match="refusing to replace it"):
        site_export.export_site(verified_build, foreign)
    assert [p.name for p in foreign.iterdir()] == ["demographics.part1.json.gz"]

    borrowed_names = tmp_path / "borrowed"
    borrowed_names.mkdir()
    for name in ("summary.json", "strata.json", "sponsors.json"):
        (borrowed_names / name).write_text("{}", encoding="utf-8")
    (borrowed_names / "run.json").write_text('{"run_id": "devices-2026-11-21-0123456789"}', encoding="utf-8")
    with pytest.raises(ValueError, match="does not name a DAP benchmark run"):
        site_export.export_site(verified_build, borrowed_names)


def test_write_failure_restores_previous_site_export(
    verified_build: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site" / "dap"
    site_export.export_site(verified_build, site_dir)
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
        site_export.export_site(verified_build, site_dir)

    assert {p.name: p.read_bytes() for p in site_dir.iterdir()} == before
    assert sorted(p.name for p in site_dir.parent.iterdir()) == ["dap"]
