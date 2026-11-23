"""Round-1 adversarial red-team and property/interval fuzz tests for age/.

Covers:
  1. 540 synthetic individual-patient age cohorts (NCT99010001..NCT99010540)
     testing the Fundamental Soundness Invariant across aligned, straddling,
     neonatal multi-unit, percentage-rounded, and gapped bracket schemes, plus
     boundary rounding / floating-point tie checks in compare_age_to_acs.
  2. Bracket boundary off-by-one, inclusivity, decimal upper bounds, multi-unit
     tiling, customized multi-category rows, and overlapping bracket traps.
  3. Baseline measure title false-positive & excluded-kind traps, mixed
     Gestational + chronological Age trials, and multi-measure double-counting
     and ranking traps.
  4. Continuous summary & dispersion formats (FULL_RANGE, INTER_QUARTILE_RANGE,
     dispersionValue, inline comma pairs, zero-width ranges, negative/inverted
     ranges), group-selection hijacking, cross-measure consistency, and
     Pigeonhole / cross-modal eligibility-vs-enrolled auditing.
  5. CSV & JSONL (.gz) round-trips, raw-envelope re-parse parity across all 7
     age_report_status states, strict json.dumps(..., allow_nan=False) safety
     across malformed inputs, negative enrollment handling, and README parity.
"""

from __future__ import annotations

import json
import math
import random
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import age  # noqa: E402
from age import parser as ap  # noqa: E402
from age import representation as ar  # noqa: E402
from age import table as at  # noqa: E402
from census import acs  # noqa: E402

SNAP = "2026-11-23"


# ---------------------------------------------------------------------------
# Helpers for synthetic studies, measures, and ACS B01001 fixtures
# ---------------------------------------------------------------------------


def _make_cat_measure(
    title: str,
    rows: list[tuple[str, str | int | float]],
    *,
    param_type: str = "COUNT_OF_PARTICIPANTS",
    unit: str = "Participants",
    group_id: str = "BG000",
    denoms: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    m: dict[str, Any] = {
        "title": title,
        "paramType": param_type,
        "unitOfMeasure": unit,
        "classes": [
            {
                "categories": [
                    {
                        "title": lbl,
                        "measurements": [{"groupId": group_id, "value": str(val)}],
                    }
                    for lbl, val in rows
                ]
            }
        ],
    }
    if denoms is not None:
        m["denoms"] = denoms
    return m


def _make_cont_measure(
    title: str = "Age, Continuous",
    *,
    param_type: str | None = "MEAN",
    dispersion_type: str | None = "STANDARD_DEVIATION",
    unit: str | None = "years",
    value: Any = "54.5",
    spread: Any = None,
    lower: Any = None,
    upper: Any = None,
    dispersion_value: Any = None,
    group_id: str = "BG000",
) -> dict[str, Any]:
    mm: dict[str, Any] = {"groupId": group_id}
    if value is not None:
        mm["value"] = str(value)
    if spread is not None:
        mm["spread"] = str(spread)
    if lower is not None:
        mm["lowerLimit"] = str(lower)
    if upper is not None:
        mm["upperLimit"] = str(upper)
    if dispersion_value is not None:
        mm["dispersionValue"] = str(dispersion_value)
    m: dict[str, Any] = {
        "title": title,
        "classes": [{"categories": [{"measurements": [mm]}]}],
    }
    if param_type is not None:
        m["paramType"] = param_type
    if dispersion_type is not None:
        m["dispersionType"] = dispersion_type
    if unit is not None:
        m["unitOfMeasure"] = unit
    return m


def _make_study(
    nct_id: str,
    measures: Any,
    *,
    enrollment: Any = 100,
    min_age: str | None = "0 Years",
    max_age: str | None = "N/A",
    std_ages: list[str] | None = None,
    groups: list[dict[str, Any]] | None = None,
    denoms: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    elig: dict[str, Any] = {}
    if min_age is not None:
        elig["minimumAge"] = min_age
    if max_age is not None:
        elig["maximumAge"] = max_age
    if std_ages is not None:
        elig["stdAges"] = std_ages

    design: dict[str, Any] = {}
    if enrollment is not None:
        design["enrollmentInfo"] = {"count": enrollment, "type": "ACTUAL"}

    bm: dict[str, Any] = {"measures": measures}
    if groups is not None:
        bm["groups"] = groups
    if denoms is not None:
        bm["denoms"] = denoms

    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id},
            "designModule": design,
            "eligibilityModule": elig,
        },
        "resultsSection": {
            "baselineCharacteristicsModule": bm,
        },
    }


def _make_acs_level_from_bands(
    geoid: str,
    male_14: list[float],
    female_14: list[float] | None = None,
    moe_fraction: float = 0.05,
) -> acs.Level:
    edges: list[tuple[float, float | None]] = [
        (0.0, 5.0),
        (5.0, 10.0),
        (10.0, 15.0),
        (15.0, 18.0),
        (18.0, 20.0),
        (20.0, 25.0),
        (25.0, 30.0),
        (30.0, 35.0),
        (35.0, 45.0),
        (45.0, 55.0),
        (55.0, 65.0),
        (65.0, 75.0),
        (75.0, 85.0),
        (85.0, None),
    ]
    f_14 = male_14 if female_14 is None else female_14
    est = np.zeros((1, len(acs.TABLE_ORDER), 2, len(edges)), dtype=float)
    moe = np.zeros_like(est)
    t_tot = acs.TABLE_ORDER.index("total")
    est[0, t_tot, 0, :] = np.array(male_14, dtype=float)
    est[0, t_tot, 1, :] = np.array(f_14, dtype=float)
    moe[0, t_tot, 0, :] = est[0, t_tot, 0, :] * moe_fraction
    moe[0, t_tot, 1, :] = est[0, t_tot, 1, :] * moe_fraction
    return acs.Level([geoid], ["Synthetic Area"], edges, est, moe)


def _true_band_counts(
    ages: list[float],
) -> dict[str, int]:
    return {
        "pediatric_under_18": sum(1 for a in ages if 0.0 <= a < 18.0),
        "working_age_18_64": sum(1 for a in ages if 18.0 <= a < 65.0),
        "older_adults_65_plus": sum(1 for a in ages if a >= 65.0),
        "older_adults_75_plus": sum(1 for a in ages if a >= 75.0),
    }


def _sample_patient_ages(rng: random.Random, n: int, profile: int) -> list[float]:
    ages: list[float] = []
    for _ in range(n):
        if profile == 0:
            ages.append(rng.uniform(0.0, 105.0))
        elif profile == 1:
            ages.append(rng.uniform(18.0, 92.0))
        elif profile == 2:
            # Pediatric-heavy including neonates (<28 days) and infants (<2 years)
            r = rng.random()
            if r < 0.25:
                ages.append(rng.uniform(0.0, 27.9 / 365.0))
            elif r < 0.55:
                ages.append(rng.uniform(28.0 / 365.0, 1.999))
            elif r < 0.85:
                ages.append(rng.uniform(2.0, 17.999))
            else:
                ages.append(rng.uniform(18.0, 85.0))
        elif profile == 3:
            # Concentrated near 18, 65, and 75 cutpoints
            center = rng.choice([17.95, 18.0, 64.95, 65.0, 65.4, 74.95, 75.0, 75.5])
            ages.append(max(0.0, min(105.0, center + rng.uniform(-0.4, 0.4))))
        else:
            # Older-adult skewed cohort
            ages.append(rng.uniform(55.0, 98.0))
    return ages


