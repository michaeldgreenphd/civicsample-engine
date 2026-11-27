"""The weekly record's two readings of the baseline table, and DAP's weekly path reading them.

`src/extract_all.py` writes `baseline_participants` (the table's own Participants count) and
`sex_primary_measure` (the primary sex/gender measure's own class flag and counted total) on every record,
and `dap/demographics.py` applies to a weekly record the rules its raw path applies to the raw record. Most
tests run the real weekly producer on a raw fixture and compare the two paths on the same trial. Fictional
NCT9980008x identifiers; no network.
"""
from __future__ import annotations

import copy
import gzip
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT: Path = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

_FX_SPEC = importlib.util.spec_from_file_location("dap_fixture", Path(__file__).resolve().parent / "fixture.py")
assert _FX_SPEC is not None and _FX_SPEC.loader is not None
fx = sys.modules.setdefault("dap_fixture", importlib.util.module_from_spec(_FX_SPEC))
if not hasattr(fx, "make_raw_ctgov_study"):
    _FX_SPEC.loader.exec_module(fx)

import build_dap_benchmarks as cli_build  # noqa: E402
from dap import artifacts, benchmarks  # noqa: E402
from dap.demographics import extract_demographics  # noqa: E402
from src import sex_gender_table as sgt  # noqa: E402
from src.extract_all import extract_demographics_from_study  # noqa: E402

# The fictional holdout index every DAP build now requires (src/holdout_exclusion.py).
HOLDOUT_ARGS: list[str] = ["--holdout-index", str(ROOT / "tests" / "fixtures" / "holdout_index" / "index.json")]

MULTICLASS = "sex_counts_from_one_class_of_several"
WITHHELD = ("percent_female", "female_share", "of_parsed_total", "of_classified")
NEW_FIELDS = ("baseline_participants", "sex_primary_measure")


def _weekly(study: dict[str, Any]) -> dict[str, Any]:
    """The weekly record the producer writes for a raw study."""
    rec = extract_demographics_from_study(study, snapshot_date="2026-11-27")
    assert rec is not None
    return rec


def _sex_by_class(classes: list[tuple[str | None, int, int]], title: str = "Sex: Female, Male") -> dict[str, Any]:
    """A standard-layout sex measure for group BG000, one class per (title, female, male)."""
    return {"title": title, "paramType": "COUNT_OF_PARTICIPANTS", "unitOfMeasure": "Participants",
            "classes": [{**({"title": t} if t is not None else {}), "categories": [
                {"title": "Female", "measurements": [{"groupId": "BG000", "value": str(f)}]},
                {"title": "Male", "measurements": [{"groupId": "BG000", "value": str(m)}]}]}
                for t, f, m in classes]}


def _gender_unknown(value: str) -> dict[str, Any]:
    """A second sex/gender measure, "Gender, Customized", posting only an Unknown row."""
    return {"title": "Gender, Customized", "paramType": "COUNT_OF_PARTICIPANTS", "unitOfMeasure": "Participants",
            "classes": [{"categories": [{"title": "Unknown or Not Reported",
                                         "measurements": [{"groupId": "BG000", "value": value}]}]}]}


# NCT04911296's "Sex: Female, Male": three age bands, then the Total class, of 150 participants.
_AGE_BANDS = [("21 - 50 years", 29, 21), ("51 - 65 years", 28, 22), ("66 - 75 years", 28, 22)]


