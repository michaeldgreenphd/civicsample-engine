import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from sponsors import sponsor_roles as sr  # noqa: E402

RULES_PATH = os.path.join(ROOT, "sponsors", "company_aliases.csv")
FIXTURE_PATH = os.path.join(ROOT, "tests", "sponsors", "fixtures", "sponsors_fixture_20260619.csv.gz")
ACQ_PATH = os.path.join(ROOT, "sponsors", "acquisitions.csv")
# The fixture is valid only for this exact rules file (README caveat a).
FIXTURE_RULES_SHA256 = "3e13bc5926f6f8bc5025151c60ea7dc8f40a5f7b8d7a209026ba29472e70601c"


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
    return pd.read_csv(FIXTURE_PATH, sep="|", dtype=str)
