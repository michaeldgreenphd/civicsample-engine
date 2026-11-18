"""
Ethnicity Data Extractor

Extracts ethnicity data from baseline characteristics.
"""
import re
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
    # Posted wordings of Hispanic or Latino that matched nothing (block
    # comment "Plain vocabulary gaps" below; matched as posted only, never as
    # a fuzzy target). Each names Hispanic or Latino and nothing else.
    # "American Hispanic" also covers its letter-case variants, as every key does.
    "American Hispanic": ("hispanic_latino", None),     # US-resident Hispanic; not a subgroup
    "Hispanic American": ("hispanic_latino", None),     # the same words in the other order
    "Hispanic-American": ("hispanic_latino", None),     # hyphenated form of the above
    "Latino, Hispanic": ("hispanic_latino", None),      # both terms, comma-joined: one category
    "Latino or Hispanic": ("hispanic_latino", None),    # "Hispanic or Latino" reversed
    "Hispanic, Latinx": ("hispanic_latino", None),      # both terms, comma-joined: one category
    "Hispanic ethnicity": ("hispanic_latino", None),    # the category named with its dimension; "Hispanic Ethnicity" too
    # Positive counterparts posted in the same tables as the entries above
    # and below (same block comment, "Counterparts").
    "Latino American": ("hispanic_latino", None),       # beside "American Hispanic" (NCT00628745)
    "of Hispanic origin": ("hispanic_latino", None),    # "White, of Hispanic origin" beside "White, not of Hispanic origin"

    # Not Hispanic or Latino
    "Not Hispanic or Latino": ("not_hispanic_latino", None),
    "Not Hispanic/Latino": ("not_hispanic_latino", None),
    "Non-Hispanic": ("not_hispanic_latino", None),
    "Non Hispanic": ("not_hispanic_latino", None),
    "Not Hispanic": ("not_hispanic_latino", None),
    # Posted wordings of not Hispanic or Latino that matched nothing (block
    # comment "Plain vocabulary gaps" below; matched as posted only). Each
    # negates every Hispanic or Latino term it names.
    "not of Hispanic origin": ("not_hispanic_latino", None),            # "Not Hispanic" with "of ... origin"; "Origin" too
    "not of Hispanic-American Origin": ("not_hispanic_latino", None),   # the same, naming Hispanic-American
    "Non-Hispanic and Non-Latino": ("not_hispanic_latino", None),       # both terms negated, "and"
    "Non-Hispanic, Non-Latino": ("not_hispanic_latino", None),          # both terms negated, comma
    "Non-Hispanic or Non-Latino": ("not_hispanic_latino", None),        # both terms negated, "or"
    "non-Latino": ("not_hispanic_latino", None),                        # Latino negated; "Non-Latino" too
    # Negative counterparts posted in the same tables as the Hispanic entries
    # above (same block comment, "Counterparts").
    "Not Hispanic ethnicity": ("not_hispanic_latino", None),            # beside "Hispanic Ethnicity"
    "Non-Hispanic ethnicity": ("not_hispanic_latino", None),            # beside "Hispanic Ethnicity"
    "Not Latino or Hispanic": ("not_hispanic_latino", None),            # beside "Latino or Hispanic"
    # Not a negation of Hispanic: "Non" modifies "American". Mapped from its
    # posted pair alone (block comment "Plain vocabulary gaps", "Why"): in
    # NCT01900899 (Finland) "Ethnicity: American hispanic" 1 and "Ethnicity:
    # Non American hispanic" 183 are the whole ethnicity classification of 184.
    "Non American Hispanic": ("not_hispanic_latino", None),

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