def _age_band_study(nct_id: str, *, baseline_denom: int | None = 150, total_first: bool = False,
                    extra: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    classes = [("Total", 85, 65), *_AGE_BANDS] if total_first else [*_AGE_BANDS, ("Total", 85, 65)]
    study = fx.make_raw_ctgov_study(nct_id, baseline_denom=baseline_denom,
                                    measures=[_sex_by_class(classes), *(extra or [])])
    if baseline_denom is None:
        # A Participants denominator posted as "NA": the table has no count, and the extractors fall back to
        # the registered enrollment (120).
        study["resultsSection"]["baselineCharacteristicsModule"]["denoms"] = [
            {"units": "Participants", "counts": [{"groupId": "BG000", "value": "NA"}]}]
    return study


# ── The producer's fields ─────────────────────────────────────────────────────────────────────────────


def test_the_weekly_record_carries_the_tables_participants_count() -> None:
    # On 03fd911 the weekly record carried no baseline count, so DAP's weekly path had none to read.
    rec = _weekly(fx.make_raw_ctgov_study("NCT99800080", baseline_denom=150, enrollment=170))
    assert rec["baseline_participants"] == {"count": 150, "absent_reason": None}


def test_a_participants_count_of_na_is_absent_with_a_reason_not_the_enrollment() -> None:
    # The extractors balance such a table against the registered enrollment (120: Female 50, Male 40 and 30
    # unknown); the field records that the table posts no count rather than the enrollment or 0.
    study = fx.make_raw_ctgov_study("NCT99800081", measures=[_sex_by_class([(None, 50, 40)])])
    study["resultsSection"]["baselineCharacteristicsModule"]["denoms"][0]["counts"][0]["value"] = "NA"
    rec = _weekly(study)
    assert rec["baseline_participants"] == {"count": None, "absent_reason": "no_participants_denominator"}
    assert rec["sex"]["totals"] == {"female": 50, "male": 40, "unknown": 30}


def test_a_study_without_results_or_without_a_baseline_module_says_which() -> None:
    no_results = _weekly(fx.make_raw_ctgov_study("NCT99800082", has_results=False))
    assert no_results["baseline_participants"] == {"count": None, "absent_reason": "no_results_section"}
    assert no_results["sex_primary_measure"] == {
        "flag_multiclass_timepoint": None, "n_total_parsed": None, "absent_reason": "no_sex_gender_measure"}
    study = fx.make_raw_ctgov_study("NCT99800083")
    study["resultsSection"] = {"participantFlowModule": {"groups": []}}
    assert _weekly(study)["baseline_participants"] == {"count": None, "absent_reason": "no_baseline_module"}


def test_the_weekly_record_carries_the_primary_measures_own_flag_and_total() -> None:
    # The first age band: the parser read 29 and 21 of its several classes.
    rec = _weekly(_age_band_study("NCT99800084"))
    assert rec["sex_primary_measure"] == {"flag_multiclass_timepoint": True, "n_total_parsed": 50.0,
                                          "absent_reason": None}
    # Another measure's classes do not set the primary measure's flag (NCT03726437's "Sexual Violence
    # Victimization"), though they set the row's (sgp.parse_trial).
    other = _sex_by_class([("Part 1", 1, 1), ("Part 2", 1, 1)], title="Sexual Violence Victimization")
    rec = _weekly(fx.make_raw_ctgov_study("NCT99800085", measures=[_sex_by_class([(None, 50, 40)]), other]))
    assert rec["sex_primary_measure"]["flag_multiclass_timepoint"] is False
    # Another measure's Unknown is the row's (a maximum across measures) but not the primary measure's own.
    rec = _weekly(fx.make_raw_ctgov_study("NCT99800086", measures=[
        _sex_by_class([("Cohort A", 60, 30), ("Cohort B", 5, 5)]), _gender_unknown("10")]))
    assert rec["sex_gender"]["n_unknown"] == 10
    assert rec["sex_primary_measure"] == {"flag_multiclass_timepoint": True, "n_total_parsed": 90.0,
                                          "absent_reason": None}


def test_a_row_the_parser_could_not_read_has_no_primary_measure_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("unreadable")

    monkeypatch.setattr(sgt, "build_row", fail)
    rec = _weekly(_age_band_study("NCT99800087"))
    assert rec["sex_gender"]["sex_report_status"] == "parse_error"
    assert rec["sex_primary_measure"] == {"flag_multiclass_timepoint": None, "n_total_parsed": None,
                                          "absent_reason": "parse_error"}


# ── DAP's weekly path, compared with its raw path on the same trial ─────────────────────────────────


def _both(study: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """DAP's demographics for a raw study and for the weekly record the producer writes for it."""
    return extract_demographics(study), extract_demographics(_weekly(study))


def test_a_weekly_record_withholds_the_first_of_several_classes_as_its_raw_record_does() -> None:
    # On 03fd911 the raw path withheld the first age band's share (29 and 21 of 150) and the weekly path
    # published it, 0.58, as the trial's.
    raw, weekly = _both(_age_band_study("NCT99800088"))
    for demo in (raw, weekly):
        sg = demo["sex_gender"]
        assert (sg["sex_report_status"], sg["n_female"], sg["n_male"]) == ("reported", 29, 21)
        assert sg["composition_withheld_reason"] == MULTICLASS
        assert all(sg[k] is None for k in WITHHELD)


def test_another_measures_unknown_does_not_complete_a_weekly_subgroups_counts() -> None:
    # On 03fd911 the weekly path kept the subgroup's share, 2/3; read from the row, its Female 60, Male 30 and
    # the gender measure's Unknown 10 would make up the table's 100. The raw path adds the primary measure's
    # own counts (90) and withholds it.
    study = fx.make_raw_ctgov_study("NCT99800089", baseline_denom=100, measures=[
        _sex_by_class([("Cohort A", 60, 30), ("Cohort B", 5, 5)]), _gender_unknown("10")])
    raw, weekly = _both(study)
    assert raw["sex_gender"]["composition_withheld_reason"] == MULTICLASS
    assert weekly["sex_gender"]["composition_withheld_reason"] == MULTICLASS
    assert weekly["sex_gender"]["female_share"] is None


def test_a_weekly_first_class_of_a_table_with_no_count_is_withheld_as_raw() -> None:
    # A first class can make up no count the table does not post, so both paths withhold it.
    raw, weekly = _both(_age_band_study("NCT99800090", baseline_denom=None))
    assert raw["sex_gender"]["composition_withheld_reason"] == MULTICLASS
    assert weekly["sex_gender"]["composition_withheld_reason"] == MULTICLASS


def test_a_weekly_first_class_that_makes_up_the_table_keeps_its_composition() -> None:
    # Control, on both heads: the Total class posted first (as "All Patients" is in NCT03043872).
    raw, weekly = _both(_age_band_study("NCT99800091", total_first=True))
    for demo in (raw, weekly):
        assert (demo["sex_gender"]["female_share"], demo["sex_gender"]["composition_withheld_reason"]) == (
            85 / 150, None)


def test_another_measures_classes_do_not_withhold_a_weekly_one_class_measure() -> None:
    # Control, on both heads: the row's flag comes from the other measure; the primary measure has one class.
    other = _sex_by_class([("Part 1", 1, 1), ("Part 2", 1, 1)], title="Sexual Violence Victimization")
    raw, weekly = _both(fx.make_raw_ctgov_study("NCT99800092", measures=[_sex_by_class([(None, 50, 40)]), other]))
    for demo in (raw, weekly):
        assert (demo["sex_gender"]["female_share"], demo["sex_gender"]["composition_withheld_reason"]) == (
            50 / 90, None)


def test_a_weekly_record_from_before_the_fields_keeps_its_first_class_composition() -> None:
    # Control, on both heads: a record extracted before the fields existed is read as before (documented in
    # dap/README.md); nothing is guessed for it.
    rec = _weekly(_age_band_study("NCT99800093"))
    for key in NEW_FIELDS:
        rec.pop(key, None)
    sg = extract_demographics(rec)["sex_gender"]
    assert (sg["female_share"], sg["composition_withheld_reason"]) == (29 / 50, None)


_RACE_OVER = ("Race (NIH/OMB)", [("White", "80"), ("Black or African American", "40")])


def test_a_weekly_race_table_above_the_tables_count_is_withheld_as_raw() -> None:
    # On 03fd911 White 80 and Black 40 of 100, with no sex table, were withheld on the raw path and published
    # as omb_compatible shares of 120 on the weekly path, which had no baseline count to compare with.
    study = fx.make_raw_ctgov_study("NCT99800094", measures=[fx.make_measure(*_RACE_OVER)])
    raw, weekly = _both(study)
    for demo in (raw, weekly):
        assert (demo["race"]["table_status"], demo["race"]["status_detail"]) == (
            "non_standard_categories_present", "table_total_exceeds_baseline_denominator")


def test_after_a_withheld_first_class_a_weekly_race_table_is_bounded_by_the_tables_count() -> None:
    # The first age band's counts are not the trial's, so on the raw path they do not bound the race table,
    # which its 150 does (160 exceeds it). On 03fd911 the weekly path compared it with the band's 50.
    race = fx.make_measure("Race (NIH/OMB)", [("White", "100"), ("Black or African American", "60")])
    raw, weekly = _both(_age_band_study("NCT99800095", extra=[race]))
    for demo in (raw, weekly):
        assert demo["sex_gender"]["composition_withheld_reason"] == MULTICLASS
        assert demo["race"]["status_detail"] == "table_total_exceeds_baseline_denominator"


def test_a_weekly_build_counts_and_checks_a_first_class_composition(tmp_path: Path) -> None:
    # On 03fd911 the weekly build published the first age band's share and the metric was 0.
    studies = [*fx.build_fictional_cohort(), _age_band_study("NCT99800096")]
    doc = {"extracted_at": fx.WEEKLY_EXTRACTED_AT, "pipeline_commit": fx.WEEKLY_PIPELINE_COMMIT,
           "data": [_weekly(s) for s in studies]}
    weekly_path = tmp_path / "weekly.json.gz"
    weekly_path.write_bytes(gzip.compress(json.dumps(doc).encode("utf-8"), mtime=0))
    out = tmp_path / "out"
    assert cli_build.main([
        "--demographics", str(weekly_path), "--snapshot-date", "2026-11-27",
        "--sponsor-rules", str(fx.write_fictional_sponsor_rules(tmp_path / "aliases.csv")),
        "--generated-at", "2026-11-27T00:00:00+00:00", "--out", str(out), *HOLDOUT_ARGS,
    ]) == 0
    loaded = artifacts.load_dir(out)
    assert artifacts.check_contract(loaded) == []
    metric = next(m for m in loaded["summary.json"]["metrics"]
                  if m["id"] == "sex_gender_composition_withheld_whole_population_unread")
    assert metric["value"] == 1
    sg = next(r for r in loaded["trial_rows"] if r["nct_id"] == "NCT99800096")["demographics"]["sex_gender"]
    assert (sg["composition_withheld_reason"], sg["female_share"]) == (MULTICLASS, None)


# ── Loading weekly records that carry the fields ───────────────────────────────────────────────────


def _load(doc: dict[str, Any], tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "demographics.json.gz"
    path.write_bytes(gzip.compress(json.dumps(doc).encode("utf-8"), mtime=0))
    return benchmarks.load_study_records(path, source_kind="full_records")[0]


def _with_fields(rec: dict[str, Any], count: int | None = 100, total: float = 100.0) -> dict[str, Any]:
    """The fixture's record (its row: Female 50, Male 50 of 100) with the producer's two fields."""
    rec["baseline_participants"] = {"count": count, "absent_reason": None if count is not None else
                                    "no_participants_denominator"}
    rec["sex_primary_measure"] = {"flag_multiclass_timepoint": False, "n_total_parsed": total, "absent_reason": None}
    return rec


def _race_and_balanced_ethnicity_at_110(rec: dict[str, Any], flag: str | None = None) -> None:
    """Race unbalanced at 110 (White 70), ethnicity balanced to 110 (unknown 15 beside rows of 5), no sex
    block: a record the pairwise rule accepts, since no other block is balanced and race is at least 110."""
    race, eth = rec["race"], rec["ethnicity"]
    next(e for e in race["raw_categories"] if e["omb_category"] == "white")["count"] = 70
    race["omb_totals"]["white"] = 70
    eth["flags"] = ["denominator_balanced"]
    eth["omb_totals"]["unknown_not_reported"] = 15
    rec["sex"].update(reported=False, totals={"female": 0, "male": 0, "unknown": 0})
    if flag is not None:
        race["flags"] = [flag]
        eth["flags"] = sorted({*eth["flags"], flag})


def test_a_weekly_block_balanced_away_from_the_tables_count_is_refused(tmp_path: Path) -> None:
    # On 03fd911 a balanced ethnicity block was only bounded by the race block's 110, so its 110 loaded beside
    # a table posting 100 participants.
    doc = fx.build_weekly_full_records_doc()
    _race_and_balanced_ethnicity_at_110(_with_fields(doc["data"][1]))
    problem = "ethnicity is balanced to 110, not to the baseline table's Participants count 100"
    with pytest.raises(ValueError, match=re.escape(f"(first: NCT99000402 {problem}")):
        _load(doc, tmp_path)


def test_a_weekly_block_below_the_tables_count_is_refused(tmp_path: Path) -> None:
    # On 03fd911 a record with no balanced block was not checked at all: race 100 of a table posting 110.
    doc = fx.build_weekly_full_records_doc()
    _with_fields(doc["data"][1], count=110)
    problem = "race sums to 100, below the baseline table's Participants count 110"
    with pytest.raises(ValueError, match=re.escape(f"(first: NCT99000402 {problem}")):
        _load(doc, tmp_path)


def test_weekly_records_with_and_without_the_fields_load(tmp_path: Path) -> None:
    # Control, on both heads: the fixture's blocks sum to its 100, and a record without the fields is read
    # as before (records 1, 3 and 5 here).
    doc = fx.build_weekly_full_records_doc()
    _with_fields(doc["data"][1])
    _with_fields(doc["data"][3], count=None)
    assert len(_load(doc, tmp_path)) == 5


def test_blocks_read_through_a_baseline_exception_are_not_pinned_to_the_posted_count(tmp_path: Path) -> None:
    # Control, on both heads: NCT07465926's blocks are balanced to the exception's 187,700, its posted table
    # counts 224,048 (src/baseline_overrides.py), so only the pairwise rule applies to them.
    doc = fx.build_weekly_full_records_doc()
    _race_and_balanced_ethnicity_at_110(_with_fields(doc["data"][1]), flag="baseline_override")
    assert len(_load(doc, tmp_path)) == 5


def _set(path: tuple[str, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def change(rec: dict[str, Any]) -> None:
        target = rec
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return change


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        # On 03fd911 each loaded and was ignored, as a record without the fields is.
        (lambda rec: rec.pop("sex_primary_measure"),
         "carries baseline_participants without sex_primary_measure (src/extract_all.py writes both)"),
        (_set(("baseline_participants",), 100),
         "baseline_participants is not an object of exactly count, absent_reason"),
        (_set(("baseline_participants", "count"), "100"),
         "baseline_participants.count is '100', not a non-negative integer or null"),
        (_set(("baseline_participants", "count"), -1),
         "baseline_participants.count is -1, not a non-negative integer or null"),
        (_set(("baseline_participants", "absent_reason"), "no_baseline_module"),
         "baseline_participants has count 100 beside absent_reason 'no_baseline_module'"),
        (_set(("baseline_participants", "count"), None),
         "baseline_participants has no count and absent_reason None, not one of"),
        (_set(("sex_primary_measure", "flag_multiclass_timepoint"), "true"),
         "sex_primary_measure.flag_multiclass_timepoint is str, not bool"),
        (_set(("sex_primary_measure", "n_total_parsed"), float("inf")),
         "sex_primary_measure.n_total_parsed is inf, not a finite non-negative number"),
        (_set(("sex_primary_measure", "absent_reason"), "corrupt"),
         "sex_primary_measure.absent_reason 'corrupt' is not one of"),
        (lambda rec: rec["sex_primary_measure"].update(absent_reason="no_sex_gender_measure"),
         "sex_primary_measure has absent_reason 'no_sex_gender_measure' beside a reading"),
        (lambda rec: rec["sex_primary_measure"].update(
            flag_multiclass_timepoint=None, n_total_parsed=None, absent_reason="no_sex_gender_measure"),
         "sex_primary_measure.absent_reason is 'no_sex_gender_measure', but the row is reported"),
        (_set(("sex_primary_measure", "n_total_parsed"), 120.0),
         "sex_primary_measure.n_total_parsed is 120, outside the 100 to 100 its sex_gender row allows"),
    ],
)
def test_a_malformed_weekly_baseline_field_is_refused(
    tmp_path: Path, change: Callable[[dict[str, Any]], None], problem: str
) -> None:
    doc = fx.build_weekly_full_records_doc()
    change(_with_fields(doc["data"][1]))
    with pytest.raises(ValueError, match=re.escape(f"(first: NCT99000402 {problem}")):
        _load(copy.deepcopy(doc), tmp_path)
