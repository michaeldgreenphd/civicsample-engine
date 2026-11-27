"""In_audit holdout EXCLUSION in the DAP family (src/holdout_exclusion.py), on fictional trials.

Every id is fictional. The holdout index is the committed fictional
tests/fixtures/holdout_index/index.json (holdouts NCT99777701 and NCT99777702). A holdout is added
to the fictional cohort as a copy of one of its diabetes trials, so before the exclusion it would
join that trial's strata.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT: Path = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_FX_SPEC = importlib.util.spec_from_file_location("dap_fixture", Path(__file__).resolve().parent / "fixture.py")
assert _FX_SPEC is not None and _FX_SPEC.loader is not None
fx = sys.modules.setdefault("dap_fixture", importlib.util.module_from_spec(_FX_SPEC))
if not hasattr(fx, "make_raw_ctgov_study"):
    _FX_SPEC.loader.exec_module(fx)

import build_dap_benchmarks as cli_build  # noqa: E402
import check_dap_benchmarks_contract as cli_check  # noqa: E402
import export_dap_benchmarks_site as site_export  # noqa: E402
from dap import artifacts, benchmarks  # noqa: E402
from src import holdout_exclusion as HX  # noqa: E402

HOLDOUT_INDEX: Path = ROOT / "tests" / "fixtures" / "holdout_index" / "index.json"
HOLDOUT_ARGS: list[str] = ["--holdout-index", str(HOLDOUT_INDEX)]
HOLDOUT: str = "NCT99777701"
GENERATED_AT: str = "2026-11-21T12:00:00+00:00"


def _as_holdout(study: dict[str, Any], nct_id: str = HOLDOUT) -> dict[str, Any]:
    """A copy of ``study`` registered under ``nct_id`` (every occurrence of its id replaced)."""
    own = study["protocolSection"]["identificationModule"]["nctId"]
    return json.loads(json.dumps(study).replace(own, nct_id))


def _cohort_with_holdout() -> list[dict[str, Any]]:
    cohort = fx.build_fictional_cohort()
    return [*cohort, _as_holdout(cohort[0])]


def _files_naming_a_holdout(root: Path) -> list[str]:
    ids = HX.load_holdout_index(HOLDOUT_INDEX).ids
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and HX.count_holdout_mentions(p.read_bytes(), ids))


def _build_dir(studies: list[dict[str, Any]], work: Path, name: str, *extra: str) -> tuple[int, Path]:
    studies_dir = fx.write_studies_dir(studies, work / f"{name}_studies")
    (work / f"{name}_rules").mkdir()
    rules = fx.write_fictional_sponsor_rules(work / f"{name}_rules" / "aliases.csv")
    out = work / name
    rc = cli_build.main(["--records-dir", str(studies_dir), "--sponsor-rules", str(rules),
                         "--generated-at", GENERATED_AT, "--out", str(out), *extra])
    return rc, out


def _without_run_id(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _without_run_id(v) for k, v in obj.items() if k != "run_id"}
    return [_without_run_id(v) for v in obj] if isinstance(obj, list) else obj


@pytest.fixture(scope="module")
def builds(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    work = tmp_path_factory.mktemp("dap_holdouts")
    rc_h, with_holdout = _build_dir(_cohort_with_holdout(), work, "with_holdout", *HOLDOUT_ARGS)
    rc_c, clean = _build_dir(fx.build_fictional_cohort(), work, "clean", *HOLDOUT_ARGS)
    assert rc_h == 0 and rc_c == 0
    return {"with_holdout": with_holdout, "clean": clean}


def test_builder_excludes_the_holdout_and_stamps_the_count(builds: dict[str, Path]) -> None:
    loaded = artifacts.load_dir(builds["with_holdout"])
    stamp = loaded["run.json"]["holdout_exclusion"]
    assert stamp == {"rules_version": HX.RULES_VERSION, "index_sha256": HX.load_holdout_index(HOLDOUT_INDEX).sha256,
                     "holdouts_in_index": 2, "excluded_from_universe": 1}
    clean = artifacts.load_dir(builds["clean"])
    assert clean["run.json"]["holdout_exclusion"]["excluded_from_universe"] == 0
    # Same trials, strata, sponsor tables and per-trial rows as a snapshot that never held the holdout.
    assert loaded["run.json"]["inputs"]["study_records"]["n_records"] == len(fx.build_fictional_cohort())
    for name in ("summary.json", "strata.json", "sponsors.json"):
        assert _without_run_id(loaded[name]) == _without_run_id(clean[name]), name
    assert _without_run_id(loaded["trial_rows"]) == _without_run_id(clean["trial_rows"])
    assert _files_naming_a_holdout(builds["with_holdout"]) == []
    assert cli_check.main([str(builds["with_holdout"])]) == 0


def test_build_is_byte_identical_on_rebuild(tmp_path: Path, builds: dict[str, Path]) -> None:
    rc, again = _build_dir(_cohort_with_holdout(), tmp_path, "with_holdout", *HOLDOUT_ARGS)
    assert rc == 0
    for path in sorted(builds["with_holdout"].iterdir()):
        assert (again / path.name).read_bytes() == path.read_bytes(), path.name


def test_a_holdout_record_is_dropped_before_any_record_check(tmp_path: Path) -> None:
    # Read, this record would refuse the build (a projected record lacking required modules).
    broken = _as_holdout(fx.build_fictional_cohort()[0])
    del broken["protocolSection"]["conditionsModule"]
    rc, out = _build_dir([*fx.build_fictional_cohort(), broken], tmp_path, "broken", *HOLDOUT_ARGS)
    assert rc == 0
    assert json.loads((out / "run.json").read_text())["holdout_exclusion"]["excluded_from_universe"] == 1
    with pytest.raises(ValueError, match="lacks conditionsModule"):
        benchmarks.load_study_records(out.parent / "broken_studies")  # no exclusion: the record is read


def test_weekly_records_and_mesh_sidecar_drop_the_holdout(tmp_path: Path) -> None:
    doc = fx.build_weekly_full_records_doc()
    holdout_row = copy.deepcopy(doc["data"][0])
    holdout_row["nct_id"] = HOLDOUT
    doc["data"].append(holdout_row)
    weekly = tmp_path / "demographics.json.gz"
    weekly.write_bytes(gzip.compress(json.dumps(doc).encode("utf-8"), mtime=0))
    ids = [r["nct_id"] for r in doc["data"]]
    mesh_lines = [
        {"schema_version": "1.0", "nct_id": n, "state": "indexed",
         "meshes": [{"id": "D003924", "term": "Diabetes Mellitus, Type 2"}], "ancestors": [], "refetched": False,
         "snapshot_date": "2026-11-21", "extracted_at": fx.WEEKLY_EXTRACTED_AT,
         "pipeline_commit": fx.WEEKLY_PIPELINE_COMMIT}
        for n in ids
    ]
    mesh_lines[-1]["state"] = "not a state"  # the holdout's line: read, it would refuse the sidecar
    mesh = tmp_path / "condition_mesh.jsonl.gz"
    mesh.write_bytes(gzip.compress(b"".join((json.dumps(x) + "\n").encode() for x in mesh_lines), mtime=0))
    out = tmp_path / "weekly"
    rules = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")
    assert cli_build.main(["--demographics", str(weekly), "--condition-mesh", str(mesh), "--sponsor-rules", str(rules),
                           "--generated-at", GENERATED_AT, "--out", str(out), *HOLDOUT_ARGS]) == 0
    run = json.loads((out / "run.json").read_text())
    assert run["holdout_exclusion"]["excluded_from_universe"] == 1
    assert run["inputs"]["study_records"]["n_records"] == 5
    assert run["inputs"]["condition_mesh"]["n_lines"] == 5
    assert json.loads((out / "summary.json").read_text())["denominators"]["all_trials"]["n"] == 5
    assert _files_naming_a_holdout(out) == []


def test_a_response_log_line_drops_the_holdout_from_requested_and_returned(tmp_path: Path) -> None:
    cohort = fx.build_fictional_cohort()
    pair = [cohort[0], _as_holdout(cohort[0])]
    ids = [s["protocolSection"]["identificationModule"]["nctId"] for s in pair]
    body = json.dumps({"studies": pair})
    lines = [{"url": f"https://clinicaltrials.gov/api/v2/studies?filter.ids={','.join(ids)}", "ids": ids,
              "status": 200, "body": body, "body_sha256": hashlib.sha256(body.encode()).hexdigest()}]
    for study in cohort[1:]:
        one = json.dumps(study)
        nct = study["protocolSection"]["identificationModule"]["nctId"]
        lines.append({"url": f"https://clinicaltrials.gov/api/v2/studies/{nct}", "status": 200, "body": one,
                      "body_sha256": hashlib.sha256(one.encode()).hexdigest()})
    log = tmp_path / "responses.jsonl.gz"
    log.write_bytes(gzip.compress(b"".join((json.dumps(x) + "\n").encode() for x in lines), mtime=0))
    exclusion = HX.HoldoutExclusion(HX.load_holdout_index(HOLDOUT_INDEX))
    records, meta = benchmarks.load_study_records(log, exclusion=exclusion)
    assert [benchmarks.record_nct_id(r) for r in records] == [
        s["protocolSection"]["identificationModule"]["nctId"] for s in cohort]
    assert meta["n_records"] == len(cohort) and exclusion.n_excluded == 1


def test_a_snapshot_of_only_holdouts_is_refused(tmp_path: Path) -> None:
    rc, out = _build_dir([_as_holdout(fx.build_fictional_cohort()[0])], tmp_path, "only", *HOLDOUT_ARGS)
    assert rc == 1 and not out.exists()


def test_build_without_holdout_index_exits_nonzero_writing_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        _build_dir(_cohort_with_holdout(), tmp_path, "no_index")
    assert exited.value.code == 2
    err = capsys.readouterr().err
    assert "usage:" in err and "--holdout-index" in err
    assert not (tmp_path / "no_index").exists()


@pytest.mark.parametrize("payload", [
    b'{"trials": []}', b'{"rows": [{"nct_id": "NCT99777701", "in_audit": "true"}]}',
    b'{"rows": [{"nct_id": "NCT9977770", "in_audit": true}]}', b"[]",
])
def test_a_malformed_index_is_refused_writing_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], payload: bytes
) -> None:
    bad = tmp_path / "index.json"
    bad.write_bytes(payload)
    rc, out = _build_dir(_cohort_with_holdout(), tmp_path, "bad_index", "--holdout-index", str(bad))
    assert rc == 1 and not out.exists()
    assert "NCT9977770" not in capsys.readouterr().err


def test_contract_requires_a_valid_stamp(builds: dict[str, Path]) -> None:
    loaded = artifacts.load_dir(builds["with_holdout"])
    assert artifacts.check_contract(loaded) == []
    missing = copy.deepcopy(loaded)
    del missing["run.json"]["holdout_exclusion"]
    assert any("holdout_exclusion is missing" in p for p in artifacts.check_contract(missing))
    for key, value in (("excluded_from_universe", -1), ("holdouts_in_index", None), ("index_sha256", 7),
                       ("rules_version", None)):
        bad = copy.deepcopy(loaded)
        bad["run.json"]["holdout_exclusion"][key] = value
        assert any(f"holdout_exclusion.{key}" in p for p in artifacts.check_contract(bad)), key
    not_object = copy.deepcopy(loaded)
    not_object["run.json"]["holdout_exclusion"] = [1]
    assert any("not an object" in p for p in artifacts.check_contract(not_object))


def _rewrite_run(build: Path, dst: Path, edit: Any) -> Path:
    """A copy of ``build`` whose run.json is edited (an old or hand-made build)."""
    dst.mkdir()
    for path in build.iterdir():
        (dst / path.name).write_bytes(path.read_bytes())
    run = json.loads((dst / "run.json").read_text())
    edit(run)
    (dst / "run.json").write_bytes(artifacts.dump_manifest(run))
    return dst


def test_export_carries_the_stamp(tmp_path: Path, builds: dict[str, Path]) -> None:
    site = tmp_path / "site"
    assert site_export.main([str(builds["with_holdout"]), str(site), *HOLDOUT_ARGS]) == 0
    run = json.loads((site / "run.json").read_text())
    assert run["holdout_exclusion"] == json.loads((builds["with_holdout"] / "run.json").read_text())["holdout_exclusion"]
    assert _files_naming_a_holdout(site) == []


def test_export_requires_holdout_index(tmp_path: Path, builds: dict[str, Path]) -> None:
    with pytest.raises(SystemExit):
        site_export.main([str(builds["with_holdout"]), str(tmp_path / "site")])
    assert not (tmp_path / "site").exists()


def test_export_refuses_a_build_without_the_stamp(
    tmp_path: Path, builds: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    old = _rewrite_run(builds["with_holdout"], tmp_path / "old", lambda run: run.pop("holdout_exclusion"))
    site = tmp_path / "site"
    assert site_export.main([str(old), str(site), *HOLDOUT_ARGS]) == 1
    assert "holdout_exclusion is missing" in capsys.readouterr().err
    assert not site.exists()


def test_export_refuses_a_stamp_of_another_index(
    tmp_path: Path, builds: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    changed = tmp_path / "index.json"
    changed.write_text(json.dumps({"rows": [{"nct_id": HOLDOUT, "in_audit": True}]}), encoding="utf-8")
    site = tmp_path / "site"
    assert site_export.main([str(builds["with_holdout"]), str(site), "--holdout-index", str(changed)]) == 1
    assert "the index changed since the build" in capsys.readouterr().err
    assert not site.exists()


def test_export_refuses_a_build_whose_output_names_a_holdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A valid build whose non-holdout trial's sponsor is written with a holdout's id: the per-trial
    # rows and sponsor tables carry it, and the exporter's scan refuses the build.
    cohort = fx.build_fictional_cohort()
    cohort[1]["protocolSection"]["sponsorCollaboratorsModule"]["leadSponsor"]["name"] = f"Sponsor of {HOLDOUT}"
    rc, out = _build_dir(cohort, tmp_path, "mentions", *HOLDOUT_ARGS)
    assert rc == 0 and _files_naming_a_holdout(out)
    site = tmp_path / "site"
    site.mkdir()
    (site / "keep.txt").write_text("untouched")
    assert site_export.main([str(out), str(site), *HOLDOUT_ARGS]) == 1
    err = capsys.readouterr().err
    assert "name an in_audit holdout trial" in err and HOLDOUT not in err
    assert sorted(p.name for p in site.iterdir()) == ["keep.txt"]


def test_a_record_whose_protocol_id_is_a_holdout_is_dropped_or_refused_whatever_its_top_level_id() -> None:
    # _extract_core_metadata reads protocolSection.identificationModule.nctId, so the drop must too.
    cohort = fx.build_fictional_cohort()
    clean_id = cohort[0]["protocolSection"]["identificationModule"]["nctId"]
    blank_top = {**_as_holdout(cohort[0]), "nct_id": ""}
    exclusion = HX.HoldoutExclusion(HX.load_holdout_index(HOLDOUT_INDEX))
    assert benchmarks.drop_holdouts([blank_top, *cohort[1:]], exclusion) == cohort[1:]
    assert exclusion.n_excluded == 1
    clean_top = {**_as_holdout(cohort[0]), "nct_id": clean_id}
    with pytest.raises(HX.HoldoutIndexError) as refused:
        benchmarks.drop_holdouts([*cohort[1:], clean_top], HX.HoldoutExclusion(HX.load_holdout_index(HOLDOUT_INDEX)))
    assert f"study record {len(cohort)}" in str(refused.value) and HOLDOUT not in str(refused.value)
    agreeing = {**cohort[0], "nct_id": clean_id}
    assert benchmarks.drop_holdouts([agreeing], HX.HoldoutExclusion(HX.load_holdout_index(HOLDOUT_INDEX))) == [agreeing]
