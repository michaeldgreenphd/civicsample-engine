"""Audit holdout (in_audit) guard shared by the M2 modules.

READS   Only the objects it is handed. WRITES nothing.
USED BY src/analysis_population_parser.py (records, batches of records) and
        sponsors/ecosystem.py (records, batches, sponsor frames), which import
        nothing from each other.
DEPENDS The Python standard library; pandas only inside check_frame.

A trial whose ``in_audit`` is true in the site's data/outcomes/index.json must
not be parsed, attributed, counted or recorded. No entry point receives the
audit id set: input without the flag relies on its caller to leave holdouts
out. Input that carries it (under a name is_flag_key accepts, at any depth) is
refused when a flag is True ("in_audit_true"); when a flag is not a bool (0/1,
"True", None, NaN are never coerced), or a batch carries it for some records
only, or a frame only inside cells, as a manifest left join leaves it
("in_audit_not_bool"); or when the id the entry point would stamp is not
exactly ^NCT[0-9]{8}$ or another id it carries disagrees, as a manifest row
merged onto another trial's payload does ("nct_id_malformed").

Flag or not, input must be plain JSON data or it is refused by type
("in_audit_not_bool"): a record is exactly a dict with exactly-str keys and,
at any depth, values that are exactly dict, list, str, int, float, bool or
None. Anything else (a dict or str subclass, UserDict, ChainMap, Series row,
namedtuple, dataclass, sqlite3.Row, tuple, set, generator, numpy value, Enum
key, XML element, an object with attributes) is not opened: a flag can hide
in such values in more ways than a scan can enumerate.

Read as the flag: the exact names and the names whose words hold "in" then
"audit", including a custom join suffix or prefix (in_audit_manifest,
manifest_in_audit); see is_flag_key.

Not seen, by design: a flag the caller removed or renamed to other words,
written with look-alike or full-width letters, serialized into a string (JSON
or repr text, a CSV round trip of a dict column), held as a value
(DataFrame.melt, key/value lists), or set as a DataFrame attribute instead of
a column. A caller who deletes the flag defeats any scanner; the guarantee
rests on the caller's exclusion.
"""

from __future__ import annotations

import re
from typing import Any, Callable, NamedTuple

HOLDOUT_REFUSAL_REASONS: tuple[str, ...] = (
    "in_audit_true",       # a record or row says it is an audit holdout trial
    "in_audit_not_bool",   # a flag is not exactly a bool, or the input is refused by
                           # type or carries the flag for only some records or rows
    "nct_id_malformed",    # flagged, but the id it would be stamped with is not
                           # exactly ^NCT[0-9]{8}$, or another id in it disagrees
)


class AuditHoldoutError(Exception):
    """Raised when an entry point is handed input it must not read further.

    ``reason`` is one of HOLDOUT_REFUSAL_REASONS. Deliberately NOT a
    ValueError: an ``except ValueError`` written for malformed input must not
    swallow a holdout refusal. Messages name the entry point, counts,
    positions and types, never an id, a name or any other value, so a refusal
    records nothing about a trial. Each caller raises its own subclass
    (PopulationAuditHoldoutError, EcosystemAuditHoldoutError).
    """

    def __init__(self, reason: str, message: str) -> None:
        if reason not in HOLDOUT_REFUSAL_REASONS:
            raise ValueError(f"unknown holdout refusal reason: {reason!r}")
        super().__init__(f"[{reason}] {message}")
        self.reason: str = reason


