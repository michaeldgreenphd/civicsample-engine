"""
Ethnicity Data Extractor

Extracts ethnicity data from baseline characteristics.
"""
from typing import Dict, List
from rapidfuzz import fuzz, process
from src.utils import clean_demographic_label

ETHNICITY_MAPPINGS = {
    # Hispanic or Latino - with subcategories
    "Hispanic or Latino": ("hispanic_latino", None),
    "Hispanic/Latino": ("hispanic_latino", None),
    "Hispanic": ("hispanic_latino", None),
    "Latino": ("hispanic_latino", None),
    "Latina": ("hispanic_latino", None),
    "Latinx": ("hispanic_latino", None),
    "Spanish origin": ("hispanic_latino", None),
    "Mexican": ("hispanic_latino", "mexican"),
    "Mexican American": ("hispanic_latino", "mexican"),
    "Chicano": ("hispanic_latino", "mexican"),
    "Puerto Rican": ("hispanic_latino", "puerto_rican"),
    "Cuban": ("hispanic_latino", "cuban"),
    "Dominican": ("hispanic_latino", "dominican"),
    "Central American": ("hispanic_latino", "central_american"),
    "South American": ("hispanic_latino", "south_american"),
    "Guatemalan": ("hispanic_latino", "central_american_guatemalan"),
    "Honduran": ("hispanic_latino", "central_american_honduran"),
    "Salvadoran": ("hispanic_latino", "central_american_salvadoran"),
    "Colombian": ("hispanic_latino", "south_american_colombian"),
    "Peruvian": ("hispanic_latino", "south_american_peruvian"),
    "Venezuelan": ("hispanic_latino", "south_american_venezuelan"),
    "Ecuadorian": ("hispanic_latino", "south_american_ecuadorian"),
    "Argentine": ("hispanic_latino", "south_american_argentine"),
    "Chilean": ("hispanic_latino", "south_american_chilean"),

    # Not Hispanic or Latino
    "Not Hispanic or Latino": ("not_hispanic_latino", None),
    "Not Hispanic/Latino": ("not_hispanic_latino", None),
    "Non-Hispanic": ("not_hispanic_latino", None),
    "Non Hispanic": ("not_hispanic_latino", None),
    "Not Hispanic": ("not_hispanic_latino", None),

    # Unknown / Not Reported  (including slash-separated variants)
    "Unknown": ("unknown_not_reported", None),
    "Unknown ethnicity": ("unknown_not_reported", None),
    "Unknown Ethnicity": ("unknown_not_reported", None),
    "Unspecified": ("unknown_not_reported", None),
    "Unspecified ethnicity": ("unknown_not_reported", None),
    "Ethnicity unknown": ("unknown_not_reported", None),
    "Ethnicity not reported": ("unknown_not_reported", None),
    "Not Reported": ("unknown_not_reported", None),
    "Unknown or Not Reported": ("unknown_not_reported", None),
    "Unknown/Not Reported": ("unknown_not_reported", None),
    "Unknown/Not-reported": ("unknown_not_reported", None),
    "Unknown/Not-Reported": ("unknown_not_reported", None),
    "Declined": ("unknown_not_reported", None),
    "Missing": ("unknown_not_reported", None),
}

ETHNICITY_TABLE_KEYWORDS = ["ethnicity", "ethnic", "hispanic", "latino"]

# Translation table that strips invisible Unicode characters commonly
# inserted by ClinicalTrials.gov (zero-width space, joiner, etc.)
_ZERO_WIDTH_CHARS = str.maketrans("", "", "\u200b\u200c\u200d\ufeff")

# Category titles that represent measurement values, not ethnicity labels.
# When a category has one of these titles the actual label is on its parent class.
_MEASUREMENT_LABELS = {"count", "number", "n", "total", "value", "mean", "median"}

# Ethnicity keywords used to detect the ethnicity fragment in combined labels
_ETHNICITY_KEYWORDS = {
    "hispanic", "latino", "latina", "latinx", "not hispanic", "non-hispanic",
    "non hispanic",
}

