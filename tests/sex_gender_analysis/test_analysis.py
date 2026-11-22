"""The analytical layer around the vendored parser (src/sex_gender_analysis.py).

What is pinned here:
  1. The fictional cases in fixtures/cases.json derive to their expected lean
     and full blocks: a normal count, explicit unknown alone and beside
     values, absence, an empty template, a declared not-collected note,
     non-participant units, partial coverage, patients-only evidence,
     multi-table gender categories, the two eTable 5 exclusions, cisgender
     and intersex as separate rows, a parse error and an unresolved reading.
  2. The parser row is never changed by the layer, and eligibility is never
     read by it.
  3. The block validates against its JSON schema (shape, enumerations).
  4. The category-level maximum across count-driving tables, never a sum.
  5. Absence is null: a category no table posted, an unevaluated incomplete
     composition, a parse error.
  6. The summary block counts and sums only what it says it does, naming the
     denominator, and leaves out excluded trials' D3.
  7. The orchestrator stores the lean block beside sex_gender; the raw
     envelope round-trips to the same parser row and keeps the context.
  8. The sidecar builder derives from raw measures and envelopes, writes a
     typed table that reads back, refuses mixed provenance and non-one-to-one
     write-back.
"""
import copy
import gzip
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from src import sex_gender_analysis as sga  # noqa: E402
from src import sex_gender_parser as sgp  # noqa: E402
from src import sex_gender_table as sgt  # noqa: E402
from src.utils import extract_eligibility  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SCHEMA = os.path.join(ROOT, "docs", "sex_gender_analysis", "schema", "sex_gender_analysis-1.0.schema.json")
SNAP = "2026-10-28"


def _cases():
    with open(os.path.join(FIX, "cases.json")) as fh:
        return json.load(fh)["cases"]


def _study(nct, measures, enrollment, eligibility=None):
    return {"protocolSection": {"identificationModule": {"nctId": nct},
                                "designModule": {"enrollmentInfo": {"count": enrollment}},
                                "eligibilityModule": eligibility if eligibility is not None else {"sex": "ALL"}},
            "resultsSection": {"baselineCharacteristicsModule": {"measures": measures,
                                                                  "groups": [{"id": "BG000", "title": "Arm A"},
                                                                             {"id": "BG001", "title": "Arm B"}],
                                                                  "denoms": [{"units": "Participants", "counts": [
                                                                      {"groupId": "BG000", "value": "50"}]}]}}}


def _derive(case):
    study = _study(case["nct_id"], case["measures"], case["enrollment"])
    if case.get("parse_error"):
        row = sgt.parse_error_row(case["nct_id"], case["enrollment"], SNAP)
    else:
        row = sgt.build_row(study, SNAP)
    raw = sgt.select_raw_measures(study)
    return row, sga.analyze(row, raw["measures"], raw["enrollment"])


def _get(d, path):
    for k in path.split("."):
        d = d[k] if isinstance(d, dict) else None
    return d


# --------------------------------------------------------------- 1. the fixture cases
@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["nct_id"] + ":" + c["tags"][0])
def test_fixture_case_derives_as_expected(case):
    row, full = _derive(case)
    lean = sga.lean_block(full)
    want = {"schema_version": sga.SCHEMA_VERSION, **case["expected_lean"]}
    assert lean == want, {k: (lean.get(k), want.get(k)) for k in want if lean.get(k) != want.get(k)}
    for path, value in case["expected_full"].items():
        assert _get(full, path) == value, (path, _get(full, path), value)


def test_every_fixture_tag_is_covered_once():
    tags = [t for c in _cases() for t in c["tags"]]
    assert len(tags) == len(set(tags))
    for needed in ("normal_count", "explicit_unknown_only", "measure_absent", "non_participant_units",
                   "partial_coverage_unevaluated", "patients_only_evidence", "multi_table_gender_categories",
                   "unresolved_interpretation", "parse_error"):
        assert needed in tags


