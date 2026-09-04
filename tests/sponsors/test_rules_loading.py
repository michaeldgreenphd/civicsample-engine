import pytest

from conftest import RULES_PATH
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


def test_shipped_rules_load():
    rules = sr.load_rules(RULES_PATH)
    assert len(rules) >= 112
    assert set(rules.status) <= {"attribute", "exclude"}


def test_shared_column_is_validated(tmp_path):
    p = tmp_path / "rules.csv"
    p.write_text("canonical,rule_type,pattern,status,shared,note\n"
                 "Pfizer,exact_literal,Pfizer,attribute,maybe,\"test: bad shared value\"\n")
    with pytest.raises(ValueError, match="unknown shared values"):
        sr.load_rules(str(p))
