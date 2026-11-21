"""Comprehensive unit and invariant tests for src/analysis_population_parser.py."""

from __future__ import annotations

import ast
import inspect
from typing import Any

import pytest

from src import analysis_population_parser as ap

PINNED_VOCABULARY_SHA256: str = (
    "33e22c5e80ee0caf9b1d7a73830ca1c403a68bdcbf99cc5b7c58c0945fa7b79e"
)


def _study_with_outcome(
    nct_id: str,
    measure: dict[str, Any],
    flow_groups: list[tuple[str, str, str | None]] | None = None,
    enrollment: int | None = 100,
    extra_measures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Helper constructing a minimal CT.gov API v2 study record."""
    measures = [measure] + (extra_measures or [])
    results: dict[str, Any] = {
        "outcomeMeasuresModule": {"outcomeMeasures": measures},
    }
    if flow_groups is not None:
        results["participantFlowModule"] = {
            "groups": [{"id": gid, "title": title} for gid, title, _ in flow_groups],
            "periods": [
                {
                    "title": "Overall Study",
                    "milestones": [
                        {
                            "type": "STARTED",
                            "achievements": [
                                (
                                    {"groupId": gid, "numSubjects": started}
                                    if started is not None
                                    else {"groupId": gid}
                                )
                                for gid, _, started in flow_groups
                            ],
                        }
                    ],
                }
            ],
        }
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id},
            "designModule": {"enrollmentInfo": {"count": enrollment}},
        },
        "resultsSection": results,
    }


# ---------------------------------------------------------------------------
# 1. Taxonomy, vocabulary, and tier precedence (all 9 disjoint classes)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_class", "expected_tier", "expected_reason"),
    [
        ("Intent-to-Treat population", "itt", "named_label", None),
        ("All randomized participants.", "itt", "definitional", None),
        ("mITT population", "modified_itt", "named_label", None),
        (
            "All randomized patients who received at least 1 dose",
            "modified_itt",
            "definitional",
            None,
        ),
        ("Full Analysis Set", "full_analysis_set", "named_label", None),
        ("Per-protocol set", "per_protocol", "named_label", None),
        (
            "Completed treatment without major protocol deviations",
            "per_protocol",
            "definitional",
            None,
        ),
        ("Efficacy evaluable population", "evaluable", "named_label", None),
        (
            "Patients with a valid baseline and at least one post-baseline assessment",
            "evaluable",
            "definitional",
            None,
        ),
        ("Safety population", "safety", "named_label", None),
        (
            "All participants who received at least one dose of study drug",
            "safety",
            "definitional",
            None,
        ),
        (
            "Biomarker-positive subcohort in Stage 2",
            "other_stated",
            "none",
            None,
        ),
        (None, "not_stated", "none", "field_absent"),
        ("", "not_stated", "none", "field_absent"),
        ("   \t\n ", "not_stated", "none", "field_absent"),
        ("Not Specified", "not_stated", "placeholder", "placeholder_token"),
        ("N/A", "not_stated", "placeholder", "placeholder_token"),
        ("ITT and Per-Protocol", "ambiguous_multi", "named_label", None),
        ("ITT and mITT", "ambiguous_multi", "named_label", None),
        ("Full Analysis Set (ITT)", "ambiguous_multi", "named_label", None),
        ("Per-protocol evaluable", "ambiguous_multi", "named_label", None),
    ],
)
def test_taxonomy_vocabulary_and_tier_precedence(
    raw: str | None,
    expected_class: str,
    expected_tier: str,
    expected_reason: str | None,
) -> None:
    res = ap.classify_population_description(raw)
    assert len(ap.POPULATION_TAXONOMY) == 9
    assert len(set(ap.POPULATION_TAXONOMY)) == 9
    assert res.population_class == expected_class
    assert res.population_class in ap.POPULATION_TAXONOMY
    assert res.match_tier == expected_tier
    assert res.not_stated_reason == expected_reason
    assert res.population_description_raw == raw
    assert res.raw_population_description == raw
    if expected_class == "ambiguous_multi":
        assert len(res.matched_classes) >= 2
        order_idx = [ap.POPULATION_TAXONOMY.index(c) for c in res.matched_classes]
        assert order_idx == sorted(order_idx)


# ---------------------------------------------------------------------------
# 2. Span masking and medical false-positive guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_class"),
    [
        (
            "Modified Intent-to-Treat (mITT): all randomized patients receiving >=1 dose",
            "modified_itt",
        ),
        ("Full Analysis Set (FAS): all randomized subjects", "full_analysis_set"),
        ("Safety evaluable population", "safety"),
        (
            "ITT population (non-evaluable patients imputed as non-responders)",
            "itt",
        ),
        ("Patients with Fas ligand positive tumors", "other_stated"),
        ("Patients with palmoplantar pustulosis (PPP)", "other_stated"),
        ("All randomized participants excluding 4 screen failures", "other_stated"),
        ("Subjects completing 12-week safety follow-up", "other_stated"),
    ],
)
def test_span_masking_and_medical_false_positive_guards(
    raw: str,
    expected_class: str,
) -> None:
    res = ap.classify_population_description(raw)
    assert res.population_class == expected_class


# ---------------------------------------------------------------------------
# 3. Verbatim raw preservation and Unicode hyphens / zero-width chars
# ---------------------------------------------------------------------------


def test_raw_verbatim_preservation_and_unicode_hyphens() -> None:
    raw = "  Intent\u2011to\u2011Treat\u200b (ITT)  "
    res = ap.classify_population_description(raw)
    assert res.population_class == "itt"
    assert res.population_description_raw == raw
    assert res.raw_population_description == raw
    assert ap.normalize_population_text(raw) == "Intent-to-Treat (ITT)"


# ---------------------------------------------------------------------------
# 4. VOCABULARY_SHA256 pinning and order/pattern sensitivity
# ---------------------------------------------------------------------------


def test_vocabulary_sha256_is_pinned_and_sensitive_to_order() -> None:
    assert len(ap.VOCABULARY_SHA256) == 64
    assert ap.VOCABULARY_SHA256 == PINNED_VOCABULARY_SHA256
    assert ap.POPULATION_PARSER_VERSION == ap.PARSER_RULES_VERSION

    swapped = list(ap.VOCABULARY_RULES)
    swapped[1], swapped[2] = swapped[2], swapped[1]
    assert ap.compute_vocabulary_sha256(swapped) != ap.VOCABULARY_SHA256

    edited = list(ap.VOCABULARY_RULES)
    r0 = edited[0]
    edited[0] = ap.VocabRule(
        rule_id=r0.rule_id,
        tier=r0.tier,
        target_class=r0.target_class,
        pattern=r0.pattern + "|blank",
        masks_for=r0.masks_for,
        forbids_pattern=r0.forbids_pattern,
        note=r0.note,
    )
    assert ap.compute_vocabulary_sha256(edited) != ap.VOCABULARY_SHA256


# ---------------------------------------------------------------------------
# 5. Outcome denoms: disjoint arms vs explicit Total vs pooled group
# ---------------------------------------------------------------------------


def test_outcome_denoms_disjoint_arms_vs_explicit_total_group() -> None:
    disjoint_measure: dict[str, Any] = {
        "type": "PRIMARY",
        "title": "Overall Survival",
        "populationDescription": "ITT",
        "groups": [
            {"id": "OG000", "title": "Drug A"},
            {"id": "OG001", "title": "Placebo"},
        ],
        "denoms": [
            {
                "units": "Participants",
                "counts": [
                    {"groupId": "OG000", "value": "50"},
                    {"groupId": "OG001", "value": "52"},
                ],
            }
        ],
    }
    p_disjoint = ap.parse_outcome_measure(disjoint_measure)
    assert p_disjoint.overall_participants_analyzed == 102.0
    assert p_disjoint.aggregation_method == "sum_disjoint_arms"
    assert p_disjoint.denom_status == "present"
    assert p_disjoint.is_participant_count is True
    assert p_disjoint.denom_unit_kind == "PARTICIPANTS"
    assert p_disjoint.flag_total_group_mismatch is False

    total_ok_measure: dict[str, Any] = {
        "type": "PRIMARY",
        "title": "Overall Survival",
        "populationDescription": "ITT",
        "groups": [
            {"id": "OG000", "title": "Drug A"},
            {"id": "OG001", "title": "Placebo"},
            {"id": "OG002", "title": "Total"},
        ],
        "denoms": [
            {
                "units": "Participants",
                "counts": [
                    {"groupId": "OG000", "value": "50"},
                    {"groupId": "OG001", "value": "52"},
                    {"groupId": "OG002", "value": "102"},
                ],
            }
        ],
    }
    p_total_ok = ap.parse_outcome_measure(total_ok_measure)
    assert p_total_ok.overall_participants_analyzed == 102.0
    assert p_total_ok.aggregation_method == "explicit_total_group_sumcheck"
    assert p_total_ok.flag_total_group_mismatch is False

    total_mismatch_measure: dict[str, Any] = {
        "type": "PRIMARY",
        "title": "Overall Survival",
        "populationDescription": "ITT",
        "groups": [
            {"id": "OG000", "title": "Drug A"},
            {"id": "OG001", "title": "Placebo"},
            {"id": "OG002", "title": "Total"},
        ],
        "denoms": [
            {
                "units": "Participants",
                "counts": [
                    {"groupId": "OG000", "value": "50"},
                    {"groupId": "OG001", "value": "52"},
                    {"groupId": "OG002", "value": "98"},
                ],
            }
        ],
    }
    p_total_bad = ap.parse_outcome_measure(total_mismatch_measure)
    assert p_total_bad.overall_participants_analyzed == 98.0
    assert p_total_bad.aggregation_method == "explicit_total_group_unverified"
    assert p_total_bad.flag_total_group_mismatch is True

    pooled_measure: dict[str, Any] = {
        "type": "PRIMARY",
        "title": "Response Rate",
        "populationDescription": "FAS",
        "groups": [
            {"id": "OG000", "title": "Dose 10 mg"},
            {"id": "OG001", "title": "Dose 20 mg"},
            {"id": "OG002", "title": "Pooled Active Doses"},
        ],
        "denoms": [
            {
                "units": "Participants",
                "counts": [
                    {"groupId": "OG000", "value": "30"},
                    {"groupId": "OG001", "value": "40"},
                    {"groupId": "OG002", "value": "70"},
                ],
            }
        ],
    }
    p_pooled = ap.parse_outcome_measure(pooled_measure)
    assert p_pooled.overall_participants_analyzed == 70.0
    assert p_pooled.aggregation_method == "pooled_group_sumcheck"


# ---------------------------------------------------------------------------
# 6. Missing counts remain None with explicit reason (never coerced to 0)
# ---------------------------------------------------------------------------


def test_missing_counts_are_none_with_reason_never_zero() -> None:
    p_not_posted = ap.parse_outcome_measure(
        {"type": "PRIMARY", "reportingStatus": "NOT_POSTED"}
    )
    assert p_not_posted.denom_status == "not_posted"
    assert p_not_posted.overall_participants_analyzed is None
    assert p_not_posted.is_participant_count is False

    p_no_denoms = ap.parse_outcome_measure({"type": "PRIMARY", "title": "No Denoms"})
    assert p_no_denoms.denom_status == "no_denoms_array"
    assert p_no_denoms.overall_participants_analyzed is None

    p_empty = ap.parse_outcome_measure(
        {
            "type": "PRIMARY",
            "denoms": [{"units": "Participants", "counts": []}],
        }
    )
    assert p_empty.denom_status == "empty_counts"
    assert p_empty.overall_participants_analyzed is None

    p_na = ap.parse_outcome_measure(
        {
            "type": "PRIMARY",
            "groups": [{"id": "OG000", "title": "Arm 1"}],
            "denoms": [
                {
                    "units": "Participants",
                    "counts": [{"groupId": "OG000", "value": "NA"}],
                }
            ],
        }
    )
    assert p_na.denom_status == "non_numeric_or_na"
    assert p_na.overall_participants_analyzed is None
    assert p_na.arms[0].analyzed_participants is None

    p_partial = ap.parse_outcome_measure(
        {
            "type": "PRIMARY",
            "groups": [
                {"id": "OG000", "title": "Arm A"},
                {"id": "OG001", "title": "Arm B"},
            ],
            "denoms": [
                {
                    "units": "Participants",
                    "counts": [
                        {"groupId": "OG000", "value": "50"},
                        {"groupId": "OG001", "value": "NA"},
                    ],
                }
            ],
        }
    )
    assert p_partial.denom_status == "partial_arm_counts"
    assert p_partial.flag_partial_arm_counts is True
    assert p_partial.overall_participants_analyzed is None
    assert p_partial.arms[0].analyzed_participants == 50.0
    assert p_partial.arms[1].analyzed_participants is None

    # Class-level varying denoms vs class-level unanimous denoms
    p_varying = ap.parse_outcome_measure(
        {
            "type": "PRIMARY",
            "groups": [
                {"id": "OG000", "title": "Arm A"},
                {"id": "OG001", "title": "Arm B"},
            ],
            "classes": [
                {
                    "title": "Week 4",
                    "denoms": [
                        {
                            "units": "Participants",
                            "counts": [
                                {"groupId": "OG000", "value": "50"},
                                {"groupId": "OG001", "value": "50"},
                            ],
                        }
                    ],
                },
                {
                    "title": "Week 12",
                    "denoms": [
                        {
                            "units": "Participants",
                            "counts": [
                                {"groupId": "OG000", "value": "42"},
                                {"groupId": "OG001", "value": "44"},
                            ],
                        }
                    ],
                },
            ],
        }
    )
    assert p_varying.denom_status == "class_level_only_varying"
    assert p_varying.flag_class_varying_denoms is True
    assert p_varying.overall_participants_analyzed is None
    assert p_varying.class_denom_min == 86.0
    assert p_varying.class_denom_max == 100.0

    p_unanimous = ap.parse_outcome_measure(
        {
            "type": "PRIMARY",
            "groups": [
                {"id": "OG000", "title": "Arm A"},
                {"id": "OG001", "title": "Arm B"},
            ],
            "classes": [
                {
                    "title": "Week 4",
                    "denoms": [
                        {
                            "units": "Participants",
                            "counts": [
                                {"groupId": "OG000", "value": "45"},
                                {"groupId": "OG001", "value": "47"},
                            ],
                        }
                    ],
                },
                {
                    "title": "Week 12",
                    "denoms": [
                        {
                            "units": "Participants",
                            "counts": [
                                {"groupId": "OG000", "value": "45"},
                                {"groupId": "OG001", "value": "47"},
                            ],
                        }
                    ],
                },
            ],
        }
    )
    assert p_unanimous.denom_source == "class_unanimous"
    assert p_unanimous.denom_status == "present"
    assert p_unanimous.overall_participants_analyzed == 92.0
    assert p_unanimous.is_participant_count is True


# ---------------------------------------------------------------------------
# 7. Units analyzed (COUNT_OF_UNITS) strictly separated from Participants
# ---------------------------------------------------------------------------


def test_units_analyzed_separated_from_participants() -> None:
    both_measure: dict[str, Any] = {
        "type": "PRIMARY",
        "title": "Visual Acuity",
        "paramType": "MEAN",
        "typeUnitsAnalyzed": "Eyes",
        "populationDescription": "Full Analysis Set",
        "groups": [
            {"id": "OG000", "title": "Active"},
            {"id": "OG001", "title": "Sham"},
        ],
        "denoms": [
            {
                "units": "Participants",
                "counts": [
                    {"groupId": "OG000", "value": "50"},
                    {"groupId": "OG001", "value": "50"},
                ],
            },
            {
                "units": "Eyes",
                "counts": [
                    {"groupId": "OG000", "value": "100"},
                    {"groupId": "OG001", "value": "100"},
                ],
            },
        ],
    }
    p_both = ap.parse_outcome_measure(both_measure)
    assert p_both.overall_participants_analyzed == 100.0
    assert p_both.is_participant_count is True
    assert p_both.units_analyzed_type == "Eyes"
    assert p_both.overall_units_analyzed == 200.0
    assert p_both.units_denom_status == "present"
    assert p_both.denom_unit_kind == "BOTH"

    units_only_measure: dict[str, Any] = {
        "type": "PRIMARY",
        "title": "Lesion Clearance",
        "paramType": "COUNT_OF_UNITS",
        "typeUnitsAnalyzed": "Lesions",
        "populationDescription": "Per-protocol population",
        "groups": [
            {"id": "OG000", "title": "Active"},
            {"id": "OG001", "title": "Vehicle"},
        ],
        "denoms": [
            {
                "units": "Lesions",
                "counts": [
                    {"groupId": "OG000", "value": "110"},
                    {"groupId": "OG001", "value": "90"},
                ],
            }
        ],
    }
    p_units = ap.parse_outcome_measure(units_only_measure)
    assert p_units.overall_participants_analyzed is None
    assert p_units.is_participant_count is False
    assert p_units.denom_status == "units_only_no_participant_denom"
    assert p_units.units_analyzed_type == "Lesions"
    assert p_units.overall_units_analyzed == 200.0
    assert p_units.units_denom_status == "present"
    assert p_units.denom_unit_kind == "COUNT_OF_UNITS"


# ---------------------------------------------------------------------------
# 8. Strict participant-flow alignment and arm_started_minus_analysed
# ---------------------------------------------------------------------------


def test_strict_flow_alignment_and_arm_started_minus_analysed() -> None:
    # Exact normalized title join (reordered) + negative gap preservation
    measure: dict[str, Any] = {
        "type": "PRIMARY",
        "title": "Primary Endpoint",
        "populationDescription": "ITT",
        "groups": [
            {"id": "OG000", "title": "Placebo"},
            {"id": "OG001", "title": "Drug\u2011A"},
            {"id": "OG002", "title": "Total"},
        ],
        "denoms": [
            {
                "units": "Participants",
                "counts": [
                    {"groupId": "OG000", "value": "52"},
                    {"groupId": "OG001", "value": "45"},
                    {"groupId": "OG002", "value": "97"},
                ],
            }
        ],
    }
    study = _study_with_outcome(
        "NCT00000001",
        measure,
        flow_groups=[
            ("FG000", "Drug-A", "50"),
            ("FG001", "Placebo", "50"),
        ],
        enrollment=100,
    )
    parsed = ap.parse_study_outcomes(study, snapshot_date="2026-11-20")
    out = parsed.outcomes[0]
    by_og = {a.outcome_group_id: a for a in out.arms}

    assert by_og["OG000"].matched_flow_group_id == "FG001"
    assert by_og["OG000"].arm_started_minus_analysed == -2.0
    assert by_og["OG000"].arm_gap_reason == "computed"
    assert by_og["OG000"].flag_analyzed_exceeds_started is True

    assert by_og["OG001"].matched_flow_group_id == "FG000"
    assert by_og["OG001"].arm_started_minus_analysed == 5.0
    assert by_og["OG001"].arm_gap_reason == "computed"
    assert by_og["OG001"].flag_analyzed_exceeds_started is False

    assert by_og["OG002"].arm_gap_reason == "total_group_not_arm"
    assert by_og["OG002"].arm_started_minus_analysed is None
    assert out.flag_positive_attrition_gap is True
    assert out.flag_analyzed_exceeds_started is True
    assert out.overall_started_participants == 100.0
    assert out.overall_started_minus_analyzed == 3.0

    # Refusal to guess positional join across distinct non-matching arm titles
    conflict_measure: dict[str, Any] = {
        "type": "PRIMARY",
        "title": "Primary Endpoint",
        "populationDescription": "ITT",
        "groups": [
            {"id": "OG000", "title": "Cohort X"},
            {"id": "OG001", "title": "Cohort Y"},
        ],
        "denoms": [
            {
                "units": "Participants",
                "counts": [
                    {"groupId": "OG000", "value": "40"},
                    {"groupId": "OG001", "value": "40"},
                ],
            }
        ],
    }
    conflict_study = _study_with_outcome(
        "NCT00000002",
        conflict_measure,
        flow_groups=[("FG000", "Drug A", "50"), ("FG001", "Drug B", "50")],
    )
    conflict_out = ap.parse_study_outcomes(conflict_study).outcomes[0]
    for arm in conflict_out.arms:
        assert arm.matched_flow_group_id is None
        assert arm.arm_started_minus_analysed is None
        assert arm.arm_gap_reason == "group_count_or_title_mismatch"

    # Generic numbered positional fallback ("Arm 1" / "Arm 2" <-> "Group 1" / "Group 2")
    generic_measure: dict[str, Any] = {
        "type": "PRIMARY",
        "title": "Generic Arms",
        "populationDescription": "ITT",
        "groups": [
            {"id": "OG000", "title": "Group 1"},
            {"id": "OG001", "title": "Group 2"},
        ],
        "denoms": [
            {
                "units": "Participants",
                "counts": [
                    {"groupId": "OG000", "value": "48"},
                    {"groupId": "OG001", "value": "47"},
                ],
            }
        ],
    }
    generic_study = _study_with_outcome(
        "NCT00000003",
        generic_measure,
        flow_groups=[("FG000", "Arm 1", "50"), ("FG001", "Arm 2", "50")],
    )
    generic_out = ap.parse_study_outcomes(generic_study).outcomes[0]
    assert [a.arm_started_minus_analysed for a in generic_out.arms] == [2.0, 3.0]
    assert all(a.arm_gap_reason == "computed" for a in generic_out.arms)

    # Duplicate normalized titles guard
    dup_study = _study_with_outcome(
        "NCT00000004",
        generic_measure,
        flow_groups=[("FG000", "Active", "50"), ("FG001", "Active", "50")],
    )
    dup_out = ap.parse_study_outcomes(dup_study).outcomes[0]
    assert all(a.arm_gap_reason == "duplicate_group_titles" for a in dup_out.arms)
    assert all(a.arm_started_minus_analysed is None for a in dup_out.arms)


# ---------------------------------------------------------------------------
# 9. Attrition gap NEVER alters population_class (runtime + AST inspection)
# ---------------------------------------------------------------------------


def test_attrition_gap_never_alters_population_class_ast_and_runtime() -> None:
    # Runtime: blank populationDescription with 25-patient gap stays not_stated
    blank_study = _study_with_outcome(
        "NCT00000010",
        {
            "type": "PRIMARY",
            "title": "Outcome 1",
            "populationDescription": "",
            "groups": [{"id": "OG000", "title": "Drug"}],
            "denoms": [
                {
                    "units": "Participants",
                    "counts": [{"groupId": "OG000", "value": "75"}],
                }
            ],
        },
        flow_groups=[("FG000", "Drug", "100")],
        enrollment=100,
    )
    blank_out = ap.parse_study_outcomes(blank_study).outcomes[0]
    assert blank_out.population_class == "not_stated"
    assert blank_out.not_stated_reason == "field_absent"
    assert blank_out.arms[0].arm_started_minus_analysed == 25.0

    # Runtime: ITT with 12-patient gap stays itt (never rewritten to modified_itt/per_protocol)
    itt_study = _study_with_outcome(
        "NCT00000011",
        {
            "type": "PRIMARY",
            "title": "Outcome 1",
            "populationDescription": "ITT",
            "groups": [{"id": "OG000", "title": "Drug"}],
            "denoms": [
                {
                    "units": "Participants",
                    "counts": [{"groupId": "OG000", "value": "88"}],
                }
            ],
        },
        flow_groups=[("FG000", "Drug", "100")],
        enrollment=100,
    )
    itt_out = ap.parse_study_outcomes(itt_study).outcomes[0]
    assert itt_out.population_class == "itt"
    assert itt_out.arms[0].arm_started_minus_analysed == 12.0

    # AST inspection: classify_population_description takes ONLY raw: str | None
    sig = inspect.signature(ap.classify_population_description)
    assert list(sig.parameters.keys()) == ["raw"]

    source = inspect.getsource(ap.classify_population_description)
    tree = ast.parse(source)
    func_node = tree.body[0]
    assert isinstance(func_node, ast.FunctionDef)
    if (
        func_node.body
        and isinstance(func_node.body[0], ast.Expr)
        and isinstance(getattr(func_node.body[0], "value", None), ast.Constant)
        and isinstance(func_node.body[0].value.value, str)
    ):
        func_node.body = func_node.body[1:]
    code_text = ast.unparse(tree)
    for forbidden in (
        "arm_started_minus_analysed",
        "overall_started",
        "enrollment",
        "flow_groups",
        "analyzed_participants",
        "denom",
    ):
        assert forbidden not in code_text, (
            f"{forbidden!r} must not appear in classify_population_description"
        )


# ---------------------------------------------------------------------------
# 10. Retained raw outcome re-parse parity, parse_error & structural_checks
# ---------------------------------------------------------------------------


def test_retained_raw_outcomes_reparse_parity_and_structural_checks() -> None:
    study = _study_with_outcome(
        "NCT00000020",
        {
            "type": "PRIMARY",
            "title": "Primary PFS",
            "timeFrame": "24 months",
            "reportingStatus": "POSTED",
            "paramType": "MEDIAN",
            "unitOfMeasure": "months",
            "populationDescription": "Full Analysis Set (FAS): all randomized subjects",
            "groups": [
                {"id": "OG000", "title": "Experimental"},
                {"id": "OG001", "title": "Control"},
            ],
            "denoms": [
                {
                    "units": "Participants",
                    "counts": [
                        {"groupId": "OG000", "value": "120"},
                        {"groupId": "OG001", "value": "118"},
                    ],
                }
            ],
        },
        flow_groups=[
            ("FG000", "Experimental", "125"),
            ("FG001", "Control", "125"),
        ],
        enrollment=250,
        extra_measures=[
            {
                "type": "SECONDARY",
                "title": "Safety Events",
                "populationDescription": "Safety population",
                "groups": [
                    {"id": "OG000", "title": "Experimental"},
                    {"id": "OG001", "title": "Control"},
                ],
                "denoms": [
                    {
                        "units": "Participants",
                        "counts": [
                            {"groupId": "OG000", "value": "124"},
                            {"groupId": "OG001", "value": "122"},
                        ],
                    }
                ],
            }
        ],
    )

    direct = ap.parse_study_outcomes(study, snapshot_date="2026-11-20")
    raw = ap.select_raw_outcome_populations(study)
    reparsed = ap.build_outcomes_from_raw(raw, snapshot_date="2026-11-20")
    assert direct == reparsed
    assert direct.primary_population_classes == ["full_analysis_set"]
    assert direct.n_primary_outcomes == 1
    assert direct.n_outcomes_total == 2

    err_trial = ap.parse_error_trial_outcomes(
        "NCT00000099", 50, "2026-11-20", "corrupt_json"
    )
    assert err_trial.parse_ok is False
    assert err_trial.nct_id == "NCT00000099"
    assert err_trial.outcomes[0].parse_ok is False
    assert err_trial.outcomes[0].population_class is None

    checks = ap.structural_checks([direct, err_trial])
    assert ap.all_pass(checks), checks

    # Inject corruption and confirm structural_checks catches it
    corrupt = ap.parse_study_outcomes(study, snapshot_date="2026-11-20")
    corrupt.outcomes[0].population_class = "itt"
    corrupt.outcomes[0].matched_classes = ["itt", "per_protocol"]
    corrupt_checks = ap.structural_checks([corrupt])
    assert corrupt_checks["ambiguous_multi_iff_ge_2_matched_classes"]["pass"] is False

    # Audit helper & optional pandas DataFrame helper
    audit = ap.population_audit(["ITT", "ITT", "Safety population", None])
    assert audit[0]["population_description_raw"] == "ITT"
    assert audit[0]["count"] == 2
    df = ap.parse_outcomes_frame([study], snapshot_date="2026-11-20")
    assert len(df) == 2
    assert list(df["population_class"]) == ["full_analysis_set", "safety"]
