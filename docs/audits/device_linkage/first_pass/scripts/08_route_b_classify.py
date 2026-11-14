#!/usr/bin/env python3
"""Items 4 and 5: classify Route B candidates from string evidence alone, and
time class (i) studies against the FDA decision date. Reads the cache; makes
no network calls (a query missing from the cache is reported, not fetched).

NAME EVIDENCE (deliberately narrow)
  A name matches a study only if a name form appears as a contiguous run of
  whole tokens in an intervention name, an intervention "other name", the
  brief title or the official title. Token runs are also compared with their
  spaces removed so "KardiaAI" meets "Kardia AI". Appearance only in an
  intervention *description* is recorded but does not count.
    full     a whole model segment of the listed name (version/parentheses removed)
    trimmed  that segment with trailing device-type words removed
             ("SKOUT system" -> "SKOUT"); accepted only if what remains has a
             non-generic token and is not a single token under 4 characters.
    company_only  the trimmed form is nothing but the manufacturer's own brand
             ("Athelas One" -> "Athelas", "ClearPoint System" -> "ClearPoint").
             That shows the MANUFACTURER is named in the study, not that this
             device is; it is recorded but never counts as a device-name match.

SPONSOR EVIDENCE (lead sponsor or any collaborator)
    exact   level-1 names equal (case, punctuation, legal form removed)
    brand   brand keys equal AND both names begin with that key (differences
            only in trailing corporate filler: "GE Medical Systems" ~ "GE Healthcare")
    family  one brand key is a leading token run of the other but real words
            differ ("Siemens" vs "Siemens Healthineers"), or the keys are equal
            only after dropping a leading filler word ("Insight Medical
            Systems" vs "Research Insight LLC"). Weak: also what "Pearl" vs
            "Pearl Therapeutics" and "Exo" vs "EXO Biologics" look like.

CLASSES (best available per device)
    i     name match AND sponsor exact/brand
    i-f   name match AND sponsor family only     (reported apart; weak sponsor evidence)
    ii    name match, sponsor differs
    ii-c  only the manufacturer's brand is named in an intervention/title, and
          the sponsor differs (reported apart; rolls up under ii)
    iii   sponsor exact/brand/family on >=1 returned study, no device-name match
    iv    no candidate (iv-a: zero hits on every name query; iv-b: hits, but
          the name is in neither a title nor an intervention name)

usage: python3 -I 08_route_b_classify.py <sample_devices.csv> <cache.jsonl[.gz]> <out_dir>
"""
from __future__ import annotations

import collections
import csv
import importlib.util
import json
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import (GENERIC_DEVICE_WORDS, company_brand, device_name_forms,  # noqa: E402
                    norm_company, norm_text)
from ctgov import Cache, _key  # noqa: E402

_spec = importlib.util.spec_from_file_location("route_b_fetch", os.path.join(HERE, "05_route_b_fetch.py"))
fetch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch)

FLOOD = 50  # a name query returning this many studies is treated as a flood


def parse_ct_date(s: str | None) -> date | None:
    """ClinicalTrials.gov dates are YYYY-MM-DD or YYYY-MM. A month-only date
    is placed on the 15th; the timing buckets below are years wide, so the
    half-month uncertainty does not move a study between them."""
    if not s:
        return None
    parts = s.split("-")
    try:
        return date(int(parts[0]), int(parts[1]) if len(parts) > 1 else 7, int(parts[2]) if len(parts) > 2 else 15)
    except ValueError:
        return None


def contains_run(field_norm: str, phrase_norm: str) -> bool:
    if not phrase_norm or not field_norm:
        return False
    if f" {phrase_norm} " in f" {field_norm} ":
        return True
    squashed = phrase_norm.replace(" ", "")
    toks = field_norm.split()
    for w in range(1, 5):
        for i in range(len(toks) - w + 1):
            if "".join(toks[i:i + w]) == squashed:
                return True
    return False


def name_forms(row: dict) -> dict:
    f = device_name_forms(row["device_clean"], row["company_clean"])
    full = []
    for v in [f["raw"]] + f["variants"]:
        n = norm_text(v)
        if n and n not in full:
            full.append(n)
    trimmed, company_only = [], []
    brand_toks = set(company_brand(row["company_clean"]).split())
    for n in list(full):
        toks = n.split()
        while toks and toks[-1] in GENERIC_DEVICE_WORDS:
            toks.pop()
        t = " ".join(toks)
        ok = bool(toks) and any(x not in GENERIC_DEVICE_WORDS for x in toks) and not (len(toks) == 1 and len(t) < 4)
        if ok and t not in full and t not in trimmed and t not in company_only:
            (company_only if set(toks) <= brand_toks else trimmed).append(t)
    # A full form that is itself only device-type words cannot count as a name.
    full = [n for n in full if any(x not in GENERIC_DEVICE_WORDS for x in n.split())
            and not (len(n.split()) == 1 and len(n) < 3)]
    return {"full": full, "trimmed": trimmed, "company_only": company_only,
            "generic": f["name_is_generic"], "core": f["core"]}


