"""dashboard-summary.json's firstView block: the Overview as it opens.

The site's desktop Overview opens on study type Interventional, results from
2009 on, every other filter at All; every other key of the summary counts all
study types. firstView (src/first_view.py, written last by
scripts/generate_mobile_data.py from the full records) holds that view's
numbers, so the site can paint the Overview from the summary before the study
records arrive. These tests pin:

- the rules, case by case, where they part from the obvious Python: the
  site's JavaScript truthiness for reported, ASCII digits for the results
  year, the study type compared strictly, the newest results year taken over
  every study type, and what the view leaves out counted in not_counted;
- the generator writes the block last, with the summary's own stamps, and
  stops on a record the site's code would throw on;
- parity: scripts/first_view_parity.mjs runs the site's own Overview code
  (tests/fixtures/site_overview/, an excerpt of the site's app.js and
  index.html copied unchanged) over the same records and finds every number
  of the block equal to what the site computes and paints, on records that
  carry every edge the rules decide, in files written as the weekly job
  writes them and compactly;
- the parity check fails when the block is wrong: a count moved between
  years, another filter, another run's stamps, or the block counted with
  Python's truthiness or Python's idea of a digit.

The parity tests run node; the CI job sets node up before the Python suite.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "tests", ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import split_helpers as h  # noqa: E402
from src import first_view as fv  # noqa: E402

PARITY = ROOT / "scripts" / "first_view_parity.mjs"
EXCERPT = ROOT / "tests" / "fixtures" / "site_overview"
DELETE = object()      # a change that removes the key

SUMMARY_KEYS = ["extracted_at", "pipeline_commit", "totalStudies", "cards", "raceDistribution",
                "ethnicityDistribution", "sexDistribution", "genderDistribution", "raceSubcategories",
                "ethnicitySubcategories", "byYear", "fda", "recentStudies", "sexGender"]


# ── the rules ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,truthy", [
    (None, False), (False, False), (True, True), (0, False), (0.0, False), (-0.0, False),
    (float("nan"), False), (1, True), (-1, True), (0.5, True), ("", False), ("0", True),
    ("false", True), ([], True), ({}, True), ([0], True), ({"reported": False}, True),
])
def test_reported_is_read_with_the_sites_javascript_truthiness(value: Any, truthy: bool) -> None:
    assert fv.js_truthy(value) is truthy
    assert fv.reports({"race": {"reported": value}}, "race") is truthy


@pytest.mark.parametrize("block", [DELETE, None, "reported", [], [{"reported": True}], 1, True, {}])
def test_a_missing_or_malformed_dimension_block_does_not_report(block: Any) -> None:
    """The site's race?.reported is undefined unless race is an object with the member."""
    record: dict[str, Any] = {} if block is DELETE else {"race": block}
    assert fv.reports(record, "race") is False


@pytest.mark.parametrize("results_date,key", [
    (DELETE, None), (None, None), ("", None), ("2024-05-01", "2024"), ("2009", "2009"),
    ("20150101", "2015"), ("201", None), ("abcd-01-01", None), (" 2015-01-01", None),
    ("+2015-01-01", None), ("0999-01-01", "0999"),
    ("\uff12\uff10\uff12\uff14-01-01", None),     # fullwidth digits: Python's isdigit and int take them
    ("\u0662\u0660\u0662\u0664-01-01", None),     # Arabic-Indic digits: so do these; JavaScript's parseInt does not
])
def test_the_results_year_is_four_ascii_digits(results_date: Any, key: str | None) -> None:
    record: dict[str, Any] = {} if results_date is DELETE else {"results_date": results_date}
    assert fv.results_year(record) == key


def _study(nct: str, results_date: Any = "2020-03-01", study_type: Any = "INTERVENTIONAL",
           race: Any = True, ethnicity: Any = False) -> dict[str, Any]:
    return {"nct_id": nct, "results_date": results_date, "study_type": study_type,
            "race": {"reported": race}, "ethnicity": {"reported": ethnicity}}


