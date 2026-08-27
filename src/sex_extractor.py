"""
Sex Data Extractor

Extracts biological sex data from baseline characteristics.
Implements context-aware routing: only biological sex labels (Female, Male)
are mapped here.  Gender identity labels (Woman, Man) are never cross-mapped.
"""
from typing import Dict, List, Optional
from src.utils import clean_demographic_label, is_sex_qualified_identity_label

# ── Strict standardized target array for biological sex ──
SEX_CATEGORIES = ["Female", "Male", "Unknown or Not Reported"]

# Direct-match labels → standardized sex category (lowercase keys for case-insensitive lookup)
_SEX_LABEL_MAP = {
    "female":           "female",
    "f":                "female",
    "females":          "female",
    "male":             "male",
    "m":                "male",
    "males":            "male",
    "intersex":         "unknown",
    "unknown":          "unknown",
    "not reported":     "unknown",
    "other":            "unknown",
    # Explicit synonyms reconciled against the manuscript's .known_buckets
    # table (real labels observed in production data) — promoted from the
    # generic low-confidence catch-all to high-confidence, auditable entries
    # so the quarantine/unmapped-review surface isn't swamped with already-
    # understood phrasings. All route to "unknown" (reported, not missing).
    "not available":        "unknown",
    "not collected":        "unknown",
    "not disclosed":        "unknown",
    "not specified":        "unknown",
    "unspecified":          "unknown",
    "undifferentiated":     "unknown",
    "de-identified":        "unknown",
    "missing":              "unknown",
    "missing data":         "unknown",
    "prefer not to answer": "unknown",
    "prefer not to say":    "unknown",
    "prefer not to disclose": "unknown",
    "decline to answer":    "unknown",
    "declined":             "unknown",
    "unknown or not reported": "unknown",
    "unknown/not reported": "unknown",
    "not reported/unknown": "unknown",
}

# Labels that are strictly gender identity — must NEVER be mapped to sex
_GENDER_ONLY_LABELS = {
    "woman", "women", "man", "men",
    "non-binary", "nonbinary", "genderqueer", "gender non-conforming",
    "agender", "genderfluid", "two-spirit",
    "cisgender woman", "cis woman", "transgender woman", "trans woman",
    "cisgender man", "cis man", "transgender man", "trans man",
    "transgender", "prefer not to say",
}

SEX_TABLE_KEYWORDS = ["sex", "biological sex"]

# Titles containing these tokens are NOT sex tables even though "sex" is a
# substring of the title (e.g. "Sexual Orientation"). Without this guard,
# is_sex_table() false-positives on orientation tables, and orientation
# labels (Bisexual, Heterosexual, Gay, ...) get silently summed into
# sex.totals.unknown — polluting sex reporting rates with unrelated data.
_SEX_TABLE_EXCLUDE_KEYWORDS = ["orientation"]

# Category titles that represent measurement values, not demographic labels.
_MEASUREMENT_LABELS = {"count", "number", "n", "total", "value", "mean", "median"}

# Keywords that mark an unmapped label as still-plausibly sex-related
# (mirrors _PLAUSIBLE_RACE_KEYWORDS in race_extractor.py). An unmapped label
# containing none of these is very likely noise that leaked in from an
# unrelated table sharing the "sex" substring (e.g. sexual-orientation
# survey options) rather than genuine sex-reporting data.
_PLAUSIBLE_SEX_KEYWORDS = {
    # NOTE: deliberately no bare "sex" entry — it is a substring of bisexual /
    # homosexual / asexual / transsexual, which would silently exempt exactly
    # the orientation noise this allowlist exists to catch.
    "female", "male", "woman", "women", "man", "men", "boy", "girl",
    "unknown", "unreport", "intersex", "ambig",
    "prefer not", "declin", "not report", "not specif", "not disclos",
    "not avail", "not collect", "not provided", "not sure", "no data",
    "no response", "did not", "chose not", "no selected", "refus",
    "undisclosed", "unavailable",
    "missing", "unspecified", "undifferentiated", "de-identif", "withheld",
    # Gender-identity vocabulary occasionally leaks into a sex-titled table
    # (substring-matched the same way orientation labels do) — these are
    # genuine reported data, not noise, so they stay counted as sex unknown
    # rather than being dropped.
    "gender", "identity", "transgender", "trans", "cisgender", "cis",
    "non-binary", "nonbinary", "non binary", "genderqueer",
    "two spirit", "two-spirit", "self-describ", "self describ",
    "self-identif", "self identif",
}


def _should_quarantine_sex_label(mapping: Dict) -> bool:
    """True for an unmapped label with no plausible sex-related keyword.

    Only fires for the generic low-confidence catch-all ("unmapped" flag on
    an "unknown" category) — deliberate routing decisions like
    gender_label_in_sex_table / sex_qualified_identity_label are never
    quarantined, since those are intentional "reported, but ambiguous"
    outcomes, not noise.
    """
    if mapping.get("category") != "unknown" or "unmapped" not in mapping.get("flags", []):
        return False
    label_lower = clean_demographic_label(mapping["original"]).lower()
    return not any(kw in label_lower for kw in _PLAUSIBLE_SEX_KEYWORDS)


