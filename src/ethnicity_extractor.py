"""
Ethnicity Data Extractor

Extracts ethnicity data from baseline characteristics.
"""
from typing import Dict, List, Optional
from rapidfuzz import fuzz, process
from src.utils import clean_demographic_label
from src import table_selection

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
    from src.utils import sum_measurements, is_percentage_measure

    # Detect percentage measures and get denominator for conversion
    is_pct = is_percentage_measure(measure)
    # Percentages convert against the measure's own Number Analyzed, else a
    # class-level one that every class repeats, else the study's baseline
    # total (table_selection.percentage_denominator, which says why
    # class numbers that differ are not used).
    denom = (table_selection.percentage_denominator(measure, overall_group_id, fallback_denom)
             if is_pct else None)

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

# Which tables are counted. A trial can post several ethnicity-titled tables
# (is_ethnicity_table), and adding them together counted people twice. On the
# 2026-11-08 snapshot (re-fetched registry records; engine main reproduced
# every published ethnicity block), 4,018 trials had more than one, and in
# 1,117 the sum exceeded the baseline participant count, by 374,659:
#   - in 993 trials (332,712 of the excess) the extra table was a RACE table
#     titled "Race/Ethnicity, Customized": its race rows are dropped below as
#     low-confidence, but its "Unknown" row maps to ethnicity unknown and was
#     added on top of the real ethnicity table (NCT05525494: +113,822);
#   - in 41 trials the same people were posted twice (an "Ethnicity
#     (NIH/OMB)" table and a customized re-post).
# The tables are now chosen by table_selection.select_tables, the rule the
# race extractor uses: one table, the fullest by Hispanic + Not Hispanic
# (unknown is residual, as the sex/gender parser leaves unknown out of
# female + male); a table that counts only "Unknown" never wins over one that
# counts anyone as Hispanic or Not Hispanic; disjoint parts are summed only
# when their own Number Analyzed make up exactly the baseline total. Tables
# left out are flagged ethnicity_table_superseded; summed parts,
# ethnicity_tables_partitioned, and ethnicity_tables_partitioned_by_description
# too when two parts differ only in their population description
# (table_selection.parted_by_description). Denominator balancing below is
# unchanged.
_ETHNICITY_SPECIFIC = frozenset({"hispanic_latino", "not_hispanic_latino"})

# ── Race rows of a combined classification ──────────────────────────────────
#
# Some registrants post race and ethnicity as one classification in one
# table, each participant in exactly one row: "Race/Ethnicity, Customized"
# with Caucasian 39 / African-American 10 / Hispanic 2 / Unknown 2, of 53
# (NCT00416078). This extractor reads that table too: "Hispanic" maps to
# hispanic_latino, "Unknown" to unknown_not_reported, and the race rows are
# dropped as unmapped. Denominator balancing then filed the 49 participants of
# the race rows as unknown ethnicity, although the table had put them outside
# its Hispanic row. The block read as one-sided: some Hispanic participants
# and everyone else unknown.
#
# Method. The race rows' participants are counted as not Hispanic or Latino
# when all of these hold:
#   - the race extractor finds the trial's one counted race table to be an
#     exact combined classification (race_extractor.combined_classification:
#     race rows + the table's unknown rows + its Hispanic rows add up to
#     exactly the baseline population, and no row negating the ethnicity has
#     participants);
#   - this block counted exactly one table, and it is that same measure;
#   - the table gives this block no "Not Hispanic" count of its own;
#   - this block's Hispanic count equals the race extractor's Hispanic rows,
#     its unknown count equals the race extractor's unknown rows, and the
#     baseline population minus both equals the race rows exactly.
# The participants of the race rows that place them outside the Hispanic row
# go to omb_totals["not_hispanic_latino"] and are recorded as
# not_hispanic_from_race_rows, with the flag not_hispanic_from_combined_table.
# Two kinds of race row are left out of that count
# (race_extractor.combined_classification): a row that also names an unknown
# ("Other/Unknown", "Other/No response"), since an unknown participant of a
# combined classification may be Hispanic; and a row that states the
# ethnicity ("Hispanic White", "Black Latino"), whose participants are
# Hispanic. Denominator balancing then files both as unknown, as before: this
# block does not count the second kind as Hispanic, because it does not read
# those labels.
# In every other case not_hispanic_from_race_rows is 0 and nothing changes.
# raw_categories still lists the posted rows only: the race rows are not
# ethnicity labels.
#
# Why under these conditions only. In an exclusive classification the table
# placed each participant in one row, so the people in "Caucasian" are not
# among those in "Hispanic". Where the counts do not add up exactly, some race
# rows may hold Hispanic participants too ("White" beside "Hispanic" in a
# table where people could be in both), and the counts cannot show which.
#
# A record extracted before this field existed has no
# not_hispanic_from_race_rows key: "not looked at", not 0.
#
# Measured on the baseline modules of the 80,890 trials in the 2026-11-08
# snapshot (CT.gov API v2, re-fetched 2026-11-14): 2,228 trials, 4,434,745
# participants move from unknown_not_reported to not_hispanic_latino. One
# trial is 45% of that (NCT02958995, 2,006,280 of 2,738,161 participants) and
# ten are 77%. In 113 of the trials, 88,793 participants of race rows that
# name an unknown or state the ethnicity stay unknown. Of the 2,688 trials whose race block
# carries shortfall_is_ethnicity_rows, 460 move nothing: 176 report no
# ethnicity, 198 count a separate table with a Not Hispanic count of its own,
# 45 count a different number of Hispanic participants from the race reading
# ("American Hispanic", which this extractor does not map), and 41 count
# another table or a different unknown. Ethnicity blocks reporting one
# category beside unknown (Hispanic only, Not Hispanic 0) fall from 2,938 to
# 710 of the 32,072 reported. No other value of any block changes.

