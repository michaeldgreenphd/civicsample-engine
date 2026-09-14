"""Snapshot regression against fixtures/snapshot_baseline_2026-06-09.json.

The baseline counts are exact only for the manuscript's 2026-06-09 extract.
A fresh weekly pull drifts (the registry moves), so on any other input the
comparison REPORTS the drift and passes; it FAILS only when the input is the
baseline data itself (snapshot_date / extracted_at of 2026-06-09) and the
counts do not reproduce. Set SEX_GENDER_TABLE=path/to/sex_gender_parsed.csv.gz
(with its _meta.json beside it) to run the comparison on a real table; CI has
no table and skips that test with a reason.
"""
import json
import os
import sys
import warnings

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from src import sex_gender_table as sgt  # noqa: E402

BASELINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "snapshot_baseline_2026-06-09.json")


def _baseline():
    with open(BASELINE) as fh:
        return json.load(fh)


def _rows_matching(baseline, perturb=None):
    """Synthetic rows whose counts equal the baseline exactly (or are perturbed)."""
    sc = dict(baseline["status_counts"])
    n_sex = baseline["reported_sex_true"]
    n_gender = baseline["reported_gender_true"]
    n_both = baseline["reported_both_true"]
    n_glb = baseline["gender_labeled_binary_only_true"]
    if perturb:
        sc["reported"] += perturb
    rows = []
    # reported rows: both first, then sex-only, then gender-only, then neither
    n_rep = sc["reported"]
    for i in range(n_rep):
        both = i < n_both
        sex = both or i < n_sex
        gender = both or (n_sex <= i < n_sex + (n_gender - n_both))
        rows.append({"raw_present": True, "sex_report_status": "reported", "reported_sex": sex,
                     "reported_gender": gender, "reported_both": both, "reported_any": True,
                     "gender_labeled_binary_only": i < n_glb})
    for st in ("uninformative", "explicit_unknown_only", "not_reported"):
        for _ in range(sc.get(st, 0)):
            rows.append({"raw_present": True, "sex_report_status": st, "reported_sex": False,
                         "reported_gender": False, "reported_both": False, "reported_any": False,
                         "gender_labeled_binary_only": False})
    return rows


def assert_or_report(rows, meta, baseline):
    """The rule: fail on baseline data, report on everything else."""
    drift = sgt.baseline_drift(rows, baseline)
    if not drift:
        return []
    if sgt.is_baseline_data(meta):
        raise AssertionError("2026-06-09 baseline data does not reproduce the baseline:\n  " + "\n  ".join(drift))
    warnings.warn("sex/gender table drifts from the 2026-06-09 baseline (fresh pull; reported, not failed):\n  "
                  + "\n  ".join(drift))
    return drift


def test_baseline_reproduces_itself():
    b = _baseline()
    assert sgt.baseline_drift(_rows_matching(b), b) == []
    assert assert_or_report(_rows_matching(b), {"snapshot_date": "2026-06-09"}, b) == []


def test_drift_fails_only_on_baseline_data():
    b = _baseline()
    rows = _rows_matching(b, perturb=3)
    with pytest.raises(AssertionError):
        assert_or_report(rows, {"snapshot_date": "2026-06-09"}, b)
    with pytest.raises(AssertionError):
        assert_or_report(rows, {"extracted_at": "2026-06-09T06:00:00+00:00"}, b)
    with pytest.warns(UserWarning, match="drifts"):
        drift = assert_or_report(rows, {"snapshot_date": "2026-09-14"}, b)
    assert any(line.startswith("status.reported: ") and "(+3)" in line for line in drift)
    assert any(line.startswith("trials_with_measure: ") for line in drift)


def test_drift_ignores_rows_without_a_measure():
    b = _baseline()
    rows = _rows_matching(b) + [{"raw_present": False, "sex_report_status": "not_reported"}] * 5000
    assert sgt.baseline_drift(rows, b) == []


@pytest.mark.skipif(not os.environ.get("SEX_GENDER_TABLE"), reason="SEX_GENDER_TABLE not set: no parsed table to compare")
def test_live_table_against_baseline():
    import pandas as pd
    path = os.environ["SEX_GENDER_TABLE"]
    meta_path = os.environ.get("SEX_GENDER_TABLE_META", path.replace("parsed.csv.gz", "parsed_meta.json"))
    df = pd.read_csv(path, low_memory=False)
    # CSV round-trip: NaN cells become None, boolean columns become bools.
    rows = [{k: (None if v != v else v) for k, v in r.items()} for r in df.to_dict("records")]
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
    for r in rows:
        for k in ("raw_present", "parse_ok", "reported_sex", "reported_gender", "reported_both", "reported_any",
                  "gender_labeled_binary_only", "is_participant_count"):
            v = r.get(k)
            r[k] = None if v is None else (str(v).lower() == "true")
    drift = assert_or_report(rows, meta, _baseline())
    print("\n".join(drift) if drift else "exact match with the 2026-06-09 baseline")