def is_sex_table(title: str) -> bool:
    """Check if a baseline measure is about sex (Context A)."""
    title_lower = title.lower()
    # Exclude pure gender tables
    if "gender" in title_lower and "sex" not in title_lower:
        return False
    if any(kw in title_lower for kw in _SEX_TABLE_EXCLUDE_KEYWORDS):
        return False
    return any(kw in title_lower for kw in SEX_TABLE_KEYWORDS)


def is_combined_sex_gender_table(title: str) -> bool:
    """Check if a baseline measure is a combined Sex/Gender table (Context C)."""
    title_lower = title.lower()
    return "sex" in title_lower and "gender" in title_lower


def map_sex_label(label: str) -> Optional[Dict]:
    """Map a label to a sex category, returning None if it's a gender-only label.

    Returns None for gender-identity labels so callers can route them
    to the gender extractor instead of cross-mapping.
    """
    label_clean = clean_demographic_label(label)
    label_lower = label_clean.lower()

    # Reject gender-only labels outright
    if label_lower in _GENDER_ONLY_LABELS:
        return None

    # Reject cis/trans + sex-word labels ("Transgender Female", "Cisgender
    # Male") — ambiguous, not a clean biological-sex assertion. The caller
    # routes these to sex "unknown" rather than counting them as female/male.
    if is_sex_qualified_identity_label(label_lower):
        return None

    # Direct mapping
    if label_lower in _SEX_LABEL_MAP:
        return {
            "category": _SEX_LABEL_MAP[label_lower],
            "confidence": "high",
            "original": label_clean,
            "flags": [],
        }

    # Heuristics — only biological sex terms
    if "female" in label_lower:
        return {"category": "female", "confidence": "medium", "original": label_clean, "flags": ["heuristic_match"]}
    if "male" in label_lower and "female" not in label_lower:
        return {"category": "male", "confidence": "medium", "original": label_clean, "flags": ["heuristic_match"]}

    # Unknown / unmapped — still a sex record (not a gender one)
    return {"category": "unknown", "confidence": "low", "original": label_clean, "flags": ["unmapped"]}


