"""Versioned deterministic `whyStopped` signal classifier and stopped-study results-state logic.

Applies to `TERMINATED`, `WITHDRAWN`, and `SUSPENDED` studies (`STOPPED_STATUSES`).

Design invariants:
1. Multi-signal preservation: Every `whyStopped` string is tested against all seven signal
   vocabularies (`SIGNAL_CATEGORIES`) with explicit negation guards (so phrases such as
   `"There were no safety concerns"`, `"not due to COVID-19"`, or `"Recruitment was fully completed"`
   never fire false positives). When two or more distinct signal categories match, the classifier
   assigns `"multiple_signals_detected"` and stores the full ordered `matched_signals` list — it
   NEVER silently picks whichever regex happened to run first.
2. Curated human-review overrides: Reads `feasibility/termination_reviews.csv` (header-only until
   a human reviewer records an override; NEVER written by code). Overrides set
   `classification_source = "human_review_override"` while keeping `deterministic_category` and
   `matched_signals` beside the override for auditability. Private reviewer fields (`reviewer`,
   `notes`) remain in the CSV and are never copied into published artifacts.
3. Stopped-study results state: Distinguishes genuinely analysed primary outcomes
   (`"results_with_analysed_primary"`) from empty results shells where every primary denominator is
   zero (`"empty_results_shell_zero_analysed"`), posted results lacking a numeric primary
   denominator (`"results_posted_no_primary_denom"`), and unposted results (`"results_not_posted"`).
"""
from __future__ import annotations

import csv
import re
from datetime import date
from pathlib import Path
from typing import Any

from feasibility.metrics import is_num, parse_num

TERMINATION_RULES_VERSION = "1.0.0"

STOPPED_STATUSES = ("TERMINATED", "WITHDRAWN", "SUSPENDED")

SIGNAL_CATEGORIES = (
    "accrual_or_recruitment",
    "futility_or_lack_of_efficacy",
    "safety_or_toxicity",
    "business_or_strategic",
    "covid_or_pandemic",
    "regulatory_or_clinical_hold",
    "supply_or_drug_availability",
)

TERMINATION_CATEGORIES = SIGNAL_CATEGORIES + (
    "multiple_signals_detected",
    "other_unclassified_text",
    "missing_why_stopped",
)

CLASSIFICATION_SOURCES = ("deterministic_rule", "human_review_override")

STOPPED_STUDY_RESULTS_STATES = (
    "results_with_analysed_primary",
    "empty_results_shell_zero_analysed",
    "results_posted_no_primary_denom",
    "results_not_posted",
)

REVIEW_COLUMNS = ("nct_id", "reviewer", "reviewed_at", "override_category", "notes")
PRIVATE_REVIEW_KEYS = frozenset({"reviewer", "notes"})

_NCT_RE = re.compile(r"^NCT\d{8}$")
_I = re.IGNORECASE

