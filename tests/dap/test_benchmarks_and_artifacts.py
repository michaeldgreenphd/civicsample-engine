"""Tests for dap.benchmarks, dap.artifacts, CLI build/contract scripts, and code standards."""
from __future__ import annotations

import ast
import copy
import gzip
import importlib.util
import json
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
import check_dap_benchmarks_contract as cli_check  # noqa: E402
from dap import artifacts, benchmarks  # noqa: E402
from src import condition_mesh as cmesh  # noqa: E402


def _write_mesh_sidecar(path: Path, lines: list[dict[str, Any]]) -> Path:
    """Write ``condition_mesh.jsonl.gz`` lines deterministically (``mtime=0``)."""
    raw = b"".join((json.dumps(line) + "\n").encode("utf-8") for line in lines)
    path.write_bytes(gzip.compress(raw, mtime=0))
    return path


@pytest.mark.parametrize(
    ("start_date", "pcomp_date", "expected_era"),
    [
        ("2016-12-31", None, "pre_2017_final_rule"),
        ("2016-12", None, "pre_2017_final_rule"),
        ("2016", None, "pre_2017_final_rule"),
        ("2017-01-17", None, "pre_2017_final_rule"),
        ("2017-01-18", None, "post_2017_pre_fdora"),
        ("2017-02", None, "post_2017_pre_fdora"),
        ("2022-12-28", None, "post_2017_pre_fdora"),
        ("2022-12-29", None, "post_2022_fdora"),
        ("2023-01", None, "post_2022_fdora"),
        ("2023", None, "post_2022_fdora"),
        # Partial dates straddling cutoffs MUST be era_indeterminate (never invent -01!)
        ("2017-01", None, "era_indeterminate"),
        ("2017", None, "era_indeterminate"),
        ("2022-12", None, "era_indeterminate"),
        ("2022", None, "era_indeterminate"),
        # Missing start_date bounded by pre-2017 primary_completion_date vs post-2017 indeterminate
        (None, "2016-06", "pre_2017_final_rule"),
        (None, "2020-06", "era_indeterminate"),
        (None, None, "era_indeterminate"),
    ],
)
def test_regulatory_era_partial_date_discipline(
    start_date: str | None, pcomp_date: str | None, expected_era: str
) -> None:
    study = fx.make_raw_ctgov_study(
        "NCT99000301",
        start_date=start_date,
        primary_completion_date=pcomp_date,
    )
    era = benchmarks.classify_regulatory_era(study)
    assert era["regulatory_era"] == expected_era


def test_short_ontology_keywords_match_only_standing_alone() -> None:
    """Keywords of <= 4 characters need word boundaries; longer keywords keep substring matching."""
    clf = benchmarks.OntologyClassifier()
    lookup = clf._flat_lookup

    # Short keywords inside unrelated words no longer decide the category.
    for condition, short_kw in (
        ("Falls", "all"),
        ("Trauma", "ra"),
        ("Behavioral Symptoms", "ms"),
        ("Caregiver Burnout", "burn"),
        ("Tardive Dyskinesia", "skin"),
    ):
        assert len(short_kw) <= 4
        assert short_kw in clf._normalize(condition)  # the old substring test hit it
        assert clf.classify_one(condition) != lookup[short_kw], condition

    # Whole-word short keywords (with an optional plural suffix) and long
    # keywords inside compounds still classify.
    expected = {
        "Neoplasms": ("Oncology", "Other Oncology"),
        "HIV Infections": ("Infectious Disease", "HIV/AIDS"),
        "Gliosarcoma": ("Oncology", "Sarcoma"),
        "Erythroleukemia": ("Oncology", "Hematologic Malignancy"),
        "Rheumatoid Arthritis": ("Autoimmune and Inflammatory", "Rheumatoid Arthritis"),
        "Multiple Sclerosis": ("Neurology", "Multiple Sclerosis"),
        "MS": ("Neurology", "Multiple Sclerosis"),
        "UTIs": lookup["uti"],
    }
    for condition, pair in expected.items():
        assert clf.classify_one(condition) == pair, condition

    # Longest-first order still holds: 'heart failure' wins over 'heart'.
    assert lookup["heart failure"] != lookup["heart"]
    assert clf.classify_one("Congestive Heart Failure") == lookup["heart failure"]
    assert clf.classify_one("Heart Disease") == lookup["heart"]


