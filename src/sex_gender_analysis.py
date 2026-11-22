"""sex_gender_analysis.py: the versioned analytical layer around the vendored parser.

READS   a parser row (src/sex_gender_table.build_row / build_row_from_raw) and
        the SELECTED raw measures the row was parsed from (the retained
        sex_gender_raw_measures record, or the envelope of
        select_raw_envelope), plus the registered enrollment.
WRITES  nothing itself. analyze() returns the full block; lean_block() the
        per-study subset that rides in the published parts under the study
        key "sex_gender_analysis" (the site's contract lists it as optional:
        a record published before this layer existed has no such key, and a
        reader must say "newer analysis unavailable", never manufacture one).
INVOKED by src/extract_all.py (weekly), scripts/build_sex_gender_analysis.py
        (the re-derivation path from the retained raw measures), and
        scripts/generate_mobile_data.py (the summary block).

WHAT THIS LAYER IS, AND IS NOT
  The parser (src/sex_gender_parser.py) is vendored unchanged: its vocabulary,
  evaluation order, choose_total and is_participant_count rule are the
  manuscript's and are not touched here. This module wraps it. It never
  re-derives a count the parser produced, never collapses the five parser
  states, and never reads enrollment arithmetic into a reporting state.

  It adds the reporting layer of the newer manuscript (JAMA submission draft
  of 2026-10-26, Supplement 1: eMethods 2, eTable 1, eTable 5), as far as
  that layer can be derived from the as-run parser output:

  reporting.dimension        sex_only | both | gender_only | neither |
                             parse_error. The separate reporting dimension
                             (eTable 1 panel A), from the parser's own
                             reported_sex / reported_gender outcomes.
  reporting.neither_reason   explicit_unknown_or_not_collected |
                             measure_absent |
                             measure_present_no_usable_breakdown. The three
                             "neither" sub-states (eTable 1), mapped from the
                             parser's status and its declared_not_collected
                             flag. Parse failure is kept as parse_error: an
                             operational failure, not a substantive state.
  reporting.explicit_unknown_with_values
                             an explicit Unknown count beside reported values
                             (eTable 1 panel B, last row). Both are kept.
  composition.*              whether the trial enters the composition and
                             percent-female denominators, with the reason it
                             does not; incomplete_composition is carried as a
                             separate, explicitly unevaluated value (see the
                             rule matrix, R-IC-01), never advertised as false.
  gender_categories.*        the detailed gender-identity categories of
                             eTable 5 (D3) and the strict lower bound without
                             "another or self-described" (D5), with intersex,
                             cisgender-labelled and explicit unknown as rows
                             of their own, counted once per trial at the
                             largest value across the count-driving tables.
                             The exclusion rules (non-participant units;
                             category sum over enrollment by more than 10%)
                             are applied and recorded, not silently.
  patients_only              NOT evaluated. The patients-only rule (E7) is
                             validated in the manuscript on named cases only;
                             no general rule is approved for new inputs.
  unresolved                 the rule ids this block leaves open, so a reader
                             can tell an undecided value from a measured one.
                             Since the owner's decisions of 2026-10-28 only
                             R-GC-05 is open; R-IC-01, R-PO-01 and R-FS-01 are
                             decided limitations, stated on the fields they
                             affect (DECIDED_RULES).

  The layer label is "as_run": the manuscript's FINAL states include manual
  reconciliation of borderline records that no automated run reproduces.

  Every regex, mapping and threshold below is documented in
  docs/sex_gender_analysis/RULE_SOURCE_MATRIX.md with its source, approval
  status and whether it was verified against the source file in this
  environment. Nothing here is a count rule of the parser.

DEPENDENCIES
  Standard library only.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from src import sex_gender_parser as sgp

SCHEMA_VERSION = "sex_gender_analysis/1.0"
# The analytical rules this module implements, with the manuscript source they
# follow. Bumped whenever a mapping, regex or threshold below changes; a row's
# value is comparable with another row's only under the same version.
RULES_VERSION = ("analysis@2026-10-28 decided (R-IC-01, R-PO-01, R-FS-01, R-GC-04; R-GC-05 open) / "
                 "manuscript 2026-10-26 supp1 (eMethods 2, eTable 1, eTable 5)")
LAYER = "as_run"

REPORTING_DIMENSIONS: tuple = ("sex_only", "both", "gender_only", "neither", "parse_error")
NEITHER_REASONS: tuple = ("explicit_unknown_or_not_collected", "measure_absent",
                          "measure_present_no_usable_breakdown")
COMPOSITION_INELIGIBLE_REASONS: tuple = ("parse_error", "not_reported_sex", "non_participant_units",
                                         "no_female_male_counts")

# eTable 5: the gender-identity categories (D3) in the manuscript's order, then
# the rows reported separately, never as gender diverse.
D3_CATEGORIES: tuple = ("transgender_with_sex_term", "transgender_no_sex_term", "nonbinary_gender_expansive",
                        "questioning", "combined_gender_minority", "another_self_described")
SEPARATE_ROWS: tuple = ("intersex", "cisgender_labeled", "explicit_unknown_declined")
GENDER_CATEGORY_STATUSES: tuple = ("derived", "not_applicable", "excluded", "not_evaluated", "parse_error")
GENDER_EXCLUSION_REASONS: tuple = ("non_participant_units", "sum_exceeds_enrollment_10pct")
# eTable 5: "whose category sum exceeded enrollment by more than 10%".
ENROLLMENT_EXCESS_TOLERANCE = 0.10

# Every rule id a block may list in `unresolved`, including ids that blocks
# built before the 2026-10-28 decisions carry. Ids are the rule matrix's.
UNRESOLVED_RULES: dict = {
    "R-IC-01": "incomplete composition (female and male counts covering only some participants, or clusters) "
               "needs group-level coverage evidence and an approved general rule; not evaluated",
    "R-PO-01": "patients-only exclusion of clinician or provider groups (E7) is validated on named historical cases "
               "only; not applied to new inputs",
    "R-FS-01": "FINAL reporting states include manual reconciliation of borderline records (unresolved cases "
               "U01-U03, U08-U11 as of the inspected v5.2 file); this block is the as-run layer",
    "R-GC-05": "the detailed gender-category vocabulary is reconstructed from eTable 5's category descriptions; "
               "parity with the manuscript's A20 derivation script is unverified in this environment",
}
# Still open: listed on every block it applies to.
OPEN_RULES: tuple = ("R-GC-05",)
# Decided by the owner on 2026-10-28 (docs/sex_gender_analysis/DECISIONS_2026-10-28.md).
# Each keeps the behaviour this module already had; the decision makes it a
# stated limitation rather than an open question, so blocks no longer list it.
DECIDED_RULES: dict = {
    "R-IC-01": "decided 2026-10-28: incomplete composition is not evaluated in this version "
               "(incomplete_composition stays null, with its reason)",
    "R-PO-01": "decided 2026-10-28: no general patients-only rule; patients_only stays not_evaluated",
    "R-FS-01": "decided 2026-10-28: the layer stays as-run and is labelled so; no FINAL overlay",
    "R-GC-04": "decided 2026-10-28: the 10% enrollment exclusion compares the D3 total",
}

# ---------------------------------------------------------------------------
# Detailed gender-identity categories (eTable 5)
# ---------------------------------------------------------------------------
# Evaluation order is the rule, as in the parser's canon_bucket: a label is
# first routed through the vendored parser's bucket (so structural rows,
# sexual orientation and plain Female/Male never reach these categories, and
# the parser's Unknown vocabulary decides explicit_unknown_declined), then
# through the patterns below, first match wins.
_TRANS_RX = re.compile(r"transgender|trandgender|trans-|\btrans\b|trans[- /]?(man|men|wom[ae]n|masc|femin|person|people|ftm|mtf)|\bftm\b|\bmtf\b")
_CIS_RX = re.compile(r"cisgender|cis-|\bcis\b")
_INTERSEX_RX = re.compile(r"intersex")
_COMBINED_RX = re.compile("|".join([
    r"gender[- ]?minorit", r"sexual or gender minorit", r"sex(ual)? and gender minorit",
    r"tgncnb", r"\btgnc\b", r"gender[- ]?diverse", r"transgender[ /,]+(or |and )?(non[- ]?binary|gender[- ]?(queer|fluid|expansive|variant|nonconforming|non-conforming))",
    r"(non[- ]?binary|gender[- ]?(queer|fluid|expansive|variant))[ /,]+(or |and )?trans",
]))
_QUESTIONING_RX = re.compile(r"questioning")
_NONBINARY_RX = re.compile("|".join([
    r"non[- ]?binary", r"nonconforming", r"non[- ]?conforming", r"gender ?queer", r"gender ?fluid",
    r"gender[- ]?expansive", r"gender[- ]?variant", r"agender", r"bigender", r"pangender", r"third gender",
    r"two-? ?spirit", r"androgynous", r"\bfluid\b", r"multi[- ]?gender", r"multiple gender", r"more than one gender",
    r"gender different from sex assigned at birth",
]))
_ANOTHER_RX = re.compile("|".join([
    r"\bothers?\b", r"another gender", r"other gender", r"self[- ]?(describe|described|identify|identifies|identification)",
    r"prefer to self", r"identify in another way", r"(another|different|additional) (identity|identification|description)",
    r"additional sex or gender", r"something else", r"(none|null) of (these|the above|the listed)",
    r"do not identify as any", r"^ambiguous$", r"\bdiverse\b",
]))
# The parser's sex words (a transgender label "also names a sex", eTable 5).
_SEXWORD_RX = sgp.CB_SEXWORD_RX


def detailed_gender_category(label: Any) -> Optional[str]:
    """The eTable 5 category of one class or category label, or None when the
    label is not a gender-identity row (Female, Male, a structural row, sexual
    orientation, an empty title). Explicit Unknown and its synonyms map to
    explicit_unknown_declined by the parser's own Unknown vocabulary."""
    z = sgp.cb_norm(label)
    if not z:
        return None
    bucket = sgp.canon_bucket(z)
    if bucket == "unknown":
        return "explicit_unknown_declined"
    if bucket in ("female", "male", ""):
        return None
    # bucket is gender_diverse or ambiguous: a gender-identity row of some kind
    if _INTERSEX_RX.search(z):
        return "intersex"
    if _COMBINED_RX.search(z):
        return "combined_gender_minority"
    if _TRANS_RX.search(z):
        return "transgender_with_sex_term" if _SEXWORD_RX.search(z) else "transgender_no_sex_term"
    if _CIS_RX.search(z):
        return "cisgender_labeled"
    if _QUESTIONING_RX.search(z):
        return "questioning"
    if _NONBINARY_RX.search(z):
        return "nonbinary_gender_expansive"
    if _ANOTHER_RX.search(z):
        return "another_self_described"
    return None


