"""
age/table.py — per-trial baseline age row, versioned raw envelope, and I/O helpers.

READS   a raw ClinicalTrials.gov API v2 study record, or a retained raw age
        envelope produced by select_raw_age_measures(study).
WRITES  flat per-trial age rows (full and lean subsets) and JSONL/CSV serializations.

INVARIANTS
  1. enrollment_minus_parsed is registered enrollment minus n_total_parsed ONLY
     when a categorical participant count exists (is_participant_count is True
     and n_total_parsed is not None). It is NEVER added to n_unknown and NEVER
     an input to age_report_status ("no denominator balancing").
  2. Any unexpected exception during study or raw-envelope parsing becomes an
     explicit row with age_report_status == "parse_error" and None boolean
     outcomes, never dropping a trial silently.
  3. Retained raw age envelopes (select_raw_age_measures) re-parse via
     build_age_row_from_raw to the exact same row as build_age_row(study).
"""

from __future__ import annotations

import csv
import gzip
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, TextIO, Union

from age import parser as ap

RAW_SCHEMA_VERSION: str = "age_raw_measures/1.0"

STATES: tuple[str, ...] = ap.AGE_REPORT_STATUS_LEVELS
VISIBLE_STATES: tuple[str, ...] = (
    "reported_both",
    "reported_categorical_only",
    "reported_continuous_only",
    "explicit_unknown_only",
    "uninformative",
    "not_reported",
)
OUTCOMES: tuple[str, ...] = (
    "reported_age_any",
    "reported_categorical",
    "reported_continuous",
)

PARSER_COLUMNS: list[str] = [
    "raw_present",
    "parse_ok",
    "n_selected_measures",
    "n_excluded_age_measures",
    "excluded_age_measure_titles",
    "excluded_age_measure_kinds",
    "group_selection_strategy",
    "overall_group_id",
    "age_report_status",
    "reported_age_any",
    "reported_categorical",
    "reported_continuous",
    "has_reported_categorical",
    "categorical_measure_title",
    "categorical_param_type",
    "categorical_unit_of_measure",
    "categorical_layout",
    "is_participant_count",
    "parsed_brackets",
    "n_classified",
    "n_unknown",
    "n_total_parsed",
    "brackets_tile_cleanly",
    "has_reported_continuous",
    "continuous_measure_title",
    "central_tendency_type",
    "dispersion_type",
    "continuous_unit_of_measure",
    "central_value",
    "dispersion_value",
    "dispersion_low",
    "dispersion_high",
    "full_range_min",
    "full_range_max",
    "mean_years",
    "sd_years",
    "se_years",
    "median_years",
    "geometric_mean_years",
    "least_squares_mean_years",
    "iqr_width_years",
    "iqr_low_years",
    "iqr_high_years",
    "ci95_low_years",
    "ci95_high_years",
    "min_years",
    "max_years",
    "uninformative_reason",
    "flag_overlapping_brackets",
    "flag_gap_between_brackets",
    "flag_nonparticipant_units",
    "flag_percentage_units",
    "flag_percentage_converted",
    "flag_summed_arms_no_total",
    "flag_ambiguous_groups",
    "flag_unmapped_category",
    "flag_customized_layout",
    "flag_unrecognized_or_mixed_unit",
    "flag_impossible_continuous_value",
    "flag_exceeds_enrollment",
    "unknown_labels",
    "unmapped_labels",
]

EXTRA_COLUMNS: list[str] = [
    "enrollment",
    "enrollment_minus_parsed",
    "min_age",
    "max_age",
    "std_ages",
    "refetched",
    "error_detail",
    "snapshot_date",
    "parser_rules_version",
    "vocabulary_sha256",
    "parser_module_version",
]

COLUMNS: list[str] = ["nct_id"] + PARSER_COLUMNS + EXTRA_COLUMNS

LEAN_COLUMNS: list[str] = [
    "age_report_status",
    "reported_age_any",
    "reported_categorical",
    "reported_continuous",
    "is_participant_count",
    "n_classified",
    "n_unknown",
    "n_total_parsed",
    "enrollment_minus_parsed",
    "brackets_tile_cleanly",
    "mean_years",
    "sd_years",
    "median_years",
    "iqr_low_years",
    "iqr_high_years",
    "min_years",
    "max_years",
    "uninformative_reason",
    "parser_rules_version",
    "vocabulary_sha256",
]

