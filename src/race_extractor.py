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

# ── Middle Eastern or North African labels ─────────────────────────────────
#
# The race categories here are the 1997 OMB minimum categories, the ones the
# registry's "Race (NIH/OMB)" template offers, under which Middle Eastern and
# North African people are White. The vocabulary already filed "Middle
# Eastern", "North African", "Arab", "Persian" and "Iranian" as White with a
# subcategory. Other wordings of the same groups matched nothing and were
# counted as other: "Middle Eastern or North African" (the term of OMB's March
# 2024 revision of SPD 15, which makes it a minimum category of its own) and
# the "White - Arabic/North African Heritage" family, posted in some thirty
# spellings ("White-Arabic/North African heritage", "WHITE (W) ARABIC/NORTH
# AFRICAN HER", "Wh - Arabic/North African H"). Some ("Arabic", "Arabian",
# "MENA") were even quarantined as labels that are not race, which left their
# participants to denominator balancing as unknown.
#
# Method. mena_subcategory reads a cleaned label as, in order:
#   - an optional White qualifier: "White", "Wh", "White (W)", "White (Wt)",
#     followed by "-", ":", "(", a dash or a space. A slash does not qualify:
#     "White/Middle Eastern" is one row for two groups;
#   - one or more group terms: Middle East(ern), Mid East, North Africa(n),
#     Maghreb, Arab / Arabic / Arabian, Arab American, Arabian Peninsula,
#     Persian, Iranian, MENA (each may carry "White" before it), joined by
#     "/", "//", ",", "&", ":", "(", "or", "and", "and/or" or a space;
#   - an optional "(MENA)" or ")" and an optional suffix: heritage (also
#     "Hertage", "HER", "H", "H.", "(NA) H"), descent, origin(s), ancestry,
#     ethnicity, ", n(%)".
# Nothing else may be in the label. Such a label maps to White with a
# subcategory: middle_eastern_arab when every term is Arab, Arabic or
# Arabian; middle_eastern_persian when every term is Persian or Iranian;
# middle_eastern or north_african when every term is that one; middle_eastern
# for several Middle Eastern groups ("Arab/Middle Eastern"); else, for North
# African beside another group or MENA, middle_eastern_north_african. The row is flagged mena_label. The rule runs
# after the exact and case-insensitive vocabulary match (so "Arab" keeps its
# entry) and before the slash fragments, which read "Arabic/North African" as
# North African alone and "Middle Eastern/North African" as Middle Eastern
# alone; those now get the subcategory that names both.
#
# What it does not do. A label naming another group beside these ("White/
# Middle Eastern", "Arab or West Asian", "Chinese, Middle Eastern, or Other
# Ethnic Group", "Middle Eastern or North African/Mediterranean", "Other:
# Arabic") is mapped as before: its participants cannot be told apart. The
# vocabulary's "Turkish" (middle_eastern_turkish) is not changed, and is not in
# MENA_SUBCATEGORIES below: whether the 2024 definition takes it in was not
# checked against the standard's text.
#
# Measured on the baseline modules of the 80,988 trials in the 2026-11-15
# snapshot (CT.gov API v2, re-fetched 2026-11-18), against engine main (the
# same before and after the ethnicity-dimension rule): 376 race blocks change
# their totals, 414 their totals or subcategories, 421 any field (7 only in
# flags); none changes reported. White +7,436: 6,894 participants from
# other (rows that matched nothing) and 542 from unknown (rows quarantined as
# not race, NCT02304406's "Arabic" 389 the largest). Their rows, with the
# 9,743 participants already White with a Middle Eastern or North African
# subcategory, make mena_in_white: 17,179 participants in 559 trials, 0.067%
# of White. Five ethnicity blocks change: with the quarantined rows counted,
# their race table adds up exactly, and the ethnicity extractor counts its
# race rows as not Hispanic (ethnicity_extractor, "Race rows of a combined
# classification"; NCT00617851, NCT01875367, NCT03395314, NCT04926779,
# NCT05208359: Not Hispanic +401, unknown -401).
_MENA_TERM = (r"(?:white\s+)?(?:(?:middle|mid)[\s\-]*e{1,2}ast(?:ern)?|north[\s\-]*afric(?:a|an)|maghreb|"
              r"arabian\s+peninsula|arab(?:ic|ian)?(?:[\s\-]+american)?|persian|iranian|mena)")
_MENA_JOIN = r"(?:\s*(?:/{1,2}|,|&|:|\()\s*|\s+(?:or|and/or|and)\s+|\s*,\s*(?:or|and)\s+|\s+)"
_MENA_LABEL_RX = re.compile(
    rf"^(?:(?:white|wh)(?:\s*\((?:w|wt)\))?\s*(?:[\-:–—(]\s*|\s+))?"
    rf"(?P<terms>{_MENA_TERM}(?:{_MENA_JOIN}{_MENA_TERM})*)"
    r"\s*(?:\(\s*mena\s*\)|\))?"
    r"(?:[\s\-]*(?:\(na\)\s*)?(?:heritage|hertage|her|h\.?|descent|origins?|ancestry|ethnicity))?"
    r"(?:\s*,?\s*n\s*\(%\))?\s*$",
    re.IGNORECASE)
_MENA_TERM_RX = re.compile(_MENA_TERM, re.IGNORECASE)

# ── Middle Eastern or North African within White ───────────────────────────
#
# Every race block carries mena_in_white: the participants of its counted
# rows filed as White with one of MENA_SUBCATEGORIES, a part of
# omb_totals["white"], never more than it. It lets a reader see White as the
# 1997 standard counts it and as the 2024 revision would, with Middle Eastern
# or North African apart, from the same rows and the same denominator: White
# without them is omb_totals["white"] - mena_in_white.
#
# It is what the rows name, not an estimate. Most trials post the registry's
# "Race (NIH/OMB)" template, which has no such category, so their Middle
# Eastern and North African participants are in White and in no row of their
# own; mena_in_white is 0 for them. On the 2026-11-15 snapshot, no
# "Race (NIH/OMB)" table posts a Middle Eastern or North African row; every
# one is in a customized table. A row naming these groups beside another
# ("White/Middle Eastern") is not in it.
#
# A record extracted before the field existed has no mena_in_white key: "not
# looked at", not 0.
MENA_SUBCATEGORIES = frozenset({
    "middle_eastern", "north_african", "middle_eastern_arab", "middle_eastern_persian",
    "middle_eastern_north_african"})


def mena_subcategory(label: str) -> Optional[str]:
    """The White subcategory of a label naming Middle Eastern or North
    African groups only, else None (block comment above).

      "White - Arabic/North African Heritage" -> "middle_eastern_north_african"
      "Middle Eastern or North African"       -> "middle_eastern_north_african"
      "Arab ethnicity"                        -> "middle_eastern_arab"
      "White/Middle Eastern"                  -> None
    """
    match = _MENA_LABEL_RX.match(clean_demographic_label(label))
    if match is None:
        return None
    kinds = set()
    for term in _MENA_TERM_RX.findall(match.group("terms")):
        word = re.sub(r"^white\s+", "", term.lower())
        if word.startswith("arab"):
            kinds.add("middle_eastern_arab")
        elif word in ("persian", "iranian"):
            kinds.add("middle_eastern_persian")
        elif word.startswith(("middle", "mid")):
            kinds.add("middle_eastern")
        elif word.startswith("north") or word == "maghreb":
            kinds.add("north_african")
        else:
            kinds.add("middle_eastern_north_african")
    if len(kinds) == 1:
        return kinds.pop()
    # Several Middle Eastern groups ("Arab/Middle Eastern", "Arab/ Persian")
    # are Middle Eastern; any mix with North African, or MENA, names both.
    if kinds <= {"middle_eastern", "middle_eastern_arab", "middle_eastern_persian"}:
        return "middle_eastern"
    return "middle_eastern_north_african"