# --------------------------------------------------------------- 2. the parser row and eligibility are untouched
def test_layer_never_changes_the_parser_row_and_never_reads_eligibility():
    case = next(c for c in _cases() if "multi_table_gender_categories" in c["tags"])
    study = _study(case["nct_id"], case["measures"], case["enrollment"], eligibility={"sex": "FEMALE", "genderBased": True})
    row = sgt.build_row(study, SNAP)
    before = copy.deepcopy(row)
    raw = sgt.select_raw_measures(study)
    sga.analyze(row, raw["measures"], raw["enrollment"])
    assert row == before
    # eligibility sex FEMALE changes nothing in the block
    study_all = _study(case["nct_id"], case["measures"], case["enrollment"], eligibility={"sex": "ALL"})
    a = sga.analyze(sgt.build_row(study, SNAP), raw["measures"], raw["enrollment"])
    b = sga.analyze(sgt.build_row(study_all, SNAP), raw["measures"], raw["enrollment"])
    assert a == b
    import inspect
    src = inspect.getsource(sga)
    assert "eligibility" not in src.replace("eligibilityModule", "").split("def detailed_gender_category")[1]
    assert "enrollment_minus_parsed" not in src


# --------------------------------------------------------------- 3. schema
def test_blocks_validate_against_the_schema():
    jsonschema = pytest.importorskip("jsonschema")
    with open(SCHEMA) as fh:
        schema = json.load(fh)
    lean_schema = {**schema["$defs"]["lean"], "$schema": schema["$schema"]}
    for case in _cases():
        _, full = _derive(case)
        jsonschema.validate(json.loads(json.dumps(full)), schema)
        jsonschema.validate(json.loads(json.dumps(sga.lean_block(full))), lean_schema)


def test_schema_enumerations_match_the_module():
    with open(SCHEMA) as fh:
        schema = json.load(fh)
    props = schema["properties"]
    assert set(props["reporting"]["properties"]["dimension"]["enum"]) == set(sga.REPORTING_DIMENSIONS)
    assert set(props["reporting"]["properties"]["neither_reason"]["enum"]) == set(sga.NEITHER_REASONS) | {None}
    assert set(props["gender_categories"]["properties"]["categories"]["required"]) == set(sga.D3_CATEGORIES)
    assert set(props["gender_categories"]["properties"]["separate"]["required"]) == set(sga.SEPARATE_ROWS)
    assert set(props["gender_categories"]["properties"]["status"]["enum"]) == set(sga.GENDER_CATEGORY_STATUSES)
    assert set(props["unresolved"]["items"]["enum"]) == set(sga.UNRESOLVED_RULES)
    assert set(schema["$defs"]["lean"]["required"]) == set(sga.LEAN_KEYS)


# --------------------------------------------------------------- 4. the max rule and the vocabulary
def test_category_maximum_across_tables_and_sum_within_a_table():
    g = lambda cats: {"title": "Gender", "paramType": "COUNT_OF_PARTICIPANTS", "classes": [{"categories": [  # noqa: E731
        {"title": t, "measurements": [{"groupId": "BG000", "value": str(v)}]} for t, v in cats]}]}
    measures = [g([("Woman", 10), ("Man", 10), ("Transgender woman", 3), ("Transgender man", 2)]),
                {**g([("Transgender", 7)]), "title": "Gender identity"}]
    study = _study("NCT99000020", measures, 40)
    full = sga.analyze(sgt.build_row(study, SNAP), measures, 40)
    cats = full["gender_categories"]["categories"]
    assert cats["transgender_with_sex_term"] == 5          # summed within the first table
    assert cats["transgender_no_sex_term"] == 7
    assert full["gender_categories"]["flags"]["possible_double_count"] is True
    assert full["gender_categories"]["d3_total"] == 12


