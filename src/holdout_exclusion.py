"""Audit holdout (in_audit) EXCLUSION for the manual data families.

READS   The site's outcomes index (clinical-trial-populations
        data/outcomes/index.json) at the path a command is given, and, for the
        exporters' defence-in-depth scan, the files of a finished build.
WRITES  Nothing. Messages and stamps carry counts, positions, file names and a
        sha256, never a trial id.
USED BY scripts/build_drug_disclosure.py, disclosure/, scripts/build_dap_benchmarks.py,
        dap/, scripts/build_feasibility_benchmarks.py, feasibility/,
        scripts/build_outcomes_comparability.py, outcomes/comparability_build.py,
        outcomes/outcome_history.py, their contract checkers and site exporters.

The rule (the investigator's decision): a trial whose ``in_audit`` is true in the
site's outcomes index is a holdout. These families EXCLUDE it: it is removed from
the trial universe by its exact id before anything reads, fetches, parses,
counts or records it, it is counted in no aggregate and it is never published.
This is the same rule ``src/holdout_guard.py`` states ("must not be parsed,
attributed, counted or recorded"); that module's contract is unchanged (it
refuses input that carries the flag and never receives the id set). Here the
families receive the id set itself, from one strict loader, and drop by id.

Every build records what it did in ``run.json`` under ``holdout_exclusion``
(``stamp``): the rules version, the sha256 of the index file's bytes, how many
holdouts the index names and how many of them were present in this build's input
universe and removed. No path and no id. A contract checker requires the stamp
(``stamp_problems``); an exporter requires that the stamp's sha256 is the index it
is given (``require_matching_stamp``) and that no file of the build names a
holdout id (``refuse_holdout_mentions``), so a build made before this rule, or
under another index, is refused until it is rebuilt.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable, Mapping

from src.holdout_guard import is_canonical_nct_id

# The rules this module applies, recorded on every stamp. Changing what counts as
# a holdout, or where it is removed, is a new version: a stamp written under
# another version is refused by stamp_problems, so such a build is rebuilt.
RULES_VERSION: Final[str] = "holdout-exclusion-1"

# The run.json key that holds the stamp, and the stamp's exact keys.
STAMP_KEY: Final[str] = "holdout_exclusion"
STAMP_FIELDS: Final[tuple[str, ...]] = (
    "rules_version",
    "index_sha256",
    "holdouts_in_index",
    "excluded_from_universe",
)

_HEX64_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
# A trial id written anywhere in an output file: NCT and eight ASCII digits, not
# inside a longer run of letters or digits (NCT123456789 is not NCT12345678). The
# prefix is matched in any letter case and the token upper-cased before it is
# compared, as `_matches` reads a lower-cased id in an input and as the citation
# verifier's embedded-id scan is case-insensitive: free text such as a citation or
# an outcome description can name a holdout as `nct...` or `Nct...`, and the scan
# is the exporters' last check that no build file names one.
_NCT_TOKEN_RE: Final[re.Pattern[bytes]] = re.compile(rb"(?<![0-9A-Za-z])[Nn][Cc][Tt][0-9]{8}(?![0-9])")
_GZIP_MAGIC: Final[bytes] = b"\x1f\x8b"


class HoldoutIndexError(Exception):
    """A holdout index, stamp or exclusion step that cannot be trusted; the command refuses.

    Deliberately NOT a ValueError (as holdout_guard.AuditHoldoutError is not): a
    caller's ``except ValueError`` written for malformed records must not turn a
    holdout refusal into a skipped row. Messages give counts, positions, types
    and file names, never an id.
    """


@dataclass(frozen=True)
class HoldoutIndex:
    """The holdout ids of one site outcomes index, with the sha256 of its bytes.

    ``ids`` is left out of the repr, so a log or an error that shows the object
    names no trial.
    """

    ids: frozenset[str] = field(repr=False)
    sha256: str
    n_rows: int

    @property
    def n_holdouts(self) -> int:
        """How many rows of the index are holdouts (``in_audit`` exactly true)."""
        return len(self.ids)


def _refuse_repeated_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """object_pairs_hook: a JSON object that repeats a key is refused, never read last-key-wins.

    ``{"in_audit": true, ..., "in_audit": false}`` reads as not a holdout to a
    plain json.loads; which value the index means is not determined, so it is
    refused.
    """
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise HoldoutIndexError("the index repeats a key inside one JSON object; refusing rather "
                                    "than reading one of the values")
        out[key] = value
    return out


def _refuse_constant(name: str) -> Any:
    """parse_constant: NaN and Infinity are not JSON, so the index is refused."""
    raise HoldoutIndexError(f"the index holds the non-JSON constant {name}")


def _positions(positions: list[int]) -> str:
    """The first few row positions, for a refusal message."""
    shown = ", ".join(str(p) for p in positions[:5])
    return shown + (", ..." if len(positions) > 5 else "")


def load_holdout_index(path: Path) -> HoldoutIndex:
    """Load the holdout id set of a site outcomes index, strictly; raise HoldoutIndexError otherwise.

    Method: the file's bytes are read once and hashed (the stamp's
    ``index_sha256``), then decoded as UTF-8 JSON with repeated keys and NaN or
    Infinity refused. The index is the object the outcomes build writes: a
    non-empty list under ``rows``, every row an object whose ``nct_id`` is
    exactly a str matching ^NCT[0-9]{8}$ (holdout_guard.is_canonical_nct_id: no
    padding, case or look-alike digits repaired) and whose ``in_audit`` is
    exactly True or False (0, 1, "true" and null are never coerced), with no id
    repeated. Anything else refuses the whole file: a holdout flag that cannot
    be read cannot be taken as "not a holdout". The holdouts are the rows with
    ``in_audit`` True. Other row fields are not read. Messages name the file,
    counts, positions and types, never an id.
    """
    name = path.name
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise HoldoutIndexError(f"{name}: cannot read the holdout index ({type(exc).__name__})") from exc
    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_refuse_repeated_keys,
                             parse_constant=_refuse_constant)
    except HoldoutIndexError as exc:
        raise HoldoutIndexError(f"{name}: {exc}") from None
    except (UnicodeDecodeError, ValueError) as exc:
        raise HoldoutIndexError(f"{name}: not UTF-8 JSON ({type(exc).__name__})") from None
    if type(payload) is not dict:
        raise HoldoutIndexError(f"{name}: the index must be a JSON object with a rows list, "
                                f"got a {type(payload).__name__}")
    if "rows" not in payload:
        raise HoldoutIndexError(f"{name}: the index has no rows list")
    rows = payload["rows"]
    if type(rows) is not list:
        raise HoldoutIndexError(f"{name}: rows is a {type(rows).__name__}, not a list")
    if not rows:
        raise HoldoutIndexError(f"{name}: rows is empty; an index that lists no trial cannot say "
                                "which trials are holdouts")
    not_object = [pos for pos, row in enumerate(rows) if type(row) is not dict]
    if not_object:
        raise HoldoutIndexError(f"{name}: {len(not_object)} row(s) are not JSON objects (row "
                                f"position(s) {_positions(not_object)})")
    bad_id = [pos for pos, row in enumerate(rows) if not is_canonical_nct_id(row.get("nct_id"))]
    if bad_id:
        raise HoldoutIndexError(f"{name}: {len(bad_id)} row(s) have an nct_id that is missing or not "
                                f"exactly ^NCT[0-9]{{8}}$ (row position(s) {_positions(bad_id)})")
    bad_flag = [pos for pos, row in enumerate(rows) if type(row.get("in_audit")) is not bool]
    if bad_flag:
        first = rows[bad_flag[0]]
        kind = type(first["in_audit"]).__name__ if "in_audit" in first else "missing"
        raise HoldoutIndexError(f"{name}: {len(bad_flag)} row(s) have an in_audit that is not exactly "
                                f"true or false (row position(s) {_positions(bad_flag)}; first: "
                                f"{kind}); refusing rather than guessing")
    first_at: dict[str, int] = {}
    repeated: list[int] = []
    for pos, row in enumerate(rows):
        if row["nct_id"] in first_at:
            repeated.append(pos)
        else:
            first_at[row["nct_id"]] = pos
    if repeated:
        raise HoldoutIndexError(f"{name}: {len(repeated)} row(s) repeat an nct_id of an earlier row "
                                f"(row position(s) {_positions(repeated)}); refusing rather than "
                                "choosing one row's in_audit")
    ids = frozenset(row["nct_id"] for row in rows if row["in_audit"] is True)
    return HoldoutIndex(ids=ids, sha256=digest, n_rows=len(rows))


def _matches(value: Any, ids: frozenset[str]) -> bool:
    """True when ``value`` is a str naming one of ``ids``.

    Method: compared exactly, and again with surrounding whitespace removed and
    upper-cased, so a padded or lower-cased copy of a holdout id in an input is
    excluded too. The normalised form is used only for this comparison, never
    written: excluding a disguised holdout is the safe direction.
    """
    if not isinstance(value, str):
        return False
    text = str.__str__(value)
    return text in ids or text.strip().upper() in ids


def _present_id(value: Any) -> bool:
    """Whether ``value`` is a trial id a record carries: anything but None and a blank string.

    A blank string is read as absent, as the history parser (``raw.get('nct_id') or ...``) and
    ``outcomes.comparability.nct_id_of`` read it, so a blank top-level id cannot stand in front of
    the id in ``protocolSection``.
    """
    return value is not None and not (isinstance(value, str) and not value.strip())


def _id_key(value: Any) -> tuple[str, str]:
    """A comparison key for one carried id: a str as ``_matches`` normalises it, anything else by type."""
    if isinstance(value, str):
        return ("str", str.__str__(value).strip().upper())
    return (type(value).__name__, repr(value))


def record_trial_id(record: Any, where: str = "a study record") -> Any:
    """The one trial id a study record carries, read with type checks only; None when it carries none.

    Method: every place a study record can carry its id is read: a weekly or flattened record's
    top-level ``nct_id`` or ``nctId``, and a CT.gov API v2 record's
    ``protocolSection.identificationModule.nctId``. None and blank strings are absent. When the
    present values disagree (compared as ``_matches`` compares: surrounding whitespace removed,
    letters upper-cased; a non-str by type and value), the record is refused
    (``HoldoutIndexError``): a loader that reads one place and an extractor that reads another
    would take it for different trials, and a clean id in one place could hide a holdout in the
    other. Otherwise the first present value is returned as it is, for the caller to test with
    ``HoldoutExclusion.exclude`` or ``is_holdout``; a non-str is returned too, and the caller's
    record checks refuse it. The message names the place, never an id.
    """
    if not isinstance(record, dict):
        return None
    ps = record.get("protocolSection")
    ident = ps.get("identificationModule") if isinstance(ps, dict) else None
    carried = [record.get("nct_id"), record.get("nctId"),
               ident.get("nctId") if isinstance(ident, dict) else None]
    present = [value for value in carried if _present_id(value)]
    if len({_id_key(value) for value in present}) > 1:
        raise HoldoutIndexError(f"{where} carries {len(present)} trial ids that disagree (top-level "
                                "nct_id/nctId and protocolSection.identificationModule.nctId); the "
                                "loader and the extractor would read different trials, so the input is "
                                "refused")
    return present[0] if present else None


class HoldoutExclusion:
    """One build's exclusion of the holdouts of a HoldoutIndex from its input universe.

    A loader calls ``exclude`` (or ``exclude_ids``) for every trial id of the
    universe it reads (a list, a snapshot's rows, a cache's frame, local
    records), before it reads anything else of that trial; a holdout is
    remembered, in memory only, so ``stamp`` can count the distinct holdouts
    removed. ``is_holdout`` drops a trial without counting it: for an auxiliary
    per-trial input (a MeSH sidecar line, a history archive line) or a
    selection inside an already-counted frame, which do not widen the universe.
    The ids themselves are never returned or written.
    """

    def __init__(self, index: HoldoutIndex) -> None:
        self.index: HoldoutIndex = index
        self._excluded: set[str] = set()
        self._carried: int = 0
        self._applied: bool = False

    def __repr__(self) -> str:
        return (f"HoldoutExclusion(holdouts_in_index={self.index.n_holdouts}, "
                f"excluded_from_universe={self.n_excluded})")

    @property
    def n_excluded(self) -> int:
        """Distinct holdouts removed from this build's universe, including any carried forward."""
        return self._carried + len(self._excluded)

    def is_holdout(self, nct_id: Any) -> bool:
        """True when ``nct_id`` names a holdout. Records nothing."""
        return _matches(nct_id, self.index.ids)

    @staticmethod
    def trial_id_of(record: Any, where: str = "a study record") -> Any:
        """``record_trial_id`` for a caller that holds an exclusion but does not import this module."""
        return record_trial_id(record, where)

    def exclude(self, nct_id: Any) -> bool:
        """True (and counted once) when ``nct_id`` is a holdout of the universe; the caller drops it."""
        self._applied = True
        if not self.is_holdout(nct_id):
            return False
        self._excluded.add(str.__str__(nct_id).strip().upper())
        return True

    def exclude_ids(self, ids: Iterable[str]) -> list[str]:
        """``ids`` in their order without the holdouts, which are counted as removed from the universe.

        Marks the exclusion applied even for an empty list: the universe was given and filtered.
        """
        self._applied = True
        return [i for i in ids if not self.exclude(i)]

    def without_holdouts(self, ids: Iterable[str]) -> list[str]:
        """``ids`` in their order without the holdouts, counting nothing (a selection or auxiliary input)."""
        return [i for i in ids if not self.is_holdout(i)]

    def carry_forward(self, stamp: Any, where: str) -> None:
        """Add the count of an earlier stage's stamp made under this same index (a frozen cache's).

        Method: the earlier stage (disclosure acquisition or a local freeze)
        removed its holdouts before they entered the cache, so the build cannot
        see them; their count is taken from the stamp it wrote. A stamp of
        another index, or of another rules version, is refused: its count says
        nothing about this index, and the cache may hold trials this index calls
        holdouts. Those removed at the earlier stage never entered its frame, so
        they are disjoint from any this build removes from that frame.
        """
        problems = stamp_problems(stamp)
        if problems:
            raise HoldoutIndexError(f"{where}: {problems[0]}")
        if stamp["index_sha256"] != self.index.sha256:
            raise HoldoutIndexError(f"{where}: was made under another holdout index (its sha256 differs "
                                    "from --holdout-index); rebuild it with this index")
        self._carried += stamp["excluded_from_universe"]

    def refuse_curated_rows(self, ids: Iterable[tuple[int, Any]], label: str) -> None:
        """Refuse a curated file (human reviews, documentary statements) with a row naming a holdout.

        A curated row is not a universe: it records something about a trial. A
        row about a holdout means a holdout was recorded where it must not be,
        so the build refuses until a person removes the row, rather than
        dropping it quietly. ``ids`` gives (row number, id); the message names
        the rows by number only.
        """
        rows = [row for row, value in ids if self.is_holdout(value)]
        if rows:
            raise HoldoutIndexError(f"{label}: {len(rows)} row(s) name an in_audit holdout trial (row(s) "
                                    f"{_positions(rows)}); a holdout must not be reviewed or recorded "
                                    "here, so remove those rows")

    def stamp(self) -> dict[str, Any]:
        """The run.json ``holdout_exclusion`` object; refuse if no loader applied the exclusion.

        A build function handed a stamp from an exclusion that no loader used
        would claim an exclusion that never ran, so that is refused.
        """
        if not self._applied:
            raise HoldoutIndexError("the holdout exclusion was never applied to this build's input "
                                    "universe; refusing to stamp a build it did not filter")
        return {
            "rules_version": RULES_VERSION,
            "index_sha256": self.index.sha256,
            "holdouts_in_index": self.index.n_holdouts,
            "excluded_from_universe": self.n_excluded,
        }


