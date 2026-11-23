"""Tests for age.cli and scripts/build_age_table.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from age.cli import run_cli
from age.table import read_age_csv


def _synthetic_study(nct_id: str, count_adult: int = 80, count_senior: int = 20) -> dict[str, object]:
    total = count_adult + count_senior
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id},
            "designModule": {"enrollmentInfo": {"count": total}},
            "eligibilityModule": {
                "minimumAge": "18 Years",
                "maximumAge": "N/A",
                "stdAges": ["ADULT", "OLDER_ADULT"],
            },
        },
        "resultsSection": {
            "baselineCharacteristicsModule": {
                "groups": [{"id": "BG000", "title": "Total"}],
                "denoms": [{"units": "Participants", "counts": [{"groupId": "BG000", "value": str(total)}]}],
                "measures": [
                    {
                        "title": "Age, Categorical",
                        "paramType": "COUNT_OF_PARTICIPANTS",
                        "unitOfMeasure": "Participants",
                        "classes": [
                            {
                                "title": "<=18 years",
                                "categories": [{"measurements": [{"groupId": "BG000", "value": "0"}]}],
                            },
                            {
                                "title": "Between 18 and 65 years",
                                "categories": [
                                    {"measurements": [{"groupId": "BG000", "value": str(count_adult)}]}
                                ],
                            },
                            {
                                "title": ">=65 years",
                                "categories": [
                                    {"measurements": [{"groupId": "BG000", "value": str(count_senior)}]}
                                ],
                            },
                        ],
                    }
                ],
            }
        },
    }


def test_cli_requires_in_audit_index_by_default(tmp_path: Path) -> None:
    studies_path = tmp_path / "studies.json"
    studies_path.write_text(json.dumps({"studies": [_synthetic_study("NCT99600001")]}), encoding="utf-8")
    out_dir = tmp_path / "out"
    with pytest.raises(SystemExit, match="--in-audit-index is required"):
        run_cli(["--studies", str(studies_path), "--out", str(out_dir)])


def test_cli_filters_in_audit_and_roundtrips_from_raw(tmp_path: Path) -> None:
    studies_path = tmp_path / "studies.json"
    studies_path.write_text(
        json.dumps(
            {
                "studies": [
                    _synthetic_study("NCT99600001", count_adult=80, count_senior=20),
                    _synthetic_study("NCT99600002", count_adult=100, count_senior=0),
                    _synthetic_study("NCT99999999", count_adult=50, count_senior=50),
                ]
            }
        ),
        encoding="utf-8",
    )
    idx_path = tmp_path / "index.json"
    idx_path.write_text(
        json.dumps(
            {
                "rows": [
                    {"nct_id": "NCT99600001", "in_audit": False},
                    {"nct_id": "NCT99999999", "in_audit": True},
                ]
            }
        ),
        encoding="utf-8",
    )

    out_studies = tmp_path / "out_studies"
    rc = run_cli(
        [
            "--studies",
            str(studies_path),
            "--in-audit-index",
            str(idx_path),
            "--snapshot-date",
            "2026-11-23",
            "--out",
            str(out_studies),
        ]
    )
    assert rc == 0
    summary = json.loads((out_studies / "age_summary.json").read_text(encoding="utf-8"))
    assert summary["n_input_records"] == 3
    assert summary["n_excluded_in_audit"] == 1
    assert summary["n_evaluated"] == 2
    assert summary["representation_gap_counts"]["older_adult_representation_gap"] == 1

    rows_direct = read_age_csv(str(out_studies / "age_parsed.csv.gz"))
    assert [r["nct_id"] for r in rows_direct] == ["NCT99600001", "NCT99600002"]

    # Re-parse offline from retained raw envelope and verify exact row parity
    out_raw = tmp_path / "out_from_raw"
    rc2 = run_cli(
        [
            "--from-raw",
            str(out_studies / "age_raw_measures.jsonl.gz"),
            "--synthetic-ids-only",
            "--snapshot-date",
            "2026-11-23",
            "--out",
            str(out_raw),
        ]
    )
    assert rc2 == 0
    rows_rebuilt = read_age_csv(str(out_raw / "age_parsed.csv.gz"))
    assert rows_direct == rows_rebuilt


def test_cli_rejects_malformed_in_audit_index_schema(tmp_path: Path) -> None:
    studies_path = tmp_path / "studies.json"
    studies_path.write_text(json.dumps({"studies": [_synthetic_study("NCT99600001")]}), encoding="utf-8")
    bad_idx = tmp_path / "bad_index.json"
    bad_idx.write_text(json.dumps({"unexpected": []}), encoding="utf-8")
    with pytest.raises(SystemExit, match="invalid outcomes index schema"):
        run_cli(["--studies", str(studies_path), "--in-audit-index", str(bad_idx), "--out", str(tmp_path / "out")])