def _assert_soundness_for_cohort(
    nct_id: str,
    ages: list[float],
    n_unknown: int,
    scheme: list[tuple[str, float, float | None]],
    *,
    include_cont: bool = False,
) -> None:
    rows: list[tuple[str, int]] = []
    binned_total = 0
    for label, lo, hi in scheme:
        cnt = sum(1 for a in ages if a >= lo and (hi is None or a < hi))
        rows.append((label, cnt))
        binned_total += cnt
    assert binned_total == len(ages), f"{nct_id}: test scheme failed to partition ages"

    if n_unknown > 0:
        rows.append(("Unknown", n_unknown))

    total_denom = len(ages) + n_unknown
    denoms = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": str(total_denom)}]}]
    measures: list[dict[str, Any]] = [_make_cat_measure("Age, Customized", rows, denoms=denoms)]
    if include_cont and ages:
        mean_v = sum(ages) / len(ages)
        measures.append(
            _make_cont_measure(
                "Age, Continuous",
                param_type="MEAN",
                dispersion_type="FULL_RANGE",
                unit="years",
                value=f"{mean_v:.6f}",
                lower=f"{min(ages):.6f}",
                upper=f"{max(ages):.6f}",
            )
        )

    study = _make_study(
        nct_id,
        measures,
        enrollment=total_denom + 5,
        min_age="0 Years",
        max_age="N/A",
        denoms=denoms,
    )
    row = at.build_age_row(study, SNAP)
    expected_status = "reported_both" if (include_cont and ages) else "reported_categorical_only"
    assert row["age_report_status"] == expected_status, f"{nct_id}: got {row['age_report_status']}"
    assert json.dumps(row, allow_nan=False)

    std_bounds = ar.standard_age_band_bounds(row)
    true_counts = _true_band_counts(ages)
    tol = 1e-11

    for band_name, true_in in true_counts.items():
        b = std_bounds[band_name]
        true_pct_a = (100.0 * true_in) / total_denom
        true_pct_b = (100.0 * true_in) / len(ages)

        assert b["min_count"] <= true_in + tol, f"{nct_id} {band_name}: min_count > true_in"
        assert true_in <= b["max_count"] + tol, f"{nct_id} {band_name}: true_in > max_count"
        assert b["floor_pct"] <= true_pct_a + tol, (
            f"{nct_id} {band_name}: floor_pct {b['floor_pct']} > true_pct_a {true_pct_a}"
        )
        assert true_pct_a <= b["ceiling_pct"] + tol, (
            f"{nct_id} {band_name}: true_pct_a {true_pct_a} > ceiling_pct {b['ceiling_pct']}"
        )

        cb = b["of_classified"]["classified_bounds"]
        assert cb["min_count"] <= true_in + tol
        assert true_in <= cb["max_count"] + tol
        assert cb["floor_pct"] <= true_pct_b + tol, (
            f"{nct_id} {band_name}: Denom B floor {cb['floor_pct']} > true {true_pct_b}"
        )
        assert true_pct_b <= cb["ceiling_pct"] + tol, (
            f"{nct_id} {band_name}: Denom B true {true_pct_b} > ceiling {cb['ceiling_pct']}"
        )

    aud = ar.audit_eligibility_vs_enrolled(study, row)
    if true_counts["older_adults_65_plus"] > 0:
        assert aud["zero_enrolled_65_plus"] is False, f"{nct_id}: false zero_enrolled_65_plus"
    if true_counts["older_adults_75_plus"] > 0:
        assert aud["zero_enrolled_75_plus"] is False, f"{nct_id}: false zero_enrolled_75_plus"
    if true_counts["pediatric_under_18"] > 0:
        assert aud["zero_enrolled_pediatric"] is False, f"{nct_id}: false zero_enrolled_pediatric"


# ===========================================================================
# 1. Interval Bounds & Straddle Property Fuzzing (540 synthetic cohorts:
#    NCT99010001 .. NCT99010540) + Rounding / Tie Boundary Checks
# ===========================================================================


def test_fuzz_120_aligned_cutpoint_cohorts_soundness() -> None:
    """NCT99010001..NCT99010120: 120 synthetic patient cohorts on aligned cutpoints."""
    rng = random.Random(20261101)
    aligned_schemes: list[list[tuple[str, float, float | None]]] = [
        [("< 18 years", 0.0, 18.0), ("18 to 64 years", 18.0, 65.0), ("65 to 74 years", 65.0, 75.0), (">= 75 years", 75.0, None)],
        [("<= 17 years", 0.0, 18.0), ("18-64 years", 18.0, 65.0), (">= 65 years", 65.0, None)],
        [("<= 64 years", 0.0, 65.0), (">= 65 years", 65.0, None)],
        [("< 65 years", 0.0, 65.0), ("65 to 74 years", 65.0, 75.0), ("75 years and older", 75.0, None)],
    ]
    for idx in range(1, 121):
        nct_id = f"NCT{99010000 + idx}"
        n_patients = rng.randint(1, 180)
        n_unk = rng.choice([0, 0, 1, 5, 19])
        ages = _sample_patient_ages(rng, n_patients, idx % 5)
        scheme = aligned_schemes[idx % len(aligned_schemes)]
        _assert_soundness_for_cohort(nct_id, ages, n_unk, scheme, include_cont=(idx % 2 == 0))


def test_fuzz_140_straddling_cutpoint_cohorts_soundness() -> None:
    """NCT99010121..NCT99010260: 140 synthetic patient cohorts on straddling brackets."""
    rng = random.Random(20261102)
    straddle_schemes: list[list[tuple[str, float, float | None]]] = [
        [("0-21 years", 0.0, 22.0), ("22-69 years", 22.0, 70.0), ("70+ years", 70.0, None)],
        [("<= 65 years", 0.0, 66.0), ("> 65 years", 66.0, None)],
        [("0 to 14 years", 0.0, 15.0), ("15 to 24 years", 15.0, 25.0), ("25 to 67 years", 25.0, 68.0), ("68 to 79 years", 68.0, 80.0), (">= 80 years", 80.0, None)],
        [("< 50 years", 0.0, 50.0), ("50 to 70 years", 50.0, 71.0), (">= 71 years", 71.0, None)],
        [("0 to 19 years", 0.0, 20.0), ("20 to 76 years", 20.0, 77.0), (">= 77 years", 77.0, None)],
    ]
    for idx in range(121, 261):
        nct_id = f"NCT{99010000 + idx}"
        n_patients = rng.randint(1, 200)
        n_unk = rng.choice([0, 2, 11])
        ages = _sample_patient_ages(rng, n_patients, idx % 5)
        scheme = straddle_schemes[idx % len(straddle_schemes)]
        _assert_soundness_for_cohort(nct_id, ages, n_unk, scheme, include_cont=(idx % 3 == 0))


def test_fuzz_120_neonatal_multi_unit_cohorts_soundness() -> None:
    """NCT99010261..NCT99010380: 120 synthetic cohorts with neonatal/pediatric multi-unit brackets."""
    rng = random.Random(20261103)
    d28 = 28.0 / 365.0
    neonatal_schemes: list[list[tuple[str, float, float | None]]] = [
        [("0-27 days", 0.0, d28), ("28 days to 23 months", d28, 2.0), ("2-17 years", 2.0, 18.0), (">=18 years", 18.0, None)],
        [("0 to 27 days", 0.0, d28), ("28 days to 11 months", d28, 1.0), ("1 to 11 years", 1.0, 12.0), ("12 to 17 years", 12.0, 18.0), ("18 to 64 years", 18.0, 65.0), (">= 65 years", 65.0, None)],
        [("0 to 51 weeks", 0.0, 1.0), ("1 to 17 years", 1.0, 18.0), ("18 to 74 years", 18.0, 75.0), (">= 75 years", 75.0, None)],
    ]
    for idx in range(261, 381):
        nct_id = f"NCT{99010000 + idx}"
        n_patients = rng.randint(2, 160)
        n_unk = rng.choice([0, 3, 7])
        ages = _sample_patient_ages(rng, n_patients, 2 if idx % 2 == 0 else idx % 5)
        scheme = neonatal_schemes[idx % len(neonatal_schemes)]
        _assert_soundness_for_cohort(nct_id, ages, n_unk, scheme, include_cont=(idx % 2 == 1))


def test_fuzz_80_percentage_rounded_cohorts_conversion_bounds() -> None:
    """NCT99010381..NCT99010460: 80 percentage-reported cohorts with rounding & denom guards."""
    rng = random.Random(20261104)
    scheme: list[tuple[str, float, float | None]] = [
        ("< 18 years", 0.0, 18.0),
        ("18 to 64 years", 18.0, 65.0),
        ("65 to 74 years", 65.0, 75.0),
        (">= 75 years", 75.0, None),
    ]
    for idx in range(381, 461):
        nct_id = f"NCT{99010000 + idx}"
        n_patients = rng.randint(15, 250)
        n_unk = rng.choice([0, 4])
        ages = _sample_patient_ages(rng, n_patients, idx % 5)
        tot = n_patients + n_unk
        decimals = rng.choice([1, 2, 4])

        pct_rows: list[tuple[str, str]] = []
        for label, lo, hi in scheme:
            cnt = sum(1 for a in ages if a >= lo and (hi is None or a < hi))
            pct_rows.append((label, f"{round(100.0 * cnt / tot, decimals):.{decimals}f}"))
        if n_unk > 0:
            pct_rows.append(("Unknown", f"{round(100.0 * n_unk / tot, decimals):.{decimals}f}"))

        denoms = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": str(tot)}]}]
        m_pct = _make_cat_measure("Age, Categorical", pct_rows, param_type="PERCENTAGE", unit="%", denoms=denoms)
        p = ap.parse_trial([m_pct], enrollment=tot)
        assert ap.classify_age_reporting(p) == "reported_categorical_only"
        assert p.flag_percentage_converted is True
        # Each converted row can drift by at most 0.5 + 0.5 * 10^(-decimals) * tot / 100 participants
        max_row_err = 0.5 + (0.5 * (10.0 ** (-decimals)) * tot / 100.0) + 1e-6
        for (lbl, lo, hi), pb in zip(scheme, p.parsed_brackets):
            true_cnt = sum(1 for a in ages if a >= lo and (hi is None or a < hi))
            assert abs(pb.count - true_cnt) <= max_row_err, f"{nct_id} {lbl}: {pb.count} vs {true_cnt}"


