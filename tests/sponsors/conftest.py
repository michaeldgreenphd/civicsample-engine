import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from sponsors import sponsor_roles as sr  # noqa: E402

RULES_PATH = os.path.join(ROOT, "sponsors", "company_aliases.csv")
FIXTURE_PATH = os.path.join(ROOT, "tests", "sponsors", "fixtures", "sponsors_fixture_20260619_industry.csv.gz")
# Integrity of the fixture file itself (233,272 rows: every 2026-06-19 sponsor row with
# agency_class in INDUSTRY/UNKNOWN/AMBIG/null, plus every row attributed under the rules)
FIXTURE_SHA256 = "a7a1e7afe42e99d7e55d14806c6f9fff561b0ae4b5150937a324608e431dcfde"
ACQ_PATH = os.path.join(ROOT, "sponsors", "acquisitions.csv")
# Stored records (our extractor's shape) for 300 trials whose AACT fixture rows were
# unchanged at the 2026-08-30 pull: the adapter parity sample (test_adapter.py).
RECORDS_SAMPLE_PATH = os.path.join(ROOT, "tests", "sponsors", "fixtures", "records_sample_20260830.json.gz")


def frame(rows):
    return pd.DataFrame(rows, columns=["nct_id", "agency_class", "lead_or_collaborator", "name"]).astype(str)


def rules_df(rows):
    df = pd.DataFrame(rows, columns=["canonical", "rule_type", "pattern", "status", "shared", "note"])
    return df.astype(str)


@pytest.fixture(scope="session")
def shipped_rules():
    return sr.load_rules(RULES_PATH)


@pytest.fixture(scope="session")
def fixture_frame():
    return pd.read_csv(FIXTURE_PATH, sep="|", dtype=str, keep_default_na=False)
