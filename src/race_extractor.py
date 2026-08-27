"""
Race Data Extractor

Extracts race data from baseline characteristics and maps to NIH/OMB categories
with granular subcategories preserved.
"""
from typing import Dict, List, Tuple, Optional
from rapidfuzz import fuzz, process
from src.utils import clean_demographic_label

# Translation table that strips invisible Unicode characters commonly
# inserted by ClinicalTrials.gov (zero-width space, joiner, etc.)
_ZERO_WIDTH_CHARS = str.maketrans("", "", "\u200b\u200c\u200d\ufeff")

# NIH/OMB Race mappings with subcategories
RACE_MAPPINGS = {
    # American Indian / Alaska Native
    "American Indian or Alaska Native": ("american_indian_alaska_native", None),
    "American Indian or Alaskan Native": ("american_indian_alaska_native", None),
    "American Indian/Alaska Native": ("american_indian_alaska_native", None),
    "American Indian/Native Alaskan": ("american_indian_alaska_native", None),
    "Native American": ("american_indian_alaska_native", None),
    "Indigenous": ("american_indian_alaska_native", None),
    "First Nations": ("american_indian_alaska_native", None),

    # Asian - with subcategories
    "Asian": ("asian", None),
    "Chinese": ("asian", "chinese"),
    "Japanese": ("asian", "japanese"),
    "Korean": ("asian", "korean"),
    "Vietnamese": ("asian", "vietnamese"),
    "Filipino": ("asian", "filipino"),
    "Asian Indian": ("asian", "south_asian_indian"),
    "Indian": ("asian", "south_asian_indian"),  # Note: ambiguous
    "Pakistani": ("asian", "south_asian_pakistani"),
    "Bangladeshi": ("asian", "south_asian_bangladeshi"),
    "Thai": ("asian", "southeast_asian_thai"),
    "Cambodian": ("asian", "southeast_asian_cambodian"),
    "Laotian": ("asian", "southeast_asian_laotian"),
    "Malaysian": ("asian", "southeast_asian_malaysian"),
    "Indonesian": ("asian", "southeast_asian_indonesian"),
    "Taiwanese": ("asian", "east_asian_taiwanese"),
    "East Asian": ("asian", "east_asian"),
    "South Asian": ("asian", "south_asian"),
    "Southeast Asian": ("asian", "southeast_asian"),

    # Black or African American - with subcategories
    "Black or African American": ("black_african_american", None),
    "Black": ("black_african_american", None),
    "African American": ("black_african_american", "african_american"),
    "African-American": ("black_african_american", "african_american"),
    "African": ("black_african_american", "african"),
    "Haitian": ("black_african_american", "caribbean_haitian"),
    "Jamaican": ("black_african_american", "caribbean_jamaican"),
    "Caribbean": ("black_african_american", "caribbean"),
    "West Indian": ("black_african_american", "caribbean"),
    "Sub-Saharan African": ("black_african_american", "african"),
    "Negro": ("black_african_american", None),  # Deprecated term

    # Native Hawaiian or Pacific Islander - with subcategories
    "Native Hawaiian or Other Pacific Islander": ("native_hawaiian_pacific_islander", None),
    "Native Hawaiian/Pacific Islander": ("native_hawaiian_pacific_islander", None),
    "Pacific Islander": ("native_hawaiian_pacific_islander", None),
    "Native Hawaiian": ("native_hawaiian_pacific_islander", "native_hawaiian"),
    "Hawaiian": ("native_hawaiian_pacific_islander", "native_hawaiian"),
    "Samoan": ("native_hawaiian_pacific_islander", "samoan"),
    "Guamanian": ("native_hawaiian_pacific_islander", "guamanian_chamorro"),
    "Chamorro": ("native_hawaiian_pacific_islander", "guamanian_chamorro"),
    "Tongan": ("native_hawaiian_pacific_islander", "tongan"),
    "Fijian": ("native_hawaiian_pacific_islander", "fijian"),

    # White - with subcategories
    "White": ("white", None),
    "Caucasian": ("white", None),
    "Caucasian/White": ("white", None),
    "White/Caucasian": ("white", None),
    "Caucasian or White": ("white", None),
    "White or Caucasian": ("white", None),
    "European": ("white", "european"),
    "Middle Eastern": ("white", "middle_eastern"),
    "North African": ("white", "north_african"),
    "Arab": ("white", "middle_eastern_arab"),
    "Persian": ("white", "middle_eastern_persian"),
    "Iranian": ("white", "middle_eastern_persian"),
    "Turkish": ("white", "middle_eastern_turkish"),
    "Eastern European": ("white", "european_eastern"),
    "Western European": ("white", "european_western"),

    # More than one race
    "More than one race": ("more_than_one_race", None),
    "More Than One Race": ("more_than_one_race", None),
    "Two or more races": ("more_than_one_race", None),
    "Two or More Races": ("more_than_one_race", None),
    "Multiple races": ("more_than_one_race", None),
    "Multi-racial": ("more_than_one_race", None),
    "Multi-Racial": ("more_than_one_race", None),
    "Multiracial": ("more_than_one_race", None),
    "Mixed": ("more_than_one_race", None),
    "Mixed Race": ("more_than_one_race", None),
    "Biracial": ("more_than_one_race", None),

    # Other — explicitly mapped so it gets high confidence instead of "unmapped"
    "Other": ("other", None),
    "Other Race": ("other", None),
    "Other race": ("other", None),
    "Some Other Race": ("other", None),
    "Some other race": ("other", None),
    "Other/Unspecified": ("other", None),
    "Another Race": ("other", None),

    # Unknown / Not Reported  (including slash-separated variants that
    # ClinicalTrials.gov returns for "Customized" measures)
    "Unknown": ("unknown_not_reported", None),
    "Unknown race": ("unknown_not_reported", None),
    "Unknown Race": ("unknown_not_reported", None),
    "Unspecified": ("unknown_not_reported", None),
    "Unspecified race": ("unknown_not_reported", None),
    "Race unknown": ("unknown_not_reported", None),
    "Race not reported": ("unknown_not_reported", None),
    "Not Reported": ("unknown_not_reported", None),
    "Unknown or Not Reported": ("unknown_not_reported", None),
    "Unknown/Not Reported": ("unknown_not_reported", None),
    "Unknown/Not-reported": ("unknown_not_reported", None),
    "Unknown/Not-Reported": ("unknown_not_reported", None),
    "Declined": ("unknown_not_reported", None),
    "Not Specified": ("unknown_not_reported", None),
    "Missing": ("unknown_not_reported", None),
}

