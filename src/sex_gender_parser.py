"""
sex_gender_parser.py

Baseline sex and gender parser for ClinicalTrials.gov API v2 records.
Python port of the R logic used for the sex and gender manuscript
(R/parsers.R, 2026-08-17 fix level; Rmd chunks `classify-reporting` and
`outcomes`, 2026-09-10). Prepared for the CivicSample dashboard backend.

Author of the R logic: Michael Green (Johns Hopkins). Port: 2026-09-14.
Co-author who must be able to run this without Michael: Maryam Aziz.

WHAT THIS MODULE DOES, IN ORDER
  1. select_sex_gender_measures(measures)
       Picks the baseline measures whose TITLE matches /sex|gender/ (case-
       insensitive). This is the upstream selection rule the paper's Python
       extraction used; it was verified on the 2026-06-09 extract (66,210
       trials, exact agreement). Measures such as "Female Reproductive Status"
       or "Age, female" are deliberately NOT selected.
  2. canon_bucket(label)
       Maps one class or category label to a canonical bucket:
       "female", "male", "unknown", "gender_diverse", "ambiguous", or ""
       (structural / orientation / unrecognized). Vocabulary and evaluation
       order are copied from parsers.R and must not be reordered.
  3. parse_trial(measures, enrollment=None)
       Deterministic parse of a trial's selected measures into counts and
       parser flags (port of det_parse_measure). Counts come from the
       sex/gender-typed measures when any exist, so a "Child Gender" or
       "Parent" table cannot leak into participant counts. Female/male are
       taken from the single fullest measure; unknown, gender_diverse and
       ambiguous are aggregated by MAX across measures (a Sex table and a
       Gender table re-count the same people, so max avoids double counting).
  4. classify_reporting(parsed)
       The five-state reporting status. THE THREE-STATE RULE lives here:
       "explicit_unknown_only" (registrant reported an Unknown category) and
       "not_reported" (no sex/gender measure at all) are different states and
       are never collapsed.
  5. derive_outcomes(parsed, status)
       reported_sex, reported_gender, reported_both, reported_any and the
       gender_labeled_binary_only audit flag, exactly as the manuscript
       defines them.

WHAT IS DIFFERENT FROM THE PAPER, AND WHY
  The paper's headline numbers come from a manually corrected column
  (Track 1). A dashboard cannot use manual corrections, so this module is the
  paper's deterministic re-parser (Track 2) promoted to production. The
  README reports how often Track 2 reproduces the manual truth on the
  2026-06-09 extract; treat that agreement as the fidelity ceiling.

DEPENDENCIES
  Standard library only. pandas is optional (parse_frame helper).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Optional

__version__ = "1.0.0"
PARSER_RULES_VERSION = "parsers.R@2026-08-17 / outcomes@2026-09-10"

# ---------------------------------------------------------------------------
# 1. Upstream measure selection
# ---------------------------------------------------------------------------

# A baseline measure is a candidate sex/gender table iff its title matches this.
# Verified against the paper's extraction on 66,210 trials (exact agreement).
MEASURE_TITLE_RX = re.compile(r"sex|gender", re.IGNORECASE)


def select_sex_gender_measures(measures: Optional[Iterable[dict]]) -> list[dict]:
    """Return the measures whose title matches /sex|gender/i, in source order."""
    out = []
    for m in measures or []:
        if not isinstance(m, dict):
            continue
        title = m.get("title")
        if title is not None and MEASURE_TITLE_RX.search(str(title)):
            out.append(m)
    return out


def measure_type(title: Optional[str]) -> Optional[str]:
    """
    Classify a measure title. Port of the mtype case_when in parsers.R.
      "sex"                    title starts with "sex:" or equals "sex"
      "sex_gender_customized"  title contains "customized"
      "gender"                 title starts with "gender"
      None                     no title
      "other"                  anything else (e.g. "Child Gender", "Parent Sex")
    Evaluation order matters: "Sex/Gender, Customized" is customized, not sex.
    """
    if title is None:
        return None
    tl = str(title).lower()
    if tl.startswith("sex:") or tl == "sex":
        return "sex"
    if "customized" in tl:
        return "sex_gender_customized"
    if tl.startswith("gender"):
        return "gender"
    return "other"


SG_TYPES = ("sex", "sex_gender_customized", "gender")

# ---------------------------------------------------------------------------
# 2. Label vocabulary (verbatim from parsers.R, R regex -> Python regex)
# ---------------------------------------------------------------------------
# Evaluation order in canon_bucket (first match wins; the order IS the rule):
#   1a. hard structural      -> ""              totals, arms, denominators, roles
#   1b. context structural   -> ""              race/arm/disposition with no sex word
#   2.  unknown              -> "unknown"       explicit non-response / not collected
#   3.  ambiguous            -> "ambiguous"     cis/trans COMBINED with a plain sex word
#   4.  gender_diverse       -> "gender_diverse"
#   5.  orientation          -> ""              sexual orientation is not gender
#   6.  female, then male    -> sex buckets
#   7.  anything else        -> ""              unrecognized, surface for review

CB_STRUCTURAL_RX = re.compile("|".join([
    r"^(all|both|neither|total|overall|any|sex|gender|count|multiple|two or more|na|n/a)$",
    r"^(all|both|any|either) ?genders?$", r"\bany gender\b", r"\ball genders?\b",
    r"^sex/gender baseline$", r"^single gender identity$",
    r"more than one (category|categories|race)", r"^number of participants$",
    r"^(patients?|participants?|subjects?|pharmacies|sites?|providers?|clinicians?)$",
    r"patients for whom", r"participants for whom",
    r"^(father|fathers|mother|mothers|grandmother|grandfather|parent|parents)$",
    r"^(child|children|infant|infants|neonates?)$",
    r"excluded from analysis", r"participated in [0-9]{4}",
    r"never treated", r"core participants", r"^pregnant$", r"^censored$",
]))

CB_CONTEXT_STRUCTURAL_RX = re.compile("|".join([
    r"white|black|african|asian|hispanic|latin[oax]|caucasian|american indian",
    r"native hawaiian|pacific islander|alaska native|middle eastern",
    r"\bcohort\b", r"\bextension\b", r"\barm\b", r"withdrew|withdrawn",
    r"^phase [ivx0-9]+ ?-",
]))

CB_UNKNOWN_RX = re.compile("|".join([
    r"unknown", r"not reported", r"unreported", r"not available", r"unavailable",
    r"not listed", r"not collected", r"not specified", r"unspecified", r"undifferentiated",
    r"not provided", r"not obtained", r"not documented", r"not measured", r"not captured",
    r"not recorded", r"not stated", r"not indicated", r"not given",
    r"not identif(ied|ies)\b",
    r"not asked", r"not known", r"no data", r"\bno\b[^.]{0,25}\bdata\b",
    r"did not (report|identify|collect|give|state|answer|respond|disclose|complete)",
    r"no (answer|response|selected answer)", r"\bmissing\b",
    r"prefer(s|red)? not", r"prefer not", r"(choose|chose|chooses) not to",
    r"do(es)? not wish", r"did not wish", r"refus", r"declin",
    r"de-?identified", r"undisclosed", r"not disclosed",
    r"\bnot sure\b", r"\bunsure\b",
]))

CB_CISTRANS_RX = re.compile(r"cisgender|transgender|trans-|cis-|\bcis\b|\btrans\b|trandgender")
CB_SEXWORD_RX = re.compile(r"female|male|wom[ae]n|\bm[ae]n\b|girl|boy")

CB_GENDER_DIVERSE_RX = re.compile("|".join([
    r"non[- ]?binary", r"nonconforming", r"non[- ]?conforming", r"gender ?queer",
    r"gender ?diverse", r"gender ?fluid", r"gender[- ]?expansive", r"gender[- ]?variant",
    r"gender[- ]?minorit", r"sexual or gender minorit", r"sex(ual)? and gender minorit",
    r"agender", r"bigender", r"pangender", r"third gender", r"another gender",
    r"other gender", r"two-? ?spirit", r"intersex", r"\bothers?\b",
    r"cisgender", r"transgender", r"trandgender",
    r"\btrans\b", r"trans[- /]?(man|men|wom[ae]n|masc|femin|person|people|ftm|mtf)",
    r"androgynous", r"\bfluid\b", r"\bdiverse\b", r"^ambiguous$",
    r"multi[- ]?gender", r"multiple gender", r"more than one gender",
    r"gender different from sex assigned at birth",
    r"self[- ]?(describe|described|identify|identifies|identification)",
    r"prefer to self", r"identify in another way",
    r"(another|different|additional) (identity|identification|description)",
    r"additional sex or gender", r"something else",
    r"(none|null) of (these|the above|the listed)", r"do not identify as any",
    r"tgncnb", r"\btgnc\b", r"questioning",
]))

CB_ORIENTATION_RX = re.compile("|".join([
    r"bisexual", r"\bqueer\b", r"\bgay\b", r"lesbian", r"asexual", r"pansexual",
    r"homosexual", r"heterosexual", r"\bstraight\b", r"sexual orientation",
]))

CB_FEMALE_RX = re.compile(r"\bfemal|wom[ae]n|girl")   # \bfemal also catches the "Femal" typo
CB_MALE_RX = re.compile(r"male|\bm[ae]n\b|boy")

BUCKETS = ("female", "male", "unknown", "gender_diverse", "ambiguous")

_WS_RX = re.compile(r"\s+")
_FOOTNOTE_RX = re.compile(r"[*\u2020\u2021]+$")   # trailing * † ‡


def cb_norm(label: Any) -> Optional[str]:
    """Lowercase, squish whitespace, drop trailing footnote marks. None for None/NaN."""
    if label is None:
        return None
    if isinstance(label, float) and math.isnan(label):
        return None
    z = str(label).strip().lower()
    z = _WS_RX.sub(" ", z)
    z = _FOOTNOTE_RX.sub("", z)
    return z.strip()


def _is_context_structural(z: str) -> bool:
    """Purely a race/arm/disposition row: no sex word, no identity token, no unknown token."""
    return (bool(CB_CONTEXT_STRUCTURAL_RX.search(z))
            and not CB_SEXWORD_RX.search(z)
            and not CB_CISTRANS_RX.search(z)
            and not CB_GENDER_DIVERSE_RX.search(z)
            and not CB_UNKNOWN_RX.search(z))


def canon_bucket(label: Any) -> str:
    """Map a label to one of BUCKETS or "" (excluded / unrecognized). Order is the rule."""
    z = cb_norm(label)
    if not z:
        return ""
    has_sexword = bool(CB_SEXWORD_RX.search(z))
    if CB_STRUCTURAL_RX.search(z):
        return ""
    if _is_context_structural(z):
        return ""
    if CB_UNKNOWN_RX.search(z):
        return "unknown"
    if CB_CISTRANS_RX.search(z) and has_sexword:
        return "ambiguous"
    if CB_GENDER_DIVERSE_RX.search(z):
        return "gender_diverse"
    if CB_ORIENTATION_RX.search(z):
        return ""
    if CB_FEMALE_RX.search(z) or z == "f":
        return "female"          # female before male: "female" contains "male"
    if CB_MALE_RX.search(z) or z == "m":
        return "male"
    return ""


def canon_unmapped_reason(label: Any) -> str:
    """Why a label maps to "": mapped | empty | structural_or_group | sexual_orientation | unrecognized."""
    z = cb_norm(label)
    if not z:
        return "empty"
    if canon_bucket(z):
        return "mapped"
    if CB_STRUCTURAL_RX.search(z) or _is_context_structural(z):
        return "structural_or_group"
    if CB_ORIENTATION_RX.search(z):
        return "sexual_orientation"
    return "unrecognized"


# ---------------------------------------------------------------------------
# 3. Deterministic measure parser (port of det_parse_one_measure / det_parse_measure)
# ---------------------------------------------------------------------------

def _num(v: Any) -> Optional[float]:
    """as.numeric with NA on failure. API values are strings; 'NA' -> None."""
    if v is None:
        return None
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


def choose_total(measurements: Optional[list]) -> dict:
    """
    Pick the trial-level total from a list of {groupId, value} measurements.
    Preference: a group equal to the sum of the others (the Total column), else
    the LAST group (positional fallback, flagged). Port of choose_total().
    NOTE (inherited limitation): with two equal groups, e.g. [5, 5], the first
    is read as the total (5), because 5 == sum(others). Flagged upstream by
    flag_no_total_sumcheck only when the fallback fires, not in this case.
    """
    if not isinstance(measurements, list) or not measurements:
        return {"total": None, "by_position": False, "sumcheck": None}
    vals = []
    for m in measurements:
        if isinstance(m, dict):
            x = _num(m.get("value"))
            if x is not None:
                vals.append(x)
    if not vals:
        return {"total": None, "by_position": False, "sumcheck": None}
    if len(vals) == 1:
        return {"total": vals[0], "by_position": False, "sumcheck": None}
    s = sum(vals)
    for v in vals:
        if abs(v - (s - v)) < 1e-6:
            return {"total": v, "by_position": False, "sumcheck": True}
    return {"total": vals[-1], "by_position": True, "sumcheck": False}


@dataclass
class ParsedMeasure:
    title: Optional[str] = None
    layout: Optional[str] = None          # "standard" | "customized"
    mtype: Optional[str] = None           # measure_type(title)
    param_type: Optional[str] = None      # COUNT_OF_PARTICIPANTS | COUNT_OF_UNITS | NUMBER ...
    unit_of_measure: Optional[str] = None
    female: Optional[float] = None
    male: Optional[float] = None
    unknown: Optional[float] = None
    gender_diverse: Optional[float] = None
    ambiguous: Optional[float] = None
    n_classes: int = 0
    has_denoms: bool = False
    raw_has_unknown: bool = False
    flag_total_by_position: bool = False
    flag_no_total_sumcheck: bool = False
    flag_multiclass_timepoint: bool = False
    flag_customized_layout: bool = False
    flag_empty_category: bool = False
    flag_unmapped_layout: bool = False
    # audit trail: which source labels fed each bucket, and which mapped to nothing
    unknown_labels: list = field(default_factory=list)
    gender_diverse_labels: list = field(default_factory=list)
    ambiguous_labels: list = field(default_factory=list)
    unmapped_labels: list = field(default_factory=list)


def parse_measure(m: dict) -> ParsedMeasure:
    """Parse ONE API v2 baseline measure dict. Port of det_parse_one_measure()."""
    r = ParsedMeasure()
    if not isinstance(m, dict):
        return r
    title = m.get("title")
    r.title = None if title is None else str(title)
    r.mtype = measure_type(r.title)
    pt = m.get("paramType");      r.param_type = None if pt is None else str(pt)
    um = m.get("unitOfMeasure");  r.unit_of_measure = None if um is None else str(um)

    classes = m.get("classes")
    if not isinstance(classes, list):
        classes = []
    r.n_classes = len(classes)
    r.has_denoms = any(isinstance(cl, dict) and cl.get("denoms") is not None for cl in classes)

    acc = {b: None for b in BUCKETS}

    def put(b: str, val: Optional[float]) -> None:
        if b and val is not None:
            acc[b] = (acc[b] or 0.0) + val

    def note_label(b: str, raw: str) -> None:
        if b == "unknown" and raw not in r.unknown_labels:
            r.unknown_labels.append(raw)
        elif b == "gender_diverse" and raw not in r.gender_diverse_labels:
            r.gender_diverse_labels.append(raw)
        elif b == "ambiguous" and raw not in r.ambiguous_labels:
            r.ambiguous_labels.append(raw)
        elif b == "" and raw and raw not in r.unmapped_labels:
            r.unmapped_labels.append(raw)

    class_buckets = [canon_bucket((cl or {}).get("title")) if isinstance(cl, dict) else "" for cl in classes]
    is_custom = any(b in BUCKETS for b in class_buckets)

    if is_custom:
        # Customized layout: the sex/gender labels are CLASS titles (rows), and each
        # class carries one or more categories whose measurements are summed via choose_total.
        r.layout = "customized"
        r.flag_customized_layout = True
        for cl in classes:
            if not isinstance(cl, dict):
                continue
            raw_t = "" if cl.get("title") is None else str(cl.get("title"))
            lbl = canon_bucket(raw_t)
            note_label(lbl, raw_t)
            if lbl == "unknown":
                r.raw_has_unknown = True
            meas = []
            for cat in (cl.get("categories") or []):
                if isinstance(cat, dict) and isinstance(cat.get("measurements"), list):
                    meas.extend(cat["measurements"])
            ct = choose_total(meas)
            if ct["by_position"]:
                r.flag_total_by_position = True
            if ct["sumcheck"] is False:
                r.flag_no_total_sumcheck = True
            put(lbl, ct["total"])
    else:
        # Standard layout: one class (often untitled), categories are the sex/gender labels.
        r.layout = "standard"
        if len(classes) > 1:
            r.flag_multiclass_timepoint = True
        cl = classes[0] if classes and isinstance(classes[0], dict) else {}
        cats = cl.get("categories")
        if not isinstance(cats, list):
            cats = []
        mapped_any = False
        for cat in cats:
            if not isinstance(cat, dict):
                continue
            raw_t = "" if cat.get("title") is None else str(cat.get("title"))
            lbl = canon_bucket(raw_t)
            if not raw_t or not lbl:
                r.flag_empty_category = True
                note_label("", raw_t)
                continue
            mapped_any = True
            note_label(lbl, raw_t)
            if lbl == "unknown":
                r.raw_has_unknown = True
            ct = choose_total(cat.get("measurements") if isinstance(cat.get("measurements"), list) else [])
            if ct["by_position"]:
                r.flag_total_by_position = True
            if ct["sumcheck"] is False:
                r.flag_no_total_sumcheck = True
            put(lbl, ct["total"])
        if not mapped_any:
            r.flag_unmapped_layout = True

    r.female, r.male, r.unknown = acc["female"], acc["male"], acc["unknown"]
    r.gender_diverse, r.ambiguous = acc["gender_diverse"], acc["ambiguous"]
    return r


@dataclass
class ParsedTrial:
    # presence
    raw_present: bool = False          # at least one sex/gender-titled measure existed
    parse_ok: bool = True              # False only if the input could not be read at all
    n_measures: int = 0
    has_sex_table: bool = False        # any measure typed "sex"
    has_gender_table: bool = False     # any measure typed "gender" or "sex_gender_customized"
    # count-driving (primary) measure
    measure_title: Optional[str] = None
    measure_type: Optional[str] = None
    layout: Optional[str] = None
    param_type: Optional[str] = None
    unit_of_measure: Optional[str] = None
    is_participant_count: Optional[bool] = None   # False only for COUNT_OF_UNITS
    # counts
    n_female: Optional[float] = None
    n_male: Optional[float] = None
    n_unknown: Optional[float] = None
    n_gender_diverse: Optional[float] = None
    n_ambiguous_gender: Optional[float] = None
    n_total_parsed: Optional[float] = None
    # flags (any over all measures)
    n_classes: Optional[int] = None
    has_denoms: bool = False
    raw_has_unknown: bool = False
    flag_total_by_position: bool = False
    flag_no_total_sumcheck: bool = False
    flag_multiclass_timepoint: bool = False
    flag_customized_layout: bool = False
    flag_empty_category: bool = False
    flag_unmapped_layout: bool = False
    flag_exceeds_enrollment: bool = False
    flag_nonparticipant_units: bool = False
    # audit labels (from the count-driving measure set)
    unknown_labels: list = field(default_factory=list)
    gender_diverse_labels: list = field(default_factory=list)
    ambiguous_labels: list = field(default_factory=list)
    unmapped_labels: list = field(default_factory=list)   # over ALL measures (review inbox)
    # ADDED FOR THE DASHBOARD (not in parsers.R). Sub-structure of "uninformative":
    #   no_measurements   template posted, categories carry no measurement objects
    #   all_values_na     every value is "NA"
    #   all_values_zero   every value is 0
    #   no_mapped_labels  counts exist but no label maps to a bucket (e.g. untitled
    #                     single category with unitOfMeasure "Male participants")
    #   other             anything else
    # None when the status is not uninformative.
    uninformative_reason: Optional[str] = None
    # Free-text declaration that sex/gender was not collected, read from
    # populationDescription / description / measurement comments. Audit flag only;
    # it does not change the status (see README, decision D2).
    declared_not_collected: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("unknown_labels", "gender_diverse_labels", "ambiguous_labels", "unmapped_labels"):
            d[k] = "; ".join(d[k]) if d[k] else None
        return d


def _max_or_none(vals: Iterable[Optional[float]]) -> Optional[float]:
    v = [x for x in vals if x is not None]
    return max(v) if v else None


def parse_trial(measures: Optional[list], enrollment: Optional[float] = None,
                preselected: bool = False) -> ParsedTrial:
    """
    Parse a trial's baseline measures. Port of det_parse_measure().

    measures     resultsSection.baselineCharacteristicsModule.measures (list of dicts),
                 or an already-selected list if preselected=True.
    enrollment   protocolSection.designModule.enrollmentInfo.count, for the
                 exceeds-enrollment flag. Optional.

    Counts come from the sex/gender-typed measures when any exist (types sex,
    sex_gender_customized, gender), else from all selected measures. Female and
    male come from the ONE measure with the largest female+male (the fullest sex
    breakdown; first wins on ties). Unknown, gender_diverse and ambiguous are the
    MAX across those measures. Presence indicators and flags are ANY over all
    selected measures.
    """
    out = ParsedTrial()
    if measures is None:
        return out
    sel = list(measures) if preselected else select_sex_gender_measures(measures)
    if not sel:
        return out
    out.raw_present = True
    ms = [parse_measure(m) for m in sel]
    out.n_measures = len(ms)
    out.has_sex_table = any(r.mtype == "sex" for r in ms)
    out.has_gender_table = any(r.mtype in ("gender", "sex_gender_customized") for r in ms)

    is_sg = [r.mtype in SG_TYPES for r in ms]
    ms_cnt = [r for r, f in zip(ms, is_sg) if f] or ms

    fm = [(r.female or 0.0) + (r.male or 0.0) for r in ms_cnt]
    prim = ms_cnt[fm.index(max(fm))]
    out.measure_title = prim.title
    out.measure_type = prim.mtype
    out.layout = prim.layout
    out.param_type = prim.param_type
    out.unit_of_measure = prim.unit_of_measure
    out.is_participant_count = (prim.param_type is None) or (prim.param_type.upper() != "COUNT_OF_UNITS")
    out.n_female = prim.female
    out.n_male = prim.male
    out.n_unknown = _max_or_none(r.unknown for r in ms_cnt)
    out.n_gender_diverse = _max_or_none(r.gender_diverse for r in ms_cnt)
    out.n_ambiguous_gender = _max_or_none(r.ambiguous for r in ms_cnt)

    out.n_classes = max(r.n_classes for r in ms)
    out.has_denoms = any(r.has_denoms for r in ms)
    out.raw_has_unknown = any(r.raw_has_unknown for r in ms)
    for fl in ("flag_total_by_position", "flag_no_total_sumcheck", "flag_multiclass_timepoint",
               "flag_customized_layout", "flag_empty_category", "flag_unmapped_layout"):
        setattr(out, fl, any(getattr(r, fl) for r in ms))

    tot = sum(x for x in (out.n_female, out.n_male, out.n_unknown,
                          out.n_gender_diverse, out.n_ambiguous_gender) if x is not None)
    out.n_total_parsed = tot
    enr = _num(enrollment)
    if enr is not None and tot > enr:
        out.flag_exceeds_enrollment = True
    out.flag_nonparticipant_units = bool(prim.param_type) and prim.param_type.upper() == "COUNT_OF_UNITS"

    def uniq(seq):
        seen, o = set(), []
        for x in seq:
            if x not in seen:
                seen.add(x); o.append(x)
        return o
    out.unknown_labels = uniq(l for r in ms_cnt for l in r.unknown_labels)
    out.gender_diverse_labels = uniq(l for r in ms_cnt for l in r.gender_diverse_labels)
    out.ambiguous_labels = uniq(l for r in ms_cnt for l in r.ambiguous_labels)
    out.unmapped_labels = uniq(l for r in ms for l in r.unmapped_labels)

    # Dashboard-only diagnostics (see ParsedTrial docstring).
    out.declared_not_collected = _declared_not_collected(sel)
    if tot == 0 and (out.n_female is None or out.n_female == 0) and (out.n_male is None or out.n_male == 0):
        out.uninformative_reason = _uninformative_reason(sel)
    return out


NOT_COLLECTED_RX = re.compile(
    r"not (be )?(collected|recorded|captured|obtained|gathered|reported|available|analy[sz]ed|assessed|tracked)"
    r"|no(t)? .{0,40}(collected|recorded)|did not collect|was not (a )?(required|pertinent)", re.IGNORECASE)


def _declared_not_collected(selected: list) -> bool:
    """True if any free-text field on a selected measure says sex/gender was not collected."""
    for m in selected:
        for k in ("populationDescription", "description"):
            if m.get(k) and NOT_COLLECTED_RX.search(str(m[k])):
                return True
        for cl in (m.get("classes") or []):
            if not isinstance(cl, dict):
                continue
            for cat in (cl.get("categories") or []):
                if not isinstance(cat, dict):
                    continue
                for mm in (cat.get("measurements") or []):
                    if isinstance(mm, dict) and mm.get("comment") and NOT_COLLECTED_RX.search(str(mm["comment"])):
                        return True
    return False


def _uninformative_reason(selected: list) -> str:
    n_meas = n_vals = nonzero = mapped = 0
    for m in selected:
        for cl in (m.get("classes") or []):
            if not isinstance(cl, dict):
                continue
            if canon_bucket(cl.get("title")):
                mapped += 1
            for cat in (cl.get("categories") or []):
                if not isinstance(cat, dict):
                    continue
                if canon_bucket(cat.get("title")):
                    mapped += 1
                for mm in (cat.get("measurements") or []):
                    if not isinstance(mm, dict):
                        continue
                    n_meas += 1
                    x = _num(mm.get("value"))
                    if x is not None:
                        n_vals += 1
                        nonzero += (x != 0)
    if n_meas == 0:
        return "no_measurements"
    if n_vals == 0:
        return "all_values_na"
    if nonzero == 0:
        return "all_values_zero"
    if mapped == 0:
        return "no_mapped_labels"
    return "other"


# ---------------------------------------------------------------------------
# 4. Reporting status (port of the `classify-reporting` chunk)
# ---------------------------------------------------------------------------

REPORT_STATUS_LEVELS = ("reported", "explicit_unknown_only", "uninformative", "not_reported", "parse_error")


def classify_reporting(p: ParsedTrial) -> str:
    """
    Five-state sex_report_status. Single source of truth for the Explicit Unknown
    vs Not Reported separation; nothing downstream may re-derive missingness.

      parse_error            input existed but could not be read
      not_reported           no sex/gender-titled measure at all (state 3, absent)
      reported               any female/male/gender_diverse count > 0, OR any
                             cis/trans-labelled (ambiguous) count > 0
      explicit_unknown_only  the only non-zero bucket is Unknown (state 2)
      uninformative          a sex/gender measure exists but carries no usable
                             count (total-only rows, all-zero cells, declared
                             "not collected" with n = 0, unrecognized labels)
    """
    if not p.parse_ok:
        return "parse_error"
    if not p.raw_present:
        return "not_reported"
    known = sum(x for x in (p.n_female, p.n_male, p.n_gender_diverse) if x is not None)
    if known > 0:
        return "reported"
    if (p.n_ambiguous_gender or 0) > 0:
        return "reported"
    if (p.n_unknown or 0) > 0:
        return "explicit_unknown_only"
    return "uninformative"


# ---------------------------------------------------------------------------
# 5. Outcomes (port of the `outcomes` chunk, strict_sex_only = FALSE)
# ---------------------------------------------------------------------------

def derive_outcomes(p: ParsedTrial, status: str, strict_sex_only: bool = False) -> dict:
    """
    reported_sex     COUNT-based: status reported AND female+male > 0, under ANY
                     measure title (sex breakdowns appear under "Child Sex", "Parent").
                     strict_sex_only=True additionally requires measure_type == "sex".
    reported_gender  CATEGORY-based: status reported AND (gender_diverse > 0 OR
                     ambiguous > 0). A Gender-titled measure carrying only
                     Male/Female is NOT gender tracking; it is scored as sex.
    reported_both    reported_sex AND reported_gender.
    reported_any     status reported.
    parse_error      -> all four are None (dropped, never scored as non-reporting).
    gender_labeled_binary_only  audit flag: gender-titled measure present, no
                     diverse/ambiguous categories, F+M > 0 (the reclassified group).
    """
    if status == "parse_error":
        return {"reported_sex": None, "reported_gender": None, "reported_both": None,
                "reported_any": None, "gender_labeled_binary_only": None}
    has_fm = (p.n_female or 0) + (p.n_male or 0) > 0
    reported_sex = (status == "reported") and has_fm and ((not strict_sex_only) or p.measure_type == "sex")
    reported_gender = (status == "reported") and ((p.n_gender_diverse or 0) > 0 or (p.n_ambiguous_gender or 0) > 0)
    return {
        "reported_sex": bool(reported_sex),
        "reported_gender": bool(reported_gender),
        "reported_both": bool(reported_sex and reported_gender),
        "reported_any": status == "reported",
        "gender_labeled_binary_only": bool(p.has_gender_table and (p.n_gender_diverse or 0) <= 0
                                           and (p.n_ambiguous_gender or 0) <= 0 and has_fm),
    }


# ---------------------------------------------------------------------------
# 6. One-call convenience and pandas helper
# ---------------------------------------------------------------------------

def parse_study_record(study: dict) -> dict:
    """
    From a full API v2 study JSON (as returned by /api/v2/studies/{nct}), return
    the flat row: parsed counts + sex_report_status + outcomes.
    """
    rs = (study.get("resultsSection") or {})
    measures = ((rs.get("baselineCharacteristicsModule") or {}).get("measures"))
    enr = (((study.get("protocolSection") or {}).get("designModule") or {})
           .get("enrollmentInfo") or {}).get("count")
    nct = ((study.get("protocolSection") or {}).get("identificationModule") or {}).get("nctId")
    return parse_measures_row(measures, enr, nct_id=nct)


def parse_measures_row(measures: Optional[list], enrollment: Optional[float] = None,
                       nct_id: Optional[str] = None, preselected: bool = False) -> dict:
    """Flat dict for one trial: identifiers, parsed fields, status, outcomes."""
    p = parse_trial(measures, enrollment, preselected=preselected)
    status = classify_reporting(p)
    row = {"nct_id": nct_id, **p.to_dict(), "sex_report_status": status,
           **derive_outcomes(p, status), "parser_rules_version": PARSER_RULES_VERSION}
    return row


def parse_frame(df, measures_col: str, enrollment_col: Optional[str] = None,
                nct_col: str = "nct_id", preselected: bool = False):
    """
    pandas helper. `measures_col` holds either a Python list of measure dicts per
    row or a JSON / Python-literal string of one. Returns a DataFrame, one row per
    input row, in input order.
    """
    import json
    import ast
    import pandas as pd

    def load(v):
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None, True
        if isinstance(v, (list, dict)):
            return (v if isinstance(v, list) else [v]), True
        st = str(v).strip()
        if st in ("", "[]", "nan", "None", "['']"):
            return None, True
        try:
            o = json.loads(st)
        except Exception:
            try:
                o = ast.literal_eval(st)
            except Exception:
                return None, False
        if isinstance(o, dict):
            o = [o]
        return (o if isinstance(o, list) else None), isinstance(o, list)

    rows = []
    for _, rec in df.iterrows():
        obj, ok = load(rec[measures_col])
        enr = rec[enrollment_col] if enrollment_col else None
        if not ok:
            p = ParsedTrial(raw_present=True, parse_ok=False)
            status = classify_reporting(p)
            rows.append({"nct_id": rec.get(nct_col), **p.to_dict(), "sex_report_status": status,
                         **derive_outcomes(p, status), "parser_rules_version": PARSER_RULES_VERSION})
        else:
            rows.append(parse_measures_row(obj, enr, nct_id=rec.get(nct_col), preselected=preselected))
    return pd.DataFrame(rows)


def label_audit(labels: Iterable[str]):
    """One row per distinct label: bucket and unmapped reason. The curation inbox builder."""
    seen = {}
    for l in labels:
        if l is None:
            continue
        if l not in seen:
            seen[l] = (canon_bucket(l), canon_unmapped_reason(l))
    return [{"label": k, "bucket": v[0], "reason": v[1]} for k, v in seen.items()]