def _label_values(measure: dict) -> tuple[list[tuple[str, Optional[float]]], bool]:
    """(label, total) per row of one selected measure, under the parser's own
    layout rule (a class whose title maps to a bucket makes the layout
    customized, else the categories are the rows) and its choose_total. The
    second value says whether any row of the table is a Female or Male row,
    for the two-question flag."""
    classes = measure.get("classes") if isinstance(measure, dict) else None
    if not isinstance(classes, list):
        return [], False
    class_buckets = [sgp.canon_bucket((cl or {}).get("title")) if isinstance(cl, dict) else "" for cl in classes]
    rows: list[tuple[str, Optional[float]]] = []
    binary = False
    if any(b in sgp.BUCKETS for b in class_buckets):
        for cl in classes:
            if not isinstance(cl, dict):
                continue
            raw_t = "" if cl.get("title") is None else str(cl.get("title"))
            meas: list = []
            for cat in (cl.get("categories") or []):
                if isinstance(cat, dict) and isinstance(cat.get("measurements"), list):
                    meas.extend(cat["measurements"])
            rows.append((raw_t, sgp.choose_total(meas)["total"]))
            if sgp.canon_bucket(raw_t) in ("female", "male"):
                binary = True
    else:
        cl = classes[0] if classes and isinstance(classes[0], dict) else {}
        for cat in (cl.get("categories") or []):
            if not isinstance(cat, dict):
                continue
            raw_t = "" if cat.get("title") is None else str(cat.get("title"))
            meas = cat.get("measurements") if isinstance(cat.get("measurements"), list) else []
            rows.append((raw_t, sgp.choose_total(meas)["total"]))
            if sgp.canon_bucket(raw_t) in ("female", "male"):
                binary = True
    return rows, binary


