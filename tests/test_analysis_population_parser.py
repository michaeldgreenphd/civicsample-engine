"""Comprehensive unit and invariant tests for src/analysis_population_parser.py."""

from __future__ import annotations

import ast
import builtins
import collections
import inspect
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
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


def _two_class_measure(second_class: dict[str, Any]) -> dict[str, Any]:
    """A class-level measure whose first class has a Participants denom of 45 + 47."""
    return {
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
            second_class,
        ],
    }


def test_class_level_denoms_missing_from_some_classes_are_incomplete_not_unanimous() -> None:
    # Codex P2 on PR #111 (r4663843574): a class without a Participants
    # denominator was skipped, so a two-class measure where only the first
    # class has one was reported class_unanimous with that class's total
    # (92). The second class's analysed count is unknown, so no total stands
    # for the measure: class_level_incomplete, denom_source "none", and no
    # overall_participants_analyzed (never the first class's count).
    second_classes: list[dict[str, Any]] = [
        {"title": "Week 12"},  # no denoms key
        {"title": "Week 12", "denoms": []},  # empty denoms
        {  # a unit denominator only, no Participants entry
            "title": "Week 12",
            "denoms": [
                {"units": "Eyes", "counts": [{"groupId": "OG000", "value": "90"}]}
            ],
        },
    ]
    for second in second_classes:
        parsed = ap.parse_outcome_measure(_two_class_measure(second))
        assert parsed.denom_source == "none", second
        assert parsed.denom_status == "class_level_incomplete", second
        assert parsed.overall_participants_analyzed is None, second
        assert parsed.is_participant_count is False, second
        assert parsed.aggregation_method == "none", second
        assert parsed.flag_class_varying_denoms is False, second
    assert "class_level_incomplete" in ap.DENOM_STATUS_LEVELS
    # The new status passes the vocabulary check of structural_checks.
    trial = ap.parse_study_outcomes(
        _study_with_outcome(
            "NCT99000901", _two_class_measure({"title": "Week 12"}), flow_groups=[]
        )
    )
    assert trial.outcomes[0].denom_status == "class_level_incomplete"
    assert ap.structural_checks([trial])["denom_status_in_vocabulary"]["pass"] is True
    # Every class carrying the same Participants denominator is still unanimous.
    same = _two_class_measure(_two_class_measure({})["classes"][0] | {"title": "Week 12"})
    unanimous = ap.parse_outcome_measure(same)
    assert unanimous.denom_source == "class_unanimous"
    assert unanimous.overall_participants_analyzed == 92.0


def _participants_denom() -> dict[str, Any]:
    """A fresh class-level "Participants" denom of 45 + 47."""
    return {
        "units": "Participants",
        "counts": [
            {"groupId": "OG000", "value": "45"},
            {"groupId": "OG001", "value": "47"},
        ],
    }


def _eyes_denom() -> dict[str, Any]:
    """A fresh class-level "Eyes" unit denom of 90 + 94."""
    return {
        "units": "Eyes",
        "counts": [
            {"groupId": "OG000", "value": "90"},
            {"groupId": "OG001", "value": "94"},
        ],
    }


