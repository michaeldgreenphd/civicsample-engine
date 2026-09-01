import hashlib

import pytest

from conftest import FIXTURE_RULES_SHA256, RULES_PATH
from sponsors import sponsor_roles as sr

HEADER = "canonical,rule_type,pattern,status,shared,note\n"


def _write(tmp_path, body):
    p = tmp_path / "aliases.csv"
    p.write_text(HEADER + body)
    return str(p)


def test_empty_note_fails(tmp_path):
    with pytest.raises(ValueError, match="empty note"):
        sr.load_rules(_write(tmp_path, "Pfizer,exact_normalized,pfizer,attribute,no,\n"))


def test_unknown_status_fails(tmp_path):
    with pytest.raises(ValueError, match="unknown status"):
        sr.load_rules(_write(tmp_path, "Pfizer,exact_normalized,pfizer,maybe,no,x decided 2026\n"))


def test_unknown_rule_type_fails(tmp_path):
    with pytest.raises(ValueError, match="unknown rule_type"):
        sr.load_rules(_write(tmp_path, "Pfizer,substring,pfizer,attribute,no,x decided 2026\n"))


def test_shipped_rules_load_and_match_fixture_baseline_sha():
    rules = sr.load_rules(RULES_PATH)
    assert len(rules) == 112
    sha = hashlib.sha256(open(RULES_PATH, "rb").read()).hexdigest()
    assert sha == FIXTURE_RULES_SHA256, (
        "company_aliases.csv changed. The 2026-06-19 fixture is only valid for the "
        "rules at sha256 3e13bc59...: re-baseline the fixture deliberately and update "
        "FIXTURE_RULES_SHA256 in tests/sponsors/conftest.py in the same PR."
    )