# ── Plain vocabulary gaps ────────────────────────────────────────────────────
#
# Registrants post Hispanic and not Hispanic in wordings the vocabulary did not
# hold: "American Hispanic", "Latino, Hispanic", "Hispanic ethnicity", "not of
# Hispanic origin", "Non-Hispanic and Non-Latino", "non-Latino". Each matched
# nothing exactly, case-insensitively or fuzzily (best ratios 57-84, under the
# threshold of 85). In a race-titled table such a row was dropped and the block
# flagged ethnicity_rows_unread; with no other row the block ended in all-zero
# rejection. In an ethnicity-only table the row was kept as unmapped and its
# participants counted in unknown_not_reported.
#
# Method. The entries above marked "Plain vocabulary gaps" are matched as
# posted, exactly or in any letter case, like every key. They are left out of
# the fuzzy match (_EXACT_ONLY_KEYS): as fuzzy targets they would also read
# labels that are not theirs. On the measured snapshot they would read
# "Non-Hispanic and Latino" (15 rows; ratio 92 against "Non-Hispanic and
# Non-Latino", and both negate Hispanic, so the negation check does not refuse
# it) as not Hispanic, and "Not Hispanic Ethnicity" (2 rows; ratio 85 against
# "Hispanic ethnicity") as Hispanic. Left out of the fuzzy match, the entries
# change only rows whose label, or the part of it that an existing rule reads,
# is one of them: "Latino/Hispanic" through the comma/semicolon/slash split
# ("Latino, Hispanic"), "White, not of Hispanic origin" through the same split,
# "Non-Latino White" through the joined reading ("non-Latino").
#
# Left unmapped by this change, pending a decision on what each means:
# "Non-Hispanic and Latino" (names both categories; in the tables measured it
# sits beside a "Hispanic or Latino" row), "Not meeting definition for Hispanic
# or Latino" (in the tables measured it sits beside Hispanic subgroup rows),
# "Caucasian (White, including Hispanic)", read through the split as "including
# Hispanic)" (a race row holding Hispanic and other participants), and
# "Another Hispanic, Latino/a, or Spanish Origin", read through the split as
# "Another Hispanic, Latino" (the census wording of the remaining Hispanic
# subgroups, posted beside Mexican, Puerto Rican and Cuban rows).
#
# Why. Each entry names Hispanic or Latino and nothing else, or negates every
# Hispanic or Latino term it names: "American Hispanic" and "Hispanic American"
# describe the same people as "Hispanic", and "non-Latino" the same people as
# "Not Hispanic". None names a subgroup, so the subcategory is None.
#
# One exception: "Non American Hispanic" does not negate Hispanic, since "Non"
# modifies "American", and read alone it could describe Hispanic people who
# are not American. It is mapped to not_hispanic_latino from its posted pair:
# the one trial that posts it (NCT01900899, healthy children in Finland, 182
# of 184 White-Caucasian) gives "American hispanic" 1 and "Non American
# hispanic" 183, the whole classification, so the row is the complement of
# the Hispanic row. That is an inference from one table, not a reading of the
# wording: it does not carry over to other "Non <X> Hispanic" labels, which
# stay unmapped unless their own tables show the same.
#
# Counterparts. A table that posts one of these wordings often posts its
# opposite beside it: "Hispanic Ethnicity" 106 / "Not Hispanic Ethnicity" 387
# (NCT02774850), "Latino or Hispanic" / "Not Latino or Hispanic", "White, not
# of Hispanic origin" / "White, of Hispanic origin" (NCT00756652). Reading one
# side alone would turn a block that was all unknown, or not reported, into
# one that is all Hispanic, or all not Hispanic, among the participants whose
# ethnicity it gives. The opposites posted beside the entries in the measured
# snapshot are entries too ("Counterparts" in the vocabulary). Two blocks
# changed by this vocabulary still read one side only, because the opposite
# row mixes categories: "non-Latino, Hispanic" (NCT00151775) and "White
# (non-latino or unknown ethnicity)" (NCT02297815). Both keep the flag
# ethnicity_rows_unread.
#
# Combined classifications. "American Hispanic" was a Hispanic row to the race
# extractor but not to this one, so the two readings of an exact combined
# classification disagreed on the Hispanic count and no race rows were counted
# as not Hispanic ("Race rows of a combined classification" below). They now
# agree in such tables, and not_hispanic_from_race_rows is set for them.
#
# Measured on the baseline modules of the 80,988 trials in the 2026-11-15
# snapshot (CT.gov API v2, re-fetched 2026-11-18; the commit that published
# the snapshot reproduced 80,986 of its ethnicity blocks, the other two having
# changed in the registry since). 85 distinct labels (284 rows) change: 84
# from unmapped to a category, and "White, Hispanic/Latinx" from a fuzzy to an
# exact match of the same category; no label moves from one category to
# another. 223 blocks change: 112 from all-zero rejection to reported, 106
# reported with moved counts, 5 in flags or rows only. Hispanic +105,842, Not
# Hispanic +191,791, unknown -141,630: the net +156,003 are the participants
# of the newly reported blocks (156,001) and 2 of NCT02094872, whose posted
# rows add up to more than its 49 participants (Hispanic 6, "Black, not of
# Hispanic Origin" 2, "White, not of Hispanic Origin" 43; with the race rows,
# 55). 218 of the blocks change through race-titled tables, 5 through
# ethnicity-only tables (Not Hispanic +558, unknown -558).
# not_hispanic_from_race_rows rises in 92 trials by 114,557 (70 newly set).
# One trial is 73% of the Hispanic change (NCT03666026: "Hispanic Ethnicity"
# 46.9% of 164,205). Blocks flagged ethnicity_rows_unread fall from 500 (231
# not reported) to 290 (119).
_EXACT_ONLY_KEYS = frozenset({
    "American Hispanic", "Hispanic American", "Hispanic-American", "Latino, Hispanic",
    "Latino or Hispanic", "Hispanic, Latinx", "Hispanic ethnicity",
    "not of Hispanic origin", "not of Hispanic-American Origin", "Non-Hispanic and Non-Latino",
    "Non-Hispanic, Non-Latino", "Non-Hispanic or Non-Latino", "non-Latino",
    # counterparts
    "Latino American", "of Hispanic origin", "Not Hispanic ethnicity", "Non-Hispanic ethnicity",
    "Not Latino or Hispanic", "Non American Hispanic",
})
# The keys a fuzzy match may return, in the vocabulary's order.
_FUZZY_KEYS = tuple(key for key in ETHNICITY_MAPPINGS if key not in _EXACT_ONLY_KEYS)

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