def test_the_block_counts_the_default_view_and_says_what_it_left_out() -> None:
    records = [
        _study("NCT00000001", "2009-01-05", race=True, ethnicity=True),
        _study("NCT00000002", "2009-11-30", race=True, ethnicity=False),
        _study("NCT00000003", "2010-02-01", race=False, ethnicity=True),
        _study("NCT00000004", "2010-02-01", race=False, ethnicity=False),
        _study("NCT00000005", "2026-07-01", race=True, ethnicity=True),
        _study("NCT00000006", "2027-01-15", study_type="OBSERVATIONAL"),   # the newest year, any type
        _study("NCT00000007", "2026-07-01", study_type=None),
        _study("NCT00000008", None),
        _study("NCT00000009", ""),
        _study("NCT00000010", "2008-12-31"),
        _study("NCT00000011", "n/a"),
    ]
    block = fv.first_view(records, "2026-10-04T12:09:21.454200+00:00", "d853532")
    assert block["extracted_at"] == "2026-10-04T12:09:21.454200+00:00" and block["pipeline_commit"] == "d853532"
    assert block["filter"] == {"study_type": "INTERVENTIONAL", "results_year_from": 2009,
                               "results_year_to": None, "other_filters": "none"}
    assert block["newest_results_year"] == 2027
    assert (block["trials"], block["trials_reporting_race"], block["trials_reporting_ethnicity"],
            block["trials_reporting_race_and_ethnicity"]) == (5, 3, 3, 2)
    assert block["by_results_year"] == {
        "2009": {"trials": 2, "trials_reporting_race": 2, "trials_reporting_ethnicity": 1,
                 "trials_reporting_race_and_ethnicity": 1},
        "2010": {"trials": 2, "trials_reporting_race": 0, "trials_reporting_ethnicity": 1,
                 "trials_reporting_race_and_ethnicity": 0},
        "2026": {"trials": 1, "trials_reporting_race": 1, "trials_reporting_ethnicity": 1,
                 "trials_reporting_race_and_ethnicity": 1},
    }
    assert list(block["by_results_year"]) == sorted(block["by_results_year"])
    assert "2027" not in block["by_results_year"], "a year with no interventional trial is no point on the chart"
    assert block["not_counted"] == {"no_results_date": 2, "results_year_not_from_2009": 2, "not_interventional": 2}
    assert block["trials"] + sum(block["not_counted"].values()) == len(records)
    assert block["denominators"] == {k: "trials" for k in fv.NUMERATORS}
    assert block["about"] and all(isinstance(s, str) for s in block["about"])


def test_with_no_results_year_from_2009_the_newest_year_is_null() -> None:
    block = fv.first_view([_study("NCT00000001", "2008-05-01"), _study("NCT00000002", None)], None, None)
    assert block["newest_results_year"] is None
    assert block["trials"] == 0 and block["by_results_year"] == {}
    assert block["not_counted"] == {"no_results_date": 1, "results_year_not_from_2009": 1, "not_interventional": 0}


@pytest.mark.parametrize("results_date", [2024, 2024.0, True, False, 0, ["2024-01-01"], {"date": "2024-01-01"}])
def test_a_results_date_the_site_cannot_read_stops_the_block(results_date: Any) -> None:
    """app.js calls results_date?.substring: anything but a string or null throws there."""
    records = [_study("NCT00000001"), _study("NCT00000002", results_date)]
    with pytest.raises(fv.FirstViewError, match=r"record 2 \(NCT00000002\): results_date is a"):
        fv.first_view(records, None, None)


@pytest.mark.parametrize("record", [None, "NCT00000001", 7, []])
def test_a_record_that_is_not_an_object_stops_the_block(record: Any) -> None:
    with pytest.raises(fv.FirstViewError, match="record 1 is a"):
        fv.first_view([record], None, None)


# ── fixture records, files and runs ─────────────────────────────────────────

