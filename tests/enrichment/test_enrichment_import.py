"""The approved-export importer (src/enrichment_import.py): the engine's end of
the durable promotion path. Every input is fictional.

Pinned here: the fictional export validates and joins under a distinct
namespace without touching a canonical field; repeated import is idempotent;
every refusal the module documents refuses (malformed and duplicate ids,
unsupported schema, method and taxonomy versions, absent provenance,
incomplete review, an unreleased or merely SUPPORTED candidate, a private
field, a null value with the wrong state, a conflicting pair, a stale
snapshot); revocation and supersession are explicit and leave history; an
unassessed label is never read as negative; a lead never overwrites
canonical conditions; an unknown NCT is quarantined, not applied.
"""
import copy
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from src import enrichment_import as ei  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "approved_export_fictional.json")
SCHEMA = os.path.join(ROOT, "docs", "sex_gender_analysis", "schema", "approved_enrichment_export-1.0.schema.json")


def export():
    with open(FIX) as fh:
        return json.load(fh)


def records():
    return [
        {"nct_id": "NCT99000010", "conditions": ["Fictional asthma"], "primary_condition": "Respiratory",
         "secondary_condition": "Asthma", "condition_classifications": [{"condition": "Fictional asthma", "primary": "Respiratory", "secondary": "Asthma"}],
         "sex_gender": {"sex_report_status": "reported"}},
        {"nct_id": "NCT99000001", "conditions": ["Fictional condition"], "primary_condition": "Other",
         "secondary_condition": "Uncategorized", "condition_classifications": []},
        {"nct_id": "NCT99000002", "conditions": [], "primary_condition": "Other", "secondary_condition": "Uncategorized",
         "condition_classifications": []},
    ]


def test_fixture_export_is_valid_and_matches_the_schema():
    assert ei.validate_export(export()) == []
    jsonschema = pytest.importorskip("jsonschema")
    with open(SCHEMA) as fh:
        jsonschema.validate(export(), json.load(fh))


def test_apply_joins_under_the_namespace_and_touches_no_canonical_field():
    recs = records()
    before = copy.deepcopy(recs)
    report = ei.apply(recs, export(), "2026-10-28")
    assert report["applied"] == 3 and report["quarantined"] == [] and report["superseded"] == 0
    for b, a in zip(before, recs):
        assert {k: v for k, v in a.items() if k != "enrichment"} == b
    ns = recs[0]["enrichment"]
    assert ns["schema_version"] == ei.NAMESPACE_SCHEMA and ns["exports"] == ["fictional-export-0001"]
    acc = ns["fields"]["conditions.accepted_membership"]
    assert [l["status"] for l in acc["value"]["labels"]] == ["accepted", "accepted", "unassessed"]
    assert acc["method"]["native_task"].startswith("fictional-task-1.0")
    leads = ns["fields"]["conditions.review_leads"]["value"]["labels"]
    assert {l["lead_kind"] for l in leads} == {"addition", "removal"}
    # canonical conditions are what they were, whatever the leads propose
    assert recs[0]["conditions"] == ["Fictional asthma"] and recs[0]["primary_condition"] == "Respiratory"
    # a not_assessed value is carried as not assessed, with its reason, never as a value
    na = recs[1]["enrichment"]["fields"]["conditions.accepted_membership"]
    assert na["value"] is None and na["value_state"] == "not_assessed" and na["state_reason"]
    # a study the export does not name has no namespace
    assert "enrichment" not in recs[2]


def test_repeated_import_is_idempotent():
    recs = records()
    ei.apply(recs, export(), "2026-10-28")
    once = copy.deepcopy(recs)
    report = ei.apply(recs, export(), "2026-10-28")
    assert recs == once
    assert report["applied"] == 0 and report["unchanged"] == 3