def test_sponsor_three_review_states_and_zero_substring_matching(tmp_path: Path) -> None:
    rules_path = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")
    studies = fx.build_fictional_cohort()
    res = benchmarks.build_benchmarks(
        studies,
        sponsor_rules_path=rules_path,
        min_cell_trials=5,
    )
    sponsors = res["sponsors"]
    summary = sponsors["attribution_summary"]

    # Merck KGaA MUST stay reviewed_excluded and NEVER match Merck & Co!
    trials_by_nct = {t["nct_id"]: t for t in res["trial_rows"]}
    assert trials_by_nct["NCT99000105"]["sponsors"]["lead"]["review_state"] == "attributed"
    assert trials_by_nct["NCT99000105"]["sponsors"]["lead"]["canonical_companies"] == ["Merck & Co"]

    assert trials_by_nct["NCT99000106"]["sponsors"]["lead"]["review_state"] == "reviewed_excluded"
    assert trials_by_nct["NCT99000106"]["sponsors"]["lead"]["canonical_companies"] == []

    assert trials_by_nct["NCT99000107"]["sponsors"]["lead"]["review_state"] == "unreviewed"
    assert trials_by_nct["NCT99000107"]["sponsors"]["lead"]["canonical_companies"] == []

    # Partnership shared=yes literal maps NCT99000103 to BOTH Acme Bio and Zenith Pharma
    # while total attributed trial count deduplicates on nct_id!
    assert trials_by_nct["NCT99000103"]["sponsors"]["current_owner"]["any"] == [
        "Acme Bio",
        "Zenith Pharma",
    ]
    assert summary["literal_counts_by_review_state"]["reviewed_excluded"] == 1
    assert summary["literal_counts_by_review_state"]["unreviewed"] >= 2

    # Subsidiary rollup: NCT99000102 rolls up to Acme Bio in current_owner and stays
    # legacysub in as_registered!
    assert trials_by_nct["NCT99000102"]["sponsors"]["current_owner"]["lead"] == ["Acme Bio"]
    assert trials_by_nct["NCT99000102"]["sponsors"]["as_registered"]["lead"] == ["legacysub"]

    acme_sc = next(c for c in sponsors["current_owner"] if c["company"] == "Acme Bio")
    # Acme touches NCT99000101, NCT99000102, NCT99000103, NCT99000108, NCT99000110 = 5 distinct trials
    assert acme_sc["by_role"]["any"]["n_trials"] == 5
    ent_names = {e["entity"] for e in acme_sc["constituent_entities"]}
    assert "legacysub" in ent_names


def test_small_cell_withholding_and_primary_vs_secondary_weights(tmp_path: Path) -> None:
    # 2 trials < min_cell_trials=5 -> withheld_small_cell=True, percentiles=None
    small = benchmarks.summarize_share_distribution(
        [(0.80, 800, 1000), (0.20, 2, 10)],
        min_cell_trials=5,
    )
    assert small["n_trials"] == 2
    assert small["n_participants"] == 1010
    assert small["withheld_small_cell"] is True
    assert small["percentiles"] is None
    assert small["trial_equal_mean"] is None
    assert small["participant_weighted_share"] is None

    # 5 trials >= min_cell_trials=5: one 10,000-person trial at 0.90 vs four 10-person trials at 0.10
    entries = [(0.10, 1, 10)] * 4 + [(0.90, 9000, 10000)]
    shown = benchmarks.summarize_share_distribution(entries, min_cell_trials=5)
    assert shown["withheld_small_cell"] is False
    assert shown["percentiles"]["p50"] == pytest.approx(0.10)  # trial-equally-weighted median!
    assert shown["trial_equal_mean"] == pytest.approx(0.26)
    assert shown["participant_weighted_share"] == pytest.approx(9004 / 10040, abs=1e-5)


