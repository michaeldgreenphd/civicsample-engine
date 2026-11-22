#!/usr/bin/env python3
"""Build or validate a cohort-agnostic sharded outcomes & provenance tree.

READS   --trials           JSON/JSONL (plain or .gz) of CT.gov study records or
                           manifest trial dicts (optional if --cohort-manifest
                           supplies the full trial list).
        --cohort-manifest  optional JSON/JSONL supplying per-trial in_audit,
                           cohorts, ecosystem_category, protocol, and sap flags.
        --documents        optional JSON/JSONL of Module 1 TrialDocumentProfile
                           records or legacy documents.jsonl rows.
        --locations        optional JSON/JSONL of decision-rule locations rows
                           (locations.jsonl) or pre-grouped location objects.
        --provenance       optional JSON/JSONL of Module 4
                           LongitudinalProvenanceRecord objects.
        --population-parsed optional JSON/JSONL of Module 2
                           TrialAnalysisPopulations or ParsedOutcomePopulation
                           outputs.
WRITES  --out-dir (default data/outcomes_bundles):
          index.json          multi-cohort trial catalog, sorted by nct_id
          shards/0.json.gz    per-trial unified shard bundles (n = int(nct[3:]) % N)
            .. {N-1}.json.gz  gzip level 9, mtime=0, OS byte 0xff
        Staged in <out-dir>.partial, verified with
        src.outcomes_layout.validate_outcomes_tree before swapping into
        --out-dir, and cleaned up in a finally block. Refuses to overwrite
        foreign data directories (e.g. data/outcomes/ or data/results_structure/).
CHECKS  --check <dir> validates an existing outcomes directory on disk without
        modifying it.
EXITS   0 on success; 2 on any AuditFirewallError (in_audit holdout violation,
        aborted BEFORE touching --out-dir); 1 on any other contract, input, or
        validation error.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import outcomes_layout as ol  # noqa: E402

DEFAULT_OUT_DIR = os.path.join("data", "outcomes_bundles")


def _open_text_maybe_gz(path: Path) -> Any:
    """Open a plain UTF-8 or ``.gz`` text file for reading."""
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def _load_records_file(path: str | os.PathLike[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load JSON or JSONL (plain or ``.gz``) into ``(records, container_meta)``.

    Accepts:
    - JSONL / ``.jsonl.gz`` (one JSON object per non-empty line)
    - JSON array of objects
    - JSON container object with ``data``, ``trials``, ``records``, or ``rows``
      list (including live ``outcomes_beta`` ``index.json``; preserving
      top-level ``extracted_at`` / ``pipeline_commit`` / ``source_pipeline_commit``)
    - JSON mapping keyed by ``nct_id`` -> object
    """
    p = Path(path)
    if not p.is_file():
        raise ol.OutcomesLayoutError(f"{p}: input file not found")

    with _open_text_maybe_gz(p) as fh:
        raw_text = fh.read()

    stripped = raw_text.strip()
    if not stripped:
        return [], {}

    # Try full JSON document first; fall back to JSONL line-by-line parsing.
    try:
        parsed = json.loads(stripped)
    except ValueError:
        rows: list[dict[str, Any]] = []
        for lineno, line in enumerate(raw_text.splitlines(), start=1):
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
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
            for k in ("extracted_at", "pipeline_commit", "source_pipeline_commit", "indexer_rules_version")
            if k in parsed
        }
        for list_key in ("data", "trials", "records", "rows"):
            if isinstance(parsed.get(list_key), list):
                items = parsed[list_key]
                if not all(isinstance(x, dict) for x in items):
                    raise ol.OutcomesLayoutError(f"{p}.{list_key}: must contain only objects")
                return items, meta
        if all(isinstance(k, str) and ol.NCT_RE.fullmatch(k) and isinstance(v, dict) for k, v in parsed.items()):
            out_rows: list[dict[str, Any]] = []
            for nct_id, val in parsed.items():
                entry = dict(val)
                entry.setdefault("nct_id", nct_id)
                out_rows.append(entry)
            return out_rows, {}
        return [parsed], {}

    raise ol.OutcomesLayoutError(f"{p}: unsupported JSON root type {type(parsed).__name__}")


