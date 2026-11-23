"""Per-study row wrapper, lean projection, error quarantine, and cohort barrier summaries.

Mirrors the non-collapsing state and provenance architecture of `src/sex_gender_table.py`:
  - `build_eligibility_row(study, snapshot_date=None)` builds the full per-trial
    record from either a ClinicalTrials.gov API v2 study payload or an extracted
    CivicSample study dictionary, catching any unexpected exception into
    `parse_error_row(...)` (`eligibility_parse_status = "parse_error"`) so a
    batch run never drops a trial.
  - `lean_eligibility_row(row)` strips verbatim evidence arrays to produce a
    compact projection suitable for per-study dashboard payloads.
  - `summarize_cohort_barriers(rows)` tabulates non-collapsing counts across all
    5 section layouts, all 5 domain statuses (+ `parse_error`), substatuses,
    clinical exception flags, and structured-vs-text discordances.
"""
from __future__ import annotations

from typing import Any, Iterable

from eligibility.parser import (
    DOMAIN_STATUS_LEVELS,
    DOMAIN_SUBSTATUS_LEVELS,
    parse_eligibility_text,
)
from eligibility.sections import SECTION_LAYOUT_LEVELS
from eligibility.vocabulary import DOMAINS, VOCABULARY_SHA256, VOCABULARY_VERSION

PARSER_MODULE_VERSION: str = "1.0.0"

ELIGIBILITY_PARSE_STATUS_LEVELS: tuple[str, ...] = (
    "parsed",
    "empty_or_placeholder",
    "parse_error",
)

DISCORDANCE_FLAGS: tuple[str, ...] = (
    "hidden_upper_age_cap",
    "hidden_lower_age_bound",
    "conflicting_age_bounds",
    "std_ages_discordance",
    "sex_eligibility_discordance",
    "healthy_volunteers_discordance",
    "any_structured_discordance",
)

LEAN_COLUMNS: tuple[str, ...] = (
    "nct_id",
    "snapshot_date",
    "eligibility_parse_status",
    "parse_ok",
    "section_layout",
    "placeholder_reason",
    "is_multi_cohort",
    "domain_statuses",
    "domain_substatuses",
    "has_gilberts_exception",
    "has_skin_or_insitu_exception",
    "has_contraception_requirement",
    "ecog_max_allowed",
    "text_min_age_years",
    "text_max_age_years",
    "hidden_upper_age_cap",
    "hidden_lower_age_bound",
    "conflicting_age_bounds",
    "std_ages_discordance",
    "sex_eligibility_discordance",
    "healthy_volunteers_discordance",
    "any_structured_discordance",
    "parser_version",
    "vocabulary_version",
    "vocabulary_sha256",
)


def _safe_nct_of(study: Any) -> str | None:
    """Extract NCT identifier safely without raising on malformed study inputs."""
    if not isinstance(study, dict):
        return None
    nct_top = study.get("nct_id")
    if isinstance(nct_top, str) and nct_top.strip():
        return nct_top.strip()
    proto = study.get("protocolSection")
    if isinstance(proto, dict):
        ident = proto.get("identificationModule")
        if isinstance(ident, dict):
            nct_v2 = ident.get("nctId")
            if isinstance(nct_v2, str) and nct_v2.strip():
                return nct_v2.strip()
    return None


def _extract_raw_criteria_from_study(study: dict[str, Any]) -> Any:
    """Extract raw `eligibilityCriteria` field without coercing non-string payloads."""
    if "protocolSection" in study:
        proto = study.get("protocolSection")
        if not isinstance(proto, dict):
            raise TypeError(f"protocolSection must be dict, got {type(proto).__name__}")
        elig_mod = proto.get("eligibilityModule")
        if elig_mod is not None and not isinstance(elig_mod, dict):
            raise TypeError(f"eligibilityModule must be dict or None, got {type(elig_mod).__name__}")
        if isinstance(elig_mod, dict):
            return elig_mod.get("eligibilityCriteria")
        return None
    if "eligibility_criteria" in study:
        return study.get("eligibility_criteria")
    if "eligibilityCriteria" in study:
        return study.get("eligibilityCriteria")
    return None