def test_fuzz_80_gapped_omitted_bin_cohorts_soundness_defect() -> None:
    """DEFECT: NCT99010461..NCT99010540 (80 cohorts) with omitted age bins violate soundness!

    When a registrant omits an age bin (e.g. posts ['0-17 years', '>=65 years'] omitting
    '18-64 years', or ['18-64 years', '> 65 years'] omitting 65-year-olds, even when
    baseline_denoms and a 'Total' row explicitly report all N participants), parse_trial
    classifies the trial as 'reported_categorical_only' while age_range_bounds excludes the
    true cohort share from [floor_pct, ceiling_pct] and audit_eligibility_vs_enrolled
    falsely asserts zero_enrolled_65_plus == True when >=65 patients sit in the omitted bin!
    """
    rng = random.Random(20261105)
    soundness_violations: list[str] = []

    for idx in range(461, 541):
        nct_id = f"NCT{99010000 + idx}"
        if idx % 2 == 0:
            # Cohort has working-age AND 65.5-year-old seniors, registrant posts "18-64" and "> 65"
            # (omitting [65, 66) where all seniors sit!), plus explicit Total and Participants denom.
            n_young = rng.randint(20, 60)
            n_age65 = rng.randint(5, 25)
            ages = [rng.uniform(18.0, 64.9) for _ in range(n_young)] + [
                rng.uniform(65.0, 65.95) for _ in range(n_age65)
            ]
            rows: list[tuple[str, int]] = [
                ("18 to 64 years", n_young),
                ("> 65 years", 0),
                ("Total", len(ages)),
            ]
        else:
            # Cohort spans pediatric, working-age, and >=65; registrant omits "18 to 64 years" row
            n_ped = rng.randint(10, 30)
            n_wrk = rng.randint(20, 50)
            n_sen = rng.randint(10, 30)
            ages = (
                [rng.uniform(1.0, 17.9) for _ in range(n_ped)]
                + [rng.uniform(18.0, 64.9) for _ in range(n_wrk)]
                + [rng.uniform(65.0, 90.0) for _ in range(n_sen)]
            )
            rows = [
                ("0 to 17 years", n_ped),
                (">= 65 years", n_sen),
                ("Total", len(ages)),
            ]

        tot = len(ages)
        denoms = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": str(tot)}]}]
        study = _make_study(
            nct_id,
            [_make_cat_measure("Age, Customized", rows, denoms=denoms)],
            enrollment=tot,
            min_age="0 Years",
            max_age="N/A",
            denoms=denoms,
        )
        row = at.build_age_row(study, SNAP)
        if row["age_report_status"] in ("reported_both", "reported_categorical_only"):
            std_b = ar.standard_age_band_bounds(row)
            true_c = _true_band_counts(ages)
            for band_name, true_in in true_c.items():
                b = std_b[band_name]
                true_pct_a = (100.0 * true_in) / tot
                if not (b["floor_pct"] - 1e-9 <= true_pct_a <= b["ceiling_pct"] + 1e-9):
                    soundness_violations.append(
                        f"{nct_id}:{band_name} true={true_pct_a:.2f}% not in [{b['floor_pct']:.2f}%, {b['ceiling_pct']:.2f}%]"
                    )
            aud = ar.audit_eligibility_vs_enrolled(study, row)
            if true_c["older_adults_65_plus"] > 0 and aud["zero_enrolled_65_plus"] is True:
                soundness_violations.append(f"{nct_id}: false zero_enrolled_65_plus=True")

    assert not soundness_violations, (
        f"{len(soundness_violations)} soundness/false-zero violations across 80 gapped cohorts "
        f"(first 3: {soundness_violations[:3]})"
    )


def test_direction_settled_false_positive_on_exact_rational_tie_1_of_7() -> None:
    """DEFECT (age/representation.py:164,544,572): exact 1/7 tie falsely settles direction!

    Both the trial (1 of 7 in >=65, 6 of 7 in 18-64) and ACS B01001 (1 of 7 in >=65,
    6 of 7 in 18-64) have the exact mathematical share 1/7. Because age_range_bounds
    computes (100.0 * 1.0) / 7.0 = 14.285714285714286 while compare_age_to_acs computes
    100.0 * (1.0 / 7.0) = 14.285714285714285, floor_pct > expected_pct evaluates True!
    """
    m_14 = [0.0] * 14
    m_14[4] = 6.0   # [18, 20): 6
    m_14[11] = 1.0  # [65, 75): 1 -> total adult >=18 = 7, >=65 share = 1/7
    lvl = _make_acs_level_from_bands("99001", m_14)

    study = _make_study(
        "NCT99020001",
        [_make_cat_measure("Age, Categorical", [("18 to 64 years", 6), ("65 to 74 years", 1)])],
        min_age="18 Years",
    )
    row = at.build_age_row(study, SNAP)
    cmp_res = ar.compare_age_to_acs(
        row, lvl, "99001", sexes=("male", "female"), eligible_lo=18.0, eligible_hi=None
    )
    b65 = cmp_res["bands"]["older_adults_65_plus"]
    assert b65["direction_settled"] is False
    assert b65["direction"] == "indeterminate_or_equal"


def test_direction_settled_false_positive_on_straddling_upper_tie_5_of_6() -> None:
    """DEFECT (age/representation.py:165,544,572): straddle interval [3/6, 5/6] vs expected 5/6!

    Trial has 1 in '18-54', 2 in '55-70' (straddling 65), and 3 in '>=71' (N=6), so its
    exact >=65 share interval is [3/6, 5/6] = [50%, 83.333333...%], which CONTAINS 5/6.
    ACS expected >=65 share is 5/6. Because (100.0 * 5.0) / 6.0 < 100.0 * (5.0 / 6.0) in
    IEEE-754 float64, ceiling_pct < expected_pct falsely evaluates to True ('below_expected')!
    """
    m_14 = [0.0] * 14
    m_14[4] = 1.0   # [18, 20): 1
    m_14[11] = 5.0  # [65, 75): 5 -> adult >=18 total = 6, expected >=65 = 5/6
    lvl = _make_acs_level_from_bands("99002", m_14)

    p = ap.parse_trial(
        [
            _make_cat_measure(
                "Age, Customized",
                [("18 to 54 years", 1), ("55 to 70 years", 2), (">= 71 years", 3)],
            )
        ]
    )
    cmp_res = ar.compare_age_to_acs(
        p, lvl, "99002", sexes=("male", "female"), eligible_lo=18.0, eligible_hi=None
    )
    b65 = cmp_res["bands"]["older_adults_65_plus"]
    assert b65["direction_settled"] is False
    assert b65["direction"] == "indeterminate_or_equal"


def test_direction_settled_near_tie_strictly_outside_vs_containing() -> None:
    """Near-tie boundary verification when expected_pct is genuinely inside vs outside bounds."""
    m_14 = [0.0] * 14
    m_14[4] = 750.0
    m_14[11] = 250.0  # exact 25.0% in >=65
    lvl = _make_acs_level_from_bands("99003", m_14)

    # [24.9%, 25.1%] contains 25.0% -> unsettled
    p_in = ap.parse_trial(
        [
            _make_cat_measure(
                "Age, Customized",
                [("18 to 54 years", 749), ("55 to 70 years", 2), (">= 71 years", 249)],
            )
        ]
    )
    r_in = ar.compare_age_to_acs(
        p_in, lvl, "99003", sexes=("male", "female"), eligible_lo=18.0, eligible_hi=None
    )
    assert r_in["bands"]["older_adults_65_plus"]["direction_settled"] is False

    # [24.8%, 24.9%] < 25.0% -> settled below_expected
    p_below = ap.parse_trial(
        [
            _make_cat_measure(
                "Age, Customized",
                [("18 to 54 years", 751), ("55 to 70 years", 1), (">= 71 years", 248)],
            )
        ]
    )
    r_below = ar.compare_age_to_acs(
        p_below, lvl, "99003", sexes=("male", "female"), eligible_lo=18.0, eligible_hi=None
    )
    assert r_below["bands"]["older_adults_65_plus"]["direction_settled"] is True
    assert r_below["bands"]["older_adults_65_plus"]["direction"] == "below_expected"


# ===========================================================================
# 2. Bracket Boundary Off-by-One, Inclusivity, Decimal & Multi-Unit Traps
# ===========================================================================