def _extract_rows_from_measure(measure: dict, overall_group_id=None,
                               fallback_denom: int = None) -> List[Dict]:
    """Low-level row extraction: yields (label, count) pairs from a measure.

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


def extract_sex_from_measure(measure: dict, overall_group_id=None,
                             fallback_denom: int = None) -> List[Dict]:
    """Extract sex data from a standard sex table (Context A).

    All rows are routed to sex.  Gender-only labels are mapped to unknown.
    """
    results = []
    for row in _extract_rows_from_measure(measure, overall_group_id, fallback_denom):
        mapping = map_sex_label(row["label"])
        if mapping is None:
            # Gender-only or cis/trans+sex-word label in a sex table — treat
            # as unknown sex. Distinguish the two cases in flags so an
            # auditor can tell "Woman"-style gender labels apart from the
            # genuinely ambiguous "Transgender Female"-style ones.
            flag = ("sex_qualified_identity_label"
                    if is_sex_qualified_identity_label(row["label"])
                    else "gender_label_in_sex_table")
            mapping = {"category": "unknown", "confidence": "low",
                       "original": row["label"], "flags": [flag]}
        mapping["count"] = row["count"]
        results.append(mapping)
    return results


def extract_sex_from_combined_measure(measure: dict, overall_group_id=None,
                                      fallback_denom: int = None):
    """Extract from a combined Sex/Gender table (Context C).

    Uses strict row-level routing: Female/Male → sex, Woman/Man → gender.
    Returns (sex_rows, gender_rows) tuple.
    """
    from src.gender_extractor import map_gender_label

    sex_rows = []
    gender_rows = []

    rows = _extract_rows_from_measure(measure, overall_group_id, fallback_denom)

    # Explicit gender identity labels that unambiguously belong in the gender array
    _EXPLICIT_GENDER_LABELS = {
        "woman", "women", "man", "men",
        "non-binary", "nonbinary", "genderqueer", "gender non-conforming",
        "agender", "genderfluid", "two-spirit", "transgender",
        "cisgender woman", "cis woman", "transgender woman", "trans woman",
        "cisgender man", "cis man", "transgender man", "trans man",
    }

    # First pass: classify each row
    unclassified = []
    for row in rows:
        label_lower = row["label"].strip().lower()

        # Try sex mapping first (Female/Male)
        sex_mapping = map_sex_label(row["label"])
        if sex_mapping is not None and sex_mapping["category"] != "unknown":
            sex_mapping["count"] = row["count"]
            sex_rows.append(sex_mapping)
            continue

        # Try explicit gender identity labels only (not "Other"/"Unknown")
        if label_lower in _EXPLICIT_GENDER_LABELS:
            gender_mapping = map_gender_label(row["label"])
            if gender_mapping is not None:
                gender_mapping["count"] = row["count"]
                gender_rows.append(gender_mapping)
                continue

        # Ambiguous (Other/Unknown) — save for second pass
        unclassified.append(row)

    # Second pass: route ambiguous rows based on table context
    has_sex_data = len(sex_rows) > 0
    has_gender_data = len(gender_rows) > 0

    for row in unclassified:
        if has_sex_data and not has_gender_data:
            # Table is primarily sex → route to sex unknown
            mapping = {"category": "unknown", "confidence": "medium",
                       "original": row["label"], "flags": ["ambiguous_routed_to_sex"],
                       "count": row["count"]}
            sex_rows.append(mapping)
        elif has_gender_data and not has_sex_data:
            # Table is primarily gender → route to gender
            label_lower = row["label"].strip().lower()
            cat = "other" if label_lower == "other" else "unknown"
            mapping = {"category": cat, "confidence": "medium",
                       "original": row["label"], "flags": ["ambiguous_routed_to_gender"],
                       "count": row["count"]}
            gender_rows.append(mapping)
        else:
            # Both present or neither — default to sex unknown
            mapping = {"category": "unknown", "confidence": "low",
                       "original": row["label"], "flags": ["ambiguous_combined_table"],
                       "count": row["count"]}
            sex_rows.append(mapping)

    return sex_rows, gender_rows


def extract_sex_data(study: dict) -> Dict:
    """Extract all sex data from a study.

    Unified iterative loop — exhaustively checks all available modules:
      1) Standard sex tables (Context A) → all rows to sex
      2) Combined Sex/Gender tables (Context C) → row-level routing, sex portion
      3) Customized sex tables → handled same as standard
    Only declares "Not Reported" after checking all tables.
    """
    from src.utils import get_baseline_measures, get_overall_group_id, get_total_baseline_participants

    measures = get_baseline_measures(study)
    overall_group_id = get_overall_group_id(study)
    total_participants = get_total_baseline_participants(study, overall_group_id)

    result = {
        "reported": False,
        "totals": {"female": 0, "male": 0, "unknown": 0},
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

    for measure in measures:
        title = measure.get("title", "")

        # Context C: combined Sex/Gender table — extract only sex rows
        if is_combined_sex_gender_table(title):
            sex_rows, _gender_rows = extract_sex_from_combined_measure(
                measure, overall_group_id, fallback_denom=total_participants
            )
            if sex_rows:
                result["reported"] = True
                for cat in sex_rows:
                    # Quarantine irrelevant unmapped labels (e.g. orientation
                    # options that slipped in via a combined table)
                    if _should_quarantine_sex_label(cat):
                        result["quarantined_labels"].append({
                            "original": cat["original"],
                            "count": cat["count"],
                            "reason": "unmapped_non_sex_label",
                        })
                        result["flags"].append("quarantined_label")
                        continue
                    result["raw_categories"].append(cat)
                    result["totals"][cat["category"]] += cat["count"]
                    result["flags"].extend(cat["flags"])
                result["flags"].append("from_combined_table")
            continue

        # Standard or Customized sex table (Context A)
        if not is_sex_table(title):
            continue

        result["reported"] = True
        categories = extract_sex_from_measure(measure, overall_group_id,
                                             fallback_denom=total_participants)
        for cat in categories:
            # Quarantine irrelevant unmapped labels (e.g. "Bisexual", "Gay" —
            # sexual-orientation options that leaked in via a substring-
            # matched table title) so they don't pollute sex totals.
            if _should_quarantine_sex_label(cat):
                result["quarantined_labels"].append({
                    "original": cat["original"],
                    "count": cat["count"],
                    "reason": "unmapped_non_sex_label",
                })
                result["flags"].append("quarantined_label")
                continue
            result["raw_categories"].append(cat)
            result["totals"][cat["category"]] += cat["count"]
            result["flags"].extend(cat["flags"])

    # All-zero rejection
    if result["reported"] and all(v == 0 for v in result["totals"].values()):
        result["reported"] = False
        result["totals"] = {k: 0 for k in result["totals"]}
        result["raw_categories"] = []
        result["flags"] = ["all_zero_rejection"]

    # Denominator balancing
    if result["reported"]:
        # Snapshot the "unknown" total accumulated purely from table rows
        # (explicit "Unknown" rows, ambiguous cis/trans-qualified labels
        # routed here, etc.) BEFORE any algorithmic denominator-gap
        # remainder is folded in. This is the row-sum-only figure a raw
        # extraction (no denominator inference) would have produced.
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
