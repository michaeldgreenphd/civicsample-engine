"""enrichment_import.py: the approved-export importer, the engine's end of the
durable promotion path for per-study enrichment.

    retained source -> validated candidate -> durable review decision
    -> approved export (this file's input) -> engine NCT join (this module)
    -> published study record (the "enrichment" namespace) -> frontend

READS   an approved export (format civicsample-approved-enrichment-export-1.0,
        docs/sex_gender_analysis/schema/approved_enrichment_export-1.0.schema.json)
        produced by the private repository's promotion tooling, and the full
        study records it is to be joined to (src/full_records.py).
WRITES  the records with each matched study's "enrichment" key set; nothing
        else on a record changes (apply() checks that and refuses otherwise).
        Canonical registry fields (conditions, primary_condition,
        condition_classifications, sex_gender, ...) are never overwritten or
        filled: approved evidence is attached beside them, keyed by field,
        with its provenance, so a manuscript value and a registry value can
        both stand for different populations or timepoints.

WHAT THE IMPORTER REFUSES (validate_export; nothing is applied when any error
is found)
  - an unknown format, record schema or allowlist version; a field outside the
    allowlist; an unsupported method kind, taxonomy version or label id;
  - a malformed or missing NCT id; a record whose trial linkage is not
    validated (a filename's first NCT, a model-proposed NCT or a temporal
    match is a candidate, never proof);
  - absent provenance (source identity and sha256, evidence pointer, method
    versions, model id and run id for AI methods, decision reference and
    chronology);
  - a record that is not validated, accepted AND released: a model's
    SUPPORTED, an audit match or an evidence quotation is none of those;
  - a value with the wrong state (a value beside value_state missing, or no
    value beside present); an unassessed label is never read as negative;
  - a duplicate (record_id, revision), or two released records for the same
    study and field that do not supersede one another (a conflict);
  - a private field anywhere in the document: reviewer identities, response
    bodies, clinical review records, secrets, URLs outside the public hosts;
  - an export whose registry snapshot is not the target records' snapshot,
    unless the caller passes an explicit historical-compatibility decision.

Repeated import is idempotent: applying the same export twice gives the same
records. A revocation (release_status "revoked", with a revocation reference)
removes the field it revokes and records why; a later revision supersedes an
earlier one and the earlier is kept in the record's history, never applied.
Quarantined records (an NCT id not in the target records, a stale source) are
reported, never applied.
"""
from __future__ import annotations

import gzip
import json
import re
from datetime import datetime
from typing import Any, Iterable, Optional

EXPORT_FORMAT = "civicsample-approved-enrichment-export-1.0"
RECORD_SCHEMA = "approved_enrichment_record/1.0"
ALLOWLIST_VERSION = "public-allowlist/1.0"
NAMESPACE_SCHEMA = "enrichment/1.0"

# The fields an export may carry, and the value type each takes. Nothing else
# is accepted: a new field is a reviewed change to this list and its schema.
FIELD_ALLOWLIST: dict = {
    "conditions.proposed_membership": "membership",
    "conditions.accepted_membership": "membership",
    "conditions.review_leads": "leads",
}
METHOD_KINDS: tuple = ("registry", "deterministic_rule", "ai_extraction", "ai_classification")
SOURCE_KINDS: tuple = ("registry_snapshot", "document")
VALUE_STATES: tuple = ("present", "missing", "error", "not_assessed")
LINKAGE_METHODS: tuple = ("registry_nct", "document_declared_nct_verified", "manual_verified")
LABEL_STATUSES: tuple = ("accepted", "proposed", "rejected", "unassessed")
LEAD_KINDS: tuple = ("removal", "addition", "confirmation")
# Statuses a field's labels may carry, by field: an accepted-membership field
# never carries a "proposed" label; an unassessed one stays unassessed.
LABEL_STATUSES_BY_FIELD: dict = {
    "conditions.proposed_membership": ("proposed", "unassessed"),
    "conditions.accepted_membership": ("accepted", "rejected", "unassessed"),
}
# The condition taxonomies this engine knows the label ids of. The 20-label
# catalog is the private experiment's (ten areas, ten rows); its ids are the
# exchange vocabulary, its clinical definitions stay private.
SUPPORTED_TAXONOMIES: dict = {
    "civicsample-condition-catalog-20/1.0": [
        "AREA_ONCOLOGY", "AREA_CARDIOVASCULAR", "AREA_MENTAL_HEALTH", "AREA_INFECTIOUS", "AREA_METABOLIC",
        "AREA_NEUROLOGICAL", "AREA_RESPIRATORY", "AREA_PATHOLOGICAL", "AREA_IMMUNE", "AREA_UROGENITAL",
        "ROW_CAN_ALL", "ROW_DIAB", "ROW_DEP", "ROW_OBE", "ROW_AST", "ROW_COPD", "ROW_COV", "ROW_SCZ",
        "ROW_HTN", "ROW_PAIN",
    ],
}
# Key names that never belong in a public export, whatever their value.
PRIVATE_KEY_RX = re.compile(r"reviewer|adjudicat|clinician|physician|response_body|raw_response|packet|"
                            r"api_key|token|secret|password|supabase|email|identity_of", re.IGNORECASE)