PATTERNS = [(True, True), (True, False), (False, True), (False, False), (True, True), (True, False)]

# One record for each edge the rules decide, on top of an ordinary counted
# record (results 2021, interventional, race reported, ethnicity not).
EDGES: list[dict[str, Any]] = [
    {"results_date": None}, {"results_date": ""}, {"results_date": DELETE},
    {"results_date": "2008-12-31"}, {"results_date": "abcd-01-01"}, {"results_date": "201"},
    {"results_date": " 2015-01-01"}, {"results_date": "0999-01-01"},
    {"results_date": "\uff12\uff10\uff12\uff14-01-01"},
    {"results_date": "20150101"}, {"results_date": "2016"},
    {"results_date": "2027-03-01", "study_type": "OBSERVATIONAL"},      # the newest year is not interventional
    {"study_type": None}, {"study_type": DELETE}, {"study_type": "interventional"},
    {"study_type": "EXPANDED_ACCESS"},
    # (A race block that is a non-empty string or list is in the unit tests only:
    # the summary's older keys already stop on one, before the block is counted.)
    {"race": DELETE}, {"race": None}, {"race": []}, {"race": {}},
    {"race": {"reported": None}}, {"race": {"reported": []}}, {"race": {"reported": {}}},
    {"race": {"reported": "false"}}, {"race": {"reported": ""}}, {"race": {"reported": 0}},
    {"race": {"reported": 1}},
    {"ethnicity": {"reported": []}}, {"ethnicity": DELETE}, {"ethnicity": {"reported": "0"}},
    # brackets, braces, commas, quotes and backslashes inside strings, for the item reader
    {"brief_title": 'He said "[x]" {y}, \\ then ] } , [ {', "official_title": "\\\"]},"},
]


def _records(n_counted: int) -> list[dict[str, Any]]:
    """Whole records (the extractor's own output, h.whole_records): n_counted in the
    default view across 2009-2026 with a mix of reporting, then one per edge."""
    base = h.whole_records(h.ids(4))
    out: list[dict[str, Any]] = []

    def add(changes: dict[str, Any]) -> None:
        r = copy.deepcopy(base[len(out) % len(base)])
        r["nct_id"] = f"NCT{30000000 + len(out):08d}"
        for key, value in changes.items():
            if value is DELETE:
                r.pop(key, None)
            else:
                r[key] = value
        out.append(r)

    for i in range(n_counted):
        race, eth = PATTERNS[(i + i // 18) % len(PATTERNS)]
        b = base[len(out) % len(base)]
        add({"results_date": f"{2009 + (i * 5) % 18}-{1 + i % 12:02d}-15", "study_type": "INTERVENTIONAL",
             "race": dict(b["race"], reported=race), "ethnicity": dict(b["ethnicity"], reported=eth)})
    for edge in EDGES:
        b = base[len(out) % len(base)]
        add({"results_date": "2021-06-01", "study_type": "INTERVENTIONAL",
             "race": dict(b["race"], reported=True), "ethnicity": dict(b["ethnicity"], reported=False), **edge})
    return out


def _write_compact(path: pathlib.Path, records: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps({"data": records, **h.STAMPS}, separators=(",", ":"), ensure_ascii=False),
                    encoding="utf-8")


def _summarise(tmp: pathlib.Path, records_file: pathlib.Path) -> pathlib.Path:
    """scripts/generate_mobile_data.py on the full records, as the weekly job runs it."""
    out = tmp / "dashboard-summary.json"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_mobile_data.py"),
                        "--demographics", str(records_file), "--table", str(tmp / "absent.csv.gz"),
                        "--out", str(out)], cwd=tmp, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]
    return out


def _node() -> str:
    node = shutil.which("node")
    if not node:
        pytest.fail("node is not on PATH: the first-view parity runs the site's own JavaScript "
                    "(CI sets node up before the Python suite)")
    return node