def _split_combined_for_ethnicity(label: str) -> str | None:
    """Extract the ethnicity fragment from a combined "Race, Ethnicity" label.

    Returns the ethnicity-relevant substring if found, else None.
    E.g. "Caucasian/White, Hispanic" -> "Hispanic"
         "Unknown race, Hispanic"    -> "Hispanic"
    """
    import re
    fragments = re.split(r"[,;/]\s*", label)
    eth_frags = []
    for frag in fragments:
        frag_stripped = frag.strip()
        if not frag_stripped:
            continue
        if any(ek in frag_stripped.lower() for ek in _ETHNICITY_KEYWORDS):
            eth_frags.append(frag_stripped)
    return ", ".join(eth_frags) if eth_frags else None

def is_ethnicity_table(title: str) -> bool:
    """Check if a baseline measure is about ethnicity."""
    title_lower = title.lower()

    # Exclude if it's primarily about race
    if "race" in title_lower and "ethnicity" not in title_lower:
        return False

    return any(kw in title_lower for kw in ETHNICITY_TABLE_KEYWORDS)

def map_ethnicity_category(label: str, fuzzy_threshold: int = 85) -> Dict:
    """Map an ethnicity category label to NIH/OMB standard."""
    label_clean = clean_demographic_label(label)
    flags = []

    # Exact match
    if label_clean in ETHNICITY_MAPPINGS:
        omb, subcat = ETHNICITY_MAPPINGS[label_clean]
        return {
            "omb_category": omb,
            "subcategory": subcat,
            "confidence": "high",
            "original": label_clean,
            "flags": flags
        }

    # Case-insensitive match
    for key, (omb, subcat) in ETHNICITY_MAPPINGS.items():
        if key.lower() == label_clean.lower():
            return {
                "omb_category": omb,
                "subcategory": subcat,
                "confidence": "high",
                "original": label_clean,
                "flags": flags
            }

    # Fuzzy match
    match = process.extractOne(label_clean, ETHNICITY_MAPPINGS.keys(), scorer=fuzz.ratio)
    if match and match[1] >= fuzzy_threshold:
        omb, subcat = ETHNICITY_MAPPINGS[match[0]]
        flags.append(f"fuzzy_match_{match[1]}")
        return {
            "omb_category": omb,
            "subcategory": subcat,
            "confidence": "medium",
            "original": label_clean,
            "flags": flags
        }

    flags.append("unmapped")
    return {
        "omb_category": "unknown_not_reported",
        "subcategory": None,
        "confidence": "low",
        "original": label_clean,
        "flags": flags
    }