def test_build_contract_and_byte_stable_run_identity(tmp_path: Path) -> None:
    studies = fx.build_fictional_cohort()
    studies_dir = fx.write_studies_dir(studies, tmp_path / "studies")
    rules_path = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")

    # Optional MeSH sidecar covering every cohort trial (as the build requires),
    # overriding one trial's derivedSection.
    mesh_sidecar = tmp_path / "condition_mesh.jsonl.gz"
    mesh_lines = [cmesh.select_condition_mesh(s) for s in studies]
    for line in mesh_lines:
        if line["nct_id"] == "NCT99000110":
            line.update(state="indexed", meshes=[{"id": "D006333", "term": "Heart Failure"}], ancestors=[])
    _write_mesh_sidecar(mesh_sidecar, mesh_lines)

    out_a = tmp_path / "build_a"
    out_b = tmp_path / "build_b"
    out_c = tmp_path / "build_c"

    rc_a = cli_build.main(
        [
            "--records-dir",
            str(studies_dir),
            "--condition-mesh",
            str(mesh_sidecar),
            "--sponsor-rules",
            str(rules_path),
            "--generated-at",
            "2026-11-21T12:00:00+00:00",
            "--out",
            str(out_a),
        ]
    )
    assert rc_a == 0
    assert cli_check.main([str(out_a)]) == 0

    # Rebuild with identical timestamp -> 100% byte-identical across every file including run.json!
    rc_b = cli_build.main(
        [
            "--records-dir",
            str(studies_dir),
            "--condition-mesh",
            str(mesh_sidecar),
            "--sponsor-rules",
            str(rules_path),
            "--generated-at",
            "2026-11-21T12:00:00+00:00",
            "--out",
            str(out_b),
        ]
    )
    assert rc_b == 0
    for fname in ("run.json", "summary.json", "strata.json", "sponsors.json", "trials.jsonl.gz"):
        assert (out_a / fname).read_bytes() == (out_b / fname).read_bytes(), fname

    # Rebuild at a later timestamp -> identical 10-hex run_identity!
    rc_c = cli_build.main(
        [
            "--records-dir",
            str(studies_dir),
            "--condition-mesh",
            str(mesh_sidecar),
            "--sponsor-rules",
            str(rules_path),
            "--generated-at",
            "2026-11-22T18:30:00+00:00",
            "--snapshot-date",
            "2026-11-21",
            "--out",
            str(out_c),
        ]
    )
    assert rc_c == 0
    run_a = json.loads((out_a / "run.json").read_text(encoding="utf-8"))
    run_c = json.loads((out_c / "run.json").read_text(encoding="utf-8"))
    assert run_a["run_id"][-10:] == run_c["run_id"][-10:]

    # Rebuild with changed parameter (--min-cell-trials 3) -> different run_identity!
    out_d = tmp_path / "build_d"
    assert (
        cli_build.main(
            [
                "--records-dir",
                str(studies_dir),
                "--condition-mesh",
                str(mesh_sidecar),
                "--sponsor-rules",
                str(rules_path),
                "--min-cell-trials",
                "3",
                "--generated-at",
                "2026-11-21T12:00:00+00:00",
                "--out",
                str(out_d),
            ]
        )
        == 0
    )
    run_d = json.loads((out_d / "run.json").read_text(encoding="utf-8"))
    assert run_d["run_id"][-10:] != run_a["run_id"][-10:]

    # Verify EVERY count in summary.json carries value, numerator, denominator, definition
    summary = json.loads((out_a / "summary.json").read_text(encoding="utf-8"))
    for item in summary["metrics"]:
        assert {"value", "numerator", "denominator", "definition"} <= set(item)
        assert item["value"] == item["numerator"]
    for blist in summary["breakdowns"].values():
        for item in blist:
            assert {"value", "numerator", "denominator", "definition"} <= set(item)
            assert item["value"] == item["numerator"]

    # Verify combined race/ethnicity quarantine count is explicit in summary.json
    quar_metric = next(
        m for m in summary["metrics"] if m["id"] == "combined_race_ethnicity_quarantined"
    )
    assert quar_metric["value"] == 1


