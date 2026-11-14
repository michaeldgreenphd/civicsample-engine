#!/usr/bin/env python3
"""Check whether NCT IDs exist on ClinicalTrials.gov (API v2 `filter.ids`).

Route A found no NCT ID in any FDA document held locally, so there was nothing
from that route to validate. This script is run on (a) the four IDs the pilot
LLM extraction recorded from one AI/ML *manuscript* (a different stream that
reaches a device only through an unreviewed fuzzy title match) and (b) a
syntactically valid but unassigned control, to show the check discriminates.

usage: python3 -I 09_validate_nct.py <cache.jsonl> <out.json> NCT... [NCT...]
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ctgov import Cache  # noqa: E402


def main() -> None:
    cache = Cache(sys.argv[1])
    ids = sys.argv[3:]
    rec = cache.get({"filter.ids": ",".join(ids), "pageSize": str(max(10, len(ids))),
                     "fields": "NCTId,BriefTitle,LeadSponsorName,CollaboratorName,InterventionName,InterventionType,"
                               "StartDate,PrimaryCompletionDate,StudyType,HasResults,NCTIdAlias"},
                    tag={"route": "A-validate"})
    found = {}
    for s in (rec.get("body") or {}).get("studies", []):
        ps = s["protocolSection"]
        idm = ps["identificationModule"]
        row = {"nct_id": idm["nctId"], "aliases": idm.get("nctIdAliases", []),
               "brief_title": idm.get("briefTitle"),
               "lead_sponsor": ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name"),
               "interventions": [(i.get("type"), i.get("name")) for i in
                                 ps.get("armsInterventionsModule", {}).get("interventions", [])],
               "start": ps.get("statusModule", {}).get("startDateStruct", {}).get("date")}
        for k in [idm["nctId"]] + idm.get("nctIdAliases", []):
            found[k] = row
    out = [{"queried": i, "exists": i in found, **({"record": found[i]} if i in found else {})} for i in ids]
    json.dump({"http_status": rec.get("status"), "results": out}, open(sys.argv[2], "w"), indent=1)
    for o in out:
        r = o.get("record", {})
        print(o["queried"], "EXISTS" if o["exists"] else "NOT FOUND", "|", (r.get("brief_title") or "")[:80], "|",
              r.get("lead_sponsor"), "|", r.get("interventions"))


if __name__ == "__main__":
    main()