# fullmatch (not "$", which accepts a trailing newline), ASCII [0-9] (not \d),
# no IGNORECASE, no strip(): an id that needs normalising to look canonical
# could be a holdout's id in disguise, so it is refused, not repaired.
_CANONICAL_NCT_ID_RE: re.Pattern[str] = re.compile(r"NCT[0-9]{8}")
_NOT_ASCII_LETTER_RE: re.Pattern[str] = re.compile(r"[^a-z]+")
_MERGE_SUFFIXES: tuple[str, ...] = ("_x", "_y")  # pandas merge's default suffixes
_FLAG_LETTERS: frozenset[str] = frozenset({"inaudit", "isinaudit"})
# A name's ASCII words: an upper-case run not followed by a lower-case letter
# (IN in IN_AUDIT), a capitalised or lower-case word (In, in), or digits.
_WORD_RE: re.Pattern[str] = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_FLAG_WORDS: tuple[str, str] = ("in", "audit")
_ID_PREFIXES: tuple[str, ...] = ("nctid", "nctnumber")
# Exact JSON scalar types; a bool is checked on its own, since it may be a flag.
_JSON_SCALARS: tuple[type, ...] = (str, int, float, type(None))
# Cell types a frame's object column may hold without being read further.
_PLAIN_CELL_TYPES: frozenset[type] = frozenset({str, int, float, bool, type(None)})
# dtype kinds of bools, numbers, dates and durations: no container fits there.
_SCALAR_KINDS: frozenset[str] = frozenset("biufcmM")
_NOT_READ: str = "refused by type rather than read"


def is_canonical_nct_id(value: Any) -> bool:
    """True iff ``value`` is exactly a str (not a subclass) matching ^NCT[0-9]{8}$.

    A str subclass is refused even with canonical content: its __str__ or
    __eq__ could say another id than the one checked.
    """
    return type(value) is str and _CANONICAL_NCT_ID_RE.fullmatch(value) is not None


def plain_text(value: Any) -> str:
    """``value`` as the plain str it stores (a str subclass's content), else str(value).

    Used where an nct_id is stamped, so output carries the characters that
    were checked, never what a subclass's __str__ says.
    """
    return str.__str__(value) if isinstance(value, str) else str(value)


def _letters(segment: str) -> str:
    """``segment`` casefolded, with every character but ASCII a-z removed."""
    return _NOT_ASCII_LETTER_RE.sub("", segment.casefold())


def is_flag_key(key: Any) -> bool:
    """True when ``key`` names the holdout flag (exact names and whole words, no substring matching).

    Method: ``key`` must be exactly a str. Its last "."-separated segment is
    taken (json_normalize names a nested manifest's flag "manifest.in_audit").
    It is the flag when either rule holds:

    - exact: with trailing pandas merge suffixes "_x"/"_y" removed (merging
      two frames that both carry the flag gives in_audit_x and in_audit_y),
      the rest, casefolded and reduced to ASCII letters, is exactly "inaudit"
      or "isinaudit" (in_audit, inAudit, IN_AUDIT, in-audit, "In Audit",
      inaudit, is_in_audit);
    - words: the segment's ASCII words (split at separators and at
      lower-to-upper case changes, casefolded) contain "in" immediately
      followed by "audit". This reads the names a join gives the flag under a
      custom suffix or prefix (in_audit_manifest from suffixes=("",
      "_manifest"), manifest_in_audit) and in_audit_flag.

    domain_audit, main_audit, login_audit and within_audit are not the flag:
    their words hold "audit" but not after a separate "in" (the earlier
    substring rule made them flags and refused clean data). Look-alike and
    full-width letters are not ASCII, so they form no word: a caller who
    disguises the flag could as easily delete it, which no rule can see.
    """
    if type(key) is not str:
        return False
    segment = key.rsplit(".", 1)[-1]
    words = [w.casefold() for w in _WORD_RE.findall(segment)]
    if any(tuple(words[i : i + 2]) == _FLAG_WORDS for i in range(len(words) - 1)):
        return True
    while segment[-2:].casefold() in _MERGE_SUFFIXES:
        segment = segment[:-2]
    return _letters(segment) in _FLAG_LETTERS


def is_id_key(key: Any) -> bool:
    """True when ``key`` names a trial id that a flagged input's ids must agree with.

    Method: exactly a str whose last "."-segment, as ASCII letters casefolded,
    starts with "nctid" or "nctnumber" and has no "alias": nct_id, nctId,
    "NCT Number", manifest.nct_id and suffixed copies (nct_id_x, nct_id_m)
    are ids; CT.gov's nctIdAliases (obsolete ids) is not. A prefix, unlike
    is_flag_key's exact rule: ids are only read on flagged input, where an
    extra id can only refuse, and a manifest joined by position brings its
    own id under whatever suffix the join chose.
    """
    if type(key) is not str:
        return False
    letters = _letters(key.rsplit(".", 1)[-1])
    return letters.startswith(_ID_PREFIXES) and "alias" not in letters