def measure_is_participant_count(measure: dict) -> Optional[bool]:
    """What the vendored parser says is_participant_count is for this one
    measure's (paramType, unitOfMeasure); asked of the module itself so the
    rule stays exact across parser versions (sex_gender_table does the same)."""
    from . import sex_gender_table as _sgt  # local import: sex_gender_table imports nothing from here
    return _sgt._module_is_participant_count(measure.get("paramType"), measure.get("unitOfMeasure"))


def _count_driving(selected: list) -> list:
    """The measures the parser takes its counts from: the sex/gender-typed
    ones when any exist (parse_trial's ms_cnt), else every selected measure."""
    typed = [m for m in selected if isinstance(m, dict) and sgp.measure_type(m.get("title")) in sgp.SG_TYPES]
    return typed or [m for m in selected if isinstance(m, dict)]


def explicit_unknown_with_values(selected: list) -> bool:
    """R-EU-01: an explicit Unknown row with a non-zero value in the SAME
    measure as a non-zero Female, Male, gender-diverse or ambiguous row. Two
    tables, one with values and one with only Unknown, do not qualify: the
    parser takes Unknown as a maximum across tables, so the row-level counts
    cannot make this distinction and the measures are read directly."""
    for m in _count_driving([x for x in selected if isinstance(x, dict)]):
        rows, _ = _label_values(m)
        has_values = any(sgp.canon_bucket(lab) in ("female", "male", "gender_diverse", "ambiguous")
                         and v is not None and v > 0 for lab, v in rows)
        has_unknown = any(sgp.canon_bucket(lab) == "unknown" and v is not None and v > 0 for lab, v in rows)
        if has_values and has_unknown:
            return True
    return False


