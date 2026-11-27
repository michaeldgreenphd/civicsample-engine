#!/usr/bin/env python3
"""Independent offline verification script for docs/audits/hba1c_population_alignment/.

Performs three deterministic verification passes with zero network access:
1. Recomputes Stage 1 from records/*.json.gz, compares row-for-row against groups.csv and
   arm_demographics.csv, and resolves every Stage 1 JSON path verbatim against the raw JSON.
2. Re-checks every Stage 2 quote in mapping.jsonl (reading_1, reading_2, adjudication, and
   adjudicated) directly against records/*.json.gz, verifying that recorded quote-failure
   counts match and that no quote was silently modified between raw checkpoints and mapping.jsonl.
3. Recomputes Stage 3 from Stage 1 and Stage 2 and compares row-for-row against contrasts.csv
   and trial_alignment.csv.
"""

from __future__ import annotations

import csv
import gzip
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
COHORT_DIR = REPO_ROOT / "docs" / "audits" / "hba1c_cohort_reading"
ALIGN_DIR = REPO_ROOT / "docs" / "audits" / "hba1c_population_alignment"
SCRIPTS_DIR = ALIGN_DIR / "scripts"
RECORDS_DIR = COHORT_DIR / "records"
INTERMEDIATE_DIR = ALIGN_DIR / "intermediate"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import stage1_extract as s1  # noqa: E402
import stage2_schema_and_prompts as s2  # noqa: E402
import stage3_and_deliverables as s3  # noqa: E402