class Found(NamedTuple):
    """What walk_json found: flag values, id values, and what it refused by type."""

    flags: list[Any]
    ids: list[Any]
    refused: list[str]  # descriptions naming a type, never a value


def walk_json(value: Any) -> Found:
    """Every flag and id in the JSON value ``value``, at any depth, and what is not JSON.

    Method: an explicit-stack walk of dicts and lists, each container once
    (so a cyclic record terminates). A dict key must be exactly a str; one
    is_flag_key accepts gives a flag, one is_id_key accepts an id. A leaf
    must be exactly str, int, float, bool or None. Anything else is listed in
    ``refused`` by its type and not opened. A bool held by position in a list
    has lost its name (a manifest row written as a list of values) and is
    refused too.
    """
    found = Found([], [], [])
    stack: list[tuple[Any, bool]] = [(value, True)]  # (node, reached under a key)
    seen: set[int] = set()
    while stack:
        node, named = stack.pop()
        kind = type(node)
        if kind is dict or kind is list:
            if id(node) in seen:
                continue
            seen.add(id(node))
            if kind is list:
                stack.extend((item, False) for item in node)
                continue
            for key, item in node.items():
                if type(key) is not str:
                    found.refused.append(f"a {type(key).__name__} key")
                elif is_flag_key(key):
                    found.flags.append(item)
                elif is_id_key(key):
                    found.ids.append(item)
                stack.append((item, True))
        elif kind is bool:
            if not named:
                found.refused.append("a bool held by position in a list")
        elif kind not in _JSON_SCALARS:
            found.refused.append(f"a {kind.__name__}")
    return found


def _refuse_flags(flags: list[Any], where: str, error: type[AuditHoldoutError]) -> None:
    """Raise unless every flag is exactly a Python bool and none is True."""
    not_bool = [flag for flag in flags if type(flag) is not bool]
    if not_bool:
        raise error("in_audit_not_bool", f"{where}: {len(not_bool)} in_audit value(s) are not "
                    f"exactly True or False (first: {type(not_bool[0]).__name__}); refusing "
                    "rather than guessing")
    if any(flags):
        raise error("in_audit_true", f"{where}: marked as an in_audit holdout trial; it must "
                    "not be parsed, attributed, counted or recorded")


def _refuse_by_type(refused: list[str], where: str, error: type[AuditHoldoutError]) -> None:
    """Raise "in_audit_not_bool" when anything was refused by type."""
    if refused:
        raise error("in_audit_not_bool", f"{where}: {len(refused)} part(s) are not JSON data "
                    f"(first: {refused[0]}); {_NOT_READ}")


def check_record(record: Any, where: str, error: type[AuditHoldoutError],
                 stamped_id_of: Callable[[dict[str, Any]], Any]) -> bool:
    """Refuse one record unless it carries no flag or is a canonical non-holdout.

    Returns whether it carries a flag. Method: a record must be exactly a
    dict, or a JSON scalar (str, int, float, None), which holds no field and
    passes (the entry point records a parse error without an id); anything
    else is refused by type. Then walk_json and, in order: every flag exactly
    a bool and none True; if flagged, ``stamped_id_of(record)`` (read as
    stored) canonical and every id at any depth equal to it; nothing refused.
    """
    if type(record) is not dict:
        if type(record) in _JSON_SCALARS:
            return False
        raise error("in_audit_not_bool",
                    f"{where}: a record must be a dict, got a {type(record).__name__}; {_NOT_READ}")
    found = walk_json(record)
    _refuse_flags(found.flags, where, error)
    if found.flags:
        stamped = stamped_id_of(record)
        if not is_canonical_nct_id(stamped):
            raise error("nct_id_malformed", f"{where}: in_audit is False but the nct_id "
                        f"({type(stamped).__name__}) is not exactly ^NCT[0-9]{{8}}$ (a plain "
                        "str: ASCII, case-sensitive, unstripped)")
        disagreeing = [i for i in found.ids if not (type(i) is str and i == stamped)]
        if disagreeing:
            raise error("nct_id_malformed", f"{where}: in_audit is False but {len(disagreeing)} "
                        "other nct_id value(s), at any depth, disagree with the id it would be "
                        "stamped with; refusing rather than choosing one")
    _refuse_by_type(found.refused, where, error)
    return bool(found.flags)


