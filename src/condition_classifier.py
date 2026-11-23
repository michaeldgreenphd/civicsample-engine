"""
Hierarchical condition classifier using condition_ontology.json.

Classifies raw condition strings from ClinicalTrials.gov into a two-level
hierarchy (Primary Category -> Secondary Category) using exact keyword matching
with a rapidfuzz fallback for typos and minor variations.
"""
import json
import re
import logging
from pathlib import Path
from typing import Optional, Tuple

from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level cache — loaded once on first call
# ---------------------------------------------------------------------------
_ontology = None
_flat_lookup = None          # keyword -> (primary, secondary)
_all_keywords = None         # ordered list of keywords for fuzzy matching
_short_patterns: Optional[dict] = None   # short keyword -> whole-word pattern
_fuzzy_keywords = None       # keywords the fuzzy step may return (no short ones)

ONTOLOGY_PATH = Path(__file__).resolve().parent.parent / "condition_ontology.json"
DEFAULT_FUZZY_THRESHOLD = 85

# Keywords this short are mostly acronyms ("ra", "ms", "pe", "ed", "gi",
# "all", "sti", "dic", "skin", "burn", "afib"). Found inside a longer word
# they name a different condition: "Trauma" holds "ra" (rheumatoid
# arthritis), "Falls" holds "all" (a leukaemia), "Sedation" holds "ed",
# "Dyskinesia" holds "skin", "Burnout" holds "burn". Such a keyword matches
# only as a whole word, with an optional plural "s"/"es" ("MS", "AIDS").
# Longer keywords keep matching anywhere in the text, because inside a
# compound term they still name its condition ("Gliosarcoma" holds
# "sarcoma", "Bronchopulmonary" holds "pulmonary", "Colonoscopy" holds
# "colon"). The threshold of 4 was chosen on the published records of
# 2026-11-15 (142,369 condition mentions): keywords of up to 4 characters
# account for nearly all matches inside unrelated words, and a threshold of
# 3 left "Burnout" -> burn wound and "Dyskinesia" -> dermatology in place.
#
# The same keywords are also left out of the fuzzy step (Step B). A short
# keyword standing as a whole word is already matched in Step A, so the fuzzy
# step could only reach it from a near-miss spelling, and for a 2-4 letter
# acronym a near-miss is a different word, not a typo: "Fall" scores 85.7
# against "all" (acute lymphoblastic leukaemia) under token_set_ratio, above
# the threshold of 85.
SHORT_KEYWORD_MAX_LEN = 4


def _short_keyword_pattern(kw: str) -> "re.Pattern[str]":
    """Whole-word pattern for a short keyword, allowing a plural "s"/"es"."""
    return re.compile(r"(?<!\w)" + re.escape(kw) + r"(?:s|es)?(?!\w)")


def _keyword_matches(kw: str, norm: str) -> bool:
    """Whether a keyword occurs in normalised condition text.

    Short keywords (SHORT_KEYWORD_MAX_LEN characters or fewer) must stand as
    a whole word; longer keywords may sit inside a longer word. See the note
    on SHORT_KEYWORD_MAX_LEN for why.
    """
    pattern = (_short_patterns or {}).get(kw)
    if pattern is not None:
        return pattern.search(norm) is not None
    return kw in norm


def _load_ontology() -> None:
    """Load and index the ontology file into fast lookup structures."""
    global _ontology, _flat_lookup, _all_keywords, _short_patterns, _fuzzy_keywords

    with open(ONTOLOGY_PATH) as f:
        _ontology = json.load(f)

    _flat_lookup = {}
    for primary, secondaries in _ontology["categories"].items():
        for secondary, keywords in secondaries.items():
            for kw in keywords:
                _flat_lookup[kw.lower()] = (primary, secondary)

    # Sort keywords longest-first so longer phrases match before short ones
    _all_keywords = sorted(_flat_lookup.keys(), key=len, reverse=True)
    _short_patterns = {
        kw: _short_keyword_pattern(kw)
        for kw in _all_keywords if len(kw) <= SHORT_KEYWORD_MAX_LEN
    }
    _fuzzy_keywords = [kw for kw in _all_keywords if len(kw) > SHORT_KEYWORD_MAX_LEN]
    logger.info(f"Loaded condition ontology: {len(_ontology['categories'])} primary categories, "
                f"{len(_flat_lookup)} keywords")


def _ensure_loaded():
    if _ontology is None:
        _load_ontology()


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------
_PUNCT_RE = re.compile(r"[^\w\s/-]")


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation (keep hyphens/slashes), collapse whitespace."""
    text = text.lower().strip()
    text = _PUNCT_RE.sub(" ", text)
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_condition(condition: str,
                       fuzzy_threshold: int = DEFAULT_FUZZY_THRESHOLD
                       ) -> Tuple[str, str]:
    """
    Classify a single condition string.

    Returns:
        (primary_category, secondary_category)
        Falls back to ("Other", "Uncategorized") if no match found.
    """
    _ensure_loaded()

    if not condition or not condition.strip():
        return ("Other", "Uncategorized")

    norm = _normalize(condition)

    # --- Step A: keyword match, longest-first (short keywords whole-word) --
    for kw in _all_keywords:
        if _keyword_matches(kw, norm):
            return _flat_lookup[kw]

    # --- Step B: fuzzy match on the whole condition string (long keywords) --
    match = process.extractOne(
        norm,
        _fuzzy_keywords,
        scorer=fuzz.token_set_ratio,
        score_cutoff=fuzzy_threshold,
    )
    if match:
        keyword, score, _idx = match
        return _flat_lookup[keyword]

    return ("Other", "Uncategorized")


def classify_conditions(conditions: list,
                        fuzzy_threshold: int = DEFAULT_FUZZY_THRESHOLD
                        ) -> dict:
    """
    Classify a list of condition strings (one study's conditions).

    Returns a dict with:
        primary_condition:   best primary category (most frequent across the
                             study's conditions; alphabetical tie-break)
        secondary_condition: best secondary category matching the chosen primary
        all_classifications: list of {condition, primary, secondary} for every
                             input condition
    """
    _ensure_loaded()

    if not conditions:
        return {
            "primary_condition": "Other",
            "secondary_condition": "Uncategorized",
            "all_classifications": [],
        }

    classifications = []
    primary_counts = {}
    primary_to_secondary = {}

    for cond in conditions:
        pri, sec = classify_condition(cond, fuzzy_threshold)
        classifications.append({
            "condition": cond,
            "primary": pri,
            "secondary": sec,
        })
        primary_counts[pri] = primary_counts.get(pri, 0) + 1
        # Keep track of the first secondary seen per primary
        if pri not in primary_to_secondary:
            primary_to_secondary[pri] = sec

    # Pick the most common primary (alphabetical tie-break for stability)
    best_primary = max(primary_counts, key=lambda p: (primary_counts[p], p))
    best_secondary = primary_to_secondary[best_primary]

    return {
        "primary_condition": best_primary,
        "secondary_condition": best_secondary,
        "all_classifications": classifications,
    }
