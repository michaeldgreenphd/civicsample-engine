"""End-to-end tests for `feasibility/benchmarks.py`, `feasibility/artifacts.py`, and CLI scripts."""
from __future__ import annotations

import ast
import csv
import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from feasibility import artifacts, benchmarks
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
            json.dumps({"nct_id": n, "state": "not_indexed", "meshes": None, "ancestors": None}) + "\n"
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
