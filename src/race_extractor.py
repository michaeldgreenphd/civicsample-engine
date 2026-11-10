"""
Race Data Extractor

Extracts race data from baseline characteristics and maps to NIH/OMB categories
with granular subcategories preserved.
"""
import re
from typing import Dict, List, Tuple, Optional
from rapidfuzz import fuzz, process
from src.utils import clean_demographic_label
# The manuscript parser's sex/gender measure selection (/sex|gender/i), reused
# to find race rows posted under a sex/gender title. Imported, not copied, so
# the two selections cannot drift; the vendored parser is not modified.
from src.sex_gender_parser import MEASURE_TITLE_RX
from src import table_selection

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

# ── Which baseline measures are race tables ─────────────────────────────────
#
# Method. A measure is a race table when its title contains the WORD race,
# races or racial, in any case. The title's words are its runs of ASCII
# letters, with a camelCase hump starting a new word and a run of capitals
# kept whole: "Race/Ethnicity, Customized" -> Race, Ethnicity, Customized;
# "RaceEthnicityOther" -> Race, Ethnicity, Other; "RACE (NIH/OMB)" -> RACE,
# NIH, OMB. Anything that is not a letter (space, punctuation, digit,
# underscore) separates words.
#
# Why. The rule it replaces admitted any title containing the letters "race"
# or "racial" anywhere, so words that merely contain them were read as race
# tables. Re-fetching the baseline modules of the 80,890 trials in the
# 2026-11-08 snapshot (CT.gov API v2, 2026-11-10), 144 distinct titles
# contained the letters; 72 of them, posted by 57 trials, are not race
# tables: contraception in 33 trials ("Contraception History", "Female method
# of contraception", "Oral Contraceptive Use History"), and "intracerebral",
# "paracetamol", "GRACE score", "extracellular", "trace", "brace",
# "Levetiracetam", "ultracentrifugation", "paracentesis". Admitted, such a
# table counted towards multiple_race_tables_N, could be a trial's only "race"
# table (making race "reported"), and its "Other" / "Unknown" rows mapped to
# race categories that quarantine (_should_quarantine) keeps, since those
# words are plausible race labels.
#
# A plain regex word boundary (\brac(e|ial)\b) would also drop
# "RaceEthnicityOther" (NCT00277212's only table with race rows; \b sees no
# boundary inside a camelCase word), which is why camelCase humps split words
# here. "races" keeps a plural title that the old rule admitted. Every title
# this rule admits, the old rule admitted too; nothing new becomes a race
# table. Titles about race that are not race-composition tables ("Racial
# Pride", "Race Consciousness", "Implicit Association Test- Race Bias") contain
# the word and are still admitted; this rule does not judge content.
RACE_TITLE_WORDS = frozenset({"race", "races", "racial"})
_TITLE_WORD_RX = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")

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

def title_words(title: Optional[str]) -> List[str]:
    """The words of a measure title, as the race-table rule reads them (see
    the block comment above RACE_TITLE_WORDS)."""
    return _TITLE_WORD_RX.findall(title or "")

def is_race_table(title: Optional[str]) -> bool:
    """Check if a baseline measure is about race: its title contains the word
    race, races or racial (block comment above RACE_TITLE_WORDS).

    Combined titles like "Race/Ethnicity, Customized" are accepted — the
    race extractor will map whatever it can and flag unmapped labels as
    "other".  The ethnicity extractor independently handles the same
    measure and drops labels it cannot map. "Contraception History" is not
    a race table: "race" is only letters inside another word.
    """
    return any(word.lower() in RACE_TITLE_WORDS for word in title_words(title))

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