def derive_gender_categories(selected: Optional[list], row: dict, enrollment: Any) -> dict:
    """eTable 5 for one trial. Participants are counted once per category at
    the largest value across the count-driving tables (within one table, rows
    that map to the same category are summed, as the parser sums its buckets).
    Returns the block described in the module docstring; counts are None for a
    category no table posted (absence is not zero)."""
    out: dict = {
        "status": None, "definition": "manuscript 2026-10-26 supp1 eTable 5 (D3 and D5 are implementation choices open "
                                      "to revision, E11)",
        "categories": {c: None for c in D3_CATEGORIES},
        "separate": {c: None for c in SEPARATE_ROWS},
        "d3_total": None, "d5_strict": None,
        "exclusion": None,
        "flags": {"two_question_table": False, "possible_double_count": False},
        "labels": {}, "unclassified_labels": [],
    }
    if row.get("sex_report_status") == "parse_error" or row.get("parse_ok") is False:
        out["status"] = "parse_error"
        return out
    # eTable 5 is "among trials reporting gender": a trial the parser's
    # category-based rule does not score as reporting gender (a Gender-titled
    # Female/Male table, a sex table with an Unknown row) has no gender
    # categories to derive, and every count stays null.
    if not selected or row.get("reported_gender") is not True:
        out["status"] = "not_applicable"
        return out
    per_table: list[dict] = []
    tables_with_identity = 0
    non_participant_tables: list = []
    for m in _count_driving(selected):
        rows, binary = _label_values(m)
        sums: dict = {}
        has_identity = False
        # The parser's is_participant_count describes its primary Female/Male
        # table only. Each table that posts an identity category is checked on
        # its own units: a COUNT_OF_UNITS (or mean/median/percent) gender table
        # never contributes participants (AGENTS.md: COUNT_OF_UNITS tables stay
        # out of composition), and its presence excludes the trial below.
        table_is_participants = measure_is_participant_count(m)
        for raw_label, value in rows:
            cat = detailed_gender_category(raw_label)
            if cat is None:
                if sgp.canon_bucket(raw_label) in ("gender_diverse", "ambiguous") and raw_label not in out["unclassified_labels"]:
                    out["unclassified_labels"].append(raw_label)
                continue
            out["labels"].setdefault(cat, [])
            if raw_label not in out["labels"][cat]:
                out["labels"][cat].append(raw_label)
            if cat in D3_CATEGORIES or cat in ("intersex", "cisgender_labeled"):
                has_identity = True
            if value is not None:
                sums[cat] = sums.get(cat, 0.0) + value
        if has_identity:
            tables_with_identity += 1
            if binary:
                out["flags"]["two_question_table"] = True
            if table_is_participants is False:
                non_participant_tables.append(m.get("title"))
        per_table.append(sums)
    out["flags"]["possible_double_count"] = tables_with_identity > 1
    for cat in (*D3_CATEGORIES, *SEPARATE_ROWS):
        vals = [t[cat] for t in per_table if cat in t]
        v = max(vals) if vals else None
        if cat in D3_CATEGORIES:
            out["categories"][cat] = v
        else:
            out["separate"][cat] = v
    posted = any(v is not None for v in out["categories"].values()) or any(
        out["separate"][c] is not None for c in ("intersex", "cisgender_labeled"))
    if not posted and not any(out["labels"].get(c) for c in (*D3_CATEGORIES, "intersex", "cisgender_labeled")):
        out["status"] = "not_applicable"
        return out
    d3 = [v for v in out["categories"].values() if v is not None]
    out["d3_total"] = sum(d3) if d3 else None
    d5 = [v for c, v in out["categories"].items() if v is not None and c != "another_self_described"]
    out["d5_strict"] = sum(d5) if d5 else None
    # The two eTable 5 exclusions, recorded rather than applied silently. The
    # first fires on the parser's primary table or on any table that posted an
    # identity category in non-participant units.
    if row.get("is_participant_count") is False or non_participant_tables:
        out["status"] = "excluded"
        out["exclusion"] = {"reason": "non_participant_units"}
        if non_participant_tables:
            out["exclusion"]["tables"] = non_participant_tables
        return out
    enr = sgp._num(enrollment)
    if enr is not None and out["d3_total"] is not None and out["d3_total"] > enr * (1 + ENROLLMENT_EXCESS_TOLERANCE):
        out["status"] = "excluded"
        out["exclusion"] = {"reason": "sum_exceeds_enrollment_10pct", "d3_total": out["d3_total"], "enrollment": enr}
        return out
    out["status"] = "derived"
    return out


