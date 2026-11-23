"""Tests for eligibility.cli and scripts/build_eligibility_barriers.py."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from eligibility.cli import run_cli


def _study(nct_id: str, criteria: str) -> dict[str, object]:
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id},
            "eligibilityModule": {
                "minimumAge": "18 Years",
                "maximumAge": "N/A",
                "stdAges": ["ADULT", "OLDER_ADULT"],
                "sex": "ALL",
                "healthyVolunteers": False,
                "eligibilityCriteria": criteria,
            },
        }
    }


def test_eligibility_cli_requires_firewall_by_default(tmp_path: Path) -> None:
    p = tmp_path / "studies.json"
    p.write_text(json.dumps({"studies": [_study("NCT99620001", "Inclusion Criteria:\n- Age >= 18 years.")]}), encoding="utf-8")
    with pytest.raises(SystemExit, match="--in-audit-index is required"):
        run_cli(["--studies", str(p), "--out", str(tmp_path / "out")])


def test_eligibility_cli_builds_summary_and_filters_in_audit(tmp_path: Path) -> None:
    p = tmp_path / "studies.json"
    p.write_text(
        json.dumps(
            {
                "studies": [
                    _study(
                        "NCT99620001",
                        "Inclusion Criteria:\n- Age 18 to 65 years.\nExclusion Criteria:\n- Pregnant or breastfeeding women.",
                    ),
                    _study(
                        "NCT99620002",
                        "Inclusion Criteria:\n- Age >= 18 years.\nExclusion Criteria:\n- Total bilirubin > 1.5 x ULN (<= 3.0 x ULN in Gilbert's syndrome).",
                    ),
                    _study(
                        "NCT99999999",
                        "Inclusion Criteria:\n- Age >= 18 years.\nExclusion Criteria:\n- Known HIV infection.",
                    ),
                ]
            }
        ),
        encoding="utf-8",
    )
    idx = tmp_path / "index.json"
    idx.write_text(
        json.dumps(
            {
                "rows": [
                    {"nct_id": "NCT99620001", "in_audit": False},
                    {"nct_id": "NCT99999999", "in_audit": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    rc = run_cli(
        [
            "--studies",
            str(p),
            "--in-audit-index",
            str(idx),
            "--snapshot-date",
            "2026-11-23",
            "--out",
            str(out_dir),
        ]
    )
    assert rc == 0
    run_doc = json.loads((out_dir / "run.json").read_text(encoding="utf-8"))
    summary = json.loads((out_dir / "eligibility_summary.json").read_text(encoding="utf-8"))
    assert run_doc["n_input_records"] == 3
    assert run_doc["n_excluded_in_audit"] == 1
    assert run_doc["n_evaluated"] == 2
    assert summary["n_trials"] == 2
    assert summary["discordance_counts"]["hidden_upper_age_cap"]["true"] == 1
    assert summary["clinical_exception_counts"]["gilberts_syndrome_allowance"] == 1

    with gzip.open(out_dir / "eligibility_lean.jsonl.gz", "rt", encoding="utf-8") as fh:
        lean_ids = [json.loads(line)["nct_id"] for line in fh]
    assert lean_ids == ["NCT99620001", "NCT99620002"]
