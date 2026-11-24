"""Baseline race, ethnicity, and sex/gender extraction with two-denominator bounds.

Implements the ``census/`` category-structure and two-denominator discipline
across both raw ClinicalTrials.gov API v2 study documents and weekly
full-record dicts (``src/full_records.py``), without modifying
``src/sex_gender_parser.py`` or any weekly extractor.

Key analytical invariants
-------------------------
1. **Seven-state table-structure classification** (``race_table_status`` and
   ``ethnicity_table_status``):
   - ``omb_compatible``
   - ``combined_race_ethnicity_quarantined``: Hispanic/Latino (or combined
     race+ethnicity labels) appears inside the race measure. Quarantined and
     excluded from all pooled OMB race distributions, with its own explicit
     quarantine count.
   - ``single_category_ethnicity``: ethnicity measure reports only one of the
     two OMB categories (e.g. ``"Hispanic or Latino"`` with no
     ``"Not Hispanic or Latino"`` row). Denominator A stands as a valid floor;
     Denominator B (``of_classified``) is withheld as ``None``.
   - ``non_standard_categories_present``: unmapped, quarantined, extra
     non-OMB category keys, or non-exclusive double-counted rows are present.
   - ``whole_population_group_unidentified``: a raw record's baseline table
     has several groups and none the extractors recognise as the whole
     population, so no counts are read (see
     ``whole_population_group_unidentified``).
   - ``table_absent_in_posted_results``: results are posted on CT.gov, with
     no usable table for this dimension.
   - ``results_not_posted``: trial has no posted results section on CT.gov.

2. **Two denominators** (neither replaces the other):
   - **Denominator A** (``of_baseline_total``):
     ``floor = count / baseline_total``,
     ``ceiling = (count + unknown_count + unmapped_count) / baseline_total``,
     ``unknown_bounds = [floor, ceiling]``, and ``direction_settled`` is
     ``True`` iff ``floor`` and ``ceiling`` lie strictly on the same side of
     ``reference_median`` (``floor > ref or ceiling < ref``; ``None`` when no
     reference median is available, never coerced to ``False``).
   - **Denominator B** (``of_classified``):
     ``count / classified_total`` where
     ``classified_total = baseline_total - unknown_count - unmapped_count``.
     Withheld as ``None`` whenever ``single_category_ethnicity`` holds or
     ``classified_total <= 0``.

3. **Sex/gender five-state fidelity**:
   Reads ``sex_gender`` rows produced by ``src/sex_gender_table.py`` (or runs
   ``sex_gender_table.build_row`` on raw API v2 studies), strictly preserving
   all five states (``reported``, ``explicit_unknown_only``, ``uninformative``,
   ``not_reported``, ``parse_error``) and keeping ``enrollment_minus_parsed``
   strictly separate from ``n_unknown``. A table read as participant counts
   whose counts are not whole keeps its status, and its composition
   (``percent_female``, ``female_share``, ``of_parsed_total``, ``of_classified``)
   is withheld with ``composition_withheld_reason`` set to
   ``participant_counts_not_whole``.

4. **US-site race/ethnicity omission audit**:
   Explicitly tracks trials with >= 1 US facility (``us_only`` or
   ``multinational_with_us_sites``) that posted results without a race table
   or without an ethnicity table; out-of-scope trials carry ``None`` rather
   than ``False``.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from dap.geography import extract_geography
from src import ethnicity_extractor, race_extractor
from src import utils as src_utils
from src import sex_gender_parser as sgp
from src import sex_gender_table as sgt

TABLE_STATUSES: tuple[str, ...] = (
    "omb_compatible",
    "combined_race_ethnicity_quarantined",
    "single_category_ethnicity",
    "non_standard_categories_present",
    "whole_population_group_unidentified",
    "table_absent_in_posted_results",
    "results_not_posted",
)

RACE_CLASSIFIED_KEYS: tuple[str, ...] = (
    "american_indian_alaska_native",
    "asian",
    "black_african_american",
    "native_hawaiian_pacific_islander",
    "white",
    "more_than_one_race",
    "other",
)

ETHNICITY_CLASSIFIED_KEYS: tuple[str, ...] = (
    "hispanic_latino",
    "not_hispanic_latino",
)

SEX_GENDER_STATES: tuple[str, ...] = tuple(str(s) for s in sgt.STATES)

_ETHNICITY_IN_RACE_RX: re.Pattern[str] = re.compile(
    r"hispanic|latin(?:o|a|x|e)?\b|spanish\s+origin",
    re.IGNORECASE,
)

# Unknown-like unmapped labels in ethnicity_extractor map to
# unknown_not_reported with flag "unmapped"; genuine non-standard ethnicity
# labels carry substantive words outside unknown/missing tokens.
_ETHNICITY_UNKNOWN_TEXT_RX: re.Pattern[str] = re.compile(
    r"^(?:unknown|unk|missing|unspecified|not\s+reported|not\s+specified|"
    r"declined|prefer\s+not.*|n/?a|none\s+reported)$",
    re.IGNORECASE,
)


def is_results_posted(record: dict[str, Any]) -> bool:
    """Determine whether a study record carries posted ClinicalTrials.gov results."""
    if "results_posted" in record and isinstance(record["results_posted"], bool):
        return record["results_posted"]
    if "hasResults" in record:
        return bool(record.get("hasResults")) or isinstance(record.get("resultsSection"), dict)
    if "resultsSection" in record:
        return isinstance(record.get("resultsSection"), dict) and bool(record.get("resultsSection"))
    if "protocolSection" in record:
        # Raw CT.gov API v2 studies without results omit `resultsSection`.
        return False
    if record.get("results_date"):
        return True
    for key in ("race", "ethnicity", "sex", "gender"):
        block = record.get(key)
        if isinstance(block, dict) and block.get("reported"):
            return True
    sg = record.get("sex_gender")
    if isinstance(sg, dict) and sg.get("sex_report_status") not in (None, "not_reported"):
        return True
    if "results_date" in record:
        return False
    return any(isinstance(record.get(k), dict) for k in ("race", "ethnicity", "sex", "sex_gender"))


def _is_weekly_record(record: dict[str, Any]) -> bool:
    """Whether a record is a weekly full record, whose `race`, `ethnicity`, `sex` and `sex_gender`
    blocks are the extractors' projection of its baseline table, rather than a raw CT.gov API v2 study.

    A raw study is always read from its own sections. Its top-level blocks are not the extractors': the
    raw loader allows extra fields, so a stale `race: {"reported": false}` beside a posted race table had
    published the table as absent, and the contract passed. No stored raw record carries any such block.
    """
    return "protocolSection" not in record and "resultsSection" not in record


def _reads_as_baseline_count(value: Any) -> bool:
    """Whether ``src.utils.get_total_baseline_participants`` reads ``value`` as a count:
    ``int(float(str(value).replace(",", "")))`` succeeds and the value is not None."""
    if value is None:
        return False
    try:
        int(float(str(value).replace(",", "")))
    except (ValueError, TypeError, OverflowError):
        return False
    return True


def _study_without_protocol_enrollment_padding(study: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow view of a raw CT.gov study that prevents protocol enrollment
    from padding baseline ``unknown_not_reported`` when ``baselineCharacteristicsModule``
    posts no ``Participants`` denominator of its own.

    Analytical assumption: Denominator A is the baseline table's own participant
    population (from ``baselineCharacteristicsModule.denoms`` or the table's rows),
    never registered protocol ``enrollmentInfo.count`` (which can exceed the
    analyzed baseline cohort due to screen/randomization drop-off).

    Enrollment is kept only when ``src.utils.get_total_baseline_participants`` is sure to return a
    baseline count before its enrollment fallback: some ``Participants`` denominator whose last count
    parses (it reads the overall group's count, else the last one, denominator by denominator). A
    denominator present with counts that do not parse (``"NA"``) had been taken as usable, so the
    race and ethnicity tables were balanced to registered enrollment.
    """
    baseline = (study.get("resultsSection") or {}).get("baselineCharacteristicsModule") or {}
    has_baseline_participants_denom = any(
        isinstance(d, dict)
        and str(d.get("units") or "").lower() == "participants"
        and isinstance(d.get("counts"), list)
        and bool(d["counts"])
        and isinstance(d["counts"][-1], dict)
        and _reads_as_baseline_count(d["counts"][-1].get("value"))
        for d in (baseline.get("denoms") or [])
    )
    if has_baseline_participants_denom:
        return study
    protocol = study.get("protocolSection")
    if not isinstance(protocol, dict):
        return study
    design = protocol.get("designModule")
    if not isinstance(design, dict) or "enrollmentInfo" not in design:
        return study
    trimmed_design = {k: v for k, v in design.items() if k != "enrollmentInfo"}
    trimmed_protocol = {**protocol, "designModule": trimmed_design}
    return {**study, "protocolSection": trimmed_protocol}


