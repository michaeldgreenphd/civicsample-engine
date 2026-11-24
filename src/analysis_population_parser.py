"""Deterministic parser for ClinicalTrials.gov outcome analysis populations.

READS   Raw ClinicalTrials.gov API v2 study dicts (resultsSection.outcomeMeasuresModule
        and resultsSection.participantFlowModule), or retained raw outcome records
        from select_raw_outcome_populations().
WRITES  Nothing itself. Emits typed dataclasses (ParsedOutcomePopulation and
        TrialAnalysisPopulations) for downstream trial shards and outcome indices.

Design invariants (mirroring src/sex_gender_parser.py and src/sex_gender_table.py):
  1. Nine disjoint, non-collapsing PopulationClass labels:
       itt, modified_itt, full_analysis_set, per_protocol, evaluable,
       safety, other_stated, not_stated, ambiguous_multi.
  2. Verbatim preservation: population_description_raw (also exposed via
     raw_population_description) is stored byte-for-byte as received;
     Unicode normalization runs only on a transient copy for rule matching.
  3. Two-tier deterministic vocabulary with character-span masking:
     - Tier 0 (placeholder) catches pure placeholders ("N/A", "Not specified")
       as not_stated (not_stated_reason="placeholder_token"), distinct from
       field_absent.
     - Tier 1 (named_label) scans all six specific classes in fixed order;
       compound labels like "Modified Intent-to-Treat" or "Safety evaluable"
       mask their matched span for the subsumed class ("itt", "evaluable") so
       single-class compound labels do not falsely trigger ambiguous_multi,
       while genuinely distinct co-occurring labels ("ITT and mITT", "ITT and
       Per-Protocol") still classify as ambiguous_multi.
     - Tier 2 (definitional) runs ONLY when Tier 1 matched zero classes, so
       definitional prose attached to a named set ("Full Analysis Set (FAS):
       all randomized patients...") never triggers false ambiguous_multi.
  4. Strict separation of denominator units:
     - "Participants" denominators populate overall_participants_analyzed and
       is_participant_count=True (denom_unit_kind="PARTICIPANTS" or "BOTH").
     - Non-participant units (e.g. "Eyes", "Lesions", COUNT_OF_UNITS) populate
       overall_units_analyzed and units_analyzed_type and NEVER leak into
       overall_participants_analyzed.
  5. Strict absence semantics: missing, empty, or "NA" counts remain None with
     an explicit denom_status / arm_gap_reason; they are NEVER coerced to 0.
  6. Strict separation of attrition gap from population classification:
     arm_started_minus_analysed = started_participants - analyzed_participants
     is stored strictly for audit (including negative values without clamping)
     and has NO code path into classify_population_description().

DEPENDS  pandas (requirements.txt: pandas>=2.2.0), imported lazily inside
         parse_outcomes_frame(), the module's only tabular helper. Importing
         this module does not import pandas (its module-level mention is
         under TYPE_CHECKING, for parse_outcomes_frame's return annotation);
         calling parse_outcomes_frame() where pandas is not installed raises
         ImportError. Every import executed at module load is from the Python
         standard library. CI runs the tests on Python 3.11
         (.github/workflows/ci.yml).

Audit holdout (in_audit). This module does no I/O: it reads only the objects
it is handed. A trial whose ``in_audit`` is true in the site's
data/outcomes/index.json must not be parsed, retained or recorded. Which
entry point guards that:

  Enforce when the record carries ``in_audit`` (raise
  PopulationAuditHoldoutError; see _refuse_audit_holdout_record):
      parse_study_outcomes, parse_outcomes_frame (the whole batch, before
      any study is parsed), select_raw_outcome_populations,
      build_outcomes_from_raw.
    "Carries" means a key, field, attribute or label whose name contains
    in_audit once case, spaces, dots, hyphens, underscores, full-width
    forms, letters that print like ASCII letters (Cyrillic, Greek and
    Armenian lookalikes, dotless i, small capitals, Roman numerals,
    modifier and subscript letters, letterlike symbols, and the
    mathematical, circled, parenthesized, squared and regional-indicator
    alphabets: the hand-written table _TO_ASCII) and characters that print
    as nothing (bidi marks and isolates, zero-width and other format
    characters, variation selectors, tag characters, combining marks) are
    ignored ("inAudit", "IN_AUDIT", "in-audit", "in.audit" count, and so do
    qualified names: the pandas merge suffixes in_audit_x/in_audit_y, a
    json_normalize path "manifest.in_audit", "manifest_in_audit",
    "is_in_audit"; a key that is not text, such as an enum member, is read
    as its name (an IntEnum's or IntFlag's too, whose str() is a number),
    its str() and its value), at any depth (beside
    the nctId, on protocolSection, in a merged ``manifest`` sub-record, in a
    pandas object's ``attrs``, a named row-index level or a bare Index's
    name), in a dict, any other mapping-like record
    (collections.UserDict, types.MappingProxyType, a collections.ChainMap,
    whose shadowed layers are read too, a pandas Series), a namedtuple (a
    ``df.itertuples()`` row), a numpy record (a ``df.to_records()`` row), a
    keyed row (a sqlite3.Row: its ``keys()`` name its values), an XML
    element (ElementTree: its tag, text, attributes and child elements, a
    child's tag naming it; a minidom node is a plain object), a dataclass
    instance, a plain object, or any sized container (list, tuple, set,
    deque, dict view, numpy object array); and beside those, as an
    instance attribute, a property or a class attribute of a value of any
    type that is not a builtin (a dict, str, dataclass or namedtuple
    subclass), or inside a callable (a functools.partial's arguments, a
    function's closure cells and defaults, a callable object's attributes)
    (_beside_items). Text that holds in_audit as a key is a flag too, and
    since text is not a bool it is refused: JSON and repr manifests
    (escaped keys, Python 3.11's OrderedDict repr, a dict items view, a
    list of pairs, a tuple-keyed dict, a pandas Series repr), JSON encoded
    twice, a CSV round trip of a dict column, YAML, "manifest=" followed by
    JSON, a key in typographic quotes or backticks, a key in prose
    ("in_audit=True"), and any of these percent-encoded (urlencode, quote)
    or HTML-escaped; and so is text that holds the name in_audit itself as
    a word, or is that name, since the name became a value and the flag
    stands elsewhere without it (long format from DataFrame.melt, a
    key/value list, the "columns" of to_dict("split") or
    to_json(orient="split"), an XML tag or a plist key, a pickle, prose
    that names the flag, a text table whose header names it); whether held
    as str or as bytes (UTF-8, latin-1, UTF-16, UTF-32) or a memoryview of
    them (_text_mentions). An nct_id is a name that contains nct_id or "NCT
    Number" once so folded (nct_id_y or nct_id_m from a merge, nctNumber,
    manifest.nct_id), never nctIdAliases. Such a record is refused when any
    flag is True, when any is not exactly a bool, when the nct_id the entry
    point would stamp is not exactly a str (a subclass's __str__ or __eq__
    could say another id) matching ``^NCT[0-9]{8}$``, or when any other
    nct_id the record carries, at any depth, disagrees with it (a manifest
    joined by position brings another trial's id under nct_id_m). Output
    is stamped with the stored characters as a plain str (_plain_text),
    never with what a str subclass's __str__ says. Any
    record is refused when part of it cannot be read (fields without
    names, such as a plain tuple row or a namedtuple field renamed to "_N";
    a bool outside a named field, used as a key, or left as the row label
    of a Series row from ``iterrows()`` or ``apply(axis=1)`` or in the
    "Index" field of an ``itertuples()`` row, which is where
    ``set_index("in_audit")`` puts the flag; a one-shot iterator; an
    attribute that raises when read), since that part may hold a flag
    (_scan_in_audit). parse_outcomes_frame also refuses a batch in which
    only some studies carry ``in_audit``, or carry it under different
    names, and a single record (a sqlite3.Row among them) passed where a
    batch is expected. Every
    entry point also refuses a snapshot_date that is not a YYYY-MM-DD date
    and outcome_types outside CT.gov's vocabulary, so an audit id set
    passed positionally fails loudly. A bare CT.gov API v2 study dict
    carries no ``in_audit``; for it these four rely on their caller, as
    below. What the scan cannot see: the name written as separate words in
    a value ("flagged in audit", a text table whose header reads "In
    Audit" beside other columns), or run into a longer word as a value
    ("ISINAUDIT"; as a key it is read); a lookalike letter that _TO_ASCII
    does not list (this module may not import unicodedata, so the table is
    hand-written, not Unicode's confusables data); text that is
    compressed, base64-encoded or encrypted, or any binary serialization
    that writes the name in pieces; a function's globals; and anything a
    caller removed before the call.
  Rely on the caller (the input carries no ``in_audit`` and no entry point
  receives an audit id set):
      parse_error_trial_outcomes (an nct_id), parse_outcome_measure,
      extract_flow_started and align_flow_and_outcome_groups (fragments of
      one trial with no trial id), classify_population_description,
      normalize_population_text and population_audit (description text),
      structural_checks (parsed outputs, which carry no ``in_audit``).
  Callers today: none in code. The only importer in this repository is
  tests/test_analysis_population_parser.py. PR #97 (dc38220, on its own
  branch) reaches this module's output in two ways, and neither has a
  holdout backstop on the reading side:
    * scripts/build_outcomes_shards.py --population-parsed reads this
      module's serialized output from a file without importing it, and
      passes ``pop_by_nct.get(nct_id)`` to src/outcomes_layout.py
      build_trial_bundle as ``parsed_populations`` for every merged trial,
      holdouts included. Its cross-stream firewall
      (assert_no_audit_cross_stream_leakage) is called with the documents,
      locations and provenance streams only, never the population stream,
      and ``analysis_population_parsed`` is not in FORBIDDEN_AUDIT_KEYS.
    * build_trial_bundle(trial, in_audit=..., population_parser=...) hands
      an injected parser to extract_reported_results, which calls
      ``population_parser(study)`` for any trial with a resultsSection,
      holdouts included. If that parser is parse_study_outcomes, it refuses
      only when the trial mapping itself carries ``in_audit``;
      build_trial_bundle takes ``in_audit`` as a separate argument, so a
      trial mapping without the key is parsed in full.
  So whoever writes the --population-parsed file, or injects this parser,
  must leave holdout trials out. The backstop (refusing a holdout's
  population record and parser call) belongs on that consumer branch, and
  its test there: this module may not import it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable, Literal, Sequence

if TYPE_CHECKING:  # annotation only; pandas is imported lazily at run time
    import pandas as pd

__version__: str = "1.0.0"
# Bumped when a parsed field can change for the same input (2026-11-24: the
# class_level_incomplete denom status).
PARSER_RULES_VERSION: str = "analysis_pop@2026-11-24.v1"
POPULATION_PARSER_VERSION: str = PARSER_RULES_VERSION

PopulationClass = Literal[
    "itt",
    "modified_itt",
    "full_analysis_set",
    "per_protocol",
    "evaluable",
    "safety",
    "other_stated",
    "not_stated",
    "ambiguous_multi",
]

MatchTier = Literal["placeholder", "named_label", "definitional", "none"]
NotStatedReason = Literal["field_absent", "placeholder_token"]
DenomSource = Literal["measure", "class_unanimous", "none"]
DenomUnitKind = Literal["PARTICIPANTS", "COUNT_OF_UNITS", "BOTH", "NONE"]

POPULATION_TAXONOMY: tuple[PopulationClass, ...] = (
    "itt",
    "modified_itt",
    "full_analysis_set",
    "per_protocol",
    "evaluable",
    "safety",
    "other_stated",
    "not_stated",
    "ambiguous_multi",
)

_TAXONOMY_ORDER: dict[str, int] = {
    cls: idx for idx, cls in enumerate(POPULATION_TAXONOMY)
}

DENOM_STATUS_LEVELS: tuple[str, ...] = (
    "present",                          # valid numeric participant count(s) parsed
    "not_posted",                       # reportingStatus == "NOT_POSTED"
    "no_denoms_array",                  # denoms absent/empty on both measure and classes
    "units_only_no_participant_denom",  # non-participant units denom exists; no "Participants" denom
    "empty_counts",                     # "Participants" denom has counts == [] (incl. stripped search records)
    "non_numeric_or_na",                # counts exist, all values "NA"/""/unparseable
    "partial_arm_counts",               # >=1 non-total arm has numeric count and >=1 non-total arm is None/NA
    "class_level_only_varying",         # measure-level denoms absent; classes[].denoms vary across rows/timepoints
    "class_level_incomplete",           # measure-level denoms absent; some classes carry a "Participants" denom, others none
)

ARM_GAP_REASONS: tuple[str, ...] = (
    "computed",                         # started_participants - analyzed_participants computed
    "no_participant_flow",              # resultsSection.participantFlowModule absent
    "no_started_milestone",             # periods[0] lacks a STARTED milestone
    "started_count_missing_or_na",      # matched FG achievement numSubjects is missing/"NA"
    "analyzed_count_missing",           # OG arm analyzed_participants is None
    "total_group_not_arm",              # OG group is a Total/Overall summary column, not an arm
    "duplicate_group_titles",           # duplicate normalized arm titles in FG or OG prevent 1:1 join
    "group_count_or_title_mismatch",    # OG arm cannot be unambiguously aligned 1:1 to an FG arm
)

AGGREGATION_METHODS: tuple[str, ...] = (
    "single_group",                     # 1 OG group
    "sum_disjoint_arms",                # >=2 non-total OG arms summed (PRS does NOT auto-add Total in outcomes)
    "explicit_total_group_sumcheck",    # 1 Total-titled OG group == sum(other OG groups)
    "explicit_total_group_unverified",  # 1 Total-titled OG group != sum(other OG groups); flagged
    "pooled_group_sumcheck",            # >=3 OG groups where last pooled-titled group == sum(preceding arms)
    "none",                             # overall_participants_analyzed is None
)


# ---------------------------------------------------------------------------
# 0. Audit holdout refusal (in_audit)
# ---------------------------------------------------------------------------

# The only nct_id shape an enforcing entry point accepts. fullmatch (not
# match + "$", which also accepts a trailing newline), ASCII [0-9] (not \d,
# which also matches non-ASCII digits), no IGNORECASE, and no strip(): an id
# that needs normalising to look canonical could be a holdout id in disguise,
# so it is refused rather than repaired.
_CANONICAL_NCT_ID_RE: re.Pattern[str] = re.compile(r"NCT[0-9]{8}")

HOLDOUT_REFUSAL_REASONS: tuple[str, ...] = (
    "in_audit_true",       # the record says it is an audit holdout trial
    "in_audit_not_bool",   # in_audit is present but is not exactly True/False, or
                           # the record cannot be read well enough to tell
                           # (see _check_scanned_record)
    "nct_id_malformed",    # in_audit is False but the nct_id is not ^NCT[0-9]{8}$,
                           # or another nct_id in the record disagrees with it
)


class PopulationAuditHoldoutError(Exception):
    """Raised when an entry point is handed a record it must not parse.

    ``reason`` is one of HOLDOUT_REFUSAL_REASONS. Deliberately NOT a
    ValueError subclass: an ``except ValueError`` written for malformed
    registry input must not swallow a holdout refusal. Messages name the
    entry point and the value's type, never the nct_id or the description
    text, so the refusal itself records nothing about a holdout trial.
    """

    def __init__(self, reason: str, message: str) -> None:
        if reason not in HOLDOUT_REFUSAL_REASONS:
            raise ValueError(f"unknown holdout refusal reason: {reason!r}")
        super().__init__(f"[{reason}] {message}")
        self.reason: str = reason


def _is_canonical_nct_id(value: Any) -> bool:
    """True iff value is exactly a str (not a subclass) that is exactly ^NCT[0-9]{8}$.

    No coercion. A str subclass is refused even when its content is
    canonical: its __str__, __eq__ or __hash__ can say another id than the
    one checked (str() of it could stamp a holdout's id), so the id checked
    would not be the id stamped or compared.
    """
    return type(value) is str and _CANONICAL_NCT_ID_RE.fullmatch(value) is not None


def _plain_text(value: Any) -> str:
    """``value`` as the plain str it holds: a str subclass's content, else str(value).

    Used wherever an nct_id is stamped, so the output carries exactly the
    characters that were stored (and checked), never what a str subclass's
    __str__ says.
    """
    if isinstance(value, str):
        return str.__str__(value)
    return str(value)


# How a key or column name is folded before it is compared with "in_audit" or
# "nct_id". Assumption: a name that differs from those only by case, spacing,
# hyphens, dashes, dots or underscores (a camelCase merge, a spreadsheet
# header, a stray space, a dotted path), by full-width forms of ASCII, by
# letters that print like ASCII letters (_TO_ASCII: Cyrillic, Greek and
# Armenian lookalikes, dotless i, small capitals, Roman numerals, modifier
# and subscript letters, letterlike symbols, and the mathematical, circled,
# parenthesized, squared and regional-indicator alphabets), or by characters
# that print as nothing, was meant as that name. "Print as nothing" is _INVISIBLE_RE (the
# Unicode Default_Ignorable_Code_Point set: soft hyphen, combining grapheme
# joiner, Arabic letter mark, Hangul fillers, Mongolian free variation
# selectors and vowel separator, zero-width space/joiners, left-to-right and
# right-to-left marks, bidi embeddings, overrides and isolates, word joiner,
# invisible operators, variation selectors, BOM, tag characters; plus the
# combining-mark blocks, lone surrogates and the braille blank) and, through
# str.isprintable(), every other format, control, private-use or unassigned
# character that is not whitespace. Treating such a name as in_audit is
# fail-closed: an unrecognised holdout flag is the failure this guards
# against, while a false match can only refuse a record. This module keeps to
# the standard-library imports its tests pin, which exclude unicodedata, so
# lookalikes and compatibility forms are folded by the hand-written table
# _TO_ASCII (built by _ascii_lookalikes), not by Unicode's confusables or
# NFKC data: a lookalike that table does not list is not folded. Twin of the
# same rule in sponsors/ecosystem.py; the two modules import nothing from
# each other.
_INVISIBLE_RE: re.Pattern[str] = re.compile(
    "["
    "\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f"
    "\u202a-\u202e\u2060-\u206f\u2800\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8"
    "\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f"
    "\ud800-\udfff"
    "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff"
    "]+"
)
# Separators a name is folded without: whitespace, underscore, dot (a
# json_normalize path, "in.audit"), hyphen, the Unicode dashes, the minus
# sign and the small hyphen-minus.
_NAME_IGNORED_RE: re.Pattern[str] = re.compile(r"[\s_.\-\u2010-\u2015\u2212\ufe63]+")
_SPACE_RE: re.Pattern[str] = re.compile(r"\s+")
_FULLWIDTH_TO_ASCII: dict[int, int] = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
# (lookalike, the ASCII character it imitates) pairs, written out because
# unicodedata may not be imported: letters that print like Latin ones
# (Cyrillic, Greek, Armenian, Latin letters outside ASCII, small capitals,
# Roman numerals, letterlike symbols, modifier and subscript letters), and
# the typographic quotes, primes and accent that stand where a key's quote
# would (a word processor's or Markdown's), mapped to ASCII " and '.
_LOOKALIKE_PAIRS: str = (
    # Cyrillic capitals
    "\u0410A\u0412B\u0415E\u0405S\u0406I\u0408J\u041aK\u041cM"
    "\u041dH\u041eO\u0420P\u0421C\u0422T\u0423Y\u0425X\u04aeY"
    "\u04baH\u04c0I\u051aQ\u051cW"
    # Cyrillic small letters
    "\u0430a\u0435e\u043eo\u0440p\u0441c\u0443y\u0445x\u0455s"
    "\u0456i\u0458j\u04bbh\u0501d\u051bq\u051dw\u04cfl\u04afy"
    "\u0475v"
    # Greek capitals
    "\u0391A\u0392B\u0395E\u0396Z\u0397H\u0399I\u039aK\u039cM"
    "\u039dN\u039fO\u03a1P\u03a4T\u03a5Y\u03a7X\u03f9C\u037fJ"
    # Greek small letters
    "\u03b1a\u03b9i\u03bfo\u03bdv\u03c5u\u03c1p\u03bak\u03c4t"
    "\u03f2c\u03f3j"
    # dotless i and j, dotted capital I, Latin and IPA letters, small capitals
    "\u0131i\u0237j\u0130I\u0251a\u0261g\u0269i\u026ai\u029fl"
    "\u0274n\u0280r\u028fy\u1d00a\u1d04c\u1d05d\u1d07e\u1d0aj"
    "\u1d0bk\u1d0dm\u1d0fo\u1d18p\u1d1bt\u1d1cu\u1d20v\u1d21w"
    "\u1d22z\ua731s"
    # Armenian
    "\u0578n\u057du\u0585o\u0570h\u0555O\u054dU"
    # Roman numerals
    "\u2160I\u2164V\u2169X\u216cL\u216dC\u216eD\u216fM\u2170i"
    "\u2174v\u2179x\u217cl\u217dc\u217ed\u217fm"
    # letterlike symbols
    "\u210eh\u210ag\u2113l\u212fe\u2134o\u2110I\u2111I\u2112L"
    "\u211bR\u211cR\u211dR\u212cB\u2130E\u2131F\u2133M\u2102C"
    "\u210dH\u2115N\u2119P\u211aQ\u2124Z\u212dC\u210cH\u2128Z"
    "\u2148i\u2149j\u2146d\u2147e\u2145D"
    # modifier (superscript) letters
    "\u1d43a\u1d47b\u1d9cc\u1d48d\u1d49e\u1da0f\u1d4dg\u02b0h"
    "\u2071i\u02b2j\u1d4fk\u02e1l\u1d50m\u207fn\u1d52o\u1d56p"
    "\u02b3r\u02e2s\u1d57t\u1d58u\u1d5bv\u02b7w\u02e3x\u02b8y"
    "\u1dbbz"
    # subscript letters
    "\u2090a\u2091e\u2095h\u1d62i\u2c7cj\u2096k\u2097l\u2098m"
    "\u2099n\u2092o\u209ap\u1d63r\u209bs\u209ct\u1d64u\u1d65v"
    "\u2093x"
    # typographic double quotes and primes
    "\u201c\"\u201d\"\u201e\"\u201f\"\u2033\"\u2036\"\u301d\""
    "\u301e\"\u301f\""
    # typographic single quotes, primes and the acute accent
    "\u2018'\u2019'\u201a'\u201b'\u2032'\u2035'\u00b4'"
)


def _ascii_lookalikes() -> dict[int, int]:
    """Code point -> the ASCII code point it prints like (a str.translate table).

    Method: full-width ASCII (_FULLWIDTH_TO_ASCII); the written-out pairs
    of _LOOKALIKE_PAIRS; superscript and subscript digits; and, by
    arithmetic on their fixed layouts, the compatibility alphabets: the 13
    mathematical alphabets (from U+1D400, each A-Z then a-z; the reserved
    holes among them map too, harmlessly, since they are unassigned), the
    mathematical dotless i and j, the 5 mathematical digit styles (from
    U+1D7CE), circled letters (U+24B6, U+24D0), parenthesized small letters
    (U+249C), squared, negative circled and negative squared capitals
    (U+1F130, U+1F150, U+1F170), regional indicator letters (U+1F1E6), and
    circled, parenthesized, full-stop and dingbat digits (one to nine, and
    the circled zeros).
    """
    upper = list(range(0x41, 0x5B))
    lower = list(range(0x61, 0x7B))
    digits = list(range(0x31, 0x3A))  # 1-9: the numbered forms start at one
    table: dict[int, int] = dict(_FULLWIDTH_TO_ASCII)
    for pos in range(0, len(_LOOKALIKE_PAIRS), 2):
        table[ord(_LOOKALIKE_PAIRS[pos])] = ord(_LOOKALIKE_PAIRS[pos + 1])
    for offset, code in enumerate((0x2070, 0xB9, 0xB2, 0xB3, *range(0x2074, 0x207A))):
        table[code] = 0x30 + offset  # superscript 0-9
    for offset in range(10):
        table[0x2080 + offset] = 0x30 + offset  # subscript 0-9
    for base in range(0x1D400, 0x1D6A4, 52):
        for offset, code in enumerate(upper + lower):
            table[base + offset] = code
    table[0x1D6A4] = ord("i")
    table[0x1D6A5] = ord("j")
    for base in range(0x1D7CE, 0x1D800, 10):
        for offset in range(10):
            table[base + offset] = 0x30 + offset
    alphabets = (
        (0x24B6, upper), (0x24D0, lower), (0x249C, lower), (0x1F130, upper),
        (0x1F150, upper), (0x1F170, upper), (0x1F1E6, upper),
        (0x2460, digits), (0x2474, digits), (0x2488, digits),
        (0x2776, digits), (0x2780, digits), (0x278A, digits),
    )
    for base, letters in alphabets:
        for offset, code in enumerate(letters):
            table[base + offset] = code
    for zero in (0x24EA, 0x24FF, 0x1F10B, 0x1F10C):  # circled and dingbat zeros
        table[zero] = 0x30
    return table


def _char_class(codes: Iterable[int]) -> str:
    """A regex character class matching exactly the code points ``codes``, as ranges."""
    ordered = sorted(set(codes))
    parts: list[str] = []
    start = prev = ordered[0]
    for code in ordered[1:] + [-1]:
        if code == prev + 1:
            prev = code
            continue
        parts.append(f"\\U{start:08x}" if start == prev else f"\\U{start:08x}-\\U{prev:08x}")
        start = prev = code
    return "[" + "".join(parts) + "]"


_TO_ASCII: dict[int, int] = _ascii_lookalikes()
# Matches any character _TO_ASCII maps, so text without one is not copied.
_TO_ASCII_RE: re.Pattern[str] = re.compile(_char_class(_TO_ASCII))
# ASCII control characters that are not whitespace, and DEL.
_ASCII_CONTROLS: dict[int, None] = {
    code: None for code in (*range(0x20), 0x7F) if not chr(code).isspace()
}
_ASCII_CONTROL_RE: re.Pattern[str] = re.compile(
    "[" + "".join(f"\\x{code:02x}" for code in _ASCII_CONTROLS) + "]"
)
# The bytes an ASCII text is folded without (whitespace, "_", ".", "-", and
# the controls above): the byte-level twin of _NAME_IGNORED_RE plus
# _visible_text, for the fast in_audit test on ASCII text (_may_name_in_audit).
_ASCII_FOLD_DELETE: bytes = bytes(
    code for code in range(0x80)
    if chr(code).isspace() or chr(code) in "_.-" or code in _ASCII_CONTROLS
)
_IN_AUDIT_NAME: str = "inaudit"
_NCT_ID_NAMES: tuple[str, ...] = ("nctid", "nctnumber")
_NCT_ID_EXCLUDED: str = "alias"
# Folded names, memoised (names repeat across records); short texts only.
_FOLD_CACHE: dict[str, str] = {}
_NAME_KIND_CACHE: dict[str, tuple[bool, bool]] = {}
_FOLD_CACHE_LIMIT: int = 65536
_FOLD_CACHE_MAX_LEN: int = 256
# Stands for "no field name" among a node's (name, value) pairs: the value is
# read as one reached by position.
_NO_NAME: object = object()


def _visible_text(text: str) -> str:
    """``text`` with lookalikes mapped to ASCII and invisible characters dropped.

    Whitespace is kept: it separates the words and keys of text. Method:
    pure-ASCII text loses only its control characters; other text has
    every character _TO_ASCII lists (full-width forms, letters that print
    like ASCII letters, compatibility alphabets, typographic quotes) mapped
    to the ASCII character it prints like, then loses _INVISIBLE_RE's
    characters and then, when anything that is neither printable nor
    whitespace is left, each such character (a format, control,
    private-use or unassigned code point, by str.isprintable()). Each step
    first searches, so text it would not change is not copied.
    """
    if text.isascii():
        if _ASCII_CONTROL_RE.search(text) is None:
            return text
        return text.translate(_ASCII_CONTROLS)
    if _TO_ASCII_RE.search(text) is not None:
        text = text.translate(_TO_ASCII)
    if _INVISIBLE_RE.search(text) is not None:
        text = _INVISIBLE_RE.sub("", text)
    if not text.isprintable() and not _SPACE_RE.sub(" ", text).isprintable():
        text = "".join(ch for ch in text if ch.isprintable() or ch.isspace())
    return text


def _may_name_in_audit(text: str) -> bool:
    """True when visible ``text``, folded whole, contains "inaudit" (a cheap necessary test).

    Every key _text_keys reads is a stretch of the text (or of a later
    layer, which is tested on its own), and folding removes characters one
    by one, so a text whose folded whole lacks "inaudit" holds no key that
    reads as in_audit. ASCII text is folded at the byte level
    (_ASCII_FOLD_DELETE, then lower()), which costs two C passes; other text
    by _fold_text.
    """
    if text.isascii():
        return b"inaudit" in text.encode("ascii").translate(None, _ASCII_FOLD_DELETE).lower()
    return _IN_AUDIT_NAME in _fold_text(text)


def _fold_text(text: str) -> str:
    """``text`` folded for comparison with a name: visible, separators dropped, casefolded.

    Method: _visible_text, then _NAME_IGNORED_RE's separators are removed
    and the result casefolded. casefold() can itself add a combining mark
    ("İ" folds to "i" and a dot above), so _INVISIBLE_RE's characters are
    removed from non-ASCII output once more. Short texts are memoised.
    """
    cacheable = type(text) is str and len(text) <= _FOLD_CACHE_MAX_LEN
    if cacheable:
        cached = _FOLD_CACHE.get(text)
        if cached is not None:
            return cached
    folded = _NAME_IGNORED_RE.sub("", _visible_text(text)).casefold()
    if not folded.isascii() and _INVISIBLE_RE.search(folded) is not None:
        folded = _INVISIBLE_RE.sub("", folded)
    if cacheable:
        if len(_FOLD_CACHE) >= _FOLD_CACHE_LIMIT:
            _FOLD_CACHE.clear()
        _FOLD_CACHE[text] = folded
    return folded


def _decoded_texts(value: Any) -> list[str]:
    """The text(s) a str, bytes, bytearray or memoryview is read as.

    A str is itself. Bytes (a memoryview is read as the bytes it exposes)
    are read as UTF-8 with undecodable bytes dropped and as latin-1, which
    never fails, and, when they hold a NUL byte (what UTF-16 and UTF-32 make
    of ASCII), as UTF-16 and UTF-32 of either byte order too, so no encoding
    a manifest is likely written in hides a key. A BOM decodes to U+FEFF,
    which _visible_text drops.
    """
    if isinstance(value, str):
        return [value]
    data = bytes(value)
    texts = [data.decode("utf-8", "ignore"), data.decode("latin-1")]
    if b"\x00" in data:
        texts.extend(
            data.decode(codec, "ignore")
            for codec in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
        )
    return texts


def _name_texts(name: Any) -> list[str]:
    """The text(s) a key, field or label name is read as; [] when it has none.

    Method: text is itself and a bytes-like name is decoded
    (_decoded_texts); a tuple name (a MultiIndex label, a tuple key) is read
    part by part. None, _NO_NAME, a bool and a plain number (exactly int,
    float or complex) have no text. Any other object is read as its own
    text ``name`` when it has one (an enum member's: an IntEnum or IntFlag
    member is an int whose str() is only its number, "1", so its name is
    the one text that says what it is), and then, unless it is a number,
    as its str() and, when it has a text ``value`` (a plain Enum member's),
    as that too: str(Key.IN_AUDIT) is "Key.IN_AUDIT". So an enum key or
    label that names in_audit reads as it whatever it holds.
    """
    if name is None or name is _NO_NAME or _is_bool_like(name):
        return []
    if isinstance(name, (str, bytes, bytearray, memoryview)):
        return _decoded_texts(name)
    if isinstance(name, tuple):
        return [text for part in name for text in _name_texts(part)]
    if type(name) in (int, float, complex):
        return []
    texts: list[str] = []
    own_name = getattr(name, "name", None)
    if isinstance(own_name, (str, bytes, bytearray)):
        texts.extend(_decoded_texts(own_name))
    if isinstance(name, (int, float, complex)):
        return texts  # a number subclass: its str() is a number, not a name
    texts.append(str(name))
    value = getattr(name, "value", None)
    if isinstance(value, (str, bytes, bytearray)):
        texts.extend(_decoded_texts(value))
    return texts


def _name_kind(name: Any) -> tuple[bool, bool]:
    """(reads as in_audit, reads as nct_id) for ``name``, each of its texts folded once.

    in_audit: "inaudit" anywhere in a folded text, so a name that qualifies
    in_audit reads as it: pandas merge suffixes (in_audit_x, in_audit_y,
    in_audit_manifest), a json_normalize path (manifest.in_audit),
    DataFrame.add_prefix (manifest_in_audit) and an "is" prefix
    (is_in_audit, isInAudit). A substring, not a split into components,
    because folding has already removed the separators a split would need
    ("ISINAUDIT"). Assumption: no CT.gov API v2 field name, retained-record
    key or sponsor column contains "audit", so a name that merely contains
    the letters ("domain_audit") is read as a flag too; fail-closed, since a
    false match can only refuse a record.

    nct_id: a folded text that contains "nctid" or "nctnumber" and not
    "alias". So nct_id itself, a json_normalize path (manifest.nct_id),
    DataFrame.add_prefix (manifest_nct_id), the pandas merge suffixes
    (nct_id_x, nct_id_y, nct_id_m, nct_id_manifest: a manifest joined by
    position brings its own id under such a name) and CT.gov's CSV export
    header ("NCT Number", nctNumber) are ids, while CT.gov's
    identificationModule.nctIdAliases (a trial's obsolete ids, folded
    "nctidaliases") is not the trial's own id. Assumption: no other CT.gov
    API v2 field, retained-record key or sponsor column has "nctid" or
    "nctnumber" in its name; one that did would be compared with the
    stamped id, which can only refuse a record.
    """
    if type(name) is str:  # the common case: one text, memoised
        kind = _NAME_KIND_CACHE.get(name)
        if kind is not None:
            return kind
    in_audit = nct_id = False
    for text in _name_texts(name):
        folded = _fold_text(text)
        in_audit = in_audit or _IN_AUDIT_NAME in folded
        nct_id = nct_id or (
            _NCT_ID_EXCLUDED not in folded and any(part in folded for part in _NCT_ID_NAMES)
        )
    if type(name) is str and len(name) <= _FOLD_CACHE_MAX_LEN:
        if len(_NAME_KIND_CACHE) >= _FOLD_CACHE_LIMIT:
            _NAME_KIND_CACHE.clear()
        _NAME_KIND_CACHE[name] = (in_audit, nct_id)
    return in_audit, nct_id


def _folded_name(name: Any) -> str | None:
    """``name`` folded for comparison (_fold_text of each _name_texts, joined by "|").

    None when the name has no text (None, a bool, a number).
    """
    texts = _name_texts(name)
    if not texts:
        return None
    return "|".join(_fold_text(text) for text in texts)


def _reads_as_in_audit(name: Any) -> bool:
    """True when ``name`` (or any part of a tuple name) reads as in_audit (see _name_kind)."""
    return _name_kind(name)[0]


def _reads_as_nct_id(name: Any) -> bool:
    """True when ``name`` (or any part of a tuple name) reads as nct_id (see _name_kind)."""
    return _name_kind(name)[1]


def _is_text_name(name: Any) -> bool:
    """True when ``name`` is text (str or bytes), or a tuple name with a text part."""
    if isinstance(name, tuple):
        return any(_is_text_name(part) for part in name)
    return isinstance(name, (str, bytes, bytearray))


def _holds_bool(name: Any) -> bool:
    """True when ``name`` is a bool, or a tuple name holding one (a MultiIndex label).

    A bool used as a key or label is a flag value that became a name
    (``set_index("in_audit")``, ``to_dict("index")``), so it cannot be read.
    """
    if type(name) is str:
        return False
    if isinstance(name, tuple):
        return any(_holds_bool(part) for part in name)
    return _is_bool_like(name)


# Text that serializes a container: what json.dumps, the repr() of a dict or
# list, and a CSV round trip of a dict column leave ("{...}", "[...]"), and
# the repr() of a namedtuple, SimpleNamespace, deque or OrderedDict
# ("Row(...)"). Read after _visible_text, so full-width forms are ASCII.
_SERIALIZED_CLOSERS: dict[str, str] = {"{": "}", "[": "]"}
_REPR_HEAD_RE: re.Pattern[str] = re.compile(r"[^\W\d][\w.]*\(")
# A quoted string (double, single or back quotes, backslash escapes inside).
# Typographic quotes have been mapped to ASCII ones by _visible_text; a
# backtick quotes a name in Markdown and in SQL.
_QUOTE_CHARS: str = "\"'`"
_QUOTED: str = r""""(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`"""
_QUOTED_RE: re.Pattern[str] = re.compile(_QUOTED, re.DOTALL)
# The characters of a bare key besides \w: the dot of a json_normalize path
# and the hyphens and dashes names are folded without.
_BARE_KEY_CHARS: str = r"\w.\-\u2010-\u2015\u2212\ufe63"
# Where text holds a key:
#   * a quoted string, or a run of key characters that does not continue a
#     longer run, followed by ":" or "=" ('in_audit': True, "in_audit":
#     true, in_audit=True, a YAML line "in_audit: true");
#   * a quoted string that opens a bracketed pair ("('in_audit', True)",
#     '["in_audit", true]': Python 3.11's OrderedDict repr, a dict items
#     view's repr, a JSON list of pairs);
#   * the quoted parts of a tuple followed by ":" or "=" (the repr of a
#     dict with tuple keys).
# The words of prose are not keys: in "[Practices in audit arm: ...]" only
# "arm" stands before its colon, and in '{"note": "in audit arm"}' the
# words are a value.
_KEY_RE: re.Pattern[str] = re.compile(
    rf"({_QUOTED})\s*[:=]|(?<![{_BARE_KEY_CHARS}])([{_BARE_KEY_CHARS}]+)\s*[:=]",
    re.DOTALL,
)
_PAIR_KEY_RE: re.Pattern[str] = re.compile(rf"[(\[]\s*({_QUOTED})\s*,", re.DOTALL)
_TUPLE_KEY_RE: re.Pattern[str] = re.compile(
    rf"\(\s*(?:{_QUOTED})(?:\s*,\s*(?:{_QUOTED}))*\s*,?\s*\)\s*[:=]", re.DOTALL
)
# The last line of a pandas Series' repr ("dtype: object", "Name: x, dtype:
# bool", "Name: x, Length: 9, dtype: object"), and the index label that
# starts each line above it (followed by two or more spaces, then the value).
_SERIES_FOOTER_RE: re.Pattern[str] = re.compile(
    r"(?:Name: .*, )?(?:Freq: \S+, )?(?:Length: \d+, )?dtype: \S+"
)
_SERIES_LABEL_RE: re.Pattern[str] = re.compile(r"\s*(\S(?:.*?\S)?)(?:\s{2,}|\s*$)")
# Backslash escapes (_unescaped).
_ESCAPE_RE: re.Pattern[str] = re.compile(
    r"\\(?:u([0-9a-fA-F]{4})|U([0-9a-fA-F]{8})|x([0-9a-fA-F]{2})|(N\{[^{}\\]{0,100}\})|(.))",
    re.DOTALL,
)
_SIMPLE_ESCAPES: dict[str, str] = {
    "n": "\n", "t": "\t", "r": "\r", "f": "\f", "v": "\v", "b": "\b", "a": "\a", "0": "\0",
}
_SURROGATE_RE: re.Pattern[str] = re.compile("[\ud800-\udfff]")
# How many layers text may be read as, its encodings undone one at a time
# (_text_layers), before it is given up on; text still giving new layers
# after that is read as a flag.
_MAX_TEXT_LAYERS: int = 16
# A run of percent-encoded bytes, as urllib.parse.quote and urlencode write
# them (":" as %3A, '"' as %22) (_percent_decoded).
_PERCENT_RE: re.Pattern[str] = re.compile(r"(?:%[0-9A-Fa-f]{2})+")
# A character reference, as html.escape and XML writers write them: decimal
# or hex (the ";" optional, as HTML parsers accept), or named (_entities_decoded).
_ENTITY_RE: re.Pattern[str] = re.compile(
    r"&(?:#([0-9]{1,8});?|#[xX]([0-9A-Fa-f]{1,8});?|([A-Za-z][A-Za-z0-9]{1,31});)"
)
# The named references that write ASCII punctuation, quotes and the space
# (HTML5 names; html.escape writes only quot, amp, lt and gt, and #x27).
_NAMED_ENTITIES: dict[str, str] = {
    "quot": '"', "apos": "'", "amp": "&", "lt": "<", "gt": ">", "nbsp": " ",
    "colon": ":", "equals": "=", "comma": ",", "semi": ";", "period": ".",
    "lowbar": "_", "underbar": "_", "hyphen": "-", "dash": "-", "grave": "`",
    "lsquo": "'", "rsquo": "'", "ldquo": '"', "rdquo": '"', "lpar": "(",
    "rpar": ")", "lsqb": "[", "rsqb": "]", "lbrack": "[", "rbrack": "]",
    "lcub": "{", "rcub": "}", "lbrace": "{", "rbrace": "}", "sol": "/",
    "bsol": "\\", "num": "#", "excl": "!", "quest": "?",
}
# Control characters (C0 other than tab, newline and the like, DEL, C1).
# _visible_text drops them, which can join the words either side of one;
# the in_audit word rule reads them as spaces instead (a pickle writes each
# key between opcode bytes) (_names_in_audit_as_a_value).
_CONTROL_CHARS_RE: re.Pattern[str] = re.compile("[\x00-\x08\x0e-\x1f\x7f-\x9f]")
# The name in_audit standing as a word of text, where a value holds it once
# the name became a value (a melted manifest's "variable", a key/value
# list's "key", a "columns" list, an XML tag, a plist <key>): "in" in any
# case where a word starts (after no letter), or "In" after a small letter
# (camelCase, isInAudit), or "in" after a capital (a pickle opcode letter
# before it, "Vin_audit"); then any underscores, dots, hyphens or dashes;
# then "audit". Read on visible text, so lookalike letters are ASCII. Words
# of prose stay apart: "in audit" has a space between them, and in
# "domain_audit" or "MAIN-AUDIT" the "in" ends a longer word.
_IN_AUDIT_WORD_RE: re.Pattern[str] = re.compile(
    r"(?:(?<![^\W\d_])|(?<=[a-z])(?=I)|(?<=[A-Z])(?=i))"
    r"[iI][nN][_.\-\u2010-\u2015\u2212\ufe63]*[aA][uU][dD][iI][tT]"
)


def _escape_value(match: re.Match[str]) -> str:
    r"""The text one backslash escape matched by _ESCAPE_RE stands for.

    \uXXXX, \UXXXXXXXX and \xXX give their character (one beyond U+10FFFF,
    nothing). \N{name} gives nothing: this module cannot look names up
    (no unicodedata), and neither JSON nor repr() writes one, so only
    hand-written text holds it; dropping it reads a key that hid an
    invisible character behind it. \n, \t and the other one-letter escapes
    give theirs; any other escaped character is itself (\" gives ").
    """
    code = match.group(1) or match.group(2) or match.group(3)
    if code is not None:
        value = int(code, 16)
        return chr(value) if value <= 0x10FFFF else ""
    if match.group(4) is not None:
        return ""
    other = match.group(5)
    return _SIMPLE_ESCAPES.get(other, other)


def _unescaped(text: str) -> str:
    """``text`` with backslash escapes decoded (_escape_value), as JSON or a literal writes them.

    Decoded by pattern, not by a JSON or literal parser (this module calls
    none), so text that is not valid JSON or Python is decoded as far as it
    goes. A surrogate pair (how JSON writes a character beyond U+FFFF) is
    joined; a lone surrogate is dropped.
    """
    decoded = _ESCAPE_RE.sub(_escape_value, text)
    if _SURROGATE_RE.search(decoded):
        decoded = decoded.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "ignore")
    return decoded


def _percent_run(match: re.Match[str]) -> str:
    """One run of %XX bytes matched by _PERCENT_RE, decoded as UTF-8 (undecodable bytes dropped)."""
    return bytes.fromhex(match.group(0).replace("%", "")).decode("utf-8", "ignore")


def _percent_decoded(text: str) -> str:
    """``text`` with its percent-encoded bytes decoded and "+" read as a space.

    Decoded by pattern (this module may not import urllib). "+" is a space
    in a form-encoded query (urlencode), and reading every "+" of text that
    holds percent escapes as a space can only make a key more legible.
    """
    return _PERCENT_RE.sub(_percent_run, text).replace("+", " ")


def _entity_value(match: re.Match[str]) -> str:
    """The text one character reference matched by _ENTITY_RE stands for.

    A numeric reference gives its character (nothing for a surrogate or one
    beyond U+10FFFF); a named one in _NAMED_ENTITIES (any case) gives its
    character; any other is left as written.
    """
    number = match.group(1)
    hex_number = match.group(2)
    if number is not None or hex_number is not None:
        code = int(number) if number is not None else int(hex_number, 16)
        return chr(code) if code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else ""
    return _NAMED_ENTITIES.get(match.group(3).lower(), match.group(0))


def _entities_decoded(text: str) -> str:
    """``text`` with its character references decoded by pattern (_entity_value; no html import)."""
    return _ENTITY_RE.sub(_entity_value, text)


def _layer_successors(layer: str) -> list[str]:
    """What visible text ``layer`` reads as once one encoding is undone, made visible and stripped.

    Each reading is made only when ``layer`` shows its encoding:
      * unquoted and unescaped: one pair of matching outer quotes removed
        (JSON encoded twice, or the repr() of a JSON string, is one quoted
        string) and backslash escapes decoded (_unescaped: json.dumps writes
        every non-ASCII character of a key as \\uXXXX and repr() every
        unprintable one as \\xXX or \\uXXXX);
      * percent-decoded (_percent_decoded: urllib.parse.quote and urlencode
        write ":" as %3A and '"' as %22);
      * reference-decoded (_entities_decoded: html.escape writes '"' as
        &quot;, so ";" would stand before a key's ":").
    A reading that equals ``layer`` stripped, or another reading, is left out.
    """
    current = layer.strip()
    readings: list[str] = []
    quoted = len(current) >= 2 and current[0] == current[-1] and current[0] in _QUOTE_CHARS
    if quoted or "\\" in current:
        inner = current[1:-1] if quoted else current
        readings.append(_unescaped(inner) if "\\" in inner else inner)
    if "%" in current and _PERCENT_RE.search(current) is not None:
        readings.append(_percent_decoded(current))
    if "&" in current and _ENTITY_RE.search(current) is not None:
        readings.append(_entities_decoded(current))
    successors: list[str] = []
    for reading in readings:
        visible = _visible_text(reading).strip()
        if visible != current and visible not in successors:
            successors.append(visible)
    return successors


def _text_layers(text: str) -> tuple[list[str], bool]:
    """``text`` made visible, then every text it reads as with its encodings undone, as layers.

    Returns (layers, settled). Method: the first layer is _visible_text of
    ``text``; each reading of a layer (_layer_successors: unquoted and
    unescaped, percent-decoded, reference-decoded) not seen before is a
    further layer, itself read the same way, breadth first, so a manifest
    JSON-encoded, then percent-encoded, then quoted is read at every stage.
    Layers stop when no reading is new (settled) or when there would be
    more than _MAX_TEXT_LAYERS (not settled).
    """
    layers = [_visible_text(text)]
    seen = {layers[0].strip()}
    pos = 0
    while pos < len(layers):
        for reading in _layer_successors(layers[pos]):
            if reading in seen:
                continue
            if len(layers) >= _MAX_TEXT_LAYERS:
                return layers, False
            seen.add(reading)
            layers.append(reading)
        pos += 1
    return layers, True


def _serializes_a_container(text: str) -> bool:
    """True when visible ``text``, stripped, opens and closes like a serialized container.

    ``text`` has been through _visible_text (invisible characters dropped,
    full-width forms mapped to ASCII), so stripping whitespace trims its
    edges. Only the edges are read: text whose last character is a letter
    or digit is let go at once. Assumption: free text (a population
    description, a sponsor name) does not both open with "{" or "[" and
    close with the matching bracket, nor open with an identifier followed
    by "(" and close with ")"; text that does may have nct_id keys read in
    it (_text_mentions).
    """
    inner = text.strip()
    if len(inner) < 2 or inner[-1].isalnum():
        return False
    if inner[0] in _SERIALIZED_CLOSERS:
        return _SERIALIZED_CLOSERS[inner[0]] == inner[-1]
    return inner[-1] == ")" and _REPR_HEAD_RE.match(inner) is not None


def _series_repr_labels(text: str) -> list[str]:
    """The index labels of a pandas Series' repr, or [] when visible ``text`` is not one.

    Method: the last non-blank line is the footer repr() ends with
    (_SERIES_FOOTER_RE). Each line above it gives two readings of its
    label: its text up to the first run of two or more spaces
    (_SERIES_LABEL_RE), and the line without its last word (the value),
    so a label with two spaces inside is still read whole.
    """
    if "dtype:" not in text:
        return []
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if len(lines) < 2 or _SERIES_FOOTER_RE.fullmatch(lines[-1].strip()) is None:
        return []
    labels: list[str] = []
    for line in lines[:-1]:
        match = _SERIES_LABEL_RE.match(line)
        labels.append(match.group(1) if match else line.strip())
        words = line.split()
        if len(words) > 1:
            labels.append(" ".join(words[:-1]))
    return labels


def _text_keys(layer: str) -> list[str]:
    """Every key visible text ``layer`` holds, unquoted and unescaped.

    The keys of _KEY_RE (before ":" or "="), _PAIR_KEY_RE (first item of a
    bracketed pair) and _TUPLE_KEY_RE (the parts of a tuple key), and the
    labels of a pandas Series repr (_series_repr_labels).
    """
    raw: list[str] = []
    for match in _KEY_RE.finditer(layer):
        raw.append(match.group(1) if match.group(1) is not None else match.group(2))
    raw.extend(match.group(1) for match in _PAIR_KEY_RE.finditer(layer))
    for match in _TUPLE_KEY_RE.finditer(layer):
        raw.extend(part.group(0) for part in _QUOTED_RE.finditer(match.group(0)))
    keys: list[str] = []
    for key in raw:
        if len(key) >= 2 and key[0] == key[-1] and key[0] in _QUOTE_CHARS:
            key = key[1:-1]
        keys.append(_unescaped(key) if "\\" in key else key)
    return keys + _series_repr_labels(layer)


def _names_in_audit_as_a_value(decoded: str, layers: list[str]) -> bool:
    """True when text holds the name in_audit as a value: a word of it, or all of it.

    Method: each layer (_text_layers), and ``decoded`` with its control
    characters read as spaces (_CONTROL_CHARS_RE), is tested with
    _IN_AUDIT_WORD_RE, and each layer as a whole, folded (_fold_text), is
    compared with "inaudit" (a spreadsheet header "In Audit" melted into a
    value). A text whose folded whole lacks "inaudit" is let go after the
    cheap test (_may_name_in_audit). So the name is read where a value
    holds it: a melted manifest's "variable", a key/value list's "key", a
    "columns" list (to_dict("split"), to_json(orient="split")), an XML tag
    or plist <key> (ElementTree.tostring, plistlib), a pickle, and also
    prose that names the flag ("flagged in_audit") or a text table whose
    header does (to_csv, to_string, Markdown). Assumption: no CT.gov API v2
    text, retained-record value or sponsor name holds such a word (the
    words "in audit" apart, or an "in" ending a longer word, are not one);
    fail-closed, since a false match can only refuse a record.
    """
    candidates = list(layers)
    if _CONTROL_CHARS_RE.search(decoded) is not None:
        candidates.append(_visible_text(_CONTROL_CHARS_RE.sub(" ", decoded)))
    for layer in candidates:
        if not _may_name_in_audit(layer):
            continue
        if _IN_AUDIT_WORD_RE.search(layer) is not None:
            return True
        if _fold_text(layer.strip()) == _IN_AUDIT_NAME:
            return True
    return False


def _text_mentions(text: Any, as_value: bool = True) -> tuple[bool, bool]:
    """(mentions in_audit, mentions nct_id) for a str, bytes, bytearray or memoryview.

    ``as_value`` is False for text that is itself a key or field name: its
    name is read by _name_kind, so only a manifest serialized inside it
    counts here, not the name in_audit it may spell.

    Method: text is not parsed (this module calls no parser of JSON, Python
    literals, URLs, HTML or XML). It is decoded (_decoded_texts) and read
    layer by layer (_text_layers: unquoted, unescaped, percent-decoded,
    reference-decoded), and each layer's keys (_text_keys: a key before ":"
    or "=", the first item of a bracketed pair, the parts of a tuple key,
    the labels of a pandas Series repr) are read with the name rules
    (_name_kind), so the same names count in text as in a dict.
      * A key that reads as in_audit is a mention wherever the text holds
        it: a JSON or repr manifest (escaped, encoded twice, as pairs, its
        key in typographic quotes or backticks), a YAML line, "manifest="
        followed by JSON, a Series repr, a key in prose such as
        "in_audit=True", and any of those percent-encoded (urlencode,
        quote) or HTML-escaped. A layer whose folded text (_fold_text) does
        not contain "inaudit" cannot hold one and is let go after a few
        string passes. Text still giving new layers after _MAX_TEXT_LAYERS
        is read as a mention (fail-closed).
      * As a value (``as_value``), text that holds the name in_audit as a
        word, or is that name, is a mention too
        (_names_in_audit_as_a_value): the name became a value (long
        format, key/value lists, XML, plist, pickle), and the flag stands
        elsewhere, unnamed.
      * A key that reads as nct_id is a mention only in a layer that
        serializes a container (_serializes_a_container) or is a Series
        repr, so a sentence such as "see nctId: NCT..." in a description
        does not make a flagged record's ids disagree.
    Words that are neither keys nor the name give (False, False): "Not in
    audit" and "[Practices in audit arm: ...]" mention nothing.
    """
    if not isinstance(text, (str, bytes, bytearray, memoryview)):
        return False, False
    in_audit = nct_id = False
    for decoded in _decoded_texts(text):
        layers, settled = _text_layers(decoded)
        in_audit = in_audit or not settled
        if as_value and not in_audit:
            in_audit = _names_in_audit_as_a_value(decoded, layers)
        for layer in layers:
            if ":" not in layer and "=" not in layer and "," not in layer:
                continue  # every key form needs one of these (_text_keys)
            may_flag = not in_audit and _may_name_in_audit(layer)
            may_id = not nct_id and (
                _serializes_a_container(layer) or bool(_series_repr_labels(layer))
            )
            if not (may_flag or may_id):
                continue
            for key in _text_keys(layer):
                key_in_audit, key_nct_id = _name_kind(key)
                in_audit = in_audit or (may_flag and key_in_audit)
                nct_id = nct_id or (may_id and key_nct_id)
        if in_audit and nct_id:
            break
    return in_audit, nct_id


def _is_mapping_like(value: Any) -> bool:
    """True for anything that answers ``keys()``, ``items()`` and ``[]`` like a dict.

    Method: duck-typed, not ``isinstance(value, dict)`` or
    ``isinstance(value, collections.abc.Mapping)``. A dict subclass, a
    collections.UserDict, a types.MappingProxyType and a pandas Series (the
    row ``df.iterrows()`` yields, whose ``in``, ``.get`` and ``.items()`` read
    its index labels) all qualify, and so does a DataFrame; a Series is not
    a Mapping, so the ABC alone would miss it. Text and classes are excluded.
    """
    return (
        not isinstance(value, (str, bytes, bytearray, type))
        and callable(getattr(value, "keys", None))
        and callable(getattr(value, "items", None))
        and hasattr(value, "__getitem__")
    )


def _has_method(value: Any, name: str) -> bool:
    """True when ``type(value)`` defines ``name`` as a callable (the protocol check)."""
    return callable(getattr(type(value), name, None))


def _is_bool_like(value: Any) -> bool:
    """A Python bool, or a numpy bool scalar (dtype kind "b" with no dimensions)."""
    if type(value) is bool:
        return True
    return (
        getattr(getattr(value, "dtype", None), "kind", None) == "b"
        and getattr(value, "ndim", None) == 0
    )


def _is_structured_scalar(value: Any) -> bool:
    """A numpy structured scalar (numpy.void / numpy.record: ``df.to_records()[i]``)."""
    names = getattr(getattr(value, "dtype", None), "names", None)
    return isinstance(names, tuple) and getattr(value, "ndim", None) == 0


def _is_named_record(value: Any) -> bool:
    """One record whose fields have names, other than a mapping-like value.

    A namedtuple (an ``df.itertuples()`` row), a numpy structured scalar (a
    ``df.to_records()`` row) or a dataclass instance. A batch entry point
    refuses one of these passed where a batch is expected: iterating it
    yields its field values, not records.
    """
    if isinstance(value, tuple) and _has_method(value, "_asdict"):
        return True
    if _is_structured_scalar(value):
        return True
    fields = getattr(type(value), "__dataclass_fields__", None)
    return isinstance(fields, dict) and not isinstance(value, type)


def _is_keyed_record(value: Any) -> bool:
    """A record naming its fields by ``keys()`` and reading them by ``[]``, without ``items()``.

    sqlite3.Row is one (a row read with ``row_factory=sqlite3.Row``): its
    ``keys()`` are the column names, ``row[name]`` a value, and iterating it
    yields the values by position, without their names. Duck-typed, so a
    driver's row type of the same shape qualifies too. Text and classes are
    excluded; a mapping-like value (_is_mapping_like) is read by its
    ``items()`` instead.
    """
    return (
        not isinstance(value, (str, bytes, bytearray, type))
        and callable(getattr(value, "keys", None))
        and _has_method(value, "__getitem__")
        and not _is_mapping_like(value)
    )


def _is_xml_element(value: Any) -> bool:
    """An XML element as xml.etree.ElementTree (and lxml) builds one, duck-typed.

    It answers ``keys()``, ``items()`` and ``[]``, so it would pass for a
    mapping, but those read only its XML attributes: its tag, text and
    child elements (where an element tree keeps the fields of a record)
    are elsewhere (``tag``, ``text``, ``tail``, iteration). Read by
    _keyed_items.
    """
    return (
        not isinstance(value, type)
        and isinstance(getattr(value, "attrib", None), dict)
        and hasattr(value, "tag")
        and hasattr(value, "text")
        and callable(getattr(value, "iter", None))
    )


# A placeholder stored among a scan's ids for an nct_id held inside text
# (_text_mentions): it equals no id, so a record that carries in_audit and
# such text is refused as "nct_id_malformed" rather than compared by guess.
_ID_IN_TEXT: object = object()
# Stands for the folded name of a flag held inside text, for the batch rule
# that compares the names records carry in_audit under.
_FLAG_IN_TEXT_NAME: str = "<in_audit inside serialized text>"
_MODULE_TYPE: type = type(json)
# Types whose instances hold nothing beside their items, elements or value
# (no instance attributes, and a namespace that cannot change), so
# _beside_items is not consulted for them.
_PLAIN_TYPES: frozenset[type] = frozenset(
    {dict, list, tuple, set, frozenset, str, bytes, bytearray, int, float, complex,
     bool, range, type(None)}
)
# CPython's Py_TPFLAGS_IMMUTABLETYPE (3.10+): set on builtin and extension
# types, whose namespace cannot be changed after creation.
_IMMUTABLE_TYPE_FLAG: int = 1 << 8
# Packages whose objects' instance attributes are library internals (a
# Series' block manager), read by name only (_beside_items).
_LIBRARY_PACKAGES: frozenset[str] = frozenset({"pandas", "numpy"})
# klass -> (its namespace's names when read, or None if immutable; the names
# in it that read as in_audit or nct_id). See _class_flag_names.
_CLASS_NAMES_CACHE: dict[type, tuple[frozenset[Any] | None, tuple[str, ...]]] = {}
_CLASS_NAMES_CACHE_LIMIT: int = 4096
# A scan that has read this many parts stops and reports the rest unreadable
# (a property that builds a new object each time it is read would otherwise
# never end).
_MAX_SCAN_NODES: int = 2_000_000


@dataclass(frozen=True)
class _InAuditScan:
    """What _scan_in_audit found in one record.

    ``flags``: every value stored under a name that reads as in_audit, at
    any depth (text that mentions in_audit as a key is itself stored as a
    flag: a str, never a bool). ``flag_names``: the folded names those flags
    were found under. ``ids``: every value stored under a name that reads as
    nct_id, at any depth (_ID_IN_TEXT for one inside serialized text).
    ``unreadable``: a description (a type name, never a value) of each part
    whose names or contents could not be read.
    """

    flags: tuple[Any, ...]
    flag_names: frozenset[str]
    ids: tuple[Any, ...]
    unreadable: tuple[str, ...]


def _keyed_items(node: Any) -> list[tuple[Any, Any]] | None:
    """(name, value) pairs of a node whose fields have names, else None.

    Method: a mapping-like value gives its items, and a dict items view
    (``d.items()``) its pairs; a namedtuple (what ``df.itertuples()``
    yields) gives its fields through ``_asdict()``; a numpy structured
    scalar (what ``df.to_records()`` holds) its ``dtype.names``; a dataclass
    instance its ``__dataclass_fields__``. Without this a namedtuple would be
    walked as a plain tuple, its values without their names, and a flag in
    it would be missed. A pandas object adds its named row-index levels and,
    for a Series, its own name (_pandas_axis_items). The field
    ``df.itertuples()`` names "Index" holds the row label, not a field, so
    its value has no name (_NO_NAME): after ``set_index("in_audit")`` it is
    the flag itself. An XML element (_is_xml_element) gives its tag (under
    "tag"), text and tail, its attributes, and each child element under the
    child's tag, so ``<in_audit>true</in_audit>`` is a field named
    in_audit; a keyed record such as sqlite3.Row (_is_keyed_record) gives
    ``(name, row[name])`` for each of its ``keys()``.
    """
    if _is_xml_element(node):
        return [
            ("tag", node.tag),
            ("text", node.text),
            ("tail", getattr(node, "tail", None)),
            *list(node.attrib.items()),
            *((child.tag, child) for child in list(node)),
        ]
    if _is_mapping_like(node):
        return list(node.items()) + _pandas_axis_items(node)
    if _is_keyed_record(node):
        return [(key, node[key]) for key in list(node.keys())]
    if any(
        klass.__name__ in ("dict_items", "odict_items", "ItemsView")
        for klass in type(node).__mro__
    ):
        return [pair for pair in node if isinstance(pair, tuple) and len(pair) == 2]
    if isinstance(node, tuple) and _has_method(node, "_asdict"):
        return [
            (_NO_NAME if key == "Index" else key, value)
            for key, value in node._asdict().items()
        ]
    if _is_structured_scalar(node):
        return [(name, node[name]) for name in node.dtype.names]
    fields = getattr(type(node), "__dataclass_fields__", None)
    if isinstance(fields, dict) and not isinstance(node, type):
        return [(name, getattr(node, name, None)) for name in fields]
    return None


def _names_a_field(name: Any) -> bool:
    """True when ``name`` is text or reads as in_audit or nct_id (an enum label)."""
    return _is_text_name(name) or any(_name_kind(name))


def _pandas_axis_items(node: Any) -> list[tuple[Any, Any]]:
    """(name, row value) pairs a pandas object holds in its axis names, and a Series' own name.

    Method: each named level of a Series' or DataFrame's row index gives
    one pair per row (``set_index("in_audit")`` keeps the flag only there),
    and so does a Series' own text name (``df["in_audit"]``). A Series' own
    name that is not text is a value without a field name (_NO_NAME): the
    row ``iterrows()`` or ``apply(axis=1)`` hands out carries its row label
    there, so after ``set_index("in_audit")`` it is the flag itself (True,
    or (0, True) with ``append=True``), and a bool there cannot be read.
    Duck-typed through ``index.names``, ``get_level_values`` and ``ndim``
    (1 for a Series; a DataFrame's ``.name`` would be a column named
    "name"); for anything else, no pairs.
    """
    pairs: list[tuple[Any, Any]] = []
    index = getattr(node, "index", None)
    names = getattr(index, "names", None)
    if names is None or not callable(getattr(index, "get_level_values", None)):
        return pairs
    for level, name in enumerate(list(names)):
        if _names_a_field(name):
            pairs.extend((name, value) for value in index.get_level_values(level).tolist())
    if getattr(node, "ndim", None) == 1:
        own_name = getattr(node, "name", None)
        if _names_a_field(own_name) and callable(getattr(node, "tolist", None)):
            pairs.extend((own_name, value) for value in node.tolist())
        elif own_name is not None:
            pairs.append((_NO_NAME, own_name))
    return pairs


def _index_items(index: Any) -> list[tuple[Any, Any]]:
    """(level name, value) pairs of a pandas Index's values; _NO_NAME for an unnamed level.

    A bare Index keeps a flag in its ``name`` (``pd.Index([...],
    name="in_audit")``, what ``df.set_index("in_audit").index`` is), so its
    values are read under their level's name. Levels of numbers or dates
    are skipped (no value of theirs can be a flag or hold text).
    """
    pairs: list[tuple[Any, Any]] = []
    for level, name in enumerate(list(index.names)):
        values = index.get_level_values(level)
        kind = getattr(getattr(values, "dtype", None), "kind", None)
        if kind in ("i", "u", "f", "c", "m", "M"):
            continue
        key = name if _names_a_field(name) else _NO_NAME
        pairs.extend((key, value) for value in values.tolist())
    return pairs


def _class_flag_names(klass: type) -> tuple[str, ...]:
    """Names defined in ``klass``'s own namespace that read as in_audit or nct_id.

    Cached per class (_CLASS_NAMES_CACHE). A class whose namespace cannot
    change (_IMMUTABLE_TYPE_FLAG: builtin and extension types such as dict
    or numpy.int64) is read once; any other class is read again whenever
    its namespace's names differ from those cached, so an attribute added
    to a class after a scan is not missed.
    """
    namespace = getattr(klass, "__dict__", None)
    if namespace is None:
        return ()
    cached = _CLASS_NAMES_CACHE.get(klass)
    if cached is not None and (cached[0] is None or cached[0] == namespace.keys()):
        return cached[1]
    names = tuple(n for n in list(namespace) if isinstance(n, str) and any(_name_kind(n)))
    immutable = bool(getattr(klass, "__flags__", 0) & _IMMUTABLE_TYPE_FLAG)
    if len(_CLASS_NAMES_CACHE) >= _CLASS_NAMES_CACHE_LIMIT:
        _CLASS_NAMES_CACHE.clear()
    _CLASS_NAMES_CACHE[klass] = (None if immutable else frozenset(namespace), names)
    return names


def _callable_parts(node: Any) -> list[tuple[Any, Any]]:
    """(name, value) pairs a callable was built from.

    A functools.partial (duck-typed: ``func``, ``args`` and ``keywords``)
    gives all three; a function gives its closure cells by free-variable
    name, its defaults by parameter name and its keyword-only defaults; a
    bound method gives ``__self__`` (unless a module) and ``__func__``. A
    function's globals are not read.
    """
    if not callable(node) or isinstance(node, type):
        return []
    pairs: list[tuple[Any, Any]] = []
    keywords = getattr(node, "keywords", None)
    if (
        isinstance(keywords, dict)
        and isinstance(getattr(node, "args", None), tuple)
        and callable(getattr(node, "func", None))
    ):
        pairs.extend((("func", node.func), ("args", node.args), ("keywords", keywords)))
    code = getattr(node, "__code__", None)
    freevars = getattr(code, "co_freevars", None)
    closure = getattr(node, "__closure__", None)
    if isinstance(freevars, tuple) and isinstance(closure, tuple):
        for name, cell in zip(freevars, closure):
            try:
                pairs.append((name, cell.cell_contents))
            except ValueError:  # an empty cell holds nothing
                continue
    defaults = getattr(node, "__defaults__", None)
    varnames = getattr(code, "co_varnames", None)
    argcount = getattr(code, "co_argcount", None)
    if isinstance(defaults, tuple) and isinstance(varnames, tuple) and isinstance(argcount, int):
        params = varnames[:argcount]
        pairs.extend(zip(params[len(params) - len(defaults):], defaults))
    kwdefaults = getattr(node, "__kwdefaults__", None)
    if isinstance(kwdefaults, dict):
        pairs.extend(kwdefaults.items())
    owner = getattr(node, "__self__", None)
    if owner is not None and not isinstance(owner, _MODULE_TYPE):
        pairs.append(("__self__", owner))
    function = getattr(node, "__func__", None)
    if function is not None:
        pairs.append(("__func__", function))
    return pairs


def _beside_items(node: Any, unreadable: list[str]) -> list[tuple[Any, Any]]:
    """(name, value) pairs held beside a node's items, fields, elements or text.

    For a node whose type is not one of _PLAIN_TYPES (a module is a leaf),
    the scan also reads:
      * its instance attributes (``vars()`` and ``__slots__``), all of them
        as named fields, except on a pandas or numpy object, whose instance
        attributes are the library's internals (a Series' block manager):
        there only those whose names read as in_audit or nct_id;
      * every attribute its class defines through the MRO (for a class,
        the class itself) whose name reads as in_audit or nct_id
        (_class_flag_names), read with getattr so a property or a class
        attribute answers; one that raises when read is unreadable;
      * what a callable was built from (_callable_parts).
    So in_audit set as an attribute of a dict subclass, beside a
    dataclass's or namedtuple subclass's fields or on a str subclass, as a
    property or a class attribute, inside a functools.partial or a callable
    namespace, and a collections.ChainMap's layers (its ``maps``: the True
    a front layer's False shadows) are all read.
    """
    if isinstance(node, _MODULE_TYPE):
        return []
    pairs: list[tuple[Any, Any]] = []
    if isinstance(node, type):
        classes: tuple[type, ...] = node.__mro__
    else:
        classes = type(node).__mro__
        package = getattr(type(node), "__module__", None)
        library = isinstance(package, str) and package.split(".")[0] in _LIBRARY_PACKAGES
        try:
            instance_dict = node.__dict__
        except AttributeError:  # no instance attributes
            instance_dict = None
        except Exception:  # a __getattr__ that fails: what it hides cannot be read
            instance_dict = None
            unreadable.append(f"a {type(node).__name__} whose attributes raised when read")
        if isinstance(instance_dict, dict):
            pairs.extend(
                (key, value)
                for key, value in list(instance_dict.items())
                if not library or any(_name_kind(key))
            )
        for klass in classes:
            slots = klass.__dict__.get("__slots__", ())
            for slot in (slots,) if isinstance(slots, str) else slots:
                if slot in ("__dict__", "__weakref__"):
                    continue
                if library and not any(_name_kind(slot)):
                    continue
                try:
                    if hasattr(node, slot):
                        pairs.append((slot, getattr(node, slot)))
                except Exception:  # any failure leaves the slot unread
                    unreadable.append(f"a {type(node).__name__} slot that raised when read")
        pairs.extend(_callable_parts(node))
    for klass in classes:
        if klass is object:
            continue
        for name in _class_flag_names(klass):
            try:
                pairs.append((name, getattr(node, name)))
            except Exception:  # any failure leaves the attribute unread
                unreadable.append(f"a {type(node).__name__} attribute that raised when read")
    return pairs


def _scan_in_audit(record: Any) -> _InAuditScan:
    """Find every in_audit flag and nct_id in ``record``, at any depth and spelling.

    Method: an explicit-stack walk (no recursion limit; at most
    _MAX_SCAN_NODES parts, past which the rest is unreadable). Each node is
    read by the first of these that applies:
      * text (str, bytes, bytearray, memoryview): a leaf, unless it
        mentions in_audit as a key (stored as a flag, so it is refused as
        not a bool) or, when it serializes a container, nct_id (stored as
        _ID_IN_TEXT) (_text_mentions);
      * a bool or numpy bool: a leaf when reached through a text name; one
        reached by position or under a non-text key has lost its name and
        could be a flag, so it is unreadable;
      * a number or range: a leaf;
      * a node with named fields (_keyed_items: XML elements, mapping-like
        values, keyed rows such as sqlite3.Row, dict items views,
        namedtuples, numpy structured scalars, dataclass instances, plus a
        pandas object's named axes and a Series' own name): every name that
        reads as in_audit gives a flag and every name that reads as nct_id
        an id (_name_kind: case, separators, lookalike letters, invisible
        characters and qualifiers ignored; an enum key read as its name,
        str() and value); a key that is a bool, or a tuple holding
        one, is unreadable (_holds_bool); a namedtuple field that pandas or
        namedtuple(rename=True) renamed to "_N" has lost its name, so it is
        unreadable; a pandas object's ``attrs`` are walked;
      * a one-shot iterator or generator: unreadable (reading it would
        consume it, and what consumes it later could read a flag in it);
      * a pandas Index: its values under their level's name (_index_items);
      * a numpy array: object and text elements are walked, a structured
        array's records are walked, an array of bools is unreadable
        (unnamed bools), numbers are leaves;
      * any other sized, re-iterable container (list, tuple, set, deque,
        dict keys or values view): its elements, by position; an
        iterable that is not sized is unreadable;
      * at the top level, an array or such a container is a record whose
        fields have no names (``df.itertuples(name=None)`` yields one), so
        it is unreadable.
    Then, for any node whose type is not one of _PLAIN_TYPES (a subclass,
    a dataclass, a pandas object, a callable, a plain object), what it
    holds beside that (_beside_items: instance attributes, class
    attributes and properties that read as in_audit or nct_id, a callable's
    parts, a ChainMap's layers) is read as named fields too.
    Each container and non-plain node is visited once; ``visited`` keeps a
    reference to it as well as its id(), because a DataFrame's ``items()``
    builds a fresh Series per column and a released object's id() can be
    reused, which would otherwise skip an unvisited node. Assumption: a
    CT.gov API v2 study, a retained raw record and a stored sponsor record
    have no name that reads as in_audit, no bool outside a named field, and
    no text that holds an in_audit key, so a flag found anywhere was put
    there by a manifest merge and marks the record.
    """
    flags: list[Any] = []
    flag_names: set[str] = set()
    ids: list[Any] = []
    unreadable: list[str] = []
    # (node, reached through a text name, is the record itself)
    stack: list[tuple[Any, bool, bool]] = [(record, False, True)]
    visited: dict[int, Any] = {}
    steps = 0
    while stack:
        steps += 1
        if steps > _MAX_SCAN_NODES:
            unreadable.append(f"more than {_MAX_SCAN_NODES} parts")
            break
        node, named, top = stack.pop()
        if node is None:
            continue
        plain = type(node) in _PLAIN_TYPES
        if not plain or isinstance(node, (dict, list, tuple, set, frozenset)):
            if id(node) in visited:
                continue
            visited[id(node)] = node
        pairs: list[tuple[Any, Any]] = []
        if isinstance(node, (str, bytes, bytearray, memoryview)):
            in_audit_text, nct_id_text = _text_mentions(node)
            if in_audit_text:
                flags.append(node)
                flag_names.add(_FLAG_IN_TEXT_NAME)
            if nct_id_text:
                ids.append(_ID_IN_TEXT)
        elif _is_bool_like(node):
            if not named:
                unreadable.append(f"a {type(node).__name__} without a field name")
        elif isinstance(node, (int, float, complex, range)):
            pass
        elif (items := _keyed_items(node)) is not None:
            if isinstance(node, tuple) and _has_method(node, "_asdict"):
                renamed = [f for f in getattr(node, "_fields", ()) if str(f).startswith("_")]
                if renamed:
                    unreadable.append(
                        f"a {type(node).__name__} with {len(renamed)} field(s) renamed to _N"
                    )
            pairs.extend(items)
            attrs = getattr(node, "attrs", None)  # pandas Series/DataFrame metadata
            if isinstance(attrs, dict):
                stack.append((attrs, True, False))
        elif _has_method(node, "__next__"):
            unreadable.append(f"a one-shot {type(node).__name__}")
        elif (
            kind := getattr(getattr(node, "dtype", None), "kind", None)
        ) is not None and getattr(node, "ndim", 0) >= 1:
            if top:
                unreadable.append(f"a {type(node).__name__} whose fields have no names")
            is_index = callable(getattr(node, "get_level_values", None))
            if is_index and getattr(node, "names", None) is not None:
                pairs.extend(_index_items(node))
            elif kind in ("O", "U", "S") and callable(getattr(node, "tolist", None)):
                stack.extend((value, False, False) for value in node.tolist())
            elif kind == "V" and callable(getattr(node, "reshape", None)):
                stack.extend((value, False, False) for value in node.reshape(-1))
            elif kind == "b" and getattr(node, "size", 0):
                unreadable.append(f"a {type(node).__name__} of bools without a field name")
        elif _has_method(node, "__iter__"):
            if not _has_method(node, "__len__"):
                unreadable.append(f"an unsized iterable {type(node).__name__}")
            else:
                if top:
                    unreadable.append(f"a {type(node).__name__} whose fields have no names")
                stack.extend((value, False, False) for value in list(node))
        if not plain:
            pairs.extend(_beside_items(node, unreadable))
        for key, value in pairs:
            key_named = False
            if key is not _NO_NAME:
                if _holds_bool(key):
                    unreadable.append(f"a key or label holding a bool ({type(key).__name__})")
                else:
                    key_in_audit, key_nct_id = _name_kind(key)
                    if key_in_audit:
                        flags.append(value)
                        flag_names.add(_folded_name(key) or "")
                        if (
                            isinstance(key, (str, bytes, bytearray))
                            and _text_mentions(key, as_value=False)[0]
                        ):
                            flags.append(key)  # the name itself is a serialized flag
                    elif key_nct_id:
                        ids.append(value)
                key_named = _is_text_name(key)
            stack.append((value, key_named, False))
    return _InAuditScan(
        flags=tuple(flags),
        flag_names=frozenset(flag_names),
        ids=tuple(ids),
        unreadable=tuple(unreadable),
    )


def _check_scanned_record(
    record: Any,
    scan: _InAuditScan,
    where: str,
    raw_nct_id_of: Callable[[Any], Any],
) -> None:
    """Refuse one scanned record unless it is a canonical non-holdout or carries no flag.

    Method, in this order, before anything else in the record is read:
      1. if it carries a flag: every flag must be exactly a bool
         (``type(v) is bool``: 0/1, "True"/"false", None, numpy bools and
         serialized text are all refused, never coerced) ("in_audit_not_bool");
         no flag may be True ("in_audit_true"); the nct_id the entry point
         would stamp, read raw by ``raw_nct_id_of`` (no str() coercion; None
         for a record that is not mapping-like), must be exactly a str (not
         a subclass, whose __str__ or __eq__ could say another id) matching
         ``^NCT[0-9]{8}$``; and every nct_id the record carries at any
         depth (a top-level key, one beside a flag, one in a nested
         sub-record, one inside serialized text) must be exactly a str
         equal to it, because a
         manifest row merged onto another trial's payload describes a
         different trial ("nct_id_malformed", never resolved);
      2. flag or not: nothing in it may be unreadable (_scan_in_audit), since
         a part whose names or contents cannot be read may hold a flag
         ("in_audit_not_bool": whether it carries in_audit is unknown).
    """
    if scan.flags:
        for flag in scan.flags:
            if type(flag) is not bool:
                raise PopulationAuditHoldoutError(
                    "in_audit_not_bool",
                    f"{where}: in_audit must be exactly True or False, got "
                    f"{type(flag).__name__}; refusing the record rather than guessing",
                )
        if any(scan.flags):
            raise PopulationAuditHoldoutError(
                "in_audit_true",
                f"{where}: record is an in_audit holdout trial; it must not be "
                "parsed, retained or recorded",
            )
        raw_nct = raw_nct_id_of(record) if _is_mapping_like(record) else None
        if not _is_canonical_nct_id(raw_nct):
            raise PopulationAuditHoldoutError(
                "nct_id_malformed",
                f"{where}: in_audit is False but the nct_id ({type(raw_nct).__name__}) "
                "is not exactly ^NCT[0-9]{8}$ (a plain str: ASCII, case-sensitive, unstripped)",
            )
        disagreeing = [  # exactly str: a subclass's __eq__ could agree with anything
            other for other in scan.ids if not (type(other) is str and other == raw_nct)
        ]
        if disagreeing:
            raise PopulationAuditHoldoutError(
                "nct_id_malformed",
                f"{where}: in_audit is False but {len(disagreeing)} other nct_id "
                "value(s) in the record, at any depth, disagree with the id it would "
                "be stamped with; refusing rather than choosing one",
            )
    if scan.unreadable:
        raise PopulationAuditHoldoutError(
            "in_audit_not_bool",
            f"{where}: {len(scan.unreadable)} part(s) of the record cannot be read "
            f"by the holdout check (first: {scan.unreadable[0]}), so whether it "
            "carries in_audit is unknown; refusing rather than guessing",
        )


def _refuse_audit_holdout_record(
    record: Any,
    where: str,
    raw_nct_id_of: Callable[[Any], Any],
) -> bool:
    """Refuse a record that carries ``in_audit`` unless it is a canonical non-holdout.

    Returns whether the record carries ``in_audit``. Method: the record is
    scanned for in_audit flags and nct_ids at any depth and under any
    spelling (_scan_in_audit), whatever its type, and checked by
    _check_scanned_record. A record with no flag and nothing unreadable is
    passed through unchanged (the caller is responsible for it; see the
    module docstring).
    """
    scan = _scan_in_audit(record)
    _check_scanned_record(record, scan, where, raw_nct_id_of)
    return bool(scan.flags)


def _refuse_audit_holdout_batch(
    records: list[Any],
    where: str,
    raw_nct_id_of: Callable[[Any], Any],
) -> None:
    """Refuse a batch unless every record passes _check_scanned_record, as a batch.

    Method: every record is scanned first (reading nothing beyond the
    scan). Then, before any study is parsed, in this order:
      1. the records that carry in_audit must carry it under the same set
         of names (folded), or the batch is refused ("in_audit_not_bool"):
         one manifest merge gives every record the same names, so a record
         whose flag sits under another name (in_audit_y beside records with
         in_audit) came from elsewhere, and its flag is not compared with
         the others' as if it meant the same;
      2. each record, in order, is checked by _check_scanned_record, so a
         True flag is reported as "in_audit_true" at its position;
      3. a batch in which some records carry in_audit and others do not is
         refused ("in_audit_not_bool"): it is what a manifest left join
         produces when a record finds no manifest row, and such a record is
         unknown, not clean (sponsors/ecosystem.py refuses the same gap as a
         NaN in its in_audit column).
    Twin of sponsors/ecosystem.py _refuse_audit_holdout_records.
    """
    scans = [_scan_in_audit(record) for record in records]
    carrying_names = [(pos, scan.flag_names) for pos, scan in enumerate(scans) if scan.flags]
    if carrying_names:
        first_pos, first_names = carrying_names[0]
        differing = [pos for pos, names in carrying_names if names != first_names]
        if differing:
            raise PopulationAuditHoldoutError(
                "in_audit_not_bool",
                f"{where}: {len(differing)} of {len(carrying_names)} records carrying "
                f"in_audit carry it under other names than the record at position "
                f"{first_pos} (first at position {differing[0]}); one manifest merge "
                "names the flag alike in every record, so the batch is refused",
            )
    for pos, (record, scan) in enumerate(zip(records, scans)):
        _check_scanned_record(record, scan, f"{where}, record at position {pos}", raw_nct_id_of)
    carrying = [bool(scan.flags) for scan in scans]
    n_carrying = sum(carrying)
    if 0 < n_carrying < len(records):
        raise PopulationAuditHoldoutError(
            "in_audit_not_bool",
            f"{where}: {n_carrying} of {len(records)} records carry in_audit and "
            f"{len(records) - n_carrying} do not (first at position "
            f"{carrying.index(False)}); in a manifest-merged batch a record without "
            "in_audit is unknown, not clean, so the batch is refused",
        )


# The CT.gov API v2 OutcomeMeasureType values, the only ones ``outcome_types``
# may name (the same vocabulary as outcomes/results_structure.py OUTCOME_TYPES,
# restated here because this module imports nothing from the repository).
OUTCOME_MEASURE_TYPES: tuple[str, ...] = ("PRIMARY", "SECONDARY", "OTHER_PRE_SPECIFIED", "POST_HOC")
_SNAPSHOT_DATE_RE: re.Pattern[str] = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")


def _checked_snapshot_date(snapshot_date: Any, where: str) -> str | None:
    """``snapshot_date`` if it is None or a YYYY-MM-DD calendar date, else raise.

    Method: the value is stamped verbatim on every output, so anything that
    is not a date (an audit id list or a single id passed positionally) is
    refused: TypeError for a non-str, ValueError for a str that is not
    exactly YYYY-MM-DD in ASCII digits naming a real day (leap years by the
    Gregorian rule). Messages give the type and length, never the value.
    """
    if snapshot_date is None:
        return None
    if not isinstance(snapshot_date, str):
        raise TypeError(
            f"{where}: snapshot_date must be a YYYY-MM-DD string or None, got "
            f"{type(snapshot_date).__name__}"
        )
    match = _SNAPSHOT_DATE_RE.fullmatch(snapshot_date)
    if match is None:
        raise ValueError(
            f"{where}: snapshot_date must be exactly YYYY-MM-DD, got a str of "
            f"length {len(snapshot_date)}"
        )
    year, month, day = (int(part) for part in match.groups())
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    month_days = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    if year < 1 or not 1 <= month <= 12 or not 1 <= day <= month_days[month - 1]:
        raise ValueError(f"{where}: snapshot_date is not a calendar date")
    return snapshot_date


def _checked_outcome_types(outcome_types: Any, where: str) -> tuple[str, ...] | None:
    """``outcome_types`` as a tuple if None or CT.gov outcome types, else raise.

    Method: a str (which would iterate as characters), a mapping-like value
    or a non-iterable is a TypeError, as is a non-str element; an element
    that is not one of OUTCOME_MEASURE_TYPES once stripped and upper-cased
    (the leniency _parse_outcomes_payload applies) is a ValueError, so an
    audit id set passed here is refused instead of silently filtering every
    measure out. Messages never echo a value.
    """
    if outcome_types is None:
        return None
    if (
        isinstance(outcome_types, (str, bytes, bytearray))
        or _is_mapping_like(outcome_types)
        or not _has_method(outcome_types, "__iter__")
    ):
        raise TypeError(
            f"{where}: outcome_types must be a tuple of CT.gov outcome types or None, "
            f"got {type(outcome_types).__name__}"
        )
    values = tuple(outcome_types)
    for value in values:
        if not isinstance(value, str):
            raise TypeError(
                f"{where}: outcome_types holds a {type(value).__name__}, not a str"
            )
        if value.strip().upper() not in OUTCOME_MEASURE_TYPES:
            raise ValueError(
                f"{where}: outcome_types may name only {list(OUTCOME_MEASURE_TYPES)}; "
                f"got a str of length {len(value)} that is not one of them"
            )
    return values


# ---------------------------------------------------------------------------
# 1. Ordered deterministic regex vocabulary & VOCABULARY_SHA256
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VocabRule:
    """Single deterministic vocabulary rule for populationDescription."""

    rule_id: str
    tier: Literal["placeholder", "named_label", "definitional"]
    target_class: str
    pattern: str
    masks_for: tuple[str, ...] = ()
    forbids_pattern: str | None = None
    note: str = ""


VOCABULARY_RULES: tuple[VocabRule, ...] = (
    # Tier 0: pure placeholders -> not_stated (not_stated_reason="placeholder_token")
    VocabRule(
        rule_id="T0_PLACEHOLDER",
        tier="placeholder",
        target_class="not_stated",
        pattern=(
            r"^(?:n/?a|not\s+(?:applicable|available|specified|stated|reported|"
            r"provided|given|known|determined|collected|defined)|"
            r"none(?:\s+specified)?|unknown|unspecified|unreported|pending|tbd|"
            r"[-.?]+)$"
        ),
        note="Pure placeholder strings map to not_stated with placeholder_token reason.",
    ),
    # Tier 1: named analysis-population labels (fixed evaluation order with span masking)
    VocabRule(
        rule_id="T1_MITT",
        tier="named_label",
        target_class="modified_itt",
        pattern=(
            r"\b(?:modified\s+intent(?:ion)?[- ]to[- ]treat|"
            r"modified\s+itt|m[- ]?itt|mod[- ]?itt)\b"
        ),
        masks_for=("itt",),
        note=(
            "Masks its matched span before T1_ITT runs so embedded 'intent-to-treat' "
            "does not double-fire itt, while an independent 'ITT and mITT' still "
            "fires both -> ambiguous_multi."
        ),
    ),
    VocabRule(
        rule_id="T1_ITT",
        tier="named_label",
        target_class="itt",
        pattern=r"(?<!\bnon[- ])(?<!\bnot\s)\b(?:intent(?:ion)?[- ]to[- ]treat|itt|ittp)\b",
        note="Named intention-to-treat population; guards against 'non-ITT'/'not ITT'.",
    ),
    VocabRule(
        rule_id="T1_FAS",
        tier="named_label",
        target_class="full_analysis_set",
        pattern=(
            r"\b(?:full[- ]analysis\s+(?:set|population)|"
            r"fas(?!\s*[-/]?\s*(?:ligand|receptor|l\b|pathway|expression|antigen|"
            r"positive|negative|gene|signaling|mediated|apoptotic)))\b"
        ),
        note="Full Analysis Set; lookahead guards CD95/Fas biology terminology.",
    ),
    VocabRule(
        rule_id="T1_PP",
        tier="named_label",
        target_class="per_protocol",
        pattern=(
            r"(?<!\bnon[- ])(?<!\bnot\s)\b(?:per[- ]protocol|according[- ]to[- ]protocol|"
            r"protocol[- ]compliant|"
            r"(?:pp|pps|ppp)\s+(?:population|set|analysis|cohort|sample|group|"
            r"participants|patients|subjects)|"
            r"atp\s+(?:population|set|cohort|analysis))\b|^(?:pp|pps|ppp)$"
        ),
        note="Per-protocol population; guards against disease acronyms (e.g. PPP/PPS).",
    ),
    VocabRule(
        rule_id="T1_SAFETY",
        tier="named_label",
        target_class="safety",
        pattern=(
            r"\b(?:safety[- ](?:evaluable|analysis\s+set|analysis\s+population|"
            r"population|set|cohort|sample|subjects|patients|participants)|"
            r"saf\s+(?:population|set)|"
            r"as[- ]treated(?:\s+(?:population|set|analysis|cohort))?)\b|^safety$"
        ),
        masks_for=("evaluable",),
        note=(
            "Safety / as-treated population; masks 'safety evaluable' before "
            "T1_EVALUABLE and ignores timepoint phrases like 'safety follow-up'."
        ),
    ),
    VocabRule(
        rule_id="T1_EVALUABLE",
        tier="named_label",
        target_class="evaluable",
        pattern=(
            r"(?<!\bnon[- ])(?<!\bnot\s)\b(?:(?:efficacy|response|clinically|clinical|"
            r"tumor|tumour|radiographically|radiologic|immunogenicity|biomarker|"
            r"pk|pd|pharmacokinetic|pharmacodynamic)[- ]+)?"
            r"(?:evaluable|assessable)\b"
        ),
        note="Evaluable/assessable population; guards against 'non-evaluable'/'not evaluable'.",
    ),
    # Tier 2: definitional prose (evaluated ONLY when Tier 1 matches 0 classes)
    VocabRule(
        rule_id="T2_DEF_MITT",
        tier="definitional",
        target_class="modified_itt",
        pattern=(
            r"\ball\s+(?:randomized|randomised)\s+(?:participants|patients|subjects|individuals)\b"
            r".*\b(?:who\s+(?:received|took|had)\s+(?:at\s+least\s+(?:one|1)|>=?\s*1)|"
            r"with\s+(?:at\s+least\s+(?:one|1)|>=?\s*1)\s+post[- ]?baseline)"
        ),
        note="Definitional modified ITT: all randomized who received >=1 dose or post-baseline.",
    ),
    VocabRule(
        rule_id="T2_DEF_ITT",
        tier="definitional",
        target_class="itt",
        pattern=(
            r"^\s*all\s+(?:randomized|randomised|assigned)\s+"
            r"(?:participants|patients|subjects|individuals)"
            r"(?:\s+in\s+the\s+(?:study|trial))?\s*\.?\s*$"
        ),
        forbids_pattern=(
            r"\b(?:who|with|without|excluding|except|completing|completed|"
            r"received|treated|evaluable|having)\b"
        ),
        note="Unrestricted all-randomized definition; exclusions prevent pure ITT match.",
    ),
    VocabRule(
        rule_id="T2_DEF_PP",
        tier="definitional",
        target_class="per_protocol",
        pattern=(
            r"\b(?:without\s+(?:any\s+)?(?:major|important|significant)\s+"
            r"protocol\s+(?:deviations?|violations?)|"
            r"completed\s+(?:the\s+)?(?:study|treatment)\s+without\s+"
            r"protocol\s+(?:deviations?|violations?))\b"
        ),
        note="Definitional per-protocol prose without major protocol deviations.",
    ),
    VocabRule(
        rule_id="T2_DEF_SAFETY",
        tier="definitional",
        target_class="safety",
        pattern=(
            r"\ball\s+(?:treated\s+(?:participants|patients|subjects)|"
            r"(?:participants|patients|subjects)\s+who\s+(?:received|took)\s+"
            r"(?:at\s+least\s+(?:one|1)|>=?\s*1|any)\s+"
            r"(?:dose|administration|infusion|study\s+(?:drug|medication|treatment|intervention)))\b"
        ),
        forbids_pattern=r"\b(?:randomized|randomised)\b",
        note="Definitional safety population (all treated / >=1 dose without randomization qualifier).",
    ),
    VocabRule(
        rule_id="T2_DEF_EVALUABLE",
        tier="definitional",
        target_class="evaluable",
        pattern=(
            r"\b(?:participants|patients|subjects)\s+(?:with|having|who\s+had)\s+"
            r"(?:a\s+)?(?:valid\s+)?baseline\s+and\s+"
            r"(?:at\s+least\s+(?:one|1)|>=?\s*1)\s+post[- ]?baseline\b"
        ),
        forbids_pattern=r"\b(?:randomized|randomised)\b",
        note="Definitional evaluable population (baseline + >=1 post-baseline without randomization qualifier).",
    ),
)


def compute_vocabulary_sha256(rules: Sequence[VocabRule] = VOCABULARY_RULES) -> str:
    """Deterministic SHA-256 digest of taxonomy + ordered vocabulary rules."""
    payload = {
        "taxonomy": list(POPULATION_TAXONOMY),
        "rules": [
            [
                idx,
                r.rule_id,
                r.tier,
                r.target_class,
                r.pattern,
                list(r.masks_for),
                r.forbids_pattern,
            ]
            for idx, r in enumerate(rules)
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


VOCABULARY_SHA256: str = compute_vocabulary_sha256(VOCABULARY_RULES)

# Pre-compiled patterns for fast, deterministic evaluation
_COMPILED_PATTERNS: dict[str, re.Pattern[str]] = {
    r.rule_id: re.compile(r.pattern, re.IGNORECASE) for r in VOCABULARY_RULES
}
_COMPILED_FORBIDS: dict[str, re.Pattern[str]] = {
    r.rule_id: re.compile(r.forbids_pattern, re.IGNORECASE)
    for r in VOCABULARY_RULES
    if r.forbids_pattern is not None
}

_ZERO_WIDTH_RE: re.Pattern[str] = re.compile(r"[\u200b\u200c\u200d\ufeff]")
_UNICODE_DASH_RE: re.Pattern[str] = re.compile(r"[\u2010-\u2015\u2212]")
_WHITESPACE_RE: re.Pattern[str] = re.compile(r"\s+")

# Audit-only detector for statements declaring no analysis took place.
# NEVER alters population_class.
_DECLARED_NOT_ANALYZED_RE: re.Pattern[str] = re.compile(
    r"\b(?:not\s+analy[sz]ed|no\s+(?:participants?|patients?|subjects?)\s+"
    r"(?:were\s+|was\s+)?analy[sz]ed|"
    r"0\s+(?:participants?|patients?|subjects?)\s+analy[sz]ed|"
    r"not\s+evaluated\s+due\s+to\s+(?:early\s+)?termination|"
    r"no\s+data\s+(?:collected|analyzed|analysed))\b",
    re.IGNORECASE,
)

# Group-title classifiers for CT.gov outcome groups (OG) and flow groups (FG).
# Unlike baselineCharacteristicsModule (where PRS auto-adds a Total BG column),
# outcomeMeasuresModule does NOT automatically add a Total OG column, though
# sponsors occasionally create one manually.
_TOTAL_GROUP_TITLE_RE: re.Pattern[str] = re.compile(
    r"^(?:total|overall|all\s+(?:groups?|arms?|participants?|patients?|subjects?|"
    r"combined|cohorts?)|combined(?:\s+(?:total|groups?|arms?))?)$",
    re.IGNORECASE,
)
_POOLED_GROUP_TITLE_RE: re.Pattern[str] = re.compile(
    r"\b(?:pooled|combined|all\s+(?:doses?|active|treated|cohorts?|arms?|groups?)|total|overall)\b",
    re.IGNORECASE,
)
_GENERIC_NUMBERED_ARM_RE: re.Pattern[str] = re.compile(
    r"^(?:arm|group)\s*(\d+)$",
    re.IGNORECASE,
)
_NON_ALNUM_GROUP_RE: re.Pattern[str] = re.compile(r"[^a-z0-9\- ]+")


# ---------------------------------------------------------------------------
# 2. Typed Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassificationResult:
    """Output of classify_population_description()."""

    population_description_raw: str | None
    population_class: PopulationClass
    match_tier: MatchTier
    matched_classes: list[str]
    matched_rule_ids: list[str]
    not_stated_reason: NotStatedReason | None
    declared_not_analyzed: bool

    @property
    def raw_population_description(self) -> str | None:
        """Alias preserving verbatim input string."""
        return self.population_description_raw


@dataclass(frozen=True)
class FlowGroupStarted:
    """Per-group STARTED counts extracted from participantFlowModule.periods[0]."""

    flow_group_id: str
    title: str | None
    is_total_group: bool
    started_participants: float | None
    started_units: float | None


@dataclass
class ArmAnalyzedRecord:
    """Per-outcome-group analyzed counts and strictly separate flow attrition gap."""

    outcome_group_id: str
    title: str | None
    is_total_group: bool
    analyzed_participants: float | None
    analyzed_units: float | None
    matched_flow_group_id: str | None
    started_participants: float | None
    arm_started_minus_analysed: float | None
    arm_gap_reason: str
    flag_analyzed_exceeds_started: bool = False


@dataclass
class ParsedOutcomePopulation:
    """Per-outcome analysis population, denominators, and attrition audit fields."""

    outcome_index: int
    outcome_type: str | None
    measure_title: str | None
    time_frame: str | None
    reporting_status: str | None
    param_type: str | None
    unit_of_measure: str | None
    population_description_raw: str | None
    population_class: PopulationClass | None
    match_tier: MatchTier | None
    matched_classes: list[str] = field(default_factory=list)
    matched_rule_ids: list[str] = field(default_factory=list)
    not_stated_reason: NotStatedReason | None = None
    declared_not_analyzed: bool = False
    denom_source: DenomSource = "none"
    denom_status: str = "no_denoms_array"
    denom_unit_kind: DenomUnitKind = "NONE"
    overall_participants_analyzed: float | None = None
    aggregation_method: str = "none"
    is_participant_count: bool = False
    units_analyzed_type: str | None = None
    units_denom_status: str = "no_denoms_array"
    overall_units_analyzed: float | None = None
    arms: list[ArmAnalyzedRecord] = field(default_factory=list)
    overall_started_participants: float | None = None
    overall_started_minus_analyzed: float | None = None
    enrollment_minus_analyzed: float | None = None
    flag_positive_attrition_gap: bool = False
    flag_analyzed_exceeds_started: bool = False
    flag_exceeds_enrollment: bool = False
    flag_total_group_mismatch: bool = False
    flag_partial_arm_counts: bool = False
    flag_class_varying_denoms: bool = False
    class_denom_min: float | None = None
    class_denom_max: float | None = None
    parse_ok: bool = True
    parse_error_reason: str | None = None
    parser_rules_version: str = PARSER_RULES_VERSION
    vocabulary_sha256: str = VOCABULARY_SHA256

    @property
    def raw_population_description(self) -> str | None:
        """Alias preserving verbatim populationDescription string."""
        return self.population_description_raw

    def to_dict(self) -> dict[str, Any]:
        """Serialize to plain JSON-compatible dict."""
        return asdict(self)


@dataclass
class TrialAnalysisPopulations:
    """Trial-level container of parsed outcome populations and primary rollups."""

    nct_id: str | None
    snapshot_date: str | None
    enrollment: float | None
    has_results_section: bool
    has_participant_flow: bool
    n_outcomes_total: int
    n_primary_outcomes: int
    outcomes: list[ParsedOutcomePopulation]
    primary_population_classes: list[str]
    parse_ok: bool = True
    parser_rules_version: str = PARSER_RULES_VERSION
    vocabulary_sha256: str = VOCABULARY_SHA256

    def to_dict(self) -> dict[str, Any]:
        """Serialize to plain JSON-compatible dict."""
        return asdict(self)


# ---------------------------------------------------------------------------
# 3. Text normalization & pure description classification
# ---------------------------------------------------------------------------


def normalize_population_text(raw: str | None) -> str | None:
    """Return a matching-only normalized copy of raw; never mutates raw.

    Removes zero-width characters (\\u200b/\\u200c/\\u200d/\\ufeff), maps
    Unicode dashes [\\u2010-\\u2015\\u2212] to ASCII '-', and collapses
    whitespace. Returns None iff raw is None.

    Audit holdout: RELIES ON ITS CALLER. It receives one description string
    with no trial id and no ``in_audit``, so it cannot refuse a holdout trial;
    the caller must not pass one. Callers today:
    classify_population_description in this module; none elsewhere in code (see
    the module docstring).
    """
    if raw is None:
        return None
    text = _ZERO_WIDTH_RE.sub("", str(raw))
    text = _UNICODE_DASH_RE.sub("-", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def _mask_spans(text: str, spans: Sequence[tuple[int, int]]) -> str:
    """Replace each [start, end) span in text with ASCII spaces of equal length."""
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        for pos in range(start, end):
            chars[pos] = " "
    return "".join(chars)


def classify_population_description(raw: str | None) -> ClassificationResult:
    """Deterministically classify a single populationDescription string.

    Takes ONLY raw: str | None — has zero access to counts, flow, enrollment,
    or attrition gaps. Preserves raw verbatim in population_description_raw.

    Audit holdout: RELIES ON ITS CALLER. It receives one description string
    with no trial id and no ``in_audit``, so it cannot refuse a holdout trial;
    the caller must not pass one. Callers today: parse_outcome_measure and
    population_audit in this module; none elsewhere in code (see the module
    docstring).
    """
    if raw is None:
        return ClassificationResult(
            population_description_raw=None,
            population_class="not_stated",
            match_tier="none",
            matched_classes=[],
            matched_rule_ids=[],
            not_stated_reason="field_absent",
            declared_not_analyzed=False,
        )

    raw_str = raw if isinstance(raw, str) else str(raw)
    norm = normalize_population_text(raw_str)
    if norm is None or norm == "":
        return ClassificationResult(
            population_description_raw=raw_str,
            population_class="not_stated",
            match_tier="none",
            matched_classes=[],
            matched_rule_ids=[],
            not_stated_reason="field_absent",
            declared_not_analyzed=False,
        )

    declared_not_analyzed = bool(_DECLARED_NOT_ANALYZED_RE.search(norm))

    # Tier 0: pure placeholder tokens
    for rule in VOCABULARY_RULES:
        if rule.tier != "placeholder":
            continue
        if _COMPILED_PATTERNS[rule.rule_id].search(norm):
            return ClassificationResult(
                population_description_raw=raw_str,
                population_class="not_stated",
                match_tier="placeholder",
                matched_classes=[],
                matched_rule_ids=[rule.rule_id],
                not_stated_reason="placeholder_token",
                declared_not_analyzed=declared_not_analyzed,
            )

    # Tier 1: named analysis-population labels with per-target span masking
    masked_by_target: dict[str, list[tuple[int, int]]] = {}
    t1_classes: list[str] = []
    t1_rule_ids: list[str] = []

    for rule in VOCABULARY_RULES:
        if rule.tier != "named_label":
            continue
        view = _mask_spans(norm, masked_by_target.get(rule.target_class, ()))
        forbids = _COMPILED_FORBIDS.get(rule.rule_id)
        if forbids is not None and forbids.search(view):
            continue
        matches = list(_COMPILED_PATTERNS[rule.rule_id].finditer(view))
        if matches:
            if rule.target_class not in t1_classes:
                t1_classes.append(rule.target_class)
            t1_rule_ids.append(rule.rule_id)
            if rule.masks_for:
                spans = [m.span() for m in matches]
                for masked_target in rule.masks_for:
                    masked_by_target.setdefault(masked_target, []).extend(spans)

    if t1_classes:
        ordered_classes = sorted(t1_classes, key=lambda c: _TAXONOMY_ORDER[c])
        final_class: PopulationClass = (
            ordered_classes[0]  # type: ignore[assignment]
            if len(ordered_classes) == 1
            else "ambiguous_multi"
        )
        return ClassificationResult(
            population_description_raw=raw_str,
            population_class=final_class,
            match_tier="named_label",
            matched_classes=ordered_classes,
            matched_rule_ids=t1_rule_ids,
            not_stated_reason=None,
            declared_not_analyzed=declared_not_analyzed,
        )

    # Tier 2: definitional prose (ONLY when Tier 1 matched zero classes)
    t2_classes: list[str] = []
    t2_rule_ids: list[str] = []
    for rule in VOCABULARY_RULES:
        if rule.tier != "definitional":
            continue
        forbids = _COMPILED_FORBIDS.get(rule.rule_id)
        if forbids is not None and forbids.search(norm):
            continue
        if _COMPILED_PATTERNS[rule.rule_id].search(norm):
            if rule.target_class not in t2_classes:
                t2_classes.append(rule.target_class)
            t2_rule_ids.append(rule.rule_id)

    if t2_classes:
        ordered_t2 = sorted(t2_classes, key=lambda c: _TAXONOMY_ORDER[c])
        final_t2_class: PopulationClass = (
            ordered_t2[0]  # type: ignore[assignment]
            if len(ordered_t2) == 1
            else "ambiguous_multi"
        )
        return ClassificationResult(
            population_description_raw=raw_str,
            population_class=final_t2_class,
            match_tier="definitional",
            matched_classes=ordered_t2,
            matched_rule_ids=t2_rule_ids,
            not_stated_reason=None,
            declared_not_analyzed=declared_not_analyzed,
        )

    return ClassificationResult(
        population_description_raw=raw_str,
        population_class="other_stated",
        match_tier="none",
        matched_classes=[],
        matched_rule_ids=[],
        not_stated_reason=None,
        declared_not_analyzed=declared_not_analyzed,
    )


# ---------------------------------------------------------------------------
# 4. Numeric parsing, participant-flow extraction, and strict 1:1 alignment
# ---------------------------------------------------------------------------


def _parse_number(value: Any) -> float | None:
    """Strictly parse a numeric count; missing/blank/'NA'/non-finite -> None.

    NEVER coerces missing or unparseable values to 0.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return None if (math.isnan(f) or math.isinf(f)) else f
    s = str(value).strip()
    if not s or s.upper() in ("NA", "N/A", "NONE", "NULL", "NAN"):
        return None
    try:
        f = float(s.replace(",", ""))
    except ValueError:
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _norm_group_title(title: str | None) -> str:
    """Normalized arm/group title for strict 1:1 FG<->OG title matching."""
    if title is None:
        return ""
    s = _ZERO_WIDTH_RE.sub("", str(title))
    s = _UNICODE_DASH_RE.sub("-", s).lower()
    s = _NON_ALNUM_GROUP_RE.sub(" ", s)
    return _WHITESPACE_RE.sub(" ", s).strip()


def _is_total_group_title(title: str | None) -> bool:
    norm = _norm_group_title(title)
    return bool(norm and _TOTAL_GROUP_TITLE_RE.match(norm))


def _is_pooled_group_title(title: str | None) -> bool:
    norm = _norm_group_title(title)
    return bool(norm and _POOLED_GROUP_TITLE_RE.search(norm))


def extract_flow_started(
    participant_flow_module: dict[str, Any] | None,
) -> tuple[list[FlowGroupStarted], str]:
    """Extract per-group STARTED counts from participantFlowModule.periods[0].

    Returns (flow_groups, status) where status is one of:
      - "ok"
      - "no_participant_flow"
      - "no_started_milestone"

    Audit holdout: RELIES ON ITS CALLER. It receives one trial's
    participantFlowModule, which carries no trial id and no ``in_audit``, so it
    cannot refuse a holdout trial; the caller must not pass one. Callers today:
    _parse_outcomes_payload (behind parse_study_outcomes, parse_outcomes_frame
    and build_outcomes_from_raw) in this module; none elsewhere in code (see
    the module docstring).
    """
    if not isinstance(participant_flow_module, dict) or not participant_flow_module:
        return [], "no_participant_flow"

    periods = participant_flow_module.get("periods")
    if not isinstance(periods, list) or not periods or not isinstance(periods[0], dict):
        return [], "no_started_milestone"

    milestones = periods[0].get("milestones")
    started_milestone: dict[str, Any] | None = None
    if isinstance(milestones, list):
        for ms in milestones:
            if not isinstance(ms, dict):
                continue
            if str(ms.get("type") or "").strip().upper() == "STARTED":
                started_milestone = ms
                break

    if started_milestone is None:
        return [], "no_started_milestone"

    ach_by_id: dict[str, dict[str, Any]] = {}
    ach_order: list[str] = []
    for ach in started_milestone.get("achievements") or []:
        if not isinstance(ach, dict):
            continue
        gid = ach.get("groupId") or ach.get("id")
        if gid is None:
            continue
        gid_str = str(gid)
        if gid_str not in ach_by_id:
            ach_order.append(gid_str)
        ach_by_id[gid_str] = ach

    raw_groups = participant_flow_module.get("groups")
    groups_list: list[dict[str, Any]] = (
        [g for g in raw_groups if isinstance(g, dict)]
        if isinstance(raw_groups, list) and raw_groups
        else [{"id": gid, "title": None} for gid in ach_order]
    )

    out: list[FlowGroupStarted] = []
    for grp in groups_list:
        gid = grp.get("id") or grp.get("groupId")
        if gid is None:
            continue
        gid_str = str(gid)
        title_raw = grp.get("title")
        title = None if title_raw is None else str(title_raw)
        ach = ach_by_id.get(gid_str, {})
        out.append(
            FlowGroupStarted(
                flow_group_id=gid_str,
                title=title,
                is_total_group=_is_total_group_title(title),
                started_participants=_parse_number(ach.get("numSubjects")),
                started_units=_parse_number(ach.get("numUnits")),
            )
        )
    return out, "ok"


def _titles_allow_generic_positional(
    fg_title: str | None,
    og_title: str | None,
    arm_count: int,
) -> bool:
    """Return True iff (fg_title, og_title) can safely join positionally.

    Allows positional fallback ONLY when titles match exactly, or when both
    sides are generic numbered placeholders with the same index (e.g. 'Arm 1'
    and 'Group 1'), or when blank on either side in a non-conflicting generic
    context. Never guesses across distinct non-matching arm names like
    'Drug A' vs 'Cohort X'.
    """
    fg_norm = _norm_group_title(fg_title)
    og_norm = _norm_group_title(og_title)
    if fg_norm == og_norm:
        return True
    if not fg_norm or not og_norm:
        return True
    fg_m = _GENERIC_NUMBERED_ARM_RE.match(fg_norm)
    og_m = _GENERIC_NUMBERED_ARM_RE.match(og_norm)
    if fg_m and og_m and fg_m.group(1) == og_m.group(1):
        return True
    if arm_count == 1 and (fg_m is not None or og_m is not None):
        return True
    return False


def align_flow_and_outcome_groups(
    flow_groups: list[FlowGroupStarted],
    outcome_groups: list[dict[str, Any]],
) -> tuple[dict[str, FlowGroupStarted], dict[str, str]]:
    """Strictly align non-total outcome groups (OG) to non-total flow groups (FG).

    Matching rules:
      1. Total/Overall OG groups are never aligned (reason='total_group_not_arm').
      2. Duplicate non-empty normalized arm titles in FG or OG block 1:1 join
         (reason='duplicate_group_titles').
      3. Unique normalized title match joins OG -> FG even if reordered.
      4. If all remaining arms have equal cardinality and every pair passes
         _titles_allow_generic_positional (e.g. 'Arm 1' <-> 'Group 1'), join
         positionally. Distinct non-matching titles ('Drug A' vs 'Cohort X')
         are NEVER guessed (reason='group_count_or_title_mismatch').

    Audit holdout: RELIES ON ITS CALLER. It receives group lists from one trial
    with no trial id and no ``in_audit``, so it cannot refuse a holdout trial;
    the caller must not pass one. Callers today: parse_outcome_measure in this
    module; none elsewhere in code (see the module docstring).
    """
    matched: dict[str, FlowGroupStarted] = {}
    reasons: dict[str, str] = {}

    fg_arms: list[FlowGroupStarted] = [fg for fg in flow_groups if not fg.is_total_group]
    og_arms: list[tuple[str, str | None]] = []

    for og in outcome_groups:
        if not isinstance(og, dict):
            continue
        og_id = og.get("id") or og.get("groupId")
        if og_id is None:
            continue
        og_id_str = str(og_id)
        title_raw = og.get("title")
        title = None if title_raw is None else str(title_raw)
        if _is_total_group_title(title):
            reasons[og_id_str] = "total_group_not_arm"
        else:
            og_arms.append((og_id_str, title))

    if not og_arms:
        return matched, reasons

    if not fg_arms:
        for og_id_str, _ in og_arms:
            reasons[og_id_str] = "group_count_or_title_mismatch"
        return matched, reasons

    fg_title_counts = Counter(
        t for t in (_norm_group_title(fg.title) for fg in fg_arms) if t
    )
    og_title_counts = Counter(
        t for t in (_norm_group_title(title) for _, title in og_arms) if t
    )
    if any(c > 1 for c in fg_title_counts.values()) or any(
        c > 1 for c in og_title_counts.values()
    ):
        for og_id_str, _ in og_arms:
            reasons[og_id_str] = "duplicate_group_titles"
        return matched, reasons

    fg_by_norm: dict[str, FlowGroupStarted] = {
        _norm_group_title(fg.title): fg
        for fg in fg_arms
        if _norm_group_title(fg.title)
    }

    unmatched_og: list[tuple[str, str | None]] = []
    used_fg_ids: set[str] = set()
    for og_id_str, title in og_arms:
        norm = _norm_group_title(title)
        if norm and norm in fg_by_norm:
            fg = fg_by_norm[norm]
            matched[og_id_str] = fg
            used_fg_ids.add(fg.flow_group_id)
        else:
            unmatched_og.append((og_id_str, title))

    if unmatched_og:
        remaining_fg = [fg for fg in fg_arms if fg.flow_group_id not in used_fg_ids]
        if len(remaining_fg) == len(unmatched_og) and all(
            _titles_allow_generic_positional(fg.title, og_title, len(remaining_fg))
            for fg, (_, og_title) in zip(remaining_fg, unmatched_og)
        ):
            for fg, (og_id_str, _) in zip(remaining_fg, unmatched_og):
                matched[og_id_str] = fg
        else:
            for og_id_str, _ in unmatched_og:
                reasons[og_id_str] = "group_count_or_title_mismatch"

    return matched, reasons


# ---------------------------------------------------------------------------
# 5. Denominator extraction & aggregation (Participants vs Count of Units)
# ---------------------------------------------------------------------------


def _is_participants_unit(units: Any) -> bool:
    return isinstance(units, str) and units.strip().lower() == "participants"


def _parse_denom_counts_map(denom: dict[str, Any]) -> tuple[bool, dict[str, float | None]]:
    """Return (has_counts_entries, {groupId: float | None}) for one denom dict."""
    counts = denom.get("counts")
    if not isinstance(counts, list) or len(counts) == 0:
        return False, {}
    mapping: dict[str, float | None] = {}
    for entry in counts:
        if not isinstance(entry, dict):
            continue
        gid = entry.get("groupId") or entry.get("id")
        if gid is None:
            continue
        mapping[str(gid)] = _parse_number(entry.get("value"))
    return (len(mapping) > 0), mapping


def _aggregate_counts(
    ordered_groups: list[tuple[str, str | None, bool]],
    counts_by_id: dict[str, float | None],
) -> tuple[str, float | None, str, bool, bool]:
    """Aggregate per-group counts into an overall analyzed count.

    Returns:
      (denom_status, overall_count, aggregation_method,
       flag_total_group_mismatch, flag_partial_arm_counts)
    """
    if not ordered_groups:
        return "empty_counts", None, "none", False, False

    if len(ordered_groups) == 1:
        gid, _, _ = ordered_groups[0]
        val = counts_by_id.get(gid)
        if val is None:
            return "non_numeric_or_na", None, "none", False, False
        return "present", val, "single_group", False, False

    total_groups = [g for g in ordered_groups if g[2]]
    arm_groups = [g for g in ordered_groups if not g[2]]

    # Case 1: Exactly one explicit Total/Overall summary column plus >=1 arms
    if len(total_groups) == 1 and len(arm_groups) >= 1:
        t_gid = total_groups[0][0]
        t_val = counts_by_id.get(t_gid)
        arm_vals = [counts_by_id.get(g[0]) for g in arm_groups]
        if t_val is not None:
            if all(v is not None for v in arm_vals):
                arm_sum = sum(v for v in arm_vals if v is not None)
                if abs(t_val - arm_sum) < 1e-9:
                    return "present", t_val, "explicit_total_group_sumcheck", False, False
                return "present", t_val, "explicit_total_group_unverified", True, False
            partial = any(v is not None for v in arm_vals)
            return "present", t_val, "explicit_total_group_unverified", True, partial
        # Explicit Total is NA/missing: evaluate remaining arms below
        eval_arms = arm_groups
    else:
        eval_arms = arm_groups if arm_groups else ordered_groups

    # Case 2: >=3 arms where the final group is pooled-titled and equals sum(preceding)
    if len(eval_arms) >= 3 and _is_pooled_group_title(eval_arms[-1][1]):
        last_val = counts_by_id.get(eval_arms[-1][0])
        prev_vals = [counts_by_id.get(g[0]) for g in eval_arms[:-1]]
        if last_val is not None and all(v is not None for v in prev_vals):
            prev_sum = sum(v for v in prev_vals if v is not None)
            if abs(last_val - prev_sum) < 1e-9:
                return "present", last_val, "pooled_group_sumcheck", False, False

    # Case 3: Disjoint arms (normal PRS outcomeMeasuresModule layout)
    eval_vals = [counts_by_id.get(g[0]) for g in eval_arms]
    non_null = [v for v in eval_vals if v is not None]
    if not non_null:
        return "non_numeric_or_na", None, "none", False, False
    if len(non_null) < len(eval_vals):
        return "partial_arm_counts", None, "none", False, True
    if len(eval_vals) == 1:
        return "present", non_null[0], "single_group", False, False
    return "present", sum(non_null), "sum_disjoint_arms", False, False


def _select_denoms_from_measure(
    measure: dict[str, Any],
) -> tuple[
    DenomSource,
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[list[dict[str, Any]]],
    list[list[dict[str, Any]]],
    bool,
]:
    """Collect measure-level or class-level participant & unit denom objects.

    Returns:
      (source_hint, measure_part_denoms, measure_unit_denoms,
       class_part_denoms_per_class, class_unit_denoms_per_class,
       class_part_incomplete)

    The per-class lists hold only the classes that have such a denom, so
    they cannot say whether every class had one. ``class_part_incomplete``
    does: it is True when the class level is used, at least one class (a
    dict in ``classes``) carries a "Participants" denom and at least one
    other carries none (its ``denoms`` missing, empty or holding unit
    counts only). Assumption: CT.gov reports one analysed count per class
    and group, so a class without a "Participants" denom has an unknown
    count, and another class's count does not stand for it.
    """
    m_denoms = measure.get("denoms")
    if isinstance(m_denoms, list) and len(m_denoms) > 0:
        valid = [d for d in m_denoms if isinstance(d, dict)]
        part = [d for d in valid if _is_participants_unit(d.get("units"))]
        units = [
            d
            for d in valid
            if isinstance(d.get("units"), str)
            and d["units"].strip() != ""
            and not _is_participants_unit(d.get("units"))
        ]
        if valid:
            return "measure", part, units, [], [], False

    classes = measure.get("classes")
    class_part: list[list[dict[str, Any]]] = []
    class_units: list[list[dict[str, Any]]] = []
    n_classes = 0
    if isinstance(classes, list):
        for cl in classes:
            if not isinstance(cl, dict):
                continue
            n_classes += 1
            cd = cl.get("denoms")
            if not isinstance(cd, list) or not cd:
                continue
            valid = [d for d in cd if isinstance(d, dict)]
            part = [d for d in valid if _is_participants_unit(d.get("units"))]
            units = [
                d
                for d in valid
                if isinstance(d.get("units"), str)
                and d["units"].strip() != ""
                and not _is_participants_unit(d.get("units"))
            ]
            if part:
                class_part.append(part)
            if units:
                class_units.append(units)

    if class_part or class_units:
        incomplete = 0 < len(class_part) < n_classes
        return "class_unanimous", [], [], class_part, class_units, incomplete
    return "none", [], [], [], [], False


def _extract_ordered_groups(
    measure: dict[str, Any],
    fallback_counts_maps: Sequence[dict[str, float | None]],
) -> list[tuple[str, str | None, bool]]:
    """Build ordered list of (group_id, title, is_total_group) from measure.groups."""
    raw_groups = measure.get("groups")
    out: list[tuple[str, str | None, bool]] = []
    seen: set[str] = set()
    if isinstance(raw_groups, list):
        for grp in raw_groups:
            if not isinstance(grp, dict):
                continue
            gid = grp.get("id") or grp.get("groupId")
            if gid is None:
                continue
            gid_str = str(gid)
            if gid_str in seen:
                continue
            seen.add(gid_str)
            title_raw = grp.get("title")
            title = None if title_raw is None else str(title_raw)
            out.append((gid_str, title, _is_total_group_title(title)))

    if not out:
        for cmap in fallback_counts_maps:
            for gid_str in cmap:
                if gid_str not in seen:
                    seen.add(gid_str)
                    out.append((gid_str, None, False))
    return out


def parse_outcome_measure(
    measure: dict[str, Any],
    outcome_index: int = 0,
    flow_groups: list[FlowGroupStarted] | None = None,
    flow_status: str = "no_participant_flow",
    enrollment: float | None = None,
) -> ParsedOutcomePopulation:
    """Parse one ClinicalTrials.gov outcomeMeasuresModule.outcomeMeasures entry.

    Audit holdout: RELIES ON ITS CALLER. It receives one outcome measure (and
    optionally that trial's flow groups), with no trial id and no ``in_audit``,
    so it cannot refuse a holdout trial; the caller must not pass one. Callers
    today: _parse_outcomes_payload (behind parse_study_outcomes,
    parse_outcomes_frame and build_outcomes_from_raw) in this module; none
    elsewhere in code (see the module docstring).
    """
    if not isinstance(measure, dict):
        return ParsedOutcomePopulation(
            outcome_index=outcome_index,
            outcome_type=None,
            measure_title=None,
            time_frame=None,
            reporting_status=None,
            param_type=None,
            unit_of_measure=None,
            population_description_raw=None,
            population_class=None,
            match_tier=None,
            parse_ok=False,
            parse_error_reason="measure_not_a_dict",
        )

    outcome_type = (
        None if measure.get("type") is None else str(measure.get("type"))
    )
    measure_title = (
        None if measure.get("title") is None else str(measure.get("title"))
    )
    time_frame = (
        None if measure.get("timeFrame") is None else str(measure.get("timeFrame"))
    )
    reporting_status = (
        None
        if measure.get("reportingStatus") is None
        else str(measure.get("reportingStatus"))
    )
    param_type = (
        None if measure.get("paramType") is None else str(measure.get("paramType"))
    )
    unit_of_measure = (
        None
        if measure.get("unitOfMeasure") is None
        else str(measure.get("unitOfMeasure"))
    )

    # 1. Pure text classification (strictly independent of counts / flow / gaps)
    raw_pop = measure.get("populationDescription")
    cls = classify_population_description(
        raw_pop if (raw_pop is None or isinstance(raw_pop, str)) else str(raw_pop)
    )

    # 2. Denominator parsing (Participants vs COUNT_OF_UNITS)
    type_units_raw = measure.get("typeUnitsAnalyzed")
    units_analyzed_type: str | None = (
        str(type_units_raw)
        if type_units_raw is not None and str(type_units_raw).strip() != ""
        else None
    )

    denom_source: DenomSource = "none"
    denom_status: str = "no_denoms_array"
    overall_participants_analyzed: float | None = None
    aggregation_method: str = "none"
    is_participant_count: bool = False
    units_denom_status: str = "no_denoms_array"
    overall_units_analyzed: float | None = None
    flag_total_group_mismatch: bool = False
    flag_partial_arm_counts: bool = False
    flag_class_varying_denoms: bool = False
    class_denom_min: float | None = None
    class_denom_max: float | None = None

    part_counts_by_id: dict[str, float | None] = {}
    unit_counts_by_id: dict[str, float | None] = {}
    ordered_groups: list[tuple[str, str | None, bool]] = []

    if reporting_status is not None and reporting_status.strip().upper() == "NOT_POSTED":
        denom_status = "not_posted"
        units_denom_status = "not_posted"
        ordered_groups = _extract_ordered_groups(measure, [])
    else:
        (
            src_hint,
            m_part,
            m_units,
            c_part,
            c_units,
            c_part_incomplete,
        ) = _select_denoms_from_measure(measure)

        # Resolve units_analyzed_type from unit denoms if typeUnitsAnalyzed absent
        if units_analyzed_type is None:
            if m_units:
                units_analyzed_type = str(m_units[0].get("units"))
            elif c_units and c_units[0]:
                units_analyzed_type = str(c_units[0][0].get("units"))

        # Parse active participant denom & unit denom depending on measure vs class level
        if src_hint == "measure":
            p_has_entries, p_map = (
                _parse_denom_counts_map(m_part[0]) if m_part else (False, {})
            )
            u_has_entries, u_map = (
                _parse_denom_counts_map(m_units[0]) if m_units else (False, {})
            )
            ordered_groups = _extract_ordered_groups(measure, [p_map, u_map])

            if m_part:
                denom_source = "measure"
                if not p_has_entries:
                    denom_status = "empty_counts"
                else:
                    part_counts_by_id = p_map
                    (
                        denom_status,
                        overall_participants_analyzed,
                        aggregation_method,
                        flag_total_group_mismatch,
                        flag_partial_arm_counts,
                    ) = _aggregate_counts(ordered_groups, part_counts_by_id)
                    is_participant_count = overall_participants_analyzed is not None
            elif m_units:
                denom_source = "none"
                denom_status = "units_only_no_participant_denom"
            else:
                denom_source = "none"
                denom_status = "no_denoms_array"

            if m_units:
                if not u_has_entries:
                    units_denom_status = "empty_counts"
                else:
                    unit_counts_by_id = u_map
                    (
                        units_denom_status,
                        overall_units_analyzed,
                        _,
                        _,
                        _,
                    ) = _aggregate_counts(ordered_groups, unit_counts_by_id)

        elif src_hint == "class_unanimous":
            # Evaluate participant denoms across classes
            parsed_class_parts: list[tuple[bool, dict[str, float | None]]] = [
                _parse_denom_counts_map(d_list[0]) for d_list in c_part
            ]
            parsed_class_units: list[tuple[bool, dict[str, float | None]]] = [
                _parse_denom_counts_map(d_list[0]) for d_list in c_units
            ]
            ordered_groups = _extract_ordered_groups(
                measure,
                [m for _, m in parsed_class_parts] + [m for _, m in parsed_class_units],
            )

            if parsed_class_parts:
                canonical_maps = [
                    tuple(sorted(m.items())) for _, m in parsed_class_parts
                ]
                all_empty = all(not has_e for has_e, _ in parsed_class_parts)
                if c_part_incomplete:
                    # Some classes carry no "Participants" denom: their
                    # analysed counts are unknown, so the classes that have
                    # one cannot be unanimous for the measure. Recorded as
                    # its own status with no count, never filled in from
                    # the classes that report one.
                    denom_source = "none"
                    denom_status = "class_level_incomplete"
                elif all_empty:
                    denom_source = "class_unanimous"
                    denom_status = "empty_counts"
                elif len(set(canonical_maps)) == 1:
                    denom_source = "class_unanimous"
                    part_counts_by_id = parsed_class_parts[0][1]
                    (
                        denom_status,
                        overall_participants_analyzed,
                        aggregation_method,
                        flag_total_group_mismatch,
                        flag_partial_arm_counts,
                    ) = _aggregate_counts(ordered_groups, part_counts_by_id)
                    is_participant_count = overall_participants_analyzed is not None
                else:
                    denom_source = "none"
                    denom_status = "class_level_only_varying"
                    flag_class_varying_denoms = True
                    class_totals: list[float] = []
                    for has_e, cmap in parsed_class_parts:
                        if not has_e:
                            continue
                        _, tot, _, _, _ = _aggregate_counts(ordered_groups, cmap)
                        if tot is not None:
                            class_totals.append(tot)
                    if class_totals:
                        class_denom_min = min(class_totals)
                        class_denom_max = max(class_totals)
            elif parsed_class_units:
                denom_source = "none"
                denom_status = "units_only_no_participant_denom"

            if parsed_class_units:
                u_canonical = [
                    tuple(sorted(m.items())) for _, m in parsed_class_units
                ]
                if all(not has_e for has_e, _ in parsed_class_units):
                    units_denom_status = "empty_counts"
                elif len(set(u_canonical)) == 1:
                    unit_counts_by_id = parsed_class_units[0][1]
                    (
                        units_denom_status,
                        overall_units_analyzed,
                        _,
                        _,
                        _,
                    ) = _aggregate_counts(ordered_groups, unit_counts_by_id)
                else:
                    units_denom_status = "class_level_only_varying"
        else:
            ordered_groups = _extract_ordered_groups(measure, [])

    # Determine DenomUnitKind (explicitly distinguishing PARTICIPANTS vs COUNT_OF_UNITS)
    has_units_signal = (
        overall_units_analyzed is not None
        or denom_status == "units_only_no_participant_denom"
        or units_denom_status
        in ("present", "partial_arm_counts", "class_level_only_varying")
        or (param_type is not None and param_type.strip().upper() == "COUNT_OF_UNITS")
    )
    if is_participant_count and (overall_units_analyzed is not None or units_denom_status == "present"):
        denom_unit_kind: DenomUnitKind = "BOTH"
    elif is_participant_count:
        denom_unit_kind = "PARTICIPANTS"
    elif has_units_signal:
        denom_unit_kind = "COUNT_OF_UNITS"
    else:
        denom_unit_kind = "NONE"

    # 3. Participant flow alignment & per-arm attrition gap (strictly separate from classification)
    raw_og_dicts: list[dict[str, Any]] = [
        {"id": gid, "title": title} for gid, title, _ in ordered_groups
    ]
    matched_fg: dict[str, FlowGroupStarted] = {}
    unmapped_reasons: dict[str, str] = {}
    if flow_status == "ok" and flow_groups is not None:
        matched_fg, unmapped_reasons = align_flow_and_outcome_groups(
            flow_groups, raw_og_dicts
        )

    arms: list[ArmAnalyzedRecord] = []
    for gid, title, is_total in ordered_groups:
        analyzed_p = part_counts_by_id.get(gid)
        analyzed_u = unit_counts_by_id.get(gid)
        if is_total:
            arms.append(
                ArmAnalyzedRecord(
                    outcome_group_id=gid,
                    title=title,
                    is_total_group=True,
                    analyzed_participants=analyzed_p,
                    analyzed_units=analyzed_u,
                    matched_flow_group_id=None,
                    started_participants=None,
                    arm_started_minus_analysed=None,
                    arm_gap_reason="total_group_not_arm",
                    flag_analyzed_exceeds_started=False,
                )
            )
            continue

        if flow_status != "ok" or flow_groups is None:
            reason = (
                flow_status
                if flow_status in ARM_GAP_REASONS
                else "no_participant_flow"
            )
            arms.append(
                ArmAnalyzedRecord(
                    outcome_group_id=gid,
                    title=title,
                    is_total_group=False,
                    analyzed_participants=analyzed_p,
                    analyzed_units=analyzed_u,
                    matched_flow_group_id=None,
                    started_participants=None,
                    arm_started_minus_analysed=None,
                    arm_gap_reason=reason,
                    flag_analyzed_exceeds_started=False,
                )
            )
            continue

        fg = matched_fg.get(gid)
        if fg is None:
            arms.append(
                ArmAnalyzedRecord(
                    outcome_group_id=gid,
                    title=title,
                    is_total_group=False,
                    analyzed_participants=analyzed_p,
                    analyzed_units=analyzed_u,
                    matched_flow_group_id=None,
                    started_participants=None,
                    arm_started_minus_analysed=None,
                    arm_gap_reason=unmapped_reasons.get(
                        gid, "group_count_or_title_mismatch"
                    ),
                    flag_analyzed_exceeds_started=False,
                )
            )
            continue

        started_p = fg.started_participants
        if started_p is None:
            arms.append(
                ArmAnalyzedRecord(
                    outcome_group_id=gid,
                    title=title,
                    is_total_group=False,
                    analyzed_participants=analyzed_p,
                    analyzed_units=analyzed_u,
                    matched_flow_group_id=fg.flow_group_id,
                    started_participants=None,
                    arm_started_minus_analysed=None,
                    arm_gap_reason="started_count_missing_or_na",
                    flag_analyzed_exceeds_started=False,
                )
            )
        elif analyzed_p is None:
            arms.append(
                ArmAnalyzedRecord(
                    outcome_group_id=gid,
                    title=title,
                    is_total_group=False,
                    analyzed_participants=None,
                    analyzed_units=analyzed_u,
                    matched_flow_group_id=fg.flow_group_id,
                    started_participants=started_p,
                    arm_started_minus_analysed=None,
                    arm_gap_reason="analyzed_count_missing",
                    flag_analyzed_exceeds_started=False,
                )
            )
        else:
            gap = started_p - analyzed_p
            arms.append(
                ArmAnalyzedRecord(
                    outcome_group_id=gid,
                    title=title,
                    is_total_group=False,
                    analyzed_participants=analyzed_p,
                    analyzed_units=analyzed_u,
                    matched_flow_group_id=fg.flow_group_id,
                    started_participants=started_p,
                    arm_started_minus_analysed=gap,
                    arm_gap_reason="computed",
                    flag_analyzed_exceeds_started=(gap < 0),
                )
            )

    # 4. Overall started & gap summaries
    overall_started_participants: float | None = None
    if flow_status == "ok" and flow_groups:
        fg_non_total = [fg for fg in flow_groups if not fg.is_total_group]
        if fg_non_total and all(fg.started_participants is not None for fg in fg_non_total):
            overall_started_participants = sum(
                fg.started_participants
                for fg in fg_non_total
                if fg.started_participants is not None
            )

    overall_started_minus_analyzed: float | None = (
        overall_started_participants - overall_participants_analyzed
        if (
            overall_started_participants is not None
            and overall_participants_analyzed is not None
        )
        else None
    )

    enr = _parse_number(enrollment)
    enrollment_minus_analyzed: float | None = (
        enr - overall_participants_analyzed
        if (enr is not None and overall_participants_analyzed is not None)
        else None
    )

    flag_positive_attrition_gap = (
        (
            overall_started_minus_analyzed is not None
            and overall_started_minus_analyzed > 0
        )
        or any(
            a.arm_started_minus_analysed is not None
            and a.arm_started_minus_analysed > 0
            for a in arms
        )
    )
    flag_analyzed_exceeds_started = (
        (
            overall_started_minus_analyzed is not None
            and overall_started_minus_analyzed < 0
        )
        or any(a.flag_analyzed_exceeds_started for a in arms)
    )
    flag_exceeds_enrollment = (
        enrollment_minus_analyzed is not None and enrollment_minus_analyzed < 0
    )

    return ParsedOutcomePopulation(
        outcome_index=outcome_index,
        outcome_type=outcome_type,
        measure_title=measure_title,
        time_frame=time_frame,
        reporting_status=reporting_status,
        param_type=param_type,
        unit_of_measure=unit_of_measure,
        population_description_raw=cls.population_description_raw,
        population_class=cls.population_class,
        match_tier=cls.match_tier,
        matched_classes=cls.matched_classes,
        matched_rule_ids=cls.matched_rule_ids,
        not_stated_reason=cls.not_stated_reason,
        declared_not_analyzed=cls.declared_not_analyzed,
        denom_source=denom_source,
        denom_status=denom_status,
        denom_unit_kind=denom_unit_kind,
        overall_participants_analyzed=overall_participants_analyzed,
        aggregation_method=aggregation_method,
        is_participant_count=is_participant_count,
        units_analyzed_type=units_analyzed_type,
        units_denom_status=units_denom_status,
        overall_units_analyzed=overall_units_analyzed,
        arms=arms,
        overall_started_participants=overall_started_participants,
        overall_started_minus_analyzed=overall_started_minus_analyzed,
        enrollment_minus_analyzed=enrollment_minus_analyzed,
        flag_positive_attrition_gap=flag_positive_attrition_gap,
        flag_analyzed_exceeds_started=flag_analyzed_exceeds_started,
        flag_exceeds_enrollment=flag_exceeds_enrollment,
        flag_total_group_mismatch=flag_total_group_mismatch,
        flag_partial_arm_counts=flag_partial_arm_counts,
        flag_class_varying_denoms=flag_class_varying_denoms,
        class_denom_min=class_denom_min,
        class_denom_max=class_denom_max,
        parse_ok=True,
        parse_error_reason=None,
        parser_rules_version=PARSER_RULES_VERSION,
        vocabulary_sha256=VOCABULARY_SHA256,
    )


# ---------------------------------------------------------------------------
# 6. Study-level entry points, raw retention & re-parse parity
# ---------------------------------------------------------------------------


def _nct_of(study: dict[str, Any]) -> str | None:
    """The nctId a parsed study is stamped with: the stored text, as a plain str.

    _plain_text, not str(): str() of a str subclass runs its __str__, which
    can say another trial's id than the content the holdout check read.
    """
    ident = (study.get("protocolSection") or {}).get("identificationModule") or {}
    val = ident.get("nctId")
    return None if val is None else _plain_text(val)


def _raw_nct_of_study(study: Any) -> Any:
    """protocolSection.identificationModule.nctId exactly as stored (no str()).

    Used only by the holdout check, which must see the value before _nct_of's
    str() coercion could turn a non-string into something id-shaped. ``study``
    is any mapping-like record (the check calls this only for one), read
    through ``.get``. Any missing or non-dict nested level yields None, which
    that check refuses.
    """
    proto = study.get("protocolSection")
    ident = proto.get("identificationModule") if isinstance(proto, dict) else None
    return ident.get("nctId") if isinstance(ident, dict) else None


def _raw_nct_of_retained(raw: Any) -> Any:
    """Top-level ``nct_id`` of a retained raw outcome record, exactly as stored.

    ``raw`` is any mapping-like record (the holdout check calls this only for
    one), read through ``.get``.
    """
    return raw.get("nct_id")


def _enrollment_of(study: dict[str, Any]) -> Any:
    return (
        ((study.get("protocolSection") or {}).get("designModule") or {})
        .get("enrollmentInfo")
        or {}
    ).get("count")


def _parse_outcomes_payload(
    nct_id: str | None,
    enrollment: Any,
    has_results_section: bool,
    participant_flow_module: dict[str, Any] | None,
    raw_measures: Any,
    snapshot_date: str | None = None,
    outcome_types: tuple[str, ...] | None = None,
) -> TrialAnalysisPopulations:
    """Shared implementation for parse_study_outcomes and build_outcomes_from_raw."""
    enr = _parse_number(enrollment)
    flow_groups, flow_status = extract_flow_started(participant_flow_module)
    has_participant_flow = flow_status != "no_participant_flow"

    allowed_types: set[str] | None = (
        {t.strip().upper() for t in outcome_types}
        if outcome_types is not None
        else None
    )

    measures_list = raw_measures if isinstance(raw_measures, list) else []
    parsed_outcomes: list[ParsedOutcomePopulation] = []

    for idx, measure in enumerate(measures_list):
        if allowed_types is not None and isinstance(measure, dict):
            m_type = str(measure.get("type") or "").strip().upper()
            if m_type not in allowed_types:
                continue
        parsed_outcomes.append(
            parse_outcome_measure(
                measure=measure,
                outcome_index=idx,
                flow_groups=flow_groups,
                flow_status=flow_status,
                enrollment=enr,
            )
        )

    primary_outcomes = [
        o
        for o in parsed_outcomes
        if (o.outcome_type or "").strip().upper() == "PRIMARY"
    ]
    primary_classes_seen = {
        o.population_class
        for o in primary_outcomes
        if o.parse_ok and o.population_class is not None
    }
    primary_population_classes = sorted(
        primary_classes_seen, key=lambda c: _TAXONOMY_ORDER[c]
    )

    return TrialAnalysisPopulations(
        nct_id=nct_id,
        snapshot_date=snapshot_date,
        enrollment=enr,
        has_results_section=bool(has_results_section),
        has_participant_flow=has_participant_flow,
        n_outcomes_total=len(parsed_outcomes),
        n_primary_outcomes=len(primary_outcomes),
        outcomes=parsed_outcomes,
        primary_population_classes=primary_population_classes,
        parse_ok=all(o.parse_ok for o in parsed_outcomes),
        parser_rules_version=PARSER_RULES_VERSION,
        vocabulary_sha256=VOCABULARY_SHA256,
    )


def parse_study_outcomes(
    study: dict[str, Any],
    snapshot_date: str | None = None,
    outcome_types: tuple[str, ...] | None = None,
) -> TrialAnalysisPopulations:
    """Parse all (or filtered) outcome measures from a raw CT.gov API v2 study dict.

    ``snapshot_date`` must be None or a YYYY-MM-DD calendar date (TypeError
    or ValueError otherwise), and ``outcome_types`` None or CT.gov outcome
    types (OUTCOME_MEASURE_TYPES), checked before the record is read: an
    audit id set passed positionally is refused, never copied into the
    output or used as a filter.

    Audit holdout: ENFORCES when ``study`` carries ``in_audit`` (a
    manifest-merged record does), at any depth and under any spelling or
    qualified name, whatever the record's type (see the module docstring).
    Raises PopulationAuditHoldoutError, before reading anything else in the
    record, if any in_audit is True (reason "in_audit_true"), is not
    exactly a bool or part of the record cannot be read
    ("in_audit_not_bool"), or is False while
    protocolSection.identificationModule.nctId is not exactly ^NCT[0-9]{8}$
    or another nct_id the record carries at any depth disagrees with it
    ("nct_id_malformed"). A refused record gets no parse-error record
    either: that would record the trial. A bare CT.gov study dict carries
    no ``in_audit``; for it this function relies on its caller to exclude
    holdout trials. Callers today: none in code (see the module docstring).
    """
    snapshot = _checked_snapshot_date(snapshot_date, "parse_study_outcomes")
    types = _checked_outcome_types(outcome_types, "parse_study_outcomes")
    _refuse_audit_holdout_record(study, "parse_study_outcomes", _raw_nct_of_study)
    return _parse_checked_study(study, snapshot, types)


def _parse_checked_study(
    study: Any,
    snapshot_date: str | None,
    outcome_types: tuple[str, ...] | None,
) -> TrialAnalysisPopulations:
    """parse_study_outcomes' body, for a study its caller has already checked.

    Callers: parse_study_outcomes (after _refuse_audit_holdout_record) and
    parse_outcomes_frame (after checking every study of its batch), each
    with arguments already checked by _checked_snapshot_date and
    _checked_outcome_types. It does no holdout check of its own, so nothing
    else may call it. Non-dict input the check let through (no flag and
    nothing unreadable: a scalar, or a mapping-like value or a record with
    named fields that carries no flag; or in_audit False on a mapping-like
    value with a canonical id) is recorded as the parse error
    "study_not_a_dict", as before.
    """
    if not isinstance(study, dict):
        return parse_error_trial_outcomes(
            nct_id=None,
            enrollment=None,
            snapshot_date=snapshot_date,
            reason="study_not_a_dict",
        )
    results_section = study.get("resultsSection")
    has_results = isinstance(results_section, dict) and bool(results_section)
    flow_mod = (
        results_section.get("participantFlowModule")
        if isinstance(results_section, dict)
        else None
    )
    om_mod = (
        results_section.get("outcomeMeasuresModule")
        if isinstance(results_section, dict)
        else None
    )
    raw_measures = (
        om_mod.get("outcomeMeasures") if isinstance(om_mod, dict) else []
    )
    return _parse_outcomes_payload(
        nct_id=_nct_of(study),
        enrollment=_enrollment_of(study),
        has_results_section=has_results,
        participant_flow_module=flow_mod if isinstance(flow_mod, dict) else None,
        raw_measures=raw_measures,
        snapshot_date=snapshot_date,
        outcome_types=outcome_types,
    )


def select_raw_outcome_populations(study: dict[str, Any]) -> dict[str, Any]:
    """Extract the minimal raw outcome + flow payload for offline re-parse parity.

    Audit holdout: ENFORCES when ``study`` carries ``in_audit`` (at any depth,
    under any spelling or qualified name, whatever the record's type), with
    the same checks and PopulationAuditHoldoutError reasons as
    parse_study_outcomes, so a holdout trial's raw outcomes are never
    retained. A bare CT.gov
    study dict carries no ``in_audit``; for it this function relies on its
    caller to exclude holdout trials. Callers today: none in code (see the
    module docstring). The returned payload carries no ``in_audit`` key.
    """
    _refuse_audit_holdout_record(
        study, "select_raw_outcome_populations", _raw_nct_of_study
    )
    results_section = study.get("resultsSection") if isinstance(study, dict) else None
    has_results = isinstance(results_section, dict) and bool(results_section)
    flow_mod = (
        results_section.get("participantFlowModule")
        if isinstance(results_section, dict)
        else None
    )
    om_mod = (
        results_section.get("outcomeMeasuresModule")
        if isinstance(results_section, dict)
        else None
    )
    raw_measures = (
        om_mod.get("outcomeMeasures")
        if isinstance(om_mod, dict) and isinstance(om_mod.get("outcomeMeasures"), list)
        else []
    )
    return {
        "nct_id": _nct_of(study) if isinstance(study, dict) else None,
        "enrollment": _enrollment_of(study) if isinstance(study, dict) else None,
        "has_results_section": has_results,
        "participant_flow_module": flow_mod if isinstance(flow_mod, dict) else None,
        "outcome_measures": raw_measures,
    }


def build_outcomes_from_raw(
    raw: dict[str, Any],
    snapshot_date: str | None = None,
    outcome_types: tuple[str, ...] | None = None,
) -> TrialAnalysisPopulations:
    """Re-parse a retained raw outcome record; exact parity with parse_study_outcomes.

    ``snapshot_date`` and ``outcome_types`` are checked as in
    parse_study_outcomes, before the record is read.

    Audit holdout: ENFORCES when ``raw`` carries ``in_audit`` (at any depth,
    for example in a merged ``manifest`` sub-record or on
    participant_flow_module, under any spelling or qualified name, whatever
    the record's type). Raises PopulationAuditHoldoutError if any in_audit
    is True ("in_audit_true"), is not exactly a bool or part of the record
    cannot be read ("in_audit_not_bool"), or is False while the record's
    top-level ``nct_id`` is not exactly ^NCT[0-9]{8}$ or another nct_id the
    record carries at any depth disagrees with it ("nct_id_malformed"). A
    record from select_raw_outcome_populations carries no ``in_audit``; for
    it this function relies on its caller to exclude holdout trials.
    Callers today: none in code (see the module docstring).
    """
    snapshot = _checked_snapshot_date(snapshot_date, "build_outcomes_from_raw")
    types = _checked_outcome_types(outcome_types, "build_outcomes_from_raw")
    _refuse_audit_holdout_record(raw, "build_outcomes_from_raw", _raw_nct_of_retained)
    if not isinstance(raw, dict):
        return parse_error_trial_outcomes(
            nct_id=None,
            enrollment=None,
            snapshot_date=snapshot,
            reason="raw_not_a_dict",
        )
    return _parse_outcomes_payload(
        # _plain_text, not str(): stamped with exactly the stored characters.
        nct_id=None if raw.get("nct_id") is None else _plain_text(raw.get("nct_id")),
        enrollment=raw.get("enrollment"),
        has_results_section=bool(raw.get("has_results_section")),
        participant_flow_module=(
            raw.get("participant_flow_module")
            if isinstance(raw.get("participant_flow_module"), dict)
            else None
        ),
        raw_measures=raw.get("outcome_measures") or [],
        snapshot_date=snapshot,
        outcome_types=types,
    )


def parse_error_trial_outcomes(
    nct_id: str | None,
    enrollment: Any,
    snapshot_date: str | None,
    reason: str,
) -> TrialAnalysisPopulations:
    """Emit an explicit parse-error record without dropping the trial.

    Audit holdout: RELIES ON ITS CALLER. It receives an nct_id but no
    ``in_audit`` and no audit id set, so it cannot tell a holdout trial from
    any other; the caller must not ask for a record for one. Callers today:
    parse_study_outcomes, parse_outcomes_frame (both through
    _parse_checked_study) and build_outcomes_from_raw in this module, each
    with nct_id=None, for non-dict input that passed its holdout check;
    none elsewhere in code (see the module docstring).
    """
    err_outcome = ParsedOutcomePopulation(
        outcome_index=0,
        outcome_type=None,
        measure_title=None,
        time_frame=None,
        reporting_status=None,
        param_type=None,
        unit_of_measure=None,
        population_description_raw=None,
        population_class=None,
        match_tier=None,
        parse_ok=False,
        parse_error_reason=reason,
    )
    return TrialAnalysisPopulations(
        nct_id=nct_id,
        snapshot_date=snapshot_date,
        enrollment=_parse_number(enrollment),
        has_results_section=False,
        has_participant_flow=False,
        n_outcomes_total=1,
        n_primary_outcomes=0,
        outcomes=[err_outcome],
        primary_population_classes=[],
        parse_ok=False,
        parser_rules_version=PARSER_RULES_VERSION,
        vocabulary_sha256=VOCABULARY_SHA256,
    )


# ---------------------------------------------------------------------------
# 7. Structural checks, vocabulary audit & pandas DataFrame helper (lazy import)
# ---------------------------------------------------------------------------


def structural_checks(
    trials: Iterable[TrialAnalysisPopulations],
) -> dict[str, dict[str, Any]]:
    """Structural invariants that must hold on any parsed snapshot.

    Audit holdout: RELIES ON ITS CALLER. It receives parsed
    TrialAnalysisPopulations, which carry an nct_id but no ``in_audit``, so it
    cannot refuse a holdout trial; the caller must not pass one. Callers today:
    none in code (see the module docstring).
    """
    trial_list = list(trials)
    all_outcomes = [o for t in trial_list for o in t.outcomes]
    checks: dict[str, dict[str, Any]] = {}

    def put(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = {"pass": bool(ok), "detail": detail}

    bad_class = [
        (t.nct_id, o.outcome_index, o.population_class, o.parse_ok)
        for t in trial_list
        for o in t.outcomes
        if (o.parse_ok and o.population_class not in POPULATION_TAXONOMY)
        or (not o.parse_ok and o.population_class is not None)
    ]
    put(
        "every_outcome_has_valid_population_class",
        not bad_class,
        f"{len(bad_class)} invalid outcomes (first: {bad_class[:3]})",
    )

    bad_ambig = [
        (t.nct_id, o.outcome_index)
        for t in trial_list
        for o in t.outcomes
        if o.parse_ok
        and ((o.population_class == "ambiguous_multi") != (len(o.matched_classes) >= 2))
    ]
    put(
        "ambiguous_multi_iff_ge_2_matched_classes",
        not bad_ambig,
        f"{len(bad_ambig)} inconsistent outcomes",
    )

    bad_not_stated = [
        (t.nct_id, o.outcome_index)
        for t in trial_list
        for o in t.outcomes
        if o.parse_ok
        and (
            (o.population_class == "not_stated")
            != (o.not_stated_reason in ("field_absent", "placeholder_token"))
        )
    ]
    put(
        "not_stated_has_explicit_reason",
        not bad_not_stated,
        f"{len(bad_not_stated)} inconsistent outcomes",
    )

    bad_denom = [
        (t.nct_id, o.outcome_index, o.denom_status)
        for t in trial_list
        for o in t.outcomes
        if o.denom_status not in DENOM_STATUS_LEVELS
    ]
    put(
        "denom_status_in_vocabulary",
        not bad_denom,
        f"{len(bad_denom)} invalid denom_status values",
    )

    bad_agg = [
        (t.nct_id, o.outcome_index, o.aggregation_method)
        for t in trial_list
        for o in t.outcomes
        if o.aggregation_method not in AGGREGATION_METHODS
    ]
    put(
        "aggregation_method_in_vocabulary",
        not bad_agg,
        f"{len(bad_agg)} invalid aggregation_method values",
    )

    bad_arms = [
        (t.nct_id, o.outcome_index, a.outcome_group_id, a.arm_gap_reason)
        for t in trial_list
        for o in t.outcomes
        for a in o.arms
        if a.arm_gap_reason not in ARM_GAP_REASONS
        or ((a.arm_gap_reason == "computed") != (a.arm_started_minus_analysed is not None))
    ]
    put(
        "arm_gap_reasons_consistent",
        not bad_arms,
        f"{len(bad_arms)} inconsistent arm gap records",
    )

    bad_part_flag = [
        (t.nct_id, o.outcome_index)
        for t in trial_list
        for o in t.outcomes
        if o.is_participant_count != (o.overall_participants_analyzed is not None)
    ]
    put(
        "is_participant_count_matches_overall_analyzed",
        not bad_part_flag,
        f"{len(bad_part_flag)} mismatched outcomes",
    )

    versions = {t.parser_rules_version for t in trial_list} | {
        o.parser_rules_version for o in all_outcomes
    }
    shas = {t.vocabulary_sha256 for t in trial_list} | {
        o.vocabulary_sha256 for o in all_outcomes
    }
    put(
        "single_parser_rules_version_and_vocab_sha",
        (not trial_list)
        or (versions == {PARSER_RULES_VERSION} and shas == {VOCABULARY_SHA256}),
        f"versions={sorted(versions)}, shas={sorted(shas)}",
    )
    return checks


def all_pass(checks: dict[str, dict[str, Any]]) -> bool:
    """Return True iff every check in structural_checks() passed."""
    return all(bool(entry.get("pass")) for entry in checks.values())


def population_audit(descriptions: Iterable[str | None]) -> list[dict[str, Any]]:
    """Aggregate distinct raw populationDescription strings with classifications.

    Audit holdout: RELIES ON ITS CALLER. It receives description strings with
    no trial id and no ``in_audit``, so it cannot refuse a holdout trial; the
    caller must not pass one. Callers today: none in code (see the module
    docstring).
    """
    counts: Counter[str | None] = Counter(descriptions)
    rows: list[dict[str, Any]] = []
    for raw, n in counts.items():
        res = classify_population_description(raw)
        rows.append(
            {
                "population_description_raw": raw,
                "count": n,
                "population_class": res.population_class,
                "match_tier": res.match_tier,
                "matched_classes": list(res.matched_classes),
                "matched_rule_ids": list(res.matched_rule_ids),
                "not_stated_reason": res.not_stated_reason,
                "declared_not_analyzed": res.declared_not_analyzed,
            }
        )
    rows.sort(
        key=lambda r: (
            -int(r["count"]),
            "" if r["population_description_raw"] is None else str(r["population_description_raw"]),
        )
    )
    return rows


def parse_outcomes_frame(
    studies: Iterable[dict[str, Any]],
    snapshot_date: str | None = None,
    outcome_types: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """pandas helper returning one flat row per parsed outcome measure.

    pandas is a declared dependency (requirements.txt) imported here and
    nowhere else in the module, so that the parser itself can be imported
    and used without it.

    ``snapshot_date`` and ``outcome_types`` are checked as in
    parse_study_outcomes, before any study is read; a snapshot_date that is
    not a date would otherwise be copied into every row.

    Audit holdout: ENFORCES on the batch as a whole, before any study is
    parsed (_refuse_audit_holdout_batch). A batch whose studies carry
    ``in_audit`` under different names (in_audit beside in_audit_y, what
    mixing two merges produces) is refused first ("in_audit_not_bool").
    Then every study is checked as parse_study_outcomes checks one (reasons
    as there); the first refused study raises PopulationAuditHoldoutError
    and no frame is returned, so no row for any study escapes. Last, a
    batch in which some studies carry ``in_audit`` and others do not is
    refused ("in_audit_not_bool"): it is what a manifest left join produces
    when a study finds no manifest row, and such a study is unknown, not
    clean (the sponsor frame path of sponsors/ecosystem.py refuses the same
    gap as a NaN in its in_audit column). ``studies`` must be an iterable
    of records: a single study (any mapping-like value, which iterates over
    its keys; a keyed row such as sqlite3.Row, a namedtuple, numpy record or
    dataclass instance, which iterates over its field values) or a string
    is refused with TypeError
    before anything in it is read. A batch in which no study carries ``in_audit`` relies on
    the caller to exclude holdout trials. Callers today: none in code (see
    the module docstring).
    """
    import pandas as pd  # lazy: the module's only pandas use (see DEPENDS above)

    snapshot = _checked_snapshot_date(snapshot_date, "parse_outcomes_frame")
    types = _checked_outcome_types(outcome_types, "parse_outcomes_frame")
    if (
        isinstance(studies, (str, bytes, bytearray))
        or _is_mapping_like(studies)
        or _is_keyed_record(studies)
        or _is_named_record(studies)
    ):
        raise TypeError(
            "parse_outcomes_frame expects an iterable of study records, got a "
            f"single {type(studies).__name__}; pass one study inside a list"
        )
    study_list: list[Any] = list(studies)
    _refuse_audit_holdout_batch(
        study_list, "parse_outcomes_frame (parse_study_outcomes check)", _raw_nct_of_study
    )

    rows: list[dict[str, Any]] = []
    for study in study_list:
        parsed = _parse_checked_study(study, snapshot, types)
        for out in parsed.outcomes:
            row = out.to_dict()
            row["nct_id"] = parsed.nct_id
            row["snapshot_date"] = parsed.snapshot_date
            row["enrollment"] = parsed.enrollment
            row["arms_count"] = len(out.arms)
            row.pop("arms", None)
            rows.append(row)
    return pd.DataFrame(rows)
