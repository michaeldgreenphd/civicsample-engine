"""The sex/gender table around the vendored parser (src/sex_gender_table.py).

The parser's own rules are pinned by the vendored suite next to this file.
These tests pin what the ENGINE adds on top of it:

  1. No denominator balancing. n_unknown is only what canon_bucket() mapped to
     "unknown"; the enrollment gap is stored as enrollment_minus_parsed, never
     added to unknown, never a status input, and no balancing code path is
     reachable from the table (amendment 1).
  2. percent_female is computed only inside the README D5 denominator set.
  3. The refetch rule for stripped search-endpoint records.
  4. The re-parse path (retained raw measures) equals the API path.
  5. The structural validation targets from README "Validation targets" pass
     on the fixtures and catch a corrupted row.
  6. Provenance: every row carries parser_rules_version and snapshot_date.
  7. The orchestrator stores the row under study["sex_gender"] with the legacy
     extractors on or off.
  8. The mobile summary block is built only from the rows.
"""
import inspect
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from src import sex_gender_table as sgt  # noqa: E402
from src import sex_gender_parser as sgp  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SNAP = "2026-09-14"


def _measure(title, cats, param_type="COUNT_OF_PARTICIPANTS", unit="Participants", **extra):
    return {"title": title, "paramType": param_type, "unitOfMeasure": unit,
            "classes": [{"categories": [
                {"title": t, "measurements": [{"groupId": "BG000", "value": str(v)}]} for t, v in cats]}],
            **extra}


def _study(nct, measures, enrollment=100):
    return {"protocolSection": {"identificationModule": {"nctId": nct},
                                "designModule": {"enrollmentInfo": {"count": enrollment}}},
            "resultsSection": {"baselineCharacteristicsModule": {"measures": measures}}}


# --------------------------------------------------------------------- 1. no balancing
def test_enrollment_gap_is_stored_not_added_to_unknown():
    """Amendment 1: enrollment 100, Female 40 / Male 50 -> n_unknown None, status
    reported, enrollment_minus_parsed 10."""
    row = sgt.build_row(_study("NCT1", [_measure("Sex: Female, Male", [("Female", 40), ("Male", 50)])], 100), SNAP)
    assert row["n_unknown"] is None
    assert row["sex_report_status"] == "reported"
    assert row["enrollment_minus_parsed"] == 10
    assert row["n_total_parsed"] == 90
    assert row["percent_female"] == pytest.approx(100 * 40 / 90)


def test_enrollment_gap_never_changes_status():
    # A large gap with a small posted table is still "reported"; a gap with an
    # empty table is still "uninformative"; a gap with an Unknown-only table is
    # still explicit_unknown_only.
    r = sgt.build_row(_study("NCT2", [_measure("Sex: Female, Male", [("Female", 1), ("Male", 0)])], 5000), SNAP)
    assert r["sex_report_status"] == "reported" and r["enrollment_minus_parsed"] == 4999 and r["n_unknown"] is None
    r = sgt.build_row(_study("NCT3", [{"title": "Sex: Female, Male",
                                       "classes": [{"categories": [{"title": "Female"}, {"title": "Male"}]}]}], 300), SNAP)
    assert r["sex_report_status"] == "uninformative" and r["enrollment_minus_parsed"] == 300 and r["n_unknown"] is None
    r = sgt.build_row(_study("NCT4", [_measure("Sex: Female, Male", [("Female", 0), ("Male", 0), ("Unknown", 9)])], 20), SNAP)
    assert r["sex_report_status"] == "explicit_unknown_only" and r["n_unknown"] == 9 and r["enrollment_minus_parsed"] == 11


def test_enrollment_gap_is_none_without_a_measure_or_an_enrollment():
    r = sgt.build_row(_study("NCT5", [{"title": "Age, Continuous"}], 100), SNAP)
    assert r["sex_report_status"] == "not_reported" and r["enrollment_minus_parsed"] is None
    r = sgt.build_row(_study("NCT6", [_measure("Sex: Female, Male", [("Female", 4), ("Male", 5)])], None), SNAP)
    assert r["enrollment"] is None and r["enrollment_minus_parsed"] is None
    # Counts above enrollment: the gap is negative and stored as-is, and the
    # parser's own flag says so; nothing is clamped.
    r = sgt.build_row(_study("NCT7", [_measure("Sex: Female, Male", [("Female", 60), ("Male", 60)])], 100), SNAP)
    assert r["enrollment_minus_parsed"] == -20 and r["flag_exceeds_enrollment"] is True