def evaluate_unknown_bounds(
    count: int,
    unknown_count: int,
    unmapped_count: int,
    baseline_total: int,
    reference_median: float | None = None,
) -> dict[str, Any]:
    """Compute Denominator A floor, ceiling, ``unknown_bounds``, and ``direction_settled``.

    Matches ``census/benchmark.py``:
    - ``floor = count / baseline_total``
    - ``ceiling = (count + unknown_count + unmapped_count) / baseline_total``
    - ``unknown_bounds = [floor, ceiling]``
    - When ``reference_median`` is present, ``direction_settled`` is ``True``
      iff ``floor > reference_median`` or ``ceiling < reference_median`` (both
      bounds strictly on the same side of the reference median). When
      ``reference_median`` is ``None``, ``direction_settled`` is ``None``.
    """
    if baseline_total <= 0:
        raise ValueError(f"baseline_total must be positive, got {baseline_total}")
    if count < 0 or unknown_count < 0 or unmapped_count < 0:
        raise ValueError("category, unknown, and unmapped counts must be non-negative")
    upper_num = min(baseline_total, count + unknown_count + unmapped_count)
    floor = count / baseline_total
    ceiling = upper_num / baseline_total
    diff_floor: float | None = (floor - reference_median) if reference_median is not None else None
    diff_ceiling: float | None = (ceiling - reference_median) if reference_median is not None else None
    direction_settled: bool | None = (
        bool(floor > reference_median or ceiling < reference_median)
        if reference_median is not None
        else None
    )
    return {
        "count": count,
        "baseline_total": baseline_total,
        "floor": floor,
        "ceiling": ceiling,
        "unknown_bounds": [floor, ceiling],
        "reference_median": reference_median,
        "diff_at_floor": diff_floor,
        "diff_at_ceiling": diff_ceiling,
        "direction_settled": direction_settled,
    }


def extract_sex_gender(
    record: dict[str, Any],
    snapshot_date: str = "1970-01-01",
) -> dict[str, Any]:
    """Extract or validate a ``sex_gender_table`` row, preserving all 5 states.

    Never modifies ``src/sex_gender_parser.py`` and never folds
    ``enrollment_minus_parsed`` into ``n_unknown`` or any denominator.
    """
    existing = record.get("sex_gender") if _is_weekly_record(record) else None
    if isinstance(existing, dict) and existing.get("sex_report_status") in SEX_GENDER_STATES:
        row = copy.deepcopy(existing)
    else:
        nct_id = (
            record.get("nct_id")
            or (((record.get("protocolSection") or {}).get("identificationModule") or {}).get("nctId"))
        )
        enr = (
            record.get("enrollment")
            if "protocolSection" not in record
            else (
                (((record.get("protocolSection") or {}).get("designModule") or {})
                 .get("enrollmentInfo") or {})
                .get("count")
            )
        )
        try:
            row = sgt.build_row(record, snapshot_date=snapshot_date)
        except Exception:  # noqa: BLE001 - matches src/extract_all.py parse_error handling
            row = sgt.parse_error_row(nct_id, enr, snapshot_date)

    status = str(row.get("sex_report_status"))
    if status not in SEX_GENDER_STATES:
        raise ValueError(f"sex_report_status {status!r} outside {SEX_GENDER_STATES}")

    n_female = row.get("n_female")
    n_male = row.get("n_male")
    n_unknown = row.get("n_unknown")
    n_gender_diverse = row.get("n_gender_diverse")
    n_ambiguous_gender = row.get("n_ambiguous_gender")
    enrollment_minus_parsed = row.get("enrollment_minus_parsed")
    is_participant_count = row.get("is_participant_count")
    reported_sex = row.get("reported_sex")
    reported_gender = row.get("reported_gender")
    reported_both = row.get("reported_both")

    # A table the parser reads as participant counts holds whole people. NCT01507090's weekly row reads
    # Female 51.7 and Male 48.3 beside an enrollment of 642: percentages entered as counts. It published
    # percent_female 51.7 and female_share 0.517 beside bounds and a Denominator B counted from the
    # rounded 52 and 48, and entered the participant-weighted pooling as 100 people. Such a row keeps its
    # reporting status and counts as posted, and its composition is withheld with a reason, as for a
    # non-participant table; refusing it would refuse the whole weekly input. It is 1 of the 80,755
    # participant-count rows of the 2026-11-08 weekly extraction. Negative and non-finite counts are
    # refused at load (`benchmarks._sex_gender_count_problems`, `benchmarks.weekly_record_problem`).
    counts_not_whole = is_participant_count is True and any(
        isinstance(v, (int, float)) and not isinstance(v, bool) and not float(v).is_integer()
        for v in (n_female, n_male, n_unknown, n_gender_diverse, n_ambiguous_gender)
    )
    composition_withheld_reason = "participant_counts_not_whole" if counts_not_whole else None

    pf = None if counts_not_whole else sgt.percent_female(row)
    female_share: float | None = (pf / 100.0) if pf is not None else None

    # Parsed sex table Denominator A & B using ONLY parsed buckets (never
    # enrollment_minus_parsed). Computed only for participant-count sex tables of whole counts.
    of_parsed_total: dict[str, dict[str, Any]] | None = None
    of_classified_sex: dict[str, float] | None = None
    if reported_sex is True and is_participant_count is True and not counts_not_whole:
        f_cnt = int(round(float(n_female or 0.0)))
        m_cnt = int(round(float(n_male or 0.0)))
        u_cnt = int(round(float(n_unknown or 0.0)))
        gd_cnt = int(round(float(n_gender_diverse or 0.0)))
        amb_cnt = int(round(float(n_ambiguous_gender or 0.0)))
        parsed_total = f_cnt + m_cnt + u_cnt + gd_cnt + amb_cnt
        binary_total = f_cnt + m_cnt
        if parsed_total > 0:
            of_parsed_total = {
                "female": evaluate_unknown_bounds(f_cnt, u_cnt, gd_cnt + amb_cnt, parsed_total),
                "male": evaluate_unknown_bounds(m_cnt, u_cnt, gd_cnt + amb_cnt, parsed_total),
            }
        if binary_total > 0:
            of_classified_sex = {
                "female": f_cnt / binary_total,
                "male": m_cnt / binary_total,
            }

    return {
        "sex_report_status": status,
        "reported_sex": reported_sex,
        "reported_gender": reported_gender,
        "reported_both": reported_both,
        "is_participant_count": is_participant_count,
        "n_female": n_female,
        "n_male": n_male,
        "n_unknown": n_unknown,
        "n_gender_diverse": n_gender_diverse,
        "n_ambiguous_gender": n_ambiguous_gender,
        "enrollment_minus_parsed": enrollment_minus_parsed,
        "uninformative_reason": row.get("uninformative_reason"),
        "declared_not_collected": row.get("declared_not_collected"),
        "parser_rules_version": row.get("parser_rules_version"),
        "percent_female": pf,
        "female_share": female_share,
        "of_parsed_total": of_parsed_total,
        "of_classified": of_classified_sex,
        "composition_withheld_reason": composition_withheld_reason,
    }