def _refused(mutate, match):
    doc = export()
    mutate(doc)
    errors = ei.validate_export(doc)
    assert any(match in e for e in errors), (match, errors)
    with pytest.raises(ei.ImportError_):
        ei.apply(records(), doc, "2026-10-28")


def test_every_documented_refusal_refuses():
    def set_(path, value):
        def f(doc):
            d = doc
            for k in path[:-1]:
                d = d[k]
            d[path[-1]] = value
        return f
    cases = [
        (set_(("format",), "civicsample-approved-enrichment-export-0.9"), "format"),
        (set_(("records", 0, "schema_version"), "approved_enrichment_record/0.9"), "schema_version"),
        (set_(("allowlist_version",), "public-allowlist/0.1"), "allowlist_version"),
        (set_(("records", 0, "nct_id"), "NCT1234"), "malformed"),
        (set_(("records", 0, "nct_id"), "fictional_NCT99000010.pdf"), "malformed"),
        (set_(("records", 0, "field"), "primary_condition"), "allowlist"),
        (set_(("records", 0, "method", "kind"), "human_guess"), "method.kind"),
        (set_(("records", 0, "method", "model_id"), None), "method.model_id"),
        (set_(("records", 0, "method", "native_task"), None), "native_task"),
        (set_(("records", 0, "value", "taxonomy_version"), "civicsample-condition-catalog-20/0.9"), "taxonomy_version"),
        (set_(("records", 0, "value", "labels", 0, "id"), "AREA_FICTIONAL"), "unknown label id"),
        (set_(("records", 0, "source", "sha256"), "abc"), "sha256"),
        (set_(("records", 0, "evidence", "pointer"), ""), "evidence.pointer"),
        (set_(("records", 0, "decision_ref"), ""), "decision_ref"),
        (set_(("records", 0, "review_status"), "screen_supported"), "review_status"),
        (set_(("records", 0, "validation_status"), "pending"), "validation_status"),
        (set_(("records", 0, "release_status"), "accepted"), "release_status"),
        (set_(("records", 0, "chronology", "released_at"), "2026-10-26T00:00:00+00:00"), "out of order"),
        (set_(("records", 0, "linkage", "status"), "candidate"), "linkage"),
        (set_(("records", 0, "linkage", "method"), "filename_first_nct"), "linkage"),
        (set_(("records", 0, "value_state"), "missing"), "a value beside value_state"),
        (set_(("records", 2, "value_state"), "present"), "no value"),
        (set_(("records", 0, "reviewer_name"), "Dr Fictional"), "private field"),
        (set_(("records", 0, "evidence", "pointer"), "https://fictional-private.example.org/packet/1"), "URL outside"),
        (set_(("release_decision", "release_status"), "pending"), "release_decision"),
        (set_(("applies_to", "registry_snapshot_date"), "soon"), "applies_to"),
        (set_(("records", 1, "value", "labels", 0, "status"), "accepted"), "always status proposed"),
        (set_(("records", 0, "value", "labels", 0, "status"), "proposed"), "not allowed on conditions.accepted_membership"),
    ]
    for mutate, match in cases:
        _refused(mutate, match)


def test_duplicate_and_conflicting_records_are_refused():
    def dup(doc):
        doc["records"].append(copy.deepcopy(doc["records"][0]))
    _refused(dup, "duplicate (record_id, revision)")

    def conflict(doc):
        other = copy.deepcopy(doc["records"][0])
        other["record_id"] = "fict-rec-0009"
        doc["records"].append(other)
    _refused(conflict, "conflict")

    def dup_label(doc):
        doc["records"][0]["value"]["labels"].append({"id": "ROW_AST", "status": "rejected"})
    _refused(dup_label, "listed twice")


def test_stale_snapshot_needs_an_explicit_historical_decision():
    doc = export()
    with pytest.raises(ei.ImportError_, match="historical compatibility decision"):
        ei.apply(records(), doc, "2026-10-11")
    recs = records()
    report = ei.apply(recs, doc, "2026-10-11", historical_decision_ref="fictional-historical-decision-0001")
    assert report["applied"] == 3 and report["historical_decision_ref"] == "fictional-historical-decision-0001"