def _parity(records_file: pathlib.Path, summary_file: pathlib.Path, *extra: str) -> tuple[int, dict[str, Any]]:
    r = subprocess.run([_node(), str(PARITY), "--records", str(records_file), "--summary", str(summary_file), *extra],
                       capture_output=True, text=True)
    assert r.returncode in (0, 1), f"the parity check could not run:\n{r.stderr[-3000:]}"
    return r.returncode, json.loads(r.stdout)


def _case(tmp: pathlib.Path, records: list[dict[str, Any]], compact: bool = False) -> tuple[pathlib.Path, pathlib.Path]:
    tmp.mkdir(parents=True, exist_ok=True)
    if compact:
        records_file = tmp / "demographics.json"
        _write_compact(records_file, records)
    else:
        records_file = tmp / "demographics.json.gz"
        h.write_full(records_file, records)              # indent=2, gzipped: the release's form
    return records_file, _summarise(tmp, records_file)


@pytest.fixture(scope="module")
def counted(tmp_path_factory: pytest.TempPathFactory) -> tuple[pathlib.Path, pathlib.Path, list[dict[str, Any]]]:
    """The main case: over 100 trials in the view, every edge, written as the release is."""
    records = _records(120)
    records_file, summary_file = _case(tmp_path_factory.mktemp("first_view"), records)
    return records_file, summary_file, records


# ── the generator ───────────────────────────────────────────────────────────

def test_the_generator_writes_the_block_last_with_the_summarys_stamps(counted: tuple[pathlib.Path, pathlib.Path, list[dict[str, Any]]]) -> None:
    _, summary_file, records = counted
    summary = json.loads(summary_file.read_text())
    assert list(summary) == SUMMARY_KEYS + ["firstView"], "the existing keys keep their order; firstView comes last"
    block = summary["firstView"]
    assert (block["extracted_at"], block["pipeline_commit"]) == (summary["extracted_at"], summary["pipeline_commit"]) \
        == (h.STAMPS["extracted_at"], h.STAMPS["pipeline_commit"])
    assert block == json.loads(json.dumps(fv.first_view(records, h.STAMPS["extracted_at"], h.STAMPS["pipeline_commit"])))
    assert block["trials"] + sum(block["not_counted"].values()) == summary["totalStudies"] == len(records)
    assert block["newest_results_year"] == 2027
    assert block["not_counted"] == {"no_results_date": 3, "results_year_not_from_2009": 6, "not_interventional": 5}


def test_the_generator_stops_on_a_results_date_the_site_cannot_read(tmp_path: pathlib.Path) -> None:
    records = _records(4)
    records[2]["results_date"] = 20200301
    records_file = tmp_path / "demographics.json"
    h.write_full(records_file, records)
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_mobile_data.py"),
                        "--demographics", str(records_file), "--table", str(tmp_path / "absent.csv.gz"),
                        "--out", str(tmp_path / "summary.json")], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode != 0
    assert "results_date is a int" in r.stderr
    assert not (tmp_path / "summary.json").exists()


# ── parity with the site's own code ─────────────────────────────────────────

def _assert_parity(code: int, report: dict[str, Any], block: dict[str, Any]) -> None:
    assert code == 0 and report["ok"], json.dumps(report["mismatches"], indent=1)[:4000]
    site = report["site"]
    for k in ["trials", *fv.NUMERATORS]:
        assert site[k] == block[k]
    assert site["newest_results_year"] == block["newest_results_year"]
    for year, c in block["by_results_year"].items():
        assert site["by_results_year"][year] == c
    # 40 checks of the view, and one for each year it shows
    assert report["checked"] == 40 + len(block["by_results_year"])