# ---------------------------------------------------------------------------
# Reporting dimension and the "neither" sub-states (eMethods 2, eTable 1)
# ---------------------------------------------------------------------------

def reporting_dimension(row: dict) -> tuple[str, Optional[str]]:
    """(dimension, neither_reason) from the parser's status and outcomes.

    The parser's reported_sex is count-based (Female + Male > 0 under any
    measure title) and reported_gender category-based (a gender-diverse or
    cis/trans-qualified category > 0), which are the manuscript's definitions
    (Methods, Outcomes). The three sub-states of "neither" follow eTable 1:
    the registrant recorded participants only as unknown or not reported, or
    stated that sex was not collected (the parser's explicit_unknown_only, and
    uninformative with declared_not_collected); the measure is absent (the
    parser's not_reported); the measure is present without a usable breakdown
    (every other uninformative). parse_error stays parse_error."""
    status = row.get("sex_report_status")
    if status == "parse_error":
        return "parse_error", None
    rs, rg = row.get("reported_sex") is True, row.get("reported_gender") is True
    if rs and rg:
        return "both", None
    if rs:
        return "sex_only", None
    if rg:
        return "gender_only", None
    if status == "explicit_unknown_only":
        return "neither", "explicit_unknown_or_not_collected"
    if status == "not_reported":
        return "neither", "measure_absent"
    if status == "uninformative":
        if row.get("declared_not_collected") is True:
            return "neither", "explicit_unknown_or_not_collected"
        return "neither", "measure_present_no_usable_breakdown"
    # "reported" with neither outcome true cannot happen under the parser's
    # rules (classify_reporting requires a count the outcomes read); kept as
    # neither with no reason rather than guessed.
    return "neither", None


