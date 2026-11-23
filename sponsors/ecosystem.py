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
  EcosystemAuditHoldoutError (see _refuse_audit_holdout_frame and
  _refuse_audit_holdout_records):
      build_ecosystem_index, partition_ecosystem, build_unified_index,
      partition_unified, attribute_study_sponsors, sector_trials,
      organization_trials, trial_cohorts, audit_ecosystem,
      uncovered_ecosystem_rules, compute_ecosystem_baseline.
    A name "reads as" in_audit once case, spaces, hyphens and underscores
    are ignored ("inAudit", "IN_AUDIT", "in-audit" count). A sponsor frame
    or index carries `in_audit` in a column or a row-index level so named,
    or inside a cell; a True flag in the frame's `attrs` refuses it too.
    Anything that is not a DataFrame (a column mapping, say) is refused
    outright, as a holdout when it carries one. A study record carries it under a key or field so named
    at any depth (beside the nctId, on protocolSection, in a merged
    `manifest` sub-record), whether the record is a dict or any other
    mapping-like value (collections.UserDict, types.MappingProxyType, a
    pandas Series row), a namedtuple (a `df.itertuples()` row) or a
    dataclass instance. Such input is refused when any in_audit value is
    True, when any is not a bool, when a frame's flag has more than one
    row-level source, or when any nct_id is not exactly `^NCT[0-9]{8}$` or
    disagrees with another nct_id carried for the same row or record.
    attribute_study_sponsors also refuses a batch in which
    only some records carry `in_audit`, and a single record passed where a
    batch is expected. An AACT-shaped sponsor frame, a CT.gov study dict and
    the index these functions build carry no `in_audit`; for them every
    function above relies on its caller, as below.
  Rely on the caller: every entry point above, for input without
  `in_audit` (no entry point receives an audit id set).
  Not trial-shaped (rules, distinct literal names or aggregate counts, no
  nct_id): validate_ecosystem_rules, load_ecosystem_rules,
  match_ecosystem_literals, check_cross_layer_conflicts, sha256_file,
  load_expected_ecosystem, write_ecosystem_baseline, diff_ecosystem_blocks.
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
from typing import Any, Iterable, NamedTuple, NoReturn

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
    "in_audit_not_bool",   # some in_audit value is present but is not a bool
    "nct_id_malformed",    # in_audit is False but an nct_id is not ^NCT[0-9]{8}$
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
# "in_audit" or "nct_id". Assumption: a name that differs from those only by
# case, spacing, hyphens, dashes or underscores (a camelCase merge, a
# spreadsheet header, a stray space), by full-width forms of ASCII, or by
# invisible characters (soft hyphen, zero-width space/joiners, word joiner,
# BOM) and combining accents a copy-paste adds, was meant as that name.
# Treating it as such is fail-closed: an unrecognised holdout flag is the
# failure this guards against, while a false match can only refuse input.
# Twin of the same rule in src/analysis_population_parser.py; the two modules
# import nothing from each other.
_NAME_IGNORED_RE: re.Pattern[str] = re.compile(
    r"[\s_\-\u00ad\u0300-\u036f\u200b-\u200d\u2010-\u2015\u2060\u2212\ufe63\ufeff]+"
)
_FULLWIDTH_TO_ASCII: dict[int, int] = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_IN_AUDIT_NAME: str = "inaudit"
_NCT_ID_NAME: str = "nctid"

# pandas.api.types.infer_dtype results for an object column whose cells are
# all scalars, so no cell can hold a nested in_audit flag. Any other result
# ("mixed", "mixed-integer", "unknown-array", or one added by a later pandas)
# gets its cells scanned: unknown is scanned, not skipped.
_SCALAR_INFERRED_DTYPES: frozenset[str] = frozenset(
    {
        "empty", "string", "bytes", "floating", "integer", "mixed-integer-float",
        "decimal", "complex", "categorical", "boolean", "datetime64", "datetime",
        "date", "timedelta64", "timedelta", "time", "period", "interval",
    }
)


