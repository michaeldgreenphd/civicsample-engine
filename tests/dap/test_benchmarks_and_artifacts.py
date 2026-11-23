"""Tests for dap.benchmarks, dap.artifacts, CLI build/contract scripts, and code standards."""
from __future__ import annotations

import ast
import copy
import gzip
import hashlib
import importlib.util
import json
import re
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

    # The fuzzy step offers only longer keywords (as in #109's src/condition_classifier.py): a
    # near-miss of a short acronym is a different word ("Fall" scores 85.7 against "all").
    assert all(len(kw) > 4 for kw in clf._fuzzy_keywords)
    assert clf.classify_one("Fall") != lookup["all"]
    assert clf.classify_one("Schizophernia") == lookup["schizophrenia"]  # a typo in a long keyword

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


def test_mesh_cells_are_one_per_descriptor_id_when_terms_differ(tmp_path: Path) -> None:
    studies = fx.build_fictional_cohort()
    old_term = "Diabetes Mellitus, Non-Insulin-Dependent"
    for s in studies:
        if s["protocolSection"]["identificationModule"]["nctId"] in ("NCT99000102", "NCT99000104"):
            s["derivedSection"]["conditionBrowseModule"]["meshes"] = [{"id": "D003924", "term": old_term}]
    studies_dir = fx.write_studies_dir(studies, tmp_path / "studies")
    rules_path = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")
    out_dir = tmp_path / "build"
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
                str(out_dir),
            ]
        )
        == 0
    )
    assert cli_check.main([str(out_dir)]) == 0
    strata_doc = json.loads((out_dir / "strata.json").read_text(encoding="utf-8"))
    ids = [s["stratum_id"] for s in strata_doc["strata"]]
    assert len(ids) == len(set(ids))

    # One cell per descriptor ID: the six US-only Phase 3 post-FDORA trials pool together.
    cells = [
        s for s in strata_doc["strata"]
        if s["stratum_kind"] == "mesh_footprint" and s["mesh_descriptor_id"] == "D003924"
        and s["site_footprint_class"] == "us_only"
    ]
    assert len(cells) == 1 and cells[0]["pooled"]["n_trials"] == 6
    # Display term: the one most trials carry (5 of 7 trials with D003924); the conflict is recorded.
    assert cells[0]["mesh_descriptor_term"] == "Diabetes Mellitus, Type 2"
    assert strata_doc["mesh_descriptor_term_conflicts"] == {"D003924": [old_term, "Diabetes Mellitus, Type 2"]}

    # The contract refuses duplicate stratum_ids.
    loaded = artifacts.load_dir(out_dir)
    loaded["strata.json"]["strata"].append(copy.deepcopy(cells[0]))
    assert any(
        x == f"strata.json: 1 stratum_id(s) are not unique (first: {cells[0]['stratum_id']})"
        for x in artifacts.check_contract(loaded)
    )


def test_site_parts_are_refused_as_study_input(tmp_path: Path) -> None:
    part = tmp_path / "demographics.part1.json.gz"
    part.write_bytes(gzip.compress(b'{"data": []}', mtime=0))
    with pytest.raises(ValueError, match="site dataset parts"):
        benchmarks.load_study_records(part)


def test_containers_with_non_object_entries_are_refused_not_filtered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = (fx.make_raw_ctgov_study(n) for n in ("NCT99000951", "NCT99000952"))
    weekly = fx.build_weekly_full_records_doc()
    weekly["data"].insert(2, None)
    containers: dict[str, tuple[str, Any, str]] = {
        "studies": ("studies.json", {"studies": [a, None, b]}, "studies: 1 of 3 entries"),
        "records": ("records.json", {"records": [a, "x", b]}, "records: 1 of 3 entries"),
        "top_list": ("list.json", [a, [1], b, 5], "top-level list: 2 of 4 entries"),
        "full_records": ("weekly.json", weekly, "data: 1 of 6 entries"),
        "response_log": (
            "log.jsonl",
            {"status": 200, "ids": ["NCT99000951", "NCT99000952"], "body": json.dumps({"studies": [a, None, b]})},
            ":1: body.studies: 1 of 3 entries",
        ),
    }
    for name, (fname, doc, where) in containers.items():
        path = tmp_path / fname
        text = json.dumps(doc) + ("\n" if fname.endswith(".jsonl") else "")
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match="not study objects") as err:
            benchmarks.load_study_records(path)
        assert where in str(err.value), (name, str(err.value))

    # The CLI refuses the input and writes nothing.
    out_dir = tmp_path / "out"
    rules_path = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")
    rc = cli_build.main(
        ["--records", str(tmp_path / "studies.json"), "--sponsor-rules", str(rules_path), "--out", str(out_dir)]
    )
    assert rc == 1 and not out_dir.exists()
    assert "(first: index 1 is NoneType); refusing the input" in capsys.readouterr().err