def _week4_week12_measure(
    week4: list[dict[str, Any]] | None,
    week12: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """A two-arm class-level measure; None leaves a class without a denoms key."""
    classes: list[dict[str, Any]] = []
    for title, denoms in (("Week 4", week4), ("Week 12", week12)):
        cl: dict[str, Any] = {"title": title}
        if denoms is not None:
            cl["denoms"] = denoms
        classes.append(cl)
    return {
        "type": "PRIMARY",
        "groups": [
            {"id": "OG000", "title": "Arm A"},
            {"id": "OG001", "title": "Arm B"},
        ],
        "classes": classes,
    }


def test_unit_denoms_missing_from_some_classes_are_incomplete_not_unanimous() -> None:
    # Codex r4681432176 on PR #111: a class without a unit denominator was
    # skipped, so a two-class measure with an Eyes denominator on Week 4
    # only was reported units_denom_status "present" with Week 4's Eyes
    # total (184) as overall_units_analyzed. Week 12's unit count is
    # unknown, so no unit total stands for the measure:
    # class_level_incomplete and no overall_units_analyzed (never Week 4's).
    week12_shapes: list[list[dict[str, Any]] | None] = [
        [_participants_denom()],  # Participants only, no unit denom
        None,  # no denoms key
        [],  # empty denoms
    ]
    for week12 in week12_shapes:
        parsed = ap.parse_outcome_measure(
            _week4_week12_measure([_participants_denom(), _eyes_denom()], week12)
        )
        assert parsed.units_denom_status == "class_level_incomplete", week12
        assert parsed.overall_units_analyzed is None, week12
        assert parsed.units_analyzed_type == "Eyes", week12

    # The participant side is decided on its own: Participants on every
    # class stays unanimous. BOTH needs a unit count, so PARTICIPANTS.
    part_all = ap.parse_outcome_measure(
        _week4_week12_measure([_participants_denom(), _eyes_denom()], [_participants_denom()])
    )
    assert part_all.denom_source == "class_unanimous"
    assert part_all.denom_status == "present"
    assert part_all.overall_participants_analyzed == 92.0
    assert part_all.denom_unit_kind == "PARTICIPANTS"

    # No Participants denom anywhere: still a units measure, with no total.
    units_only = ap.parse_outcome_measure(_week4_week12_measure([_eyes_denom()], None))
    assert units_only.denom_status == "units_only_no_participant_denom"
    assert units_only.units_denom_status == "class_level_incomplete"
    assert units_only.overall_units_analyzed is None
    assert units_only.denom_unit_kind == "COUNT_OF_UNITS"

    # Both denoms on Week 4 only: both sides incomplete, and the unit denom
    # still marks the measure COUNT_OF_UNITS (it is a units signal, as
    # class_level_only_varying is), not NONE.
    neither = ap.parse_outcome_measure(
        _week4_week12_measure([_participants_denom(), _eyes_denom()], None)
    )
    assert neither.denom_status == "class_level_incomplete"
    assert neither.units_denom_status == "class_level_incomplete"
    assert neither.overall_participants_analyzed is None
    assert neither.overall_units_analyzed is None
    assert neither.denom_unit_kind == "COUNT_OF_UNITS"

    # End to end, the status passes the units vocabulary check of
    # structural_checks, and that check rejects a value outside it.
    trial = ap.parse_study_outcomes(
        _study_with_outcome(
            "NCT99000921",
            _week4_week12_measure([_participants_denom(), _eyes_denom()], [_participants_denom()]),
            flow_groups=[],
        )
    )
    assert trial.outcomes[0].units_denom_status == "class_level_incomplete"
    assert trial.outcomes[0].overall_units_analyzed is None
    checks = ap.structural_checks([trial])
    assert checks["units_denom_status_in_vocabulary"]["pass"] is True
    assert ap.all_pass(checks), checks
    trial.outcomes[0].units_denom_status = "class_level_incomplet"
    assert ap.structural_checks([trial])["units_denom_status_in_vocabulary"]["pass"] is False


def test_unit_denoms_on_every_class_stay_unanimous_control() -> None:
    # Control for the test above: the same Eyes denominator on every class
    # is unanimous, and its total stands for the measure.
    parsed = ap.parse_outcome_measure(
        _week4_week12_measure(
            [_participants_denom(), _eyes_denom()],
            [_participants_denom(), _eyes_denom()],
        )
    )
    assert parsed.units_denom_status == "present"
    assert parsed.overall_units_analyzed == 184.0
    assert parsed.units_analyzed_type == "Eyes"
    assert parsed.denom_source == "class_unanimous"
    assert parsed.overall_participants_analyzed == 92.0
    assert parsed.denom_unit_kind == "BOTH"
    trial = ap.parse_study_outcomes(
        _study_with_outcome(
            "NCT99000922",
            _week4_week12_measure([_eyes_denom()], [_eyes_denom()]),
            flow_groups=[],
        )
    )
    assert trial.outcomes[0].units_denom_status == "present"
    assert trial.outcomes[0].overall_units_analyzed == 184.0
    assert trial.outcomes[0].denom_unit_kind == "COUNT_OF_UNITS"
    assert ap.all_pass(ap.structural_checks([trial]))


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
    # _refuse_audit_holdout_record returned early for any non-dict, and the
    # entry point then emitted a parse-error record (parse_outcomes_frame a
    # row) for the trial instead of refusing it. A refused record gets no
    # parse-error record either (module docstring), so the correct result is
    # a PopulationAuditHoldoutError. Since the consolidation into
    # src/holdout_guard.py a record that is not exactly a dict is refused by
    # type, unread, so the reason is "in_audit_not_bool", not "in_audit_true".
    for wrapper_name, wrap in _redteam_mapping_wrappers():
        held = wrap(_redteam_holdout_record(retained, in_audit=True))
        with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
            entry(held)
        assert exc_info.value.reason == "in_audit_not_bool", wrapper_name


def test_redteam_m2_parse_outcomes_frame_mixed_batch_with_non_dict_holdout_is_refused() -> None:
    # Attack: a list mixing a clean dict record and the holdout as a
    # UserDict. Observed without the fix: a two-row frame, the second row a
    # parse error for the holdout trial. The UserDict is refused by type
    # (src/holdout_guard.py), so the reason is "in_audit_not_bool".
    studies = [
        _study_carrying(_CLEAN_NCT, in_audit=False),
        collections.UserDict(_study_carrying(_HOLDOUT_NCT, in_audit=True)),
    ]
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        ap.parse_outcomes_frame(studies)
    assert exc_info.value.reason == "in_audit_not_bool"


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
    # parse_outcomes_frame) and, from this repository, src/holdout_guard.py
    # alone, which itself imports only the standard library (pandas lazily,
    # inside check_frame); neither calls a builtin that reads, writes,
    # prints or executes.
    from src import holdout_guard

    tree = ast.parse(inspect.getsource(ap))
    guard_tree = ast.parse(inspect.getsource(holdout_guard))
    allowed = {
        "__future__", "hashlib", "json", "math", "re", "collections",
        "dataclasses", "typing", "pandas", "src",
    }
    imported: set[str] = set()
    from_src: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            if node.module == "src":
                from_src.update(alias.name for alias in node.names)
    assert imported <= allowed, sorted(imported - allowed)
    assert from_src == {"holdout_guard"}
    guard_imported = {
        (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0]
        for node in ast.walk(guard_tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert guard_imported <= {"__future__", "re", "typing", "pandas"}, sorted(guard_imported)
    forbidden_calls = {"open", "print", "input", "exec", "eval", "compile", "__import__"}
    for module_tree in (tree, guard_tree):
        called = {
            node.func.id
            for node in ast.walk(module_tree)
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


# ---------------------------------------------------------------------------
# 13. Holdout fixes for red team round 1: cases the fix covers beyond the
# red team's own tests, and inputs that must still pass. Every id is
# fictional (NCT99000001..NCT99999999); nothing touches the network or a file.
# ---------------------------------------------------------------------------


def _assert_same_output(name: str, a: Any, b: Any) -> None:
    """Equal entry-point outputs (a DataFrame for parse_outcomes_frame)."""
    if name == "parse_outcomes_frame":
        assert a.equals(b)
    else:
        assert a == b


def _clean_record(retained: bool) -> dict[str, Any]:
    """The fictional clean trial as a study dict, or as a retained raw record."""
    return _retained_carrying(_CLEAN_NCT) if retained else _study_carrying(_CLEAN_NCT)


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
@pytest.mark.parametrize(
    "key",
    [
        "in\u200b_audit",  # zero-width space
        "\ufeffin_audit",  # byte-order mark
        "in\u00adaudit",  # soft hyphen
        "in_au\u0301dit",  # combining acute accent
        "In Audit",
        b"in_audit",  # a bytes key, as a msgpack or raw decoder may give
    ],
)
def test_in_audit_invisible_fullwidth_and_bytes_spellings_are_refused(
    name: str, entry: Callable[[Any], Any], retained: bool, key: Any
) -> None:
    # The flag-key rule keeps only ASCII letters, so invisible characters
    # and combining accents inside the name do not hide it. A bytes key is
    # not a str: the record is refused by type ("in_audit_not_bool"). A
    # full-width spelling is no longer read as the flag (a look-alike
    # disguise; src/holdout_guard.py lists it as not seen).
    held = _redteam_holdout_record(retained)
    held[key] = True
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        entry(held)
    expected = "in_audit_not_bool" if isinstance(key, bytes) else "in_audit_true"
    assert exc_info.value.reason == expected


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_nested_flag_beside_another_trials_id_is_refused(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # A merged manifest sub-record that says in_audit False for a different
    # trial describes that trial, not this one: refused, not resolved. The
    # same sub-record naming this trial is accepted and changes nothing.
    other = _clean_record(retained)
    other["manifest"] = {"nct_id": _HOLDOUT_NCT, "in_audit": False}
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        entry(other)
    assert exc_info.value.reason == "nct_id_malformed"
    same = _clean_record(retained)
    same["manifest"] = {"nct_id": _CLEAN_NCT, "in_audit": False}
    _assert_same_output(name, entry(same), entry(_clean_record(retained)))


@pytest.mark.parametrize(("name", "entry"), _STUDY_ENTRY_POINTS)
def test_nct_id_aliases_are_not_conflicting_ids(
    name: str, entry: Callable[[dict[str, Any]], Any]
) -> None:
    # CT.gov keeps a trial's obsolete ids in identificationModule.nctIdAliases.
    # That key does not read as nct_id, so a flagged record carrying aliases
    # is not refused as an id disagreement.
    with_aliases = _study_carrying(_CLEAN_NCT, in_audit=False)
    with_aliases["protocolSection"]["identificationModule"]["nctIdAliases"] = ["NCT99000009"]
    without = _study_carrying(_CLEAN_NCT, in_audit=False)
    without["protocolSection"]["identificationModule"]["nctIdAliases"] = ["NCT99000009"]
    del without["in_audit"]
    _assert_same_output(name, entry(with_aliases), entry(without))


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_record_that_is_a_list_carrying_a_flag_is_refused(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # A record must be a dict: a list record is refused by type, whatever it
    # carries ("in_audit_not_bool"), and never recorded.
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        entry([{"nct_id": _HOLDOUT_NCT, "in_audit": True}])
    assert exc_info.value.reason == "in_audit_not_bool"
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        entry([{"nct_id": _CLEAN_NCT, "in_audit": False}])
    assert exc_info.value.reason == "in_audit_not_bool"


def test_parse_outcomes_frame_batch_gap_is_refused_in_either_order() -> None:
    # The study without in_audit may come first; the batch is still refused,
    # and no study of it is parsed. A batch in which no study carries
    # in_audit is unchanged (it relies on the caller).
    studies = [_study_carrying(_HOLDOUT_NCT), _study_carrying(_CLEAN_NCT, in_audit=False)]
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        ap.parse_outcomes_frame(iter(studies))
    assert exc_info.value.reason == "in_audit_not_bool"
    assert "position 0" in str(exc_info.value)
    assert _HOLDOUT_NCT not in str(exc_info.value)
    plain = ap.parse_outcomes_frame([_study_carrying(_CLEAN_NCT), _study_carrying("NCT99000003")])
    assert list(plain["nct_id"]) == [_CLEAN_NCT, "NCT99000003"]


def test_parse_outcomes_frame_refuses_input_that_is_not_a_batch() -> None:
    # Iterating a mapping (dict, UserDict, Series, DataFrame) or a string
    # yields keys or characters, not studies, so each is refused with
    # TypeError before anything in it is read, holdout or not.
    pd = pytest.importorskip("pandas")
    clean = _study_carrying(_CLEAN_NCT, in_audit=False)
    for single in (
        clean,
        collections.UserDict(clean),
        types.MappingProxyType(clean),
        pd.Series(clean),
        pd.DataFrame([{"nct_id": _CLEAN_NCT, "in_audit": False}]),
        _CLEAN_NCT,
        _CLEAN_NCT.encode(),
    ):
        with pytest.raises(TypeError):
            ap.parse_outcomes_frame(single)  # type: ignore[arg-type]


def test_cyclic_record_is_scanned_without_looping() -> None:
    # The scan visits each container once, so a record that contains itself
    # terminates: parsed when it carries no flag, refused when it does.
    study = _study_carrying(_CLEAN_NCT)
    study["self"] = study
    assert ap.parse_study_outcomes(study).nct_id == _CLEAN_NCT
    study["self_list"] = [study]
    study["protocolSection"]["identificationModule"]["in_audit"] = True
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        ap.parse_study_outcomes(study)
    assert exc_info.value.reason == "in_audit_true"


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_namedtuple_dataclass_and_attrs_records_carrying_in_audit_are_refused(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # df.itertuples() yields namedtuples, a dataclass instance holds its
    # fields as attributes, a pandas Series row can carry the flag in its
    # attrs metadata. None is a dict, so each is refused by type
    # ("in_audit_not_bool") rather than recorded as a "not a dict" parse
    # error.
    import dataclasses

    pd = pytest.importorskip("pandas")

    @dataclasses.dataclass
    class HeldRecord:
        nct_id: str
        in_audit: bool

    held = _redteam_holdout_record(retained, in_audit=True)
    row = next(pd.DataFrame([held]).itertuples(index=False))
    with_attrs = pd.Series(_redteam_holdout_record(retained))
    with_attrs.attrs["in_audit"] = True
    for record in (row, HeldRecord(_HOLDOUT_NCT, True), with_attrs):
        with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
            entry(record)
        assert exc_info.value.reason == "in_audit_not_bool", type(record).__name__
    study_rows = pd.DataFrame(
        [_study_carrying(_HOLDOUT_NCT, in_audit=True), _study_carrying(_CLEAN_NCT, in_audit=False)]
    )
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        ap.parse_outcomes_frame(study_rows.itertuples(index=False))
    assert exc_info.value.reason == "in_audit_not_bool"
    # A namedtuple flagged False is refused by type too, not recorded as a
    # parse error.
    clean_row = next(study_rows.iloc[[1]].itertuples(index=False))
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        ap.parse_study_outcomes(clean_row)
    assert exc_info.value.reason == "in_audit_not_bool"


# ---------------------------------------------------------------------------
# 14. Red team round 2 (test_redteam_m2_*). Each test asserts the correct
# behaviour: a record that marks itself in_audit, however it is shaped, is
# refused before anything in it is parsed, retained or recorded. Tests for a
# bypass fail until the module is fixed. Every id is fictional
# (NCT99000001..NCT99999999); nothing touches the network, and no file is
# read except this repository's own source and, for the dc38220 claims, git
# objects of this repository.
# ---------------------------------------------------------------------------

# Names that qualify in_audit rather than spell it and are read as the flag
# (src/holdout_guard.py is_flag_key): pandas' default merge suffixes _x/_y,
# a json_normalize path (sep "."), an "is_" prefix, and a custom join suffix
# or prefix (in_audit_manifest, manifest_in_audit), read because their words
# hold "in" then "audit".
_REDTEAM_QUALIFIED_IN_AUDIT_NAMES: list[str] = [
    "in_audit_x",
    "in_audit_y",
    "manifest.in_audit",
    "is_in_audit",
    "in_audit_manifest",
    "manifest_in_audit",
]


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
@pytest.mark.parametrize("key", _REDTEAM_QUALIFIED_IN_AUDIT_NAMES)
def test_redteam_m2_qualified_in_audit_names_are_refused(
    name: str, entry: Callable[[Any], Any], retained: bool, key: str
) -> None:
    # Attack: the holdout flag under a name that qualifies in_audit. The
    # name folding (_folded_name) compares the whole folded name with
    # "inaudit", so "in_audit_x" folds to "inauditx" and is not a flag.
    # Observed: the holdout is parsed in full (parse_study_outcomes and
    # build_outcomes_from_raw return its "itt" outcome, parse_outcomes_frame
    # a row, select_raw_outcome_populations its raw outcomes). Correct: a
    # name that contains in_audit as a component reads as in_audit.
    held = _redteam_holdout_record(retained, **{key: True})
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        entry(held)
    assert exc_info.value.reason == "in_audit_true"


def test_redteam_m2_parse_outcomes_frame_refuses_records_from_a_pandas_manifest_merge() -> None:
    # Attack: studies that already carry a stale in_audit False (from an
    # earlier manifest) are merged with the current manifest, which marks
    # the holdout True. pandas suffixes both columns (in_audit_x,
    # in_audit_y), so no record carries a name that folds to "inaudit".
    # Observed: a frame with one "itt" row per study, the holdout's
    # included. Correct: refused, the holdout's True being a holdout flag.
    pd = pytest.importorskip("pandas")
    studies = [
        _study_carrying(_CLEAN_NCT, in_audit=False),
        _study_carrying(_HOLDOUT_NCT, in_audit=False),
    ]
    for study in studies:  # the merge key, equal to the study's own nctId
        study["nct_id"] = study["protocolSection"]["identificationModule"]["nctId"]
    manifest = pd.DataFrame({"nct_id": [_CLEAN_NCT, _HOLDOUT_NCT], "in_audit": [False, True]})
    merged = pd.DataFrame(studies).merge(manifest, on="nct_id", how="left")
    assert {"in_audit_x", "in_audit_y"} <= set(merged.columns)
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        ap.parse_outcomes_frame(merged.to_dict("records"))
    assert exc_info.value.reason == "in_audit_true"


def _redteam_unwalked_containers() -> list[tuple[str, Any]]:
    """Containers holding the holdout's manifest that _scan_in_audit does not open.

    Method: the scan walks mapping-like values, namedtuples, dataclass
    instances, list/tuple/set/frozenset and numpy object arrays. A
    types.SimpleNamespace (or any plain object with attributes), a
    collections.deque, a dict view and the row index of a nested DataFrame
    are none of these, so a flag inside one is never seen.
    """
    pd = pytest.importorskip("pandas")
    manifest = {"nct_id": _HOLDOUT_NCT, "in_audit": True}
    return [
        ("SimpleNamespace", types.SimpleNamespace(**manifest)),
        ("deque", collections.deque([dict(manifest)])),
        ("dict_values", {"manifest": dict(manifest)}.values()),
        (
            "DataFrame with in_audit as its index name",
            pd.DataFrame({"nct_id": [_HOLDOUT_NCT]}, index=pd.Index([True], name="in_audit")),
        ),
    ]


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_redteam_m2_flags_in_containers_the_scan_does_not_walk_are_refused(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # Attack: the merged manifest sub-record is held in a container the
    # scan does not walk. Observed: the holdout is parsed in full by every
    # entry point. Correct: refused (a flag inside any container counts,
    # or a container the scan cannot read is refused rather than skipped).
    for label, container in _redteam_unwalked_containers():
        held = _redteam_holdout_record(retained, manifest=container)
        with pytest.raises(ap.PopulationAuditHoldoutError):
            entry(held)
        assert label  # names the container in the failure's locals


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_redteam_m2_nested_sub_record_id_that_disagrees_with_the_stamped_id_is_refused(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # Attack: the manifest merge hoisted in_audit False to the top level and
    # nested the rest of the manifest row, which names a different trial
    # (the holdout). Only top-level ids and ids beside a flag are compared
    # with the stamped id, so this id is never read. Observed: accepted and
    # parsed under the clean id. Correct: an nct_id anywhere in a record
    # that carries in_audit must equal the id it is stamped with
    # ("nct_id_malformed"). The same sub-record naming the record's own id
    # is accepted, unchanged.
    same = _clean_record(retained)
    same["in_audit"] = False
    same["manifest"] = {"nct_id": _CLEAN_NCT, "cohorts": ["redteam"]}
    plain = _clean_record(retained)
    plain["in_audit"] = False
    _assert_same_output(name, entry(same), entry(plain))
    for key in ("nct_id", "nctId"):
        other = _clean_record(retained)
        other["in_audit"] = False
        other["manifest"] = {key: _HOLDOUT_NCT, "cohorts": ["redteam"]}
        with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
            entry(other)
        assert exc_info.value.reason == "nct_id_malformed"


def _redteam_holdout_study_frame() -> Any:
    """A one-row DataFrame of the holdout study, flagged in_audit True."""
    pd = pytest.importorskip("pandas")
    return pd.DataFrame([_study_carrying(_HOLDOUT_NCT, in_audit=True)])


def test_redteam_m2_rows_whose_field_names_the_scan_cannot_read_are_refused() -> None:
    # Attack: hand the holdout over as a row type whose field names the
    # scan does not read. df.to_records() yields numpy.record (dtype kind
    # "V", its names in dtype.names); itertuples() renames a column that is
    # not an identifier ("In Audit", a spreadsheet header the folding is
    # meant to catch) to "_2"; itertuples(name=None) yields plain tuples.
    # No flag is found, so _parse_checked_study records each as the parse
    # error "study_not_a_dict". Observed: parse_outcomes_frame returns one
    # parse-error row per holdout, and parse_study_outcomes a parse-error
    # TrialAnalysisPopulations, although the module docstring says a
    # refused record gets no parse-error record. Correct: refused (a row
    # whose in_audit cannot be read is not parsed or recorded).
    df = _redteam_holdout_study_frame()
    renamed = df.rename(columns={"in_audit": "In Audit"})
    assert next(renamed.itertuples(index=False))._fields[-1].startswith("_")
    batches: list[tuple[str, Any]] = [
        ("to_records", df.to_records(index=False)),
        ("itertuples with a renamed field", renamed.itertuples(index=False)),
        ("itertuples(name=None)", df.itertuples(index=False, name=None)),
    ]
    for label, batch in batches:
        with pytest.raises((ap.PopulationAuditHoldoutError, TypeError)):
            ap.parse_outcomes_frame(batch)
        assert label
    for record in (df.to_records(index=False)[0], next(renamed.itertuples(index=False))):
        with pytest.raises((ap.PopulationAuditHoldoutError, TypeError)):
            ap.parse_study_outcomes(record)


def test_redteam_m2_parse_outcomes_frame_single_row_instead_of_a_batch_is_refused() -> None:
    # Attack: pass one holdout row where an iterable of studies is
    # expected, as a namedtuple (one itertuples() row) or a numpy.record.
    # Round 1 refused a single mapping-like record; these are not
    # mapping-like, so the batch is their field values. Observed: a frame
    # with a "study_not_a_dict" row for the holdout's True flag (and its
    # protocolSection parsed as a study of its own). Correct: refused.
    df = _redteam_holdout_study_frame()
    for single in (next(df.itertuples(index=False)), df.to_records(index=False)[0]):
        with pytest.raises((ap.PopulationAuditHoldoutError, TypeError)):
            ap.parse_outcomes_frame(single)


def test_redteam_m2_audit_id_set_passed_positionally_is_refused() -> None:
    # Attack: the caller passes the audit id set positionally, believing the
    # second parameter filters on it. It is snapshot_date (then
    # outcome_types), neither of which is type-checked. Observed: the
    # holdout is parsed in full and the audit id list, holdout id included,
    # is copied into the output's snapshot_date (parse_outcomes_frame puts
    # it in every row); as outcome_types it filters every measure out but
    # still returns a record stamped with the holdout's id. Correct: a
    # snapshot_date that is not a str (or None) and an outcome type outside
    # CT.gov's vocabulary are refused (TypeError or ValueError).
    audit_ids: list[str] = [_HOLDOUT_NCT, "nct99000001"]
    held = _study_carrying(_HOLDOUT_NCT)
    calls: list[tuple[str, Callable[[], Any]]] = [
        ("parse_study_outcomes snapshot_date", lambda: ap.parse_study_outcomes(held, audit_ids)),
        ("parse_outcomes_frame snapshot_date", lambda: ap.parse_outcomes_frame([held], audit_ids)),
        (
            "build_outcomes_from_raw snapshot_date",
            lambda: ap.build_outcomes_from_raw(_retained_carrying(_HOLDOUT_NCT), set(audit_ids)),
        ),
        (
            "parse_study_outcomes outcome_types",
            lambda: ap.parse_study_outcomes(held, None, tuple(audit_ids)),
        ),
    ]
    for label, call in calls:
        with pytest.raises((TypeError, ValueError, ap.PopulationAuditHoldoutError)):
            call()
        assert label


def _redteam_undocumented_callers(module: types.ModuleType) -> dict[str, list[str]]:
    """Public in-module callers missing from each docstring's "Callers today:".

    Method: AST only. A public function reaches a target when it calls it
    directly or through private helpers (a "_" name, followed transitively).
    For every top-level function whose whitespace-normalised docstring has
    "Callers today:", the public functions reaching it must all be named in
    the text after that marker. Returns {function: [missing callers]}.
    """
    tree = ast.parse(inspect.getsource(module))
    funcs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    calls: dict[str, set[str]] = {
        name: {
            sub.func.id
            for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in funcs
        }
        for name, node in funcs.items()
    }

    def public_reachers(target: str) -> set[str]:
        found: set[str] = set()
        frontier, seen = [target], {target}
        while frontier:
            current = frontier.pop()
            for caller, callees in calls.items():
                if current not in callees or caller == current:
                    continue
                if caller.startswith("_"):
                    if caller not in seen:
                        seen.add(caller)
                        frontier.append(caller)
                else:
                    found.add(caller)
        return found

    missing: dict[str, list[str]] = {}
    for name, node in funcs.items():
        doc = " ".join((ast.get_docstring(node) or "").split())
        marker = re.search(r"Callers today:(.*)", doc)
        if marker is None:
            continue
        named = {f for f in funcs if re.search(rf"(?<![\w.]){re.escape(f)}(?!\w)", marker.group(1))}
        gaps = sorted(public_reachers(name) - named)
        if gaps:
            missing[name] = gaps
    return missing


def test_redteam_m2_documented_callers_name_every_in_module_caller() -> None:
    # Claim under test: each relies-on-caller docstring names its callers,
    # because those callers carry the duty to exclude holdout trials.
    # Observed: extract_flow_started, parse_outcome_measure and
    # parse_error_trial_outcomes name parse_study_outcomes and
    # build_outcomes_from_raw but not parse_outcomes_frame, which reaches
    # all three through _parse_checked_study (and is the entry point that
    # records the parse-error rows of the row-type bypass above).
    assert _redteam_undocumented_callers(ap) == {}


class _RedteamIoRecorder:
    """Process-wide audit hook state: inert unless ``events`` is a list.

    Method: sys.addaudithook sees every file open, socket, subprocess and
    os-level call made by any code (gzip.open, pathlib, urllib, shutil and
    tempfile included), which monkeypatching a few names cannot. A hook
    cannot be removed, so it is installed once and records only while a
    test sets ``events``.
    """

    installed: bool = False
    events: list[str] | None = None
    prefixes: tuple[str, ...] = (
        "os.", "socket.", "subprocess.", "shutil.", "urllib.", "http.", "ftplib.",
        "smtplib.", "sqlite3.", "tempfile.", "glob.", "webbrowser.",
    )


def _redteam_io_audit_hook(event: str, _args: tuple[Any, ...]) -> None:
    """Record a file, process or network audit event while recording is on."""
    events = _RedteamIoRecorder.events
    if events is not None and (event == "open" or event.startswith(_RedteamIoRecorder.prefixes)):
        events.append(event)


def _redteam_io_events_during(calls: list[Callable[[], Any]]) -> list[str]:
    """Audit events for file, process or network access raised while ``calls`` run.

    Method: every call runs once unrecorded first, so lazy imports (which
    open files) happen before recording starts; then again while
    recording. Exceptions are swallowed in both runs (refusals are calls
    too) and nothing is formatted while recording, so no traceback reads a
    source file.
    """
    if not _RedteamIoRecorder.installed:
        sys.addaudithook(_redteam_io_audit_hook)
        _RedteamIoRecorder.installed = True
    for call in calls:
        try:
            call()
        except Exception:  # noqa: BLE001 - refusals are expected here
            pass
    _RedteamIoRecorder.events = []
    try:
        for call in calls:
            try:
                call()
            except Exception:  # noqa: BLE001
                pass
        return list(_RedteamIoRecorder.events)
    finally:
        _RedteamIoRecorder.events = None


def test_redteam_m2_entry_points_raise_no_io_audit_event() -> None:
    # Claim under test: "This module does no I/O: it reads only the objects
    # it is handed." Round 1 patched open/socket/print; this records every
    # file, process and network audit event instead, across every public
    # function (relies-on-caller fragments, the enforcing entry points on
    # unflagged input, and their refusals). The harness is checked first:
    # an open() while recording must be seen.
    assert "open" in _redteam_io_events_during([lambda: open(os.devnull).close()])
    study = _study_carrying(_CLEAN_NCT)
    measure = study["resultsSection"]["outcomeMeasuresModule"]["outcomeMeasures"][0]
    flow_module = study["resultsSection"]["participantFlowModule"]
    parsed = ap.parse_study_outcomes(study)
    flow_groups, flow_status = ap.extract_flow_started(flow_module)
    held = _study_carrying(_HOLDOUT_NCT, in_audit=True)
    calls: list[Callable[[], Any]] = [
        lambda: ap.extract_flow_started(flow_module),
        lambda: ap.align_flow_and_outcome_groups(flow_groups, measure["groups"]),
        lambda: ap.parse_outcome_measure(measure, 0, flow_groups, flow_status, 100.0),
        lambda: ap.classify_population_description("Modified intent-to-treat population"),
        lambda: ap.normalize_population_text("All randomized participants"),
        lambda: ap.population_audit(["ITT", None, "Per protocol population"]),
        lambda: ap.structural_checks([parsed]),
        lambda: ap.all_pass(ap.structural_checks([parsed])),
        lambda: ap.compute_vocabulary_sha256(),
        lambda: ap.parse_error_trial_outcomes(None, None, None, "study_not_a_dict"),
        lambda: ap.parse_study_outcomes(study),
        lambda: ap.select_raw_outcome_populations(study),
        lambda: ap.build_outcomes_from_raw(ap.select_raw_outcome_populations(study)),
        lambda: ap.parse_outcomes_frame([study]),
        lambda: ap.parse_study_outcomes(held),
        lambda: ap.parse_outcomes_frame([study, held]),
    ]
    assert _redteam_io_events_during(calls) == []


def _redteam_git_show(revision: str, path: str) -> str:
    """``git show revision:path`` from this repository, or skip when unavailable.

    Method: the claims below are about PR #97's own branch (dc38220), which
    this branch may not contain; a shallow CI checkout lacks the object, so
    the test skips there rather than fetch anything.
    """
    repo = Path(__file__).resolve().parents[1]
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    probe = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{revision}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(f"commit {revision} is not in this checkout")
    shown = subprocess.run(
        ["git", "-C", str(repo), "show", f"{revision}:{path}"],
        capture_output=True,
        check=True,
        text=True,
    )
    return shown.stdout


def test_redteam_m2_dc38220_caller_claims_in_the_parser_docstring_hold() -> None:
    # Claim under test (module docstring): at dc38220,
    # scripts/build_outcomes_shards.py reads the parser's output from
    # --population-parsed without importing the parser, passes
    # pop_by_nct.get(nct_id) to build_trial_bundle for every merged trial
    # (holdouts included), and calls assert_no_audit_cross_stream_leakage
    # without the population stream; FORBIDDEN_AUDIT_KEYS lacks
    # analysis_population_parsed; build_trial_bundle takes in_audit and
    # population_parser, and extract_reported_results calls
    # population_parser(study). Only source code is read.
    shards = ast.parse(_redteam_git_show("dc38220", "scripts/build_outcomes_shards.py"))
    layout = ast.parse(_redteam_git_show("dc38220", "src/outcomes_layout.py"))
    imported = [
        alias.name if isinstance(node, ast.Import) else f"{node.module}.{alias.name}"
        for node in ast.walk(shards)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    ]
    assert not any("analysis_population_parser" in name for name in imported)
    assert any(
        isinstance(node, ast.Constant) and node.value == "--population-parsed"
        for node in ast.walk(shards)
    )
    bundle_loops = [
        loop
        for loop in ast.walk(shards)
        if isinstance(loop, ast.For)
        and any(
            isinstance(call, ast.Call)
            and ast.unparse(call.func).endswith("build_trial_bundle")
            and any(
                kw.arg == "parsed_populations" and ast.unparse(kw.value) == "pop_by_nct.get(nct_id)"
                for kw in call.keywords
            )
            for call in ast.walk(loop)
        )
    ]
    assert len(bundle_loops) == 1
    assert ast.unparse(bundle_loops[0].iter) == "sorted(merged)"
    assert not any(isinstance(node, ast.Continue) for node in ast.walk(bundle_loops[0]))
    firewall_calls = [
        call
        for call in ast.walk(shards)
        if isinstance(call, ast.Call)
        and ast.unparse(call.func).endswith("assert_no_audit_cross_stream_leakage")
    ]
    assert len(firewall_calls) == 1
    assert {kw.arg for kw in firewall_calls[0].keywords} == {
        "documents_by_nct",
        "locations_by_nct",
        "provenance_by_nct",
    }
    forbidden = next(
        node.value
        for node in ast.walk(layout)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "FORBIDDEN_AUDIT_KEYS"
    )
    keys = {c.value for c in ast.walk(forbidden) if isinstance(c, ast.Constant)}
    assert keys and "analysis_population_parsed" not in keys
    funcs = {n.name: n for n in layout.body if isinstance(n, ast.FunctionDef)}
    assert {"in_audit", "population_parser"} <= {
        a.arg for a in funcs["build_trial_bundle"].args.kwonlyargs
    }
    assert any(
        isinstance(call, ast.Call) and ast.unparse(call) == "population_parser(study)"
        for call in ast.walk(funcs["extract_reported_results"])
    )


def test_redteam_m2_tuple_keys_chainmaps_rows_and_object_arrays_carrying_in_audit_are_refused() -> None:
    # Attack (refused): the flag under a tuple key (a MultiIndex column
    # label), in a ChainMap layered over the study, in a Series row of a
    # frame with MultiIndex columns, and inside a numpy object array or an
    # object-dtype pandas Index of manifests. Expected and observed: every
    # one is refused by every study entry point, by type ("in_audit_not_bool":
    # a tuple key, a ChainMap, a Series, an ndarray and an Index are not JSON).
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")
    manifest = {"nct_id": _HOLDOUT_NCT, "in_audit": True}
    tuple_key = _study_carrying(_HOLDOUT_NCT)
    tuple_key[("manifest", "in_audit")] = True
    chained = collections.ChainMap({"in_audit": True}, _study_carrying(_HOLDOUT_NCT))
    wide = pd.DataFrame(
        [[_HOLDOUT_NCT, True]],
        columns=pd.MultiIndex.from_tuples([("manifest", "nct_id"), ("manifest", "in_audit")]),
    )
    series_row = next(wide.iterrows())[1]
    in_array = _study_carrying(_HOLDOUT_NCT, manifests=np.array([dict(manifest)], dtype=object))
    in_index = _study_carrying(_HOLDOUT_NCT, manifests=pd.Index([dict(manifest)], dtype=object))
    for name, entry in _REDTEAM_STUDY_ENTRY_POINTS:
        for record in (tuple_key, chained, series_row, in_array, in_index):
            with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
                entry(record)
            assert exc_info.value.reason == "in_audit_not_bool", (name, type(record).__name__)


def test_redteam_m2_mixed_batch_with_a_qualified_name_holdout_is_refused() -> None:
    # Attack (refused): clean studies carrying in_audit False, and the
    # holdout carrying only in_audit_y True. in_audit_y (a pandas merge
    # suffix) is read as the flag, so the holdout is refused as
    # "in_audit_true" at its position, wherever it sits.
    for position in (0, 1, 2):
        studies = [_study_carrying(_CLEAN_NCT, in_audit=False) for _ in range(2)]
        studies.insert(position, _study_carrying(_HOLDOUT_NCT, in_audit_y=True))
        with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
            ap.parse_outcomes_frame(studies)
        assert exc_info.value.reason == "in_audit_true"
        assert f"position {position}" in str(exc_info.value)
        assert _HOLDOUT_NCT not in str(exc_info.value)


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_redteam_m2_id_only_in_a_nested_field_is_refused(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # Attack (refused): in_audit False with the trial's id present only in
    # a nested manifest, not where the entry point reads the id it stamps
    # (identificationModule.nctId for a study, top-level nct_id for a
    # retained record). Expected and observed: "nct_id_malformed".
    record = _clean_record(retained)
    if retained:
        del record["nct_id"]
    else:
        del record["protocolSection"]["identificationModule"]["nctId"]
    record["manifest"] = {"nct_id": _CLEAN_NCT, "in_audit": False}
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        entry(record)
    assert exc_info.value.reason == "nct_id_malformed"


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
@pytest.mark.parametrize(
    "sibling",
    ["nct99000002", " NCT99000002", "NCT99000002​", "NCT99000002\n", "ＮＣＴ99000002"],
)
def test_redteam_m2_odd_id_beside_a_nested_flag_is_refused(
    name: str, entry: Callable[[Any], Any], retained: bool, sibling: str
) -> None:
    # Attack (refused): the stamped id is canonical and the nested manifest
    # beside its in_audit False carries a lowercase, padded, zero-width,
    # newline or full-width variant of it. Expected and observed: the
    # disagreement is refused ("nct_id_malformed"), not normalised away.
    record = _clean_record(retained)
    record["manifest"] = {"nct_id": sibling, "in_audit": False}
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        entry(record)
    assert exc_info.value.reason == "nct_id_malformed"


# ---------------------------------------------------------------------------
# 15. Holdout fixes for red team round 2: cases the fix covers beyond the red
# team's own tests, and inputs that must still pass. Every id is fictional
# (NCT99000001..NCT99999999); nothing touches the network or a file.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_qualified_names_and_unwalked_containers_report_in_audit_true(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # A True flag under a qualified name, or inside a list (JSON), is
    # reported as a holdout. A container that is not JSON (a namespace, a
    # deque, a dict view) is refused by type, unread ("in_audit_not_bool").
    for key in ("in_audit_y", "manifest.in_audit", "isInAudit"):
        with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
            entry(_redteam_holdout_record(retained, **{key: True}))
        assert exc_info.value.reason == "in_audit_true", key
    manifest = {"nct_id": _HOLDOUT_NCT, "in_audit": True}
    for container, reason in (
        (types.SimpleNamespace(**manifest), "in_audit_not_bool"),
        (collections.deque([dict(manifest)]), "in_audit_not_bool"),
        (dict(manifest).items(), "in_audit_not_bool"),
        ([dict(manifest)], "in_audit_true"),
    ):
        with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
            entry(_redteam_holdout_record(retained, manifest=container))
        assert exc_info.value.reason == reason, type(container).__name__


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_text_and_prose_that_must_not_refuse_a_clean_record(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # Only text that serializes a container AND has in_audit (or nct_id) in
    # key position is read. Prose, even bracketed prose that mentions an
    # audit, and a serialized container without such a key change nothing.
    plain = entry(_clean_record(retained))
    for text in (
        "[Practices in audit arm: audit and feedback]",
        "{in audit}",
        "Not in audit",
        "Cohort(A)",
        json.dumps({"cohort": "redteam", "note": "in audit arm"}),
        "[Not Specified]",
    ):
        record = _clean_record(retained)
        record["note"] = text
        _assert_same_output(name, entry(record), plain)


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_unreadable_parts_are_refused_and_qualified_ids_are_compared(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # A part the scan cannot read without consuming it, or a bool that lost
    # its name, may hold a flag: refused even without any flag. An nct_id
    # under a json_normalize path name is an id; nctIdAliases is not.
    for odd in (
        (x for x in [{"in_audit": True}]),
        iter([{"in_audit": True}]),
        [_CLEAN_NCT, True],
        ("manifest", False),
    ):
        record = _clean_record(retained)
        record["manifest"] = odd
        with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
            entry(record)
        assert exc_info.value.reason == "in_audit_not_bool", type(odd).__name__
    other = _clean_record(retained)
    other["in_audit"] = False
    other["manifest.nct_id"] = _HOLDOUT_NCT
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        entry(other)
    assert exc_info.value.reason == "nct_id_malformed"
    own = _clean_record(retained)
    own["in_audit"] = False
    own["manifest.nct_id"] = _CLEAN_NCT
    own["nctIdAliases"] = [_HOLDOUT_NCT]
    plain = _clean_record(retained)
    plain["in_audit"] = False
    plain["nctIdAliases"] = [_HOLDOUT_NCT]
    _assert_same_output(name, entry(own), entry(plain))


def test_batch_gap_rule_runs_last_and_flag_names_may_differ() -> None:
    # Records may carry the flag under different names (in_audit beside
    # in_audit_y): each record is checked on its own, so the True is
    # reported as in_audit_true at its position (the earlier rule refusing
    # mixed names first is gone). A True beside an unflagged record is
    # still reported at its own position (the gap rule runs last).
    mixed = [_study_carrying(_HOLDOUT_NCT, in_audit=True), _study_carrying(_CLEAN_NCT, in_audit_y=False)]
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        ap.parse_outcomes_frame(mixed)
    assert exc_info.value.reason == "in_audit_true"
    assert "position 0" in str(exc_info.value)
    gap = [_study_carrying(_CLEAN_NCT), _study_carrying(_HOLDOUT_NCT, in_audit=True)]
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        ap.parse_outcomes_frame(gap)
    assert exc_info.value.reason == "in_audit_true"
    assert "position 1" in str(exc_info.value)
    merged_alike = [
        _study_carrying(_CLEAN_NCT, in_audit_x=False, in_audit_y=False),
        _study_carrying("NCT99000003", in_audit_x=False, in_audit_y=False),
    ]
    rows = ap.parse_outcomes_frame(merged_alike)
    assert list(rows["nct_id"]) == [_CLEAN_NCT, "NCT99000003"]


def test_record_shapes_without_names_and_scalars() -> None:
    # A top-level plain sequence has no field names: refused. A scalar has
    # no fields at all and is still recorded as a parse error, as before.
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        ap.parse_study_outcomes((_CLEAN_NCT, "x"))  # type: ignore[arg-type]
    assert exc_info.value.reason == "in_audit_not_bool"
    for scalar in (None, 7):
        parsed = ap.parse_study_outcomes(scalar)  # type: ignore[arg-type]
        assert parsed.parse_ok is False and parsed.nct_id is None
        assert parsed.outcomes[0].parse_error_reason == "study_not_a_dict"
    import dataclasses

    @dataclasses.dataclass
    class Row:
        nct_id: str

    with pytest.raises(TypeError):
        ap.parse_outcomes_frame(Row(_CLEAN_NCT))  # type: ignore[arg-type]


def test_snapshot_date_and_outcome_types_are_validated() -> None:
    # The arguments are checked before the record is read, and messages
    # never echo the value (which may be an audit id).
    study = _study_carrying(_CLEAN_NCT)
    assert ap.parse_study_outcomes(study, "2024-02-29").snapshot_date == "2024-02-29"
    assert ap.parse_study_outcomes(study, None, (" primary ",)).n_outcomes_total == 1
    assert ap.parse_study_outcomes(study, None, ["SECONDARY"]).n_outcomes_total == 0
    bad_dates: list[Any] = ["2026-02-30", "2025-02-29", "20261120", "2026-11-20 ", _HOLDOUT_NCT]
    for bad in bad_dates:
        with pytest.raises(ValueError) as exc_info:
            ap.parse_study_outcomes(study, bad)
        assert _HOLDOUT_NCT not in str(exc_info.value)
    for bad in ([_HOLDOUT_NCT], 20261120, b"2026-11-20"):
        with pytest.raises(TypeError):
            ap.parse_study_outcomes(study, bad)
    for bad_types in ("PRIMARY", {"PRIMARY": 1}, 5, [None]):
        with pytest.raises(TypeError):
            ap.parse_study_outcomes(study, None, bad_types)
    with pytest.raises(ValueError) as exc_info:
        ap.build_outcomes_from_raw(_retained_carrying(_CLEAN_NCT), None, (_HOLDOUT_NCT,))
    assert _HOLDOUT_NCT not in str(exc_info.value)
    assert ap.OUTCOME_MEASURE_TYPES == ("PRIMARY", "SECONDARY", "OTHER_PRE_SPECIFIED", "POST_HOC")


# ---------------------------------------------------------------------------
# 16. Red team, M2 round 3 (test_redteam_m2_*). Each test asserts the
# CORRECT behaviour, which is refusal, for one attack on an entry point that
# enforces the holdout; a test that fails here names a bypass that is still
# open and is not a test to loosen. Tests for attempts that are already
# refused say so ("Attack (refused)"). Every id is fictional
# (NCT99000001..NCT99999999); nothing touches the network or a file.
# ---------------------------------------------------------------------------


def _redteam3_series_rows(record: dict[str, Any]) -> list[tuple[str, Any]]:
    """Rows of a one-record frame whose in_audit column became the row index.

    Method: ``set_index("in_audit")`` (or with ``append=True``) moves the
    flag into the row index; ``iterrows()`` and ``apply(axis=1)`` then hand
    out Series rows that keep the flag's value only as their ``.name`` (a
    bool, or a tuple holding one), with the level's name "in_audit" left
    on the frame, and ``itertuples()`` keeps it as the field "Index".
    """
    pd = pytest.importorskip("pandas")
    indexed = pd.DataFrame([record]).set_index("in_audit")
    appended = pd.DataFrame([record]).set_index("in_audit", append=True)
    applied: list[Any] = []
    # copy(): apply may hand every row in one reused Series object.
    indexed.apply(lambda row: applied.append(row.copy()), axis=1)
    return [
        ("iterrows after set_index('in_audit')", next(indexed.iterrows())[1]),
        ("iterrows after set_index('in_audit', append=True)", next(appended.iterrows())[1]),
        ("apply(axis=1) row after set_index('in_audit')", applied[0]),
        ("itertuples() row after set_index('in_audit')", next(indexed.itertuples())),
    ]


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_redteam_m2_row_whose_index_value_holds_in_audit_is_refused(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # Attack: the manifest-merged holdout (in_audit True) in a frame indexed
    # by in_audit, handed over row by row. The Series row's .name is True
    # ((0, True) with append=True) and carries no "in_audit" label, so the
    # scan finds no flag; a bool .name is never read as one that lost its
    # field name, which the scan refuses everywhere else. Observed: every
    # entry point accepts it; parse_study_outcomes and
    # build_outcomes_from_raw return a "study_not_a_dict"/"raw_not_a_dict"
    # parse-error record, select_raw_outcome_populations a retained record
    # (nct_id None, no measures), parse_outcomes_frame a parse-error row.
    # Correct: refused, with no record for the trial.
    for label, row in _redteam3_series_rows(_redteam_holdout_record(retained, in_audit=True)):
        with pytest.raises(ap.PopulationAuditHoldoutError):
            entry(row)
        assert label


def test_redteam_m2_parse_outcomes_frame_batch_of_rows_indexed_by_in_audit_is_refused() -> None:
    # Attack: the whole manifest-merged batch (holdout True, clean False)
    # indexed by in_audit and passed as iterrows() rows. No row carries a
    # flag, so the batch gap rule has nothing to compare. Observed: a frame
    # with two "study_not_a_dict" rows, one of them the holdout's. Correct:
    # refused.
    pd = pytest.importorskip("pandas")
    merged = pd.DataFrame(
        [_study_carrying(_HOLDOUT_NCT, in_audit=True), _study_carrying(_CLEAN_NCT, in_audit=False)]
    )
    rows = [row for _, row in merged.set_index("in_audit").iterrows()]
    with pytest.raises(ap.PopulationAuditHoldoutError):
        ap.parse_outcomes_frame(rows)


# Characters that are invisible when printed, other than the ones the name
# folding already ignores: bidi marks and controls (a copy from right-to-left
# text adds them), invisible operators, a variation selector, the Mongolian
# vowel separator and a tag character.
_REDTEAM3_INVISIBLE: list[str] = [
    "‎",  # LEFT-TO-RIGHT MARK
    "‏",  # RIGHT-TO-LEFT MARK
    "؜",  # ARABIC LETTER MARK
    "‪",  # LEFT-TO-RIGHT EMBEDDING
    "⁦",  # LEFT-TO-RIGHT ISOLATE
    "⁣",  # INVISIBLE SEPARATOR
    "️",  # VARIATION SELECTOR-16
    "᠎",  # MONGOLIAN VOWEL SEPARATOR
    "\U000e0020",  # TAG SPACE
]


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
@pytest.mark.parametrize("char", _REDTEAM3_INVISIBLE)
def test_redteam_m2_in_audit_name_with_other_invisible_characters_is_refused(
    name: str, entry: Callable[[Any], Any], retained: bool, char: str
) -> None:
    # Attack: in_audit True under "in<char>_audit". The folding drops
    # zero-width space/joiners, the word joiner, the soft hyphen and the BOM
    # (round 2) but none of these, which are just as invisible. Observed:
    # parsed in full under the holdout's id by every entry point. Correct:
    # refused as in_audit_true.
    held = _redteam_holdout_record(retained, **{f"in{char}_audit": True})
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        entry(held)
    assert exc_info.value.reason == "in_audit_true"


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_redteam_m2_non_text_key_holding_a_text_flag_is_refused(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # Attack: an enum member as the key (str() gives "Key.IN_AUDIT") holding
    # the CSV string "True". A bool under a non-text key is refused as
    # unreadable; a string there is a leaf. Observed: parsed in full.
    # Correct: refused.
    import enum

    class Key(enum.Enum):
        IN_AUDIT = "in_audit"

    held = _redteam_holdout_record(retained)
    held[Key.IN_AUDIT] = "True"  # type: ignore[index]
    with pytest.raises(ap.PopulationAuditHoldoutError):
        entry(held)


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_redteam_m2_chainmap_shadowing_a_true_flag_is_refused(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # Attack: a ChainMap layering {"in_audit": False} over the holdout's
    # record, which carries in_audit True. The scan read ChainMap.items(),
    # where the True is shadowed. Observed: accepted (a parse-error record
    # or a retained record for the holdout). Correct: refused. A ChainMap is
    # not a dict, so it is refused by type ("in_audit_not_bool"), unread.
    held = collections.ChainMap({"in_audit": False}, _redteam_holdout_record(retained, in_audit=True))
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        entry(held)
    assert exc_info.value.reason == "in_audit_not_bool"


def _redteam3_attribute_holders(record: dict[str, Any]) -> list[tuple[str, Any]]:
    """The holdout with in_audit True kept where _scan_in_audit reads no names.

    Method: _keyed_items reads a mapping's items, a namedtuple's _asdict()
    and a dataclass's declared fields only, never instance or class
    attributes beside them (or properties); _attribute_items skips anything
    callable; a bare pandas Index's name is read nowhere; a str subclass is
    a leaf.
    """
    import dataclasses
    import functools

    pd = pytest.importorskip("pandas")

    class WithAttribute(dict):  # type: ignore[type-arg]
        pass

    class WithProperty(dict):  # type: ignore[type-arg]
        @property
        def in_audit(self) -> bool:
            return True

    class WithClassFlag(dict):  # type: ignore[type-arg]
        in_audit = True

    @dataclasses.dataclass
    class Fields:
        payload: Any

    Pair = collections.namedtuple("Pair", ["payload"])

    class PairWithDict(Pair):  # type: ignore[misc, valid-type]
        pass

    class CallableNamespace(types.SimpleNamespace):
        def __call__(self) -> None:
            return None

    class TaggedText(str):
        pass

    attr = WithAttribute(record)
    attr.in_audit = True  # type: ignore[attr-defined]
    fields = Fields(dict(record))
    fields.in_audit = True  # type: ignore[attr-defined]
    pair = PairWithDict(dict(record))
    pair.in_audit = True  # type: ignore[attr-defined]
    tagged = TaggedText("cohort-a")
    tagged.in_audit = True  # type: ignore[attr-defined]
    return [
        ("dict subclass, instance attribute", attr),
        ("dict subclass, property", WithProperty(record)),
        ("dict subclass, class attribute", WithClassFlag(record)),
        ("dataclass, attribute beside its fields", fields),
        ("namedtuple subclass, attribute beside its fields", pair),
        ("manifest = functools.partial", dict(record, manifest=functools.partial(dict, in_audit=True))),
        ("manifest = callable namespace", dict(record, manifest=CallableNamespace(in_audit=True))),
        ("manifest = pd.Index named in_audit", dict(record, manifest=pd.Index(["True"], name="in_audit"))),
        ("str subclass value with an attribute", dict(record, cohort=tagged)),
    ]


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_redteam_m2_in_audit_held_where_the_scan_reads_no_names_is_refused(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # Attack: in_audit True as an attribute, property or class attribute of
    # a record type, inside a callable, as a bare Index's name, or on a str
    # subclass. Observed: the dict subclasses, callables, Index and str
    # subclass are parsed in full under the holdout's id; the dataclass and
    # namedtuple subclass become parse-error records. Correct: refused.
    for label, held in _redteam3_attribute_holders(_redteam_holdout_record(retained)):
        with pytest.raises(ap.PopulationAuditHoldoutError):
            entry(held)
        assert label


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
@pytest.mark.parametrize("key", ["nct_id_y", "nct_id_m", "nct_id_manifest", "NCT Number", "nctNumber"])
def test_redteam_m2_qualified_nct_id_name_that_disagrees_is_refused(
    name: str, entry: Callable[[Any], Any], retained: bool, key: str
) -> None:
    # Attack: the holdout's payload carries the manifest row of another
    # trial (in_audit False), whose id sits under a merge-suffixed or
    # CT.gov-export name. Only names equal to or ending in nct_id are ids,
    # while in_audit is read under any qualified name. Observed: the
    # holdout is parsed in full under its own id. Correct: refused as
    # nct_id_malformed, as the same id under "nct_id" is.
    held = _redteam_holdout_record(retained, in_audit=False, **{key: _CLEAN_NCT})
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        entry(held)
    assert exc_info.value.reason == "nct_id_malformed"


def test_redteam_m2_batch_joined_to_the_manifest_by_position_is_refused() -> None:
    # Attack: retained records and studies (each with a top-level nct_id)
    # joined to the manifest by row position, the default of
    # DataFrame.join, with suffixes ("", "_m"). The manifest has more rows
    # than the batch, so the holdout's own True row is dropped and the
    # holdout gets another trial's False. Observed: both batches parsed,
    # the holdout under its own id. Correct: refused, because the
    # nct_id_m the holdout now carries names another trial.
    pd = pytest.importorskip("pandas")
    manifest = pd.DataFrame(
        {
            "nct_id": [_CLEAN_NCT, "NCT99000003", _HOLDOUT_NCT],
            "in_audit": [False, False, True],
        }
    )
    studies = [_study_carrying(_HOLDOUT_NCT), _study_carrying(_CLEAN_NCT)]
    for study in studies:
        study["nct_id"] = study["protocolSection"]["identificationModule"]["nctId"]
    raws = [_retained_carrying(_HOLDOUT_NCT), _retained_carrying(_CLEAN_NCT)]
    for batch, run in (
        (studies, ap.parse_outcomes_frame),
        (raws, lambda records: [ap.build_outcomes_from_raw(r) for r in records]),
    ):
        joined = pd.DataFrame(batch).merge(
            manifest, left_index=True, right_index=True, how="left", suffixes=("", "_m")
        )
        assert "nct_id_m" in joined.columns
        with pytest.raises(ap.PopulationAuditHoldoutError):
            run(joined.to_dict("records"))


def test_redteam_m2_parser_result_methods_raise_no_io_audit_event() -> None:
    # Claim under test (refused/holds): the result objects' own methods and
    # properties, which a caller uses to serialize what the relies-on-caller
    # functions return, read only the object. Expected and observed: no
    # file, process or network audit event.
    study = _study_carrying(_CLEAN_NCT)
    parsed = ap.parse_study_outcomes(study)
    outcome = parsed.outcomes[0]
    error = ap.parse_error_trial_outcomes(None, None, None, "study_not_a_dict")
    classified = ap.classify_population_description("Safety population")
    calls: list[Callable[[], Any]] = [
        lambda: parsed.to_dict(),
        lambda: outcome.to_dict(),
        lambda: outcome.raw_population_description,
        lambda: error.to_dict(),
        lambda: classified.raw_population_description,
        lambda: ap.compute_vocabulary_sha256(ap.VOCABULARY_RULES),
        lambda: ap.all_pass(ap.structural_checks([parsed, error])),
    ]
    assert _redteam_io_events_during(calls) == []


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_redteam_m2_round3_attempts_that_are_refused(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # Attack (refused): a bool under an enum key and a str-valued enum key
    # (keys that are not exactly str: refused by type), in_audit spelled
    # with a combining grapheme joiner, a no-break space or an ideographic
    # space (dropped by the flag-key rule, which keeps ASCII letters), and a
    # 0-d object array holding the manifest (not JSON: refused by type).
    # Expected and observed: each raises PopulationAuditHoldoutError, so
    # nothing is parsed or recorded. (A full-width key inside a repr string
    # is serialized text, no longer read.)
    import enum

    np = pytest.importorskip("numpy")

    class Key(enum.Enum):
        IN_AUDIT = "in_audit"

    class TextKey(str, enum.Enum):
        IN_AUDIT = "in_audit"

    attempts: list[tuple[str, Any, type[BaseException]]] = []
    plain_enum = _redteam_holdout_record(retained)
    plain_enum[Key.IN_AUDIT] = True  # type: ignore[index]
    attempts.append(("bool under an enum key", plain_enum, ap.PopulationAuditHoldoutError))
    text_enum = _redteam_holdout_record(retained)
    text_enum[TextKey.IN_AUDIT] = True
    attempts.append(("str enum key", text_enum, ap.PopulationAuditHoldoutError))
    for char in ("͏", " ", "　"):
        attempts.append(
            (
                f"in{char!r}_audit",
                _redteam_holdout_record(retained, **{f"in{char}_audit": True}),
                ap.PopulationAuditHoldoutError,
            )
        )
    attempts.append(
        (
            "0-d object array",
            _redteam_holdout_record(
                retained,
                manifest=np.array({"nct_id": _HOLDOUT_NCT, "in_audit": True}, dtype=object),
            ),
            ap.PopulationAuditHoldoutError,
        )
    )
    for label, held, expected in attempts:
        with pytest.raises(expected):
            entry(held)
        assert label


def test_redteam_m2_round3_mixed_batch_with_an_index_named_row_is_refused() -> None:
    # Attack (refused): a clean dict carrying in_audit False beside the
    # holdout as an iterrows() row of a frame indexed by in_audit. The row
    # carries no flag, so the batch gap rule refuses the batch. Expected and
    # observed: in_audit_not_bool.
    pd = pytest.importorskip("pandas")
    row = next(
        pd.DataFrame([_study_carrying(_HOLDOUT_NCT, in_audit=True)]).set_index("in_audit").iterrows()
    )[1]
    with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
        ap.parse_outcomes_frame([_study_carrying(_CLEAN_NCT, in_audit=False), row])
    assert exc_info.value.reason == "in_audit_not_bool"


# ---------------------------------------------------------------------------
# 18. Red team, M2 round 4 (test_redteam_m2_*). Each test asserts the
# CORRECT behaviour, which is refusal, for one attack on an entry point that
# enforces the holdout; a test that fails here names a bypass that is still
# open at fc7bc78 and is not a test to loosen. Tests for attempts that are
# already refused, or for a claim that holds, say so ("Attack (refused)",
# "Claim under test"). Every id is fictional (NCT99000001..NCT99999999);
# nothing touches the network or a file (the sqlite rows come from an
# in-memory database).
# ---------------------------------------------------------------------------


def _redteam4_manifest(nct_id: str = _HOLDOUT_NCT, flag: bool = True) -> dict[str, Any]:
    """The two keys of a site index.json row (data/outcomes/index.json) the holdout rule reads."""
    return {"nct_id": nct_id, "in_audit": flag}


def _redteam4_not_refused(entry: Callable[[Any], Any], cases: list[tuple[str, Any]]) -> list[str]:
    """Labels of the cases ``entry`` does not refuse with PopulationAuditHoldoutError.

    Method: each case is passed to ``entry`` on its own. A case that returns
    is listed by its label; one that raises another exception is listed with
    that exception's type name (it was stopped, but not as a holdout, so a
    caller catching that type would go on). Messages are never formatted.
    """
    missed: list[str] = []
    for label, held in cases:
        try:
            entry(held)
        except ap.PopulationAuditHoldoutError:
            continue
        except Exception as exc:  # noqa: BLE001 - reported by type only
            missed.append(f"{label} ({type(exc).__name__})")
            continue
        missed.append(label)
    return missed


def _redteam4_sqlite_row(nct_id: str, flag: bool) -> Any:
    """A manifest row as a sqlite3.Row from an in-memory database (no file is opened).

    sqlite stores a bool as the integer 1 or 0, so the row holds
    (nct_id, 1): fields with names (Row.keys()) but no items(), and an int,
    not a bool, where the flag was.
    """
    import sqlite3

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("CREATE TABLE manifest (nct_id TEXT, in_audit BOOLEAN)")
        connection.execute("INSERT INTO manifest VALUES (?, ?)", (nct_id, flag))
        return connection.execute("SELECT nct_id, in_audit FROM manifest").fetchone()
    finally:
        connection.close()


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_redteam_m2_round4_nested_sqlite_row_is_refused(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # Attack (LOW-MEDIUM): the holdout's manifest row read from SQLite with
    # row_factory=sqlite3.Row and kept under "manifest". sqlite3.Row has
    # keys() and [] but no items(), so _keyed_items does not see its names
    # and the scan walks it by position: a str and the int 1 (sqlite's
    # True), both leaves. (At the top level the same row is refused as a
    # record whose fields have no names.) Observed: parsed in full under
    # NCT99000001. Correct: refused (in_audit is 1, not a bool).
    held = _redteam_holdout_record(retained, manifest=_redteam4_sqlite_row(_HOLDOUT_NCT, True))
    assert _redteam4_not_refused(entry, [("nested sqlite3.Row", held)]) == []


def test_redteam_m2_round4_parse_outcomes_frame_single_sqlite_row_is_refused() -> None:
    # Attack (LOW): the holdout's manifest row (a sqlite3.Row) passed where
    # a batch is expected. The single-record guard knows mappings,
    # namedtuples, numpy records and dataclasses, not sqlite3.Row, so the
    # row is iterated: its id (a str) and its flag (the int 1) each become
    # a "study_not_a_dict" parse-error row. Observed: a two-row frame of
    # parse errors. Correct: TypeError or a holdout refusal, as for a
    # single dict or namedtuple.
    with pytest.raises((ap.PopulationAuditHoldoutError, TypeError)):
        ap.parse_outcomes_frame(_redteam4_sqlite_row(_HOLDOUT_NCT, True))


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_redteam_m2_round4_int_enum_key_naming_in_audit_is_refused(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # Attack (LOW; round 3's "enum key holding the CSV string 'True'",
    # still open for int-valued enums): a key that is an IntEnum or IntFlag
    # member named IN_AUDIT, holding the CSV string "True" or sqlite's 1.
    # _name_texts returns no text for any int before it reaches str(), so
    # the key names nothing and its value is a leaf. Observed: parsed in
    # full under NCT99000001. Correct: refused (the value is not a bool),
    # as Key.IN_AUDIT of a plain Enum is.
    import enum

    class IntKey(enum.IntEnum):
        IN_AUDIT = 1

    class FlagKey(enum.IntFlag):
        IN_AUDIT = 1

    cases: list[tuple[str, Any]] = []
    for key in (IntKey.IN_AUDIT, FlagKey.IN_AUDIT):
        for value in ("True", 1):
            held = _redteam_holdout_record(retained)
            held[key] = value  # type: ignore[index]
            cases.append((f"{type(key).__name__} key holding {value!r}", held))
    assert _redteam4_not_refused(entry, cases) == []


def _redteam4_stamped_ids(out: Any) -> list[Any]:
    """The nct_id(s) an entry point's output is stamped with, as stored there."""
    if isinstance(out, ap.TrialAnalysisPopulations):
        return [out.nct_id]
    if isinstance(out, dict):
        return [out.get("nct_id")]
    return list(out["nct_id"])


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_redteam_m2_round4_output_is_stamped_with_the_id_that_was_checked(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # Attack (LOW; needs a str subclass): the record says in_audit False for
    # the clean trial, and its nctId (top-level nct_id for a retained
    # record) is a str subclass whose content is NCT99000002 but whose
    # str() is NCT99000001. The check reads the raw value (canonical, equal
    # to every other id in the record); _nct_of and build_outcomes_from_raw
    # then stamp str(value). Observed: the output is stamped NCT99000001,
    # the holdout. Correct: refused, or stamped with exactly the plain str
    # that was checked.
    class Shown(str):
        def __str__(self) -> str:
            return _HOLDOUT_NCT

    make = _retained_carrying if retained else _study_carrying
    held = make(Shown(_CLEAN_NCT), in_audit=False)
    try:
        out = entry(held)
    except ap.PopulationAuditHoldoutError:
        return
    stamped = _redteam4_stamped_ids(out)
    assert all(type(value) is str and value == _CLEAN_NCT for value in stamped), [
        "holdout id" if str(value) == _HOLDOUT_NCT else type(value).__name__ for value in stamped
    ]


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_redteam_m2_round4_attempts_that_are_refused(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # Attack (refused): the manifest as an email.message.Message header
    # ("In-Audit") or a configparser section, an int subclass carrying
    # in_audit as an attribute, a Series row given in_audit by attribute
    # assignment, an IntEnum key holding True, and the sqlite3.Row itself
    # as the record (none of them JSON: refused by type); to_dict("tight")
    # and to_dict("split") with bools (a bool held by position in a list).
    # Expected and observed: each raises PopulationAuditHoldoutError. (TOML
    # text, a query string and pandas to_json text are serialized strings,
    # no longer read.)
    import configparser
    import email.message
    import enum
    import warnings

    pd = pytest.importorskip("pandas")
    manifest = _redteam4_manifest()
    frame = pd.DataFrame([manifest])
    header = email.message.Message()
    header["In-Audit"] = "true"
    section = configparser.ConfigParser()
    section.read_string("[manifest]\nin_audit = true\n")

    class TaggedInt(int):
        pass

    tagged = TaggedInt(1)
    tagged.in_audit = True  # type: ignore[attr-defined]

    class IntKey(enum.IntEnum):
        IN_AUDIT = 1

    int_key = _redteam_holdout_record(retained)
    int_key[IntKey.IN_AUDIT] = True  # type: ignore[index]
    row = pd.Series(_redteam_holdout_record(retained))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        row.in_audit = True  # the attribute, not a label: pandas warns and sets it
    cases: list[tuple[str, Any]] = [
        ("email.message.Message", _redteam_holdout_record(retained, manifest=header)),
        ("configparser.ConfigParser", _redteam_holdout_record(retained, manifest=section)),
        ("to_dict('tight')", _redteam_holdout_record(retained, manifest=frame.to_dict("tight"))),
        ("to_dict('split') with bools", _redteam_holdout_record(retained, manifest=frame.to_dict("split"))),
        ("int subclass with an in_audit attribute", _redteam_holdout_record(retained, rank=tagged)),
        ("Series row with an in_audit attribute", row),
        ("IntEnum key holding True", int_key),
        ("sqlite3.Row as the record", _redteam4_sqlite_row(_HOLDOUT_NCT, True)),
    ]
    assert _redteam4_not_refused(entry, cases) == []


def test_redteam_m2_round4_dc38220_bundle_parses_before_any_holdout_branch() -> None:
    # Claim under test (module docstring; holds): at dc38220,
    # build_trial_bundle takes in_audit from its argument and reads the
    # trial mapping's own in_audit only when that argument is None, and it
    # calls extract_reported_results(trial, ...) (which runs an injected
    # population_parser) as a statement of its own body, outside any branch
    # on the flag. So a holdout passed with in_audit=True and a trial
    # mapping without the key reaches the parser, which sees no flag. Only
    # source code is read. Expected and observed: the claim holds.
    layout = ast.parse(_redteam_git_show("dc38220", "src/outcomes_layout.py"))
    funcs = {node.name: node for node in layout.body if isinstance(node, ast.FunctionDef)}
    bundle = funcs["build_trial_bundle"]
    assert any(
        isinstance(node, ast.Assign)
        and ast.unparse(node)
        == "raw_in_audit = in_audit if in_audit is not None else trial.get('in_audit')"
        for node in bundle.body
    )
    calls = [
        node.value
        for node in bundle.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func) == "extract_reported_results"
    ]
    assert len(calls) == 1 and ast.unparse(calls[0].args[0]) == "trial"
    assert {kw.arg for kw in calls[0].keywords} >= {"population_parser"}


# ---------------------------------------------------------------------------
# 19. The consolidated holdout guard (src/holdout_guard.py): the flag-key
# rule, merge-suffixed flags and refusal by type, at every entry point.
# Every id is fictional (NCT99000001..NCT99999999); nothing touches the
# network or a file.
# ---------------------------------------------------------------------------


def test_flag_key_rule_names_and_non_names() -> None:
    # The flag is an exact name once the last "."-segment is taken, pandas'
    # _x/_y merge suffixes are removed and only ASCII letters are kept, or a
    # name whose ASCII words hold "in" then "audit" (a custom join suffix or
    # prefix). No substring matching: words that merely contain "audit" are fields.
    from src import holdout_guard as hg

    flags = [
        "in_audit", "inAudit", "IN_AUDIT", "in-audit", "In Audit", "in_audit ",
        "is_in_audit", "isInAudit", "in_audit_x", "in_audit_y", "IN_AUDIT_Y",
        "manifest.in_audit", "manifest.in_audit_y", "in​_audit", "inaudit",
        "in_audit_manifest", "manifest_in_audit", "in_audit_flag", "InAuditFlag",
        "manifest.in_audit_manifest",
    ]
    not_flags: list[Any] = [
        "domain_audit", "main_audit", "login_audit", "within_audit", "plugin_audit",
        "audit", "audit_in", "in.audit", "inaudit_x2", "ｉｎ＿ａｕｄｉｔ", "іn_audit",
        b"in_audit", None, 1,
    ]
    ids = ["nct_id", "nctId", "NCT Number", "nctNumber", "nct_id_x", "nct_id_m",
           "manifest.nct_id", "NCT ID"]
    not_ids: list[Any] = ["nctIdAliases", "nct_id_aliases", "org_study_id", "id", b"nct_id"]
    assert [k for k in flags if not hg.is_flag_key(k)] == []
    assert [k for k in not_flags if hg.is_flag_key(k)] == []
    assert [k for k in ids if not hg.is_id_key(k)] == []
    assert [k for k in not_ids if hg.is_id_key(k)] == []


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_audit_words_that_are_not_the_flag_change_nothing(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # Before the consolidation any key whose folded name contained "inaudit"
    # was a flag, so a clean record with domain_audit, main_audit or
    # login_audit was refused ("in_audit_true" for True, "in_audit_not_bool"
    # for anything else). They are ordinary fields now: the record parses as
    # it does without them, whatever they hold.
    plain = entry(_clean_record(retained))
    for key in ("domain_audit", "main_audit", "login_audit"):
        for value in (True, False, "yes", None, {"domain_audit": True}):
            record = _clean_record(retained)
            record[key] = value
            _assert_same_output(name, entry(record), plain)


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_agreeing_merge_suffixed_flags_are_accepted_and_disagreeing_refused(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # A record from merging two frames that both carry in_audit holds
    # in_audit_x and in_audit_y. Both False: a clean record, parsed as
    # without them. Either True: a holdout. A non-bool in either: refused.
    plain = entry(_clean_record(retained))
    agreeing = _clean_record(retained)
    agreeing.update(in_audit_x=False, in_audit_y=False)
    _assert_same_output(name, entry(agreeing), plain)
    for x, y, reason in (
        (False, True, "in_audit_true"),
        (True, False, "in_audit_true"),
        (False, "False", "in_audit_not_bool"),
    ):
        record = _clean_record(retained)
        record.update(in_audit_x=x, in_audit_y=y)
        with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
            entry(record)
        assert exc_info.value.reason == reason, (x, y)


def test_parse_outcomes_frame_accepts_records_from_a_merge_of_two_flagged_frames() -> None:
    # The realistic shape of the case above: studies already carrying
    # in_audit False merged with a manifest whose in_audit is False too.
    pd = pytest.importorskip("pandas")
    studies = [_study_carrying(_CLEAN_NCT, in_audit=False), _study_carrying("NCT99000003", in_audit=False)]
    for study in studies:
        study["nct_id"] = study["protocolSection"]["identificationModule"]["nctId"]
    manifest = pd.DataFrame({"nct_id": [_CLEAN_NCT, "NCT99000003"], "in_audit": [False, False]})
    merged = pd.DataFrame(studies).merge(manifest, on="nct_id", how="left")
    assert {"in_audit_x", "in_audit_y"} <= set(merged.columns)
    rows = ap.parse_outcomes_frame(merged.to_dict("records"))
    assert rows.equals(ap.parse_outcomes_frame([_study_carrying(_CLEAN_NCT), _study_carrying("NCT99000003")]))


@pytest.mark.parametrize(("name", "entry", "retained"), _REDTEAM_ALL_ENTRY_POINTS)
def test_non_json_input_is_refused_by_type_at_every_entry_point(
    name: str, entry: Callable[[Any], Any], retained: bool
) -> None:
    # A record is a dict of JSON data, flag or not: any other value or key
    # type anywhere in it, and any other record type, is refused by type
    # ("in_audit_not_bool") without being read, so no disguise inside it
    # matters. JSON values (nested dicts and lists, numbers, None) pass.
    import decimal
    import enum

    np = pytest.importorskip("numpy")

    class Text(str):
        pass

    class Key(enum.Enum):
        NOTE = "note"

    values: list[tuple[str, Any]] = [
        ("tuple", ("a",)),
        ("set", {"a"}),
        ("bytes", b"a"),
        ("UserDict", collections.UserDict(note="a")),
        ("Decimal", decimal.Decimal("1")),
        ("numpy int", np.int64(1)),
        ("str subclass", Text("a")),
        ("generator", (x for x in ["a"])),
        ("bool in a list", ["a", False]),
    ]
    records: list[tuple[str, Any]] = []
    for label, value in values:
        record = _clean_record(retained)
        record["extra"] = {"nested": [value]} if label == "numpy int" else value
        records.append((label, record))
    for label, key in (("Enum key", Key.NOTE), ("int key", 1)):
        record = _clean_record(retained)
        record[key] = "a"
        records.append((label, record))
    records.append(("UserDict record", collections.UserDict(_clean_record(retained))))
    records.append(("tuple record", (_CLEAN_NCT,)))
    records.append(("bool record", True))
    for label, record in records:
        with pytest.raises(ap.PopulationAuditHoldoutError) as exc_info:
            entry(record)
        assert exc_info.value.reason == "in_audit_not_bool", label
        assert "refused by type" in str(exc_info.value), label
    plain = entry(_clean_record(retained))
    json_only = _clean_record(retained)
    json_only["extra"] = {"list": [1, 2.5, None, "a", {"ok": True}], "count": 0}
    _assert_same_output(name, entry(json_only), plain)