# Clauses inside `whyStopped` that explicitly negate a signal category are masked before matching
# that category, preventing false positives on real registry text such as
# "Terminated for strategic reasons; there were no safety concerns."
_NEGATION_PATTERNS: dict[str, re.Pattern[str]] = {
    "accrual_or_recruitment": re.compile(
        r"\b(?:recruitment|enrol+ment|accrual)\s+was\s+(?:fully\s+|already\s+)?completed\b"
        r"|\b(?:not|no|neither|nor|unrelated\s+to)\b[^.;,]{0,35}\b(?:accrual|enrol+ment|recruitment)\b",
        _I,
    ),
    "futility_or_lack_of_efficacy": re.compile(
        r"\b(?:not|no|neither|nor|without|unrelated\s+to)\b[^.;,]{0,35}\b(?:futil(?:e|ity)|lack\s+of\s+efficacy|efficacy\s+concern)\b",
        _I,
    ),
    "safety_or_toxicity": re.compile(
        r"\b(?:there\s+(?:were|was|are|is)\s+no|no|not|neither|nor|without|unrelated\s+to|free\s+of|absence\s+of|non[- ])\b"
        r"[^.;,]{0,40}\b(?:safety|toxicit(?:y|ies)|tolerability|adverse\s+events?|saes?|dlts?)\b",
        _I,
    ),
    "business_or_strategic": re.compile(
        r"\b(?:not|non[- ]|unrelated\s+to)\b[^.;,]{0,30}\b(?:business|commercial|financial|strategic)\b",
        _I,
    ),
    "covid_or_pandemic": re.compile(
        r"\b(?:not|non[- ]|unrelated\s+to|prior\s+to)\b[^.;,]{0,35}\b(?:covid(?:[- ]?19)?|sars[- ]cov[- ]2|pandemic|coronavirus)\b",
        _I,
    ),
    "regulatory_or_clinical_hold": re.compile(
        r"\b(?:no|not|without|lifted)\b[^.;,]{0,30}\b(?:clinical\s+hold|regulatory\s+hold)\b",
        _I,
    ),
    "supply_or_drug_availability": re.compile(
        r"\b(?:no|not|without|unrelated\s+to)\b[^.;,]{0,35}\b(?:supply|shortage|drug\s+availability)\b",
        _I,
    ),
}