@pytest.mark.parametrize(
    ("lo_lbl", "hi_lbl", "seam"),
    [
        ("<= 64 years", ">= 65 years", 65.0),
        ("< 65 years", ">= 65 years", 65.0),
        ("<= 65 years", "> 65 years", 66.0),
        ("<= 17 years", ">= 18 years", 18.0),
        ("< 18 years", ">= 18 years", 18.0),
        ("0 to 23 months", "2 to 17 years", 2.0),
        ("<= 23 months", "2 to 11 years", 2.0),
    ],
)
def test_paired_boundary_brackets_tile_with_zero_overlap_and_zero_gap(
    lo_lbl: str, hi_lbl: str, seam: float
) -> None:
    p = ap.parse_trial([_make_cat_measure("Age Group", [(lo_lbl, 45), (hi_lbl, 55)])])
    assert p.brackets_tile_cleanly is True
    assert p.flag_overlapping_brackets is False
    assert p.flag_gap_between_brackets is False
    assert p.parsed_brackets[0].hi_years == pytest.approx(seam)
    assert p.parsed_brackets[1].lo_years == pytest.approx(seam)


def test_le_65_years_straddles_65_cutoff_while_le_64_is_exact() -> None:
    """'<= 65 years' ([0, 66)) includes age 65.5 and MUST straddle [65, None) (cutpoint_exact=False)."""
    p_strad = ap.parse_trial([_make_cat_measure("Age Group", [("<= 65 years", 70), ("> 65 years", 30)])])
    b65_s = ar.age_range_bounds(p_strad.parsed_brackets, p_strad.n_unknown, 65.0, None)
    assert b65_s["has_straddling_bracket"] is True
    assert b65_s["cutpoint_exact"] is False
    assert b65_s["straddling_brackets"] == ["<= 65 years"]
    assert b65_s["min_count"] == 30.0
    assert b65_s["max_count"] == 100.0
    assert b65_s["of_classified"]["point_pct"] is None

    p_exact = ap.parse_trial([_make_cat_measure("Age Group", [("<= 64 years", 70), (">= 65 years", 30)])])
    b65_e = ar.age_range_bounds(p_exact.parsed_brackets, p_exact.n_unknown, 65.0, None)
    assert b65_e["has_straddling_bracket"] is False
    assert b65_e["cutpoint_exact"] is True
    assert b65_e["min_count"] == 30.0
    assert b65_e["max_count"] == 30.0
    assert b65_e["of_classified"]["point_pct"] == pytest.approx(30.0)


def test_pediatric_28_days_to_23_months_and_2_to_11_years_no_gap_no_straddle() -> None:
    p = ap.parse_trial(
        [
            _make_cat_measure(
                "Age, Customized",
                [
                    ("0 to 27 days", 8),
                    ("28 days to 23 months", 17),
                    ("2 to 11 years", 35),
                    ("12 to 17 years", 20),
                    (">= 18 years", 20),
                ],
            )
        ]
    )
    assert p.brackets_tile_cleanly is True
    assert p.flag_overlapping_brackets is False
    assert p.flag_gap_between_brackets is False
    b_ped = ar.age_range_bounds(p.parsed_brackets, 0.0, 0.0, 18.0)
    assert b_ped["cutpoint_exact"] is True
    assert b_ped["min_count"] == 80.0
    assert b_ped["max_count"] == 80.0


def test_decimal_sub_18_and_sub_65_non_boundary_intervals() -> None:
    p = ap.parse_trial(
        [
            _make_cat_measure(
                "Age, Customized",
                [("0.5 to 11.5 years", 20), ("18.0 to 64.9 years", 30)],
            )
        ]
    )
    assert p.parsed_brackets[0].lo_years == pytest.approx(0.5)
    assert p.parsed_brackets[0].hi_years == pytest.approx(11.5)
    assert p.parsed_brackets[1].lo_years == pytest.approx(18.0)
    assert p.parsed_brackets[1].hi_years == pytest.approx(64.9)


def test_decimal_inclusive_upper_le_65_point_0_soundness_defect() -> None:
    """DEFECT (age/parser.py:1002,1061 & age/representation.py:88): '<= 65.0 years' excludes age 65.0!

    A participant aged exactly 65.0 years (or integer age 65 formatted as '65.0') satisfies
    age <= 65.0 and is placed into '<= 65.0 years' (not '> 65.0 years'). Because '65.0'
    contains '.', parse_interval_label maps '<= 65.0 years' to half-open [0.0, 65.0), and
    age_range_bounds classifies it as strictly outside [65.0, None), yielding max_count=0
    (ceiling_pct=0.0%) when 10 participants aged 65.0 are in [65.0, None) (true share 20%)!
    """
    p = ap.parse_trial(
        [_make_cat_measure("Age Group", [("<= 65.0 years", 50), ("> 65.0 years", 0)])]
    )
    # 40 participants aged 50.0 + 10 participants aged 65.0 -> true [65.0, None) count is 10 (20%)
    b65 = ar.age_range_bounds(p.parsed_brackets, p.n_unknown, 65.0, None)
    assert b65["max_count"] >= 10.0
    assert b65["ceiling_pct"] >= 20.0


def test_decimal_continuous_infant_brackets_tile_cleanly() -> None:
    """Continuous decimal brackets '0.0 to 1.0 years' + '1.0 to 2.0 years' tile at 1.0."""
    p = ap.parse_trial(
        [
            _make_cat_measure(
                "Age, Customized",
                [("0.0 to 1.0 years", 25), ("1.0 to 2.0 years", 35)],
            )
        ]
    )
    assert p.brackets_tile_cleanly is True
    assert p.flag_overlapping_brackets is False
    assert p.flag_gap_between_brackets is False


def test_categorical_age_in_days_or_months_title_unit_ignored_defect() -> None:
    """DEFECT (age/parser.py:1508,1562): 'Age in Days' / 'Age in Months' unitless brackets parsed as years!

    measure_kind('Age in Days') returns 'participant_baseline_age', yet parse_categorical_measure
    calls parse_interval_label(raw_lbl) with default_unit='years', turning 65-to-90-day-old
    NICU infants into 65-to-91-year-old seniors!
    """
    m_days = _make_cat_measure(
        "Age in Days",
        [("0 to 30", 25), ("31 to 64", 15), ("65 to 90", 10)],
    )
    p = ap.parse_trial([m_days])
    b65 = ar.age_range_bounds(p.parsed_brackets, p.n_unknown, 65.0, None)
    b_ped = ar.age_range_bounds(p.parsed_brackets, p.n_unknown, 0.0, 18.0)
    assert b65["max_count"] == 0.0
    assert b_ped["min_count"] == 50.0


def test_categorical_unit_of_measure_months_ignored_defect() -> None:
    """DEFECT (age/parser.py:1562): categorical unitOfMeasure='Months' ignored on unitless brackets!"""
    m_mo = _make_cat_measure(
        "Age, Categorical",
        [("0 to 5", 12), ("6 to 11", 18), ("12 to 23", 20)],
        param_type="COUNT_OF_PARTICIPANTS",
        unit="Months",
    )
    p = ap.parse_trial([m_mo])
    b_ped = ar.age_range_bounds(p.parsed_brackets, p.n_unknown, 0.0, 18.0)
    assert b_ped["min_count"] == 50.0
    assert b_ped["cutpoint_exact"] is True


def test_customized_layout_multi_category_class_overwrites_counts_defect() -> None:
    """DEFECT (age/parser.py:1394,1518-1524): customized class with 2 categories overwrites BG000!

    When a customized class ('18 to 64 years') contains two sub-categories ('Female': 30,
    'Male': 40) sharing groupId='BG000', _extract_group_cell_value overwrites by_gid['BG000']
    with the last value (40) instead of summing (70) or refusing as ambiguous!
    """
    m_cust = {
        "title": "Age, Customized",
        "paramType": "COUNT_OF_PARTICIPANTS",
        "unitOfMeasure": "Participants",
        "classes": [
            {
                "title": "18 to 64 years",
                "categories": [
                    {"title": "Female", "measurements": [{"groupId": "BG000", "value": "30"}]},
                    {"title": "Male", "measurements": [{"groupId": "BG000", "value": "40"}]},
                ],
            }
        ],
    }
    p = ap.parse_trial([m_cust])
    # Must either sum both categories (70.0) or refuse as uninformative — never silently return 40.0!
    assert p.n_classified in (70.0, None), f"silently dropped 30 participants: n_classified={p.n_classified}"


