"""Stratified operational feasibility benchmarks and read-only curated sponsor attribution.

Stratifies trials across:
  - Indication: hierarchical `condition_ontology.json` category (`ontology_primary`,
    `ontology_secondary`) + optional ClinicalTrials.gov MeSH descriptors (`src/condition_mesh.py`).
  - Phase: canonical phase stratum (`PHASE1`, `PHASE2`, `PHASE2/PHASE3`, `PHASE3`, etc.).
  - Footprint tier: `single_site`, `2_to_10_sites`, `11_to_50_sites`, `51_plus_sites`,
    `no_listed_sites`.
  - Sponsor class & curated canonical company (`sponsors/` read-only attribution keeping
    `attributed`, `reviewed_excluded`, and `unreviewed` strictly disjoint, and deduplicating
    on `nct_id` for partnership and dual-role trials).

Percentile distributions use pure deterministic linear interpolation (zero RNG) and withhold
`p10..p90` (`None`) whenever either the stratum's `n_trials < min_cell_trials` OR the metric's
`n_eligible < min_cell_trials`, while always reporting exact `n_trials` and `n_eligible`.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from feasibility.metrics import (  # noqa: E402
    FACILITY_BASIS,
    FOOTPRINT_TIERS,
    RATE_BASIS,
    extract_trial_metrics,
)
from feasibility.termination import (  # noqa: E402
    STOPPED_STUDY_RESULTS_STATES,
    TERMINATION_CATEGORIES,
    evaluate_termination,
)
from src import condition_mesh  # noqa: E402

try:
    from sponsors import adapter as _sp_adapter  # noqa: E402
    from sponsors import audit_states as _sp_audit_states  # noqa: E402
    from sponsors import sponsor_roles as _sp_roles  # noqa: E402

    _HAS_PANDAS_SPONSORS = True
except ModuleNotFoundError:
    _sp_adapter = None  # type: ignore[assignment]
    _sp_audit_states = None  # type: ignore[assignment]
    _sp_roles = None  # type: ignore[assignment]
    _HAS_PANDAS_SPONSORS = False

try:
    from src import condition_classifier as _cond_cls  # noqa: E402

    _HAS_RAPIDFUZZ = True
except ModuleNotFoundError:
    _cond_cls = None  # type: ignore[assignment]
    _HAS_RAPIDFUZZ = False

DEFAULT_RULES_PATH = ROOT / "sponsors" / "company_aliases.csv"
ONTOLOGY_PATH = ROOT / "condition_ontology.json"
DEFAULT_MIN_CELL_TRIALS = 5

SPONSOR_REVIEW_STATES = ("attributed", "reviewed_excluded", "unreviewed")
ACTIVE_STATUSES = frozenset(
    {
        "NOT_YET_RECRUITING",
        "RECRUITING",
        "ENROLLING_BY_INVITATION",
        "ACTIVE_NOT_RECRUITING",
    }
)

_NCT_RE = re.compile(r"^NCT[0-9]{8}$")  # ASCII digits only: `\d` also matches other scripts' digits

# Exact stdlib mirror of sponsors/sponsor_roles.py & sponsors/adapter.py for environments
# where pandas is not installed (such as minimal stdlib+pytest sandboxes). Zero substring matching.
_AACT_AGENCY_CLASSES = frozenset(
    {"INDUSTRY", "OTHER", "NIH", "OTHER_GOV", "FED", "NETWORK", "UNKNOWN", "INDIV", "AMBIG"}
)
_VALID_RULE_TYPES = frozenset({"exact_literal", "exact_normalized", "subsidiary_of"})
_VALID_STATUS = frozenset({"attribute", "exclude"})
_VALID_SHARED = frozenset({"yes", "no"})
_SUFFIX_RE = re.compile(
    r"\s+(incorporated|inc|llc|llp|ltd|limited|gmbh|ag|plc|corp|corporation|"
    r"co|company|sa|nv|bv|ab|oy|kk|spa|sas)$"
)
_SUBSIDIARY_RE = re.compile(
    r"^(?P<sub>.+?),?\s+(?:is\s+)?(?:now\s+)?(?:a\s+)?(?:wholly[- ]owned\s+)?"
    r"subsidiar\w+\s+of\s+(?P<parent>.+)$",
    re.IGNORECASE,
)
_ONT_PUNCT_RE = re.compile(r"[^\w\s/-]")
# Ontology keywords of at most this many characters match only standing alone (see
# `_classify_conditions_FallbackOrFull`); the same rule as `src/condition_classifier.py`.
_SHORT_KEYWORD_MAX_LEN = 4
_ONTOLOGY_CACHE: (
    tuple[dict[str, tuple[str, str]], list[tuple[str, re.Pattern[str] | None]]] | None
) = None


def _normalize_sponsor_name(name: str) -> str:
    """Exact mirror of `sponsors.sponsor_roles.normalize`."""
    s = str(name).lower().strip()
    s = re.sub(r"[.,;:()\"']", " ", s)
    s = re.sub(r"&", " and ", s)
    s = re.sub(r"\s+", " ", s).strip()
    prev: str | None = None
    while prev != s:
        prev = s
        s = _SUFFIX_RE.sub("", s).strip()
        s = re.sub(r"\s+and$", "", s)
    return s


def _subsidiary_parts(name: str) -> tuple[str | None, str | None]:
    """Exact mirror of `sponsors.sponsor_roles._subsidiary_parts`."""
    m = _SUBSIDIARY_RE.match(str(name))
    if not m:
        return None, None
    entity = _normalize_sponsor_name(m.group("sub"))
    parent = m.group("parent")
    if _SUBSIDIARY_RE.match(parent):
        _, parent_norm = _subsidiary_parts(parent)
        return entity, parent_norm
    parent = re.sub(r"\(.*?\)\s*$", "", parent).strip(" ,.")
    return entity, _normalize_sponsor_name(parent)


def _stdlib_load_sponsor_rules(rules_path: Path) -> list[dict[str, str]]:
    """Exact validation and loading mirror of `sponsors.sponsor_roles.load_rules`."""
    with rules_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        cols = list(reader.fieldnames or [])
        missing = [c for c in ("canonical", "rule_type", "pattern") if c not in cols]
        if missing:
            raise ValueError(f"alias file missing columns: {missing}")
        rows: list[dict[str, str]] = []
        for raw in reader:
            canonical = (raw.get("canonical") or "").strip()
            rule_type = (raw.get("rule_type") or "").strip()
            pattern = raw.get("pattern") or ""
            status = (raw.get("status") or "").strip() or "attribute"
            shared = (raw.get("shared") or "").strip() or "no"
            note = raw.get("note") or ""
            if rule_type not in _VALID_RULE_TYPES:
                raise ValueError(f"unknown rule_type values: {{{rule_type!r}}}")
            if status not in _VALID_STATUS:
                raise ValueError(f"unknown status values: {{{status!r}}}")
            if shared not in _VALID_SHARED:
                raise ValueError(f"unknown shared values: {{{shared!r}}} (must be yes or no)")
            if not note.strip():
                raise ValueError(
                    "rule has an empty note. Every rule must record who decided and why "
                    f"(offender: {{'canonical': {canonical!r}, 'pattern': {pattern!r}}})"
                )
            rows.append(
                {
                    "canonical": canonical,
                    "rule_type": rule_type,
                    "pattern": pattern,
                    "status": status,
                    "shared": shared,
                    "note": note,
                }
            )
    return rows


def _attribute_sponsors(
    adapter_inputs: list[dict[str, Any]],
    rules_path: Path,
) -> tuple[
    dict[str, set[str]],
    dict[str, set[str]],
    dict[str, set[str]],
    dict[str, set[str]],
    dict[str, Any],
]:
    """Run read-only `sponsors/` attribution, returning `(literal_states, lead_by_nct,
    collab_by_nct, any_by_nct, adapter_log_dict)`.
    """
    if _HAS_PANDAS_SPONSORS and _sp_adapter is not None and _sp_roles is not None and _sp_audit_states is not None:
        rules = _sp_roles.load_rules(str(rules_path))
        frame, adapter_log = _sp_adapter.records_to_frame(adapter_inputs)
        literal_states = _sp_audit_states.partition(frame, rules)
        index = _sp_roles.build_index(frame, rules)

        lead_by_nct: dict[str, set[str]] = {}
        collab_by_nct: dict[str, set[str]] = {}
        any_by_nct: dict[str, set[str]] = {}
        for nct, canon, role in zip(index["nct_id"], index["canonical"], index["role"]):
            any_by_nct.setdefault(nct, set()).add(canon)
            if role == "lead":
                lead_by_nct.setdefault(nct, set()).add(canon)
            elif role == "collaborator":
                collab_by_nct.setdefault(nct, set()).add(canon)
        return literal_states, lead_by_nct, collab_by_nct, any_by_nct, adapter_log.to_dict()

    # Exact stdlib mirror of sponsors/adapter.py + sponsors/sponsor_roles.py + sponsors/audit_states.py.
    rules_list = _stdlib_load_sponsor_rules(rules_path)
    n_records = 0
    n_rows_emitted = 0
    n_dropped_empty_collab = 0
    n_unknown_lead = 0
    n_empty_lead = 0
    novel_classes: Counter[str] = Counter()
    frame_rows: list[tuple[str, str, str, str]] = []

    for rec in adapter_inputs:
        n_records += 1
        nct_id = str(rec.get("nct_id"))
        lead_cls = rec.get("sponsor_class")
        lead_name = rec.get("lead_sponsor_name")
        if lead_name == "Unknown":
            n_unknown_lead += 1
            lead_str = "Unknown"
        elif lead_name is None or str(lead_name).strip() == "":
            n_empty_lead += 1
            lead_str = "" if lead_name is None else str(lead_name)
        else:
            lead_str = str(lead_name)
        if lead_cls not in _AACT_AGENCY_CLASSES:
            novel_classes[str(lead_cls)] += 1
        frame_rows.append((nct_id, str(lead_cls), "lead", lead_str))

        for collab in rec.get("collaborators") or []:
            cname = collab.get("name")
            if cname is None or str(cname).strip() == "":
                n_dropped_empty_collab += 1
                continue
            ccls = collab.get("class")
            if ccls not in _AACT_AGENCY_CLASSES:
                novel_classes[str(ccls)] += 1
            frame_rows.append((nct_id, str(ccls), "collaborator", str(cname)))

    n_rows_emitted = len(frame_rows)
    universe = sorted({row[3] for row in frame_rows})
    norm_of = {lit: _normalize_sponsor_name(lit) for lit in universe}
    parent_of = {lit: _subsidiary_parts(lit)[1] for lit in universe}

    def _hits(rule: dict[str, str]) -> list[str]:
        rtype = rule["rule_type"]
        pat = rule["pattern"]
        if rtype == "exact_literal":
            return [lit for lit in universe if lit == pat]
        if rtype == "exact_normalized":
            return [lit for lit in universe if norm_of[lit] == pat]
        return [lit for lit in universe if parent_of[lit] is not None and parent_of[lit] == pat]

    lit_map: dict[str, list[tuple[str, str, str]]] = {}
    for rt in ("exact_literal", "exact_normalized", "subsidiary_of"):
        for r in rules_list:
            if r["status"] == "attribute" and r["rule_type"] == rt:
                for lit in _hits(r):
                    claims = lit_map.setdefault(lit, [])
                    if any(c == r["canonical"] for c, _, _ in claims):
                        continue
                    if claims and not (r["shared"] == "yes" and all(s == "yes" for _, _, s in claims)):
                        others = sorted({c for c, _, _ in claims})
                        raise ValueError(
                            f"conflicting rules for literal {lit!r}: {others} vs {r['canonical']!r} (via {rt})"
                        )
                    claims.append((r["canonical"], rt, r["shared"]))

    excluded_set: set[str] = set()
    for r in rules_list:
        if r["status"] == "exclude":
            for lit in _hits(r):
                if lit in lit_map:
                    claimed = sorted({c for c, _, _ in lit_map[lit]})
                    raise ValueError(
                        f"conflicting rules for literal {lit!r}: attributed to {claimed} "
                        f"but also excluded by rule ({r['canonical']!r}, {r['rule_type']}, {r['pattern']!r})"
                    )
                excluded_set.add(lit)

    universe_set = set(universe)
    attributed_set = set(lit_map) & universe_set
    excluded_set &= universe_set
    unreviewed_set = universe_set - attributed_set - excluded_set

    lead_by_nct = {}
    collab_by_nct = {}
    any_by_nct = {}
    for nct_id, _cls, role, lit in frame_rows:
        for canon, _rt, _sh in lit_map.get(lit, []):
            any_by_nct.setdefault(nct_id, set()).add(canon)
            if role == "lead":
                lead_by_nct.setdefault(nct_id, set()).add(canon)
            elif role == "collaborator":
                collab_by_nct.setdefault(nct_id, set()).add(canon)

    log_dict: dict[str, Any] = {
        "n_records": n_records,
        "n_rows_emitted": n_rows_emitted,
        "n_rows_dropped_empty_collaborator_name": n_dropped_empty_collab,
        "n_unknown_lead_names": n_unknown_lead,
        "n_empty_lead_names": n_empty_lead,
        "novel_agency_classes": dict(novel_classes),
    }
    return (
        {
            "attributed": attributed_set,
            "reviewed_excluded": excluded_set,
            "unreviewed": unreviewed_set,
        },
        lead_by_nct,
        collab_by_nct,
        any_by_nct,
        log_dict,
    )


def _classify_conditions_FallbackOrFull(conditions: list[str]) -> tuple[str, str]:
    """Classify conditions via `src.condition_classifier` when installed, else keyword lookup
    over `condition_ontology.json` (no fuzzy step).

    Fallback method, the same "short keywords stand alone" rule as `src/condition_classifier.py`:
    keywords are tried longest first and the first hit wins. A keyword of at most
    `_SHORT_KEYWORD_MAX_LEN` (4) characters (abbreviations such as `all`, `ra`, `als`, `burn`)
    matches only as a whole word or phrase with an optional plural `s`/`es`, so `Falls`, `Trauma`
    and `Caregiver Burnout` no longer read as ALL leukemia, rheumatoid arthritis or a burn wound.
    A longer keyword keeps the substring test, so compounds still classify (`Gliosarcoma` ->
    `sarcoma`, `Neoplasms` -> `neoplasm`). The threshold is the classifier's: its measurement on
    published data found that whole-word matching for every keyword loses such compounds, while
    nearly all wrong substring hits come from keywords of 4 characters or fewer.
    """
    global _ONTOLOGY_CACHE
    if _HAS_RAPIDFUZZ and _cond_cls is not None:
        info = _cond_cls.classify_conditions(conditions)
        return info["primary_condition"], info["secondary_condition"]

    if _ONTOLOGY_CACHE is None:
        doc = json.loads(ONTOLOGY_PATH.read_text(encoding="utf-8"))
        lookup: dict[str, tuple[str, str]] = {}
        for pri, secs in doc["categories"].items():
            for sec, kws in secs.items():
                for kw in kws:
                    lookup[kw.lower()] = (pri, sec)
        ordered = [
            (
                kw,
                re.compile(r"(?<!\w)" + re.escape(kw) + r"(?:s|es)?(?!\w)")
                if len(kw) <= _SHORT_KEYWORD_MAX_LEN
                else None,
            )
            for kw in sorted(lookup.keys(), key=len, reverse=True)
        ]
        _ONTOLOGY_CACHE = (lookup, ordered)

    if not conditions:
        return ("Other", "Uncategorized")
    lookup, ordered = _ONTOLOGY_CACHE
    pri_counts: dict[str, int] = {}
    pri_sec: dict[str, str] = {}
    for cond in conditions:
        norm = " ".join(_ONT_PUNCT_RE.sub(" ", cond.lower().strip()).split())
        matched_pair = ("Other", "Uncategorized")
        if norm:
            for kw, short_kw_re in ordered:
                hit = short_kw_re.search(norm) is not None if short_kw_re is not None else kw in norm
                if hit:
                    matched_pair = lookup[kw]
                    break
        pri, sec = matched_pair
        pri_counts[pri] = pri_counts.get(pri, 0) + 1
        if pri not in pri_sec:
            pri_sec[pri] = sec
    best_pri = max(pri_counts, key=lambda p: (pri_counts[p], p))
    return best_pri, pri_sec[best_pri]


def _quantile_linear(sorted_vals: list[float], q: float) -> float:
    """Deterministic type-7 continuous quantile over non-empty `sorted_vals`."""
    n = len(sorted_vals)
    if n == 1:
        return round(sorted_vals[0], 4)
    pos = q * (n - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    val = sorted_vals[lo] + (pos - lo) * (sorted_vals[hi] - sorted_vals[lo])
    return round(val, 4)


def compute_percentiles(
    values: Sequence[float],
    *,
    cell_n_trials: int,
    min_cell_trials: int,
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute `[p10, p25, p50, p75, p90]` or withhold below `min_cell_trials`."""
    n_eligible = len(values)
    n_excluded = cell_n_trials - n_eligible
    out: dict[str, Any] = {
        "n_trials": cell_n_trials,
        "n_eligible": n_eligible,
        "n_excluded": n_excluded,
        "min_cell_trials": min_cell_trials,
        "withheld": False,
        "withheld_reason": None,
        "p10": None,
        "p25": None,
        "p50": None,
        "p75": None,
        "p90": None,
    }
    if extra_meta:
        out.update(extra_meta)

    if cell_n_trials < min_cell_trials:
        out["withheld"] = True
        out["withheld_reason"] = f"cell_trials_below_min ({cell_n_trials} < {min_cell_trials})"
        return out
    if n_eligible < min_cell_trials:
        out["withheld"] = True
        out["withheld_reason"] = f"eligible_trials_below_min ({n_eligible} < {min_cell_trials})"
        return out

    xs = sorted(float(v) for v in values)
    out.update(
        p10=_quantile_linear(xs, 0.10),
        p25=_quantile_linear(xs, 0.25),
        p50=_quantile_linear(xs, 0.50),
        p75=_quantile_linear(xs, 0.75),
        p90=_quantile_linear(xs, 0.90),
    )
    return out