_SIGNAL_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "accrual_or_recruitment": (
        re.compile(
            r"\b(?:slow|low|poor|insufficient|inadequate|difficult(?:y|ies)?|challeng(?:e|es|ing)|"
            r"suboptimal|lack\s+of|limited|delayed|declining|halted|stopped|unable)\b"
            r"[^.;]{0,35}\b(?:accrual|enrol+ment|enrol+ing|recruitment|recruiting)\b",
            _I,
        ),
        re.compile(
            r"\b(?:accrual|enrol+ment|recruitment)\b[^.;]{0,25}"
            r"\b(?:slow|low|poor|insufficient|inadequate|difficult(?:y|ies)?|challeng(?:e|es|ing)|"
            r"rate\s+was\s+low|below\s+target|did\s+not\s+meet|failed|problem|issue|barrier|feasibility)\b",
            _I,
        ),
        re.compile(
            r"\b(?:inability|unable|failure|failed)\s+to\s+(?:recruit|enrol+|accrue)\b"
            r"|\b(?:no|zero|0|few)\s+(?:patients?|subjects?|participants?)\s+(?:were\s+)?(?:enrol+ed|recruited|accrued|screened)\b"
            r"|\bhigh\s+screen(?:ing)?\s+failure\b",
            _I,
        ),
    ),
    "futility_or_lack_of_efficacy": (
        re.compile(r"\bfutil(?:e|ity)\b", _I),
        re.compile(
            r"\b(?:lack|absence|insufficient|inadequate|limited|modest|suboptimal|low)\s+"
            r"(?:of\s+)?(?:clinical\s+|antitumor\s+|therapeutic\s+)?(?:efficacy|benefit|activity|effect|response)\b",
            _I,
        ),
        re.compile(
            r"\b(?:did\s+not|failed\s+to|failure\s+to|unlikely\s+to|low\s+probability\s+(?:of|to))\s+"
            r"(?:meet|achieve|demonstrate|reach|show)\b[^.;]{0,45}"
            r"\b(?:primary|efficacy|clinical|prespecified|pre-specified|target)\s*"
            r"(?:endpoint|outcome|objective|criteria|response|benefit)?\b",
            _I,
        ),
        re.compile(r"\bunfavorable\s+(?:risk[- /]benefit|benefit[- /]risk)\s*(?:ratio|profile|balance)?\b", _I),
    ),
    "safety_or_toxicity": (
        re.compile(
            r"\b(?:serious\s+)?adverse\s+(?:drug\s+)?(?:events?|reactions?|effects?)\b"
            r"|\bdose[- ]limiting\s+toxicit(?:y|ies)\b"
            r"|\b(?:unacceptable|excessive|severe|unexpected|treatment[- ]related|drug[- ]related|limited)\s+"
            r"(?:toxicit(?:y|ies)|tolerability)\b"
            r"|\btoxicit(?:y|ies)\b"
            r"|\bintolerab(?:le|ility)\b"
            r"|\blimited\s+tolerability\b",
            _I,
        ),
        re.compile(
            r"\bsafety\s+(?:concerns?|signals?|issues?|findings?|risks?|reasons?|events?|review|board|committee|data)\b"
            r"|\b(?:due\s+to|for|because\s+of|owing\s+to|based\s+on)\s+"
            r"(?:a\s+|an\s+|patient\s+|subject\s+|participant\s+)?safety\b"
            r"|\b(?:hepatotoxicit|cardiotoxicit|neurotoxicit|nephrotoxicit)\w*\b"
            r"|\btreatment[- ]related\s+deaths?\b",
            _I,
        ),
    ),
    "business_or_strategic": (
        re.compile(
            r"\b(?:business|commercial|strategic|corporate|portfolio|reprioriti[sz]\w*|prioriti[sz]ation)\b"
            r"|\b(?:financial|funding|budget(?:ary)?)\s*(?:constraints?|limitations?|reasons?|issues?|cuts?|withdrawal|loss|lack)?\b"
            r"|\b(?:lack|loss|withdrawal|termination|end)\s+of\s+(?:funding|financial\s+support|grant|sponsorship)\b",
            _I,
        ),
        re.compile(
            r"\b(?:refocus(?:ed|ing)?|shift(?:ed|ing)?|change\s+in)\s+(?:clinical\s+|company\s+|sponsor\s+)?(?:development|strategy|priorit\w*)\b"
            r"|\b(?:development\s+program|clinical\s+development)\s+(?:was\s+)?(?:terminated|discontinued|halted|stopped)\b"
            r"|\bsponsor(?:'s)?\s+(?:administrative\s+)?decision\b"
            r"|\badministrative\s+(?:decision|reasons?)\b",
            _I,
        ),
    ),
    "covid_or_pandemic": (
        re.compile(
            r"\bcovid(?:[- ]?19)?\b|\bsars[- ]cov[- ]2\b|\bcoronavirus\b|\bpandemic\b",
            _I,
        ),
    ),
    "regulatory_or_clinical_hold": (
        re.compile(
            r"\bclinical\s+hold\b"
            r"|\bregulatory\s+(?:hold|authority|agency|decision|request|directive|feedback|requirement|non[- ]approval)\b"
            r"|\b(?:fda|ema|mhra|pmda|health\s+canada|competent\s+authority)\s+"
            r"(?:placed|imposed|hold|requested|ordered|directive|feedback)\b"
            r"|\b(?:irb|iec|ethics\s+committee)\s+(?:suspended|terminated|halted|withdrew|disapproved)\b",
            _I,
        ),
    ),
    "supply_or_drug_availability": (
        re.compile(
            r"\b(?:study\s+drug|drug|investigational\s+(?:medicinal\s+)?product|medication|vaccine|device|placebo|compound|imp)\s+"
            r"(?:supply|shortage|unavailab\w*|availability|expir(?:ation|y|ed)|stability\s+issue)\b"
            r"|\b(?:shortage|unavailability|lack\s+of\s+supply|supply\s+(?:issue|problem|chain|constraint|shortage))\b"
            r"|\bmanufacturing\s+(?:issue|problem|delay|defect|failure|hold|constraint)s?\b"
            r"|\bunavailable\s+(?:study\s+)?(?:drug|medication|investigational\s+product|device)\b",
            _I,
        ),
    ),
}


def _validate_review_date(value: str, nct_id: str) -> str:
    text = value.strip()
    date_part = text[:10]
    try:
        parsed = date.fromisoformat(date_part)
    except ValueError as exc:
        raise ValueError(f"termination_reviews.csv ({nct_id}): invalid reviewed_at date {value!r}") from exc
    if parsed.isoformat() != date_part:
        raise ValueError(f"termination_reviews.csv ({nct_id}): reviewed_at must begin with YYYY-MM-DD: {value!r}")
    return text