# ── Dimension wording around an ethnicity label ─────────────────────────────
#
# A combined race and ethnicity table often names the dimension in every row:
# "Race: Caucasian/White" ... "Ethnicity: Hispanic" / "Ethnicity: Non
# Hispanic" (NCT00707031, one "Race/Ethnicity, Customized" table holding a
# race classification and an ethnicity classification, 634 participants in
# each). Some rows instead say that the Hispanic row takes any race:
# "Hispanic (regardless of race)", "Hispanic (any race)".
#
# Method. When the posted label matches the vocabulary neither exactly nor
# case-insensitively, one leading dimension name followed by a separator
# (":", "-", "–", "—" or "_"; the names are "Ethnicity", "Ethnic group" and
# "Race and/&// Ethnicity", and "Race" when what follows names Hispanic or
# Latino) is removed, then one trailing race-neutral qualifier in parentheses
# ("regardless of race", "any race", "of any race", "all races"); the rest is
# matched exactly, case-insensitively, then fuzzily, as before. The category
# records the posted label as "original" and carries the flag
# dimension_wording_stripped.
#
# "Race" alone is removed only before a Hispanic or Latino term ("Race :
# Hispanic", posted as a category of an exclusive classification) because
# "Race: Unknown", "Race - Not Reported" and "Race: Missing" are the race
# classification's own unknown rows: read as ethnicity they would put people
# whose race is unknown into ethnicity unknown. Such a label is returned
# unmapped at once, flagged race_dimension_label, without the fuzzy match:
# before, "Race-Unknown or Not Reported" scored 90 against "Unknown or Not
# Reported" and was counted as ethnicity unknown (NCT04459715: 77 participants
# beside an ethnicity classification of all 730).
#
# A fuzzy match of a stripped label is kept only when the label and the
# vocabulary entry agree on negating Hispanic (_negates_hispanic). The ratio
# is case-sensitive, so "non-Hispanic/Latino" scores 88 against
# "Hispanic/Latino" and below the threshold against "Not Hispanic/Latino". The
# race-joined reading below applies the same check. A label that matches as
# posted is fuzzily matched as before, without the check.
#
# Why. Neither piece says anything about ethnicity: "Ethnicity: Hispanic" is
# the row "Hispanic" of the table's ethnicity classification. Before this the
# label matched nothing, the row was dropped with the race rows of a
# race-titled table, and a block with no other row ended in all-zero
# rejection: reported False although the table counted every participant's
# ethnicity.
#
# What it does not do. A separator is required, so "Ethnicity unknown" and
# "Ethnicity not reported" are matched as posted. A label that is not
# ethnicity once stripped ("Race: Black") stays unmapped and is dropped from a
# race-titled table, as before. Other qualifiers ("Hispanic (not African
# American)") are not removed.
#
# Measured on the baseline modules of the 80,988 trials in the 2026-11-15
# snapshot (CT.gov API v2, re-fetched 2026-11-18; engine main reproduced
# 80,987 of their published ethnicity blocks). 166 trials change with the
# flag dimension_wording_stripped on a counted row, 128 of them from all-zero
# rejection to reported: Hispanic +11,164, Not Hispanic +56,548, unknown
# -13,592. The race_dimension_label rule alone changes 10 trials, unknown -153.
_DIMENSION_PREFIX_RX = re.compile(
    r"^(?P<name>race\s*(?:and|&|/)\s*ethnicity|ethnic\s+group|ethnicity|race)\s*[:\-–—_]\s*(?=\S)",
    re.IGNORECASE)