# Keywords to identify race tables
RACE_TABLE_KEYWORDS = ["race", "racial"]

# Labels containing these keywords are plausibly race-related even when
# unmapped (e.g. "Other race", "Race - Other").  Labels that don't contain
# ANY of these tokens are almost certainly from a non-race table that was
# mis-labelled (e.g. birth control methods) and should be quarantined.
_PLAUSIBLE_RACE_KEYWORDS = {
    "race", "racial", "ethnic", "origin", "other", "unknown", "mixed",
    "biracial", "multiracial", "prefer not", "declined", "not reported",
    "not specified", "missing", "unspecified",
    # Actual race category names — prevents quarantine of valid labels
    # that have measurement suffixes stripped (e.g. "White, %" → "White")
    "white", "caucasian", "black", "african", "asian", "hispanic",
    "latino", "latina", "native", "hawaiian", "pacific", "indian",
    "alaska", "indigenous", "caribbean",
}

def is_race_table(title: str) -> bool:
    """Check if a baseline measure is about race.

    Combined titles like "Race/Ethnicity, Customized" are accepted — the
    race extractor will map whatever it can and flag unmapped labels as
    "other".  The ethnicity extractor independently handles the same
    measure and drops labels it cannot map.
    """
    title_lower = title.lower()
    return any(kw in title_lower for kw in RACE_TABLE_KEYWORDS)

