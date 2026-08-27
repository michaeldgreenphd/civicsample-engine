"""
Gender Identity Data Extractor

Extracts gender identity data from baseline characteristics.
Strictly decoupled from biological sex: Female/Male are NEVER mapped to
Woman/Man.  Only tables explicitly labeled as "gender" are parsed here.
"""
from typing import Dict, List, Optional
from src.utils import clean_demographic_label, is_sex_qualified_identity_label

# ── Strict standardized target array for gender identity ──
GENDER_CATEGORIES = ["Woman", "Man", "Non-binary", "Transgender", "Other", "Unknown or Not Reported"]

# Direct-match labels → standardized gender category
_GENDER_LABEL_MAP = {
    "woman":                    "woman",
    "women":                    "woman",
    "cisgender woman":          "woman",
    "cis woman":                "woman",
    "man":                      "man",
    "men":                      "man",
    "cisgender man":            "man",
    "cis man":                  "man",
    "non-binary":               "nonbinary",
    "nonbinary":                "nonbinary",
    "genderqueer":              "nonbinary",
    "gender non-conforming":    "nonbinary",
    "agender":                  "nonbinary",
    "genderfluid":              "nonbinary",
    "two-spirit":               "nonbinary",
    "transgender":              "transgender",
    "transgender woman":        "transgender",
    "trans woman":              "transgender",
    "transgender man":          "transgender",
    "trans man":                "transgender",
    "other":                    "other",
    "unknown":                  "unknown",
    "not reported":             "unknown",
    "prefer not to say":        "unknown",
    # Explicit synonyms reconciled against the manuscript's .known_buckets
    # table (real labels observed in production data) — promoted from the
    # generic low-confidence catch-all to high-confidence, auditable entries.
    "not available":            "unknown",
    "not collected":            "unknown",
    "not disclosed":            "unknown",
    "not specified":            "unknown",
    "unspecified":              "unknown",
    "de-identified":            "unknown",
    "missing":                  "unknown",
    "missing data":             "unknown",
    "prefer not to answer":     "unknown",
    "prefer not to disclose":   "unknown",
    "decline to answer":        "unknown",
    "declined":                 "unknown",
    "unknown or not reported":  "unknown",
}

# Labels that are strictly biological sex — must NEVER be mapped to gender
_SEX_ONLY_LABELS = {"female", "f", "females", "male", "m", "males", "intersex"}

GENDER_TABLE_KEYWORDS = ["gender", "gender identity"]

_MEASUREMENT_LABELS = {"count", "number", "n", "total", "value", "mean", "median"}

# Keywords that mark an unmapped label as still-plausibly gender-related
# (mirrors _PLAUSIBLE_RACE_KEYWORDS in race_extractor.py / _PLAUSIBLE_SEX_KEYWORDS
# in sex_extractor.py). An unmapped label containing none of these is likely
# noise rather than genuine gender-reporting data.
_PLAUSIBLE_GENDER_KEYWORDS = {
    "gender", "identity", "woman", "women", "man", "men", "boy", "girl",
    "female", "male", "non-binary", "nonbinary", "non binary", "binary",
    "transgender", "trans", "cisgender", "cis", "queer", "fluid", "agender",
    "two-spirit", "two spirit", "unknown", "unreport", "ambig",
    "prefer not", "declin", "self-describ", "self describ", "self-identif", "self identif",
    "not report", "not specif", "not disclos", "not avail", "not collect",
    "not provided", "not sure", "no data", "no response", "did not",
    "chose not", "no selected", "refus",
    "undisclosed", "unavailable", "missing", "unspecified", "de-identif",
    "withheld", "other", "diverse",
    # Culturally-specific gender-identity terms observed in real survey data —
    # legitimate reported categories, never noise.
    "muxe", "mahu", "travesti",
}


def _should_quarantine_gender_label(mapping: Dict) -> bool:
    """True for an unmapped label with no plausible gender-related keyword.

    Only fires for the generic low-confidence catch-all ("unmapped" flag on
    an "unknown" category) — the deliberate sex_qualified_identity_label
    routing decision above is never quarantined.
    """
    if mapping.get("category") != "unknown" or "unmapped" not in mapping.get("flags", []):
        return False
    label_lower = clean_demographic_label(mapping["original"]).lower()
    return not any(kw in label_lower for kw in _PLAUSIBLE_GENDER_KEYWORDS)


