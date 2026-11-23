"""End-to-end tests for `feasibility/benchmarks.py`, `feasibility/artifacts.py`, and CLI scripts."""
from __future__ import annotations

import ast
import csv
import gzip
import json
import re
from pathlib import Path
from typing import Any

import pytest

from feasibility import artifacts, benchmarks
from feasibility.metrics import extract_trial_metrics
from scripts import (
    build_feasibility_benchmarks,
    check_feasibility_contract,
    export_feasibility_site,
)
from tests.feasibility.conftest import make_ctgov_study, make_weekly_record

ROOT = Path(__file__).resolve().parents[2]


def _sample_cohort() -> list[dict[str, Any]]:
    # "Wake Forest University Health Sciences" matches the real status=exclude rule in company_aliases.csv.
    excluded_lit = "Wake Forest University Health Sciences"
    studies: list[dict[str, Any]] = []
    # 5 completed Pfizer Phase 3 diabetes trials (>= min_cell_trials=5 so cell percentiles are shown).
    for i in range(1, 6):
        studies.append(
            make_ctgov_study(
                f"NCT9920000{i}",
                start_date="2020-01-10" if i % 2 == 1 else "2020-01",
                primary_completion_date=f"2021-0{i}-15" if i % 2 == 1 else f"2021-0{i}",
                enrollment_count=100 + 20 * i,
                enrollment_type="ACTUAL",
                n_sites=5 + i,
                lead_sponsor_name="Pfizer" if i < 5 else "Bristol-Meyers Squibb & Pfizer",
                lead_sponsor_class="INDUSTRY",
                conditions=["Type 2 Diabetes Mellitus"],
                meshes=[{"id": "D003924", "term": "Diabetes Mellitus, Type 2"}],
            )
        )
    # 1 terminated multi-signal trial with empty results shell and reviewed-excluded sponsor literal.
    studies.append(
        make_ctgov_study(
            "NCT99200006",
            overall_status="TERMINATED",
            why_stopped="Stopped early due to slow enrollment and serious adverse events.",
            start_date="2020-02",
            primary_completion_date="2020-11",
            enrollment_count=12,
            enrollment_type="ACTUAL",
            n_sites=1,
            lead_sponsor_name=excluded_lit,
            lead_sponsor_class="OTHER",
            conditions=["Breast Cancer"],
            meshes=[{"id": "D001943", "term": "Breast Neoplasms"}],
            outcome_measures=[
                {
                    "type": "PRIMARY",
                    "title": "Objective Response Rate",
                    "groups": [{"id": "OG000", "title": "Arm A"}],
                    "denoms": [{"units": "Participants", "counts": [{"groupId": "OG000", "value": "0"}]}],
                }
            ],
        )
    )
    # 1 withdrawn trial with ESTIMATED target enrollment and unreviewed fictional sponsor literal.
    studies.append(
        make_ctgov_study(
            "NCT99200007",
            overall_status="WITHDRAWN",
            why_stopped="Business prioritization; there were no safety concerns.",
            start_date="2025-06",
            start_type="ESTIMATED",
            primary_completion_date="2027-06",
            primary_completion_type="ESTIMATED",
            enrollment_count=400,
            enrollment_type="ESTIMATED",
            n_sites=0,
            lead_sponsor_name="Fictional Unreviewed Biotech ZZZ",
            lead_sponsor_class="INDUSTRY",
            conditions=["Heart Failure"],
            has_results=False,
        )
    )
    # 1 dual-role study (Pfizer lead + subsidiary of Pfizer collaborator) to verify nct_id deduplication.
    studies.append(
        make_ctgov_study(
            "NCT99200008",
            overall_status="COMPLETED",
            lead_sponsor_name="Pfizer",
            collaborators=[{"name": "Wyeth is now a wholly owned subsidiary of Pfizer", "class": "INDUSTRY"}],
            n_sites=55,
            conditions=["Type 2 Diabetes Mellitus"],
            meshes=[{"id": "D003924", "term": "Diabetes Mellitus, Type 2"}],
        )
    )
    return studies


def test_sponsor_three_states_deduplication_and_small_cell_withholding() -> None:
    cohort = _sample_cohort()
    trials, sponsor_meta, review_meta = benchmarks.build_trial_records(cohort, reviews={})
    assert review_meta["n_applied"] == 0

    # All three review states are present and strictly disjoint!
    state_counts = sponsor_meta["lead_review_state_counts"]
    assert state_counts == {
        "attributed": 6,
        "reviewed_excluded": 1,
        "unreviewed": 1,
    }

    # NCT99200008 has Pfizer as lead AND Wyeth (current_owner Pfizer) as collaborator:
    # Pfizer's any-role trial count must be 6 (deduplicated by nct_id), NOT 7!
    strata_obj, sponsors_obj = benchmarks.build_strata_and_sponsors(
        trials,
        sponsor_meta=sponsor_meta,
        min_cell_trials=5,
    )
    pfizer_any = [
        c for c in sponsors_obj["by_canonical_company_any_role"] if c["canonical_company"] == "Pfizer"
    ]
    assert len(pfizer_any) == 1
    assert pfizer_any[0]["n_trials"] == 6
    assert pfizer_any[0]["realized_primary_duration_months"]["withheld"] is False
    assert pfizer_any[0]["realized_primary_duration_months"]["p50"] is not None

    # Oncology cell has n_trials == 1 (< min_cell_trials=5): exact n_trials reported, percentiles withheld.
    onc = [c for c in strata_obj["by_indication"] if c["ontology_primary"] == "Oncology"][0]
    assert onc["n_trials"] == 1
    assert onc["realized_primary_duration_months"]["withheld"] is True
    assert onc["realized_primary_duration_months"]["p50"] is None


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        # Short keywords (<= 4 chars) inside a longer word no longer hit ('all', 'als', 'ra', 'burn').
        ("Falls", ("Other", "Uncategorized")),
        ("Trauma", ("Other", "Uncategorized")),
        ("Caregiver Burnout", ("Other", "Uncategorized")),
        # Short keywords standing alone, or with a plural suffix, still match.
        ("ALL", ("Oncology", "Hematologic Malignancy")),
        ("Rheumatoid Arthritis (RA)", ("Autoimmune and Inflammatory", "Rheumatoid Arthritis")),
        ("TIAs", ("Neurology", "Stroke and Cerebrovascular")),
        ("Severe Burns", ("Dermatology", "Wound and Ulcer")),
        # Longer keywords keep the substring test, so compounds and plurals still classify.
        ("Neoplasms", ("Oncology", "Other Oncology")),
        ("Gliosarcoma", ("Oncology", "Sarcoma")),
        ("Metastatic Breast Cancers", ("Oncology", "Breast Cancer")),
    ],
)
def test_condition_fallback_short_keywords_stand_alone(
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
    expected: tuple[str, str],
) -> None:
    # Exercise the stdlib fallback used when rapidfuzz (src.condition_classifier) is absent.
    monkeypatch.setattr(benchmarks, "_HAS_RAPIDFUZZ", False)
    monkeypatch.setattr(benchmarks, "_ONTOLOGY_CACHE", None)
    assert benchmarks._classify_conditions_FallbackOrFull([condition]) == expected


