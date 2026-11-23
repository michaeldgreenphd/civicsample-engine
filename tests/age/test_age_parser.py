"""Unit tests for age/parser.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from age import parser as ap  # noqa: E402


def _cat_measure(
    title: str,
    rows: list[tuple[str, str | int | float]],
    *,
    param_type: str = "COUNT_OF_PARTICIPANTS",
    unit: str = "Participants",
    group_id: str = "BG000",
    denoms: list[dict] | None = None,
) -> dict:
    m: dict = {
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


def _cont_measure(
    title: str = "Age, Continuous",
    *,
    param_type: str = "MEAN",
    dispersion_type: str | None = "STANDARD_DEVIATION",
    unit: str | None = "years",
    value: str | float | None = "54.5",
    spread: str | float | None = "11.2",
    lower: str | float | None = None,
    upper: str | float | None = None,
    group_id: str = "BG000",
) -> dict:
    mm: dict = {"groupId": group_id}
    if value is not None:
        mm["value"] = str(value)
    if spread is not None:
        mm["spread"] = str(spread)
    if lower is not None:
        mm["lowerLimit"] = str(lower)
    if upper is not None:
        mm["upperLimit"] = str(upper)
    m: dict = {
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


# ---------------------------------------------------------------------------
# 1. Vocabulary SHA-256 pinning
# ---------------------------------------------------------------------------


def test_vocabulary_sha256_is_pinned_and_deterministic() -> None:
    assert len(ap.VOCABULARY_SHA256) == 64
    assert ap.VOCABULARY_SHA256 == ap.EXPECTED_VOCABULARY_SHA256
    assert ap.compute_vocabulary_sha256() == ap.EXPECTED_VOCABULARY_SHA256
    ap.validate_vocabulary_pin()


def test_validate_vocabulary_pin_detects_tampering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ap, "EXPECTED_VOCABULARY_SHA256", "0" * 64)
    with pytest.raises(AssertionError, match="vocabulary SHA-256 drift"):
        ap.validate_vocabulary_pin()


# ---------------------------------------------------------------------------
# 2. Measure title classification & false-positive / excluded-kind guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Age",
        "Age, Continuous",
        "Age, Categorical",
        "Age, Customized",
        "Age Group",
        "Age Groups",
        "Age Category",
        "Baseline Age",
        "Participant Age",
        "Subject Age",
        "Patient Age",
        "Chronological Age",
        "Age (Years)",
        "Age in Months",
        "Age at Baseline",
        "Age at Enrollment",
        "Age at Randomization",
        "Age: <=18, 18-64, >=65 years",
        "Age, Mean (SD)",
        "Age (in years)",
        "Age [years]",
        "Participant's Age",
    ],
)
def test_measure_kind_accepts_participant_baseline_age_titles(title: str) -> None:
    assert ap.measure_kind(title) == "participant_baseline_age"


@pytest.mark.parametrize(
    "false_positive_title",
    [
        "Dosage",
        "Cumulative Drug Dosage",
        "Disease Stage",
        "TNM Stage",
        "Coverage",
        "Vaccine Coverage",
        "Percentage",
        "Percentage of Responders",
        "Hemorrhage",
        "Cartilage",
        "Drainage",
        "Language",
        "Primary Language",
        "Storage",
        "Average",
        "Average Daily Score",
        "Damage",
        "Leakage",
        "Weight-for-Age",
        "Weight for Age Z-score",
        "Height-for-Age",
        "Length-for-Age",
        "BMI-for-Age",
        "Age-Adjusted Charlson Comorbidity Index",
        "Women of Childbearing Age",
        None,
        "",
    ],
)
def test_measure_kind_rejects_substring_and_index_false_positives(
    false_positive_title: str | None,
) -> None:
    assert ap.measure_kind(false_positive_title) == "not_age"


@pytest.mark.parametrize(
    ("title", "expected_kind"),
    [
        ("Gestational Age", "gestational_or_developmental_age"),
        ("Postmenstrual Age", "gestational_or_developmental_age"),
        ("Corrected Age", "gestational_or_developmental_age"),
        ("Bone Age", "gestational_or_developmental_age"),
        ("Age at Diagnosis", "disease_history_age"),
        ("Age at Initial Diagnosis", "disease_history_age"),
        ("Age at Onset", "disease_history_age"),
        ("Age at Screening", "disease_history_age"),
        ("Age at Menarche", "disease_history_age"),
        ("Age at Menopause", "disease_history_age"),
        ("Maternal Age", "non_participant_role_age"),
        ("Paternal Age", "non_participant_role_age"),
        ("Caregiver Age", "non_participant_role_age"),
        ("Donor Age", "non_participant_role_age"),
        ("Age, Female", "subgroup_stratified_age"),
        ("Age, Male", "subgroup_stratified_age"),
        ("Age by Sex", "subgroup_stratified_age"),
    ],
)
def test_measure_kind_classifies_excluded_age_kinds(title: str, expected_kind: str) -> None:
    assert ap.measure_kind(title) == expected_kind


def test_excluded_age_measures_recorded_and_never_mixed_into_cohort_age() -> None:
    measures = [
        _cont_measure("Gestational Age", unit="weeks", value="38.5", spread="1.4"),
        _cont_measure("Age at Diagnosis", unit="years", value="42.0", spread="9.0"),
        _cont_measure("Maternal Age", unit="years", value="31.0", spread="4.5"),
        _cont_measure("Age, Female", unit="years", value="60.0", spread="8.0"),
        {"title": "Dosage", "paramType": "MEAN", "unitOfMeasure": "mg"},
    ]
    assert ap.select_age_measures(measures) == []
    parsed = ap.parse_trial(measures)
    assert parsed.raw_present is False
    assert ap.classify_age_reporting(parsed) == "not_reported"
    assert parsed.mean_years is None
    assert parsed.excluded_age_measure_titles == [
        "Gestational Age",
        "Age at Diagnosis",
        "Maternal Age",
        "Age, Female",
    ]
    assert len(parsed.excluded_age_measure_kinds) == 4


# ---------------------------------------------------------------------------
# 3. Whole-population BG group selection
# ---------------------------------------------------------------------------


def test_select_overall_group_single_and_explicit_total() -> None:
    single = ap.select_overall_group([{"id": "BG000", "title": "All Treated"}])
    assert single.strategy == "single_group"
    assert single.overall_group_id == "BG000"
    assert not single.flag_summed_arms_no_total

    # Arm with "Overall" in name before "Total" must NOT hijack Total (src/utils.py rule).
    groups = [
        {"id": "BG000", "title": "Tafamidis 80 mg Treated-Overall"},
        {"id": "BG001", "title": "Placebo"},
        {"id": "BG002", "title": "Total"},
    ]
    sel = ap.select_overall_group(groups)
    assert sel.strategy == "explicit_total"
    assert sel.overall_group_id == "BG002"

    # Whole-population title when not named "Total"
    sel2 = ap.select_overall_group(
        [
            {"id": "BG000", "title": "Arm A"},
            {"id": "BG001", "title": "Arm B"},
            {"id": "BG002", "title": "All Participants"},
        ]
    )
    assert sel2.strategy == "explicit_total"
    assert sel2.overall_group_id == "BG002"


def test_select_overall_group_sums_disjoint_arms_when_no_total_exists() -> None:
    groups = [
        {"id": "BG000", "title": "Drug X 10 mg"},
        {"id": "BG001", "title": "Placebo"},
    ]
    sel = ap.select_overall_group(groups)
    assert sel.strategy == "sum_disjoint_arms"
    assert sel.selected_group_ids == ("BG000", "BG001")
    assert sel.flag_summed_arms_no_total is True
    assert sel.flag_ambiguous_groups is False

    m = {
        "title": "Age, Categorical",
        "paramType": "COUNT_OF_PARTICIPANTS",
        "unitOfMeasure": "Participants",
        "classes": [
            {
                "categories": [
                    {
                        "title": "18 to 64 years",
                        "measurements": [
                            {"groupId": "BG000", "value": "30"},
                            {"groupId": "BG001", "value": "25"},
                        ],
                    },
                    {
                        "title": ">= 65 years",
                        "measurements": [
                            {"groupId": "BG000", "value": "10"},
                            {"groupId": "BG001", "value": "15"},
                        ],
                    },
                ]
            }
        ],
    }
    p = ap.parse_trial([m], baseline_groups=groups)
    assert p.flag_summed_arms_no_total is True
    assert p.n_total_parsed == 80.0
    assert [b.count for b in p.parsed_brackets] == [55.0, 25.0]


@pytest.mark.parametrize(
    "bad_groups",
    [
        [{"id": "BG000", "title": "Safety Population"}, {"id": "BG001", "title": "ITT Population"}],
        [{"id": "BG000", "title": "Arm A"}, {"id": "BG001", "title": "Pooled Active Arms"}],
        [{"id": "BG000", "title": "Part 1"}, {"id": "BG001", "title": "Open-Label Extension"}],
        [{"id": "BG000", "title": "Cohort 1"}, {"id": "BG001", "title": "Cohort 1"}],
        [{"id": "BG000", "title": ""}, {"id": "BG001", "title": "Arm B"}],
    ],
)
def test_select_overall_group_flags_ambiguous_groups(bad_groups: list[dict]) -> None:
    sel = ap.select_overall_group(bad_groups)
    assert sel.strategy == "ambiguous_groups"
    assert sel.flag_ambiguous_groups is True
    assert sel.selected_group_ids == ()


# ---------------------------------------------------------------------------
# 4. Participant vs non-participant units & percentage conversion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("param_type", "unit"),
    [
        ("COUNT_OF_UNITS", "eyes"),
        ("COUNT_OF_UNITS", "lesions"),
        ("COUNT_OF_PARTICIPANTS", "Cycles"),
        ("NUMBER", "Biopsies"),
        ("NUMBER", "Infusions"),
        ("NUMBER", "Teeth"),
        ("NUMBER", "Knees"),
    ],
)
def test_nonparticipant_units_never_enter_participant_counts(
    param_type: str, unit: str
) -> None:
    m = _cat_measure(
        "Age, Categorical",
        [("18 to 64 years", 40), (">= 65 years", 20)],
        param_type=param_type,
        unit=unit,
    )
    p = ap.parse_trial([m])
    assert p.is_participant_count is False
    assert p.flag_nonparticipant_units is True
    assert p.parsed_brackets == []
    assert p.n_total_parsed is None
    assert ap.classify_age_reporting(p) == "uninformative"
    assert p.uninformative_reason == "non_participant_units"


def test_percentage_measure_converts_only_with_explicit_participant_denom() -> None:
    m_no_denom = _cat_measure(
        "Age, Categorical",
        [("18 to 64 years", "60.0"), (">= 65 years", "40.0")],
        param_type="PERCENTAGE",
        unit="Percentage of Participants",
    )
    p_bad = ap.parse_trial([m_no_denom], enrollment=200)
    assert p_bad.is_participant_count is False
    assert p_bad.flag_percentage_converted is False
    assert ap.classify_age_reporting(p_bad) == "uninformative"
    assert p_bad.uninformative_reason == "percentage_without_denominator"

    # Non-participant denom ("Sites") must also be rejected
    m_site_denom = _cat_measure(
        "Age, Categorical",
        [("18 to 64 years", "60.0"), (">= 65 years", "40.0")],
        param_type="PERCENTAGE",
        unit="%",
        denoms=[{"units": "Sites", "counts": [{"groupId": "BG000", "value": "50"}]}],
    )
    p_site = ap.parse_trial([m_site_denom])
    assert ap.classify_age_reporting(p_site) == "uninformative"
    assert p_site.uninformative_reason == "percentage_without_denominator"

    # Explicit Participants denom converts cleanly
    m_ok = _cat_measure(
        "Age, Categorical",
        [("18 to 64 years", "62.5"), (">= 65 years", "37.5")],
        param_type="PERCENTAGE",
        unit="%",
        denoms=[{"units": "Participants", "counts": [{"groupId": "BG000", "value": "80"}]}],
    )
    p_ok = ap.parse_trial([m_ok])
    assert p_ok.is_participant_count is True
    assert p_ok.flag_percentage_converted is True
    assert ap.classify_age_reporting(p_ok) == "reported_categorical_only"
    assert [b.count for b in p_ok.parsed_brackets] == [50.0, 30.0]
    assert p_ok.n_total_parsed == 80.0


# ---------------------------------------------------------------------------
# 5. Categorical age bracket grammar & pairwise overlap/gap auditing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "exp_lo", "exp_hi"),
    [
        ("< 18 years", 0.0, 18.0),
        ("Under 18 years", 0.0, 18.0),
        ("<= 17 years", 0.0, 18.0),
        ("<= 18 years", 0.0, 19.0),
        ("18 to 64 years", 18.0, 65.0),
        ("18-64 years", 18.0, 65.0),
        ("Between 18 and 64 years", 18.0, 65.0),
        ("18 - <65 years", 18.0, 65.0),
        (">= 65 years", 65.0, None),
        ("65 years and older", 65.0, None),
        ("65 years and over", 65.0, None),
        ("65+", 65.0, None),
        ("> 64 years", 65.0, None),
        ("> 65 years", 66.0, None),
        ("<= 65 years", 0.0, 66.0),
        ("0 to 27 days", 0.0, 28.0 / 365.0),
        ("28 days to 23 months", 28.0 / 365.0, 2.0),
        ("2 to 11 years", 2.0, 12.0),
        ("12 to 17 years", 12.0, 18.0),
        ("Adolescents (12 to 17 years)", 12.0, 18.0),
        ("Age < 18 years", 0.0, 18.0),
        ("Age 18 to 64 years", 18.0, 65.0),
        ("Aged 65 years & older", 65.0, None),
        ("18-64 yrs.", 18.0, 65.0),
    ],
)
def test_parse_interval_label_canonical_cases(
    label: str, exp_lo: float, exp_hi: float | None
) -> None:
    res = ap.parse_interval_label(label)
    assert res.kind == "interval"
    assert res.lo_years == pytest.approx(exp_lo)
    if exp_hi is None:
        assert res.hi_years is None
    else:
        assert res.hi_years == pytest.approx(exp_hi)
    assert res.bound_basis is not None and len(res.bound_basis) > 0


def test_pediatric_multi_unit_brackets_tile_cleanly() -> None:
    m = _cat_measure(
        "Age, Customized",
        [
            ("0 to 27 days", 5),
            ("28 days to 23 months", 12),
            ("2 to 11 years", 20),
            ("12 to 17 years", 13),
        ],
    )
    p = ap.parse_trial([m])
    assert p.brackets_tile_cleanly is True
    assert p.flag_overlapping_brackets is False
    assert p.flag_gap_between_brackets is False
    assert p.n_total_parsed == 50.0


def test_paired_le_65_and_gt_65_tiles_cleanly_while_gap_is_flagged() -> None:
    # "<= 65 years" ([0, 66)) + "> 65 years" ([66, None)) tiles cleanly at 66.0
    m_paired = _cat_measure("Age Group", [("<= 65 years", 70), ("> 65 years", 30)])
    p_paired = ap.parse_trial([m_paired])
    assert p_paired.brackets_tile_cleanly is True
    assert p_paired.flag_overlapping_brackets is False
    assert p_paired.flag_gap_between_brackets is False

    # "18-64 years" ([18, 65)) + "> 65 years" ([66, None)) omits age 65 -> gap flagged
    m_gap = _cat_measure("Age Group", [("18-64 years", 70), ("> 65 years", 30)])
    p_gap = ap.parse_trial([m_gap])
    assert p_gap.flag_gap_between_brackets is True
    assert p_gap.brackets_tile_cleanly is False
    assert ap.classify_age_reporting(p_gap) == "reported_categorical_only"


def test_overlapping_brackets_with_nonzero_counts_rejected_as_uninformative() -> None:
    m_overlap = _cat_measure("Age, Customized", [("18-65", 40), ("65-80", 25)])
    p = ap.parse_trial([m_overlap])
    assert p.flag_overlapping_brackets is True
    assert p.brackets_tile_cleanly is False
    assert p.parsed_brackets == []
    assert p.n_total_parsed is None
    assert ap.classify_age_reporting(p) == "uninformative"
    assert p.uninformative_reason == "overlapping_age_brackets"


def test_overlapping_bracket_with_zero_count_preserves_valid_counts() -> None:
    # Classic CT.gov 3-row template in an adult >=18 trial where "<= 18 years" has count 0.
    m = _cat_measure(
        "Age, Categorical",
        [("<= 18 years", 0), ("18 to 64 years", 75), (">= 65 years", 25)],
    )
    p = ap.parse_trial([m])
    assert p.flag_overlapping_brackets is True
    assert p.brackets_tile_cleanly is False
    assert ap.classify_age_reporting(p) == "reported_categorical_only"
    assert p.n_total_parsed == 100.0


def test_unknown_and_unmapped_category_labels_and_customized_class_layout() -> None:
    m_custom = {
        "title": "Age, Customized",
        "paramType": "COUNT_OF_PARTICIPANTS",
        "unitOfMeasure": "Participants",
        "classes": [
            {"title": "18-64 years", "categories": [{"measurements": [{"groupId": "BG000", "value": "40"}]}]},
            {"title": ">=65 years", "categories": [{"measurements": [{"groupId": "BG000", "value": "10"}]}]},
            {"title": "Unknown / Not Reported", "categories": [{"measurements": [{"groupId": "BG000", "value": "5"}]}]},
            {"title": "Total", "categories": [{"measurements": [{"groupId": "BG000", "value": "55"}]}]},
        ],
    }
    p = ap.parse_trial([m_custom])
    assert p.flag_customized_layout is True
    assert p.n_classified == 50.0
    assert p.n_unknown == 5.0
    assert p.n_total_parsed == 55.0
    assert p.unknown_labels == ["Unknown / Not Reported"]

    # Unmapped non-structural label
    m_unmapped = _cat_measure("Age Group", [("Younger Cohort", 12), ("Older Cohort", 8)])
    p_unm = ap.parse_trial([m_unmapped])
    assert p_unm.flag_unmapped_category is True
    assert p_unm.unmapped_labels == ["Younger Cohort", "Older Cohort"]
    assert ap.classify_age_reporting(p_unm) == "uninformative"
    assert p_unm.uninformative_reason == "no_mapped_age_brackets"


# ---------------------------------------------------------------------------
# 6. Continuous age summary grammar & unit / impossibility guards
# ---------------------------------------------------------------------------


def test_continuous_stats_and_chronological_unit_conversions() -> None:
    # Mean + SD in months (infant trial: 6 months = 0.5 years)
    c_mo = ap.parse_continuous_measure(
        _cont_measure("Age", param_type="MEAN", dispersion_type="STANDARD_DEVIATION", unit="months", value="6", spread="3")
    )
    assert c_mo.parse_valid is True
    assert c_mo.central_value == 6.0
    assert c_mo.unit_of_measure == "months"
    assert c_mo.mean_years == pytest.approx(0.5)
    assert c_mo.sd_years == pytest.approx(0.25)

    # Median + IQR bounds in weeks (26 weeks = 0.5 years)
    c_wk = ap.parse_continuous_measure(
        _cont_measure(
            "Age",
            param_type="MEDIAN",
            dispersion_type="INTER_QUARTILE_RANGE",
            unit="weeks",
            value="26",
            spread=None,
            lower="13",
            upper="39",
        )
    )
    assert c_wk.median_years == pytest.approx(0.5)
    assert c_wk.iqr_low_years == pytest.approx(0.25)
    assert c_wk.iqr_high_years == pytest.approx(0.75)

    # Geometric mean + 95% CI in days & Least squares mean + SE in hours
    c_gm = ap.parse_continuous_measure(
        _cont_measure(
            "Age",
            param_type="GEOMETRIC_MEAN",
            dispersion_type="CONFIDENCE_INTERVAL_95",
            unit="days",
            value="365",
            spread=None,
            lower="180",
            upper="730",
        )
    )
    assert c_gm.geometric_mean_years == pytest.approx(1.0)
    assert c_gm.ci95_low_years == pytest.approx(180.0 / 365.0)
    assert c_gm.ci95_high_years == pytest.approx(2.0)

    c_ls = ap.parse_continuous_measure(
        _cont_measure(
            "Age",
            param_type="LEAST_SQUARES_MEAN",
            dispersion_type="STANDARD_ERROR",
            unit="hours",
            value="8760",
            spread="876",
        )
    )
    assert c_ls.least_squares_mean_years == pytest.approx(1.0)
    assert c_ls.se_years == pytest.approx(0.1)


def test_continuous_full_range_and_multi_measure_merging() -> None:
    m1 = _cont_measure("Age, Continuous", param_type="MEAN", dispersion_type="STANDARD_DEVIATION", unit="years", value="61.0", spread="8.5")
    m2 = _cont_measure("Age, Continuous", param_type="MEDIAN", dispersion_type="FULL_RANGE", unit="years", value="62.0", spread=None, lower="40", upper="78")
    p = ap.parse_trial([m1, m2])
    assert ap.classify_age_reporting(p) == "reported_continuous_only"
    assert p.mean_years == pytest.approx(61.0)
    assert p.sd_years == pytest.approx(8.5)
    assert p.median_years == pytest.approx(62.0)
    assert p.min_years == pytest.approx(40.0)
    assert p.max_years == pytest.approx(78.0)
    assert p.full_range_min == 40.0
    assert p.full_range_max == 78.0


@pytest.mark.parametrize(
    "bad_unit",
    [None, "", "Participants", "kg", "years / months", "months or years", "score"],
)
def test_continuous_rejects_missing_non_age_or_mixed_units(bad_unit: str | None) -> None:
    c = ap.parse_continuous_measure(
        _cont_measure("Age, Continuous", unit=bad_unit, value="52.0", spread="10.0")
    )
    assert c.parse_valid is False
    assert c.flag_unrecognized_or_mixed_unit is True
    assert c.mean_years is None
    assert c.sd_years is None
    assert c.central_value == 52.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"value": "NaN", "spread": "5.0"},
        {"value": "Inf", "spread": "5.0"},
        {"value": "-3.0", "spread": "5.0"},
        {"value": "50.0", "spread": "-2.0"},
        {"dispersion_type": "FULL_RANGE", "value": "50.0", "spread": None, "lower": "70.0", "upper": "30.0"},
        {"dispersion_type": "FULL_RANGE", "value": "80.0", "spread": None, "lower": "20.0", "upper": "60.0"},
    ],
)
def test_continuous_rejects_impossible_values(kwargs: dict) -> None:
    c = ap.parse_continuous_measure(_cont_measure("Age, Continuous", unit="years", **kwargs))
    assert c.parse_valid is False
    assert c.flag_impossible_continuous_value is True
    assert c.mean_years is None


def test_continuous_arm_pooling_for_mean_sd_vs_median_refusal() -> None:
    groups = [{"id": "BG000", "title": "Arm 1"}, {"id": "BG001", "title": "Arm 2"}]
    denoms = [
        {
            "units": "Participants",
            "counts": [{"groupId": "BG000", "value": "10"}, {"groupId": "BG001", "value": "30"}],
        }
    ]
    m_mean = {
        "title": "Age, Continuous",
        "paramType": "MEAN",
        "dispersionType": "STANDARD_DEVIATION",
        "unitOfMeasure": "years",
        "classes": [
            {
                "categories": [
                    {
                        "measurements": [
                            {"groupId": "BG000", "value": "40.0", "spread": "5.0"},
                            {"groupId": "BG001", "value": "60.0", "spread": "5.0"},
                        ]
                    }
                ]
            }
        ],
    }
    p = ap.parse_trial([m_mean], baseline_groups=groups, baseline_denoms=denoms)
    assert p.flag_summed_arms_no_total is True
    assert p.mean_years == pytest.approx(55.0)
    assert p.sd_years is not None and p.sd_years > 5.0

    # Medians across arms cannot be pooled into a whole-cohort median
    m_med = {**m_mean, "paramType": "MEDIAN", "dispersionType": "INTER_QUARTILE_RANGE"}
    p_med = ap.parse_trial([m_med], baseline_groups=groups, baseline_denoms=denoms)
    assert ap.classify_age_reporting(p_med) == "uninformative"
    assert p_med.median_years is None


# ---------------------------------------------------------------------------
# 7. Seven non-collapsing reporting states
# ---------------------------------------------------------------------------


def test_all_seven_non_collapsing_reporting_states() -> None:
    cat_m = _cat_measure("Age, Categorical", [("18 to 64 years", 30), (">= 65 years", 10)])
    cont_m = _cont_measure("Age, Continuous", value="55.0", spread="9.0")
    unk_m = _cat_measure("Age, Categorical", [("18 to 64 years", 0), ("Unknown", 15)])
    zero_m = _cat_measure("Age, Categorical", [("18 to 64 years", 0), (">= 65 years", 0)])

    assert ap.classify_age_reporting(ap.parse_trial([cat_m, cont_m])) == "reported_both"
    assert ap.classify_age_reporting(ap.parse_trial([cat_m])) == "reported_categorical_only"
    assert ap.classify_age_reporting(ap.parse_trial([cont_m])) == "reported_continuous_only"
    assert ap.classify_age_reporting(ap.parse_trial([unk_m])) == "explicit_unknown_only"
    assert ap.classify_age_reporting(ap.parse_trial([zero_m])) == "uninformative"
    assert ap.classify_age_reporting(ap.parse_trial([])) == "not_reported"
    assert ap.classify_age_reporting(ap.ParsedAgeTrial(raw_present=True, parse_ok=False)) == "parse_error"


def test_nih_prs_standard_between_18_and_65_tiles_cleanly_and_spelled_comparators() -> None:
    """NIH PRS's built-in Age, Categorical rows ('<=18 years', 'Between 18 and 65 years',
    '>=65 years') partition participants without overlap: [0, 19), [19, 65), [65, None)."""
    prs_m = _cat_measure(
        "Age, Categorical",
        [("<=18 years", 2), ("Between 18 and 65 years", 78), (">=65 years", 20)],
    )
    p = ap.parse_trial([prs_m], enrollment=100)
    assert ap.classify_age_reporting(p) == "reported_categorical_only"
    assert p.brackets_tile_cleanly is True
    assert p.flag_overlapping_brackets is False
    assert p.flag_gap_between_brackets is False
    assert [(b.lo_years, b.hi_years, b.count) for b in p.parsed_brackets] == [
        (0.0, 19.0, 2.0),
        (19.0, 65.0, 78.0),
        (65.0, None, 20.0),
    ]

    # Spelled-out comparators with optional redundant parenthetical symbols
    p_lt = ap.parse_interval_label("Less than (<) 18 years")
    assert (p_lt.kind, p_lt.lo_years, p_lt.hi_years) == ("interval", 0.0, 18.0)

    p_mid = ap.parse_interval_label("Greater than or equal to (>=) 18 and <= 45 years")
    assert (p_mid.kind, p_mid.lo_years, p_mid.hi_years) == ("interval", 18.0, 46.0)

    p_ge = ap.parse_interval_label("Greater than or equal to 85 years")
    assert (p_ge.kind, p_ge.lo_years, p_ge.hi_years) == ("interval", 85.0, None)

    # Logical typo 'or' instead of 'and' stays unmapped rather than guessed
    p_typo = ap.parse_interval_label("≥ 65 or < 75")
    assert p_typo.kind == "unmapped"