def map_race_category(label: str, fuzzy_threshold: int = 85) -> Dict:
    """
    Map a race category label to NIH/OMB standard.

    Returns dict with:
        - omb_category: NIH/OMB standard category
        - subcategory: Granular subcategory if available
        - confidence: high/medium/low
        - original: Original label
        - flags: Any issues noted
    """
    # Strip invisible Unicode characters and measurement suffixes (e.g. ", %")
    # that ClinicalTrials.gov "Customized" tables append to labels
    label_clean = clean_demographic_label(label)
    flags = []

    # Exact match
    if label_clean in RACE_MAPPINGS:
        omb, subcat = RACE_MAPPINGS[label_clean]
        return {
            "omb_category": omb,
            "subcategory": subcat,
            "confidence": "high",
            "original": label_clean,
            "flags": flags
        }

    # Case-insensitive match
    for key, (omb, subcat) in RACE_MAPPINGS.items():
        if key.lower() == label_clean.lower():
            return {
                "omb_category": omb,
                "subcategory": subcat,
                "confidence": "high",
                "original": label_clean,
                "flags": flags
            }

    # Slash-compound normalization: try each "/" fragment individually
    # so "Caucasian/White" resolves even without a dedicated mapping entry
    if "/" in label_clean:
        for frag in label_clean.split("/"):
            frag = frag.strip()
            if not frag:
                continue
            for key, (omb, subcat) in RACE_MAPPINGS.items():
                if key.lower() == frag.lower():
                    flags.append("slash_normalized")
                    return {
                        "omb_category": omb,
                        "subcategory": subcat,
                        "confidence": "high",
                        "original": label_clean,
                        "flags": flags
                    }

    # Fuzzy match
    match = process.extractOne(label_clean, RACE_MAPPINGS.keys(), scorer=fuzz.ratio)
    if match and match[1] >= fuzzy_threshold:
        omb, subcat = RACE_MAPPINGS[match[0]]
        flags.append(f"fuzzy_match_{match[1]}")
        return {
            "omb_category": omb,
            "subcategory": subcat,
            "confidence": "medium",
            "original": label_clean,
            "flags": flags
        }

    # Could not map
    flags.append("unmapped")
    return {
        "omb_category": "other",
        "subcategory": None,
        "confidence": "low",
        "original": label_clean,
        "flags": flags
    }

# Ethnicity keywords used to detect combined "Race, Ethnicity" labels
_ETHNICITY_KEYWORDS = {
    "hispanic", "latino", "latina", "latinx", "not hispanic", "non-hispanic",
    "non hispanic",
}

def _is_ethnicity_only_label(label: str) -> bool:
    """Return True if *label* is purely an ethnicity term with no race component.

    Labels like "Hispanic or Latino", "Not Hispanic", "Non-Hispanic" are
    ethnicity-only and must be routed exclusively to the ethnicity pipeline.
    Labels like "Caucasian/White, Hispanic" contain BOTH and are NOT
    ethnicity-only — they need the split logic.
    """
    label_lower = label.lower().strip()
    # Must contain at least one ethnicity keyword
    if not any(ek in label_lower for ek in _ETHNICITY_KEYWORDS):
        return False
    # Check whether any fragment also matches a known race key
    race_keys_lower = {k.lower() for k in RACE_MAPPINGS}
    import re
    fragments = re.split(r"[,;/]\s*", label)
    for frag in fragments:
        frag_lower = frag.strip().lower()
        if not frag_lower:
            continue
        # Skip fragments that ARE the ethnicity keyword themselves
        if any(ek in frag_lower for ek in _ETHNICITY_KEYWORDS):
            continue
        # This fragment has no ethnicity keyword — check if it's a race term
        if frag_lower in race_keys_lower:
            return False
        if process.extractOne(frag_lower, race_keys_lower, scorer=fuzz.ratio):
            best = process.extractOne(frag_lower, race_keys_lower, scorer=fuzz.ratio)
            if best and best[1] >= 85:
                return False
    return True