# ── Heritage-template labels ───────────────────────────────────────────────
#
# A family of customized race tables posts its rows as a race followed by the
# heritage groups it covers: "White - White/Caucasian/European Heritage",
# "Asian - East Asian Heritage", "Asian - Japanese Heritage", "Asian - South
# East Asian Heritage", "Asian - Central/South Asian Heritage", "African
# American/African Heritage", "White - Arabic/North African Heritage", and
# combinations ("African American/African Heritage & White",
# "Multiple-Afr Amer/Afr Her/Amer Ind or Al N"). It is posted in hundreds of
# spellings: "White - Caucasian / European heritage", "Asian-East Asian H",
# "Wt-Wt/Caucasian (Ca.)/European (Eu.) H.", "Black, of African Heritage",
# "Japanese H./East Asian H./SouthEast Asian H.". The vocabulary read only the
# spellings whose slash fragments happened to be keys ("White - White/
# Caucasian/European Heritage" through "Caucasian"); the rest matched nothing
# and were counted as other, and "Japanese Heritage" was quarantined as not
# race. Combinations were read through their first fragment: "African
# American/African Heritage & White" as Black.
#
# Method. A label is read by this rule only when it holds a heritage word:
# "heritage" or one of its abbreviations ("Herit", "Hertage", "Heri", "Her",
# "Hrtg", "H", the truncated "Heritag"), as a word of its own. Then, after
# removing short parenthesised glosses whose words are 1-4 letters each ("(Af
# Am)", "(EAH)", "(Ca.)", "(AA/AHER)"), the heritage words and a final
# "n (%)", and reading "of" after a race as a comma ("Black of African"):
#   - combinations (_heritage_combination, before the slash fragments): a
#     label starting "Multiple" or "Mixed" with a separator when every group
#     after it reads as a race, every word of it and not by a fuzzy match
#     (not "Mixed-Heritage or Other", "Multiple-Heritage Refused",
#     "Multiple-White-Refused Heritage", "Multiple-African American-NA
#     Heritage"), or whose parts
#     joined by "&" or "and" each read as one race (below) and name two or
#     more races, is more than one race;
#   - one race (_heritage_race, after the fuzzy match, for a label nothing else
#     reads): a leading race qualifier ("White", "Wh", "Wt", "W", "Asian",
#     "Black", "African American") followed by "-", ":", ";" or "," names
#     the race;
#     the rest is split at "/", "or", "and", "&" and a dash beside a space
#     ("African - White") into groups, each mapped
#     through the vocabulary (exact, slash or fuzzy, never this rule) or a
#     template abbreviation (_HERITAGE_ABBREVIATIONS: "Wt", "AA", "Af",
#     "Amer Ind", "AI", "AN", "Nat Hawaiian", "East A", "SEA").
#     A group that maps must name a race, and every such group and the
#     qualifier must name the same one. Without a qualifier, a group that
#     maps to nothing is allowed only when the rest as a whole maps to that
#     race as one text (exact or fuzzy), not through one of its slash
#     fragments: "Af Amer/White Heritage" is not read as White. With or
#     without a qualifier, a group that maps to nothing and reads as a
#     non-answer ("Refused", "Prefer not to say", a lone letter of "N/A")
#     fails the rule: "White - Refused Heritage" is not White. A compact one
#     ("NA", "NR", "Unk") fails it when no other group maps ("White - NA
#     Heritage"); "NA" is also the template's North African ("White-Arabic/NA
#     H"), read beside groups that map. A direction
#     word left by a shared ending ("East" of "East/South East Asian") is
#     allowed; a group mapping to other or unknown ("Mixed Heritage or
#     Other") fails the rule. The
#     subcategory is the one subcategory every mapped group shares, else none.
# A label naming a negation ("Non-European heritage") is not read. The row is
# flagged heritage_label.
#
# What it does not do. A heritage label the vocabulary already reads as one
# race keeps that reading and its subcategory, including the first-fragment
# subcategory of a grouped row ("Japanese/East Asian/South East Asian
# Heritage" is Asian, japanese; "African American/African Heritage" is Black,
# african_american). A label without a heritage word ("Asian - East Asian")
# is not read here. Two races joined by "/" or "or" without "Multiple"
# ("African Amer/African Heritage/Amer Ind or Alaska N") are not read: the
# row may name either race or both. "Asian and Pacific Islander" (or "&",
# "Other Pacific Islander") is not read as a combination: it is the grouped
# category of the 1977 standard. Only that wording, with at most one heritage
# word at its end: "Asian Heritage and Native Hawaiian Heritage" and "Asian
# Heritage and Pacific Islander Heritage" name two races (review findings).
#
# Measured on the baseline modules of the 80,988 trials in the 2026-11-15
# snapshot (CT.gov API v2, re-fetched 2026-11-18), against engine main: rows
# flagged heritage_label hold 263,829 participants in 768 trials, and 766
# race blocks change their totals. Other falls by 263,392: White +152,092,
# Asian +105,659 (east_asian +44,039, southeast_asian +23,699, japanese
# +14,101, chinese +13,479), Black +5,437, more than one race +498 (65 of them
# had been counted as Black or White through a combination's first race),
# American Indian +78. Unknown falls by 349: quarantined rows ("Japanese
# Heritage", "Multiple-Afr Amer/Afr Her/Amer Ind or Al N") are now counted,
# where denominator balancing had put their participants in unknown. One
# block becomes reported: NCT00540423, whose only row, "Japanese Heritage" 23,
# had been quarantined. No ethnicity block changes; the 338 race blocks over
# their population stay 338, none larger. Heritage rows left unread: 111
# participants in 15 trials, the forms above ("African Amer/African Heritage/
# Amer Ind or Alaska N" 55, "American/African Heritage" 34) and wordings the
# vocabulary lacks ("Central Asian Heritage", "Japanese East Asian Heritage",
# "Multiple-Amer Ind or Al N/A-Central/South A Her" 1).
_HERITAGE_WORDS = r"(?:heritage|heritag|herit|hertage|heri|her|hrtg|h)\.?"
_HERITAGE_WORD_RX = re.compile(r"(?<![a-z])" + _HERITAGE_WORDS + r"(?![a-z])", re.IGNORECASE)
_HERITAGE_GLOSS_RX = re.compile(r"\(\s*[a-z]{1,4}\.?(?:\s*[/ ]\s*[a-z]{1,4}\.?)*\s*\)", re.IGNORECASE)
_HERITAGE_COUNT_SUFFIX_RX = re.compile(r",?\s*n\s*\(\s*%\s*\)\s*$", re.IGNORECASE)
_HERITAGE_MULTIPLE_RX = re.compile(r"^(?:multiple|mixed)\s*[\-:–—]\s*", re.IGNORECASE)
# The grouped wording with at most one heritage word, at its end: "Asian and
# Pacific Islander Heritage", not "Asian Heritage and Pacific Islander
# Heritage".
_HERITAGE_GROUPED_API_RX = re.compile(
    r"asian\s*(?:&|and)\s*(?:other\s+)?pacific\s+islanders?(?:\s*" + _HERITAGE_WORDS + r")?",
    re.IGNORECASE)
_HERITAGE_QUALIFIER_RX = re.compile(
    r"^(?P<race>white|wh|wt|w|asian|black|african\s+american)\s*[\-:;–—,]\s*(?=\S)", re.IGNORECASE)
_HERITAGE_PART_SPLIT_RX = re.compile(r"\s*(?:&|\band\b)\s*", re.IGNORECASE)
_HERITAGE_GROUP_SPLIT_RX = re.compile(r"\s*(?:/|&|\bor\b|\band\b)\s*|\s+[\-–—]\s*|\s*[\-–—]\s+", re.IGNORECASE)
# Non-answers the vocabulary does not hold, and lone letters ("N" and "A" of
# "N/A"): an unread group like these is not a subgroup name.
_HERITAGE_NON_ANSWER_RX = re.compile(
    r"(?<![a-z])(?:refus\w*|declin\w*|prefer\w*|unknown|unspecified|unavailable|withheld|missing|"
    r"none|not)(?![a-z])|^[a-z]$", re.IGNORECASE)
# Compact non-answers ("NA", "N.A.", "NR", "NK", "DK", "Unk"). "NA" is also the
# template's own abbreviation of North African ("White-Arabic/NA H"), so these
# fail the rule only when no other group maps: when the qualifier alone would
# give the race ("White - NA Heritage").
_HERITAGE_COMPACT_NON_ANSWERS = frozenset({"na", "nr", "nk", "dk", "unk"})
_HERITAGE_DIRECTIONS = frozenset({"east", "south", "central", "west", "north", "south east",
                                  "southeast", "south-east"})
# Abbreviations the template's own spellings use for a race or a group in it.
_HERITAGE_ABBREVIATIONS = {
    "w": "white", "wh": "white", "wt": "white", "eu": "white", "eur": "white", "cau": "white",
    "aa": "black_african_american", "afr amer": "black_african_american",
    "af am": "black_african_american", "afam": "black_african_american",
    "african amer": "black_african_american", "african amc": "black_african_american",
    "amer ind": "american_indian_alaska_native", "am ind": "american_indian_alaska_native",
    "al n": "american_indian_alaska_native", "alaska n": "american_indian_alaska_native",
    "alaskan n": "american_indian_alaska_native",
    "alaska native": "american_indian_alaska_native",
    "alaskan native": "american_indian_alaska_native",
    "ala native": "american_indian_alaska_native",
    "ai": "american_indian_alaska_native", "an": "american_indian_alaska_native",
    "af": "black_african_american", "afr": "black_african_american",
    "nat hawaiian": "native_hawaiian_pacific_islander",
    # The second half of the category's own name, posted as a group of its
    # own ("Asian Heritage and Other Pacific Islander Heritage").
    "other pacific islander": "native_hawaiian_pacific_islander",
    "other pacific islanders": "native_hawaiian_pacific_islander",
    "asi": "asian", "east a": "asian", "east asi": "asian", "south a": "asian",
    "south east a": "asian", "southeast a": "asian", "sea": "asian",
}
_HERITAGE_QUALIFIER_RACE = {
    "white": "white", "wh": "white", "wt": "white", "w": "white", "asian": "asian",
    "black": "black_african_american", "african american": "black_african_american",
}


def _heritage_text(label: str) -> Optional[str]:
    """The label without glosses, heritage words, "of" and a final "n (%)",
    or None when it holds no heritage word or names a negation (block comment
    "Heritage-template labels")."""
    text = clean_demographic_label(label)
    if _HERITAGE_WORD_RX.search(text) is None or _negates_race(text):
        return None
    text = _HERITAGE_COUNT_SUFFIX_RX.sub("", text)
    text = _HERITAGE_GLOSS_RX.sub(" ", text)
    text = _HERITAGE_WORD_RX.sub(" ", text)
    # "Black, of African" and "Black of African": "of" separates the race
    # from its groups, as a comma does.
    text = re.sub(r"(?:\s*,\s*|\s+)of\s+", ", ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*/\s*", "/", text)
    text = re.sub(r"\s+", " ", text).strip(" -–—:,/")
    return text or None