def is_gender_table(title: str) -> bool:
    """Check if a baseline measure is about gender identity (Context B)."""
    title_lower = title.lower()
    # Exclude combined Sex/Gender tables (handled by orchestrator)
    if "sex" in title_lower and "gender" in title_lower:
        return False
    return any(kw in title_lower for kw in GENDER_TABLE_KEYWORDS)


def map_gender_label(label: str) -> Optional[Dict]:
    """Map a label to a gender category, returning None if it's a sex-only label.

    Returns None for biological sex labels so callers can route them
    to the sex extractor instead of cross-mapping.
    """
    label_clean = clean_demographic_label(label)
    label_lower = label_clean.lower()

    # Reject sex-only labels outright
    if label_lower in _SEX_ONLY_LABELS:
        return None

    # Cis/trans + sex-word labels ("Transgender Female", "Cisgender Male")
    # are ambiguous — route to gender "unknown" rather than guessing they
    # mean the same thing as "Transgender Woman"/"Cisgender Man" (which stay
    # on their normal, intentional buckets via _GENDER_LABEL_MAP above).
    if is_sex_qualified_identity_label(label_lower):
        return {"category": "unknown", "confidence": "low", "original": label_clean,
                "flags": ["sex_qualified_identity_label"]}

    # Direct mapping
    if label_lower in _GENDER_LABEL_MAP:
        return {
            "category": _GENDER_LABEL_MAP[label_lower],
            "confidence": "high",
            "original": label_clean,
            "flags": [],
        }

    # Unknown / unmapped
    return {"category": "unknown", "confidence": "low", "original": label_clean, "flags": ["unmapped"]}


def _extract_rows_from_measure(measure: dict, overall_group_id=None,
                               fallback_denom: int = None) -> List[Dict]:
    """Low-level row extraction: yields (label, count) pairs.

    Percentage-aware: if the measure reports percentages, values are
    converted to estimated counts using the measure's Number Analyzed,
    or the study enrollment as a fallback for cluster-randomized studies.
    """
    from src.utils import sum_measurements, is_percentage_measure, get_measure_denom

    is_pct = is_percentage_measure(measure)
    denom = None
    if is_pct:
        denom = get_measure_denom(measure, overall_group_id)
        if denom is None:
            denom = fallback_denom

    rows = []
    for cls in measure.get("classes", []):
        categories = cls.get("categories", [])
        if categories:
            for cat in categories:
                cat_title = (cat.get("title") or "").strip()
                if cat_title.lower() in _MEASUREMENT_LABELS:
                    label = cls.get("title", "").strip()
                else:
                    label = cat_title or cls.get("title", "").strip()
                if not label:
                    continue
                count = sum_measurements(cat.get("measurements", []), overall_group_id,
                                         is_pct=is_pct, denom=denom)
                rows.append({"label": label, "count": count})
        else:
            label = cls.get("title", "").strip()
            if not label:
                continue
            count = sum_measurements(cls.get("measurements", []), overall_group_id,
                                     is_pct=is_pct, denom=denom)
            rows.append({"label": label, "count": count})
    return rows


def extract_gender_from_measure(measure: dict, overall_group_id=None,
                                fallback_denom: int = None) -> List[Dict]:
    """Extract gender data from a standard gender table (Context B).

    All rows are routed to gender.  Sex-only labels are mapped to unknown.
    """
    results = []
    for row in _extract_rows_from_measure(measure, overall_group_id, fallback_denom):
        mapping = map_gender_label(row["label"])
        if mapping is None:
            # Sex-only label in a gender table — treat as unknown gender
            mapping = {"category": "unknown", "confidence": "low",
                       "original": row["label"], "flags": ["sex_label_in_gender_table"]}
        mapping["count"] = row["count"]
        results.append(mapping)
    return results