def _split_combined_label(label: str) -> Optional[Tuple[str, str]]:
    """If *label* contains BOTH a race keyword and an ethnicity keyword,
    return (race_part, ethnicity_part).  Otherwise return None.

    The race_part is the single best-matching race fragment (not all
    fragments joined), so it maps cleanly to a RACE_MAPPINGS key.

    Handles patterns like:
      - "Caucasian/White, Hispanic"   -> ("White", "Hispanic")
      - "Unknown race, Hispanic"      -> ("Unknown race", "Hispanic")
      - "Black or African American, Not Hispanic or Latino"
    """
    label_lower = label.lower()
    # Check for ethnicity keyword presence
    if not any(kw in label_lower for kw in _ETHNICITY_KEYWORDS):
        return None

    race_keys_lower = {k.lower(): k for k in RACE_MAPPINGS}
    import re
    fragments = re.split(r"[,;/]\s*", label)
    race_frags = []
    eth_frags = []
    for frag in fragments:
        frag_stripped = frag.strip()
        if not frag_stripped:
            continue
        frag_lower = frag_stripped.lower()
        is_eth = any(ek in frag_lower for ek in _ETHNICITY_KEYWORDS)
        is_race = frag_lower in race_keys_lower or any(
            fuzz.ratio(frag_lower, rk) >= 85 for rk in race_keys_lower
        )
        if is_eth:
            eth_frags.append(frag_stripped)
        elif is_race:
            race_frags.append(frag_stripped)
        else:
            race_frags.append(frag_stripped)

    if not (race_frags and eth_frags):
        return None

    # Pick the single best race fragment (highest fuzzy score against known keys)
    best_frag = race_frags[0]
    best_score = 0
    for frag in race_frags:
        frag_lower = frag.lower()
        if frag_lower in race_keys_lower:
            best_frag = frag
            best_score = 100
            break
        match = process.extractOne(frag, RACE_MAPPINGS.keys(), scorer=fuzz.ratio)
        if match and match[1] > best_score:
            best_score = match[1]
            best_frag = frag

    return (best_frag, ", ".join(eth_frags))

def _should_quarantine(mapping: Dict) -> bool:
    """Return True if an unmapped label is irrelevant to race and should be quarantined.

    Unmapped labels that contain at least one plausible race-related keyword
    (e.g. "Other", "Unknown") are kept in the normal "other" bucket.
    Labels like "Condom", "IUD", "Withdrawal" — which come from non-race
    tables that happen to be titled "Race" — are quarantined.
    """
    if mapping.get("omb_category") != "other" or "unmapped" not in mapping.get("flags", []):
        return False
    label_lower = clean_demographic_label(mapping["original"]).lower()
    return not any(kw in label_lower for kw in _PLAUSIBLE_RACE_KEYWORDS)

# Category titles that represent measurement values, not race labels.
# When a category has one of these titles the actual label is on its parent class.
_MEASUREMENT_LABELS = {"count", "number", "n", "total", "value", "mean", "median"}

def _map_race_label(label: str) -> Dict:
    """Map a single label to a race category.

    Attempts direct mapping first, then tries combined-label splitting as
    a fallback.  Each unmapped row is kept as an independent entity — labels
    are never concatenated or merged.
    """
    mapping = map_race_category(label)

    # Combined label splitting: if the whole label fails to map
    # but contains both race and ethnicity fragments, re-map
    # using only the race fragment.
    if mapping.get("omb_category") == "other" and "unmapped" in mapping.get("flags", []):
        split = _split_combined_label(label)
        if split:
            race_part, _eth_part = split
            mapping = map_race_category(race_part)
            mapping["flags"].append("split_from_combined")

    return mapping

