#!/usr/bin/env python3
"""Supplementary route (D): does any ClinicalTrials.gov record cite one of our
FDA submission numbers in its own text?

Not in the original brief. Added because registry records were observed to
carry 510(k) numbers in intervention/arm descriptions, and the submission
number is the one identifier we hold for every device. It is the reverse of
Route A (registry cites FDA, rather than FDA document cites registry).

Method: full-text `query.term` over all DISTINCT submission numbers, 25 per
request joined with OR (58 requests). Any batch with hits is re-queried one
number at a time, and each hit study is then fetched in full so the exact
JSON field and surrounding text holding the number can be recorded.

A bare string hit is NOT a link: "K203042" could be a sponsor's protocol
number. Each hit is therefore graded on where the string sits and whether
regulatory words (510(k), FDA, cleared, De Novo, PMA, premarket) occur within
150 characters of it.

usage: python3 -I 07_route_d_submission_number.py <enriched.csv> <cache.jsonl> <out.json>
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (company_brand, norm_company, norm_text, parse_decision_date,  # noqa: E402
                    pathway_of, read_devices)
from ctgov import Cache  # noqa: E402

BATCH = 25
REG_WORDS = re.compile(r"510\s*\(?k\)?|\bFDA\b|clear(ed|ance)|de novo|\bPMA\b|premarket|pre-market|approv", re.I)


def walk(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from walk(v, path + "/" + k)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v, path + "[]")
    elif isinstance(o, str):
        yield path, o


def main() -> None:
    src, cache_path, out = sys.argv[1], sys.argv[2], sys.argv[3]
    cache = Cache(cache_path)
    dev: dict[str, dict] = {}
    for r in read_devices(src):
        dev.setdefault(r["Submission Number"].strip(), r)
    # "P140011/S008": search the base PMA number; the slash is query syntax.
    numbers = sorted({sn.split("/")[0] for sn in dev})
    base_to_sn = {sn.split("/")[0]: sn for sn in dev}
    positives: list[str] = []
    n_batches = 0
    for i in range(0, len(numbers), BATCH):
        chunk = numbers[i:i + BATCH]
        rec = cache.get({"query.term": " OR ".join(chunk), "pageSize": "1", "countTotal": "true", "fields": "NCTId"},
                        tag={"route": "D", "step": "batch", "first": chunk[0], "n": len(chunk)})
        n_batches += 1
        if rec.get("status") != 200:
            print(f"  ! batch {chunk[0]}: HTTP {rec.get('status')} {rec.get('error')}", file=sys.stderr)
            continue
        if rec["body"].get("totalCount", 0) > 0:
            for sn in chunk:
                one = cache.get({"query.term": sn, "pageSize": "50", "countTotal": "true", "fields": "NCTId"},
                                tag={"route": "D", "step": "single", "submission_number": sn})
                if one.get("status") == 200 and one["body"].get("totalCount", 0) > 0:
                    positives.append(sn)
    hits = []
    for sn in positives:
        one = cache.get({"query.term": sn, "pageSize": "50", "countTotal": "true", "fields": "NCTId"})
        d = dev[base_to_sn[sn]]
        name_n = norm_text(re.sub(r"\([^)]*\)", " ", d["Device"]))
        brand = company_brand(d["Company"])
        for st in one["body"].get("studies", []):
            nct = st["protocolSection"]["identificationModule"]["nctId"]
            full = cache.get({"filter.ids": nct, "pageSize": "1"},
                             tag={"route": "D", "step": "full_record", "nct": nct})
            if full.get("status") != 200 or not full["body"].get("studies"):
                continue
            study = full["body"]["studies"][0]
            where = []
            for path, text in walk(study):
                for m in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(sn) + r"(?![0-9])", text):
                    ctx = text[max(0, m.start() - 150): m.end() + 150]
                    where.append({"path": path, "context": re.sub(r"\s+", " ", ctx),
                                  "regulatory_word_nearby": bool(REG_WORDS.search(ctx))})
            ps = study.get("protocolSection", {})
            blob = norm_text(json.dumps(study))
            spons = [ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name", "")] + \
                    [c.get("name", "") for c in ps.get("sponsorCollaboratorsModule", {}).get("collaborators", [])]
            sm = ps.get("statusModule", {})
            hits.append({
                "submission_number": base_to_sn[sn], "pathway": pathway_of(sn), "device": d["Device"],
                "company": d["Company"], "decision_date": parse_decision_date(d["Date of Final Decision"]).isoformat(),
                "nct_id": nct, "brief_title": ps.get("identificationModule", {}).get("briefTitle"),
                "sponsors": spons, "start_date": sm.get("startDateStruct", {}).get("date"),
                "primary_completion_date": sm.get("primaryCompletionDateStruct", {}).get("date"),
                "string_found_in_record": bool(where),
                "paths": sorted({w["path"] for w in where}),
                "regulatory_word_nearby": any(w["regulatory_word_nearby"] for w in where),
                "first_context": where[0]["context"] if where else None,
                "device_name_in_record": bool(name_n) and (" " + name_n + " ") in (" " + blob + " "),
                "company_brand_in_sponsors": any(brand and brand in norm_company(s) for s in spons),
                "company_brand_anywhere_in_record": bool(brand) and (" " + brand + " ") in (" " + blob + " "),
            })
    res = {"distinct_submission_numbers_searched": len(numbers), "batch_requests": n_batches,
           "numbers_with_ge1_hit": len(positives), "hit_pairs": len(hits), "hits": hits}
    json.dump(res, open(out, "w"), indent=1)
    print({k: v for k, v in res.items() if k != "hits"})
    for h in hits:
        print(h["submission_number"], h["nct_id"], "| reg:", h["regulatory_word_nearby"], "| name:", h["device_name_in_record"],
              "| spons:", h["company_brand_in_sponsors"], "|", h["device"][:35], "|", (h["first_context"] or "")[:160])


if __name__ == "__main__":
    main()