def extract_gender_data(study: dict) -> Dict:
    """Extract all gender data from a study.

    Unified iterative loop — exhaustively checks all available modules:
      1) Standard gender tables (Context B) → all rows to gender
      2) Combined Sex/Gender tables (Context C) → row-level routing, gender portion
      3) Customized gender tables → handled same as standard
    Only declares "Not Reported" after checking all tables.
    """
    from src.utils import get_baseline_measures, get_overall_group_id, get_total_baseline_participants
    from src.sex_extractor import is_combined_sex_gender_table, extract_sex_from_combined_measure

    measures = get_baseline_measures(study)
    overall_group_id = get_overall_group_id(study)
    total_participants = get_total_baseline_participants(study, overall_group_id)

    result = {
        "reported": False,
        "totals": {
            "woman": 0,
            "man": 0,
            "nonbinary": 0,
            "transgender": 0,
            "other": 0,
            "unknown": 0,
        },
        # unknown_explicit / unknown_inferred disaggregate totals["unknown"]
        # into its two sources (see the denominator-balancing comment below).
        # totals["unknown"] always equals their sum — current dashboard
        # display is unaffected; this is purely additive.
        "unknown_explicit": 0,
        "unknown_inferred": 0,
        "raw_categories": [],
        "quarantined_labels": [],
        "flags": [],
    }

    combined_table_found = False

    for measure in measures:
        title = measure.get("title", "")

        # Context C: combined Sex/Gender table — extract only gender rows
        if is_combined_sex_gender_table(title):
            combined_table_found = True
            _sex_rows, gender_rows = extract_sex_from_combined_measure(
                measure, overall_group_id, fallback_denom=total_participants
            )
            if gender_rows:
                result["reported"] = True
                for cat in gender_rows:
                    if _should_quarantine_gender_label(cat):
                        result["quarantined_labels"].append({
                            "original": cat["original"],
                            "count": cat["count"],
                            "reason": "unmapped_non_gender_label",
                        })
                        result["flags"].append("quarantined_label")
                        continue
                    result["raw_categories"].append(cat)
                    result["totals"][cat["category"]] += cat["count"]
                    result["flags"].extend(cat["flags"])
                result["flags"].append("from_combined_table")
            continue

        # Standard or Customized gender table (Context B)
        if not is_gender_table(title):
            continue

        result["reported"] = True
        categories = extract_gender_from_measure(measure, overall_group_id,
                                                 fallback_denom=total_participants)
        for cat in categories:
            # Quarantine irrelevant unmapped labels that leaked in via a
            # gender-titled table (e.g. survey noise unrelated to gender).
            if _should_quarantine_gender_label(cat):
                result["quarantined_labels"].append({
                    "original": cat["original"],
                    "count": cat["count"],
                    "reason": "unmapped_non_gender_label",
                })
                result["flags"].append("quarantined_label")
                continue
            result["raw_categories"].append(cat)
            result["totals"][cat["category"]] += cat["count"]
            result["flags"].extend(cat["flags"])

    # "Not Collected" enforcement: if a combined table was found but yielded
    # no gender rows (only Female/Male), explicitly mark as not collected
    if combined_table_found and not result["reported"]:
        result["flags"] = ["not_collected_from_combined_table"]

    # All-zero rejection
    if result["reported"] and all(v == 0 for v in result["totals"].values()):
        result["reported"] = False
        result["totals"] = {k: 0 for k in result["totals"]}
        result["raw_categories"] = []
        result["flags"] = ["all_zero_rejection"]

    # Denominator balancing
    if result["reported"]:
        # Snapshot the "unknown" total accumulated purely from table rows
        # (explicit "Unknown" rows, sex_qualified_identity_label routing,
        # etc.) BEFORE any algorithmic denominator-gap remainder is folded
        # in. This is the row-sum-only figure a raw extraction (no
        # denominator inference) would have produced.
        result["unknown_explicit"] = result["totals"]["unknown"]
        total_participants = get_total_baseline_participants(study, overall_group_id)
        if total_participants is not None and total_participants > 0:
            reported_sum = sum(result["totals"].values())
            remainder = total_participants - reported_sum
            if remainder > 0:
                result["totals"]["unknown"] += remainder
                result["unknown_inferred"] = remainder
                result["flags"].append("denominator_balanced")

    result["flags"] = list(set(result["flags"]))
    return result
