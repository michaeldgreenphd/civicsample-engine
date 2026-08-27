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

ONTOLOGY_PATH = Path(__file__).resolve().parent.parent / "condition_ontology.json"
DEFAULT_FUZZY_THRESHOLD = 85


def _load_ontology():
    """Load and index the ontology file into fast lookup structures."""
    global _ontology, _flat_lookup, _all_keywords

    with open(ONTOLOGY_PATH) as f:
        _ontology = json.load(f)

    _flat_lookup = {}
    for primary, secondaries in _ontology["categories"].items():
        for secondary, keywords in secondaries.items():
            for kw in keywords:
                _flat_lookup[kw.lower()] = (primary, secondary)

    # Sort keywords longest-first so longer phrases match before short ones
    _all_keywords = sorted(_flat_lookup.keys(), key=len, reverse=True)
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

    # --- Step A: exact / substring keyword match (longest-first) -----------
    for kw in _all_keywords:
        if kw in norm:
            return _flat_lookup[kw]

    # --- Step B: fuzzy match on the whole condition string -----------------
    match = process.extractOne(
        norm,
        _all_keywords,
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