def _heritage_group(group: str, atomic: bool = False) -> Optional[Tuple[str, Optional[str], str]]:
    """(omb_category, subcategory, confidence) of one heritage group through
    the vocabulary or a template abbreviation, or None when it maps to
    nothing. Never applies the heritage rule itself. atomic=True refuses a
    mapping made from one slash fragment of the text, so a whole text is read
    only as a whole ("Af Amer/White" is not White by its "White")."""
    key = re.sub(r"[.\s]+", " ", group.lower()).strip()
    if key in _HERITAGE_ABBREVIATIONS:
        return _HERITAGE_ABBREVIATIONS[key], None, "high"
    mapping = map_race_category(group, heritage=False)
    if "unmapped" in mapping["flags"] or (atomic and "slash_normalized" in mapping["flags"]):
        return None
    return mapping["omb_category"], mapping["subcategory"], mapping["confidence"]


def _heritage_race(text: str, strict: bool = False) -> Optional[Tuple[str, Optional[str], str]]:
    """(omb_category, subcategory, confidence) when the heritage text names
    one race, else None (block comment "Heritage-template labels").
    strict=True requires every group to map, not fuzzily, also after a
    qualifier: a group of a "Multiple-" tail is read whole ("White-Refused"
    is not White)."""
    races: set[str] = set()
    subcategories: set[Optional[str]] = set()
    confidence = "high"
    qualifier = _HERITAGE_QUALIFIER_RX.match(text)
    rest = text
    if qualifier is not None:
        races.add(_HERITAGE_QUALIFIER_RACE[re.sub(r"\s+", " ", qualifier.group("race").lower())])
        rest = text[qualifier.end():]
    mapped = unmapped = 0
    compact_non_answer = False
    for group in _HERITAGE_GROUP_SPLIT_RX.split(rest):
        group = group.strip(" -–—:,")
        if not group:
            continue
        if re.sub(r"\s+", " ", group.lower()) in _HERITAGE_DIRECTIONS:
            subcategories.add(None)
            continue
        reading = _heritage_group(group)
        if reading is None:
            # A group that maps to nothing may be a subgroup name the
            # vocabulary lacks ("Caucasian European" after "White -"), but not
            # a non-answer the vocabulary lacks ("White - Refused Heritage").
            if strict or _HERITAGE_NON_ANSWER_RX.search(group) is not None:
                return None
            if re.sub(r"[.\s]+", "", group.lower()) in _HERITAGE_COMPACT_NON_ANSWERS:
                compact_non_answer = True
            unmapped += 1
            continue
        omb, subcat, conf = reading
        if omb not in _RACE_SPECIFIC or conf == "low" or (strict and conf != "high"):
            return None
        races.add(omb)
        subcategories.add(subcat)
        mapped += 1
        if conf != "high":
            confidence = "medium"
    if qualifier is None:
        # No qualifier names the race: every group must map, or the rest as
        # a whole must ("American Indian/Alaska native" by its fuzzy match),
        # never through one of its slash fragments, which would drop the
        # unmapped group ("Af Amer/White", a review finding).
        whole = _heritage_group(rest, atomic=True) if unmapped else None
        if unmapped and (whole is None or whole[0] not in _RACE_SPECIFIC or whole[2] == "low"):
            return None
        if whole is not None:
            races.add(whole[0])
            subcategories.add(whole[1])
            if whole[2] != "high":
                confidence = "medium"
        if not mapped and whole is None:
            return None
    if compact_non_answer and not mapped:
        return None
    if len(races) != 1:
        return None
    subcategory = subcategories.pop() if len(subcategories) == 1 else None
    return races.pop(), subcategory, confidence


def _heritage_combination(label: str) -> bool:
    """True when a heritage label names more than one race: "Multiple-" or
    "Mixed-" before it, or parts joined by "&" or "and" that each read as one
    race and name two or more (block comment "Heritage-template labels")."""
    text = _heritage_text(label)
    if text is None:
        return False
    multiple = _HERITAGE_MULTIPLE_RX.match(text)
    if multiple is not None:
        # "Multiple-" or "Mixed-" names more than one race only when every
        # group after it reads as a race: through the vocabulary or a template
        # abbreviation ("Amer Ind or Al N"), or with a race qualifier of its
        # own ("Asian-Japanese", "W-W"). A group that is other or unknown
        # ("Mixed-Heritage or Other") or reads as nothing ("Multiple-Heritage
        # Refused", "N/A") stops it (review findings).
        for group in _HERITAGE_GROUP_SPLIT_RX.split(text[multiple.end():]):
            group = group.strip(" -–—:,")
            if not group:
                continue
            # Only a reading that is not fuzzy counts directly: "African
            # American-NA" is close to "African American" but holds "NA"
            # (a review finding); such a group is read whole instead.
            reading = _heritage_group(group)
            if reading is None or reading[2] != "high":
                reading = _heritage_race(group, strict=True)
            if reading is None or reading[0] not in _RACE_SPECIFIC:
                return False
        return True
    # "Asian and Pacific Islander" is the grouped category of the 1977
    # standard, one group rather than a combination: not read. Only that
    # wording, matched on the label with its glosses and "n (%)" removed but
    # its heritage words kept: "Asian Heritage and Native Hawaiian Heritage"
    # and "Asian Heritage and Pacific Islander Heritage" name two races
    # (review findings).
    grouped = _HERITAGE_GLOSS_RX.sub(" ", _HERITAGE_COUNT_SUFFIX_RX.sub("", clean_demographic_label(label)))
    if _HERITAGE_GROUPED_API_RX.fullmatch(re.sub(r"\s+", " ", grouped).strip(" -–—:,/")) is not None:
        return False
    parts = [p for p in _HERITAGE_PART_SPLIT_RX.split(text) if p.strip()]
    if len(parts) < 2:
        return False
    races: set[str] = set()
    for part in parts:
        reading = _heritage_race(part.strip(" -–—:,"))
        if reading is None:
            return False
        races.add(reading[0])
    return len(races) >= 2

