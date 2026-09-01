"""API v2 study records -> AACT-shaped `sponsors` frame.

This adapter reproduces AACT's `sponsors` table derivation one-for-one.
It is the first instance of the parity pattern: a pure record->rows
function, a docstring naming the AACT source it reproduces, and a parity
test against a real AACT fixture: test_adapter.py runs 300 stored records
through this adapter and asserts the rows equal AACT's own rows for those
trials (tests/sponsors/fixtures/records_sample_20260830.json.gz against
sponsors_fixture_20260619_industry.csv.gz). scripts/sponsor_concordance.py
does the same comparison across the whole weekly pull, per trial.

Source of truth
---------------
michaeldgreenphd/aact @ b8c16d3395ba7e548d852bccf3b47a7ff22af5f5 (branch dev;
no local modifications — every commit in its history is by CTTI maintainers,
so this is upstream ctti-clinicaltrials/aact behavior), file
app/models/sponsor.rb lines 3-21:

    root: protocolSection.sponsorCollaboratorsModule.leadSponsor
        agency_class         <- class
        lead_or_collaborator <- 'lead'
        name                 <- name
    root: protocolSection.sponsorCollaboratorsModule.collaborators   (one row per element)
        agency_class         <- class
        lead_or_collaborator <- 'collaborator'
        name                 <- name

No transformation, no filtering, `class` passed through verbatim.

Our stored record (src/utils.py lines 467-479) keeps exactly those fields:
    record["lead_sponsor_name"]  (default literal "Unknown" when the API omits it)
    record["sponsor_class"]      (the lead's class; default "Unknown")
    record["collaborators"][i]["name"], ["class"]   (defaults "")

Policy applied on top of the pure mapping (all counted, none silent):
    * "Unknown" lead names pass through as a literal (they can never match a
      rule; they show up in the audit's unreviewed state) — counted separately.
    * Empty lead names ("") pass through likewise — counted separately.
    * Empty collaborator names are dropped from the frame (an empty literal
      can never match anything) — counted separately.
    * agency_class values outside AACT's vocabulary are KEPT and logged.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, asdict

import pandas as pd

AACT_LOADER_COMMIT = "b8c16d3395ba7e548d852bccf3b47a7ff22af5f5"
AACT_SOURCE_FILES = ["app/models/sponsor.rb:3-21"]

# AACT's agency_class vocabulary (passes through the API AgencyClass enum).
AACT_AGENCY_CLASSES = frozenset(
    {"INDUSTRY", "OTHER", "NIH", "OTHER_GOV", "FED", "NETWORK", "UNKNOWN", "INDIV", "AMBIG"}
)
UNKNOWN_LEAD_LITERAL = "Unknown"  # src/utils.py:469 default for a missing lead name

FRAME_COLUMNS = ["nct_id", "agency_class", "lead_or_collaborator", "name"]


def record_rows(record: dict) -> list[dict]:
    """Pure mapping of one stored study record to AACT-shaped sponsor rows.

    Mirrors app/models/sponsor.rb exactly: one 'lead' row, then one
    'collaborator' row per collaborators[] element, in order, with
    agency_class copied verbatim. Applies NO policy — empty and "Unknown"
    names are returned as-is so callers can count them.
    """
    nct_id = record.get("nct_id")
    rows = [{
        "nct_id": nct_id,
        "agency_class": record.get("sponsor_class"),
        "lead_or_collaborator": "lead",
        "name": record.get("lead_sponsor_name"),
    }]
    for collab in record.get("collaborators") or []:
        rows.append({
            "nct_id": nct_id,
            "agency_class": collab.get("class"),
            "lead_or_collaborator": "collaborator",
            "name": collab.get("name"),
        })
    return rows


@dataclass
class AdapterLog:
    """Everything that did not map cleanly, each defect on its own line."""
    n_records: int = 0
    n_rows_emitted: int = 0
    n_rows_dropped_empty_collaborator_name: int = 0
    n_unknown_lead_names: int = 0        # literal "Unknown" (extractor default)
    n_empty_lead_names: int = 0          # "" or None
    novel_agency_classes: dict = field(default_factory=dict)  # value -> count (kept, not dropped)

    def to_dict(self) -> dict:
        return asdict(self)

    def lines(self) -> list[str]:
        return [
            f"records in: {self.n_records}; sponsor rows emitted: {self.n_rows_emitted}",
            f"lead names == \"{UNKNOWN_LEAD_LITERAL}\" (extractor default, kept as literal): {self.n_unknown_lead_names}",
            f"lead names empty (kept as literal): {self.n_empty_lead_names}",
            f"collaborator rows dropped for empty name: {self.n_rows_dropped_empty_collaborator_name}",
            "agency_class values outside AACT vocabulary (kept): "
            + (", ".join(f"{k}={v}" for k, v in sorted(self.novel_agency_classes.items())) or "none"),
        ]


def records_to_frame(records) -> tuple[pd.DataFrame, AdapterLog]:
    """Adapter over an iterable of stored records -> (frame, log).

    The frame has exactly the columns sponsor_roles.build_index() needs.
    """
    log = AdapterLog()
    novel: Counter = Counter()
    out = []
    for rec in records:
        log.n_records += 1
        for row in record_rows(rec):
            name = row["name"]
            if row["lead_or_collaborator"] == "lead":
                if name == UNKNOWN_LEAD_LITERAL:
                    log.n_unknown_lead_names += 1
                elif name is None or str(name).strip() == "":
                    log.n_empty_lead_names += 1
                    row["name"] = "" if name is None else name
            else:
                if name is None or str(name).strip() == "":
                    log.n_rows_dropped_empty_collaborator_name += 1
                    continue
            cls = row["agency_class"]
            if cls not in AACT_AGENCY_CLASSES:
                novel[str(cls)] += 1
            out.append(row)
    log.n_rows_emitted = len(out)
    log.novel_agency_classes = dict(novel)
    frame = pd.DataFrame(out, columns=FRAME_COLUMNS).astype(str)
    return frame, log