def flatten(study: dict) -> dict:
    ps = study.get("protocolSection", {})
    idm, sm = ps.get("identificationModule", {}), ps.get("statusModule", {})
    sc = ps.get("sponsorCollaboratorsModule", {})
    ivs = ps.get("armsInterventionsModule", {}).get("interventions", []) or []
    return {
        "nct_id": idm.get("nctId"), "brief_title": idm.get("briefTitle") or "",
        "official_title": idm.get("officialTitle") or "",
        "lead_sponsor": (sc.get("leadSponsor") or {}).get("name") or "",
        "lead_sponsor_class": (sc.get("leadSponsor") or {}).get("class") or "",
        "collaborators": [c.get("name", "") for c in sc.get("collaborators", []) or []],
        "interventions": [{"type": i.get("type"), "name": i.get("name") or "",
                           "other_names": i.get("otherNames") or [], "description": i.get("description") or ""}
                          for i in ivs],
        "start_date": (sm.get("startDateStruct") or {}).get("date"),
        "primary_completion_date": (sm.get("primaryCompletionDateStruct") or {}).get("date"),
        "overall_status": sm.get("overallStatus"),
        "study_type": (ps.get("designModule") or {}).get("studyType"),
        "has_results": study.get("hasResults"),
    }


def name_evidence(st: dict, forms: dict) -> dict | None:
    fields = [("intervention_name", norm_text(i["name"])) for i in st["interventions"]]
    fields += [("intervention_other_name", norm_text(o)) for i in st["interventions"] for o in i["other_names"]]
    fields += [("brief_title", norm_text(st["brief_title"])), ("official_title", norm_text(st["official_title"]))]
    for strength in ("full", "trimmed", "company_only"):
        for form in forms[strength]:
            for where, text in fields:
                if contains_run(text, form):
                    return {"strength": strength, "form": form, "where": where}
    for form in forms["full"] + forms["trimmed"]:
        for i in st["interventions"]:
            if contains_run(norm_text(i["description"]), form):
                return {"strength": "description_only", "form": form, "where": "intervention_description"}
    return None


def sponsor_evidence(st: dict, company_clean: str) -> dict | None:
    l1, brand = norm_company(company_clean), company_brand(company_clean)
    best = None
    rank = {"exact": 3, "brand": 2, "family": 1}
    for role, s in [("lead", st["lead_sponsor"])] + [("collaborator", c) for c in st["collaborators"]]:
        sl1, sb = norm_company(s), company_brand(s)
        tier = None
        if sl1 and sl1 == l1:
            tier = "exact"
        elif sb and sb == brand and sl1.startswith(brand) and l1.startswith(brand):
            tier = "brand"
        elif sb and sb == brand:
            tier = "family"
        else:
            a, b = brand.split(), sb.split()
            short, long_ = (a, b) if len(a) <= len(b) else (b, a)
            if short and long_[:len(short)] == short and any(len(x) >= 3 for x in short):
                tier = "family"
        if tier and (best is None or rank[tier] > rank[best["tier"]]):
            best = {"tier": tier, "role": role, "sponsor": s}
    return best


def timing(st: dict, decision: date) -> dict:
    sd, pcd = parse_ct_date(st["start_date"]), parse_ct_date(st["primary_completion_date"])
    yrs = lambda d: None if d is None else round((d - decision).days / 365.25, 2)
    if sd is None:
        rel = "start date missing"
    elif pcd is not None and pcd <= decision:
        rel = "primary completion on/before decision"
    elif sd <= decision:
        rel = "started before decision, primary completion after (or missing)"
    elif (sd - decision).days <= 365:
        rel = "started within 1 year after decision"
    else:
        rel = "started more than 1 year after decision"
    return {"start_minus_decision_years": yrs(sd), "primary_completion_minus_decision_years": yrs(pcd), "relation": rel}