def _measure_rows(measure: dict, overall_group_id: Optional[str] = None,
                  fallback_denom: Optional[int] = None) -> Tuple[List[Tuple[str, int]], bool]:
    """Return one measure's labelled rows as (label, count), in source order,
    and whether percentages were converted to counts.

    A row is a category, or a class that carries measurements directly. When a
    category title is a measurement label (e.g. "Count") the row's label is its
    parent class title. Unlabelled rows are dropped.

    Percentage-aware: if the measure reports percentages (detected via
    unitOfMeasure/paramType), values are converted to estimated counts using
    the measure's Number Analyzed, else a class-level one every class
    repeats, or the study's baseline total as a fallback (for
    cluster-randomized studies where the measure's denom is "Number of
    Clinics" instead of "Participants").
    """
    from src.utils import sum_measurements, is_percentage_measure

    is_pct = is_percentage_measure(measure)
    # Percentages convert against the measure's own Number Analyzed, else a
    # class-level one that every class repeats, else the study's baseline
    # total (table_selection.percentage_denominator, which says why
    # class numbers that differ are not used).
    denom = (table_selection.percentage_denominator(measure, overall_group_id, fallback_denom)
             if is_pct else None)

    rows: List[Tuple[str, int]] = []
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
                rows.append((label, sum_measurements(cat.get("measurements", []), overall_group_id,
                                                     is_pct=is_pct, denom=denom)))
        else:
            # Fallback: class itself carries measurements with no categories
            label = cls.get("title", "").strip()
            if not label:
                continue
            rows.append((label, sum_measurements(cls.get("measurements", []), overall_group_id,
                                                 is_pct=is_pct, denom=denom)))
    return rows, bool(is_pct and denom)

def _map_plain_row(label: str, count: int, pct_converted: bool) -> Optional[Dict]:
    """Map one row the way every plain race row is mapped.

    Returns None for a purely ethnicity label (e.g. "Hispanic or Latino"), which
    is left to the ethnicity pipeline. Otherwise the mapping of
    _map_race_label (direct, slash-fragment, fuzzy, then race/ethnicity split)
    with the row's count. A race-by-sex row's race part goes through exactly
    this function, so it lands where the same text would land on its own row.
    """
    if _is_ethnicity_only_label(label):
        return None
    mapping = _map_race_label(label)
    mapping["count"] = count
    if pct_converted:
        mapping["flags"].append("pct_to_count")
    return mapping

def extract_race_from_measure(measure: dict, overall_group_id: Optional[str] = None,
                              fallback_denom: Optional[int] = None) -> List[Dict]:
    """
    Extract race data from a single baseline measure, one plain row at a time.

    Row-level routing: each row is evaluated individually.  Rows that are
    purely ethnicity labels (e.g. "Hispanic or Latino") are skipped entirely
    so they are handled exclusively by the ethnicity pipeline.  Each
    remaining row produces an independent mapping — labels are never
    concatenated or merged.

    Args:
        measure: A single baseline measure dict from the API
        overall_group_id: groupId of the Overall group (avoids double-counting arms)
        fallback_denom: Study enrollment count, used when measure-level denom
            is unavailable (e.g. cluster-randomized studies)

    Returns list of category records with counts. Race-by-sex tables are not
    split here; extract_race_data reads them through _parse_race_table.
    """
    rows, pct_converted = _measure_rows(measure, overall_group_id, fallback_denom)
    results = []
    for label, count in rows:
        mapping = _map_plain_row(label, count, pct_converted)
        if mapping is not None:
            results.append(mapping)
    return results