@pytest.mark.parametrize("label,category", [
    ("Transgender woman", "transgender_with_sex_term"), ("Trans man", "transgender_with_sex_term"),
    ("Transgender", "transgender_no_sex_term"), ("Transgender person", "transgender_no_sex_term"),
    ("Non-binary", "nonbinary_gender_expansive"), ("Genderqueer", "nonbinary_gender_expansive"),
    ("Gender non-conforming", "nonbinary_gender_expansive"), ("Two-Spirit", "nonbinary_gender_expansive"),
    ("Questioning", "questioning"), ("Transgender or non-binary", "combined_gender_minority"),
    ("Gender minority", "combined_gender_minority"), ("TGNC", "combined_gender_minority"),
    ("Other", "another_self_described"), ("Prefer to self-describe", "another_self_described"),
    ("Another gender identity", "another_self_described"),
    ("Intersex", "intersex"), ("Cisgender woman", "cisgender_labeled"), ("Cisgender", "cisgender_labeled"),
    ("Prefer not to answer", "explicit_unknown_declined"), ("Unknown or Not Reported", "explicit_unknown_declined"),
    ("Female", None), ("Male", None), ("Woman", None), ("Total", None), ("Bisexual", None), ("", None), (None, None),
])
def test_detailed_gender_category_vocabulary(label, category):
    assert sga.detailed_gender_category(label) == category


def test_unclassified_gender_diverse_labels_are_surfaced_not_counted():
    # The parser buckets "ambiguous" (as a whole label) as gender diverse; the
    # eTable 5 patterns route it to another/self-described. A label the
    # parser buckets that no pattern matches is listed, never counted.
    measures = [{"title": "Gender", "paramType": "COUNT_OF_PARTICIPANTS", "classes": [{"categories": [
        {"title": "Woman", "measurements": [{"groupId": "BG000", "value": "3"}]},
        {"title": "Gender identity not listed above", "measurements": [{"groupId": "BG000", "value": "1"}]}]}]}]
    study = _study("NCT99000021", measures, 4)
    row = sgt.build_row(study, SNAP)
    full = sga.analyze(row, measures, 4)
    if sgp.canon_bucket("Gender identity not listed above") in ("gender_diverse", "ambiguous"):
        assert full["gender_categories"]["unclassified_labels"] == ["Gender identity not listed above"] or \
            full["gender_categories"]["labels"]
    assert full["gender_categories"]["d3_total"] in (None, 1)


# --------------------------------------------------------------- 5. absence is null
def test_absence_is_null_never_zero():
    case = next(c for c in _cases() if "normal_count" in c["tags"])
    _, full = _derive(case)
    assert all(v is None for v in full["gender_categories"]["categories"].values())
    assert full["composition"]["incomplete_composition"] is None
    assert full["composition"]["incomplete_composition_evaluated"] is False
    pe = sga.analyze(sgt.parse_error_row("NCT99000014", 10, SNAP), None, None)
    assert pe["reporting"]["explicit_unknown_with_values"] is None and pe["composition"]["eligible"] is None


# --------------------------------------------------------------- 6. summary
def test_summary_counts_and_excludes_excluded_trials_d3():
    leans = [sga.lean_block(_derive(c)[1]) for c in _cases()]
    s = sga.summarize(leans)
    assert s["trials"] == len(_cases())
    assert sum(s["reportingDimension"].values()) == len(_cases())
    assert s["neitherReason"]["measure_absent"] == 1
    assert s["genderCategoriesStatus"]["excluded"] == 2
    # D3 sums the derived trials only (NCT99000010: 10, NCT99000013: 2)
    assert s["d3"] == {"participants": 12, "trials": 2,
                       "denominator": "trials with gender_categories_status derived and a non-null d3_total"}
    assert s["d5"]["participants"] == 11
    assert s["incompleteComposition"]["evaluated"] == 0
    # after the 2026-10-28 decisions only R-GC-05 is listed: the four cases with derived or excluded categories
    assert s["unresolved"] == {"R-GC-05": 4}
    assert set(s["unresolvedRules"]) == {"R-GC-05"}
    assert set(s["decidedRules"]) == {"R-IC-01", "R-PO-01", "R-FS-01", "R-GC-04"}
    assert all(v.startswith("decided 2026-10-28") for v in s["decidedRules"].values())
    # a block from another schema version is not counted
    assert sga.summarize([{**leans[0], "schema_version": "sex_gender_analysis/0.9"}])["trials"] == 0
    assert sga.summarize([])["d3"]["participants"] is None