def composition(row: dict) -> dict:
    """Whether the trial enters the composition and percent-female
    denominators (README D5: reported_sex AND is_participant_count AND
    Female + Male > 0), and why not. incomplete_composition is left
    unevaluated: a value of false would claim complete coverage no rule
    established (R-IC-01)."""
    f = row.get("n_female") or 0.0
    m = row.get("n_male") or 0.0
    status = row.get("sex_report_status")
    reason: Optional[str] = None
    if status == "parse_error":
        reason = "parse_error"
    elif row.get("reported_sex") is not True:
        reason = "not_reported_sex"
    elif row.get("is_participant_count") is False:
        reason = "non_participant_units"
    elif f + m <= 0:
        reason = "no_female_male_counts"
    return {
        "eligible": None if status == "parse_error" else reason is None,
        "ineligible_reason": reason,
        "incomplete_composition": None,
        "incomplete_composition_evaluated": False,
        "incomplete_composition_reason": "not evaluated (R-IC-01)",
    }


def analyze(row: dict, selected: Optional[list], enrollment: Any) -> dict:
    """The full sex_gender_analysis block for one trial.

    row        the parser row (full or lean; the full row is needed for
               declared_not_collected and is_participant_count, which the lean
               row carries too)
    selected   the SELECTED sex/gender measures the row was parsed from
    enrollment registered enrollment (protocolSection.designModule
               .enrollmentInfo.count), for the eTable 5 exclusion only"""
    dim, reason = reporting_dimension(row)
    status = row.get("sex_report_status")
    eu_with_values: Optional[bool]
    if status == "parse_error":
        eu_with_values = None
    elif selected is not None:
        eu_with_values = explicit_unknown_with_values(selected)
    else:
        # No measures to hand (a lean-only caller): the row-level reading, which
        # cannot tell one table with both from two tables with one each.
        known = (row.get("n_female") or 0) + (row.get("n_male") or 0) + (row.get("n_gender_diverse") or 0) \
            + (row.get("n_ambiguous_gender") or 0)
        eu_with_values = bool((row.get("n_unknown") or 0) > 0 and known > 0)
    gc = derive_gender_categories(selected, row, enrollment)
    # Only open rules are listed (OPEN_RULES): the decided ones are stated on
    # the fields they affect (incomplete_composition_reason, patients_only,
    # layer, gender_categories.exclusion).
    unresolved = []
    if dim != "parse_error" and gc["status"] in ("derived", "excluded"):
        unresolved.append("R-GC-05")
    return {
        "schema_version": SCHEMA_VERSION,
        "rules_version": RULES_VERSION,
        "layer": LAYER,
        "parser": {
            "rules_version": row.get("parser_rules_version") or sgp.PARSER_RULES_VERSION,
            "module_version": row.get("parser_module_version") or sgp.__version__,
            "sex_report_status": status,
        },
        "reporting": {
            "dimension": dim,
            "neither_reason": reason,
            "explicit_unknown_with_values": eu_with_values,
            "declared_not_collected": bool(row.get("declared_not_collected")) if status != "parse_error" else None,
        },
        "composition": composition(row),
        "gender_categories": gc,
        "patients_only": {"status": "not_evaluated", "rule": "R-PO-01"},
        "unresolved": unresolved,
    }