_RACE_NEUTRAL_QUALIFIER_RX = re.compile(
    r"\s*\(\s*(?:regardless\s+of\s+race|(?:of\s+)?any\s+race|all\s+races)\s*\)$",
    re.IGNORECASE)
# "Spanish" counts as a Hispanic term: the vocabulary maps "Spanish origin"
# to hispanic_latino, so a "Spanish" row is ethnicity evidence.
_HISPANIC_TERM_RX = re.compile(r"hispanic|latin[oaxe]|spanish", re.IGNORECASE)
_NEGATED_HISPANIC_RX = re.compile(
    r"(?<![a-z])(?:non|not|no|neither)[\s\-]*(?:of\s+)?(?:hispanic|latin[oaxe])", re.IGNORECASE)


def _strip_dimension_wording(label: str) -> str:
    """The label without a leading dimension name or a trailing race-neutral
    qualifier (block comment above); the label itself when it has neither.

      "Ethnicity: Hispanic"                -> "Hispanic"
      "Ethnicity - Not Hispanic or Latino" -> "Not Hispanic or Latino"
      "Race : Hispanic"                    -> "Hispanic"
      "Race: Unknown"                      -> "Race: Unknown"
      "Hispanic (Regardless of Race)"      -> "Hispanic"
      "Ethnicity unknown"                  -> "Ethnicity unknown"
    """
    text = label
    prefix = _DIMENSION_PREFIX_RX.match(text)
    if prefix is not None:
        rest = text[prefix.end():]
        if prefix.group("name").lower() != "race" or _HISPANIC_TERM_RX.search(rest):
            text = rest
    text = _RACE_NEUTRAL_QUALIFIER_RX.sub("", text)
    return text.strip() or label