def extract_ethnicity_from_measure(measure: dict, overall_group_id=None,
                                   fallback_denom: int = None) -> List[Dict]:
    """Extract ethnicity data from a single baseline measure.

    Percentage-aware: if the measure reports percentages, values are converted
    to estimated counts using the measure's Number Analyzed denominator, or
    the study enrollment as a fallback for cluster-randomized studies.

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
                # the real ethnicity label lives on the parent class
                if cat_title.lower() in _MEASUREMENT_LABELS:
                    label = cls.get("title", "").strip()
                else:
                    label = cat_title or cls.get("title", "").strip()
                if not label:
                    continue

                count = sum_measurements(cat.get("measurements", []), overall_group_id,
                                         is_pct=is_pct, denom=denom)

                mapping = map_ethnicity_category(label)

                # Combined label splitting: if the whole label doesn't map
                # but contains an ethnicity fragment, re-map with that fragment
                if mapping.get("confidence") == "low" and "unmapped" in mapping.get("flags", []):
                    eth_part = _split_combined_for_ethnicity(label)
                    if eth_part:
                        mapping = map_ethnicity_category(eth_part)
                        mapping["flags"].append("split_from_combined")

                mapping["count"] = count
                if is_pct and denom:
                    mapping["flags"].append("pct_to_count")
                results.append(mapping)
        else:
            # Fallback: class itself carries measurements with no categories
            label = cls.get("title", "").strip()
            if not label:
                continue

            count = sum_measurements(cls.get("measurements", []), overall_group_id,
                                     is_pct=is_pct, denom=denom)

            mapping = map_ethnicity_category(label)

            if mapping.get("confidence") == "low" and "unmapped" in mapping.get("flags", []):
                eth_part = _split_combined_for_ethnicity(label)
                if eth_part:
                    mapping = map_ethnicity_category(eth_part)
                    mapping["flags"].append("split_from_combined")

            mapping["count"] = count
            if is_pct and denom:
                mapping["flags"].append("pct_to_count")
            results.append(mapping)

    return results

def extract_ethnicity_data(study: dict) -> Dict:
    """Extract all ethnicity data from a study."""
    from src.utils import get_baseline_measures, get_overall_group_id, get_total_baseline_participants

    measures = get_baseline_measures(study)
    overall_group_id = get_overall_group_id(study)
    total_participants = get_total_baseline_participants(study, overall_group_id)

    result = {
        "reported": False,
        "omb_totals": {
            "hispanic_latino": 0,
            "not_hispanic_latino": 0,
            "unknown_not_reported": 0
        },
        "subcategory_totals": {},
        "raw_categories": [],
        "flags": []
    }

    ethnicity_tables_found = 0
    has_combined_table = False

    for measure in measures:
        title = measure.get("title", "")

        if not is_ethnicity_table(title):
            continue

        ethnicity_tables_found += 1
        result["reported"] = True

        if "race" in title.lower():
            has_combined_table = True

        categories = extract_ethnicity_from_measure(measure, overall_group_id,
                                                    fallback_denom=total_participants)

        # Combined "Race/Ethnicity" measures contain race labels (e.g.
        # "White", "Non-white") that don't match any ethnicity keyword.
        # Those end up as unmapped (confidence="low") and would corrupt
        # ethnicity totals — drop them.  Genuine ethnicity labels
        # (Hispanic, Not Hispanic, Unknown) map with high confidence and
        # are kept.
        if "race" in title.lower():
            categories = [c for c in categories if c.get("confidence") != "low"]

        result["raw_categories"].extend(categories)

        for cat in categories:
            omb = cat["omb_category"]
            result["omb_totals"][omb] = result["omb_totals"].get(omb, 0) + cat["count"]

            if cat["subcategory"]:
                key = f"{omb}_{cat['subcategory']}"
                result["subcategory_totals"][key] = result["subcategory_totals"].get(key, 0) + cat["count"]

            result["flags"].extend(cat["flags"])

    if ethnicity_tables_found > 1:
        result["flags"].append(f"multiple_ethnicity_tables_{ethnicity_tables_found}")

    # Denominator balancing: ensure category counts sum to total participants.
    # For combined Race/Ethnicity tables (e.g. LatinX listed alongside
    # White, Black), the ethnicity total will be less than enrollment because
    # only ethnicity-specific rows (like LatinX) are routed here.  The
    # remaining participants' ethnicity is genuinely unknown, so the
    # difference should be added to unknown_not_reported.
    # Only balance when at least some non-zero data was extracted.
    if result["reported"] and total_participants is not None:
        extracted_sum = sum(result["omb_totals"].values())
        if extracted_sum > 0:
            remainder = total_participants - extracted_sum
            if remainder > 0:
                result["omb_totals"]["unknown_not_reported"] += remainder
                result["flags"].append("denominator_balanced")

    # All-zero rejection
    if result["reported"] and all(v == 0 for v in result["omb_totals"].values()):
        result["reported"] = False
        result["omb_totals"] = {k: 0 for k in result["omb_totals"]}
        result["raw_categories"] = []
        result["subcategory_totals"] = {}
        result["flags"] = ["all_zero_rejection"]

    result["flags"] = list(set(result["flags"]))

    return result
