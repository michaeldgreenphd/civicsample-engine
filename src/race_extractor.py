"""
Race Data Extractor

Extracts race data from baseline characteristics and maps to NIH/OMB categories
with granular subcategories preserved.
"""
import re
import unicodedata
from typing import Dict, FrozenSet, List, Optional, Set, Tuple
from rapidfuzz import fuzz, process
from src.utils import (clean_demographic_label, COMBINED_DIMENSION, DIMENSION_JOIN,
                       is_overall_group_title, measurement_number)
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

# ── European labels ────────────────────────────────────────────────────────
#
# OMB's White category is the original peoples of Europe (the 1997 standard
# adds the Middle East and North Africa, which the 2024 revision makes a
# category of its own; block comment "Middle Eastern or North African within
# White"). The vocabulary held "European", "Eastern European" and "Western
# European", and slash fragments read many template wordings ("White - White/
# Caucasian/European Heritage" through "White"). Other wordings naming only
# European origin matched nothing: "White (European)" (10,806 participants in
# NCT00412984, in the 2026-11-15 inputs), "White or European American"
# (609 in 4 trials), "Western European White", "Caucasian (European
# descent)", "White European" were counted as other, and "North/Mid/East
# European" (3,717), "South European" (1,621), "North/Middle European",
# "European descent" and the New Zealand census wordings "NZ European/
# Pākehā" (151), "New Zealand European" (31 in 3 trials), "NZ European" and
# "New Zealander, European" were quarantined as not race, their participants
# then balanced to unknown.
#
# Method (european_label; after the slash fragments and before the fuzzy
# match, so a label the vocabulary or a slash fragment already reads as White
# keeps that reading and its subcategory). A cleaned label (lower case,
# accents decomposed, so "Pākehā" reads "pakeha"; a hyphen between letters as
# a space; "." and "'" removed) maps to White, flagged european_label, when it
# is made only of these terms, joined by "/", ",", ";", "&", "(", ")", ":",
# " - ", "or", "and" or "and/or", with at least one European name:
#   - European names: European(s), Europe, European American(s), Pakeha
#     (the Maori name for New Zealanders of European descent, posted as "NZ
#     European/Pākehā"; Stats NZ's ethnicity classification splits European
#     into New Zealand European and Other European, and its 2001 review notes
#     that some prefer "Pākehā" to "New Zealand European"; read from search
#     summaries, the Stats NZ pages could not be fetched here);
#   - with a European name only: White, Caucasian(s); a direction (North(ern),
#     South(ern), East(ern), West(ern), Central, Mid, Middle, but not "Middle
#     East"); New Zealand, New Zealander(s), NZ; "Other" ("Other European");
#     the nationalities posted as examples of European (British, French,
#     Scandinavian: "European (e.g., British, French, Western European,
#     Eastern European)");
#   - fillers: descent, origin(s), ancestry, background, people(s), of, only,
#     e.g., i.e.
# A label beginning or ending with a join is not read (a piece of a longer
# label). Each alternative the label joins ("or", "and", "/", ",", "&",
# ";") must name a group on its own (_european_alternatives; review
# findings): a European name with its qualifiers ("NZ European", "Western
# European White"), or White, Caucasian or a European nationality. So
# "European or New Zealander" and "North or European" are not read. An
# alternative made only of directions shares the next one's noun
# ("North/Mid/East European"), and "New Zealander, European" keeps its
# comma as apposition. The subcategory follows the vocabulary's:
# european_eastern when every alternative is qualified by an east direction,
# european_western likewise for west, else european; an unqualified
# alternative ("European or Western European", "White people or Eastern
# European", "Eastern European or British") names a wider group.
#
# What it does not do. A label naming anything else beside European is not
# read: "Western background (European countries (excluding Turkey), North
# America, Japan, Indonesia, Oceania)", "Other (European/Arab)",
# "African-European", "White(origins of Europe, Middle East, or North
# Africa)" (which names the Middle East and North Africa, tracked apart),
# "Not Collected - European Participants" (a non-answer), "Non-White/European
# descent". "Roma European" stays as it was: whether Roma participants are
# counted as White is left to its own decision. A nationality alone
# ("French", "New Zealander") names no race and is not read.
#
# Measured on the baseline modules of the 80,988 trials in the 2026-11-15
# snapshot (CT.gov API v2, re-fetched 2026-11-18), against engine main after
# the Native Hawaiian or Pacific Islander labels: White +19,821 (34 labels,
# 48 trials), from other 13,310 and from rows quarantined as not race 6,511;
# other -13,310; unknown -6,511 (unknown_inferred -6,522,
# unknown_ethnicity_only +11). No trial changes whether it reports race. The
# white_european subcategory grows from 15,748 to 35,343, european_western
# from 22 to 526 and european_eastern from 5 to 32; this includes two
# heritage-template rows already read as White whose group, "Caucasian
# European", the heritage rule now reads through this rule ("White -
# Caucasian European Heritage", NCT00553358, 282; NCT04630002, 23). Two New
# Zealand trials reported through their "Maori" row alone now count their
# European rows as White (NCT06428591, 9; NCT06834568, 8; its "Dutch" row
# stays quarantined). In one combined classification (NCT01798992)
# the counted "European Ancestry" row leaves a shortfall that is exactly its
# Hispanic row, so the ethnicity extractor counts its race rows as not
# Hispanic (49 participants from unknown). No other ethnicity block, no sex
# block and no sex/gender-titled gate changes; mena_in_white is unchanged.
_EUROPEAN_NAME = r"european\s+americans?|europeans?|europe|pakehas?"
_EUROPEAN_DIRECTION = (r"north(?:ern)?|south(?:ern)?|east(?:ern)?|west(?:ern)?|central|mid|"
                       r"middle(?!\s*east)")
_EUROPEAN_TOKEN_RX = re.compile(
    rf"(?:(?P<name>{_EUROPEAN_NAME})|(?P<white>white|caucasians?)|"
    rf"(?P<other>other)|"
    rf"(?P<direction>{_EUROPEAN_DIRECTION})|"
    rf"(?P<new_zealand>new\s+zealand(?:ers?)?|nz)|"
    rf"(?P<nationality>british|french|scandinavians?)|"
    rf"(?P<filler>descent|origins?|ancestry|background|peoples?|of|only|e\s*g|i\s*e)|"
    rf"(?P<join>and/or|or|and))(?![a-z])|(?P<mark>[/,;&():\-–—])",
    re.IGNORECASE)