def test_overlapping_brackets_zero_vs_nonzero_counts() -> None:
    # One overlapping bracket has count == 0 -> valid, count preserved
    p_zero = ap.parse_trial(
        [_make_cat_measure("Age, Categorical", [("<= 18 years", 0), ("18 to 64 years", 62), (">= 65 years", 38)])]
    )
    assert ap.classify_age_reporting(p_zero) == "reported_categorical_only"
    assert p_zero.flag_overlapping_brackets is True
    assert p_zero.n_classified == 100.0
    b18 = ar.age_range_bounds(p_zero.parsed_brackets, 0.0, 0.0, 18.0)
    assert b18["has_zero_count_straddling_bracket"] is True
    assert b18["has_straddling_bracket"] is False
    assert b18["cutpoint_exact"] is True

    # Both overlapping brackets have count > 0 -> uninformative ("overlapping_age_brackets")
    p_pos = ap.parse_trial(
        [_make_cat_measure("Age, Categorical", [("<= 18 years", 4), ("18 to 64 years", 62), (">= 65 years", 38)])]
    )
    assert ap.classify_age_reporting(p_pos) == "uninformative"
    assert p_pos.uninformative_reason == "overlapping_age_brackets"
    assert p_pos.n_classified is None
    assert p_pos.n_total_parsed is None
    assert p_pos.parsed_brackets == []


# ===========================================================================
# 3. Title & Subgroup Measure Selection & Multi-Measure Traps
# ===========================================================================


@pytest.mark.parametrize(
    ("title", "expected_kind"),
    [
        ("Stage of Disease", "not_age"),
        ("Dosage Regimen", "not_age"),
        ("Percent Coverage", "not_age"),
        ("Gastric Hemorrhage", "not_age"),
        ("Storage Duration", "not_age"),
        ("Marriage Status", "not_age"),
        ("Bone Age", "gestational_or_developmental_age"),
        ("Gestational Age at Birth", "gestational_or_developmental_age"),
        ("Postmenstrual Age", "gestational_or_developmental_age"),
        ("Corrected Age", "gestational_or_developmental_age"),
        ("Maternal Age", "non_participant_role_age"),
        ("Caregiver Age", "non_participant_role_age"),
        ("Donor Age", "non_participant_role_age"),
        ("Age at Diagnosis", "disease_history_age"),
        ("Age at Onset", "disease_history_age"),
        ("Age, Female", "subgroup_stratified_age"),
        ("Age, Male", "subgroup_stratified_age"),
    ],
)
def test_measure_kind_prompt_canonical_traps(title: str, expected_kind: str) -> None:
    assert ap.measure_kind(title) == expected_kind


@pytest.mark.parametrize(
    ("tricky_title", "expected_kind"),
    [
        ("Age (years) at Diagnosis", "disease_history_age"),
        ("Age (years) at Onset", "disease_history_age"),
        ("Age (years) of Donor", "non_participant_role_age"),
        ("Age (years) of Mother", "non_participant_role_age"),
        ("Age (years), Caregivers", "non_participant_role_age"),
        ("Age (weeks), Gestational", "gestational_or_developmental_age"),
    ],
)
def test_measure_kind_unit_interposed_excluded_titles_defect(
    tricky_title: str, expected_kind: str
) -> None:
    """DEFECT (age/parser.py:249-301): unit in parens after 'Age' bypasses excluded-kind regexes!

    'Age (years) at Diagnosis' and 'Age (years) of Donor' fail _DISEASE_HISTORY_RX /
    _NON_PARTICIPANT_ROLE_RX and match _PRIMARY_BASELINE_AGE_RX ('^ages?\\s*\\(\\s*years?.*$'),
    polluting participant_baseline_age with disease-onset or donor/maternal ages!
    """
    assert ap.measure_kind(tricky_title) == expected_kind


@pytest.mark.parametrize(
    "plural_role_title",
    [
        "Age, Caregivers",
        "Age: Donors",
        "Age, Mothers",
        "Age, Parents",
    ],
)
def test_measure_kind_plural_non_participant_role_suffix_defect(plural_role_title: str) -> None:
    """DEFECT (age/parser.py:264-265): plural role suffixes fall through to 'not_age' instead of excluded!"""
    assert ap.measure_kind(plural_role_title) == "non_participant_role_age"


def test_both_gestational_age_and_chronological_age_on_same_trial() -> None:
    """When both 'Gestational Age' and 'Age' are posted, 'Age' is parsed cleanly and 'Gestational Age' is audited."""
    m_gest = _make_cont_measure("Gestational Age", unit="weeks", value="39.0", spread="1.2")
    m_age_cat = _make_cat_measure(
        "Age",
        [("0 to 27 days", 15), ("28 days to 23 months", 35)],
    )
    m_age_cont = _make_cont_measure("Age", unit="months", value="6.0", spread="4.0")
    study = _make_study("NCT99030001", [m_gest, m_age_cat, m_age_cont], enrollment=50)
    row = at.build_age_row(study, SNAP)
    assert row["age_report_status"] == "reported_both"
    assert row["n_selected_measures"] == 2
    assert row["n_excluded_age_measures"] == 1
    assert row["excluded_age_measure_titles"] == ["Gestational Age"]
    assert row["excluded_age_measure_kinds"] == [
        {"title": "Gestational Age", "kind": "gestational_or_developmental_age"}
    ]
    assert row["n_classified"] == 50.0
    assert row["n_total_parsed"] == 50.0
    assert row["mean_years"] == pytest.approx(0.5)


def test_multiple_categorical_measures_recounting_same_100_never_double_counts() -> None:
    """Two full categorical tables recounting the same 100 participants pick finer clean table, never 200."""
    m_coarse = _make_cat_measure(
        "Age, Categorical",
        [("< 18 years", 10), ("18 to 64 years", 60), (">= 65 years", 30)],
    )
    m_fine = _make_cat_measure(
        "Age, Customized",
        [
            ("< 18 years", 10),
            ("18 to 39 years", 25),
            ("40 to 64 years", 35),
            ("65 to 74 years", 20),
            (">= 75 years", 10),
        ],
    )
    p = ap.parse_trial([m_coarse, m_fine], enrollment=100)
    assert p.n_classified == 100.0
    assert p.n_total_parsed == 100.0
    assert p.flag_exceeds_enrollment is False
    assert p.categorical_measure_title == "Age, Customized"
    assert len(p.parsed_brackets) == 5


def test_multi_categorical_ranking_prefers_single_bracket_subset_over_full_cohort_defect() -> None:
    """DEFECT (age/parser.py:2219): _cat_rank_key ranks single-bracket subset (N=30) above full cohort (N=100)!

    Standard CT.gov 3-row 'Age, Categorical' ('<=18': 0, '18-64': 70, '>=65': 30 -> N=100)
    has brackets_tile_cleanly=False due to zero-count '<=18' overlap. A companion 'Age, Customized'
    indicator row ('>=65 years': 30 -> N=30) has 1 bracket so brackets_tile_cleanly=True.
    Ranking `tiles` ahead of `n_classified` discards 70% of the cohort and claims 100% are >=65!
    """
    m_full = _make_cat_measure(
        "Age, Categorical",
        [("<= 18 years", 0), ("18 to 64 years", 70), (">= 65 years", 30)],
    )
    m_subset = _make_cat_measure("Age, Customized", [(">= 65 years", 30)])
    p = ap.parse_trial([m_full, m_subset], enrollment=100)
    assert p.n_classified == 100.0
    b65 = ar.age_range_bounds(p.parsed_brackets, p.n_unknown, 65.0, None)
    assert b65["floor_pct"] == pytest.approx(30.0)


def test_multi_categorical_unselected_measure_contaminates_primary_flags_defect() -> None:
    """DEFECT (age/parser.py:2318-2324): unselected COUNT_OF_UNITS / overlapping table pollutes clean primary flags!"""
    m_clean = _make_cat_measure("Age, Categorical", [("18 to 64 years", 70), (">= 65 years", 30)])
    m_eyes = _make_cat_measure(
        "Age, Customized",
        [("18 to 64 years", 140), (">= 65 years", 60)],
        param_type="COUNT_OF_UNITS",
        unit="Eyes",
    )
    p = ap.parse_trial([m_clean, m_eyes], enrollment=100)
    assert p.is_participant_count is True
    assert p.flag_nonparticipant_units is False


# ===========================================================================
# 4. Continuous Summary, Dispersion & Eligibility-vs-Enrolled Edge Cases
# ===========================================================================


def test_continuous_zero_width_range_45_to_45_via_limits_valid() -> None:
    """All participants aged 45 -> valid zero-width range min_years == max_years == 45.0."""
    m = _make_cont_measure(
        "Age",
        param_type="MEDIAN",
        dispersion_type="FULL_RANGE",
        unit="years",
        value="45",
        lower="45",
        upper="45",
    )
    c = ap.parse_continuous_measure(m)
    assert c.parse_valid is True
    assert c.min_years == pytest.approx(45.0)
    assert c.max_years == pytest.approx(45.0)
    assert c.flag_impossible_continuous_value is False