def _sex_participant_total(record: dict[str, Any], sex_gender: dict[str, Any]) -> int | None:
    """Participant count from the sex table when available, for double-count detection.

    A table whose counts are not whole (`composition_withheld_reason`) gives no participant total, so it
    falls back to the legacy block as a non-participant table does.
    """
    if sex_gender.get("is_participant_count") is True and sex_gender.get(
        "composition_withheld_reason"
    ) is None and sex_gender.get("sex_report_status") in (
        "reported",
        "explicit_unknown_only",
    ):
        total = sum(
            float(sex_gender.get(k) or 0.0)
            for k in ("n_female", "n_male", "n_unknown", "n_gender_diverse", "n_ambiguous_gender")
        )
        if total > 0:
            return int(round(total))
    legacy_sex = record.get("sex") if _is_weekly_record(record) else None
    if isinstance(legacy_sex, dict) and legacy_sex.get("reported") and isinstance(legacy_sex.get("totals"), dict):
        legacy_total = sum(
            int(v) for v in legacy_sex["totals"].values() if isinstance(v, (int, float))
        )
        if legacy_total > 0:
            return legacy_total
    return None


def _label_and_count(entry: Any) -> tuple[str, int] | None:
    """Extract ``(label, count)`` from either a dict or legacy ``[label, count]`` pair."""
    if isinstance(entry, dict):
        orig = str(entry.get("original") or entry.get("label") or "")
        raw_cnt = entry.get("count")
        cnt = int(raw_cnt) if isinstance(raw_cnt, (int, float)) else 0
        return orig, cnt
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        orig = str(entry[0] or "")
        raw_cnt = entry[1]
        cnt = int(raw_cnt) if isinstance(raw_cnt, (int, float)) else 0
        return orig, cnt
    if isinstance(entry, str):
        return entry, 0
    return None


def _detect_combined_race_ethnicity(race_block: dict[str, Any]) -> tuple[bool, list[str], str | None]:
    """Return ``(is_combined, matched_labels, reason)`` when Hispanic/Latino appears in a race measure."""
    flags = {str(f) for f in (race_block.get("flags") or [])}
    matched: list[str] = []

    for cat in race_block.get("raw_categories") or []:
        if not isinstance(cat, dict):
            continue
        orig = str(cat.get("original") or "")
        cat_flags = {str(f) for f in (cat.get("flags") or [])}
        if _ETHNICITY_IN_RACE_RX.search(orig) or "split_from_combined" in cat_flags:
            if orig and orig not in matched:
                matched.append(orig)

    for q in race_block.get("quarantined_labels") or []:
        parsed_q = _label_and_count(q)
        if parsed_q is None:
            continue
        orig_q, _ = parsed_q
        if _ETHNICITY_IN_RACE_RX.search(orig_q) and orig_q not in matched:
            matched.append(orig_q)

    skipped = race_block.get("skipped_ethnicity_rows")
    if isinstance(skipped, list):
        for row in skipped:
            parsed_row = _label_and_count(row)
            if parsed_row is None:
                continue
            orig, cnt = parsed_row
            if cnt > 0 and orig and orig not in matched:
                matched.append(orig)
        if matched:
            return True, matched[:10], "hispanic_or_ethnicity_row_in_race_measure"

    if matched:
        return True, matched[:10], "combined_race_ethnicity_label_in_race_measure"

    if (
        "ethnicity_rows_skipped" in flags
        or "shortfall_is_ethnicity_rows" in flags
        or "split_from_combined" in flags
        or int(race_block.get("unknown_ethnicity_only") or 0) > 0
    ):
        return True, [], "ethnicity_rows_recorded_on_race_block"

    # Legacy pre-extracted record without `skipped_ethnicity_rows`: a title
    # naming ethnicity alongside an unexplained shortfall cannot rule out a
    # dropped Hispanic row (mirrors census/trials.py::category_structure).
    if (
        "skipped_ethnicity_rows" not in race_block
        and (
            "combined_race_ethnicity" in flags
            or any(
                "combined_race_ethnicity" in {str(f) for f in (c.get("flags") or [])}
                for c in (race_block.get("raw_categories") or [])
                if isinstance(c, dict)
            )
        )
        and int(race_block.get("unknown_inferred") or 0) > 0
    ):
        return True, [], "title_flagged_race_table_with_unverified_shortfall"

    return False, [], None


def _table_total_excess(
    baseline_total: int, sex_total: int | None, baseline_participants: int | None
) -> str | None:
    """Why a race or ethnicity table's total exceeds a count of the same people, or None.

    A table summing to more than its participants counts some of them twice (a multi-select race
    question, or rows of another table read again), so its shares would be normalized to the wrong
    total. It is compared with the sex table's participant total, as before, and with the baseline
    table's own Participants denominator, the total `race_extractor` and `ethnicity_extractor` balance
    against. Without a sex total, White 80 and Black 40 of 100 had been published as `omb_compatible`
    shares of 120. Each comparison allows 1% (at least 1). The sex comparison is tried first, so the 15
    race and 8 ethnicity tables of the stored raw trials it already caught keep their detail; none of
    those trials lacks a sex total.
    """
    def exceeds(limit: int | None) -> bool:
        return limit is not None and baseline_total > limit + max(1, int(0.01 * limit))

    if exceeds(sex_total):
        return "table_total_exceeds_sex_table"
    if exceeds(baseline_participants):
        return "table_total_exceeds_baseline_denominator"
    return None


