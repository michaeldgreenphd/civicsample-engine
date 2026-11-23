"""Comprehensive unit and invariant tests for src/analysis_population_parser.py."""

from __future__ import annotations

import ast
import builtins
import collections
import inspect
import io
import os
import socket
import types
from pathlib import Path
from typing import Any, Callable

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

    # Audit helper & pandas DataFrame helper (pandas imported lazily inside it)
    audit = ap.population_audit(["ITT", "ITT", "Safety population", None])
    assert audit[0]["population_description_raw"] == "ITT"
    assert audit[0]["count"] == 2
    df = ap.parse_outcomes_frame([study], snapshot_date="2026-11-20")
    assert len(df) == 2
    assert list(df["population_class"]) == ["full_analysis_set", "safety"]


# ---------------------------------------------------------------------------
# 11. Audit holdout (in_audit) refusals
#
# Every id below is fictional (NCT99000001..NCT99999999). A record that
# carries in_audit is refused when it is True, when it is not exactly a bool,
# or when its nct_id is not exactly ^NCT[0-9]{8}$; a record without the key
# is left to the caller, unchanged.
# ---------------------------------------------------------------------------

_HOLDOUT_NCT: str = "NCT99000001"
_CLEAN_NCT: str = "NCT99000002"

# Values an id-shaped check must not accept: case-folded, padded, newline
# (which re "$" alone would accept), wrong digit counts, non-ASCII digits
# (which \d would accept), punctuation, empty, None and a non-string.
_MALFORMED_NCT_IDS: list[Any] = [
    "nct99000002",
    "Nct99000002",
    " NCT99000002",
    "NCT99000002 ",
    "NCT99000002\n",
    "NCT9900002",
    "NCT990000022",
    "NCT" + "٩" * 8,
    "NCT９９000002",
    "NCT-99000002",
    "",
    None,
    99000002,
]

# in_audit values that are present but are not exactly a Python bool.
_NON_BOOL_IN_AUDIT: list[Any] = [1, 0, 1.0, "True", "true", "False", "yes", None, []]


def _holdout_measure() -> dict[str, Any]:
    """One ITT primary measure with a single participant denominator."""
    return {
        "type": "PRIMARY",
        "title": "Primary Endpoint",
        "populationDescription": "ITT",
        "groups": [{"id": "OG000", "title": "Drug"}],
        "denoms": [
            {"units": "Participants", "counts": [{"groupId": "OG000", "value": "90"}]}
        ],
    }


def _study_carrying(nct_id: Any, **extra: Any) -> dict[str, Any]:
    """A fictional CT.gov-shaped study whose nctId is exactly ``nct_id``."""
    study = _study_with_outcome(
        _CLEAN_NCT, _holdout_measure(), flow_groups=[("FG000", "Drug", "100")]
    )
    study["protocolSection"]["identificationModule"]["nctId"] = nct_id
    study.update(extra)
    return study


_STUDY_ENTRY_POINTS: list[tuple[str, Callable[[dict[str, Any]], Any]]] = [
    ("parse_study_outcomes", ap.parse_study_outcomes),
    ("select_raw_outcome_populations", ap.select_raw_outcome_populations),
    ("parse_outcomes_frame", lambda study: ap.parse_outcomes_frame([study])),
]


def _retained_carrying(nct_id: Any, **extra: Any) -> dict[str, Any]:
    """A retained raw outcome record (select_raw_outcome_populations shape)."""
    raw = ap.select_raw_outcome_populations(_study_carrying(_CLEAN_NCT))
    raw["nct_id"] = nct_id
    raw.update(extra)
    return raw


