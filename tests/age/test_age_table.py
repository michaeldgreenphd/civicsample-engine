"""Unit tests for age/table.py (rows, raw envelopes, no-balancing invariant, and I/O)."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from age import parser as ap  # noqa: E402
from age import table as at  # noqa: E402

SNAP = "2026-11-23"


def _study(
    nct_id: str,
    measures: list[dict],
    *,
    enrollment: int | None = 100,
    min_age: str | None = "18 Years",
    max_age: str | None = "N/A",
    std_ages: list[str] | None = None,
    groups: list[dict] | None = None,
    denoms: list[dict] | None = None,
) -> dict:
    elig: dict = {}
    if min_age is not None:
        elig["minimumAge"] = min_age
    if max_age is not None:
        elig["maximumAge"] = max_age
    if std_ages is not None:
        elig["stdAges"] = std_ages

    design: dict = {}
    if enrollment is not None:
        design["enrollmentInfo"] = {"count": enrollment, "type": "ACTUAL"}

    bm: dict = {"measures": measures}
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


def _cat_m(rows: list[tuple[str, str | int]], *, param_type: str = "COUNT_OF_PARTICIPANTS", unit: str = "Participants") -> dict:
    return {
        "title": "Age, Categorical",
        "paramType": param_type,
        "unitOfMeasure": unit,
        "classes": [
            {
                "categories": [
                    {"title": t, "measurements": [{"groupId": "BG000", "value": str(v)}]}
                    for t, v in rows
                ]
            }
        ],
    }


def _cont_m(val: str = "60.2", spread: str = "10.4", unit: str = "years") -> dict:
    return {
        "title": "Age, Continuous",
        "paramType": "MEAN",
        "dispersionType": "STANDARD_DEVIATION",
        "unitOfMeasure": unit,
        "classes": [{"categories": [{"measurements": [{"groupId": "BG000", "value": val, "spread": spread}]}]}],
    }


# ---------------------------------------------------------------------------
# 1. No denominator balancing & enrollment_minus_parsed rules
# ---------------------------------------------------------------------------


def test_enrollment_minus_parsed_never_added_to_unknown_or_status() -> None:
    s = _study("NCT99000101", [_cat_m([("18 to 64 years", 50), (">= 65 years", 35)])], enrollment=100)
    row = at.build_age_row(s, SNAP)
    assert row["age_report_status"] == "reported_categorical_only"
    assert row["n_classified"] == 85.0
    assert row["n_unknown"] is None
    assert row["n_total_parsed"] == 85.0
    assert row["enrollment_minus_parsed"] == 15.0


def test_enrollment_minus_parsed_across_states_and_exceeding_enrollment() -> None:
    # Continuous-only has no categorical participant count -> enrollment_minus_parsed is None
    r_cont = at.build_age_row(_study("NCT99000102", [_cont_m()], enrollment=100), SNAP)
    assert r_cont["age_report_status"] == "reported_continuous_only"
    assert r_cont["enrollment_minus_parsed"] is None

    # Explicit unknown only keeps n_unknown untouched and status explicit_unknown_only
    r_unk = at.build_age_row(_study("NCT99000103", [_cat_m([("18-64 years", 0), ("Unknown", 12)])], enrollment=50), SNAP)
    assert r_unk["age_report_status"] == "explicit_unknown_only"
    assert r_unk["n_unknown"] == 12.0
    assert r_unk["enrollment_minus_parsed"] == 38.0

    # COUNT_OF_UNITS -> is_participant_count False -> enrollment_minus_parsed is None
    r_units = at.build_age_row(
        _study("NCT99000104", [_cat_m([("18-64 years", 40)], param_type="COUNT_OF_UNITS", unit="Eyes")], enrollment=20),
        SNAP,
    )
    assert r_units["is_participant_count"] is False
    assert r_units["enrollment_minus_parsed"] is None

    # Exceeds enrollment -> negative gap preserved verbatim, flag_exceeds_enrollment True
    r_over = at.build_age_row(_study("NCT99000105", [_cat_m([("18-64 years", 70), (">=65 years", 50)])], enrollment=100), SNAP)
    assert r_over["enrollment_minus_parsed"] == -20.0
    assert r_over["flag_exceeds_enrollment"] is True

    # Missing enrollment -> None
    r_no_enr = at.build_age_row(_study("NCT99000106", [_cat_m([("18-64 years", 40)])], enrollment=None), SNAP)
    assert r_no_enr["enrollment"] is None
    assert r_no_enr["enrollment_minus_parsed"] is None


def test_table_module_contains_zero_denominator_balancing_code() -> None:
    src = inspect.getsource(at)
    for forbidden in ("get_total_baseline_participants", "remainder"):
        assert forbidden not in src


# ---------------------------------------------------------------------------
# 2. Raw measure envelope & offline re-parse equivalence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("nct_id", "measures", "groups", "denoms"),
    [
        ("NCT99000201", [_cat_m([("18-64 years", 60), (">=65 years", 40)]), _cont_m()], None, None),
        (
            "NCT99000202",
            [_cont_m(), {"title": "Age at Diagnosis", "paramType": "MEAN", "unitOfMeasure": "years"}, {"title": "Dosage"}],
            None,
            None,
        ),
        (
            "NCT99000203",
            [
                {
                    "title": "Age Group",
                    "paramType": "PERCENTAGE",
                    "unitOfMeasure": "%",
                    "classes": [
                        {
                            "categories": [
                                {"title": "< 65 years", "measurements": [{"groupId": "BG002", "value": "75"}]},
                                {"title": ">= 65 years", "measurements": [{"groupId": "BG002", "value": "25"}]},
                            ]
                        }
                    ],
                }
            ],
            [
                {"id": "BG000", "title": "Arm A"},
                {"id": "BG001", "title": "Arm B"},
                {"id": "BG002", "title": "Total"},
            ],
            [{"units": "Participants", "counts": [{"groupId": "BG002", "value": "120"}]}],
        ),
        ("NCT99000204", [], None, None),
    ],
)
def test_select_raw_age_measures_reparse_equals_direct_study_parse(
    nct_id: str,
    measures: list[dict],
    groups: list[dict] | None,
    denoms: list[dict] | None,
) -> None:
    study = _study(
        nct_id,
        measures,
        enrollment=120,
        min_age="18 Years",
        max_age="85 Years",
        std_ages=["ADULT", "OLDER_ADULT"],
        groups=groups,
        denoms=denoms,
    )
    raw_env = at.select_raw_age_measures(study)
    assert raw_env["schema_version"] == at.RAW_SCHEMA_VERSION
    assert raw_env["nct_id"] == nct_id
    assert raw_env["min_age"] == "18 Years"
    assert raw_env["max_age"] == "85 Years"
    assert raw_env["std_ages"] == ["ADULT", "OLDER_ADULT"]

    direct = at.build_age_row(study, SNAP, refetched=True)
    rebuilt = at.build_age_row_from_raw(raw_env, SNAP, refetched=True)
    assert direct == rebuilt


def test_build_age_row_from_raw_rejects_incompatible_schema_version() -> None:
    with pytest.raises(ValueError, match="unsupported raw age schema_version"):
        at.build_age_row_from_raw({"schema_version": "age_raw_measures/99.0", "nct_id": "NCT99000299"})


# ---------------------------------------------------------------------------
# 3. parse_error safety net & lean_age_row
# ---------------------------------------------------------------------------


def test_parse_error_row_and_unexpected_exception_never_drops_trial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pe = at.parse_error_row("NCT99000301", 80, SNAP, error_detail="synthetic")
    assert pe["age_report_status"] == "parse_error"
    assert pe["reported_age_any"] is None
    assert pe["reported_categorical"] is None
    assert pe["reported_continuous"] is None
    assert pe["enrollment_minus_parsed"] is None
    assert pe["parser_rules_version"] == ap.PARSER_RULES_VERSION
    assert pe["vocabulary_sha256"] == ap.VOCABULARY_SHA256

    # Malformed baselineCharacteristicsModule
    bad_study = {
        "protocolSection": {"identificationModule": {"nctId": "NCT99000302"}},
        "resultsSection": {"baselineCharacteristicsModule": "corrupt_string"},
    }
    r_bad = at.build_age_row(bad_study, SNAP)
    assert r_bad["nct_id"] == "NCT99000302"
    assert r_bad["age_report_status"] == "parse_error"

    # Unexpected internal exception inside ap.parse_trial
    def _boom(*args: object, **kwargs: object) -> ap.ParsedAgeTrial:
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(ap, "parse_trial", _boom)
    r_exc = at.build_age_row(_study("NCT99000303", [_cont_m()]), SNAP)
    assert r_exc["nct_id"] == "NCT99000303"
    assert r_exc["age_report_status"] == "parse_error"
    assert "RuntimeError" in (r_exc["error_detail"] or "")


def test_lean_age_row_and_column_ordering() -> None:
    row = at.build_age_row(
        _study("NCT99000310", [_cat_m([("18-64 years", 30), (">=65 years", 20)]), _cont_m()], enrollment=50),
        SNAP,
    )
    assert list(row.keys()) == at.COLUMNS
    lean = at.lean_age_row(row)
    assert list(lean.keys()) == at.LEAN_COLUMNS
    assert lean["age_report_status"] == "reported_both"
    assert lean["vocabulary_sha256"] == ap.VOCABULARY_SHA256


# ---------------------------------------------------------------------------
# 4. JSONL / CSV round-trips & structural checks
# ---------------------------------------------------------------------------


def test_jsonl_and_csv_round_trip_plain_and_gzip(tmp_path: Path) -> None:
    studies = [
        _study("NCT99000401", [_cat_m([("18-64 years", 40), (">=65 years", 10)]), _cont_m()], enrollment=55),
        _study("NCT99000402", [_cat_m([("18-64 years", 0), ("Unknown; Not Reported", 8)])], enrollment=10),
        _study("NCT99000403", [_cat_m([("18-65", 10), ("65-80", 10)])], enrollment=20),
        _study("NCT99000404", [{"title": "Age at Diagnosis", "paramType": "MEAN", "unitOfMeasure": "years"}], enrollment=30),
    ]
    rows = [at.build_age_row(s, SNAP) for s in studies]
    rows.append(at.parse_error_row("NCT99000405", 25, SNAP, error_detail="corrupt"))

    for ext in (".jsonl", ".jsonl.gz"):
        p_j = tmp_path / f"age_rows{ext}"
        assert at.write_age_jsonl(rows, p_j) == 5
        assert at.read_age_jsonl(p_j) == rows

    for ext in (".csv", ".csv.gz"):
        p_c = tmp_path / f"age_rows{ext}"
        assert at.write_age_csv(rows, p_c) == 5
        assert at.read_age_csv(p_c) == rows

    raw_envs = [at.select_raw_age_measures(s) for s in studies]
    p_raw = tmp_path / "raw_age.jsonl.gz"
    assert at.write_raw_age_jsonl(raw_envs, p_raw) == 4
    assert at.read_raw_age_jsonl(p_raw) == raw_envs


def test_structural_checks_pass_and_detect_corruption() -> None:
    rows = [
        at.build_age_row(_study("NCT99000501", [_cat_m([("18-64 years", 20), (">=65 years", 5)])]), SNAP),
        at.build_age_row(_study("NCT99000502", [_cont_m()]), SNAP),
        at.build_age_row(_study("NCT99000503", [_cat_m([("Unknown", 6)])]), SNAP),
        at.build_age_row(_study("NCT99000504", [_cat_m([("18-64 years", 10)], param_type="COUNT_OF_UNITS", unit="Eyes")]), SNAP),
        at.build_age_row(_study("NCT99000505", []), SNAP),
        at.parse_error_row("NCT99000506", 10, SNAP),
    ]
    checks = at.structural_checks(rows)
    assert at.all_pass(checks), checks

    # Corrupt COUNT_OF_UNITS row
    corrupted = [dict(r) for r in rows]
    corrupted[3]["is_participant_count"] = True
    checks_bad = at.structural_checks(corrupted)
    assert checks_bad["count_of_units_never_enters_participant_counts"]["pass"] is False
