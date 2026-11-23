"""Non-industry sponsor ecosystem attribution & unified cohort composition.

READS   Curated ecosystem rules (`sponsors/ecosystem_rules.csv`), AACT-shaped
        sponsors DataFrames (nct_id | agency_class | lead_or_collaborator | name),
        or study records via `sponsors.adapter.records_to_frame`.
WRITES  Nothing during attribution; `write_ecosystem_baseline()` writes the
        SHA-256-keyed regression file (`tests/sponsors/expected_ecosystem_counts.json`)
        only during deliberate re-baselining.

Design invariants (shared with `sponsors/sponsor_roles.py` and `AGENTS.md`):
  1. List-driven exact matching ONLY in production attribution paths
     (`exact_literal`, `exact_normalized`, `subsidiary_of` via `sr._rule_hits`).
     Zero substring or regex matching in production attribution; plain
     case-insensitive substring `stem` is restricted strictly to
     `audit_ecosystem()` to surface unreviewed candidates for human curation.
  2. Three disjoint review states (`attributed`, `reviewed_excluded`,
     `unreviewed`) partition `literal_universe(frame)` and are NEVER collapsed.
  3. Every rule carries a non-empty `note` recording rationale; conflicting
     rules (attribute vs attribute without `shared=yes`, attribute vs exclude,
     or one `canonical` mapped to multiple sectors) raise
     `EcosystemRuleValidationError` (a `ValueError` subclass) at build time.
  4. Composes cleanly with `sponsors/company_aliases.csv` via
     `build_unified_index()` and `attribute_study_sponsors()`:
     - Industry rows receive `sector = "industry"`.
     - Cross-layer `attribute` vs `attribute` overlap raises unless EVERY
       claiming rule across both files has `shared == "yes"`.
     - `status == "exclude"` in `company_aliases.csv` ("reviewed: not industry")
       cleanly composes with `status == "attribute"` in `ecosystem_rules.csv`
       (e.g., Wake Forest University Health Sciences).
  5. `sponsors/company_aliases.csv` and `tests/sponsors/expected_counts.json`
     are never modified by this module.

DEPENDS  pandas (requirements.txt: pandas>=2.2.0), imported at module level:
         importing this module imports pandas. Its sibling modules
         sponsors.adapter, sponsors.baseline and sponsors.sponsor_roles import
         pandas at module level too. The other imports are from the Python
         standard library.

Audit holdout (in_audit). The only files this module reads or writes are the
two rules CSVs and the aggregate baseline JSON above; it never reads or
writes per-trial data. A trial whose `in_audit` is true in the site's
data/outcomes/index.json must not be attributed, counted or recorded. Which
entry point guards that:

  Enforce when the input carries `in_audit`, raising
  EcosystemAuditHoldoutError (see _refuse_audit_holdout_frame,
  _refuse_audit_holdout_records and _refuse_literal_universe_holdout):
      build_ecosystem_index, partition_ecosystem, build_unified_index,
      partition_unified, attribute_study_sponsors, sector_trials,
      organization_trials, trial_cohorts, audit_ecosystem,
      uncovered_ecosystem_rules, compute_ecosystem_baseline, and the two
      functions that take a literal universe, match_ecosystem_literals and
      check_cross_layer_conflicts (a universe sr.literal_universe builds
      carries none; one assembled by hand from trial rows may).
    A name "reads as" in_audit when it contains in_audit once case,
    spaces, hyphens, underscores, full-width forms and characters that
    print as nothing (bidi marks and isolates, zero-width and other format
    characters, variation selectors, tag characters, combining marks) are
    ignored ("inAudit", "IN_AUDIT", "in-audit" count, and so do qualified
    names: the pandas merge suffixes in_audit_x/in_audit_y, a json_normalize
    path "manifest.in_audit", "manifest_in_audit", "is_in_audit"; a name
    that is not text, such as an enum member, is read as its str() and its
    value); a name reads as nct_id when it contains nct_id or "NCT Number"
    so folded (nct_id_y or nct_id_m from a merge, nctNumber), never
    nctIdAliases. A sponsor frame or index carries `in_audit` in a column
    or a row-index level so named, or inside a cell or a row-index value (a
    dict, SimpleNamespace, deque, callable, str subclass, Index or other
    container, in any column dtype including sparse and other extension
    dtypes, or text that holds in_audit as a key, as a CSV round trip
    leaves a dict column); a True flag in the frame's `attrs` refuses it
    too. Anything that is not a DataFrame (a column mapping, say) is
    refused outright, as a holdout when it carries one. A study record
    carries it under a key, field, attribute or label so named at any depth
    (beside the nctId, on protocolSection, in a merged `manifest`
    sub-record, in a bare Index's name), whatever the record's type: a
    dict, any other mapping-like value (collections.UserDict,
    types.MappingProxyType, a collections.ChainMap, whose shadowed layers
    are read too, a pandas Series row), a namedtuple (a `df.itertuples()`
    row), a numpy record (a `df.to_records()` row), a dataclass or plain
    object, or any sized container; and beside those, as an instance
    attribute, a property or a class attribute of a value of any type that
    is not a builtin (a dict, str, dataclass or namedtuple subclass), or
    inside a callable (a functools.partial's arguments, a function's
    closure cells and defaults, a callable object's attributes)
    (_beside_items). Text that holds in_audit as a key counts as a flag,
    and since text is not a bool it is refused: JSON and repr manifests
    (escaped keys, Python 3.11's OrderedDict repr, a dict items view, a
    list of pairs, a tuple-keyed dict, a pandas Series repr), JSON encoded
    twice, YAML, "manifest=" followed by JSON, and a key in prose
    ("in_audit=True"), whether held as str or as bytes (UTF-8, latin-1,
    UTF-16, UTF-32) or a memoryview of them (_text_mentions). Such input is
    refused when any in_audit value is True, when any is not a bool, when a
    frame's flag has more than one row-level source, or when any nct_id is
    not exactly `^NCT[0-9]{8}$` or disagrees with another nct_id carried
    for the same row or record at any depth (a nested manifest naming
    another trial, or a manifest joined by position, whose id arrives as
    nct_id_m). Any input is refused when part of it cannot be read (fields
    without names, such as a plain tuple row or a namedtuple field renamed
    to "_N"; a bool outside a named field, used as a key or column label,
    in a column or row-index level without a text name, or left as the row
    label of a Series row from `iterrows()` or `apply(axis=1)` or in the
    "Index" field of an `itertuples()` row, which is where
    `set_index("in_audit")` puts the flag; a one-shot iterator; an
    attribute that raises when read), since that part may hold a flag
    (_scan_in_audit, _frame_cell_scan). attribute_study_sponsors also
    refuses a batch in which only some records carry `in_audit`, or carry
    it under different names, and a single record passed where a batch is
    expected. An AACT-shaped sponsor frame, a CT.gov study dict and the
    index these functions build carry no `in_audit`; for them every
    function above relies on its caller, as below. What the scan cannot
    see: a flag named in prose without ":" or "=" after it ("flagged
    in_audit"), a table rendered as text whose header names in_audit (a CSV
    line, DataFrame.to_string), a name written in compatibility letters
    other than full-width ASCII (mathematical alphanumerics: this module
    imports no unicodedata), a function's globals, and anything a caller
    removed before the call.
  Rely on the caller: every entry point above, for input without
  `in_audit` (no entry point receives an audit id set).
  Not trial-shaped (rules, file paths or aggregate counts, no nct_id):
  validate_ecosystem_rules, load_ecosystem_rules, sha256_file,
  load_expected_ecosystem, write_ecosystem_baseline, diff_ecosystem_blocks.
  write_ecosystem_baseline enforces it: a block, uncovered list or
  fixture_meta that is not the aggregate shape their producers give, or
  that names an NCT id anywhere, raises ValueError before the file is
  read or written (_refuse_trial_shaped_baseline).
  Callers today: none in code. The only importer in this repository is
  tests/sponsors/test_ecosystem.py. PR #97's outcomes layout
  (src/outcomes_layout.py at dc38220, on its own branch) reads sector names
  from a trial's or cohort manifest's `ecosystem_category` field without
  importing this module, and has no holdout backstop for it:
  _study_scalar_fields reads `ecosystem_category` from the trial, and
  build_trial_bundle copies it into the index row and passes it to
  derive_cohorts for every trial, in_audit ones included; no firewall
  vector checks that field for a holdout. Whoever fills that field
  must not compute it for a holdout trial; the backstop (refusing a
  holdout's `ecosystem_category`) belongs on that consumer branch, and its
  test there: this module may not import it.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, NamedTuple, NoReturn

import pandas as pd

from . import adapter
from . import baseline as bl
from . import sponsor_roles as sr

MODULE_VERSION: str = "v1.0"
RULES_FORMAT_VERSION: str = "ecosystem_rules@v1.0"

VALID_ECOSYSTEM_SECTORS: frozenset[str] = frozenset(
    {
        "nih",
        "us_federal_other",
        "nctn_cooperative_group",
        "academic_medical_center",
        "non_profit_foundation",
    }
)

UNIFIED_SECTORS: tuple[str, ...] = (
    "industry",
    "nih",
    "us_federal_other",
    "nctn_cooperative_group",
    "academic_medical_center",
    "non_profit_foundation",
)

ECOSYSTEM_RULE_COLUMNS: list[str] = [
    "canonical",
    "sector",
    "rule_type",
    "pattern",
    "status",
    "shared",
    "note",
]

ECOSYSTEM_INDEX_COLUMNS: list[str] = [
    "nct_id",
    "canonical",
    "sector",
    "entity",
    "role",
    "literal_name",
    "agency_class",
    "match_rule",
    "shared",
]

_SPONSORS_DIR: str = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT: str = os.path.dirname(_SPONSORS_DIR)
DEFAULT_INDUSTRY_RULES_PATH: str = os.path.join(_SPONSORS_DIR, "company_aliases.csv")
DEFAULT_ECOSYSTEM_RULES_PATH: str = os.path.join(_SPONSORS_DIR, "ecosystem_rules.csv")
EXPECTED_ECOSYSTEM_PATH: str = os.path.join(
    _REPO_ROOT, "tests", "sponsors", "expected_ecosystem_counts.json"
)


class EcosystemRuleValidationError(ValueError):
    """Raised on malformed, conflicting, or inconsistent ecosystem rules."""


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
    "in_audit_true",       # some row or record says it is an audit holdout trial
    "in_audit_not_bool",   # some in_audit value is present but is not a bool, or
                           # the input cannot be read well enough to tell
    "nct_id_malformed",    # in_audit is False but an nct_id is not ^NCT[0-9]{8}$,
                           # or two nct_ids for one row or record disagree
)


class EcosystemAuditHoldoutError(Exception):
    """Raised when an entry point is handed sponsor rows it must not attribute.

    `reason` is one of HOLDOUT_REFUSAL_REASONS. Deliberately NOT a subclass
    of ValueError or EcosystemRuleValidationError: an `except ValueError`
    written for a rules problem must not swallow a holdout refusal. Messages
    give counts, row positions and value types, never an nct_id or a sponsor
    name, so the refusal itself records nothing about a holdout trial.
    """

    def __init__(self, reason: str, message: str) -> None:
        if reason not in HOLDOUT_REFUSAL_REASONS:
            raise ValueError(f"unknown holdout refusal reason: {reason!r}")
        super().__init__(f"[{reason}] {message}")
        self.reason: str = reason


def _is_canonical_nct_id(value: Any) -> bool:
    """True iff value is a str that is exactly ^NCT[0-9]{8}$ (no coercion)."""
    return isinstance(value, str) and _CANONICAL_NCT_ID_RE.fullmatch(value) is not None


# How a key, column or index-level name is folded before it is compared with
# "in_audit" or "nct_id". Assumption: a name that differs from those only by case, spacing,
# hyphens, dashes or underscores (a camelCase merge, a spreadsheet header, a
# stray space), by full-width forms of ASCII, or by characters that print as
# nothing, was meant as that name. "Print as nothing" is _INVISIBLE_RE (the
# Unicode Default_Ignorable_Code_Point set: soft hyphen, combining grapheme
# joiner, Arabic letter mark, Hangul fillers, Mongolian free variation
# selectors and vowel separator, zero-width space/joiners, left-to-right and
# right-to-left marks, bidi embeddings, overrides and isolates, word joiner,
# invisible operators, variation selectors, BOM, tag characters; plus the
# combining-mark blocks, lone surrogates and the braille blank) and, through
# str.isprintable(), every other format, control, private-use or unassigned
# character that is not whitespace. Treating such a name as in_audit is
# fail-closed: an unrecognised holdout flag is the failure this guards
# against, while a false match can only refuse input. This module keeps to
# the standard-library imports its tests pin, which exclude unicodedata, so
# compatibility forms other than full-width ASCII (mathematical alphanumeric
# letters, circled letters) are not folded. Twin of the same rule in
# src/analysis_population_parser.py; the two modules import nothing from
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
# Separators a name is folded without: whitespace, underscore, hyphen, the
# Unicode dashes, the minus sign and the small hyphen-minus.
_NAME_IGNORED_RE: re.Pattern[str] = re.compile(r"[\s_\-\u2010-\u2015\u2212\ufe63]+")
_SPACE_RE: re.Pattern[str] = re.compile(r"\s+")
_FULLWIDTH_TO_ASCII: dict[int, int] = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_FULLWIDTH_RE: re.Pattern[str] = re.compile("[！-～]")
# ASCII control characters that are not whitespace, and DEL.
_ASCII_CONTROLS: dict[int, None] = {
    code: None for code in (*range(0x20), 0x7F) if not chr(code).isspace()
}
_ASCII_CONTROL_RE: re.Pattern[str] = re.compile(
    "[" + "".join(f"\\x{code:02x}" for code in _ASCII_CONTROLS) + "]"
)
# The bytes an ASCII text is folded without (whitespace, "_", "-", and the
# controls above): the byte-level twin of _NAME_IGNORED_RE plus
# _visible_text, for the fast in_audit test on ASCII text (_may_name_in_audit).
_ASCII_FOLD_DELETE: bytes = bytes(
    code for code in range(0x80)
    if chr(code).isspace() or chr(code) in "_-" or code in _ASCII_CONTROLS
)
_IN_AUDIT_NAME: str = "inaudit"
_NCT_ID_NAMES: tuple[str, ...] = ("nctid", "nctnumber")
_NCT_ID_EXCLUDED: str = "alias"
# pandas.api.types.infer_dtype results for an object column whose cells are
# all non-text scalars, so no cell can hold a nested in_audit flag or text
# that serializes one. Any other result ("string", "bytes", "mixed",
# "mixed-integer", "unknown-array", or one added by a later pandas) gets its
# cells scanned: unknown is scanned, not skipped.
_SCALAR_INFERRED_DTYPES: frozenset[str] = frozenset(
    {
        "empty", "floating", "integer", "mixed-integer-float", "decimal",
        "complex", "boolean", "datetime64", "datetime", "date", "timedelta64",
        "timedelta", "time", "period", "interval",
    }
)
# Folded names, memoised (names repeat across records); short texts only.
_FOLD_CACHE: dict[str, str] = {}
_NAME_KIND_CACHE: dict[str, tuple[bool, bool]] = {}
_FOLD_CACHE_LIMIT: int = 65536
_FOLD_CACHE_MAX_LEN: int = 256
# Stands for "no field name" among a node's (name, value) pairs: the value is
# read as one reached by position.
_NO_NAME: object = object()


def _visible_text(text: str) -> str:
    """`text` with full-width ASCII mapped to ASCII and invisible characters dropped.

    Whitespace is kept: it separates the words and keys of text. Method:
    pure-ASCII text loses only its control characters; other text loses
    _INVISIBLE_RE's characters and then, when anything that is neither
    printable nor whitespace is left, each such character (a format,
    control, private-use or unassigned code point, by str.isprintable()).
    Each step first searches, so text it would not change is not copied.
    """
    if text.isascii():
        if _ASCII_CONTROL_RE.search(text) is None:
            return text
        return text.translate(_ASCII_CONTROLS)
    if _FULLWIDTH_RE.search(text) is not None:
        text = text.translate(_FULLWIDTH_TO_ASCII)
    if _INVISIBLE_RE.search(text) is not None:
        text = _INVISIBLE_RE.sub("", text)
    if not text.isprintable() and not _SPACE_RE.sub(" ", text).isprintable():
        text = "".join(ch for ch in text if ch.isprintable() or ch.isspace())
    return text


def _may_name_in_audit(text: str) -> bool:
    """True when visible `text`, folded whole, contains "inaudit" (a cheap necessary test).

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
    """`text` folded for comparison with a name: visible, separators dropped, casefolded.

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
    part by part. None, _NO_NAME, a bool and a number have no text. Any
    other object (an enum member, a custom key type) is read as its str()
    and, when it has a text `value` (an enum member's), as that too:
    str(Key.IN_AUDIT) is "Key.IN_AUDIT", so an enum key that names in_audit
    reads as it whatever it holds.
    """
    if name is None or name is _NO_NAME or _is_bool_like(name):
        return []
    if isinstance(name, (str, bytes, bytearray, memoryview)):
        return _decoded_texts(name)
    if isinstance(name, tuple):
        return [text for part in name for text in _name_texts(part)]
    if isinstance(name, (int, float, complex)):
        return []
    texts = [str(name)]
    value = getattr(name, "value", None)
    if isinstance(value, (str, bytes, bytearray)):
        texts.extend(_decoded_texts(value))
    return texts