def extract_race_from_measure(measure: dict, overall_group_id: Optional[str] = None,
                              fallback_denom: int = None) -> List[Dict]:
    """
    Extract race data from a single baseline measure.

    Row-level routing: each row is evaluated individually.  Rows that are
    purely ethnicity labels (e.g. "Hispanic or Latino") are skipped entirely
    so they are handled exclusively by the ethnicity pipeline.  Each
    remaining row produces an independent mapping — labels are never
    concatenated or merged.

    Percentage-aware: if the measure reports percentages (detected via
    unitOfMeasure/paramType), values are converted to estimated counts
    using the measure's Number Analyzed denominator, or the study's
    enrollment count as a fallback (for cluster-randomized studies where
    the measure's denom is "Number of Clinics" instead of "Participants").

    Args:
        measure: A single baseline measure dict from the API
        overall_group_id: groupId of the Overall group (avoids double-counting arms)
        fallback_denom: Study enrollment count, used when measure-level denom
            is unavailable (e.g. cluster-randomized studies)

    Returns list of category records with counts.
    """
    from src.utils import sum_measurements, is_percentage_measure, get_measure_denom

    # Detect percentage measures and get denominator for conversion
    is_pct = is_percentage_measure(measure)
    denom = None
    if is_pct:
        denom = get_measure_denom(measure, overall_group_id)
        if denom is None:
            denom = fallback_denom  # fall back to study enrollment

    results = []

    for cls in measure.get("classes", []):
        categories = cls.get("categories", [])

        if categories:
            for cat in categories:
                cat_title = (cat.get("title") or "").strip()
                # If the category title is a measurement label (e.g. "Count")
                # the real race label lives on the parent class
                if cat_title.lower() in _MEASUREMENT_LABELS:
                    label = cls.get("title", "").strip()
                else:
                    label = cat_title or cls.get("title", "").strip()
                if not label:
                    continue

                # Row-level routing: skip pure ethnicity labels
                if _is_ethnicity_only_label(label):
                    continue

                count = sum_measurements(cat.get("measurements", []), overall_group_id,
                                         is_pct=is_pct, denom=denom)
                mapping = _map_race_label(label)
                mapping["count"] = count
                if is_pct and denom:
                    mapping["flags"].append("pct_to_count")
                results.append(mapping)
        else:
            # Fallback: class itself carries measurements with no categories
            label = cls.get("title", "").strip()
            if not label:
                continue

            # Row-level routing: skip pure ethnicity labels
            if _is_ethnicity_only_label(label):
                continue

            count = sum_measurements(cls.get("measurements", []), overall_group_id,
                                     is_pct=is_pct, denom=denom)
            mapping = _map_race_label(label)
            mapping["count"] = count
            if is_pct and denom:
                mapping["flags"].append("pct_to_count")
            results.append(mapping)

    return results