def _is_count(value: Any) -> bool:
    """Exactly an int (not a bool) and not negative."""
    return type(value) is int and value >= 0


def stamp_problems(stamp: Any) -> list[str]:
    """Why ``stamp`` is not a valid run.json ``holdout_exclusion`` object (an empty list passes).

    Exactly the keys STAMP_FIELDS: ``rules_version`` is RULES_VERSION,
    ``index_sha256`` 64 lowercase hex, ``holdouts_in_index`` and
    ``excluded_from_universe`` non-negative ints (bools refused). The excluded
    count is not bounded by the index count: a frame may hold a disguised copy
    of an id, and an earlier stage's count is carried forward.
    """
    where = f"run.json {STAMP_KEY}"
    if stamp is None:
        return [f"{where} is missing; a build made before holdout exclusion (or without "
                "--holdout-index) must be rebuilt"]
    if type(stamp) is not dict:
        return [f"{where} is a {type(stamp).__name__}, not an object"]
    problems: list[str] = []
    if set(stamp) != set(STAMP_FIELDS):
        problems.append(f"{where} must hold exactly {list(STAMP_FIELDS)}, got {sorted(map(str, stamp))}")
    if stamp.get("rules_version") != RULES_VERSION:
        problems.append(f"{where}.rules_version is not {RULES_VERSION!r}")
    sha = stamp.get("index_sha256")
    if not (type(sha) is str and _HEX64_RE.fullmatch(sha)):
        problems.append(f"{where}.index_sha256 must be a 64-hex sha256")
    for key in ("holdouts_in_index", "excluded_from_universe"):
        if not _is_count(stamp.get(key)):
            problems.append(f"{where}.{key} must be a non-negative integer")
    return problems