def summarize_cell(
    trials: Sequence[dict[str, Any]],
    *,
    min_cell_trials: int = DEFAULT_MIN_CELL_TRIALS,
) -> dict[str, Any]:
    """Aggregate operational outcomes and percentile distributions for one stratum cell."""
    n_trials = len(trials)
    n_completed = sum(1 for t in trials if t["overall_status"] == "COMPLETED")
    n_terminated = sum(1 for t in trials if t["overall_status"] == "TERMINATED")
    n_withdrawn = sum(1 for t in trials if t["overall_status"] == "WITHDRAWN")
    n_suspended = sum(1 for t in trials if t["overall_status"] == "SUSPENDED")
    n_stopped = n_terminated + n_withdrawn + n_suspended
    n_resolved = n_completed + n_terminated + n_withdrawn
    n_active = sum(1 for t in trials if t["overall_status"] in ACTIVE_STATUSES)
    n_other_status = n_trials - n_completed - n_stopped - n_active

    n_actual_enr = sum(1 for t in trials if t["enrollment"]["type"] == "ACTUAL")
    n_est_enr = sum(1 for t in trials if t["enrollment"]["type"] == "ESTIMATED")
    n_missing_enr = sum(1 for t in trials if t["enrollment"]["type"] == "MISSING_TYPE")

    prim_durs = [
        t["durations"]["primary_completion"]
        for t in trials
        if t["durations"]["primary_completion"]["eligible_for_realized_duration"]
    ]
    study_durs = [
        t["durations"]["study_completion"]
        for t in trials
        if t["durations"]["study_completion"]["eligible_for_realized_duration"]
    ]
    accrual_rows = [t["accrual"] for t in trials if t["accrual"]["rate_status"] == "computed"]
    facility_vals = [
        float(t["accrual"]["actual_participants_per_listed_facility"])
        for t in trials
        if t["accrual"]["facility_metric_status"] == "computed"
        and t["accrual"]["actual_participants_per_listed_facility"] is not None
    ]
    retention_vals = [
        float(t["retention"]["period1_completion_rate"])
        for t in trials
        if t["retention"]["completion_rate_status"] == "computed"
        and t["retention"]["period1_completion_rate"] is not None
    ]

    prim_days_pct = compute_percentiles(
        [float(d["midpoint_days"]) for d in prim_durs],
        cell_n_trials=n_trials,
        min_cell_trials=min_cell_trials,
        extra_meta={
            "n_exact_days": sum(1 for d in prim_durs if d["status"] == "exact_days"),
            "n_bounded_partial": sum(1 for d in prim_durs if d["status"] == "bounded_partial_precision"),
        },
    )
    prim_months_pct = compute_percentiles(
        [float(d["midpoint_months"]) for d in prim_durs],
        cell_n_trials=n_trials,
        min_cell_trials=min_cell_trials,
    )
    if not prim_months_pct["withheld"]:
        prim_months_pct["min_months_p50"] = _quantile_linear(
            sorted(float(d["min_months"]) for d in prim_durs), 0.50
        )
        prim_months_pct["max_months_p50"] = _quantile_linear(
            sorted(float(d["max_months"]) for d in prim_durs), 0.50
        )
    else:
        prim_months_pct["min_months_p50"] = None
        prim_months_pct["max_months_p50"] = None

    study_months_pct = compute_percentiles(
        [float(d["midpoint_months"]) for d in study_durs],
        cell_n_trials=n_trials,
        min_cell_trials=min_cell_trials,
    )

    accrual_pct = compute_percentiles(
        [float(a["actual_participants_per_month"]) for a in accrual_rows],
        cell_n_trials=n_trials,
        min_cell_trials=min_cell_trials,
        extra_meta={"rate_basis": RATE_BASIS},
    )
    if not accrual_pct["withheld"]:
        accrual_pct["min_rate_p50"] = _quantile_linear(
            sorted(float(a["min_rate"]) for a in accrual_rows), 0.50
        )
        accrual_pct["max_rate_p50"] = _quantile_linear(
            sorted(float(a["max_rate"]) for a in accrual_rows), 0.50
        )
    else:
        accrual_pct["min_rate_p50"] = None
        accrual_pct["max_rate_p50"] = None

    facility_pct = compute_percentiles(
        facility_vals,
        cell_n_trials=n_trials,
        min_cell_trials=min_cell_trials,
        extra_meta={"facility_basis": FACILITY_BASIS},
    )

    retention_pct = compute_percentiles(
        retention_vals,
        cell_n_trials=n_trials,
        min_cell_trials=min_cell_trials,
    )

    stopped_trials = [t for t in trials if t["termination"]["is_stopped_study"]]
    term_cat_counts = Counter(t["termination"]["category"] for t in stopped_trials)
    stopped_res_counts = Counter(t["termination"]["stopped_study_results_state"] for t in stopped_trials)

    return {
        "n_trials": n_trials,
        "status_counts": {
            "completed": n_completed,
            "terminated": n_terminated,
            "withdrawn": n_withdrawn,
            "suspended": n_suspended,
            "stopped_total": n_stopped,
            "resolved_terminal_total": n_resolved,
            "active_or_recruiting": n_active,
            "other_or_unknown": n_other_status,
        },
        "outcome_rates": {
            "completion_share_of_all": round(n_completed / n_trials, 6) if n_trials > 0 else None,
            "termination_or_withdrawal_share_of_all": (
                round((n_terminated + n_withdrawn) / n_trials, 6) if n_trials > 0 else None
            ),
            "stopped_share_of_all": round(n_stopped / n_trials, 6) if n_trials > 0 else None,
            "completion_rate_of_resolved": round(n_completed / n_resolved, 6) if n_resolved > 0 else None,
            "termination_rate_of_resolved": round(n_terminated / n_resolved, 6) if n_resolved > 0 else None,
            "withdrawal_rate_of_resolved": round(n_withdrawn / n_resolved, 6) if n_resolved > 0 else None,
            "early_stop_rate_of_resolved": (
                round((n_terminated + n_withdrawn) / n_resolved, 6) if n_resolved > 0 else None
            ),
        },
        "enrollment_type_counts": {
            "ACTUAL": n_actual_enr,
            "ESTIMATED": n_est_enr,
            "MISSING_TYPE": n_missing_enr,
        },
        "realized_primary_duration_days": prim_days_pct,
        "realized_primary_duration_months": prim_months_pct,
        "realized_study_duration_months": study_months_pct,
        "actual_participants_per_month": accrual_pct,
        "actual_participants_per_listed_facility": facility_pct,
        "period1_completion_rate": retention_pct,
        "termination_categories": {cat: term_cat_counts.get(cat, 0) for cat in TERMINATION_CATEGORIES},
        "stopped_study_results_states": {
            st: stopped_res_counts.get(st, 0) for st in STOPPED_STUDY_RESULTS_STATES
        },
    }