def _extract_nct_id(obj: Mapping[str, Any], source_label: str) -> str:
    """Resolve an NCT ID from a flat record or CT.gov protocolSection."""
    nct = obj.get("nct_id")
    if nct is None and isinstance(obj.get("protocolSection"), Mapping):
        ident = obj["protocolSection"].get("identificationModule")
        if isinstance(ident, Mapping):
            nct = ident.get("nctId")
    if not isinstance(nct, str) or not ol.NCT_RE.fullmatch(nct):
        raise ol.OutcomesLayoutError(f"{source_label}: record carries invalid or missing nct_id {nct!r}")
    return nct


def _load_documents_stream(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load ``--documents`` as either Module 1 profiles or legacy ``documents.jsonl`` rows."""
    rows, _ = _load_records_file(path)
    grouped: dict[str, Any] = {}
    for row in rows:
        nct_id = _extract_nct_id(row, str(path))
        if "documents" in row and isinstance(row["documents"], list):
            if nct_id in grouped:
                raise ol.OutcomesLayoutError(f"{path}: duplicate TrialDocumentProfile for {nct_id}")
            grouped[nct_id] = row
        else:
            existing = grouped.get(nct_id)
            if isinstance(existing, dict):
                raise ol.OutcomesLayoutError(
                    f"{path}: mixed TrialDocumentProfile and flat document rows for {nct_id}"
                )
            grouped.setdefault(nct_id, []).append(row)
    return grouped


def _load_locations_stream(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load ``--locations`` as flat ``locations.jsonl`` rows or pre-grouped dicts."""
    rows, _ = _load_records_file(path)
    grouped: dict[str, Any] = {}
    for row in rows:
        nct_id = _extract_nct_id(row, str(path))
        if "primary_pass" in row and isinstance(row["primary_pass"], list):
            if nct_id in grouped:
                raise ol.OutcomesLayoutError(f"{path}: duplicate decision_rule_locations object for {nct_id}")
            grouped[nct_id] = row
        else:
            existing = grouped.get(nct_id)
            if isinstance(existing, dict):
                raise ol.OutcomesLayoutError(
                    f"{path}: mixed grouped and flat location rows for {nct_id}"
                )
            grouped.setdefault(nct_id, []).append(row)
    return grouped


def _load_provenance_stream(path: str | os.PathLike[str]) -> dict[str, dict[str, Any]]:
    """Load ``--provenance`` records keyed by ``nct_id``."""
    rows, _ = _load_records_file(path)
    by_nct: dict[str, dict[str, Any]] = {}
    for row in rows:
        nct_id = _extract_nct_id(row, str(path))
        if nct_id in by_nct:
            raise ol.OutcomesLayoutError(f"{path}: duplicate longitudinal_provenance record for {nct_id}")
        by_nct[nct_id] = row
    return by_nct


def _load_population_parsed_stream(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load ``--population-parsed`` Module 2 outputs keyed by ``nct_id``."""
    rows, _ = _load_records_file(path)
    by_nct: dict[str, Any] = {}
    for row in rows:
        nct_id = _extract_nct_id(row, str(path))
        if "outcomes" in row and isinstance(row["outcomes"], list):
            if nct_id in by_nct:
                raise ol.OutcomesLayoutError(f"{path}: duplicate population-parsed record for {nct_id}")
            by_nct[nct_id] = row
        else:
            by_nct.setdefault(nct_id, []).append(row)
    return by_nct


def _merge_trials_and_manifest(
    trials_rows: Sequence[Mapping[str, Any]],
    manifest_rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Merge ``--trials`` and ``--cohort-manifest`` keyed by ``nct_id``."""
    trials_by_nct: dict[str, dict[str, Any]] = {}
    for row in trials_rows:
        nct_id = _extract_nct_id(row, "--trials")
        if nct_id in trials_by_nct:
            raise ol.OutcomesLayoutError(f"--trials: duplicate nct_id {nct_id}")
        trials_by_nct[nct_id] = dict(row)

    manifest_by_nct: dict[str, dict[str, Any]] = {}
    for row in manifest_rows:
        nct_id = _extract_nct_id(row, "--cohort-manifest")
        if nct_id in manifest_by_nct:
            raise ol.OutcomesLayoutError(f"--cohort-manifest: duplicate nct_id {nct_id}")
        manifest_by_nct[nct_id] = dict(row)

    if not trials_by_nct and not manifest_by_nct:
        raise ol.OutcomesLayoutError("no trials supplied (pass --trials and/or --cohort-manifest)")

    merged: dict[str, dict[str, Any]] = {}
    all_ids = sorted(set(trials_by_nct) | set(manifest_by_nct))
    for nct_id in all_ids:
        base = dict(trials_by_nct.get(nct_id, {}))
        overlay = manifest_by_nct.get(nct_id, {})
        # If both specify in_audit and disagree, refuse rather than silently picking one.
        if "in_audit" in base and "in_audit" in overlay and base["in_audit"] != overlay["in_audit"]:
            raise ol.AuditFirewallError(
                f"{nct_id}: conflicting in_audit values between --trials ({base['in_audit']!r}) "
                f"and --cohort-manifest ({overlay['in_audit']!r})"
            )
        combined = {**base, **overlay}
        for flag in ("protocol", "sap"):
            if flag in base and flag in overlay:
                b_val = ol._require_strict_bool(base[flag], f"{nct_id}.trials.{flag}")
                o_val = ol._require_strict_bool(overlay[flag], f"{nct_id}.cohort_manifest.{flag}")
                combined[flag] = b_val or o_val
        combined["nct_id"] = nct_id
        merged[nct_id] = combined
    return merged


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Build or validate a cohort-agnostic sharded outcomes & provenance tree."
    )
    ap.add_argument("--trials", default=None, help="JSON/JSONL (.gz ok) of CT.gov studies or trial rows")
    ap.add_argument("--cohort-manifest", default=None, help="JSON/JSONL (.gz ok) of manifest rows (in_audit, cohorts, etc.)")
    ap.add_argument("--documents", default=None, help="JSON/JSONL (.gz ok) of Module 1 profiles or legacy documents.jsonl")
    ap.add_argument("--locations", default=None, help="JSON/JSONL (.gz ok) of decision-rule locations rows")
    ap.add_argument("--provenance", default=None, help="JSON/JSONL (.gz ok) of Module 4 longitudinal provenance records")
    ap.add_argument("--population-parsed", default=None, help="JSON/JSONL (.gz ok) of Module 2 parsed population records")
    ap.add_argument("--extracted-at", default=None, help="ISO-8601 UTC timestamp stamp (overrides input container)")
    ap.add_argument("--now-utc", default=None, help="Fallback UTC ISO-8601 timestamp when --extracted-at is omitted")
    ap.add_argument(
        "--source-pipeline-commit",
        "--pipeline-commit",
        dest="source_pipeline_commit",
        default=None,
        help="Git commit SHA stamp (7..40 lowercase hex); falls back to GITHUB_SHA or git rev-parse HEAD",
    )
    ap.add_argument(
        "--allow-null-commit",
        action="store_true",
        help="Allow null source_pipeline_commit during --check or build validation",
    )
    ap.add_argument(
        "--require-commit",
        action="store_true",
        help="Abort build if no source_pipeline_commit can be resolved",
    )
    ap.add_argument(
        "--shards",
        type=int,
        default=ol.DEFAULT_OUTCOMES_SHARDS,
        help=f"Number of detail shards (1..{ol.MAX_SHARDS}; default %(default)s)",
    )
    ap.add_argument(
        "--out-dir",
        default=DEFAULT_OUT_DIR,
        help="Target outcomes bundles directory (default %(default)s)",
    )
    ap.add_argument("--report", default=None, help="Optional JSON path to write build/check report")
    ap.add_argument("--check", default=None, metavar="DIR", help="Validate an existing outcomes tree without writing")
    return ap


def _write_report(path: str | None, payload: Mapping[str, Any]) -> None:
    if path is None:
        return
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.check is not None:
        result = ol.validate_outcomes_tree(
            args.check,
            allow_null_commit=args.allow_null_commit,
        )
        _write_report(args.report, result.to_dict())
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

    try:
        trials_rows: list[dict[str, Any]] = []
        trials_meta: dict[str, Any] = {}
        manifest_rows: list[dict[str, Any]] = []
        manifest_meta: dict[str, Any] = {}

        if args.trials is not None:
            trials_rows, trials_meta = _load_records_file(args.trials)
        if args.cohort_manifest is not None:
            manifest_rows, manifest_meta = _load_records_file(args.cohort_manifest)

        merged = _merge_trials_and_manifest(trials_rows, manifest_rows)

        # Vector 1 check on every trial + collect audit holdout set upfront.
        audit_nct_ids: set[str] = set()
        for nct_id, row in merged.items():
            in_audit = row.get("in_audit")
            if type(in_audit) is not bool:
                raise ol.AuditFirewallError(
                    f"{nct_id}.in_audit must be an explicit bool (True or False), "
                    f"got {type(in_audit).__name__}: {in_audit!r}"
                )
            if in_audit:
                audit_nct_ids.add(nct_id)

        docs_by_nct = _load_documents_stream(args.documents) if args.documents else {}
        locs_by_nct = _load_locations_stream(args.locations) if args.locations else {}
        prov_by_nct = _load_provenance_stream(args.provenance) if args.provenance else {}
        pop_by_nct = _load_population_parsed_stream(args.population_parsed) if args.population_parsed else {}

        # Vector 6 hard cross-stream firewall BEFORE building or touching --out-dir.
        ol.assert_no_audit_cross_stream_leakage(
            audit_nct_ids,
            documents_by_nct=docs_by_nct,
            locations_by_nct=locs_by_nct,
            provenance_by_nct=prov_by_nct,
        )

        # Resolve timestamps and commit stamp gracefully (never fabricating a fake SHA).
        extracted_at = (
            args.extracted_at
            or trials_meta.get("extracted_at")
            or manifest_meta.get("extracted_at")
            or args.now_utc
            or datetime.now(timezone.utc).isoformat()
        )
        explicit_commit = (
            args.source_pipeline_commit
            or trials_meta.get("source_pipeline_commit")
            or trials_meta.get("pipeline_commit")
            or manifest_meta.get("source_pipeline_commit")
            or manifest_meta.get("pipeline_commit")
        )
        commit = ol.resolve_pipeline_commit(explicit_commit)
        effective_allow_null = bool(args.allow_null_commit)
        if commit is None:
            if args.require_commit:
                raise ol.OutcomesLayoutError(
                    "source_pipeline_commit unavailable (git HEAD not accessible); "
                    "pass --pipeline-commit <sha>"
                )
            effective_allow_null = True
            print(
                "WARNING: git commit unavailable; writing source_pipeline_commit=null "
                "(pass --pipeline-commit <sha> for strict provenance validation)",
                file=sys.stderr,
            )

        component_versions: dict[str, str | None] = {
            "outcomes_layout": f"v{ol.LAYOUT_VERSION}",
        }
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
            row = merged[nct_id]
            bundle = ol.build_trial_bundle(
                row,
                shards=args.shards,
                document_profile=docs_by_nct.get(nct_id),
                decision_rule_locations=locs_by_nct.get(nct_id),
                longitudinal_provenance=prov_by_nct.get(nct_id),
                parsed_populations=pop_by_nct.get(nct_id),
            )
            bundles.append(bundle)

        index_doc, shard_docs = ol.build_outcomes_documents(
            bundles,
            extracted_at=str(extracted_at),
            source_pipeline_commit=commit,
            shards=args.shards,
            component_versions=component_versions,
            allow_null_commit=effective_allow_null,
        )

        result = ol.write_outcomes_tree(
            args.out_dir,
            index_doc,
            shard_docs,
            allow_null_commit=effective_allow_null,
        )
        _write_report(args.report, result.to_dict())
        b = result.budget
        size_note = f"{b.total_bytes:,} B" if b else "0 B"
        print(
            f"✓ wrote {result.total_trials} trials ({result.audit_holdout_trials} holdout) "
            f"to {args.out_dir} across {result.shards} shards ({size_note})"
        )
        return 0
    except ol.AuditFirewallError as exc:
        print(f"AUDIT FIREWALL BLOCKED: {exc}", file=sys.stderr)
        return 2
    except ol.OutcomesLayoutError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