def _is_race_dimension_label(label: str) -> bool:
    """True for a label naming the race dimension before a value that is not
    Hispanic or Latino ("Race: Unknown", "Race - Not Reported"): a row of the
    race classification (block comment above)."""
    prefix = _DIMENSION_PREFIX_RX.match(label)
    return (prefix is not None and prefix.group("name").lower() == "race"
            and not _HISPANIC_TERM_RX.search(label[prefix.end():]))


def _negates_hispanic(text: str) -> bool:
    """True when the text negates Hispanic or Latino ("Non-Hispanic", "not of
    Hispanic origin", "Nonhispanic", "Neither Hispanic nor Latino")."""
    return bool(_NEGATED_HISPANIC_RX.search(text))


def _match_vocabulary(label: str) -> Optional[tuple[str, Optional[str]]]:
    """(omb_category, subcategory) for an exact or case-insensitive match of
    the vocabulary, else None."""
    if label in ETHNICITY_MAPPINGS:
        return ETHNICITY_MAPPINGS[label]
    for key, value in ETHNICITY_MAPPINGS.items():
        if key.lower() == label.lower():
            return value
    return None


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

def map_ethnicity_category(label: str, fuzzy_threshold: int = 85, *,
                           guard_negation: bool = False) -> Dict:
    """Map an ethnicity category label to NIH/OMB standard.

    guard_negation applies the negation check of the block comment
    "Dimension wording around an ethnicity label" to the fuzzy match, as it
    always applies to a label whose dimension wording was removed."""
    label_clean = clean_demographic_label(label)
    flags = []

    # Exact, then case-insensitive match of the posted label; then of the
    # label without its dimension wording (block comment "Dimension wording
    # around an ethnicity label"), which also feeds the fuzzy match below.
    matched = _match_vocabulary(label_clean)
    match_text = label_clean
    if matched is None:
        if _is_race_dimension_label(label_clean):
            # A row of the race classification ("Race: Unknown"): not read as
            # ethnicity, not even fuzzily (same block comment).
            return {
                "omb_category": "unknown_not_reported",
                "subcategory": None,
                "confidence": "low",
                "original": label_clean,
                "flags": ["race_dimension_label", "unmapped"]
            }
        stripped = _strip_dimension_wording(label_clean)
        if stripped != label_clean:
            match_text = stripped
            flags.append("dimension_wording_stripped")
            guard_negation = True
            matched = _match_vocabulary(stripped)
    if matched is not None:
        omb, subcat = matched
        return {
            "omb_category": omb,
            "subcategory": subcat,
            "confidence": "high",
            "original": label_clean,
            "flags": flags
        }

    # Fuzzy match, never to a key matched as posted only (block comment
    # "Plain vocabulary gaps").
    match = process.extractOne(match_text, _FUZZY_KEYS, scorer=fuzz.ratio)
    if (match and match[1] >= fuzzy_threshold
            and not (guard_negation and _negates_hispanic(match_text) != _negates_hispanic(match[0]))):
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