def test_site_parts_are_refused_as_study_input(tmp_path: Path) -> None:
    part = tmp_path / "demographics.part1.json.gz"
    part.write_bytes(gzip.compress(b'{"data": []}', mtime=0))
    with pytest.raises(ValueError, match="site dataset parts"):
        benchmarks.load_study_records(part)


def test_weekly_full_records_container_supported(tmp_path: Path) -> None:
    weekly_doc = fx.build_weekly_full_records_doc()
    weekly_path = tmp_path / "demographics.json.gz"
    weekly_path.write_bytes(gzip.compress(json.dumps(weekly_doc).encode("utf-8"), mtime=0))
    rules_path = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")
    out_dir = tmp_path / "weekly_build"
    assert (
        cli_build.main(
            [
                "--demographics",
                str(weekly_path),
                "--sponsor-rules",
                str(rules_path),
                "--generated-at",
                "2026-11-21T00:00:00+00:00",
                "--out",
                str(out_dir),
            ]
        )
        == 0
    )
    assert cli_check.main([str(out_dir)]) == 0


def _weekly_mesh_lines(
    nct_ids: list[str],
    *,
    extracted_at: str = fx.WEEKLY_EXTRACTED_AT,
    pipeline_commit: str = fx.WEEKLY_PIPELINE_COMMIT,
) -> list[dict[str, Any]]:
    """Sidecar lines stamped as ``src/extract_all.py`` stamps them."""
    return [
        {
            "schema_version": "1.0",
            "nct_id": nct,
            "state": "indexed",
            "meshes": [{"id": "D003924", "term": "Diabetes Mellitus, Type 2"}],
            "ancestors": [],
            "refetched": False,
            "snapshot_date": "2026-11-21",
            "extracted_at": extracted_at,
            "pipeline_commit": pipeline_commit,
        }
        for nct in nct_ids
    ]


def _build_weekly_with_mesh(
    tmp_path: Path, mesh_lines: list[dict[str, Any]], out_name: str
) -> tuple[int, Path]:
    """Build from the fictional weekly full records with the given sidecar lines."""
    weekly_path = tmp_path / "demographics.json.gz"
    weekly_path.write_bytes(
        gzip.compress(json.dumps(fx.build_weekly_full_records_doc()).encode("utf-8"), mtime=0)
    )
    rules_path = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")
    mesh_path = _write_mesh_sidecar(tmp_path / f"{out_name}.condition_mesh.jsonl.gz", mesh_lines)
    out_dir = tmp_path / out_name
    rc = cli_build.main(
        [
            "--demographics",
            str(weekly_path),
            "--condition-mesh",
            str(mesh_path),
            "--sponsor-rules",
            str(rules_path),
            "--generated-at",
            "2026-11-21T00:00:00+00:00",
            "--out",
            str(out_dir),
        ]
    )
    return rc, out_dir