def map_race_category(label: str, fuzzy_threshold: int = 85, *,
                      guard_negation: bool = False, heritage: bool = True) -> Dict:
    """
    Map a race category label to NIH/OMB standard.

    guard_negation keeps a fuzzy match only when the label and the matched
    key agree on negation (_negates_race); _map_plain_row sets it for a label
    whose race dimension name was removed (block comment "Race dimension
    named in the row label").

    heritage=False leaves out the heritage-template rule (block comment
    "Heritage-template labels"), which maps the groups of a heritage label
    through this function.

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

    # A label naming Middle Eastern or North African groups only (block
    # comment "Middle Eastern or North African labels"), before the slash
    # fragments, which would keep one group of "Arabic/North African".
    mena = mena_subcategory(label_clean)
    if mena is not None:
        flags.append("mena_label")
        return {
            "omb_category": "white",
            "subcategory": mena,
            "confidence": "high",
            "original": label_clean,
            "flags": flags
        }

    # A heritage-template label naming two or more races (block comment
    # "Heritage-template labels"), before the slash fragments, which would
    # keep the first race of "African American/African Heritage & White".
    if heritage and _heritage_combination(label_clean):
        flags.append("heritage_label")
        return {
            "omb_category": "more_than_one_race",
            "subcategory": None,
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
    if (match and match[1] >= fuzzy_threshold
            and not (guard_negation and _negates_race(label_clean) != _negates_race(match[0]))):
        omb, subcat = RACE_MAPPINGS[match[0]]
        flags.append(f"fuzzy_match_{match[1]}")
        return {
            "omb_category": omb,
            "subcategory": subcat,
            "confidence": "medium",
            "original": label_clean,
            "flags": flags
        }

    # A heritage-template label naming one race that nothing above read
    # (block comment "Heritage-template labels").
    heritage_text = _heritage_text(label_clean) if heritage else None
    reading = _heritage_race(heritage_text) if heritage_text is not None else None
    if reading is not None:
        flags.append("heritage_label")
        return {
            "omb_category": reading[0],
            "subcategory": reading[1],
            "confidence": reading[2],
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

# ── A race named beside an ethnicity term in one label ──────────────────────
#
# Registrants who post race and ethnicity as one classification often write
# both in one label with no separator between them: "Non-Hispanic White",
# "Black Non-Hispanic", "White (non-Hispanic)", "Hispanic Black". Each such
# row names its participants' race.
#
# Method. The label is cleaned as every race label is. When it holds exactly
# one ethnicity phrase (_ETHNICITY_PHRASE_RX: Hispanic, Latino/a/x/e, with an
# optional non/not before it, alternatives joined by "/", "," or "or", and an
# optional "origin" after it), the phrase is taken out. If what is left, with
# brackets and separators at its ends removed and an optional final "only"
# dropped, is exactly a key of RACE_MAPPINGS in any letter case, the row is a
# race row of that key: it is not ethnicity-only, and it maps as the key
# does, with the flag split_from_combined. The row keeps the posted label as
# its "original" and carries the mapped part as "race_part", so "Hispanic
# White" and "Non-Hispanic White" stay two source labels. Anything else is
# left to the rules that were there before.
#
# The match on what is left is exact, never fuzzy, so text that only limits
# or joins groups stays out: "Non-Black Hispanic" leaves "Non-Black",
# "Hispanic (not African American)" leaves "not African American", "Hispanic
# or African-American" leaves "or African-American", "Hispanic (regardless of
# race)" leaves "regardless of race", "American Hispanic" leaves "American".
# None is a race label, and each stays an ethnicity-only row, recorded in
# skipped_ethnicity_rows.
#
# The ethnicity word does not change the race: "Hispanic White" and
# "Non-Hispanic White" are both White. In the tables that post such rows they
# sit side by side as one exclusive classification ("Hispanic Black" 16 /
# "Hispanic White" 9 / "Hispanic Other" 27 / "Non-Hispanic Black" 16 /
# "Non-Hispanic White" 24 / "Non-Hispanic Other" 5, of 97: NCT03150173), and
# a plain row beside them holds the people the joined rows do not ("White"
# 1,363 / "White Hispanic" 83 / "Black Hispanic" 2 / "Black" 110 / "Asian"
# 23, of 1,581: NCT00360555).
#
# Why. The ethnicity-only test looked for a race term only in fragments that
# held no ethnicity keyword, and split fragments only at "," ";" and "/". A
# label with no separator was one fragment holding an ethnicity keyword, so
# "Non-Hispanic White" was read as ethnicity alone and its participants were
# in no race category: unknown_inferred when another row was counted, and no
# race reported at all when every row was of this kind.
#
# Measured on the baseline modules of the 80,890 trials in the 2026-11-08
# snapshot (CT.gov API v2, re-fetched 2026-11-14), against the extraction
# without this rule: 1,292 rows (162 distinct labels) holding 1,242,690
# participants become race rows, 981,350 of them White, 194,863 Black or
# African American, 63,878 Other; one row that was an unmapped "other"
# becomes White ("Non-Latine White", 96). unknown_inferred falls by 799,459
# and 127 trials that reported no race now report it (46,565 to 46,692). The
# counts of 691 race blocks change; in 327 more only the record of a row
# that was already counted in the same category changes ("White" becomes the
# posted "White, not Hispanic"; "White/Non-Hispanic" keeps its label and is
# flagged split_from_combined where it was slash_normalized; each gains
# race_part). No trial's counted table changes and no other block changes.
# Three trials newly add up to more than their baseline population, by 1, 2
# and 7 participants, from rows that do so as posted (NCT02841033,
# NCT01049919, NCT04646616).
#
# Left as ethnicity-only by the exact match, with a race word in the label:
# 119 rows in 92 trials (37,413 participants), mostly "Hispanic (regardless of
# race)" and its variants, and a few plural or longer forms ("Non-Hispanic
# Whites", "Non-Hispanic White or European American").
# "Latin" alone is not an ethnicity word here: taking it out of "Native Latin
# American" would leave "Native American", a different label.
_ETHNICITY_WORD = r"(?:hispanic|latin(?:o|a|x|e))s?"
_ETHNICITY_NEGATION = r"(?:(?:non|not)[\s\-]*(?:of\s+)?)"
_ETHNICITY_PHRASE_RX = re.compile(
    rf"(?<![a-z]){_ETHNICITY_NEGATION}?{_ETHNICITY_WORD}"
    rf"(?:\s*(?:/|,|\bor\b)?\s*{_ETHNICITY_NEGATION}?{_ETHNICITY_WORD})*"
    rf"(?:\s+origin)?(?![a-z])",
    re.IGNORECASE)
_JOINED_LABEL_EDGES = " \t:;,-–—/()[]"
_RACE_KEYS_LOWER = {key.lower() for key in RACE_MAPPINGS}

def _race_joined_to_ethnicity(label: str) -> Optional[Tuple[str, str]]:
    """(race_part, ethnicity_part) when the label is one ethnicity phrase and,
    beside it, exactly a known race label (block comment above); else None.

      "Non-Hispanic White"               -> ("White", "Non-Hispanic")
      "Black or African American (not Hispanic)"
                                         -> ("Black or African American", "not Hispanic")
      "Hispanic/Latino Black"            -> ("Black", "Hispanic/Latino")
      "Non-Hispanic White only"          -> ("White", "Non-Hispanic")
      "Non-Black Hispanic"               -> None
      "Hispanic or Latino"               -> None
    """
    text = clean_demographic_label(label)
    hits = list(_ETHNICITY_PHRASE_RX.finditer(text))
    if len(hits) != 1:
        return None
    hit = hits[0]
    rest = " ".join((text[:hit.start()] + " " + text[hit.end():]).split())
    rest = rest.strip(_JOINED_LABEL_EDGES)
    if rest.lower().endswith(" only"):
        rest = rest[:-len(" only")].rstrip(_JOINED_LABEL_EDGES)
    if rest.lower() not in _RACE_KEYS_LOWER:
        return None
    return rest, hit.group(0).strip()

def _is_ethnicity_only_label(label: str) -> bool:
    """Return True if *label* is purely an ethnicity term with no race component.

    Labels like "Hispanic or Latino", "Not Hispanic", "Non-Hispanic" are
    ethnicity-only and must be routed exclusively to the ethnicity pipeline.
    Labels like "Caucasian/White, Hispanic" contain BOTH and are NOT
    ethnicity-only — they need the split logic. Nor is a label that names a
    race beside the ethnicity term without a separator ("Non-Hispanic White":
    _race_joined_to_ethnicity).
    """
    label_lower = label.lower().strip()
    # Must contain at least one ethnicity keyword
    if not any(ek in label_lower for ek in _ETHNICITY_KEYWORDS):
        return False
    # A race label beside one ethnicity phrase is a race row.
    if _race_joined_to_ethnicity(label) is not None:
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
      - "Non-Hispanic White"          -> ("White", "Non-Hispanic"), with no
        separator to split at (_race_joined_to_ethnicity, tried first: what
        it returns is exactly a known race label)
    """
    # Tried before the keyword test below: the joined reader has its own
    # ethnicity vocabulary (_ETHNICITY_PHRASE_RX), which also knows forms the
    # keyword list does not ("Latine White").
    joined = _race_joined_to_ethnicity(label)
    if joined is not None:
        return joined

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

def _map_race_label(label: str, guard_negation: bool = False) -> Dict:
    """Map a single label to a race category.

    guard_negation is passed to every map_race_category call that can reach
    the fuzzy match (block comment "Race dimension named in the row label").

    A label that names a race beside one ethnicity phrase
    (_race_joined_to_ethnicity) is mapped from that exact race part, before
    anything else: the generic mapping below would split "Native
    Hawaiian/Pacific Islander Hispanic" at the slash and keep only "Native
    Hawaiian", or reach "Black or African American Latino" by fuzzy match.
    Otherwise attempts direct mapping first, then tries combined-label
    splitting as a fallback.  Each unmapped row is kept as an independent
    entity — labels are never concatenated or merged.
    """
    joined = _race_joined_to_ethnicity(label)
    if joined is not None:
        race_part, _eth_part = joined
        mapping = map_race_category(race_part)
        mapping["flags"].append("split_from_combined")
        # A joined label keeps what was posted: "Non-Hispanic White" and
        # "Hispanic White" stay two different source labels, with the part
        # that was mapped beside them.
        mapping["original"] = clean_demographic_label(label)
        mapping["race_part"] = race_part
        return mapping

    mapping = map_race_category(label, guard_negation=guard_negation)

    # Combined label splitting: if the whole label fails to map
    # but contains both race and ethnicity fragments, re-map
    # using only the race fragment.
    if mapping.get("omb_category") == "other" and "unmapped" in mapping.get("flags", []):
        split = _split_combined_label(label)
        if split:
            race_part, _eth_part = split
            mapping = map_race_category(race_part, guard_negation=guard_negation)
            mapping["flags"].append("split_from_combined")

    return mapping

def _measure_rows(measure: dict, overall_group_id: Optional[str] = None,
                  fallback_denom: Optional[int] = None) -> Tuple[List[Tuple[str, int]], bool, bool]:
    """Return one measure's labelled rows as (label, count), in source order,
    whether percentages were converted to counts, and whether the measure was
    read from its Total class alone.

    A row is a category, or a class that carries measurements directly. When a
    category title is a measurement label (e.g. "Count") the row's label is its
    parent class title. Unlabelled rows are dropped. Subgroup classes beside a
    Total class that repeats their rows are not read: the Total class is
    (table_selection block comment "A Total class beside subgroup classes").

    Percentage-aware: if the measure reports percentages (detected via
    unitOfMeasure/paramType), values are converted to estimated counts using
    the measure's Number Analyzed, else a class-level one every class
    repeats, or the study's baseline total as a fallback (for
    cluster-randomized studies where the measure's denom is "Number of
    Clinics" instead of "Participants").
    """
    from src.utils import sum_measurements, is_percentage_measure

    measure, total_only = table_selection.total_class_view(measure, overall_group_id)
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
    return rows, bool(is_pct and denom), total_only