def load_termination_reviews(path: Path) -> dict[str, dict[str, str]]:
    """Read curated human review overrides from `feasibility/termination_reviews.csv`.

    Never writes or modifies the CSV. Raises `ValueError` on malformed headers, invalid NCT IDs,
    blank reviewer/notes, invalid dates, unknown override categories, or duplicate `nct_id` rows.
    """
    if not path.is_file():
        raise ValueError(f"{path}: termination reviews CSV not found")

    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if tuple(reader.fieldnames or ()) != REVIEW_COLUMNS:
            raise ValueError(
                f"{path.name}: expected exact header {list(REVIEW_COLUMNS)}, got {reader.fieldnames!r}"
            )
        out: dict[str, dict[str, str]] = {}
        for row_idx, row in enumerate(reader, start=2):
            if any(v is None for v in row.values()) or None in row:
                raise ValueError(f"{path.name}:{row_idx}: malformed CSV column count")
            nct_id = row["nct_id"].strip()
            reviewer = row["reviewer"].strip()
            reviewed_at = row["reviewed_at"].strip()
            override_cat = row["override_category"].strip()
            notes = row["notes"].strip()

            if not _NCT_RE.fullmatch(nct_id):
                raise ValueError(f"{path.name}:{row_idx}: invalid NCT id {nct_id!r}")
            if nct_id in out:
                raise ValueError(f"{path.name}:{row_idx}: duplicate review override for {nct_id}")
            if not reviewer:
                raise ValueError(f"{path.name}:{row_idx} ({nct_id}): reviewer must be non-empty")
            validated_date = _validate_review_date(reviewed_at, nct_id)
            if override_cat not in TERMINATION_CATEGORIES:
                raise ValueError(
                    f"{path.name}:{row_idx} ({nct_id}): unknown override_category {override_cat!r}"
                )
            if not notes:
                raise ValueError(f"{path.name}:{row_idx} ({nct_id}): notes must be non-empty")

            out[nct_id] = {
                "nct_id": nct_id,
                "reviewer": reviewer,
                "reviewed_at": validated_date,
                "override_category": override_cat,
                "notes": notes,
            }
    return out


def classify_why_stopped(why_stopped: str | None) -> dict[str, Any]:
    """Deterministically classify a stopped study's `whyStopped` text under versioned rules."""
    if why_stopped is None or not why_stopped.strip():
        return {
            "deterministic_category": "missing_why_stopped",
            "matched_signals": [],
            "matched_excerpts": {},
        }

    matched: list[str] = []
    excerpts: dict[str, str] = {}

    for cat in SIGNAL_CATEGORIES:
        neg_rx = _NEGATION_PATTERNS.get(cat)
        # Replace negated clauses with spaces of equal length so non-negated text elsewhere in the
        # same `whyStopped` sentence still matches at its original character offsets.
        cleaned = neg_rx.sub(lambda m: " " * len(m.group(0)), why_stopped) if neg_rx else why_stopped
        for pattern in _SIGNAL_PATTERNS[cat]:
            hit = pattern.search(cleaned)
            if hit is not None:
                matched.append(cat)
                excerpts[cat] = why_stopped[hit.start() : hit.end()].strip()
                break

    if not matched:
        det_cat = "other_unclassified_text"
    elif len(matched) == 1:
        det_cat = matched[0]
    else:
        det_cat = "multiple_signals_detected"

    return {
        "deterministic_category": det_cat,
        "matched_signals": matched,
        "matched_excerpts": excerpts,
    }