@pytest.mark.parametrize(
    ("range_str", "exp_min", "exp_max"),
    [
        ("18 to 82", 18.0, 82.0),
        ("18 - 82", 18.0, 82.0),
        ("(18, 82)", 18.0, 82.0),
        ("[18.5, 79.2]", 18.5, 79.2),
        ("18-82 years", 18.0, 82.0),
        ("45 to 45", 45.0, 45.0),
    ],
)
def test_continuous_full_range_string_formats_in_value_defect(
    range_str: str, exp_min: float, exp_max: float
) -> None:
    """DEFECT (age/parser.py:1793-1808): standalone FULL_RANGE string in value fails as all_values_na!"""
    m = _make_cont_measure(
        "Age",
        param_type="NUMBER",
        dispersion_type="FULL_RANGE",
        unit="years",
        value=range_str,
    )
    c = ap.parse_continuous_measure(m)
    assert c.parse_valid is True
    assert c.min_years == pytest.approx(exp_min)
    assert c.max_years == pytest.approx(exp_max)


@pytest.mark.parametrize(
    ("range_str", "exp_min", "exp_max"),
    [
        ("18 to 82", 18.0, 82.0),
        ("18 - 82", 18.0, 82.0),
        ("(18, 82)", 18.0, 82.0),
        ("[18.5, 79.2]", 18.5, 79.2),
        ("18-82 years", 18.0, 82.0),
        ("45 to 45", 45.0, 45.0),
    ],
)
def test_continuous_full_range_string_formats_in_spread_defect(
    range_str: str, exp_min: float, exp_max: float
) -> None:
    """DEFECT (age/parser.py:1779-1808): FULL_RANGE string formats in spread/value drop min_years & max_years!"""
    m = _make_cont_measure(
        "Age",
        param_type="MEDIAN",
        dispersion_type="FULL_RANGE",
        unit="years",
        value="45.0" if exp_min <= 45.0 <= exp_max else str((exp_min + exp_max) / 2.0),
        spread=range_str,
    )
    c = ap.parse_continuous_measure(m)
    assert c.parse_valid is True
    assert c.min_years == pytest.approx(exp_min)
    assert c.max_years == pytest.approx(exp_max)


@pytest.mark.parametrize("bad_range_str", ["-5 to 40", "82 to 18"])
def test_continuous_negative_or_inverted_full_range_string_in_spread_defect(
    bad_range_str: str,
) -> None:
    """DEFECT (age/parser.py:1788,2006): negative '-5 to 40' & inverted '82 to 18' in spread silently ignored!"""
    m = _make_cont_measure(
        "Age",
        param_type="MEDIAN",
        dispersion_type="FULL_RANGE",
        unit="years",
        value="30.0",
        spread=bad_range_str,
    )
    c = ap.parse_continuous_measure(m)
    assert c.parse_valid is False
    assert c.flag_impossible_continuous_value is True


def test_continuous_inline_comma_pair_iqr_and_full_range_defect() -> None:
    """DEFECT (age/parser.py:1741,1794): raw_v.replace(',', '') strips comma before _INLINE_RANGE_INNER_RX!"""
    c_iqr = ap.parse_continuous_measure(
        _make_cont_measure(
            "Age",
            param_type="MEDIAN",
            dispersion_type="INTER_QUARTILE_RANGE",
            unit="years",
            value="54.2 (45.0, 62.0)",
        )
    )
    assert c_iqr.parse_valid is True
    assert c_iqr.median_years == pytest.approx(54.2)
    assert c_iqr.iqr_low_years == pytest.approx(45.0)
    assert c_iqr.iqr_high_years == pytest.approx(62.0)


def test_continuous_iqr_dispersion_value_width_and_pair_defect() -> None:
    """DEFECT (age/parser.py:1787-1790): dispersionValue key ignored for INTER_QUARTILE_RANGE!"""
    c_width = ap.parse_continuous_measure(
        _make_cont_measure(
            "Age",
            param_type="MEDIAN",
            dispersion_type="INTER_QUARTILE_RANGE",
            unit="years",
            value="54.0",
            dispersion_value="12.5",
        )
    )
    assert c_width.iqr_width_years == pytest.approx(12.5)

    c_pair = ap.parse_continuous_measure(
        _make_cont_measure(
            "Age",
            param_type="MEDIAN",
            dispersion_type="INTER_QUARTILE_RANGE",
            unit="years",
            value="54.0",
            dispersion_value="(45.0, 62.0)",
        )
    )
    assert c_pair.iqr_low_years == pytest.approx(45.0)
    assert c_pair.iqr_high_years == pytest.approx(62.0)


def test_continuous_iqr_via_spread_and_limits_standard_keys() -> None:
    c_w = ap.parse_continuous_measure(
        _make_cont_measure(
            "Age",
            param_type="MEDIAN",
            dispersion_type="INTER_QUARTILE_RANGE",
            unit="years",
            value="54.0",
            spread="12.5",
        )
    )
    assert c_w.iqr_width_years == pytest.approx(12.5)
    assert c_w.iqr_low_years is None

    c_lu = ap.parse_continuous_measure(
        _make_cont_measure(
            "Age",
            param_type="MEDIAN",
            dispersion_type="INTER_QUARTILE_RANGE",
            unit="years",
            value="54.0",
            lower="45.0",
            upper="62.0",
        )
    )
    assert c_lu.iqr_low_years == pytest.approx(45.0)
    assert c_lu.iqr_high_years == pytest.approx(62.0)
    assert c_lu.iqr_width_years == pytest.approx(17.0)


def test_continuous_explicit_total_group_hijacks_single_arm_defect() -> None:
    """DEFECT (age/parser.py:1916): when BG002 ('Total') is absent from measurements, BG000 arm is hijacked!"""
    groups = [
        {"id": "BG000", "title": "Pediatric Arm"},
        {"id": "BG001", "title": "Adult Arm"},
        {"id": "BG002", "title": "Total"},
    ]
    m_arm_only = _make_cont_measure("Age", value="8.0", spread="2.0", group_id="BG000")
    p = ap.parse_trial([m_arm_only], baseline_groups=groups)
    assert p.has_reported_continuous is False
    assert p.mean_years is None


def test_cross_measure_continuous_contradiction_geometric_mean_and_iqr_defect() -> None:
    """DEFECT (age/parser.py:2384-2407): cross-measure range check skips geometric_mean, IQR, & full_range_max!"""
    m_gm = _make_cont_measure(
        "Age",
        param_type="GEOMETRIC_MEAN",
        dispersion_type="STANDARD_DEVIATION",
        unit="years",
        value="72.0",
        spread="4.0",
    )
    m_rng = _make_cont_measure(
        "Age",
        param_type="MEDIAN",
        dispersion_type="FULL_RANGE",
        unit="years",
        value="40.0",
        lower="20.0",
        upper="55.0",
    )
    p = ap.parse_trial([m_gm, m_rng])
    assert p.flag_impossible_continuous_value is True
    assert p.has_reported_continuous is False


@pytest.mark.parametrize(
    ("param_type", "val"),
    [
        ("MEAN", "50.0"),
        ("MEAN", "70.0"),
        ("MEDIAN", "50.0"),
        ("MEDIAN", "70.0"),
    ],
)
def test_zero_enrolled_65_plus_never_falsely_true_on_mean_or_median_without_max(
    param_type: str, val: str
) -> None:
    """Confirmed invariant: without max_years or categorical table, zero_enrolled_65_plus is NEVER True."""
    s = _make_study(
        "NCT99040001",
        [_make_cont_measure("Age", param_type=param_type, dispersion_type="STANDARD_DEVIATION", value=val, spread="8.0")],
        min_age="18 Years",
        max_age="N/A",
    )
    aud = ar.audit_eligibility_vs_enrolled(s)
    assert aud["zero_enrolled_65_plus"] is False
    assert aud["older_adult_representation_gap"] is False
    assert aud["zero_enrolled_75_plus"] is False


def test_pigeonhole_mean_70_exceeds_max_age_64_years_defect() -> None:
    """DEFECT (age/representation.py:398-405): MEAN=70.0 on maximumAge='64 Years' missed as out-of-bounds!

    By Pigeonhole Principle, if cohort MEAN == 70.0 (or MEDIAN == 70.0), at least one
    enrolled participant has age >= 70.0 >= 65.0, violating protocol maximumAge='64 Years'!
    """
    s = _make_study(
        "NCT99040002",
        [_make_cont_measure("Age", param_type="MEAN", dispersion_type="STANDARD_DEVIATION", value="70.0", spread="3.0")],
        min_age="18 Years",
        max_age="64 Years",
    )
    aud = ar.audit_eligibility_vs_enrolled(s)
    assert aud["enrolled_outside_eligibility_bounds"] is True