def test_unknown_nct_is_quarantined_not_applied():
    doc = export()
    doc["records"][0]["nct_id"] = "NCT99000050"
    doc["records"][1]["nct_id"] = "NCT99000050"
    recs = records()
    report = ei.apply(recs, doc, "2026-10-28")
    assert report["applied"] == 1
    assert report["quarantined"] == [{"nct_id": "NCT99000050", "reason": "not in the target records",
                                      "record_ids": ["fict-rec-0001", "fict-rec-0002"]}]
    assert "enrichment" not in recs[0]


def test_supersession_and_revocation_are_explicit_and_keep_history():
    recs = records()
    ei.apply(recs, export(), "2026-10-28")
    # revision 2 of the same record supersedes revision 1
    doc = export()
    doc["export_id"] = "fictional-export-0002"
    r = copy.deepcopy(doc["records"][0])
    r["revision"] = 2
    r["value"]["labels"][2]["status"] = "rejected"
    doc["records"] = [r]
    report = ei.apply(recs, doc, "2026-10-28")
    assert report["superseded"] == 1
    ns = recs[0]["enrichment"]
    assert ns["fields"]["conditions.accepted_membership"]["revision"] == 2
    assert ns["history"][0]["revision"] == 1 and ns["history"][0]["superseded_by"] == {"record_id": "fict-rec-0001", "revision": 2}
    assert ns["exports"] == ["fictional-export-0001", "fictional-export-0002"]
    # a different record that does not supersede the live one is quarantined
    doc["export_id"] = "fictional-export-0003"
    other = copy.deepcopy(r)
    other["record_id"] = "fict-rec-0010"
    other["revision"] = 1
    doc["records"] = [other]
    report = ei.apply(recs, doc, "2026-10-28")
    assert report["applied"] == 0 and report["quarantined"][0]["reason"].startswith("conditions.accepted_membership already carries")
    # and one that does supersede it replaces it
    other["supersedes"] = "fict-rec-0001"
    doc["export_id"] = "fictional-export-0004"
    assert ei.apply(recs, doc, "2026-10-28")["superseded"] == 1
    assert ns["fields"]["conditions.accepted_membership"]["record_id"] == "fict-rec-0010"
    # revocation removes the field and records why; nothing canonical moves
    doc["export_id"] = "fictional-export-0005"
    rev = copy.deepcopy(other)
    rev["revision"] = 2
    rev["release_status"] = "revoked"
    rev["revocation_ref"] = "fictional-revocation-0001"
    rev["revoked_at"] = "2026-10-29T00:00:00+00:00"
    rev["value"] = None
    rev["value_state"] = "missing"
    rev["state_reason"] = "release revoked after review"
    doc["records"] = [rev]
    report = ei.apply(recs, doc, "2026-10-28")
    assert report["revoked"] == 1
    f = ns["fields"]["conditions.accepted_membership"]
    assert f["value"] is None and f["value_state"] == "revoked" and f["revocation_ref"] == "fictional-revocation-0001"
    assert recs[0]["conditions"] == ["Fictional asthma"]
    # applying the revocation again changes nothing
    assert ei.apply(recs, doc, "2026-10-28")["unchanged"] == 1
    # a revocation without its reference is refused
    bad = copy.deepcopy(doc)
    del bad["records"][0]["revocation_ref"]
    assert any("revocation_ref" in e for e in ei.validate_export(bad))


