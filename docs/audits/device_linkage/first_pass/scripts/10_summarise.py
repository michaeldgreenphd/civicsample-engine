#!/usr/bin/env python3
"""Roll the Route B/C classification up into the numbers and lists the report
quotes. Reads out/route_b_classified.json only.

usage: python3 -I 10_summarise.py <route_b_classified.json> <out.json> <out.md>
"""
from __future__ import annotations

import collections
import json
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import GENERIC_DEVICE_WORDS, norm_text  # noqa: E402


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(100 * (c - h), 1), round(100 * (c + h), 1))


def main() -> None:
    D = json.load(open(sys.argv[1]))
    n = len(D)
    S: dict = {"n_devices": n}
    cls = collections.Counter(d["class"] for d in D)
    S["class_counts"] = dict(cls)
    roll = {"i": cls["i"], "ii": cls["ii"] + cls["i-f"] + cls["ii-c"], "iii": cls["iii"],
            "iv": cls["iv-a"] + cls["iv-b"]}
    S["class_rollup"] = roll
    S["class_rollup_wilson95_pct"] = {k: wilson(v, n) for k, v in roll.items()}
    S["class_by_stratum"] = {}
    for d in D:
        S["class_by_stratum"].setdefault(d["stratum"], collections.Counter())[d["class"]] += 1
    S["class_by_stratum"] = {k: dict(v) for k, v in sorted(S["class_by_stratum"].items())}
    S["class_by_pathway"] = {}
    for d in D:
        S["class_by_pathway"].setdefault(d["pathway"], collections.Counter())[d["class"]] += 1
    S["class_by_pathway"] = {k: dict(v) for k, v in S["class_by_pathway"].items()}
    S["class_by_name_generic"] = {}
    for d in D:
        S["class_by_name_generic"].setdefault(str(d["name_forms"]["generic"]), collections.Counter())[d["class"]] += 1
    S["class_by_name_generic"] = {k: dict(v) for k, v in S["class_by_name_generic"].items()}

    # hits per query
    labels = ["n_intr_raw", "n_term_raw", "n_intr_core", "n_term_core", "s_spons_l1", "s_lead_l1",
              "s_spons_brand", "c_term_spons", "c_intr_spons"]
    q = {}
    for lab in labels:
        vals = [d["query_counts"][lab] for d in D if isinstance(d["query_counts"].get(lab), int)]
        if vals:
            q[lab] = {"devices_queried": len(vals), "zero_hits": sum(1 for v in vals if v == 0),
                      "ge1": sum(1 for v in vals if v > 0), "gt10_truncated_at_page": sum(1 for v in vals if v > 10),
                      "ge50_flood": sum(1 for v in vals if v >= 50), "median": statistics.median(vals), "max": max(vals)}
    S["hits_per_query"] = q
    # best-of name queries (raw or core)
    def best(d, a, b):
        x = [d["query_counts"].get(k) for k in (a, b)]
        x = [v for v in x if isinstance(v, int)]
        return max(x) if x else 0
    S["devices_any_intr_hit"] = sum(1 for d in D if best(d, "n_intr_raw", "n_intr_core") > 0)
    S["devices_any_term_phrase_hit"] = sum(1 for d in D if best(d, "n_term_raw", "n_term_core") > 0)
    S["devices_any_name_hit"] = sum(1 for d in D if d["max_name_query_count"] > 0)
    S["devices_core_differs_from_raw"] = sum(1 for d in D if "n_intr_core" in d["query_counts"])
    S["devices_raw_zero_but_core_hits"] = sum(
        1 for d in D if "n_intr_core" in d["query_counts"]
        and max(d["query_counts"]["n_intr_raw"], d["query_counts"]["n_term_raw"]) == 0
        and max(d["query_counts"]["n_intr_core"], d["query_counts"]["n_term_core"]) > 0)
    S["devices_any_sponsor_hit"] = sum(1 for d in D if any(
        isinstance(d["query_counts"].get(k), int) and d["query_counts"][k] > 0
        for k in ("s_spons_l1", "s_lead_l1", "s_spons_brand")))
    S["devices_sponsor_l1_hit"] = sum(1 for d in D if d["query_counts"].get("s_spons_l1", 0) > 0)
    S["devices_lead_l1_hit"] = sum(1 for d in D if d["query_counts"].get("s_lead_l1", 0) > 0)
    S["floods"] = [{"submission_number": d["submission_number"], "device": d["device"], "class": d["class"],
                    "counts": {k: v for k, v in d["query_counts"].items() if k.startswith("n_")}}
                   for d in D if d["flood"]]
    S["generic_names_in_sample"] = sum(1 for d in D if d["name_forms"]["generic"])

    # name-matched studies whose sponsor does not match: how many distinct lead sponsors (collision signal)
    S["name_match_other_sponsor"] = [
        {"submission_number": d["submission_number"], "device": d["device"], "matched_form": sorted({c["name_evidence"]["form"] for c in d["candidates"] if c["class"] == "ii"}),
         "n_studies": sum(1 for c in d["candidates"] if c["class"] == "ii"),
         "lead_sponsors": sorted({c["lead_sponsor"] for c in d["candidates"] if c["class"] == "ii"})[:8],
         "intervention_types": sorted({i["type"] or "?" for c in d["candidates"] if c["class"] == "ii" for i in c["interventions"]})}
        for d in D if any(c["class"] == "ii" for c in d["candidates"])]

    # class iii anatomy
    iii = [d for d in D if d["class"] == "iii"]
    tier = collections.Counter()
    for d in iii:
        tiers = {c["sponsor_evidence"]["tier"] for c in d["candidates"] if c["class"] == "iii"}
        tier["exact" if "exact" in tiers else "brand" if "brand" in tiers else "family_only"] += 1
    S["class_iii_best_sponsor_tier"] = dict(tier)
    S["class_iii_with_name_in_intervention_description"] = sum(
        1 for d in iii if any((c["name_evidence"] or {}).get("strength") == "description_only" for c in d["candidates"]))
    S["class_iii_with_company_only_name_in_intervention_or_title"] = sum(
        1 for d in iii if any((c["name_evidence"] or {}).get("strength") == "company_only" for c in d["candidates"]))
    S["class_iii_name_generic"] = sum(1 for d in iii if d["name_forms"]["generic"])
    S["class_iii_sponsor_role"] = dict(collections.Counter(
        "lead" if any(c["sponsor_evidence"]["role"] == "lead" for c in d["candidates"] if c["class"] == "iii")
        else "collaborator_only" for d in iii))

    # class iii near-misses: a sponsor-matched study whose intervention name or
    # title shares a distinctive token (>=4 chars, not a device-type word, not
    # the company's own brand) with the listed device name. This is the
    # "same family, different generation/label" signal ("Ceevra Reveal" for
    # "Ceevra Reveal 3+"); it is a pointer for a reviewer, not a link.
    near = []
    for d in iii:
        brand = set(d["company_brand"].split())
        toks = {t for f in d["name_forms"]["full"] + d["name_forms"]["trimmed"] for t in f.split()
                if len(t) >= 4 and t not in GENERIC_DEVICE_WORDS and t not in brand}
        hit = None
        for c in d["candidates"]:
            if c["class"] != "iii":
                continue
            text = set(norm_text(" ".join([c["brief_title"], c["official_title"]] +
                                          [i["name"] for i in c["interventions"]] +
                                          [o for i in c["interventions"] for o in i["other_names"]])).split())
            shared = sorted(toks & text)
            if shared:
                hit = {"submission_number": d["submission_number"], "device": d["device"], "nct_id": c["nct_id"],
                       "shared_tokens": shared, "interventions": [i["name"] for i in c["interventions"]][:3],
                       "brief_title": c["brief_title"]}
                break
        if hit:
            near.append(hit)
    S["class_iii_near_miss_shared_distinctive_token"] = len(near)
    S["class_iii_near_miss_list"] = near

    # class i: which queries surfaced them, evidence location, timing
    pairs = [(d, c) for d in D for c in d["candidates"] if c["class"] == "i"]
    S["class_i_devices"] = cls["i"]
    S["class_i_study_pairs"] = len(pairs)
    S["class_i_pairs_per_device"] = {d["submission_number"]: d["n_by_class"].get("i", 0) for d in D if d["class"] == "i"}
    S["class_i_found_by_query"] = dict(collections.Counter(lab for _, c in pairs for lab in set(c["from_queries"])))
    S["class_i_found_only_by_sponsor_queries"] = sum(1 for _, c in pairs if all(l.startswith("s_") for l in c["from_queries"]))
    S["class_i_name_where"] = dict(collections.Counter(c["name_evidence"]["where"] for _, c in pairs))
    S["class_i_name_strength"] = dict(collections.Counter(c["name_evidence"]["strength"] for _, c in pairs))
    S["class_i_sponsor_tier_role"] = dict(collections.Counter(
        c["sponsor_evidence"]["tier"] + "/" + c["sponsor_evidence"]["role"] for _, c in pairs))
    S["class_i_timing_pairs"] = dict(collections.Counter(c["timing"]["relation"] for _, c in pairs))
    S["class_i_study_type_pairs"] = dict(collections.Counter(c["study_type"] for _, c in pairs))
    S["class_i_has_results_pairs"] = dict(collections.Counter(str(c["has_results"]) for _, c in pairs))
    PRE = "primary completion on/before decision"
    MID = "started before decision, primary completion after (or missing)"
    dev_t = collections.Counter()
    for d in D:
        if d["class"] != "i":
            continue
        rels = {c["timing"]["relation"] for c in d["candidates"] if c["class"] == "i"}
        dev_t["has >=1 study completed on/before decision" if PRE in rels else
              "none completed before; >=1 ongoing at decision" if MID in rels else
              "all class (i) studies started after decision"] += 1
    S["class_i_timing_devices"] = dict(dev_t)
    json.dump(S, open(sys.argv[2], "w"), indent=1)

    # markdown lists
    L = []
    L.append("### All class (i) candidates (device x study pairs)\n")
    L.append("| # | Submission | Device (company) | FDA decision | NCT ID | Lead sponsor (matching party) | Name evidence | Start | Primary completion | Start - decision (y) | Relation |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for d, c in pairs:
        se, ne, t = c["sponsor_evidence"], c["name_evidence"], c["timing"]
        L.append(f"| {d['sample_id']} | {d['submission_number']} | {d['device'][:45]} ({d['company'][:28]}) | {d['decision_date']} | {c['nct_id']} | "
                 f"{c['lead_sponsor'][:34]} ({se['tier']}, {se['role']}) | \"{ne['form']}\" in {ne['where'].replace('_', ' ')} ({ne['strength']}) | "
                 f"{c['start_date'] or '-'} | {c['primary_completion_date'] or '-'} | {t['start_minus_decision_years']} | {t['relation']} |")
    L.append("\n### Class (ii) / (ii-c) best candidates (all)\n")
    L.append("| # | Submission | Device (company) | NCT ID | Brief title | Lead sponsor | Name evidence | n such studies |")
    L.append("|---|---|---|---|---|---|---|---|")
    for d in D:
        if d["class"] in ("ii", "ii-c", "i-f"):
            c, ne = d["best"], d["best"]["name_evidence"]
            L.append(f"| {d['sample_id']} | {d['submission_number']} | {d['device'][:40]} ({d['company'][:26]}) | {c['nct_id']} | {c['brief_title'][:70]} | "
                     f"{c['lead_sponsor'][:32]} | \"{ne['form']}\" in {ne['where'].replace('_', ' ')} ({ne['strength']}) | {d['n_by_class'].get(d['class'], 0)} |")
    L.append("\n### Class (iii) best candidates (all)\n")
    L.append("| # | Submission | Device (company) | NCT ID | Brief title | Matching sponsor (tier, role) | Interventions (first 2) | Name elsewhere? | n sponsor-matched studies in top 10s |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for d in D:
        if d["class"] == "iii":
            c, se = d["best"], d["best"]["sponsor_evidence"]
            ne = c["name_evidence"]
            L.append(f"| {d['sample_id']} | {d['submission_number']} | {d['device'][:40]} ({d['company'][:26]}) | {c['nct_id']} | {c['brief_title'][:60]} | "
                     f"{se['sponsor'][:32]} ({se['tier']}, {se['role']}) | {'; '.join(i['name'][:28] for i in c['interventions'][:2]) or '-'} | "
                     f"{(ne['strength'] + ': ' + ne['form']) if ne else 'no'} | {d['n_by_class'].get('iii', 0)} |")
    L.append("\n### Class (iv) devices\n")
    L.append("| # | Submission | Device (company) | Sub-class | intr / phrase hits (raw; core) |")
    L.append("|---|---|---|---|---|")
    for d in D:
        if d["class"].startswith("iv"):
            qc = d["query_counts"]
            L.append(f"| {d['sample_id']} | {d['submission_number']} | {d['device'][:50]} ({d['company'][:28]}) | {d['class']} | "
                     f"{qc.get('n_intr_raw')} / {qc.get('n_term_raw')}; {qc.get('n_intr_core', '-')} / {qc.get('n_term_core', '-')} |")
    open(sys.argv[3], "w").write("\n".join(L) + "\n")
    print(json.dumps(S, indent=1))


if __name__ == "__main__":
    main()