def _race_rows_not_hispanic(study: dict, counted: List[Dict], omb_totals: Dict,
                            total_participants: Optional[int]) -> int:
    """Participants of a combined classification's race rows that this block
    counts as not Hispanic, or 0 (block comment above). omb_totals is the
    block's row sums before denominator balancing."""
    if total_participants is None or len(counted) != 1:
        return 0
    from src.race_extractor import combined_classification   # imported here: race reads no ethnicity
    combined = combined_classification(study)
    if combined is None or counted[0]["index"] != combined["index"]:
        return 0
    if omb_totals["not_hispanic_latino"] != 0:
        return 0
    if (omb_totals["hispanic_latino"] != combined["ethnicity_rows"]
            or omb_totals["unknown_not_reported"] != combined["unknown_rows"]):
        return 0
    race_rows = total_participants - omb_totals["hispanic_latino"] - omb_totals["unknown_not_reported"]
    if race_rows <= 0 or race_rows != combined["race_rows"]:
        return 0
    return combined["race_rows_named"]

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
        # Participants of a combined classification's race rows counted in
        # not_hispanic_latino (block comment "Race rows of a combined
        # classification"); 0 when there are none.
        "not_hispanic_from_race_rows": 0,
        "subcategory_totals": {},
        "raw_categories": [],
        "flags": []
    }

    tables: List[Dict] = []
    for index, measure in enumerate(measures):
        title = measure.get("title", "")

        if not is_ethnicity_table(title):
            continue

        result["reported"] = True

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

        tables.append({"title": title, "index": index,
                       "analyzed": table_selection.number_analyzed(measure, overall_group_id),
                       "population": table_selection.population_of(measure),
                       "categories": categories})

    ethnicity_tables_found = len(tables)
    counted, partitioned = table_selection.select_tables(
        tables, total_participants, _ETHNICITY_SPECIFIC, "omb_category", allow_parts=True)
    if len(counted) < len(tables):
        result["flags"].append("ethnicity_table_superseded")
    if partitioned:
        result["flags"].append("ethnicity_tables_partitioned")
        if table_selection.parted_by_description(counted, "omb_category"):
            result["flags"].append("ethnicity_tables_partitioned_by_description")

    for table in counted:
        categories = table["categories"]
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

    # The race rows of an exact combined classification are not Hispanic
    # (block comment "Race rows of a combined classification"). Before
    # balancing, which then finds no shortfall.
    if result["reported"]:
        not_hispanic = _race_rows_not_hispanic(study, counted, result["omb_totals"], total_participants)
        if not_hispanic:
            result["omb_totals"]["not_hispanic_latino"] += not_hispanic
            result["not_hispanic_from_race_rows"] = not_hispanic
            result["flags"].append("not_hispanic_from_combined_table")

    # Denominator balancing: ensure category counts sum to total participants.
    # For combined Race/Ethnicity tables (e.g. LatinX listed alongside
    # White, Black), the ethnicity total will be less than enrollment because
    # only ethnicity-specific rows (like LatinX) are routed here.  The
    # remaining participants' ethnicity is genuinely unknown, so the
    # difference should be added to unknown_not_reported — except for an
    # exact combined classification, whose race rows were counted as not
    # Hispanic just above.
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

    # sorted() so identical extractions serialize identically across runs
    # (set iteration order varies per process and made week-over-week
    # data diffs noisy for no reason)
    result["flags"] = sorted(set(result["flags"]))

    return result