def test_cli_writes_a_new_file_and_a_report(tmp_path):
    import subprocess
    demo = tmp_path / "demo.json"
    json.dump({"extracted_at": "2026-10-28T06:00:00+00:00", "pipeline_commit": "abc1234", "data": records()}, open(demo, "w"))
    out, rep = tmp_path / "out.json", tmp_path / "report.json"
    res = subprocess.run([sys.executable, os.path.join(ROOT, "src", "enrichment_import.py"), "--export", FIX,
                          "--demographics", str(demo), "--snapshot-date", "2026-10-28", "--out", str(out),
                          "--report", str(rep)], cwd=ROOT, capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
    assert json.load(open(out))["data"][0]["enrichment"]["exports"] == ["fictional-export-0001"]
    assert json.load(open(rep))["applied"] == 3
    assert json.load(open(demo))["data"][0].get("enrichment") is None     # never in place
    res = subprocess.run([sys.executable, os.path.join(ROOT, "src", "enrichment_import.py"), "--export", FIX,
                          "--demographics", str(demo), "--snapshot-date", "2026-10-11", "--out", str(out)],
                         cwd=ROOT, capture_output=True, text=True)
    assert res.returncode == 1 and "historical compatibility decision" in res.stdout


# ------------------------------------------------------------------ cross-repo
def test_private_tool_export_is_accepted_and_joins():
    """An export written by the private repository's promotion tooling
    (scripts/enrichment-promotion, fictional fixtures, release attested) passes
    the importer, the JSON Schema, and joins with its run coverage intact."""
    import jsonschema
    with open(os.path.join(os.path.dirname(FIX), "approved_export_from_private_tool_fictional.json")) as fh:
        doc = json.load(fh)
    assert ei.validate_export(doc) == []
    with open(SCHEMA) as fh:
        schema = json.load(fh)
    jsonschema.validate(doc, schema)
    assert doc["coverage"]["fictional-c-run-1"]["coverage"] == "complete"
    records = [{"nct_id": "NCT99000010", "conditions": ["fictional asthma"]},
               {"nct_id": "NCT99000011", "conditions": ["fictional diabetes"]}]
    report = ei.apply(records, doc, "2026-10-28")
    assert report["applied"] == 3 and report["quarantined"] == []
    ns = records[0]["enrichment"]
    assert ns["fields"]["conditions.accepted_membership"]["method"]["run_coverage"] == "complete"
    assert [l["id"] for l in ns["fields"]["conditions.accepted_membership"]["value"]["labels"]] == ["AREA_RESPIRATORY", "ROW_AST"]
    lead = records[1]["enrichment"]["fields"]["conditions.review_leads"]["value"]["labels"][0]
    assert lead == {"id": "ROW_DIAB", "status": "proposed", "lead_kind": "removal",
                    "native_task": "civicsample-ai-label-pilot/1.0", "native_decision": "NOT_SUPPORTED"}
    assert records[1]["conditions"] == ["fictional diabetes"]
    # The same export again changes nothing.
    again = ei.apply(records, doc, "2026-10-28")
    assert again["applied"] == 0 and again["unchanged"] == 3


def test_revocation_of_an_older_revision_does_not_remove_the_newer_value():
    recs = records()
    ei.apply(recs, export(), "2026-10-28")
    doc = export()
    doc["export_id"] = "fictional-export-0002"
    r3 = copy.deepcopy(doc["records"][0]); r3["revision"] = 3
    doc["records"] = [r3]
    assert ei.apply(recs, doc, "2026-10-28")["superseded"] == 1
    doc["export_id"] = "fictional-export-0003"
    rev2 = copy.deepcopy(doc["records"][0]); rev2["revision"] = 2
    rev2["release_status"] = "revoked"; rev2["revocation_ref"] = "fictional-revocation-0002"
    rev2["revoked_at"] = "2026-10-29T00:00:00+00:00"; rev2["value"] = None; rev2["value_state"] = "not_assessed"
    rev2["state_reason"] = "revoked"
    doc["records"] = [rev2]
    report = ei.apply(recs, doc, "2026-10-28")
    assert report["revoked"] == 0 and report["quarantined"][0]["reason"].startswith("revocation of an older revision")
    f = recs[0]["enrichment"]["fields"]["conditions.accepted_membership"]
    assert f["revision"] == 3 and f["value_state"] == "present"


def test_chronology_is_compared_as_instants():
    doc = export()
    r = doc["records"][0]
    # review at 10:00+02:00 is 08:00Z, before validation at 09:00Z: out of order despite the string order
    r["chronology"] = {"validated_at": "2026-10-27T09:00:00+00:00", "reviewed_at": "2026-10-27T10:00:00+02:00",
                       "released_at": "2026-10-28T11:00:00+00:00"}
    assert any("out of order" in e for e in ei.validate_export(doc))
    # review at 08:30-01:00 is 09:30Z, after validation: in order despite the string order
    r["chronology"]["reviewed_at"] = "2026-10-27T08:30:00-01:00"
    assert not any("chronology" in e for e in ei.validate_export(doc))
    # a naive timestamp carries no instant and is refused
    r["chronology"]["reviewed_at"] = "2026-10-27T15:00:00"
    assert any("chronology needs" in e for e in ei.validate_export(doc))


def test_supersession_chain_in_one_export_applies_in_dependency_order():
    recs = records()
    ei.apply(recs, export(), "2026-10-28")
    doc = export()
    doc["export_id"] = "fictional-export-0002"
    base = doc["records"][0]
    middle = copy.deepcopy(base); middle["record_id"] = "z-middle"; middle["revision"] = 1; middle["supersedes"] = base["record_id"]
    newest = copy.deepcopy(base); newest["record_id"] = "a-newest"; newest["revision"] = 1; newest["supersedes"] = "z-middle"
    newest["value"]["labels"][2]["status"] = "rejected"
    doc["records"] = [newest, middle]
    assert ei.validate_export(doc) == []
    report = ei.apply(recs, doc, "2026-10-28")
    f = recs[0]["enrichment"]["fields"]["conditions.accepted_membership"]
    assert f["record_id"] == "a-newest", report
    assert report["quarantined"] == [] and report["superseded"] == 2
    again = ei.apply(recs, doc, "2026-10-28")
    assert again["applied"] == 0 and again["superseded"] == 0 and again["quarantined"] == []
    assert recs[0]["enrichment"]["fields"]["conditions.accepted_membership"]["record_id"] == "a-newest"


def test_supersession_cycle_is_refused_and_chain_order_is_irrelevant():
    base = export()["records"][0]
    def rec(rid, supersedes):
        r = copy.deepcopy(base); r["record_id"] = rid; r["revision"] = 1; r["supersedes"] = supersedes
        return r
    # a cycle is refused before anything is applied
    doc = export(); doc["records"] = [rec("original", "middle"), rec("middle", "original")]
    errs = ei.validate_export(doc)
    assert any("supersession cycle" in e for e in errs), errs
    recs = records()
    with pytest.raises(ei.ImportError_):
        ei.apply(recs, doc, "2026-10-28")
    assert "enrichment" not in recs[0]
    # a chain validates and applies to its tip whatever the array order
    for order in (["newest", "middle", "original"], ["original", "middle", "newest"], ["middle", "newest", "original"]):
        by = {"original": rec("original", None), "middle": rec("middle", "original"), "newest": rec("newest", "middle")}
        doc = export(); doc["records"] = [by[k] for k in order]
        assert ei.validate_export(doc) == [], order
        recs = records()
        ei.apply(recs, doc, "2026-10-28")
        assert recs[0]["enrichment"]["fields"]["conditions.accepted_membership"]["record_id"] == "newest", order
        again = ei.apply(recs, doc, "2026-10-28")
        assert again["applied"] == 0 and again["superseded"] == 0 and again["quarantined"] == [], order
    # two tips that nothing links are still a conflict
    doc = export(); doc["records"] = [rec("left", None), rec("right", None)]
    assert any("conflict" in e for e in ei.validate_export(doc))
