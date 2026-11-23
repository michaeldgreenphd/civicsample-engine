#!/usr/bin/env python3
"""Build or validate a cohort-agnostic sharded outcomes & provenance tree.

READS   --cohort-manifest  REQUIRED JSON/JSONL (plain or .gz): one row per trial
                           in the build, each with a strict bool in_audit. It is
                           the allowlist: every other input may name only its
                           trials, and its in_audit is the only one that counts.
                           The site's live outcomes index (data/outcomes/
                           index.json, {"rows": [...]}) is accepted as is;
                           a trial built from its manifest row alone (no
                           --trials record) has has_reported_results null,
                           since the row does not carry the CT.gov results.
        --manifest-cohort  optional cohort slug every manifest row belongs to
                           (e.g. industry_outcomes_beta for the live outcomes
                           index, whose rows carry no cohort, sponsor class or
                           phase); without it such a row resolves to no cohort
                           and is refused.
        --trials           optional JSON/JSONL of CT.gov study records or flat
                           trial rows; every trial must be in the manifest.
        --documents        optional JSON/JSONL of Module 1 TrialDocumentProfile
                           records or legacy documents.jsonl rows.
        --locations        optional JSON/JSONL of decision-rule location rows
                           (locations.jsonl) or pre-grouped location objects;
                           may be given more than once (rows are appended; a
                           cell given twice, or a pre-grouped object for a trial
                           that already has rows, is refused).
        --provenance       optional JSON/JSONL of Module 4 provenance projections.
        --population-parsed optional JSON/JSONL of Module 2 parsed populations.
        Every option but --locations may be given once. Every ID anywhere in
        these inputs must be strict ASCII ^NCT[0-9]{8}$; a side record for a
        trial outside the manifest, for a holdout trial, naming another trial
        (under a trial-ID key such as nct_id/nctId/trial_nct_id, in a URL, or
        as a mapping key), naming a holdout trial anywhere, or carrying a
        holdout flag (in_audit under any spelling: inAudit, IN_AUDIT,
        in-audit, is_in_audit) that contradicts the manifest is refused,
        never dropped. A --trials record is held to the same rule for its
        trial-ID keys and holdout flags, and a holdout's own record may carry
        no side section inline. A JSON object repeating a key is refused.
WRITES  --out-dir (default <engine root>/data/outcomes_bundles, resolved from
        this file, not the working directory):
          index.json            multi-cohort trial catalog, sorted by nct_id
          shards/0.json.gz      per-trial unified shard bundles (n = int(nct[3:]) % N)
            .. {N-1}.json.gz    gzip level 9, mtime=0, OS byte 0xff
          outcomes_bundle.json  the marker naming this layout and its version
        Holdout (in_audit) trials are written as stubs: index fields only.
        Staged in <out-dir>.partial, verified with
        src.outcomes_layout.validate_outcomes_tree, then swapped in. The
        target must be absent, empty, or a bundle this builder wrote; see
        src.outcomes_layout.check_output_target for every refusal (a foreign
        or symlinked target or ancestor, a .partial this builder did not
        create, a retained backup from a failed restore). The default target
        is refused too when the engine's data/ folder holds a weekly dataset
        (data/run.json); pass --out-dir elsewhere then.
        source_pipeline_commit is --pipeline-commit, else the engine
        checkout's own HEAD (git -C <engine root>); when neither resolves the
        build is refused, never stamped null.
        --report (optional) gets the same allowlist.
CHECKS  --check <dir> validates an existing outcomes directory on disk without
        modifying it (--allow-null-commit accepts a tree stamped null).
EXITS   0 on success; 2 on any AuditFirewallError (in_audit holdout violation,
        aborted BEFORE touching --out-dir; trial IDs are withheld from the
        message); 1 on any other contract, input, or validation error. Any
        refusal that names a holdout trial, of either kind, in a build or in
        --check, has every trial ID in it withheld.
"""
from __future__ import annotations

import argparse
import gzip
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import outcomes_layout as ol  # noqa: E402