@pytest.mark.parametrize(("name", "entry"), _STUDY_ENTRY_POINTS)
def test_study_entry_points_refuse_in_audit_true(
    name: str, entry: Callable[[dict[str, Any]], Any]
) -> None:
    for nct_id in [_HOLDOUT_NCT, " nct-malformed "]:
        with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
            entry(_study_carrying(nct_id, in_audit=True))
        # The holdout check runs before the id check, so a malformed id on a
        # holdout record is still reported as a holdout.
        assert exc_info.value.reason == "in_audit_true"
        # parse_outcomes_frame refuses through parse_study_outcomes.
        refusing = "parse_study_outcomes" if name == "parse_outcomes_frame" else name
        assert refusing in str(exc_info.value)
        assert _HOLDOUT_NCT not in str(exc_info.value)


@pytest.mark.parametrize(("name", "entry"), _STUDY_ENTRY_POINTS)
@pytest.mark.parametrize("flag", _NON_BOOL_IN_AUDIT)
def test_study_entry_points_refuse_in_audit_not_bool(
    name: str, entry: Callable[[dict[str, Any]], Any], flag: Any
) -> None:
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        entry(_study_carrying(_HOLDOUT_NCT, in_audit=flag))
    assert exc_info.value.reason == "in_audit_not_bool"
    assert _HOLDOUT_NCT not in str(exc_info.value)


def test_study_entry_points_refuse_numpy_bool_in_a_dict_record() -> None:
    np = pytest.importorskip("numpy")
    for flag in (np.bool_(False), np.bool_(True)):
        with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
            ap.parse_study_outcomes(_study_carrying(_CLEAN_NCT, in_audit=flag))
        assert exc_info.value.reason == "in_audit_not_bool"


@pytest.mark.parametrize(("name", "entry"), _STUDY_ENTRY_POINTS)
@pytest.mark.parametrize("nct_id", _MALFORMED_NCT_IDS)
def test_study_entry_points_refuse_malformed_nct_id_when_in_audit_false(
    name: str, entry: Callable[[dict[str, Any]], Any], nct_id: Any
) -> None:
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        entry(_study_carrying(nct_id, in_audit=False))
    assert exc_info.value.reason == "nct_id_malformed"


@pytest.mark.parametrize(("name", "entry"), _STUDY_ENTRY_POINTS)
def test_study_entry_points_refuse_missing_nct_id_when_in_audit_false(
    name: str, entry: Callable[[dict[str, Any]], Any]
) -> None:
    no_key = _study_carrying(_CLEAN_NCT, in_audit=False)
    del no_key["protocolSection"]["identificationModule"]["nctId"]
    no_ident = _study_carrying(_CLEAN_NCT, in_audit=False)
    del no_ident["protocolSection"]["identificationModule"]
    no_proto = _study_carrying(_CLEAN_NCT, in_audit=False)
    del no_proto["protocolSection"]
    # A top-level nct_id is not the id parse_study_outcomes stamps, so it
    # does not stand in for a missing nested nctId.
    top_level_only = _study_carrying(_CLEAN_NCT, in_audit=False)
    top_level_only["nct_id"] = _CLEAN_NCT
    del top_level_only["protocolSection"]["identificationModule"]["nctId"]
    for study in (no_key, no_ident, no_proto, top_level_only):
        with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
            entry(study)
        assert exc_info.value.reason == "nct_id_malformed"


@pytest.mark.parametrize(("name", "entry"), _STUDY_ENTRY_POINTS)
def test_study_entry_points_in_audit_false_matches_record_without_key(
    name: str, entry: Callable[[dict[str, Any]], Any]
) -> None:
    with_key = entry(_study_carrying(_CLEAN_NCT, in_audit=False))
    without_key = entry(_study_carrying(_CLEAN_NCT))
    if name == "parse_outcomes_frame":
        assert with_key.equals(without_key)
        assert list(with_key["nct_id"]) == [_CLEAN_NCT]
    else:
        assert with_key == without_key
    if name == "select_raw_outcome_populations":
        assert "in_audit" not in with_key