JSON_COLUMNS: tuple[str, ...] = (
    "excluded_age_measure_titles",
    "excluded_age_measure_kinds",
    "parsed_brackets",
    "unknown_labels",
    "unmapped_labels",
    "std_ages",
)

_BOOL_COLUMNS: frozenset[str] = frozenset(
    {
        "raw_present",
        "parse_ok",
        "reported_age_any",
        "reported_categorical",
        "reported_continuous",
        "has_reported_categorical",
        "is_participant_count",
        "brackets_tile_cleanly",
        "has_reported_continuous",
        "flag_overlapping_brackets",
        "flag_gap_between_brackets",
        "flag_nonparticipant_units",
        "flag_percentage_units",
        "flag_percentage_converted",
        "flag_summed_arms_no_total",
        "flag_ambiguous_groups",
        "flag_unmapped_category",
        "flag_customized_layout",
        "flag_unrecognized_or_mixed_unit",
        "flag_impossible_continuous_value",
        "flag_exceeds_enrollment",
        "refetched",
    }
)

_FLOAT_COLUMNS: frozenset[str] = frozenset(
    {
        "n_classified",
        "n_unknown",
        "n_total_parsed",
        "central_value",
        "dispersion_value",
        "dispersion_low",
        "dispersion_high",
        "full_range_min",
        "full_range_max",
        "mean_years",
        "sd_years",
        "se_years",
        "median_years",
        "geometric_mean_years",
        "least_squares_mean_years",
        "iqr_width_years",
        "iqr_low_years",
        "iqr_high_years",
        "ci95_low_years",
        "ci95_high_years",
        "min_years",
        "max_years",
        "enrollment",
        "enrollment_minus_parsed",
    }
)

_INT_COLUMNS: frozenset[str] = frozenset(
    {
        "n_selected_measures",
        "n_excluded_age_measures",
    }
)


def today_utc() -> str:
    """UTC date stamp YYYY-MM-DD."""
    return datetime.now(timezone.utc).date().isoformat()


def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f < 0.0:
        return None
    return f