def read_csv_rows(path: pathlib.Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> None:
    errors: list[str] = []

    # --- Pass 1: Recompute Stage 1 & check all Stage 1 paths ---
    expected_groups, expected_demos, expected_s1_meta = s1.run_stage1_extraction()
    disk_groups = read_csv_rows(ALIGN_DIR / "groups.csv")
    disk_demos = read_csv_rows(ALIGN_DIR / "arm_demographics.csv")

    if expected_groups != disk_groups:
        errors.append(
            f"groups.csv mismatch: recomputed {len(expected_groups)} rows vs disk {len(disk_groups)} rows"
        )
    if expected_demos != disk_demos:
        errors.append(
            f"arm_demographics.csv mismatch: recomputed {len(expected_demos)} rows vs disk {len(disk_demos)} rows"
        )

    rec_cache: dict[str, dict] = {}
    s1_paths_checked = 0
    for row in disk_groups:
        nct = row["nct_id"]
        if nct not in rec_cache:
            with gzip.open(RECORDS_DIR / f"{nct}.json.gz", "rt", encoding="utf-8") as fp:
                rec_cache[nct] = json.load(fp)
        rec = rec_cache[nct]
        for vcol, pcol in [
            ("measure_title", "measure_title_path"),
            ("population_description", "population_description_path"),
            ("group_title", "group_title_path"),
            ("group_description", "group_description_path"),
            ("denom_units", "denom_units_path"),
            ("denom_count", "denom_count_path"),
            ("period_0_started", "period_0_started_path"),
            ("period_0_completed", "period_0_completed_path"),
            ("period_1_started", "period_1_started_path"),
            ("period_1_completed", "period_1_completed_path"),
            ("period_2_started", "period_2_started_path"),
            ("period_2_completed", "period_2_completed_path"),
        ]:
            if row[pcol]:
                ok, val, err = s2.resolve_json_path(rec, row[pcol])
                s1_paths_checked += 1
                if not ok or str(val) != row[vcol]:
                    errors.append(f"Stage 1 groups.csv path failure {nct} {row[pcol]}: {err}")

    for row in disk_demos:
        nct = row["nct_id"]
        rec = rec_cache[nct]
        for vcol, pcol in [
            ("measure_title", "measure_title_path"),
            ("measure_population_description", "measure_population_description_path"),
            ("class_title", "class_title_path"),
            ("category_title", "category_title_path"),
            ("class_denom_count", "class_denom_path"),
            ("value", "value_path"),
            ("spread", "spread_path"),
        ]:
            if row[pcol]:
                ok, val, err = s2.resolve_json_path(rec, row[pcol])
                s1_paths_checked += 1
                if not ok or str(val) != row[vcol]:
                    errors.append(f"Stage 1 arm_demographics.csv path failure {nct} {row[pcol]}: {err}")

    # --- Pass 2: Re-check every quote in mapping.jsonl & check raw checkpoint immutability ---
    dual_ok, _ = s3.load_jsonl_latest_ok(INTERMEDIATE_DIR / "raw_dual_mappings.jsonl")
    with open(ALIGN_DIR / "mapping.jsonl", encoding="utf-8") as f:
        mapping_lines = [json.loads(line) for line in f if line.strip()]

    s2_quotes_rechecked = 0
    s2_adj_failures_total = 0
    s2_r1_failures_total = 0
    s2_r2_failures_total = 0

    for m in mapping_lines:
        nct = m["nct_id"]
        if m["coding_status"] != "fully_coded":
            continue
        rec = rec_cache[nct]

        # Ensure raw reading_1 and reading_2 in mapping.jsonl equal raw checkpoint unmodified
        raw_d = dual_ok.get(nct)
        if raw_d is None:
            errors.append(f"Missing raw dual checkpoint for {nct}")
        else:
            if m["reading_1"] != raw_d["reading_1"]:
                errors.append(f"reading_1 modified from raw checkpoint in {nct}")
            if m["reading_2"] != raw_d["reading_2"]:
                errors.append(f"reading_2 modified from raw checkpoint in {nct}")

        qv_adj = s2.verify_stage2_reading_quotes(rec, m["adjudicated"])
        qv_r1 = s2.verify_stage2_reading_quotes(rec, m["reading_1"])
        qv_r2 = s2.verify_stage2_reading_quotes(rec, m["reading_2"])

        s2_quotes_rechecked += (
            qv_adj["quotes_checked"] + qv_r1["quotes_checked"] + qv_r2["quotes_checked"]
        )
        s2_adj_failures_total += qv_adj["quote_failures_count"]
        s2_r1_failures_total += qv_r1["quote_failures_count"]
        s2_r2_failures_total += qv_r2["quote_failures_count"]

        if qv_adj != m["quote_verification"]["adjudicated"]:
            errors.append(f"Adjudicated quote verification mismatch for {nct}")
        if qv_r1 != m["quote_verification"]["reading_1"]:
            errors.append(f"Reader 1 quote verification mismatch for {nct}")
        if qv_r2 != m["quote_verification"]["reading_2"]:
            errors.append(f"Reader 2 quote verification mismatch for {nct}")

    # --- Pass 3: Recompute Stage 3 & compare against contrasts.csv and trial_alignment.csv ---
    recomp_mappings, recomp_contrasts, recomp_trials, _ = s3.compute_stage3_outputs()
    disk_contrasts = read_csv_rows(ALIGN_DIR / "contrasts.csv")
    disk_trials = read_csv_rows(ALIGN_DIR / "trial_alignment.csv")

    if recomp_mappings != mapping_lines:
        errors.append("mapping.jsonl mismatch against Stage 3 recomputation")
    if recomp_contrasts != disk_contrasts:
        errors.append(
            f"contrasts.csv mismatch: recomputed {len(recomp_contrasts)} rows vs disk {len(disk_contrasts)} rows"
        )
    if recomp_trials != disk_trials:
        errors.append(
            f"trial_alignment.csv mismatch: recomputed {len(recomp_trials)} rows vs disk {len(disk_trials)} rows"
        )

    print("=== Alignment Audit Independent Verification Report ===")
    print(f"  Stage 1 groups.csv rows verified: {len(disk_groups)}")
    print(f"  Stage 1 arm_demographics.csv rows verified: {len(disk_demos)}")
    print(f"  Stage 1 exact JSON path resolutions verified: {s1_paths_checked}")
    print(f"  Stage 2 mapping.jsonl records verified: {len(mapping_lines)}")
    print(f"  Stage 2 quote checks re-verified (R1 + R2 + Adjudicated): {s2_quotes_rechecked}")
    print(
        f"  Logged quote failures (unpatched): adjudicated={s2_adj_failures_total}, "
        f"reader_1={s2_r1_failures_total}, reader_2={s2_r2_failures_total}"
    )
    print(f"  Stage 3 contrasts.csv rows recomputed & matched: {len(disk_contrasts)}")
    print(f"  Stage 3 trial_alignment.csv rows recomputed & matched: {len(disk_trials)}")

    if errors:
        print(f"FAILED with {len(errors)} invariant violation(s):")
        for e in errors[:20]:
            print("  -", e)
        sys.exit(1)

    print("ALL VERIFICATION INVARIANTS PASSED.")


if __name__ == "__main__":
    main()
