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
   zero (`"empty_results_shell_zero_analysed"`), posted results whose primary denominators are
   zero where reported but missing or malformed elsewhere (`"results_posted_primary_denom_incomplete"`,
   rules 1.3.0), posted results lacking a numeric primary denominator
   (`"results_posted_no_primary_denom"`), and unposted results (`"results_not_posted"`).
"""
from __future__ import annotations

import csv
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from feasibility.metrics import is_num, parse_num

TERMINATION_RULES_VERSION = "1.4.0"

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
    "results_posted_primary_denom_incomplete",
    "results_posted_no_primary_denom",
    "results_not_posted",
)

REVIEW_COLUMNS = ("nct_id", "reviewer", "reviewed_at", "override_category", "notes")
PRIVATE_REVIEW_KEYS = frozenset({"reviewer", "notes"})

_NCT_RE = re.compile(r"^NCT[0-9]{8}$")  # ASCII digits only: `\d` also matches other scripts' digits
_I = re.IGNORECASE

# Clauses inside `whyStopped` that explicitly negate a signal category are masked before matching
# that category, preventing false positives on real registry text such as
# "Terminated for strategic reasons; there were no safety concerns."
#
# Method (rules 1.1.0; COVID's "no" in 1.2.0; business's "no ... reason" in 1.3.0). A negation
# masks a signal only when the negator governs the signal itself:
#   * Determiner-type negators ("no", "without", "neither", "nor", "there were no", "free of", ...)
#     modify the noun phrase that follows, so the signal may sit a few words later ("no new safety
#     signals"), but never past a causal connective: in "no further enrolment due to slow accrual"
#     the accrual signal is the stated cause, not the thing negated.
#   * "not" negates a verb, so it negates a signal only through a connective that names the signal
#     as the (non-)reason: "not due to COVID-19", "not related to safety", "not based on safety",
#     "not for a safety concern", "not terminated because of safety", "not considered a safety
#     issue". In "Could not continue due to COVID-19" it negates "continue", and COVID-19 is the
#     reason, so nothing is masked. (Rules 1.0.0 masked any signal within 35 characters of "not".)
#   * "unrelated to" and "non-" attach directly to the signal.
#   * Rules 1.2.0: for COVID, "no" masks the signal only when it heads the noun phrase holding it,
#     with nothing but filler words between ("No COVID-19 impact", "there were no COVID-19 related
#     issues", "no significant COVID-19 impact"). The pandemic is often the time or circumstance of
#     what "no" negates, so the span the other determiners use would mask it in "No participants
#     enrolled during the COVID-19 pandemic", where "no" governs "participants" and "during" is not
#     a causal connective; COVID's only span determiner stays "prior to". Rules 1.0.0 and 1.1.0 had
#     no "no" guard for COVID, so "No COVID-19 impact; terminated for strategic reasons" read as
#     COVID plus business (multiple_signals_detected). Known limits: "No impact of COVID-19" is
#     still a COVID signal, and "No COVID-19 patients were enrolled" loses it, as any phrase "no"
#     heads does.
#   * Rules 1.3.0: for business, "no" masks the signal only when it heads a phrase naming the
#     business word as a reason: "no", filler words, the business word(s), then a reason noun
#     ("No business reason; investigator relocated", "There were no strategic reasons"). A plain
#     "no" determiner would hide real business stops. In the 8,499 distinct `whyStopped` texts of
#     the weekly full records (extracted 2026-11-08), 17 put "no", "not", "neither", "nor" or
#     "without" within 40 characters of a word starting business, commercial, financial or
#     strategic, in the same clause: 16 state a business cause ("App developer is no longer in
#     business", "Study no longer met business objectives", "No interest in continuing trial due to
#     business changes"), one neither states nor denies one ("...not representing what will be made
#     commercially available"), and none denies one. 1.3.0 changes the category of none of the
#     11,291 weekly trials with a `whyStopped`. "No financial support" and "no commercial interest"
#     name no reason noun and stay business signals. Rules 1.0.0 to 1.2.0 had only the "not" /
#     "unrelated to" / "non-" guards here.
_CAUSAL = r"(?:due\s+to|because\s+of|owing\s+to|as\s+a\s+result\s+of|caused\s+by|resulting\s+from)"
_NOT_LINK = (
    r"(?:(?:be|been|being|was|were|is|are)\s+)?"
    r"(?:(?:terminated|stopped|halted|discontinued|withdrawn|suspended|closed|ended|made|taken)\s+)?"
    r"(?:(?:considered|deemed|believed|thought|judged)\s+(?:to\s+be\s+)?)?"
    r"(?:due\s+to|because\s+of|owing\s+to|related\s+to|attributable\s+to|attributed\s+to|associated\s+with|"
    r"linked\s+to|caused\s+by|driven\s+by|based\s+on|a\s+result\s+of|the\s+result\s+of|for|from|a|an|any)?"
)
# Nouns that name a business word as a (non-)reason after "no" (rules 1.3.0, business only).
_REASON_NOUNS = (
    r"\s+(?:reasons?|considerations?|concerns?|motives?|motivations?|grounds|rationale)\b"
)
_FILLERS = (
    r"(?:\s+(?:any|a|an|the|its|their|this|that|specific|particular|known|new|significant|major|serious|"
    r"reported|identified|observed|unexpected|of|or|and|product|study|drug|treatment)){0,3}"
)


def _negation(
    determiners: str,
    signal: str,
    gap: int,
    heads: str = "",
    head_tail: str = "",
    linked_signal: str = "",
) -> re.Pattern[str]:
    """Negation guard for one category: determiner-type negators within `gap` characters (never
    across a clause break or a causal connective), `heads` negators followed by the signal with
    only filler words between (and then `head_tail`, when given), or "not" / "unrelated to" /
    "non-" linked directly to the signal (see the method note above). `linked_signal`, when given,
    widens only that last form: a cause a "not ... due to" link excludes is excluded whatever its
    wording, while a determiner ("no drug supply") can itself state the problem (rules 1.4.0)."""
    # A negated signal may run into further signal words ("COVID-19 pandemic", "safety or
    # tolerability"); the mask covers the whole run so none of it is matched afterwards.
    run = rf"(?:{signal})\b(?:[\s/-]+(?:(?:or|and|nor)\s+)?(?:{signal})\b){{0,3}}"
    det = (
        rf"\b(?:{determiners})\b(?:(?!\b{_CAUSAL}\b)[^.;,]){{0,{gap}}}\b{run}"
        if determiners
        else None
    )
    head = rf"\b(?:{heads})\b{_FILLERS}\s+{run}{head_tail}" if heads else None
    lrun = rf"(?:{linked_signal or signal})\b(?:[\s/-]+(?:(?:or|and|nor)\s+)?(?:{linked_signal or signal})\b){{0,3}}"
    linked = rf"\bnot\s+{_NOT_LINK}{_FILLERS}\s*\b{lrun}|\bunrelated\s+to{_FILLERS}\s+{lrun}|\bnon[- ]{lrun}"
    return re.compile("|".join(x for x in (det, head, linked) if x), _I)


# The phrases `_SIGNAL_PATTERNS` detects for these two categories, for their linked negation guard.
_REGULATORY_LINKED = (
    r"(?:clinical|regulatory)\s+hold|regulatory\s+(?:authority|agency|decision|request|directive|feedback|"
    r"requirements?|non[- ]approval|reasons?|issues?)"
    r"|(?:the\s+)?(?:fda|ema|mhra|pmda|health\s+canada|competent\s+authority)"
    r"(?:\s+(?:placed|imposed|hold|requests?|requested|ordered|directive|feedback|decision))?"
    r"|(?:irb|iec|ethics\s+committee)(?:\s+(?:suspension|decision|disapproval))?"
)
_SUPPLY_LINKED = (
    r"(?:(?:study\s+)?drug|investigational\s+(?:medicinal\s+)?product|medication|vaccine|device|placebo"
    r"|compound|imp)\s+"
    r"(?:supply|shortage|unavailab\w*|availability|expir(?:ation|y|ed)|stability\s+issues?)"
    r"|shortages?|unavailability|lack\s+of\s+supply|supply(?:\s+(?:issues?|problems?|chain|constraints?|shortages?))?"
    r"|manufacturing(?:\s+(?:issues?|problems?|delays?|defects?|failures?|holds?|constraints?))?"
    r"|drug\s+availability"
)


_NEGATION_PATTERNS: dict[str, re.Pattern[str]] = {
    "accrual_or_recruitment": re.compile(
        r"\b(?:recruitment|enrol+ment|accrual)\s+was\s+(?:fully\s+|already\s+)?completed\b|"
        + _negation(r"no|neither|nor", r"accrual|enrol+ment|recruitment", 35).pattern,
        _I,
    ),
    "futility_or_lack_of_efficacy": _negation(
        r"no|neither|nor|without", r"futil(?:e|ity)|lack\s+of\s+efficacy|efficacy\s+concerns?", 35
    ),
    "safety_or_toxicity": _negation(
        r"there\s+(?:were|was|are|is)\s+no|no|neither|nor|without|free\s+of|absence\s+of",
        r"safety|toxicit(?:y|ies)|tolerability|adverse\s+events?|saes?|dlts?",
        40,
    ),
    "business_or_strategic": _negation(
        r"", r"business|commercial|financial|strategic", 30, heads=r"no", head_tail=_REASON_NOUNS
    ),
    "covid_or_pandemic": _negation(
        r"prior\s+to", r"covid(?:[- ]?19)?|sars[- ]cov[- ]2|pandemic|coronavirus", 35, heads=r"no"
    ),
    # Rules 1.4.0: the linked forms cover every phrase the category's detector matches ("Not due to
    # FDA feedback", "not related to manufacturing issues"); the determiners keep the narrow nouns.
    "regulatory_or_clinical_hold": _negation(
        r"no|without|lifted", r"clinical\s+hold|regulatory\s+hold", 30, linked_signal=_REGULATORY_LINKED
    ),
    # Rules 1.4.0 also narrow the determiners to problem nouns: "no drug supply" and "without drug
    # supply" state a supply problem (three weekly texts: "no further drug supply", "due to no drug
    # supply", "there was no drug supply"), while "no shortage" or "no supply issues" deny one.
    "supply_or_drug_availability": _negation(
        r"no|without",
        r"shortages?|(?:supply|manufacturing)\s+(?:issues?|problems?|constraints?|shortages?|delays?)",
        35,
        linked_signal=_SUPPLY_LINKED,
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
    """Validate the whole `reviewed_at` value and return it stripped.

    Accepted forms: an exact calendar date `YYYY-MM-DD`, or a complete ISO-8601 timestamp
    `YYYY-MM-DDTHH:MM[:SS[.ffffff]][Z|+HH:MM]` that `datetime.fromisoformat` parses in full. The value
    is published verbatim, so a valid date prefix followed by other text (`2026-11-20garbage`) is
    refused rather than published.
    """
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"termination_reviews.csv ({nct_id}): invalid reviewed_at date {value!r}") from exc
        return text
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\S+", text):
        try:
            datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                f"termination_reviews.csv ({nct_id}): invalid reviewed_at timestamp {value!r}"
            ) from exc
        return text
    raise ValueError(
        f"termination_reviews.csv ({nct_id}): reviewed_at must be YYYY-MM-DD or a full ISO-8601 "
        f"timestamp, got {value!r}"
    )


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


def _participants_denom_rows(denoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The Participants denominator rows (case-insensitive), else the first row, else none.

    A `units` that is not a string names no unit, so it never matches; before rules 1.3.0 it raised
    `AttributeError` and one malformed record stopped the whole build. Every Participants row is
    returned, not the first (rules 1.4.0): with two, the first decided the state by order, so zeros
    in it read as an empty shell while the second reported analysed participants.
    """
    valid = [d for d in denoms if isinstance(d, dict)]
    participants = [
        d for d in valid
        if isinstance(d.get("units"), str) and d["units"].strip().casefold() == "participants"
    ]
    return participants or valid[:1]