def test_records_without_in_audit_are_left_to_the_caller_unchanged() -> None:
    # No in_audit key: the module does not validate the id (the caller owns
    # holdout exclusion), so existing behaviour is untouched.
    parsed = ap.parse_study_outcomes(_study_carrying("not-an-nct-id"))
    assert parsed.nct_id == "not-an-nct-id"
    assert parsed.parse_ok is True
    raw = ap.select_raw_outcome_populations(_study_carrying(None))
    assert raw["nct_id"] is None
    assert ap.build_outcomes_from_raw(raw).nct_id is None


def test_parse_outcomes_frame_refuses_whole_batch_on_one_holdout() -> None:
    studies = [
        _study_carrying(_CLEAN_NCT, in_audit=False),
        _study_carrying(_HOLDOUT_NCT, in_audit=True),
    ]
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        ap.parse_outcomes_frame(iter(studies))
    assert exc_info.value.reason == "in_audit_true"


def test_build_outcomes_from_raw_refusals() -> None:
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        ap.build_outcomes_from_raw(_retained_carrying(_HOLDOUT_NCT, in_audit=True))
    assert exc_info.value.reason == "in_audit_true"
    assert _HOLDOUT_NCT not in str(exc_info.value)

    for flag in _NON_BOOL_IN_AUDIT:
        with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
            ap.build_outcomes_from_raw(_retained_carrying(_HOLDOUT_NCT, in_audit=flag))
        assert exc_info.value.reason == "in_audit_not_bool"

    for nct_id in _MALFORMED_NCT_IDS:
        with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
            ap.build_outcomes_from_raw(_retained_carrying(nct_id, in_audit=False))
        assert exc_info.value.reason == "nct_id_malformed"

    missing = _retained_carrying(_CLEAN_NCT, in_audit=False)
    del missing["nct_id"]
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        ap.build_outcomes_from_raw(missing)
    assert exc_info.value.reason == "nct_id_malformed"

    clean = ap.build_outcomes_from_raw(
        _retained_carrying(_CLEAN_NCT, in_audit=False), snapshot_date="2026-11-20"
    )
    assert clean == ap.build_outcomes_from_raw(
        _retained_carrying(_CLEAN_NCT), snapshot_date="2026-11-20"
    )
    assert clean == ap.parse_study_outcomes(
        _study_carrying(_CLEAN_NCT), snapshot_date="2026-11-20"
    )


def test_population_holdout_error_contract() -> None:
    assert not issubclass(ap.PopulationAuditHoldoutError, ValueError)
    assert ap.HOLDOUT_REFUSAL_REASONS == (
        "in_audit_true",
        "in_audit_not_bool",
        "nct_id_malformed",
    )
    with pytest.raises(ValueError):
        ap.PopulationAuditHoldoutError("nct_id_in_audit_set", "not a reason")


# ---------------------------------------------------------------------------
# 12. Red team, M2 round 1: holdout bypass attempts (test_redteam_m2_*)
#
# Each test asserts the CORRECT behaviour, which is refusal, for one attack
# on an entry point that enforces the holdout. A test that fails here names
# a bypass that is still open; it is not a test to loosen. Every id is
# fictional (NCT99000001..NCT99999999) and no test touches the network or a
# file. The holdout trial is NCT99000001 (_HOLDOUT_NCT); NCT99000002
# (_CLEAN_NCT) is a trial the record says is not held out.
# ---------------------------------------------------------------------------


_REDTEAM_MARKER: str = "REDTEAM-M2-HOLDOUT-DESCRIPTION-MARKER"

# Entry points that take one CT.gov-shaped study, and the retained-record one.
_REDTEAM_STUDY_ENTRY_POINTS: list[tuple[str, Callable[[Any], Any]]] = [
    ("parse_study_outcomes", ap.parse_study_outcomes),
    ("select_raw_outcome_populations", ap.select_raw_outcome_populations),
    ("parse_outcomes_frame", lambda study: ap.parse_outcomes_frame([study])),
]
_REDTEAM_ALL_ENTRY_POINTS: list[tuple[str, Callable[[Any], Any], bool]] = [
    (name, entry, False) for name, entry in _REDTEAM_STUDY_ENTRY_POINTS
] + [("build_outcomes_from_raw", ap.build_outcomes_from_raw, True)]