def _sanitize_json_value(obj: Any) -> Any:
    """Recursively replace non-finite Python floats so raw envelopes are valid RFC 8259 JSON."""
    if isinstance(obj, float):
        if math.isnan(obj):
            return "NaN"
        if math.isinf(obj):
            return "Infinity" if obj > 0 else "-Infinity"
        return obj
    if isinstance(obj, dict):
        return {str(k): _sanitize_json_value(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_json_value(x) for x in obj]
    return obj


def _nct_of(study: dict[str, Any]) -> Optional[str]:
    if not isinstance(study, dict):
        return None
    ident = ((study.get("protocolSection") or {}).get("identificationModule")) or {}
    nct = ident.get("nctId")
    if nct is not None:
        return str(nct)
    top = study.get("nct_id")
    return None if top is None else str(top)


def _enrollment_of(study: dict[str, Any]) -> Any:
    if not isinstance(study, dict):
        return None
    design = ((study.get("protocolSection") or {}).get("designModule")) or {}
    enr_info = design.get("enrollmentInfo") or {}
    if "count" in enr_info:
        return enr_info.get("count")
    return study.get("enrollment")


def _eligibility_fields_of(
    study: dict[str, Any],
) -> tuple[Optional[str], Optional[str], list[str]]:
    if not isinstance(study, dict):
        return None, None, []
    elig = ((study.get("protocolSection") or {}).get("eligibilityModule")) or {}
    min_age = elig.get("minimumAge")
    if min_age is None and "min_age" in study:
        min_age = study.get("min_age")
    max_age = elig.get("maximumAge")
    if max_age is None and "max_age" in study:
        max_age = study.get("max_age")
    std_raw = elig.get("stdAges")
    if std_raw is None and "std_ages" in study:
        std_raw = study.get("std_ages")
    std_ages = [str(x) for x in std_raw] if isinstance(std_raw, list) else []
    return (
        None if min_age is None else str(min_age),
        None if max_age is None else str(max_age),
        std_ages,
    )


def select_raw_age_measures(study: dict[str, Any]) -> dict[str, Any]:
    """
    Build a versioned raw envelope retaining everything needed to re-parse a
    trial's baseline age offline without re-pulling ClinicalTrials.gov:
      nct_id, enrollment, min_age, max_age, std_ages, baseline_groups,
      baseline_denoms, measures (selected participant baseline age measures),
      excluded_measures (excluded age-kind measures for audit reproducibility),
      raw_parse_error (when upstream container fields are malformed), and
      schema_version.
    """
    if not isinstance(study, dict):
        raise TypeError("study must be a dict")
    min_age, max_age, std_ages = _eligibility_fields_of(study)
    enr_raw = _enrollment_of(study)
    if isinstance(enr_raw, bool) or (isinstance(enr_raw, float) and not math.isfinite(enr_raw)):
        enr_clean: Any = None
    else:
        enr_clean = _sanitize_json_value(enr_raw)

    raw_parse_error: Optional[str] = None
    rs = study.get("resultsSection")
    if rs is not None and not isinstance(rs, dict):
        raw_parse_error = "malformed_results_section"
        module: dict[str, Any] = {}
    else:
        mod_candidate = (rs or {}).get("baselineCharacteristicsModule")
        if mod_candidate is not None and not isinstance(mod_candidate, dict):
            raw_parse_error = "malformed_baseline_module"
            module = {}
        else:
            module = mod_candidate or {}

    measures_raw = module.get("measures")
    measures_list: list[dict[str, Any]] = []
    if raw_parse_error is None and measures_raw is not None:
        if isinstance(measures_raw, (str, bytes)) or not isinstance(measures_raw, list):
            raw_parse_error = "malformed_measures"
        elif any(not isinstance(m, dict) for m in measures_raw):
            raw_parse_error = "malformed_measures_item"
        else:
            measures_list = measures_raw

    groups_raw = module.get("groups")
    denoms_raw = module.get("denoms")

    selected = _sanitize_json_value(ap.select_age_measures(measures_list))
    excluded = _sanitize_json_value([m for m, _ in ap.select_excluded_age_measures(measures_list)])

    env: dict[str, Any] = {
        "schema_version": RAW_SCHEMA_VERSION,
        "nct_id": _nct_of(study),
        "enrollment": enr_clean,
        "min_age": min_age,
        "max_age": max_age,
        "std_ages": std_ages,
        "baseline_groups": _sanitize_json_value(groups_raw if isinstance(groups_raw, list) else []),
        "baseline_denoms": _sanitize_json_value(denoms_raw if isinstance(denoms_raw, list) else []),
        "population_description": _sanitize_json_value(module.get("populationDescription")),
        "measures": selected,
        "excluded_measures": excluded,
    }
    if raw_parse_error is not None:
        env["raw_parse_error"] = raw_parse_error
    return env


def _finish_age_row(
    parsed: ap.ParsedAgeTrial,
    *,
    nct_id: Optional[str],
    enrollment: Any,
    min_age: Optional[str],
    max_age: Optional[str],
    std_ages: Sequence[str],
    snapshot_date: str,
    refetched: bool,
    error_detail: Optional[str] = None,
) -> dict[str, Any]:
    status = ap.classify_age_reporting(parsed)
    outcomes = ap.derive_age_outcomes(parsed, status)
    row: dict[str, Any] = {
        "nct_id": nct_id,
        **parsed.to_dict(),
        "age_report_status": status,
        **outcomes,
    }

    enr = _num(enrollment)
    tot = parsed.n_total_parsed
    row["enrollment"] = enr
    # Stored audit gap ONLY when a categorical participant count exists.
    # NEVER added to n_unknown; NEVER an input to age_report_status.
    if (
        parsed.parse_ok
        and parsed.raw_present
        and parsed.is_participant_count is True
        and enr is not None
        and tot is not None
    ):
        row["enrollment_minus_parsed"] = enr - tot
    else:
        row["enrollment_minus_parsed"] = None

    row["min_age"] = min_age
    row["max_age"] = max_age
    row["std_ages"] = list(std_ages)
    row["refetched"] = bool(refetched)
    row["error_detail"] = error_detail
    row["snapshot_date"] = snapshot_date
    row["parser_rules_version"] = ap.PARSER_RULES_VERSION
    row["vocabulary_sha256"] = ap.VOCABULARY_SHA256
    row["parser_module_version"] = ap.__version__
    return ordered_age_row(row)


def parse_error_row(
    nct_id: Optional[str],
    enrollment: Any = None,
    snapshot_date: Optional[str] = None,
    *,
    min_age: Optional[str] = None,
    max_age: Optional[str] = None,
    std_ages: Optional[Sequence[str]] = None,
    refetched: bool = False,
    error_detail: Optional[str] = None,
) -> dict[str, Any]:
    """Construct a schema-complete parse_error row with None boolean outcomes."""
    snap = snapshot_date or today_utc()
    p = ap.ParsedAgeTrial(raw_present=True, parse_ok=False)
    return _finish_age_row(
        p,
        nct_id=nct_id,
        enrollment=enrollment,
        min_age=min_age,
        max_age=max_age,
        std_ages=list(std_ages) if std_ages is not None else [],
        snapshot_date=snap,
        refetched=refetched,
        error_detail=error_detail,
    )


def build_age_row(
    study: dict[str, Any],
    snapshot_date: Optional[str] = None,
    *,
    refetched: bool = False,
) -> dict[str, Any]:
    """
    Build a full flat age row from a ClinicalTrials.gov API v2 study dict.
    Any unexpected exception is caught and converted into a parse_error row so
    no trial is ever silently dropped.
    """
    snap = snapshot_date or today_utc()
    nct_id = _nct_of(study) if isinstance(study, dict) else None
    enrollment = _enrollment_of(study) if isinstance(study, dict) else None
    min_age, max_age, std_ages = _eligibility_fields_of(study) if isinstance(study, dict) else (None, None, [])

    if not isinstance(study, dict):
        return parse_error_row(
            nct_id,
            enrollment,
            snap,
            min_age=min_age,
            max_age=max_age,
            std_ages=std_ages,
            refetched=refetched,
            error_detail="study_not_a_dict",
        )

    try:
        raw_env = select_raw_age_measures(study)
        return build_age_row_from_raw(raw_env, snap, refetched=refetched)
    except Exception as exc:  # noqa: BLE001 - intentional trial-preserving safety net
        return parse_error_row(
            nct_id,
            enrollment,
            snap,
            min_age=min_age,
            max_age=max_age,
            std_ages=std_ages,
            refetched=refetched,
            error_detail=f"{type(exc).__name__}: {exc}",
        )


def build_age_row_from_raw(
    raw_env: dict[str, Any],
    snapshot_date: Optional[str] = None,
    *,
    refetched: bool = False,
) -> dict[str, Any]:
    """
    Rebuild the exact per-trial age row from a retained raw envelope
    (select_raw_age_measures output). Raises ValueError on an incompatible
    schema_version; converts unexpected measure-parsing failures to parse_error.
    """
    if not isinstance(raw_env, dict):
        return parse_error_row(
            None,
            None,
            snapshot_date or today_utc(),
            refetched=refetched,
            error_detail="raw_env_not_a_dict",
        )

    sv = raw_env.get("schema_version")
    if sv is not None and sv != RAW_SCHEMA_VERSION:
        raise ValueError(f"unsupported raw age schema_version: {sv!r} (expected {RAW_SCHEMA_VERSION!r})")

    snap = snapshot_date or (
        str(raw_env["snapshot_date"]) if raw_env.get("snapshot_date") else today_utc()
    )
    eff_refetched = bool(refetched or raw_env.get("refetched"))
    nct_id = None if raw_env.get("nct_id") is None else str(raw_env.get("nct_id"))
    enrollment = raw_env.get("enrollment")
    min_age = None if raw_env.get("min_age") is None else str(raw_env.get("min_age"))
    max_age = None if raw_env.get("max_age") is None else str(raw_env.get("max_age"))
    std_raw = raw_env.get("std_ages")
    std_ages = [str(x) for x in std_raw] if isinstance(std_raw, list) else []

    if raw_env.get("raw_parse_error") is not None:
        return parse_error_row(
            nct_id,
            enrollment,
            snap,
            min_age=min_age,
            max_age=max_age,
            std_ages=std_ages,
            refetched=eff_refetched,
            error_detail=str(raw_env["raw_parse_error"]),
        )

    try:
        measures_raw = raw_env.get("measures")
        if measures_raw is not None and (
            not isinstance(measures_raw, list) or any(not isinstance(m, dict) for m in measures_raw)
        ):
            return parse_error_row(
                nct_id,
                enrollment,
                snap,
                min_age=min_age,
                max_age=max_age,
                std_ages=std_ages,
                refetched=eff_refetched,
                error_detail="malformed_raw_measures",
            )
        excl_raw = raw_env.get("excluded_measures")
        excl_list = excl_raw if isinstance(excl_raw, list) else None
        groups = raw_env.get("baseline_groups") if isinstance(raw_env.get("baseline_groups"), list) else None
        denoms = raw_env.get("baseline_denoms") if isinstance(raw_env.get("baseline_denoms"), list) else None

        parsed = ap.parse_trial(
            measures_raw or [],
            enrollment=enrollment,
            baseline_groups=groups,
            baseline_denoms=denoms,
            excluded_measures=excl_list,
            preselected=False,
        )
        return _finish_age_row(
            parsed,
            nct_id=nct_id,
            enrollment=enrollment,
            min_age=min_age,
            max_age=max_age,
            std_ages=std_ages,
            snapshot_date=snap,
            refetched=eff_refetched,
        )
    except Exception as exc:  # noqa: BLE001 - intentional trial-preserving safety net
        return parse_error_row(
            nct_id,
            enrollment,
            snap,
            min_age=min_age,
            max_age=max_age,
            std_ages=std_ages,
            refetched=eff_refetched,
            error_detail=f"{type(exc).__name__}: {exc}",
        )


def ordered_age_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return row in canonical published column order (missing keys default to None)."""
    return {c: row.get(c) for c in COLUMNS}


def lean_age_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return compact per-study subset for published dataset parts."""
    return {c: row.get(c) for c in LEAN_COLUMNS}


# ---------------------------------------------------------------------------
# JSONL & CSV round-trip helpers
# ---------------------------------------------------------------------------


def _open_text(path: Union[str, Path], mode: str) -> TextIO:
    p = str(path)
    if p.endswith(".gz"):
        return gzip.open(p, mode, newline="", encoding="utf-8")  # type: ignore[return-value]
    return open(p, mode, newline="", encoding="utf-8")


def write_age_jsonl(rows: Iterable[dict[str, Any]], path: Union[str, Path]) -> int:
    """Write ordered age rows to UTF-8 JSONL (or .jsonl.gz). Returns row count."""
    count = 0
    with _open_text(path, "wt") as fh:
        for r in rows:
            fh.write(
                json.dumps(ordered_age_row(r), separators=(",", ":"), ensure_ascii=False, allow_nan=False)
                + "\n"
            )
            count += 1
    return count


def read_age_jsonl(path: Union[str, Path]) -> list[dict[str, Any]]:
    """Read age rows from UTF-8 JSONL (or .jsonl.gz) in canonical column order."""
    out: list[dict[str, Any]] = []
    with _open_text(path, "rt") as fh:
        for line in fh:
            s = line.strip()
            if s:
                out.append(ordered_age_row(json.loads(s)))
    return out


def write_raw_age_jsonl(envelopes: Iterable[dict[str, Any]], path: Union[str, Path]) -> int:
    """Write raw age envelopes (select_raw_age_measures output) to JSONL / .jsonl.gz."""
    count = 0
    with _open_text(path, "wt") as fh:
        for env in envelopes:
            fh.write(
                json.dumps(
                    _sanitize_json_value(env),
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            )
            count += 1
    return count


def read_raw_age_jsonl(path: Union[str, Path]) -> list[dict[str, Any]]:
    """Read raw age envelopes from JSONL / .jsonl.gz."""
    out: list[dict[str, Any]] = []
    with _open_text(path, "rt") as fh:
        for line in fh:
            s = line.strip()
            if s:
                out.append(json.loads(s))
    return out


_CSV_STR_ESCAPE: str = "\x1f"


def _to_csv_cell(col: str, val: Any) -> str:
    if val is None:
        return ""
    if col in JSON_COLUMNS:
        return json.dumps(val, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, str):
        if val == "":
            return _CSV_STR_ESCAPE
        if val.startswith(_CSV_STR_ESCAPE):
            return _CSV_STR_ESCAPE + val
        return val
    return str(val)


def _from_csv_cell(col: str, val: Optional[str]) -> Any:
    if col in JSON_COLUMNS:
        if not val:
            return []
        return json.loads(val)
    if val is None or val == "":
        return None
    if col in _BOOL_COLUMNS:
        return val.strip().lower() == "true"
    if col in _FLOAT_COLUMNS:
        return float(val)
    if col in _INT_COLUMNS:
        return int(float(val))
    if val == _CSV_STR_ESCAPE:
        return ""
    if val.startswith(_CSV_STR_ESCAPE):
        return val[1:]
    return val


def write_age_csv(rows: Iterable[dict[str, Any]], path: Union[str, Path]) -> int:
    """Write full age rows to CSV (or .csv.gz) with JSON-encoded array fields."""
    count = 0
    with _open_text(path, "wt") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for r in rows:
            ord_r = ordered_age_row(r)
            writer.writerow({c: _to_csv_cell(c, ord_r[c]) for c in COLUMNS})
            count += 1
    return count


def read_age_csv(path: Union[str, Path]) -> list[dict[str, Any]]:
    """Read CSV (or .csv.gz) written by write_age_csv back into typed Python dicts."""
    out: list[dict[str, Any]] = []
    with _open_text(path, "rt") as fh:
        reader = csv.DictReader(fh)
        for raw_row in reader:
            typed = {k: _from_csv_cell(k, v) for k, v in raw_row.items()}
            out.append(ordered_age_row(typed))
    return out


# ---------------------------------------------------------------------------
# Cohort status counts & structural validation checks
# ---------------------------------------------------------------------------


def status_counts(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Count rows across all seven non-collapsing age_report_status states and outcomes."""
    row_list = list(rows)
    uninf: dict[str, int] = {}
    for r in row_list:
        if r.get("age_report_status") == "uninformative":
            reason = str(r.get("uninformative_reason") or "other")
            uninf[reason] = uninf.get(reason, 0) + 1
    return {
        "n_rows": len(row_list),
        "status": {s: sum(1 for r in row_list if r.get("age_report_status") == s) for s in STATES},
        "outcomes": {o: sum(1 for r in row_list if r.get(o) is True) for o in OUTCOMES},
        "uninformative_reason": uninf,
    }


def structural_checks(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Verify non-collapsing status invariants, unit guards, and provenance stamps on rows."""
    row_list = list(rows)
    n = len(row_list)
    sc = status_counts(row_list)
    checks: dict[str, dict[str, Any]] = {}

    checks["status_counts_sum_to_rows"] = {
        "pass": sum(sc["status"].values()) == n,
        "detail": f"{sum(sc['status'].values())} == {n}",
    }
    bad_status = [r.get("nct_id") for r in row_list if r.get("age_report_status") not in STATES]
    checks["every_row_has_valid_status"] = {
        "pass": not bad_status,
        "detail": f"{len(bad_status)} invalid status rows",
    }
    pe_bad = [
        r.get("nct_id")
        for r in row_list
        if r.get("age_report_status") == "parse_error"
        and any(r.get(o) is not None for o in OUTCOMES)
    ]
    checks["parse_error_rows_have_none_outcomes"] = {
        "pass": not pe_bad,
        "detail": f"{len(pe_bad)} parse_error rows with non-None outcome",
    }
    cou_bad = [
        r.get("nct_id")
        for r in row_list
        if str(r.get("categorical_param_type") or "").upper() == "COUNT_OF_UNITS"
        and (r.get("is_participant_count") is not False or r.get("n_total_parsed") is not None)
    ]
    checks["count_of_units_never_enters_participant_counts"] = {
        "pass": not cou_bad,
        "detail": f"{len(cou_bad)} COUNT_OF_UNITS rows leaked into participant counts",
    }
    euo_bad = [
        r.get("nct_id")
        for r in row_list
        if r.get("age_report_status") == "explicit_unknown_only"
        and not ((r.get("n_unknown") or 0.0) > 0.0 and (r.get("n_classified") or 0.0) == 0.0)
    ]
    checks["explicit_unknown_only_rows_are_unknown_only"] = {
        "pass": not euo_bad,
        "detail": f"{len(euo_bad)} rows",
    }
    nr_bad = [
        r.get("nct_id")
        for r in row_list
        if r.get("age_report_status") == "not_reported" and r.get("raw_present")
    ]
    checks["not_reported_rows_have_no_selected_measure"] = {
        "pass": not nr_bad,
        "detail": f"{len(nr_bad)} rows",
    }
    ver_bad = [
        r.get("nct_id")
        for r in row_list
        if r.get("parser_rules_version") != ap.PARSER_RULES_VERSION
        or r.get("vocabulary_sha256") != ap.VOCABULARY_SHA256
    ]
    checks["provenance_stamps_match_parser"] = {
        "pass": not ver_bad,
        "detail": f"{len(ver_bad)} rows with mismatched provenance",
    }
    return checks


def all_pass(checks: dict[str, dict[str, Any]]) -> bool:
    """True when every check in structural_checks(rows) passed."""
    return all(bool(v.get("pass")) for v in checks.values())