# ── Race joined to ethnicity in one label ──────────────────────────────────
#
# A combined table can cross race with ethnicity in its row labels:
# "Non-Hispanic White", "Non-Hispanic Black", "Hispanic", "Other"; or "White
# (non-Hispanic)", "Black non-Hispanic", "Hispanic White". The race extractor
# reads such a label as a race row and counts its race part
# (race_extractor._race_joined_to_ethnicity: exactly one ethnicity phrase
# beside exactly a known race label). Here the whole label matched no
# ethnicity term, the row was dropped with the race rows of a race-titled
# table, and its participants were balanced into unknown, or, with no other
# row, the block ended in all-zero rejection.
#
# Method. A label that the vocabulary, the dimension wording rule and the
# comma/semicolon/slash split all leave unmapped is passed, without its
# dimension wording ("Race and Ethnicity : White Non-Hispanic", NCT04523714),
# to that same race extractor function. When it returns (race_part, ethnicity_part) and the
# ethnicity part maps to a category (exactly, case-insensitively, or fuzzily
# with the negation check of "Dimension wording around an ethnicity label"),
# the row is counted in that category: "Non-Hispanic White" as
# not_hispanic_latino, "Hispanic White" as hispanic_latino. The category keeps
# the posted label as "original" and carries the flag race_joined_label.
#
# Why the race extractor's function. The two blocks then split the same rows
# by the same rule: a row the race block counts as White because it reads
# "Non-Hispanic White" is the row this block counts as Not Hispanic. A label
# the function does not accept ("Non-Hispanic and Latino", "Non-Black
# Hispanic") is left unmapped here too.
#
# A row of a race-titled table that names Hispanic or Latino and is still
# unmapped ("American Hispanic", "not of Hispanic origin") is dropped as
# before; the block records the flag ethnicity_rows_unread when such a row has
# participants in a counted table, and the flag survives all-zero rejection,
# so a block with no counts says why.
#
# Measured on the baseline modules of the 80,988 trials in the 2026-11-15
# snapshot (CT.gov API v2, re-fetched 2026-11-18; engine main reproduced
# 80,987 of their published ethnicity blocks). 650 trials change with the
# flag race_joined_label, 199 of them from all-zero rejection to reported:
# Hispanic +107,941, Not Hispanic +596,294, unknown -410,643. In 347 trials the
# Not Hispanic participants that the combined-classification rule moved from
# race rows (533,163) are now read from the labels (4,288 still moved). 500
# blocks carry ethnicity_rows_unread, 231 of them not reported.

def _map_row_label(label: str) -> Dict:
    """map_ethnicity_category for one row's label, then the two readings of a
    label that joins race and ethnicity: the comma/semicolon/slash split, then
    the race extractor's joined-label rule (block comment above)."""
    mapping = map_ethnicity_category(label)
    if mapping.get("confidence") == "low" and "unmapped" in mapping.get("flags", []):
        eth_part = _split_combined_for_ethnicity(label)
        if eth_part:
            mapping = map_ethnicity_category(eth_part)
            mapping["flags"].append("split_from_combined")
    if mapping.get("confidence") == "low" and "unmapped" in mapping.get("flags", []):
        # imported here, as combined_classification below: race reads no ethnicity
        from src.race_extractor import _race_joined_to_ethnicity
        # The joined reading sees the label without its dimension wording, so
        # "Race: Non-Hispanic White" is read as "Non-Hispanic White".
        posted = clean_demographic_label(label)
        stripped = _strip_dimension_wording(posted)
        joined = _race_joined_to_ethnicity(stripped)
        if joined is not None:
            joined_mapping = map_ethnicity_category(joined[1], guard_negation=True)
            if joined_mapping["confidence"] != "low":
                joined_mapping["original"] = posted
                if stripped != posted and "dimension_wording_stripped" not in joined_mapping["flags"]:
                    joined_mapping["flags"].append("dimension_wording_stripped")
                joined_mapping["flags"].append("race_joined_label")
                mapping = joined_mapping
    return mapping


def _names_hispanic(category: Dict) -> bool:
    """True when a dropped row's label names Hispanic or Latino."""
    return bool(_HISPANIC_TERM_RX.search(category.get("original") or ""))


def extract_ethnicity_from_measure(measure: dict, overall_group_id: Optional[str] = None,
                                   fallback_denom: Optional[int] = None) -> List[Dict]:
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

    # Subgroup classes beside a Total class: read the Total class alone
    # (table_selection block comment "A Total class beside subgroup classes").
    measure, total_only = table_selection.total_class_view(measure, overall_group_id)

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

                mapping = _map_row_label(label)
                mapping["count"] = count
                if is_pct and denom:
                    mapping["flags"].append("pct_to_count")
                if total_only:
                    mapping["flags"].append(table_selection.TOTAL_CLASS_FLAG)
                results.append(mapping)
        else:
            # Fallback: class itself carries measurements with no categories
            label = cls.get("title", "").strip()
            if not label:
                continue

            count = sum_measurements(cls.get("measurements", []), overall_group_id,
                                     is_pct=is_pct, denom=denom)

            mapping = _map_row_label(label)
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
# Hispanic. Denominator balancing files the first kind as unknown. The second
# kind is now read here for its ethnicity (block comment "Race joined to
# ethnicity in one label"): "Hispanic White" counts as Hispanic and
# "Non-Hispanic White" as Not Hispanic on its own label. A table whose rows
# this block reads as Not Hispanic gives a Not Hispanic count of its own, so
# no race rows are moved for it.
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