def require_matching_stamp(run: Any, index: HoldoutIndex) -> dict[str, Any]:
    """The build's valid stamp when it was made under ``index``; else raise HoldoutIndexError.

    Used by the exporters: a build without a valid stamp predates this rule (or
    was made without --holdout-index) and is refused; a stamp whose sha256 is
    not the given index's was made under an index that has changed since, so
    the build must be made again.
    """
    stamp = run.get(STAMP_KEY) if isinstance(run, dict) else None
    problems = stamp_problems(stamp)
    if problems:
        raise HoldoutIndexError(f"{problems[0]}; rebuild with the current builder and --holdout-index")
    if stamp["index_sha256"] != index.sha256 or stamp["holdouts_in_index"] != index.n_holdouts:
        raise HoldoutIndexError("the build was made under another holdout index (its "
                                f"{STAMP_KEY}.index_sha256 is not the sha256 of --holdout-index): "
                                "the index changed since the build; rebuild it")
    return dict(stamp)


def count_holdout_mentions(data: bytes, ids: frozenset[str]) -> int:
    """How many NCT id tokens in ``data`` (gunzipped first when it is gzip) name a holdout.

    A token is matched in any letter case (``_NCT_TOKEN_RE``) and upper-cased before it
    is compared with ``ids``, so ``nct99777701`` counts as ``NCT99777701``.
    """
    if data[:2] == _GZIP_MAGIC:
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError) as exc:
            raise HoldoutIndexError(f"a gzip file of the build cannot be decompressed for the holdout "
                                    f"scan ({type(exc).__name__})") from exc
    return sum(1 for token in _NCT_TOKEN_RE.findall(data) if token.decode("ascii").upper() in ids)