def test_weekly_mesh_sidecar_must_match_records_run_and_cover_cohort(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cohort = [r["nct_id"] for r in fx.build_weekly_full_records_doc()["data"]]

    # Matching sidecar: builds, and run.json records the matched stamps.
    rc, out_ok = _build_weekly_with_mesh(tmp_path, _weekly_mesh_lines(cohort), "ok")
    assert rc == 0
    assert cli_check.main([str(out_ok)]) == 0
    run = json.loads((out_ok / "run.json").read_text(encoding="utf-8"))
    mesh_in = run["inputs"]["condition_mesh"]
    assert mesh_in["extracted_at"] == fx.WEEKLY_EXTRACTED_AT == run["inputs"]["study_records"]["extracted_at"]
    assert (
        mesh_in["pipeline_commit"]
        == fx.WEEKLY_PIPELINE_COMMIT
        == run["inputs"]["study_records"]["pipeline_commit"]
    )
    assert mesh_in["snapshot_date"] == "2026-11-21"
    assert mesh_in["provenance_check"] == "full_records_run_stamps_matched_and_cohort_covered"
    assert mesh_in["n_cohort_trials"] == 5 and mesh_in["n_lines_outside_cohort"] == 0
    rows = artifacts.load_dir(out_ok)["trial_rows"]
    assert {r["indication"]["mesh_source"] for r in rows} == {"condition_mesh_sidecar"}
    capsys.readouterr()

    # Stale sidecar: another week's extracted_at -> refused, nothing written.
    stale = _weekly_mesh_lines(cohort, extracted_at="2026-11-14T00:00:00+00:00")
    rc, out_stale = _build_weekly_with_mesh(tmp_path, stale, "stale")
    err = capsys.readouterr().err
    assert rc == 1 and not out_stale.exists()
    assert "not from the study records' pull" in err and "2026-11-14T00:00:00+00:00" in err

    # Another commit's sidecar, even one line of it -> refused.
    mixed = _weekly_mesh_lines(cohort)
    mixed[2]["pipeline_commit"] = "0123abcd"
    rc, out_mixed = _build_weekly_with_mesh(tmp_path, mixed, "mixed")
    err = capsys.readouterr().err
    assert rc == 1 and not out_mixed.exists()
    assert "1 of 5 condition_mesh lines" in err and cohort[2] in err

    # Partial sidecar missing a cohort trial -> refused, naming the trial.
    rc, out_partial = _build_weekly_with_mesh(tmp_path, _weekly_mesh_lines(cohort[:-1]), "partial")
    err = capsys.readouterr().err
    assert rc == 1 and not out_partial.exists()
    assert "no line for 1 of 5 cohort trials" in err and cohort[-1] in err

    # Records without stamps cannot be matched to a sidecar -> refused.
    loaded_mesh, mesh_meta = benchmarks.load_condition_mesh(
        _write_mesh_sidecar(tmp_path / "plain.jsonl.gz", _weekly_mesh_lines(cohort))
    )
    records = fx.build_weekly_full_records_doc()["data"]
    unstamped_meta = {
        "input_format": "full_records",
        "path": "demographics.json",
        "extracted_at": None,
        "pipeline_commit": None,
    }
    with pytest.raises(ValueError, match="full records carry no extracted_at/pipeline_commit"):
        benchmarks.check_condition_mesh_provenance(loaded_mesh, mesh_meta, records, unstamped_meta)


def test_raw_records_mesh_sidecar_checks_coverage_and_one_run(tmp_path: Path) -> None:
    studies = fx.build_fictional_cohort()
    records, study_meta = benchmarks.load_study_records(fx.write_studies_dir(studies, tmp_path / "studies"))
    lines = [cmesh.select_condition_mesh(s) for s in studies]

    # Full coverage, unstamped lines: accepted; absent stamps recorded with the reason.
    mesh_map, mesh_meta = benchmarks.load_condition_mesh(
        _write_mesh_sidecar(tmp_path / "full.jsonl.gz", lines)
    )
    checked = benchmarks.check_condition_mesh_provenance(mesh_map, mesh_meta, records, study_meta)
    assert checked is not None
    assert checked["provenance_check"] == "cohort_covered_and_one_run_only"
    assert "records_directory input carries no extraction stamps" in checked["provenance_reason"]
    assert [checked[k] for k in benchmarks.MESH_RUN_STAMP_KEYS] == [None, None, None]
    assert checked["n_cohort_trials"] == len(studies)

    # Extra lines beyond the cohort (e.g. --limit) are allowed and counted.
    limited = records[:3]
    checked_lim = benchmarks.check_condition_mesh_provenance(mesh_map, mesh_meta, limited, study_meta)
    assert checked_lim is not None and checked_lim["n_lines_outside_cohort"] == len(studies) - 3

    # Missing a cohort trial -> refused.
    part_map, part_meta = benchmarks.load_condition_mesh(
        _write_mesh_sidecar(tmp_path / "part.jsonl.gz", lines[1:])
    )
    want = rf"no line for 1 of {len(studies)} cohort trials \(first: NCT99000101\)"
    with pytest.raises(ValueError, match=want):
        benchmarks.check_condition_mesh_provenance(part_map, part_meta, records, study_meta)

    # Lines from two extraction runs -> refused.
    two_runs = [
        dict(line, snapshot_date="2026-11-21", extracted_at="2026-11-21T00:00:00+00:00", pipeline_commit="aaa")
        for line in lines
    ]
    two_runs[0]["extracted_at"] = "2026-11-14T00:00:00+00:00"
    run_map, run_meta = benchmarks.load_condition_mesh(
        _write_mesh_sidecar(tmp_path / "runs.jsonl.gz", two_runs)
    )
    with pytest.raises(ValueError, match="carry 2 different"):
        benchmarks.check_condition_mesh_provenance(run_map, run_meta, records, study_meta)

    # No sidecar -> nothing to check.
    assert benchmarks.check_condition_mesh_provenance({}, None, records, study_meta) is None


def test_contract_checker_refuses_tampered_or_invariant_violating_builds(tmp_path: Path) -> None:
    studies_dir = fx.write_studies_dir(fx.build_fictional_cohort(), tmp_path / "studies")
    rules_path = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")
    valid = tmp_path / "valid"
    assert (
        cli_build.main(
            [
                "--records-dir",
                str(studies_dir),
                "--sponsor-rules",
                str(rules_path),
                "--generated-at",
                "2026-11-21T00:00:00+00:00",
                "--out",
                str(valid),
            ]
        )
        == 0
    )
    base_loaded = artifacts.load_dir(valid)
    assert artifacts.check_contract(base_loaded) == []

    # 1. Tamper bytes on disk -> CLI exits 1
    bad_disk = tmp_path / "bad_disk"
    bad_disk.mkdir()
    for p in valid.iterdir():
        (bad_disk / p.name).write_bytes(p.read_bytes())
    summary_obj = json.loads((bad_disk / "summary.json").read_text(encoding="utf-8"))
    summary_obj["metrics"][0]["value"] += 1
    summary_obj["metrics"][0]["numerator"] += 1
    (bad_disk / "summary.json").write_bytes(artifacts.dump(summary_obj))
    assert cli_check.main([str(bad_disk)]) == 1

    # 2. Extra file in build dir -> refused
    extra_dir = tmp_path / "extra"
    extra_dir.mkdir()
    for p in valid.iterdir():
        (extra_dir / p.name).write_bytes(p.read_bytes())
    (extra_dir / "stray.txt").write_text("stray", encoding="utf-8")
    assert cli_check.main([str(extra_dir)]) == 1

    # 3. Domain invariant: single_category_ethnicity with non-None of_classified
    tampered_sc = copy.deepcopy(base_loaded)
    sc_row = next(
        r for r in tampered_sc["trial_rows"]
        if r["demographics"]["ethnicity_table_status"] == "single_category_ethnicity"
    )
    sc_row["demographics"]["ethnicity"]["of_classified"] = {"hispanic_latino": 1.0, "not_hispanic_latino": 0.0}
    probs_sc = artifacts.check_contract(tampered_sc)
    assert any("single_category_ethnicity must withhold of_classified" in x for x in probs_sc)

    # 4. Domain invariant: combined_race_ethnicity_quarantined leaking OMB counts
    tampered_q = copy.deepcopy(base_loaded)
    q_row = next(
        r for r in tampered_q["trial_rows"]
        if r["demographics"]["race_table_status"] == "combined_race_ethnicity_quarantined"
    )
    q_row["demographics"]["race"]["counts"] = {"white": 60}
    probs_q = artifacts.check_contract(tampered_q)
    assert any("combined_race_ethnicity_quarantined" in x for x in probs_q)

    # 5. Domain invariant: small cell leaking percentiles
    tampered_small = copy.deepcopy(base_loaded)
    small_cell = next(
        s for s in tampered_small["strata.json"]["strata"] if s["pooled"]["n_trials"] < 5
    )
    small_cell["pooled"]["us_facility_share_distribution"]["percentiles"] = {
        "p10": 0.1,
        "p25": 0.2,
        "p50": 0.3,
        "p75": 0.4,
        "p90": 0.5,
    }
    probs_small = artifacts.check_contract(tampered_small)
    assert any("withhold percentiles" in x for x in probs_small)


def test_contract_reports_non_integer_min_cell_trials_instead_of_raising(tmp_path: Path) -> None:
    studies_dir = fx.write_studies_dir(fx.build_fictional_cohort(), tmp_path / "studies")
    rules_path = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")
    valid = tmp_path / "valid"
    assert (
        cli_build.main(
            [
                "--records-dir",
                str(studies_dir),
                "--sponsor-rules",
                str(rules_path),
                "--generated-at",
                "2026-11-21T00:00:00+00:00",
                "--out",
                str(valid),
            ]
        )
        == 0
    )
    base_loaded = artifacts.load_dir(valid)
    assert artifacts.check_contract(base_loaded) == []

    for bad_value in ("five", "5", True, 2.5, None, 0):
        tampered = copy.deepcopy(base_loaded)
        tampered["run.json"]["parameters"]["min_cell_trials"] = bad_value
        problems = artifacts.check_contract(tampered)  # must not raise
        want = f"run.json: parameters.min_cell_trials must be an integer >= 1 (got {bad_value!r})"
        assert want in problems, (bad_value, problems)
        # The fallback threshold is the build's own default (5): no cell is misjudged.
        assert not any("withhold percentiles" in x or "must publish percentiles" in x for x in problems)

    # The remaining checks still run after the problem: a leaked small cell is reported too.
    tampered_both = copy.deepcopy(base_loaded)
    tampered_both["run.json"]["parameters"]["min_cell_trials"] = "five"
    small_cell = next(
        s for s in tampered_both["strata.json"]["strata"] if s["pooled"]["n_trials"] < 5
    )
    small_cell["pooled"]["us_facility_share_distribution"]["percentiles"] = {
        "p10": 0.1,
        "p25": 0.2,
        "p50": 0.3,
        "p75": 0.4,
        "p90": 0.5,
    }
    probs_both = artifacts.check_contract(tampered_both)
    assert "run.json: parameters.min_cell_trials must be an integer >= 1 (got 'five')" in probs_both
    assert any("withhold percentiles" in x for x in probs_both)


def test_unicode_line_separators_in_jsonl_roundtrip(tmp_path: Path) -> None:
    """U+2028, U+2029, \\x85, \\x0b, \\x0c inside JSON strings must never split JSONL lines."""
    tricky_title = "Phase 3 Study\u2028LineSep\u2029ParaSep\x85NextLine\x0bVT\x0cFF"
    tricky_mesh_term = "Diabetes Mellitus\u2028Type 2\u2029Adult"
    study = fx.make_raw_ctgov_study(
        "NCT99000901",
        locations=[{"facility": "Clinic\u2028North\u2029Wing", "state": "MA", "country": "United States"}],
        conditions=["Type 2 Diabetes\u2028Mellitus"],
        lead_sponsor_name="Acme Bio",
        measures=[
            fx.make_measure("Race (NIH/OMB)", [("White", "80"), ("Black or African American", "20")]),
            fx.make_measure("Sex: Female, Male", [("Female", "50"), ("Male", "50")]),
        ],
    )
    study["protocolSection"]["identificationModule"]["briefTitle"] = tricky_title

    studies_jsonl = tmp_path / "unicode_studies.jsonl.gz"
    studies_jsonl.write_bytes(
        gzip.compress(
            (json.dumps(study, ensure_ascii=False) + "\n").encode("utf-8"),
            mtime=0,
        )
    )
    mesh_jsonl = tmp_path / "unicode_mesh.jsonl.gz"
    mesh_row = {
        "schema_version": "1.0",
        "nct_id": "NCT99000901",
        "state": "indexed",
        "meshes": [{"id": "D003924", "term": tricky_mesh_term}],
        "ancestors": [],
    }
    mesh_jsonl.write_bytes(
        gzip.compress(
            (json.dumps(mesh_row, ensure_ascii=False) + "\n").encode("utf-8"),
            mtime=0,
        )
    )

    rules_path = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")
    out_dir = tmp_path / "unicode_build"
    assert (
        cli_build.main(
            [
                "--records",
                str(studies_jsonl),
                "--condition-mesh",
                str(mesh_jsonl),
                "--sponsor-rules",
                str(rules_path),
                "--generated-at",
                "2026-11-22T12:00:00+00:00",
                "--out",
                str(out_dir),
            ]
        )
        == 0
    )
    assert cli_check.main([str(out_dir)]) == 0

    reloaded = artifacts.load_dir(out_dir)
    assert len(reloaded["trial_rows"]) == 1
    assert reloaded["trial_rows"][0]["brief_title"] == tricky_title
    assert reloaded["trial_rows"][0]["indication"]["meshes"][0]["term"] == tricky_mesh_term


def test_producer_paths_cover_the_build_import_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = cli_build.producer_paths()
    # The modules the build executes beyond dap/: full-record loading, the
    # race/ethnicity extractors, the sex/gender table, condition MeSH, sponsors.
    for dep in (
        "scripts/build_dap_benchmarks.py",
        "dap/__init__.py",
        "dap/artifacts.py",
        "dap/benchmarks.py",
        "dap/demographics.py",
        "dap/geography.py",
        "src/full_records.py",
        "src/race_extractor.py",
        "src/ethnicity_extractor.py",
        "src/sex_gender_table.py",
        "src/sex_gender_parser.py",
        "src/condition_mesh.py",
        "sponsors/adapter.py",
        "sponsors/audit_states.py",
        "sponsors/sponsor_roles.py",
    ):
        assert dep in paths, dep
    assert paths == sorted(set(paths))
    # Repo modules loaded in this process but not imported by the build stay out,
    # so the digest does not depend on what else the process imported.
    assert "scripts/check_dap_benchmarks_contract.py" in {
        p.relative_to(ROOT).as_posix() for p in cli_build._loaded_repo_modules(ROOT).values()
    }
    assert "scripts/check_dap_benchmarks_contract.py" not in paths
    assert not any(p.startswith("tests/") for p in paths)

    # The digest covers every listed file: a change to a src/ dependency moves it.
    for rel in paths:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((ROOT / rel).read_bytes())
    same = cli_build.source_digest(tmp_path)
    assert same == cli_build.source_digest(ROOT)
    (tmp_path / "src" / "full_records.py").write_bytes(
        (ROOT / "src" / "full_records.py").read_bytes() + b"\n# changed\n"
    )
    assert cli_build.source_digest(tmp_path) != same

    # The dirty check asks git about exactly the same list.
    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = "abc123\n"

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _Done:
        calls.append(list(cmd))
        return _Done()

    monkeypatch.setattr(cli_build.subprocess, "run", _fake_run)
    info = cli_build.producer_info(ROOT)
    status_call = next(c for c in calls if c[:2] == ["git", "status"])
    assert status_call[status_call.index("--") + 1 :] == paths
    assert info["source_sha256"] == cli_build.source_digest(ROOT, paths)


def test_codebase_ast_typing_future_annotations_and_zero_rng() -> None:
    """Enforce CLAUDE.md Python 3.11, full type annotations, and zero-RNG constraints."""
    target_files = sorted((ROOT / "dap").rglob("*.py")) + [
        ROOT / "scripts" / "build_dap_benchmarks.py",
        ROOT / "scripts" / "check_dap_benchmarks_contract.py",
        ROOT / "scripts" / "export_dap_benchmarks_site.py",
    ]
    assert len(target_files) == 8

    forbidden_modules = {"random"}
    for fpath in target_files:
        source = fpath.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(fpath))

        has_future = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in tree.body
        )
        assert has_future, f"{fpath}: missing from __future__ import annotations"

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in forbidden_modules, f"{fpath}: imports {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_modules, f"{fpath}: imports from {node.module}"
            elif isinstance(node, ast.Attribute):
                assert node.attr not in ("default_rng", "RandomState"), f"{fpath}: uses RNG {node.attr}"
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.returns is not None, f"{fpath}:{node.lineno} {node.name} lacks return type annotation"
                all_args = (
                    list(node.args.posonlyargs)
                    + list(node.args.args)
                    + list(node.args.kwonlyargs)
                )
                if node.args.vararg is not None:
                    all_args.append(node.args.vararg)
                if node.args.kwarg is not None:
                    all_args.append(node.args.kwarg)
                for arg in all_args:
                    if arg.arg in ("self", "cls"):
                        continue
                    assert arg.annotation is not None, (
                        f"{fpath}:{node.lineno} {node.name} parameter {arg.arg!r} lacks type annotation"
                    )