# ── A race table under a race-and-ethnicity title ──────────────────────────
#
# is_ethnicity_table accepts every title naming ethnicity, so a race
# classification posted as "Race/Ethnicity, Customized" is read here too:
# American Indian 0 / Asian 7 / Black 19 / Native Hawaiian 1 / White 485 /
# "Unknown or Not Reported'" 2, of 514 (NCT02787551). Its race rows are
# dropped below as unmapped, its unknown row maps to ethnicity unknown, and
# denominator balancing then filed the other 512 as unknown too: a block
# reported as 514 participants of unknown ethnicity, from a table that
# classifies no one's ethnicity.
#
# Method. A race-titled table (race_extractor.is_race_table: the word race,
# races or racial in the title, not the letters inside "Contraception") is not
# counted for ethnicity when all of these hold:
#   - none of its rows maps to hispanic_latino or not_hispanic_latino, at any
#     count, and none of its mapped rows begins with the ethnicity dimension
#     ("Ethnicity: Unknown" beside "Race: White" is the table's own ethnicity
#     classification, all unknown);
#   - none of the rows dropped as unmapped names Hispanic, Latino or Spanish, or
#     begins with the ethnicity dimension ("Ethnicity: Not Collected",
#     "Ethnicity - Other"): such a row is an ethnicity row the vocabulary
#     cannot read, and the table is not a race table;
#   - at least one dropped row has participants (the race rows).
# The dropped rows need not be race: in 33 trials of the measured snapshot they
# are labels the race vocabulary cannot read ("White - Caucasian / European
# Heritage", "American Indian Only") or another classification altogether
# (nationality "Qatar" / "Egypt", "Yoruba" / "Igbo", "Majority language"). What
# the rule establishes is that the table classifies participants by something
# other than Hispanic ethnicity and gives no Hispanic or Not Hispanic row, so
# its unknown row is not an ethnicity count. The flag keeps its name for the
# common case.
# The block records the flag race_only_table_skipped for each such table. When
# no other table is counted, the block is reported False with that flag and
# no counts, as for a trial that posted no ethnicity table, and does not reach
# all-zero rejection: the flag says why.
#
# Why. The unknown row of a race classification is unknown race. Without a
# Hispanic or Not Hispanic row the table says nothing about ethnicity, and the
# participants it does classify are not of unknown ethnicity; they were not
# asked, or not reported, in this table.
#
# What it does not do. A race-titled table holding only an unknown row, with no
# race rows ("Race/Ethnicity, Customized": Unknown 25), is counted as before:
# it may be the whole population's unknown race and ethnicity, and the table
# selection keeps it only when no table counts anyone as Hispanic or Not
# Hispanic. A table with a Hispanic or Not Hispanic row is read as before, also
# when that row is 0.
#
# Measured on the baseline modules of the 80,988 trials in the 2026-11-15
# snapshot (CT.gov API v2, re-fetched 2026-11-18; engine main reproduced
# 80,987 of their published ethnicity blocks). 909 trials go from reported to
# not reported, taking 8,121,192 participants out of unknown_not_reported,
# most of them added by denominator balancing; ten trials hold 89% of that
# (NCT04053075: 2,905,455). 9,826 blocks carry the flag; in most the race table
# had already lost the table selection to an ethnicity table, or the block
# ended in all-zero rejection, and only the flags change
# (ethnicity_table_superseded and multiple_ethnicity_tables_N no longer count
# the race table; all_zero_rejection gives way to race_only_table_skipped).