def _analyze_race_block(
    race_block: dict[str, Any] | None,
    *,
    results_posted: bool,
    sex_total: int | None,
    reference_medians: dict[str, float] | None = None,
    baseline_participants: int | None = None,
) -> dict[str, Any]:
    """Classify and compute Denominator A & B metrics for a baseline race block."""
    if not results_posted:
        return {
            "table_status": "results_not_posted",
            "usable_for_omb_pooling": False,
            "quarantined_from_omb_pooling": False,
            "baseline_total": None,
            "classified_total": None,
            "unknown_count": None,
            "unmapped_count": None,
            "counts": None,
            "of_baseline_total": None,
            "of_classified": None,
            "of_classified_withheld_reason": "results_not_posted",
            "status_detail": "trial_has_no_posted_results",
        }

    block = race_block if isinstance(race_block, dict) else {}
    totals = block.get("omb_totals") if isinstance(block.get("omb_totals"), dict) else {}
    if not block.get("reported") or not totals:
        skipped = block.get("skipped_ethnicity_rows") or []
        detail = (
            "race_titled_table_held_ethnicity_rows_only"
            if skipped
            else ("all_zero_rejection" if "all_zero_rejection" in (block.get("flags") or []) else "no_race_measure")
        )
        return {
            "table_status": "table_absent_in_posted_results",
            "usable_for_omb_pooling": False,
            "quarantined_from_omb_pooling": False,
            "baseline_total": None,
            "classified_total": None,
            "unknown_count": None,
            "unmapped_count": None,
            "counts": None,
            "of_baseline_total": None,
            "of_classified": None,
            "of_classified_withheld_reason": "table_absent_in_posted_results",
            "status_detail": detail,
        }

    raw_counts = {k: int(totals.get(k) or 0) for k in RACE_CLASSIFIED_KEYS}
    unknown_count = int(totals.get("unknown_not_reported") or 0)
    extra_keys = {
        k: int(v)
        for k, v in totals.items()
        if k not in RACE_CLASSIFIED_KEYS and k != "unknown_not_reported" and int(v or 0) > 0
    }

    is_combined, combined_labels, combined_reason = _detect_combined_race_ethnicity(block)
    if is_combined:
        quarantined_total = sum(raw_counts.values()) + unknown_count + sum(extra_keys.values())
        return {
            "table_status": "combined_race_ethnicity_quarantined",
            "usable_for_omb_pooling": False,
            "quarantined_from_omb_pooling": True,
            "baseline_total": quarantined_total if quarantined_total > 0 else None,
            "classified_total": None,
            "unknown_count": None,
            "unmapped_count": None,
            "counts": None,
            "quarantined_counts": {**raw_counts, "unknown_not_reported": unknown_count, **extra_keys},
            "quarantined_ethnicity_labels": combined_labels,
            "of_baseline_total": None,
            "of_classified": None,
            "of_classified_withheld_reason": "combined_race_ethnicity_quarantined",
            "status_detail": combined_reason,
        }

    # Separate unmapped raw_categories that race_extractor placed into "other",
    # quarantined_labels, and any extra non-standard keys in omb_totals.
    raw_cats = [c for c in (block.get("raw_categories") or []) if isinstance(c, dict)]
    unmapped_in_raw = [c for c in raw_cats if "unmapped" in (c.get("flags") or [])]
    unmapped_from_other = min(raw_counts["other"], sum(int(c.get("count") or 0) for c in unmapped_in_raw))
    raw_counts["other"] -= unmapped_from_other

    quarantined_parsed = [
        pq for q in (block.get("quarantined_labels") or [])
        if (pq := _label_and_count(q)) is not None
    ]
    quarantined_sum = sum(cnt for _, cnt in quarantined_parsed)
    flags = {str(f) for f in (block.get("flags") or [])}
    if quarantined_sum > 0 and "denominator_balanced" in flags:
        inferred = int(block.get("unknown_inferred") or 0)
        absorbed = min(quarantined_sum, inferred, unknown_count)
        unknown_count -= absorbed

    unmapped_count = unmapped_from_other + quarantined_sum + sum(extra_keys.values())
    classified_total = sum(raw_counts.values())
    baseline_total = classified_total + unknown_count + unmapped_count

    if baseline_total <= 0:
        return {
            "table_status": "table_absent_in_posted_results",
            "usable_for_omb_pooling": False,
            "quarantined_from_omb_pooling": False,
            "baseline_total": None,
            "classified_total": None,
            "unknown_count": None,
            "unmapped_count": None,
            "counts": None,
            "of_baseline_total": None,
            "of_classified": None,
            "of_classified_withheld_reason": "table_absent_in_posted_results",
            "status_detail": "race_table_total_is_zero",
        }

    excess = _table_total_excess(baseline_total, sex_total, baseline_participants)
    double_counted = excess is not None
    has_non_standard = bool(unmapped_in_raw or quarantined_parsed or extra_keys or double_counted)
    status = "non_standard_categories_present" if has_non_standard else "omb_compatible"

    if double_counted:
        return {
            "table_status": status,
            "usable_for_omb_pooling": False,
            "quarantined_from_omb_pooling": False,
            "baseline_total": baseline_total,
            "classified_total": None,
            "unknown_count": unknown_count,
            "unmapped_count": unmapped_count,
            "counts": raw_counts,
            "of_baseline_total": None,
            "of_classified": None,
            "of_classified_withheld_reason": excess,
            "status_detail": excess,
        }

    refs = reference_medians or {}
    of_baseline = {
        cat: evaluate_unknown_bounds(
            raw_counts[cat],
            unknown_count,
            unmapped_count,
            baseline_total,
            refs.get(cat),
        )
        for cat in RACE_CLASSIFIED_KEYS
    }
    of_classified: dict[str, float] | None = (
        {cat: raw_counts[cat] / classified_total for cat in RACE_CLASSIFIED_KEYS}
        if classified_total > 0
        else None
    )
    withheld_reason: str | None = None if classified_total > 0 else "classified_total_is_zero"

    out: dict[str, Any] = {
        "table_status": status,
        "usable_for_omb_pooling": status == "omb_compatible",
        "quarantined_from_omb_pooling": False,
        "baseline_total": baseline_total,
        "classified_total": classified_total,
        "unknown_count": unknown_count,
        "unmapped_count": unmapped_count,
        "counts": raw_counts,
        "of_baseline_total": of_baseline,
        "of_classified": of_classified,
        "of_classified_withheld_reason": withheld_reason,
        "status_detail": (
            "non_standard_or_unmapped_categories"
            if has_non_standard
            else (
                "title_names_ethnicity_rows_are_race_only"
                if "combined_race_ethnicity" in flags
                else "standard_omb_categories"
            )
        ),
    }
    if unmapped_in_raw or quarantined_parsed or extra_keys:
        out["non_standard_labels"] = (
            [str(c.get("original") or "") for c in unmapped_in_raw]
            + [lbl for lbl, _ in quarantined_parsed]
            + sorted(extra_keys)
        )[:10]
    return out