def _folded_name(name: Any) -> str | None:
    """`name` folded for comparison (see _NAME_IGNORED_RE), or None if not text.

    bytes are decoded as latin-1, which never fails, so a bytes key cannot
    hide a flag; any other non-str name (an int label, None) is not text.
    """
    if isinstance(name, (bytes, bytearray)):
        name = bytes(name).decode("latin-1")
    if not isinstance(name, str):
        return None
    return _NAME_IGNORED_RE.sub("", name.translate(_FULLWIDTH_TO_ASCII)).casefold()


def _name_reads_as(name: Any, folded_target: str) -> bool:
    """True when `name` (or any part of a tuple name, as in a MultiIndex) folds to it."""
    if isinstance(name, tuple):
        return any(_name_reads_as(part, folded_target) for part in name)
    return _folded_name(name) == folded_target


def _is_mapping_like(value: Any) -> bool:
    """True for anything that answers `keys()`, `items()` and `[]` like a dict.

    Method: duck-typed, not `isinstance(value, dict)`. A dict subclass, a
    collections.UserDict, a types.MappingProxyType, a pandas Series (the row
    `df.iterrows()` yields, whose `in`, `.get` and `.items()` read its index
    labels) and a DataFrame all qualify; sponsors.adapter.record_rows reads
    a record only through `.get`, so each of them is attributed in full if
    the holdout check skips it. Text and classes are excluded.
    """
    return (
        not isinstance(value, (str, bytes, bytearray, type))
        and callable(getattr(value, "keys", None))
        and callable(getattr(value, "items", None))
        and hasattr(value, "__getitem__")
    )


class _InAuditScan(NamedTuple):
    """What _scan_in_audit found in one record (or one frame cell).

    `flags`: every value stored under a key that reads as in_audit, at any
    depth. `sibling_ids`: the values of keys that read as nct_id in the same
    mapping as such a flag (the id that flag describes).
    """

    flags: tuple[Any, ...]
    sibling_ids: tuple[Any, ...]


def _keyed_items(node: Any) -> list[tuple[Any, Any]] | None:
    """(name, value) pairs of a record whose fields have names, else None.

    Method: a mapping-like value gives its items; a namedtuple (what
    `df.itertuples()` yields) gives its fields through `_asdict()`, and a
    dataclass instance through its `__dataclass_fields__`. Without this a
    namedtuple would be walked as a plain tuple, its values without their
    names, and a flag in it would be missed.
    """
    if _is_mapping_like(node):
        return list(node.items())
    if isinstance(node, tuple) and callable(getattr(node, "_asdict", None)):
        return list(node._asdict().items())
    fields = getattr(type(node), "__dataclass_fields__", None)
    if isinstance(fields, dict) and not isinstance(node, type):
        return [(name, getattr(node, name, None)) for name in fields]
    return None


