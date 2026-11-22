#!/usr/bin/env python3
"""Item 4 (Route B): query ClinicalTrials.gov by device name and manufacturer.

Per sampled device, up to nine queries (pageSize 10, countTotal=true):

  n_intr_raw    query.intr = <device name as listed, control chars removed>
  n_term_raw    query.term = "<same, as a quoted phrase>"
  n_intr_core   query.intr = <core name>            (only if core != raw)
  n_term_core   query.term = "<core name>"          (only if core != raw)
  s_spons_l1    query.spons = <company, legal suffixes stripped>
  s_lead_l1     query.lead  = <same>
  s_spons_brand query.spons = <brand key>           (only if brand != level-1 name)
  c_term_spons  query.term = "<core name>" AND query.spons = <brand key>
  c_intr_spons  query.intr = <core name>   AND query.spons = <brand key>

"core" = first model segment with parenthesised text and trailing version
removed (common.device_name_forms). Raw responses, including HTTP errors, are
cached to JSONL; nothing is classified here.

usage: python3 -I 05_route_b_fetch.py <sample_devices.csv> <cache.jsonl>
"""
from __future__ import annotations

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import company_brand, device_name_forms  # noqa: E402
from ctgov import Cache  # noqa: E402

FIELDS = ",".join([
    # requested: (the API calls the collaborator field CollaboratorName; "Collaborators" is rejected with HTTP 400)
    "NCTId", "BriefTitle", "OfficialTitle", "LeadSponsorName", "CollaboratorName",
    "InterventionName", "InterventionType", "StartDate", "PrimaryCompletionDate", "StudyType", "HasResults",
    # extras, used only to show WHERE a name matched and for timing context:
    "InterventionOtherName", "InterventionDescription", "LeadSponsorClass", "OverallStatus",
    "StudyFirstSubmitDate", "CompletionDate",
])


def phrase(s: str) -> str:
    return '"' + s.replace('"', " ") + '"'


def plan(row: dict) -> list[tuple[str, dict]]:
    forms = device_name_forms(row["device_clean"], row["company_clean"])
    raw, core = forms["raw"], forms["core"]
    l1 = row["company_level1"]
    brand = company_brand(row["company_clean"])
    q: list[tuple[str, dict]] = [
        ("n_intr_raw", {"query.intr": raw}),
        ("n_term_raw", {"query.term": phrase(raw)}),
    ]
    if core != raw:
        q += [("n_intr_core", {"query.intr": core}), ("n_term_core", {"query.term": phrase(core)})]
    q += [("s_spons_l1", {"query.spons": l1}), ("s_lead_l1", {"query.lead": l1})]
    if brand != l1:
        q.append(("s_spons_brand", {"query.spons": brand}))
    q += [("c_term_spons", {"query.term": phrase(core), "query.spons": brand}),
          ("c_intr_spons", {"query.intr": core, "query.spons": brand})]
    return q


def main() -> None:
    sample, cache_path = sys.argv[1], sys.argv[2]
    cache = Cache(cache_path)
    with open(sample, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    n_req = n_err = 0
    for row in rows:
        for label, qp in plan(row):
            params = {**qp, "pageSize": "10", "countTotal": "true", "fields": FIELDS}
            rec = cache.get(params, tag={"route": "B", "sample_id": row["sample_id"],
                                         "submission_number": row["submission_number"], "query": label})
            n_req += 1
            if rec.get("status") != 200:
                n_err += 1
                print(f"  ! {row['submission_number']} {label}: HTTP {rec.get('status')} {str(rec.get('error'))[:120]}",
                      file=sys.stderr)
        print(f"{row['sample_id']:>2} {row['submission_number']} done", flush=True)
    print(f"queries planned/served: {n_req}; non-200: {n_err}")


if __name__ == "__main__":
    main()