def test_no_balancing_code_path_is_reachable_from_the_table():
    """The legacy extractors balance by reading the baseline denominators. The
    table module must not import them, call get_total_baseline_participants,
    or carry any remainder arithmetic; the orchestrator must build the row
    from build_row only."""
    import ast

    def code_only(source):
        """The module's code with docstrings and comments removed, so the
        prohibition is on what runs, not on the prose that explains it."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
        return ast.unparse(tree)

    src = code_only(inspect.getsource(sgt))
    for forbidden in ("remainder", "balanc", "get_total_baseline_participants",
                      "sex_extractor", "gender_extractor", "get_overall_group_id"):
        assert forbidden not in src, f"{forbidden!r} appears in sex_gender_table.py code"
    from src import extract_all
    orch = code_only(inspect.getsource(extract_all.extract_demographics_from_study))
    assert "sgt.build_row(study" in orch
    for forbidden in ("remainder", "balanc", "get_total_baseline_participants"):
        assert forbidden not in orch


# --------------------------------------------------------------------- 2. percent female
def test_percent_female_only_inside_the_denominator_set():
    # single-sex trials are included (0 or 100)
    r = sgt.build_row(_study("A", [_measure("Sex: Female, Male", [("Female", 30), ("Male", 0)])]), SNAP)
    assert r["percent_female"] == 100.0
    # COUNT_OF_UNITS: reported, but not a participant count -> None
    r = sgt.build_row(_study("B", [_measure("Sex: Female, Male", [("Female", 30), ("Male", 10)],
                                           param_type="COUNT_OF_UNITS", unit="tests")]), SNAP)
    assert r["reported_sex"] is True and r["is_participant_count"] is False and r["percent_female"] is None
    # explicit unknown only -> None; gender-only categories never enter
    r = sgt.build_row(_study("C", [_measure("Sex: Female, Male", [("Female", 0), ("Male", 0), ("Unknown", 5)])]), SNAP)
    assert r["percent_female"] is None
    r = sgt.build_row(_study("D", [_measure("Gender", [("Woman", 10), ("Man", 30), ("Non-binary", 60)])]), SNAP)
    assert r["percent_female"] == 25.0          # 10 / (10 + 30); the 60 non-binary are excluded
    # not reported -> None
    r = sgt.build_row(_study("E", [{"title": "Age"}]), SNAP)
    assert r["percent_female"] is None


def test_percent_female_series_uses_both_rules_over_one_set():
    rows = [
        {"year": "2020", "percent_female": 100.0, "n_female": 10, "n_male": 0},
        {"year": "2020", "percent_female": 0.0, "n_female": 0, "n_male": 990},
        {"year": "2020", "percent_female": None, "n_female": 5, "n_male": 5},   # outside the set
    ]
    s = sgt.percent_female_series(rows)["2020"]
    assert s["pf_sum"] / s["pf_count"] == 50.0                     # series (a)
    assert 100 * s["f_sum"] / s["fm_sum"] == pytest.approx(1.0)    # series (b)


# --------------------------------------------------------------------- 3. refetch rule
def test_refetch_only_for_no_measurements_without_any_measurement_object():
    empty = _study("R1", [{"title": "Sex: Female, Male", "classes": [{"categories": [{"title": "Female"}, {"title": "Male"}]}]}])
    row, raw = sgt.build_row(empty, SNAP), sgt.select_raw_measures(empty)
    assert row["uninformative_reason"] == "no_measurements" and sgt.needs_refetch(row, raw) is True
    # all-NA: measurement objects exist, so the data is what the registry holds
    na = _study("R2", [_measure("Sex: Female, Male", [("Female", "NA"), ("Male", "NA")])])
    row, raw = sgt.build_row(na, SNAP), sgt.select_raw_measures(na)
    assert row["uninformative_reason"] == "all_values_na" and sgt.needs_refetch(row, raw) is False
    ok = _study("R3", [_measure("Sex: Female, Male", [("Female", 4), ("Male", 5)])])
    assert sgt.needs_refetch(sgt.build_row(ok, SNAP), sgt.select_raw_measures(ok)) is False
    # a refetched-but-still-empty record keeps its parsed state and is marked
    row = sgt.build_row(empty, SNAP, refetched=True)
    assert row["sex_report_status"] == "uninformative" and row["refetched"] is True


# --------------------------------------------------------------------- 4. re-parse path
def _fixture_cases():
    with open(os.path.join(FIX, "fixture_trials.json")) as fh:
        return json.load(fh)["cases"]


@pytest.mark.parametrize("case", _fixture_cases(), ids=lambda c: c["nct_id"])
def test_retained_raw_measures_reparse_to_the_same_row(case):
    study = _study(case["nct_id"], case["measures"], case["enrollment"])
    a = sgt.ordered(sgt.build_row(study, SNAP))
    b = sgt.ordered(sgt.build_row_from_raw(sgt.select_raw_measures(study), SNAP))
    assert a == b
    for k, v in case["expected"].items():
        assert a[k] == v or (isinstance(v, float) and a[k] == pytest.approx(v)), (k, a[k], v)


def test_select_raw_measures_keeps_only_selected_measures_verbatim():
    study = _study("S", [{"title": "Age, Continuous", "classes": []},
                         _measure("Sex: Female, Male", [("Female", 4), ("Male", 5)],
                                  populationDescription="all randomized")])
    raw = sgt.select_raw_measures(study)
    assert raw["nct_id"] == "S" and raw["enrollment"] == 100
    assert [m["title"] for m in raw["measures"]] == ["Sex: Female, Male"]
    assert raw["measures"][0]["populationDescription"] == "all randomized"
    assert raw["measures"][0] is study["resultsSection"]["baselineCharacteristicsModule"]["measures"][1]


# --------------------------------------------------------------------- 5. structural targets
def _fixture_rows():
    rows = [sgt.ordered(sgt.build_row(_study(c["nct_id"], c["measures"], c["enrollment"]), SNAP)) for c in _fixture_cases()]
    rows.append(sgt.ordered(sgt.build_row(_study("NR", [{"title": "Age"}]), SNAP)))
    rows.append(sgt.ordered(sgt.parse_error_row("PE", 10, SNAP)))
    return rows


def test_structural_checks_pass_on_the_fixtures():
    checks = sgt.structural_checks(_fixture_rows())
    assert sgt.all_pass(checks), {k: v for k, v in checks.items() if not v["pass"]}
    sc = sgt.status_counts(_fixture_rows())
    assert sum(sc["status"].values()) == 34
    assert sc["status"]["parse_error"] == 1 and sc["status"]["not_reported"] == 1


def test_structural_checks_catch_a_corrupted_row():
    rows = _fixture_rows()
    bad = next(r for r in rows if r["param_type"] == "COUNT_OF_UNITS")
    bad["is_participant_count"] = True
    checks = sgt.structural_checks(rows)
    assert not checks["count_of_units_never_participant_count"]["pass"]
    assert not checks["is_participant_count_matches_parser_rule"]["pass"]
    rows = _fixture_rows()
    pe = next(r for r in rows if r["sex_report_status"] == "parse_error")
    pe["reported_sex"] = False
    assert not sgt.structural_checks(rows)["parse_error_rows_have_none_outcomes"]["pass"]


def test_parse_error_row_has_none_outcomes_and_no_percent():
    r = sgt.parse_error_row("X", 10, SNAP)
    assert r["sex_report_status"] == "parse_error"
    assert all(r[o] is None for o in sgt.OUTCOMES)
    assert r["percent_female"] is None and r["enrollment_minus_parsed"] is None


# --------------------------------------------------------------------- 6. provenance
def test_every_row_carries_provenance_and_the_published_column_order():
    r = sgt.build_row(_study("P", [_measure("Sex: Female, Male", [("Female", 4), ("Male", 5)])]), SNAP)
    assert r["parser_rules_version"] == sgp.PARSER_RULES_VERSION
    assert r["parser_module_version"] == sgp.__version__
    assert r["snapshot_date"] == SNAP
    assert list(sgt.ordered(r).keys()) == sgt.COLUMNS
    assert sgt.COLUMNS[0] == "nct_id" and "parser_rules_version" in sgt.COLUMNS


# --------------------------------------------------------------------- 7. orchestrator
def test_orchestrator_stores_the_row_with_legacy_on_and_off():
    from validate_fixes import _make_study, _make_measure, _make_class, _make_category
    from src.extract_all import extract_demographics_from_study, _needs_refetch
    study = _make_study("NCT_ORCH", "orch", [
        _make_measure("Sex: Female, Male", [_make_class("", [_make_category("Female", 40), _make_category("Male", 35)])])
    ], total_participants=75)
    on = extract_demographics_from_study(study, snapshot_date=SNAP)
    assert on["sex_gender"]["sex_report_status"] == "reported" and on["sex_gender"]["n_female"] == 40
    assert on["sex"]["reported"] is True                     # legacy still runs for sg=v1
    off = extract_demographics_from_study(study, snapshot_date=SNAP, legacy_sex_gender=False)
    assert off["sex"] is None and off["gender"] is None
    assert off["sex_gender"]["sex_report_status"] == "reported"
    assert _needs_refetch(on, study, sgt.select_raw_measures(study)) == ""
    empty = _make_study("NCT_EMPTY", "e", [{"title": "Sex: Female, Male", "paramType": "COUNT_OF_PARTICIPANTS",
                                             "classes": [{"categories": [{"title": "Female"}, {"title": "Male"}]}]}],
                        total_participants=75)
    res = extract_demographics_from_study(empty, snapshot_date=SNAP)
    assert _needs_refetch(res, empty, sgt.select_raw_measures(empty)) == "sg_v2"


def test_parser_failure_becomes_a_parse_error_row_not_a_dropped_trial(monkeypatch):
    """A parser exception on one record is that record's parse_error state; the
    trial stays in the output with None outcomes (never False, never absent)."""
    from validate_fixes import _make_study, _make_measure, _make_class, _make_category
    from src import extract_all

    def boom(study, snapshot_date, refetched=False):
        raise ValueError("synthetic parser failure")

    monkeypatch.setattr(extract_all.sgt, "build_row", boom)
    study = _make_study("NCT_BOOM", "boom", [
        _make_measure("Sex: Female, Male", [_make_class("", [_make_category("Female", 1), _make_category("Male", 2)])])
    ], total_participants=3)
    res = extract_all.extract_demographics_from_study(study, snapshot_date=SNAP)
    assert res is not None and res["nct_id"] == "NCT_BOOM"
    row = res["sex_gender"]
    assert row["sex_report_status"] == "parse_error"
    assert row["reported_sex"] is None and row["reported_gender"] is None
    assert row["parser_rules_version"] == sgp.PARSER_RULES_VERSION
    assert res["sex"]["reported"] is True                    # the legacy path is unaffected


def test_refetch_flag_and_extraction_stamp_survive_the_rebuild_from_raw():
    study = _study("RF", [_measure("Sex: Female, Male", [("Female", 4), ("Male", 5)])])
    raw = sgt.select_raw_measures(study)
    raw["refetched"] = True
    raw["extracted_at"] = "2026-09-14T06:00:00+00:00"
    row = sgt.build_row_from_raw(raw, SNAP)
    assert row["refetched"] is True
    assert sgt.build_row_from_raw(sgt.select_raw_measures(study), SNAP)["refetched"] is False
    # the builder's raw reader hands the stamp on to the meta
    import gzip
    import json
    import tempfile
    from build_sex_gender_table import rows_from_raw, rows_from_records
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "raw.jsonl.gz")
        with gzip.open(p, "wt") as fh:
            fh.write(json.dumps({**raw, "snapshot_date": SNAP}) + "\n")
        rows, snap, extracted_at = rows_from_raw(p, None)
        assert snap == SNAP and extracted_at == "2026-09-14T06:00:00+00:00"
        assert rows[0]["refetched"] is True
    # and lean rows are refused as input to the full table
    with pytest.raises(SystemExit, match="lean row"):
        rows_from_records([{"nct_id": "L", "sex_gender": sgt.lean_row(row)}])


# --------------------------------------------------------------------- 8. mobile block
def test_mobile_summary_block_is_built_from_the_rows_only():
    from generate_mobile_data import sex_gender_summary
    studies = []
    for nct, cats, enr, yr in [("A", [("Female", 40), ("Male", 50)], 100, "2020"),
                               ("B", [("Female", 0), ("Male", 0), ("Unknown", 9)], 9, "2020"),
                               ("C", [("Female", 10), ("Male", 0)], 10, "2021")]:
        s = _study(nct, [_measure("Sex: Female, Male", cats)], enr)
        studies.append({"nct_id": nct, "results_date": f"{yr}-06-01", "enrollment": enr,
                        "sex_gender": sgt.build_row(s, SNAP)})
    studies.append({"nct_id": "D", "results_date": "2021-01-01", "enrollment": 50,
                    "sex_gender": sgt.build_row(_study("D", [{"title": "Age"}], 50), SNAP)})
    # The parts carry only the lean row; the block reads the full rows (the CSV).
    full_rows = [dict(s["sex_gender"]) for s in studies]
    for s in studies:
        s["sex_gender"] = sgt.lean_row(s["sex_gender"])
    blk = sex_gender_summary(studies, full_rows)
    lean_only = sex_gender_summary(studies)                # fallback: same counts, no label lists
    assert lean_only["statusCounts"] == blk["statusCounts"] and lean_only["totals"] == blk["totals"]
    assert lean_only["byYear"]["2020"]["sg_pf_sum"] == blk["byYear"]["2020"]["sg_pf_sum"]
    assert blk["statusCounts"] == {"reported": 2, "explicit_unknown_only": 1, "uninformative": 0, "not_reported": 1, "parse_error": 0}
    assert blk["totals"] == {"female": 50, "male": 50, "explicit_unknown": 0, "gender_diverse": 0, "ambiguous": 0}
    assert blk["enrollmentMinusParsed"] == 10           # A's gap; shipped separately, never in explicit_unknown
    y20, y21 = blk["byYear"]["2020"], blk["byYear"]["2021"]
    assert y20["status_reported"] == 1 and y20["status_explicit_unknown_only"] == 1
    assert y20["sg_pf_sum"] == pytest.approx(100 * 40 / 90) and y20["sg_pf_count"] == 1
    assert y21["sg_pf_sum"] == 100.0 and y21["status_not_reported"] == 1
    assert y21["sg_enrollment_not_reported"] == 50
    assert blk["parser_rules_version"] == sgp.PARSER_RULES_VERSION
    assert "year" not in studies[0]["sex_gender"]        # the helper leaves the rows as it found them


# --------------------------------------------------------------------- 9. lean row and the CSV round trip
def test_lean_row_is_the_ui_subset_and_the_csv_is_the_full_record(tmp_path):
    from build_sex_gender_table import write_table
    row = sgt.build_row(_study("L", [_measure("Gender", [("Woman", 10), ("Man", 30), ("Non-binary", 2)])], 50), SNAP)
    lean = sgt.lean_row(row)
    assert list(lean.keys()) == sgt.LEAN_COLUMNS
    assert lean["sex_report_status"] == "reported" and lean["n_gender_diverse"] == 2
    assert lean["enrollment_minus_parsed"] == 8 and lean["parser_rules_version"] == sgp.PARSER_RULES_VERSION
    for heavy in ("gender_diverse_labels", "measure_title", "layout", "percent_female", "flag_customized_layout"):
        assert heavy not in lean
    path = str(tmp_path / "t.csv.gz")
    write_table([sgt.ordered(row), sgt.ordered(sgt.parse_error_row("PE", None, SNAP))], path)
    back = sgt.read_table(path)
    assert len(back) == 2 and list(back[0].keys()) == sgt.COLUMNS
    assert back[0]["n_female"] == 10.0 and back[0]["reported_gender"] is True and back[0]["is_participant_count"] is True
    assert back[0]["gender_diverse_labels"] == "Non-binary" and back[0]["n_unknown"] is None
    assert back[1]["sex_report_status"] == "parse_error" and back[1]["reported_sex"] is None
    assert sgt.lean_row(back[0]) == lean