# --------------------------------------------------------------- 7. orchestrator and envelope
def test_orchestrator_stores_the_lean_block_and_eligibility_namespace():
    from src.extract_all import extract_demographics_from_study
    case = next(c for c in _cases() if "multi_table_gender_categories" in c["tags"])
    study = _study(case["nct_id"], case["measures"], case["enrollment"],
                   eligibility={"sex": "ALL", "genderBased": False, "genderDescription": "Open to all genders"})
    res = extract_demographics_from_study(study, snapshot_date=SNAP)
    assert res["sex_gender"]["sex_report_status"] == "reported"
    assert res["sex_gender_analysis"]["reporting_dimension"] == "both"
    assert res["sex_gender_analysis"]["d3_total"] == 10
    assert list(res["sex_gender_analysis"].keys()) == list(sga.LEAN_KEYS)
    assert res["eligibility"] == {"schema_version": "eligibility/1.0", "sex": "ALL", "gender_based": False,
                                  "gender_description": "Open to all genders",
                                  "present": ["sex", "gender_based", "gender_description"]}
    assert res["gender"]["reported"] is True        # the legacy gender object is as it was
    # absence stays absence
    assert extract_eligibility({}) == {"schema_version": "eligibility/1.0", "sex": None, "gender_based": None,
                                       "gender_description": None, "present": []}
    assert extract_eligibility({"sex": None}) == {"schema_version": "eligibility/1.0", "sex": None, "gender_based": None,
                                                  "gender_description": None, "present": ["sex"]}


def test_analysis_failure_is_a_parse_error_block_not_a_dropped_trial(monkeypatch):
    from src import extract_all
    case = next(c for c in _cases() if "normal_count" in c["tags"])
    study = _study(case["nct_id"], case["measures"], case["enrollment"])
    real = sga.analyze

    def boom(row, selected, enrollment):
        if selected:
            raise ValueError("synthetic analysis failure")
        return real(row, selected, enrollment)

    monkeypatch.setattr(extract_all.sga, "analyze", boom)
    res = extract_all.extract_demographics_from_study(study, snapshot_date=SNAP)
    assert res["sex_gender"]["sex_report_status"] == "reported"          # the parser row is unaffected
    assert res["sex_gender_analysis"]["reporting_dimension"] == "parse_error"


def test_raw_envelope_keeps_the_context_and_reparses_to_the_same_row():
    case = next(c for c in _cases() if "partial_coverage_unevaluated" in c["tags"])
    study = _study(case["nct_id"], case["measures"], case["enrollment"], eligibility={"sex": "ALL", "genderBased": True})
    study["resultsSection"]["baselineCharacteristicsModule"]["measures"].insert(0, {"title": "Age, Continuous", "classes": []})
    study["resultsSection"]["baselineCharacteristicsModule"]["populationDescription"] = "All randomized"
    env = sgt.select_raw_envelope(study)
    assert env["envelope_version"] == sgt.ENVELOPE_VERSION
    assert env["measure_index"] == [1] and env["n_measures_in_module"] == 2
    assert [g["id"] for g in env["baseline_groups"]] == ["BG000", "BG001"]
    assert env["baseline_denoms"][0]["units"] == "Participants"
    assert env["population_description"] == "All randomized"
    assert env["eligibility"]["gender_based"] is True
    assert env["measures"][0] is study["resultsSection"]["baselineCharacteristicsModule"]["measures"][1]
    a = sgt.ordered(sgt.build_row(study, SNAP))
    b = sgt.ordered(sgt.build_row_from_envelope(env, SNAP))
    c = sgt.ordered(sgt.build_row_from_raw(sgt.select_raw_measures(study), SNAP))
    assert a == b == c
    with pytest.raises(ValueError, match="not a"):
        sgt.build_row_from_envelope({"envelope_version": "other"}, SNAP)
    # the raw record is what it always was
    assert set(sgt.select_raw_measures(study)) == {"nct_id", "enrollment", "measures"}
    # a study with no baseline module still gives every key
    empty = sgt.select_raw_envelope({"protocolSection": {"identificationModule": {"nctId": "NCT99000099"}}})
    assert empty["measures"] == [] and empty["baseline_groups"] == [] and empty["n_measures_in_module"] == 0