def _name_kind(name: Any) -> tuple[bool, bool]:
    """(reads as in_audit, reads as nct_id) for `name`, each of its texts folded once.

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
    """`name` folded for comparison (_fold_text of each _name_texts, joined by "|").

    None when the name has no text (None, a bool, a number).
    """
    texts = _name_texts(name)
    if not texts:
        return None
    return "|".join(_fold_text(text) for text in texts)


def _reads_as_in_audit(name: Any) -> bool:
    """True when `name` (or any part of a tuple name) reads as in_audit (see _name_kind)."""
    return _name_kind(name)[0]


def _reads_as_nct_id(name: Any) -> bool:
    """True when `name` (or any part of a tuple name) reads as nct_id (see _name_kind)."""
    return _name_kind(name)[1]


def _is_text_name(name: Any) -> bool:
    """True when `name` is text (str or bytes), or a tuple name with a text part."""
    if isinstance(name, tuple):
        return any(_is_text_name(part) for part in name)
    return isinstance(name, (str, bytes, bytearray))


def _holds_bool(name: Any) -> bool:
    """True when `name` is a bool, or a tuple name holding one (a MultiIndex label).

    A bool used as a key or label is a flag value that became a name
    (`set_index("in_audit")`, `to_dict("index")`), so it cannot be read.
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
# A quoted string (double or single quotes, backslash escapes inside).
_QUOTED: str = r""""(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'"""
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
# How many times text is unquoted and unescaped before it is given up on
# (_text_layers); text still changing after that is read as a flag.
_MAX_TEXT_LAYERS: int = 8


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
    """`text` with backslash escapes decoded (_escape_value), as JSON or a literal writes them.

    Decoded by pattern, not by a JSON or literal parser (the parser twin
    may call none, and the two keep one rule), so text that is not valid
    JSON or Python is decoded as far as it goes. A surrogate pair (how JSON
    writes a character beyond U+FFFF) is joined; a lone surrogate is
    dropped.
    """
    decoded = _ESCAPE_RE.sub(_escape_value, text)
    if _SURROGATE_RE.search(decoded):
        decoded = decoded.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "ignore")
    return decoded


def _text_layers(text: str) -> tuple[list[str], bool]:
    """`text` made visible, then what it reads as unquoted and unescaped, layer by layer.

    Returns (layers, settled). Method: the first layer is _visible_text of
    `text`; each next one is the previous one stripped, with one pair of
    matching outer quotes removed (JSON encoded twice, or the repr() of a
    JSON string, is one quoted string), its backslash escapes decoded
    (_unescaped: json.dumps writes every non-ASCII character of a key as
    \\uXXXX and repr() every unprintable one as \\xXX or \\uXXXX) and made
    visible again. Layers stop when one changes nothing (settled) or after
    _MAX_TEXT_LAYERS (not settled).
    """
    layers = [_visible_text(text)]
    for _ in range(_MAX_TEXT_LAYERS):
        current = layers[-1].strip()
        inner = current
        quoted = len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'"
        if not quoted and "\\" not in inner:
            return layers, True  # nothing to unquote or unescape: settled
        if quoted:
            inner = inner[1:-1]
        if "\\" in inner:
            inner = _unescaped(inner)
        inner = _visible_text(inner).strip()
        if inner == current:
            return layers, True
        layers.append(inner)
    return layers, False


