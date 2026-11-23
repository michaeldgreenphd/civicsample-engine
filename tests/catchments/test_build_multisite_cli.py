"""Tests for catchments.cli and scripts/build_multisite_catchments.py."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from catchments.cli import run_cli


def _release_payload() -> dict[str, object]:
    return {
        "extracted_at": "2026-11-22T14:00:00+00:00",
        "pipeline_commit": "deadbeef",
        "data": [
            {
                "nct_id": "NCT99610001",
                "study_sites": [
                    {"facility": "Clinic A", "city": "Boston", "state": "Massachusetts", "zip": "02114", "country": "United States"},
                    {"facility": "Clinic B", "city": "Houston", "state": "Texas", "zip": "77030", "country": "United States"},
                ],
            },
            {
                "nct_id": "NCT99610002",
                "study_sites": [
                    {"facility": "Clinic C", "city": "Boston", "state": "Massachusetts", "zip": "02114", "country": "United States"},
                    {"facility": "Clinic D", "city": "Toronto", "state": "Ontario", "zip": "M5G 2C4", "country": "Canada"},
                ],
            },
            {
                "nct_id": "NCT99999999",
                "study_sites": [
                    {"facility": "Holdout Clinic", "city": "Boston", "state": "Massachusetts", "zip": "02114", "country": "United States"},
                ],
            },
        ],
    }


def test_catchments_cli_requires_firewall_by_default(tmp_path: Path) -> None:
    rel = tmp_path / "demographics.json.gz"
    with gzip.open(rel, "wt", encoding="utf-8") as fh:
        json.dump(_release_payload(), fh)
    with pytest.raises(SystemExit, match="--in-audit-index is required"):
        run_cli(["--release", str(rel), "--release-tag", "data-2026-11-22", "--footprint-only", "--out", str(tmp_path / "out")])


def test_catchments_cli_footprint_only_and_in_audit_exclusion(tmp_path: Path) -> None:
    rel = tmp_path / "demographics.json.gz"
    with gzip.open(rel, "wt", encoding="utf-8") as fh:
        json.dump(_release_payload(), fh)
    idx = tmp_path / "index.json"
    idx.write_text(
        json.dumps(
            {
                "rows": [
                    {"nct_id": "NCT99610001", "in_audit": False},
                    {"nct_id": "NCT99999999", "in_audit": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    rc = run_cli(
        [
            "--release",
            str(rel),
            "--release-tag",
            "data-2026-11-22",
            "--in-audit-index",
            str(idx),
            "--footprint-only",
            "--out",
            str(out_dir),
        ]
    )
    assert rc == 0
    summary = json.loads((out_dir / "footprint_summary.json").read_text(encoding="utf-8"))
    assert summary["n_input_records"] == 3
    assert summary["n_excluded_in_audit"] == 1
    assert summary["n_evaluated"] == 2
    assert summary["footprint_tier_counts"]["multi_site_us_only"] == 1
    assert summary["footprint_tier_counts"]["mixed_us_and_international"] == 1
    assert summary["location_source_counts"]["study_sites_projection"] == 2