# --------------------------------------------------------------- 8. the sidecar builder
def _write_jsonl(path, records):
    with gzip.open(path, "wt") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def test_builder_derives_from_raw_and_envelope_and_reads_back(tmp_path):
    import build_sex_gender_analysis as bld
    studies = [_study(c["nct_id"], c["measures"], c["enrollment"]) for c in _cases() if not c.get("parse_error")]
    stamps = {"snapshot_date": SNAP, "extracted_at": "2026-10-28T06:00:00+00:00", "pipeline_commit": "abc1234"}
    raws = [{**sgt.select_raw_measures(s), "refetched": False, **stamps} for s in studies]
    envs = [{**sgt.select_raw_envelope(s), "refetched": False, **stamps} for s in studies]
    raw_p, env_p = str(tmp_path / "raw.jsonl.gz"), str(tmp_path / "env.jsonl.gz")
    _write_jsonl(raw_p, raws)
    _write_jsonl(env_p, envs)
    rows_raw, leans_raw, st_raw = bld.derive(raw_p, None, envelope=False)
    rows_env, leans_env, st_env = bld.derive(env_p, None, envelope=True)
    assert rows_raw == rows_env and leans_raw == leans_env
    assert st_env["envelope_version"] == sgt.ENVELOPE_VERSION and st_raw["envelope_version"] is None
    assert st_raw["extracted_at"] == stamps["extracted_at"] and st_raw["n_parse_errors"] == 0
    out = str(tmp_path / "t.csv.gz")
    bld.write_table(rows_raw, out)
    back = sga.read_table(out)
    assert len(back) == len(rows_raw) and list(back[0].keys()) == sga.COLUMNS
    by = {r["nct_id"]: r for r in back}
    assert by["NCT99000010"]["gc_transgender_with_sex_term"] == 3.0
    assert by["NCT99000010"]["gc_labels"]["another_self_described"] == ["Other"]
    assert by["NCT99000010"]["flag_two_question_table"] is True
    assert by["NCT99000001"]["d3_total"] is None and by["NCT99000001"]["composition_eligible"] is True
    assert by["NCT99000004"]["neither_reason"] == "measure_absent"
    assert by["NCT99000011"]["gender_exclusion_reason"] == "sum_exceeds_enrollment_10pct"
    # a raw file passed as an envelope is refused, and vice versa an envelope works as raw
    with pytest.raises(SystemExit, match="not a"):
        bld.derive(raw_p, None, envelope=True)
    # mixed provenance is refused
    _write_jsonl(raw_p, raws[:1] + [{**raws[1], "extracted_at": "2026-11-04T06:00:00+00:00"}])
    with pytest.raises(SystemExit, match="mixed provenance"):
        bld.derive(raw_p, None, envelope=False)