# ── Race-by-sex tables ──────────────────────────────────────────────────────
#
# Some registrants post race crossed with sex in one table, one row per
# (race, sex) pair: "White : Female", "Caucasian (Female)", "African American-
# Male", "African American Male Veterans". Mapped whole, such a label is not a
# race label: the exact and case-insensitive lookups miss, a fuzzy match lands
# on one row of a pair and not the other ("Black or African American : Male"
# scored 87.7 against "Black or African American"; ": Female" did not), and the
# rest fall to "other". The race totals of those trials were then wrong and
# the joint counts were lost.
#
# Method. A row is a race-by-sex row when its label, cleaned as every race
# label is (clean_demographic_label), contains exactly one sex word (female,
# females, male, males; whole words, any case) and other text. The race part
# is the text before the sex word with trailing separators (space : ; , - – —
# / and an opening bracket) removed; when nothing precedes the sex word it is
# the text after it ("Female: White"). Text after the sex word that is not the
# race part is a descriptor ("Veterans") and is kept only in the source label.
# The race part is then mapped by _map_plain_row, the function every plain
# row goes through: an ethnicity-only race part ("Hispanic/Latino- Male") is
# left to the ethnicity pipeline, an unmapped one falls to "other" or
# quarantine, exactly as that text would on a row of its own. The sex word is
# kept as "female" or "male" on the row and in joint_sex_counts.
#
# Only sex words trigger the split. Gender words (woman/man, women/men) are a
# different taxonomy that this pipeline never maps onto sex (see
# sex_extractor._GENDER_ONLY_LABELS), and "Man" is also the Manchu ethnic
# group in posted Chinese ethnicity lists ("Han", "Hui", "Man", "Man
# Chinese"). The 2026-11-08 snapshot has no race-by-gender-word table.
#
# A table is a race-by-sex table only when EVERY labelled row is a
# race-by-sex row and at least one race part maps to a category without the
# "unmapped" flag. A sex word on some rows only is not a crossing: in the
# 2026-11-08 snapshot that pattern is "Condoms (female and male)" and "Male
# Partner Sterilization" in contraception tables that the substring title
# test admitted before is_race_table became a word rule, "male subject: not
# recorded", and "Man Chinese" beside "Han Chinese".
# Those tables, and a table whose race parts all fail to map ("Female
# participants" / "Male participants"), are read row by row as before.
#
# Race rows posted under a sex/gender title (e.g. "Sex/Gender, Customized"
# holding "White", or "White male" / "White female") are read for race only
# when every row of that table, plain or race-by-sex, maps to one of the
# race-specific categories (_RACE_SPECIFIC) without the "unmapped" flag, and
# none is skipped or quarantined. An "Unknown" or "Other" row fails the test:
# in a sex table it is more likely sex than race. The title test is the
# manuscript parser's measure selection (sex_gender_parser.MEASURE_TITLE_RX).
#
# Which table wins. The sex/gender parser (sex_gender_parser.parse_trial)
# never adds duplicate tables: it counts from the sex/gender-typed measures
# when any exist, else from all selected measures, and takes female/male from
# the ONE measure with the largest female + male, the first in source order on
# ties. The same rule is applied here, with one exception for tables that the
# registry itself marks as covering different people (rule 3):
#   1. Race-titled tables (is_race_table) are the typed tables. A
#      sex/gender-titled table that carries race rows is used only when the
#      trial has no race-titled table at all; otherwise it is superseded.
#   2. When more than one table is in play, one table is counted: the fullest
#      of those that cover the whole baseline population. Fullest means the
#      largest sum over the race-specific categories (the analogue of female
#      + male: "other" and "unknown_not_reported" are left out, as the parser
#      leaves out unknown); on equal sums, the one that counts more
#      participants in all; then the first in source order, except that of
#      two tables with the same count in every OMB category the one with more
#      subcategory detail is kept (see table_selection.fullest). A table with no
#      race-specific count (an ethnicity table posted as "Race/Ethnicity,
#      Customized" whose "Unknown" row is the only row kept) never wins over
#      one with any.
#   3. Disjoint parts. A table analyzed on part of the population carries its
#      own Number Analyzed (see table_selection.number_analyzed): e.g. one "Race (NIH/OMB)"
#      table for caregivers and one for patients, or one per study part. The
#      tables are summed (flag race_tables_partitioned) only when all of these
#      hold:
#        - every table in play is a plain race-titled table (a race-by-sex or
#          sex/gender-titled table is always deduplicated by rule 2: it is
#          the same population re-posted with another breakdown);
#        - every table with race-specific counts carries its own Number
#          Analyzed below the baseline total;
#        - those numbers add up to EXACTLY the baseline total, so the parts
#          account for every baseline participant once. A smaller sum is not
#          evidence of disjoint people: the same 20 of 50 participants posted
#          twice also add up to less than 50;
#        - no table counts more participants than its own Number Analyzed.
#      Otherwise rule 2 applies: a table with no Number Analyzed of its own
#      was analyzed on everyone, so another table can only re-count some of
#      the same people, and two subsets that do not make up the whole may be
#      the same subset.
# Every table not counted is recorded with the race_table_superseded flag.
# The parser takes its residual buckets (unknown, gender diverse) as the MAX
# across tables; here the winning table is used whole, so the totals stay the
# sum of posted rows of the tables counted and raw_categories lists exactly
# those rows. Denominator balancing below restores any gap to unknown_inferred.
#
# Rule 2 replaced summing every race-titled table. On the 2026-11-08 snapshot
# the sum exceeded the baseline participant count in 701 of the 1,704 trials
# with more than one race-titled table (by 326,105 participants), mostly an
# ethnicity table's "Unknown" or "Other" row counted again as race, or the
# same people posted in a "Race (NIH/OMB)" and a customized table. Rule 3
# keeps the 32 trials whose posted denominators make up the baseline total
# summed. Parts posted without a Number Analyzed of their own, or whose
# numbers fall short of the total (a "Child Race" table
# beside "Race", one table per country) cannot be told from re-counts by
# their counts alone; only the fullest is counted and the rest of the
# baseline total becomes unknown_inferred, never a second count.
#
# joint_sex_counts holds the posted (race, sex) counts of the race-by-sex
# table that supplied the totals, else of the first race-by-sex table in
# source order (used_for_totals says which). Counts are keyed by OMB category
# and then "female"/"male", only for the pairs posted: a table that posts
# female rows only has no "male" key, and nothing is inferred, balanced or
# carried over from the sex table. Rows skipped as ethnicity-only or
# quarantined are not in it. It is None for every trial without a
# race-by-sex table and after all-zero rejection.
#
# Flags added: "race_by_sex" on each crossed row (which also carries
# race_part and sex, with original kept as the posted label);
# "race_by_sex_table" and "race_from_sex_titled_table" when the counted table
# is one; "race_table_superseded" when a table was left out by the rules above;
# "race_tables_partitioned" when rule 3 summed disjoint parts;
# "multiple_race_by_sex_tables" when more than one race-by-sex table exists
# (joint_sex_counts then describes one of them, as above).