def check_batch(records: list[Any], where: str, error: type[AuditHoldoutError],
                stamped_id_of: Callable[[dict[str, Any]], Any]) -> None:
    """Refuse a batch unless every record passes check_record and the flag has no gap.

    Method: each record is checked in order (a True is reported at its
    position); then a batch in which some records carry the flag and others
    do not is refused ("in_audit_not_bool").
    """
    carrying = [
        check_record(record, f"{where}, record at position {pos}", error, stamped_id_of)
        for pos, record in enumerate(records)
    ]
    n_carrying = sum(carrying)
    if 0 < n_carrying < len(records):
        raise error("in_audit_not_bool", f"{where}: {n_carrying} of {len(records)} records carry "
                    f"in_audit and {len(records) - n_carrying} do not (first at position "
                    f"{carrying.index(False)}); in a manifest-merged batch a record without "
                    "in_audit is unknown, not clean, so the batch is refused")


def refuse_single_record(batch: Any, where: str) -> None:
    """Raise TypeError when ``batch`` is one record or a string, not an iterable of records.

    Iterating a mapping (dict, Series, DataFrame, sqlite3.Row) yields its
    keys, a namedtuple, dataclass or numpy record its field values, and a
    string its characters: none of them a record. Names the type only.
    """
    if (
        isinstance(batch, (str, bytes, bytearray))
        or callable(getattr(batch, "keys", None))
        or hasattr(batch, "_fields")
        or hasattr(type(batch), "__dataclass_fields__")
        or getattr(getattr(batch, "dtype", None), "names", None) is not None
    ):
        raise TypeError(f"{where} expects an iterable of records, got a single "
                        f"{type(batch).__name__}; pass one record inside a list")