def test_the_block_is_what_the_sites_own_code_counts_and_paints(counted: tuple[pathlib.Path, pathlib.Path, list[dict[str, Any]]]) -> None:
    records_file, summary_file, _ = counted
    block = json.loads(summary_file.read_text())["firstView"]
    code, report = _parity(records_file, summary_file)
    _assert_parity(code, report, block)
    assert report["site_code"].startswith("michaeldgreenphd/clinical-trial-populations@")
    painted = report["site"]["painted"]
    assert "</strong> of these trials report race" in painted["finding-headline"]
    assert "-point gap" in painted["finding-context"]
    assert painted["stat-sub-total"] == "results posted 2009\u20132027", "the window ends at the newest year of any study type"
    assert "<b>interventional</b>" in painted["filter-summary-text"]


@pytest.mark.parametrize("case,finding", [
    ("under_100_trials", "Too few trials to state a rate"),
    ("no_trials", "Widen the year range or clear a filter"),
    ("no_results_year_from_2009", "Widen the year range or clear a filter"),
    ("compact_plain_file", "-point gap"),
])
def test_parity_holds_on_every_branch_the_overview_paints(tmp_path: pathlib.Path, case: str, finding: str) -> None:
    builders: dict[str, Callable[[], list[dict[str, Any]]]] = {
        "under_100_trials": lambda: _records(20),
        "no_trials": lambda: [dict(r, study_type="OBSERVATIONAL") for r in _records(30)],
        "no_results_year_from_2009": lambda: [dict(r, results_date="2008-01-01") for r in _records(10)],
        "compact_plain_file": lambda: _records(120),
    }
    records_file, summary_file = _case(tmp_path, builders[case](), compact=case == "compact_plain_file")
    block = json.loads(summary_file.read_text())["firstView"]
    code, report = _parity(records_file, summary_file)
    assert code == 0 and report["ok"], json.dumps(report["mismatches"], indent=1)[:4000]
    for k in ["trials", *fv.NUMERATORS]:
        assert report["site"][k] == block[k]
    assert finding in report["site"]["painted"]["finding-context"]
    if case == "no_results_year_from_2009":
        assert block["newest_results_year"] is None and report["site"]["newest_results_year"] is None


# ── the parity check fails when the block is wrong ──────────────────────────

def _mutated(tmp: pathlib.Path, summary_file: pathlib.Path, change: Callable[[dict[str, Any]], None]) -> pathlib.Path:
    summary = json.loads(summary_file.read_text())
    change(summary)
    out = tmp / "dashboard-summary.json"
    out.write_text(json.dumps(summary, separators=(",", ":")))
    return out


def _move_one_race_trial(s: dict[str, Any]) -> None:
    """2016's race count down one and 2017's up one: totals and sums still agree."""
    years = s["firstView"]["by_results_year"]
    years["2016"]["trials_reporting_race"] -= 1
    years["2017"]["trials_reporting_race"] += 1


@pytest.mark.parametrize("change,expected", [
    (_move_one_race_trial, {"counts in 2016", "counts in 2017", "painted: the trend chart's Race series"}),
    (lambda s: s["firstView"]["filter"].update(study_type="OBSERVATIONAL"), {"filter: study type"}),
    (lambda s: s["firstView"]["filter"].update(results_year_to=2026), {"filter: the year window"}),
    (lambda s: s["firstView"]["filter"].update(other_filters="sponsor INDUSTRY"),
     {"filter: every other control is at no filter"}),
    (lambda s: s["firstView"].update(newest_results_year=2026),
     {"filter: the newest results year", "filter: the slider shows results_year_from to newest_results_year"}),
    (lambda s: s["firstView"].update(extracted_at="2026-09-27T11:49:53+00:00"),
     {"stamps: the records, the block and the summary are one run"}),
    (lambda s: s["firstView"]["not_counted"].update(no_results_date=0, not_interventional=8),
     {"absence: no results date", "absence: not interventional, by the site's filter with study type All"}),
])
def test_the_parity_check_names_what_the_block_gets_wrong(tmp_path: pathlib.Path, counted: tuple[pathlib.Path, pathlib.Path, list[dict[str, Any]]],
                                                          change: Callable[[dict[str, Any]], None], expected: set[str]) -> None:
    records_file, summary_file, _ = counted
    code, report = _parity(records_file, _mutated(tmp_path, summary_file, change))
    assert code == 1 and not report["ok"]
    assert {m["check"] for m in report["mismatches"]} == expected