def _serializes_a_container(text: str) -> bool:
    """True when visible `text`, stripped, opens and closes like a serialized container.

    `text` has been through _visible_text (invisible characters dropped,
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
    """The index labels of a pandas Series' repr, or [] when visible `text` is not one.

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
    """Every key visible text `layer` holds, unquoted and unescaped.

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
        if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
            key = key[1:-1]
        keys.append(_unescaped(key) if "\\" in key else key)
    return keys + _series_repr_labels(layer)


def _text_mentions(text: Any) -> tuple[bool, bool]:
    """(mentions in_audit, mentions nct_id) for a str, bytes, bytearray or memoryview.

    Method: text is not parsed (the parser twin may call no JSON or literal
    parser, and the two keep one rule). It is decoded (_decoded_texts) and
    read layer by layer (_text_layers), and each layer's keys (_text_keys: a key before ":" or
    "=", the first item of a bracketed pair, the parts of a tuple key, the
    labels of a pandas Series repr) are read with the name rules
    (_name_kind), so the same names count in text as in a dict.
      * A key that reads as in_audit is a mention wherever the text holds
        it: a JSON or repr manifest (escaped, encoded twice, as pairs), a
        YAML line, "manifest=" followed by JSON, a Series repr, and a key
        in prose such as "in_audit=True". A layer whose folded text
        (_fold_text) does not contain "inaudit" cannot hold one and is let
        go after a few string passes. Text still changing after
        _MAX_TEXT_LAYERS layers is read as a mention (fail-closed).
      * A key that reads as nct_id is a mention only in a layer that
        serializes a container (_serializes_a_container) or is a Series
        repr, so a sentence such as "see nctId: NCT..." in a description
        does not make a flagged record's ids disagree.
    Words that are not keys give (False, False): "Not in audit" and
    "[Practices in audit arm: ...]" mention nothing.
    """
    if not isinstance(text, (str, bytes, bytearray, memoryview)):
        return False, False
    in_audit = nct_id = False
    for decoded in _decoded_texts(text):
        layers, settled = _text_layers(decoded)
        in_audit = in_audit or not settled
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
    """True for anything that answers `keys()`, `items()` and `[]` like a dict.

    Method: duck-typed, not `isinstance(value, dict)` or
    `isinstance(value, collections.abc.Mapping)`. A dict subclass, a
    collections.UserDict, a types.MappingProxyType and a pandas Series (the
    row `df.iterrows()` yields, whose `in`, `.get` and `.items()` read
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
    """True when `type(value)` defines `name` as a callable (the protocol check)."""
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
    """A numpy structured scalar (numpy.void / numpy.record: `df.to_records()[i]`)."""
    names = getattr(getattr(value, "dtype", None), "names", None)
    return isinstance(names, tuple) and getattr(value, "ndim", None) == 0


def _is_named_record(value: Any) -> bool:
    """One record whose fields have names, other than a mapping-like value.

    A namedtuple (an `df.itertuples()` row), a numpy structured scalar (a
    `df.to_records()` row) or a dataclass instance. A batch entry point
    refuses one of these passed where a batch is expected: iterating it
    yields its field values, not records.
    """
    if isinstance(value, tuple) and _has_method(value, "_asdict"):
        return True
    if _is_structured_scalar(value):
        return True
    fields = getattr(type(value), "__dataclass_fields__", None)
    return isinstance(fields, dict) and not isinstance(value, type)


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


class _InAuditScan(NamedTuple):
    """What _scan_in_audit found in one record (or one frame cell, or `attrs`).

    `flags`: every value stored under a name that reads as in_audit, at
    any depth (text that mentions in_audit as a key is itself stored as a
    flag: a str, never a bool). `flag_names`: the folded names those flags
    were found under. `ids`: every value stored under a name that reads as
    nct_id, at any depth (_ID_IN_TEXT for one inside serialized text).
    `unreadable`: a description (a type name, never a value) of each part
    whose names or contents could not be read.
    """

    flags: tuple[Any, ...]
    flag_names: frozenset[str]
    ids: tuple[Any, ...]
    unreadable: tuple[str, ...]


def _keyed_items(node: Any) -> list[tuple[Any, Any]] | None:
    """(name, value) pairs of a node whose fields have names, else None.

    Method: a mapping-like value gives its items, and a dict items view
    (`d.items()`) its pairs; a namedtuple (what `df.itertuples()`
    yields) gives its fields through `_asdict()`; a numpy structured
    scalar (what `df.to_records()` holds) its `dtype.names`; a dataclass
    instance its `__dataclass_fields__`. Without this a namedtuple would be
    walked as a plain tuple, its values without their names, and a flag in
    it would be missed. A pandas object adds its named row-index levels and,
    for a Series, its own name (_pandas_axis_items). The field
    `df.itertuples()` names "Index" holds the row label, not a field, so
    its value has no name (_NO_NAME): after `set_index("in_audit")` it is
    the flag itself.
    """
    if _is_mapping_like(node):
        return list(node.items()) + _pandas_axis_items(node)
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
    """True when `name` is text or reads as in_audit or nct_id (an enum label)."""
    return _is_text_name(name) or any(_name_kind(name))


def _pandas_axis_items(node: Any) -> list[tuple[Any, Any]]:
    """(name, row value) pairs a pandas object holds in its axis names, and a Series' own name.

    Method: each named level of a Series' or DataFrame's row index gives
    one pair per row (`set_index("in_audit")` keeps the flag only there),
    and so does a Series' own text name (`df["in_audit"]`). A Series' own
    name that is not text is a value without a field name (_NO_NAME): the
    row `iterrows()` or `apply(axis=1)` hands out carries its row label
    there, so after `set_index("in_audit")` it is the flag itself (True,
    or (0, True) with `append=True`), and a bool there cannot be read.
    Duck-typed through `index.names`, `get_level_values` and `ndim`
    (1 for a Series; a DataFrame's `.name` would be a column named
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

    A bare Index keeps a flag in its `name` (`pd.Index([...],
    name="in_audit")`, what `df.set_index("in_audit").index` is), so its
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
    """Names defined in `klass`'s own namespace that read as in_audit or nct_id.

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

    A functools.partial (duck-typed: `func`, `args` and `keywords`)
    gives all three; a function gives its closure cells by free-variable
    name, its defaults by parameter name and its keyword-only defaults; a
    bound method gives `__self__` (unless a module) and `__func__`. A
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
      * its instance attributes (`vars()` and `__slots__`), all of them
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
    namespace, and a collections.ChainMap's layers (its `maps`: the True
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


def _scan_in_audit(
    record: Any, as_record: bool = True, cell_named: bool = True
) -> _InAuditScan:
    """Find every in_audit flag and nct_id in `record`, at any depth and spelling.

    `as_record` is False for a frame cell or a frame's `attrs`: the value is
    then read as one reached through a name (its column's, when
    `cell_named`; a column or index level whose label is not text, such as
    an int, passes False, so a bool there has lost its name), not as a
    record whose own fields must have names.

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
      * a node with named fields (_keyed_items: mapping-like values, dict
        items views, namedtuples, numpy structured scalars, dataclass
        instances, plus a pandas object's named axes and a Series' own
        name): every name that reads as in_audit gives a flag and every
        name that reads as nct_id an id (_name_kind: case, separators,
        invisible characters and qualifiers ignored; an enum key read as
        its str() and value); a key that is a bool, or a tuple holding
        one, is unreadable (_holds_bool); a namedtuple field that pandas or
        namedtuple(rename=True) renamed to "_N" has lost its name, so it is
        unreadable; a pandas object's `attrs` are walked;
      * a one-shot iterator or generator: unreadable (reading it would
        consume it, and what consumes it later could read a flag in it);
      * a pandas Index: its values under their level's name (_index_items);
      * a numpy array: object and text elements are walked, a structured
        array's records are walked, an array of bools is unreadable
        (unnamed bools), numbers are leaves;
      * any other sized, re-iterable container (list, tuple, set, deque,
        dict keys or values view): its elements, by position; an
        iterable that is not sized is unreadable;
      * at the top level of a record, an array or such a container is a
        record whose fields have no names (`df.itertuples(name=None)`
        yields one), so it is unreadable.
    Then, for any node whose type is not one of _PLAIN_TYPES (a subclass,
    a dataclass, a pandas object, a callable, a plain object), what it
    holds beside that (_beside_items: instance attributes, class
    attributes and properties that read as in_audit or nct_id, a callable's
    parts, a ChainMap's layers) is read as named fields too.
    Each container and non-plain node is visited once; `visited` keeps a
    reference to it as well as its id(), because a DataFrame's `items()`
    builds a fresh Series per column and a released object's id() can be
    reused, which would otherwise skip an unvisited node. Assumption: a
    stored sponsor record and a CT.gov API v2 study have no name that reads
    as in_audit, no bool outside a named field, and no text that holds an
    in_audit key, so a flag found anywhere was put there by a manifest
    merge and marks the record.
    """
    flags: list[Any] = []
    flag_names: set[str] = set()
    ids: list[Any] = []
    unreadable: list[str] = []
    # (node, reached through a text name, is the record itself)
    stack: list[tuple[Any, bool, bool]] = [
        (record, (not as_record) and cell_named, as_record)
    ]
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
                        if isinstance(key, (str, bytes, bytearray)) and _text_mentions(key)[0]:
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




def _row_values(values: Any) -> list[Any]:
    """A column-like value (Series, Index, list, tuple, array) as a list; else [value].

    Read with `to_numpy(dtype=object)` for pandas objects, so a missing value
    in a categorical, string or nullable-boolean column arrives as NaN or
    pd.NA (not a bool) instead of being skipped, as Series.map with
    na_action would skip it.
    """
    if isinstance(values, (pd.Series, pd.Index)):
        return values.to_numpy(dtype=object).tolist()
    if isinstance(values, (list, tuple)):
        return list(values)
    if getattr(values, "ndim", 0) >= 1 and callable(getattr(values, "reshape", None)):
        return pd.Series(values.reshape(-1), dtype=object).tolist()
    return [values]


def _frame_sources(
    frame: pd.DataFrame, reads_as: Callable[[Any], bool]
) -> list[tuple[str, list[Any]]]:
    """Every column and row-index level of `frame` whose name `reads_as` accepts.

    `reads_as` is _reads_as_in_audit or _reads_as_nct_id. Returns
    (description, one value per row) pairs. Method: in_audit (or nct_id)
    can sit in the columns or, after set_index, in the row index; both are
    row-aligned, so both are read. A description names a position or level
    number, never the label or a value, so a refusal records nothing about
    a trial.
    """
    sources: list[tuple[str, list[Any]]] = []
    for pos, label in enumerate(frame.columns):
        if reads_as(label):
            sources.append((f"column at position {pos}", _row_values(frame.iloc[:, pos])))
    for level, name in enumerate(frame.index.names):
        if reads_as(name):
            sources.append(
                (f"index level {level}", _row_values(frame.index.get_level_values(level)))
            )
    return sources


class _CellScan(NamedTuple):
    """What _frame_cell_scan found inside a frame's cells and row-index values.

    `flags`: every in_audit value held inside a cell or row-index value, at
    any depth. `row_ids`: (row position, value) for every nct_id held there,
    at any depth, so it can be compared with that row's own nct_id.
    `unreadable`: what could not be read (see _scan_in_audit), and every
    column or row-index level of bools without a text name.
    """

    flags: tuple[Any, ...]
    row_ids: tuple[tuple[int, Any], ...]
    unreadable: tuple[str, ...]


# dtype kinds whose values are numbers, dates or durations: no value of
# theirs can hold text, a container or a bool.
_SCALAR_DTYPE_KINDS: frozenset[str] = frozenset({"i", "u", "f", "c", "m", "M"})


def _frame_cell_scan(frame: pd.DataFrame) -> _CellScan:
    """in_audit flags and nct_ids held inside the cells and row-index values of `frame`.

    Method: every column and every row-index level is read alike, under its
    label (a level's name; `set_index` moves a column there, and a manifest
    can be a frame's row labels):
      * a label that is a bool, or a tuple holding one, is unreadable (a
        flag that became a column label);
      * a column or level of bools (a bool dtype, or an object column
        pandas infers as "boolean") whose label is not text and does not
        read as in_audit is unreadable: those bools lost their name, as an
        unnamed bool in a record has (`set_index("in_audit")` followed by
        `rename_axis(None)`, or a column labelled 0); under a text label
        they are leaves (the index's is_lead);
      * a column or level of numbers, dates or durations
        (_SCALAR_DTYPE_KINDS), or an object one pandas infers as all
        non-text scalars (_SCALAR_INFERRED_DTYPES), is skipped;
      * any other one (object, string, categorical, sparse or any other
        extension dtype) has each value read: text (exactly str or bytes)
        once per distinct value with _text_mentions, which is what
        _scan_in_audit does with text, so an AACT sponsor frame costs a
        few string tests per distinct name; anything else (a dict,
        SimpleNamespace, deque, callable, str subclass, Index or other
        container) is scanned on its own (_scan_in_audit, as a value
        reached through the label), so a nested manifest, as
        pd.DataFrame(records) builds from manifest-merged records, and a
        serialized one, as a CSV round trip leaves it, are both read.
    An nct_id found keeps its row. The frame's own `attrs` are checked
    separately (_refuse_frame_attrs_holdout).
    """
    flags: list[Any] = []
    row_ids: list[tuple[int, Any]] = []
    unreadable: list[str] = []
    axes: list[tuple[str, Any, Any]] = [
        (f"column at position {pos}", label, frame.iloc[:, pos])
        for pos, label in enumerate(frame.columns)
    ]
    axes.extend(
        (f"index level {level}", name, frame.index.get_level_values(level))
        for level, name in enumerate(frame.index.names)
    )
    for desc, label, values in axes:
        if _holds_bool(label):
            unreadable.append(f"a label holding a bool ({desc})")
        named = _is_text_name(label) or _reads_as_in_audit(label)
        dtype = values.dtype
        kind = getattr(dtype, "kind", None)
        if kind == "b":
            if not named:
                unreadable.append(f"bools without a text name ({desc})")
            continue
        if kind in _SCALAR_DTYPE_KINDS:
            continue
        if dtype == object:
            inferred = pd.api.types.infer_dtype(values, skipna=True)
            if inferred == "boolean" and not named:
                unreadable.append(f"bools without a text name ({desc})")
                continue
            if inferred in _SCALAR_INFERRED_DTYPES:
                continue
        cells = _row_values(values)
        hits: dict[Any, tuple[bool, bool]] = {}
        for value in {cell for cell in cells if type(cell) in (str, bytes)}:
            mentions = _text_mentions(value)
            if mentions[0] or mentions[1]:
                hits[value] = mentions
        for row, cell in enumerate(cells):
            if type(cell) in (str, bytes):
                in_audit_text, nct_id_text = hits.get(cell, (False, False))
                if in_audit_text:
                    flags.append(cell)
                if nct_id_text:
                    row_ids.append((row, _ID_IN_TEXT))
                continue
            scan = _scan_in_audit(cell, as_record=False, cell_named=named)
            flags.extend(scan.flags)
            row_ids.extend((row, value) for value in scan.ids)
            unreadable.extend(scan.unreadable)
    return _CellScan(flags=tuple(flags), row_ids=tuple(row_ids), unreadable=tuple(unreadable))


def _refuse_frame_attrs_holdout(frame: pd.DataFrame, where: str) -> None:
    """Refuse a frame whose `attrs` metadata marks it as holding holdout trials.

    Method: an in_audit flag found in `frame.attrs` (at any depth, any
    spelling or qualified name) that is not exactly a Python bool is refused
    ("in_audit_not_bool"), and one that is True is refused ("in_audit_true"):
    it says the frame as a whole is a holdout's. So is anything in `attrs`
    the scan cannot read ("in_audit_not_bool"). A False there is ignored
    and does not make the frame count as carrying in_audit. Assumption:
    pandas copies `attrs` onto frames derived from a frame (copy, merge,
    concat), so an index built from a checked frame would inherit a False
    flag while no longer having the row-level column; requiring that column
    for it would refuse a clean pipeline, while ignoring a True would let a
    holdout frame through.
    """
    attrs = getattr(frame, "attrs", None)
    if not isinstance(attrs, dict) or not attrs:
        return
    scan = _scan_in_audit(attrs, as_record=False)
    not_bool = [f for f in scan.flags if type(f) is not bool]
    if not_bool:
        raise EcosystemAuditHoldoutError(
            "in_audit_not_bool",
            f"{where}: frame.attrs holds {len(not_bool)} in_audit value(s) that are "
            f"not exactly True or False (first: {type(not_bool[0]).__name__}); "
            "refusing rather than guessing",
        )
    if any(scan.flags):
        raise EcosystemAuditHoldoutError(
            "in_audit_true",
            f"{where}: frame.attrs marks the frame as in_audit holdout data; it "
            "must not be attributed, counted or recorded",
        )
    if scan.unreadable:
        raise EcosystemAuditHoldoutError(
            "in_audit_not_bool",
            f"{where}: frame.attrs holds {len(scan.unreadable)} part(s) the holdout "
            f"check cannot read (first: {scan.unreadable[0]}); refusing rather than "
            "guessing",
        )


def _refuse_non_frame(frame: Any, where: str) -> NoReturn:
    """Refuse input that is not a DataFrame, as a holdout if it carries one.

    Method: the frame entry points read columns by name, so a column mapping
    ({column: Series}) gets as far as sr.literal_universe and partitions its
    sponsor names, while the check needs a frame's columns and row index to
    see every flag. Such input is scanned first (_scan_in_audit, each flag
    read as one value per row by _row_values), so a holdout is refused as
    one ("in_audit_not_bool" or "in_audit_true"); anything else is refused
    with TypeError. Nothing is attributed either way.
    """
    values: list[Any] = []
    for flag in _scan_in_audit(frame).flags:
        values.extend(_row_values(flag))
    not_bool = [v for v in values if not pd.api.types.is_bool(v)]
    if not_bool:
        raise EcosystemAuditHoldoutError(
            "in_audit_not_bool",
            f"{where}: given a {type(frame).__name__}, not a DataFrame, carrying "
            f"{len(not_bool)} in_audit value(s) that are not a bool; refusing "
            "rather than guessing",
        )
    held = sum(1 for v in values if bool(v))
    if held:
        raise EcosystemAuditHoldoutError(
            "in_audit_true",
            f"{where}: given a {type(frame).__name__}, not a DataFrame, in which "
            f"{held} in_audit value(s) mark in_audit holdout trials; they must "
            "not be attributed, counted or recorded",
        )
    raise TypeError(
        f"{where} expects a pandas DataFrame, got {type(frame).__name__}; the "
        "holdout check needs a frame's columns and row index"
    )


def _refuse_audit_holdout_frame(frame: Any, where: str) -> None:
    """Refuse a frame carrying `in_audit` unless every row is a canonical non-holdout.

    Method: anything that is not a DataFrame is refused (_refuse_non_frame),
    and so is a frame whose `attrs` metadata holds a True, non-bool or
    unreadable flag (_refuse_frame_attrs_holdout; a False there is ignored).
    A frame carries in_audit when a column or a row-index level has a name
    that reads as in_audit (_reads_as_in_audit: case, separators and
    qualifiers such as in_audit_y or manifest.in_audit ignored), or a cell
    or row-index value holds one at any depth, or holds text that holds one
    as a key (_frame_cell_scan). A frame with a cell or row-index value the
    scan cannot read, or a column or row-index level of bools without a
    text name (`set_index("in_audit")` then `rename_axis(None)`, a column
    labelled 0), is refused whether it carries in_audit or not
    ("in_audit_not_bool"), since that part may hold one. A frame that carries none is passed through
    unchanged (the caller is responsible for it; see the module docstring).
    A frame that carries in_audit is checked in this order, over every row,
    before any rule runs:
      1. each row-level value must be a bool, i.e. Python bool or
         numpy.bool_ (`pd.api.types.is_bool`; a bool-dtype column or index
         holds numpy.bool_), so NaN, None, pd.NA, 0/1 and the strings
         "True"/"False" of a CSV read with dtype=str are refused, never
         coerced; a flag inside a cell must be exactly a Python bool, as a
         record's must, so serialized text is refused ("in_audit_not_bool");
      2. no value may be True ("in_audit_true");
      3. there must be exactly one row-level source (one column or one
         index level), so neither two disagreeing flags (in_audit_x beside
         in_audit_y) nor flags found only inside cells can describe the
         rows ("in_audit_not_bool");
      4. every nct_id must be exactly `^NCT[0-9]{8}$`, read as stored, from
         each column or index level whose name reads as nct_id
         (_reads_as_nct_id: nct_id, nct_id_m or nct_id_y from a merge,
         "NCT Number"); there must be at least one, and when there are
         several they must agree row by row (a manifest joined by position
         gives another trial's id under nct_id_m), as must every nct_id
         held inside a cell or row-index value of that row, with or without
         a flag beside it ("nct_id_malformed").
    """
    if not isinstance(frame, pd.DataFrame):
        _refuse_non_frame(frame, where)
    _refuse_frame_attrs_holdout(frame, where)
    sources = _frame_sources(frame, _reads_as_in_audit)
    cells = _frame_cell_scan(frame)
    if not sources and not cells.flags:
        if cells.unreadable:
            raise EcosystemAuditHoldoutError(
                "in_audit_not_bool",
                f"{where}: {len(cells.unreadable)} part(s) of the frame's cells cannot "
                f"be read by the holdout check (first: {cells.unreadable[0]}), so "
                "whether it carries in_audit is unknown; refusing rather than guessing",
            )
        return
    for desc, values in sources:
        bad = [i for i, v in enumerate(values) if not pd.api.types.is_bool(v)]
        if bad:
            raise EcosystemAuditHoldoutError(
                "in_audit_not_bool",
                f"{where}: {len(bad)} row(s) carry an in_audit value that is not "
                f"a bool (first at row position {bad[0]}, type "
                f"{type(values[bad[0]]).__name__}, {desc}); refusing rather than "
                "guessing",
            )
    cell_not_bool = [f for f in cells.flags if type(f) is not bool]
    if cell_not_bool:
        raise EcosystemAuditHoldoutError(
            "in_audit_not_bool",
            f"{where}: {len(cell_not_bool)} in_audit value(s) inside cells are not "
            f"exactly True or False (first: {type(cell_not_bool[0]).__name__}); "
            "refusing rather than guessing",
        )
    for desc, values in sources:
        held = [i for i, v in enumerate(values) if bool(v)]
        if held:
            raise EcosystemAuditHoldoutError(
                "in_audit_true",
                f"{where}: {len(held)} row(s) belong to in_audit holdout trials "
                f"(first at row position {held[0]}, {desc}); they must not be "
                "attributed, counted or recorded",
            )
    if any(cells.flags):
        raise EcosystemAuditHoldoutError(
            "in_audit_true",
            f"{where}: {sum(cells.flags)} in_audit value(s) inside cells mark "
            "in_audit holdout trials; they must not be attributed, counted or "
            "recorded",
        )
    if cells.unreadable:
        raise EcosystemAuditHoldoutError(
            "in_audit_not_bool",
            f"{where}: {len(cells.unreadable)} part(s) of the frame's cells cannot "
            f"be read by the holdout check (first: {cells.unreadable[0]}); refusing "
            "rather than guessing",
        )
    if len(sources) != 1:
        raise EcosystemAuditHoldoutError(
            "in_audit_not_bool",
            f"{where}: frame carries in_audit in {len(sources)} columns or index "
            "levels" + (" and inside cells" if cells.flags else "")
            + "; exactly one row-level in_audit column or index level is required",
        )
    id_sources = _frame_sources(frame, _reads_as_nct_id)
    if not id_sources:
        raise EcosystemAuditHoldoutError(
            "nct_id_malformed",
            f"{where}: frame carries in_audit but has no nct_id column or index level",
        )
    for desc, ids in id_sources:
        bad = [i for i, v in enumerate(ids) if not _is_canonical_nct_id(v)]
        if bad:
            raise EcosystemAuditHoldoutError(
                "nct_id_malformed",
                f"{where}: {len(bad)} row(s) have an nct_id that is not exactly "
                f"^NCT[0-9]{{8}}$ (first at row position {bad[0]}, type "
                f"{type(ids[bad[0]]).__name__}, {desc})",
            )
    first_ids = id_sources[0][1]
    for desc, ids in id_sources[1:]:
        bad = [i for i, (a, b) in enumerate(zip(first_ids, ids)) if a != b]
        if bad:
            raise EcosystemAuditHoldoutError(
                "nct_id_malformed",
                f"{where}: {len(bad)} row(s) carry nct_id values that disagree "
                f"between {id_sources[0][0]} and {desc} (first at row position "
                f"{bad[0]}); refusing rather than choosing one",
            )
    stray = [
        row
        for row, value in cells.row_ids
        if not (isinstance(value, str) and value == first_ids[row])
    ]
    if stray:
        raise EcosystemAuditHoldoutError(
            "nct_id_malformed",
            f"{where}: {len(stray)} nct_id value(s) inside cells disagree with their "
            f"row's nct_id (first at row position {stray[0]}); a manifest merged "
            "onto the wrong trial is refused rather than resolved",
        )


# The columns sr.literal_universe builds and sr._rule_hits reads.
_LITERAL_UNIVERSE_COLUMNS: tuple[str, ...] = ("name", "norm", "parent_norm")


def _refuse_literal_universe_holdout(uniq: Any, where: str) -> None:
    """Refuse a literal universe that is not one, or that carries in_audit.

    Method: a DataFrame lacking a column sr.literal_universe builds
    (_LITERAL_UNIVERSE_COLUMNS) is not a universe and raises KeyError, as
    reading it would have (a sponsor frame passed by mistake still fails
    that way). Then the universe gets the full frame check
    (_refuse_audit_holdout_frame): a universe built by sr.literal_universe
    holds distinct names only, with no nct_id and no in_audit, so it passes
    unchanged; one that was assembled by hand and still carries a trial's
    in_audit is refused like a sponsor frame.
    """
    if isinstance(uniq, pd.DataFrame):
        missing = [c for c in _LITERAL_UNIVERSE_COLUMNS if c not in uniq.columns]
        if missing:
            raise KeyError(
                f"{where} expects a literal universe (sr.literal_universe columns "
                f"{list(_LITERAL_UNIVERSE_COLUMNS)}); missing {missing}"
            )
    _refuse_audit_holdout_frame(uniq, where)


def _raw_record_nct_id(record: Any) -> Any:
    """The nct_id a study record would be attributed under, exactly as stored.

    A raw CT.gov API v2 dict (has `protocolSection`) keeps it at
    protocolSection.identificationModule.nctId; a CivicSample stored record
    keeps it at top-level `nct_id`. This mirrors
    _normalize_study_record_for_adapter without its .get() fallbacks, so a
    missing or non-dict nested level yields None, which the holdout check
    refuses. `record` is any mapping-like value (the check calls this only
    for one), read through `in` and `.get`.
    """
    if "protocolSection" not in record:
        return record.get("nct_id")
    proto = record.get("protocolSection")
    ident = proto.get("identificationModule") if isinstance(proto, dict) else None
    return ident.get("nctId") if isinstance(ident, dict) else None


def _check_scanned_record(record: Any, scan: _InAuditScan, where: str) -> None:
    """Refuse one scanned record unless it is a canonical non-holdout or carries no flag.

    Method, in this order, before any record is adapted:
      1. if it carries a flag: every flag must be exactly a Python bool
         (`type(v) is bool`; records come from JSON, so numpy bools, 0/1,
         strings, None and serialized text are refused, never coerced)
         ("in_audit_not_bool"); no flag may be True ("in_audit_true"); the
         record's nct_id (_raw_record_nct_id; None for a record that is not
         mapping-like) must be exactly `^NCT[0-9]{8}$`, and every nct_id
         the record carries at any depth (a top-level key, one beside a
         flag, one in a nested sub-record, one inside serialized text) must
         equal it, because a manifest row merged onto another trial's
         payload describes a different trial ("nct_id_malformed");
      2. flag or not: nothing in it may be unreadable (_scan_in_audit), since
         a part whose names or contents cannot be read may hold a flag
         ("in_audit_not_bool": whether it carries in_audit is unknown).
    """
    if scan.flags:
        for flag in scan.flags:
            if type(flag) is not bool:
                raise EcosystemAuditHoldoutError(
                    "in_audit_not_bool",
                    f"{where} carries an in_audit value that is not exactly True or "
                    f"False ({type(flag).__name__}); refusing rather than guessing",
                )
        if any(scan.flags):
            raise EcosystemAuditHoldoutError(
                "in_audit_true",
                f"{where} is an in_audit holdout trial; it must not be attributed, "
                "counted or recorded",
            )
        raw_nct = _raw_record_nct_id(record) if _is_mapping_like(record) else None
        if not _is_canonical_nct_id(raw_nct):
            raise EcosystemAuditHoldoutError(
                "nct_id_malformed",
                f"{where} has in_audit False but an nct_id ({type(raw_nct).__name__}) "
                "that is not exactly ^NCT[0-9]{8}$ (ASCII, case-sensitive, unstripped)",
            )
        disagreeing = [
            other for other in scan.ids if not (isinstance(other, str) and other == raw_nct)
        ]
        if disagreeing:
            raise EcosystemAuditHoldoutError(
                "nct_id_malformed",
                f"{where} has in_audit False but {len(disagreeing)} other nct_id "
                "value(s) in it, at any depth, disagree with the id it would be "
                "attributed under; refusing rather than choosing one",
            )
    if scan.unreadable:
        raise EcosystemAuditHoldoutError(
            "in_audit_not_bool",
            f"{where} has {len(scan.unreadable)} part(s) the holdout check cannot "
            f"read (first: {scan.unreadable[0]}), so whether it carries in_audit is "
            "unknown; refusing rather than guessing",
        )


def _refuse_audit_holdout_records(records: list[Any], where: str) -> None:
    """Refuse records carrying `in_audit` unless each is a canonical non-holdout.

    Method: every record is scanned first for in_audit flags and nct_ids at
    any depth and under any spelling or qualified name (_scan_in_audit),
    whatever its type: a dict, any other mapping-like value (UserDict,
    MappingProxyType, a pandas Series row), a namedtuple, a numpy record, a
    dataclass or plain object, or a list. sponsors.adapter reads a record
    through `.get` and attributes it, so none may be skipped. Then, before
    any record is adapted, in this order:
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
         produces when a record finds no manifest row, and the frame path
         refuses the same gap as a NaN in its column.
    A batch in which no record carries a flag and nothing is unreadable is
    passed through unchanged (the caller is responsible for it).
    """
    scans = [_scan_in_audit(record) for record in records]
    carrying_names = [(pos, scan.flag_names) for pos, scan in enumerate(scans) if scan.flags]
    if carrying_names:
        first_pos, first_names = carrying_names[0]
        differing = [pos for pos, names in carrying_names if names != first_names]
        if differing:
            raise EcosystemAuditHoldoutError(
                "in_audit_not_bool",
                f"{where}: {len(differing)} of {len(carrying_names)} records carrying "
                f"in_audit carry it under other names than the record at position "
                f"{first_pos} (first at position {differing[0]}); one manifest merge "
                "names the flag alike in every record, so the batch is refused",
            )
    for pos, (record, scan) in enumerate(zip(records, scans)):
        _check_scanned_record(record, scan, f"{where}: record at position {pos}")
    carrying = [bool(scan.flags) for scan in scans]
    n_carrying = sum(carrying)
    if 0 < n_carrying < len(records):
        raise EcosystemAuditHoldoutError(
            "in_audit_not_bool",
            f"{where}: {n_carrying} of {len(records)} records carry in_audit and "
            f"{len(records) - n_carrying} do not (first at position "
            f"{carrying.index(False)}); in a manifest-merged batch a record without "
            "in_audit is unknown, not clean, so the batch is refused",
        )


# ---------------------------------------------------------------------------
# 1. Loading & validating curated ecosystem rules
# ---------------------------------------------------------------------------


def validate_ecosystem_rules(rules: pd.DataFrame) -> pd.DataFrame:
    """Validate an ecosystem rules DataFrame and return a normalized copy.

    Enforces:
      - required columns (canonical, sector, rule_type, pattern, status, shared, note)
      - non-empty canonical and pattern
      - sector in VALID_ECOSYSTEM_SECTORS
      - rule_type in sr.VALID_RULE_TYPES (no substring/regex rules)
      - status in sr.VALID_STATUS
      - shared in sr.VALID_SHARED
      - non-empty note on every rule
      - single-sector consistency per canonical across all status == 'attribute' rows
    """
    df = rules.copy()
    for col, default in [("status", "attribute"), ("shared", "no"), ("note", "")]:
        if col not in df.columns:
            df[col] = default

    required = ["canonical", "sector", "rule_type", "pattern"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise EcosystemRuleValidationError(
            f"ecosystem rules missing columns: {missing}"
        )

    df["status"] = df["status"].fillna("attribute").astype(str)
    df["shared"] = df["shared"].fillna("no").astype(str)

    empty_canon = df["canonical"].isna() | (df["canonical"].astype(str).str.strip() == "")
    if empty_canon.any():
        raise EcosystemRuleValidationError(
            f"{int(empty_canon.sum())} ecosystem rule(s) have an empty canonical"
        )

    empty_pat = df["pattern"].isna() | (df["pattern"].astype(str).str.strip() == "")
    if empty_pat.any():
        raise EcosystemRuleValidationError(
            f"{int(empty_pat.sum())} ecosystem rule(s) have an empty pattern"
        )

    bad_sectors = set(df["sector"].dropna().astype(str)) - VALID_ECOSYSTEM_SECTORS
    if bad_sectors or df["sector"].isna().any():
        raise EcosystemRuleValidationError(
            f"unknown ecosystem sector values: {sorted(bad_sectors)} "
            f"(must be in {sorted(VALID_ECOSYSTEM_SECTORS)})"
        )

    bad_types = set(df["rule_type"].dropna().astype(str)) - sr.VALID_RULE_TYPES
    if bad_types or df["rule_type"].isna().any():
        raise EcosystemRuleValidationError(
            f"unknown rule_type values: {sorted(bad_types)} "
            f"(must be in {sorted(sr.VALID_RULE_TYPES)})"
        )

    bad_status = set(df["status"]) - sr.VALID_STATUS
    if bad_status:
        raise EcosystemRuleValidationError(
            f"unknown status values: {sorted(bad_status)}"
        )

    bad_shared = set(df["shared"]) - sr.VALID_SHARED
    if bad_shared:
        raise EcosystemRuleValidationError(
            f"unknown shared values: {sorted(bad_shared)} (must be yes or no)"
        )

    no_note = df["note"].isna() | (df["note"].astype(str).str.strip() == "")
    if no_note.any():
        offender = df.loc[no_note, ["canonical", "pattern"]].iloc[0].to_dict()
        raise EcosystemRuleValidationError(
            f"{int(no_note.sum())} ecosystem rule(s) have an empty note. "
            f"Every rule must record rationale (first offender: {offender})"
        )

    # Single-sector consistency per canonical across all attribute rules
    attr = df[df["status"] == "attribute"]
    if not attr.empty:
        sectors_per_canon = attr.groupby("canonical")["sector"].nunique()
        multi = sectors_per_canon[sectors_per_canon > 1]
        if not multi.empty:
            offending = {
                str(c): sorted(set(attr.loc[attr["canonical"] == c, "sector"]))
                for c in sorted(multi.index)
            }
            raise EcosystemRuleValidationError(
                f"canonical mapped to multiple sectors: {offending}"
            )

    return df


def load_ecosystem_rules(path: str = DEFAULT_ECOSYSTEM_RULES_PATH) -> pd.DataFrame:
    """Read and validate curated ecosystem rules CSV (7-column schema)."""
    rules = pd.read_csv(path, dtype=str)
    return validate_ecosystem_rules(rules)


# ---------------------------------------------------------------------------
# 2. Exact literal matching & ecosystem index construction
# ---------------------------------------------------------------------------


def match_ecosystem_literals(
    uniq: pd.DataFrame,
    rules: pd.DataFrame,
) -> pd.DataFrame:
    """Apply curated ecosystem rules to a literal universe using exact matching ONLY.

    Precedence per literal: exact_literal > exact_normalized > subsidiary_of.
    Raises EcosystemRuleValidationError when:
      * two attribute rules claim one literal for different canonicals and not
        EVERY claiming rule has shared == 'yes', or
      * an attribute rule and an exclude rule both match the same literal.

    Audit holdout: ENFORCES when `uniq` carries `in_audit`. `uniq` is meant
    to be a literal universe (sr.literal_universe: distinct sponsor names,
    no nct_id), which carries none, so the functions that build it from
    trial rows enforce first. A frame without the universe's columns raises
    KeyError; a universe that carries in_audit anyway (assembled by hand
    from trial rows) gets the full frame check, raising
    EcosystemAuditHoldoutError as build_ecosystem_index does (see
    _refuse_literal_universe_holdout). Callers today: build_ecosystem_index,
    partition_ecosystem, check_cross_layer_conflicts, partition_unified and
    audit_ecosystem in this module (and through them build_unified_index,
    attribute_study_sponsors and compute_ecosystem_baseline); none elsewhere
    in code (see the module docstring).
    """
    _refuse_literal_universe_holdout(uniq, "match_ecosystem_literals")
    validated = validate_ecosystem_rules(rules)
    active = validated[validated["status"] == "attribute"]

    # literal -> list of (canonical, sector, rule_type, shared)
    lit_map: dict[str, list[tuple[str, str, str, str]]] = {}

    def _assign(
        literal: str,
        canonical: str,
        sector: str,
        rule: str,
        shared: str,
    ) -> None:
        claims = lit_map.setdefault(literal, [])
        if any(c == canonical for c, _, _, _ in claims):
            return  # same canonical via a second rule: keep higher-precedence rule
        if claims and not (shared == "yes" and all(s == "yes" for _, _, _, s in claims)):
            others = sorted({c for c, _, _, _ in claims})
            raise EcosystemRuleValidationError(
                f"conflicting ecosystem rules for literal {literal!r}: {others} vs "
                f"{canonical!r} (via {rule}). If this literal genuinely names "
                "several organizations, mark EVERY claiming rule shared=yes."
            )
        claims.append((canonical, sector, rule, shared))

    for rt in ("exact_literal", "exact_normalized", "subsidiary_of"):
        for _, r in active[active["rule_type"] == rt].iterrows():
            for lit in sr._rule_hits(uniq, r):
                _assign(str(lit), str(r.canonical), str(r.sector), rt, str(r.shared))

    for _, r in validated[validated["status"] == "exclude"].iterrows():
        for lit in sr._rule_hits(uniq, r):
            lit_s = str(lit)
            if lit_s in lit_map:
                claimed = sorted({c for c, _, _, _ in lit_map[lit_s]})
                raise EcosystemRuleValidationError(
                    f"conflicting ecosystem rules for literal {lit_s!r}: attributed to "
                    f"{claimed} but also excluded by rule ({r.canonical!r}, "
                    f"{r.rule_type}, {r.pattern!r}). A literal is attributed or "
                    "reviewed-excluded, never both."
                )

    return pd.DataFrame(
        [
            (lit, canon, sector, rule, shared)
            for lit, claims in lit_map.items()
            for canon, sector, rule, shared in claims
        ],
        columns=["name", "canonical", "sector", "match_rule", "shared"],
    )


def build_ecosystem_index(
    sponsors: pd.DataFrame,
    rules: pd.DataFrame,
) -> pd.DataFrame:
    """Attribute sponsor rows to canonical non-industry ecosystem organizations.

    Audit holdout: ENFORCES when `sponsors` carries `in_audit`: a column or a
    row-index level whose name reads as in_audit (contains it once case,
    separators and invisible characters are ignored, so in_audit_y and
    manifest.in_audit count), or a flag inside a cell or row-index value
    (text holding it as a key included) or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool, a cell, row-index value
    or bool column without a text name cannot be read or the flag has more
    than one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$, two nct_id sources (nct_id beside nct_id_m, say)
    disagree or an nct_id inside a cell disagrees with its row's
    ("nct_id_malformed");
    a False in `attrs` alone is ignored. Anything that is not a DataFrame
    is refused (see _refuse_audit_holdout_frame).
    An AACT-shaped sponsor frame carries no `in_audit`; for it this function
    relies on its caller to exclude holdout trials. Callers today:
    build_unified_index and compute_ecosystem_baseline in this module; none
    elsewhere in code (see the module docstring).
    """
    _refuse_audit_holdout_frame(sponsors, "build_ecosystem_index")
    sp = sponsors.copy()
    bad_roles = set(sp["lead_or_collaborator"].dropna().unique()) - sr.FRAME_ROLES
    if bad_roles:
        raise EcosystemRuleValidationError(
            f"lead_or_collaborator must be one of {sorted(sr.FRAME_ROLES)}; "
            f"found {sorted(bad_roles)}"
        )

    matched = match_ecosystem_literals(sr.literal_universe(sp), rules)
    if matched.empty:
        return pd.DataFrame(columns=ECOSYSTEM_INDEX_COLUMNS)

    matched["entity"] = matched["name"].map(sr.parsed_entity)
    out = sp.merge(matched, on="name", how="inner")
    out = out.rename(columns={"lead_or_collaborator": "role", "name": "literal_name"})
    out = (
        out[ECOSYSTEM_INDEX_COLUMNS]
        .drop_duplicates(subset=["nct_id", "canonical", "entity", "role"])
        .reset_index(drop=True)
    )
    return out


def partition_ecosystem(
    frame: pd.DataFrame,
    rules: pd.DataFrame,
) -> dict[str, set[str]]:
    """Return disjoint {'attributed', 'reviewed_excluded', 'unreviewed'} literal sets.

    Audit holdout: ENFORCES when `frame` carries `in_audit`: a column or a
    row-index level whose name reads as in_audit (contains it once case,
    separators and invisible characters are ignored, so in_audit_y and
    manifest.in_audit count), or a flag inside a cell or row-index value
    (text holding it as a key included) or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool, a cell, row-index value
    or bool column without a text name cannot be read or the flag has more
    than one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$, two nct_id sources (nct_id beside nct_id_m, say)
    disagree or an nct_id inside a cell disagrees with its row's
    ("nct_id_malformed");
    a False in `attrs` alone is ignored. Anything that is not a DataFrame
    is refused (see _refuse_audit_holdout_frame).
    An AACT-shaped sponsor frame carries no `in_audit`; for it this function
    relies on its caller to exclude holdout trials. Callers today: none in
    code (see the module docstring).
    """
    _refuse_audit_holdout_frame(frame, "partition_ecosystem")
    validated = validate_ecosystem_rules(rules)
    uniq = sr.literal_universe(frame)
    universe = set(uniq["name"].astype(str))
    matched = match_ecosystem_literals(uniq, validated)
    attributed = set(matched["name"].astype(str)) & universe

    excluded: set[str] = set()
    for _, r in validated[validated["status"] == "exclude"].iterrows():
        excluded.update(str(x) for x in sr._rule_hits(uniq, r))
    excluded &= universe

    both = attributed & excluded
    if both:
        raise EcosystemRuleValidationError(
            f"{len(both)} literal(s) are both attributed and reviewed-excluded "
            f"(first: {sorted(both)[0]!r})"
        )
    unreviewed = universe - attributed - excluded
    return {
        "attributed": attributed,
        "reviewed_excluded": excluded,
        "unreviewed": unreviewed,
    }


# ---------------------------------------------------------------------------
# 3. Cross-layer composition with company_aliases.csv (industry + ecosystem)
# ---------------------------------------------------------------------------


def check_cross_layer_conflicts(
    uniq: pd.DataFrame,
    industry_rules: pd.DataFrame,
    ecosystem_rules: pd.DataFrame,
) -> None:
    """Ensure no literal is attributed across both layers unless every claim is shared=yes.

    Note: status == 'exclude' in company_aliases.csv ("not industry") cleanly
    composes with status == 'attribute' in ecosystem_rules.csv (e.g., Wake Forest
    University Health Sciences).

    Audit holdout: ENFORCES when `uniq` carries `in_audit`, exactly as
    match_ecosystem_literals does (_refuse_literal_universe_holdout), before
    either layer's rules run. A universe built by sr.literal_universe
    carries none. Callers today: build_unified_index and partition_unified
    in this module (and through build_unified_index,
    attribute_study_sponsors); none elsewhere in code (see the module
    docstring).
    """
    _refuse_literal_universe_holdout(uniq, "check_cross_layer_conflicts")
    ind_matched = sr.match_literals(uniq, industry_rules)
    eco_matched = match_ecosystem_literals(uniq, ecosystem_rules)

    if ind_matched.empty or eco_matched.empty:
        return

    overlap = set(ind_matched["name"].astype(str)) & set(eco_matched["name"].astype(str))
    for lit in sorted(overlap):
        ind_rows = ind_matched[ind_matched["name"] == lit]
        eco_rows = eco_matched[eco_matched["name"] == lit]
        all_shared = (ind_rows["shared"] == "yes").all() and (
            eco_rows["shared"] == "yes"
        ).all()
        if not all_shared:
            ind_canons = sorted(set(ind_rows["canonical"].astype(str)))
            eco_canons = sorted(set(eco_rows["canonical"].astype(str)))
            raise EcosystemRuleValidationError(
                f"cross-layer conflict for literal {lit!r}: attributed in industry "
                f"to {ind_canons} and in ecosystem to {eco_canons} without every "
                "claiming rule marked shared=yes."
            )


def build_unified_index(
    sponsors: pd.DataFrame,
    industry_rules: pd.DataFrame,
    ecosystem_rules: pd.DataFrame,
) -> pd.DataFrame:
    """Build unified industry + ecosystem sponsor index with sector column.

    Audit holdout: ENFORCES when `sponsors` carries `in_audit`: a column or a
    row-index level whose name reads as in_audit (contains it once case,
    separators and invisible characters are ignored, so in_audit_y and
    manifest.in_audit count), or a flag inside a cell or row-index value
    (text holding it as a key included) or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool, a cell, row-index value
    or bool column without a text name cannot be read or the flag has more
    than one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$, two nct_id sources (nct_id beside nct_id_m, say)
    disagree or an nct_id inside a cell disagrees with its row's
    ("nct_id_malformed");
    a False in `attrs` alone is ignored. Anything that is not a DataFrame
    is refused (see _refuse_audit_holdout_frame).
    An AACT-shaped sponsor frame (or the frame sponsors.adapter builds from
    records) carries no `in_audit`; for it this function relies on its caller
    to exclude holdout trials. Callers today: attribute_study_sponsors in this
    module; none elsewhere in code (see the module docstring).
    """
    _refuse_audit_holdout_frame(sponsors, "build_unified_index")
    sp = sponsors.copy()
    bad_roles = set(sp["lead_or_collaborator"].dropna().unique()) - sr.FRAME_ROLES
    if bad_roles:
        raise EcosystemRuleValidationError(
            f"lead_or_collaborator must be one of {sorted(sr.FRAME_ROLES)}; "
            f"found {sorted(bad_roles)}"
        )

    uniq = sr.literal_universe(sp)
    check_cross_layer_conflicts(uniq, industry_rules, ecosystem_rules)

    ind_idx = sr.build_index(sp, industry_rules)
    if ind_idx.empty:
        ind_idx = pd.DataFrame(columns=ECOSYSTEM_INDEX_COLUMNS)
    else:
        ind_idx = ind_idx.copy()
        ind_idx["sector"] = "industry"
        ind_idx = ind_idx[ECOSYSTEM_INDEX_COLUMNS]

    eco_idx = build_ecosystem_index(sp, ecosystem_rules)

    frames = [f for f in (ind_idx, eco_idx) if not f.empty]
    if not frames:
        return pd.DataFrame(columns=ECOSYSTEM_INDEX_COLUMNS)

    combined = pd.concat(frames, ignore_index=True)
    combined = (
        combined[ECOSYSTEM_INDEX_COLUMNS]
        .drop_duplicates(subset=["nct_id", "canonical", "sector", "entity", "role"])
        .reset_index(drop=True)
    )
    return combined


def _normalize_study_record_for_adapter(record: dict[str, Any]) -> dict[str, Any]:
    """Accept either CivicSample stored records or raw CT.gov API v2 study dicts."""
    if "protocolSection" not in record:
        return record
    proto = record.get("protocolSection") or {}
    ident = proto.get("identificationModule") or {}
    sc_mod = proto.get("sponsorCollaboratorsModule") or {}
    lead = sc_mod.get("leadSponsor") or {}
    collabs = sc_mod.get("collaborators") or []
    return {
        "nct_id": ident.get("nctId"),
        "lead_sponsor_name": lead.get("name"),
        "sponsor_class": lead.get("class"),
        "collaborators": [
            {"name": c.get("name"), "class": c.get("class")}
            for c in collabs
            if isinstance(c, dict)
        ],
    }


def attribute_study_sponsors(
    records: Iterable[dict[str, Any]],
    industry_rules: pd.DataFrame | None = None,
    ecosystem_rules: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, adapter.AdapterLog]:
    """Compose industry (company_aliases.csv) + ecosystem attribution for study records.

    Audit holdout: ENFORCES on every record that carries `in_audit` (at any
    depth, under any spelling or qualified name, whatever the record's type:
    see the module docstring), checked for all records before any rules
    file is read or any record is adapted (_refuse_audit_holdout_records):
    raises EcosystemAuditHoldoutError if a value is True ("in_audit_true"),
    is not exactly a bool or part of a record cannot be read
    ("in_audit_not_bool"), or is False while the record's nct_id (top-level
    `nct_id` of a stored record, protocolSection.identificationModule.nctId
    of a CT.gov dict) is not exactly ^NCT[0-9]{8}$ or another nct_id the
    record carries at any depth disagrees with it ("nct_id_malformed"). A
    batch in which some records carry `in_audit` and others do not, or
    carry it under different names, is refused ("in_audit_not_bool"). A
    single record (any mapping-like value, a namedtuple, a numpy record or
    a dataclass instance) or a string passed in place of an iterable of
    records is refused with TypeError before anything is read. A stored
    record or a CT.gov study dict carries no `in_audit`; for a batch of them
    this function relies on its caller to exclude holdout trials. Callers
    today: none in code (see the module docstring).
    """
    if (
        isinstance(records, (str, bytes, bytearray))
        or _is_mapping_like(records)
        or _is_named_record(records)
    ):
        raise TypeError(
            "attribute_study_sponsors expects an iterable of study records, got a "
            f"single {type(records).__name__}; pass one record inside a list"
        )
    record_list: list[dict[str, Any]] = list(records)
    _refuse_audit_holdout_records(record_list, "attribute_study_sponsors")
    ind_rules = (
        sr.load_rules(DEFAULT_INDUSTRY_RULES_PATH)
        if industry_rules is None
        else industry_rules
    )
    eco_rules = (
        load_ecosystem_rules(DEFAULT_ECOSYSTEM_RULES_PATH)
        if ecosystem_rules is None
        else ecosystem_rules
    )
    normalized_records = [_normalize_study_record_for_adapter(r) for r in record_list]
    frame, log = adapter.records_to_frame(normalized_records)
    unified = build_unified_index(frame, ind_rules, eco_rules)
    return unified, log


def partition_unified(
    frame: pd.DataFrame,
    industry_rules: pd.DataFrame,
    ecosystem_rules: pd.DataFrame,
) -> dict[str, set[str]]:
    """Return disjoint 3-state literal partition across industry + ecosystem rules.

    A literal excluded by one layer (e.g., not industry) and attributed by the
    other (e.g., academic_medical_center) belongs strictly to 'attributed'.

    Audit holdout: ENFORCES when `frame` carries `in_audit`: a column or a
    row-index level whose name reads as in_audit (contains it once case,
    separators and invisible characters are ignored, so in_audit_y and
    manifest.in_audit count), or a flag inside a cell or row-index value
    (text holding it as a key included) or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool, a cell, row-index value
    or bool column without a text name cannot be read or the flag has more
    than one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$, two nct_id sources (nct_id beside nct_id_m, say)
    disagree or an nct_id inside a cell disagrees with its row's
    ("nct_id_malformed");
    a False in `attrs` alone is ignored. Anything that is not a DataFrame
    is refused (see _refuse_audit_holdout_frame).
    An AACT-shaped sponsor frame carries no `in_audit`; for it this function
    relies on its caller to exclude holdout trials. Callers today: none in
    code (see the module docstring).
    """
    _refuse_audit_holdout_frame(frame, "partition_unified")
    uniq = sr.literal_universe(frame)
    universe = set(uniq["name"].astype(str))
    check_cross_layer_conflicts(uniq, industry_rules, ecosystem_rules)

    ind_matched = sr.match_literals(uniq, industry_rules)
    eco_matched = match_ecosystem_literals(uniq, ecosystem_rules)
    attributed = (
        set(ind_matched["name"].astype(str)) | set(eco_matched["name"].astype(str))
    ) & universe

    excluded: set[str] = set()
    for _, r in industry_rules[industry_rules["status"] == "exclude"].iterrows():
        excluded.update(str(x) for x in sr._rule_hits(uniq, r))
    eco_valid = validate_ecosystem_rules(ecosystem_rules)
    for _, r in eco_valid[eco_valid["status"] == "exclude"].iterrows():
        excluded.update(str(x) for x in sr._rule_hits(uniq, r))
    excluded = (excluded & universe) - attributed

    unreviewed = universe - attributed - excluded
    return {
        "attributed": attributed,
        "reviewed_excluded": excluded,
        "unreviewed": unreviewed,
    }


# ---------------------------------------------------------------------------
# 4. Sector, organization & multi-sector cohort queries (nct_id deduplicated)
# ---------------------------------------------------------------------------


def sector_trials(
    index: pd.DataFrame,
    sector: str,
    role: str = "any",
) -> pd.DataFrame:
    """Trials touching `sector` in the given role scope, deduplicated on nct_id.

    Audit holdout: ENFORCES when `index` carries `in_audit`: a column or a
    row-index level whose name reads as in_audit (contains it once case,
    separators and invisible characters are ignored, so in_audit_y and
    manifest.in_audit count), or a flag inside a cell or row-index value
    (text holding it as a key included) or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool, a cell, row-index value
    or bool column without a text name cannot be read or the flag has more
    than one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$, two nct_id sources (nct_id beside nct_id_m, say)
    disagree or an nct_id inside a cell disagrees with its row's
    ("nct_id_malformed");
    a False in `attrs` alone is ignored. Anything that is not a DataFrame
    is refused (see _refuse_audit_holdout_frame).
    The index build_ecosystem_index and build_unified_index return
    (ECOSYSTEM_INDEX_COLUMNS only) carries no `in_audit`; for it this function
    relies on its caller to exclude holdout trials. Callers today:
    compute_ecosystem_baseline in this module; none elsewhere in code (see the
    module docstring).
    """
    _refuse_audit_holdout_frame(index, "sector_trials")
    if sector not in UNIFIED_SECTORS:
        raise EcosystemRuleValidationError(
            f"sector must be one of {list(UNIFIED_SECTORS)}; got {sector!r}"
        )
    if role not in sr.VALID_ROLES:
        raise EcosystemRuleValidationError(
            f"role must be one of {sorted(sr.VALID_ROLES)}; got {role!r}"
        )
    sub = index[index["sector"] == sector]
    if role != "any":
        sub = sub[sub["role"] == role]
    if sub.empty:
        return pd.DataFrame(columns=["nct_id", "roles", "is_lead", "is_collaborator"])
    g = sub.groupby("nct_id")["role"].agg(lambda r: ",".join(sorted(set(r))))
    out = g.reset_index().rename(columns={"role": "roles"})
    out["is_lead"] = out["roles"].str.contains("lead")
    out["is_collaborator"] = out["roles"].str.contains("collaborator")
    return out


def organization_trials(
    index: pd.DataFrame,
    canonical: str,
    role: str = "any",
    view: str = "current_owner",
) -> pd.DataFrame:
    """Trials touching a canonical organization (or as-registered entity).

    Audit holdout: ENFORCES when `index` carries `in_audit`: a column or a
    row-index level whose name reads as in_audit (contains it once case,
    separators and invisible characters are ignored, so in_audit_y and
    manifest.in_audit count), or a flag inside a cell or row-index value
    (text holding it as a key included) or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool, a cell, row-index value
    or bool column without a text name cannot be read or the flag has more
    than one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$, two nct_id sources (nct_id beside nct_id_m, say)
    disagree or an nct_id inside a cell disagrees with its row's
    ("nct_id_malformed");
    a False in `attrs` alone is ignored. Anything that is not a DataFrame
    is refused (see _refuse_audit_holdout_frame).
    The index build_ecosystem_index and build_unified_index return
    (ECOSYSTEM_INDEX_COLUMNS only) carries no `in_audit`; for it this function
    relies on its caller to exclude holdout trials. Callers today:
    compute_ecosystem_baseline in this module; none elsewhere in code (see the
    module docstring).
    """
    _refuse_audit_holdout_frame(index, "organization_trials")
    return sr.trials(index, canonical, role=role, view=view)


def trial_cohorts(
    unified_index: pd.DataFrame,
    role: str = "any",
) -> dict[str, list[str]]:
    """Map each attributed nct_id to its sorted unique list of touched sectors.

    Audit holdout: ENFORCES when `unified_index` carries `in_audit`: a column or a
    row-index level whose name reads as in_audit (contains it once case,
    separators and invisible characters are ignored, so in_audit_y and
    manifest.in_audit count), or a flag inside a cell or row-index value
    (text holding it as a key included) or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool, a cell, row-index value
    or bool column without a text name cannot be read or the flag has more
    than one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$, two nct_id sources (nct_id beside nct_id_m, say)
    disagree or an nct_id inside a cell disagrees with its row's
    ("nct_id_malformed");
    a False in `attrs` alone is ignored. Anything that is not a DataFrame
    is refused (see _refuse_audit_holdout_frame).
    The index build_unified_index returns (ECOSYSTEM_INDEX_COLUMNS only)
    carries no `in_audit`; for it this function relies on its caller to
    exclude holdout trials. Callers today: none in code (see the module
    docstring).
    """
    _refuse_audit_holdout_frame(unified_index, "trial_cohorts")
    if role not in sr.VALID_ROLES:
        raise EcosystemRuleValidationError(
            f"role must be one of {sorted(sr.VALID_ROLES)}; got {role!r}"
        )
    sub = unified_index if role == "any" else unified_index[unified_index["role"] == role]
    if sub.empty:
        return {}
    grouped = sub.groupby("nct_id")["sector"].agg(lambda s: sorted(set(s.astype(str))))
    return {str(nct): list(sectors) for nct, sectors in grouped.items()}


# ---------------------------------------------------------------------------
# 5. Three-state coverage audit (the ONLY path that uses substring `stem`)
# ---------------------------------------------------------------------------


def audit_ecosystem(
    sponsors: pd.DataFrame,
    rules: pd.DataFrame,
    canonical: str,
    stem: str,
) -> dict[str, pd.DataFrame]:
    """Three-state coverage review for one ecosystem organization.

    Returns pairwise-disjoint DataFrames:
      - 'attributed': literals mapped to `canonical`
      - 'attributed_elsewhere': literals containing `stem` mapped to another canonical
      - 'reviewed_excluded': literals containing `stem` matched by status == 'exclude'
      - 'unreviewed_candidates': literals containing `stem` matched by no rule

    Audit holdout: ENFORCES when `sponsors` carries `in_audit`: a column or a
    row-index level whose name reads as in_audit (contains it once case,
    separators and invisible characters are ignored, so in_audit_y and
    manifest.in_audit count), or a flag inside a cell or row-index value
    (text holding it as a key included) or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool, a cell, row-index value
    or bool column without a text name cannot be read or the flag has more
    than one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$, two nct_id sources (nct_id beside nct_id_m, say)
    disagree or an nct_id inside a cell disagrees with its row's
    ("nct_id_malformed");
    a False in `attrs` alone is ignored. Anything that is not a DataFrame
    is refused (see _refuse_audit_holdout_frame).
    An AACT-shaped sponsor frame carries no `in_audit`; for it this function
    relies on its caller to exclude holdout trials. Callers today: none in
    code (see the module docstring).
    """
    _refuse_audit_holdout_frame(sponsors, "audit_ecosystem")
    validated = validate_ecosystem_rules(rules)
    uniq = sr.literal_universe(sponsors)
    matched = match_ecosystem_literals(uniq, validated)

    attributed_lits = set(
        matched.loc[matched["canonical"] == canonical, "name"].astype(str)
    )
    all_attributed = set(matched["name"].astype(str))
    excluded_lits: set[str] = set()
    for _, r in validated[validated["status"] == "exclude"].iterrows():
        excluded_lits.update(str(x) for x in sr._rule_hits(uniq, r))

    def _summarize(names: set[str]) -> pd.DataFrame:
        subs = sponsors[sponsors["name"].isin(names)]
        if subs.empty:
            return pd.DataFrame(columns=["name", "n_trials", "roles", "agency_class"])
        return (
            subs.groupby("name")
            .agg(
                n_trials=("nct_id", "nunique"),
                roles=("lead_or_collaborator", lambda r: ",".join(sorted(set(r)))),
                agency_class=("agency_class", "first"),
            )
            .reset_index()
            .sort_values(["n_trials", "name"], ascending=[False, True])
            .reset_index(drop=True)
        )

    stem_mask = uniq["name"].str.contains(stem, case=False, regex=False, na=False)
    stem_names = set(uniq.loc[stem_mask, "name"].astype(str))
    elsewhere = (stem_names & all_attributed) - attributed_lits
    unreviewed = stem_names - all_attributed - excluded_lits

    elsewhere_df = _summarize(elsewhere)
    canon_of = matched.groupby("name")["canonical"].agg(
        lambda c: "|".join(sorted(set(c.astype(str))))
    )
    elsewhere_df["canonicals"] = (
        elsewhere_df["name"].map(canon_of) if len(elsewhere_df) else []
    )

    return {
        "attributed": _summarize(attributed_lits),
        "attributed_elsewhere": elsewhere_df,
        "reviewed_excluded": _summarize(stem_names & excluded_lits),
        "unreviewed_candidates": _summarize(unreviewed),
    }


# ---------------------------------------------------------------------------
# 6. SHA-256 Regression Baseline Helpers (mirroring sponsors/baseline.py)
# ---------------------------------------------------------------------------


def sha256_file(path: str) -> str:
    """Compute SHA-256 hex digest of a file (delegates to sponsors.baseline)."""
    return bl.sha256_file(path)


def uncovered_ecosystem_rules(
    fixture: pd.DataFrame,
    rules: pd.DataFrame,
) -> list[dict[str, str]]:
    """Return status == 'attribute' ecosystem rules matching zero fixture literals.

    Audit holdout: ENFORCES when `fixture` carries `in_audit`: a column or a
    row-index level whose name reads as in_audit (contains it once case,
    separators and invisible characters are ignored, so in_audit_y and
    manifest.in_audit count), or a flag inside a cell or row-index value
    (text holding it as a key included) or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool, a cell, row-index value
    or bool column without a text name cannot be read or the flag has more
    than one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$, two nct_id sources (nct_id beside nct_id_m, say)
    disagree or an nct_id inside a cell disagrees with its row's
    ("nct_id_malformed");
    a False in `attrs` alone is ignored. Anything that is not a DataFrame
    is refused (see _refuse_audit_holdout_frame).
    An AACT-shaped sponsor frame carries no `in_audit`; for it this function
    relies on its caller to exclude holdout trials. Callers today: none in
    code (see the module docstring).
    """
    _refuse_audit_holdout_frame(fixture, "uncovered_ecosystem_rules")
    validated = validate_ecosystem_rules(rules)
    uniq = sr.literal_universe(fixture)
    out: list[dict[str, str]] = []
    for _, r in validated[validated["status"] == "attribute"].iterrows():
        if len(sr._rule_hits(uniq, r)) == 0:
            out.append(
                {
                    "canonical": str(r.canonical),
                    "sector": str(r.sector),
                    "rule_type": str(r.rule_type),
                    "pattern": str(r.pattern),
                }
            )
    return out


def compute_ecosystem_baseline(
    fixture: pd.DataFrame,
    rules: pd.DataFrame,
) -> dict[str, Any]:
    """Compute deterministic baseline block: index, sectors, organizations, entities.

    Audit holdout: ENFORCES when `fixture` carries `in_audit`: a column or a
    row-index level whose name reads as in_audit (contains it once case,
    separators and invisible characters are ignored, so in_audit_y and
    manifest.in_audit count), or a flag inside a cell or row-index value
    (text holding it as a key included) or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool, a cell, row-index value
    or bool column without a text name cannot be read or the flag has more
    than one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$, two nct_id sources (nct_id beside nct_id_m, say)
    disagree or an nct_id inside a cell disagrees with its row's
    ("nct_id_malformed");
    a False in `attrs` alone is ignored. Anything that is not a DataFrame
    is refused (see _refuse_audit_holdout_frame).
    An AACT-shaped sponsor frame carries no `in_audit`; for it this function
    relies on its caller to exclude holdout trials. Callers today: none in
    code (see the module docstring).
    """
    _refuse_audit_holdout_frame(fixture, "compute_ecosystem_baseline")
    idx = build_ecosystem_index(fixture, rules)

    sectors: dict[str, dict[str, int]] = {}
    for s in sorted(idx["sector"].unique()):
        sectors[str(s)] = {
            "any": int(len(sector_trials(idx, str(s)))),
            "lead": int(len(sector_trials(idx, str(s), role="lead"))),
            "collaborator": int(len(sector_trials(idx, str(s), role="collaborator"))),
        }

    organizations: dict[str, dict[str, int]] = {}
    for c in sorted(idx["canonical"].unique()):
        organizations[str(c)] = {
            "any": int(len(organization_trials(idx, str(c)))),
            "lead": int(len(organization_trials(idx, str(c), role="lead"))),
            "collaborator": int(
                len(organization_trials(idx, str(c), role="collaborator"))
            ),
        }

    entities: dict[str, dict[str, int]] = {}
    for e in sorted(idx["entity"].unique()):
        t = organization_trials(idx, str(e), view="as_registered")
        entities[str(e)] = {
            "any": int(len(t)),
            "lead": int(t["is_lead"].sum()),
            "collaborator": int(t["is_collaborator"].sum()),
        }

    return {
        "index": {
            "rows": int(len(idx)),
            "sectors": int(idx["sector"].nunique()),
            "organizations": int(idx["canonical"].nunique()),
            "trials": int(idx["nct_id"].nunique()),
        },
        "sectors": sectors,
        "organizations": organizations,
        "entities": entities,
    }


# The shape write_ecosystem_baseline accepts: what compute_ecosystem_baseline,
# uncovered_ecosystem_rules and the fixture description produce
# (_refuse_trial_shaped_baseline).
_BASELINE_SECTIONS: frozenset[str] = frozenset({"index", "sectors", "organizations", "entities"})
_BASELINE_INDEX_KEYS: frozenset[str] = frozenset({"rows", "sectors", "organizations", "trials"})
_BASELINE_COUNT_KEYS: frozenset[str] = frozenset({"any", "lead", "collaborator"})
_UNCOVERED_KEYS: frozenset[str] = frozenset({"canonical", "sector", "rule_type", "pattern"})
_FIXTURE_META_KEYS: frozenset[str] = frozenset({"file", "sha256", "snapshot_date"})
_RULES_SHA_RE: re.Pattern[str] = re.compile(r"[0-9a-f]{64}")
# An NCT id written anywhere in a string: "NCT", any separators, eight
# digits, in any case (wider than the canonical form on purpose).
_NCT_ID_ANYWHERE_RE: re.Pattern[str] = re.compile(r"NCT[\W_]*\d{8}", re.IGNORECASE)


def _counts_section_ok(section: Any, keys: frozenset[str] | None) -> bool:
    """True when `section` maps names to {count name: int}, or (keys given) names to ints.

    With `keys`, `section` itself must map exactly those names to ints (the
    "index" section); without, it maps any str name to a mapping of
    _BASELINE_COUNT_KEYS to ints. An int is `type(v) is int`: a bool is not
    a count.
    """
    if not isinstance(section, dict):
        return False
    if keys is not None:
        return set(section) == keys and all(type(v) is int for v in section.values())
    return all(
        isinstance(name, str)
        and isinstance(counts, dict)
        and set(counts) == _BASELINE_COUNT_KEYS
        and all(type(v) is int for v in counts.values())
        for name, counts in section.items()
    )


def _strings_of(value: Any) -> list[str]:
    """Every str key and value in a JSON-shaped value (dicts, lists, scalars)."""
    out: list[str] = []
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            stack.extend(node.keys())
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return out


def _refuse_trial_shaped_baseline(
    rules_sha: Any, block: Any, uncovered: Any, fixture_meta: Any
) -> None:
    """Refuse a baseline write whose parts are not the aggregate shape documented for it.

    write_ecosystem_baseline writes the committed baseline file
    (tests/sponsors/expected_ecosystem_counts.json by default), which the
    module docstring lists as not trial-shaped: rules, file paths and
    aggregate counts, no nct_id and no in_audit. Method: before anything
    is read or written, each part must have exactly the shape its producer
    gives it, or ValueError is raised:
      * rules_sha: 64 lowercase hex digits (sha256_file);
      * block: exactly the sections "index", "sectors", "organizations"
        and "entities" (compute_ecosystem_baseline); "index" maps "rows",
        "sectors", "organizations" and "trials" to ints, and each other
        section maps a name to "any", "lead" and "collaborator" ints (a
        bool is not an int here), "sectors" naming only sectors this
        module defines;
      * uncovered: a list of records with exactly the str fields
        "canonical", "sector", "rule_type" and "pattern"
        (uncovered_ecosystem_rules);
      * fixture_meta: a mapping of any of "file", "sha256" and
        "snapshot_date" to str;
      * no key or str anywhere in them may contain an NCT id ("NCT" then
        eight digits, case and separators ignored).
    So a trial_cohorts() result, a manifest row or an in_audit flag cannot
    land in the committed file. Messages name the part and a count, never a
    key or value. Assumption: compute_ecosystem_baseline,
    uncovered_ecosystem_rules and the fixture description keep these
    shapes; a change to one of them changes this check in the same commit.
    """
    if not isinstance(rules_sha, str) or _RULES_SHA_RE.fullmatch(rules_sha) is None:
        raise ValueError(
            "write_ecosystem_baseline: rules_sha must be 64 lowercase hex digits "
            "(sha256_file of the rules); nothing is written"
        )
    if not isinstance(block, dict) or set(block) != _BASELINE_SECTIONS:
        extra = len(set(block) - _BASELINE_SECTIONS) if isinstance(block, dict) else 0
        raise ValueError(
            "write_ecosystem_baseline: block must hold exactly the sections index, "
            f"sectors, organizations and entities ({extra} other key(s)); a baseline "
            "holds aggregate counts only, so nothing is written"
        )
    sections_ok = (
        _counts_section_ok(block["index"], _BASELINE_INDEX_KEYS)
        and all(
            _counts_section_ok(block[section], None)
            for section in ("sectors", "organizations", "entities")
        )
        and set(block["sectors"]) <= VALID_ECOSYSTEM_SECTORS | set(UNIFIED_SECTORS)
    )
    if not sections_ok:
        raise ValueError(
            "write_ecosystem_baseline: a block section is not names mapped to "
            "integer counts as compute_ecosystem_baseline gives them; nothing is written"
        )
    if not isinstance(uncovered, list) or not all(
        isinstance(rule, dict)
        and set(rule) == _UNCOVERED_KEYS
        and all(isinstance(v, str) for v in rule.values())
        for rule in uncovered
    ):
        raise ValueError(
            "write_ecosystem_baseline: uncovered must be a list of rule records "
            "(canonical, sector, rule_type, pattern) as uncovered_ecosystem_rules "
            "gives them; nothing is written"
        )
    if not isinstance(fixture_meta, dict) or not (
        set(fixture_meta) <= _FIXTURE_META_KEYS
        and all(isinstance(v, str) for v in fixture_meta.values())
    ):
        raise ValueError(
            "write_ecosystem_baseline: fixture_meta may hold only file, sha256 and "
            "snapshot_date strings; nothing is written"
        )
    parts = (("block", block), ("uncovered", uncovered), ("fixture_meta", fixture_meta))
    for part_name, part in parts:
        held = sum(1 for text in _strings_of(part) if _NCT_ID_ANYWHERE_RE.search(text))
        if held:
            raise ValueError(
                f"write_ecosystem_baseline: {part_name} holds {held} string(s) naming "
                "an NCT id; a baseline is not trial-shaped, so nothing is written"
            )


def load_expected_ecosystem(path: str = EXPECTED_ECOSYSTEM_PATH) -> dict[str, Any]:
    """Load SHA-256-keyed expected ecosystem counts JSON."""
    if not os.path.exists(path):
        return {"fixture": {}, "baselines": {}}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_ecosystem_baseline(
    rules_sha: str,
    block: dict[str, Any],
    uncovered: list[dict[str, str]],
    fixture_meta: dict[str, Any],
    path: str = EXPECTED_ECOSYSTEM_PATH,
) -> None:
    """Write a generated baseline block keyed by ecosystem_rules.csv SHA-256.

    Not trial-shaped, and enforced: before the file is read or written,
    rules_sha, block, uncovered and fixture_meta must have exactly the
    aggregate shape their producers give them (sha256_file,
    compute_ecosystem_baseline, uncovered_ecosystem_rules, the fixture
    description), with no NCT id anywhere in them; otherwise ValueError is
    raised and nothing is written (_refuse_trial_shaped_baseline).
    """
    _refuse_trial_shaped_baseline(rules_sha, block, uncovered, fixture_meta)
    data = load_expected_ecosystem(path)
    data["fixture"] = fixture_meta
    data.setdefault("baselines", {})[rules_sha] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": (
            "tests/sponsors/test_ecosystem.py (auto re-baseline on rules change)"
        ),
        "not_covered_by_fixture": uncovered,
        **block,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


def diff_ecosystem_blocks(
    expected: dict[str, Any],
    actual: dict[str, Any],
    limit: int = 15,
) -> list[str]:
    """Human-readable differences between two ecosystem baseline blocks."""
    out: list[str] = []
    for section in ("index", "sectors", "organizations", "entities"):
        e = expected.get(section, {})
        a = actual.get(section, {})
        for k in sorted(set(e) | set(a)):
            if e.get(k) != a.get(k):
                out.append(f"{section}.{k}: expected {e.get(k)} got {a.get(k)}")
            if len(out) >= limit:
                out.append("…")
                return out
    return out