def check_frame(frame: Any, where: str, error: type[AuditHoldoutError]) -> None:
    """Refuse a sponsor frame that carries the flag unless every row is a canonical non-holdout.

    Method, before any rule runs: ``frame`` must be exactly a DataFrame
    (TypeError otherwise) with str column labels and str or None index level
    names. ``frame.attrs`` is walked as a record (a True or non-bool flag
    refuses; a False alone is ignored, since pandas copies attrs onto frames
    derived from a clean flagged one). A column or index level whose label
    is_flag_key accepts is a row flag whose values must be bools, Python or
    numpy (a bool-dtype column holds numpy bools); several (in_audit_x,
    in_audit_y) pass when none is True, so they agree. Every other column or
    level that can hold objects (not a bool, number, date or string dtype)
    has each cell read: a dict or list is walked (its flags and ids belong
    to its row); a cell that is not JSON data, pd.NA or pd.NaT is refused by
    type. A flag only inside cells is refused; a flagged frame needs an id
    column or level (is_id_key), every id canonical and all ids of a row
    equal. Attributes of the frame object and pandas internals are not read.
    """
    import pandas as pd  # lazy: the parser imports this module without pandas

    if type(frame) is not pd.DataFrame:
        raise TypeError(f"{where} expects a pandas DataFrame, got {type(frame).__name__}; the "
                        "holdout check reads a frame's columns, row index and attrs")
    attrs = walk_json(frame.attrs)
    _refuse_flags(attrs.flags, f"{where}: frame.attrs", error)
    _refuse_by_type(attrs.refused, f"{where}: frame.attrs", error)
    labels = [f"a {type(c).__name__} column label" for c in frame.columns if type(c) is not str]
    labels += [f"a {type(n).__name__} index level name" for n in frame.index.names
               if n is not None and type(n) is not str]
    _refuse_by_type(labels, where, error)
    axes = [(f"column at position {pos}", label, frame.iloc[:, pos])
            for pos, label in enumerate(frame.columns)]
    axes += [(f"index level {level}", name, frame.index.get_level_values(level))
             for level, name in enumerate(frame.index.names)]
    flag_sources: list[tuple[str, list[Any]]] = []
    id_sources: list[tuple[str, Any]] = []  # (description, Series or Index), read if flagged
    cell_flags: list[Any] = []
    cell_ids: list[tuple[int, Any]] = []
    refused: list[str] = []
    for desc, label, values in axes:
        if is_flag_key(label):
            flag_sources.append((desc, values.to_numpy(dtype=object).tolist()))
            continue
        if is_id_key(label):
            id_sources.append((desc, values))
        kind = getattr(values.dtype, "kind", "O")
        if kind in _SCALAR_KINDS or isinstance(values.dtype, pd.StringDtype):
            continue
        cells = values.to_numpy(dtype=object).tolist()
        if set(map(type, cells)) <= _PLAIN_CELL_TYPES:  # the common case, at C speed
            continue
        for row, cell in enumerate(cells):
            if type(cell) is dict or type(cell) is list:
                found = walk_json(cell)
                cell_flags.extend(found.flags)
                cell_ids.extend((row, value) for value in found.ids)
                refused.extend(f"{text} in a cell ({desc})" for text in found.refused)
            elif type(cell) not in _PLAIN_CELL_TYPES and cell is not pd.NA and cell is not pd.NaT:
                refused.append(f"a {type(cell).__name__} cell ({desc})")
    for desc, flags in flag_sources:
        bad = [row for row, flag in enumerate(flags) if not pd.api.types.is_bool(flag)]
        if bad:
            raise error("in_audit_not_bool", f"{where}: {len(bad)} row(s) carry an in_audit value "
                        f"that is not a bool (first at row position {bad[0]}, type "
                        f"{type(flags[bad[0]]).__name__}, {desc}); refusing rather than guessing")
    _refuse_flags(cell_flags, f"{where}: inside cells", error)
    for desc, flags in flag_sources:
        held = [row for row, flag in enumerate(flags) if flag]
        if held:
            raise error("in_audit_true", f"{where}: {len(held)} row(s) belong to in_audit holdout "
                        f"trials (first at row position {held[0]}, {desc}); they must not be "
                        "attributed, counted or recorded")
    _refuse_by_type(refused, where, error)
    if not flag_sources and cell_flags:
        raise error("in_audit_not_bool", f"{where}: in_audit is found only inside cells, which "
                    "cannot say which rows it describes; a row-level in_audit column or index "
                    "level is required")
    if flag_sources:
        _refuse_frame_ids(where, error, id_sources, cell_ids)


def _refuse_frame_ids(
    where: str, error: type[AuditHoldoutError], id_sources: list[tuple[str, Any]],
    cell_ids: list[tuple[int, Any]],
) -> None:
    """For a flagged frame: an id source exists, every id is canonical, all ids agree by row."""
    if not id_sources:
        raise error("nct_id_malformed",
                    f"{where}: frame carries in_audit but has no nct_id column or index level")
    columns = [(desc, values.to_numpy(dtype=object).tolist()) for desc, values in id_sources]
    for desc, ids in columns:
        bad = [row for row, value in enumerate(ids) if not is_canonical_nct_id(value)]
        if bad:
            raise error("nct_id_malformed", f"{where}: {len(bad)} row(s) have an nct_id that is "
                        f"not exactly ^NCT[0-9]{{8}}$ (first at row position {bad[0]}, type "
                        f"{type(ids[bad[0]]).__name__}, {desc})")
    first_desc, first = columns[0]
    for desc, ids in columns[1:]:
        bad = [row for row, (a, b) in enumerate(zip(first, ids)) if a != b]
        if bad:
            raise error("nct_id_malformed", f"{where}: {len(bad)} row(s) carry nct_id values that "
                        f"disagree between {first_desc} and {desc} (first at row position "
                        f"{bad[0]}); refusing rather than choosing one")
    stray = [row for row, value in cell_ids if not (type(value) is str and value == first[row])]
    if stray:
        raise error("nct_id_malformed", f"{where}: {len(stray)} nct_id value(s) inside cells "
                    f"disagree with their row's nct_id (first at row position {stray[0]}); a "
                    "manifest merged onto the wrong trial is refused rather than resolved")