# The per-study subset that rides in the published parts. Small on purpose: the
# site's startup budget counts every byte of every record, and the full block
# is published beside the parsed table (scripts/build_sex_gender_analysis.py).
LEAN_KEYS: tuple = ("schema_version", "reporting_dimension", "neither_reason", "explicit_unknown_with_values",
                    "composition_eligible", "incomplete_composition", "incomplete_composition_evaluated",
                    "gender_categories_status", "d3_total", "d5_strict", "unresolved")


def lean_block(full: dict) -> dict:
    """The lean per-study block from a full analyze() block."""
    return {
        "schema_version": full["schema_version"],
        "reporting_dimension": full["reporting"]["dimension"],
        "neither_reason": full["reporting"]["neither_reason"],
        "explicit_unknown_with_values": full["reporting"]["explicit_unknown_with_values"],
        "composition_eligible": full["composition"]["eligible"],
        "incomplete_composition": full["composition"]["incomplete_composition"],
        "incomplete_composition_evaluated": full["composition"]["incomplete_composition_evaluated"],
        "gender_categories_status": full["gender_categories"]["status"],
        "d3_total": full["gender_categories"]["d3_total"],
        "d5_strict": full["gender_categories"]["d5_strict"],
        "unresolved": list(full["unresolved"]),
    }


def lean_from_study(study_record: dict) -> Optional[dict]:
    """The lean block a published study record carries, or None when the
    record predates this layer (the reader's "unavailable" case)."""
    b = study_record.get("sex_gender_analysis") if isinstance(study_record, dict) else None
    return b if isinstance(b, dict) and b.get("schema_version") == SCHEMA_VERSION else None


# ---------------------------------------------------------------------------
# Flat row (the published sidecar table) and aggregates (the summary block)
# ---------------------------------------------------------------------------

COLUMNS: list = [
    "nct_id", "schema_version", "rules_version", "layer", "parser_rules_version", "parser_module_version",
    "sex_report_status", "reporting_dimension", "neither_reason", "explicit_unknown_with_values",
    "declared_not_collected", "composition_eligible", "composition_ineligible_reason",
    "incomplete_composition", "incomplete_composition_evaluated",
    "gender_categories_status", "gender_exclusion_reason",
    *[f"gc_{c}" for c in D3_CATEGORIES], *[f"gc_{c}" for c in SEPARATE_ROWS],
    "d3_total", "d5_strict", "flag_two_question_table", "flag_possible_double_count",
    "gc_labels", "gc_unclassified_labels", "patients_only_status", "unresolved", "snapshot_date",
]
JSON_COLUMNS: tuple = ("gc_labels", "gc_unclassified_labels", "unresolved")


def flat_row(nct_id: Optional[str], full: dict, snapshot_date: Optional[str]) -> dict:
    gc = full["gender_categories"]
    r: dict = {
        "nct_id": nct_id, "schema_version": full["schema_version"], "rules_version": full["rules_version"],
        "layer": full["layer"], "parser_rules_version": full["parser"]["rules_version"],
        "parser_module_version": full["parser"]["module_version"], "sex_report_status": full["parser"]["sex_report_status"],
        "reporting_dimension": full["reporting"]["dimension"], "neither_reason": full["reporting"]["neither_reason"],
        "explicit_unknown_with_values": full["reporting"]["explicit_unknown_with_values"],
        "declared_not_collected": full["reporting"]["declared_not_collected"],
        "composition_eligible": full["composition"]["eligible"],
        "composition_ineligible_reason": full["composition"]["ineligible_reason"],
        "incomplete_composition": full["composition"]["incomplete_composition"],
        "incomplete_composition_evaluated": full["composition"]["incomplete_composition_evaluated"],
        "gender_categories_status": gc["status"],
        "gender_exclusion_reason": (gc["exclusion"] or {}).get("reason") if gc["exclusion"] else None,
        "d3_total": gc["d3_total"], "d5_strict": gc["d5_strict"],
        "flag_two_question_table": gc["flags"]["two_question_table"],
        "flag_possible_double_count": gc["flags"]["possible_double_count"],
        "gc_labels": gc["labels"], "gc_unclassified_labels": gc["unclassified_labels"],
        "patients_only_status": full["patients_only"]["status"], "unresolved": full["unresolved"],
        "snapshot_date": snapshot_date,
    }
    for c in D3_CATEGORIES:
        r[f"gc_{c}"] = gc["categories"][c]
    for c in SEPARATE_ROWS:
        r[f"gc_{c}"] = gc["separate"][c]
    return {c: r.get(c) for c in COLUMNS}