def extract_race_data(study: dict) -> Dict:
    """
    Extract all race data from a study.

    Returns dict with:
        - reported: bool
        - omb_totals: Dict of NIH/OMB category totals
        - subcategory_totals: Dict of granular subcategory totals
        - raw_categories: List of all extracted categories
        - flags: List of any issues
    """
    from src.utils import get_baseline_measures, get_overall_group_id, get_total_baseline_participants

    measures = get_baseline_measures(study)
    overall_group_id = get_overall_group_id(study)
    total_participants = get_total_baseline_participants(study, overall_group_id)

    result = {
        "reported": False,
        "omb_totals": {
            "american_indian_alaska_native": 0,
            "asian": 0,
            "black_african_american": 0,
            "native_hawaiian_pacific_islander": 0,
            "white": 0,
            "more_than_one_race": 0,
            "unknown_not_reported": 0,
            "other": 0
        },
        # unknown_explicit / unknown_inferred disaggregate
        # omb_totals["unknown_not_reported"] into its two sources (see the
        # denominator-balancing comment below). unknown_not_reported always
        # equals their sum — current dashboard display is unaffected; this
        # is purely additive.
        "unknown_explicit": 0,
        "unknown_inferred": 0,
        "subcategory_totals": {},
        "raw_categories": [],
        "quarantined_labels": [],
        "flags": []
    }

    race_tables_found = 0
    has_combined_table = False

    for measure in measures:
        title = measure.get("title", "")

        if not is_race_table(title):
            continue

        race_tables_found += 1
        result["reported"] = True

        categories = extract_race_from_measure(measure, overall_group_id,
                                               fallback_denom=total_participants)

        # Annotate categories from non-standard / combined measures so the
        # dashboard can surface the match-quality signal to the user
        title_lower = title.lower()
        is_combined   = "ethnicity" in title_lower
        is_customized = "customized" in title_lower
        if is_combined:
            has_combined_table = True
        if is_combined or is_customized:
            for cat in categories:
                if is_combined:
                    cat["flags"].append("combined_race_ethnicity")
                if is_customized:
                    cat["flags"].append("customized_table")

        for cat in categories:
            # Quarantine irrelevant unmapped labels (e.g. "Condom", "IUD")
            if _should_quarantine(cat):
                result["quarantined_labels"].append({
                    "original": cat["original"],
                    "count": cat["count"],
                    "reason": "unmapped_non_race_label"
                })
                result["flags"].append("quarantined_label")
                continue

            result["raw_categories"].append(cat)

            # Add to OMB totals
            omb = cat["omb_category"]
            result["omb_totals"][omb] = result["omb_totals"].get(omb, 0) + cat["count"]

            # Add to subcategory totals if present
            if cat["subcategory"]:
                key = f"{omb}_{cat['subcategory']}"
                result["subcategory_totals"][key] = result["subcategory_totals"].get(key, 0) + cat["count"]

            # Collect flags
            result["flags"].extend(cat["flags"])

    if race_tables_found > 1:
        result["flags"].append(f"multiple_race_tables_{race_tables_found}")

    # Denominator balancing: ensure category counts sum to total participants.
    # For combined Race/Ethnicity tables, ethnicity rows (e.g. LatinX) are
    # routed to the ethnicity pipeline, so the race total will be less than
    # enrollment.  The remaining participants' race is genuinely unknown
    # (they were categorized by ethnicity only), so the difference should
    # be added to unknown_not_reported.
    # Only balance when at least some non-zero data was extracted — if every
    # count is zero, the study has no real data and all-zero rejection should
    # take precedence.
    if result["reported"] and total_participants is not None:
        extracted_sum = sum(result["omb_totals"].values())
        if extracted_sum > 0:
            # Snapshot the "unknown_not_reported" total accumulated purely
            # from table rows (explicit Unknown rows, ethnicity-only-routed
            # remainder, etc.) BEFORE the denominator-gap remainder below is
            # folded in — the row-sum-only figure a raw extraction (no
            # denominator inference) would have produced.
            result["unknown_explicit"] = result["omb_totals"]["unknown_not_reported"]
            remainder = total_participants - extracted_sum
            if remainder > 0:
                result["omb_totals"]["unknown_not_reported"] += remainder
                result["unknown_inferred"] = remainder
                result["flags"].append("denominator_balanced")

    # All-zero rejection: if every mapped category has 0 participants,
    # mark the demographic as not collected rather than showing empty rows
    if result["reported"] and all(v == 0 for v in result["omb_totals"].values()):
        result["reported"] = False
        result["omb_totals"] = {k: 0 for k in result["omb_totals"]}
        result["raw_categories"] = []
        result["subcategory_totals"] = {}
        result["flags"] = ["all_zero_rejection"]

    # Dedupe flags
    result["flags"] = list(set(result["flags"]))

    return result