def test_response_log_lines_must_be_whole_successful_answers(tmp_path: Path) -> None:
    a, b, c = (fx.make_raw_ctgov_study(n) for n in ("NCT99000961", "NCT99000962", "NCT99000963"))
    base_url = "https://clinicaltrials.gov/api/v2/studies"

    def env(body: Any, **fields: Any) -> dict[str, Any]:
        """A complete (unprojected) cached response line: status, body, body_sha256, ids or url."""
        text = body if isinstance(body, str) or body is None else json.dumps(body)
        line: dict[str, Any] = {"status": 200, "body": text, **fields}
        if isinstance(text, str) and "body_sha256" not in fields:
            line["body_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return line

    def load(lines: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
        path = tmp_path / f"{name}.jsonl"
        path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
        return benchmarks.load_study_records(path)[0]

    good = env({"studies": [a, b]}, ids=["NCT99000961", "NCT99000962"])
    # Requested ids from `ids`, from the url's filter.ids, or from a single-study path.
    loaded = load(
        [good, env({"studies": [c]}, url=f"{base_url}?filter.ids=NCT99000963&pageSize=50")], "ok_filter"
    )
    assert [s["protocolSection"]["identificationModule"]["nctId"] for s in loaded] == [
        "NCT99000961",
        "NCT99000962",
        "NCT99000963",
    ]
    assert len(load([good, env(c, url=f"{base_url}/NCT99000963")], "ok_single")) == 3

    no_nct = copy.deepcopy(c)
    no_nct["protocolSection"]["identificationModule"].pop("nctId")
    bad_lines: dict[str, tuple[dict[str, Any], str]] = {
        "null_body": (env(None, ids=["NCT99000963"]), "holds no study"),
        "blank_body": (env("", ids=["NCT99000963"]), "response body is not JSON"),
        "body_without_studies": (env({"totalCount": 1}, ids=["NCT99000963"]), "holds no study"),
        "empty_studies": (env({"studies": []}, ids=["NCT99000963"]), "holds no study"),
        "failed_request": (env({"studies": [c]}, ids=["NCT99000963"], status=500), "status 500, not 200"),
        "altered_body": (env({"studies": [c]}, ids=["NCT99000963"], body_sha256="0" * 64), "body_sha256"),
        "no_requested_ids": (env({"studies": [c]}, url=base_url), "names no requested NCT ids"),
        "short_answer": (env({"studies": [c]}, ids=["NCT99000963", "NCT99000964"]), "1 requested trial(s) missing"),
        "extra_answer": (env({"studies": [c, a]}, ids=["NCT99000963"]), "1 unrequested trial(s)"),
        "study_without_nct": (env({"studies": [no_nct]}, ids=["NCT99000963"]), "without a valid NCT id"),
        "repeated_in_line": (env({"studies": [c, c]}, ids=["NCT99000963"]), "repeated NCT ids"),
        "repeated_across_lines": (env({"studies": [a]}, ids=["NCT99000961"]), "already returned on line 1"),
    }
    for name, (bad, message) in bad_lines.items():
        with pytest.raises(ValueError, match=re.escape(message)) as err:
            load([good, bad], name)
        assert f"{name}.jsonl:2" in str(err.value), (name, str(err.value))


def test_projected_or_incomplete_raw_records_are_refused(tmp_path: Path) -> None:
    """A fields= projection or a missing required protocol module refuses the input (see the audit caches)."""
    full = fx.make_raw_ctgov_study("NCT99000971")
    body = json.dumps({"studies": [full]})
    projected = {
        "status": 200,
        "url": "https://clinicaltrials.gov/api/v2/studies?filter.ids=NCT99000971"
        "&fields=NCTId,HasResults,ResultsSection,OutcomesModule,StatusModule,DesignModule",
        "body": body,
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    log = tmp_path / "projected.jsonl"
    log.write_text(json.dumps(projected) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"projected.jsonl:1: response was requested with fields=NCTId,HasResults"):
        benchmarks.load_study_records(log)

    # A study missing any required module is refused through every raw input shape.
    assert benchmarks.REQUIRED_PROTOCOL_MODULES == (
        "identificationModule",
        "statusModule",
        "sponsorCollaboratorsModule",
        "conditionsModule",
        "designModule",
    )
    for module in benchmarks.REQUIRED_PROTOCOL_MODULES:
        partial = fx.make_raw_ctgov_study("NCT99000972")
        del partial["protocolSection"][module]
        key = "NCT99000972" if module != "identificationModule" else "index 1"
        want = rf"1 of 2 study records lack required protocol modules or fields \(first: {key} lacks {module}\)"
        studies_dir = fx.write_studies_dir([full], tmp_path / f"dir_{module}")
        (studies_dir / "z_partial.json").write_text(json.dumps(partial), encoding="utf-8")
        container = tmp_path / f"{module}.json"
        container.write_text(json.dumps({"studies": [full, partial]}), encoding="utf-8")
        plain = tmp_path / f"{module}.jsonl"
        plain.write_text(json.dumps(full) + "\n" + json.dumps(partial) + "\n", encoding="utf-8")
        for path in (studies_dir, container, plain):
            with pytest.raises(ValueError, match=want):
                benchmarks.load_study_records(path)
    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps({"records": [full, {"nct_id": "NCT99000973", "conditions": ["X"]}]}), encoding="utf-8")
    with pytest.raises(ValueError, match=r"first: NCT99000973 lacks protocolSection"):
        benchmarks.load_study_records(flat)

    # No contactsLocationsModule and no derivedSection are real absences: the study loads and builds.
    bare = fx.make_raw_ctgov_study("NCT99000974", locations=None, meshes=None)
    assert "contactsLocationsModule" not in bare["protocolSection"] and "derivedSection" not in bare
    out_dir = tmp_path / "bare_build"
    rules_path = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")
    rc = cli_build.main(
        [
            "--records-dir",
            str(fx.write_studies_dir([bare], tmp_path / "bare")),
            "--sponsor-rules",
            str(rules_path),
            "--generated-at",
            "2026-11-21T00:00:00+00:00",
            "--out",
            str(out_dir),
        ]
    )
    assert rc == 0
    row = artifacts.load_dir(out_dir)["trial_rows"][0]
    assert row["geography"]["site_footprint_class"] == "no_listed_facilities"
    assert row["indication"]["mesh_state"] == "no_derived"
    assert row["indication"]["primary_condition"] != "Other"


@pytest.mark.parametrize("source_kind", ["full_records", "auto"])
@pytest.mark.parametrize(
    "phase",
    # separator-only or with an empty piece: normalize_phase dropped the pieces and read "no phase"
    ["BANANA", "PHASE3, PHASE_9", 3, None, ",", " / ", "PHASE1,,PHASE2", "PHASE2,", " "],
)
def test_weekly_records_with_a_phase_outside_ctgov_values_are_refused(
    tmp_path: Path, phase: object, source_kind: str
) -> None:
    # Weekly records never pass the raw-record checks, so "BANANA" became a phase stratum of its own.
    weekly_doc = fx.build_weekly_full_records_doc()
    weekly_doc["data"][1]["phase"] = phase
    weekly_path = tmp_path / "demographics.json.gz"
    weekly_path.write_bytes(gzip.compress(json.dumps(weekly_doc).encode("utf-8"), mtime=0))
    with pytest.raises(ValueError, match=r"weekly records carry a phase outside CT\.gov's values"):
        benchmarks.load_study_records(weekly_path, source_kind=source_kind)


def test_weekly_records_with_ctgov_phase_strings_load(tmp_path: Path) -> None:
    # Control: the weekly extractor's own forms, multi-phase and empty, pass. (A missing key is refused
    # since round 24: src/utils.py writes `phase` on every record.)
    weekly_doc = fx.build_weekly_full_records_doc()
    weekly_doc["data"][0]["phase"] = "PHASE1, PHASE2"
    weekly_doc["data"][1]["phase"] = ""
    weekly_path = tmp_path / "demographics.json.gz"
    weekly_path.write_bytes(gzip.compress(json.dumps(weekly_doc).encode("utf-8"), mtime=0))
    assert len(benchmarks.load_study_records(weekly_path, source_kind="full_records")[0]) == 5


_WEEKLY_SITE = {"facility": "US1", "state": "MA", "country": "United States"}


@pytest.mark.parametrize("source_kind", ["full_records", "auto"])
@pytest.mark.parametrize(
    ("key", "value", "problem"),
    [
        # On c37ed67 each loaded: "Cancer" was read one character at a time, a dropped condition could
        # move the indication to Other, and a dropped site lowered the exact site counts.
        ("conditions", "Cancer", "conditions is str, not a list"),
        ("conditions", ["Type 2 Diabetes", 7], "conditions[1] is int, not a string"),
        ("conditions", ["Type 2 Diabetes", "  "], "conditions[1] is blank"),
        ("conditions", None, "conditions is NoneType, not a list"),
        ("study_sites", [_WEEKLY_SITE, None], "study_sites[1] is NoneType, not an object"),
        ("study_sites", [dict(_WEEKLY_SITE, country=7)], "study_sites[0].country is int, not a string"),
        ("study_sites", [dict(_WEEKLY_SITE, state=["MA"])], "study_sites[0].state is list, not a string"),
        ("collaborators", ["Fictional CRO"], "collaborators[0] is str, not an object"),
        ("collaborators", [{"name": "Fictional CRO", "class": None}],
         "collaborators[0].class is NoneType, not a string"),
        ("conditions", [], "conditions is empty"),
        # src/utils.py keeps a site only for a location with a country.
        ("study_sites", [dict(_WEEKLY_SITE, country="")],
         "study_sites[0].country is missing or blank (src/utils.py keeps only sites with one)"),
        ("study_sites", [{"facility": "US1", "state": "MA"}],
         "study_sites[0].country is missing or blank (src/utils.py keeps only sites with one)"),
        # The other fields read, with the producer's type.
        ("lead_sponsor_name", None, "lead_sponsor_name is NoneType, not str"),
        ("lead_sponsor_name", ["Acme Bio"], "lead_sponsor_name is list, not str"),
        ("lead_sponsor_name", "  ", "lead_sponsor_name is blank"),
        ("sponsor_class", 7, "sponsor_class is int, not str"),
        ("enrollment", "100", "enrollment is str, not int"),
        ("enrollment", True, "enrollment is bool, not int"),
        ("status", "", "status is blank"),
        ("race", None, "race is NoneType, not an object"),
        ("sex_gender", "reported", "sex_gender is str, not an object"),
        ("sex_gender", {"sex_report_status": "BANANA"},
         "sex_gender.sex_report_status 'BANANA' is not one of SEX_GENDER_STATES"),
        # A partial row was reused as it stood: a reported trial with every flag and count empty.
        ("sex_gender", {"sex_report_status": "reported"},
         "sex_gender has no reported_sex (sgt.lean_row always writes it)"),
    ],
)
def test_weekly_records_with_malformed_list_fields_are_refused(
    tmp_path: Path, source_kind: str, key: str, value: object, problem: str
) -> None:
    weekly_doc = fx.build_weekly_full_records_doc()
    weekly_doc["data"][1][key] = value
    weekly_path = tmp_path / "demographics.json.gz"
    weekly_path.write_bytes(gzip.compress(json.dumps(weekly_doc).encode("utf-8"), mtime=0))
    message = f"1 of 5 weekly records carry a missing or malformed field (first: NCT99000402 {problem})"
    with pytest.raises(ValueError, match=re.escape(message)):
        benchmarks.load_study_records(weekly_path, source_kind=source_kind)


@pytest.mark.parametrize("source_kind", ["full_records", "auto"])
@pytest.mark.parametrize(
    "key",
    ["study_sites", "collaborators", "conditions", "lead_sponsor_name", "sponsor_class", "enrollment", "sex_gender",
     "phase"],
)
def test_weekly_records_missing_a_list_field_are_refused(tmp_path: Path, source_kind: str, key: str) -> None:
    # On 1e05c40 a missing key was skipped and read as an empty list: no sites, or ontology Other.
    weekly_doc = fx.build_weekly_full_records_doc()
    weekly_doc["data"][1].pop(key)
    weekly_path = tmp_path / "demographics.json.gz"
    weekly_path.write_bytes(gzip.compress(json.dumps(weekly_doc).encode("utf-8"), mtime=0))
    message = f"(first: NCT99000402 has no {key} (src/utils.py always writes it))"
    with pytest.raises(ValueError, match=re.escape(message)):
        benchmarks.load_study_records(weekly_path, source_kind=source_kind)


def test_weekly_list_fields_the_extractor_writes_load(tmp_path: Path) -> None:
    # Control: two sites and a collaborator as src/utils.py writes them pass.
    weekly_doc = fx.build_weekly_full_records_doc()
    weekly_doc["data"][0]["study_sites"] = [_WEEKLY_SITE, {"facility": "CA1", "city": "", "state": "", "zip": "",
                                                           "country": "Canada"}]
    weekly_doc["data"][0]["collaborators"] = [{"name": "Fictional CRO", "class": "INDUSTRY"}]
    weekly_path = tmp_path / "demographics.json.gz"
    weekly_path.write_bytes(gzip.compress(json.dumps(weekly_doc).encode("utf-8"), mtime=0))
    assert len(benchmarks.load_study_records(weekly_path, source_kind="full_records")[0]) == 5


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
                "--snapshot-date",
                "2026-11-21",
                "--out",
                str(out_dir),
            ]
        )
        == 0
    )
    assert cli_check.main([str(out_dir)]) == 0

    # Weekly study_sites are truncated: no footprint class is asserted from them,
    # so none of the five trials is counted as a US-site trial.
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    footprint = {i["label"]: i["value"] for i in summary["breakdowns"]["site_footprint_class"]}
    assert footprint == {fp: (5 if fp == "footprint_unknown" else 0) for fp in benchmarks.SITE_FOOTPRINT_CLASSES}
    assert summary["denominators"]["us_site_trials"]["n"] == 0
    assert summary["denominators"]["posted_us_site_trials"]["n"] == 0


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