# The engine checkout this script belongs to. The default output directory is
# resolved from here so that running the script from another working
# directory cannot write a data/outcomes_bundles tree somewhere else.
ENGINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = str(ENGINE_ROOT / "data" / ol.DEFAULT_OUT_DIR_NAME)


class _StoreOnce(argparse.Action):
    """Store an option's value, refusing a second occurrence.

    argparse's default ``store`` keeps the last of two ``--documents`` and
    drops the first without a word; a second occurrence is an error instead.
    """

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        if getattr(namespace, f"_seen_{self.dest}", False):
            parser.error(f"{option_string} given more than once; pass it once")
        setattr(namespace, f"_seen_{self.dest}", True)
        setattr(namespace, self.dest, values)


# A JSON object in an input repeats a key. The rule is the module's
# (src.outcomes_layout.loads_refusing_repeated_keys), so inputs and a tree on
# disk (--check) are read the same way.
_RepeatedKeyError = ol.RepeatedKeyError


def _loads_strict(text: str) -> Any:
    """``json.loads`` refusing an object that repeats a key (``ol.loads_refusing_repeated_keys``)."""
    return ol.loads_refusing_repeated_keys(text)


def _is_holdout_flag_key(key: Any) -> bool:
    """A key a reader takes for the holdout flag (``ol.is_holdout_flag_key``, Module 4's reading)."""
    return ol.is_holdout_flag_key(key)


def _read_text(path: Path) -> str:
    """Read a plain UTF-8 or ``.gz`` text file."""
    try:
        if str(path).endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return fh.read()
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, EOFError) as exc:
        raise ol.OutcomesLayoutError(f"{path}: cannot be read ({exc})") from exc