def european_label(label: str) -> Tuple[bool, Optional[str]]:
    """(True, subcategory) when a label names European origin only, by the
    terms of the block comment above; else (False, None).

      "White (European)"                         -> (True, "european")
      "North/Mid/East European"                  -> (True, "european")
      "Eastern European White"                   -> (True, "european_eastern")
      "NZ European/Pākehā"                       -> (True, "european")
      "White or European American"               -> (True, "european")
      "Other European"                           -> (True, "european")
      "Roma European"                            -> (False, None)
      "Other (European/Arab)"                    -> (False, None)
      "White(origins of Europe, Middle East, or North Africa)" -> (False, None)
    """
    text = unicodedata.normalize("NFKD", clean_demographic_label(label).lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", text)
    text = re.sub(r"[.'’]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    tokens: List[Tuple[str, str]] = []
    position = 0
    while position < len(text):
        match = _EUROPEAN_TOKEN_RX.match(text, position)
        if match is None or match.end() == position:
            return False, None
        kind = match.lastgroup
        if kind == "mark" and match.group("mark") in ("(", ")"):
            kind = "bracket"
        tokens.append((kind, match.group(0).lower()))
        position = match.end()
        while position < len(text) and text[position].isspace():
            position += 1
    kinds = [kind for kind, _text in tokens]
    # Every other term names nothing without a European name beside it.
    if "name" not in kinds:
        return False, None
    # A label beginning or ending with a join is a piece of a longer label.
    if kinds[0] in ("join", "mark") or kinds[-1] in ("join", "mark"):
        return False, None
    groups = _european_alternatives(tokens)
    if groups is None:
        return False, None
    return True, _european_subcategory(groups)


# Joins that set two groups side by side as alternatives, not one group
# described twice ("White or Eastern European", "Eastern European/White",
# "White & Eastern European", "Caucasian; Western European"; not "White:
# Eastern European", "White - Eastern European", "Eastern European White").
# Every join the rule accepts is here but ":", the dashes and brackets, which
# describe (a review finding).
_EUROPEAN_ALTERNATIVE_JOINS = frozenset({"or", "and", "and/or", "/", ",", "&", ";"})


def _european_alternatives(tokens: List[Tuple[str, str]]) -> Optional[List[List[str]]]:
    """Read each alternative of a label on its own (block comment above;
    review findings). The (kind, text) tokens are split at the joins in
    _EUROPEAN_ALTERNATIVE_JOINS; fillers, brackets, ":" and dashes describe
    and are passed over. Returns, for each alternative naming a group, the
    directions that qualify it, or [] for an unqualified group; None when an
    alternative names no European or White group.

    An alternative names a group when it holds a European name, qualified
    by the directions in it ("NZ European", "White: Western European"); or
    when it is White, Caucasian or a European nationality ("White people",
    "British"), unqualified. One holding none of these ("Other", "New
    Zealander") names nothing. Two shapes cross a
    join: an alternative made only of directions shares the noun of the next
    alternative that names one after a direction ("North/Mid/East European",
    "Western or Eastern European"; not "North or European"), and "New
    Zealander" before a comma and a European name is that name's qualifier
    ("New Zealander, European", posted beside a separate "European" row in
    NCT01117792; not "European or New Zealander")."""
    segments: List[List[Tuple[str, str]]] = [[]]
    joins: List[str] = []
    for kind, text in tokens:
        if kind in ("join", "mark") and text in _EUROPEAN_ALTERNATIVE_JOINS:
            segments.append([])
            joins.append(text)
        elif kind not in ("filler", "bracket", "mark"):
            segments[-1].append((kind, text))

    def only_directions(segment: List[Tuple[str, str]]) -> bool:
        return bool(segment) and all(kind == "direction" for kind, _text in segment)

    groups: List[List[str]] = []
    for i, segment in enumerate(segments):
        if not segment:
            continue  # ", or" leaves nothing between two joins
        segment_kinds = [kind for kind, _text in segment]
        if only_directions(segment):
            following = next((s for s in segments[i + 1:] if s and not only_directions(s)), None)
            if following is None or following[0][0] != "direction" or (
                    "name" not in [kind for kind, _text in following]):
                return None
            groups.append([text for _kind, text in segment])
            continue
        if segment_kinds == ["new_zealand"]:
            if (i < len(joins) and joins[i] == "," and i + 1 < len(segments)
                    and segments[i + 1] and segments[i + 1][0][0] == "name"):
                continue
            return None
        if "name" in segment_kinds:
            groups.append([text for kind, text in segment if kind == "direction"])
        elif "white" in segment_kinds or "nationality" in segment_kinds:
            groups.append([])
        else:
            return None
    return groups


def _european_subcategory(groups: List[List[str]]) -> str:
    """The subcategory of a label european_label reads, from the directions
    of its alternatives (_european_alternatives): european_eastern when every
    alternative is qualified and every direction is east, european_western
    likewise for west, else european. An unqualified alternative ("European
    or Western European", "White or Eastern European", "Eastern European or
    British") names a wider group than a direction does (review findings)."""
    if not groups or any(not directions for directions in groups):
        return "european"
    directions = [direction for group in groups for direction in group]
    if all(direction.startswith("east") for direction in directions):
        return "european_eastern"
    if all(direction.startswith("west") for direction in directions):
        return "european_western"
    return "european"


# ── Asian labels ───────────────────────────────────────────────────────────
#
# OMB's 1997 Asian category, the one this engine counts (block comment
# "Middle Eastern or North African labels"), is the original peoples of the
# Far East, Southeast Asia or the Indian subcontinent, with Cambodia, China,
# India, Japan, Korea, Malaysia, Pakistan, the Philippine Islands, Thailand
# and Vietnam as its examples. The 2024 revision keeps Asian as a minimum
# category and names Central or East Asia, Southeast Asia and South Asia
# instead, with Pakistani, Hmong and Afghan among its examples. Both are read
# from search summaries: the Federal Register pages could not be fetched
# here, so the 1997 wording is paraphrased, not quoted.
#
# The vocabulary held "Asian", national origins ("Chinese", "Pakistani") and
# the three regions ("East Asian", "South Asian", "Southeast Asian"). Wordings
# qualifying or restating those matched nothing. They were counted as other:
# "Asian (Japanese)" (2,181 participants in 19 trials, in the 2026-11-15
# inputs), "Asian-Chinese", "Asian: East", "Other Asian" (3,503 in 35),
# "Asian, Bangladeshi" (44,567, NCT00860470), "Asian or Asian American". Or
# they were quarantined as not race, their participants then balanced to
# unknown: "Sri Lankan" (223,865 in 4 trials; NCT01146496's only race row,
# 223,861, the count of its enrollment country), "Han" and its variants,
# "Malay", "Oriental" (1,218 in 44), "Nepalese". "Asian American" was
# fuzzy-matched to "African American" and counted as Black (84 in 18 trials;
# "Asian-American" 8 in 4).
#
# Method (asian_label; after the slash fragments and the European labels, so
# a label they read keeps that reading, and before the fuzzy match). A
# label cleaned as for European labels maps to Asian, flagged asian_label,
# when it is made only of these terms, with at least one Asian name:
#   - names, each with the vocabulary's subcategory key (_ASIAN_NAMES):
#     Asian(s), Asian American(s), American Asian, Oriental(s) (no key); Asian
#     Indian, East Indian; Chinese, Han (the majority people of China);
#     Japanese; (South) Korean; Vietnamese; Filipino; Pakistani; Bangladeshi;
#     Sri Lankan, Nepalese, Nepali, Desi (south_asian); Thai; Cambodian;
#     Laotian; Malaysian; Indonesian, Javanese; Taiwanese; Burmese, Hmong,
#     Malay, Singaporean (southeast_asian); the Indian subcontinent, OMB's own
#     wording (south_asian). "Indian" and "Indian American" name a group only
#     when the label names another Asian group or place: alone, "Indian" is
#     also the American Indian. A label holding "West Indian" (the
#     Caribbean) is not read;
#   - beside a name only: places (China, India, Japan, ..., Asia), which
#     describe ("Japanese from Japan"); directions (North East, South East,
#     East, South, Central, West); "Other"; negations (non, not, other than,
#     except, excluding), which take the names after them out of the reading
#     ("Asian (other than Chinese or Japanese)" is Asian; "Non-Chinese" alone
#     is not read); example words (e.g., including, etc.);
#   - fillers: descent, origin(s), ancestry, background, ethnic(ity), race,
#     people(s), population(s), subjects, participants, nationality, only,
#     of, from, in, i.e., total, number.
# A label beginning or ending with a join is not read. As for European
# labels, each alternative must name a group on its own (_asian_alternatives),
# and a bracket describes its alternative rather than adding alternatives
# ("Asian (China, Philippines, Vietnam, Korea, etc.)"). The subcategory:
# within an alternative, the term every other one refines ("East Asian
# (Chinese)": chinese), else the region they all lie in; across alternatives,
# theirs when all agree, else the region they all lie in when none is
# unqualified ("Indian or Pakistani": south_asian), else none ("Asian or Asian
# American", "East/South East Asian"). Terms given as examples, terms in a
# bracket holding "other" and a Central direction name no subcategory
# (Central Asia has no key; "Central/South Asian Heritage" has none).
#
# West Asian. In Eli Lilly's race template "West Asian" is the Indian
# subcontinent: of the 60 Lilly trials posting a West Asian wording (of 74
# in all), 14 spell it out ("West Asian (Indian sub-continent)" and three
# other spellings), and none posts a South Asian or Indian row. Elsewhere it
# is the Middle East: beside "South Asian" in Imperial College, Novartis and
# Rutgers tables, and Iranian or Afghan in the Canadian census categories VBI
# posts. A label naming the subcontinent is read as south_asian by
# asian_label. "West Asian" or "Western Asian" alone is read so only in a
# table that follows the template (_west_asian_template_rows): some row is
# read as East or Southeast Asian, every other Asian row is one of those,
# and no row names the Middle East, North Africa, Iran, Afghanistan, Turkey
# or South Asia. Such rows are flagged west_asian_template, confidence
# medium. Other West-qualified labels ("South or West Asian") are not read.
#
# One table may post two classifications of the same participants: "Chinese"
# 44 and "Han" 44 of 44 (NCT05135871), "Asian" 32 and "Japanese Ancestry" 32
# of 32 (NCT00661141), "Asian (Japanese)" 4, "Asian (Except for Japanese)" 0
# and "Asian (Total)" 4 of 18 (NCT04227600). _asian_breakdown_rows leaves such
# a breakdown out of the counts (quarantined, reason asian_breakdown) when the
# table's excess over its population is exactly the breakdown, reading a
# race-by-sex or race-by-group table one sex or group at a time (review
# findings; no such table in the 2026-11-15 inputs).
#
# What it does not do. "Asian or Pacific Islander" and other wordings joining
# Asian to the Pacific are the 1977 grouped category, left as they were
# ("Asian American and Pacific Islander" included, through
# _asian_joined_to_nhpi). The UK census wordings ("Asian or Asian British"
# 64,897 in 17 trials, beside "Black or Black British" and "White British")
# are left for a rule of their own. Rows crossing Asian with something else
# are not read: comparisons (NCT07465926 crosses every race with its
# comparison), ages, phases, parts, arms, generations, enrollment region.
# Neither are pooled labels ("Asian or Other", "Other (Asian, multiracial,
# unknown)"), "Non-Asian", mixed ones ("Asian - Mixed Race", "Mixed Asian"),
# the Chinese minorities posted beside Han (Zhuang, Hui, Uyghur and others,
# about 150 participants), "Afghan (Asian)" (25; Afghan is among the 2024
# examples, not clearly within the 1997 regions), or "Indian" in no Asian
# company ("Indian ancestry", "Indian (American)"). Asian national origins
# alone are read, as the
# vocabulary already read "Chinese" and "Pakistani" and as OMB's definition
# names them; European nationalities alone are not (block comment "European
# labels").
#
# Measured on the baseline modules of the 80,988 trials in the 2026-11-15
# snapshot (CT.gov API v2, re-fetched 2026-11-18), against engine main after
# the European labels: Asian +310,231; other -71,640; unknown -10,364
# (unknown_inferred -10,532, unknown_ethnicity_only +168); Black -92 (the
# "Asian American" rows); more than one race +2 ("White and
# Asian-Non-Japanese", "White, Asian-Non-Japanese", whose Asian part now
# reads). 32 trials now report race, with 228,137 participants, 223,861 of
# them in NCT01146496. Rows read by asian_label or the West Asian template:
# 231 labels, 312,912 participants in 547 trials (west_asian_template 803 in
# 41 trials). Subcategories: south_asian +226,792, south_asian_bangladeshi
# +44,567, chinese +16,081, japanese +5,038, southeast_asian +4,405,
# east_asian +1,720; "Javanese" moves 90 from japanese to
# southeast_asian_indonesian; heritage rows naming "South East Asian" gain
# southeast_asian (254). Race blocks over their population stay 330:
# NCT04227600 leaves; NCT05597787 joins (Malay 28, Chinese 35, Indian 24 and
# Other 1 of 78, as posted). Seven combined classifications now read every
# race row ("Oriental", "chinese han"), so the ethnicity extractor counts
# their race rows as not Hispanic: 3,984 participants from unknown. No sex
# block and no sex/gender-titled gate changes.
_ASIAN_NAMES = (
    # (pattern, subcategory), longest first: the vocabulary's keys, so a name
    # read here lands where the same name alone already lands.
    (r"asian\s+indians?|east\s+indians?", "south_asian_indian"),
    (r"asian\s+americans?|american\s+asians?|asians?", None),
    (r"orientals?|oriential", None),
    (r"chinese|han", "chinese"),
    (r"japanese", "japanese"),
    (r"(?:south\s+)?koreans?", "korean"),
    (r"vietnamese", "vietnamese"),
    (r"filipin(?:o|a)s?", "filipino"),
    (r"pakistanis?", "south_asian_pakistani"),
    (r"bangladeshis?", "south_asian_bangladeshi"),
    (r"sri\s+lankans?|nepal(?:ese|is?)|desis?", "south_asian"),
    (r"thais?", "southeast_asian_thai"),
    (r"cambodians?", "southeast_asian_cambodian"),
    (r"laotians?", "southeast_asian_laotian"),
    (r"malaysians?", "southeast_asian_malaysian"),
    (r"indonesians?|javanese", "southeast_asian_indonesian"),
    (r"taiwanese", "east_asian_taiwanese"),
    (r"burmese|hmong|malays?|singaporeans?", "southeast_asian"),
)
_ASIAN_PLACES = (
    (r"(?:mainland\s+)?china(?:\s+mainland)?", "chinese"),
    (r"japan", "japanese"),
    (r"(?:south\s+)?korea", "korean"),
    (r"india", "south_asian_indian"),
    (r"pakistan", "south_asian_pakistani"),
    (r"bangladesh", "south_asian_bangladeshi"),
    (r"sri\s+lanka|nepal", "south_asian"),
    (r"viet\s*nam", "vietnamese"),
    (r"phil+ip+ines", "filipino"),
    (r"thailand", "southeast_asian_thai"),
    (r"taiwan", "east_asian_taiwanese"),
    (r"malaysia", "southeast_asian_malaysian"),
    (r"indonesia", "southeast_asian_indonesian"),
    (r"cambodia", "southeast_asian_cambodian"),
    (r"laos", "southeast_asian_laotian"),
    (r"myanmar|burma|singapore", "southeast_asian"),
    (r"asia", None),
)
_ASIAN_DIRECTIONS = (
    (r"north\s*east(?:ern)?", "east_asian"),
    (r"south\s*east(?:ern)?", "southeast_asian"),
    (r"east(?:ern)?", "east_asian"),
    (r"south(?:ern)?", "south_asian"),
    (r"central", None),
    (r"west(?:ern)?", None),
)
# The region a subgroup key lies in, for reading two subgroups together.
_ASIAN_REGION = {
    "chinese": "east_asian", "japanese": "east_asian", "korean": "east_asian",
    "east_asian_taiwanese": "east_asian", "vietnamese": "southeast_asian",
    "filipino": "southeast_asian", "south_asian_indian": "south_asian",
    "south_asian_pakistani": "south_asian", "south_asian_bangladeshi": "south_asian",
    "southeast_asian_thai": "southeast_asian", "southeast_asian_cambodian": "southeast_asian",
    "southeast_asian_laotian": "southeast_asian", "southeast_asian_malaysian": "southeast_asian",
    "southeast_asian_indonesian": "southeast_asian",
}
_ASIAN_REGIONS = frozenset({"east_asian", "south_asian", "southeast_asian"})


def _asian_alternation(table: Tuple[Tuple[str, Optional[str]], ...]) -> str:
    return "|".join(pattern for pattern, _subcategory in table)


_ASIAN_TOKEN_RX = re.compile(
    r"(?:(?P<west_indian>west\s+indians?)|"
    r"(?P<subcontinent>(?:the\s+)?(?:indian\s+)?sub\s*continent)|"
    rf"(?P<name>{_asian_alternation(_ASIAN_NAMES)})|"
    r"(?P<indian>indian\s+americans?|indians?)|"
    rf"(?P<place>{_asian_alternation(_ASIAN_PLACES)})|"
    r"(?P<negation>other\s+than|except(?:\s+for)?|excluding|non|not|no)|"
    r"(?P<other>any\s+other|others?)|"
    rf"(?P<direction>{_asian_alternation(_ASIAN_DIRECTIONS)})|"
    r"(?P<example>e\s*g|including|includes|etc)|"
    r"(?P<filler>descent|decent|origins?|ancestry|ancestral|background|ethnicity|ethnic|ethinic|"
    r"race|racial|peoples?|populations?|subjects?|participants?|nationality|only|of|from|in|"
    r"i\s*e|total|number)|"
    r"(?P<join>and/or|or|and))(?![a-z])|(?P<mark>[/,;&():\-–—])",
    re.IGNORECASE)
# Joins that set two groups side by side, as for European labels
# (_EUROPEAN_ALTERNATIVE_JOINS); ":", dashes and brackets describe.
_ASIAN_ALTERNATIVE_JOINS = _EUROPEAN_ALTERNATIVE_JOINS


def _asian_subcategory_of(kind: str, text: str) -> Optional[str]:
    """The subcategory a name, place, direction or Indian-subcontinent token
    names (the tables above); None for one naming the whole category."""
    if kind == "subcontinent":
        return "south_asian"
    if kind == "indian":
        return "south_asian_indian"
    table = {"name": _ASIAN_NAMES, "place": _ASIAN_PLACES, "direction": _ASIAN_DIRECTIONS}.get(kind)
    for pattern, subcategory in table or ():
        if re.fullmatch(pattern, text, re.IGNORECASE):
            return subcategory
    return None


def _asian_tokens(label: str) -> Optional[List[Tuple[str, str, int]]]:
    """(kind, text, bracket depth) of each term of a cleaned label (cleaned as
    for european_label), or None when a term is not one of the rule's."""
    text = unicodedata.normalize("NFKD", clean_demographic_label(label).lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", text)
    text = re.sub(r"[.'’]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    tokens: List[Tuple[str, str, int]] = []
    depth = 0
    position = 0
    while position < len(text):
        match = _ASIAN_TOKEN_RX.match(text, position)
        if match is None or match.end() == position:
            return None
        kind, term = match.lastgroup, match.group(0).lower()
        if kind == "west_indian":
            return None  # the Caribbean, not a direction of Asia
        if kind == "mark" and term == "(":
            tokens.append(("open", term, depth))
            depth += 1
        elif kind == "mark" and term == ")":
            depth -= 1
            if depth < 0:
                return None
            tokens.append(("close", term, depth))
        else:
            tokens.append((kind, re.sub(r"\s+", " ", term), depth))
        position = match.end()
        while position < len(text) and text[position].isspace():
            position += 1
    return tokens


def _asian_reading(label: str) -> Optional[Tuple[str, Optional[str]]]:
    """("asian", subcategory) when a label names Asian groups only (block
    comment above); ("west", None) when it would, but a top-level alternative
    is qualified by West and nothing names the Indian subcontinent (read by
    the table, _west_asian_template_rows); else None."""
    tokens = _asian_tokens(label)
    if not tokens:
        return None
    if tokens[0][0] in ("join", "mark", "close") or tokens[-1][0] in ("join", "mark", "open"):
        return None  # a piece of a longer label
    alternatives = _asian_alternatives(tokens)
    if alternatives is None:
        return None
    has_subcontinent = any(kind == "subcontinent" for kind, _text, _depth in tokens)
    if any(west for _subcategory, west in alternatives) and not has_subcontinent:
        return "west", None
    return "asian", _asian_label_subcategory([subcategory for subcategory, _west in alternatives])


def asian_label(label: str) -> Tuple[bool, Optional[str]]:
    """(True, subcategory) when a label names Asian groups only, by the terms
    of the block comment above; else (False, None).

      "Asian (Japanese)"                         -> (True, "japanese")
      "Asian: East"                              -> (True, "east_asian")
      "East/South East Asian"                    -> (True, None)
      "Han Chinese"                              -> (True, "chinese")
      "Sri Lankan"                               -> (True, "south_asian")
      "Asian (other than Chinese or Japanese)"   -> (True, None)
      "West Asian (Indian sub-continent)"        -> (True, "south_asian")
      "West Asian"                               -> (False, None)
      "Non-Asian"                                -> (False, None)
      "Asian (Comparison 1)"                     -> (False, None)
    """
    reading = _asian_reading(label)
    if reading is None or reading[0] != "asian":
        return False, None
    return True, reading[1]


_ASIAN_NAMING_KINDS = frozenset({"name", "place", "subcontinent", "indian"})
_ASIAN_SEGMENT_KINDS = _ASIAN_NAMING_KINDS | {"direction", "negation", "other", "example",
                                              "open", "close"}


def _asian_alternatives(tokens: List[Tuple[str, str, int]]
                        ) -> Optional[List[Tuple[Optional[str], bool]]]:
    """Read each top-level alternative of a label on its own, as for European
    labels (_european_alternatives): the tokens are split at the joins in
    _ASIAN_ALTERNATIVE_JOINS outside brackets; a bracket describes the
    alternative it is in. Returns, for each alternative naming an Asian
    group, (its subcategory, whether West qualifies it); None when an
    alternative names no Asian group.

    An alternative names a group when it holds a name, or the Indian
    subcontinent, that no negation removes (_asian_segment_terms). "Indian"
    names a group only when the label names another Asian group or place: it
    is also the American Indian ("Indian (American)"). A place names no group;
    it describes one ("Japanese from Japan", "Asian (China Mainland)"). Two
    shapes cross a join: an alternative made only of directions shares the
    noun of a neighbour that names one beside a direction ("East/South East
    Asian", "Asian: Central/South"), and a bare "Asian" before a comma heads
    what follows when the label has two alternatives ("Asian, Bangladeshi",
    "Asian, Non-Japanese")."""
    segments: List[List[Tuple[str, str, int]]] = [[]]
    joins: List[str] = []
    for kind, text, depth in tokens:
        if depth == 0 and kind in ("join", "mark") and text in _ASIAN_ALTERNATIVE_JOINS:
            segments.append([])
            joins.append(text)
        elif kind in _ASIAN_SEGMENT_KINDS:
            segments[-1].append((kind, text, depth))
    if (len(segments) == 2 and joins == [","]
            and [(k, t) for k, t, _d in segments[0]] in ([("name", "asian")], [("name", "asians")])):
        segments = [segments[0] + segments[1]]
    company = any(kind != "direction" for segment in segments
                  for kind, _text in _asian_segment_terms(segment, False)[0])

    def only_directions(segment: List[Tuple[str, str, int]]) -> bool:
        return bool(segment) and all(kind == "direction" for kind, _text, _depth in segment)

    def names_group(segment: List[Tuple[str, str, int]]) -> bool:
        return any(kind in ("name", "subcontinent", "indian")
                   for kind, _text in _asian_segment_terms(segment, company)[0])

    results: List[Tuple[Optional[str], bool]] = []
    for i, segment in enumerate(segments):
        if not segment:
            continue  # ", or" leaves nothing between two joins
        if only_directions(segment):
            following = next((s for s in segments[i + 1:] if s and not only_directions(s)), None)
            previous = next((s for s in reversed(segments[:i]) if s and not only_directions(s)), None)
            if not ((following is not None and following[0][0] == "direction"
                     and names_group(following))
                    or (previous is not None and previous[-1][0] == "direction"
                        and names_group(previous))):
                return None
        elif not names_group(segment):
            return None
        _named, specific = _asian_segment_terms(segment, company)
        west = any(kind == "direction" and text.startswith("west") for kind, text in specific)
        results.append((_asian_segment_subcategory(specific), west))
    return results or None


def _asian_segment_terms(segment: List[Tuple[str, str, int]], company: bool
                         ) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """(named, specific) for one alternative: the (kind, text) of its names,
    places and directions that no negation removes, and those of them not
    given as examples. A negation ("non", "not", "other than", "except",
    "excluding") removes what follows it in its bracket, or in the
    alternative at the top level, brackets opened after it included ("Asian
    (other than Chinese or Japanese)", "Asian Non Chinese"). An example word
    ("e.g.", "including", "etc.") makes every term of its bracket an example
    ("Asian (China, Philippines, Vietnam, Korea, etc.)"): it names members of
    a wider group, not the group; so does "other" in a bracket ("Asian Eastern
    (Chinese, other)"). "Indian" counts only when company is True
    (the label names another Asian group or place)."""
    # The bracket frame of each token: 0 at the top level, a new frame for
    # each "(" (the brackets themselves belong to the frame around them).
    frames: List[int] = []
    parent: Dict[int, Optional[int]] = {0: None}
    stack = [0]
    for kind, _text, _depth in segment:
        if kind == "open":
            frames.append(stack[-1])
            parent[len(parent)] = stack[-1]
            stack.append(len(parent) - 1)
        elif kind == "close":
            if len(stack) > 1:
                stack.pop()
            frames.append(stack[-1])
        else:
            frames.append(stack[-1])
    # "Other" in a bracket widens it as an example word does ("Asian Eastern
    # (Chinese, other)"); at the top level it does not ("Other East Asian").
    example_frames = {frame for (kind, _t, _d), frame in zip(segment, frames)
                      if kind == "example" or (kind == "other" and frame != 0)}

    def within(frame: Optional[int], marked: set) -> bool:
        while frame is not None:
            if frame in marked:
                return True
            frame = parent.get(frame)
        return False

    negated: set = set()
    named: List[Tuple[str, str]] = []
    specific: List[Tuple[str, str]] = []
    for (kind, text, _depth), frame in zip(segment, frames):
        if kind == "negation":
            negated.add(frame)
        elif kind in _ASIAN_NAMING_KINDS or kind == "direction":
            if within(frame, negated) or (kind == "indian" and not company):
                continue
            named.append((kind, text))
            if not within(frame, example_frames):
                specific.append((kind, text))
    return named, specific


def _asian_refines(subcategory: str, other: str) -> bool:
    """True when subcategory is other or a group within it ("chinese" in
    "east_asian")."""
    return subcategory == other or _ASIAN_REGION.get(subcategory) == other


def _asian_region(subcategory: Optional[str]) -> Optional[str]:
    if subcategory in _ASIAN_REGIONS:
        return subcategory
    return _ASIAN_REGION.get(subcategory) if subcategory is not None else None


def _asian_segment_subcategory(specific: List[Tuple[str, str]]) -> Optional[str]:
    """The subcategory of one alternative from the terms that specify it: the
    one term every other refines ("East Asian (Chinese)" -> chinese); else the
    region all of them lie in ("South Asian (India, Pakistan, ...)" with an
    example word, or "Indian, Pakistani" in one bracket -> south_asian); else
    None ("Asian", "East - South East Asian")."""
    # Central Asia has no key of its own, and "Central/South Asian Heritage"
    # has none (block comment "Heritage-template labels"): a Central
    # direction leaves the alternative unqualified ("Asian - Central South
    # Asian").
    if any(kind == "direction" and text == "central" for kind, text in specific):
        return None
    candidates = [sub for sub in (_asian_subcategory_of(kind, text) for kind, text in specific)
                  if sub is not None]
    if not candidates:
        return None
    for candidate in candidates:
        if all(_asian_refines(candidate, other) for other in candidates):
            return candidate
    regions = {_asian_region(candidate) for candidate in candidates}
    return regions.pop() if len(regions) == 1 and None not in regions else None


def _asian_label_subcategory(subcategories: List[Optional[str]]) -> Optional[str]:
    """The subcategory of a label from those of its alternatives: theirs when
    all agree; the region all of them lie in when none is unqualified
    ("Indian or Pakistani" -> south_asian); else None ("Asian or Asian
    American", "East/South East Asian")."""
    if len(set(subcategories)) == 1:
        return subcategories[0]
    if None in subcategories:
        return None
    regions = {_asian_region(subcategory) for subcategory in subcategories}
    return regions.pop() if len(regions) == 1 and None not in regions else None


# ── Black labels ───────────────────────────────────────────────────────────
#
# OMB's Black or African American category is a person having origins in
# any of the Black racial groups of Africa (1997 standard; read from the
# search summaries cited in the block comment "Asian labels"). The
# vocabulary held "Black", "Black or African American", "African
# American", "African", "Caribbean", "West Indian", "Haitian", "Jamaican",
# "Sub-Saharan African" and "Negro". Wordings qualifying or restating them
# matched nothing. They were counted as other, in the 2026-11-15 inputs:
# "Race - Black African" (12,979 participants, 1 trial), "African, black"
# (10,069), "Black African" (6,791 in 35 trials), "Black, African American,
# or African", "African American or Black" (1,151 in 29), "African Descent",
# "Black or African", "Afro-Caribbean". Or they were quarantined as not
# race: the peoples and nationalities "Zulu" (2,953), "Hausa", "Luo",
# "Xhosa", "Yoruba", "Igbo", "Akan", "Kikuyu", "Ghanaian", "Kenyan".
#
# Method (black_label; after the Asian labels and before the fuzzy match).
# A label cleaned as for European labels, with the registry's bracketed
# abbreviations removed ("(Afr) Amer", "(BLA)", "(B or Af Am)", "(US)";
# _BLACK_GLOSS_RX) and "African - American" or "African/American" joined,
# maps to Black, flagged black_label, when it is made only of these terms,
# with at least one Black name:
#   - names, each with the vocabulary's subcategory key (_BLACK_NAMES):
#     Black(s), Negro (no key); African American(s), Afro-American, Black
#     American, African Amer, Afr Amer; Caribbean, Afro-Caribbean (any
#     spelling of Caribbean with one or two r's and b's), African Caribbean,
#     West Indian; Haitian; Jamaican; African(s), Sub-Saharan African,
#     Afro-descendant, the peoples Zulu, Xhosa, Yoruba, Igbo, Hausa, Fulani,
#     Akan, Ashanti, Luo, Kikuyu, Bantu, and the nationalities Kenyan,
#     Ghanaian, Nigerian, Ethiopian, Eritrean, Somali, Rwandan, Ugandan,
#     Zimbabwean (african); "AA" and "Af Am" only beside another name;
#   - beside a name only: places (Africa, South Africa, Kenya, Uganda,
#     Rwanda, Nigeria, Ghana, Ethiopia, Somalia, Zimbabwe, Brazil, Canada);
#     directions (West, East, Central, Southern; North is not one: North
#     Africa is the Middle East and North Africa); "Other"; example words;
#     non-specified, unspecified ("African, Caribbean, or non-specified
#     Black", NCT04588415) and indigenous ("Indigenous African",
#     NCT00080119; beside African peoples only, so "Indigenous Caribbean"
#     is not read); alone as an alternative a non-answer or another race,
#     so "Black or unspecified" and "Black or Indigenous" are not read
#     (review findings);
#   - fillers: descent, origin(s), ancestry, background, ethnic(ity),
#     race, people(s), only, of, from, in, all, tribe(s), group(s),
#     speaking, continent, self-reported, participants, subjects.
# A label beginning or ending with a join is not read; each alternative must
# name a Black group (as for European labels). The subcategory: within an
# alternative, the name every other one refines (Haitian and Jamaican lie
# in Caribbean), else none; Black beside African American is the category's
# own name, not the subgroup ("Black (or African American)"); names given
# as examples name none, in a bracket holding an example word (and the
# brackets within it, not a sibling bracket: "Black (African) (e.g.
# Haitian)" is african) or after one outside brackets ("Black including
# Haitian"; review findings); across
# alternatives, theirs when all agree, else none.
#
# What it does not do. "South African" alone names a country of every race,
# and "Coloured" (550 in 17 trials), "Colored" and "Southern African
# Colored" a group the South African census counts apart: not read. Mixed
# or pooled wordings ("Black or mixed race", "African American or Other",
# "Black, Euro-African, or African American"), negations ("Not Black",
# "Non-Black"), rows crossed with something else ("Black or African
# American (Comparison 1)", "African Mothers", "Black : Children",
# "Black (11 to 18 years)", "Phase 1, Black") and "Hisp"/"NH" qualifiers
# ("Black not Hisp", "NH-Black") are not read. "Tonga" stays withheld (block
# comment "Native Hawaiian or Pacific Islander labels"). UK census wordings
# naming "British" are the UK census rule's.
#
# Measured on the baseline modules of the 80,988 trials in the 2026-11-15
# snapshot (CT.gov API v2, re-fetched 2026-11-18), against engine main after
# the Asian labels: Black +50,378; more than one race +170 (the multirace
# rule now reads the Black part of "Black or African Amer. & White", 148,
# "White and Black African", "White + Black or African"); other -43,602;
# unknown -6,418 (unknown_inferred). Rows read by black_label: 157 labels,
# 70,269 participants in 453 trials, of them 43,432 counted as other before,
# 6,946 quarantined and 19,891 already read as Black (by a fuzzy match or a
# slash fragment, with the same subcategory). Subcategories: african
# +31,848, african_american +70, caribbean +134. Four trials now report race,
# each posting one row naming an African people or nationality: "Luo" 244
# (NCT03178643), "Ghanaian" 162 (NCT03062228), "Kenyan" 60 (NCT04550221),
# "Ugandan" 60 (NCT04030520). No ethnicity block, no sex block and no
# sex/gender-titled gate changes; race blocks over their population stay
# 330.
_BLACK_NAMES = (
    # (pattern, subcategory), longest first: the vocabulary's keys.
    (r"african\s+americans?|afro\s+americans?|black\s+americans?|african\s+amer|afr\s+amer", "african_american"),
    (r"afro\s*car{1,2}ib{1,2}eans?|african\s+car{1,2}ib{1,2}eans?|car{1,2}ib{1,2}eans?|"
     r"west\s+indians?", "caribbean"),
    (r"haitians?", "caribbean_haitian"),
    (r"jamaicans?", "caribbean_jamaican"),
    (r"sub\s*saharan\s+africans?|africans?|afro\s+descendants?|zulus?|xhosas?|yorubas?|igbos?|"
     r"hausas?|fulanis?|akans?|ashantis?|luos?|kikuyus?|bantus?|kenyans?|ghanaians?|nigerians?|"
     r"ethiopians?|eritreans?|somalis?|rwandans?|ugandans?|zimbabweans?", "african"),
    (r"blacks?|negro(?:es)?", None),
)
_BLACK_REGION = {"caribbean_haitian": "caribbean", "caribbean_jamaican": "caribbean"}
_BLACK_TOKEN_RX = re.compile(
    r"(?:(?P<place>south\s+africans?|south\s+africa|africa|kenya|uganda|rwanda|nigeria|ghana|ethiopia|"
    r"somalia|zimbabwe|brazil|canadians?|canada)|"
    rf"(?P<name>{'|'.join(pattern for pattern, _sub in _BLACK_NAMES)})|"
    r"(?P<abbrev>aa|af\s+am)|"
    r"(?P<direction>west(?:ern)?|east(?:ern)?|central|southern)|"
    r"(?P<other>other)|(?P<example>e\s*g|i\s*e|including|etc)|"
    r"(?P<filler>descent|decent|origins?|ancestry|background|ethnicit(?:y|ies)|ethnic|race|racial|"
    r"peoples?|only|of|from|in|all|tribes?|groups?|speaking|continent|self\s+reported|"
    r"participants?|subjects?)|"
    # Describes a name in its alternative ("non-specified Black",
    # "Indigenous African"); alone it names no Black group, a non-answer or
    # another race ("Black or unspecified", "Black or Indigenous": review
    # findings).
    r"(?P<qualifier>non\s+specified|unspecified|indigenous)|"
    r"(?P<join>and/or|or|and))(?![a-z])|(?P<mark>[/,;&():\-–—])",
    re.IGNORECASE)
_BLACK_ALTERNATIVE_JOINS = _EUROPEAN_ALTERNATIVE_JOINS
# The joins that build a heading before its colon ("Race and Ethnicity :",
# "Race/Ethnicity:", "Race, Ethnicity:"), unlike "or", which pools.
_BLACK_HEADING_JOINS = frozenset({"and", "&", ",", "/"})
# The words a heading part is made of: at least one dimension word, and
# otherwise only these ("Ethnic group and race:", "Self-reported race and
# ethnicity:"). "all participants and Race: Black" pools all participants
# (a review finding).
_BLACK_DIMENSION_WORDS = frozenset({"race", "racial", "ethnic", "ethnicity", "ethnicities",
                                    "ancestry", "origin", "origins", "background"})
_BLACK_HEADING_WORDS = _BLACK_DIMENSION_WORDS | {"group", "groups", "self reported"}
# Fillers that widen an "other" choice to any race ("or other race").
_BLACK_RACE_WIDE_FILLERS = frozenset({"race", "racial", "ethnic", "ethnicity", "ethnicities",
                                      "participant", "participants", "subject", "subjects"})
# "African/American" (and "African / American"): the one name split by a
# slash, which the slash fragments would otherwise read as "African". Only
# where "American" ends its fragment: at the end of the label, or before a
# slash, comma, semicolon, "&", ")", "or" or "and". "African/American
# Samoan", "African/American Pacific Islander" or "African/American
# (Black)" go on to name something else, and keep the slash reading (a
# review finding). Not "African/American Indian", which names two races,
# whatever spaces or marks come before "Indian" ("African/American-Indian",
# "African/American [Indian]", "African/American/Indian"; review findings).
_AFRICAN_SLASH_AMERICAN_RX = re.compile(
    r"\bafrican\s*/\s*american(?![^a-z]*indian)(?=\s*(?:$|[/,;&)]|(?:or|and)\b))", re.IGNORECASE)
# A bracketed abbreviation of the label's own words, as registrants post
# them: "Black or African (Afr) Amer", "BLACK (BLA) OR AFRICAN AMERICAN (AFR
# AME)", "(B or Af Am)", "(US)". Only these words: "(Part C)" is a crossing.
_BLACK_GLOSS_WORD = r"(?:afr|ame|amer|bla|b|af|am|aa|us)\.?"
_BLACK_GLOSS_RX = re.compile(
    rf"\(\s*{_BLACK_GLOSS_WORD}(?:\s*(?:\s|/|\bor\b)\s*{_BLACK_GLOSS_WORD})*\s*\)", re.IGNORECASE)


def _black_subcategory_of(text: str) -> Optional[str]:
    for pattern, subcategory in _BLACK_NAMES:
        if re.fullmatch(pattern, text, re.IGNORECASE):
            return subcategory
    return None


def _black_choice_names(tokens: List[Tuple[str, str, Tuple[int, ...]]], position: int) -> List[str]:
    """The names in the choice holding tokens[position]: the run of terms
    in its bracket (or outside brackets), brackets within it included, up to
    the alternative joins at its own level."""
    within = tokens[position][2]

    def inside(i: int) -> bool:
        return tokens[i][2][:len(within)] == within

    def splits(i: int) -> bool:
        kind, term, there = tokens[i]
        return there == within and kind in ("join", "mark") and term in _BLACK_ALTERNATIVE_JOINS

    start = end = position
    while start > 0 and inside(start - 1) and not splits(start - 1):
        start -= 1
    while end + 1 < len(tokens) and inside(end + 1) and not splits(end + 1):
        end += 1
    return [term for kind, term, _w in tokens[start:end + 1] if kind == "name"]


def _black_refined(subs: List[Optional[str]]) -> Optional[str]:
    """The subcategory of names within one alternative or one choice, each
    refining the others ("Caribbean (Haitian)" is caribbean_haitian, "Black
    African" african; None is a name of Black itself): the most specific
    subcategory every other agrees with, else their shared region, else
    None. Black beside African American is the category's own name and
    names no subgroup ("Black of African American")."""
    candidates = [sub for sub in subs if sub is not None]
    if not candidates or (None in subs and "african_american" in candidates):
        return None
    for candidate in candidates:
        if all(candidate == other or _BLACK_REGION.get(candidate) == other for other in candidates):
            return candidate
    regions = {_BLACK_REGION.get(candidate, candidate) for candidate in candidates}
    return regions.pop() if len(regions) == 1 else None


def _black_alternatives(subs: List[Optional[str]]) -> Optional[str]:
    """The subcategory of alternatives side by side ("Haitian or Jamaican"):
    theirs when all agree, None when one names no subgroup ("Haitian or
    Black"), else their shared region ("Caribbean or Haitian" is
    caribbean), else None."""
    if len(set(subs)) == 1:
        return subs[0]
    if None in subs:
        return None
    regions = {_BLACK_REGION.get(sub, sub) for sub in subs}
    return regions.pop() if len(regions) == 1 else None


def black_label(label: str) -> Tuple[bool, Optional[str]]:
    """(True, subcategory) when a label names Black groups only, by the terms
    of the block comment above; else (False, None).

      "Black African"                            -> (True, "african")
      "African American or Black"                -> (True, None)
      "Black, African American, or African"      -> (True, None)
      "Afro-Caribbean"                           -> (True, "caribbean")
      "Zulu-speaking, Black South African"       -> (True, "african")
      "African Ancestry"                         -> (True, "african")
      "South African"                            -> (False, None)
      "Coloured"                                 -> (False, None)
      "Non-Black"                                -> (False, None)
      "Black or mixed race"                      -> (False, None)
    """
    text = unicodedata.normalize("NFKD", clean_demographic_label(label).lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _BLACK_GLOSS_RX.sub(" ", text)  # "(Afr)", "(BLA)", "(B or Af Am)"
    # "African - American", "African/American": the one name split.
    text = re.sub(r"\bafrican\s*(?:[\-–—/])\s*american", "african american", text)
    text = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", text)
    text = re.sub(r"[.'’]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Each term carries the brackets it sits in, outermost first, each
    # bracket by its own number: an example word marks its bracket and the
    # brackets within it, not a sibling bracket at the same depth ("Black
    # (African) (e.g. Haitian)"; a review finding).
    tokens: List[Tuple[str, str, Tuple[int, ...]]] = []
    frames: List[int] = []
    opened = 0
    position = 0
    while position < len(text):
        match = _BLACK_TOKEN_RX.match(text, position)
        if match is None or match.end() == position:
            return False, None
        kind, term = match.lastgroup, re.sub(r"\s+", " ", match.group(0).lower())
        if kind == "mark" and term == "(":
            opened += 1
            frames.append(opened)
            tokens.append(("open", term, tuple(frames)))
        elif kind == "mark" and term == ")":
            if not frames:
                return False, None
            tokens.append(("close", term, tuple(frames)))
            frames.pop()
        else:
            tokens.append((kind, term, tuple(frames)))
        position = match.end()
        while position < len(text) and text[position].isspace():
            position += 1
    if not tokens or (tokens[0][0] in ("join", "mark", "close")) or tokens[-1][0] in ("join", "mark", "open"):
        return False, None  # empty, or a piece of a longer label
    # Each alternative's terms, with their places in `tokens`.
    segments: List[List[Tuple[str, str, Tuple[int, ...], int]]] = [[]]
    # Whether each alternative held any term, fillers included, and whether
    # it holds a heading colon: an alternative of fillers only pools a group
    # the rule cannot name ("Black or participants of all ethnicities"),
    # unless it is part of a heading before a colon ("Race and Ethnicity :
    # Black"); one left empty by two joins (", or") is nothing (review
    # findings).
    held: List[bool] = [False]
    colon: List[bool] = [False]
    fillers: List[List[str]] = [[]]  # each alternative's fillers, for the heading check
    joins: List[str] = []  # the join after each alternative
    for position, (kind, term, within) in enumerate(tokens):
        if not within and kind in ("join", "mark") and term in _BLACK_ALTERNATIVE_JOINS:
            segments.append([])
            held.append(False)
            colon.append(False)
            fillers.append([])
            joins.append(term)
            continue
        held[-1] = True
        if kind == "filler":
            fillers[-1].append(term)
        if not within and kind == "mark" and term == ":":
            colon[-1] = True
        if kind in ("name", "abbrev", "place", "direction", "other", "example", "qualifier",
                    "open", "close"):
            segments[-1].append((kind, term, within, position))
    has_name = any(kind == "name" for kind, _t, _w in tokens)
    # Inside a bracket the joins that split alternatives outside it ("or",
    # "and", "and/or", "/", ",", "&", ";") also set choices side by side,
    # and every choice, before or after the join, must name a Black group:
    # one naming none pools another race or a non-answer ("Black (or
    # Indigenous)", "Black (or unspecified)", "Black (or Other)", "Black
    # (and Other)", "Black (unspecified or Black)", "Black (Haitian /
    # unspecified)", "Black (Haitian, unspecified)"; review findings), while
    # "Black (or African American)" names the category. An example word
    # alone is no choice ("e.g.," before its list, "etc." after it). A bare
    # "other" is the other Black groups only in a bracket whose other
    # choices name a group or give examples ("(Haitian, Jamaican, or
    # other)", "(e.g., Haitian, Jamaican, and other)"); "other race", "other
    # ethnicity" or "other participants" names any race ("Black (Haitian or
    # other race)").
    # A bare "other" choice widens the group the bracket describes: the
    # names in its other choices list some of that group's members, as
    # examples do, and name no subcategory ("Black (Haitian, Jamaican, or
    # other)" is Black with no subgroup; "Black African (Nigerian, Ghanaian,
    # or other)" stays african by the names outside the bracket). A named
    # "other" choice ("other Caribbean", "other Black") is an alternative
    # like the others, reduced with them below: "Black (Haitian or other
    # Caribbean)" is caribbean, "Black (African, Haitian, or other
    # Caribbean)" names no subgroup (review findings).
    listed_positions: Set[int] = set()
    frames = {within[-1] for kind, term, within in tokens
              if within and kind in ("join", "mark") and term in _BLACK_ALTERNATIVE_JOINS}
    depth_of = {within[-1]: len(within) for kind, _t, within in tokens if kind == "open"}
    choice_of: Dict[Tuple[int, int], int] = {}  # (bracket, place in tokens) -> its choice
    for frame in frames:
        choices: List[List[Tuple[str, str, int]]] = [[]]
        for position, (kind, term, within) in enumerate(tokens):
            if frame not in within or (kind in ("open", "close") and within[-1] == frame):
                continue
            if kind in ("join", "mark") and within[-1] == frame and term in _BLACK_ALTERNATIVE_JOINS:
                choices.append([])
            else:
                choices[-1].append((kind, term, position))
                choice_of[(frame, position)] = len(choices) - 1
        named = [any(k == "name" or (k == "abbrev" and has_name) for k, _t, _p in choice)
                 for choice in choices]
        for c, choice in enumerate(choices):
            kinds_here = [k for k, _t, _p in choice]
            if not choice or named[c] or all(k == "example" for k in kinds_here):
                continue
            listed = any(named[d] or any(k == "example" for k, _t, _p in choices[d])
                         for d in range(len(choices)) if d != c) or "example" in kinds_here
            race_wide = any(t in _BLACK_RACE_WIDE_FILLERS for _k, t, _p in choice)
            if ("qualifier" in kinds_here or all(k == "filler" for k in kinds_here)
                    or "other" not in kinds_here or not listed or race_wide):
                return False, None
        if any(any(k == "other" for k, _t, _p in choice) and not any(k == "name" for k, _t, _p in choice)
               for choice in choices):
            listed_positions.update(p for choice in choices for k, _t, p in choice if k == "name")
    subcategories: List[Optional[str]] = []
    for i, segment in enumerate(segments):
        if not segment:
            # A heading runs to its colon through heading joins only ("Race
            # and Ethnicity :", "Race/Ethnicity:"); "or" sets a pooled
            # choice beside it ("all participants or Race: Black"), and a
            # part of a heading names a dimension in heading words only
            # ("all participants and Race: Black" pools; review findings).
            heading = next((j for j in range(i + 1, len(segments)) if colon[j]), None)
            heading_words = (all(word in _BLACK_HEADING_WORDS for word in fillers[i])
                             and any(word in _BLACK_DIMENSION_WORDS for word in fillers[i]))
            if held[i] and (heading is None or not heading_words or any(
                    join not in _BLACK_HEADING_JOINS for join in joins[i:heading])):
                return False, None  # fillers only, not a heading
            continue  # ", or" leaves nothing between two joins
        # A name, or an abbreviation beside a name elsewhere ("Black or Af
        # Am"): "AA" alone names nothing the rule can tell.
        if not any(kind == "name" or (kind == "abbrev" and has_name) for kind, _t, _w, _p in segment):
            return False, None
        # "Indigenous" qualifies African peoples only ("Indigenous African",
        # NCT00080119): beside another Black name it may name a people who
        # are not Black ("Indigenous Caribbean"; a review finding). It
        # qualifies the names in its own choice, not the label's other names
        # ("Black (Indigenous African or Haitian)"; a review finding).
        for kind, term, _w, position in segment:
            if kind == "qualifier" and term == "indigenous":
                qualified = _black_choice_names(tokens, position)
                if not qualified or not all(_black_subcategory_of(name) == "african" for name in qualified):
                    return False, None
        # Names given as examples describe a wider group and name no
        # subcategory: every name in a bracket holding an example word, or in
        # a bracket within it ("(..., Nigerian, Ethiopian, Somali, etc.)",
        # "(including Caribbean (Haitian))"), and every name after an example
        # word outside brackets ("Black including Haitian"; review findings);
        # likewise the names listed beside an "other" choice (above).
        example_frames = {within[-1] for kind, _t, within, _p in segment if kind == "example" and within}
        first_example = next((i for i, (kind, _t, within, _p) in enumerate(segment)
                              if kind == "example" and not within), None)
        # The names that count, each as (place, brackets, subcategory,
        # forced); forced marks a bracket whose choices share no subgroup.
        # A Black given as an example does not count ("African American
        # including Black" is african_american; a review finding).
        items: List[Tuple[int, Tuple[int, ...], Optional[str], bool]] = [
            (position, within, _black_subcategory_of(term), False)
            for i, (kind, term, within, position) in enumerate(segment)
            if kind == "name" and not example_frames & set(within)
            and position not in listed_positions
            and not (first_example is not None and i > first_example)]
        # A bracket's choices are alternatives and are reduced as
        # alternatives are, innermost bracket first, each to one item:
        # "Black (Haitian or Black)" names no subgroup, as "Haitian or
        # Black" does, and "Black (Caribbean or Haitian)" is caribbean, as
        # "Caribbean or Haitian" is (a review finding). Names within one
        # choice, and outside choices, refine each other (_black_refined):
        # "Caribbean (Haitian)", "Black (or African American)".
        for frame in sorted(frames, key=lambda f: -depth_of[f]):
            members = [item for item in items if frame in item[1]]
            if not members:
                continue
            by_choice: Dict[int, List[Tuple[int, Tuple[int, ...], Optional[str], bool]]] = {}
            for item in members:
                by_choice.setdefault(choice_of[(frame, item[0])], []).append(item)
            results = [(None, True) if any(item[3] for item in its) else (_black_refined([item[2] for item in its]), False)
                       for _c, its in sorted(by_choice.items())]
            subs = [sub for sub, _forced in results]
            if any(forced for _sub, forced in results):
                reduced, forced = None, True
            elif len(subs) == 1 or all(sub is None for sub in subs):
                reduced, forced = subs[0], False  # one choice named, or Black in each
            else:
                reduced = _black_alternatives(subs)
                forced = reduced is None
            outer = members[0][1][:members[0][1].index(frame)]
            items = [item for item in items if frame not in item[1]] + [(members[0][0], outer, reduced, forced)]
        subcategories.append(None if any(item[3] for item in items) else _black_refined([item[2] for item in items]))
    if not subcategories:
        return False, None
    return True, _black_alternatives(subcategories)


# ── American Indian or Alaska Native labels ────────────────────────────────
#
# OMB's 2024 revision of SPD 15 (Federal Register document 2024-06469, March
# 29, 2024) defines American Indian or Alaska Native by origins in any of the
# original peoples of North, Central and South America, with Navajo Nation,
# Blackfeet, Aztec and Maya among its examples; the 1997 standard covered the
# original peoples of North and South America (including Central America)
# and also required tribal affiliation or community attachment. (Read from
# the notice's search summary; the notice itself could not be fetched here.)
# The vocabulary held the standard spellings ("American Indian or Alaska
# Native", "Native American", "First Nations", "Indigenous"); other wordings
# of the same category matched nothing and were counted as other: "American
# Indian" alone (6,113 participants in 114 trials of the 2026-11-15 inputs),
# "North American Native" (4,257 in 4), "Alaska Native" (2,031 in 5), "Native
# American or Alaskan Native" (1,211 in 11), "Indigenous Mayan" (706),
# "American Indian (Amer. Ind.) or Alaska Native" (518). Tribal names were
# quarantined as not race, leaving their participants to denominator
# balancing as unknown: NCT00121108 posts "Navajo" (1,725), "San Carlos
# Apache", "Hopi" and "Zuni" (quarantined) beside "White Mountain Apache"
# (other).
#
# Method (aian_label; after the Middle Eastern or North African rule and
# before the heritage, multirace and slash-fragment rules). A cleaned label
# (lower case, "é" as "e", a hyphen between letters as a space, "." and "'"
# removed, a bracketed template abbreviation such as "(Amer)" or "(N)"
# removed) maps to American Indian or Alaska Native, flagged aian_label, when
# it is made only of these terms, joined by "/", ",", ";", "&", "(", ")",
# ":", " - ", "or", "and" or "and/or":
#   - names: American Indian, Am/Amer/Amc Indian, Amer Ind, Alaska(n) Native,
#     Native Alaskan, Native American, North/South/Central American Indian,
#     First Nation(s), Inuit, Metis, Amerindian, Eskimo, and the tribal and
#     national names posted in the inputs or given as the standard's
#     examples (Navajo, Apache, White Mountain Apache, San Carlos Apache, Hopi,
#     Zuni, Yup'ik, Inupiat, Blackfeet, Maya(n), Aztec, Quechua, Aymara,
#     Guarani, Xavante, Kaingang);
#   - "Indigenous", "Aboriginal" or "Aborigine" beside a place of the Americas
#     (the Americas, North/South/Central/Latin America(n), American, Canada,
#     Alaska, Brazil, Mexico, Guatemala, Peru, Bolivia, Ecuador, Chile,
#     Colombia): "Indigenous (Brazil)", "Canadian Aboriginal", "Aboriginal
#     Peoples of North America";
#   - "Native" beside a continent only ("North American Native", "Native
#     South American"): "Native Mexican" may mean born in Mexico;
#   - with a name only, the template's abbreviations ("(Amer. Ind.)", "(AI)",
#     "(AN)", "(N)", "(Al)", "Alaska" alone); with a name or a continent,
#     "North", "South" and "Central" ("North, South or Central American
#     Native");
#   - with a name only, a tribal suffix: Nation(s), Tribe(s), Tribal, Band(s),
#     Member(s), Community ("Navajo Nation", "Blackfeet Tribe"; a review
#     finding), so "Tribal members" alone is not read;
#   - fillers: people(s), person(s), only, e.g., i.e., from, of, non-status,
#     ethnicity, descent, origin(s), ancestry.
# At least one name, or an Indigenous word with a place, must be present.
# A label the vocabulary holds whole keeps its entry (the exact match runs
# first). Labels already read through the fuzzy match ("American
# Indian/Alaskan Native", 98) keep their category and are now read without
# it.
#
# What it does not do. A label naming an Indigenous people outside the
# Americas stays as it was ("Indigenous African", "Aboriginal Australian",
# "Torres Strait Islander", "Maori", "Indigenous swede"). An unqualified
# "Indigenous", "Aboriginal" or "Aborigine" names Indigenous people of the
# trial's own country, which the label does not give: it is read by the
# trial's sites (block comment "Unqualified Indigenous labels, read by the
# trial's sites"). "Native" alone and "Tribal members" stay as they were.
# A label naming another group
# beside these ("Native American or Pacific Islander", "American Indian and
# White", "Other (including American Indian or Alaska Native)") is not read
# by this rule; a combination is read by the multirace rules, which now find
# "American Indian" a race. A row crossing race with another variable
# ("Native American or Alaskan Native - Usual Care", "Adult Native
# American") is not read.
#
# Measured on the baseline modules of the 80,988 trials in the 2026-11-15
# snapshot (CT.gov API v2, re-fetched 2026-11-18), against the comma-list
# rule: American Indian +21,327 (19,515 from other, 1,815 from rows
# quarantined as not race, less 3 now in combinations), more than one race
# +272 (combinations naming American Indian, now readable: "American Indian
# and White" 205 in NCT04839978), other -19,782, unknown -1,815 (all
# unknown_inferred), White -1, Asian -1; 112 labels in 263 trials. A further
# 9,441 participants in 324 trials were American Indian already, through the
# fuzzy match (8,244) or a slash fragment (1,197), and are now read by this
# rule. No block changes reported, no ethnicity or sex block changes, no
# sex/gender-titled measure changes the gate, and the 338 race blocks over
# their population stay 338, none larger. "Aboriginal or American Indian"
# (NCT03954314, 20 participants, sites in Canada and New Zealand among
# others) is read as American Indian: it names American Indian, and
# "Aboriginal" beside a name is taken with it.
_AIAN_NAME = (
    r"american\s+indians?|am(?:er|c)?\s+indians?|amer\s+ind|alaskan?\s*natives?|native\s+alaskans?|"
    r"native\s+americans?|(?:north|south|central)\s+american\s+indians?|first\s+nations?|inuits?|"
    r"metis|amerindians?|eskimos?|(?:white\s+mountain\s+|san\s+carlos\s+)?apaches?|navajos?|hopis?|"
    r"zunis?|yupik|inupiat|blackfeet|mayans?|mayas?|aztecs?|quechuas?|aymaras?|guaranis?|xavante|"
    r"kaingang|kaigang")
_AIAN_CONTINENT = r"(?:north|south|central|latin)\s+americ(?:a|an|ans)|the\s+americas|americas|americ(?:an|ans)"
_AIAN_COUNTRY = (r"canad(?:a|ian|ians)|alaskan?|brazil(?:ian|ians)?|mexic(?:o|an|ans)|guatemal(?:a|an|ans)|"
                 r"peru(?:vian|vians)?|bolivi(?:a|an|ans)|ecuador(?:ian|ians)?|chile(?:an|ans)?|colombi(?:a|an|ans)")
_AIAN_INDIGENOUS = r"indigenous|aboriginals?|aborigines?"
_AIAN_ABBREV = r"amer|am|amc|ind|al|alk|n|nat|ai|an|amin|alaska"
# A tribal suffix ("Navajo Nation", "Blackfeet Tribe"; a review finding).
_AIAN_TRIBE = r"nations?|tribes?|tribal|bands?|members?|communit(?:y|ies)"
_AIAN_FILLER = (r"peoples?|persons?|only|e\s*g|i\s*e|from|of|non\s+status|ethnicity|descent|origins?|"
                r"ancestry")
# A bracketed template abbreviation, removed before the label is read.
_AIAN_GLOSS_RX = re.compile(rf"\s*\(\s*(?:{_AIAN_ABBREV}|alk\s+n|amer\s+ind)\s*\)\s*", re.IGNORECASE)
# Words end at a non-letter; a punctuation join may be followed by anything.
_AIAN_TOKEN_RX = re.compile(
    rf"(?:(?P<name>{_AIAN_NAME})|(?P<indigenous>{_AIAN_INDIGENOUS})|(?P<native>natives?)|"
    rf"(?P<continent>{_AIAN_CONTINENT})|(?P<country>{_AIAN_COUNTRY})|"
    rf"(?P<direction>north|south|central)|(?P<abbrev>{_AIAN_ABBREV})|(?P<tribe>{_AIAN_TRIBE})|"
    rf"(?P<filler>{_AIAN_FILLER})|(?P<join>and/or|or|and))(?![a-z])|(?P<mark>[/,;&():\-–—])",
    re.IGNORECASE)


def aian_label(label: str) -> bool:
    """True when a label names American Indian or Alaska Native peoples
    only, by the terms of the block comment above.

      "American Indian"                                -> True
      "North American Native"                          -> True
      "American (Amer) Indian (Ind) or Alaska Native (N)" -> True
      "Navajo"                                         -> True
      "Indigenous Mayan"                               -> True
      "Aboriginal"                                     -> False (no place)
      "Indigenous African"                             -> False
      "Native American or Pacific Islander"            -> False
    """
    text = clean_demographic_label(label).lower().replace("é", "e")
    text = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", text)
    text = re.sub(r"[.'’]", "", text)
    # A gloss inside a name ("American (Amer) Indian (Ind) or Alaska (Al)
    # Native (N)") would split it: removed, so the name reads whole.
    text = _AIAN_GLOSS_RX.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    kinds = set()
    position = 0
    while position < len(text):
        match = _AIAN_TOKEN_RX.match(text, position)
        if match is None or match.end() == position:
            return False
        kinds.add(match.lastgroup)
        position = match.end()
        while position < len(text) and text[position].isspace():
            position += 1
    named = "name" in kinds
    # "Native" takes a continent only: "Native Mexican" may mean born in Mexico.
    placed = ("indigenous" in kinds and ("continent" in kinds or "country" in kinds)
              or "native" in kinds and "continent" in kinds)
    if not (named or placed):
        return False
    # A template abbreviation or a tribal suffix ("Tribal members") names
    # nothing without a name beside it; a direction ("North, South or
    # Central American Native") needs a name or a continent.
    if ("abbrev" in kinds or "tribe" in kinds) and not named:
        return False
    if "direction" in kinds and not (named or "continent" in kinds):
        return False
    return True

# ── Native Hawaiian or Pacific Islander labels ─────────────────────────────
#
# OMB's 2024 revision of SPD 15 defines Native Hawaiian or Pacific Islander
# by origins in any of the original peoples of Hawaii, Guam, Samoa or other
# Pacific Islands, with Native Hawaiian, Samoan, Chamorro, Tongan, Fijian and
# Marshallese among its examples. The Census Bureau tabulates Maori among
# these groups in the 2020 Census detailed tables. (Both read from search
# summaries; the standard's and the Bureau's pages could not be fetched
# here.) The vocabulary held the standard spellings ("Native Hawaiian or
# Other Pacific Islander", "Native Hawaiian/Pacific Islander", "Pacific
# Islander", "Native Hawaiian", "Hawaiian") and five peoples (Samoan,
# Guamanian, Chamorro, Tongan, Fijian). Other wordings of the same category
# matched nothing and were counted as other: "Hawaiian or Pacific Islander"
# (2,188 participants in 15 trials of the 2026-11-15 inputs, and 5,129 in the
# race parts of NCT02675777), "Hawaiian-Pacific" (1,090), "Other Pacific
# Islander", "South Pacific Islander", "Pacific Peoples". Peoples the
# vocabulary did not hold were quarantined as not race: "Marshallese" (33),
# "Maori" (30 in 11 trials), "Micronesian", "Polynesian", "Tahitian",
# "Guamanian or Chamorro".
#
# Method (nhpi_label; after the American Indian or Alaska Native rule and
# before the heritage, multirace and slash-fragment rules). A cleaned label
# (lower case, accents and ligatures decomposed, so "Māori" reads "maori" and
# "Paciﬁc" "pacific"; a hyphen between letters as a space; "." and "'"
# removed) maps to Native Hawaiian or Pacific Islander, flagged nhpi_label,
# when it is made only of these terms, joined by "/", ",", ";", "&", "(",
# ")", ":", " - ", "or", "and" or "and/or", with at least one name:
#   - names: Hawaiian (also the posted misspellings Hawaiin, Hawiian,
#     Hawain, Hawaian, Haiwaiian), Native Hawaii, Pacific (or Pacif, Pac)
#     Islander(s), Island(s), Isle(s) or Peoples, each optionally after
#     "Other" or "Oth"; the standard's examples (Samoan, Chamorro, Tongan,
#     Fijian, Marshallese) and the peoples posted in the inputs (Guamanian,
#     Polynesian, Micronesian, Tahitian, Maori);
#   - with a name only: "Native" ("Hawaiian Native", "Pacific Island
#     Native"), "Pacific" alone, optionally after "Other" ("Hawaiian-Pacific",
#     "Native Hawaiian or other Pacific", a label cut short), "South" ("South
#     Pacific Islander"), and the template abbreviations PI, OPI, NH, NHPI,
#     NHOPI ("Native Hawaiian (NH) or other Pacific Islander");
#   - fillers: people(s), person(s), only, of, descent, origin(s), ancestry,
#     ethnicity.
# "Other" counts only before "Pacific": "Native Hawaiian or Other" and
# "Native Hawaiian or Some other race(s)" pool other races and stay other.
# A label that begins or ends with a join ("/or Pacific Islander", "Native
# Hawaiian or") is a piece of a longer label and is not read.
# The subcategory is the vocabulary's when every name in the label has the
# same one (Hawaiian: native_hawaiian; Samoan; Tongan; Fijian; Guamanian or
# Chamorro: guamanian_chamorro) and nothing else names Pacific Islanders
# ("Pacific" alone, PI, OPI, NHPI, NHOPI), else none: "Hawaiian or Pacific
# Islander" and "Hawaiian-Pacific" name Pacific Islanders too, so they are
# not counted as Native Hawaiian. A
# label the vocabulary holds whole keeps its entry (the exact match runs
# first).
#
# What it does not do. A label naming another group beside these ("Asian or
# Pacific Islander", the 1977 grouped category; "Native American or Pacific
# Islander"; "Pacific Islander-White"; "Oceania (e.g., Australian, New
# Zealander, Pacific Islander)") is not read by this rule, nor is a row
# crossing race with another variable ("Adolescent Native Hawaiian or Pacific
# Islander", "Pacific Islander (Part C)"). "Islander" alone and "Torres
# Strait Islander" (an Indigenous people of Australia, kept with Aboriginal
# Australian) stay as they were, as does a country name ("Fiji", "Indian
# (Fiji)").
#
# "Asian and Native Hawaiian or Pacific Islander" now has two parts the
# multirace rule reads (block comment "Rows naming two or more races"), so it
# would be read as more than one race. A table posting it beside "American
# Indian or Alaska Native", "Black or African American" and "White", and no
# Asian or Pacific row (NCT01912287, 21 participants), uses it for the 1977
# Asian or Pacific Islander group, the reading "Asian and Pacific Islander"
# already has; tables posting it among combinations ("Asian and White") name
# Asian alone in another row (NCT01192178, NCT00843024, NCT01691859,
# NCT03261167). So a row read as more than one race whose text is "Asian"
# joined by "and", "&" or "+", in either order, to a part this rule reads
# that names the category, Pacific Islanders as a group and nothing besides
# Hawaiian ("Native Hawaiian or Pacific Islander", "Hawaiian or Pacific
# Islander", "Hawaiian-Pacific"; review findings), is counted as other (flags unmapped, asian_pacific_grouped) when no other
# row of its table names Asian, or Native Hawaiian or Pacific Islander,
# apart from the other: a row read as Asian whose text names no Pacific
# group ("Asian", "Japanese/East Asian Heritage"; not "Asian/Pacific
# Islander", which slash fragments read as Asian), or a row read as Native
# Hawaiian or Pacific Islander whose text does not name Asian
# (_grouped_asian_nhpi_rows). Readings of low confidence and unmapped rows
# are not evidence. A part naming a people ("Asian and Samoan", "Asian and
# Maori", "Asian and Native Hawaiian") is a combination, not the grouped
# category, and keeps its reading.
#
# "Tonga" is not read as Tongan. It is the name of a country, and of the
# Tonga of southern Zambia and northern Zimbabwe, a Bantu people; the fuzzy
# match read it as "Tongan" (90.9) in NCT01241318, whose sites are all in
# Zambia (34,653 participants, about a quarter of all Native Hawaiian or
# Pacific Islander participants on the dashboard), and in NCT00705679 (South
# Africa, Uganda, Zimbabwe; 0 participants). A label in _FUZZY_WITHHELD is not
# matched fuzzily (flag fuzzy_match_withheld) and is not quarantined as not
# race: it names a people the vocabulary does not hold, so it is counted as
# other, as "Other African tribe" is.
#
# Measured on the baseline modules of the 80,988 trials in the 2026-11-15
# snapshot (CT.gov API v2, re-fetched 2026-11-18), against engine main after
# the race-by-group rule: Native Hawaiian or Pacific Islander -26,111 net,
# made of -34,653 (Tonga), +8,436 from other and +111 from rows quarantined
# as not race (27 labels, 48 trials), and -5 now read as more than one race
# ("Native Hawaiin or other Pacific Islander and White", NCT02712047, read
# as Native Hawaiian by the fuzzy match before); other +26,216; more than
# one race +6; unknown -91 (unknown_inferred -2,616, unknown_ethnicity_only
# +2,525: in four combined classifications the Pacific row, once counted,
# leaves a shortfall that is exactly the ethnicity rows). Two New Zealand
# trials whose only other rows ("New Zealand European", quarantined as not
# race) left nothing counted are now reported (NCT06428591, NCT06834568).
# The Native Hawaiian subcategory falls by 534 in 104 trials, rows such as
# "Hawaiian/Pacific Islander" that a slash fragment had read as Native
# Hawaiian only. A further 10,075 participants in 467 trials were Native
# Hawaiian or Pacific Islander already, through the fuzzy match (9,521) or
# a slash fragment (554), and are now read by this rule. In the four
# combined classifications the ethnicity extractor counts the race rows as
# not Hispanic (5,528 participants from unknown). No sex block changes and
# no sex/gender-titled measure changes the gate.
_NHPI_HAWAIIAN = r"hawaiians?|hawaiins?|hawiians?|hawains?|hawaians?|haiwaiians?"
_NHPI_PACIFIC = r"(?:(?:other|oth)\s+)?(?:pacific|pacif|pac)"
_NHPI_PEOPLE = (r"samoans?|tongans?|fijians?|marshallese|chamorros?|guamanians?|polynesians?|"
                r"micronesians?|tahitians?|maoris?")
_NHPI_NAME = (rf"{_NHPI_PACIFIC}\s+(?:islanders?|islands?|isles?|peoples?)|native\s+hawaii|"
              rf"{_NHPI_HAWAIIAN}|{_NHPI_PEOPLE}")
_NHPI_FILLER = r"peoples?|persons?|only|of|descent|origins?|ancestry|ethnicity"
# Words end at a non-letter; a punctuation join may be followed by anything.
_NHPI_TOKEN_RX = re.compile(
    rf"(?:(?P<name>{_NHPI_NAME})|(?P<native>natives?)|(?P<pacific>{_NHPI_PACIFIC})|"
    rf"(?P<direction>south)|(?P<abbrev>pi|opi|nh|nhpi|nhopi)|(?P<filler>{_NHPI_FILLER})|"
    rf"(?P<join>and/or|or|and))(?![a-z])|(?P<mark>[/,;&():\-–—])",
    re.IGNORECASE)
# The vocabulary's subcategory of each name that has one (block comment above).
_NHPI_NAME_SUBCATEGORY = (
    (re.compile(rf"(?:native\s+hawaii|{_NHPI_HAWAIIAN})$"), "native_hawaiian"),
    (re.compile(r"samoans?$"), "samoan"),
    (re.compile(r"tongans?$"), "tongan"),
    (re.compile(r"fijians?$"), "fijian"),
    (re.compile(r"(?:chamorros?|guamanians?)$"), "guamanian_chamorro"),
)
# Labels whose closest vocabulary key names a different people (block comment
# above): not matched fuzzily, and not quarantined as not race.
_FUZZY_WITHHELD = frozenset({"tonga"})


def _nhpi_name_subcategory(name: str) -> Optional[str]:
    """The vocabulary's subcategory of one name read by nhpi_label, or None."""
    for rx, subcategory in _NHPI_NAME_SUBCATEGORY:
        if rx.match(name):
            return subcategory
    return None


def _nhpi_terms(label: str) -> Optional[Tuple[List[str], bool]]:
    """The names a label is read through by nhpi_label (lower case), and
    whether "Pacific" alone or a Pacific abbreviation (PI, OPI, NHPI, NHOPI)
    is among its terms; None when the label is not one nhpi_label reads."""
    text = unicodedata.normalize("NFKD", clean_demographic_label(label).lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", text)
    text = re.sub(r"[.'’]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    names: List[str] = []
    # "Pacific" alone and the abbreviations PI, OPI, NHPI, NHOPI name Pacific
    # Islanders too, so the label has no single subcategory.
    names_pacific = False
    kinds: List[str] = []
    position = 0
    while position < len(text):
        match = _NHPI_TOKEN_RX.match(text, position)
        if match is None or match.end() == position:
            return None
        kinds.append(match.lastgroup if match.group("mark") not in ("(", ")") else "bracket")
        if match.lastgroup == "name":
            names.append(re.sub(r"\s+", " ", match.group("name")))
        elif match.lastgroup == "pacific" or (match.lastgroup == "abbrev"
                                               and match.group("abbrev").lower() != "nh"):
            names_pacific = True
        position = match.end()
        while position < len(text) and text[position].isspace():
            position += 1
    # Every other term ("Native", "Pacific", "South", an abbreviation, a
    # filler) names nothing without a name beside it.
    if not names:
        return None
    # A label beginning or ending with a join is a piece of a longer label:
    # the multirace rule splits "Asian and/or Pacific Islander" at "and" and
    # reads "/or Pacific Islander" through map_race_category.
    if kinds[0] in ("join", "mark") or kinds[-1] in ("join", "mark"):
        return None
    return names, names_pacific


def nhpi_label(label: str) -> Tuple[bool, Optional[str]]:
    """(True, subcategory) when a label names Native Hawaiian or Pacific
    Islander peoples only, by the terms of the block comment above; else
    (False, None). The subcategory is set only when every name agrees.

      "Hawaiian or Pacific Islander"                    -> (True, None)
      "Hawaiian-Pacific"                                -> (True, None)
      "Native Hawaiians"                                -> (True, "native_hawaiian")
      "Guamanian or Chamorro"                           -> (True, "guamanian_chamorro")
      "Maori"                                           -> (True, None)
      "Native Hawaiian or Other"                        -> (False, None)
      "Asian or Pacific Islander"                       -> (False, None)
      "Torres Strait Islander"                          -> (False, None)
      "Tonga"                                           -> (False, None)
    """
    terms = _nhpi_terms(label)
    if terms is None:
        return False, None
    names, names_pacific = terms
    subcategories = {_nhpi_name_subcategory(name) for name in names}
    if names_pacific:
        subcategories.add(None)
    return True, (subcategories.pop() if len(subcategories) == 1 else None)


# The category's own words: Pacific Islanders as a group, and Hawaiian.
_NHPI_GROUP_NAME_RX = re.compile(rf"{_NHPI_PACIFIC}\s+(?:islanders?|islands?|isles?|peoples?)$")
_NHPI_HAWAIIAN_NAME_RX = re.compile(rf"(?:native\s+hawaii|{_NHPI_HAWAIIAN})$")


def _nhpi_names_the_category(label: str) -> bool:
    """True when nhpi_label reads the label and it names the category, not a
    people: it names Pacific Islanders as a group ("Pacific Islander",
    "Pacific" alone, PI, OPI, NHPI, NHOPI) and no name besides those and
    Hawaiian ("Native Hawaiian or Pacific Islander", "Hawaiian-Pacific"; not
    "Samoan", "Maori" or "Native Hawaiian" alone). Used by
    _asian_joined_to_nhpi (block comment above; a review finding)."""
    terms = _nhpi_terms(label)
    if terms is None:
        return False
    names, names_pacific = terms
    if any(_NHPI_GROUP_NAME_RX.match(name) is None and _NHPI_HAWAIIAN_NAME_RX.match(name) is None
           for name in names):
        return False
    return names_pacific or any(_NHPI_GROUP_NAME_RX.match(name) is not None for name in names)

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
#     maps to nothing is allowed only when the rest as a whole matches the
#     vocabulary exactly (any letter case), not fuzzily and not through one
#     of its slash fragments: "Af Amer/White Heritage" is not read as White,
#     nor "American Indian or Alaska Native-NA Heritage" as American Indian. With or
#     without a qualifier, a group that maps to nothing and reads as a
#     non-answer ("Refused", "Prefer not to say", a lone letter of "N/A")
#     fails the rule: "White - Refused Heritage" is not White. A compact one
#     ("NA", "NR", "Unk") fails it when no other group maps ("White - NA
#     Heritage", "White - Caucasian-NA Heritage"); "NA" is also the
#     template's North African ("White-Arabic/NA H"), read beside groups that
#     map. A direction
#     word left by a shared ending ("East" of "East/South East Asian") is
#     allowed; a group mapping to other or unknown ("Mixed Heritage or
#     Other") fails the rule. The
#     subcategory is the one subcategory every mapped group shares, else none.
# A label naming a negation ("Non-European heritage") is not read, nor one
# with a parenthesised non-answer ("White - (N/A) Heritage"). A heritage label
# holding a non-answer is not fuzzily matched by the vocabulary step before
# this rule either ("American Indian or Alaska Native-NA H"); without a
# heritage word such a label keeps that fuzzy match, as before. The row is
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
    r"none|not)(?![a-z])", re.IGNORECASE)
_HERITAGE_LONE_LETTER_RX = re.compile(r"[a-z]", re.IGNORECASE)
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
    "american indian": "american_indian_alaska_native",
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


def _heritage_holds_non_answer(text: str) -> bool:
    """True when the text holds a non-answer: a word of _HERITAGE_NON_ANSWER_RX,
    or a compact one ("NA", "N/A", "Unk") as a word or as the whole text with
    its punctuation removed."""
    if _HERITAGE_NON_ANSWER_RX.search(text) is not None:
        return True
    words = [w for w in re.split(r"[\s.\-–—/()]+", text.lower()) if w]
    return (any(w in _HERITAGE_COMPACT_NON_ANSWERS for w in words)
            or re.sub(r"[^a-z]", "", text.lower()) in _HERITAGE_COMPACT_NON_ANSWERS)


def _heritage_text(label: str) -> Optional[str]:
    """The label without glosses, heritage words, "of" and a final "n (%)",
    or None when it holds no heritage word or names a negation (block comment
    "Heritage-template labels")."""
    text = clean_demographic_label(label)
    if _HERITAGE_WORD_RX.search(text) is None or _negates_race(text):
        return None
    text = _HERITAGE_COUNT_SUFFIX_RX.sub("", text)
    # A gloss is removed as an abbreviation ("(Af Am)"), but a parenthesised
    # non-answer ("(N/A)", "(Unk)") is not one: the label is not read (a
    # review finding).
    if any(_heritage_holds_non_answer(gloss.group(0)) for gloss in _HERITAGE_GLOSS_RX.finditer(text)):
        return None
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
    mapping = map_race_category(group, heritage=False, multirace=False)
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
        # A non-answer in any group fails the rule before the group is
        # mapped, so a fuzzy match cannot absorb it: "Refused", or a compact
        # one attached to other words ("Caucasian-NA", "Alaska Native-NA").
        words = [w for w in re.split(r"[\s.\-–—]+", group.lower()) if w]
        if (_HERITAGE_NON_ANSWER_RX.search(group) is not None
                or (len(words) > 1 and any(w in _HERITAGE_COMPACT_NON_ANSWERS for w in words))):
            return None
        reading = _heritage_group(group)
        if reading is None:
            # A group that maps to nothing may be a subgroup name the
            # vocabulary lacks ("Caucasian European" after "White -"), but not
            # a lone letter ("N" and "A" of "N/A"). A compact non-answer
            # standing alone ("NA") is noted: also the template's North
            # African, it is read only beside groups that map.
            if strict or _HERITAGE_LONE_LETTER_RX.fullmatch(group) is not None:
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
        # a whole must match the vocabulary exactly ("American Indian/Alaska
        # native", in any letter case), never by a fuzzy match, which would
        # absorb an unread suffix ("American Indian or Alaska Native-NA"),
        # nor through one of its slash fragments, which would drop the
        # unmapped group ("Af Amer/White"; review findings).
        whole = _heritage_group(rest, atomic=True) if unmapped else None
        if unmapped and (whole is None or whole[0] not in _RACE_SPECIFIC or whole[2] != "high"):
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

# ── Rows naming two or more races ──────────────────────────────────────────
#
# Customized race tables often break participants who report more than one
# race into the combinations they reported, one row per combination:
# "American Indian or Alaska Native & White", "Asian and White", "Black or
# African American and White", "Multiple-Asian and White", "Mixed (Black/
# White)", "Multiracial (Asian, White)". Without a heritage word (block comment
# "Heritage-template labels") these were counted as other, or as their first
# race through a slash fragment or a fuzzy match: "American Indian or Alaska
# Native & White" as American Indian, "Black or African American & White" as
# Black, "Mixed: Black/Indian" as Asian.
#
# Method (_multirace_label, after the heritage combinations and before the
# slash fragments). Only readings that are not fuzzy count: an exact or
# case-insensitive vocabulary match, its slash fragments, or the Middle
# Eastern or North African rule, each naming a race (not other or unknown).
#   - A label starting with "Multiple", "Multiracial", "Mixed", "Biracial",
#     "More than one race" or "Two or more races" names more than one race
#     when the rest names two or more different races. The rest is split at
#     ",", ";", "&", "+" and "and", and each part also at "/"; parts read as
#     nothing are passed over, since the prefix already says more than one
#     race and the two races named confirm the row is about race ("Multiple
#     - child" names none). "Mixed Asian" names one race: not read.
#   - Without such a prefix, a label joining parts by "&", "+" or "and" (and
#     then also split at "," and ";": "Caucasian, Black, and Asian") names
#     more than one race when every part reads as one race, passing over a
#     part that is only an ethnicity term ("Hispanic"), and the parts name
#     two or more. "Black and African American" names one; "American Indian
#     and Alaska Native" and "White and Other" have a part that is not a race;
#     "White and Hispanic" names one race; none is read. "Asian and Pacific
#     Islander" (and "&", "Other") is the grouped category of the 1977
#     standard, one group: not read.
# A category name holding "and" ("Native Hawaiian and Pacific Islander",
# "American Indian and Alaska Native") is read as one part, so its "and"
# joins no list: "Asian, Indigenous, Native Hawaiian and Pacific Islander,
# non-Hispanic" (NCT03360539) is a grouped category, not a combination. The
# row maps to more than one race, flagged multirace_label.
#
# What it does not do. Parts joined only by "," or ";" without a prefix are
# not read by this rule: a comma joins the names of one grouped category as
# often as it joins a combination ("Asian, Pacific Islander" beside "American
# Indian, Alaskan Native" in NCT00851799, but "White, Black or African
# American" in NCT03492281), and only the rest of the table tells the two
# apart (block comment "Comma lists naming two or more races"). Two races
# joined by "/" or "or" without a prefix are not read either: the row may
# name either race.
#
# These rows add to the table's population, not to a "More than one race" row
# beside them: of the tables holding them in the 2026-11-15 inputs, those with
# such a row add up exactly to their population (NCT01641809 243, NCT00609674
# 315), and the rows sit in blocks over their population for 2 participants.
#
# Measured on the baseline modules of the 80,988 trials in the 2026-11-15
# snapshot (CT.gov API v2, re-fetched 2026-11-18), against engine main: rows
# flagged multirace_label in 129 trials, 99 labels; more than one race +829,
# from American Indian 520 ("American Indian or Alaska Native & White" 413 in
# 16 trials), other 260, Black 22, Native Hawaiian 14, Asian 8, White 5.
# mena_in_white falls by 2 ("Black & Middle Eastern/North African" and "Asian
# & Middle Eastern/North African", White through a slash fragment before). No
# block changes reported, no ethnicity block changes, and the 338 race blocks
# over their population stay 338, none larger. Comma lists are measured in
# block comment "Comma lists naming two or more races".
_MULTIRACE_PREFIX_RX = re.compile(
    r"^(?:multiple(?:\s+races?)?|multi[\s\-]?racial|mixed(?:\s+races?)?|bi[\s\-]?racial|"
    r"more\s+than\s+one\s+races?|two\s+or\s+more\s+races)\s*(?:[\-:–—(]\s*|\s+(?=\S))",
    re.IGNORECASE)
_MULTIRACE_PART_SPLIT_RX = re.compile(r"\s*(?:,|;|&|\+|\band\b)\s*", re.IGNORECASE)
_MULTIRACE_JOIN_SPLIT_RX = re.compile(r"\s*(?:&|\+|\band\b)\s*", re.IGNORECASE)
# Category names holding "and", read as one part: their "and" joins no list.
_MULTIRACE_ONE_CATEGORY = (
    (re.compile(r"native\s+hawaiians?\s+and\s+(?:other\s+)?pacific\s+islanders?", re.IGNORECASE),
     "Native Hawaiian or Other Pacific Islander"),
    (re.compile(r"american\s+indians?\s+and\s+alaskan?\s+natives?", re.IGNORECASE),
     "American Indian or Alaska Native"),
)


def _named_race(text: str) -> Optional[str]:
    """The race a text names through a reading that is not fuzzy (exact or
    case-insensitive vocabulary match, slash fragment, or the Middle Eastern
    or North African rule), else None; never other or unknown."""
    text = text.strip(" -–—:,;()/.")
    if not text:
        return None
    mapping = map_race_category(text, heritage=False, multirace=False)
    if (mapping["confidence"] != "high" or "unmapped" in mapping["flags"]
            or mapping["omb_category"] not in _RACE_SPECIFIC):
        return None
    return mapping["omb_category"]


def _multirace_label(label: str) -> bool:
    """True when a label without a heritage word names two or more races
    (block comment "Rows naming two or more races")."""
    text = clean_demographic_label(label)
    for name_rx, name in _MULTIRACE_ONE_CATEGORY:
        text = name_rx.sub(name, text)
    prefix = _MULTIRACE_PREFIX_RX.match(text)
    if prefix is not None:
        races = set()
        for part in _MULTIRACE_PART_SPLIT_RX.split(text[prefix.end():].rstrip(") ")):
            for piece in [part] + part.split("/"):
                race = _named_race(piece)
                if race is not None and race != "more_than_one_race":
                    races.add(race)
        return len(races) >= 2
    if _HERITAGE_GROUPED_API_RX.fullmatch(text) is not None:
        return False
    if len(_MULTIRACE_JOIN_SPLIT_RX.split(text)) < 2:
        return False
    # A list with a joiner is split at its commas too ("Caucasian, Black,
    # and Asian"); a part that is only an ethnicity term is passed over
    # ("African-American/Black, Hispanic, and Caucasian/White").
    races = set()
    for part in _MULTIRACE_PART_SPLIT_RX.split(text):
        if not part.strip(" -–—:,;()/.") or _is_ethnicity_only_label(part):
            continue
        race = _named_race(part)
        if race is None:
            return False
        races.add(race)
    return len(races) >= 2

def _slash_fragment_mapping(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """The (category, subcategory) of the first "/" fragment of `text` that
    is a RACE_MAPPINGS key, matched exactly, ignoring case; None when the
    text holds no slash or no fragment matches."""
    if "/" not in text:
        return None
    for frag in text.split("/"):
        frag = frag.strip()
        if not frag:
            continue
        for key, (omb, subcat) in RACE_MAPPINGS.items():
            if key.lower() == frag.lower():
                return omb, subcat
    return None


def map_race_category(label: str, fuzzy_threshold: int = 85, *,
                      guard_negation: bool = False, heritage: bool = True,
                      multirace: bool = True) -> Dict:
    """
    Map a race category label to NIH/OMB standard.

    guard_negation keeps a fuzzy match only when the label and the matched
    key agree on negation (_negates_race); _map_plain_row sets it for a label
    whose race dimension name was removed (block comment "Race dimension
    named in the row label").

    heritage=False leaves out the heritage-template rule (block comment
    "Heritage-template labels"), which maps the groups of a heritage label
    through this function. multirace=False leaves out the rule for rows
    naming two or more races (block comment "Rows naming two or more races"),
    which reads the parts of a label through this function.

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

    # A label naming American Indian or Alaska Native peoples only (block
    # comment "American Indian or Alaska Native labels"), before the slash
    # fragments and the fuzzy match.
    if aian_label(label_clean):
        flags.append("aian_label")
        return {
            "omb_category": "american_indian_alaska_native",
            "subcategory": None,
            "confidence": "high",
            "original": label_clean,
            "flags": flags
        }

    # A label naming Native Hawaiian or Pacific Islander peoples only (block
    # comment "Native Hawaiian or Pacific Islander labels"), before the slash
    # fragments and the fuzzy match.
    is_nhpi, nhpi_subcategory = nhpi_label(label_clean)
    if is_nhpi:
        flags.append("nhpi_label")
        return {
            "omb_category": "native_hawaiian_pacific_islander",
            "subcategory": nhpi_subcategory,
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

    # A label naming two or more races without a heritage word (block
    # comment "Rows naming two or more races"), before the slash fragments,
    # which would keep the first race of "Black/African American and White".
    if (multirace and _HERITAGE_WORD_RX.search(label_clean) is None
            and _multirace_label(label_clean)):
        flags.append("multirace_label")
        return {
            "omb_category": "more_than_one_race",
            "subcategory": None,
            "confidence": "high",
            "original": label_clean,
            "flags": flags
        }

    # Slash-compound normalization: try each "/" fragment individually
    # so "Caucasian/White" resolves even without a dedicated mapping entry.
    # A slash inside "African/American" splits one name, not two, so it is
    # rejoined first and the other slashes are read as before: "African/
    # American" alone has no slash left and the Black labels read it
    # (african_american, not african), and "White/African/American" keeps
    # its "White" fragment (block comment "Black labels"; review findings).
    # When the rejoined label gives no reading, neither from a fragment nor
    # by the Black labels, its own slashes are read as before the rejoin:
    # "African/American or Indian" stays Black/african through "African"
    # rather than going unread (a review finding). The rejoin can then
    # change a label's reading but never take one away.
    slash_text = _AFRICAN_SLASH_AMERICAN_RX.sub("African American", label_clean)
    slash_reading = _slash_fragment_mapping(slash_text)
    if slash_reading is None and slash_text != label_clean and not black_label(label_clean)[0]:
        slash_reading = _slash_fragment_mapping(label_clean)
    if slash_reading is not None:
        flags.append("slash_normalized")
        return {
            "omb_category": slash_reading[0],
            "subcategory": slash_reading[1],
            "confidence": "high",
            "original": label_clean,
            "flags": flags
        }

    # A label naming European origin only (block comment "European labels"),
    # after the slash fragments, so a label they read as White keeps that
    # reading, and before the fuzzy match.
    is_european, european_subcategory = european_label(label_clean)
    if is_european:
        flags.append("european_label")
        return {
            "omb_category": "white",
            "subcategory": european_subcategory,
            "confidence": "high",
            "original": label_clean,
            "flags": flags
        }

    # A label naming Asian groups only (block comment "Asian labels"), after
    # the slash fragments, so a label they read as Asian keeps that reading,
    # and before the fuzzy match.
    is_asian, asian_subcategory = asian_label(label_clean)
    if is_asian:
        flags.append("asian_label")
        return {
            "omb_category": "asian",
            "subcategory": asian_subcategory,
            "confidence": "high",
            "original": label_clean,
            "flags": flags
        }

    # A label naming Black groups only (block comment "Black labels"), after
    # the Asian labels and before the fuzzy match.
    is_black, black_subcategory = black_label(label_clean)
    if is_black:
        flags.append("black_label")
        return {
            "omb_category": "black_african_american",
            "subcategory": black_subcategory,
            "confidence": "high",
            "original": label_clean,
            "flags": flags
        }

    # Fuzzy match. A heritage-template label holding a non-answer is not
    # matched fuzzily: "American Indian or Alaska Native-NA H" scores 93
    # against "American Indian or Alaska Native" (block comment
    # "Heritage-template labels"; a review finding). A label in
    # _FUZZY_WITHHELD ("Tonga") is not matched fuzzily (block comment "Native
    # Hawaiian or Pacific Islander labels").
    withheld = label_clean.lower() in _FUZZY_WITHHELD
    if withheld:
        flags.append("fuzzy_match_withheld")
    match = (None if withheld
             else process.extractOne(label_clean, RACE_MAPPINGS.keys(), scorer=fuzz.ratio))
    if (match and match[1] >= fuzzy_threshold
            and not (guard_negation and _negates_race(label_clean) != _negates_race(match[0]))
            and not (heritage and _HERITAGE_WORD_RX.search(label_clean) is not None
                     and _heritage_holds_non_answer(_HERITAGE_WORD_RX.sub(" ", label_clean)))):
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
    # A label withheld from the fuzzy match names a people the vocabulary
    # does not hold ("Tonga"; block comment "Native Hawaiian or Pacific
    # Islander labels"): counted as other, not quarantined.
    if label_lower in _FUZZY_WITHHELD:
        return False
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

    rows: List[Tuple[str, int]] = [
        (label, sum_measurements(measurements, overall_group_id, is_pct=is_pct, denom=denom))
        for label, measurements in _labelled_measurements(measure)]
    return rows, bool(is_pct and denom), total_only


def _labelled_measurements(measure: dict) -> List[Tuple[str, list]]:
    """Each labelled row of a measure as (label, measurements), in source
    order: the rows _measure_rows counts, before a count is read from them.
    _race_by_group_splits reads the same rows' measurements column by column,
    so both go through this one walk."""
    rows: List[Tuple[str, list]] = []
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
                rows.append((label, cat.get("measurements", [])))
        else:
            # Fallback: class itself carries measurements with no categories
            label = cls.get("title", "").strip()
            if not label:
                continue
            rows.append((label, cls.get("measurements", [])))
    return rows

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
# utils.COMBINED_DIMENSION) is removed before the row is routed and mapped, so the rest goes through
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
_RACE_DIMENSION_PREFIX_RX = re.compile(
    r"^(?:" + COMBINED_DIMENSION + r"|race)\s*[:\-–—_](?:\s*[:\-–—_])*\s*(?=\S)", re.IGNORECASE)
# An ethnicity name joined to a following race name by ",", a dash or no
# joiner as well as the joiners of utils.DIMENSION_JOIN ("Ethnicity, Race:
# White", "Ethnicity Race Unknown"): such a label names race too, so the rule
# of "Ethnicity dimension named in the row label" does not leave it out, and
# the ethnicity name is removed here so that the race name after it is read
# by the rule above.
_ETHNICITY_BEFORE_RACE_RX = re.compile(
    r"^\s*ethnic(?:ity|\s+group)\s*(?:" + DIMENSION_JOIN + r"|[,\-–—])?\s*(?=rac(?:e|es|ial)(?![a-z]))",
    re.IGNORECASE)


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
      "Ethnicity, Race: White"                  -> "White"
      "Ethnicity Race Unknown"                  -> "Race Unknown"
      "Race unknown"                            -> None
      "Ethnicity: Hispanic"                     -> None
    """
    text = clean_demographic_label(label)
    lead = _ETHNICITY_BEFORE_RACE_RX.match(text)
    if lead is not None and _RACE_DIMENSION_PREFIX_RX.match(text) is None:
        # The ethnicity name before a race name (comment above): what follows
        # is read as a label of its own, with its race name removed when a
        # separator follows it ("Race: White" -> "White"), else as it stands
        # ("Race Unknown").
        rest = text[lead.end():].strip()
        prefix = _RACE_DIMENSION_PREFIX_RX.match(rest)
        rest = rest[prefix.end():].strip() if prefix is not None else rest
        return rest or None
    prefix = _RACE_DIMENSION_PREFIX_RX.match(text)
    if prefix is None:
        return None
    return text[prefix.end():].strip() or None


def _map_plain_row(label: str, count: int, pct_converted: bool,
                   total_only: bool = False, reading: Optional[Dict] = None,
                   guard_negation: bool = False) -> Optional[Dict]:
    """Map one row the way every plain race row is mapped.

    Returns None for a purely ethnicity label (e.g. "Hispanic or Latino"), which
    is left to the ethnicity pipeline. Otherwise the mapping of
    _map_race_label (direct, slash-fragment, fuzzy, then race/ethnicity split)
    with the row's count. A race-by-sex row's race part goes through exactly
    this function, so it lands where the same text would land on its own row.
    A leading race dimension name is removed first (block comment "Race
    dimension named in the row label"). guard_negation applies that
    comment's negation check to the fuzzy match even when no race name is
    removed: a caller passes it for text whose ethnicity dimension name it
    removed (block comment "Ethnicity dimension named in the row label").

    reading, when given, is used in place of _map_race_label's mapping (a
    copy is taken): the comma-list rule, which needs the rest of the table,
    passes its reading here so the row gets every row-level step as any
    other row does (block comment "Comma lists naming two or more races").
    """
    stripped = _strip_race_dimension(label)
    text = stripped if stripped is not None else label
    if _is_ethnicity_only_label(text):
        return None
    if reading is not None:
        mapping = {**reading, "flags": list(reading["flags"])}
    else:
        mapping = _map_race_label(text, guard_negation=guard_negation or stripped is not None)
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
    return [mapping for mapping, _skipped in
            _map_table_rows(rows, pct_converted, total_only, whole_table)
            if mapping is not None]

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
#                    and "sex" beside it as on a counted race-by-sex row (for
#                    a race-by-group row, "race_part" and "group"),
#    "count":        the row's count, read exactly as a counted row's is,
#    "source_table": the title of the table the row was posted in,
#    "flags":        "pct_to_count" when the count was converted from a
#                    percentage, "race_by_sex" for a race-by-sex row,
#                    "race_by_group" for a race-by-group row,
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
# followed by the word race, races or racial, either directly ("Ethnicity Race
# Unknown") or after "/", "&", "and", "or", "and/or", "," or a dash (a
# combined name, as "Race/Ethnicity", whose rows belong to the one
# classification; its prefix is removed by the race dimension rule above, so
# "Ethnicity or Race: White" is read as White; the forms with ",", a dash or
# no joiner are removed there too, by _ETHNICITY_BEFORE_RACE_RX, so
# "Ethnicity, Race: White" is White and "Ethnicity Race Unknown" unknown).
# Then, by table:
#   - some row names it, no other row is a race row of the table's own
#     (_is_own_race_row: a row in a race-specific category without
#     "unmapped", or one named for the race dimension, "Race - Undisclosed",
#     "Race,Asian", a bare "Race"), and at least one named row, without the
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
#     comment "Race dimension named in the row label"), including its
#     negation check: "Ethnicity: Non-African American" is unmapped other,
#     not Black, though "Non-African American" alone scores 88.9 against
#     "African American". A row not naming the dimension beside them, a bare
#     "Unknown" or "Other", is read as posted: it is that classification's
#     own residual, not a race classification, and taking it for one would
#     leave every named race row out and balance the cohort into unknown
#     ("Ethnicity: White" 50 / "Ethnicity: Black" 30 / "Unknown" 5 read as
#     unknown 85; a review finding). A row named for race does count as one,
#     mapped or not: NCT01256177 posts "Race,Asian" 279 beside "Ethnicity,
#     Chinese" 279, the same 279 classified twice. In the 2026-11-15 inputs
#     no table has a residual row beside named race rows, a negated named
#     row or a "," / dash / bare joiner before race, so these refinements
#     change no count there; one block changes in its flags only
#     (NCT00552448: "Ethnicity/Race Unknown" 2, still unknown, now flagged
#     dimension_wording_stripped as its ethnicity name is removed). A row of
#     such a table
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
    r"^\s*ethnic(?:ity|\s+group)(?![a-z])"
    r"(?!\s*(?:" + DIMENSION_JOIN + r"|[,\-–—])?\s*rac(?:e|es|ial)(?![a-z]))", re.IGNORECASE)
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


_RACE_DIMENSION_NAME_RX = re.compile(r"^\s*rac(?:e|es|ial)(?![a-z])", re.IGNORECASE)


def _names_race_dimension(label: str) -> bool:
    """True when a row's cleaned label begins with the word race, races or
    racial ("Race - Undisclosed", "Race,Asian", "Race", "Race unknown"): a row
    of a race classification, mapped or not ("Racecourse" -> False)."""
    return _RACE_DIMENSION_NAME_RX.match(clean_demographic_label(label)) is not None


def _is_own_race_row(label: str) -> bool:
    """True when a row NOT naming the ethnicity dimension shows the table to
    hold a race classification of its own: it maps to a race-specific
    category without the "unmapped" flag, or it is named for the race
    dimension. A bare residual ("Unknown", "Other"), an unmapped row and an
    ethnicity-only row ("Hispanic") are not (block comment "Ethnicity
    dimension named in the row label")."""
    if _names_race_dimension(label):
        return True
    mapping = _map_plain_row(label, 0, False)
    return (mapping is not None and mapping.get("omb_category") in _RACE_SPECIFIC
            and "unmapped" not in mapping.get("flags", []))


def _ethnicity_dimension_is_the_table(labels: List[str]) -> bool:
    """True when the rows naming the ethnicity dimension are the table's race
    classification under that name: at least one row names the dimension, no
    other row is a race row of its own (_is_own_race_row; a bare "Unknown" or
    "Other" beside them is that classification's residual), and at least one
    named row, without the prefix, names a race category itself. A row naming
    an origin group the vocabulary files under a race with a subcategory
    ("Japanese", "Arab", "African") is not that evidence, since such a name is
    as much an ethnicity as a race. A table with no such row ("Ethnicity:
    Hispanic" / "Ethnicity: Not Hispanic" / "Ethnicity: Unknown"; "Ethnicity:
    Japanese" / "Ethnicity: Not Japanese") is an ethnicity classification
    (block comment "Ethnicity dimension named in the row label")."""
    named = [label for label in labels if _names_ethnicity_dimension(label)]
    if not named or any(_is_own_race_row(label) for label in labels if not _names_ethnicity_dimension(label)):
        return False
    for label in named:
        stripped = _strip_ethnicity_dimension(label)
        mapping = (_map_plain_row(stripped, 0, False, guard_negation=True)
                   if stripped is not None else None)
        if (mapping is not None and mapping.get("confidence") != "low"
                and mapping.get("omb_category") in _RACE_SPECIFIC
                and not mapping.get("subcategory")):
            return True
    return False


def _map_table_row(label: str, count: int, pct_converted: bool, total_only: bool,
                   whole_table: bool, race_part: Optional[str] = None,
                   sex: Optional[str] = None,
                   reading: Optional[Dict] = None) -> Tuple[Optional[Dict], Optional[Dict]]:
    """(mapping, None) for a row counted as race, (None, record) for a row left
    out of the race counts (_skipped_ethnicity_row): an ethnicity-only row,
    or a row naming the ethnicity dimension in a table where other rows do
    not (block comment "Ethnicity dimension named in the row label").
    whole_table is _ethnicity_dimension_is_the_table for the row's table, over
    the race parts when the table crosses race with sex or with baseline
    group. For a crossed row the race part is what is read, and a skipped
    record keeps race_part (and sex, for a race-by-sex row) beside the posted
    label. reading is passed to _map_plain_row (the comma-list rule;
    _map_table_rows)."""
    text = race_part if race_part is not None else label
    stripped = _strip_ethnicity_dimension(text) if whole_table else None
    mapping = _map_plain_row(stripped if stripped is not None else text, count,
                             pct_converted, total_only, reading=reading,
                             guard_negation=stripped is not None)
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
    comment above). The table title is added when the table is counted. A
    row of a crossed table keeps its race_part; a race-by-sex row also its
    sex and the race_by_sex flag (a race-by-group row's group and flag are
    added by _parse_race_table)."""
    row: Dict = {"original": clean_demographic_label(label), "count": count}
    flags: List[str] = []
    if race_part is not None:
        row["race_part"] = race_part
    if sex is not None:
        row["sex"] = sex
        flags.append("race_by_sex")
    if pct_converted:
        flags.append("pct_to_count")
    if total_only:
        flags.append(table_selection.TOTAL_CLASS_FLAG)
    row["flags"] = flags
    return row

# ── Comma lists naming two or more races ───────────────────────────────────
#
# A row listing races with "," or ";" only, with no prefix and no "&", "+" or
# "and" (block comment "Rows naming two or more races"), is posted for two
# different things. It is a combination, the participants who reported each
# race named, in "Asian / Black or African American / Native Hawaiian or
# Other Pacific Islander / White / White, American Indian or Alaska Native /
# White, Black or African American / White, Native Hawaiian or Other Pacific
# Islander" (NCT05064449). It is a grouped category,
# participants who reported any one of the races named, in "White
# Non-Hispanic / Black Non-Hispanic / Hispanic (Regardless of Race) / Asian,
# Pacific Islander" (NCT01403051): the 1977 Asian or Pacific Islander group.
# The row alone cannot tell the two apart; the rest of its table can.
#
# Method (_comma_list_rows, over the rows of one table after each is mapped).
# A row is a comma list when its label, read as every row is (dimension
# wording removed, _row_text_read; category names holding "and" kept whole,
# as in the rule for rows naming two or more races), has no heritage word, no
# prefix such as "Multiple" or "Mixed" and no "&", "+" or "and", and splits at
# "," and ";" into parts each naming one race through a reading that is not
# fuzzy (_named_race), passing over a part that is only an ethnicity term
# ("Hispanic"), with two or more different races named. A part read through
# one of its slash fragments (flag slash_normalized: the first fragment the
# vocabulary holds decides) names one race only when every other fragment
# names that race too, through a reading that is not fuzzy, a known
# abbreviation ("Cau") or a race part beside an ethnicity phrase, or is only
# an ethnicity term ("Black/African American", "White/Cau",
# "White/Hispanic"). Any other fragment may be an alternative, so the row is
# not a comma list: another race ("Asian/Black"), a residual category
# ("Asian/Other"), a non-answer ("Asian/Refused", "Asian/Not Applicable") or
# text the vocabulary does not hold ("Asian/Oriental");
# _slash_reading_names_one_race, review findings. A part starting with "or"
# ("White, Asian, or Hispanic", NCT03506724) offers alternatives: the row
# is not a comma list. A label the vocabulary holds whole is not one either.
# A comma list maps to more than one race, flagged multirace_comma_list,
# when the same table has another row, not itself a comma list, read as one
# of the races named through a reading that is not fuzzy (confidence high,
# not unmapped) and, when read through a slash fragment, passing the same
# test as a part ("Asian/Black" or "Asian/Not Applicable" is no evidence of
# an Asian row). A part read by the Middle Eastern or North African rule is
# matched only by a row read by that rule, not by a "White" row
# (_comma_list_key): a table listing it apart from White does not count it as
# White ("White" beside "Metis, Black, Latino/Latina/Latinx, Middle Eastern"
# in NCT05997901, a grouping of the others); two races are still counted in
# the 1997 categories, so "White, Middle Eastern" names one. (Added with the
# rule for American Indian or Alaska Native labels, which made "Metis" a
# race.) A grouped category stands in the table for its races, so
# no row of the table names any of them alone: in
# every table of the inputs holding a comma list with no such row, the row is
# a grouped category ("Asian, Pacific Islander" in 31 trials; "American
# Indian or Alaska Native, Asian" in NCT03596450; "Native American, Native
# Hawaiian, Pacific Islander" in NCT05299983). One race named alone is
# enough, not every race: a table posts only the categories someone
# reported, and "White, American Indian or Alaska Native" is a combination in
# NCT05064449, whose table has a White row and no American Indian row.
#
# What it does not do. A comma list in a table naming none of its races
# alone keeps the reading it had: "Asian, Pacific Islander" stays other, and
# "American Indian or Alaska Native, Asian" stays American Indian through
# the fuzzy match, which is the earlier reading, not a verdict. A part that
# is not a race ("Asian, Black or African American, Other") leaves the row
# as it was, as in the rule for rows naming two or more races. ("American
# Indian, White or Caucasian" in NCT01401842 was such a row until "American
# Indian" was read as a race: block comment "American Indian or Alaska
# Native labels".) The rule reads the posted rows of one
# table; it does not compare a row's count with the counts of the rows
# beside it. A comma list it reads is race-specific, so a sex/gender-titled
# measure holding one can now pass the gate above _sex_titled_race_table.
#
# Measured on the baseline modules of the 80,988 trials in the 2026-11-15
# snapshot (CT.gov API v2, re-fetched 2026-11-18), against the rule for rows
# naming two or more races: rows flagged multirace_comma_list in 38 trials,
# 27 labels; more than one race +82, from other 40, Black 21 ("White, Black
# or African American" 15 in 6 trials, "Black or African American, White" 6
# in 6), American Indian 19, Native Hawaiian 1, Asian 1. "Asian, Pacific
# Islander" is read as a combination in one trial (NCT01401842, 1
# participant), whose table lists Asian and Pacific Islander alone beside 12
# other rows joining labels with commas. No block changes reported, no ethnicity or sex block
# changes, no sex/gender-titled measure changes the gate, and the 338 race
# blocks over their population stay 338, none larger. Comma lists left as
# they were, in tables naming none of their races alone: 1,050 participants
# in 37 trials, every one a grouped category ("Asian, Pacific Islander" 977
# in 31 trials; "American Indian or Alaska Native, Asian" 43 in NCT03596450).
_COMMA_LIST_SPLIT_RX = re.compile(r"\s*[,;]\s*")
_COMMA_LIST_ALTERNATIVE_RX = re.compile(r"^or\b", re.IGNORECASE)
_COMMA_LIST_FLAG = "multirace_comma_list"


# A part read by the Middle Eastern or North African rule is White under the
# 1997 categories, but a table that lists it apart from a "White" row does not
# count it as White: for the evidence of a race named alone it is its own key,
# matched only by a row read by that rule ("White" beside "Metis, Black,
# Latino/Latina/Latinx, Middle Eastern" in NCT05997901 is a grouping of the
# others, not their combination). Two races are still counted in the 1997
# categories, so "White, Middle Eastern" names one.
_COMMA_LIST_MENA_KEY = "white_middle_eastern_north_african"


def _comma_list_key(category: str, subcategory: Optional[str]) -> str:
    """The key a race named in a comma list, or a row of its table, is
    matched on: the category, or _COMMA_LIST_MENA_KEY for White with a
    Middle Eastern or North African subcategory."""
    if category == "white" and subcategory in MENA_SUBCATEGORIES:
        return _COMMA_LIST_MENA_KEY
    return category


def _comma_list_races(text: str) -> Optional[FrozenSet[str]]:
    """The keys (_comma_list_key) of the races a comma list names, or None
    when the text is not a comma list (block comment "Comma lists naming two
    or more races")."""
    text = clean_demographic_label(text)
    if any(key.lower() == text.lower() for key in RACE_MAPPINGS):
        return None
    for name_rx, name in _MULTIRACE_ONE_CATEGORY:
        text = name_rx.sub(name, text)
    if (_HERITAGE_WORD_RX.search(text) is not None
            or _MULTIRACE_PREFIX_RX.match(text) is not None
            or len(_MULTIRACE_JOIN_SPLIT_RX.split(text)) >= 2):
        return None
    parts = _COMMA_LIST_SPLIT_RX.split(text)
    if len(parts) < 2:
        return None
    races = set()
    for part in parts:
        # Before the ethnicity test: "or Hispanic" offers an alternative.
        if _COMMA_LIST_ALTERNATIVE_RX.match(part):
            return None
        if not part.strip(" -–—:,;()/.") or _is_ethnicity_only_label(part):
            continue
        race = _comma_list_part_race(part)
        if race is None or race == "more_than_one_race":
            return None
        races.add(_comma_list_key(race, mena_subcategory(part)))
    named = {"white" if key == _COMMA_LIST_MENA_KEY else key for key in races}
    return frozenset(races) if len(named) >= 2 else None


def _slash_fragment_category(fragment: str) -> Optional[str]:
    """The category a slash fragment reads as without a fuzzy match (exact
    or case-insensitive vocabulary match, or the Middle Eastern or North
    African rule), whatever the category, or None."""
    fragment = fragment.strip(" -–—:,;()/.")
    if not fragment:
        return None
    mapping = map_race_category(fragment, heritage=False, multirace=False)
    if mapping["confidence"] != "high" or "unmapped" in mapping["flags"]:
        return None
    return mapping["omb_category"]


def _fragment_names_race(fragment: str, race: str) -> bool:
    """True when a slash fragment names race: it reads as race without a
    fuzzy match (_slash_fragment_category), is an abbreviation of race the
    module knows (_HERITAGE_ABBREVIATIONS: "Cau"), or is race joined to an
    ethnicity phrase whose race part does ("African American, non-Hispanic";
    _race_joined_to_ethnicity)."""
    if (_slash_fragment_category(fragment) == race
            or _HERITAGE_ABBREVIATIONS.get(fragment.strip(" -–—:,;()/.").lower()) == race):
        return True
    joined = _race_joined_to_ethnicity(fragment)
    return joined is not None and _slash_fragment_category(joined[0]) == race


def _slash_reading_names_one_race(text: str, race: str) -> bool:
    """For a text read as race through one of its slash fragments (flag
    slash_normalized): True only when every other fragment names race too
    (_fragment_names_race) or is only an ethnicity term ("White/Hispanic").
    Any other fragment may be an alternative: another race ("Asian/Black"),
    a residual category ("Asian/Other"), a non-answer ("Asian/Refused",
    "Asian/N/A", "Asian/Not Applicable") or text the vocabulary does not
    hold ("Asian/Oriental"); review findings. "Black/African American" and
    "White/Cau" name one race."""
    for fragment in text.split("/"):
        if (not fragment.strip(" -–—:,;()/.") or _is_ethnicity_only_label(fragment)
                or _fragment_names_race(fragment, race)):
            continue
        return False
    return True


def _read_through_slash(text: str) -> bool:
    """True when map_race_category reads text through one of its slash
    fragments (flag slash_normalized), not as a whole: "Black/African
    American" is, "American Indian/Alaska Native" (a vocabulary entry) and
    "Middle Eastern/North African" (the Middle Eastern or North African
    rule) are not."""
    text = text.strip(" -–—:,;()/.")
    return ("/" in text and "slash_normalized"
            in map_race_category(text, heritage=False, multirace=False)["flags"])


def _comma_list_part_race(part: str) -> Optional[str]:
    """The one race a part of a comma list names (_named_race), or None when
    it names none, or names one only through a slash fragment that the other
    fragments do not confirm (_slash_reading_names_one_race)."""
    race = _named_race(part)
    if race is None or not _read_through_slash(part):
        return race
    return race if _slash_reading_names_one_race(part.strip(" -–—:,;()/."), race) else None


def _row_text_read(text: str, whole_table: bool) -> str:
    """The text _map_race_label reads for a row (or race part): ethnicity
    dimension wording removed when the table is that dimension, as
    _map_table_row does, then race dimension wording, as _map_plain_row
    does."""
    stripped = _strip_ethnicity_dimension(text) if whole_table else None
    text = stripped if stripped is not None else text
    stripped = _strip_race_dimension(text)
    return stripped if stripped is not None else text


def _comma_list_rows(texts: List[str], mappings: List[Optional[Dict]]) -> List[int]:
    """Indices of the rows of one table read as more than one race by the
    comma-list rule. texts[i] is the text read for row i (_row_text_read),
    mappings[i] its mapping, or None for a row left out of the race counts."""
    lists = [_comma_list_races(text) if mapping is not None else None
             for text, mapping in zip(texts, mappings)]
    alone = {_comma_list_key(mapping["omb_category"], mapping.get("subcategory"))
             for text, races, mapping in zip(texts, lists, mappings)
             if races is None and mapping is not None
             and mapping["confidence"] == "high" and "unmapped" not in mapping["flags"]
             and mapping["omb_category"] in _RACE_SPECIFIC
             and mapping["omb_category"] != "more_than_one_race"
             and ("slash_normalized" not in mapping["flags"]
                  or _slash_reading_names_one_race(text, mapping["omb_category"]))}
    return [i for i, races in enumerate(lists) if races is not None and races & alone]


# "Asian and Native Hawaiian or Pacific Islander", "Asian & Native
# Hawaiian/Other Pacific Islander", "Hawaiian or Pacific Islander and Asian":
# the 1977 Asian or Pacific Islander group under later names, or a row of
# participants who reported both races (block comment "Native Hawaiian or
# Pacific Islander labels").
_ASIAN_PART_RX = re.compile(r"asians?|asian[\s\-]+americans?", re.IGNORECASE)
_GROUPED_JOIN_RX = re.compile(r"\s*(?:&|\+|\band\b)\s*", re.IGNORECASE)
_GROUPED_ASIAN_NHPI_FLAG = "asian_pacific_grouped"


def _asian_joined_to_nhpi(text: str) -> bool:
    """True when a row's text is two parts joined by "and", "&" or "+", one
    "Asian" and the other naming the Native Hawaiian or Pacific Islander
    category (_nhpi_names_the_category), in either order. A category name
    holding "and" ("Native Hawaiian and Other Pacific Islander") is one part,
    as for the multirace rule (_MULTIRACE_ONE_CATEGORY)."""
    text = re.sub(r"\s+", " ", clean_demographic_label(text)).strip()
    for rx, name in _MULTIRACE_ONE_CATEGORY:
        text = rx.sub(name, text)
    parts = [part.strip() for part in _GROUPED_JOIN_RX.split(text)]
    if len(parts) != 2:
        return False
    first, second = parts
    return bool((_ASIAN_PART_RX.fullmatch(first) and _nhpi_names_the_category(second))
                or (_ASIAN_PART_RX.fullmatch(second) and _nhpi_names_the_category(first)))
_PACIFIC_TERM_RX = re.compile(r"pacific|hawai|islander", re.IGNORECASE)
_ASIAN_TERM_RX = re.compile(r"asian", re.IGNORECASE)


def _grouped_asian_nhpi_rows(texts: List[str], mappings: List[Optional[Dict]]) -> List[int]:
    """Indices of the rows of one table read as more than one race whose
    text is Asian joined to Native Hawaiian or Pacific Islander
    (_asian_joined_to_nhpi) when no other row of the table names Asian or
    Native Hawaiian or Pacific Islander apart from the other: there they are
    the grouped category, not a combination (block comment "Native Hawaiian
    or Pacific Islander labels"). A row names Asian apart when it is read as
    Asian (not unmapped, not by a low-confidence reading) and its text names
    no Pacific group (_PACIFIC_TERM_RX), and Native Hawaiian or Pacific
    Islander apart when it is read so and its text does not name Asian.
    texts and mappings as for _comma_list_rows."""
    candidates = [i for i, (text, mapping) in enumerate(zip(texts, mappings))
                  if mapping is not None and mapping["omb_category"] == "more_than_one_race"
                  and _asian_joined_to_nhpi(text)]
    if not candidates:
        return []
    for text, mapping in zip(texts, mappings):
        if mapping is None or mapping["confidence"] == "low" or "unmapped" in mapping["flags"]:
            continue
        if mapping["omb_category"] == "asian" and _PACIFIC_TERM_RX.search(text) is None:
            return []
        if (mapping["omb_category"] == "native_hawaiian_pacific_islander"
                and _ASIAN_TERM_RX.search(text) is None):
            return []
    return candidates


_WEST_ASIAN_ROW_RX = re.compile(r"west(?:ern)?[\s\-]+asians?", re.IGNORECASE)
_WEST_ASIAN_TEMPLATE_FLAG = "west_asian_template"
# A row naming the Middle East, North Africa, Iran, Afghanistan, Turkey or
# South Asia: beside one, "West Asian" is not the template's Indian
# subcontinent (block comment "Asian labels").
_WEST_ASIAN_ELSEWHERE_RX = re.compile(
    r"arab|middle[\s\-]*east|north[\s\-]*afric|persian|iran|afghan|turk|south[\s\-]*asia|"
    r"indian?(?![a-z])|pakistan|bangladesh|sri[\s\-]*lanka|sub[\s\-]*continent", re.IGNORECASE)
_WEST_ASIAN_TEMPLATE_PEERS = frozenset({"east_asian", "southeast_asian"})


def _west_asian_template_rows(texts: List[str], mappings: List[Optional[Dict]]) -> List[int]:
    """Indices of the rows of one table reading "West Asian" or "Western
    Asian" alone, unread, in a table that follows the template where it
    names the Indian subcontinent (block comment "Asian labels"): at least
    one row is read as East or Southeast Asian, every other row read as Asian
    is one of those, and no row names the Middle East, North Africa, Iran,
    Afghanistan, Turkey or South Asia (_WEST_ASIAN_ELSEWHERE_RX; a row read
    as American Indian or Alaska Native is not South Asia, a review finding).
    texts and mappings as for _comma_list_rows."""
    candidates = [i for i, (text, mapping) in enumerate(zip(texts, mappings))
                  if mapping is not None and "unmapped" in mapping["flags"]
                  and _WEST_ASIAN_ROW_RX.fullmatch(clean_demographic_label(text).strip())]
    if not candidates:
        return []
    peers = 0
    for i, (text, mapping) in enumerate(zip(texts, mappings)):
        if i in candidates:
            continue
        if mapping is not None and mapping["omb_category"] == "american_indian_alaska_native":
            continue  # "American Indian" names the Americas, not India (a review finding)
        if _WEST_ASIAN_ELSEWHERE_RX.search(text):
            return []
        if (mapping is None or mapping["omb_category"] != "asian" or mapping["confidence"] == "low"
                or "unmapped" in mapping["flags"]):
            continue
        if mapping["subcategory"] not in _WEST_ASIAN_TEMPLATE_PEERS:
            return []
        peers += 1
    return candidates if peers else []


def _map_table_rows(rows: List[Tuple[str, int]], pct_converted: bool, total_only: bool,
                    whole_table: bool,
                    splits: Optional[List[Tuple[str, Optional[str]]]] = None,
                    indigenous_region: Optional[str] = None,
                    ) -> List[Tuple[Optional[Dict], Optional[Dict]]]:
    """_map_table_row for every row of one table, then the rule for
    unqualified Indigenous labels when the trial's site region is given
    (indigenous_region, from _trial_site_region; block comment "Unqualified
    Indigenous labels, read by the trial's sites"), then the West Asian
    template rule (_west_asian_template_rows; block comment "Asian labels"),
    then the comma-list rule
    (block comment "Comma lists naming two or more races"), then the rule for
    Asian joined to Native Hawaiian or Pacific Islander
    (_grouped_asian_nhpi_rows; block comment "Native Hawaiian or Pacific
    Islander labels"). splits holds the
    (race part, sex) of each row of a race-by-sex table, (race part, None)
    for a race-by-group table (block comment "Race crossed with baseline
    group"), else None."""
    parts: List[Tuple[Optional[str], Optional[str]]] = (
        list(splits) if splits is not None else [(None, None)] * len(rows))
    results = [_map_table_row(label, count, pct_converted, total_only, whole_table,
                              race_part, sex)
               for (label, count), (race_part, sex) in zip(rows, parts)]
    texts = [_row_text_read(race_part if race_part is not None else label, whole_table)
             for (label, _count), (race_part, _sex) in zip(rows, parts)]
    if indigenous_region is not None:
        for i, (mapping, _skipped) in enumerate(results):
            if mapping is None or not _unqualified_indigenous(texts[i]):
                continue
            (label, count), (race_part, sex) = rows[i], parts[i]
            category = ("american_indian_alaska_native" if indigenous_region == "americas"
                        else "other")
            reading = {"omb_category": category, "subcategory": None, "confidence": "high",
                       "original": clean_demographic_label(texts[i]),
                       "flags": [f"indigenous_sites_{indigenous_region}"]}
            results[i] = _map_table_row(label, count, pct_converted, total_only, whole_table,
                                        race_part, sex, reading)
    for i in _west_asian_template_rows(texts, [mapping for mapping, _skipped in results]):
        (label, count), (race_part, sex) = rows[i], parts[i]
        reading = {"omb_category": "asian", "subcategory": "south_asian", "confidence": "medium",
                   "original": clean_demographic_label(texts[i]),
                   "flags": [_WEST_ASIAN_TEMPLATE_FLAG]}
        results[i] = _map_table_row(label, count, pct_converted, total_only, whole_table,
                                    race_part, sex, reading)
    for i in _comma_list_rows(texts, [mapping for mapping, _skipped in results]):
        (label, count), (race_part, sex) = rows[i], parts[i]
        reading = {"omb_category": "more_than_one_race", "subcategory": None,
                   "confidence": "high", "original": clean_demographic_label(texts[i]),
                   "flags": [_COMMA_LIST_FLAG]}
        results[i] = _map_table_row(label, count, pct_converted, total_only, whole_table,
                                    race_part, sex, reading)
    for i in _grouped_asian_nhpi_rows(texts, [mapping for mapping, _skipped in results]):
        (label, count), (race_part, sex) = rows[i], parts[i]
        reading = {"omb_category": "other", "subcategory": None, "confidence": "low",
                   "original": clean_demographic_label(texts[i]),
                   "flags": ["unmapped", _GROUPED_ASIAN_NHPI_FLAG]}
        results[i] = _map_table_row(label, count, pct_converted, total_only, whole_table,
                                    race_part, sex, reading)
    return results

# ── Unqualified Indigenous labels, read by the trial's sites ───────────────
#
# A row reading "Indigenous", "Aboriginal", "Aborigine", "Indigenous Person"
# or "Native/Aboriginal" names the Indigenous people of the place the trial
# ran, which the label does not give. The vocabulary read "Indigenous" as
# American Indian or Alaska Native wherever it was posted, also in an
# Australia and New Zealand trial (NCT05610176), and counted the others as
# other, also in Canadian trials posting the First Nations, Inuit and Metis
# as "Aboriginal" (11 trials). OMB's American Indian or Alaska Native is the
# original peoples of North, Central and South America (block comment
# "American Indian or Alaska Native labels"); Indigenous peoples elsewhere
# are not in it.
#
# Method. A row whose text, read as every row is (_row_text_read), is made
# only of "Indigenous", "Aboriginal(s)", "Aborigine(s)" and "Native(s)",
# joined by "/", ",", "&", "or", "and" or a space, with at least one of the
# first three, and an optional "people(s)", "person(s)" or "population"
# (_unqualified_indigenous), is read by the trial's site countries
# (protocolSection.contactsLocationsModule.locations[].country, the source
# of the published countries; _trial_site_region):
#   - every site in the Americas (_AMERICAS_COUNTRIES: North, Central and
#     South America and the Caribbean): American Indian or Alaska Native,
#     flagged indigenous_sites_americas;
#   - no site in the Americas: other, flagged
#     indigenous_sites_outside_americas;
#   - sites in and outside the Americas: other, flagged
#     indigenous_sites_mixed: the participants may be either;
#   - no site country posted: other, flagged indigenous_sites_unknown.
# American Indian or Alaska Native is given only where the sites place the
# trial in the Americas; elsewhere the row is other, with the flag saying
# why, never left as "unmapped".
#
# What it does not do. "Native" alone, "Tribal members", and a label naming
# a people or place ("Indigenous Mayan", "Aboriginal Australian", block
# comment "American Indian or Alaska Native labels") are not read by this
# rule. The United States counts as the Americas whole: a trial whose only
# sites are in Hawaii, where "Indigenous" would name Native Hawaiians, is
# read as American Indian (none in the 2026-11-15 inputs posts such a row).
# Sex/gender-titled tables and extract_race_from_measure, read without the
# study, are not changed. Guam, American Samoa, Micronesia and the United
# States Minor Outlying Islands are Pacific, not the Americas.
#
# Measured on the baseline modules of the 80,988 trials in the 2026-11-15
# snapshot (CT.gov API v2, re-fetched 2026-11-18), with each trial's site
# countries from the snapshot's published countries, against engine main:
# American Indian +92 (110 participants in 20 trials from other, mostly
# Canadian "Aboriginal" rows: NCT03149328 32, NCT00326924 30, and an
# Arizona trial, NCT00706121 18; 18 in 3 trials to other: NCT03385304 16,
# sites in Canada, Spain and the United States; NCT05610176 1, Australia and
# New Zealand; NCT01908751 1, Australia, Canada and the United States).
# Other -92. 22 race blocks change their totals, 68 any field (rows that
# stay other lose "unmapped" and gain the reason flag); none changes
# reported, no ethnicity block changes, and the race blocks over their
# population do not change. Rows flagged: indigenous_sites_americas 354
# participants in 35 trials, indigenous_sites_mixed 96 in 11,
# indigenous_sites_outside_americas 91 in 5, indigenous_sites_unknown 26 in
# 17 (15 of them "Aborigine" in trials posting no site).
_AMERICAS_COUNTRIES = frozenset({
    # North and Central America
    "United States", "Canada", "Mexico", "Greenland", "Saint Pierre and Miquelon", "Bermuda",
    "Guatemala", "Belize", "Honduras", "El Salvador", "Nicaragua", "Costa Rica", "Panama",
    # South America
    "Colombia", "Venezuela", "Ecuador", "Peru", "Bolivia", "Brazil", "Paraguay", "Uruguay",
    "Argentina", "Chile", "Guyana", "Suriname", "French Guiana", "Falkland Islands",
    # The Caribbean
    "Puerto Rico", "Dominican Republic", "Haiti", "Jamaica", "Cuba", "Trinidad and Tobago",
    "Barbados", "The Bahamas", "Bahamas", "Guadeloupe", "Martinique", "Saint Lucia", "Grenada",
    "Antigua and Barbuda", "Dominica", "Saint Kitts and Nevis",
    "Saint Vincent and the Grenadines", "Aruba", "Curaçao", "Curacao", "Sint Maarten",
    "Saint Martin", "Saint Barthélemy", "Netherlands Antilles", "Cayman Islands",
    "Turks and Caicos Islands", "British Virgin Islands", "Virgin Islands",
    "Virgin Islands (U.S.)", "U.S. Virgin Islands", "Anguilla", "Montserrat",
})
_INDIGENOUS_WORD = r"(?:indigenous|aborigin(?:al|als|e|es)|natives?)"
_UNQUALIFIED_INDIGENOUS_RX = re.compile(
    rf"^{_INDIGENOUS_WORD}(?:\s*(?:/|,|&|\bor\b|\band\b)\s*{_INDIGENOUS_WORD}|\s+{_INDIGENOUS_WORD})*"
    r"(?:\s+(?:people|peoples|person|persons|population))?$", re.IGNORECASE)
_NAMES_INDIGENOUS_RX = re.compile(r"indigenous|aborigin", re.IGNORECASE)


def _unqualified_indigenous(text: str) -> bool:
    """True when a row's text names Indigenous people without saying which
    (block comment above): "Indigenous", "Aboriginal", "Native/Aboriginal",
    "Indigenous Person"; not "Native" alone or "Indigenous Mayan"."""
    text = clean_demographic_label(text).strip()
    return (_UNQUALIFIED_INDIGENOUS_RX.match(text) is not None
            and _NAMES_INDIGENOUS_RX.search(text) is not None)


def _trial_site_region(study: dict) -> str:
    """Where a trial's sites are (block comment above): "americas" when every
    site country is in the Americas, "outside_americas" when none is,
    "mixed" when some are, "unknown" when no site posts a country."""
    locations = (((study or {}).get("protocolSection") or {})
                 .get("contactsLocationsModule") or {}).get("locations") or []
    countries = {(location or {}).get("country") for location in locations} - {None, ""}
    if not countries:
        return "unknown"
    inside = {country in _AMERICAS_COUNTRIES for country in countries}
    if inside == {True}:
        return "americas"
    if inside == {False}:
        return "outside_americas"
    return "mixed"

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
# is one; "race_by_group" on each row of a race-by-group table (block comment
# "Race crossed with baseline group"; the row carries race_part and group)
# and "race_by_group_table" when the counted table is one;
# "race_table_superseded" when a table was left out by the rules above;
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

# ── Race crossed with baseline group ───────────────────────────────────────
#
# A registrant can post a study's groups as rows of the race table instead of
# as its columns. NCT02675777 (484,047 participants; baseline groups
# "Quality Improvement (SPARC) Intervention Period", "Usual Care Period" and
# "Total") posts "Race/Ethnicity, Customized" with one row per race and
# period: "Asian - SPARC", "Asian - Usual Care", ..., "White - SPARC"
# (160,764), "White - Usual Care" (184,654), 16 rows. Each row's count is in
# its own period's column and in the Total column, nowhere else. Every row
# was mapped whole: "Black or African American - SPARC" reached Black by
# fuzzy match and the other 15 rows were counted as other, so the trial read
# other 471,522 of 484,047 and White 0.
#
# Method (_race_by_group_splits). A race-titled table is read as race crossed
# with baseline group when all of these hold:
#   - every row's label, cleaned (clean_demographic_label), splits at its
#     last spaced dash (" - ", " – " or " — ") into a race part and a group
#     reference, and the reference names exactly one group column: its words
#     (runs of letters and digits, case-insensitive) are all words of that
#     column's title and not all words of any other's
#     (split_race_by_group_label). "SPARC" names "Quality Improvement (SPARC)
#     Intervention Period"; "Usual Care" names "Usual Care Period". The
#     columns are the baseline groups other than the whole-population one,
#     the single group whose title utils.is_overall_group_title accepts
#     ("Total", "Overall", "All participants" ...). With none or more than one
#     such title every group is a column, so a Total column holding the row's
#     count again fails the next test (_baseline_group_columns);
#   - every row's count is posted in its named column alone: no other column
#     holds a number other than 0, and the count the row is read at (the
#     value utils.sum_measurements reads) is the number in that column, or 0
#     when that column holds none. This is what makes the rows disjoint, so
#     they can be added: each participant is counted in one group's row;
#   - the rows name at least two columns;
#   - the counts are posted as counts, not converted from percentages, and
#     the table is not read from a Total class (table_selection block comment
#     "A Total class beside subgroup classes").
# Then each row's race part is read as a row of its own would be
# (_map_table_rows over the race parts: the ethnicity-dimension routing, the
# unqualified-Indigenous rule and the comma-list rule, as for a race-by-sex
# table). Each record keeps the posted label as "original" and carries
# "race_part", "group" (the column's title as posted) and the flag
# "race_by_group"; an ethnicity row left out of the counts carries the same.
# When no race part maps, the table is read as plain rows, as a race-by-sex
# table is. Other separators and the reverse order ("SPARC - White") are not
# read: no table in the 2026-11-15 snapshot posts them.
#
# Why both tests. A label of the same shape can name something that is not
# a group of people counted once: NCT04346628 (135 participants) posts every
# race twice, "White - smITT Analysis Set" and "White - mITT Analysis Set";
# the smITT rows add up to 135 and the mITT rows to another 116, so summing
# them would count 251 of 135. Its suffixes name no baseline group and its
# counts sit in both arm columns, so it fails both tests and stays plain
# rows. The posting alone is not enough either: in 142 race-titled tables of
# the snapshot every row's count sits in one group's column, mostly because
# the groups were defined by race ("White Adults" / "Black Adults") or a race
# has a few participants in one arm; those rows name races only and stay
# plain rows. NCT02675777's is the only one of the 142 whose labels name a
# group, and all 16 of its rows do.
#
# A race-by-sex reading is tried first and kept when it applies, so no
# race-by-sex table changes. Which tables are counted is decided as before
# (block comment on table selection below): a race-by-group table is a
# race-titled table whose rows are read differently, and nothing else.

_GROUP_REFERENCE_SPLIT_RE = re.compile(r"\s+[-–—]\s+")
_GROUP_WORD_RE = re.compile(r"[a-z0-9]+")


def _group_words(text: str) -> FrozenSet[str]:
    """The words of a group title or reference: runs of letters and digits,
    lowercased (block comment above)."""
    return frozenset(_GROUP_WORD_RE.findall(text.lower()))


def _baseline_group_columns(study: dict) -> List[Tuple[str, str]]:
    """(group id, title) of each baseline group column, in source order: every
    baseline group except the whole-population one, when exactly one title
    names the whole population (block comment above). The id is the group's
    "id" (the registry's key) or "groupId" (the key the test fixtures use)."""
    groups = ((study.get("resultsSection") or {})
              .get("baselineCharacteristicsModule", {})
              .get("groups") or [])
    pairs: List[Tuple[str, str]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_id = group.get("id") or group.get("groupId")
        if group_id:
            pairs.append((group_id, (group.get("title") or "").strip()))
    whole = [i for i, (_gid, title) in enumerate(pairs) if is_overall_group_title(title)]
    if len(whole) == 1:
        del pairs[whole[0]]
    return pairs


def split_race_by_group_label(label: str,
                              columns: List[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
    """Split a race-by-group label into (race_part, group id), or None.

    The cleaned label is split at its last spaced dash; the part after it must
    name exactly one of columns, (group id, title) pairs (block comment
    above). With columns [("BG000", "Quality Improvement (SPARC) Intervention
    Period"), ("BG001", "Usual Care Period")]:
      "White - SPARC"       -> ("White", "BG000")
      "Asian - Usual Care"  -> ("Asian", "BG001")
      "White - Period"      -> None (names both)
      "White/Caucasian"     -> None (no spaced dash)
    """
    text = clean_demographic_label(label)
    dashes = list(_GROUP_REFERENCE_SPLIT_RE.finditer(text))
    if not dashes:
        return None
    race_part = text[:dashes[-1].start()].strip()
    reference = _group_words(text[dashes[-1].end():])
    if not race_part or not reference:
        return None
    named = [group_id for group_id, title in columns if reference <= _group_words(title)]
    if len(named) != 1:
        return None
    return race_part, named[0]


def _race_by_group_splits(measure: dict, rows: List[Tuple[str, int]], pct_converted: bool,
                          total_only: bool, columns: List[Tuple[str, str]]
                          ) -> Optional[List[Tuple[str, str]]]:
    """(race_part, group id) for each row, in order, when the table is race
    crossed with baseline group; else None (block comment above). rows are
    the measure's rows as _measure_rows read them, with pct_converted and
    total_only; columns from _baseline_group_columns."""
    if pct_converted or total_only or len(columns) < 2 or not rows:
        return None
    splits = [split_race_by_group_label(label, columns) for label, _count in rows]
    if any(split is None for split in splits):
        return None
    named = [split for split in splits if split is not None]
    if len({group_id for _race_part, group_id in named}) < 2:
        return None
    sources = _labelled_measurements(measure)
    if [label for label, _m in sources] != [label for label, _c in rows]:
        return None
    column_ids = {group_id for group_id, _title in columns}
    for (_label, count), (_race_part, group_id), (_same, measurements) in zip(rows, named, sources):
        posted: Dict[str, float] = {}
        for measurement in measurements:
            column = measurement.get("groupId") if isinstance(measurement, dict) else None
            if column in column_ids:
                number = measurement_number([measurement])
                if number:
                    posted[column] = number
        if set(posted) - {group_id} or count != posted.get(group_id, 0):
            return None
    return named

_ASIAN_RULE_FLAGS = frozenset({"asian_label", _WEST_ASIAN_TEMPLATE_FLAG})
_ASIAN_BREAKDOWN_FLAG = "asian_breakdown"


def _asian_breakdown_rows(categories: List[Dict], population: Optional[int]) -> List[int]:
    """Indices of the counted rows of one table to leave out because they
    count again Asian participants another row counts (block comment "Asian
    labels"). The rows are taken by partition: the whole of a plain table,
    each sex of a race-by-sex table, each group of a race-by-group table (a
    review finding). In each partition holding a row the Asian label rules
    read, the parent is the largest row read as Asian (ties: a reading made
    before those rules, then one with no subcategory, then the first), and
    the other Asian rows are its breakdown when they add up to no more than
    it. The table, which adds up to more than its population, must add up to
    exactly its population without the breakdowns: the excess is the
    breakdown. A table over its population by less is left as posted
    ("Asian" 11 beside "Indian Subcontinent" 139 in NCT01829295, over by 7; a
    review of the first version). Then, in each partition, the breakdown rows
    the Asian label rules read are left out; when the parent is the only row
    those rules read, the parent is. A table no row of which those rules read
    is left as it is."""
    if population is None:
        return []
    table_sum = sum(category["count"] for category in categories)
    if table_sum <= population:
        return []
    partitions: Dict[Tuple[Optional[str], Optional[str]], List[int]] = {}
    for i, category in enumerate(categories):
        if category["omb_category"] == "asian":
            partitions.setdefault((category.get("sex"), category.get("group")), []).append(i)
    new = {i for rows in partitions.values() for i in rows
           if _ASIAN_RULE_FLAGS & set(categories[i]["flags"])}
    nests: List[Tuple[int, List[int]]] = []
    for asian in partitions.values():
        if len(asian) < 2 or not new.intersection(asian):
            continue
        parent = max(asian, key=lambda i: (categories[i]["count"], i not in new,
                                           categories[i]["subcategory"] is None, -i))
        others = [i for i in asian if i != parent]
        if categories[parent]["count"] >= sum(categories[i]["count"] for i in others):
            nests.append((parent, others))
    breakdown = sum(categories[i]["count"] for _parent, others in nests for i in others)
    if not nests or table_sum - breakdown != population:
        return []
    left_out: List[int] = []
    for parent, others in nests:
        left_out.extend([i for i in others if i in new] or [parent])
    return sorted(left_out)


def _parse_race_table(measure: dict, index: int, overall_group_id: Optional[str],
                      fallback_denom: Optional[int], sex_titled: bool,
                      indigenous_region: Optional[str] = None,
                      group_columns: Optional[List[Tuple[str, str]]] = None) -> Optional[Dict]:
    """Read one baseline measure as a race table.

    Returns {"title", "index", "analyzed", "sex_titled", "race_by_sex",
    "race_by_group", "categories", "quarantined", "skipped_ethnicity"}: the
    table's own Number Analyzed (or None), the counted category records, the
    quarantined labels and the ethnicity-only rows left out of the counts
    (_skipped_ethnicity_row). A race-titled measure always returns a table
    (plain rows unless it passes the race-by-sex test or, given the study's
    group_columns from _baseline_group_columns, the race-by-group test: block
    comment "Race crossed with baseline group"). A sex/gender-titled measure
    returns None unless every row maps to a race-specific category (see the
    block comment above).
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
        mapped = _map_table_rows(rows, pct_converted, total_only, whole_by_sex, splits,
                                 indigenous_region=indigenous_region)
        for (label, _count), (race_part, sex), (mapping, skipped_row) in zip(rows, splits, mapped):
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

    race_by_group = False
    group_splits = (_race_by_group_splits(measure, rows, pct_converted, total_only, group_columns)
                    if not race_by_sex and group_columns else None)
    if group_splits is not None:
        # Race crossed with baseline group (block comment "Race crossed with
        # baseline group"): the race parts are read as a race-by-sex table's
        # are, and each record names its group column.
        categories, quarantined, skipped = [], [], []
        group_titles = dict(group_columns or [])
        whole_by_group = _ethnicity_dimension_is_the_table(
            [race_part for race_part, _group_id in group_splits])
        mapped = _map_table_rows(rows, pct_converted, total_only, whole_by_group,
                                 [(race_part, None) for race_part, _group_id in group_splits],
                                 indigenous_region=indigenous_region)
        for (label, _count), (race_part, group_id), (mapping, skipped_row) in zip(
                rows, group_splits, mapped):
            if skipped_row is not None:
                skipped_row["group"] = group_titles[group_id]
                skipped_row["flags"].append("race_by_group")
                skipped.append(skipped_row)
                continue
            is_quarantined = _should_quarantine(mapping)
            mapping["original"] = clean_demographic_label(label)
            mapping["race_part"] = race_part
            mapping["group"] = group_titles[group_id]
            mapping["flags"].append("race_by_group")
            if is_quarantined:
                quarantined.append(mapping)
            else:
                categories.append(mapping)
        race_by_group = any("unmapped" not in c["flags"] for c in categories)

    if not race_by_sex and not race_by_group:
        # Plain table, or sex words or group names that are not a crossing:
        # every row mapped whole, as before.
        categories, quarantined, skipped = [], [], []
        whole_table = _ethnicity_dimension_is_the_table([label for label, _count in rows])
        for mapping, skipped_row in _map_table_rows(rows, pct_converted, total_only, whole_table,
                                                    indigenous_region=indigenous_region):
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

    # Asian rows that count the table's Asian participants again (block
    # comment "Asian labels"), in a plain, race-by-sex or race-by-group table
    # (a review finding), against the table's own Number Analyzed, else the
    # study's baseline population. After the sex/gender gate, which reads a
    # table whole.
    analyzed = _number_analyzed(measure, overall_group_id)
    breakdown = set(_asian_breakdown_rows(
        categories, analyzed if analyzed is not None else fallback_denom))
    for i in sorted(breakdown):
        categories[i]["flags"].append(_ASIAN_BREAKDOWN_FLAG)
        categories[i]["quarantine_reason"] = _ASIAN_BREAKDOWN_FLAG
        quarantined.append(categories[i])
    categories = [category for i, category in enumerate(categories) if i not in breakdown]

    return {
        "title": title,
        "index": index,
        "analyzed": analyzed,
        "population": table_selection.population_of(measure),
        "sex_titled": sex_titled,
        "race_by_sex": race_by_sex,
        "race_by_group": race_by_group,
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
                                           total_participants, sex_titled=False,
                                           indigenous_region=_trial_site_region(study),
                                           group_columns=_baseline_group_columns(study)))
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
        if table["race_by_group"]:
            result["flags"].append("race_by_group_table")
        if table["sex_titled"]:
            result["flags"].append("race_from_sex_titled_table")

        # Quarantine irrelevant unmapped labels (e.g. "Condom", "IUD")
        for cat in table["quarantined"]:
            result["quarantined_labels"].append({
                "original": cat["original"],
                "count": cat["count"],
                "reason": cat.get("quarantine_reason", "unmapped_non_race_label")
            })
            result["flags"].append("quarantined_label")

        # Ethnicity-only rows of this table: recorded, never counted (block
        # comment "Ethnicity rows inside a race table").
        for row in table["skipped_ethnicity"]:
            result["skipped_ethnicity_rows"].append({
                "original": row["original"],
                "count": row["count"],
                "source_table": table["title"],
                **{k: row[k] for k in ("race_part", "sex", "group") if k in row},
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
