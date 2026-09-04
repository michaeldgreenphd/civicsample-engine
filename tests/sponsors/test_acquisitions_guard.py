import re

import pytest

from conftest import ACQ_PATH, frame, rules_df
from sponsors import sponsor_roles as sr

NOTE = "test rule; decided by tests 2026"


def test_shipped_acquisitions_all_blank_refuses_naming_the_file():
    acq = sr.load_acquisitions(ACQ_PATH)
    assert acq["effective_date"].isna().all(), "fixture assumption: shipped dates are blank"
    idx = sr.build_index(frame([("NCT1", "INDUSTRY", "lead", "Wyeth")]),
                         rules_df([("Pfizer", "exact_literal", "Wyeth", "attribute", "no", NOTE)]))
    # the message must name the actual file, not a generic fallback
    with pytest.raises(ValueError, match=re.escape(ACQ_PATH)):
        sr.attach_acquisition_dates(idx, acq)


def test_dated_acquisitions_attach(tmp_path):
    p = tmp_path / "acq.csv"
    p.write_text("entity,canonical,effective_date,source,note\nwyeth,Pfizer,2009-10-15,SEC 8-K,test\n")
    acq = sr.load_acquisitions(str(p))
    idx = sr.build_index(frame([("NCT1", "INDUSTRY", "lead", "Wyeth")]),
                         rules_df([("Pfizer", "exact_literal", "Wyeth", "attribute", "no", NOTE)]))
    out = sr.attach_acquisition_dates(idx, acq)
    assert str(out.effective_date.iloc[0].date()) == "2009-10-15"


def test_dated_row_without_a_source_refuses(tmp_path):
    p = tmp_path / "acq.csv"
    p.write_text("entity,canonical,effective_date,source,note\n"
                 "wyeth,Pfizer,2009-10-15,SEC 8-K,test\n"
                 "hospira,Pfizer,2015-09-03,,test: no source\n")
    acq = sr.load_acquisitions(str(p))
    idx = sr.build_index(frame([("NCT1", "INDUSTRY", "lead", "Wyeth")]),
                         rules_df([("Pfizer", "exact_literal", "Wyeth", "attribute", "no", NOTE)]))
    with pytest.raises(ValueError, match="1 dated row\\(s\\) in .*acq.csv have no source.*'hospira'"):
        sr.attach_acquisition_dates(idx, acq)