def _counts_cover_groups(groups: Any, counts: list[Any]) -> bool:
    """True when `counts` holds exactly one entry per id in the measure's `groups` and no other.

    A measure whose `groups` is not a non-empty list of objects with distinct non-blank string ids
    cannot show which groups its denominator should cover, so it is not complete either.
    """
    if not isinstance(groups, list) or not groups:
        return False
    group_ids = [g.get("id") if isinstance(g, dict) else None for g in groups]
    if not all(isinstance(g, str) and g.strip() for g in group_ids) or len(set(group_ids)) != len(group_ids):
        return False
    count_ids = [c.get("groupId") if isinstance(c, dict) else None for c in counts]
    # Checked before any set is built: a list or object id is unhashable and would raise.
    if not all(isinstance(c, str) and c.strip() for c in count_ids):
        return False
    return len(count_ids) == len(set(count_ids)) and set(count_ids) == set(group_ids)


def classify_stopped_study_results_state(study: dict[str, Any]) -> str:
    """Classify results availability and primary-outcome denominator presence on a stopped study.

    Returns one of:
      - `"results_with_analysed_primary"`: posted results with >= 1 PRIMARY measure carrying a
        numeric group denominator > 0.
      - `"empty_results_shell_zero_analysed"`: posted results where every PRIMARY measure carries a
        complete denominator row (one count for each of its groups, each numeric and >= 0) and every
        count is 0.
      - `"results_posted_primary_denom_incomplete"` (rules 1.3.0): no PRIMARY denominator is > 0,
        at least one is 0, and at least one PRIMARY measure lacks a denominator row, has a group
        count that is missing, non-numeric or negative, or has counts that do not match its groups
        one to one, so zero analysed is not established for every primary outcome.
        Rules 1.2.0 dropped such counts and read the rest as an empty shell.
      - `"results_posted_no_primary_denom"`: results posted, but no PRIMARY outcome measure carries
        a numeric denominator (or weekly projection lacks `resultsSection`).

    One positive count on any PRIMARY measure establishes analysed participants, whatever the other
    measures hold; a zero-analysed shell needs every primary count, so a gap there is kept apart.
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
    # A primary measure is complete when it has a denominator row whose counts are a non-empty list
    # of group entries, each numeric and >= 0, with exactly one entry for each of the measure's
    # `groups` ids (none missing, repeated or unexpected). Anything else leaves its analysed count
    # unknown. On the 2,200 cached records with posted outcome measures under
    # docs/audits/outcomes_results_structure/, every one of the 4,670 primary denominator rows lists
    # exactly its measure's group ids, in order.
    all_complete = True
    # An outcome measure that is not an object could be a primary one, so zero analysed is not
    # established for every primary outcome.
    if any(not isinstance(m, dict) for m in measures):
        all_complete = False
    for measure in primary_measures:
        denoms = measure.get("denoms")
        rows = _participants_denom_rows(denoms if isinstance(denoms, list) else [])
        # No row, or more than one Participants row, leaves the measure's analysed count unsettled:
        # every row's counts are still read, so a positive count anywhere shows analysed participants,
        # but zeros never make an empty shell unless they are the measure's one denominator row.
        if len(rows) != 1:
            all_complete = False
        for row in rows:
            counts = row.get("counts")
            if not isinstance(counts, list) or not counts:
                all_complete = False
                continue
            if not _counts_cover_groups(measure.get("groups"), counts):
                all_complete = False
            for entry in counts:
                value = entry.get("value") if isinstance(entry, dict) else None
                val = parse_num(value)
                if val is not None and is_num(value) and val >= 0:
                    numeric_counts.append(val)
                else:
                    all_complete = False

    if not numeric_counts:
        return "results_posted_no_primary_denom"
    if any(v > 0 for v in numeric_counts):
        return "results_with_analysed_primary"
    if not all_complete:
        return "results_posted_primary_denom_incomplete"
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