# ── Race dimension named in the row label ──────────────────────────────────
#
# A combined table often names the dimension in every row: "Race: Caucasian/
# White" / "Race: Black" / "Race: Asian/Oriental" / "Race: Other" beside
# "Ethnicity: Hispanic" / "Ethnicity: Non Hispanic" (NCT00707031, 634
# participants in each classification). The prefix stopped most of these rows
# from matching: "Race: Black" and "Race: Asian/Oriental" were counted as
# other, so the block read Black 0 and Asian 0 although the table posts 18 and
# 7; "Race - Black or African American" reached Black only by fuzzy match. A
# prefixed joined label ("Race and Ethnicity : White Non-Hispanic",
# NCT04523714) was worse off: _is_ethnicity_only_label saw an ethnicity term
# and no race key beside it, so the row was taken for an ethnicity row and
# left out of the race block altogether.
#
# Method. One leading race dimension name followed by a separator (":", "-",
# "–", "—" or "_"; the names are "Race" and a combined name, "Race" and
# "Ethnicity" or "Ethnic group" in either order joined by "/", "&", "and",
# "or" or "and/or": "Race and Ethnicity", "Ethnicity or Race",
# _COMBINED_DIMENSION) is removed before the row is routed and mapped, so the rest goes through
# exactly the path the same text takes as a row of its own:
# _is_ethnicity_only_label first ("Race : Hispanic" is an ethnicity row, as
# before), then _map_race_label. The mapping keeps the posted label as
# "original" and carries the flag dimension_wording_stripped. A separator is
# required: "Race unknown" and "Race not reported" are vocabulary entries and
# match as posted. A doubled separator ("Race: : White", 879 participants in
# one trial) is removed whole.
#
# A consequence of mapping the rest as its own row: "RACE - OTHER/ NOT
# REPORTED" now maps as "OTHER/ NOT REPORTED" does, to other by its first
# slash fragment, where the prefixed label reached unknown through its second
# fragment only because the first ("RACE - OTHER") matched nothing; likewise
# "Race: Mixed/Unknown" now maps to more than one race, as "Mixed/Unknown"
# does.
#
# Measured on the baseline modules of the 80,988 trials in the 2026-11-15
# snapshot (CT.gov API v2, re-fetched 2026-11-18; engine main reproduced all
# 80,988 published race blocks). 136 race blocks change, none in reported:
# 41,089 participants leave other (White +34,198, Asian +4,831, Black
# +3,926, more than one race +166, American Indian +31, Native Hawaiian +3)
# and unknown falls by 2,066. Of that, 2,049 are NCT04523714, whose joined
# rows are now counted instead of inferred as unknown (its Hispanic row then
# accounts for the remaining shortfall, unknown_ethnicity_only 77); 116 are
# the two slash labels above; and 99 participants move into unknown from rows
# such as "Race: Not Reported" and "Race : Missing" that were counted as
# other. 11 more blocks change only in their flags. No ethnicity block
# changes.
#
# A row prefixed with the ethnicity dimension ("Ethnicity-Unknown or Not
# Reported", "Ethnicity-Not Japanese") is not read by this rule: block comment
# "Ethnicity dimension named in the row label".
#
# Negation check. A fuzzy match of a label whose prefix was removed is kept
# only when the label and the matched key agree on negation (_negates_race),
# as on the ethnicity side (ethnicity_extractor, "Dimension wording around an
# ethnicity label"). fuzz.ratio scores characters, not meaning: "Non-African
# American" scores 88.9 against "African American". Posted with the prefix,
# "Race: Non-African American" scored 76 and stayed other; with the prefix
# removed it would be counted as Black without this check. A negation on both
# sides ("Race: Not reportd" against "Not Reported") is kept. A label with no
# prefix is fuzzily matched as before, without the check. On the re-fetched
# 2026-11-15 snapshot the check stops no match: no race block changes. It
# guards the stripped labels against a registrant's next negated row.
#
# Combined names. A combined name starting with the ethnicity dimension, or
# joined by "or" or "and/or" ("Ethnicity or Race: White"), was not removed
# and the row was counted as other (a review finding on the ethnicity
# dimension rule below). It is now removed as "Race and Ethnicity" is. On the
# re-fetched 2026-11-15 inputs no row label carries such a prefix before a
# value (the combined names posted there are bare titles, as "Ethnicity/Race",
# "Race or Ethnic Group"), so no race block changes.
_DIMENSION_JOIN = r"(?:/|&|and\s*/\s*or\b|and\b|or\b)"
_COMBINED_DIMENSION = (r"(?:race\s*" + _DIMENSION_JOIN + r"\s*(?:ethnicity|ethnic\s+group)"
                       r"|(?:ethnicity|ethnic\s+group)\s*" + _DIMENSION_JOIN + r"\s*race)")
_RACE_DIMENSION_PREFIX_RX = re.compile(
    r"^(?:" + _COMBINED_DIMENSION + r"|race)\s*[:\-–—_](?:\s*[:\-–—_])*\s*(?=\S)", re.IGNORECASE)


# Negation as a word ("Not Reported", "Non-White", "No race") or "non" joined
# to the next word ("Nonwhite"): the negation check above.
_RACE_NEGATION_RX = re.compile(r"(?<![a-z])(?:non|not|no)(?![a-z])|(?<![a-z])non(?=[a-z])",
                               re.IGNORECASE)


def _negates_race(text: str) -> bool:
    """True when the text holds a negation ("Not Reported", "Non-African
    American", "Nonwhite"); a fuzzy match of a stripped label must agree with
    its key on this (block comment above)."""
    return _RACE_NEGATION_RX.search(text) is not None


def _strip_race_dimension(label: str) -> Optional[str]:
    """The cleaned label without a leading race dimension name, or None when
    it has none (block comment above).

      "Race: Black"                             -> "Black"
      "Race and Ethnicity : White Non-Hispanic" -> "White Non-Hispanic"
      "Ethnicity or Race: White"                -> "White"
      "Race unknown"                            -> None
      "Ethnicity: Hispanic"                     -> None
    """
    text = clean_demographic_label(label)
    prefix = _RACE_DIMENSION_PREFIX_RX.match(text)
    if prefix is None:
        return None
    return text[prefix.end():].strip() or None


def _map_plain_row(label: str, count: int, pct_converted: bool,
                   total_only: bool = False) -> Optional[Dict]:
    """Map one row the way every plain race row is mapped.

    Returns None for a purely ethnicity label (e.g. "Hispanic or Latino"), which
    is left to the ethnicity pipeline. Otherwise the mapping of
    _map_race_label (direct, slash-fragment, fuzzy, then race/ethnicity split)
    with the row's count. A race-by-sex row's race part goes through exactly
    this function, so it lands where the same text would land on its own row.
    A leading race dimension name is removed first (block comment "Race
    dimension named in the row label").
    """
    stripped = _strip_race_dimension(label)
    text = stripped if stripped is not None else label
    if _is_ethnicity_only_label(text):
        return None
    mapping = _map_race_label(text, guard_negation=stripped is not None)
    if stripped is not None:
        mapping["original"] = clean_demographic_label(label)
        mapping["flags"].append("dimension_wording_stripped")
    mapping["count"] = count
    if pct_converted:
        mapping["flags"].append("pct_to_count")
    if total_only:
        mapping["flags"].append(table_selection.TOTAL_CLASS_FLAG)
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
    rows, pct_converted, total_only = _measure_rows(measure, overall_group_id, fallback_denom)
    whole_table = _ethnicity_dimension_is_the_table([label for label, _count in rows])
    results = []
    for label, count in rows:
        mapping, _skipped = _map_table_row(label, count, pct_converted, total_only, whole_table)
        if mapping is not None:
            results.append(mapping)
    return results

# ── Ethnicity rows inside a race table ──────────────────────────────────────
#
# Some registrants post race and ethnicity as one classification in one table:
# "Race/Ethnicity, Customized" with rows Caucasian / African-American /
# Hispanic / Unknown, each participant in exactly one row (NCT00416078).
# Others list ethnicity beside a complete race breakdown in the same table,
# or post an ethnicity table under a race title.
#
# Method. A row whose label _is_ethnicity_only_label accepts ("Hispanic",
# "Not Hispanic or Latino") is never counted as race: _map_plain_row returns
# None for it and the ethnicity pipeline reads the same measure on its own.
# Each such row of a table that is counted is recorded on the race block in
# skipped_ethnicity_rows, in source order, as
#   {"original":     the posted label, cleaned as every race label is
#                    (clean_demographic_label); for a race-by-sex row the whole
#                    posted label ("Hispanic/Latino- Male"), with "race_part"
#                    and "sex" beside it as on a counted race-by-sex row,
#    "count":        the row's count, read exactly as a counted row's is,
#    "source_table": the title of the table the row was posted in,
#    "flags":        "pct_to_count" when the count was converted from a
#                    percentage, "race_by_sex" for a race-by-sex row,
#                    "ethnicity_dimension_row" for a row naming the ethnicity
#                    dimension that is not ethnicity-only (block comment
#                    "Ethnicity dimension named in the row label")}.
# The block carries the flag "ethnicity_rows_skipped" exactly when at least
# one recorded row has a count above zero.
#
# Why. Before this, the row left no trace on the race block: not in
# raw_categories, quarantined_labels or flags. In a combined classification
# the published White total is then non-Hispanic White only, and the Hispanic
# participants appear only as unknown_inferred through denominator balancing,
# which a reader could not tell from participants whose race the table did not
# report. The title flag combined_race_ethnicity does not separate the two: it
# is set from the word "ethnicity" in the title, also on tables whose rows are
# race only.
#
# What it does not do. The recorded rows are in none of omb_totals,
# raw_categories or the table selection, which are computed as if the rows
# were not there. (Whether a shortfall is filed as unknown_ethnicity_only or
# unknown_inferred does read their counts: next block comment.) The record is
# the posted rows, not a verdict on the table: whether the rows form one
# exclusive classification (race rows short of the population by about the
# recorded counts) or repeat people already in the race rows (race rows cover
# everyone) is for the reader to work out from the counts.
#
# Scope. Only tables counted for the totals are recorded, as raw_categories
# lists only their rows; a superseded table's rows (race_table_superseded) are
# not. A sex/gender-titled table with an ethnicity-only row is not read as a
# race table at all (_sex_titled_race_table), so it records nothing. The
# record survives all-zero rejection, as quarantined_labels does, and so does
# its flag: a race-titled table whose every row is ethnicity ("Hispanic or
# Latino" / "Not Hispanic or Latino") ends as reported False with flags
# ["all_zero_rejection", "ethnicity_rows_skipped"] and its rows listed, which
# says why a posted "race" table produced no race counts.
#
# skipped_ethnicity_rows is present on every race block: [] means no counted
# race table had such a row. A record extracted before the field existed has
# no such key, which is "not looked at", not "none".
#
# Measured on the baseline modules of the 80,890 trials in the 2026-11-08
# snapshot (CT.gov API v2, re-fetched 2026-11-14): 3,779 trials record at
# least one row (4,332 rows) and 3,693 carry the flag. In 3,239 the block is
# reported and the race rows fall short of the population (unknown_inferred
# above zero), in 335 the race rows cover everyone, in 80 every recorded row
# is zero, and 125 end in all-zero rejection. In 15 of the reported ones the
# table's title does not mention ethnicity ("Race" with a "Hispanic" row), so
# the title flag cannot find them.
#
# A label that names a race beside the ethnicity term ("Non-Hispanic White")
# is a race row and is not recorded here (_race_joined_to_ethnicity). Before
# that rule such rows were skipped too, and the field listed 5,624 rows in
# 3,929 trials. A label the rule's exact match leaves out can still name a
# race ("Hispanic (regardless of race)", "Non-Hispanic Whites": 119 of the
# 4,332 rows), so a reader must not take every recorded row for ethnicity
# alone.