def _analyze_ethnicity_block(
    eth_block: dict[str, Any] | None,
    *,
    results_posted: bool,
    sex_total: int | None,
    reference_medians: dict[str, float] | None = None,
    baseline_participants: int | None = None,
) -> dict[str, Any]:
    """Classify and compute Denominator A & B metrics for a baseline ethnicity block."""
    if not results_posted:
        return {
            "table_status": "results_not_posted",
            "usable_for_omb_pooling": False,
            "quarantined_from_omb_pooling": False,
            "baseline_total": None,
            "classified_total": None,
            "unknown_count": None,
            "unmapped_count": None,
            "counts": None,
            "of_baseline_total": None,
            "of_classified": None,
            "of_classified_withheld_reason": "results_not_posted",
            "status_detail": "trial_has_no_posted_results",
        }

    block = eth_block if isinstance(eth_block, dict) else {}
    totals = block.get("omb_totals") if isinstance(block.get("omb_totals"), dict) else {}
    if not block.get("reported") or not totals:
        detail = (
            "all_zero_rejection"
            if "all_zero_rejection" in (block.get("flags") or [])
            else "no_ethnicity_measure"
        )
        return {
            "table_status": "table_absent_in_posted_results",
            "usable_for_omb_pooling": False,
            "quarantined_from_omb_pooling": False,
            "baseline_total": None,
            "classified_total": None,
            "unknown_count": None,
            "unmapped_count": None,
            "counts": None,
            "of_baseline_total": None,
            "of_classified": None,
            "of_classified_withheld_reason": "table_absent_in_posted_results",
            "status_detail": detail,
        }

    raw_cats = [c for c in (block.get("raw_categories") or []) if isinstance(c, dict)]
    raw_counts = {k: int(totals.get(k) or 0) for k in ETHNICITY_CLASSIFIED_KEYS}
    unknown_count = int(totals.get("unknown_not_reported") or 0)

    # Undo ethnicity_extractor's `not_hispanic_from_race_rows` inference when
    # no explicit Not Hispanic or Latino row was posted in the ethnicity table:
    # a single-category "Hispanic" row in a combined table must remain
    # `single_category_ethnicity` with `of_classified = None`.
    inferred_not_hisp = int(block.get("not_hispanic_from_race_rows") or 0)
    explicit_not_hisp_rows = [
        c for c in raw_cats
        if c.get("omb_category") == "not_hispanic_latino" and "unmapped" not in (c.get("flags") or [])
    ]
    if inferred_not_hisp > 0 and not explicit_not_hisp_rows:
        reverted = min(inferred_not_hisp, raw_counts["not_hispanic_latino"])
        raw_counts["not_hispanic_latino"] -= reverted
        unknown_count += reverted

    extra_keys = {
        k: int(v)
        for k, v in totals.items()
        if k not in ETHNICITY_CLASSIFIED_KEYS and k != "unknown_not_reported" and int(v or 0) > 0
    }

    # Unmapped rows in ethnicity_extractor land in unknown_not_reported with
    # flag "unmapped"; distinguish substantive non-standard ethnicity labels
    # (e.g. "European", "Jewish") from plain unknown/missing wording.
    non_standard_raw = [
        c for c in raw_cats
        if "unmapped" in (c.get("flags") or [])
        and not _ETHNICITY_UNKNOWN_TEXT_RX.match(str(c.get("original") or "").strip())
    ]
    unmapped_from_unknown = min(
        unknown_count,
        sum(int(c.get("count") or 0) for c in non_standard_raw),
    )
    unknown_count -= unmapped_from_unknown
    unmapped_count = unmapped_from_unknown + sum(extra_keys.values())

    classified_total = sum(raw_counts.values())
    baseline_total = classified_total + unknown_count + unmapped_count
    if baseline_total <= 0:
        return {
            "table_status": "table_absent_in_posted_results",
            "usable_for_omb_pooling": False,
            "quarantined_from_omb_pooling": False,
            "baseline_total": None,
            "classified_total": None,
            "unknown_count": None,
            "unmapped_count": None,
            "counts": None,
            "of_baseline_total": None,
            "of_classified": None,
            "of_classified_withheld_reason": "table_absent_in_posted_results",
            "status_detail": "ethnicity_table_total_is_zero",
        }

    excess = _table_total_excess(baseline_total, sex_total, baseline_participants)
    if excess is not None:
        return {
            "table_status": "non_standard_categories_present",
            "usable_for_omb_pooling": False,
            "quarantined_from_omb_pooling": False,
            "baseline_total": baseline_total,
            "classified_total": None,
            "unknown_count": unknown_count,
            "unmapped_count": unmapped_count,
            "counts": raw_counts,
            "of_baseline_total": None,
            "of_classified": None,
            "of_classified_withheld_reason": excess,
            "status_detail": excess,
        }

    if raw_cats:
        specific_present = {
            str(c.get("omb_category"))
            for c in raw_cats
            if "unmapped" not in (c.get("flags") or [])
        } & set(ETHNICITY_CLASSIFIED_KEYS)
        is_single_category = len(specific_present) == 1
    else:
        nonzero_specific = [k for k in ETHNICITY_CLASSIFIED_KEYS if raw_counts[k] > 0]
        is_single_category = len(nonzero_specific) == 1 and (unknown_count + unmapped_count) > 0

    has_non_standard = bool(non_standard_raw or extra_keys)
    if is_single_category:
        status = "single_category_ethnicity"
    elif has_non_standard:
        status = "non_standard_categories_present"
    else:
        status = "omb_compatible"

    refs = reference_medians or {}
    of_baseline = {
        cat: evaluate_unknown_bounds(
            raw_counts[cat],
            unknown_count,
            unmapped_count,
            baseline_total,
            refs.get(cat),
        )
        for cat in ETHNICITY_CLASSIFIED_KEYS
    }

    if status == "single_category_ethnicity":
        of_classified: dict[str, float] | None = None
        withheld_reason: str | None = "single_category_ethnicity"
    elif classified_total <= 0:
        of_classified = None
        withheld_reason = "classified_total_is_zero"
    else:
        of_classified = {cat: raw_counts[cat] / classified_total for cat in ETHNICITY_CLASSIFIED_KEYS}
        withheld_reason = None

    out: dict[str, Any] = {
        "table_status": status,
        "usable_for_omb_pooling": status in ("omb_compatible", "single_category_ethnicity"),
        "quarantined_from_omb_pooling": False,
        "baseline_total": baseline_total,
        "classified_total": classified_total,
        "unknown_count": unknown_count,
        "unmapped_count": unmapped_count,
        "counts": raw_counts,
        "of_baseline_total": of_baseline,
        "of_classified": of_classified,
        "of_classified_withheld_reason": withheld_reason,
        "status_detail": (
            "table_reports_one_ethnicity_category_only"
            if status == "single_category_ethnicity"
            else ("non_standard_or_unmapped_categories" if has_non_standard else "standard_omb_categories")
        ),
    }
    if non_standard_raw or extra_keys:
        out["non_standard_labels"] = (
            [str(c.get("original") or "") for c in non_standard_raw] + sorted(extra_keys)
        )[:10]
    return out