def test_the_fixture_catches_the_block_counted_with_pythons_truthiness(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
                                                                       counted: tuple[pathlib.Path, pathlib.Path, list[dict[str, Any]]]) -> None:
    """reported: [] or {} or "false" counts on the site; Python's bool says no."""
    records_file, summary_file, records = counted
    monkeypatch.setattr(fv, "js_truthy", bool)
    block = json.loads(json.dumps(fv.first_view(records, h.STAMPS["extracted_at"], h.STAMPS["pipeline_commit"])))
    code, report = _parity(records_file, _mutated(tmp_path, summary_file, lambda s: s.update(firstView=block)))
    assert code == 1
    failed = {m["check"] for m in report["mismatches"]}
    assert {"counts: trials_reporting_race", "counts: trials_reporting_ethnicity", "counts in 2021"} <= failed


def test_the_fixture_catches_the_block_counted_with_pythons_digits(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
                                                                   counted: tuple[pathlib.Path, pathlib.Path, list[dict[str, Any]]]) -> None:
    """Fullwidth digits are digits to str.isdigit and int(); parseInt reads NaN."""
    records_file, summary_file, records = counted

    def unicode_year(record: dict[str, Any], where: str = "") -> str | None:
        head = (record.get("results_date") or "")[:4]
        return str(int(head)) if len(head) == 4 and head.isdigit() else None

    monkeypatch.setattr(fv, "results_year", unicode_year)
    block = json.loads(json.dumps(fv.first_view(records, h.STAMPS["extracted_at"], h.STAMPS["pipeline_commit"])))
    code, report = _parity(records_file, _mutated(tmp_path, summary_file, lambda s: s.update(firstView=block)))
    assert code == 1
    failed = {m["check"] for m in report["mismatches"]}
    assert {"counts: trials", "counts in 2024"} <= failed


# ── the vendored site code ──────────────────────────────────────────────────