def test_mesh_sidecar_line_state_must_agree_with_its_payload(tmp_path: Path) -> None:
    good_terms = [{"id": "D003924", "term": "Diabetes Mellitus, Type 2"}]
    base = fx.make_raw_ctgov_study("NCT99000801")
    modules: dict[str, dict[str, Any]] = {
        "indexed": {"meshes": good_terms, "ancestors": []},
        "not_indexed": {"meshes": []},
        "malformed": {"meshes": good_terms, "ancestors": [{"id": 1}]},
    }
    producer_studies: dict[str, dict[str, Any]] = {
        **{st: {**base, "derivedSection": {"conditionBrowseModule": mod}} for st, mod in modules.items()},
        "no_module": {**base, "derivedSection": {}},
        "no_derived": base,
    }
    # Everything src/condition_mesh.py writes, one line per state, loads.
    for i, (state, study) in enumerate(producer_studies.items()):
        line = cmesh.select_condition_mesh(study)
        assert line["state"] == state
        line["nct_id"] = f"NCT9900081{i}"
        ok_path = _write_mesh_sidecar(tmp_path / f"ok_{state}.jsonl.gz", [line])
        mesh_map, _ = benchmarks.load_condition_mesh(ok_path)
        assert mesh_map[line["nct_id"]]["state"] == state

    def line(**fields: Any) -> dict[str, Any]:
        return {"schema_version": "1.0", "nct_id": "NCT99000820", "meshes": None, "ancestors": None, **fields}

    bad_lines = {
        "indexed_null_meshes": line(state="indexed"),
        "indexed_empty_meshes": line(state="indexed", meshes=[]),
        "indexed_malformed_meshes": line(state="indexed", meshes=[{"id": "D1"}]),
        "indexed_meshes_not_list": line(state="indexed", meshes={"id": "D1", "term": "x"}),
        "not_indexed_with_descriptors": line(state="not_indexed", meshes=good_terms),
        "no_module_with_list": line(state="no_module", meshes=[]),
        "no_derived_with_ancestors": line(state="no_derived", ancestors=good_terms),
        "malformed_without_problem": line(state="malformed"),
        "malformed_blank_problem": line(state="malformed", problem=" "),
        "indexed_with_problem": line(state="indexed", meshes=good_terms, problem="x"),
        "bad_ancestors": line(state="indexed", meshes=good_terms, ancestors=["D1"]),
        "wrong_schema_version": line(state="no_derived", schema_version="2.0"),
        "missing_meshes_key": {k: v for k, v in line(state="no_derived").items() if k != "meshes"},
        # a list or object stamp reached the one-run check, where it raised TypeError (unhashable)
        "list_extracted_at": line(state="no_derived", extracted_at=["2026-11-15T06:00:00+00:00"]),
        "object_pipeline_commit": line(state="no_derived", pipeline_commit={"sha": "x"}),
        "number_snapshot_date": line(state="no_derived", snapshot_date=20261115),
    }
    for name, bad in bad_lines.items():
        path = _write_mesh_sidecar(tmp_path / f"bad_{name}.jsonl.gz", [bad])
        with pytest.raises(ValueError, match=r":1: NCT99000820: condition_mesh line breaks"):
            benchmarks.load_condition_mesh(path)


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

    # 3b. A trial key that is not an NCT ID (the build refuses such input; an edited file is refused here)
    for bad_key in ("123", "NCT\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668"):  # Arabic-Indic digits
        tampered_nct = copy.deepcopy(base_loaded)
        tampered_nct["trial_rows"][0]["nct_id"] = bad_key
        assert any(
            f"trial row nct_id {bad_key!r} is not NCT followed by eight digits" in x
            for x in artifacts.check_contract(tampered_nct)
        )

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


def test_contract_reports_non_integer_facility_counts_instead_of_raising(tmp_path: Path) -> None:
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
    multinat = [
        i for i, r in enumerate(base_loaded["trial_rows"])
        if r["geography"]["site_footprint_class"] == "multinational_with_us_sites"
    ]
    assert multinat

    missing = object()
    for key, bad in (
        ("n_facilities_with_country", "four"),
        ("n_us_facilities", "one"),
        ("n_us_facilities", None),
        ("n_us_facilities", missing),
        ("n_us_facilities", 1.0),
        ("n_us_facilities", True),
        ("n_us_facilities", 0),
        ("n_facilities_with_country", 1),  # == n_us: no non-US facility
    ):
        tampered = copy.deepcopy(base_loaded)
        geo = tampered["trial_rows"][multinat[0]]["geography"]
        if bad is missing:
            del geo[key]
        else:
            geo[key] = bad
        problems = artifacts.check_contract(tampered)  # must not raise
        nct = tampered["trial_rows"][multinat[0]]["nct_id"]
        assert any(
            x.startswith(f"{nct}: multinational_with_us_sites needs integer facility counts") for x in problems
        ), (key, bad, problems)

    # The CLI reports it and exits 1 instead of a traceback.
    bad_disk = tmp_path / "bad_disk"
    shutil.copytree(valid, bad_disk)
    rows = copy.deepcopy(base_loaded["trial_rows"])
    rows[multinat[0]]["geography"]["n_facilities_with_country"] = "four"
    (bad_disk / "trials.jsonl.gz").write_bytes(artifacts.encode_trials_gzip(rows))
    assert cli_check.main([str(bad_disk)]) == 1


def test_contract_reports_non_object_trial_rows_and_cli_never_tracebacks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
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

    # JSON-valid non-object rows are reported, not dereferenced.
    for bad in (None, "text", [1, 2], 5):
        tampered = copy.deepcopy(base_loaded)
        tampered["trial_rows"][3] = bad
        problems = artifacts.check_contract(tampered)  # must not raise
        assert f"trials.jsonl.gz: 1 row(s) are not JSON objects (first: row 4 is {type(bad).__name__})" in problems

    # On disk, through load_dir and the CLI: exit 1 with the problem, no traceback.
    bad_disk = tmp_path / "bad_disk"
    shutil.copytree(valid, bad_disk)
    raw = gzip.decompress((valid / "trials.jsonl.gz").read_bytes())
    (bad_disk / "trials.jsonl.gz").write_bytes(gzip.compress(b"null\n" + raw, mtime=0))
    capsys.readouterr()
    assert cli_check.main([str(bad_disk)]) == 1
    assert "1 row(s) are not JSON objects (first: row 1 is NoneType)" in capsys.readouterr().out

    # Any exception the checker did not anticipate is a reported failure, not a traceback.
    def _crash(loaded: dict[str, Any]) -> list[str]:
        raise RuntimeError("unanticipated shape")

    monkeypatch.setattr(cli_check.artifacts, "check_contract", _crash)
    assert cli_check.main([str(valid)]) == 1
    out = capsys.readouterr().out
    assert f"FAIL: contract check could not complete on {valid}: RuntimeError: unanticipated shape" in out