_SEX_WORD_RE = re.compile(r"\b(females?|males?)\b", re.IGNORECASE)
_RACE_PART_TRAILING = " \t:;,-–—/(["
_RACE_PART_EDGES = " \t:;,-–—/()[]"

# The race-specific OMB categories. "other" and "unknown_not_reported" are
# residual categories: they do not make a sex/gender-titled table a race
# table, and they do not count towards "fullest table".
_RACE_SPECIFIC = frozenset({
    "american_indian_alaska_native", "asian", "black_african_american",
    "native_hawaiian_pacific_islander", "white", "more_than_one_race",
})

def split_race_by_sex_label(label: str) -> Optional[Tuple[str, str]]:
    """Split a race-by-sex label into (race_part, sex).

    sex is "female" or "male". Returns None when the cleaned label has no sex
    word, more than one, or nothing besides the sex word. Examples:
      "White : Female"                  -> ("White", "female")
      "Caucasian (Female)"              -> ("Caucasian", "female")
      "Asian or Pacific Islander-Male"  -> ("Asian or Pacific Islander", "male")
      "African American Male Veterans" -> ("African American", "male")
      "Female: Black"                   -> ("Black", "female")
      "Condoms (female and male)"       -> None
    """
    text = clean_demographic_label(label)
    hits = list(_SEX_WORD_RE.finditer(text))
    if len(hits) != 1:
        return None
    hit = hits[0]
    sex = "female" if hit.group(1).lower().startswith("f") else "male"
    before = text[:hit.start()].rstrip(_RACE_PART_TRAILING)
    after = text[hit.end():].strip(_RACE_PART_EDGES)
    race_part = before or after
    if not race_part:
        return None
    return race_part, sex