def test_the_vendored_excerpt_is_the_sites_code_unchanged(tmp_path: pathlib.Path) -> None:
    """The excerpt is the pieces the check slices, written by its --excerpt mode and
    nothing else: writing it again from itself gives the same bytes. Its first line
    names the site commit it was copied from."""
    app = (EXCERPT / "app_overview.js").read_text(encoding="utf-8")
    source = re.search(r"^// Copied unchanged from (.+?):$", app, re.M)
    assert source and re.fullmatch(r"michaeldgreenphd/clinical-trial-populations@[0-9a-f]{40}", source.group(1))
    r = subprocess.run([_node(), str(PARITY), "--excerpt", "--app", str(EXCERPT / "app_overview.js"),
                        "--index", str(EXCERPT / "index_overview.html"), "--source", source.group(1),
                        "--out-dir", str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    for name in ("app_overview.js", "index_overview.html"):
        assert (tmp_path / name).read_bytes() == (EXCERPT / name).read_bytes(), name


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pins() -> dict[str, Any]:
    return json.loads((EXCERPT / "SOURCE.json").read_text(encoding="utf-8"))


def test_the_vendored_excerpt_is_what_its_pins_say(tmp_path: pathlib.Path) -> None:
    """SOURCE.json, written by --excerpt with the excerpt, pins the excerpt's bytes:
    an edit to either file, however it keeps the pieces sliceable, fails here
    unless SOURCE.json is changed too, in plain sight."""
    pins = _pins()
    header = re.search(r"^// Copied unchanged from (.+?):$", (EXCERPT / "app_overview.js").read_text(encoding="utf-8"), re.M)
    assert header and header.group(1) == pins["source"], "the excerpt and SOURCE.json name different site commits"
    for name, digest in pins["excerpt"].items():
        assert _sha256((EXCERPT / name).read_bytes()) == digest, f"{name} is not the excerpt SOURCE.json pins"


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as resp:      # noqa: S310 (a fixed https host)
        return resp.read()


def test_the_vendored_excerpt_is_the_site_at_the_commit_it_names(tmp_path: pathlib.Path) -> None:
    """The authoritative check: the site's own app.js and index.html at the pinned
    commit (the site repository is public) have the pinned digests, and --excerpt
    run on them writes the vendored excerpt byte for byte. CI must reach GitHub;
    offline, outside CI, the test is skipped."""
    pins = _pins()
    repo, commit = pins["source"].split("@")
    assert re.fullmatch(r"michaeldgreenphd/clinical-trial-populations", repo) and re.fullmatch(r"[0-9a-f]{40}", commit)
    site = tmp_path / "site"
    site.mkdir()
    for name in ("app.js", "index.html"):
        try:
            data = _fetch(f"https://raw.githubusercontent.com/{repo}/{commit}/{name}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if os.environ.get("CI"):
                pytest.fail(f"cannot fetch the site's {name} at {commit} to check the excerpt: {e}")
            pytest.skip(f"offline: cannot fetch the site's {name} at {commit} ({e})")
        assert _sha256(data) == pins["site_files"][name], f"the site's {name} at {commit} is not the file SOURCE.json pins"
        (site / name).write_bytes(data)
    out = tmp_path / "excerpt"
    r = subprocess.run([_node(), str(PARITY), "--excerpt", "--app", str(site / "app.js"), "--index", str(site / "index.html"),
                        "--source", pins["source"], "--out-dir", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    for name in ("app_overview.js", "index_overview.html", "SOURCE.json"):
        assert (out / name).read_bytes() == (EXCERPT / name).read_bytes(), f"{name} is not what the site's commit gives"


def test_the_check_reads_a_weeks_file_item_by_item(tmp_path: pathlib.Path, counted: tuple[pathlib.Path, pathlib.Path, list[dict[str, Any]]]) -> None:
    """A week's full records are ~1.3 GB, over V8's string limit: the check never
    holds the file as one string, and reads the same records either way it is written."""
    _, summary_file, records = counted
    reordered = tmp_path / "demographics.json.gz"
    with gzip.open(reordered, "wt", encoding="utf-8") as f:     # data first, stamps after, other members around it
        json.dump({"note": ["[", "{"], "data": records, **h.STAMPS, "tail": {"data": []}}, f, indent=1)
    code, report = _parity(reordered, summary_file)
    assert code == 0 and report["ok"], json.dumps(report["mismatches"], indent=1)[:2000]
    assert report["records"]["count"] == len(records)
    assert (report["records"]["extracted_at"], report["records"]["pipeline_commit"]) == \
        (h.STAMPS["extracted_at"], h.STAMPS["pipeline_commit"])


# ── the records cut to what the block reads (the publish gate's recount) ────

def test_the_cut_records_count_as_the_whole_ones(counted: tuple[pathlib.Path, pathlib.Path, list[dict[str, Any]]]) -> None:
    """scripts/check_site_contract.py holds a week's records cut to fv.READS:
    every edge the rules decide counts the same cut as whole."""
    _, _, records = counted
    cut = [fv.essentials(r) for r in records]
    assert fv.first_view(cut, "a", "b") == fv.first_view(records, "a", "b")
    assert all(set(c) <= {p.split(".")[0] for p in fv.READS} for c in cut)


@pytest.mark.parametrize("record,cut", [
    ({"nct_id": "NCT1", "results_date": None, "study_type": "X", "phase": "P1"},
     {"nct_id": "NCT1", "results_date": None, "study_type": "X"}),
    ({}, {}),
    ({"race": {"reported": [], "omb_totals": {}}, "ethnicity": {"omb_totals": {}}},
     {"race": {"reported": []}, "ethnicity": {}}),
    ({"race": "reported", "ethnicity": [{"reported": True}]}, {"race": "reported", "ethnicity": [{"reported": True}]}),
    ({"race": None}, {"race": None}),
])
def test_the_cut_keeps_absence_and_shape(record: dict[str, Any], cut: dict[str, Any]) -> None:
    assert fv.essentials(record) == cut
    assert fv.reports(fv.essentials(record), "race") is fv.reports(record, "race")
    assert fv.reports(fv.essentials(record), "ethnicity") is fv.reports(record, "ethnicity")


@pytest.mark.parametrize("record", [None, "NCT00000001", 7, []])
def test_a_record_that_is_not_an_object_is_cut_to_itself_and_still_refused(record: Any) -> None:
    assert fv.essentials(record) is record
    with pytest.raises(fv.FirstViewError, match="record 1 is a"):
        fv.first_view([fv.essentials(record)], None, None)


# ── the staged parts as the records (the weekly publish) ────────────────────

def _write_parts(folder: pathlib.Path, records: list[dict[str, Any]], n: int,
                 stamps: list[dict[str, Any]] | None = None) -> list[pathlib.Path]:
    """The records as the site's parts: n gzipped containers, each with the
    run's stamps and its own data array (scripts/split_data.py's shape)."""
    folder.mkdir(parents=True, exist_ok=True)
    size = -(-len(records) // n)
    paths = []
    for k in range(n):
        path = folder / f"demographics.part{k + 1}.json.gz"
        body = {**((stamps or [])[k] if stamps else h.STAMPS), "part": k + 1, "total_parts": n,
                "data": records[k * size:(k + 1) * size]}
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(body, f, separators=(",", ":"))
        paths.append(path)
    return paths


def test_the_parity_reads_the_staged_parts_as_one_set_of_records(
        tmp_path: pathlib.Path, counted: tuple[pathlib.Path, pathlib.Path, list[dict[str, Any]]]) -> None:
    records_file, summary_file, records = counted
    parts = _write_parts(tmp_path, records, 4)
    r = subprocess.run([_node(), str(PARITY), *[a for p in parts for a in ("--records", str(p))],
                        "--summary", str(summary_file)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    report = json.loads(r.stdout)
    block = json.loads(summary_file.read_text())["firstView"]
    _assert_parity(r.returncode, report, block)
    assert report["records"]["file"] == [str(p) for p in parts] and report["records"]["count"] == len(records)
    # One file is still reported as one path, as before.
    _, one = _parity(records_file, summary_file)
    assert one["records"]["file"] == str(records_file)


def test_the_parity_on_parts_still_finds_a_wrong_block(
        tmp_path: pathlib.Path, counted: tuple[pathlib.Path, pathlib.Path, list[dict[str, Any]]]) -> None:
    _, summary_file, records = counted
    parts = _write_parts(tmp_path / "parts", records, 3)
    wrong = _mutated(tmp_path, summary_file, _move_one_race_trial)
    r = subprocess.run([_node(), str(PARITY), *[a for p in parts for a in ("--records", str(p))],
                        "--summary", str(wrong)], capture_output=True, text=True)
    assert r.returncode == 1
    assert {m["check"] for m in json.loads(r.stdout)["mismatches"]} == \
        {"counts in 2016", "counts in 2017", "painted: the trend chart's Race series"}


def test_parts_from_two_runs_are_an_input_error(tmp_path: pathlib.Path,
                                                counted: tuple[pathlib.Path, pathlib.Path, list[dict[str, Any]]]) -> None:
    _, summary_file, records = counted
    other = {**h.STAMPS, "pipeline_commit": "abc1234"}
    parts = _write_parts(tmp_path, records, 2, stamps=[h.STAMPS, other])
    r = subprocess.run([_node(), str(PARITY), "--records", str(parts[0]), "--records", str(parts[1]),
                        "--summary", str(summary_file)], capture_output=True, text=True)
    assert r.returncode == 2 and r.stdout == ""
    assert f"first_view_parity: {parts[1]} is from another run" in r.stderr and "abc1234" in r.stderr