def main() -> None:
    sample, cache_path, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    cache = Cache(cache_path)
    rows = list(csv.DictReader(open(sample, encoding="utf-8", newline="")))
    per_device, missing = [], 0
    for row in rows:
        forms = name_forms(row)
        decision = date.fromisoformat(row["decision_date_iso"])
        counts, studies = {}, {}
        for label, qp in fetch.plan(row):
            params = {**qp, "pageSize": "10", "countTotal": "true", "fields": fetch.FIELDS}
            rec = cache.mem.get(_key(params))
            if rec is None:
                counts[label] = "NOT_IN_CACHE"
                missing += 1
                continue
            if rec.get("status") != 200:
                counts[label] = f"HTTP_{rec.get('status')}"
                continue
            counts[label] = rec["body"].get("totalCount")
            for s in rec["body"].get("studies", []):
                st = flatten(s)
                studies.setdefault(st["nct_id"], {**st, "from_queries": []})["from_queries"].append(label)
        cands = []
        for st in studies.values():
            ne, se = name_evidence(st, forms), sponsor_evidence(st, row["company_clean"])
            name_ok = bool(ne) and ne["strength"] in ("full", "trimmed")
            if name_ok and se and se["tier"] in ("exact", "brand"):
                cls = "i"
            elif name_ok and se:
                cls = "i-f"
            elif name_ok:
                cls = "ii"
            elif se:
                cls = "iii"
            elif ne and ne["strength"] == "company_only":
                cls = "ii-c"
            else:
                cls = None
            if cls:
                cands.append({**st, "class": cls, "name_evidence": ne, "sponsor_evidence": se,
                              "timing": timing(st, decision)})
        order = {"i": 0, "i-f": 1, "ii": 2, "iii": 3, "ii-c": 4}
        # within a class prefer intervention-name evidence, then stronger sponsor tier, then NCT id for determinism
        cands.sort(key=lambda c: (order[c["class"]],
                                  0 if (c["name_evidence"] or {}).get("where", "").startswith("intervention") else 1,
                                  -{"exact": 3, "brand": 2, "family": 1}.get((c["sponsor_evidence"] or {}).get("tier"), 0),
                                  c["nct_id"]))
        name_counts = [v for k, v in counts.items() if k.startswith("n_") and isinstance(v, int)]
        any_name_hit = any(v > 0 for v in name_counts)
        if cands:
            best_class = cands[0]["class"]
        else:
            best_class = "iv-b" if any_name_hit else "iv-a"
        per_device.append({
            "sample_id": int(row["sample_id"]), "stratum": row["stratum"], "submission_number": row["submission_number"],
            "pathway": row["pathway"], "decision_date": row["decision_date_iso"], "device": row["device_clean"],
            "company": row["company_clean"], "company_level1": row["company_level1"],
            "company_brand": company_brand(row["company_clean"]),
            "name_forms": forms, "query_counts": counts,
            "max_name_query_count": max(name_counts) if name_counts else 0,
            "flood": bool(name_counts) and max(name_counts) >= FLOOD,
            "unique_studies_returned": len(studies),
            "class": best_class,
            "n_by_class": dict(collections.Counter(c["class"] for c in cands)),
            "best": cands[0] if cands else None,
            "candidates": cands,
        })
    os.makedirs(out_dir, exist_ok=True)
    json.dump(per_device, open(os.path.join(out_dir, "route_b_classified.json"), "w"), indent=1)
    with open(os.path.join(out_dir, "route_b_per_device.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        labels = ["n_intr_raw", "n_term_raw", "n_intr_core", "n_term_core", "s_spons_l1", "s_lead_l1",
                  "s_spons_brand", "c_term_spons", "c_intr_spons"]
        w.writerow(["sample_id", "stratum", "submission_number", "pathway", "decision_date", "device", "company",
                    "name_generic", "flood"] + labels +
                   ["unique_studies_returned", "class", "n_class_i", "n_class_i_f", "n_class_ii", "n_class_ii_c", "n_class_iii",
                    "best_nct", "best_lead_sponsor", "best_name_form", "best_name_where", "best_sponsor_tier",
                    "best_start", "best_primary_completion", "best_timing"])
        for d in per_device:
            b = d["best"] or {}
            w.writerow([d["sample_id"], d["stratum"], d["submission_number"], d["pathway"], d["decision_date"],
                        d["device"], d["company"], d["name_forms"]["generic"], d["flood"]] +
                       [d["query_counts"].get(k, "") for k in labels] +
                       [d["unique_studies_returned"], d["class"], d["n_by_class"].get("i", 0),
                        d["n_by_class"].get("i-f", 0), d["n_by_class"].get("ii", 0), d["n_by_class"].get("ii-c", 0),
                        d["n_by_class"].get("iii", 0),
                        b.get("nct_id", ""), b.get("lead_sponsor", ""), (b.get("name_evidence") or {}).get("form", ""),
                        (b.get("name_evidence") or {}).get("where", ""), (b.get("sponsor_evidence") or {}).get("tier", ""),
                        b.get("start_date", ""), b.get("primary_completion_date", ""),
                        (b.get("timing") or {}).get("relation", "")])
    print("devices:", len(per_device), "| queries missing from cache:", missing)
    print("class counts:", dict(collections.Counter(d["class"] for d in per_device)))


if __name__ == "__main__":
    main()