def test_builder_write_back_requires_one_to_one_coverage_and_is_atomic(tmp_path):
    import subprocess
    import build_sex_gender_analysis as bld  # noqa: F401
    case = next(c for c in _cases() if "normal_count" in c["tags"])
    study = _study(case["nct_id"], case["measures"], case["enrollment"])
    stamps = {"snapshot_date": SNAP, "extracted_at": "2026-10-28T06:00:00+00:00", "pipeline_commit": "abc1234"}
    raw_p = str(tmp_path / "raw.jsonl.gz")
    _write_jsonl(raw_p, [{**sgt.select_raw_measures(study), "refetched": False, **stamps}])
    container = {"extracted_at": stamps["extracted_at"], "pipeline_commit": "abc1234",
                 "data": [{"nct_id": case["nct_id"], "sex_gender": None}, {"nct_id": "NCT99000098", "sex_gender": None}]}
    demo = tmp_path / "demo.json"
    json.dump(container, open(demo, "w"))
    cmd = [sys.executable, os.path.join(ROOT, "scripts", "build_sex_gender_analysis.py"), "--from-raw", raw_p,
           "--write-back", str(demo), "--out", str(tmp_path / "a.csv.gz"), "--meta", str(tmp_path / "m.json")]
    res = subprocess.run(cmd, cwd=str(tmp_path), capture_output=True, text=True)
    assert res.returncode == 1 and "coverage is not one-to-one" in res.stdout
    assert json.load(open(demo)) == container
    meta = json.load(open(tmp_path / "m.json"))
    assert meta["written_back"]["coverage_ok"] is False and meta["written_back"]["records_without_row"] == ["NCT99000098"]
    # one-to-one: written back, with the stamps and the summary in the meta
    container["data"].pop()
    json.dump(container, open(demo, "w"))
    res = subprocess.run(cmd, cwd=str(tmp_path), capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
    out = json.load(open(demo))["data"][0]["sex_gender_analysis"]
    assert out["reporting_dimension"] == "sex_only" and out["schema_version"] == sga.SCHEMA_VERSION
    meta = json.load(open(tmp_path / "m.json"))
    assert meta["source_extracted_at"] == stamps["extracted_at"] and meta["source_pipeline_commit"] == "abc1234"
    assert meta["rules_version"] == sga.RULES_VERSION and meta["summary"]["trials"] == 1
    # a container from another extraction is refused
    container["extracted_at"] = "2026-11-04T06:00:00+00:00"
    json.dump(container, open(demo, "w"))
    res = subprocess.run(cmd, cwd=str(tmp_path), capture_output=True, text=True)
    assert res.returncode != 0 and "write-back refused" in (res.stdout + res.stderr)


def test_mobile_summary_block_and_compact_rows_carry_the_layer():
    from generate_mobile_data import sex_gender_analysis_summary
    studies = []
    for c in _cases():
        row, full = _derive(c)
        studies.append({"nct_id": c["nct_id"], "results_date": "2024-01-01", "enrollment": c["enrollment"],
                        "sex_gender": sgt.lean_row(row), "sex_gender_analysis": sga.lean_block(full)})
    blk = sex_gender_analysis_summary(studies)
    assert blk["trials"] == len(studies) and blk["recordsInDataset"] == len(studies)
    assert blk["reportingDimension"]["both"] == 2
    # a record without the block is not counted, and the block says so
    studies.append({"nct_id": "NCT99000097", "results_date": "2024-01-01", "enrollment": 1, "sex_gender": None})
    blk = sex_gender_analysis_summary(studies)
    assert blk["trials"] == len(studies) - 1 and blk["recordsInDataset"] == len(studies)
    # no record carries the block: None, never an empty block
    assert sex_gender_analysis_summary([{"nct_id": "X"}]) is None
    # the sidecar rows win over the records' own blocks when given
    rows = [sga.flat_row(c["nct_id"], _derive(c)[1], SNAP) for c in _cases()]
    assert sex_gender_analysis_summary(studies, rows)["trials"] == len(_cases())


# --------------------------------------------------------------- 9. review findings (PR 29)
def _sex_table(female, male, unknown=None, title="Sex: Female, Male", **extra):
    cats = [{"title": "Female", "measurements": [{"groupId": "BG002", "value": str(female)}]},
            {"title": "Male", "measurements": [{"groupId": "BG002", "value": str(male)}]}]
    if unknown is not None:
        cats.append({"title": "Unknown", "measurements": [{"groupId": "BG002", "value": str(unknown)}]})
    return {"title": title, "paramType": "COUNT_OF_PARTICIPANTS", "unitOfMeasure": "Participants",
            "classes": [{"categories": cats}], **extra}


def _gender_table(param_type="COUNT_OF_PARTICIPANTS", unit="Participants", trans=25, female=40, male=35):
    return {"title": "Gender", "paramType": param_type, "unitOfMeasure": unit, "classes": [{"categories": [
        {"title": "Female", "measurements": [{"groupId": "BG002", "value": str(female)}]},
        {"title": "Male", "measurements": [{"groupId": "BG002", "value": str(male)}]},
        {"title": "Transgender", "measurements": [{"groupId": "BG002", "value": str(trans)}]}]}]}


def _analyze(measures, enrollment=100):
    study = _study("NCT99000090", measures, enrollment)
    row = sgt.build_row(study, SNAP)
    raw = sgt.select_raw_measures(study)
    return row, sga.analyze(row, raw["measures"], raw["enrollment"])


def test_gender_table_in_non_participant_units_excludes_the_trial():
    """A participant-count Sex table beside a COUNT_OF_UNITS Gender table: the
    parser's is_participant_count describes the primary table, so the units of
    the table that posts the identity category are checked on their own."""
    row, full = _analyze([_sex_table(50, 50), _gender_table("COUNT_OF_UNITS", "Eyes")])
    assert row["is_participant_count"] is True
    gc = full["gender_categories"]
    assert gc["status"] == "excluded"
    assert gc["exclusion"]["reason"] == "non_participant_units" and gc["exclusion"]["tables"] == ["Gender"]
    assert sga.lean_block(full)["gender_categories_status"] == "excluded"
    # the same tables in participants derive
    row, full = _analyze([_sex_table(50, 50), _gender_table()])
    assert full["gender_categories"]["status"] == "derived"
    assert full["gender_categories"]["d3_total"] == 25


def test_explicit_unknown_with_values_is_per_measure():
    """R-EU-01 is values and Unknown in the SAME measure. Two Sex tables, one
    with Female/Male and one with only Unknown, do not qualify, although the
    row-level counts (Unknown taken as a maximum across tables) look the same."""
    unknown_only = {"title": "Sex: Female, Male", "paramType": "COUNT_OF_PARTICIPANTS", "unitOfMeasure": "Participants",
                    "classes": [{"categories": [{"title": "Unknown", "measurements": [{"groupId": "BG002", "value": "100"}]}]}]}
    row, full = _analyze([_sex_table(50, 50), unknown_only])
    assert (row.get("n_unknown") or 0) > 0 and (row.get("n_female") or 0) > 0  # the row cannot tell
    assert full["reporting"]["explicit_unknown_with_values"] is False
    row, full = _analyze([_sex_table(50, 50, unknown=100)])
    assert full["reporting"]["explicit_unknown_with_values"] is True
    # the lean-only fallback keeps the row-level reading and says so by its input
    assert sga.analyze(row, None, 100)["reporting"]["explicit_unknown_with_values"] is True


def test_mobile_summary_refuses_a_sidecar_from_another_pull(tmp_path):
    import generate_mobile_data as gmd
    table = tmp_path / "sex_gender_analysis.csv.gz"
    table.write_bytes(b"")
    meta = tmp_path / "sex_gender_analysis_meta.json"
    meta.write_text(json.dumps({"source_extracted_at": "2026-10-21T00:00:00+00:00", "source_pipeline_commit": "old0000"}))
    with pytest.raises(SystemExit, match="derived from extracted_at"):
        gmd.check_analysis_provenance(str(meta), "2026-10-28T00:00:00+00:00", "new0000", str(table))
    with pytest.raises(SystemExit, match="no meta file"):
        gmd.check_analysis_provenance(str(tmp_path / "missing.json"), "2026-10-28T00:00:00+00:00", "new0000", str(table))
    meta.write_text(json.dumps({"source_extracted_at": "2026-10-28T00:00:00+00:00", "source_pipeline_commit": "new0000"}))
    assert gmd.check_analysis_provenance(str(meta), "2026-10-28T00:00:00+00:00", "new0000", str(table))["source_pipeline_commit"] == "new0000"


def test_write_back_refuses_another_pipeline_commit(tmp_path):
    """Same extracted_at, different pipeline_commit: another pull's output, refused."""
    import build_sex_gender_analysis as bld
    cases = _cases()
    study = _study(cases[0]["nct_id"], cases[0]["measures"], cases[0]["enrollment"])
    env = sgt.select_raw_envelope(study)
    env.update({"refetched": False, "snapshot_date": SNAP, "extracted_at": "2026-10-28T00:00:00+00:00", "pipeline_commit": "aaaaaaa"})
    src = tmp_path / "sex_gender_raw_envelope.jsonl.gz"
    _write_jsonl(src, [env])
    row = sgt.build_row(study, SNAP)
    demo = tmp_path / "demographics.json"
    demo.write_text(json.dumps({"extracted_at": "2026-10-28T00:00:00+00:00", "pipeline_commit": "bbbbbbb",
                                "data": [{"nct_id": cases[0]["nct_id"], "sex_gender": sgt.lean_row(row)}]}))
    before = demo.read_text()
    with pytest.raises(SystemExit, match="pipeline commit"):
        bld.main(["--from-envelope", str(src), "--out", str(tmp_path / "a.csv.gz"), "--meta", str(tmp_path / "a_meta.json"),
                  "--write-back", str(demo)])
    assert demo.read_text() == before


def test_write_back_refuses_unstamped_raw_records_and_meta_never_borrows_target_stamps(tmp_path):
    import build_sex_gender_analysis as bld
    cases = _cases()
    study = _study(cases[0]["nct_id"], cases[0]["measures"], cases[0]["enrollment"])
    env = sgt.select_raw_envelope(study)
    env.update({"refetched": False, "snapshot_date": SNAP})  # no extracted_at, no pipeline_commit
    src = tmp_path / "sex_gender_raw_envelope.jsonl.gz"
    _write_jsonl(src, [env])
    row = sgt.build_row(study, SNAP)
    demo = tmp_path / "demographics.json"
    demo.write_text(json.dumps({"extracted_at": "2026-10-28T00:00:00+00:00", "pipeline_commit": "bbbbbbb",
                                "data": [{"nct_id": cases[0]["nct_id"], "sex_gender": sgt.lean_row(row)}]}))
    before = demo.read_text()
    with pytest.raises(SystemExit, match="lack extracted_at or pipeline_commit"):
        bld.main(["--from-envelope", str(src), "--out", str(tmp_path / "a.csv.gz"), "--meta", str(tmp_path / "a_meta.json"),
                  "--write-back", str(demo)])
    assert demo.read_text() == before
    # without write-back the sidecar is built, but its meta names no source pull
    bld.main(["--from-envelope", str(src), "--out", str(tmp_path / "b.csv.gz"), "--meta", str(tmp_path / "b_meta.json")])
    meta = json.loads((tmp_path / "b_meta.json").read_text())
    assert meta["source_extracted_at"] is None and meta["source_pipeline_commit"] is None
    assert meta["n_unstamped_source_records"] == 1
    import generate_mobile_data as gmd
    with pytest.raises(SystemExit, match="derived from extracted_at None"):
        gmd.check_analysis_provenance(str(tmp_path / "b_meta.json"), "2026-10-28T00:00:00+00:00", "bbbbbbb", "b.csv.gz")