def _scan_in_audit(record: Any) -> _InAuditScan:
    """Find every in_audit flag in `record`, at any depth and under any spelling.

    Method: an explicit-stack walk (no recursion limit) through every record
    with named fields (_keyed_items: mapping-like values, namedtuples,
    dataclass instances), the `attrs` metadata dict of a pandas object, and
    every list, tuple, set and numpy object array. Each container is visited
    once; `visited` keeps a reference to it as well as its id(), because a
    DataFrame's `items()` builds a fresh Series per column and a released
    object's id() can be reused, which would otherwise skip an unvisited
    container. Strings and numbers are leaves; an iterator or generator is
    not walked, since reading it would consume it. Assumption: a stored
    sponsor record and a CT.gov API v2 study have no key that reads as
    in_audit, so a flag found anywhere was put there by a manifest merge and
    marks the record (a flag on identificationModule beside the nctId, on
    protocolSection, or in a merged manifest sub-record all count).
    """
    flags: list[Any] = []
    sibling_ids: list[Any] = []
    stack: list[Any] = [record]
    visited: dict[int, Any] = {}
    while stack:
        node = stack.pop()
        if node is None or isinstance(node, (str, bytes, bytearray, int, float)):
            continue
        if id(node) in visited:
            continue
        items = _keyed_items(node)
        if items is not None:
            visited[id(node)] = node
            flagged = False
            ids_here: list[Any] = []
            for key, value in items:
                if _name_reads_as(key, _IN_AUDIT_NAME):
                    flags.append(value)
                    flagged = True
                elif _name_reads_as(key, _NCT_ID_NAME):
                    ids_here.append(value)
                stack.append(value)
            if flagged:
                sibling_ids.extend(ids_here)
            attrs = getattr(node, "attrs", None)  # pandas Series/DataFrame metadata
            if isinstance(attrs, dict):
                stack.append(attrs)
        elif isinstance(node, (list, tuple, set, frozenset)):
            visited[id(node)] = node
            stack.extend(node)
        elif getattr(getattr(node, "dtype", None), "kind", None) == "O" and callable(
            getattr(node, "tolist", None)
        ):
            visited[id(node)] = node
            stack.append(node.tolist())
    return _InAuditScan(flags=tuple(flags), sibling_ids=tuple(sibling_ids))


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


def _frame_sources(frame: pd.DataFrame, folded_target: str) -> list[tuple[str, list[Any]]]:
    """Every column and row-index level of `frame` whose name reads as the target.

    Returns (description, one value per row) pairs. Method: in_audit (or
    nct_id) can sit in the columns or, after set_index, in the row index;
    both are row-aligned, so both are read. A description names a position
    or level number, never the label or a value, so a refusal records
    nothing about a trial.
    """
    sources: list[tuple[str, list[Any]]] = []
    for pos, label in enumerate(frame.columns):
        if _name_reads_as(label, folded_target):
            sources.append((f"column at position {pos}", _row_values(frame.iloc[:, pos])))
    for level, name in enumerate(frame.index.names):
        if _name_reads_as(name, folded_target):
            sources.append(
                (f"index level {level}", _row_values(frame.index.get_level_values(level)))
            )
    return sources


class _CellScan(NamedTuple):
    """What _frame_cell_scan found inside a frame's cells.

    `flags`: every in_audit value held inside a cell, at any depth.
    `row_ids`: (row position, value) for each nct_id found beside such a
    flag, so it can be compared with that row's own nct_id.
    """

    flags: tuple[Any, ...]
    row_ids: tuple[tuple[int, Any], ...]


def _frame_cell_scan(frame: pd.DataFrame) -> _CellScan:
    """in_audit flags held inside the cells of `frame` (dicts, lists in object columns).

    Method: only object columns whose cells pandas does not infer as all
    scalars are scanned (_SCALAR_INFERRED_DTYPES), so an AACT-shaped
    sponsor frame (strings only) costs one infer_dtype call per column. A
    frame built with pd.DataFrame(records) from manifest-merged records
    holds a nested manifest as a column of dicts; this is where its flags
    are. Each cell is scanned on its own so an nct_id beside a flag keeps
    its row. The frame's own `attrs` are checked separately
    (_refuse_frame_attrs_holdout).
    """
    flags: list[Any] = []
    row_ids: list[tuple[int, Any]] = []
    for pos in range(frame.shape[1]):
        column = frame.iloc[:, pos]
        if column.dtype != object:
            continue
        if pd.api.types.infer_dtype(column, skipna=True) in _SCALAR_INFERRED_DTYPES:
            continue
        for row, cell in enumerate(column.to_numpy(dtype=object).tolist()):
            scan = _scan_in_audit(cell)
            flags.extend(scan.flags)
            row_ids.extend((row, value) for value in scan.sibling_ids)
    return _CellScan(flags=tuple(flags), row_ids=tuple(row_ids))