def test_build_contract_tamper_refusals_and_site_export(tmp_path: Path) -> None:
    records_dir = tmp_path / "records"
    records_dir.mkdir()
    for study in _sample_cohort():
        nct = study["protocolSection"]["identificationModule"]["nctId"]
        raw = gzip.compress(json.dumps(study).encode("utf-8"), mtime=0)
        (records_dir / f"{nct}.json.gz").write_bytes(raw)

    out_a = tmp_path / "build_a"
    out_b = tmp_path / "build_b"
    rc_a = build_feasibility_benchmarks.main(
        [
            "--records-dir",
            str(records_dir),
            "--generated-at",
            "2026-11-21T12:00:00+00:00",
            "--out",
            str(out_a),
        ]
    )
    rc_b = build_feasibility_benchmarks.main(
        [
            "--records-dir",
            str(records_dir),
            "--generated-at",
            "2026-11-21T18:30:00+00:00",
            "--out",
            str(out_b),
        ]
    )
    assert rc_a == 0
    assert rc_b == 0

    # Same inputs & date with different wall-clock timestamps produce identical run_id/run_identity
    # and byte-identical sibling artifacts!
    run_a = json.loads((out_a / "run.json").read_text(encoding="utf-8"))
    run_b = json.loads((out_b / "run.json").read_text(encoding="utf-8"))
    assert run_a["run_id"] == run_b["run_id"]
    for sibling in artifacts.FILES:
        assert (out_a / sibling).read_bytes() == (out_b / sibling).read_bytes()

    # Changing a parameter (--min-cell-trials) MUST change run_identity!
    out_c = tmp_path / "build_c"
    assert (
        build_feasibility_benchmarks.main(
            [
                "--records-dir",
                str(records_dir),
                "--min-cell-trials",
                "3",
                "--generated-at",
                "2026-11-21T12:00:00+00:00",
                "--out",
                str(out_c),
            ]
        )
        == 0
    )
    run_c = json.loads((out_c / "run.json").read_text(encoding="utf-8"))
    assert run_c["run_id"] != run_a["run_id"]

    # Valid build passes check_feasibility_contract.py.
    assert check_feasibility_contract.main([str(out_a)]) == 0

    # Every count in summary.json carries value, numerator, denominator, definition.
    summary = json.loads((out_a / "summary.json").read_text(encoding="utf-8"))
    for m in summary["metrics"]:
        assert {"value", "numerator", "denominator", "definition"} <= set(m)
    for tbl in summary["tables"].values():
        for row in tbl["rows"]:
            assert {"value", "numerator", "denominator", "definition"} <= set(row)

    # Site export succeeds on valid build, stages atomically, and allows clean re-export over itself.
    site_out = tmp_path / "site_feasibility"
    assert export_feasibility_site.main([str(out_a), str(site_out)]) == 0
    assert export_feasibility_site.main([str(out_b), str(site_out)]) == 0
    site_run = json.loads((site_out / "run.json").read_text(encoding="utf-8"))
    assert site_run["build_run_id"] == run_a["run_id"]
    for name, expected_sha in site_run["files"].items():
        assert artifacts.sha256_bytes((site_out / name).read_bytes()) == expected_sha

    # Site export refuses to overwrite an unrelated non-empty directory!
    unrelated = tmp_path / "unrelated_dir"
    unrelated.mkdir()
    (unrelated / "important.json").write_text("{}", encoding="utf-8")
    assert export_feasibility_site.main([str(out_a), str(unrelated)]) == 1
    assert (unrelated / "important.json").exists()

    # Tampering with summary.json causes BOTH check_feasibility_contract.py AND export_feasibility_site.py to fail!
    tampered = tmp_path / "tampered"
    export_rc_before = check_feasibility_contract.main([str(out_b)])
    assert export_rc_before == 0
    tampered.mkdir()
    for fp in out_b.iterdir():
        (tampered / fp.name).write_bytes(fp.read_bytes())
    bad_sum = json.loads((tampered / "summary.json").read_text(encoding="utf-8"))
    bad_sum["metrics"][0]["numerator"] += 1
    (tampered / "summary.json").write_bytes(artifacts.dump_canonical(bad_sum))

    assert check_feasibility_contract.main([str(tampered)]) == 1
    assert export_feasibility_site.main([str(tampered), str(tmp_path / "site_bad")]) == 1
    assert not (tmp_path / "site_bad").exists()


def test_refuses_site_projected_demographics_parts(tmp_path: Path) -> None:
    fake_part = tmp_path / "demographics.part001.json.gz"
    fake_part.write_bytes(gzip.compress(b'{"data": []}', mtime=0))
    rc = build_feasibility_benchmarks.main(
        ["--demographics", str(fake_part), "--out", str(tmp_path / "out")]
    )
    assert rc == 1
    assert not (tmp_path / "out").exists()


def test_unicode_line_separators_in_jsonl_roundtrip(tmp_path: Path) -> None:
    """Regression test: U+2028, U+2029, \\x85, \\x0b, and \\x0c inside ensure_ascii=False JSONL
    strings must never be split mid-string by archive/sidecar/trials.jsonl.gz readers.
    """
    tricky_title = "Phase 3 Trial\u2028Line Two\u2029Paragraph Two\x85Next\x0bVT\x0cFF"
    tricky_why = "Slow accrual across sites\u2028and COVID-19 lockdown\u2029disruption."
    study = make_ctgov_study(
        "NCT99500001",
        brief_title=tricky_title,
        overall_status="TERMINATED",
        why_stopped=tricky_why,
    )
    archive_path = tmp_path / "unicode_studies.jsonl.gz"
    archive_raw = (json.dumps(study, ensure_ascii=False) + "\n").encode("utf-8")
    archive_path.write_bytes(gzip.compress(archive_raw, mtime=0))

    mesh_path = tmp_path / "unicode_mesh.jsonl.gz"
    mesh_line = {
        "schema_version": "1.0",
        "nct_id": "NCT99500001",
        "state": "indexed",
        "meshes": [{"id": "D003924", "term": "Diabetes\u2028Mellitus\u2029Type 2"}],
        "ancestors": [],
    }
    mesh_raw = (json.dumps(mesh_line, ensure_ascii=False) + "\n").encode("utf-8")
    mesh_path.write_bytes(gzip.compress(mesh_raw, mtime=0))

    out_dir = tmp_path / "unicode_build"
    site_dir = tmp_path / "unicode_site"
    assert (
        build_feasibility_benchmarks.main(
            [
                "--archive",
                str(archive_path),
                "--condition-mesh",
                str(mesh_path),
                "--generated-at",
                "2026-11-22T12:00:00+00:00",
                "--out",
                str(out_dir),
            ]
        )
        == 0
    )
    assert check_feasibility_contract.main([str(out_dir)]) == 0
    assert export_feasibility_site.main([str(out_dir), str(site_dir)]) == 0

    decoded_trials = artifacts.decode_trials_jsonl_gz((out_dir / "trials.jsonl.gz").read_bytes())
    assert len(decoded_trials) == 1
    assert decoded_trials[0]["brief_title"] == tricky_title
    assert decoded_trials[0]["termination"]["why_stopped_verbatim"] == tricky_why
    assert decoded_trials[0]["indication"]["condition_mesh"]["meshes"][0]["term"] == (
        "Diabetes\u2028Mellitus\u2029Type 2"
    )