def test_generated_at_must_be_a_whole_utc_timestamp(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    studies_dir = fx.write_studies_dir(fx.build_fictional_cohort(), tmp_path / "studies")
    rules_path = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")

    def build(generated_at: str, name: str) -> int:
        return cli_build.main(
            [
                "--records-dir",
                str(studies_dir),
                "--sponsor-rules",
                str(rules_path),
                "--generated-at",
                generated_at,
                "--out",
                str(tmp_path / name),
            ]
        )

    for i, bad in enumerate(
        (
            "2026-99-99T12:00:00+00:00",  # impossible date
            "2026-02-30T12:00:00+00:00",
            "2026-11-21T12:00:00+00:00junk",  # trailing text
            "2026-11-21",  # no time
            "2026-11-21T12:00:00",  # naive
            "2026-11-21T12:00:00+01:00",  # not UTC
            "2026-11-21T12:00:00-00:00",  # RFC 3339: offset unknown, not a stated UTC time
        )
    ):
        capsys.readouterr()
        assert build(bad, f"bad{i}") == 1, bad
        assert "refused: generated_at must" in capsys.readouterr().err, bad
        assert not (tmp_path / f"bad{i}").exists(), bad

    assert build("2026-11-21T12:00:00Z", "zulu") == 0
    loaded = artifacts.load_dir(tmp_path / "zulu")
    assert loaded["run.json"]["run_id"].startswith("dap-2026-11-21-")
    assert artifacts.check_contract(loaded) == []

    # The contract re-parses generated_at and ties it to the run_id date.
    for edited, message in (
        ("2026-11-22T00:00:00+00:00", "run.json: run_id date 2026-11-21 != generated_at date 2026-11-22"),
        ("2026-99-99T00:00:00+00:00", "run.json: generated_at must be an ISO-8601 UTC timestamp"),
        ("2026-11-21T12:00:00+05:30", "run.json: generated_at must carry a UTC offset"),
        ("2026-11-21T12:00:00-00:00", "run.json: generated_at must carry a UTC offset"),
    ):
        tampered = copy.deepcopy(loaded)
        tampered["run.json"]["generated_at"] = edited
        assert any(x.startswith(message) for x in artifacts.check_contract(tampered)), edited


def test_snapshot_date_is_validated_recorded_and_part_of_the_run_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --snapshot-date stamped only the parsed sex/gender rows, which the trial projection drops, so the
    # asserted snapshot left no trace: two builds with different dates were byte-identical.
    studies_dir = fx.write_studies_dir(fx.build_fictional_cohort(), tmp_path / "studies")
    rules_path = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")

    def build(snapshot_date: str, name: str) -> int:
        return cli_build.main(["--records-dir", str(studies_dir), "--sponsor-rules", str(rules_path),
                               "--generated-at", "2026-11-21T12:00:00Z", "--snapshot-date", snapshot_date,
                               "--out", str(tmp_path / name)])

    for i, bad in enumerate(("2026-02-30", "garbage", "2026-11-01T00:00:00", "2026-11")):
        capsys.readouterr()
        assert build(bad, f"bad{i}") == 1, bad
        assert "refused: snapshot_date must" in capsys.readouterr().err, bad
        assert not (tmp_path / f"bad{i}").exists(), bad

    assert build("2024-06-01", "june") == 0 and build("2024-07-01", "july") == 0
    june, july = artifacts.load_dir(tmp_path / "june"), artifacts.load_dir(tmp_path / "july")
    assert june["run.json"]["parameters"]["snapshot_date"] == "2024-06-01"
    assert june["run.json"]["run_id"] != july["run.json"]["run_id"]
    assert artifacts.check_contract(june) == []
    for edited in ("2024-02-30", None):
        tampered = copy.deepcopy(june)
        tampered["run.json"]["parameters"]["snapshot_date"] = edited
        assert any(x.startswith("run.json: parameters.snapshot_date must") for x in artifacts.check_contract(tampered))


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


@pytest.mark.parametrize(
    "edit, expect",
    [
        ("drop_brief_title", "identificationModule.briefTitle"),
        ("drop_status_verified", "statusModule.statusVerifiedDate"),
        ("drop_study_type", "designModule.studyType"),
        ("drop_has_results", "hasResults"),
        ("string_has_results", "hasResults"),
        ("drop_results_section", "resultsSection"),
    ],
)
def test_records_with_projected_module_shells_or_results_parts_are_refused(
    tmp_path: Path, edit: str, expect: str
) -> None:
    # A field-by-field projection keeps the five module objects but drops fields every registration
    # holds, or keeps `hasResults: true` without the results section the baseline table is read from.
    complete = fx.make_raw_ctgov_study("NCT99800001")
    projected = fx.make_raw_ctgov_study("NCT99800002")
    ps = projected["protocolSection"]
    if edit == "drop_brief_title":
        ps["identificationModule"].pop("briefTitle")
    elif edit == "drop_status_verified":
        ps["statusModule"].pop("statusVerifiedDate")
    elif edit == "drop_study_type":
        ps["designModule"].pop("studyType")
    elif edit == "drop_has_results":
        projected.pop("hasResults")
    elif edit == "string_has_results":
        projected["hasResults"] = "true"
    else:
        projected.pop("resultsSection")
    rdir = tmp_path / "records"
    rdir.mkdir()
    (rdir / "a.json").write_text(json.dumps(complete), encoding="utf-8")
    (rdir / "b.json").write_text(json.dumps(projected), encoding="utf-8")
    with pytest.raises(ValueError, match=f"NCT99800002 lacks {expect}"):
        benchmarks.load_study_records(rdir)


def test_records_without_posted_results_are_still_accepted_as_complete(tmp_path: Path) -> None:
    study = fx.make_raw_ctgov_study("NCT99800003", has_results=False)
    rdir = tmp_path / "records"
    rdir.mkdir()
    (rdir / "a.json").write_text(json.dumps(study), encoding="utf-8")
    records, _meta = benchmarks.load_study_records(rdir)
    assert len(records) == 1


def _strip_payload(study: dict[str, Any], edit: str) -> None:
    """Apply one malformation to ``study``'s conditions or lead-sponsor payload, in place."""
    ps = study["protocolSection"]
    lead = ps["sponsorCollaboratorsModule"]["leadSponsor"]
    if edit == "empty_conditions_module":
        ps["conditionsModule"] = {}
    elif edit == "string_conditions":
        ps["conditionsModule"]["conditions"] = "Type 2 Diabetes"
    elif edit == "empty_conditions_list":
        ps["conditionsModule"]["conditions"] = []
    elif edit == "blank_condition_only":
        ps["conditionsModule"]["conditions"] = ["   "]
    elif edit == "blank_condition_entry":
        ps["conditionsModule"]["conditions"] = ["Type 2 Diabetes", "   "]
    elif edit == "non_string_condition_entry":
        ps["conditionsModule"]["conditions"] = ["Type 2 Diabetes", 7]
    elif edit == "drop_lead_sponsor":
        ps["sponsorCollaboratorsModule"].pop("leadSponsor")
    elif edit == "string_lead_sponsor":
        ps["sponsorCollaboratorsModule"]["leadSponsor"] = "Fictional Pharma Inc."
    elif edit == "drop_lead_name":
        lead.pop("name")
    elif edit == "blank_lead_name":
        lead["name"] = "  "
    elif edit == "drop_lead_class":
        lead.pop("class")
    else:
        raise AssertionError(f"unknown edit {edit}")


@pytest.mark.parametrize(
    "edit, expect",
    [
        ("empty_conditions_module", "conditionsModule.conditions"),
        ("string_conditions", "conditionsModule.conditions"),
        ("empty_conditions_list", "conditionsModule.conditions"),
        ("blank_condition_only", "conditionsModule.conditions"),
        ("blank_condition_entry", "conditionsModule.conditions"),
        ("non_string_condition_entry", "conditionsModule.conditions"),
        ("drop_lead_sponsor", "sponsorCollaboratorsModule.leadSponsor"),
        ("string_lead_sponsor", "sponsorCollaboratorsModule.leadSponsor"),
        ("drop_lead_name", "sponsorCollaboratorsModule.leadSponsor.name"),
        ("blank_lead_name", "sponsorCollaboratorsModule.leadSponsor.name"),
        ("drop_lead_class", "sponsorCollaboratorsModule.leadSponsor.class"),
    ],
)
def test_records_missing_condition_or_lead_sponsor_payloads_are_refused(
    tmp_path: Path, edit: str, expect: str
) -> None:
    # The module objects are kept, so the module check passes; built anyway, the trial would read as
    # ontology Other or an "Unknown" sponsor. The error names exactly the one missing payload.
    complete = fx.make_raw_ctgov_study("NCT99800011")
    shell = fx.make_raw_ctgov_study("NCT99800012")
    _strip_payload(shell, edit)
    want = (
        r"1 of 2 study records lack required protocol modules or fields "
        rf"\(first: NCT99800012 lacks {re.escape(expect)}\)"
    )
    rdir = tmp_path / "records"
    rdir.mkdir()
    (rdir / "a.json").write_text(json.dumps(complete), encoding="utf-8")
    (rdir / "b.json").write_text(json.dumps(shell), encoding="utf-8")
    container = tmp_path / "studies.json"
    container.write_text(json.dumps({"studies": [complete, shell]}), encoding="utf-8")
    plain = tmp_path / "studies.jsonl"
    plain.write_text(json.dumps(complete) + "\n" + json.dumps(shell) + "\n", encoding="utf-8")
    for path in (rdir, container, plain):
        with pytest.raises(ValueError, match=want):
            benchmarks.load_study_records(path)


_BASE = "resultsSection.baselineCharacteristicsModule"


def _measured_study(nct_id: str) -> dict:
    """The fixture study with locations, a collaborator and race and sex baseline measures."""
    return fx.make_raw_ctgov_study(
        nct_id,
        locations=[
            {"facility": "Fictional Site A", "city": "Boston", "state": "Massachusetts", "country": "United States"},
            {"facility": "Fictional Site B", "city": "Toronto", "country": "Canada"},
        ],
        collaborators=[{"name": "Fictional CRO", "class": "INDUSTRY"}],
        measures=[
            fx.make_measure("Race (NIH/OMB)", [("White", "60"), ("Asian", "40")]),
            fx.make_measure("Sex: Female, Male", [("Female", "55"), ("Male", "45")]),
        ],
    )


@pytest.mark.parametrize(
    "path, value, expect",
    [
        (("protocolSection", "statusModule", "startDateStruct"), "2023-04-15",
         "statusModule.startDateStruct (an object)"),
        (("protocolSection", "designModule", "enrollmentInfo"), 120, "designModule.enrollmentInfo (an object)"),
        # locations and collaborators: a skipped or coerced entry changed the listed facilities or sponsors
        (("protocolSection", "contactsLocationsModule"), "Boston", "contactsLocationsModule (an object)"),
        (("protocolSection", "contactsLocationsModule", "locations"), {"country": "United States"},
         "contactsLocationsModule.locations (a list)"),
        (("protocolSection", "contactsLocationsModule", "locations", 0), None,
         "contactsLocationsModule.locations[0] (an object)"),
        (("protocolSection", "contactsLocationsModule", "locations", 0, "country"), ["United States"],
         "contactsLocationsModule.locations[0].country (a string)"),
        (("protocolSection", "contactsLocationsModule", "locations", 0, "state"), 7,
         "contactsLocationsModule.locations[0].state (a string)"),
        (("protocolSection", "sponsorCollaboratorsModule", "collaborators"), ["Fictional CRO"],
         "sponsorCollaboratorsModule.collaborators[0] (an object)"),
        (("protocolSection", "sponsorCollaboratorsModule", "collaborators"), [{"name": ["Fictional CRO"]}],
         "sponsorCollaboratorsModule.collaborators[0].name (a string)"),
        # an explicit null is a present field of the wrong type, not an absent one
        (("protocolSection", "contactsLocationsModule", "locations", 0, "country"), None,
         "contactsLocationsModule.locations[0].country (a string)"),
        (("protocolSection", "contactsLocationsModule", "locations", 0, "state"), None,
         "contactsLocationsModule.locations[0].state (a string)"),
        (("protocolSection", "sponsorCollaboratorsModule", "collaborators"), [{"name": None, "class": "INDUSTRY"}],
         "sponsorCollaboratorsModule.collaborators[0].name (a string)"),
        # phases: str() turned these into strata of their own
        (("protocolSection", "designModule", "phases"), [7],
         "designModule.phases (a list of CT.gov phase values)"),
        (("protocolSection", "designModule", "phases"), [None],
         "designModule.phases (a list of CT.gov phase values)"),
        (("protocolSection", "designModule", "phases"), "PHASE3",
         "designModule.phases (a list of CT.gov phase values)"),
        # values outside CT.gov's phase list became strata of their own
        (("protocolSection", "designModule", "phases"), ["PHASE_3"],
         "designModule.phases (a list of CT.gov phase values)"),
        (("protocolSection", "designModule", "phases"), ["PHASE3", "BANANA"],
         "designModule.phases (a list of CT.gov phase values)"),
        (("resultsSection", "baselineCharacteristicsModule"), None, f"{_BASE} (an object)"),
        (("resultsSection", "baselineCharacteristicsModule", "denoms"), 0, f"{_BASE}.denoms (a list)"),
        (("resultsSection", "baselineCharacteristicsModule", "denoms", 0, "units"), None,
         f"{_BASE}.denoms[0].units (a string)"),
        (("resultsSection", "baselineCharacteristicsModule", "denoms", 0, "counts"), "100",
         f"{_BASE}.denoms[0].counts (a list)"),
        (("resultsSection", "baselineCharacteristicsModule", "groups", 0, "title"), 5,
         f"{_BASE}.groups[0].title (a string)"),
        (("resultsSection", "baselineCharacteristicsModule", "measures", 0), "Race",
         f"{_BASE}.measures[0] (an object)"),
        (("resultsSection", "baselineCharacteristicsModule", "measures", 0, "title"), None,
         f"{_BASE}.measures[0].title (a string)"),
        (("resultsSection", "baselineCharacteristicsModule", "measures", 1, "paramType"), ["COUNT"],
         f"{_BASE}.measures[1].paramType (a string)"),
        (("resultsSection", "baselineCharacteristicsModule", "measures", 0, "classes"), None,
         f"{_BASE}.measures[0].classes (a list)"),
        (("resultsSection", "baselineCharacteristicsModule", "measures", 0, "classes", 0, "categories", 1), [],
         f"{_BASE}.measures[0].classes[0].categories[1] (an object)"),
        (("resultsSection", "baselineCharacteristicsModule", "measures", 0, "classes", 0, "categories", 0, "title"), 7,
         f"{_BASE}.measures[0].classes[0].categories[0].title (a string)"),
        (("resultsSection", "baselineCharacteristicsModule", "measures", 0, "classes", 0, "categories", 0,
          "measurements"), True, f"{_BASE}.measures[0].classes[0].categories[0].measurements (a list)"),
    ],
)
def test_records_with_mistyped_parts_are_refused_before_the_extractors_raise(
    tmp_path: Path, path: tuple, value: object, expect: str
) -> None:
    # DAP or the shared race, ethnicity and sex extractors read each of these without a type check, so
    # each raised AttributeError or TypeError and one record stopped the whole build with a traceback.
    complete = _measured_study("NCT99800021")
    bad = _measured_study("NCT99800022")
    holder = bad
    for key in path[:-1]:
        holder = holder[key]
    holder[path[-1]] = value
    want = (
        r"1 of 2 study records lack required protocol modules or fields "
        rf"\(first: NCT99800022 lacks {re.escape(expect)}\)"
    )
    plain = tmp_path / "studies.jsonl"
    plain.write_text(json.dumps(complete) + "\n" + json.dumps(bad) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=want):
        benchmarks.load_study_records(plain)


@pytest.mark.parametrize(
    "nct",
    [123, "NCT123", "nct99800022", " NCT99800022", "NCT\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668"],
)
def test_records_with_a_malformed_nct_id_are_refused(tmp_path: Path, nct: object) -> None:
    # The trial's key in every published file; a number such as 123 was published as "123".
    bad = _measured_study("NCT99800022")
    bad["protocolSection"]["identificationModule"]["nctId"] = nct
    plain = tmp_path / "studies.jsonl"
    plain.write_text(json.dumps(_measured_study("NCT99800021")) + "\n" + json.dumps(bad) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"lacks identificationModule\.nctId \(NCT followed by eight digits\)"):
        benchmarks.load_study_records(plain)


@pytest.mark.parametrize("section", ["full", {}, None])
def test_record_with_results_but_has_results_false_is_refused(tmp_path: Path, section: object) -> None:
    # demographics read any dict-valued section, {} included, as posted results whatever the flag said.
    bad = _measured_study("NCT99800025")
    bad["hasResults"] = False
    if section != "full":
        bad["resultsSection"] = section
    plain = tmp_path / "studies.jsonl"
    plain.write_text(json.dumps(_measured_study("NCT99800026")) + "\n" + json.dumps(bad) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"first: NCT99800025 lacks resultsSection \(hasResults is false\)"):
        benchmarks.load_study_records(plain)
    # Control: hasResults false with no section loads.
    clean = fx.make_raw_ctgov_study("NCT99800027", has_results=False)
    clean.pop("resultsSection", None)
    plain.write_text(json.dumps(clean) + "\n", encoding="utf-8")
    assert len(benchmarks.load_study_records(plain)[0]) == 1


def test_records_with_well_typed_baseline_parts_load_and_build(tmp_path: Path) -> None:
    # Control: the measured fixture passes the type checks and builds.
    plain = tmp_path / "studies.jsonl"
    plain.write_text(
        json.dumps(_measured_study("NCT99800023")) + "\n" + json.dumps(_measured_study("NCT99800024")) + "\n",
        encoding="utf-8",
    )
    records, _ = benchmarks.load_study_records(plain)
    assert len(benchmarks.build_benchmarks(records, min_cell_trials=1)["trial_rows"]) == 2


def test_records_with_condition_and_lead_sponsor_payloads_are_accepted(tmp_path: Path) -> None:
    # Control for the refusals above: the same fixture, untouched, loads through every raw shape, with
    # a single condition, a lead sponsor of class OTHER and no collaborators.
    study = fx.make_raw_ctgov_study(
        "NCT99800013", conditions=["Asthma"], lead_sponsor_name="Fictional Academic Center",
        lead_sponsor_class="OTHER",
    )
    assert "collaborators" not in study["protocolSection"]["sponsorCollaboratorsModule"]
    rdir = tmp_path / "records"
    rdir.mkdir()
    (rdir / "a.json").write_text(json.dumps(study), encoding="utf-8")
    container = tmp_path / "studies.json"
    container.write_text(json.dumps({"studies": [study]}), encoding="utf-8")
    plain = tmp_path / "studies.jsonl"
    plain.write_text(json.dumps(study) + "\n", encoding="utf-8")
    for path in (rdir, container, plain):
        records, _meta = benchmarks.load_study_records(path)
        assert len(records) == 1
        core = benchmarks._extract_core_metadata(records[0])
        assert core["conditions"] == ["Asthma"]
        assert (core["lead_sponsor_name"], core["sponsor_class"]) == ("Fictional Academic Center", "OTHER")


@pytest.mark.parametrize(
    "module", ["identificationModule", "statusModule", "sponsorCollaboratorsModule", "conditionsModule", "designModule"]
)
@pytest.mark.parametrize("value", ["projected", ["x"], 7])
def test_non_object_required_module_is_refused_not_a_traceback(tmp_path: Path, module: str, value: object) -> None:
    complete = fx.make_raw_ctgov_study("NCT99800004")
    broken = fx.make_raw_ctgov_study("NCT99800005")
    broken["protocolSection"][module] = value
    rdir = tmp_path / "records"
    rdir.mkdir()
    (rdir / "a.json").write_text(json.dumps(complete), encoding="utf-8")
    (rdir / "b.json").write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(ValueError, match=f"lacks {module}"):
        benchmarks.load_study_records(rdir)


def test_core_metadata_reads_malformed_parts_as_empty() -> None:
    study = fx.make_raw_ctgov_study("NCT99800006")
    study["protocolSection"]["conditionsModule"]["conditions"] = "Asthma"  # a string, not a list
    study["protocolSection"]["sponsorCollaboratorsModule"]["collaborators"] = {"name": "X"}
    study["protocolSection"]["designModule"]["enrollmentInfo"] = "120"
    core = benchmarks._extract_core_metadata(study)
    assert core["conditions"] == [] and core["collaborators"] == [] and core["enrollment"] is None


def _build_weekly(
    tmp_path: Path,
    name: str,
    *,
    doc: dict[str, Any] | None = None,
    mesh_lines: list[dict[str, Any]] | None = None,
    snapshot_date: str | None = None,
) -> tuple[int, Path]:
    """Build from weekly full records on a later day than the pull (generated 2026-11-23)."""
    weekly_path = tmp_path / f"{name}.demographics.json.gz"
    weekly_doc = doc if doc is not None else fx.build_weekly_full_records_doc()
    weekly_path.write_bytes(gzip.compress(json.dumps(weekly_doc).encode("utf-8"), mtime=0))
    argv = ["--demographics", str(weekly_path),
            "--sponsor-rules", str(fx.write_fictional_sponsor_rules(tmp_path / f"{name}.aliases.csv")),
            "--generated-at", "2026-11-23T09:00:00Z", "--out", str(tmp_path / name)]
    if mesh_lines is not None:
        argv += ["--condition-mesh", str(_write_mesh_sidecar(tmp_path / f"{name}.mesh.jsonl.gz", mesh_lines))]
    if snapshot_date is not None:
        argv += ["--snapshot-date", snapshot_date]
    return cli_build.main(argv), tmp_path / name


def test_weekly_snapshot_date_comes_from_the_pull_not_the_build_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # On e285f9d each of these builds recorded parameters.snapshot_date 2026-11-23, the build date, although
    # extract_sex_gender reused rows parsed on 2026-11-21; with a sidecar the manifest gave two dates.
    cohort = [r["nct_id"] for r in fx.build_weekly_full_records_doc()["data"]]

    def snapshot_of(out: Path) -> str:
        return json.loads((out / "run.json").read_text(encoding="utf-8"))["parameters"]["snapshot_date"]

    rc, out = _build_weekly(tmp_path, "sidecar", mesh_lines=_weekly_mesh_lines(cohort))
    assert rc == 0 and snapshot_of(out) == "2026-11-21"
    assert artifacts.load_dir(out)["run.json"]["inputs"]["condition_mesh"]["snapshot_date"] == "2026-11-21"
    assert cli_check.main([str(out)]) == 0

    stamped = fx.build_weekly_full_records_doc()
    for rec in stamped["data"]:
        rec["sex_gender"]["snapshot_date"] = "2026-11-21"
    rc, out = _build_weekly(tmp_path, "rows", doc=stamped)
    assert rc == 0 and snapshot_of(out) == "2026-11-21"

    rc, out = _build_weekly(tmp_path, "explicit", snapshot_date="2026-11-21")
    assert rc == 0 and snapshot_of(out) == "2026-11-21"
    rc, out = _build_weekly(tmp_path, "explicit_matching", mesh_lines=_weekly_mesh_lines(cohort),
                            snapshot_date="2026-11-21")
    assert rc == 0 and snapshot_of(out) == "2026-11-21"
    capsys.readouterr()

    # Refused, nothing written: no stamp anywhere, or stamps that disagree.
    disagreeing_rows = copy.deepcopy(stamped)
    disagreeing_rows["data"][0]["sex_gender"]["snapshot_date"] = "2026-11-14"
    refused = {
        "none": ({}, "weekly full records carry no snapshot date"),
        "explicit_vs_sidecar": ({"mesh_lines": _weekly_mesh_lines(cohort), "snapshot_date": "2026-11-23"},
                                "snapshot dates disagree"),
        "explicit_vs_rows": ({"doc": stamped, "snapshot_date": "2026-11-23"}, "snapshot dates disagree"),
        "rows_disagree": ({"doc": disagreeing_rows}, "2 different snapshot_date values"),
        "rows_vs_sidecar": ({"doc": {**stamped, "data": [dict(r, sex_gender=dict(r["sex_gender"],
                                                                              snapshot_date="2026-11-14"))
                                                          for r in stamped["data"]]},
                             "mesh_lines": _weekly_mesh_lines(cohort)}, "snapshot dates disagree"),
        "bad_row_stamp": ({"doc": {**stamped, "data": [dict(r, sex_gender=dict(r["sex_gender"],
                                                                            snapshot_date="2026-02-30"))
                                                        for r in stamped["data"]]}},
                          "sex_gender.snapshot_date must be a real calendar date"),
    }
    for name, (kwargs, message) in refused.items():
        rc, out = _build_weekly(tmp_path, name, **kwargs)
        err = capsys.readouterr().err
        assert rc == 1 and not out.exists(), name
        assert message in err, (name, err)


def test_contract_refuses_a_weekly_manifest_whose_snapshot_date_contradicts_its_sidecar(tmp_path: Path) -> None:
    cohort = [r["nct_id"] for r in fx.build_weekly_full_records_doc()["data"]]
    rc, out = _build_weekly(tmp_path, "weekly", mesh_lines=_weekly_mesh_lines(cohort))
    assert rc == 0
    built = artifacts.load_dir(out)
    assert artifacts.check_contract(built) == []
    tampered = copy.deepcopy(built)
    tampered["run.json"]["parameters"]["snapshot_date"] = "2026-11-23"
    assert any("differs from inputs.condition_mesh.snapshot_date '2026-11-21'" in x
               for x in artifacts.check_contract(tampered))
    # Raw CT.gov input: the sidecar's stamps are recorded, not matched, so no such problem is reported.
    raw = copy.deepcopy(tampered)
    raw["run.json"]["inputs"]["study_records"]["input_format"] = "records_directory"
    assert not any("differs from inputs.condition_mesh" in x for x in artifacts.check_contract(raw))


def test_raw_records_snapshot_date_still_defaults_to_the_build_date(tmp_path: Path) -> None:
    studies_dir = fx.write_studies_dir(fx.build_fictional_cohort(), tmp_path / "studies")
    rules_path = fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")
    assert cli_build.main(["--records-dir", str(studies_dir), "--sponsor-rules", str(rules_path),
                           "--generated-at", "2026-11-23T09:00:00Z", "--out", str(tmp_path / "raw")]) == 0
    run = json.loads((tmp_path / "raw" / "run.json").read_text(encoding="utf-8"))
    assert run["parameters"]["snapshot_date"] == "2026-11-23"


@pytest.mark.parametrize(
    ("column", "value", "problem"),
    [
        ("n_female", "50", "sex_gender.n_female is str, not int or float or NoneType"),
        ("n_male", True, "sex_gender.n_male is bool, not int or float or NoneType"),
        ("reported_sex", "yes", "sex_gender.reported_sex is str, not bool"),
        ("is_participant_count", "yes", "sex_gender.is_participant_count is str, not bool or NoneType"),
        ("reported_gender", None, "sex_gender.reported_gender is NoneType, not bool"),  # only parse_error may
        ("parser_rules_version", None, "sex_gender.parser_rules_version is NoneType, not str"),
        ("uninformative_reason", 7, "sex_gender.uninformative_reason is int, not str or NoneType"),
        # JSON `Infinity`, `NaN` or `1e309` parse as non-finite floats, which passed the type check.
        ("n_female", float("inf"), "sex_gender.n_female is inf, not a finite number"),
        ("enrollment_minus_parsed", float("nan"), "sex_gender.enrollment_minus_parsed is nan, not a finite number"),
        ("n_male", -1.0, "sex_gender.n_male is -1.0, a negative count"),
    ],
)
def test_weekly_sex_gender_row_needs_every_lean_column_with_its_type(
    tmp_path: Path, column: str, value: object, problem: str
) -> None:
    weekly_doc = fx.build_weekly_full_records_doc()
    weekly_doc["data"][1]["sex_gender"][column] = value
    weekly_path = tmp_path / "demographics.json.gz"
    weekly_path.write_bytes(gzip.compress(json.dumps(weekly_doc).encode("utf-8"), mtime=0))
    message = f"(first: NCT99000402 {problem})"
    with pytest.raises(ValueError, match=re.escape(message)):
        benchmarks.load_study_records(weekly_path, source_kind="full_records")
    weekly_doc["data"][1]["sex_gender"].pop(column)
    weekly_path.write_bytes(gzip.compress(json.dumps(weekly_doc).encode("utf-8"), mtime=0))
    with pytest.raises(ValueError, match=re.escape(f"sex_gender has no {column} (sgt.lean_row always writes it)")):
        benchmarks.load_study_records(weekly_path, source_kind="full_records")


@pytest.mark.parametrize("make_row", ["parse_error", "not_reported"])
def test_weekly_sex_gender_rows_the_extractor_writes_for_every_status_load(tmp_path: Path, make_row: str) -> None:
    # On 99f072f a `parse_error` row (`sgt.parse_error_row`: outcome flags None) and a `not_reported` row
    # (`is_participant_count` None) were refused, although src/extract_all.py writes both.
    from src import sex_gender_table as sgt

    if make_row == "parse_error":
        row = sgt.lean_row(sgt.parse_error_row("NCT99000402", 100, "2026-11-21"))
    else:
        row = sgt.lean_row(sgt.build_row({"protocolSection": {"identificationModule": {"nctId": "NCT99000402"}}},
                                         "2026-11-21"))
    assert row["sex_report_status"] == make_row
    weekly_doc = fx.build_weekly_full_records_doc()
    weekly_doc["data"][1]["sex_gender"] = row
    weekly_path = tmp_path / "demographics.json.gz"
    weekly_path.write_bytes(gzip.compress(json.dumps(weekly_doc).encode("utf-8"), mtime=0))
    records, _ = benchmarks.load_study_records(weekly_path, source_kind="full_records")
    assert len(records) == 5


_DROP = object()  # remove the key instead of setting it


def _write_weekly(weekly_doc: dict[str, Any], tmp_path: Path) -> Path:
    weekly_path = tmp_path / "demographics.json.gz"
    weekly_path.write_bytes(gzip.compress(json.dumps(weekly_doc).encode("utf-8"), mtime=0))
    return weekly_path


@pytest.mark.parametrize(
    ("path", "value", "problem"),
    [
        # On 28a5001 each loaded, and `dap/demographics.py` read the child as absent or zero: a reported
        # table published as absent, its counts lowered, or the double-count check skipped.
        (("race", "omb_totals"), "corrupt", "race.omb_totals is str, not an object"),
        (("race", "omb_totals", "american_indian_alaska_native"), _DROP,
         "race.omb_totals has no american_indian_alaska_native (the extractor writes every category key)"),
        (("race", "omb_totals", "white"), "60", "race.omb_totals.white is '60', not a non-negative integer"),
        (("race", "omb_totals", "white"), True, "race.omb_totals.white is True, not a non-negative integer"),
        (("race", "omb_totals", "white"), 60.5, "race.omb_totals.white is 60.5, not a non-negative integer"),
        (("race", "reported"), "yes", "race.reported is str, not bool"),
        (("race", "reported"), _DROP, "race has no reported (the extractor always writes it)"),
        (("race", "unknown_inferred"), None, "race.unknown_inferred is None, not a non-negative integer"),
        (("race", "flags"), "denominator_balanced", "race.flags is str, not a list"),
        (("race", "flags"), [None], "race.flags[0] is NoneType, not a string"),
        (("race", "raw_categories", 0, "count"), _DROP,
         "race.raw_categories[0].count is missing or not a non-negative integer"),
        (("race", "raw_categories", 0, "flags"), None,
         "race.raw_categories[0].flags is missing or not a list of strings"),
        (("race", "quarantined_labels"), [["Other", 3]], "race.quarantined_labels[0] is list, not an object"),
        (("race", "quarantined_labels"), [{"original": "Other", "count": -2}],
         "race.quarantined_labels[0].count is missing or not a non-negative integer"),
        (("race", "skipped_ethnicity_rows"), None, "race.skipped_ethnicity_rows is NoneType, not a list"),
        (("race", "unknown_ethnicity_only"), "3", "race.unknown_ethnicity_only is '3', not a non-negative integer"),
        (("ethnicity", "omb_totals"), {},
         "ethnicity.omb_totals has no hispanic_latino (the extractor writes every category key)"),
        (("ethnicity", "raw_categories", 1, "omb_category"), None,
         "ethnicity.raw_categories[1].omb_category is missing or not a string"),
        (("ethnicity", "not_hispanic_from_race_rows"), -1,
         "ethnicity.not_hispanic_from_race_rows is -1, not a non-negative integer"),
        # `_sex_participant_total` falls back to the legacy sex totals for the race double-count check.
        (("sex", "totals"), "corrupt", "sex.totals is str, not an object"),
        (("sex", "totals", "female"), 50.5, "sex.totals.female is 50.5, not a non-negative integer"),
        (("sex", "reported"), None, "sex.reported is NoneType, not bool"),
        # `is_results_posted` reads every block's `reported` by truthiness: "false" read as posted results.
        (("sex", "reported"), "false", "sex.reported is str, not bool"),
        (("gender", "reported"), "false", "gender.reported is str, not bool"),
    ],
)
def test_weekly_race_and_ethnicity_children_need_the_extractors_kind(
    tmp_path: Path, path: tuple[Any, ...], value: object, problem: str
) -> None:
    weekly_doc = fx.build_weekly_full_records_doc()
    target = weekly_doc["data"][1]
    for step in path[:-1]:
        target = target[step]
    if value is _DROP:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    message = f"1 of 5 weekly records carry a missing or malformed field (first: NCT99000402 {problem})"
    with pytest.raises(ValueError, match=re.escape(message)):
        benchmarks.load_study_records(_write_weekly(weekly_doc, tmp_path), source_kind="full_records")


def test_weekly_race_and_ethnicity_blocks_of_both_extractor_versions_load(tmp_path: Path) -> None:
    # The 2026-11-08 extraction predates `unknown_ethnicity_only`, `skipped_ethnicity_rows` and
    # `not_hispanic_from_race_rows`, so its blocks lack them; the current extractors write them.
    from src.ethnicity_extractor import extract_ethnicity_data
    from src.race_extractor import extract_race_data

    legacy = fx.build_weekly_full_records_doc()
    for rec in legacy["data"]:
        rec["race"].pop("skipped_ethnicity_rows")
        assert "unknown_ethnicity_only" not in rec["race"] and "not_hispanic_from_race_rows" not in rec["ethnicity"]
    assert len(benchmarks.load_study_records(_write_weekly(legacy, tmp_path), source_kind="full_records")[0]) == 5

    current = fx.build_weekly_full_records_doc()
    # NCT99000106 to NCT99000111: reported tables, one combined race/ethnicity table with flags, and
    # three with no race or ethnicity table.
    posted = [st for st in fx.build_fictional_cohort() if st.get("resultsSection")][-5:]
    for rec, study in zip(current["data"], posted, strict=True):
        rec["race"], rec["ethnicity"] = extract_race_data(study), extract_ethnicity_data(study)
    assert all("skipped_ethnicity_rows" in rec["race"] for rec in current["data"])
    assert any(rec["race"]["reported"] and rec["race"]["flags"] for rec in current["data"])
    assert any(not rec["race"]["reported"] for rec in current["data"])
    assert len(benchmarks.load_study_records(_write_weekly(current, tmp_path), source_kind="full_records")[0]) == 5


@pytest.mark.parametrize(
    ("make_row", "column", "value", "derived"),
    [
        # On 28a5001 each loaded and `extract_sex_gender` reused the row as it stood: a trial counted as
        # both not reported and reporting sex, as reporting both without reporting gender, and so on.
        ("not_reported", "reported_sex", True, False),
        ("not_reported", "reported_both", True, False),
        ("parse_error", "reported_gender", False, None),
        ("reported", "reported_sex", False, True),  # the fixture row has 50 female and 50 male
        ("reported", "reported_gender", True, False),  # and no gender-diverse or ambiguous count
        ("reported", "reported_both", True, False),
    ],
)
def test_weekly_sex_gender_outcome_flags_must_be_the_derived_ones(
    tmp_path: Path, make_row: str, column: str, value: bool, derived: bool | None
) -> None:
    from src import sex_gender_table as sgt

    weekly_doc = fx.build_weekly_full_records_doc()
    if make_row == "parse_error":
        weekly_doc["data"][1]["sex_gender"] = sgt.lean_row(sgt.parse_error_row("NCT99000402", 100, "2026-11-21"))
    elif make_row == "not_reported":
        weekly_doc["data"][1]["sex_gender"] = sgt.lean_row(
            sgt.build_row({"protocolSection": {"identificationModule": {"nctId": "NCT99000402"}}}, "2026-11-21"))
    row = weekly_doc["data"][1]["sex_gender"]
    assert row["sex_report_status"] == make_row and row[column] is derived
    row[column] = value
    problem = (f"sex_gender.{column} is {value!r}, but sgp.derive_outcomes gives {derived!r} "
               f"for status {make_row!r} and the row's counts")
    with pytest.raises(ValueError, match=re.escape(f"(first: NCT99000402 {problem})")):
        benchmarks.load_study_records(_write_weekly(weekly_doc, tmp_path), source_kind="full_records")


def test_weekly_sex_gender_row_reporting_gender_loads(tmp_path: Path) -> None:
    weekly_doc = fx.build_weekly_full_records_doc()
    weekly_doc["data"][1]["sex_gender"].update(n_gender_diverse=2.0, reported_gender=True, reported_both=True)
    assert len(benchmarks.load_study_records(_write_weekly(weekly_doc, tmp_path), source_kind="full_records")[0]) == 5


def test_weekly_sex_gender_count_written_as_1e309_is_refused(tmp_path: Path) -> None:
    # On d97d844 it loaded, and `extract_sex_gender` raised `OverflowError` on the infinite count.
    weekly_doc = fx.build_weekly_full_records_doc()
    weekly_doc["data"][1]["sex_gender"]["n_female"] = 12345.5
    text = json.dumps(weekly_doc).replace("12345.5", "1e309")
    weekly_path = tmp_path / "demographics.json.gz"
    weekly_path.write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))
    problem = "sex_gender.n_female is inf, not a finite number"
    with pytest.raises(ValueError, match=re.escape(f"(first: NCT99000402 {problem})")):
        benchmarks.load_study_records(weekly_path, source_kind="full_records")


