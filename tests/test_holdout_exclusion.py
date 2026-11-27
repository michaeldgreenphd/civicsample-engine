"""src/holdout_exclusion.py: the strict holdout index loader, the per-build exclusion and its stamp.

Every id is fictional (NCT99777701..NCT99777709); no real index is read.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from src import holdout_exclusion as HX

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "holdout_index" / "index.json"
H1, H2, KEPT = "NCT99777701", "NCT99777702", "NCT99777703"


def _index(tmp_path: Path, payload: Any, name: str = "index.json") -> Path:
    path = tmp_path / name
    path.write_bytes(payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8"))
    return path


def test_fixture_index_loads_with_its_sha256_and_counts() -> None:
    index = HX.load_holdout_index(FIXTURE)
    assert index.ids == frozenset({H1, H2})
    assert (index.n_rows, index.n_holdouts) == (3, 2)
    assert index.sha256 == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert H1 not in repr(index) and H1 not in repr(HX.HoldoutExclusion(index))


@pytest.mark.parametrize("payload, message", [
    (b"\x80not utf-8", "not UTF-8 JSON"),
    (b"{", "not UTF-8 JSON"),
    ([], "must be a JSON object"),
    ({"data": [{"nct_id": H1, "in_audit": True}]}, "has no rows list"),
    ({"rows": {"nct_id": H1}}, "not a list"),
    ({"rows": []}, "rows is empty"),
    ({"rows": [[H1, True]]}, "not JSON objects (row position(s) 0)"),
    ({"rows": [{"nct_id": KEPT, "in_audit": False}, {"in_audit": True}]}, "missing or not exactly"),
    ({"rows": [{"nct_id": "NCT9977770", "in_audit": True}]}, "missing or not exactly"),
    ({"rows": [{"nct_id": f"{H1}\n", "in_audit": True}]}, "missing or not exactly"),
    ({"rows": [{"nct_id": "NCT９９７７７７０１", "in_audit": True}]}, "missing or not exactly"),
    ({"rows": [{"nct_id": H1, "in_audit": 1}]}, "first: int"),
    ({"rows": [{"nct_id": H1, "in_audit": "true"}]}, "first: str"),
    ({"rows": [{"nct_id": H1, "in_audit": None}]}, "first: NoneType"),
    ({"rows": [{"nct_id": H1}]}, "first: missing"),
    ({"rows": [{"nct_id": H1, "in_audit": True}, {"nct_id": H1, "in_audit": False}]}, "repeat an nct_id"),
    (b'{"rows": [{"nct_id": "NCT99777701", "in_audit": true, "in_audit": false}]}', "repeats a key"),
    (b'{"rows": [{"nct_id": "NCT99777701", "in_audit": true}], "rows": []}', "repeats a key"),
    (b'{"rows": [{"nct_id": "NCT99777701", "in_audit": true, "x": NaN}]}', "non-JSON constant NaN"),
])
def test_a_malformed_index_is_refused_with_counts_and_positions_never_an_id(
    tmp_path: Path, payload: Any, message: str
) -> None:
    with pytest.raises(HX.HoldoutIndexError) as refused:
        HX.load_holdout_index(_index(tmp_path, payload))
    text = str(refused.value)
    assert message in text and text.startswith("index.json: ")
    assert "777701" not in text and "７７７" not in text


def test_index_error_is_not_a_value_error() -> None:
    assert not issubclass(HX.HoldoutIndexError, ValueError)


def test_a_missing_index_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(HX.HoldoutIndexError, match="cannot read the holdout index"):
        HX.load_holdout_index(tmp_path / "absent.json")


def test_an_index_with_no_holdouts_loads_with_zero_holdouts(tmp_path: Path) -> None:
    index = HX.load_holdout_index(_index(tmp_path, {"rows": [{"nct_id": KEPT, "in_audit": False}]}))
    assert index.n_holdouts == 0 and index.n_rows == 1


def test_exclusion_counts_distinct_holdouts_of_the_universe_only() -> None:
    exclusion = HX.HoldoutExclusion(HX.load_holdout_index(FIXTURE))
    assert exclusion.exclude_ids([KEPT, H1, "NCT99777709", H1]) == [KEPT, "NCT99777709"]
    assert exclusion.exclude(f" {H1.lower()} ") is True  # a padded or lower-cased copy is the same holdout
    assert exclusion.without_holdouts([H2, KEPT]) == [KEPT]  # a selection: dropped, not counted
    assert exclusion.is_holdout(H2) and not exclusion.is_holdout(KEPT) and not exclusion.is_holdout(None)
    assert exclusion.n_excluded == 1
    assert exclusion.stamp() == {"rules_version": HX.RULES_VERSION,
                                 "index_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
                                 "holdouts_in_index": 2, "excluded_from_universe": 1}


def test_a_stamp_is_refused_when_no_loader_applied_the_exclusion() -> None:
    exclusion = HX.HoldoutExclusion(HX.load_holdout_index(FIXTURE))
    exclusion.without_holdouts([H1])
    with pytest.raises(HX.HoldoutIndexError, match="never applied"):
        exclusion.stamp()


def test_carry_forward_adds_an_earlier_count_under_the_same_index_only(tmp_path: Path) -> None:
    index = HX.load_holdout_index(FIXTURE)
    earlier = HX.HoldoutExclusion(index)
    earlier.exclude_ids([H1, H2, KEPT])
    build = HX.HoldoutExclusion(index)
    build.carry_forward(earlier.stamp(), "manifest.json")
    build.exclude_ids([KEPT])
    assert build.stamp()["excluded_from_universe"] == 2
    other = HX.HoldoutExclusion(HX.load_holdout_index(_index(tmp_path, {"rows": [{"nct_id": H1, "in_audit": True}]})))
    with pytest.raises(HX.HoldoutIndexError, match="another holdout index"):
        other.carry_forward(earlier.stamp(), "manifest.json")
    with pytest.raises(HX.HoldoutIndexError, match="rules_version"):
        build.carry_forward({**earlier.stamp(), "rules_version": "holdout-exclusion-0"}, "manifest.json")


@pytest.mark.parametrize("stamp, fragment", [
    (None, "is missing"),
    ([], "not an object"),
    ({}, "must hold exactly"),
])
def test_stamp_problems_refuses_a_missing_or_malformed_stamp(stamp: Any, fragment: str) -> None:
    assert any(fragment in p for p in HX.stamp_problems(stamp))


@pytest.mark.parametrize("key, value", [
    ("rules_version", "holdout-exclusion-2"), ("index_sha256", "0" * 63), ("index_sha256", "F" * 64),
    ("holdouts_in_index", True), ("holdouts_in_index", 2.0), ("excluded_from_universe", -1),
    ("excluded_from_universe", "1"),
])
def test_stamp_problems_checks_each_field_type(key: str, value: Any) -> None:
    exclusion = HX.HoldoutExclusion(HX.load_holdout_index(FIXTURE))
    exclusion.exclude_ids([KEPT])
    good = exclusion.stamp()
    assert HX.stamp_problems(good) == []
    assert any(f"holdout_exclusion.{key}" in p for p in HX.stamp_problems({**good, key: value}))
    assert any("must hold exactly" in p for p in HX.stamp_problems({**good, "nct_ids": []}))


def test_require_matching_stamp_refuses_another_index(tmp_path: Path) -> None:
    index = HX.load_holdout_index(FIXTURE)
    exclusion = HX.HoldoutExclusion(index)
    exclusion.exclude_ids([KEPT])
    run = {"holdout_exclusion": exclusion.stamp()}
    assert HX.require_matching_stamp(run, index) == run["holdout_exclusion"]
    changed = HX.load_holdout_index(_index(tmp_path, FIXTURE.read_bytes() + b" "))
    with pytest.raises(HX.HoldoutIndexError, match="the index changed since the build"):
        HX.require_matching_stamp(run, changed)
    with pytest.raises(HX.HoldoutIndexError, match="holdout_exclusion is missing"):
        HX.require_matching_stamp({}, index)


def test_count_holdout_mentions_reads_gzip_and_whole_tokens_only() -> None:
    ids = frozenset({H1})
    text = f'{{"nct_id": "{H1}", "note": "see {H1}; not {H1}9 nor X{H1}", "other": "{KEPT}"}}'.encode()
    assert HX.count_holdout_mentions(text, ids) == 2
    assert HX.count_holdout_mentions(gzip.compress(text, mtime=0), ids) == 2
    assert HX.count_holdout_mentions(b"nothing here", ids) == 0


def test_count_holdout_mentions_reads_the_id_in_any_letter_case() -> None:
    # Free text (a citation, an outcome description) can name a holdout in lower or mixed case.
    ids = frozenset({H1})
    lower, mixed = H1.lower(), "Nct" + H1[3:]
    text = f'{{"citation": "see {lower}", "description": "extends {mixed}", "x": "x{lower}"}}'.encode()
    assert HX.count_holdout_mentions(text, ids) == 2
    assert HX.count_holdout_mentions(gzip.compress(text, mtime=0), ids) == 2
    assert HX.count_holdout_mentions(KEPT.lower().encode(), ids) == 0


def test_refuse_holdout_mentions_refuses_a_lower_case_mention(tmp_path: Path) -> None:
    index = HX.load_holdout_index(FIXTURE)
    (tmp_path / "records.json").write_text(f'{{"note": "follow-up of {H2.lower()}"}}', encoding="utf-8")
    with pytest.raises(HX.HoldoutIndexError) as refused:
        HX.refuse_holdout_mentions(tmp_path, index)
    assert "records.json (1)" in str(refused.value) and H2.lower() not in str(refused.value).lower()


def _record(top: Any = None, proto: Any = None, key: str = "nct_id") -> dict[str, Any]:
    record: dict[str, Any] = {}
    if top is not None:
        record[key] = top
    if proto is not None:
        record["protocolSection"] = {"identificationModule": {"nctId": proto}}
    return record


def test_record_trial_id_reads_every_place_a_record_carries_its_id() -> None:
    assert HX.record_trial_id(_record(top=KEPT)) == KEPT
    assert HX.record_trial_id(_record(top=KEPT, key="nctId")) == KEPT
    assert HX.record_trial_id(_record(proto=H1)) == H1
    assert HX.record_trial_id(_record(top=H1, proto=H1)) == H1
    # The same id written with padding or in lower case agrees, as _matches reads it.
    assert HX.record_trial_id(_record(top=f" {H1.lower()} ", proto=H1)) == f" {H1.lower()} "
    # A blank top-level id is absent, so the protocol id decides.
    assert HX.record_trial_id(_record(top="", proto=H1)) == H1
    assert HX.record_trial_id(_record(top="   ", proto=H1)) == H1
    assert HX.record_trial_id({}) is None and HX.record_trial_id(["not", "a", "record"]) is None
    assert HX.record_trial_id(_record(top=123)) == 123


def test_record_trial_id_refuses_ids_that_disagree_without_naming_them() -> None:
    for record in (_record(top=KEPT, proto=H1), _record(top=H1, proto=KEPT),
                   _record(top=KEPT, proto=H1, key="nctId"), _record(top=123, proto=H1)):
        with pytest.raises(HX.HoldoutIndexError) as refused:
            HX.record_trial_id(record, "study record 7")
        message = str(refused.value)
        assert "study record 7" in message and "disagree" in message
        assert H1 not in message and KEPT not in message
    both = {"nct_id": KEPT, "nctId": H1}
    with pytest.raises(HX.HoldoutIndexError):
        HX.record_trial_id(both)
    exclusion = HX.HoldoutExclusion(HX.load_holdout_index(FIXTURE))
    assert exclusion.trial_id_of(_record(top="", proto=H1)) == H1


def test_refuse_holdout_mentions_names_files_and_counts_not_ids(tmp_path: Path) -> None:
    index = HX.load_holdout_index(FIXTURE)
    (tmp_path / "summary.json").write_text('{"n": 1}')
    HX.refuse_holdout_mentions(tmp_path, index)
    (tmp_path / "trials.jsonl.gz").write_bytes(gzip.compress(f'{{"nct_id": "{H2}"}}\n'.encode(), mtime=0))
    with pytest.raises(HX.HoldoutIndexError) as refused:
        HX.refuse_holdout_mentions(tmp_path, index)
    assert "trials.jsonl.gz (1)" in str(refused.value) and H2 not in str(refused.value)


def test_curated_rows_naming_a_holdout_are_refused_by_row_number(tmp_path: Path) -> None:
    csv_path = tmp_path / "reviews.csv"
    csv_path.write_text(f"nct_id,notes\n{KEPT},a\n{H1},b\n", encoding="utf-8")
    exclusion = HX.HoldoutExclusion(HX.load_holdout_index(FIXTURE))
    with pytest.raises(HX.HoldoutIndexError) as refused:
        exclusion.refuse_curated_rows(HX.curated_csv_ids(csv_path), "reviews.csv")
    assert "row(s) 3" in str(refused.value) and H1 not in str(refused.value)
    assert HX.curated_csv_ids(tmp_path / "absent.csv") == []
    (tmp_path / "other.csv").write_text("link_id\nx\n", encoding="utf-8")
    assert HX.curated_csv_ids(tmp_path / "other.csv") == []