PUBLIC_URL_HOSTS: tuple = ("https://clinicaltrials.gov/", "https://pubmed.ncbi.nlm.nih.gov/",
                           "https://doi.org/", "https://github.com/michaeldgreenphd/civicsample-engine/")
NCT_RX = re.compile(r"NCT\d{8}")
SHA_RX = re.compile(r"[0-9a-f]{64}")
HEX7_RX = re.compile(r"[0-9a-f]{7,40}")
DATE_RX = re.compile(r"\d{4}-\d{2}-\d{2}")


class ImportError_(ValueError):
    """The export cannot be applied; the message lists every reason."""


def _instant(v: Any) -> Optional[datetime]:
    """An ISO-8601 timestamp WITH a UTC offset, parsed; None otherwise. A naive
    timestamp is refused: without an offset two decisions cannot be ordered."""
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def _iso(v: Any) -> bool:
    return _instant(v) is not None


def _scan_private(obj: Any, path: str, out: list[str]) -> None:
    """Every key that looks private and every URL outside the public hosts."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if PRIVATE_KEY_RX.search(str(k)):
                out.append(f"{path}.{k}: a private field")
            _scan_private(v, f"{path}.{k}", out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _scan_private(v, f"{path}[{i}]", out)
    elif isinstance(obj, str) and "://" in obj:
        if not any(obj.startswith(h) for h in PUBLIC_URL_HOSTS):
            out.append(f"{path}: a URL outside the public hosts")


def _validate_value(field: str, record: dict, where: str, errors: list[str]) -> None:
    state = record.get("value_state")
    value = record.get("value")
    if state not in VALUE_STATES:
        errors.append(f"{where}: value_state {state!r} is not one of {VALUE_STATES}")
        return
    if state != "present":
        if value is not None:
            errors.append(f"{where}: a value beside value_state {state!r}")
        if not record.get("state_reason"):
            errors.append(f"{where}: value_state {state!r} needs a state_reason")
        return
    if value is None:
        errors.append(f"{where}: value_state present with no value")
        return
    kind = FIELD_ALLOWLIST[field]
    if kind in ("membership", "leads"):
        if not isinstance(value, dict):
            errors.append(f"{where}: value is not an object")
            return
        tax = value.get("taxonomy_version")
        if tax not in SUPPORTED_TAXONOMIES:
            errors.append(f"{where}: taxonomy_version {tax!r} is not supported")
            return
        ids = SUPPORTED_TAXONOMIES[tax]
        labels = value.get("labels")
        if not isinstance(labels, list) or not labels:
            errors.append(f"{where}: labels is not a non-empty list")
            return
        seen: set[str] = set()
        allowed = LABEL_STATUSES_BY_FIELD.get(field, LABEL_STATUSES)
        for i, lab in enumerate(labels):
            if not isinstance(lab, dict) or lab.get("id") not in ids:
                errors.append(f"{where}.labels[{i}]: unknown label id {lab.get('id') if isinstance(lab, dict) else lab!r}")
                continue
            if lab["id"] in seen:
                errors.append(f"{where}.labels[{i}]: label {lab['id']} listed twice")
            seen.add(lab["id"])
            if kind == "membership" and lab.get("status") not in allowed:
                errors.append(f"{where}.labels[{i}]: status {lab.get('status')!r} is not allowed on {field} ({allowed})")
            if kind == "leads":
                if lab.get("lead_kind") not in LEAD_KINDS:
                    errors.append(f"{where}.labels[{i}]: lead_kind {lab.get('lead_kind')!r} is not one of {LEAD_KINDS}")
                if lab.get("status") != "proposed":
                    errors.append(f"{where}.labels[{i}]: a review lead is always status proposed, not {lab.get('status')!r}")
                if not lab.get("native_task") or not lab.get("native_decision"):
                    errors.append(f"{where}.labels[{i}]: a lead names its native task and native decision")


def validate_export(doc: Any) -> list[str]:
    """Every reason the export cannot be applied; [] when it can."""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["the export is not a JSON object"]
    if doc.get("format") != EXPORT_FORMAT:
        errors.append(f"format {doc.get('format')!r} is not {EXPORT_FORMAT}")
    if doc.get("allowlist_version") != ALLOWLIST_VERSION:
        errors.append(f"allowlist_version {doc.get('allowlist_version')!r} is not {ALLOWLIST_VERSION}")
    for k in ("export_id", "source_repository"):
        if not isinstance(doc.get(k), str) or not doc.get(k):
            errors.append(f"{k} is missing")
    if not _iso(doc.get("exported_at")):
        errors.append("exported_at is not a timestamp")
    if not isinstance(doc.get("source_commit"), str) or not HEX7_RX.fullmatch(doc.get("source_commit", "")):
        errors.append("source_commit is not a commit id")
    rd = doc.get("release_decision")
    if not isinstance(rd, dict) or not rd.get("decision_id") or not _iso(rd.get("decided_at")) \
            or rd.get("release_status") != "released":
        errors.append("release_decision must carry decision_id, decided_at and release_status released")
    applies = doc.get("applies_to")
    if not isinstance(applies, dict) or not DATE_RX.fullmatch(str(applies.get("registry_snapshot_date", ""))):
        errors.append("applies_to.registry_snapshot_date (YYYY-MM-DD) is missing")
    private: list[str] = []
    _scan_private(doc, "export", private)
    errors.extend(private)
    records = doc.get("records")
    if not isinstance(records, list):
        errors.append("records is not a list")
        return errors
    seen_rev: set[tuple] = set()
    by_id: dict = {}
    for i, r in enumerate(records):
        where = f"records[{i}]"
        if not isinstance(r, dict):
            errors.append(f"{where}: not an object")
            continue
        rid, rev = r.get("record_id"), r.get("revision")
        if not isinstance(rid, str) or not rid:
            errors.append(f"{where}: record_id is missing")
        if type(rev) is not int or rev < 1:
            errors.append(f"{where}: revision is not a positive integer")
        if (rid, rev) in seen_rev:
            errors.append(f"{where}: duplicate (record_id, revision) ({rid}, {rev})")
        seen_rev.add((rid, rev))
        by_id.setdefault(rid, []).append(r)
        if r.get("schema_version") != RECORD_SCHEMA:
            errors.append(f"{where}: schema_version {r.get('schema_version')!r} is not {RECORD_SCHEMA}")
        nct = r.get("nct_id")
        if not isinstance(nct, str) or not NCT_RX.fullmatch(nct):
            errors.append(f"{where}: nct_id {nct!r} is malformed")
        field = r.get("field")
        if field not in FIELD_ALLOWLIST:
            errors.append(f"{where}: field {field!r} is not in the allowlist")
            continue
        src = r.get("source")
        if not isinstance(src, dict) or src.get("kind") not in SOURCE_KINDS or not src.get("identity") \
                or not SHA_RX.fullmatch(str(src.get("sha256", ""))):
            errors.append(f"{where}: source needs kind, identity and sha256")
        elif src.get("kind") == "registry_snapshot" and not DATE_RX.fullmatch(str(src.get("snapshot_date", ""))):
            errors.append(f"{where}: a registry_snapshot source names its snapshot_date")
        ev = r.get("evidence")
        if not isinstance(ev, dict) or not ev.get("pointer"):
            errors.append(f"{where}: evidence.pointer is missing")
        else:
            for k in ("population", "timepoint"):
                if k not in ev:
                    errors.append(f"{where}: evidence.{k} is missing (null is allowed, absence is not)")
        me = r.get("method")
        if not isinstance(me, dict) or me.get("kind") not in METHOD_KINDS:
            errors.append(f"{where}: method.kind is not one of {METHOD_KINDS}")
        else:
            if me["kind"].startswith("ai_"):
                for k in ("model_id", "run_id"):
                    if not me.get(k):
                        errors.append(f"{where}: method.{k} is required for {me['kind']}")
                if not (me.get("prompt_version") or me.get("guide_version")):
                    errors.append(f"{where}: method.prompt_version or guide_version is required for {me['kind']}")
                if not me.get("native_task"):
                    errors.append(f"{where}: method.native_task (the experiment and its vocabulary) is required")
            if me["kind"] == "deterministic_rule" and not me.get("rule_version"):
                errors.append(f"{where}: method.rule_version is required for deterministic_rule")
            if FIELD_ALLOWLIST[field] in ("membership", "leads") and not me.get("taxonomy_version"):
                errors.append(f"{where}: method.taxonomy_version is required for {field}")
        _validate_value(field, r, where, errors)
        rs = r.get("release_status")
        if rs == "revoked":
            if not r.get("revocation_ref") or not _iso(r.get("revoked_at")):
                errors.append(f"{where}: a revoked record carries revocation_ref and revoked_at")
        elif rs != "released":
            errors.append(f"{where}: release_status {rs!r} is not released (or revoked)")
        if r.get("validation_status") != "validated":
            errors.append(f"{where}: validation_status {r.get('validation_status')!r} is not validated")
        if r.get("review_status") != "accepted":
            errors.append(f"{where}: review_status {r.get('review_status')!r} is not accepted")
        chron = r.get("chronology")
        if not isinstance(chron, dict) or not all(_iso(chron.get(k)) for k in ("validated_at", "reviewed_at", "released_at")):
            errors.append(f"{where}: chronology needs validated_at, reviewed_at and released_at timestamps")
        elif not (_instant(chron["validated_at"]) <= _instant(chron["reviewed_at"]) <= _instant(chron["released_at"])):
            errors.append(f"{where}: chronology is out of order (validated <= reviewed <= released, as instants)")
        if not r.get("decision_ref"):
            errors.append(f"{where}: decision_ref is missing")
        if "supersedes" not in r:
            errors.append(f"{where}: supersedes is missing (null when nothing is superseded)")
        link = r.get("linkage")
        if not isinstance(link, dict) or link.get("status") != "validated" or link.get("method") not in LINKAGE_METHODS \
                or not link.get("evidence"):
            errors.append(f"{where}: linkage must be validated by one of {LINKAGE_METHODS} with evidence")
    # Conflicts: for one study and field, the heads (latest revision per
    # record id) must form one supersession chain. Supersession is followed
    # across the whole chain, whatever order the records sit in the array: a
    # cycle (records superseding each other) is refused, and more than one
    # tip (a head no other head supersedes) is a conflict.
    heads: dict = {rid: max(rs, key=lambda r: r.get("revision") or 0) for rid, rs in by_id.items()}
    groups: dict = {}
    for rid, head in heads.items():
        groups.setdefault((head.get("nct_id"), head.get("field")), []).append(rid)
    for (nct, field), rids in groups.items():
        in_group = set(rids)
        superseded_here = set()
        for rid in rids:
            prev = heads[rid].get("supersedes")
            if prev in in_group:
                superseded_here.add(prev)
            # follow the chain from this head; a revisit is a cycle
            seen = [rid]
            cur = prev
            while cur in in_group:
                if cur in seen:
                    errors.append(f"supersession cycle among {', '.join(sorted(seen))} for {field} of {nct}")
                    break
                seen.append(cur)
                cur = heads[cur].get("supersedes")
        tips = sorted(r for r in rids if r not in superseded_here)
        if len(tips) > 1:
            errors.append(f"conflict: records {' and '.join(tips)} both release {field} for {nct} "
                          "and neither supersedes the other")
    return errors


def _public_record(r: dict) -> dict:
    """What of an export record reaches the published study record."""
    return {
        "record_id": r["record_id"], "revision": r["revision"], "schema_version": r["schema_version"],
        "value": r.get("value"), "value_state": r["value_state"], "state_reason": r.get("state_reason"),
        "source": r["source"], "evidence": r["evidence"], "method": r["method"],
        "validation_status": r["validation_status"], "review_status": r["review_status"],
        "release_status": r["release_status"], "decision_ref": r["decision_ref"], "chronology": r["chronology"],
        "supersedes": r.get("supersedes"), "linkage": r["linkage"],
    }


def _supersession_order(heads: dict) -> list:
    """The heads of one export for one study, a record after the one it
    supersedes when both are in the export, so a chain (original -> middle ->
    newest) installs in one pass whatever the record ids sort to. Ties keep
    record-id order; validate_export refuses a cycle before apply reaches
    this, and the depth bound is a guard, never an ordering."""
    def depth(r: dict, seen: int = 0) -> int:
        prev = heads.get(r.get("supersedes"))
        if prev is None or seen >= len(heads):
            return 0
        return 1 + depth(prev, seen + 1)
    return sorted(heads.values(), key=lambda r: (depth(r), r["record_id"]))


def apply(records: list[dict], export: dict, target_snapshot_date: Optional[str],
          historical_decision_ref: Optional[str] = None) -> dict:
    """Join the export onto the full records in place. Returns a report.

    target_snapshot_date  the records' snapshot date (data/run.json). The
                          export's applies_to.registry_snapshot_date must be
                          the same, or historical_decision_ref must name the
                          explicit compatibility decision that lets today's
                          enrichment be joined to another snapshot.
    Raises ImportError_ when validate_export finds anything, or when the
    snapshot does not match without a decision. Applies nothing then."""
    errors = validate_export(export)
    if errors:
        raise ImportError_("export refused:\n  " + "\n  ".join(errors))
    applies = export["applies_to"]["registry_snapshot_date"]
    if target_snapshot_date != applies and not historical_decision_ref:
        raise ImportError_(f"export refused: it applies to registry snapshot {applies}, the target records are "
                           f"{target_snapshot_date!r}; a historical compatibility decision is required to join them")
    by_nct: dict = {}
    for r in sorted(export["records"], key=lambda r: (r["record_id"], r["revision"])):
        by_nct.setdefault(r["nct_id"], []).append(r)
    report: dict = {"export_id": export["export_id"], "applied": 0, "revoked": 0, "superseded": 0,
                    "quarantined": [], "unchanged": 0, "historical_decision_ref": historical_decision_ref}
    index = {rec.get("nct_id"): rec for rec in records if isinstance(rec, dict)}
    for nct, items in by_nct.items():
        rec = index.get(nct)
        if rec is None:
            report["quarantined"].append({"nct_id": nct, "reason": "not in the target records",
                                          "record_ids": [r["record_id"] for r in items]})
            continue
        before = {k: v for k, v in rec.items() if k != "enrichment"}
        ns = rec.get("enrichment")
        if not isinstance(ns, dict) or ns.get("schema_version") != NAMESPACE_SCHEMA:
            ns = {"schema_version": NAMESPACE_SCHEMA, "exports": [], "fields": {}, "history": []}
        ns.setdefault("history", [])
        if export["export_id"] not in ns["exports"]:
            ns["exports"].append(export["export_id"])
        heads: dict = {}
        for r in items:
            cur = heads.get(r["record_id"])
            if cur is None or r["revision"] > cur["revision"]:
                heads[r["record_id"]] = r
        for r in _supersession_order(heads):
            field = r["field"]
            existing = ns["fields"].get(field)
            pub = _public_record(r)
            if r["release_status"] == "revoked":
                if existing and existing.get("record_id") == r["record_id"] and existing.get("value_state") == "revoked":
                    if r["revision"] > (existing.get("revision") or 0):
                        # A later revocation advances the tombstone, so an older
                        # release replayed afterwards cannot come back as newer.
                        ns["history"].append({**existing, "superseded_by": {"record_id": r["record_id"],
                                                                            "revision": r["revision"]}})
                        ns["fields"][field] = {**existing, "revision": r["revision"],
                                               "state_reason": r.get("state_reason") or "release revoked",
                                               "revocation_ref": r["revocation_ref"], "revoked_at": r["revoked_at"]}
                        report["revoked"] += 1
                    else:
                        report["unchanged"] += 1
                elif existing and existing.get("record_id") == r["record_id"] \
                        and r["revision"] < (existing.get("revision") or 0):
                    # A revocation of an older revision than the one installed
                    # (exports replayed out of order): the newer accepted value stands.
                    report["quarantined"].append({"nct_id": nct, "reason": "revocation of an older revision than the installed one",
                                                  "record_ids": [r["record_id"]],
                                                  "installed_revision": existing.get("revision"), "revoked_revision": r["revision"]})
                elif existing and existing.get("record_id") == r["record_id"]:
                    ns["history"].append({**existing, "removed_by": {"revocation_ref": r["revocation_ref"],
                                                                     "revoked_at": r["revoked_at"]}})
                    ns["fields"][field] = {"record_id": r["record_id"], "revision": r["revision"],
                                           "value": None, "value_state": "revoked",
                                           "state_reason": r.get("state_reason") or "release revoked",
                                           "revocation_ref": r["revocation_ref"], "revoked_at": r["revoked_at"],
                                           "schema_version": r["schema_version"]}
                    report["revoked"] += 1
                else:
                    report["quarantined"].append({"nct_id": nct, "reason": "revocation of a record not applied here",
                                                  "record_ids": [r["record_id"]]})
                continue
            if existing is None:
                ns["fields"][field] = pub
                report["applied"] += 1
            elif existing.get("record_id") == r["record_id"]:
                if existing.get("revision") == r["revision"]:
                    report["unchanged"] += 1
                elif r["revision"] > existing.get("revision", 0):
                    ns["history"].append({**existing, "superseded_by": {"record_id": r["record_id"],
                                                                        "revision": r["revision"]}})
                    ns["fields"][field] = pub
                    report["superseded"] += 1
                else:
                    report["unchanged"] += 1
            elif r.get("supersedes") == existing.get("record_id"):
                ns["history"].append({**existing, "superseded_by": {"record_id": r["record_id"],
                                                                    "revision": r["revision"]}})
                ns["fields"][field] = pub
                report["superseded"] += 1
            elif any(h.get("record_id") == r["record_id"] and h.get("revision") == r["revision"]
                     and h.get("superseded_by") for h in ns["history"]):
                # Already applied and since superseded (a replayed export whose
                # chain this study has walked to its end): nothing to do.
                report["unchanged"] += 1
            else:
                report["quarantined"].append({"nct_id": nct, "reason": f"{field} already carries {existing.get('record_id')}, "
                                                                        "which this record does not supersede",
                                              "record_ids": [r["record_id"]]})
        rec["enrichment"] = ns
        after = {k: v for k, v in rec.items() if k != "enrichment"}
        if before != after:
            raise ImportError_(f"{nct}: a canonical field changed during import; nothing is written")
    return report


def load_export(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src import full_records
    ap = argparse.ArgumentParser(description="Join an approved enrichment export onto the full study records.")
    ap.add_argument("--export", required=True)
    ap.add_argument("--demographics", default=full_records.DEFAULT_PATH)
    ap.add_argument("--snapshot-date", required=True, help="the records' snapshot date (data/run.json)")
    ap.add_argument("--out", required=True, help="where to write the joined records (never in place)")
    ap.add_argument("--historical-decision-ref", default=None,
                    help="the explicit compatibility decision when the export's snapshot is not the records'")
    ap.add_argument("--report", default=None)
    a = ap.parse_args(argv)
    records, extracted_at, commit = full_records.load(a.demographics)
    export = load_export(a.export)
    try:
        report = apply(records, export, a.snapshot_date, a.historical_decision_ref)
    except ImportError_ as e:
        print(f"::error::{e}")
        return 1
    # The same suffix rule full_records.load reads by: a .gz path is gzipped,
    # so the joined records load back in the next step.
    opener = gzip.open if str(a.out).endswith(".gz") else open
    with opener(a.out, "wt", encoding="utf-8") as fh:
        json.dump({"extracted_at": extracted_at, "pipeline_commit": commit, "data": records}, fh, indent=2)
    if a.report:
        with open(a.report, "w") as fh:
            json.dump(report, fh, indent=2)
    print(f"enrichment import: applied {report['applied']}, superseded {report['superseded']}, "
          f"revoked {report['revoked']}, unchanged {report['unchanged']}, quarantined {len(report['quarantined'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
