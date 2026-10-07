"""The Overview as it opens, counted for dashboard-summary.json's firstView block.

The site's desktop Overview opens on one view: study type Interventional,
results posted from 2009 with no upper bound (the year slider ends at the
newest results year in the data, and an end thumb left there applies no
bound), every other filter at "All". The site counts that view from the
study records once they have arrived. This module counts the same view from
the week's full records, so the site can paint the Overview from the summary
before the records arrive and phones can open on the same numbers: the engine
computes, the site formats.

Every rule below is the site's own (app.js; function names as of the site
commit the parity excerpt was copied from, tests/fixtures/site_overview/):

- results year (getFilteredData): parseInt(results_date.substring(0, 4)), kept
  when it is 2009 or later. For a string that is exactly "its first four
  characters are ASCII digits reading 2009 or later": anything else reads as
  NaN or as a number under 1000 (a sign, a space or a letter in those four
  characters leaves at most three digits). The year a study is filed under in
  the trend chart (renderReportingTrends) is those same four characters.
- study type (getFilteredData): study_type === 'INTERVENTIONAL', strictly. A
  study with another type, a lower-case one or none is not counted.
- reported (renderDashboard, renderReportingTrends): race?.reported and
  ethnicity?.reported by JavaScript truthiness. A study whose race block is
  missing, null, not an object, or whose reported is false, null, 0 or ""
  counts in trials (the denominator) and not in trials_reporting_race (the
  numerator), as on the site. Truthiness is JavaScript's, not Python's: an
  empty list or object, or the string "false", is truthy there and counts as
  reported here too.
- newest results year (datasetLatestYear): the newest results year among ALL
  the records, any study type, or null when none is 2009 or later. It is where
  the slider ends and the end of the Overview's "results posted 2009-YYYY".

Absence is not zero, and is not hidden either: a study with no results date,
or a results date that does not read as a year from 2009 on, or another study
type, is left out of every count exactly as the site leaves it out, and
not_counted says how many were left out for each reason (in the site's order
of checks: year first, then study type), so trials plus not_counted is every
record in the file.

A results_date that is neither a string nor null, or a record that is not an
object, raises FirstViewError: the site's own code throws on the first and
skips or throws on the second, so there is no number the Overview would show
for them, and the weekly job should stop rather than publish one.

scripts/first_view_parity.mjs runs the site's own filtering and Overview code
over the same records and checks every number of this block against it.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# The site's desktop defaults, from index.html and app.js.
STUDY_TYPE = "INTERVENTIONAL"   # #study-type's selected option
RESULTS_YEAR_FROM = 2009        # app.js YEAR_WINDOW_MIN: #year-start's min and value

# Every record field the block is counted from, as site-contract paths. The
# publish gate (scripts/check_site_contract.py) recounts the block from the
# staged parts, which under the split layout carry the contract's core class
# only: it checks these are all core before it counts.
READS = ("nct_id", "results_date", "study_type", "race.reported", "ethnicity.reported")

DENOMINATOR = "trials"
NOT_FROM = f"results_year_not_from_{RESULTS_YEAR_FROM}"
NUMERATORS = {                  # numerator field -> the dimensions a trial must report
    "trials_reporting_race": ("race",),
    "trials_reporting_ethnicity": ("ethnicity",),
    "trials_reporting_race_and_ethnicity": ("race", "ethnicity"),
}

ABOUT = [
    "The Overview as it opens: what it paints, counted from the week's full records by the site's own rules "
    "(civicsample-engine src/first_view.py). The site formats these counts.",
    "filter: study_type exactly INTERVENTIONAL; results year (the first four characters of results_date) from "
    "results_year_from, with no upper bound (results_year_to null); every other filter at All. "
    "newest_results_year: the newest results year of any study type in the file, where the year slider ends.",
    "Each percentage is a numerator over the trials of its scope, as denominators names: the whole view, or one "
    "year of by_results_year (years with trials only). A trial whose race.reported (ethnicity.reported) is not "
    "set counts in trials and not in the numerator.",
    "not_counted: the file's other studies, by the first rule they fail. trials plus these is totalStudies.",
]


class FirstViewError(ValueError):
    """A record the site's Overview code cannot count (it would throw on it)."""


def js_truthy(value: Any) -> bool:
    """JavaScript truthiness of a JSON value: null, false, 0 and "" are falsy;
    every object and array is truthy, empty or not (unlike Python's bool)."""
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, (int, float)):
        return value == value and value != 0       # NaN and +-0 are falsy
    if isinstance(value, str):
        return value != ""
    return True


