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

    # Optional MeSH sidecar overriding one trial
    mesh_sidecar = tmp_path / "condition_mesh.jsonl.gz"
    mesh_line = {
        "schema_version": "1.0",
        "nct_id": "NCT99000110",
        "state": "indexed",
        "meshes": [{"id": "D006333", "term": "Heart Failure"}],
        "ancestors": [],
    }
    mesh_sidecar.write_bytes(
        gzip.compress((json.dumps(mesh_line) + "\n").encode("utf-8"), mtime=0)
    )

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
    weekly_doc = {
        "extracted_at": "2026-11-21T00:00:00+00:00",
        "pipeline_commit": "deadbeef1234",
        "data": [
            {
                "nct_id": f"NCT9900040{i}",
                "brief_title": f"Weekly trial {i}",
                "study_type": "INTERVENTIONAL",
                "phase": "PHASE3",
                "enrollment": 100,
                "enrollment_type": "ACTUAL",
                "status": "COMPLETED",
                "start_date": "2023-06-01",
                "primary_completion_date": "2024-06-01",
                "results_date": "2024-10-01",
                "sponsor_class": "INDUSTRY",
                "lead_sponsor_name": "Acme Bio",
                "collaborators": [],
                "study_sites": [{"facility": "US1", "state": "MA", "country": "United States"}],
                "conditions": ["Type 2 Diabetes"],
                "race": {
                    "reported": True,
                    "omb_totals": {
                        "white": 60,
                        "black_african_american": 25,
                        "asian": 10,
                        "unknown_not_reported": 5,
                    },
                    "raw_categories": [
                        {"omb_category": "white", "original": "White", "count": 60},
                        {"omb_category": "black_african_american", "original": "Black", "count": 25},
                        {"omb_category": "asian", "original": "Asian", "count": 10},
                        {"omb_category": "unknown_not_reported", "original": "Unknown", "count": 5},
                    ],
                    "skipped_ethnicity_rows": [],
                    "flags": [],
                },
                "ethnicity": {
                    "reported": True,
                    "omb_totals": {
                        "hispanic_latino": 15,
                        "not_hispanic_latino": 80,
                        "unknown_not_reported": 5,
                    },
                    "raw_categories": [
                        {"omb_category": "hispanic_latino", "original": "Hispanic or Latino", "count": 15},
                        {"omb_category": "not_hispanic_latino", "original": "Not Hispanic or Latino", "count": 80},
                    ],
                    "flags": [],
                },
                "sex_gender": {
                    "sex_report_status": "reported",
                    "reported_sex": True,
                    "reported_gender": False,
                    "reported_both": False,
                    "n_female": 50.0,
                    "n_male": 50.0,
                    "n_unknown": 0.0,
                    "n_gender_diverse": 0.0,
                    "n_ambiguous_gender": 0.0,
                    "is_participant_count": True,
                    "uninformative_reason": None,
                    "declared_not_collected": False,
                    "enrollment_minus_parsed": 0.0,
                    "parser_rules_version": "1.0.0",
                },
            }
            for i in range(1, 6)
        ],
    }
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