def build_us_site_omission_audit(
    geography: dict[str, Any],
    *,
    results_posted: bool,
    race_table_status: str,
    ethnicity_table_status: str,
    sex_report_status: str,
) -> dict[str, Any]:
    """Audit race/ethnicity/sex baseline omission among US-site trials with posted results.

    Out-of-scope trials (no confirmed US footprint or results not posted) carry
    ``None`` on omission flags rather than ``False``.
    """
    footprint = geography.get("site_footprint_class")
    in_us_site_scope = footprint in ("us_only", "multinational_with_us_sites")
    has_any_us_facility = int(geography.get("n_us_facilities") or 0) >= 1
    in_denom = bool(in_us_site_scope and results_posted)

    if not in_denom:
        return {
            "in_us_site_scope": in_us_site_scope,
            "has_any_us_facility": has_any_us_facility,
            "results_posted": results_posted,
            "in_posted_us_site_audit_denominator": False,
            "omitted_race_table": None,
            "omitted_ethnicity_table": None,
            "omitted_race_or_ethnicity_table": None,
            "omitted_both_race_and_ethnicity_tables": None,
            "omitted_sex_table": None,
        }

    omitted_race = race_table_status == "table_absent_in_posted_results"
    omitted_eth = ethnicity_table_status == "table_absent_in_posted_results"
    omitted_sex = sex_report_status == "not_reported"
    return {
        "in_us_site_scope": True,
        "has_any_us_facility": has_any_us_facility,
        "results_posted": True,
        "in_posted_us_site_audit_denominator": True,
        "omitted_race_table": omitted_race,
        "omitted_ethnicity_table": omitted_eth,
        "omitted_race_or_ethnicity_table": bool(omitted_race or omitted_eth),
        "omitted_both_race_and_ethnicity_tables": bool(omitted_race and omitted_eth),
        "omitted_sex_table": omitted_sex,
    }


def attach_reference_medians(
    demographics: dict[str, Any],
    *,
    race_medians: dict[str, float] | None = None,
    ethnicity_medians: dict[str, float] | None = None,
    sex_female_median: float | None = None,
    reference_stratum_id: str | None = None,
    reference_withheld_small_cell: bool | None = None,
) -> dict[str, Any]:
    """Return a copy of ``demographics`` with ``reference_median`` and ``direction_settled`` updated."""
    updated = copy.deepcopy(demographics)
    updated["reference_stratum_id"] = reference_stratum_id
    updated["reference_withheld_small_cell"] = reference_withheld_small_cell

    race_block = updated.get("race")
    if isinstance(race_block, dict) and isinstance(race_block.get("of_baseline_total"), dict):
        b_tot = int(race_block["baseline_total"])
        unk = int(race_block["unknown_count"])
        unm = int(race_block["unmapped_count"])
        counts = race_block["counts"]
        race_refs = race_medians or {}
        race_block["of_baseline_total"] = {
            cat: evaluate_unknown_bounds(counts[cat], unk, unm, b_tot, race_refs.get(cat))
            for cat in RACE_CLASSIFIED_KEYS
        }

    eth_block = updated.get("ethnicity")
    if isinstance(eth_block, dict) and isinstance(eth_block.get("of_baseline_total"), dict):
        b_tot = int(eth_block["baseline_total"])
        unk = int(eth_block["unknown_count"])
        unm = int(eth_block["unmapped_count"])
        counts = eth_block["counts"]
        eth_refs = ethnicity_medians or {}
        eth_block["of_baseline_total"] = {
            cat: evaluate_unknown_bounds(counts[cat], unk, unm, b_tot, eth_refs.get(cat))
            for cat in ETHNICITY_CLASSIFIED_KEYS
        }

    sg_block = updated.get("sex_gender")
    if (
        isinstance(sg_block, dict)
        and isinstance(sg_block.get("of_parsed_total"), dict)
        and sex_female_median is not None
    ):
        fem_entry = sg_block["of_parsed_total"].get("female")
        male_entry = sg_block["of_parsed_total"].get("male")
        if isinstance(fem_entry, dict):
            u_cnt = int(round(float(sg_block.get("n_unknown") or 0.0)))
            gd_cnt = int(round(float(sg_block.get("n_gender_diverse") or 0.0)))
            amb_cnt = int(round(float(sg_block.get("n_ambiguous_gender") or 0.0)))
            p_tot = int(fem_entry["baseline_total"])
            sg_block["of_parsed_total"]["female"] = evaluate_unknown_bounds(
                int(fem_entry["count"]), u_cnt, gd_cnt + amb_cnt, p_tot, sex_female_median
            )
            if isinstance(male_entry, dict):
                sg_block["of_parsed_total"]["male"] = evaluate_unknown_bounds(
                    int(male_entry["count"]), u_cnt, gd_cnt + amb_cnt, p_tot, 1.0 - sex_female_median
                )

    return updated


def whole_population_group_unidentified(record: dict[str, Any]) -> bool:
    """Whether a raw record's baseline table has several groups and none that
    `src.utils.get_overall_group_id` recognises as the whole population.

    The race and ethnicity extractors read each row at that group, and without one
    `_get_measurement_value` and `get_total_baseline_participants` read the last group,
    so a table of arms with no Total group was published with its last arm's counts as
    the trial's. CT.gov ends a multi-group table with its Total group in all but one of
    the 55,810 multi-group trials of the 2026-11-15 weekly inputs (`src/utils.py`), and
    in all 2,198 multi-group tables of the 3,133 stored ones. The sex/gender parser picks
    its total by a sum check, which falls back to an arm, so its composition is withheld
    too (`_withhold_sex_composition`).
    """
    return whole_population_group_problem(record) is not None


def whole_population_group_problem(record: dict[str, Any]) -> str | None:
    """Why no one group of a raw record's multi-group baseline table is the whole population, or None.

    `multi_group_baseline_without_total_group`: none is recognised (above).
    `several_groups_titled_as_whole_population`: more than one group passes the test
    `src.utils.get_overall_group_id` applies (the last group's title starting with "total", or a title
    exactly in `_WHOLE_POPULATION_GROUP_TITLES`), so its choice is a matter of rule order: in
    `[Total, Arm A, Overall]` it read the first, whatever the other held. None of the 2,171 stored
    multi-group tables has two such groups (NCT03410693's arms are titled "... Overall Population",
    which only the looser `is_overall_group_title` matches, and its last group, "Total", is read).
    """
    results = record.get("resultsSection")
    module = results.get("baselineCharacteristicsModule") if isinstance(results, dict) else None
    groups = module.get("groups") if isinstance(module, dict) else None
    if not (isinstance(groups, list) and len(groups) > 1 and all(isinstance(g, dict) for g in groups)):
        return None
    if src_utils.get_overall_group_id(record) is None:
        return "multi_group_baseline_without_total_group"
    titles = [str(g.get("title") or "").strip().lower() for g in groups]
    candidates = {
        i for i, title in enumerate(titles)
        if title in src_utils._WHOLE_POPULATION_GROUP_TITLES or (i == len(titles) - 1 and title.startswith("total"))
    }
    return "several_groups_titled_as_whole_population" if len(candidates) > 1 else None