def reports(record: dict[str, Any], dimension: str) -> bool:
    """The site's record[dimension]?.reported, by JavaScript truthiness. A block
    that is missing, null or not an object has no reported member (undefined)."""
    block = record.get(dimension)
    return js_truthy(block.get("reported")) if isinstance(block, dict) else False


def results_year(record: dict[str, Any], where: str = "a record") -> str | None:
    """The results year the site files a study under, as its four-digit key, or
    None when results_date is missing, null, or its first four characters are
    not ASCII digits (the site reads those as NaN or as a number under 1000).

    Python slices code points where JavaScript's substring counts UTF-16 units;
    the two agree on whether the first four are ASCII digits, which is all this
    decides."""
    value = record.get("results_date")
    if value is None:
        return None
    if not isinstance(value, str):
        raise FirstViewError(f"{where}: results_date is a {type(value).__name__} ({value!r}), not a date string; "
                             "the site's year filter throws on it, so the Overview has no number to show")
    head = value[:4]
    if len(head) == 4 and all("0" <= ch <= "9" for ch in head):
        return head
    return None


def essentials(record: Any) -> Any:
    """The record cut to what first_view reads (READS), so a whole week's
    records can be held for a recount: first_view counts the cut records
    exactly as it counts the whole ones. A key that is absent stays absent, a
    dimension block that is not an object is kept as it is (the site reads no
    reported member on it), and a record that is not an object is returned
    unchanged, for first_view to refuse."""
    if not isinstance(record, dict):
        return record
    out = {key: record[key] for key in ("nct_id", "results_date", "study_type") if key in record}
    for dim in ("race", "ethnicity"):
        if dim in record:
            block = record[dim]
            out[dim] = ({"reported": block["reported"]} if "reported" in block else {}) \
                if isinstance(block, dict) else block
    return out


def _where(index: int, record: Any) -> str:
    nct = record.get("nct_id") if isinstance(record, dict) else None
    return f"record {index + 1}" + (f" ({nct})" if isinstance(nct, str) else "")


def _counts() -> dict[str, int]:
    return {DENOMINATOR: 0, **{name: 0 for name in NUMERATORS}}


def newest_results_year(records: Iterable[Any]) -> int | None:
    """The site's datasetLatestYear: the newest results year among all the
    records, any study type, or None when none is 2009 or later."""
    newest = 0
    for i, record in enumerate(records):
        if not isinstance(record, dict):
            raise FirstViewError(f"{_where(i, record)} is a {type(record).__name__}, not a study record")
        year = results_year(record, _where(i, record))
        if year is not None:
            newest = max(newest, int(year))
    return newest if newest >= RESULTS_YEAR_FROM else None


def first_view(records: list[Any], extracted_at: str | None, pipeline_commit: str | None) -> dict[str, Any]:
    """The firstView block: the Overview's default view, counted from records.

    extracted_at and pipeline_commit are the run's stamps, the same values the
    summary carries at its top level, so the block can be matched to its run."""
    totals = _counts()
    by_year: dict[str, dict[str, int]] = {}
    not_counted = {"no_results_date": 0, NOT_FROM: 0, "not_interventional": 0}
    for i, record in enumerate(records):
        if not isinstance(record, dict):
            raise FirstViewError(f"{_where(i, record)} is a {type(record).__name__}, not a study record")
        year = results_year(record, _where(i, record))
        if record.get("results_date") in (None, ""):
            not_counted["no_results_date"] += 1
            continue
        if year is None or int(year) < RESULTS_YEAR_FROM:
            not_counted[NOT_FROM] += 1
            continue
        if record.get("study_type") != STUDY_TYPE:
            not_counted["not_interventional"] += 1
            continue
        reported = {dim: reports(record, dim) for dim in ("race", "ethnicity")}
        for bucket in (totals, by_year.setdefault(year, _counts())):
            bucket[DENOMINATOR] += 1
            for name, dims in NUMERATORS.items():
                if all(reported[d] for d in dims):
                    bucket[name] += 1
    return {
        "about": ABOUT,
        "extracted_at": extracted_at,
        "pipeline_commit": pipeline_commit,
        "filter": {
            "study_type": STUDY_TYPE,
            "results_year_from": RESULTS_YEAR_FROM,
            "results_year_to": None,
            "other_filters": "none",
        },
        "newest_results_year": newest_results_year(records),
        **totals,
        "denominators": {name: DENOMINATOR for name in NUMERATORS},
        "by_results_year": dict(sorted(by_year.items())),
        "not_counted": not_counted,
    }