def _participants_denom_row(denoms: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the Participants denominator row (case-insensitive), else first row, else None."""
    valid = [d for d in denoms if isinstance(d, dict)]
    for d in valid:
        if (d.get("units") or "").strip().casefold() == "participants":
            return d
    return valid[0] if valid else None


def classify_stopped_study_results_state(study: dict[str, Any]) -> str:
    """Classify results availability and primary-outcome denominator presence on a stopped study.

    Returns one of:
      - `"results_with_analysed_primary"`: posted results with >= 1 PRIMARY measure carrying a
        numeric group denominator > 0.
      - `"empty_results_shell_zero_analysed"`: posted results where PRIMARY measure(s) carry numeric
        denominators and every numeric primary denominator is 0.
      - `"results_posted_no_primary_denom"`: results posted, but no PRIMARY outcome measure carries
        a numeric denominator (or weekly projection lacks `resultsSection`).
      - `"results_not_posted"`: no posted results (`hasResults` is false / `resultsSection` absent).
    """
    is_raw = "protocolSection" in study or "resultsSection" in study or "hasResults" in study
    if not is_raw:
        return "results_posted_no_primary_denom" if study.get("results_date") else "results_not_posted"

    has_results = study.get("hasResults")
    results_sec = study.get("resultsSection")
    if has_results is False or not isinstance(results_sec, dict) or not results_sec:
        return "results_not_posted"

    om_mod = results_sec.get("outcomeMeasuresModule")
    measures = om_mod.get("outcomeMeasures") if isinstance(om_mod, dict) else None
    if not isinstance(measures, list):
        return "results_posted_no_primary_denom"

    primary_measures = [m for m in measures if isinstance(m, dict) and m.get("type") == "PRIMARY"]
    if not primary_measures:
        return "results_posted_no_primary_denom"

    numeric_counts: list[float] = []
    for measure in primary_measures:
        row = _participants_denom_row(measure.get("denoms") or [])
        if row is None:
            continue
        for entry in row.get("counts") or []:
            if not isinstance(entry, dict):
                continue
            val = parse_num(entry.get("value"))
            if val is not None and is_num(entry.get("value")) and val >= 0:
                numeric_counts.append(val)

    if not numeric_counts:
        return "results_posted_no_primary_denom"
    if any(v > 0 for v in numeric_counts):
        return "results_with_analysed_primary"
    return "empty_results_shell_zero_analysed"


def evaluate_termination(
    study: dict[str, Any],
    overall_status: str | None,
    why_stopped: str | None,
    review: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Evaluate termination/withdrawal/suspension signals, overrides, and stopped results state.

    Private review columns (`reviewer`, `notes`) are never emitted into the returned artifact dict.
    """
    verbatim = why_stopped if isinstance(why_stopped, str) and why_stopped.strip() else None
    if overall_status not in STOPPED_STATUSES:
        return {
            "is_stopped_study": False,
            "overall_status": overall_status,
            "rules_version": TERMINATION_RULES_VERSION,
            "why_stopped_verbatim": verbatim,
            "category": None,
            "deterministic_category": None,
            "matched_signals": None,
            "matched_excerpts": None,
            "classification_source": None,
            "reviewed_at": None,
            "stopped_study_results_state": None,
        }

    det = classify_why_stopped(verbatim)
    results_state = classify_stopped_study_results_state(study)

    if review is not None:
        return {
            "is_stopped_study": True,
            "overall_status": overall_status,
            "rules_version": TERMINATION_RULES_VERSION,
            "why_stopped_verbatim": verbatim,
            "category": review["override_category"],
            "deterministic_category": det["deterministic_category"],
            "matched_signals": det["matched_signals"],
            "matched_excerpts": det["matched_excerpts"],
            "classification_source": "human_review_override",
            "reviewed_at": review["reviewed_at"],
            "stopped_study_results_state": results_state,
        }

    return {
        "is_stopped_study": True,
        "overall_status": overall_status,
        "rules_version": TERMINATION_RULES_VERSION,
        "why_stopped_verbatim": verbatim,
        "category": det["deterministic_category"],
        "deterministic_category": det["deterministic_category"],
        "matched_signals": det["matched_signals"],
        "matched_excerpts": det["matched_excerpts"],
        "classification_source": "deterministic_rule",
        "reviewed_at": None,
        "stopped_study_results_state": results_state,
    }