# The absent details the extractors decide from measure titles and row labels alone, never from a group's
# values: no race-titled (or race-carrying sex/gender-titled) measure, a race-titled table holding only
# ethnicity rows, and no ethnicity-titled measure (or only a race table under one). `race_extractor`
# sets `reported` for every table it counts and `ethnicity_extractor` on accepting one, before any value
# is read. An all-zero rejection is read at a group's values, so in a table without a whole-population
# group it is not a finding about the trial and becomes unidentified.
ABSENT_BY_LABEL_DETAILS: frozenset[str] = frozenset(
    {"no_race_measure", "race_titled_table_held_ethnicity_rows_only", "no_ethnicity_measure"}
)


def _reads_as_number(value: Any) -> bool:
    """Whether a baseline measurement value reads as a number (`"1,060"` does, `"NA"` does not)."""
    try:
        float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return False
    return value is not None


def _primary_sex_measure(record: dict[str, Any]) -> tuple[dict[str, Any], Any] | None:
    """The measure the vendored parser takes Female and Male from, with that measure parsed alone.

    With several sex/gender measures the parser takes Female and Male from one primary measure (the
    largest Female + Male, the first on ties) but Unknown, gender-diverse and ambiguous as maxima across
    the measures, and a flag such as `flag_multiclass_timepoint` from any of them (`sgp.parse_trial`). A
    check on the primary measure's layout or total therefore reads that measure alone (`sgp.parse_measure`):
    it is the first selected measure whose own title, Female and Male are the row's.
    """
    module = (record.get("resultsSection") or {}).get("baselineCharacteristicsModule") or {}
    measures = module.get("measures") if isinstance(module, dict) else None
    if not isinstance(measures, list):
        return None
    try:
        parsed = sgp.parse_trial(measures, None)
        for measure in sgp.select_sex_gender_measures(measures):
            alone = sgp.parse_measure(measure)
            if (alone.title, alone.female, alone.male) == (parsed.measure_title, parsed.n_female, parsed.n_male):
                return measure, alone
    except Exception:  # noqa: BLE001 - extract_sex_gender already read the same failure
        return None
    return None


def _measure_counts_miss_denominator(alone: Any, baseline_participants: int | None) -> bool:
    """Whether one parsed sex/gender measure's counts fail to add up to the table's Participants denominator.

    Only the primary measure's own counts are added: an Unknown taken from another measure could otherwise
    make an arm's or a subgroup's counts add up to the table (Female 60 and Male 30 of a subgroup, Unknown 10
    from a gender measure, against 100).
    """
    counted = sum(float(getattr(alone, k) or 0.0) for k in ("female", "male", "unknown", "gender_diverse", "ambiguous"))
    return baseline_participants is None or round(counted) != baseline_participants


def _whole_population_sex_problem(
    record: dict[str, Any], sex_gender: dict[str, Any], baseline_participants: int | None
) -> str | None:
    """Why the sex/gender counts the parser read are an arm's rather than the trial's, or None.

    A table with a whole-population group can post that group's sex entries as `"NA"` beside an arm's
    counts. The parser's sum check (`sgp.choose_total`) then reads the arm: NCT01388361 posts Female 90
    and Male 146 for one arm of 236 and `"NA"` for the Total of 413, and its arm's share, 0.381, had been
    published as the trial's (`whole_population_sex_counts_missing`). The whole-population entry can also
    post a number the parser does not take (`sex_counts_differ_from_whole_population`): with a numeric
    Total listed before an arm and an `"NA"` arm after it, no value is the sum of the others, and the
    fallback takes the last number, the arm's (Total 60, arm 30, `"NA"` reads 30). That comparison is made
    where the parser reads one entry list: each labelled category of a standard layout's first class, and
    each labelled one-category class of a customized layout (a customized class pooling several categories
    has its value chosen among all of them, so no one entry is the whole population's).

    Either way, the counts are taken for the trial's when they add up to the table's own Participants
    denominator: NCT00596817 posts an open-label period for its one group (Total `"NA"`) and a
    double-blind period with a Total, and its counts, 647 and 388, make up its 1,035. These are the only
    two of the 2,171 stored multi-group tables whose selected sex measure has a whole-population entry
    without a number beside a number, and none has a numeric one the parser did not take.
    """
    if sex_gender.get("is_participant_count") is not True:
        return None
    overall = src_utils.get_overall_group_id(record)
    primary = _primary_sex_measure(record)
    if overall is None or primary is None:
        return None
    measure, alone = primary
    raw_classes = measure.get("classes") if isinstance(measure.get("classes"), list) else []
    missing = False
    for cls in raw_classes:
        for cat in (cls.get("categories") or []) if isinstance(cls, dict) else []:
            rows = cat.get("measurements") if isinstance(cat, dict) else None
            entries = [e for e in rows or [] if isinstance(e, dict)]
            whole = [e for e in entries if e.get("groupId") == overall]
            if any(_reads_as_number(e.get("value")) for e in entries if e.get("groupId") != overall) and not (
                whole and _reads_as_number(whole[0].get("value"))
            ):
                missing = True
    # The entry lists the parser reads, each with the label it maps (`sgp.parse_measure`).
    read: list[tuple[Any, Any]] = []
    if alone.layout == "customized":
        for cls in raw_classes:
            cats = [c for c in (cls.get("categories") or []) if isinstance(c, dict)] if isinstance(cls, dict) else []
            if len(cats) == 1:
                read.append((cls.get("title"), cats[0].get("measurements")))
    else:
        first = raw_classes[0] if raw_classes and isinstance(raw_classes[0], dict) else {}
        cats = first.get("categories") if isinstance(first.get("categories"), list) else []
        read = [(c.get("title"), c.get("measurements")) for c in cats if isinstance(c, dict)]
    differs = False
    for label, rows in read:
        if label is None or not sgp.canon_bucket(str(label)) or not isinstance(rows, list):
            continue
        whole_values = [sgp._num(e.get("value")) for e in rows if isinstance(e, dict) and e.get("groupId") == overall]
        if len(whole_values) == 1 and whole_values[0] is not None:
            taken = sgp.choose_total(rows)["total"]
            differs = differs or taken is None or abs(taken - whole_values[0]) > 1e-6
    if not (missing or differs) or not _measure_counts_miss_denominator(alone, baseline_participants):
        return None
    return "whole_population_sex_counts_missing" if missing else "sex_counts_differ_from_whole_population"