def _load_records_file(path: str | os.PathLike[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load JSON or JSONL (plain or ``.gz``) into ``(records, container_meta)``.

    Accepts:
    - JSONL / ``.jsonl.gz`` (one JSON object per non-empty line)
    - JSON array of objects
    - JSON container object with ``data``, ``trials``, ``records``, or ``rows``
      list (including the live outcomes ``index.json``; preserving top-level
      ``extracted_at`` / ``pipeline_commit`` / ``source_pipeline_commit`` /
      ``run_id`` / ``schema_version``)
    - JSON mapping keyed by ``nct_id`` -> object (an inner ``nct_id`` must agree)
    Every object refuses a repeated key (``_refuse_repeated_keys``).
    """
    p = Path(path)
    if not p.is_file():
        raise ol.OutcomesLayoutError(f"{p}: input file not found")

    raw_text = _read_text(p)
    stripped = raw_text.strip()
    if not stripped:
        return [], {}

    # Try full JSON document first; fall back to JSONL line-by-line parsing.
    try:
        parsed = _loads_strict(stripped)
    except _RepeatedKeyError as exc:
        raise _RepeatedKeyError(f"{p}: {exc}") from None
    except ValueError:
        rows: list[dict[str, Any]] = []
        for lineno, line in enumerate(raw_text.splitlines(), start=1):
            text = line.strip()
            if not text:
                continue
            try:
                obj = _loads_strict(text)
            except _RepeatedKeyError as exc:
                raise _RepeatedKeyError(f"{p}:{lineno}: {exc}") from None
            except ValueError as exc:
                raise ol.OutcomesLayoutError(f"{p}:{lineno}: invalid JSONL ({exc})") from exc
            if not isinstance(obj, dict):
                raise ol.OutcomesLayoutError(f"{p}:{lineno}: JSONL line must be a JSON object")
            rows.append(obj)
        return rows, {}

    if isinstance(parsed, list):
        if not all(isinstance(x, dict) for x in parsed):
            raise ol.OutcomesLayoutError(f"{p}: JSON list must contain only objects")
        return parsed, {}

    if isinstance(parsed, dict):
        meta = {
            k: parsed.get(k)
            for k in (
                "extracted_at",
                "pipeline_commit",
                "source_pipeline_commit",
                "indexer_rules_version",
                "run_id",
                "schema_version",
            )
            if k in parsed
        }
        for list_key in ("data", "trials", "records", "rows"):
            if isinstance(parsed.get(list_key), list):
                items = parsed[list_key]
                if not all(isinstance(x, dict) for x in items):
                    raise ol.OutcomesLayoutError(f"{p}.{list_key}: must contain only objects")
                return items, meta
        if parsed and all(isinstance(k, str) and ol.NCT_RE.fullmatch(k) and isinstance(v, dict) for k, v in parsed.items()):
            out_rows: list[dict[str, Any]] = []
            for nct_id, val in parsed.items():
                entry = dict(val)
                if "nct_id" in entry and entry["nct_id"] != nct_id:
                    raise ol.OutcomesLayoutError(
                        f"{p}: an entry's nct_id differs from the key it is filed under"
                    )
                entry["nct_id"] = nct_id
                out_rows.append(entry)
            return out_rows, {}
        return [parsed], {}

    raise ol.OutcomesLayoutError(f"{p}: unsupported JSON root type {type(parsed).__name__}")


def _extract_nct_id(obj: Mapping[str, Any], source_label: str) -> str:
    """Resolve a record's strict NCT ID (flat ``nct_id`` or CT.gov ``nctId``; both must agree)."""
    try:
        return ol.trial_nct_id(obj)
    except ol.OutcomesLayoutError as exc:
        raise ol.OutcomesLayoutError(f"{source_label}: {exc}") from None


def _group_side_stream(
    rows: Sequence[Mapping[str, Any]],
    *,
    source: str,
    is_grouped: Any,
    grouped_label: str,
) -> dict[str, Any]:
    """Group a side stream by trial: one grouped object, or a list of flat rows.

    A second grouped object for a trial, or grouped and flat rows for the same
    trial, is refused (one would otherwise shadow the other).
    """
    grouped: dict[str, Any] = {}
    for row in rows:
        nct_id = _extract_nct_id(row, source)
        if is_grouped(row):
            if nct_id in grouped:
                raise ol.OutcomesLayoutError(
                    f"{source}: duplicate {grouped_label} for a trial, or {grouped_label} mixed with flat rows"
                )
            grouped[nct_id] = row
        else:
            existing = grouped.get(nct_id)
            if isinstance(existing, dict):
                raise ol.OutcomesLayoutError(f"{source}: {grouped_label} mixed with flat rows for a trial")
            grouped.setdefault(nct_id, []).append(row)
    return grouped


def _load_documents_stream(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load ``--documents`` as either Module 1 profiles or legacy ``documents.jsonl`` rows."""
    rows, _ = _load_records_file(path)
    return _group_side_stream(
        rows,
        source=f"--documents {path}",
        is_grouped=lambda r: isinstance(r.get("documents"), list),
        grouped_label="TrialDocumentProfile",
    )


def _is_grouped_locations(row: Mapping[str, Any]) -> bool:
    """A pre-grouped location object: any key that names a pass section.

    Read broadly (any key ending in ``_pass``) so an object whose only pass is
    misspelled, or that lacks ``primary_pass``, reaches the normalizer as a
    grouped object and is refused there with its unknown key named, instead of
    being read as one flat row.
    """
    return any(k in ol.GROUPED_PASS_KEYS or str(k).endswith("_pass") for k in row)


def _load_locations_streams(paths: Sequence[str]) -> dict[str, Any]:
    """Load every ``--locations`` file and append their rows (L6).

    The same path twice is refused outright. Rows from several files are
    concatenated before grouping, so a cell given in two files reaches the
    normalizer twice and is refused as a duplicate, and a pre-grouped object
    for a trial that another file gives rows for is refused as mixed.
    """
    seen: set[str] = set()
    all_rows: list[dict[str, Any]] = []
    for path in paths:
        key = os.path.abspath(path)
        if key in seen:
            raise ol.OutcomesLayoutError(f"--locations {path} given more than once")
        seen.add(key)
        rows, _ = _load_records_file(path)
        all_rows.extend(rows)
    return _group_side_stream(
        all_rows,
        source="--locations",
        is_grouped=_is_grouped_locations,
        grouped_label="pre-grouped decision_rule_locations object",
    )


def _load_provenance_stream(path: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    """Load ``--provenance`` records keyed by ``nct_id`` (one per trial)."""
    rows, _ = _load_records_file(path)
    by_nct: dict[str, dict[str, Any]] = {}
    for row in rows:
        nct_id = _extract_nct_id(row, f"--provenance {path}")
        if nct_id in by_nct:
            raise ol.OutcomesLayoutError(f"--provenance {path}: duplicate longitudinal_provenance record for a trial")
        by_nct[nct_id] = row
    return by_nct


def _load_population_parsed_stream(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load ``--population-parsed`` Module 2 outputs keyed by ``nct_id``."""
    rows, _ = _load_records_file(path)
    return _group_side_stream(
        rows,
        source=f"--population-parsed {path}",
        is_grouped=lambda r: isinstance(r.get("outcomes"), list),
        grouped_label="TrialAnalysisPopulations record",
    )


def _merge_trials_and_manifest(
    trials_rows: Sequence[Mapping[str, Any]],
    manifest_rows: Sequence[Mapping[str, Any]],
    *,
    manifest_cohort: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Merge ``--trials`` into the ``--cohort-manifest`` rows, keyed by ``nct_id``.

    Method: the manifest is the allowlist and the authority on ``in_audit``.
    Every manifest row needs a strict bool under the exact key ``in_audit``
    (the only key that admits a trial); any other spelling of the holdout
    flag a reader takes for it (Module 4's ``_is_in_audit_key``: ``inAudit``,
    ``IN_AUDIT``, ``in-audit``, ``is_in_audit``) on the same row must be the
    same bool, or the row does not say which it means and is refused
    (red-team m3 round 1: a row with ``in_audit`` false and ``inAudit`` true
    was built in scope). A trial in ``--trials`` that the manifest does not
    list is refused whatever it says about itself; a trial's own holdout
    flags, under every spelling, when present, must be the manifest's bool
    (red-team m3 round 1: ``inAudit: true`` on a --trials record was ignored).
    Both conflicts are ``AuditFirewallError`` (exit 2). Manifest fields
    override trial fields, except ``protocol`` and ``sap``, which are true
    when either source says so. ``manifest_cohort`` is added to every row's
    cohorts.
    """
    manifest_by_nct: dict[str, dict[str, Any]] = {}
    for row in manifest_rows:
        nct_id = _extract_nct_id(row, "--cohort-manifest")
        if nct_id in manifest_by_nct:
            raise ol.OutcomesLayoutError("--cohort-manifest: duplicate nct_id")
        if type(row.get("in_audit")) is not bool:
            raise ol.AuditFirewallError(
                f"--cohort-manifest: in_audit must be an explicit bool on every row, got "
                f"{type(row.get('in_audit')).__name__}"
            )
        if any(value is not row["in_audit"] for key, value in row.items() if _is_holdout_flag_key(key)):
            raise ol.AuditFirewallError(
                "--cohort-manifest: a row carries a second holdout flag (a spelling of in_audit such as "
                "inAudit or is_in_audit) that is not the same bool as its in_audit; the row does not say "
                "which it means and is refused"
            )
        manifest_by_nct[nct_id] = dict(row)
    if not manifest_by_nct:
        raise ol.OutcomesLayoutError("--cohort-manifest lists no trials")

    trials_by_nct: dict[str, dict[str, Any]] = {}
    outside = 0
    for row in trials_rows:
        nct_id = _extract_nct_id(row, "--trials")
        if nct_id in trials_by_nct:
            raise ol.OutcomesLayoutError("--trials: duplicate nct_id")
        if nct_id not in manifest_by_nct:
            outside += 1
            continue
        trials_by_nct[nct_id] = dict(row)
    if outside:
        raise ol.OutcomesLayoutError(
            f"--trials: {outside} trial(s) absent from --cohort-manifest; the manifest is the "
            f"allowlist, and a trial outside it is refused whatever it says about itself"
        )

    merged: dict[str, dict[str, Any]] = {}
    for nct_id in sorted(manifest_by_nct):
        overlay = manifest_by_nct[nct_id]
        base = trials_by_nct.get(nct_id, {})
        if any(value is not overlay["in_audit"] for key, value in base.items() if _is_holdout_flag_key(key)):
            raise ol.AuditFirewallError(
                "conflicting holdout flags between --trials and --cohort-manifest for one trial: the "
                "--trials record carries in_audit (under some spelling) that is not the manifest's bool"
            )
        combined = {**base, **overlay}
        for flag in ("protocol", "sap"):
            if flag in base and flag in overlay:
                b_val = ol._require_strict_bool(base[flag], f"trials.{flag}")
                o_val = ol._require_strict_bool(overlay[flag], f"cohort_manifest.{flag}")
                combined[flag] = b_val or o_val
        if manifest_cohort is not None:
            own = combined.get("cohorts")
            if own is not None and (isinstance(own, (str, bytes)) or not isinstance(own, Sequence)):
                raise ol.OutcomesLayoutError(
                    f"cohorts must be a list of slugs, got {type(own).__name__}"
                )
            combined["cohorts"] = sorted(set(own or []) | {manifest_cohort})
        combined["nct_id"] = nct_id
        merged[nct_id] = combined
    return merged


def _check_side_stream_membership(
    streams: Mapping[str, Mapping[str, Any]],
    build_ids: set[str],
) -> None:
    """Refuse side-stream records for trials outside the build (L5/L6).

    Before, a record for a trial the build does not contain was looked up by
    no one and dropped without a word; now it is an input error.
    """
    for name, by_nct in streams.items():
        outside = sum(1 for nct in by_nct if nct not in build_ids)
        if outside:
            raise ol.OutcomesLayoutError(
                f"{name}: {outside} trial(s) absent from --cohort-manifest; a record for a trial "
                f"outside the build is refused, not dropped"
            )


def _build_parser() -> argparse.ArgumentParser:
    """The command-line parser (see the module docstring for the contract)."""
    ap = argparse.ArgumentParser(
        description=(
            "Build or validate a cohort-agnostic sharded outcomes & provenance tree. "
            "The cohort manifest is required for a build and is the allowlist of trials."
        )
    )
    once: dict[str, Any] = {"action": _StoreOnce, "default": None}
    ap.add_argument("--trials", help="JSON/JSONL (.gz ok) of CT.gov studies or trial rows; each must be in the manifest", **once)
    ap.add_argument(
        "--cohort-manifest",
        help="REQUIRED for a build: JSON/JSONL (.gz ok) of manifest rows, each with a bool in_audit "
        "(the live outcomes index {\"rows\": [...]} is accepted as is)",
        **once,
    )
    ap.add_argument(
        "--manifest-cohort",
        metavar="SLUG",
        help="cohort slug every manifest row belongs to (e.g. industry_outcomes_beta for the live "
        "outcomes index, whose rows carry no cohort, sponsor class or phase)",
        **once,
    )
    ap.add_argument("--documents", help="JSON/JSONL (.gz ok) of Module 1 profiles or legacy documents.jsonl", **once)
    ap.add_argument(
        "--locations",
        action="append",
        default=None,
        help="JSON/JSONL (.gz ok) of decision-rule location rows; repeatable (rows are appended, a "
        "duplicate cell or object is refused)",
    )
    ap.add_argument("--provenance", help="JSON/JSONL (.gz ok) of Module 4 longitudinal provenance records", **once)
    ap.add_argument("--population-parsed", help="JSON/JSONL (.gz ok) of Module 2 parsed population records", **once)
    ap.add_argument("--extracted-at", help="ISO-8601 UTC timestamp stamp (overrides input container)", **once)
    ap.add_argument("--now-utc", help="Fallback UTC ISO-8601 timestamp when --extracted-at is omitted", **once)
    ap.add_argument(
        "--source-pipeline-commit",
        "--pipeline-commit",
        dest="source_pipeline_commit",
        help="Git commit SHA stamp (7..40 lowercase hex); default: this engine checkout's HEAD "
        "(git -C <engine root>); a build refuses when neither resolves",
        **once,
    )
    ap.add_argument(
        "--allow-null-commit",
        action="store_true",
        help="With --check only: accept a tree whose source_pipeline_commit is null",
    )
    ap.add_argument(
        "--shards",
        type=int,
        action=_StoreOnce,
        default=ol.DEFAULT_OUTCOMES_SHARDS,
        help=f"Number of detail shards (1..{ol.MAX_SHARDS}; default %(default)s)",
    )
    ap.add_argument(
        "--out-dir",
        default=DEFAULT_OUT_DIR,
        action=_StoreOnce,
        help="Target outcomes bundles directory: absent, empty, or a bundle this builder wrote "
        "(default %(default)s, resolved from this file)",
    )
    ap.add_argument("--report", help="Optional JSON path for the build/check report (same allowlist as --out-dir)", **once)
    ap.add_argument("--check", metavar="DIR", help="Validate an existing outcomes tree without writing", **once)
    return ap


def _fail(message: str) -> NoReturn:
    """Raise an input error (exit 1)."""
    raise ol.OutcomesLayoutError(message)


def _run_check(args: argparse.Namespace) -> int:
    """``--check DIR``: validate a tree on disk, optionally writing a report."""
    if args.report is not None:
        ol.check_report_path(args.report, out_dir=args.check)
    result = ol.validate_outcomes_tree(
        args.check,
        allow_null_commit=args.allow_null_commit,
    )
    if args.report is not None:
        ol.write_report(args.report, result.to_dict(), out_dir=args.check)
    if result.ok:
        b = result.budget
        size_info = f"{b.total_bytes:,} B across {result.shards} shards" if b else f"{result.shards} shards"
        print(
            f"outcomes tree check: ok ({result.total_trials} trials, "
            f"{result.audit_holdout_trials} holdout, {size_info})"
        )
        return 0
    for problem in result.problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    return 2 if result.audit_leakage_detected else 1


def _run_build(args: argparse.Namespace) -> int:
    """Build a tree from the manifest and side streams, then write it."""
    if args.cohort_manifest is None:
        _fail("--cohort-manifest is required for a build: it is the allowlist of trials and the authority on in_audit")
    if args.allow_null_commit:
        _fail("--allow-null-commit applies to --check only; a build always stamps a resolved commit")
    if args.manifest_cohort is not None and not ol.COHORT_RE.fullmatch(args.manifest_cohort):
        _fail(f"--manifest-cohort {args.manifest_cohort!r} is not a cohort slug ({ol.COHORT_RE.pattern})")

    # Refuse a bad output location or report path before reading any input.
    ol.check_output_target(args.out_dir)
    if args.report is not None:
        ol.check_report_path(args.report, out_dir=args.out_dir)

    commit = ol.resolve_pipeline_commit(args.source_pipeline_commit, repo_root=ENGINE_ROOT)
    if commit is None:
        _fail(
            f"source_pipeline_commit unresolvable: git -C {ENGINE_ROOT} rev-parse HEAD gave no commit of "
            f"this engine checkout; pass --pipeline-commit <sha> (nothing was written)"
        )

    trials_rows: list[dict[str, Any]] = []
    trials_meta: dict[str, Any] = {}
    if args.trials is not None:
        trials_rows, trials_meta = _load_records_file(args.trials)
    manifest_rows, manifest_meta = _load_records_file(args.cohort_manifest)
    merged = _merge_trials_and_manifest(trials_rows, manifest_rows, manifest_cohort=args.manifest_cohort)
    audit_nct_ids = frozenset(nct for nct, row in merged.items() if row["in_audit"] is True)
    try:
        return _build_and_write(args, merged, audit_nct_ids, commit, trials_meta, manifest_meta)
    except ol.OutcomesLayoutError as exc:
        # Every refusal from here on, not only firewall refusals, has its
        # NCT-like tokens withheld when it names a holdout trial in any
        # reading (red-team m3 round 1: "NCT...enrollment must be an int"
        # printed a holdout's ID).
        message = str(exc)
        shown = ol.withhold_if_names(message, audit_nct_ids)
        if shown == message:
            raise
        raise type(exc)(shown) from None


def _build_and_write(
    args: argparse.Namespace,
    merged: Mapping[str, dict[str, Any]],
    audit_nct_ids: frozenset[str],
    commit: str,
    trials_meta: Mapping[str, Any],
    manifest_meta: Mapping[str, Any],
) -> int:
    """Read the side streams, build every trial's bundle and write the tree.

    The vector-6 cross-stream firewall (every key, and every record read
    against its key with ``ol.check_side_input_identity``) runs before any
    bundle is built or ``--out-dir`` is touched; each bundle is built with
    the build's holdout set, so a trial record or side input naming a holdout
    anywhere is refused.
    """
    docs_by_nct = _load_documents_stream(args.documents) if args.documents else {}
    locs_by_nct = _load_locations_streams(args.locations) if args.locations else {}
    prov_by_nct = _load_provenance_stream(args.provenance) if args.provenance else {}
    pop_by_nct = _load_population_parsed_stream(args.population_parsed) if args.population_parsed else {}

    # Vector 6 hard cross-stream firewall BEFORE building or touching --out-dir.
    ol.assert_no_audit_cross_stream_leakage(
        audit_nct_ids,
        documents_by_nct=docs_by_nct,
        locations_by_nct=locs_by_nct,
        provenance_by_nct=prov_by_nct,
        population_parsed_by_nct=pop_by_nct,
    )
    _check_side_stream_membership(
        {
            "--documents": docs_by_nct,
            "--locations": locs_by_nct,
            "--provenance": prov_by_nct,
            "--population-parsed": pop_by_nct,
        },
        set(merged),
    )

    extracted_at = (
        args.extracted_at
        or trials_meta.get("extracted_at")
        or manifest_meta.get("extracted_at")
        or args.now_utc
        or datetime.now(timezone.utc).isoformat()
    )

    component_versions: dict[str, str | None] = {
        "outcomes_layout": f"v{ol.LAYOUT_VERSION}",
    }
    if isinstance(manifest_meta.get("run_id"), str):
        component_versions["cohort_manifest"] = manifest_meta["run_id"]
    for prof in docs_by_nct.values():
        if isinstance(prof, Mapping) and isinstance(prof.get("indexer_rules_version"), str):
            component_versions["document_indexer"] = prof["indexer_rules_version"]
            break
    for prov in prov_by_nct.values():
        if isinstance(prov, Mapping) and isinstance(prov.get("provenance_rules_version"), str):
            component_versions["longitudinal_provenance"] = prov["provenance_rules_version"]
            break
    for pop in pop_by_nct.values():
        if isinstance(pop, Mapping) and isinstance(pop.get("parser_rules_version"), str):
            component_versions["analysis_population_parser"] = pop["parser_rules_version"]
            break

    bundles: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for nct_id in sorted(merged):
        bundles.append(
            ol.build_trial_bundle(
                merged[nct_id],
                shards=args.shards,
                in_audit=merged[nct_id]["in_audit"],
                document_profile=docs_by_nct.get(nct_id),
                decision_rule_locations=locs_by_nct.get(nct_id),
                longitudinal_provenance=prov_by_nct.get(nct_id),
                parsed_populations=pop_by_nct.get(nct_id),
                audit_nct_ids=audit_nct_ids,
            )
        )

    index_doc, shard_docs = ol.build_outcomes_documents(
        bundles,
        extracted_at=str(extracted_at),
        source_pipeline_commit=commit,
        shards=args.shards,
        component_versions=component_versions,
    )

    result = ol.write_outcomes_tree(args.out_dir, index_doc, shard_docs)
    if args.report is not None:
        ol.write_report(args.report, result.to_dict(), out_dir=args.out_dir)
    for warning in result.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    b = result.budget
    size_note = f"{b.total_bytes:,} B" if b else "0 B"
    print(
        f"wrote {result.total_trials} trials ({result.audit_holdout_trials} holdout) "
        f"to {args.out_dir} across {result.shards} shards ({size_note})"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the exit code (see EXITS in the module docstring)."""
    ap = _build_parser()
    args = ap.parse_args(list(argv) if argv is not None else None)
    try:
        if args.check is not None:
            return _run_check(args)
        return _run_build(args)
    except ol.AuditFirewallError as exc:
        print(f"AUDIT FIREWALL BLOCKED: {ol.withhold_nct_ids(str(exc))}", file=sys.stderr)
        return 2
    except ol.OutcomesLayoutError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