def _refuse_frame_attrs_holdout(frame: pd.DataFrame, where: str) -> None:
    """Refuse a frame whose `attrs` metadata marks it as holding holdout trials.

    Method: an in_audit flag found in `frame.attrs` (at any depth, any
    spelling) that is not exactly a Python bool is refused
    ("in_audit_not_bool"), and one that is True is refused ("in_audit_true"):
    it says the frame as a whole is a holdout's. A False there is ignored
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
    flags = _scan_in_audit(attrs).flags
    not_bool = [f for f in flags if type(f) is not bool]
    if not_bool:
        raise EcosystemAuditHoldoutError(
            "in_audit_not_bool",
            f"{where}: frame.attrs holds {len(not_bool)} in_audit value(s) that are "
            f"not exactly True or False (first: {type(not_bool[0]).__name__}); "
            "refusing rather than guessing",
        )
    if any(flags):
        raise EcosystemAuditHoldoutError(
            "in_audit_true",
            f"{where}: frame.attrs marks the frame as in_audit holdout data; it "
            "must not be attributed, counted or recorded",
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
    and so is a frame whose `attrs` metadata holds a True or non-bool flag
    (_refuse_frame_attrs_holdout; a False there is ignored). A frame carries
    in_audit when a column or a row-index level has a name that reads as
    in_audit (_name_reads_as: case, spaces, hyphens and underscores
    ignored), or a cell holds one at any depth (_frame_cell_scan). A frame
    that carries none is passed through unchanged (the caller is
    responsible for it; see the module docstring).
    A frame that carries in_audit is checked in this order, over every row,
    before any rule runs:
      1. each row-level value must be a bool, i.e. Python bool or
         numpy.bool_ (`pd.api.types.is_bool`; a bool-dtype column or index
         holds numpy.bool_), so NaN, None, pd.NA, 0/1 and the strings
         "True"/"False" of a CSV read with dtype=str are refused, never
         coerced; a flag inside a cell must be exactly a Python bool, as a
         record's must ("in_audit_not_bool");
      2. no value may be True ("in_audit_true");
      3. there must be exactly one row-level source (one column or one
         index level), so neither two disagreeing flags nor flags found only
         inside cells can describe the rows ("in_audit_not_bool");
      4. every nct_id must be exactly `^NCT[0-9]{8}$`, read as stored, from
         each column or index level whose name reads as nct_id; there must
         be at least one, and when there are several they must agree row by
         row, as must an nct_id beside a flag inside a cell
         ("nct_id_malformed").
    """
    if not isinstance(frame, pd.DataFrame):
        _refuse_non_frame(frame, where)
    _refuse_frame_attrs_holdout(frame, where)
    sources = _frame_sources(frame, _IN_AUDIT_NAME)
    cells = _frame_cell_scan(frame)
    if not sources and not cells.flags:
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
    if len(sources) != 1:
        raise EcosystemAuditHoldoutError(
            "in_audit_not_bool",
            f"{where}: frame carries in_audit in {len(sources)} columns or index "
            "levels" + (" and inside cells" if cells.flags else "")
            + "; exactly one row-level in_audit column or index level is required",
        )
    id_sources = _frame_sources(frame, _NCT_ID_NAME)
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
            f"{where}: {len(stray)} nct_id value(s) beside in_audit flags inside "
            f"cells disagree with their row's nct_id (first at row position "
            f"{stray[0]}); refusing rather than choosing one",
        )


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