def summarize(blocks: Iterable[dict]) -> dict:
    """The summary block (dashboard-summary.json "sexGenderAnalysis") from
    lean or full-derived lean blocks. Every number is a count over blocks or a
    sum of a published per-trial value; each sum names its denominator."""
    blocks = [b for b in blocks if isinstance(b, dict) and b.get("schema_version") == SCHEMA_VERSION]
    dims = {d: 0 for d in REPORTING_DIMENSIONS}
    reasons = {r: 0 for r in NEITHER_REASONS}
    gcs = {s: 0 for s in GENDER_CATEGORY_STATUSES}
    unresolved: dict = {}
    eu = elig = d3n = d5n = 0
    d3 = d5 = 0.0
    for b in blocks:
        d = b.get("reporting_dimension")
        if d in dims:
            dims[d] += 1
        r = b.get("neither_reason")
        if r in reasons:
            reasons[r] += 1
        if b.get("explicit_unknown_with_values") is True:
            eu += 1
        if b.get("composition_eligible") is True:
            elig += 1
        s = b.get("gender_categories_status")
        if s in gcs:
            gcs[s] += 1
        if s == "derived":
            if b.get("d3_total") is not None:
                d3 += b["d3_total"]; d3n += 1
            if b.get("d5_strict") is not None:
                d5 += b["d5_strict"]; d5n += 1
        for u in (b.get("unresolved") or []):
            unresolved[u] = unresolved.get(u, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "rules_version": RULES_VERSION,
        "layer": LAYER,
        "trials": len(blocks),
        "reportingDimension": dims,
        "neitherReason": reasons,
        "explicitUnknownWithValues": eu,
        "compositionEligible": elig,
        "incompleteComposition": {"evaluated": 0, "value_true": 0, "value_false": 0,
                                  "note": "not evaluated in this version (R-IC-01)"},
        "genderCategoriesStatus": gcs,
        "d3": {"participants": d3 if d3n else None, "trials": d3n,
               "denominator": "trials with gender_categories_status derived and a non-null d3_total"},
        "d5": {"participants": d5 if d5n else None, "trials": d5n,
               "denominator": "trials with gender_categories_status derived and a non-null d5_strict"},
        "unresolved": unresolved,
        "unresolvedRules": {k: UNRESOLVED_RULES[k] for k in OPEN_RULES},
        "decidedRules": dict(DECIDED_RULES),
    }


_BOOL_COLUMNS = {"explicit_unknown_with_values", "declared_not_collected", "composition_eligible",
                 "incomplete_composition", "incomplete_composition_evaluated", "flag_two_question_table",
                 "flag_possible_double_count"}
_FLOAT_COLUMNS = {*[f"gc_{c}" for c in D3_CATEGORIES], *[f"gc_{c}" for c in SEPARATE_ROWS], "d3_total", "d5_strict"}


def _from_csv(col: str, v: Any) -> Any:
    if col in JSON_COLUMNS:
        import json
        return json.loads(v) if v else ({} if col == "gc_labels" else [])
    if v == "" or v is None:
        return None
    if col in _BOOL_COLUMNS:
        return str(v).lower() == "true"
    if col in _FLOAT_COLUMNS:
        return float(v)
    return v


def read_table(path: str) -> list:
    """Read sex_gender_analysis.csv.gz back into typed rows (None for empty
    cells; bools, numbers and the JSON columns restored)."""
    import csv
    import gzip
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", newline="", encoding="utf-8") as fh:
        return [{k: _from_csv(k, v) for k, v in r.items()} for r in csv.DictReader(fh)]