def test_weekly_negative_enrollment_gap_loads(tmp_path: Path) -> None:
    # `enrollment_minus_parsed` is negative in 896 of the 80,890 weekly rows: more parsed than enrolled.
    weekly_doc = fx.build_weekly_full_records_doc()
    weekly_doc["data"][1]["sex_gender"]["enrollment_minus_parsed"] = -5.0
    assert len(benchmarks.load_study_records(_write_weekly(weekly_doc, tmp_path), source_kind="full_records")[0]) == 5


@pytest.mark.parametrize(
    ("block", "totals_key", "reported", "total"),
    [
        # On 96f721b each loaded, and DAP dropped the block's positive counts as not reported.
        ("race", "omb_totals", False, 100),
        ("ethnicity", "omb_totals", False, 100),
        ("sex", "totals", False, 100),
        # A reported block whose totals are all zero is one the extractor's all-zero rejection clears.
        ("race", "omb_totals", True, 0),
    ],
)
def test_weekly_block_reported_flag_must_match_its_totals(
    tmp_path: Path, block: str, totals_key: str, reported: bool, total: int
) -> None:
    weekly_doc = fx.build_weekly_full_records_doc()
    payload = weekly_doc["data"][1][block]
    payload["reported"] = reported
    if total == 0:
        payload[totals_key] = {k: 0 for k in payload[totals_key]}
    assert sum(payload[totals_key].values()) == total
    problem = (f"{block}.reported is {reported} but its {totals_key} sum to {total} "
               "(the extractor reports a block exactly when its counts are not all zero)")
    with pytest.raises(ValueError, match=re.escape(f"(first: NCT99000402 {problem})")):
        benchmarks.load_study_records(_write_weekly(weekly_doc, tmp_path), source_kind="full_records")


@pytest.mark.parametrize("value", ["corrupt", " ", "2026-02-30", "2024-10-01T00:00"])
def test_weekly_results_date_must_be_blank_or_a_date(tmp_path: Path, value: str) -> None:
    # On d97d844 each loaded, and `is_results_posted` read the non-empty value as posted results.
    weekly_doc = fx.build_weekly_full_records_doc()
    weekly_doc["data"][1]["results_date"] = value
    problem = f"results_date {value!r} is neither \"\" nor a date (any other value reads as posted results)"
    with pytest.raises(ValueError, match=re.escape(f"(first: NCT99000402 {problem})")):
        benchmarks.load_study_records(_write_weekly(weekly_doc, tmp_path), source_kind="full_records")


@pytest.mark.parametrize("value", ["", "2024-10", "2024-10-01"])
def test_weekly_results_date_blank_or_a_date_loads(tmp_path: Path, value: str) -> None:
    weekly_doc = fx.build_weekly_full_records_doc()
    weekly_doc["data"][1]["results_date"] = value
    assert len(benchmarks.load_study_records(_write_weekly(weekly_doc, tmp_path), source_kind="full_records")[0]) == 5