# ── A shortfall that the ethnicity rows account for ─────────────────────────
#
# In a combined classification each participant is in exactly one row: a race
# row, the table's own "Unknown" row, or a Hispanic row. The Hispanic row's
# participants were given no race by the table. They are not participants the
# table lost track of, yet denominator balancing put them in unknown_inferred
# with every other gap.
#
# Method. unknown_not_reported has three parts that add up to it:
#   unknown_explicit        counted rows that map to unknown ("Unknown", "Not
#                           Reported");
#   unknown_ethnicity_only  participants of the recorded ethnicity rows, when
#                           those rows account for the whole shortfall (below);
#   unknown_inferred        the rest of the gap between the counted rows and
#                           the baseline population, with no row behind it.
# The shortfall (baseline population minus the sum of the counted rows) is
# moved from unknown_inferred to unknown_ethnicity_only only when all of
# these hold:
#   - the block is reported and its baseline population is known;
#   - the recorded rows (skipped_ethnicity_rows) that state the ethnicity
#     ("Hispanic", "Latinx", "Hispanic (regardless of race)") add up to
#     EXACTLY the shortfall;
#   - no recorded row that negates it ("Not Hispanic or Latino",
#     "Non-Hispanic": _negates_ethnicity) has a count above zero. Such a row
#     describes people the race rows already hold, so the table is not one
#     exclusive classification.
# The block then carries the flag "shortfall_is_ethnicity_rows", and
# unknown_inferred is 0. In every other case unknown_ethnicity_only is 0 and
# unknown_inferred is the whole shortfall, as before.
#
# The test is exact on purpose. Where the Hispanic rows are smaller than the
# shortfall, part of the gap has no row behind it; where they are larger,
# they overlap the race rows. In neither case do the counts show which
# participants of the gap are the Hispanic row's, so nothing is moved.
#
# What does not change: omb_totals (unknown_not_reported is the same number),
# unknown_explicit, raw_categories, skipped_ethnicity_rows, the table
# selection and the flag denominator_balanced, which still says that the
# shortfall was added to unknown_not_reported. Only the split of that number
# between its parts changes. A reader of unknown_inferred alone must add
# unknown_ethnicity_only to it to get the shortfall it used to hold.
#
# A record extracted before this field existed has no unknown_ethnicity_only
# key: "not looked at", not 0.
#
# Measured on the baseline modules of the 80,890 trials in the 2026-11-08
# snapshot (CT.gov API v2, re-fetched 2026-11-14): 2,688 trials carry the
# flag, and their 881,433 participants are filed as unknown_ethnicity_only.
# unknown_inferred goes from 2,329,443 to 1,448,010; unknown_not_reported
# (5,659,420) and every other value of every block are the same. In 551 more
# trials with a shortfall and recorded rows above zero, the rows do not add
# up to the shortfall exactly or a negating row has participants, and
# nothing is moved.

_ETHNICITY_TERM_RX = re.compile(r"hispanic|latin(?:o|a|x|e)", re.IGNORECASE)
_NEGATION_RX = re.compile(r"\b(?:non|not|no|neither|nor)\b|\bnon(?=[a-z])|\bother\s+than\b", re.IGNORECASE)
# A bare answer after the last ethnicity term: "Hispanic or Latino: No",
# "Hispanic or Latino ethnicity - No". Anything longer ("Hispanic (not
# African American)", "Hispanic: No retinopathy") qualifies something else.
_NEGATED_ANSWER_RX = re.compile(
    r"^[\W_]*(?:(?:ethnicity|origin|descent)[\W_]*)?(?:no|not|none)[\W_]*$", re.IGNORECASE)

def _negates_ethnicity(label: str) -> bool:
    """True when a recorded ethnicity row says its participants are NOT of the
    ethnicity: a negation ("non", "not", "no", "neither", "nor", "other than",
    or "non" joined to the next word) stands before the first ethnicity term,
    or the label ends in a bare negative answer after the last one.

      "Not Hispanic or Latino", "Non-Hispanic", "Nonhispanic"   -> True
      "Not of Hispanic, Latino/a, or Spanish origin"           -> True
      "Other than Hispanic or Latino"                          -> True
      "Hispanic or Latino: No", "Hispanic ethnicity - No"      -> True
      "Hispanic", "Hispanic (not African American)"            -> False
      "Latino, non-Black", "Hispanic: No retinopathy"          -> False

    A negation before the term that belongs to another word ("Non-Black
    Hispanic", "Prefer not to answer (Hispanic)") also reads as True. That
    errs towards leaving a shortfall in unknown_inferred. A label with no
    ethnicity term at all reads as True for the same reason.
    """
    text = clean_demographic_label(label)
    terms = list(_ETHNICITY_TERM_RX.finditer(text))
    if not terms:
        return True
    if _NEGATION_RX.search(text[:terms[0].start()]) is not None:
        return True
    return _NEGATED_ANSWER_RX.match(text[terms[-1].end():]) is not None