def _parse_race_table(measure: dict, index: int, overall_group_id: Optional[str],
                      fallback_denom: Optional[int], sex_titled: bool) -> Optional[Dict]:
    """Read one baseline measure as a race table.

    Returns {"title", "index", "analyzed", "sex_titled", "race_by_sex",
    "categories", "quarantined"}: the table's own Number Analyzed (or None),
    the counted category records and the quarantined labels.
    A race-titled measure always returns a table (plain rows unless it passes
    the race-by-sex test). A sex/gender-titled measure returns None unless
    every row maps to a race-specific category (see the block comment above).
    """
    title = measure.get("title", "")
    rows, pct_converted = _measure_rows(measure, overall_group_id, fallback_denom)
    splits = [split_race_by_sex_label(label) for label, _count in rows]
    by_sex_rows = bool(rows) and all(sp is not None for sp in splits)

    categories: List[Dict] = []
    quarantined: List[Dict] = []
    skipped = 0
    race_by_sex = False
    if by_sex_rows:
        for (label, count), (race_part, sex) in zip(rows, splits):
            mapping = _map_plain_row(race_part, count, pct_converted)
            if mapping is None:
                skipped += 1
                continue
            # Quarantine is decided on the race part, as for a plain row; the
            # record then carries the posted label so the source stays visible.
            is_quarantined = _should_quarantine(mapping)
            mapping["original"] = clean_demographic_label(label)
            mapping["race_part"] = race_part
            mapping["sex"] = sex
            mapping["flags"].append("race_by_sex")
            if is_quarantined:
                quarantined.append(mapping)
            else:
                categories.append(mapping)
        race_by_sex = any("unmapped" not in c["flags"] for c in categories)

    if not race_by_sex:
        # Plain table, or sex words that are not a race-by-sex crossing:
        # every row mapped whole, as before.
        categories, quarantined, skipped = [], [], 0
        for label, count in rows:
            mapping = _map_plain_row(label, count, pct_converted)
            if mapping is None:
                skipped += 1
                continue
            if _should_quarantine(mapping):
                quarantined.append(mapping)
            else:
                categories.append(mapping)

    if sex_titled:
        # Race rows under a sex/gender title: every row must be race-specific
        # and mapped, whether plain or race-by-sex; a row with a sex word that
        # did not split ("Female") fails the plain mapping test here.
        if (not categories or skipped or quarantined
                or any(c["omb_category"] not in _RACE_SPECIFIC or "unmapped" in c["flags"]
                       for c in categories)):
            return None
        for cat in categories:
            cat["flags"].append("race_from_sex_titled_table")

    return {
        "title": title,
        "index": index,
        "analyzed": _number_analyzed(measure, overall_group_id),
        "sex_titled": sex_titled,
        "race_by_sex": race_by_sex,
        "categories": categories,
        "quarantined": quarantined,
    }

# The selection helpers live in src/table_selection.py, shared with the
# ethnicity and legacy sex/gender extractors; race passes its race-specific
# categories and its "omb_category" field.
_number_analyzed = table_selection.number_analyzed

def _race_specific_sum(table: Dict) -> int:
    """Participants a table places in the race-specific categories."""
    return table_selection.specific_sum(table, _RACE_SPECIFIC, "omb_category")

def _select_tables(tables: List[Dict], total_participants: Optional[int],
                   allow_parts: bool) -> Tuple[List[Dict], bool]:
    """The tables to count, and whether they were summed as disjoint parts
    (rules 2 and 3 of the block comment above; table_selection.select_tables).
    allow_parts is False when any table in play is a race-by-sex or
    sex/gender-titled table."""
    return table_selection.select_tables(tables, total_participants, _RACE_SPECIFIC,
                                         "omb_category", allow_parts)

def _joint_sex_counts(table: Dict, used_for_totals: bool) -> Dict:
    """The posted (race, sex) counts of one race-by-sex table, with sources."""
    counts: Dict[str, Dict[str, int]] = {}
    rows = []
    for cat in table["categories"]:
        by_sex = counts.setdefault(cat["omb_category"], {})
        by_sex[cat["sex"]] = by_sex.get(cat["sex"], 0) + cat["count"]
        rows.append({
            "source_label": cat["original"],
            "race_part": cat["race_part"],
            "omb_category": cat["omb_category"],
            "subcategory": cat["subcategory"],
            "sex": cat["sex"],
            "count": cat["count"],
        })
    return {
        "source_table": table["title"],
        "used_for_totals": used_for_totals,
        "counts": counts,
        "rows": rows,
    }