def _sex_counts_from_one_class_of_several(
    record: dict[str, Any], sex_gender: dict[str, Any], baseline_participants: int | None
) -> bool:
    """Whether the sex/gender counts the parser read are one class's of several rather than the trial's.

    A standard-layout sex measure (the categories are the sex labels) can post several classes: study
    parts, periods, cohorts, subgroups or populations, each holding the same rows. The vendored parser
    reads only the first class (`flag_multiclass_timepoint`, a field the lean row does not carry), so a
    subgroup's share had been published as the trial's: NCT04911296's first age band (Female 29, Male 21
    of 150), NCT05209932's child participants (62 and 69 of 233, its parents in the second class),
    NCT04680273's Part 1 (6 women of 16, a share of 1.0). The counts are taken for the trial's when they
    add up to the table's own Participants denominator, as when the first class is "All Patients"
    (NCT03043872, 987) or a period everyone entered (NCT03323749, 218). Otherwise the composition is
    withheld rather than read from another class: no class title says which is the trial's population,
    and the class that adds up to the denominator can be a population pooled with another trial
    (NCT02163759: its own 358 first, then "GA28948 & GA28949 Pooled Population", 716). Of the 3,096
    stored raw trials with a baseline measure, 22 have such a measure and 14 are withheld. The flag and
    the counts are the primary measure's own (`_primary_sex_measure`), not the row's, which can carry
    another measure's flag or Unknown. The parser is not changed.
    """
    if sex_gender.get("is_participant_count") is not True:
        return False
    primary = _primary_sex_measure(record)
    if primary is None or primary[1].flag_multiclass_timepoint is not True:
        return False
    return _measure_counts_miss_denominator(primary[1], baseline_participants)


def _withhold_sex_composition(sex_gender: dict[str, Any], group_problem: str) -> dict[str, Any]:
    """The sex/gender row of a table with no one whole-population group, its composition withheld.

    The vendored parser picks its total by a sum check (`sgp.choose_total`): with no Total column it
    falls back to the last group, an arm, flagging the fallback in a field the lean row does not carry;
    with two equal arms it reads one as the total, unflagged. So the arm's female share had been
    published as the trial's and entered the pooled benchmarks while race and ethnicity were withheld.
    The reporting state and the parsed counts are kept, as the parser read them; `percent_female`,
    `female_share`, `of_parsed_total` and `of_classified` are withheld, with `composition_withheld_reason`
    naming the group problem, and the counts give no participant total to the double-count check. None
    of the stored raw trials has such a table. The parser's choice rule is not changed. The same applies
    when the parser read an arm instead of the whole-population entry (`_whole_population_sex_problem`)
    or the first of several classes (`_sex_counts_from_one_class_of_several`): such counts are not the
    trial's, so a race or ethnicity table is bounded by the baseline table's own denominator instead.
    """
    if sex_gender.get("is_participant_count") is not True:
        return sex_gender
    return {
        **sex_gender,
        "percent_female": None,
        "female_share": None,
        "of_parsed_total": None,
        "of_classified": None,
        "composition_withheld_reason": group_problem,
    }


def _whole_population_unidentified_block(detail: str) -> dict[str, Any]:
    """The race or ethnicity block of a table with no one whole-population group: no counts or shares."""
    return {
        "table_status": "whole_population_group_unidentified",
        "usable_for_omb_pooling": False,
        "quarantined_from_omb_pooling": False,
        "baseline_total": None,
        "classified_total": None,
        "unknown_count": None,
        "unmapped_count": None,
        "counts": None,
        "of_baseline_total": None,
        "of_classified": None,
        "of_classified_withheld_reason": "whole_population_group_unidentified",
        "status_detail": detail,
    }


def extract_demographics(
    record: dict[str, Any],
    *,
    geography: dict[str, Any] | None = None,
    snapshot_date: str = "1970-01-01",
    reference_medians: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """Extract structured race, ethnicity, sex/gender, and US-site omission audit for one trial."""
    geo = geography if geography is not None else extract_geography(record)
    posted = is_results_posted(record)
    # The baseline table's own Participants count, read as the extractors balance against it, without
    # the registered-enrollment fallback. Weekly records carry no baseline table, so for them it is None
    # and only the sex total bounds a race or ethnicity table.
    baseline_participants = (
        src_utils.get_total_baseline_participants(
            _study_without_protocol_enrollment_padding(record), src_utils.get_overall_group_id(record)
        )
        if posted and "resultsSection" in record
        else None
    )
    sex_gender = extract_sex_gender(record, snapshot_date=snapshot_date)
    group_problem = whole_population_group_problem(record) if posted and "protocolSection" in record else None
    if group_problem is not None:
        sex_gender = _withhold_sex_composition(sex_gender, group_problem)
    elif posted and "protocolSection" in record and (
        unread := _whole_population_sex_problem(record, sex_gender, baseline_participants)
    ):
        sex_gender = _withhold_sex_composition(sex_gender, unread)
    elif posted and "protocolSection" in record and _sex_counts_from_one_class_of_several(
        record, sex_gender, baseline_participants
    ):
        sex_gender = _withhold_sex_composition(sex_gender, "sex_counts_from_one_class_of_several")
    sex_total = _sex_participant_total(record, sex_gender)

    weekly = _is_weekly_record(record)
    if weekly:
        raw_race = record.get("race") if isinstance(record.get("race"), dict) else None
    elif posted:
        raw_race = race_extractor.extract_race_data(_study_without_protocol_enrollment_padding(record))
    else:
        raw_race = None

    if weekly:
        raw_eth = record.get("ethnicity") if isinstance(record.get("ethnicity"), dict) else None
    elif posted:
        raw_eth = ethnicity_extractor.extract_ethnicity_data(_study_without_protocol_enrollment_padding(record))
    else:
        raw_eth = None

    refs = reference_medians or {}
    race_info = _analyze_race_block(
        raw_race,
        results_posted=posted,
        sex_total=sex_total,
        reference_medians=refs.get("race"),
        baseline_participants=baseline_participants,
    )
    eth_info = _analyze_ethnicity_block(
        raw_eth,
        results_posted=posted,
        sex_total=sex_total,
        reference_medians=refs.get("ethnicity"),
        baseline_participants=baseline_participants,
    )
    if group_problem is not None:
        # No group stands for the whole trial, so neither extractor's counts are the trial's. Whether a
        # dimension has a measure at all does not depend on the group, so a dimension with none keeps its
        # absent status (`ABSENT_BY_LABEL_DETAILS`) and still counts as omitted in the US-site audit: an
        # arm-only table with a race measure and no ethnicity measure had marked both unidentified.
        if race_info["status_detail"] not in ABSENT_BY_LABEL_DETAILS:
            race_info = _whole_population_unidentified_block(group_problem)
        if eth_info["status_detail"] not in ABSENT_BY_LABEL_DETAILS:
            eth_info = _whole_population_unidentified_block(group_problem)

    omission_audit = build_us_site_omission_audit(
        geo,
        results_posted=posted,
        race_table_status=race_info["table_status"],
        ethnicity_table_status=eth_info["table_status"],
        sex_report_status=sex_gender["sex_report_status"],
    )

    return {
        "results_posted": posted,
        "race_table_status": race_info["table_status"],
        "ethnicity_table_status": eth_info["table_status"],
        "sex_report_status": sex_gender["sex_report_status"],
        "race": race_info,
        "ethnicity": eth_info,
        "sex_gender": sex_gender,
        "us_site_omission_audit": omission_audit,
        "reference_stratum_id": None,
        "reference_withheld_small_cell": None,
    }