def _refuse_audit_holdout_records(records: list[Any], where: str) -> None:
    """Refuse records carrying `in_audit` unless each is a canonical non-holdout.

    Method: each record is scanned for in_audit flags at any depth and under
    any spelling (_scan_in_audit), whatever its type: a dict, any other
    mapping-like value (UserDict, MappingProxyType, a pandas Series row),
    or a list. sponsors.adapter reads every one of those through `.get` and
    attributes it, so none may be skipped. A record with no flag is passed
    through unchanged (the caller is responsible for it). For a record WITH
    a flag, checked before any record is adapted: every flag must be
    exactly a Python bool (`type(v) is bool`; records come from JSON, so
    numpy bools, 0/1, strings and None are refused, never coerced); any
    True is refused; then the record's nct_id (_raw_record_nct_id; None for
    a record that is not mapping-like) must be exactly `^NCT[0-9]{8}$`, and
    every other id the record carries for itself (a top-level key that
    reads as nct_id, or an nct_id beside a flag) must equal it, because a
    manifest row merged onto another trial's payload describes a different
    trial. After every record is checked, a batch in which some records
    carry in_audit and others do not is refused ("in_audit_not_bool"): it
    is what a manifest left join produces when a record finds no manifest
    row, and the frame path refuses the same gap as a NaN in its column.
    """
    carrying: list[bool] = []
    for pos, record in enumerate(records):
        scan = _scan_in_audit(record)
        if not scan.flags:
            carrying.append(False)
            continue
        for flag in scan.flags:
            if type(flag) is not bool:
                raise EcosystemAuditHoldoutError(
                    "in_audit_not_bool",
                    f"{where}: record at position {pos} carries an in_audit value "
                    f"that is not exactly True or False ({type(flag).__name__}); "
                    "refusing rather than guessing",
                )
        if any(scan.flags):
            raise EcosystemAuditHoldoutError(
                "in_audit_true",
                f"{where}: record at position {pos} is an in_audit holdout trial; "
                "it must not be attributed, counted or recorded",
            )
        raw_nct = _raw_record_nct_id(record) if _is_mapping_like(record) else None
        if not _is_canonical_nct_id(raw_nct):
            raise EcosystemAuditHoldoutError(
                "nct_id_malformed",
                f"{where}: record at position {pos} has in_audit False but an "
                f"nct_id ({type(raw_nct).__name__}) that is not exactly "
                "^NCT[0-9]{8}$ (ASCII, case-sensitive, unstripped)",
            )
        own_ids = [v for k, v in list(record.items()) if _name_reads_as(k, _NCT_ID_NAME)]
        disagreeing = [
            other
            for other in [*own_ids, *scan.sibling_ids]
            if not (isinstance(other, str) and other == raw_nct)
        ]
        if disagreeing:
            raise EcosystemAuditHoldoutError(
                "nct_id_malformed",
                f"{where}: record at position {pos} has in_audit False but "
                f"{len(disagreeing)} other nct_id value(s) in it disagree with the "
                "id it would be attributed under; refusing rather than choosing one",
            )
        carrying.append(True)
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

    Audit holdout: not a trial entry point. `uniq` is the literal universe
    (distinct sponsor names, no nct_id), so there is no trial to refuse; the
    functions that build it from trial rows enforce (see the module docstring).
    """
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
    row-index level whose name reads as in_audit (case, spaces, hyphens and
    underscores ignored), or a flag inside a cell or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool or the flag has more than
    one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$ or two nct_id sources disagree
    ("nct_id_malformed"); a False in `attrs` alone is ignored. Anything that
    is not a DataFrame is refused (see _refuse_audit_holdout_frame).
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
    row-index level whose name reads as in_audit (case, spaces, hyphens and
    underscores ignored), or a flag inside a cell or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool or the flag has more than
    one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$ or two nct_id sources disagree
    ("nct_id_malformed"); a False in `attrs` alone is ignored. Anything that
    is not a DataFrame is refused (see _refuse_audit_holdout_frame).
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

    Audit holdout: not a trial entry point. `uniq` is the literal universe
    (distinct sponsor names, no nct_id), so there is no trial to refuse; the
    functions that build it from trial rows enforce (see the module docstring).
    """
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
    row-index level whose name reads as in_audit (case, spaces, hyphens and
    underscores ignored), or a flag inside a cell or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool or the flag has more than
    one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$ or two nct_id sources disagree
    ("nct_id_malformed"); a False in `attrs` alone is ignored. Anything that
    is not a DataFrame is refused (see _refuse_audit_holdout_frame).
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
    depth, under any spelling, in a dict or any mapping-like record such as
    a UserDict, a MappingProxyType or a pandas Series row), checked for all
    records before any rules file is read or any record is adapted: raises
    EcosystemAuditHoldoutError if a value is True ("in_audit_true"), is not
    exactly a bool ("in_audit_not_bool"), or is False while the record's
    nct_id (top-level `nct_id` of a stored record,
    protocolSection.identificationModule.nctId of a CT.gov dict) is not
    exactly ^NCT[0-9]{8}$ or another nct_id the record carries for itself
    disagrees with it ("nct_id_malformed"). A batch in which some records
    carry `in_audit` and others do not is refused ("in_audit_not_bool"). A
    single record (any mapping-like value) or a string passed in place of an
    iterable of records is refused with TypeError before anything is read.
    A stored record or a CT.gov study dict carries no `in_audit`; for a batch
    of them this function relies on its caller to exclude holdout trials.
    Callers today: none in code (see the module docstring).
    """
    if isinstance(records, (str, bytes, bytearray)) or _is_mapping_like(records):
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
    row-index level whose name reads as in_audit (case, spaces, hyphens and
    underscores ignored), or a flag inside a cell or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool or the flag has more than
    one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$ or two nct_id sources disagree
    ("nct_id_malformed"); a False in `attrs` alone is ignored. Anything that
    is not a DataFrame is refused (see _refuse_audit_holdout_frame).
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
    row-index level whose name reads as in_audit (case, spaces, hyphens and
    underscores ignored), or a flag inside a cell or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool or the flag has more than
    one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$ or two nct_id sources disagree
    ("nct_id_malformed"); a False in `attrs` alone is ignored. Anything that
    is not a DataFrame is refused (see _refuse_audit_holdout_frame).
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
    row-index level whose name reads as in_audit (case, spaces, hyphens and
    underscores ignored), or a flag inside a cell or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool or the flag has more than
    one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$ or two nct_id sources disagree
    ("nct_id_malformed"); a False in `attrs` alone is ignored. Anything that
    is not a DataFrame is refused (see _refuse_audit_holdout_frame).
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
    row-index level whose name reads as in_audit (case, spaces, hyphens and
    underscores ignored), or a flag inside a cell or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool or the flag has more than
    one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$ or two nct_id sources disagree
    ("nct_id_malformed"); a False in `attrs` alone is ignored. Anything that
    is not a DataFrame is refused (see _refuse_audit_holdout_frame).
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
    row-index level whose name reads as in_audit (case, spaces, hyphens and
    underscores ignored), or a flag inside a cell or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool or the flag has more than
    one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$ or two nct_id sources disagree
    ("nct_id_malformed"); a False in `attrs` alone is ignored. Anything that
    is not a DataFrame is refused (see _refuse_audit_holdout_frame).
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
    row-index level whose name reads as in_audit (case, spaces, hyphens and
    underscores ignored), or a flag inside a cell or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool or the flag has more than
    one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$ or two nct_id sources disagree
    ("nct_id_malformed"); a False in `attrs` alone is ignored. Anything that
    is not a DataFrame is refused (see _refuse_audit_holdout_frame).
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
    row-index level whose name reads as in_audit (case, spaces, hyphens and
    underscores ignored), or a flag inside a cell or the frame's `attrs`.
    Before any rule runs it raises EcosystemAuditHoldoutError if any value
    is True ("in_audit_true"), any is not a bool or the flag has more than
    one row-level source ("in_audit_not_bool"), or any nct_id is not
    exactly ^NCT[0-9]{8}$ or two nct_id sources disagree
    ("nct_id_malformed"); a False in `attrs` alone is ignored. Anything that
    is not a DataFrame is refused (see _refuse_audit_holdout_frame).
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
    """Write a generated baseline block keyed by ecosystem_rules.csv SHA-256."""
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