def refuse_holdout_mentions(build_dir: Path, index: HoldoutIndex) -> None:
    """Refuse a build any of whose files names a holdout id (defence in depth for the exporters).

    Method: every regular file under ``build_dir`` (per-trial files, summaries,
    run.json) is read as bytes, gunzipped when it is gzip, and its NCT id tokens
    compared with the holdout ids. A build made under the exclusion holds none;
    one that does was made otherwise, so nothing of it is exported. The message
    names files and counts, never an id.
    """
    hits: list[tuple[str, int]] = []
    for path in sorted(p for p in build_dir.rglob("*") if p.is_file() and not p.is_symlink()):
        found = count_holdout_mentions(path.read_bytes(), index.ids)
        if found:
            hits.append((path.relative_to(build_dir).as_posix(), found))
    if hits:
        listed = ", ".join(f"{name} ({n})" for name, n in hits[:5])
        raise HoldoutIndexError(f"{len(hits)} file(s) of the build name an in_audit holdout trial "
                                f"({listed}); a holdout is never published, so the build is refused")


def curated_csv_ids(path: Path, column: str = "nct_id") -> list[tuple[int, Any]]:
    """(row number, value of ``column``) for each data row of a curated CSV; [] when it has no such column.

    Read before the family's own reader validates the file, so a row about a
    holdout is refused (HoldoutExclusion.refuse_curated_rows) before any other
    field of it is checked or echoed. A missing file gives [], since the
    family's reader refuses it in its own words. Row numbers count the header
    as row 1, as the readers' messages do.
    """
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if column not in (reader.fieldnames or ()):
            return []
        return [(row_no, (row.get(column) or "")) for row_no, row in enumerate(reader, start=2)]


def stamp_for_site(stamp: Mapping[str, Any]) -> dict[str, Any]:
    """The stamp as a site export's run.json carries it: the same four keys (a sha256 and counts)."""
    return {key: stamp[key] for key in STAMP_FIELDS}