_ETHNICITY_DIMENSION_RX = re.compile(r"^\s*ethnic(?:ity|\s+group)(?![a-z])", re.IGNORECASE)


def _names_ethnicity_dimension(category: Dict) -> bool:
    """True when a row's label names the ethnicity dimension itself
    ("Ethnicity: Not Collected", "Ethnicity: Unknown"): a row of an ethnicity
    classification, not a race row (block comment above)."""
    return bool(_ETHNICITY_DIMENSION_RX.match(category.get("original") or ""))


def _is_race_only_table(kept: List[Dict], dropped: List[Dict]) -> bool:
    """True when a race-titled table is a race classification and not an
    ethnicity report (block comment above). kept are its mapped rows, dropped
    those dropped as unmapped."""
    if any(c["omb_category"] in _ETHNICITY_SPECIFIC or _names_ethnicity_dimension(c) for c in kept):
        return False
    if any(_names_hispanic(c) or _names_ethnicity_dimension(c) for c in dropped):
        return False
    return any((c.get("count") or 0) > 0 for c in dropped)


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

        categories = extract_ethnicity_from_measure(measure, overall_group_id,
                                                    fallback_denom=total_participants)

        # Combined "Race/Ethnicity" measures contain race labels (e.g.
        # "White", "Non-white") that don't match any ethnicity keyword.
        # Those end up as unmapped (confidence="low") and would corrupt
        # ethnicity totals — drop them.  Genuine ethnicity labels
        # (Hispanic, Not Hispanic, Unknown) map with high confidence and
        # are kept.
        unread = 0
        if "race" in title.lower():
            dropped = [c for c in categories if c.get("confidence") == "low"]
            categories = [c for c in categories if c.get("confidence") != "low"]
            # A race classification under an ethnicity title is not an
            # ethnicity report (block comment "A race table under a
            # race-and-ethnicity title").
            # Only when "race" is a word of the title (race_extractor.is_race_table):
            # "Ethnicity by Contraception" holds the letters, not the word.
            from src.race_extractor import is_race_table
            if is_race_table(title) and _is_race_only_table(categories, dropped):
                result["flags"].append("race_only_table_skipped")
                continue
            # Rows naming Hispanic or Latino that no rule could read (block
            # comment "Race joined to ethnicity in one label").
            unread = sum(c.get("count") or 0 for c in dropped if _names_hispanic(c))

        result["reported"] = True
        tables.append({"title": title, "index": index,
                       "analyzed": table_selection.number_analyzed(measure, overall_group_id),
                       "population": table_selection.population_of(measure),
                       "categories": categories, "unread": unread})

    ethnicity_tables_found = len(tables)
    counted, partitioned = table_selection.select_tables(
        tables, total_participants, _ETHNICITY_SPECIFIC, "omb_category", allow_parts=True)
    if len(counted) < len(tables):
        result["flags"].append("ethnicity_table_superseded")
    if partitioned:
        result["flags"].append("ethnicity_tables_partitioned")
        if table_selection.parted_by_description(counted, "omb_category"):
            result["flags"].append("ethnicity_tables_partitioned_by_description")

    if any(table["unread"] > 0 for table in counted):
        result["flags"].append("ethnicity_rows_unread")

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
        # ethnicity_rows_unread survives: it says why a posted table gave no
        # counts (block comment "Race joined to ethnicity in one label").
        result["flags"] = ["all_zero_rejection"] + [f for f in result["flags"] if f == "ethnicity_rows_unread"]

    # sorted() so identical extractions serialize identically across runs
    # (set iteration order varies per process and made week-over-week
    # data diffs noisy for no reason)
    result["flags"] = sorted(set(result["flags"]))

    return result
