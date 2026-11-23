"""Tests for the deterministic baseline age engine (age/).

Because tests/ has no __init__.py in this repository, pytest's default prepend
import mode loads tests/age/__init__.py as top-level package 'age'. Extending
__path__ with the repository's production age/ package directory and importing
its public symbols ensures both `age.*` production exports and `tests/age/test_*.py`
resolve cleanly.
"""

from __future__ import annotations

from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_PROD_AGE_DIR = _THIS_DIR.parent.parent / "age"
if str(_PROD_AGE_DIR) not in __path__:
    __path__.insert(0, str(_PROD_AGE_DIR))

from age.parser import (  # noqa: E402
    AGE_REPORT_STATUS_LEVELS,
    EXCLUDED_AGE_KINDS,
    EXPECTED_VOCABULARY_SHA256,
    MEASURE_KINDS,
    PARSER_RULES_VERSION,
    UNINFORMATIVE_REASON_PRIORITY,
    VOCABULARY_SHA256,
    GroupSelection,
    ParsedAgeTrial,
    ParsedBracket,
    ParsedCategoricalMeasure,
    ParsedContinuousMeasure,
    ParsedInterval,
    classify_age_reporting,
    compute_vocabulary_sha256,
    derive_age_outcomes,
    measure_kind,
    parse_categorical_measure,
    parse_continuous_measure,
    parse_interval_label,
    parse_trial,
    select_age_measures,
    select_excluded_age_measures,
    select_overall_group,
    validate_vocabulary_pin,
)
from age.representation import (  # noqa: E402
    STANDARD_TARGET_BANDS,
    UNIT_CONVERSION_TOL_YEARS,
    age_range_bounds,
    audit_eligibility_vs_enrolled,
    compare_age_to_acs,
    standard_age_band_bounds,
)
from age.table import (  # noqa: E402
    COLUMNS,
    LEAN_COLUMNS,
    RAW_SCHEMA_VERSION,
    STATES,
    all_pass,
    build_age_row,
    build_age_row_from_raw,
    lean_age_row,
    ordered_age_row,
    parse_error_row,
    read_age_csv,
    read_age_jsonl,
    read_raw_age_jsonl,
    select_raw_age_measures,
    status_counts,
    structural_checks,
    write_age_csv,
    write_age_jsonl,
    write_raw_age_jsonl,
)

SCHEMA_VERSION: int = 1
METHOD_VERSION: str = "baseline-age-1"
