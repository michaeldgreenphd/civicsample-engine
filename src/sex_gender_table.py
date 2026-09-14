"""
sex_gender_table.py — the dashboard's sex/gender row, built from the vendored parser.

READS   a raw ClinicalTrials.gov API v2 study record (in src/extract_all.py), or a
        retained raw-measure record (scripts/build_sex_gender_table.py --from-raw)
WRITES  nothing itself. It returns the flat row that extract_all stores under the
        study key "sex_gender" and that scripts/build_sex_gender_table.py tabulates.
INVOKED by src/extract_all.py (weekly), scripts/build_sex_gender_table.py,
        scripts/sex_gender_audit.py, scripts/sex_gender_side_by_side.py.

Every count comes from src/sex_gender_parser.py, vendored unchanged from the
manuscript bundle. This module adds provenance and three derived quantities:

  enrollment_minus_parsed   registered enrollment minus n_total_parsed. It is the
                            gap between what the sponsor registered and what the
                            posted table accounts for. It is stored so it can be
                            audited, it is NEVER added to n_unknown, and it is
                            NEVER an input to sex_report_status. The legacy
                            extractors folded this gap into "unknown"
                            ("denominator balancing"); this table has no such
                            code path, and tests/sex_gender/test_table.py pins
                            that.
  percent_female            100 * n_female / (n_female + n_male) for rows with
                            reported_sex AND is_participant_count AND
                            n_female + n_male > 0; None otherwise. gender_diverse
                            and ambiguous never enter the denominator (README D5).
  refetched                 the record was re-fetched individually because the
                            paginated search endpoint had stripped its
                            measurement arrays (see needs_refetch).

Nothing here re-derives missingness: the five-state sex_report_status from the
parser is the only source of "not reported".
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from src import sex_gender_parser as sgp

STATES: tuple = tuple(sgp.REPORT_STATUS_LEVELS)
VISIBLE_STATES: tuple = ("reported", "explicit_unknown_only", "uninformative", "not_reported")
OUTCOMES: tuple = ("reported_sex", "reported_gender", "reported_both", "reported_any",
                   "gender_labeled_binary_only")

# Column order of the published table: identifiers, the parser's own columns
# in README "Output columns" order, then the provenance and derived columns.
PARSER_COLUMNS: list = [
    "raw_present", "parse_ok", "n_measures", "has_sex_table", "has_gender_table",
    "measure_title", "measure_type", "layout", "param_type", "unit_of_measure", "is_participant_count",
    "n_female", "n_male", "n_unknown", "n_gender_diverse", "n_ambiguous_gender", "n_total_parsed",
    "sex_report_status", "reported_sex", "reported_gender", "reported_both", "reported_any",
    "gender_labeled_binary_only",
    "uninformative_reason", "declared_not_collected",
    "n_classes", "has_denoms", "raw_has_unknown",
    "flag_total_by_position", "flag_no_total_sumcheck", "flag_multiclass_timepoint",
    "flag_customized_layout", "flag_empty_category", "flag_unmapped_layout",
    "flag_exceeds_enrollment", "flag_nonparticipant_units",
    "unknown_labels", "gender_diverse_labels", "ambiguous_labels", "unmapped_labels",
]
EXTRA_COLUMNS: list = [
    "enrollment", "enrollment_minus_parsed", "percent_female", "refetched",
    "snapshot_date", "parser_rules_version", "parser_module_version",
]
COLUMNS: list = ["nct_id"] + PARSER_COLUMNS + EXTRA_COLUMNS

# The row stored inside each study record in the published parts: only what
# the UI needs per trial. Labels, flags, measure title, layout, percent_female
# and the rest live only in sex_gender_parsed.csv.gz, the full record. The
# parts sit close to the 20 MiB CDN ceiling; this keeps them there.
LEAN_COLUMNS: list = [
    "sex_report_status", "reported_sex", "reported_gender", "reported_both",
    "n_female", "n_male", "n_unknown", "n_gender_diverse", "n_ambiguous_gender",
    "is_participant_count", "uninformative_reason", "declared_not_collected",
    "enrollment_minus_parsed", "parser_rules_version",
]

_BOOL_COLUMNS = {"raw_present", "parse_ok", "has_sex_table", "has_gender_table", "is_participant_count",
                 "reported_sex", "reported_gender", "reported_both", "reported_any", "gender_labeled_binary_only",
                 "declared_not_collected", "has_denoms", "raw_has_unknown", "refetched",
                 "flag_total_by_position", "flag_no_total_sumcheck", "flag_multiclass_timepoint",
                 "flag_customized_layout", "flag_empty_category", "flag_unmapped_layout",
                 "flag_exceeds_enrollment", "flag_nonparticipant_units"}
_FLOAT_COLUMNS = {"n_female", "n_male", "n_unknown", "n_gender_diverse", "n_ambiguous_gender", "n_total_parsed",
                  "enrollment", "enrollment_minus_parsed", "percent_female"}
_INT_COLUMNS = {"n_measures", "n_classes"}


def lean_row(row: dict) -> dict:
    """The per-study subset of a full row for the published parts."""
    return {c: row.get(c) for c in LEAN_COLUMNS}


def _from_csv(col: str, v: str):
    if v == "" or v is None:
        return None
    if col in _BOOL_COLUMNS:
        return v.lower() == "true"
    if col in _FLOAT_COLUMNS:
        return float(v)
    if col in _INT_COLUMNS:
        return int(float(v))
    return v


def read_table(path: str) -> list:
    """Read sex_gender_parsed.csv.gz back into typed rows (None for empty cells,
    bools and numbers restored). The CSV is the full record; the parts carry
    only lean_row()."""
    import csv
    import gzip
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", newline="", encoding="utf-8") as fh:
        return [{k: _from_csv(k, v) for k, v in r.items()} for r in csv.DictReader(fh)]


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _enrollment_of(study: dict) -> Any:
    return (((study.get("protocolSection") or {}).get("designModule") or {})
            .get("enrollmentInfo") or {}).get("count")


def _nct_of(study: dict) -> Optional[str]:
    return ((study.get("protocolSection") or {}).get("identificationModule") or {}).get("nctId")


def select_raw_measures(study: dict) -> dict:
    """The record retained on the weekly backup so any later rule bump can re-parse
    without a registry pull: the SELECTED sex/gender measures (the output of
    select_sex_gender_measures(), verbatim), the enrollment count and the NCT id.
    Trials with no selected measure are retained too (measures == []) so a
    re-parse reproduces not_reported as well."""
    measures = ((study.get("resultsSection") or {}).get("baselineCharacteristicsModule") or {}).get("measures")
    return {
        "nct_id": _nct_of(study),
        "enrollment": _enrollment_of(study),
        "measures": sgp.select_sex_gender_measures(measures),
    }


def raw_has_measurements(measures: Optional[Iterable[dict]]) -> bool:
    """True if any selected measure carries at least one measurement object."""
    for m in measures or []:
        for cl in (m.get("classes") or []):
            if not isinstance(cl, dict):
                continue
            for cat in (cl.get("categories") or []):
                if isinstance(cat, dict) and cat.get("measurements"):
                    return True
    return False


def needs_refetch(row: dict, raw: dict) -> bool:
    """The paginated /studies endpoint sometimes returns class and category titles
    with the measurement arrays stripped. The parser cannot tell that apart from a
    sponsor who posted an empty template (a real registry state, e.g. NCT00205868),
    so: a row that parsed as uninformative / no_measurements whose selected
    measures carry no measurement object at all is re-fetched individually. If the
    individual record is still empty, the row stays uninformative and is marked
    refetched so the audit can list it; it is never silently promoted or dropped."""
    return (row.get("uninformative_reason") == "no_measurements"
            and not raw_has_measurements(raw.get("measures")))


def percent_female(row: dict) -> Optional[float]:
    """README D5, series (a) numerator per trial. None unless reported_sex AND
    is_participant_count AND n_female + n_male > 0. Single-sex trials are included
    (0 or 100); gender_diverse and ambiguous never enter the denominator."""
    if not row.get("reported_sex") or not row.get("is_participant_count"):
        return None
    f = row.get("n_female") or 0.0
    m = row.get("n_male") or 0.0
    if f + m <= 0:
        return None
    return 100.0 * f / (f + m)


def finish_row(row: dict, enrollment: Any, snapshot_date: str, refetched: bool = False) -> dict:
    """Add provenance and the derived columns to a parser row. Pure."""
    enr = _num(enrollment)
    tot = row.get("n_total_parsed")
    row["enrollment"] = enr
    # The gap between registered enrollment and the parsed table. Stored, never
    # shown as unknown, never a status input. None when either side is absent
    # (no selected measure, or no enrollment count).
    row["enrollment_minus_parsed"] = (enr - tot) if (row.get("raw_present") and enr is not None and tot is not None) else None
    row["percent_female"] = percent_female(row)
    row["refetched"] = bool(refetched)
    row["snapshot_date"] = snapshot_date
    row["parser_rules_version"] = sgp.PARSER_RULES_VERSION
    row["parser_module_version"] = sgp.__version__
    return row


def build_row(study: dict, snapshot_date: str, refetched: bool = False) -> dict:
    """Row for one raw API v2 study record."""
    row = sgp.parse_study_record(study)
    return finish_row(row, _enrollment_of(study), snapshot_date, refetched)


def build_row_from_raw(raw: dict, snapshot_date: str, refetched: bool = False) -> dict:
    """Row for one retained raw-measure record (select_raw_measures output).
    This is the re-parse path: identical output to build_row on the same trial."""
    row = sgp.parse_measures_row(raw.get("measures") or [], raw.get("enrollment"),
                                 nct_id=raw.get("nct_id"), preselected=True)
    return finish_row(row, raw.get("enrollment"), snapshot_date, refetched)


def parse_error_row(nct_id: Optional[str], enrollment: Any, snapshot_date: str) -> dict:
    """A row for input that could not be read at all: status parse_error, outcomes None."""
    p = sgp.ParsedTrial(raw_present=True, parse_ok=False)
    status = sgp.classify_reporting(p)
    row = {"nct_id": nct_id, **p.to_dict(), "sex_report_status": status,
           **sgp.derive_outcomes(p, status), "parser_rules_version": sgp.PARSER_RULES_VERSION}
    return finish_row(row, enrollment, snapshot_date)


def ordered(row: dict) -> dict:
    """The row in published column order (missing keys become None)."""
    return {c: row.get(c) for c in COLUMNS}


# ---------------------------------------------------------------------------
# Aggregates and the structural validation targets (README "Validation targets")
# ---------------------------------------------------------------------------

def status_counts(rows: Iterable[dict]) -> dict:
    rows = list(rows)
    out: dict = {"n_rows": len(rows)}
    out["status"] = {s: sum(1 for r in rows if r.get("sex_report_status") == s) for s in STATES}
    out["outcomes"] = {o: sum(1 for r in rows if r.get(o) is True) for o in OUTCOMES}
    out["count_of_units_primary"] = sum(1 for r in rows if (r.get("param_type") or "").upper() == "COUNT_OF_UNITS")
    out["is_participant_count_false"] = sum(1 for r in rows if r.get("is_participant_count") is False)
    out["declared_not_collected"] = sum(1 for r in rows if r.get("declared_not_collected") is True)
    out["refetched"] = sum(1 for r in rows if r.get("refetched") is True)
    out["uninformative_reason"] = {}
    for r in rows:
        if r.get("sex_report_status") == "uninformative":
            k = r.get("uninformative_reason") or "other"
            out["uninformative_reason"][k] = out["uninformative_reason"].get(k, 0) + 1
    return out


def _module_is_participant_count(param_type: Any, unit: Any) -> Optional[bool]:
    """What the vendored parser itself says is_participant_count is for this
    (paramType, unitOfMeasure). Asking the module rather than restating the rule
    keeps the check exact across parser versions (v1.0.0: COUNT_OF_UNITS only;
    later versions may add MEAN/MEDIAN/percent units, README D6)."""
    m = {"title": "Sex: Female, Male", "classes": [{"categories": [
        {"title": "Female", "measurements": [{"groupId": "BG000", "value": "1"}]}]}]}
    if param_type is not None:
        m["paramType"] = param_type
    if unit is not None:
        m["unitOfMeasure"] = unit
    return sgp.parse_trial([m]).is_participant_count


def structural_checks(rows: Iterable[dict]) -> dict:
    """The structural targets that must hold on ANY snapshot. Returns
    {check_name: {"pass": bool, "detail": str}}. Nothing here is a count rule;
    every check reads what the parser produced."""
    rows = list(rows)
    n = len(rows)
    checks: dict = {}

    def put(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = {"pass": bool(ok), "detail": detail}

    sc = status_counts(rows)
    o = sc["outcomes"]
    put("reported_any_ge_reported_sex", o["reported_any"] >= o["reported_sex"],
        f"{o['reported_any']} >= {o['reported_sex']}")
    put("reported_any_ge_reported_gender", o["reported_any"] >= o["reported_gender"],
        f"{o['reported_any']} >= {o['reported_gender']}")
    put("reported_both_le_min", o["reported_both"] <= min(o["reported_sex"], o["reported_gender"]),
        f"{o['reported_both']} <= min({o['reported_sex']}, {o['reported_gender']})")
    put("status_counts_sum_to_rows", sum(sc["status"].values()) == n,
        f"{sum(sc['status'].values())} == {n}")
    bad_status = [r.get("nct_id") for r in rows if r.get("sex_report_status") not in STATES]
    put("every_row_has_one_status", not bad_status, f"{len(bad_status)} rows with a status outside {STATES}")

    mism = []
    for r in rows:
        if not r.get("raw_present") or r.get("parse_ok") is False:
            continue
        want = _module_is_participant_count(r.get("param_type"), r.get("unit_of_measure"))
        if bool(r.get("is_participant_count")) != bool(want):
            mism.append(r.get("nct_id"))
    put("is_participant_count_matches_parser_rule", not mism,
        f"{len(mism)} rows disagree with the parser's own rule (first: {mism[:5]})")
    cou = [r.get("nct_id") for r in rows
           if (r.get("param_type") or "").upper() == "COUNT_OF_UNITS" and r.get("is_participant_count") is not False]
    put("count_of_units_never_participant_count", not cou, f"{len(cou)} COUNT_OF_UNITS rows with is_participant_count True")

    pe = [r.get("nct_id") for r in rows if r.get("sex_report_status") == "parse_error"
          and any(r.get(k) is not None for k in OUTCOMES)]
    put("parse_error_rows_have_none_outcomes", not pe, f"{len(pe)} parse_error rows with a non-None outcome")

    euo = [r.get("nct_id") for r in rows if r.get("sex_report_status") == "explicit_unknown_only"
           and not ((r.get("n_unknown") or 0) > 0
                    and ((r.get("n_female") or 0) + (r.get("n_male") or 0)
                         + (r.get("n_gender_diverse") or 0) + (r.get("n_ambiguous_gender") or 0)) == 0)]
    put("explicit_unknown_only_rows_are_unknown_only", not euo, f"{len(euo)} rows")

    nr = [r.get("nct_id") for r in rows if r.get("sex_report_status") == "not_reported" and r.get("raw_present")]
    put("not_reported_rows_have_no_measure", not nr, f"{len(nr)} not_reported rows with a selected measure")

    pf = [r.get("nct_id") for r in rows if r.get("percent_female") is not None
          and not (r.get("reported_sex") and r.get("is_participant_count"))]
    put("percent_female_only_in_denominator_set", not pf, f"{len(pf)} rows")

    rv = {r.get("parser_rules_version") for r in rows}
    put("single_parser_rules_version", rv == {sgp.PARSER_RULES_VERSION}, f"versions seen: {sorted(map(str, rv))}")
    return checks


def all_pass(checks: dict) -> bool:
    return all(c["pass"] for c in checks.values())


BASELINE_SNAPSHOT_DATE = "2026-06-09"


def is_baseline_data(meta: dict) -> bool:
    """True when a table was parsed from the manuscript's 2026-06-09 extract (the
    only input the snapshot regression may FAIL on; every other pull only reports)."""
    for k in ("snapshot_date", "source_snapshot_date", "extracted_at"):
        v = meta.get(k)
        if isinstance(v, str) and v.startswith(BASELINE_SNAPSHOT_DATE):
            return True
    return False


def baseline_drift(rows: Iterable[dict], baseline: dict) -> list:
    """Compare a table with fixtures/snapshot_baseline_2026-06-09.json.

    The baseline counts the 66,210 trials that HAD a sex/gender-titled measure,
    so rows are restricted to raw_present before comparing. Returns one line
    per quantity that differs (empty list == exact match). Callers decide
    whether a difference fails (baseline data) or is reported (fresh pull)."""
    rows = [r for r in rows if r.get("raw_present")]
    sc = status_counts(rows)
    got = {
        "trials_with_measure": len(rows),
        **{f"status.{k}": sc["status"].get(k, 0) for k in baseline.get("status_counts", {})},
        "reported_sex_true": sc["outcomes"]["reported_sex"],
        "reported_gender_true": sc["outcomes"]["reported_gender"],
        "reported_both_true": sc["outcomes"]["reported_both"],
        "gender_labeled_binary_only_true": sc["outcomes"]["gender_labeled_binary_only"],
    }
    want = {
        "trials_with_measure": sum(baseline.get("status_counts", {}).values()),
        **{f"status.{k}": v for k, v in baseline.get("status_counts", {}).items()},
        "reported_sex_true": baseline.get("reported_sex_true"),
        "reported_gender_true": baseline.get("reported_gender_true"),
        "reported_both_true": baseline.get("reported_both_true"),
        "gender_labeled_binary_only_true": baseline.get("gender_labeled_binary_only_true"),
    }
    return [f"{k}: got {got[k]}, baseline {want[k]} ({got[k] - want[k]:+d})"
            for k in want if want[k] is not None and got[k] != want[k]]


def percent_female_series(rows: Iterable[dict]) -> dict:
    """Both README D5 series over the same denominator set, keyed by results-posted
    year (the caller puts the year on each row as "year"). Returns per year:
    pf_sum, pf_count (series a: mean of within-trial shares), f_sum, fm_sum
    (series b: participant-weighted)."""
    out: dict = {}
    for r in rows:
        pf = r.get("percent_female")
        if pf is None:
            continue
        y = r.get("year")
        if not y:
            continue
        d = out.setdefault(y, {"pf_sum": 0.0, "pf_count": 0, "f_sum": 0.0, "fm_sum": 0.0})
        d["pf_sum"] += pf
        d["pf_count"] += 1
        d["f_sum"] += r.get("n_female") or 0.0
        d["fm_sum"] += (r.get("n_female") or 0.0) + (r.get("n_male") or 0.0)
    return out