def _resolve_indication(
    study: dict[str, Any],
    conditions: list[str],
    nct_id: str,
    condition_mesh_by_nct: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    pri = study.get("primary_condition")
    sec = study.get("secondary_condition")
    if isinstance(pri, str) and pri.strip() and isinstance(sec, str) and sec.strip():
        ont_primary = pri.strip()
        ont_secondary = sec.strip()
    else:
        ont_primary, ont_secondary = _classify_conditions_FallbackOrFull(conditions)

    if condition_mesh_by_nct is not None and nct_id in condition_mesh_by_nct:
        cm_line = condition_mesh_by_nct[nct_id]
        mesh_info = {
            "state": cm_line.get("state"),
            "meshes": cm_line.get("meshes"),
            "ancestors": cm_line.get("ancestors"),
        }
        if "problem" in cm_line:
            mesh_info["problem"] = cm_line["problem"]
    elif "derivedSection" in study or isinstance(study.get("protocolSection"), dict):
        cm_line = condition_mesh.select_condition_mesh(study)
        mesh_info = {
            "state": cm_line["state"],
            "meshes": cm_line["meshes"],
            "ancestors": cm_line["ancestors"],
        }
        if "problem" in cm_line:
            mesh_info["problem"] = cm_line["problem"]
    else:
        mesh_info = {
            "state": "not_provided",
            "meshes": None,
            "ancestors": None,
        }

    return {
        "ontology_primary": ont_primary,
        "ontology_secondary": ont_secondary,
        "condition_mesh": mesh_info,
    }


def build_trial_records(
    studies: Sequence[dict[str, Any]],
    *,
    reviews: dict[str, dict[str, str]],
    rules_path: Path = DEFAULT_RULES_PATH,
    condition_mesh_by_nct: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Build per-trial feasibility records, sponsor attribution metadata, and review accounting."""
    extracted_by_nct: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for idx, study in enumerate(studies):
        if not isinstance(study, dict):
            raise ValueError(f"study[{idx}]: expected dict study record")
        metrics = extract_trial_metrics(study)
        nct_id = metrics["nct_id"]
        if not isinstance(nct_id, str) or not _NCT_RE.fullmatch(nct_id):
            raise ValueError(f"study[{idx}]: missing or invalid NCT id {nct_id!r}")
        if nct_id in extracted_by_nct:
            raise ValueError(f"{nct_id}: duplicate study record in input")
        extracted_by_nct[nct_id] = (study, metrics)

    ordered_ncts = sorted(extracted_by_nct)

    adapter_inputs = [
        {
            "nct_id": nct_id,
            "sponsor_class": (
                extracted_by_nct[nct_id][1]["raw_sponsor_class"]
                if extracted_by_nct[nct_id][1]["raw_sponsor_class"] is not None
                else "Unknown"
            ),
            "lead_sponsor_name": (
                extracted_by_nct[nct_id][1]["lead_sponsor_name"]
                if extracted_by_nct[nct_id][1]["lead_sponsor_name"] is not None
                else "Unknown"
            ),
            "collaborators": extracted_by_nct[nct_id][1]["collaborators"],
        }
        for nct_id in ordered_ncts
    ]
    rules_sha256 = hashlib.sha256(rules_path.read_bytes()).hexdigest()
    literal_states, lead_canon_by_nct, collab_canon_by_nct, any_canon_by_nct, adapter_log_dict = (
        _attribute_sponsors(adapter_inputs, rules_path)
    )

    lead_state_counts: Counter[str] = Counter()
    applied_review_ids: list[str] = []
    orphaned_reviews: list[dict[str, str]] = []

    trial_records: list[dict[str, Any]] = []
    for adapter_rec in adapter_inputs:
        nct_id = adapter_rec["nct_id"]
        study, m = extracted_by_nct[nct_id]
        lead_lit = adapter_rec["lead_sponsor_name"]
        if lead_lit in literal_states["attributed"]:
            rev_state = "attributed"
        elif lead_lit in literal_states["reviewed_excluded"]:
            rev_state = "reviewed_excluded"
        else:
            rev_state = "unreviewed"
        lead_state_counts[rev_state] += 1

        rev_row = reviews.get(nct_id)
        term = evaluate_termination(study, m["overall_status"], m["why_stopped"], rev_row)
        if rev_row is not None:
            if term["is_stopped_study"]:
                applied_review_ids.append(nct_id)
            else:
                orphaned_reviews.append(
                    {"nct_id": nct_id, "reason": f"trial_status_not_stopped ({m['overall_status']})"}
                )

        indication = _resolve_indication(study, m["conditions"], nct_id, condition_mesh_by_nct)

        trial_records.append(
            {
                "nct_id": nct_id,
                "brief_title": m["brief_title"],
                "study_type": m["study_type"],
                "phase": m["phase"],
                "overall_status": m["overall_status"],
                "conditions": m["conditions"],
                "indication": indication,
                "sponsor": {
                    "lead_literal": lead_lit,
                    "lead_sponsor_class": m["lead_sponsor_class"],
                    "review_state": rev_state,
                    "lead_canonical_companies": sorted(lead_canon_by_nct.get(nct_id, set())),
                    "collaborator_canonical_companies": sorted(collab_canon_by_nct.get(nct_id, set())),
                    "any_role_canonical_companies": sorted(any_canon_by_nct.get(nct_id, set())),
                },
                "facilities": m["facilities"],
                "enrollment": m["enrollment"],
                "durations": m["durations"],
                "accrual": m["accrual"],
                "retention": m["retention"],
                "termination": term,
            }
        )

    cohort_set = set(ordered_ncts)
    for rev_nct in sorted(set(reviews) - cohort_set):
        orphaned_reviews.append({"nct_id": rev_nct, "reason": "trial_not_in_cohort"})
    orphaned_reviews.sort(key=lambda r: r["nct_id"])

    sponsor_meta = {
        "method": "curated sponsor rules (sponsors/company_aliases.csv, read-only)",
        "rules_sha256": rules_sha256,
        "lead_review_state_counts": {st: lead_state_counts.get(st, 0) for st in SPONSOR_REVIEW_STATES},
        "distinct_literal_review_state_counts": {
            st: len(literal_states[st]) for st in SPONSOR_REVIEW_STATES
        },
        "adapter_log": adapter_log_dict,
    }
    review_meta = {
        "n_rows": len(reviews),
        "n_applied": len(applied_review_ids),
        "applied_nct_ids": sorted(applied_review_ids),
        "orphaned_reviews": orphaned_reviews,
    }
    return trial_records, sponsor_meta, review_meta


def build_strata_and_sponsors(
    trials: Sequence[dict[str, Any]],
    *,
    sponsor_meta: dict[str, Any],
    min_cell_trials: int = DEFAULT_MIN_CELL_TRIALS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build `strata.json` and `sponsors.json` benchmark payloads."""
    by_indication: dict[str, list[dict[str, Any]]] = {}
    by_phase: dict[str, list[dict[str, Any]]] = {}
    by_tier: dict[str, list[dict[str, Any]]] = {t: [] for t in FOOTPRINT_TIERS}
    by_cross: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    by_mesh: dict[tuple[str, str], list[dict[str, Any]]] = {}

    by_sponsor_class: dict[str, list[dict[str, Any]]] = {}
    by_lead_state: dict[str, list[dict[str, Any]]] = {st: [] for st in SPONSOR_REVIEW_STATES}
    by_company_lead: dict[str, list[dict[str, Any]]] = {}
    by_company_any: dict[str, list[dict[str, Any]]] = {}
    unreviewed_lits: Counter[str] = Counter()
    excluded_lits: Counter[str] = Counter()

    for t in trials:
        ind = t["indication"]["ontology_primary"]
        phase = t["phase"]
        tier = t["facilities"]["footprint_tier"]
        sclass = t["sponsor"]["lead_sponsor_class"]
        rstate = t["sponsor"]["review_state"]

        by_indication.setdefault(ind, []).append(t)
        by_phase.setdefault(phase, []).append(t)
        by_tier.setdefault(tier, []).append(t)
        by_cross.setdefault((ind, phase, tier, sclass), []).append(t)

        cm = t["indication"]["condition_mesh"]
        if cm["state"] == "indexed" and isinstance(cm["meshes"], list):
            seen_mesh_ids: set[str] = set()
            for entry in cm["meshes"]:
                mid, mterm = entry["id"], entry["term"]
                if mid not in seen_mesh_ids:
                    seen_mesh_ids.add(mid)
                    by_mesh.setdefault((mid, mterm), []).append(t)

        by_sponsor_class.setdefault(sclass, []).append(t)
        by_lead_state[rstate].append(t)

        for comp in t["sponsor"]["lead_canonical_companies"]:
            by_company_lead.setdefault(comp, []).append(t)
        for comp in t["sponsor"]["any_role_canonical_companies"]:
            by_company_any.setdefault(comp, []).append(t)

        if rstate == "unreviewed":
            unreviewed_lits[t["sponsor"]["lead_literal"]] += 1
        elif rstate == "reviewed_excluded":
            excluded_lits[t["sponsor"]["lead_literal"]] += 1

    strata_payload: dict[str, Any] = {
        "min_cell_trials": min_cell_trials,
        "overall": summarize_cell(trials, min_cell_trials=min_cell_trials),
        "by_indication": [
            {
                "ontology_primary": ind,
                **summarize_cell(rows, min_cell_trials=min_cell_trials),
            }
            for ind, rows in sorted(by_indication.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ],
        "by_phase": [
            {
                "phase": ph,
                **summarize_cell(rows, min_cell_trials=min_cell_trials),
            }
            for ph, rows in sorted(by_phase.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ],
        "by_footprint_tier": [
            {
                "footprint_tier": tier,
                **summarize_cell(by_tier[tier], min_cell_trials=min_cell_trials),
            }
            for tier in FOOTPRINT_TIERS
        ],
        "by_mesh_descriptor": [
            {
                "mesh_id": mid,
                "mesh_term": mterm,
                **summarize_cell(rows, min_cell_trials=min_cell_trials),
            }
            for (mid, mterm), rows in sorted(by_mesh.items(), key=lambda kv: (-len(kv[1]), kv[0][0]))
        ],
        "cross_strata": [
            {
                "ontology_primary": ind,
                "phase": ph,
                "footprint_tier": tier,
                "lead_sponsor_class": sclass,
                **summarize_cell(rows, min_cell_trials=min_cell_trials),
            }
            for (ind, ph, tier, sclass), rows in sorted(
                by_cross.items(),
                key=lambda kv: (-len(kv[1]), kv[0]),
            )
        ],
    }

    sponsors_payload: dict[str, Any] = {
        "min_cell_trials": min_cell_trials,
        "attribution": sponsor_meta,
        "by_sponsor_class": [
            {
                "lead_sponsor_class": sclass,
                **summarize_cell(rows, min_cell_trials=min_cell_trials),
            }
            for sclass, rows in sorted(by_sponsor_class.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ],
        "by_lead_review_state": [
            {
                "review_state": st,
                **summarize_cell(by_lead_state[st], min_cell_trials=min_cell_trials),
            }
            for st in SPONSOR_REVIEW_STATES
        ],
        "by_canonical_company_lead": [
            {
                "canonical_company": comp,
                "role_scope": "lead",
                **summarize_cell(rows, min_cell_trials=min_cell_trials),
            }
            for comp, rows in sorted(by_company_lead.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ],
        "by_canonical_company_any_role": [
            {
                "canonical_company": comp,
                "role_scope": "any",
                **summarize_cell(rows, min_cell_trials=min_cell_trials),
            }
            for comp, rows in sorted(by_company_any.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ],
        "unreviewed_lead_literals": [
            {"literal_name": lit, "n_trials": cnt, "review_state": "unreviewed"}
            for lit, cnt in sorted(unreviewed_lits.items(), key=lambda kv: (-kv[1], kv[0]))[:100]
        ],
        "reviewed_excluded_lead_literals": [
            {"literal_name": lit, "n_trials": cnt, "review_state": "reviewed_excluded"}
            for lit, cnt in sorted(excluded_lits.items(), key=lambda kv: (-kv[1], kv[0]))[:100]
        ],
    }

    return strata_payload, sponsors_payload