def extract_race_data(study: dict) -> Dict:
    """
    Extract all race data from a study.

    Returns dict with:
        - reported: bool
        - omb_totals: Dict of NIH/OMB category totals
        - subcategory_totals: Dict of granular subcategory totals
        - raw_categories: List of all extracted categories
        - joint_sex_counts: posted race-by-sex counts, or None (see the
          race-by-sex block comment above)
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
        "joint_sex_counts": None,
        "flags": []
    }

    # Typed (race-titled) tables, and sex/gender-titled tables that carry
    # race rows, in source order.
    typed: List[Dict] = []
    sex_titled: List[Dict] = []
    for index, measure in enumerate(measures):
        title = measure.get("title", "")
        if is_race_table(title):
            typed.append(_parse_race_table(measure, index, overall_group_id,
                                           total_participants, sex_titled=False))
        elif MEASURE_TITLE_RX.search(title):
            table = _parse_race_table(measure, index, overall_group_id,
                                      total_participants, sex_titled=True)
            if table is not None:
                sex_titled.append(table)

    race_tables_found = len(typed)
    # Rule 1: race-titled tables when any exist, else the sex/gender-titled ones.
    in_play = typed or sex_titled
    # Rules 2 and 3: the fullest whole-population table, or disjoint parts.
    plain_only = bool(typed) and not any(t["race_by_sex"] for t in in_play)
    counted, partitioned = _select_tables(in_play, total_participants, allow_parts=plain_only)
    superseded = (sex_titled if typed else []) + [t for t in in_play if not any(t is c for c in counted)]

    if superseded:
        result["flags"].append("race_table_superseded")
    if partitioned:
        result["flags"].append("race_tables_partitioned")

    for table in counted:
        result["reported"] = True
        categories = table["categories"]

        # Annotate categories from non-standard / combined measures so the
        # dashboard can surface the match-quality signal to the user
        title_lower = table["title"].lower()
        is_combined   = "ethnicity" in title_lower
        is_customized = "customized" in title_lower
        if is_combined or is_customized:
            for cat in categories:
                if is_combined:
                    cat["flags"].append("combined_race_ethnicity")
                if is_customized:
                    cat["flags"].append("customized_table")
        if table["race_by_sex"]:
            result["flags"].append("race_by_sex_table")
        if table["sex_titled"]:
            result["flags"].append("race_from_sex_titled_table")

        # Quarantine irrelevant unmapped labels (e.g. "Condom", "IUD")
        for cat in table["quarantined"]:
            result["quarantined_labels"].append({
                "original": cat["original"],
                "count": cat["count"],
                "reason": "unmapped_non_race_label"
            })
            result["flags"].append("quarantined_label")

        for cat in categories:
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

    # Joint counts: the counted race-by-sex table, else the first one posted.
    by_sex_tables = sorted((t for t in typed + sex_titled if t["race_by_sex"]),
                           key=lambda t: t["index"])
    if by_sex_tables:
        joint = next((t for t in by_sex_tables if any(t is c for c in counted)), by_sex_tables[0])
        result["joint_sex_counts"] = _joint_sex_counts(joint, any(joint is c for c in counted))
        if len(by_sex_tables) > 1:
            result["flags"].append("multiple_race_by_sex_tables")

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
    # take precedence. joint_sex_counts is never balanced: it holds posted
    # counts only.
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
        result["joint_sex_counts"] = None
        result["flags"] = ["all_zero_rejection"]

    # Dedupe flags
    # sorted() so identical extractions serialize identically across runs
    # (set iteration order varies per process and made week-over-week
    # data diffs noisy for no reason)
    result["flags"] = sorted(set(result["flags"]))

    return result