def _redteam_holdout_record(retained: bool, **extra: Any) -> dict[str, Any]:
    """The fictional holdout trial as a study dict, or as a retained raw record."""
    if retained:
        return _retained_carrying(_HOLDOUT_NCT, **extra)
    return _study_carrying(_HOLDOUT_NCT, **extra)


def _redteam_mapping_wrappers() -> list[tuple[str, Callable[[dict[str, Any]], Any]]]:
    """Read-only or dict-like Mappings that are not dict subclasses.

    Method: each wraps the same holdout record. A caller gets these from
    collections.UserDict, from a frozen view (MappingProxyType) or from
    pandas (df.iterrows() yields Series, whose ``in`` and ``.get`` read the
    index labels). None is a dict, so an ``isinstance(record, dict)`` guard
    skips it.
    """
    pd = pytest.importorskip("pandas")
    return [
        ("UserDict", collections.UserDict),
        ("MappingProxyType", types.MappingProxyType),
        ("pandas.Series", pd.Series),
    ]


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_redteam_m2_non_dict_mapping_carrying_in_audit_true_is_refused(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # Attack: hand the holdout record over as a Mapping that is not a dict.
    # _refuse_audit_holdout_record returns early for any non-dict, and the
    # entry point then emits a parse-error record (parse_outcomes_frame a
    # row) for the trial instead of refusing it. A refused record gets no
    # parse-error record either (module docstring), so the correct result is
    # a PopulationAuditHoldoutError.
    for wrapper_name, wrap in _redteam_mapping_wrappers():
        held = wrap(_redteam_holdout_record(retained, in_audit=True))
        with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
            entry(held)
        assert exc_info.value.reason == "in_audit_true", wrapper_name


def test_redteam_m2_parse_outcomes_frame_mixed_batch_with_non_dict_holdout_is_refused() -> None:
    # Attack: a list mixing a clean dict record and the holdout as a
    # UserDict. Observed without the fix: a two-row frame, the second row a
    # parse error for the holdout trial.
    studies = [
        _study_carrying(_CLEAN_NCT, in_audit=False),
        collections.UserDict(_study_carrying(_HOLDOUT_NCT, in_audit=True)),
    ]
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        ap.parse_outcomes_frame(studies)
    assert exc_info.value.reason == "in_audit_true"


def test_redteam_m2_parse_outcomes_frame_single_record_instead_of_list_is_refused() -> None:
    # Attack: pass the holdout study itself where an iterable of studies is
    # expected. Iterating a dict yields its keys, so each top-level key
    # becomes a "study_not_a_dict" parse-error row of the holdout trial
    # rather than a refusal.
    held = _study_carrying(_HOLDOUT_NCT, in_audit=True)
    with pytest.raises((ap.PopulationAuditHoldoutError, TypeError)):
        ap.parse_outcomes_frame(held)  # type: ignore[arg-type]


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_redteam_m2_nested_in_audit_true_is_refused(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # Attack: the record says it is a holdout, but below the top level, so
    # the top-level ``"in_audit" in record`` test does not see it. Shapes:
    # the flag beside the id it describes (identificationModule), on
    # protocolSection, or in a merged manifest sub-record. The correct
    # result is refusal: a record that marks itself in_audit anywhere is a
    # holdout record.
    shapes: list[dict[str, Any]] = []
    if retained:
        manifest_nested = _retained_carrying(_HOLDOUT_NCT)
        manifest_nested["manifest"] = {"nct_id": _HOLDOUT_NCT, "in_audit": True}
        flow_nested = _retained_carrying(_HOLDOUT_NCT)
        flow_nested["participant_flow_module"] = dict(
            flow_nested["participant_flow_module"] or {}, in_audit=True
        )
        shapes = [manifest_nested, flow_nested]
    else:
        in_ident = _study_carrying(_HOLDOUT_NCT)
        in_ident["protocolSection"]["identificationModule"]["in_audit"] = True
        in_proto = _study_carrying(_HOLDOUT_NCT)
        in_proto["protocolSection"]["in_audit"] = True
        manifest_nested = _study_carrying(_HOLDOUT_NCT)
        manifest_nested["manifest"] = {"nct_id": _HOLDOUT_NCT, "in_audit": True}
        shapes = [in_ident, in_proto, manifest_nested]
    for held in shapes:
        with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
            entry(held)
        assert exc_info.value.reason == "in_audit_true"


def test_redteam_m2_parse_outcomes_frame_batch_with_a_record_missing_in_audit_is_refused() -> None:
    # Attack: a manifest-merged batch in which one record lost its in_audit
    # key (a left join that found no manifest row). Every other record
    # carries the key, so the batch is manifest-merged and the missing one
    # is unknown, not clean; the frame path of sponsors/ecosystem.py refuses
    # the same gap (a NaN in its in_audit column). Observed without the fix:
    # both studies parsed, the holdout's outcome classified "itt".
    studies = [
        _study_carrying(_CLEAN_NCT, in_audit=False),
        _study_carrying(_HOLDOUT_NCT),
    ]
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        ap.parse_outcomes_frame(studies)
    assert exc_info.value.reason in ap.HOLDOUT_REFUSAL_REASONS


@pytest.mark.parametrize(("name", "entry"), _REDTEAM_STUDY_ENTRY_POINTS)
def test_redteam_m2_conflicting_top_level_nct_id_is_refused(
    name: str, entry: Callable[[Any], Any]
) -> None:
    # Attack: the manifest row (in_audit False, for the clean trial) is
    # merged onto the holdout trial's payload, leaving two different ids in
    # one record: top-level nct_id (from the manifest) and the nested nctId
    # (from the payload). The check reads only the nested one, accepts it
    # and parses a trial the in_audit flag does not describe. Correct: an
    # id disagreement is refused rather than resolved.
    study = _study_carrying(_HOLDOUT_NCT, in_audit=False)
    study["nct_id"] = _CLEAN_NCT
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        entry(study)
    assert exc_info.value.reason == "nct_id_malformed"


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
@pytest.mark.parametrize("key", ["inAudit", "In_Audit", "IN_AUDIT", "in_audit ", "in-audit"])
def test_redteam_m2_in_audit_key_spelling_variants_are_refused(
    name: str, entry: Callable[[Any], Any], retained: bool, key: str
) -> None:
    # Attack: the holdout flag under a spelling a camelCase merge, a
    # spreadsheet header or a stray space produces. The exact-key test
    # misses it and the trial is parsed in full. Correct: a key that reads
    # as in_audit once case, spaces, hyphens and underscores are ignored is
    # treated as in_audit.
    held = _redteam_holdout_record(retained, **{key: True})
    with pytest.raises(ap.PopulationAuditHoldoutError):
        entry(held)


# Ids an id-shaped check must not accept, beyond _MALFORMED_NCT_IDS:
# invisible or whitespace characters a copy-paste or a spreadsheet adds, and
# full-width letters.
_REDTEAM_ODD_NCT_IDS: list[str] = [
    "NCT99000002​",
    "​NCT99000002",
    "NCT99000‍002",
    "﻿NCT99000002",
    "NCT99000002 ",
    "NCT99000002\t",
    "NCT99000002\r",
    "NCT99000002\x00",
    "ＮＣＴ99000002",
    "NCT99000002́",
]


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
@pytest.mark.parametrize("nct_id", _REDTEAM_ODD_NCT_IDS)
def test_redteam_m2_odd_nct_ids_with_in_audit_false_are_refused(
    name: str, entry: Callable[[Any], Any], retained: bool, nct_id: str
) -> None:
    # Attack: in_audit False on an id that only looks canonical. Expected
    # to be refused by the fullmatch on ^NCT[0-9]{8}$ (ASCII, unstripped).
    record = (
        _retained_carrying(nct_id, in_audit=False)
        if retained
        else _study_carrying(nct_id, in_audit=False)
    )
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        entry(record)
    assert exc_info.value.reason == "nct_id_malformed"


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_redteam_m2_odd_in_audit_values_are_refused(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # Attack: in_audit values a CSV, a form or a JSON writer produces that a
    # truthiness test would read as False (or as True), none of them a bool.
    np = pytest.importorskip("numpy")
    odd: list[Any] = [
        "false", "FALSE", "", "0", "no", b"False", b"True", 0.0, -0.0,
        float("nan"), {}, {"value": False}, (), np.False_, np.True_, np.int64(0),
    ]
    for flag in odd:
        with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
            entry(_redteam_holdout_record(retained, in_audit=flag))
        assert exc_info.value.reason == "in_audit_not_bool", repr(flag)


def test_redteam_m2_lists_mixing_clean_and_holdout_refuse_wherever_the_holdout_sits() -> None:
    # Attack: the holdout first, last, or between clean records, from a
    # list or a one-shot generator, with a malformed id on the holdout too.
    # Expected: the whole batch is refused and no frame is returned.
    def batch(position: int, nct_id: str) -> list[dict[str, Any]]:
        studies = [_study_carrying(_CLEAN_NCT, in_audit=False) for _ in range(2)]
        studies.insert(position, _study_carrying(nct_id, in_audit=True))
        return studies

    for position in (0, 1, 2):
        for nct_id in (_HOLDOUT_NCT, "nct99000001", "NCT99000001​"):
            for source in (batch(position, nct_id), iter(batch(position, nct_id))):
                with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
                    ap.parse_outcomes_frame(source)
                assert exc_info.value.reason == "in_audit_true"


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_redteam_m2_refusal_records_nothing_about_the_holdout(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # Attack: read the holdout's id or description text back out of the
    # refusal itself (message, args, notes, chained exception).
    if retained:
        held = _retained_carrying(_HOLDOUT_NCT, in_audit=True)
        held["outcome_measures"][0]["populationDescription"] = _REDTEAM_MARKER
    else:
        held = _study_carrying(_HOLDOUT_NCT, in_audit=True)
        measures = held["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"]
        measures[0]["populationDescription"] = _REDTEAM_MARKER
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        entry(held)
    exc = exc_info.value
    texts = [str(exc), repr(exc), *map(str, exc.args), *getattr(exc, "__notes__", [])]
    assert exc.__cause__ is None and exc.__context__ is None
    for text in texts:
        assert _HOLDOUT_NCT not in text
        assert _REDTEAM_MARKER not in text


def test_redteam_m2_entry_points_reject_an_audit_id_set_argument() -> None:
    # Attack: a caller passes the audit id set (as a list, with odd ids) in
    # the belief that the module filters on it. No entry point takes one,
    # so it must fail loudly (TypeError), never be swallowed by **kwargs.
    audit_ids: list[Any] = [_HOLDOUT_NCT, "nct99000001", " NCT99000001", None]
    study = _study_carrying(_HOLDOUT_NCT)
    calls: list[Callable[[], Any]] = [
        lambda: ap.parse_study_outcomes(study, audit_ids=audit_ids),  # type: ignore[call-arg]
        lambda: ap.select_raw_outcome_populations(study, audit_ids=audit_ids),  # type: ignore[call-arg]
        lambda: ap.parse_outcomes_frame([study], audit_ids=audit_ids),  # type: ignore[call-arg]
        lambda: ap.build_outcomes_from_raw(  # type: ignore[call-arg]
            _retained_carrying(_HOLDOUT_NCT), audit_ids=audit_ids
        ),
        lambda: ap.parse_study_outcomes(study, in_audit=True),  # type: ignore[call-arg]
    ]
    for call in calls:
        with pytest.raises(TypeError):
            call()
    public = [
        obj
        for attr, obj in vars(ap).items()
        if callable(obj) and not attr.startswith("_") and getattr(obj, "__module__", "") == ap.__name__
        and inspect.isfunction(obj)
    ]
    for fn in public:
        params = inspect.signature(fn).parameters.values()
        assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params), fn.__name__


def _redteam_forbid_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every file, process and network primitive raise while patched."""

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("I/O attempted by an entry point that relies on its caller")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(io, "open", refuse)
    monkeypatch.setattr(os, "open", refuse)
    monkeypatch.setattr(Path, "open", refuse)
    monkeypatch.setattr(Path, "read_text", refuse)
    monkeypatch.setattr(Path, "write_text", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(builtins, "print", refuse)


def test_redteam_m2_relies_on_caller_entry_points_do_no_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Claim under test: the entry points documented as relying on the caller
    # read only the objects they are handed. Run each one on fictional input
    # with every file, socket and print primitive patched to raise.
    study = _study_carrying(_HOLDOUT_NCT)
    measure = study["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"][0]
    flow_module = study["resultsSection"]["participantFlowModule"]
    parsed = ap.parse_study_outcomes(study)
    _redteam_forbid_io(monkeypatch)
    flow_groups, flow_status = ap.extract_flow_started(flow_module)
    ap.align_flow_and_outcome_groups(flow_groups, measure["groups"])
    ap.parse_outcome_measure(measure, 0, flow_groups, flow_status, 100.0)
    ap.classify_population_description("Intent-to-treat population")
    ap.normalize_population_text("All randomized participants")
    ap.population_audit(["ITT", None, "Per protocol population"])
    ap.structural_checks([parsed])
    ap.parse_error_trial_outcomes(None, None, None, "study_not_a_dict")
    # The enforcing entry points on input without in_audit rely on the
    # caller as well; they must not do I/O either.
    ap.parse_study_outcomes(study)
    ap.build_outcomes_from_raw(ap.select_raw_outcome_populations(study))


def test_redteam_m2_parser_module_imports_and_calls_no_io_primitives() -> None:
    # Static half of the claim "This module does no I/O": the module imports
    # only these standard-library modules (pandas lazily, inside
    # parse_outcomes_frame) and calls no builtin that reads, writes, prints
    # or executes.
    source = inspect.getsource(ap)
    tree = ast.parse(source)
    allowed = {
        "__future__", "hashlib", "json", "math", "re", "collections",
        "dataclasses", "typing", "pandas",
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= allowed, sorted(imported - allowed)
    forbidden_calls = {"open", "print", "input", "exec", "eval", "compile", "__import__"}
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not (called & forbidden_calls), sorted(called & forbidden_calls)
    json_attrs = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "json"
    }
    assert json_attrs <= {"dumps"}, sorted(json_attrs)


def test_redteam_m2_parser_has_no_in_code_caller_on_this_branch() -> None:
    # Claim under test (module docstring): "Callers today: none in code. The
    # only importer in this repository is
    # tests/test_analysis_population_parser.py." Every .py file outside
    # tests/ is parsed and its imports checked.
    repo = Path(__file__).resolve().parents[1]
    importers: list[str] = []
    for path in sorted(repo.rglob("*.py")):
        rel = path.relative_to(repo)
        if rel.parts[0] in {"tests", ".git", ".venv", "venv"} or "__pycache__" in rel.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [
                    f"{node.module}.{alias.name}" for alias in node.names
                ]
            if any(n.endswith("analysis_population_parser") for n in names):
                importers.append(str(rel))
    assert importers == []