# ── Ethnicity dimension named in the row label ─────────────────────────────
#
# A combined race and ethnicity table can carry rows that name the ethnicity
# dimension: "Ethnicity Not Reported", "Ethnicity-Unknown or Not Reported",
# "Ethnicity-Mandinka" / "Ethnicity-Wolof", "Ethnicity-Not Japanese",
# "Ethnicity: White British". Unless such a row named Hispanic
# (_is_ethnicity_only_label), it was mapped as race and, matching nothing,
# counted as other. Where the table's race rows already hold everyone, that
# counted the same people twice: NCT00452530 posts White 2,427 / Black 31 /
# Asian 506 / Native Hawaiian 2 / Other 91 of 3,057, and "Ethnicity not
# reported" 2,946 was added to other (3,037); NCT03197376 posts Race-African
# 2,249 of 2,250 and its eight ethnic groups were added to other (2,250).
#
# Method. A row names the ethnicity dimension when its cleaned label begins
# with "Ethnicity" or "Ethnic group" as a word ("Ethnicities" does not), not
# followed by "/ Race", "& Race", "and Race", "or Race" or "and/or Race" (a
# combined name, as "Race/Ethnicity", whose rows belong to the one
# classification; its prefix is removed by the race dimension rule above, so
# "Ethnicity or Race: White" is read as White). Then, by table:
#   - every row of the table names it and at least one row, without the
#     prefix, names a race category itself (White, Black, Asian, American
#     Indian, Native Hawaiian or Pacific Islander, more than one race: a
#     mapping to one of them with no subcategory;
#     _ethnicity_dimension_is_the_table): the registrant used "Ethnicity" for
#     the one classification the table posts, a race classification by its
#     rows (NCT03476187: "Ethnicity : Caucasian" 154 / "Ethnicity : Black or
#     African American" 62 / "Ethnicity : Hispanic or Latin" 16 ...). A row
#     naming an origin group that the vocabulary files under a race with a
#     subcategory ("Japanese", "Chinese", "Arab", "African", "Native
#     Hawaiian") is not that evidence: such a name is as much an ethnicity as
#     a race, and "Ethnicity: Japanese" / "Ethnicity: Not Japanese" classifies
#     one ethnicity, not race (a review finding). In the 2026-11-15 inputs,
#     all five all-prefixed tables that pass the test have a row naming a race
#     category itself, so this requirement changes no output there. The
#     dimension name and its separator(s) (":", "-",
#     "–", "—", "_", ",") are removed and the rest is read exactly as the bare
#     label, flagged dimension_wording_stripped, as a "Race:" prefix is (block
#     comment "Race dimension named in the row label"). A row of such a table
#     with no separator ("Ethnicity" alone) is recorded as below. An
#     all-prefixed table with no race row ("Ethnicity: Hispanic" / "Ethnicity:
#     Not Hispanic" / "Ethnicity: Unknown") is an ethnicity classification, read
#     as below: its "Unknown" is not a race count;
#   - otherwise the row is a row of the table's ethnicity classification, not
#     a race row: it is left out of the race counts and recorded in
#     skipped_ethnicity_rows with the flag ethnicity_dimension_row, beside the
#     ethnicity-only rows (block comment "Ethnicity rows inside a race table").
# An ethnicity-only row ("Ethnicity: Hispanic") is recorded as before, without
# the new flag. _map_table_row holds the rule, for _parse_race_table and
# extract_race_from_measure alike; in a table crossing race with sex
# ("Ethnicity-Not Reported Female") it is applied to the race parts, and a
# recorded row keeps its race_part and sex.
#
# The recorded rows enter the shortfall test (block comment "A shortfall that
# the ethnicity rows account for") as every recorded row does. A row with no
# Hispanic term reads there as negating the ethnicity, so where such a row has
# participants the shortfall stays in unknown_inferred. That is the intended
# reading: the table classifies ethnicity by more than the Hispanic row, so it
# is not one exclusive classification whose Hispanic row is the shortfall.
#
# Where the race rows do not hold everyone, the participants of a recorded row
# now count in unknown_inferred instead of other: NCT01022268 posts race rows
# and "Declined" 77 beside "Ethnicity not available" 291, of 902, and those 291
# were given no race by the table.
#
# Measured on the baseline modules of the 80,988 trials in the 2026-11-15
# snapshot (CT.gov API v2, re-fetched 2026-11-18; engine main reproduced its
# race blocks): 64 race blocks change. Other falls by 14,742; White +414,
# Black +84, American Indian +19, Asian +14 (three all-"Ethnicity" tables
# read as race: NCT03476187, NCT04711460, NCT03178669); unknown +237. One
# block goes from reported to not reported: NCT01452191 posts only
# "Ethnicity: White British" 1,008 and "Ethnicity: Other ethnic group" 54,
# neither a race the vocabulary reads, so no row shows the table to be a race
# classification and it is read as an ethnicity one (before: other 1,062).
# 67 trials record rows flagged ethnicity_dimension_row, 14,225
# participants. Race blocks whose counts add up to more than the baseline
# population fall from 392 to 338, their excess by 12,862; none rises. No
# ethnicity block changes.

_ETHNICITY_DIMENSION_RX = re.compile(
    r"^\s*ethnic(?:ity|\s+group)(?![a-z])(?!\s*" + _DIMENSION_JOIN + r"\s*race)", re.IGNORECASE)
_ETHNICITY_DIMENSION_PREFIX_RX = re.compile(
    r"^\s*(?:ethnicity|ethnic\s+group)\s*[:\-–—_,](?:\s*[:\-–—_,])*\s*(?=\S)", re.IGNORECASE)


def _names_ethnicity_dimension(label: str) -> bool:
    """True when a row's label begins with the ethnicity dimension
    ("Ethnicity: Unknown", "Ethnicity Not Reported", "Ethnic group - Other")."""
    return _ETHNICITY_DIMENSION_RX.match(clean_demographic_label(label)) is not None


def _strip_ethnicity_dimension(label: str) -> Optional[str]:
    """The cleaned label without a leading ethnicity dimension name and its
    separator, or None when it has no such prefix ("Ethnicity : White" ->
    "White"; "Ethnicity Not Reported" -> None)."""
    text = clean_demographic_label(label)
    prefix = _ETHNICITY_DIMENSION_PREFIX_RX.match(text)
    if prefix is None:
        return None
    return text[prefix.end():].strip() or None


def _ethnicity_dimension_is_the_table(labels: List[str]) -> bool:
    """True when every row of a table names the ethnicity dimension and at
    least one of them, without the prefix, names a race category itself: the
    registrant's name for the race classification the table posts. A row
    naming an origin group the vocabulary files under a race with a
    subcategory ("Japanese", "Arab", "African") is not that evidence, since
    such a name is as much an ethnicity as a race. An all-prefixed table with
    no such row ("Ethnicity: Hispanic" / "Ethnicity: Not Hispanic" /
    "Ethnicity: Unknown"; "Ethnicity: Japanese" / "Ethnicity: Not Japanese")
    is an ethnicity classification (block comment "Ethnicity dimension named
    in the row label")."""
    if not labels or not all(_names_ethnicity_dimension(label) for label in labels):
        return False
    for label in labels:
        stripped = _strip_ethnicity_dimension(label)
        mapping = _map_plain_row(stripped, 0, False) if stripped is not None else None
        if (mapping is not None and mapping.get("confidence") != "low"
                and mapping.get("omb_category") in _RACE_SPECIFIC
                and not mapping.get("subcategory")):
            return True
    return False


def _map_table_row(label: str, count: int, pct_converted: bool, total_only: bool,
                   whole_table: bool, race_part: Optional[str] = None,
                   sex: Optional[str] = None) -> Tuple[Optional[Dict], Optional[Dict]]:
    """(mapping, None) for a row counted as race, (None, record) for a row left
    out of the race counts (_skipped_ethnicity_row): an ethnicity-only row,
    or a row naming the ethnicity dimension in a table where other rows do
    not (block comment "Ethnicity dimension named in the row label").
    whole_table is _ethnicity_dimension_is_the_table for the row's table, over
    the race parts when the table crosses race with sex. For a race-by-sex row
    the race part is what is read, and the record keeps race_part and sex
    beside the posted label."""
    text = race_part if race_part is not None else label
    stripped = _strip_ethnicity_dimension(text) if whole_table else None
    mapping = _map_plain_row(stripped if stripped is not None else text, count,
                             pct_converted, total_only)
    if mapping is None or (stripped is None and _names_ethnicity_dimension(text)):
        row = _skipped_ethnicity_row(label, count, pct_converted, race_part, sex, total_only)
        if mapping is not None:
            row["flags"].append("ethnicity_dimension_row")
        return None, row
    if stripped is not None:
        # The text read, prefix included: the posted label for a plain row
        # (a race-by-sex caller then records the whole posted label itself).
        mapping["original"] = clean_demographic_label(text)
        mapping["flags"].append("dimension_wording_stripped")
    return mapping, None


def _skipped_ethnicity_row(label: str, count: int, pct_converted: bool,
                           race_part: Optional[str] = None,
                           sex: Optional[str] = None, total_only: bool = False) -> Dict:
    """The record of one ethnicity-only row left out of the race counts (block
    comment above). The table title is added when the table is counted."""
    row: Dict = {"original": clean_demographic_label(label), "count": count}
    flags: List[str] = []
    if race_part is not None:
        row["race_part"] = race_part
        row["sex"] = sex
        flags.append("race_by_sex")
    if pct_converted:
        flags.append("pct_to_count")
    if total_only:
        flags.append(table_selection.TOTAL_CLASS_FLAG)
    row["flags"] = flags
    return row

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
# The whole gate is _sex_titled_race_table; the legacy sex extractor calls it
# too (is_sex_titled_race_table) and quarantines the rows of a table that
# passes instead of counting them as sex "unknown".
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
# "race_tables_partitioned_by_description" as well when two of those parts
# are told apart only by their population description
# (table_selection.parted_by_description);
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
    "categories", "quarantined", "skipped_ethnicity"}: the table's own Number
    Analyzed (or None), the counted category records, the quarantined labels
    and the ethnicity-only rows left out of the counts (_skipped_ethnicity_row).
    A race-titled measure always returns a table (plain rows unless it passes
    the race-by-sex test). A sex/gender-titled measure returns None unless
    every row maps to a race-specific category (see the block comment above).
    """
    title = measure.get("title", "")
    rows, pct_converted, total_only = _measure_rows(measure, overall_group_id, fallback_denom)
    splits = [split_race_by_sex_label(label) for label, _count in rows]
    by_sex_rows = bool(rows) and all(sp is not None for sp in splits)

    categories: List[Dict] = []
    quarantined: List[Dict] = []
    skipped: List[Dict] = []
    race_by_sex = False
    if by_sex_rows:
        # The same ethnicity-dimension routing as a plain table, on the race
        # parts (block comment "Ethnicity dimension named in the row label").
        whole_by_sex = _ethnicity_dimension_is_the_table([race_part for race_part, _sex in splits])
        for (label, count), (race_part, sex) in zip(rows, splits):
            mapping, skipped_row = _map_table_row(label, count, pct_converted, total_only,
                                                  whole_by_sex, race_part, sex)
            if skipped_row is not None:
                skipped.append(skipped_row)
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
        categories, quarantined, skipped = [], [], []
        whole_table = _ethnicity_dimension_is_the_table([label for label, _count in rows])
        for label, count in rows:
            mapping, skipped_row = _map_table_row(label, count, pct_converted, total_only, whole_table)
            if skipped_row is not None:
                skipped.append(skipped_row)
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
        "population": table_selection.population_of(measure),
        "sex_titled": sex_titled,
        "race_by_sex": race_by_sex,
        "categories": categories,
        "quarantined": quarantined,
        "skipped_ethnicity": skipped,
    }

def _sex_titled_race_table(measure: dict, index: int, overall_group_id: Optional[str],
                           fallback_denom: Optional[int]) -> Optional[Dict]:
    """The race table a sex/gender-titled measure carries, or None.

    The gate of the block comment above, in one place: a measure that is not
    race-titled (is_race_table) but whose title the manuscript parser selects
    (MEASURE_TITLE_RX) is read as race only when _parse_race_table accepts
    every row. extract_race_data and the legacy sex extractor
    (sex_extractor.extract_sex_data, through is_sex_titled_race_table) both
    call this, so a row the race path reads as race is the row the sex path
    keeps out of sex "unknown".
    """
    title = measure.get("title", "")
    if is_race_table(title) or not MEASURE_TITLE_RX.search(title):
        return None
    return _parse_race_table(measure, index, overall_group_id, fallback_denom, sex_titled=True)

def is_sex_titled_race_table(measure: dict, overall_group_id: Optional[str] = None,
                             fallback_denom: Optional[int] = None) -> bool:
    """True when extract_race_data reads this sex/gender-titled measure as race
    (the race_from_sex_titled_table gate), whether or not a race-titled table
    later supersedes it. Pass the overall group id and baseline participant
    count extract_race_data would use, so percentage rows convert the same way.
    """
    return _sex_titled_race_table(measure, 0, overall_group_id, fallback_denom) is not None

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
        - unknown_explicit, unknown_ethnicity_only, unknown_inferred: the
          three parts of omb_totals["unknown_not_reported"]
        - skipped_ethnicity_rows: the ethnicity-only rows of the counted
          tables, which are in none of the totals (see the block comment
          "Ethnicity rows inside a race table")
        - flags: List of any issues
    """
    return _extract_race(study)[0]