def test_eligibility_audit_non_year_max_age_23_months_and_inverted_tolerance_defect() -> None:
    """DEFECT (age/representation.py:403,415): maximumAge='23 Months' with enrolled max=23 months flagged invalid!

    Also flags age 65.9999995 years on maximumAge='65 Years' (eligible_hi=66.0) because
    UNIT_CONVERSION_TOL_YEARS is subtracted (`eligible_hi - tol`) instead of added!
    """
    s_mo = _make_study(
        "NCT99040003",
        [
            _make_cont_measure(
                "Age",
                param_type="MEDIAN",
                dispersion_type="FULL_RANGE",
                unit="months",
                value="12",
                lower="1",
                upper="23",
            )
        ],
        min_age="0 Months",
        max_age="23 Months",
        std_ages=["CHILD"],
    )
    aud_mo = ar.audit_eligibility_vs_enrolled(s_mo)
    assert aud_mo["enrolled_outside_eligibility_bounds"] is False

    s_tol = _make_study(
        "NCT99040004",
        [
            _make_cont_measure(
                "Age",
                param_type="MEDIAN",
                dispersion_type="FULL_RANGE",
                unit="years",
                value="45",
                lower="18",
                upper="65.9999995",
            )
        ],
        min_age="18 Years",
        max_age="65 Years",
    )
    aud_tol = ar.audit_eligibility_vs_enrolled(s_tol)
    assert aud_tol["enrolled_outside_eligibility_bounds"] is False


def test_eligibility_audit_contradicts_continuous_max_82_when_rounded_pct_is_zero_defect() -> None:
    """DEFECT (age/representation.py:368-373): zero_enrolled_65_plus=True when max_years==82.0!

    On N=300 with 1 participant aged 82 (0.33% -> '0%' in integer-rounded percentage table)
    and continuous FULL_RANGE [20, 82], max_years=82.0 proves >=65 and >=75 enrollment, yet
    audit_eligibility_vs_enrolled reports zero_enrolled_65_plus=True and zero_enrolled_75_plus=True!
    """
    denoms = [{"units": "Participants", "counts": [{"groupId": "BG000", "value": "300"}]}]
    s = _make_study(
        "NCT99040005",
        [
            _make_cat_measure(
                "Age, Categorical",
                [("18 to 64 years", "100"), (">= 65 years", "0")],
                param_type="PERCENTAGE",
                unit="%",
            ),
            _make_cont_measure(
                "Age, Continuous",
                param_type="MEDIAN",
                dispersion_type="FULL_RANGE",
                unit="years",
                value="45",
                lower="20",
                upper="82",
            ),
        ],
        min_age="18 Years",
        max_age="N/A",
        denoms=denoms,
    )
    aud = ar.audit_eligibility_vs_enrolled(s)
    assert aud["zero_enrolled_65_plus"] is False
    assert aud["zero_enrolled_75_plus"] is False


def test_extract_eligibility_record_drops_top_level_min_age_when_protocol_section_empty_defect() -> None:
    """DEFECT (age/representation.py:270): top-level min_age ignored when protocolSection={} is present!"""
    hybrid_study = {
        "nct_id": "NCT99040006",
        "min_age": "18 Years",
        "max_age": "64 Years",
        "protocolSection": {},
        "resultsSection": {
            "baselineCharacteristicsModule": {
                "measures": [_make_cat_measure("Age, Categorical", [("18 to 64 years", 40)])]
            }
        },
    }
    row = at.build_age_row(hybrid_study, SNAP)
    assert row["min_age"] == "18 Years"
    aud = ar.audit_eligibility_vs_enrolled(hybrid_study)
    assert aud["eligible_lo"] == pytest.approx(18.0)
    assert aud["nominally_open_pediatric"] is False


# ===========================================================================
# 5. Table Serialization, Raw Re-Parse Parity, NaN Safety, & README Parity
# ===========================================================================


def test_csv_and_jsonl_gz_round_trip_six_non_error_states_and_types(tmp_path: Path) -> None:
    """Verify CSV/JSONL (.gz) round-trip preserving None vs 0 vs False vs [] on standard 6 states."""
    studies = [
        _make_study("NCT99050001", [_make_cat_measure("Age", [("18-64 years", 30), (">=65 years", 10)]), _make_cont_measure()]),
        _make_study("NCT99050002", [_make_cat_measure("Age", [("18-64 years", 40), (">=65 years", 0)])]),
        _make_study("NCT99050003", [_make_cont_measure("Age", value="0.0", spread="0.0")]),
        _make_study("NCT99050004", [_make_cat_measure("Age", [("18-64 years", 0), ("Unknown", 9)])]),
        _make_study("NCT99050005", [_make_cat_measure("Age", [("18-64 years", 0), (">=65 years", 0)])]),
        _make_study("NCT99050006", []),
    ]
    rows = [at.build_age_row(s, SNAP) for s in studies]
    seen_statuses = {r["age_report_status"] for r in rows}
    assert seen_statuses == set(at.STATES) - {"parse_error"}

    for s, r in zip(studies, rows):
        env = at.select_raw_age_measures(s)
        assert at.build_age_row_from_raw(env, SNAP) == r

    rows_with_pe = [*rows, at.parse_error_row("NCT99050007", 50, SNAP, error_detail="corrupt")]
    assert {r["age_report_status"] for r in rows_with_pe} == set(at.STATES)

    p_jsonl = tmp_path / "round1.jsonl.gz"
    p_csv = tmp_path / "round1.csv.gz"
    assert at.write_age_jsonl(rows_with_pe, p_jsonl) == 7
    assert at.read_age_jsonl(p_jsonl) == rows_with_pe
    assert at.write_age_csv(rows_with_pe, p_csv) == 7
    assert at.read_age_csv(p_csv) == rows_with_pe
    assert at.all_pass(at.structural_checks(rows_with_pe))


@pytest.mark.parametrize(
    ("nct_id", "corrupt_study"),
    [
        (
            "NCT99050101",
            {
                "protocolSection": {"identificationModule": {"nctId": "NCT99050101"}},
                "resultsSection": {"baselineCharacteristicsModule": {"measures": "bad"}},
            },
        ),
        (
            "NCT99050102",
            {
                "protocolSection": {"identificationModule": {"nctId": "NCT99050102"}},
                "resultsSection": {
                    "baselineCharacteristicsModule": {"measures": [{"title": "Age"}, 123]}
                },
            },
        ),
        (
            "NCT99050103",
            {
                "protocolSection": {"identificationModule": {"nctId": "NCT99050103"}},
                "resultsSection": "bad_results_section",
            },
        ),
        (
            "NCT99050104",
            {
                "protocolSection": {"identificationModule": {"nctId": "NCT99050104"}},
                "resultsSection": {"baselineCharacteristicsModule": "bad_module"},
            },
        ),
    ],
)
def test_raw_envelope_reparse_parity_on_parse_error_state_defect(
    nct_id: str, corrupt_study: dict[str, Any]
) -> None:
    """DEFECT (age/table.py:297-304): select_raw_age_measures breaks 7th-state ('parse_error') parity!

    build_age_row(corrupt_study) returns age_report_status == 'parse_error' (outcomes None),
    whereas select_raw_age_measures either crashes with AttributeError or coerces corrupt
    measures to [] / filtered dicts so build_age_row_from_raw returns 'not_reported' /
    'uninformative' (reported_age_any == False)!
    """
    direct = at.build_age_row(corrupt_study, SNAP)
    assert direct["age_report_status"] == "parse_error"
    raw_env = at.select_raw_age_measures(corrupt_study)
    rebuilt = at.build_age_row_from_raw(raw_env, SNAP)
    assert rebuilt == direct


def test_csv_round_trip_empty_string_columns_become_none_defect(tmp_path: Path) -> None:
    """DEFECT (age/table.py:633-657): CSV round-trip turns '' into None on string columns!"""
    s = _make_study(
        "NCT99050201",
        [_make_cat_measure("Age", [("18 to 64 years", 20)], unit="")],
        min_age="",
        max_age="",
    )
    row = at.build_age_row(s, SNAP)
    p_jsonl = tmp_path / "empty_str.jsonl.gz"
    p_csv = tmp_path / "empty_str.csv.gz"
    at.write_age_jsonl([row], p_jsonl)
    at.write_age_csv([row], p_csv)
    assert at.read_age_jsonl(p_jsonl) == [row]
    assert at.read_age_csv(p_csv) == [row]


