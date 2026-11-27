"""In_audit holdout EXCLUSION in the feasibility family (src/holdout_exclusion.py), on fictional trials.

Every id is fictional. The holdout index is the committed fictional
tests/fixtures/holdout_index/index.json (holdouts NCT99777701 and NCT99777702). The holdout added
here is a terminated trial, so before the exclusion it would be a stopped-trial row and count in the
termination tables.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from feasibility import artifacts
from scripts import build_feasibility_benchmarks, check_feasibility_contract, export_feasibility_site
from src import holdout_exclusion as HX
from tests.feasibility.conftest import make_ctgov_study
from tests.feasibility.test_benchmarks_and_contract import _sample_cohort, _write_weekly_inputs

ROOT = Path(__file__).resolve().parents[2]
HOLDOUT_INDEX = ROOT / "tests" / "fixtures" / "holdout_index" / "index.json"
HOLDOUT_ARGS = ["--holdout-index", str(HOLDOUT_INDEX)]
HOLDOUT = "NCT99777701"
GENERATED_AT = "2026-11-21T12:00:00+00:00"


def _holdout_study(**overrides: Any) -> dict[str, Any]:
    """A fictional terminated holdout trial: a stopped-trial row, were it not excluded."""
    return make_ctgov_study(HOLDOUT, overall_status="TERMINATED", why_stopped="Sponsor decision to stop enrolment",
                            **overrides)


def _write_records_dir(studies: list[dict[str, Any]], target: Path) -> Path:
    target.mkdir(parents=True)
    for study in studies:
        nct = study["protocolSection"]["identificationModule"]["nctId"]
        (target / f"{nct}.json.gz").write_bytes(gzip.compress(json.dumps(study).encode("utf-8"), mtime=0))
    return target


def _build(records_dir: Path, out: Path, *extra: str) -> int:
    return build_feasibility_benchmarks.main(["--records-dir", str(records_dir), "--generated-at", GENERATED_AT,
                                              "--out", str(out), *extra])


def _files_naming_a_holdout(root: Path) -> list[str]:
    ids = HX.load_holdout_index(HOLDOUT_INDEX).ids
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and HX.count_holdout_mentions(p.read_bytes(), ids))


def _without_run_id(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _without_run_id(v) for k, v in obj.items() if k != "run_id"}
    return [_without_run_id(v) for v in obj] if isinstance(obj, list) else obj


@pytest.fixture(scope="module")
def builds(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    work = tmp_path_factory.mktemp("feasibility_holdouts")
    with_holdout = _write_records_dir([*_sample_cohort(), _holdout_study()], work / "with_holdout" / "records")
    clean = _write_records_dir(_sample_cohort(), work / "clean" / "records")
    out_h, out_c = work / "with_holdout" / "build", work / "clean" / "build"
    assert _build(with_holdout, out_h, *HOLDOUT_ARGS) == 0
    assert _build(clean, out_c, *HOLDOUT_ARGS) == 0
    return {"with_holdout": out_h, "clean": out_c, "records": with_holdout}


def test_builder_excludes_the_holdout_and_stamps_the_count(builds: dict[str, Path]) -> None:
    parsed, _ = artifacts.load_dir(builds["with_holdout"])
    assert parsed["run.json"]["holdout_exclusion"] == {
        "rules_version": HX.RULES_VERSION, "index_sha256": HX.load_holdout_index(HOLDOUT_INDEX).sha256,
        "holdouts_in_index": 2, "excluded_from_universe": 1,
    }
    clean, _ = artifacts.load_dir(builds["clean"])
    assert parsed["run.json"]["inputs"]["records"]["n_records"] == len(_sample_cohort())
    assert parsed["run.json"]["n_trials"] == clean["run.json"]["n_trials"]
    for name in ("summary.json", "strata.json", "sponsors.json", artifacts.TRIALS_FILE):
        assert _without_run_id(parsed[name]) == _without_run_id(clean[name]), name
    assert _files_naming_a_holdout(builds["with_holdout"]) == []
    assert check_feasibility_contract.main([str(builds["with_holdout"])]) == 0


def test_build_is_byte_identical_on_rebuild(tmp_path: Path, builds: dict[str, Path]) -> None:
    again = tmp_path / "again"
    assert _build(builds["records"], again, *HOLDOUT_ARGS) == 0
    for name in (*artifacts.FILES, "run.json"):
        assert (again / name).read_bytes() == (builds["with_holdout"] / name).read_bytes(), name


def test_a_holdout_record_is_dropped_before_any_record_check(tmp_path: Path) -> None:
    broken = _holdout_study()
    del broken["protocolSection"]["conditionsModule"]  # read, this refuses the build as projected
    records = _write_records_dir([*_sample_cohort(), broken], tmp_path / "records")
    out = tmp_path / "build"
    assert _build(records, out, *HOLDOUT_ARGS) == 0
    assert json.loads((out / "run.json").read_text())["holdout_exclusion"]["excluded_from_universe"] == 1
    with pytest.raises(ValueError, match="conditionsModule"):
        build_feasibility_benchmarks.load_studies_input(demographics_path=None, records_dir=records, archive_path=None)


def test_weekly_records_and_mesh_sidecar_drop_the_holdout(tmp_path: Path) -> None:
    demog, mesh = _write_weekly_inputs(tmp_path)
    body = json.loads(gzip.decompress(demog.read_bytes()))
    row = copy.deepcopy(body["data"][0])
    row.update(nct_id=HOLDOUT, status="TERMINATED", why_stopped="Sponsor decision")
    body["data"].append(row)
    demog.write_bytes(gzip.compress(json.dumps(body).encode("utf-8"), mtime=0))
    lines = gzip.decompress(mesh.read_bytes()).decode().splitlines()
    # The holdout's sidecar line breaks the line contract: read, it would refuse the sidecar.
    lines.append(json.dumps({"schema_version": "9", "nct_id": HOLDOUT, "state": "unknown"}))
    mesh.write_bytes(gzip.compress(("\n".join(lines) + "\n").encode("utf-8"), mtime=0))
    out = tmp_path / "weekly"
    assert build_feasibility_benchmarks.main(["--demographics", str(demog), "--condition-mesh", str(mesh),
                                              "--generated-at", GENERATED_AT, "--out", str(out), *HOLDOUT_ARGS]) == 0
    run = json.loads((out / "run.json").read_text())
    assert run["holdout_exclusion"]["excluded_from_universe"] == 1
    assert run["inputs"]["records"]["n_records"] == 5 and run["n_trials"] == 5
    assert run["inputs"]["condition_mesh"]["n_rows"] == 5
    summary = json.loads((out / "summary.json").read_text())
    assert summary["denominators"]["stopped_trials"]["n"] == 0
    assert _files_naming_a_holdout(out) == []


def test_an_archive_envelope_drops_the_holdout_from_requested_and_returned(tmp_path: Path) -> None:
    cohort = _sample_cohort()
    pair = [cohort[0], _holdout_study()]
    ids = [s["protocolSection"]["identificationModule"]["nctId"] for s in pair]
    body = json.dumps({"studies": pair})
    lines = [json.dumps({"url": f"https://clinicaltrials.gov/api/v2/studies?filter.ids={','.join(ids)}", "ids": ids,
                         "status": 200, "body": body, "body_sha256": hashlib.sha256(body.encode()).hexdigest()})]
    lines += [json.dumps(s) for s in cohort[1:]]
    archive = tmp_path / "archive.jsonl.gz"
    archive.write_bytes(gzip.compress(("\n".join(lines) + "\n").encode("utf-8"), mtime=0))
    exclusion = HX.HoldoutExclusion(HX.load_holdout_index(HOLDOUT_INDEX))
    studies, meta = build_feasibility_benchmarks.load_studies_input(
        demographics_path=None, records_dir=None, archive_path=archive, exclusion=exclusion)
    assert len(studies) == len(cohort) == meta["n_records"]
    assert HOLDOUT not in json.dumps(studies)
    assert exclusion.n_excluded == 1


def test_a_curated_review_of_a_holdout_is_refused(tmp_path: Path, builds: dict[str, Path]) -> None:
    reviews = tmp_path / "termination_reviews.csv"
    reviews.write_text("nct_id,reviewer,reviewed_at,override_category,notes\n"
                       f"{HOLDOUT},A Person,2026-10-01,other,fictional\n", encoding="utf-8")
    out = tmp_path / "build"
    assert _build(builds["records"], out, "--reviews", str(reviews), *HOLDOUT_ARGS) == 1
    assert not out.exists()


def test_build_without_holdout_index_exits_nonzero_writing_nothing(
    tmp_path: Path, builds: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "build"
    with pytest.raises(SystemExit) as exited:
        _build(builds["records"], out)
    assert exited.value.code == 2
    assert "--holdout-index" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize("payload", [
    b'{"rows": "NCT99777701"}', b'{"rows": [{"nct_id": "NCT99777701", "in_audit": null}]}', b'{"rows": [7]}',
    b'{"rows": [{"nct_id": "NCT99777701", "in_audit": NaN}]}',
])
def test_a_malformed_index_is_refused_writing_nothing(
    tmp_path: Path, builds: dict[str, Path], capsys: pytest.CaptureFixture[str], payload: bytes
) -> None:
    bad = tmp_path / "index.json"
    bad.write_bytes(payload)
    out = tmp_path / "build"
    assert _build(builds["records"], out, "--holdout-index", str(bad)) == 1
    assert not out.exists() and HOLDOUT not in capsys.readouterr().err


def test_contract_requires_a_valid_stamp(builds: dict[str, Path]) -> None:
    parsed, raw = artifacts.load_dir(builds["with_holdout"])
    assert artifacts.check_contract(parsed, raw) == []
    missing = copy.deepcopy(parsed)
    del missing["run.json"]["holdout_exclusion"]
    assert any("holdout_exclusion is missing" in p for p in artifacts.check_contract(missing))
    for key, value in (("excluded_from_universe", 1.0), ("holdouts_in_index", False), ("index_sha256", "A" * 64),
                       ("rules_version", "")):
        bad = copy.deepcopy(parsed)
        bad["run.json"]["holdout_exclusion"][key] = value
        assert any(f"holdout_exclusion.{key}" in p for p in artifacts.check_contract(bad)), key


def test_export_carries_the_stamp(tmp_path: Path, builds: dict[str, Path]) -> None:
    site = tmp_path / "site"
    assert export_feasibility_site.main([str(builds["with_holdout"]), str(site), *HOLDOUT_ARGS]) == 0
    run = json.loads((site / "run.json").read_text())
    assert run["holdout_exclusion"] == json.loads((builds["with_holdout"] / "run.json").read_text())["holdout_exclusion"]
    assert _files_naming_a_holdout(site) == []


def test_export_requires_holdout_index(tmp_path: Path, builds: dict[str, Path]) -> None:
    with pytest.raises(SystemExit):
        export_feasibility_site.main([str(builds["with_holdout"]), str(tmp_path / "site")])
    assert not (tmp_path / "site").exists()


def test_export_refuses_a_build_without_the_stamp(
    tmp_path: Path, builds: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    old = tmp_path / "old"
    old.mkdir()
    for path in builds["with_holdout"].iterdir():
        (old / path.name).write_bytes(path.read_bytes())
    run = json.loads((old / "run.json").read_text())
    del run["holdout_exclusion"]
    (old / "run.json").write_bytes(artifacts.dump_manifest(run))
    site = tmp_path / "site"
    assert export_feasibility_site.main([str(old), str(site), *HOLDOUT_ARGS]) == 1
    assert "holdout_exclusion is missing" in capsys.readouterr().err
    assert not site.exists()


def test_export_refuses_a_stamp_of_another_index(
    tmp_path: Path, builds: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    changed = tmp_path / "index.json"
    changed.write_text(json.dumps({"rows": [{"nct_id": HOLDOUT, "in_audit": False}]}), encoding="utf-8")
    site = tmp_path / "site"
    assert export_feasibility_site.main([str(builds["with_holdout"]), str(site), "--holdout-index", str(changed)]) == 1
    assert "the index changed since the build" in capsys.readouterr().err
    assert not site.exists()


def test_export_refuses_a_build_whose_stopped_trial_rows_name_a_holdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A valid build of non-holdouts, one of them stopped with a reason that names a holdout's id: its
    # per-trial row (and its stopped-trial row) carries the id, and the exporter's scan refuses it.
    mentioning = make_ctgov_study("NCT99799901", overall_status="TERMINATED",
                                  why_stopped=f"Merged into {HOLDOUT}")
    records = _write_records_dir([*_sample_cohort(), mentioning], tmp_path / "records")
    out = tmp_path / "build"
    assert _build(records, out, *HOLDOUT_ARGS) == 0
    assert _files_naming_a_holdout(out) == [artifacts.TRIALS_FILE]
    site = tmp_path / "site"
    assert export_feasibility_site.main([str(out), str(site), *HOLDOUT_ARGS]) == 1
    err = capsys.readouterr().err
    assert "trials.jsonl.gz (1)" in err and HOLDOUT not in err
    assert not site.exists()


def test_a_study_whose_protocol_id_is_a_holdout_is_dropped_or_refused_whatever_its_top_level_id() -> None:
    # The metrics read protocolSection.identificationModule.nctId, so the drop must too.
    kept = make_ctgov_study("NCT99777703")
    blank_top = {**_holdout_study(), "nct_id": ""}
    exclusion = HX.HoldoutExclusion(HX.load_holdout_index(HOLDOUT_INDEX))
    assert build_feasibility_benchmarks.drop_holdouts([blank_top, kept], exclusion) == [kept]
    assert exclusion.n_excluded == 1
    clean_top = {**_holdout_study(), "nct_id": "NCT99777703"}
    with pytest.raises(HX.HoldoutIndexError) as refused:
        build_feasibility_benchmarks.drop_holdouts([kept, clean_top], HX.HoldoutExclusion(HX.load_holdout_index(HOLDOUT_INDEX)))
    assert "study 2" in str(refused.value) and HOLDOUT not in str(refused.value)