def parse_error_row(
    nct_id: str | None = None,
    snapshot_date: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build a schema-complete quarantine row for an unreadable or exception-raising study record.

    Domain statuses and discordance booleans are set to `None` — never silently
    coerced into `"not_mentioned"` or `False`.
    """
    row: dict[str, Any] = {
        "nct_id": nct_id,
        "snapshot_date": snapshot_date,
        "eligibility_parse_status": "parse_error",
        "parse_ok": False,
        "parse_error": error,
        "raw_criteria_present": None,
        "raw_criteria_char_len": None,
        "section_layout": None,
        "placeholder_reason": None,
        "has_unheaded_preamble": None,
        "is_multi_cohort": None,
        "n_sections": None,
        "n_clauses": None,
        "n_inclusion_clauses": None,
        "n_exclusion_clauses": None,
        "n_unheaded_clauses": None,
        "domain_statuses": {dom: None for dom in DOMAINS},
        "domain_substatuses": {dom: None for dom in DOMAINS},
        "domains": {dom: None for dom in DOMAINS},
        "has_gilberts_exception": None,
        "has_skin_or_insitu_exception": None,
        "has_contraception_requirement": None,
        "ecog_max_allowed": None,
        "text_min_age_years": None,
        "text_max_age_years": None,
        "hidden_upper_age_cap": None,
        "hidden_lower_age_bound": None,
        "conflicting_age_bounds": None,
        "std_ages_discordance": None,
        "sex_eligibility_discordance": None,
        "healthy_volunteers_discordance": None,
        "any_structured_discordance": None,
        "discordance": None,
        "parser_version": PARSER_MODULE_VERSION,
        "vocabulary_version": VOCABULARY_VERSION,
        "vocabulary_sha256": VOCABULARY_SHA256,
    }
    for dom in DOMAINS:
        row[f"{dom}_status"] = None
        row[f"{dom}_substatus"] = None
    return row


def build_eligibility_row(
    study: dict[str, Any],
    snapshot_date: str | None = None,
) -> dict[str, Any]:
    """Parse one study record into a full eligibility barrier and discordance row.

    Any unexpected runtime exception is caught and surfaced as
    `eligibility_parse_status = "parse_error"` via `parse_error_row(...)`.
    """
    nct_id = _safe_nct_of(study)
    try:
        if not isinstance(study, dict):
            raise TypeError(f"study must be a dict, got {type(study).__name__}")

        raw_criteria = _extract_raw_criteria_from_study(study)
        parsed = parse_eligibility_text(raw_criteria, eligibility_module_or_study=study)
        sec = parsed.sectioned
        disc = parsed.discordance
        assert disc is not None

        parse_status = (
            "empty_or_placeholder"
            if sec.section_layout == "empty_or_placeholder"
            else "parsed"
        )
        raw_present = isinstance(raw_criteria, str) and bool(raw_criteria.strip())
        raw_len = len(raw_criteria) if isinstance(raw_criteria, str) else 0

        n_inc = sum(1 for c in sec.clauses if c.section_kind == "inclusion")
        n_exc = sum(1 for c in sec.clauses if c.section_kind == "exclusion")
        n_unh = sum(1 for c in sec.clauses if c.section_kind == "unheaded")

        domain_statuses = {dom: parsed.domains[dom].status for dom in DOMAINS}
        domain_substatuses = {dom: parsed.domains[dom].substatus for dom in DOMAINS}
        domains_dict = {dom: parsed.domains[dom].to_dict() for dom in DOMAINS}

        hep = parsed.domains["hepatic_function"]
        mal = parsed.domains["prior_malignancy"]
        preg = parsed.domains["pregnancy_or_lactation"]
        ps = parsed.domains["performance_status"]
        ecog_max = ps.extracted_thresholds.get("ecog_max_allowed")

        row: dict[str, Any] = {
            "nct_id": nct_id,
            "snapshot_date": snapshot_date,
            "eligibility_parse_status": parse_status,
            "parse_ok": True,
            "parse_error": None,
            "raw_criteria_present": raw_present,
            "raw_criteria_char_len": raw_len,
            "section_layout": sec.section_layout,
            "placeholder_reason": sec.placeholder_reason,
            "has_unheaded_preamble": sec.has_unheaded_preamble,
            "is_multi_cohort": sec.is_multi_cohort,
            "n_sections": len(sec.sections),
            "n_clauses": len(sec.clauses),
            "n_inclusion_clauses": n_inc,
            "n_exclusion_clauses": n_exc,
            "n_unheaded_clauses": n_unh,
            "domain_statuses": domain_statuses,
            "domain_substatuses": domain_substatuses,
            "domains": domains_dict,
            "has_gilberts_exception": hep.has_gilberts_exception,
            "has_skin_or_insitu_exception": mal.has_skin_or_insitu_exception,
            "has_contraception_requirement": preg.condition_type == "contraception_required",
            "ecog_max_allowed": int(ecog_max) if ecog_max is not None else None,
            "text_min_age_years": disc.text_min_age_years,
            "text_max_age_years": disc.text_max_age_years,
            "hidden_upper_age_cap": disc.hidden_upper_age_cap,
            "hidden_lower_age_bound": disc.hidden_lower_age_bound,
            "conflicting_age_bounds": disc.conflicting_age_bounds,
            "std_ages_discordance": disc.std_ages_discordance,
            "sex_eligibility_discordance": disc.sex_eligibility_discordance,
            "healthy_volunteers_discordance": disc.healthy_volunteers_discordance,
            "any_structured_discordance": disc.any_discordance,
            "discordance": disc.to_dict(),
            "parser_version": PARSER_MODULE_VERSION,
            "vocabulary_version": VOCABULARY_VERSION,
            "vocabulary_sha256": VOCABULARY_SHA256,
        }
        for dom in DOMAINS:
            row[f"{dom}_status"] = domain_statuses[dom]
            row[f"{dom}_substatus"] = domain_substatuses[dom]
        return row
    except Exception as exc:  # noqa: BLE001 — deliberate quarantine barrier so no trial is dropped
        return parse_error_row(
            nct_id=nct_id,
            snapshot_date=snapshot_date,
            error=f"{type(exc).__name__}: {exc}",
        )


def lean_eligibility_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return compact per-trial projection omitting verbatim evidence spans."""
    out: dict[str, Any] = {}
    for col in LEAN_COLUMNS:
        val = row.get(col)
        if isinstance(val, dict):
            out[col] = dict(val)
        else:
            out[col] = val
    return out


def summarize_cohort_barriers(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate non-collapsing section layouts, 12-domain statuses, exceptions, and discordances."""
    row_list = list(rows)
    n_trials = len(row_list)

    parse_status_counts: dict[str, int] = {
        s: sum(1 for r in row_list if r.get("eligibility_parse_status") == s)
        for s in ELIGIBILITY_PARSE_STATUS_LEVELS
    }

    section_layout_counts: dict[str, int] = {
        lay: sum(
            1
            for r in row_list
            if r.get("eligibility_parse_status") != "parse_error" and r.get("section_layout") == lay
        )
        for lay in SECTION_LAYOUT_LEVELS
    }
    section_layout_counts["parse_error"] = parse_status_counts["parse_error"]

    domain_counts: dict[str, dict[str, Any]] = {}
    for dom in DOMAINS:
        st_col = f"{dom}_status"
        sub_col = f"{dom}_substatus"
        d_entry: dict[str, Any] = {}
        for st in DOMAIN_STATUS_LEVELS:
            d_entry[st] = sum(
                1
                for r in row_list
                if (r.get(st_col) or (r.get("domain_statuses") or {}).get(dom)) == st
            )
        d_entry["parse_error"] = sum(
            1 for r in row_list if r.get("eligibility_parse_status") == "parse_error"
        )
        d_entry["substatus_counts"] = {
            sub: sum(
                1
                for r in row_list
                if (r.get(sub_col) or (r.get("domain_substatuses") or {}).get(dom)) == sub
            )
            for sub in DOMAIN_SUBSTATUS_LEVELS
        }
        domain_counts[dom] = d_entry

    contraception_only_count = 0
    for r in row_list:
        if r.get("has_contraception_requirement") is True:
            contraception_only_count += 1
            continue
        doms = r.get("domains")
        if isinstance(doms, dict):
            preg = doms.get("pregnancy_or_lactation")
            if isinstance(preg, dict) and preg.get("condition_type") == "contraception_required":
                contraception_only_count += 1

    clinical_exception_counts: dict[str, int] = {
        "gilberts_syndrome_allowance": sum(1 for r in row_list if r.get("has_gilberts_exception") is True),
        "skin_or_insitu_malignancy_exception": sum(
            1 for r in row_list if r.get("has_skin_or_insitu_exception") is True
        ),
        "contraception_only_requirement": contraception_only_count,
    }

    discordance_counts: dict[str, dict[str, int]] = {}
    for flag in DISCORDANCE_FLAGS:
        discordance_counts[flag] = {
            "true": sum(1 for r in row_list if r.get(flag) is True),
            "false": sum(1 for r in row_list if r.get(flag) is False),
            "parse_error": sum(1 for r in row_list if r.get(flag) is None),
        }

    vocab_shas = sorted({
        str(r["vocabulary_sha256"])
        for r in row_list
        if r.get("vocabulary_sha256") is not None
    })

    return {
        "n_trials": n_trials,
        "parse_status_counts": parse_status_counts,
        "section_layout_counts": section_layout_counts,
        "domain_counts": domain_counts,
        "clinical_exception_counts": clinical_exception_counts,
        "discordance_counts": discordance_counts,
        "vocabulary_sha256_values": vocab_shas,
    }


def structural_checks(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Run invariant checks over a cohort of eligibility rows (mirrors `sex_gender_table.structural_checks`)."""
    row_list = list(rows)
    n = len(row_list)
    summary = summarize_cohort_barriers(row_list)
    checks: dict[str, dict[str, Any]] = {}

    def put(name: str, ok: bool, detail: str) -> None:
        checks[name] = {"pass": bool(ok), "detail": detail}

    ps_sum = sum(summary["parse_status_counts"].values())
    put("parse_status_counts_sum_to_n_trials", ps_sum == n, f"{ps_sum} == {n}")

    sl_sum = sum(summary["section_layout_counts"].values())
    put("section_layout_counts_sum_to_n_trials", sl_sum == n, f"{sl_sum} == {n}")

    bad_domain_sums: list[str] = []
    bad_substatus_sums: list[str] = []
    for dom in DOMAINS:
        dc = summary["domain_counts"][dom]
        tot = sum(dc[st] for st in DOMAIN_STATUS_LEVELS) + dc["parse_error"]
        if tot != n:
            bad_domain_sums.append(f"{dom}:{tot}!={n}")
        sub_tot = sum(dc["substatus_counts"].values())
        if sub_tot != dc["threshold_or_conditional"]:
            bad_substatus_sums.append(f"{dom}:{sub_tot}!={dc['threshold_or_conditional']}")

    put("every_domain_status_sums_to_n_trials", not bad_domain_sums, f"mismatches={bad_domain_sums}")
    put(
        "threshold_or_conditional_equals_substatus_sum",
        not bad_substatus_sums,
        f"mismatches={bad_substatus_sums}",
    )

    bad_parse_error_rows: list[str | None] = []
    for r in row_list:
        if r.get("eligibility_parse_status") == "parse_error":
            dom_st_map = r.get("domain_statuses") if isinstance(r.get("domain_statuses"), dict) else {}
            dom_sub_map = r.get("domain_substatuses") if isinstance(r.get("domain_substatuses"), dict) else {}
            if r.get("section_layout") is not None:
                bad_parse_error_rows.append(r.get("nct_id"))
            elif any(r.get(f"{d}_status") is not None or dom_st_map.get(d) is not None for d in DOMAINS):
                bad_parse_error_rows.append(r.get("nct_id"))
            elif any(r.get(f"{d}_substatus") is not None or dom_sub_map.get(d) is not None for d in DOMAINS):
                bad_parse_error_rows.append(r.get("nct_id"))
            elif any(r.get(fl) is not None for fl in DISCORDANCE_FLAGS):
                bad_parse_error_rows.append(r.get("nct_id"))
    put(
        "parse_error_rows_never_collapsed",
        not bad_parse_error_rows,
        f"{len(bad_parse_error_rows)} invalid parse_error rows",
    )

    shas = summary["vocabulary_sha256_values"]
    put(
        "single_pinned_vocabulary_sha256",
        len(shas) <= 1 and (not shas or shas == [VOCABULARY_SHA256]),
        f"shas={shas}",
    )
    return checks


def all_structural_checks_pass(checks: dict[str, dict[str, Any]]) -> bool:
    """Return True iff every check returned by `structural_checks` passed."""
    return all(bool(v.get("pass")) for v in checks.values())