def test_sponsor_rule_conflicts_and_empty_notes_fail_loudly(tmp_path: Path) -> None:
    studies = [make_ctgov_study("NCT99400001", lead_sponsor_name="Contested Pharma")]

    conflict_rules = tmp_path / "conflict_aliases.csv"
    conflict_rules.write_text(
        "canonical,rule_type,pattern,status,shared,note\n"
        "Alpha Corp,exact_literal,Contested Pharma,attribute,no,Decided by reviewer A\n"
        "Beta Corp,exact_literal,Contested Pharma,attribute,no,Decided by reviewer B\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="conflicting rules for literal"):
        benchmarks.build_trial_records(studies, reviews={}, rules_path=conflict_rules)

    attr_vs_excl = tmp_path / "attr_vs_excl.csv"
    attr_vs_excl.write_text(
        "canonical,rule_type,pattern,status,shared,note\n"
        "Alpha Corp,exact_literal,Contested Pharma,attribute,no,Attributed rule\n"
        "Alpha Corp,exact_literal,Contested Pharma,exclude,no,Excluded rule\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="attributed.*excluded"):
        benchmarks.build_trial_records(studies, reviews={}, rules_path=attr_vs_excl)

    empty_note = tmp_path / "empty_note.csv"
    empty_note.write_text(
        "canonical,rule_type,pattern,status,shared,note\n"
        "Alpha Corp,exact_literal,Contested Pharma,attribute,no,   \n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="empty note"):
        benchmarks.build_trial_records(studies, reviews={}, rules_path=empty_note)


def test_contract_checker_catches_domain_invariant_violations() -> None:
    cohort = _sample_cohort()
    trials, sponsor_meta, review_meta = benchmarks.build_trial_records(cohort, reviews={})
    inputs_meta = {
        "records": {"mode": "fixture", "source": "mem", "n_records": len(trials), "sha256": "a" * 64},
        "sponsor_rules": {"file": "company_aliases.csv", "sha256": sponsor_meta["rules_sha256"]},
        "termination_reviews": {"file": "termination_reviews.csv", "n_rows": 0, "sha256": "b" * 64},
        "condition_mesh": None,
    }
    producer_meta = {
        "repository": "civicsample-engine",
        "commit": None,
        "uncommitted_changes": None,
        "source_sha256": "c" * 64,
    }
    base_family, base_raw = artifacts.build_artifact_family(
        trials=trials,
        sponsor_meta=sponsor_meta,
        review_meta=review_meta,
        inputs_meta=inputs_meta,
        producer_meta=producer_meta,
        generated_at="2026-11-21T12:00:00+00:00",
    )
    assert artifacts.check_contract(base_family, base_raw) == []

    # 1. Leak a private review key ("reviewer") into a trial record and recompute hashes/run_id:
    leaked = json.loads(json.dumps(base_family))
    leaked["trials.jsonl.gz"][0]["termination"]["reviewer"] = "Secret Reviewer"
    assert any("private review key leaked" in p for p in artifacts.check_contract(leaked))

    # 2. Fabricate exact days on a YYYY-MM partial date:
    fab = json.loads(json.dumps(base_family))
    partial_trial = next(
        t for t in fab["trials.jsonl.gz"] if t["durations"]["primary_completion"]["start_precision"] == "month"
    )
    partial_trial["durations"]["primary_completion"]["days"] = 31
    assert any("partial date fabricated exact days" in p for p in artifacts.check_contract(fab))

    # 3. Pool ESTIMATED enrollment into actual_participants_per_month:
    pooled = json.loads(json.dumps(base_family))
    est_trial = next(t for t in pooled["trials.jsonl.gz"] if t["enrollment"]["type"] == "ESTIMATED")
    est_trial["accrual"]["actual_participants_per_month"] = 12.5
    assert any("non-ACTUAL enrollment leaked" in p for p in artifacts.check_contract(pooled))

    # 4. Unwithhold percentiles on a small cell (< min_cell_trials):
    unwithheld = json.loads(json.dumps(base_family))
    small_cell = next(c for c in unwithheld["strata.json"]["by_indication"] if c["n_trials"] < 5)
    small_cell["realized_primary_duration_months"]["withheld"] = False
    small_cell["realized_primary_duration_months"]["p50"] = 14.0
    assert any("failed to withhold percentiles" in p for p in artifacts.check_contract(unwithheld))


def _fixture_family(
    trials: list[dict[str, Any]],
    sponsor_meta: dict[str, Any],
    review_meta: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Assemble an in-memory artifact family with fixed fixture provenance."""
    return artifacts.build_artifact_family(
        trials=trials,
        sponsor_meta=sponsor_meta,
        review_meta=review_meta,
        inputs_meta={
            "records": {"mode": "fixture", "source": "mem", "n_records": len(trials), "sha256": "a" * 64},
            "sponsor_rules": {"file": "company_aliases.csv", "sha256": sponsor_meta["rules_sha256"]},
            "termination_reviews": {"file": "termination_reviews.csv", "n_rows": 0, "sha256": "b" * 64},
            "condition_mesh": None,
        },
        producer_meta={
            "repository": "civicsample-engine",
            "commit": None,
            "uncommitted_changes": None,
            "source_sha256": "c" * 64,
        },
        generated_at="2026-11-21T12:00:00+00:00",
    )


def test_weekly_untyped_dates_excluded_from_percentiles_and_counted_in_summary() -> None:
    weekly_status = "date_type_unavailable_in_weekly_projection"
    api_studies = [make_ctgov_study(f"NCT9960000{i}") for i in range(1, 6)]
    weekly_records = [make_weekly_record(f"NCT9960001{i}") for i in range(0, 5)]
    trials, sponsor_meta, review_meta = benchmarks.build_trial_records(
        api_studies + weekly_records, reviews={}
    )

    overall = benchmarks.summarize_cell(trials, min_cell_trials=5)
    for block in (
        "realized_primary_duration_days",
        "realized_primary_duration_months",
        "realized_study_duration_months",
        "actual_participants_per_month",
    ):
        assert overall[block]["n_trials"] == 10
        assert overall[block]["n_eligible"] == 5, block  # API v2 ACTUAL-typed trials only
        assert overall[block]["n_excluded"] == 5, block
    # Every API v2 fixture runs 2020-01-15 -> 2021-01-15; a projected 2031 date would move p90.
    assert overall["realized_primary_duration_days"]["p90"] == 366.0

    summary = artifacts.summarize_trials(trials, min_cell_trials=5)
    metrics = {m["id"]: m for m in summary["metrics"]}
    assert metrics["realized_primary_duration_eligible"]["numerator"] == 5
    assert metrics["realized_study_duration_eligible"]["numerator"] == 5
    assert metrics["actual_accrual_rate_computed"]["numerator"] == 5
    assert metrics["actual_accrual_rate_computed"]["denominator"] == "actual_enrollment_trials"
    for table, prefix in (
        ("primary_duration_states", "primary_duration"),
        ("study_duration_states", "study_duration"),
        ("accrual_rate_states", "accrual_rate"),
    ):
        rows = {r["id"]: r["numerator"] for r in summary["tables"][table]["rows"]}
        assert rows[f"{prefix}:{weekly_status}"] == 5, table
        assert sum(rows.values()) == 10, table  # the partition still covers every trial

    family, raw = _fixture_family(trials, sponsor_meta, review_meta)
    assert artifacts.check_contract(family, raw) == []
    assert weekly_status in family["run.json"]["vocabularies"]["duration_statuses"]
    assert weekly_status in family["run.json"]["vocabularies"]["accrual_rate_statuses"]

    # A weekly window flipped to realized, or velocity computed over it, fails the contract.
    realized_bounds = {
        "eligible_for_realized_duration": True,
        "days": 2706,
        "min_days": 2706,
        "max_days": 2706,
        "midpoint_days": 2706.0,
        "min_months": 88.9,
        "max_months": 88.9,
        "midpoint_months": 88.9,
    }
    flipped = json.loads(json.dumps(family))
    wk = next(t for t in flipped["trials.jsonl.gz"] if t["nct_id"] == "NCT99600010")
    wk["durations"]["primary_completion"].update(realized_bounds)
    assert any("no recorded type treated as realized" in p for p in artifacts.check_contract(flipped))
    # The original defect: a weekly build reading an untyped window as ACTUAL under any status.
    as_actual = json.loads(json.dumps(family))
    as_actual["run.json"]["inputs"]["records"]["mode"] = "weekly_full_records"
    wk = next(t for t in as_actual["trials.jsonl.gz"] if t["nct_id"] == "NCT99600010")
    wk["durations"]["primary_completion"].update(realized_bounds, status="exact_days")
    assert any("no recorded type treated as realized" in p for p in artifacts.check_contract(as_actual))
    leaked = json.loads(json.dumps(family))
    wk = next(t for t in leaked["trials.jsonl.gz"] if t["nct_id"] == "NCT99600010")
    wk["accrual"].update(
        rate_status="computed",
        calendar_window_used="start_to_primary_completion",
        actual_participants_per_month=2.7,
        min_rate=2.7,
        max_rate=2.7,
    )
    assert any("window not eligible for realized duration" in p for p in artifacts.check_contract(leaked))


_WEEKLY_EXTRACTED_AT = "2026-11-15T06:00:00+00:00"
_WEEKLY_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _write_weekly_inputs(
    tmp_path: Path,
    *,
    mesh_stamps: dict[str, Any] | None = None,
    stale_line_index: int | None = None,
    n_mesh_lines: int = 5,
) -> tuple[Path, Path]:
    """Write a 5-trial weekly `demographics.json.gz` and a `condition_mesh.jsonl.gz` sidecar.

    Sidecar lines carry the run stamps `src/extract_all.py` writes (`snapshot_date`,
    `extracted_at`, `pipeline_commit`), matching the demographics container unless `mesh_stamps`
    overrides them for every line or `stale_line_index` gives one line another week's stamps.
    `n_mesh_lines < 5` writes a sidecar covering only part of the cohort.
    """
    weekly_records = [
        {
            "nct_id": f"NCT9930000{i}",
            "brief_title": f"Weekly study {i}",
            "study_type": "INTERVENTIONAL",
            "phase": "PHASE3",
            "enrollment": 150 + i,
            "enrollment_type": "ACTUAL",
            "status": "COMPLETED",
            "start_date": "2019-06-01",
            "primary_completion_date": "2021-06-01",
            "completion_date": "2021-09-01",
            "results_date": "2022-03-01",
            "why_stopped": "",
            "sponsor_class": "INDUSTRY",
            "lead_sponsor_name": "Pfizer",
            "collaborators": [],
            "study_sites": [{"facility": "Hospital A", "country": "United States"}],
            "conditions": ["Hypertension"],
            "primary_condition": "Cardiovascular",
            "secondary_condition": "Hypertension",
        }
        for i in range(1, 6)
    ]
    demog_path = tmp_path / "demographics.json.gz"
    demog_body = {
        "extracted_at": _WEEKLY_EXTRACTED_AT,
        "pipeline_commit": _WEEKLY_COMMIT,
        "data": weekly_records,
    }
    demog_path.write_bytes(gzip.compress(json.dumps(demog_body).encode("utf-8"), mtime=0))

    stamps = mesh_stamps or {
        "snapshot_date": "2026-11-15",
        "extracted_at": _WEEKLY_EXTRACTED_AT,
        "pipeline_commit": _WEEKLY_COMMIT,
    }
    stale = {
        "snapshot_date": "2026-11-08",
        "extracted_at": "2026-11-08T06:00:00+00:00",
        "pipeline_commit": "fedcba9876543210fedcba9876543210fedcba98",
    }
    mesh_path = tmp_path / "condition_mesh.jsonl.gz"
    mesh_lines = [
        json.dumps(
            {
                "schema_version": "1.0",
                "nct_id": f"NCT9930000{i}",
                "state": "indexed",
                "meshes": [{"id": "D006973", "term": "Hypertension"}],
                "ancestors": [],
                "refetched": False,
                **(stale if stale_line_index == i else stamps),
            }
        )
        for i in range(1, n_mesh_lines + 1)
    ]
    mesh_path.write_bytes(gzip.compress(("\n".join(mesh_lines) + "\n").encode("utf-8"), mtime=0))
    return demog_path, mesh_path


def _build_weekly(demog_path: Path, mesh_path: Path, out_dir: Path) -> int:
    return build_feasibility_benchmarks.main(
        [
            "--demographics",
            str(demog_path),
            "--condition-mesh",
            str(mesh_path),
            "--generated-at",
            "2026-11-21T12:00:00+00:00",
            "--out",
            str(out_dir),
        ]
    )


def test_weekly_demographics_and_condition_mesh_sidecar_build(tmp_path: Path) -> None:
    demog_path, mesh_path = _write_weekly_inputs(tmp_path)
    out_dir = tmp_path / "weekly_build"
    assert _build_weekly(demog_path, mesh_path, out_dir) == 0
    assert check_feasibility_contract.main([str(out_dir)]) == 0
    strata = json.loads((out_dir / "strata.json").read_text(encoding="utf-8"))
    assert strata["by_mesh_descriptor"][0]["mesh_id"] == "D006973"
    assert strata["by_mesh_descriptor"][0]["n_trials"] == 5

    # The matched run stamps are recorded alongside the sidecar's sha256.
    run = json.loads((out_dir / "run.json").read_text(encoding="utf-8"))
    mesh_meta = run["inputs"]["condition_mesh"]
    assert mesh_meta["provenance_check"] == "weekly_run_stamps_and_cohort_coverage"
    assert mesh_meta["extracted_at"] == _WEEKLY_EXTRACTED_AT
    assert mesh_meta["pipeline_commit"] == _WEEKLY_COMMIT
    assert mesh_meta["extracted_at"] == run["inputs"]["records"]["source_extracted_at"]
    assert mesh_meta["pipeline_commit"] == run["inputs"]["records"]["source_pipeline_commit"]
    assert mesh_meta["n_cohort_trials_covered"] == 5
    assert mesh_meta["n_rows_outside_cohort"] == 0


@pytest.mark.parametrize(
    "case",
    ["every_line_from_another_week", "one_line_from_another_week", "stamps_missing", "other_commit"],
)
def test_weekly_build_refuses_condition_mesh_sidecar_from_another_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    if case == "every_line_from_another_week":
        demog_path, mesh_path = _write_weekly_inputs(
            tmp_path,
            mesh_stamps={
                "snapshot_date": "2026-11-08",
                "extracted_at": "2026-11-08T06:00:00+00:00",
                "pipeline_commit": "fedcba9876543210fedcba9876543210fedcba98",
            },
        )
        expect = "extracted_at '2026-11-08T06:00:00+00:00'"
    elif case == "one_line_from_another_week":
        demog_path, mesh_path = _write_weekly_inputs(tmp_path, stale_line_index=4)
        expect = "NCT99300004 extracted_at"
    elif case == "stamps_missing":
        demog_path, mesh_path = _write_weekly_inputs(tmp_path, mesh_stamps={"snapshot_date": "2026-11-15"})
        expect = "extracted_at None"
    else:
        demog_path, mesh_path = _write_weekly_inputs(
            tmp_path,
            mesh_stamps={
                "snapshot_date": "2026-11-15",
                "extracted_at": _WEEKLY_EXTRACTED_AT,
                "pipeline_commit": "fedcba9876543210fedcba9876543210fedcba98",
            },
        )
        expect = "pipeline_commit 'fedcba9876543210fedcba9876543210fedcba98'"
    out_dir = tmp_path / "refused"
    assert _build_weekly(demog_path, mesh_path, out_dir) == 1
    err = capsys.readouterr().err
    assert "REFUSED" in err and "not from the run that wrote these full records" in err
    assert expect in err
    assert not out_dir.exists()


def test_weekly_build_refuses_unstamped_full_records(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    demog_path, mesh_path = _write_weekly_inputs(tmp_path)
    body = json.loads(gzip.decompress(demog_path.read_bytes()))
    body["pipeline_commit"] = None
    demog_path.write_bytes(gzip.compress(json.dumps(body).encode("utf-8"), mtime=0))
    out_dir = tmp_path / "refused"
    assert _build_weekly(demog_path, mesh_path, out_dir) == 1
    assert "carry no pipeline_commit" in capsys.readouterr().err
    assert not out_dir.exists()


def test_condition_mesh_sidecar_must_cover_every_cohort_trial(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Weekly input: matching stamps, but lines for only 3 of the 5 cohort trials.
    demog_path, mesh_path = _write_weekly_inputs(tmp_path, n_mesh_lines=3)
    assert _build_weekly(demog_path, mesh_path, tmp_path / "partial_weekly") == 1
    err = capsys.readouterr().err
    assert "no line for 2 of 5 cohort trials (first: NCT99300004, NCT99300005)" in err
    assert not (tmp_path / "partial_weekly").exists()

    # API v2 archive input: no run stamps to compare, but cohort coverage is still required.
    archive_path = tmp_path / "studies.jsonl.gz"
    studies = [make_ctgov_study(f"NCT9970000{i}") for i in (1, 2)]
    archive_path.write_bytes(
        gzip.compress(("\n".join(json.dumps(s) for s in studies) + "\n").encode("utf-8"), mtime=0)
    )
    def _mesh_lines(ncts: tuple[str, ...]) -> bytes:
        body = "".join(
            json.dumps(
                {"schema_version": "1.0", "nct_id": n, "state": "not_indexed", "meshes": None, "ancestors": None}
            )
            + "\n"
            for n in ncts
        )
        return gzip.compress(body.encode("utf-8"), mtime=0)

    mesh_one = tmp_path / "mesh_one.jsonl.gz"
    mesh_one.write_bytes(_mesh_lines(("NCT99700001",)))
    archive_args = ["--archive", str(archive_path), "--generated-at", "2026-11-21T12:00:00+00:00"]
    out_partial = tmp_path / "partial_archive"
    assert (
        build_feasibility_benchmarks.main(
            archive_args + ["--condition-mesh", str(mesh_one), "--out", str(out_partial)]
        )
        == 1
    )
    assert "no line for 1 of 2 cohort trials (first: NCT99700002)" in capsys.readouterr().err
    assert not out_partial.exists()

    # A superset sidecar covering the cohort builds and records which check ran.
    mesh_all = tmp_path / "mesh_all.jsonl.gz"
    mesh_all.write_bytes(_mesh_lines(("NCT99700001", "NCT99700002", "NCT99700099")))
    out_ok = tmp_path / "archive_ok"
    assert (
        build_feasibility_benchmarks.main(archive_args + ["--condition-mesh", str(mesh_all), "--out", str(out_ok)])
        == 0
    )
    mesh_meta = json.loads((out_ok / "run.json").read_text(encoding="utf-8"))["inputs"]["condition_mesh"]
    assert mesh_meta["provenance_check"] == "cohort_coverage_only"
    assert mesh_meta["extracted_at"] is None and mesh_meta["pipeline_commit"] is None
    assert mesh_meta["n_cohort_trials_covered"] == 2
    assert mesh_meta["n_rows_outside_cohort"] == 1


def test_python311_ast_full_type_hints_and_zero_rng_hygiene() -> None:
    owned_py_files = sorted(
        list((ROOT / "feasibility").glob("*.py"))
        + [
            ROOT / "scripts" / "build_feasibility_benchmarks.py",
            ROOT / "scripts" / "check_feasibility_contract.py",
            ROOT / "scripts" / "export_feasibility_site.py",
        ]
        + list((ROOT / "tests" / "feasibility").glob("*.py"))
    )
    assert len(owned_py_files) >= 8

    for py_path in owned_py_files:
        source = py_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_path), feature_version=(3, 11))

        has_future_annotations = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in tree.body
        )
        assert has_future_annotations, f"{py_path}: missing 'from __future__ import annotations'"

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else ([node.module] if node.module else [])
                )
                for mod in mods:
                    assert mod != "random" and not mod.startswith("random."), (
                        f"{py_path}: forbidden RNG import {mod!r}"
                    )
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.returns is not None, f"{py_path}:{node.lineno} {node.name} missing return annotation"
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
                        f"{py_path}:{node.lineno} {node.name} parameter {arg.arg!r} missing type annotation"
                    )


@pytest.mark.parametrize(
    "condition, wrong",
    [
        ("Falls", ("Oncology", "Hematologic Malignancy")),
        ("Trauma", ("Autoimmune and Inflammatory", "Rheumatoid Arthritis")),
        ("Caregiver Burnout", ("Dermatology", "Wound and Ulcer")),
    ],
)
def test_production_classifier_keeps_short_keywords_standing_alone(
    monkeypatch: pytest.MonkeyPatch, condition: str, wrong: tuple[str, str]
) -> None:
    # The normal environment has rapidfuzz, so indication comes from src.condition_classifier.
    assert benchmarks._HAS_RAPIDFUZZ and benchmarks._cond_cls is not None
    monkeypatch.setattr(benchmarks, "_ONTOLOGY_CACHE", None)
    assert benchmarks._classify_conditions_FallbackOrFull([condition]) != wrong
    assert benchmarks._classify_conditions_FallbackOrFull(["Gliosarcoma"]) == ("Oncology", "Sarcoma")


@pytest.mark.parametrize(
    "generated_at",
    [
        "2026-02-30T12:00:00+00:00",   # impossible date
        "2026-11-21garbage",           # trailing text after the date
        "2026-11-21",                  # no time
        "2026-11-21T12:00:00",         # naive time
        "2026-11-21T12:00:00+05:00",   # not UTC
        "not a timestamp",
    ],
)
def test_generated_at_must_be_a_whole_utc_timestamp(generated_at: str) -> None:
    with pytest.raises(ValueError, match="generated_at"):
        artifacts.utc_timestamp_date(generated_at)


@pytest.mark.parametrize("generated_at", ["2026-11-21T12:00:00+00:00", "2026-11-21T12:00:00Z"])
def test_generated_at_accepts_utc(generated_at: str) -> None:
    assert artifacts.utc_timestamp_date(generated_at) == "2026-11-21"


def test_site_export_keeps_the_source_pipeline_commit(tmp_path: Path) -> None:
    demog_path, mesh_path = _write_weekly_inputs(tmp_path)
    out_dir = tmp_path / "weekly_build"
    site_dir = tmp_path / "weekly_site"
    assert _build_weekly(demog_path, mesh_path, out_dir) == 0
    assert export_feasibility_site.main([str(out_dir), str(site_dir)]) == 0
    site_run = json.loads((site_dir / "run.json").read_text(encoding="utf-8"))
    records = site_run["inputs"]["records"]
    assert records["source_pipeline_commit"] == _WEEKLY_COMMIT
    assert records["source_extracted_at"] == _WEEKLY_EXTRACTED_AT


def test_site_export_names_the_termination_review_file_by_digest(tmp_path: Path) -> None:
    # Two review files with the same number of rows can publish different classifications, so the
    # site manifest carries the build's review-file digest, not only its row count.
    demog_path, mesh_path = _write_weekly_inputs(tmp_path)
    out_dir = tmp_path / "weekly_build"
    site_dir = tmp_path / "weekly_site"
    assert _build_weekly(demog_path, mesh_path, out_dir) == 0
    assert export_feasibility_site.main([str(out_dir), str(site_dir)]) == 0
    build_reviews = json.loads((out_dir / "run.json").read_text(encoding="utf-8"))["inputs"]["termination_reviews"]
    site_inputs = json.loads((site_dir / "run.json").read_text(encoding="utf-8"))["inputs"]
    assert site_inputs["termination_reviews_sha256"] == build_reviews["sha256"]
    assert site_inputs["termination_reviews_n_rows"] == build_reviews["n_rows"]


# ---- Producer digest: the build's import closure plus the data files it reads


def test_producer_paths_cover_every_imported_producer() -> None:
    import src.utils  # noqa: F401  # loaded in this process but not imported by the build

    paths = build_feasibility_benchmarks.producer_paths()
    for required in (
        "condition_ontology.json",
        "feasibility/benchmarks.py",
        "feasibility/termination_reviews.csv",
        "scripts/build_feasibility_benchmarks.py",
        "scripts/export_feasibility_site.py",
        "src/condition_classifier.py",
        "src/condition_mesh.py",
        "src/full_records.py",
        "sponsors/adapter.py",
        "sponsors/audit_states.py",
        "sponsors/sponsor_roles.py",
    ):
        assert required in paths, required
    assert "src/utils.py" not in paths
    assert not any(p.startswith("tests/") for p in paths)


@pytest.mark.parametrize("changed", ["condition_ontology.json", "src/condition_classifier.py"])
def test_source_digest_changes_with_an_imported_producer(tmp_path: Path, changed: str) -> None:
    paths = build_feasibility_benchmarks.producer_paths()
    for rel in paths:
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT / rel).read_bytes())
    same = build_feasibility_benchmarks.source_digest(tmp_path, paths)
    assert same == build_feasibility_benchmarks.source_digest(ROOT, paths)
    (tmp_path / changed).write_bytes((tmp_path / changed).read_bytes() + b"\n")
    assert build_feasibility_benchmarks.source_digest(tmp_path, paths) != same


# ---- --archive response envelopes: a failed, altered or short response is refused


def _envelope(
    studies: list[dict[str, Any]],
    ids: list[str] | None,
    *,
    status: int = 200,
    url: str | None = None,
    body: str | None = None,
) -> dict[str, Any]:
    import hashlib

    text = body if body is not None else json.dumps({"studies": studies, "nextPageToken": "tok"})
    env: dict[str, Any] = {
        "status": status,
        "body_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "body": text,
    }
    if ids is not None:
        env["ids"] = ids
    if url is not None:
        env["url"] = url
    return env


def _write_archive(tmp_path: Path, lines: list[dict[str, Any]]) -> Path:
    path = tmp_path / "raw_responses.jsonl.gz"
    text = "".join(json.dumps(line) + "\n" for line in lines)
    path.write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))
    return path


def _load_archive(path: Path) -> list[dict[str, Any]]:
    studies, _meta = build_feasibility_benchmarks.load_studies_input(
        demographics_path=None, records_dir=None, archive_path=path
    )
    return studies


def test_archive_accepts_complete_response_envelopes(tmp_path: Path) -> None:
    a, b, c = (make_ctgov_study(f"NCT9960000{i}") for i in range(1, 4))
    url = "https://clinicaltrials.gov/api/v2/studies?filter.ids=NCT99600003&pageSize=50"
    path = _write_archive(
        tmp_path,
        [
            _envelope([a, b], ["NCT99600001", "NCT99600002"]),
            _envelope([c], None, url=url),  # second-pass format: ids only in the url
        ],
    )
    assert len(_load_archive(path)) == 3


@pytest.mark.parametrize(
    "case, match",
    [
        ("http_failure", "status 500"),
        ("altered_body", "body_sha256"),
        ("zero_studies", "zero studies"),
        ("missing_trial", "1 requested trial"),
        ("unrequested_trial", "unrequested"),
        ("repeated_trial", "repeated"),
        ("no_requested_ids", "names no requested NCT ids"),
        ("empty_studies_batch", "zero study records"),
    ],
)
def test_archive_refuses_failed_or_short_response_envelopes(
    tmp_path: Path, case: str, match: str
) -> None:
    a, b = make_ctgov_study("NCT99600001"), make_ctgov_study("NCT99600002")
    ok = _envelope([a], ["NCT99600001"])
    bad: dict[str, Any] = {
        "http_failure": _envelope([], ["NCT99600002"], status=500, body="upstream error"),
        "altered_body": {**_envelope([b], ["NCT99600002"]), "body_sha256": "0" * 64},
        "zero_studies": _envelope([], ["NCT99600002"]),
        "missing_trial": _envelope([b], ["NCT99600002", "NCT99600003"]),
        "unrequested_trial": _envelope([b, make_ctgov_study("NCT99600004")], ["NCT99600002"]),
        "repeated_trial": _envelope([b, b], ["NCT99600002"]),
        "no_requested_ids": _envelope([b], None),
        "empty_studies_batch": {"studies": []},
    }[case]
    path = _write_archive(tmp_path, [ok, bad])
    with pytest.raises(ValueError, match=match):
        _load_archive(path)
    out = tmp_path / "out"
    assert build_feasibility_benchmarks.main(["--archive", str(path), "--out", str(out)]) == 1
    assert not out.exists()


# ---- Contract: generated_at and condition_mesh states are rechecked on a written build


@pytest.mark.parametrize(
    "generated_at, match",
    [
        ("garbage", "generated_at must be an ISO-8601 UTC timestamp"),
        ("2026-02-30T12:00:00+00:00", "generated_at must be an ISO-8601 UTC timestamp"),
        ("2026-11-21T12:00:00+05:00", "generated_at must carry a UTC offset"),
        ("2026-11-22T12:00:00+00:00", "run_id date 2026-11-21 != generated_at date 2026-11-22"),
    ],
)
def test_contract_rechecks_generated_at(generated_at: str, match: str) -> None:
    trials, sponsor_meta, review_meta = benchmarks.build_trial_records(_sample_cohort(), reviews={})
    family, raw = _fixture_family(trials, sponsor_meta, review_meta)
    assert artifacts.check_contract(family, raw) == []
    edited = json.loads(json.dumps(family))
    edited["run.json"]["generated_at"] = generated_at
    assert any(match in p for p in artifacts.check_contract(edited)), artifacts.check_contract(edited)


def test_contract_refuses_an_unknown_condition_mesh_state() -> None:
    trials, sponsor_meta, review_meta = benchmarks.build_trial_records(_sample_cohort(), reviews={})
    family, _raw = _fixture_family(trials, sponsor_meta, review_meta)
    edited = json.loads(json.dumps(family))
    edited["trials.jsonl.gz"][0]["indication"]["condition_mesh"]["state"] = "future_state"
    nct = edited["trials.jsonl.gz"][0]["nct_id"]
    assert f"{nct}.indication.condition_mesh: invalid state 'future_state'" in artifacts.check_contract(edited)


# ---- Sidecar lines must keep the src/condition_mesh.py line contract

_MESH = [{"id": "D003924", "term": "Diabetes Mellitus, Type 2"}]


def _mesh_line(**fields: Any) -> dict[str, Any]:
    line: dict[str, Any] = {"schema_version": "1.0", "nct_id": "NCT99800001", "meshes": None, "ancestors": None}
    line.update(fields)
    return line


def _load_mesh(tmp_path: Path, line: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    path = tmp_path / "condition_mesh.jsonl.gz"
    path.write_bytes(gzip.compress((json.dumps(line) + "\n").encode("utf-8"), mtime=0))
    by_nct, _meta = build_feasibility_benchmarks.load_condition_mesh_sidecar(
        path, records_meta={"mode": "jsonl_archive"}
    )
    return by_nct


@pytest.mark.parametrize(
    "line",
    [
        _mesh_line(state="indexed", meshes=_MESH, ancestors=[]),
        _mesh_line(state="not_indexed", meshes=[], ancestors=_MESH),
        _mesh_line(state="not_indexed"),
        _mesh_line(state="no_module"),
        _mesh_line(state="no_derived"),
        _mesh_line(state="malformed", meshes=None, ancestors=_MESH, problem="meshes is not a list of id/term pairs"),
    ],
)
def test_condition_mesh_sidecar_accepts_each_producer_state(tmp_path: Path, line: dict[str, Any]) -> None:
    by_nct = _load_mesh(tmp_path, line)
    assert by_nct is not None and by_nct["NCT99800001"]["state"] == line["state"]


@pytest.mark.parametrize(
    "line, match",
    [
        (_mesh_line(state="future_state"), "state 'future_state' is not one of"),
        (_mesh_line(state="indexed", meshes=_MESH, ancestors=[], schema_version="2.0"), "schema_version '2.0'"),
        ({k: v for k, v in _mesh_line(state="no_module").items() if k != "schema_version"}, "schema_version None"),
        (_mesh_line(state="indexed", meshes=[], ancestors=[]), "indexed without"),
        (_mesh_line(state="indexed", ancestors=[]), "indexed without"),
        (_mesh_line(state="not_indexed", meshes=_MESH), "not_indexed with meshes"),
        (_mesh_line(state="indexed", meshes=[{"id": "D1"}], ancestors=[]), "meshes is neither null"),
        (_mesh_line(state="indexed", meshes=_MESH, ancestors="D2"), "ancestors is neither null"),
        ({k: v for k, v in _mesh_line(state="no_module").items() if k != "ancestors"}, "no ancestors key"),
        (_mesh_line(state="no_derived", meshes=[]), "no_derived with a meshes or ancestors list"),
        (_mesh_line(state="malformed"), "malformed without a problem"),
        (_mesh_line(state="indexed", meshes=_MESH, ancestors=[], problem="x"), "indexed carries a problem"),
    ],
)
def test_condition_mesh_sidecar_refuses_lines_outside_the_producer_contract(
    tmp_path: Path, line: dict[str, Any], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        _load_mesh(tmp_path, line)


@pytest.mark.parametrize("bad_entry", [None, "NCT99600009", ["x"], 7])
def test_direct_and_enveloped_studies_batches_refuse_a_malformed_entry(
    tmp_path: Path, bad_entry: object
) -> None:
    a, b = make_ctgov_study("NCT99600001"), make_ctgov_study("NCT99600002")
    direct = tmp_path / "direct"
    direct.mkdir()
    (direct / "batch.json").write_text(json.dumps({"studies": [a, bad_entry, b]}), encoding="utf-8")
    with pytest.raises(ValueError, match="not study objects"):
        build_feasibility_benchmarks.load_studies_input(
            demographics_path=None, records_dir=direct, archive_path=None
        )
    path = _write_archive(
        tmp_path,
        [_envelope([a, bad_entry, b], ["NCT99600001", "NCT99600002"])],  # type: ignore[list-item]
    )
    with pytest.raises(ValueError, match="not study objects"):
        _load_archive(path)


# ---- Inputs must be complete study records, never field-projected ones


def test_archive_refuses_a_field_projected_response_envelope(tmp_path: Path) -> None:
    study = make_ctgov_study("NCT99600011")
    url = (
        "https://clinicaltrials.gov/api/v2/studies?filter.ids=NCT99600011&pageSize=50"
        "&fields=NCTId,HasResults,ResultsSection,OutcomesModule,StatusModule,DesignModule"
    )
    path = _write_archive(tmp_path, [_envelope([study], None, url=url)])
    with pytest.raises(ValueError, match="fields=NCTId"):
        _load_archive(path)


@pytest.mark.parametrize(
    "module", ["sponsorCollaboratorsModule", "conditionsModule", "statusModule", "designModule", "identificationModule"]
)
def test_records_missing_a_required_protocol_module_are_refused(tmp_path: Path, module: str) -> None:
    complete = make_ctgov_study("NCT99600012")
    projected = make_ctgov_study("NCT99600013")
    if module == "identificationModule":
        projected["protocolSection"]["identificationModule"] = None
    else:
        projected["protocolSection"].pop(module)
    # records-dir
    rdir = tmp_path / "records"
    rdir.mkdir()
    (rdir / "a.json").write_text(json.dumps(complete), encoding="utf-8")
    (rdir / "b.json").write_text(json.dumps(projected), encoding="utf-8")
    with pytest.raises(ValueError, match=f"lack required protocol modules.*{module}"):
        build_feasibility_benchmarks.load_studies_input(
            demographics_path=None, records_dir=rdir, archive_path=None
        )
    # archive of direct study lines
    apath = tmp_path / "studies.jsonl.gz"
    apath.write_bytes(
        gzip.compress((json.dumps(complete) + "\n" + json.dumps(projected) + "\n").encode("utf-8"), mtime=0)
    )
    with pytest.raises(ValueError, match="lack required protocol modules"):
        _load_archive(apath)


@pytest.mark.parametrize(
    "edit, expect",
    [
        ("drop_results_section", "resultsSection"),  # hasResults true, results not carried
        ("empty_results_section", "resultsSection"),
        ("drop_has_results", "hasResults"),
        ("string_has_results", "hasResults"),
    ],
)
def test_records_with_an_incomplete_results_part_are_refused(tmp_path: Path, edit: str, expect: str) -> None:
    complete = make_ctgov_study("NCT99600015")
    assert complete["hasResults"] is True and complete["resultsSection"]
    projected = make_ctgov_study("NCT99600016")
    if edit == "drop_results_section":
        projected.pop("resultsSection")
    elif edit == "empty_results_section":
        projected["resultsSection"] = {}
    elif edit == "drop_has_results":
        projected.pop("hasResults")
    else:
        projected["hasResults"] = "true"
    rdir = tmp_path / "records"
    rdir.mkdir()
    (rdir / "a.json").write_text(json.dumps(complete), encoding="utf-8")
    (rdir / "b.json").write_text(json.dumps(projected), encoding="utf-8")
    with pytest.raises(ValueError, match=f"NCT99600016 lacks {expect}"):
        build_feasibility_benchmarks.load_studies_input(demographics_path=None, records_dir=rdir, archive_path=None)
    apath = tmp_path / "studies.jsonl.gz"
    apath.write_bytes(
        gzip.compress((json.dumps(complete) + "\n" + json.dumps(projected) + "\n").encode("utf-8"), mtime=0)
    )
    with pytest.raises(ValueError, match="or results parts"):
        _load_archive(apath)


@pytest.mark.parametrize(
    "drop", [("identificationModule", "briefTitle"), ("statusModule", "statusVerifiedDate"),
             ("statusModule", "overallStatus"), ("designModule", "studyType")],
)
def test_records_missing_a_registration_field_inside_a_module_shell_are_refused(
    tmp_path: Path, drop: tuple[str, str]
) -> None:
    complete = make_ctgov_study("NCT99600018")
    shell = make_ctgov_study("NCT99600019")
    shell["protocolSection"][drop[0]].pop(drop[1])
    rdir = tmp_path / "records"
    rdir.mkdir()
    (rdir / "a.json").write_text(json.dumps(complete), encoding="utf-8")
    (rdir / "b.json").write_text(json.dumps(shell), encoding="utf-8")
    with pytest.raises(ValueError, match=f"NCT99600019 lacks {drop[0]}.{drop[1]}"):
        build_feasibility_benchmarks.load_studies_input(demographics_path=None, records_dir=rdir, archive_path=None)


def test_field_projected_record_keeping_every_module_shell_is_refused(tmp_path: Path) -> None:
    # A bare record projected to NCT ID, status, sponsor, conditions, study type and hasResults: all five
    # module objects are present, but dates, enrollment and locations are gone.
    full = make_ctgov_study("NCT99600020", has_results=False)
    ps = full["protocolSection"]
    projected = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT99600020"},
            "statusModule": {"overallStatus": ps["statusModule"]["overallStatus"]},
            "sponsorCollaboratorsModule": {"leadSponsor": ps["sponsorCollaboratorsModule"]["leadSponsor"]},
            "conditionsModule": {"conditions": ps["conditionsModule"].get("conditions", ["Asthma"])},
            "designModule": {"studyType": ps["designModule"]["studyType"]},
        },
        "hasResults": False,
    }
    apath = tmp_path / "studies.jsonl.gz"
    apath.write_bytes(gzip.compress((json.dumps(projected) + "\n").encode("utf-8"), mtime=0))
    with pytest.raises(ValueError, match="identificationModule.briefTitle, statusModule.statusVerifiedDate"):
        _load_archive(apath)


def _strip_payload(study: dict[str, Any], edit: str) -> None:
    """Apply one malformation to `study`'s conditions or lead-sponsor payload, in place."""
    ps = study["protocolSection"]
    lead = ps["sponsorCollaboratorsModule"]["leadSponsor"]
    if edit == "empty_conditions_module":
        ps["conditionsModule"] = {}
    elif edit == "string_conditions":
        ps["conditionsModule"]["conditions"] = "Type 2 Diabetes Mellitus"
    elif edit == "empty_conditions_list":
        ps["conditionsModule"]["conditions"] = []
    elif edit == "blank_condition_only":
        ps["conditionsModule"]["conditions"] = ["   "]
    elif edit == "blank_condition_entry":
        ps["conditionsModule"]["conditions"] = ["Type 2 Diabetes Mellitus", "   "]
    elif edit == "non_string_condition_entry":
        ps["conditionsModule"]["conditions"] = ["Type 2 Diabetes Mellitus", 7]
    elif edit == "drop_lead_sponsor":
        ps["sponsorCollaboratorsModule"].pop("leadSponsor")
    elif edit == "string_lead_sponsor":
        ps["sponsorCollaboratorsModule"]["leadSponsor"] = "Pfizer"
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
    # indication Other, an "Unknown" sponsor or MISSING_CLASS. The error names exactly the one missing
    # payload, through the records directory, direct study lines and response envelopes.
    complete = make_ctgov_study("NCT99600021")
    shell = make_ctgov_study("NCT99600022")
    _strip_payload(shell, edit)
    want = (
        r"1 of 2 study records lack required protocol modules or results parts "
        rf"\(first: NCT99600022 lacks {re.escape(expect)}\)"
    )
    rdir = tmp_path / "records"
    rdir.mkdir()
    (rdir / "a.json").write_text(json.dumps(complete), encoding="utf-8")
    (rdir / "b.json").write_text(json.dumps(shell), encoding="utf-8")
    with pytest.raises(ValueError, match=want):
        build_feasibility_benchmarks.load_studies_input(demographics_path=None, records_dir=rdir, archive_path=None)
    apath = tmp_path / "studies.jsonl.gz"
    apath.write_bytes(gzip.compress((json.dumps(complete) + "\n" + json.dumps(shell) + "\n").encode("utf-8"), mtime=0))
    with pytest.raises(ValueError, match=want):
        _load_archive(apath)
    epath = _write_archive(tmp_path, [_envelope([complete, shell], ["NCT99600021", "NCT99600022"])])
    with pytest.raises(ValueError, match=want):
        _load_archive(epath)


def test_records_with_condition_and_lead_sponsor_payloads_are_accepted(tmp_path: Path) -> None:
    # Control for the refusals above: an untouched fixture record, with a single condition, a lead
    # sponsor of class OTHER and no collaborators, loads through every raw shape and reads back as such.
    study = make_ctgov_study(
        "NCT99600023", conditions=["Asthma"], lead_sponsor_name="Fictional Academic Center",
        lead_sponsor_class="OTHER",
    )
    assert study["protocolSection"]["sponsorCollaboratorsModule"]["collaborators"] == []
    rdir = tmp_path / "records"
    rdir.mkdir()
    (rdir / "a.json").write_text(json.dumps(study), encoding="utf-8")
    apath = tmp_path / "studies.jsonl.gz"
    apath.write_bytes(gzip.compress((json.dumps(study) + "\n").encode("utf-8"), mtime=0))
    epath = _write_archive(tmp_path, [_envelope([study], ["NCT99600023"])])
    loaded = [
        build_feasibility_benchmarks.load_studies_input(demographics_path=None, records_dir=rdir, archive_path=None)[0],
        _load_archive(apath),
        _load_archive(epath),
    ]
    for records in loaded:
        assert len(records) == 1
        m = extract_trial_metrics(records[0])
        assert m["conditions"] == ["Asthma"]
        assert (m["lead_sponsor_name"], m["lead_sponsor_class"]) == ("Fictional Academic Center", "OTHER")


@pytest.mark.parametrize("value", ["bad", "5", True, 2.5, None, 0, -3])
def test_contract_reports_a_malformed_min_cell_threshold_without_raising(value: object) -> None:
    trials, sponsor_meta, review_meta = benchmarks.build_trial_records(_sample_cohort(), reviews={})
    family, _raw = _fixture_family(trials, sponsor_meta, review_meta)
    edited = json.loads(json.dumps(family))
    edited["run.json"]["parameters"]["min_cell_trials"] = value
    problems = artifacts.check_contract(edited)
    assert any("parameters.min_cell_trials must be an integer >= 1" in p for p in problems), problems


def test_records_without_posted_results_are_still_accepted(tmp_path: Path) -> None:
    # hasResults false with no resultsSection is a complete record of a trial that posted nothing.
    study = make_ctgov_study("NCT99600017", has_results=False)
    study.pop("resultsSection", None)
    apath = tmp_path / "studies.jsonl.gz"
    apath.write_bytes(gzip.compress((json.dumps(study) + "\n").encode("utf-8"), mtime=0))
    assert len(_load_archive(apath)) == 1


def test_records_without_locations_or_derived_section_are_still_accepted(tmp_path: Path) -> None:
    study = make_ctgov_study("NCT99600014")
    study["protocolSection"].pop("contactsLocationsModule", None)
    study.pop("derivedSection", None)
    apath = tmp_path / "studies.jsonl.gz"
    apath.write_bytes(gzip.compress((json.dumps(study) + "\n").encode("utf-8"), mtime=0))
    assert len(_load_archive(apath)) == 1


# ---- Provenance: a build whose git revision could not be read is not exported


@pytest.mark.parametrize(
    "commit, dirty, exported",
    [
        (None, None, False),          # git unavailable
        ("a" * 40, None, False),      # git status failed
        ("a" * 40, True, True),       # dirty tree, honestly recorded
        ("a" * 40, False, True),
    ],
)
def test_site_export_requires_readable_git_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commit: str | None, dirty: bool | None, exported: bool
) -> None:
    def fake_producer(root: Path = ROOT) -> dict[str, Any]:
        return {"repository": "civicsample-engine", "commit": commit, "uncommitted_changes": dirty,
                "source_sha256": "c" * 64}

    monkeypatch.setattr(build_feasibility_benchmarks, "producer_metadata", fake_producer)
    demog_path, mesh_path = _write_weekly_inputs(tmp_path)
    out_dir, site_dir = tmp_path / "build", tmp_path / "site"
    assert _build_weekly(demog_path, mesh_path, out_dir) == 0
    assert check_feasibility_contract.main([str(out_dir)]) == 0
    assert (export_feasibility_site.main([str(out_dir), str(site_dir)]) == 0) is exported
    assert site_dir.exists() is exported


@pytest.mark.parametrize("field, value", [("commit", "HEAD"), ("commit", 7), ("uncommitted_changes", "no")])
def test_contract_refuses_malformed_producer_provenance(field: str, value: object) -> None:
    trials, sponsor_meta, review_meta = benchmarks.build_trial_records(_sample_cohort(), reviews={})
    family, _raw = _fixture_family(trials, sponsor_meta, review_meta)
    edited = json.loads(json.dumps(family))
    edited["run.json"]["producer"][field] = value
    assert any(f"producer.{field}" in p for p in artifacts.check_contract(edited))


@pytest.mark.parametrize("stamp, value", [("pipeline_commit", None), ("extracted_at", None), ("extracted_at", " ")])
def test_weekly_build_without_sidecar_still_needs_run_stamps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], stamp: str, value: str | None
) -> None:
    demog_path, _mesh_path = _write_weekly_inputs(tmp_path)
    body = json.loads(gzip.decompress(demog_path.read_bytes()))
    if value is None:
        body.pop(stamp, None)
    else:
        body[stamp] = value
    demog_path.write_bytes(gzip.compress(json.dumps(body).encode("utf-8"), mtime=0))
    out_dir = tmp_path / "refused"
    rc = build_feasibility_benchmarks.main(
        ["--demographics", str(demog_path), "--generated-at", "2026-11-21T12:00:00+00:00", "--out", str(out_dir)]
    )
    assert rc == 1
    assert f"carry no {stamp}" in capsys.readouterr().err
    assert not out_dir.exists()


@pytest.mark.parametrize(
    "stamp, value",
    [
        ("extracted_at", "not-a-time"),
        ("extracted_at", "2026-11-15"),  # a date, no time
        ("extracted_at", "2026-11-15T06:00:00"),  # naive
        ("extracted_at", "2026-11-15T06:00:00+01:00"),  # not UTC
        ("extracted_at", "2026-02-30T06:00:00+00:00"),  # impossible date
        ("pipeline_commit", "garbage"),
        ("pipeline_commit", "0123abc"),  # abbreviated
        ("pipeline_commit", _WEEKLY_COMMIT.upper()),
        ("pipeline_commit", 7),
    ],
)
def test_weekly_build_refuses_malformed_run_stamps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], stamp: str, value: object
) -> None:
    # Present but not in the producer's format (a whole UTC timestamp, a 40-hex revision): such a
    # stamp names no extraction run, so the build is refused, with or without a sidecar.
    demog_path, mesh_path = _write_weekly_inputs(tmp_path)
    body = json.loads(gzip.decompress(demog_path.read_bytes()))
    body[stamp] = value
    demog_path.write_bytes(gzip.compress(json.dumps(body).encode("utf-8"), mtime=0))
    out_dir = tmp_path / "refused"
    rc = build_feasibility_benchmarks.main(
        ["--demographics", str(demog_path), "--generated-at", "2026-11-21T12:00:00+00:00", "--out", str(out_dir)]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "run stamps cannot name the extraction run" in err and stamp in err
    assert not out_dir.exists()


def test_contract_and_export_refuse_edited_weekly_source_stamps(tmp_path: Path) -> None:
    demog_path, mesh_path = _write_weekly_inputs(tmp_path)
    out_dir = tmp_path / "weekly_build"
    assert _build_weekly(demog_path, mesh_path, out_dir) == 0
    family, raw = artifacts.load_dir(out_dir)
    assert not [p for p in artifacts.check_contract(family, raw) if "source stamp" in p]

    for key, value in (("source_extracted_at", "not-a-time"), ("source_pipeline_commit", "garbage")):
        edited = json.loads(json.dumps(family))
        edited["run.json"]["inputs"]["records"][key] = value
        assert any("inputs.records source stamp" in p for p in artifacts.check_contract(edited, raw)), key

    # run.json is outside its own file hashes, so an edit on disk reaches the exporter, which refuses it.
    run_path = out_dir / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["inputs"]["records"]["source_pipeline_commit"] = "garbage"
    run_path.write_text(json.dumps(run), encoding="utf-8")
    site_dir = tmp_path / "site"
    assert export_feasibility_site.main([str(out_dir), str(site_dir)]) == 1
    assert not (site_dir / "run.json").exists()


# ---- Malformed benchmark cells are contract problems, never exceptions

_MISSING = object()


@pytest.mark.parametrize(
    "fname, key, value, expected",
    [
        ("strata.json", "by_indication", [None], "strata.by_indication[0]: cell must be a JSON object, got NoneType"),
        ("strata.json", "cross_strata", ["cell"], "strata.cross_strata[0]: cell must be a JSON object, got str"),
        ("strata.json", "by_phase", {"PHASE3": {}}, "strata.by_phase: must be a list of cell objects, got dict"),
        ("strata.json", "by_mesh_descriptor", 5, "strata.by_mesh_descriptor: must be a list of cell objects, got int"),
        ("strata.json", "by_footprint_tier", _MISSING, "strata.by_footprint_tier: missing cell list"),
        ("strata.json", "overall", None, "strata.overall: must be a cell object, got NoneType"),
        ("strata.json", "overall", [], "strata.overall: must be a cell object, got list"),
        ("sponsors.json", "by_sponsor_class", None, "sponsors.by_sponsor_class: must be a list of cell objects, got NoneType"),
        (
            "sponsors.json",
            "by_canonical_company_lead",
            [7],
            "sponsors.by_canonical_company_lead[0]: cell must be a JSON object, got int",
        ),
        (
            "sponsors.json",
            "by_lead_review_state",
            [None, "attributed"],
            "sponsors.by_lead_review_state[1]: cell must be a JSON object, got str",
        ),
        (
            "sponsors.json",
            "by_lead_review_state",
            "attributed",
            "sponsors.by_lead_review_state: must be a list of cell objects, got str",
        ),
    ],
)
def test_contract_reports_malformed_benchmark_cells_without_raising(
    fname: str, key: str, value: object, expected: str
) -> None:
    trials, sponsor_meta, review_meta = benchmarks.build_trial_records(_sample_cohort(), reviews={})
    family, _raw = _fixture_family(trials, sponsor_meta, review_meta)
    edited = json.loads(json.dumps(family))
    if value is _MISSING:
        del edited[fname][key]
    else:
        edited[fname][key] = value
    problems = artifacts.check_contract(edited)  # must not raise
    assert expected in problems, problems


@pytest.mark.parametrize(
    "fname, value, expected",
    [
        ("strata.json", [], "strata.json: not a JSON object"),
        ("sponsors.json", None, "sponsors.json: not a JSON object"),
        ("summary.json", "summary", "summary.json: not a JSON object"),
    ],
)
def test_contract_reports_a_benchmark_file_that_is_not_an_object_without_raising(
    fname: str, value: object, expected: str
) -> None:
    trials, sponsor_meta, review_meta = benchmarks.build_trial_records(_sample_cohort(), reviews={})
    family, _raw = _fixture_family(trials, sponsor_meta, review_meta)
    edited = json.loads(json.dumps(family))
    edited[fname] = value
    assert expected in artifacts.check_contract(edited)  # must not raise


def test_contract_reports_a_trial_row_that_is_not_an_object_without_raising() -> None:
    trials, sponsor_meta, review_meta = benchmarks.build_trial_records(_sample_cohort(), reviews={})
    family, _raw = _fixture_family(trials, sponsor_meta, review_meta)
    edited = json.loads(json.dumps(family))
    edited["trials.jsonl.gz"][2] = None
    assert "trials[2]: not a JSON object" in artifacts.check_contract(edited)  # must not raise


def test_contract_cli_and_export_fail_on_a_malformed_cell_on_disk(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trials, sponsor_meta, review_meta = benchmarks.build_trial_records(_sample_cohort(), reviews={})
    _family, raw = _fixture_family(trials, sponsor_meta, review_meta)
    build = tmp_path / "build"
    artifacts.write_artifact_family(raw, build)
    assert check_feasibility_contract.main([str(build)]) == 0

    strata_path = build / "strata.json"
    strata = json.loads(strata_path.read_text(encoding="utf-8"))
    strata["by_indication"] = [None]
    strata_path.write_text(json.dumps(strata), encoding="utf-8")
    capsys.readouterr()
    assert check_feasibility_contract.main([str(build)]) == 1
    out = capsys.readouterr().out
    assert "strata.by_indication[0]: cell must be a JSON object, got NoneType" in out
    assert "FAIL:" in out

    site = tmp_path / "site"
    assert export_feasibility_site.main([str(build), str(site)]) == 1
    assert "build failed contract verification" in capsys.readouterr().err
    assert not site.exists()


def test_contract_cli_and_export_report_an_unanticipated_exception(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    trials, sponsor_meta, review_meta = benchmarks.build_trial_records(_sample_cohort(), reviews={})
    _family, raw = _fixture_family(trials, sponsor_meta, review_meta)
    build = tmp_path / "build"
    artifacts.write_artifact_family(raw, build)

    def _crash(parsed_family: dict[str, Any], raw_bytes: dict[str, bytes] | None = None) -> list[str]:
        raise RuntimeError("unanticipated shape")

    monkeypatch.setattr(artifacts, "check_contract", _crash)
    capsys.readouterr()
    assert check_feasibility_contract.main([str(build)]) == 1
    assert (
        f"FAIL: contract check could not complete on {build}: RuntimeError: unanticipated shape"
        in capsys.readouterr().out
    )

    site = tmp_path / "site"
    assert export_feasibility_site.main([str(build), str(site)]) == 1
    assert "contract check could not complete (RuntimeError: unanticipated shape)" in capsys.readouterr().err
    assert not site.exists()