def test_negative_enrollment_accepted_and_sets_exceeds_enrollment_defect() -> None:
    """DEFECT (age/table.py:235,344 & age/parser.py:2422): enrollment=-10 stored as -10.0!"""
    s = _make_study(
        "NCT99050202",
        [_make_cat_measure("Age", [("18 to 64 years", 0), (">= 65 years", 0)])],
        enrollment=-10,
    )
    row = at.build_age_row(s, SNAP)
    assert row["enrollment"] is None or row["enrollment"] >= 0.0
    assert row["flag_exceeds_enrollment"] is False


@pytest.mark.parametrize(
    "malformed_study",
    [
        None,
        "not_a_study_dict",
        12345,
        ["list_study"],
        _make_study("NCT99050301", "bad", enrollment=-10),
        _make_study("NCT99050302", [_make_cont_measure("Age", value="NaN", spread="Infinity")], enrollment="NaN"),
        _make_study("NCT99050303", [_make_cont_measure("Age", value=float("nan"), spread=float("inf"))], enrollment=float("inf")),
        _make_study("NCT99050304", [_make_cat_measure("Age", [("18-64 years", "NaN"), (">=65 years", "-Infinity")])], enrollment="Infinity"),
    ],
)
def test_strict_json_allow_nan_false_on_all_malformed_inputs(malformed_study: Any) -> None:
    row = at.build_age_row(malformed_study, SNAP)  # type: ignore[arg-type]
    encoded = json.dumps(row, allow_nan=False)
    decoded = json.loads(encoded)
    assert list(decoded.keys()) == at.COLUMNS
    for v in decoded.values():
        if isinstance(v, float):
            assert math.isfinite(v)


def test_raw_jsonl_writer_rejects_or_sanitizes_nan_enrollment_defect(tmp_path: Path) -> None:
    """DEFECT (age/table.py:309,617): select_raw_age_measures + write_raw_age_jsonl emits non-standard NaN JSON!"""
    s = _make_study("NCT99050305", [], enrollment=float("nan"))
    env = at.select_raw_age_measures(s)
    # Standard strict JSON (RFC 8259) forbids bare NaN tokens
    encoded = json.dumps(env, allow_nan=False)
    assert "NaN" not in encoded


def test_readme_parity_with_constants_columns_and_exports() -> None:
    readme_text = (_ROOT / "age" / "README.md").read_text(encoding="utf-8")
    for status in ap.AGE_REPORT_STATUS_LEVELS:
        assert f"`{status}`" in readme_text
    for kind in ap.MEASURE_KINDS:
        assert f"`\"{kind}\"`" in readme_text or f"`{kind}`" in readme_text
    for fn_name in (
        "measure_kind",
        "select_age_measures",
        "select_overall_group",
        "parse_interval_label",
        "select_raw_age_measures",
        "build_age_row",
        "build_age_row_from_raw",
        "lean_age_row",
        "parse_error_row",
        "structural_checks",
        "age_range_bounds",
        "audit_eligibility_vs_enrolled",
        "compare_age_to_acs",
    ):
        assert hasattr(age, fn_name)
        assert fn_name in readme_text
    assert set(at.LEAN_COLUMNS).issubset(set(at.COLUMNS))


def test_readme_and_priority_tuple_cover_all_emitted_uninformative_reasons_defect() -> None:
    """DEFECT (age/parser.py:2436-2449 & age/README.md:122): emitted uninformative_reason codes missing!

    1) negative_category_count, percentage_exceeds_100, percentage_sum_exceeds_100,
       ambiguous_multicategory_continuous, arms_not_poolable_for_continuous, and
       no_usable_continuous_stat are missing from parse_trial's priority tuple, causing
       an empty secondary template ('no_measurements') to mask 'negative_category_count'!
    2) 8 emitted uninformative_reason codes are missing from age/README.md's list.
    """
    # Empty template ('no_measurements') + table with negative count ('negative_category_count')
    m_empty = {"title": "Age, Continuous", "paramType": "MEAN", "unitOfMeasure": "years", "classes": []}
    m_neg = _make_cat_measure("Age, Categorical", [("18 to 64 years", -5), (">= 65 years", 20)])
    p = ap.parse_trial([m_empty, m_neg])
    assert ap.classify_age_reporting(p) == "uninformative"
    assert p.uninformative_reason == "negative_category_count"

    readme_text = (_ROOT / "age" / "README.md").read_text(encoding="utf-8")
    parser_src = (_ROOT / "age" / "parser.py").read_text(encoding="utf-8")
    emitted = set(re.findall(r'uninformative_reason\s*=\s*"([a-z0-9_]+)"', parser_src))
    missing_in_readme = sorted(r for r in emitted if r not in readme_text)
    assert not missing_in_readme, f"uninformative_reason codes missing from age/README.md: {missing_in_readme}"


def test_continuous_iqr_outside_full_range_cross_measure_defect() -> None:
    """DEFECT (age/parser.py:2380-2407): cross-measure merge allows IQR [35, 68] outside FULL_RANGE [40, 60]!"""
    m_iqr = _make_cont_measure(
        "Age, Continuous",
        param_type="MEDIAN",
        dispersion_type="INTER_QUARTILE_RANGE",
        unit="years",
        value="50.0",
        lower="35.0",
        upper="68.0",
    )
    m_rng = _make_cont_measure(
        "Age, Range",
        param_type="MEDIAN",
        dispersion_type="FULL_RANGE",
        unit="years",
        value="50.0",
        lower="40.0",
        upper="60.0",
    )
    p = ap.parse_trial([m_iqr, m_rng])
    assert p.flag_impossible_continuous_value is True
    assert p.has_reported_continuous is False


def test_continuous_disjoint_arm_pooling_negative_sd_in_one_arm_defect() -> None:
    """DEFECT (age/parser.py:1964-1982): negative SD in Arm 1 ignored when Arm 2 omits SD!"""
    groups = [{"id": "BG000", "title": "Arm 1"}, {"id": "BG001", "title": "Arm 2"}]
    denoms = [
        {
            "units": "Participants",
            "counts": [{"groupId": "BG000", "value": "20"}, {"groupId": "BG001", "value": "20"}],
        }
    ]
    m = {
        "title": "Age, Continuous",
        "paramType": "MEAN",
        "dispersionType": "STANDARD_DEVIATION",
        "unitOfMeasure": "years",
        "classes": [
            {
                "categories": [
                    {
                        "measurements": [
                            {"groupId": "BG000", "value": "50.0", "spread": "-4.0"},
                            {"groupId": "BG001", "value": "55.0"},
                        ]
                    }
                ]
            }
        ],
    }
    p = ap.parse_trial([m], baseline_groups=groups, baseline_denoms=denoms)
    assert p.flag_impossible_continuous_value is True
    assert p.has_reported_continuous is False


def test_parenthetical_trailing_unit_after_and_older_label_defect() -> None:
    """DEFECT (age/parser.py:944,1022-1024): '65 and older (years)' rewrites to '65 and older years' -> unmapped!"""
    parsed = ap.parse_interval_label("65 and older (years)")
    assert parsed.kind == "interval"
    assert parsed.lo_years == pytest.approx(65.0)
    assert parsed.hi_years is None


def test_zero_enrolled_pediatric_never_falsely_true_on_mean_or_median_without_min() -> None:
    """Confirmed invariant: without min_years or categorical table, zero_enrolled_pediatric is NEVER True."""
    s = _make_study(
        "NCT99050401",
        [_make_cont_measure("Age", param_type="MEAN", dispersion_type="STANDARD_DEVIATION", value="42.0", spread="12.0")],
        min_age="0 Years",
        max_age="N/A",
    )
    aud = ar.audit_eligibility_vs_enrolled(s)
    assert aud["nominally_open_pediatric"] is True
    assert aud["zero_enrolled_pediatric"] is False
    assert aud["pediatric_representation_gap"] is False


def test_denominator_b_classified_bounds_invariant_to_unknown_count() -> None:
    """Confirmed invariant: increasing n_unknown widens Denominator A monotonically while Denominator B stays invariant."""
    p = ap.parse_trial(
        [_make_cat_measure("Age, Customized", [("18 to 49 years", 40), ("50 to 70 years", 30), (">= 71 years", 30)])]
    )
    prev_floor_a = 100.0
    prev_ceil_a = 0.0
    ref_b: dict[str, Any] | None = None
    for unk in (0.0, 10.0, 50.0, 250.0, 5000.0):
        res = ar.age_range_bounds(p.parsed_brackets, unk, 65.0, None)
        assert res["floor_pct"] <= prev_floor_a + 1e-12
        assert res["ceiling_pct"] >= prev_ceil_a - 1e-12
        prev_floor_a = res["floor_pct"]
        prev_ceil_a = res["ceiling_pct"]
        if ref_b is None:
            ref_b = res["of_classified"]
        else:
            assert res["of_classified"] == ref_b