def _extract_race(study: dict) -> Tuple[Dict, List[Dict]]:
    """extract_race_data's block, and the tables it counted (each as
    _parse_race_table returns it). The tables are for combined_classification;
    they are not part of the published block."""
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
        # unknown_explicit / unknown_ethnicity_only / unknown_inferred
        # disaggregate omb_totals["unknown_not_reported"] into its three
        # sources (see the denominator-balancing comment below and the block
        # comment "A shortfall that the ethnicity rows account for").
        # unknown_not_reported always equals their sum — current dashboard
        # display is unaffected; this is purely additive.
        "unknown_explicit": 0,
        "unknown_ethnicity_only": 0,
        "unknown_inferred": 0,
        # The part of omb_totals["white"] in Middle Eastern or North African
        # rows (block comment "Middle Eastern or North African within
        # White"); 0 when there are none.
        "mena_in_white": 0,
        "subcategory_totals": {},
        "raw_categories": [],
        "quarantined_labels": [],
        "joint_sex_counts": None,
        "skipped_ethnicity_rows": [],
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
        else:
            table = _sex_titled_race_table(measure, index, overall_group_id, total_participants)
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
        if table_selection.parted_by_description(counted, "omb_category"):
            result["flags"].append("race_tables_partitioned_by_description")

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

        # Ethnicity-only rows of this table: recorded, never counted (block
        # comment "Ethnicity rows inside a race table").
        for row in table["skipped_ethnicity"]:
            result["skipped_ethnicity_rows"].append({
                "original": row["original"],
                "count": row["count"],
                "source_table": table["title"],
                **{k: row[k] for k in ("race_part", "sex") if k in row},
                "flags": row["flags"],
            })

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
    # be added to unknown_not_reported.  The rows themselves are listed in
    # skipped_ethnicity_rows; when they add up to exactly this gap, the gap
    # is filed as unknown_ethnicity_only, else as unknown_inferred. The
    # amount added to unknown_not_reported is the same either way.
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
                result["flags"].append("denominator_balanced")
                # Which part of unknown the shortfall belongs to (block
                # comment "A shortfall that the ethnicity rows account for"):
                # the ethnicity rows' participants when those rows add up to
                # exactly the shortfall and none negates the ethnicity; else
                # a gap with no row behind it.
                stated = sum(row["count"] for row in result["skipped_ethnicity_rows"]
                             if not _negates_ethnicity(row["original"]))
                negated = sum(row["count"] for row in result["skipped_ethnicity_rows"]
                              if _negates_ethnicity(row["original"]))
                if stated == remainder and negated == 0:
                    result["unknown_ethnicity_only"] = remainder
                    result["flags"].append("shortfall_is_ethnicity_rows")
                else:
                    result["unknown_inferred"] = remainder

    # All-zero rejection: if every mapped category has 0 participants,
    # mark the demographic as not collected rather than showing empty rows
    if result["reported"] and all(v == 0 for v in result["omb_totals"].values()):
        result["reported"] = False
        result["omb_totals"] = {k: 0 for k in result["omb_totals"]}
        result["raw_categories"] = []
        result["subcategory_totals"] = {}
        result["joint_sex_counts"] = None
        result["flags"] = ["all_zero_rejection"]

    # After all-zero rejection, so the flag and the rows it describes stay
    # together on a rejected block too (quarantined_labels is kept there the
    # same way). The flag means "a counted table posted participants in an
    # ethnicity-only row"; rows of zero are recorded without it.
    if any(row["count"] > 0 for row in result["skipped_ethnicity_rows"]):
        result["flags"].append("ethnicity_rows_skipped")

    # The MENA part of White, from the counted rows' subcategories (block
    # comment "Middle Eastern or North African within White"). After
    # all-zero rejection, which empties subcategory_totals: a rejected block
    # holds 0.
    result["mena_in_white"] = sum(result["subcategory_totals"].get(f"white_{sub}", 0)
                                  for sub in MENA_SUBCATEGORIES)

    # Dedupe flags
    # sorted() so identical extractions serialize identically across runs
    # (set iteration order varies per process and made week-over-week
    # data diffs noisy for no reason)
    result["flags"] = sorted(set(result["flags"]))

    return result, counted


def combined_classification(study: dict) -> Optional[Dict]:
    """The one counted race table of a trial when it is an exact combined race
    and ethnicity classification, else None.

    Exact means what the flag shortfall_is_ethnicity_rows means (block comment
    "A shortfall that the ethnicity rows account for"): race rows, the table's
    own unknown rows and its rows stating the ethnicity add up to exactly the
    baseline population, and no row negating the ethnicity has participants.
    Summed parts (race_tables_partitioned) are not one classification and
    give None.

    Returns {"index": the measure's position among the baseline measures,
    "title", "race_rows": participants in the counted rows other than
    unknown (the race-specific categories and "other"), "race_rows_named":
    the part of race_rows in rows that place their participants outside the
    Hispanic row, "ethnicity_rows": unknown_ethnicity_only, "unknown_rows":
    unknown_explicit}. The ethnicity extractor reads it to count the
    participants of race_rows_named as not Hispanic (ethnicity_extractor,
    "Race rows of a combined classification").

    Two kinds of race row are in race_rows and not in race_rows_named:
      - a row whose label also names an unknown ("Other/Unknown",
        "Other/No response", "Other/UTD", "Subject did not offer
        ethnicity"): it maps to race "other", but some of its participants
        are unknown, and an unknown participant may be Hispanic;
      - a row whose label states the ethnicity ("Hispanic White", "Black
        Latino", "White - Hispanic/Latino"): a race row since the joined
        reading, but its participants are Hispanic, not outside the Hispanic
        row. A label that negates it ("Non-Hispanic White") or names no
        ethnicity ("White") stays in.
    """
    race, counted = _extract_race(study)
    if "shortfall_is_ethnicity_rows" not in race["flags"] or len(counted) != 1:
        return None
    table = counted[0]
    race_cats = [c for c in table["categories"] if c["omb_category"] != "unknown_not_reported"]
    named = [c for c in race_cats
             if not _UNKNOWN_TERM_RX.search(c["original"]) and not _states_ethnicity(c["original"])]
    return {"index": table["index"], "title": table["title"],
            "race_rows": sum(c["count"] for c in race_cats),
            "race_rows_named": sum(c["count"] for c in named),
            "ethnicity_rows": race["unknown_ethnicity_only"], "unknown_rows": race["unknown_explicit"]}


# Words that put a row's participants, or some of them, among the unknown.
_UNKNOWN_TERM_RX = re.compile(
    r"\b(?:unknown|unk|missing|unspecified|undisclosed|undetermined|indeterminate|unsure|utd|n/?a"
    r"|not\s+(?:reported|known|available|specified|stated|collected|disclosed|given|provided|obtained"
    r"|recorded|documented|determined|identified|answered|sure)"
    r"|declin(?:e|ed|es|ing)|refus(?:e|ed|es|al)|prefer(?:red|s)?\s+not"
    r"|no\s+(?:response|answer|data)|non-?response|unable\s+to\s+determine"
    r"|did\s+not\s+(?:offer|answer|provide|report|disclose|specify|state|respond)"
    r"|don'?t\s+know|do\s+not\s+know)\b",
    re.IGNORECASE)


def _states_ethnicity(label: str) -> bool:
    """True when a race row's label says its participants ARE Hispanic or
    Latino: it holds an ethnicity term and _negates_ethnicity does not read
    it as negated ("Hispanic White", "Black Latino"; not "Non-Hispanic
    White", not "White")."""
    text = clean_demographic_label(label)
    return _ETHNICITY_TERM_RX.search(text) is not None and not _negates_ethnicity(text)
