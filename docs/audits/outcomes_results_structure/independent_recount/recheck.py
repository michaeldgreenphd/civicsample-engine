"""Independent recount of the outcomes audit's headline figures from its cached registry
responses. Written without reading the audit's analysis code; definitions are stated here."""
import gzip, json, sys, collections
cache, release = sys.argv[1], sys.argv[2]
studies = {}
with gzip.open(cache) as fh:
    for line in fh:
        obj = json.loads(line)
        for s in (obj.get("studies") or (json.loads(obj["body"]) if isinstance(obj.get("body"), str) else (obj.get("body") or obj.get("response") or {})).get("studies") or ([obj] if "protocolSection" in obj else [])):
            studies[s["protocolSection"]["identificationModule"]["nctId"]] = s
race = {r["nct_id"]: bool((r.get("race") or {}).get("reported")) for r in json.load(gzip.open(release))["data"]}
MODS = ["baselineCharacteristicsModule", "participantFlowModule", "outcomeMeasuresModule", "adverseEventsModule", "moreInfoModule"]
c = collections.Counter(); two = collections.Counter()
def has_value(m):   # a measure "carries data" if any measurement has a value that is not empty or NA
    return any((x.get("value") not in (None, "", "NA")) for cl in m.get("classes", []) for cat in cl.get("categories", []) for x in cat.get("measurements", []))
for nct, s in studies.items():
    rs = s.get("resultsSection") or {}
    c["n"] += 1
    c["hasResults"] += bool(s.get("hasResults"))
    present = [m for m in MODS if rs.get(m)]
    c["all_five_modules"] += len(present) == 5
    c["four_substantive_modules"] += all(rs.get(m) for m in MODS[:4])
    for m in MODS: c["has_" + m] += bool(rs.get(m))
    prim = [m for m in (rs.get("outcomeMeasuresModule") or {}).get("outcomeMeasures", []) if m.get("type") == "PRIMARY"]
    c["primary_posted"] += bool(prim)
    c["primary_with_value"] += any(has_value(m) for m in prim)
    multi = any(len(m.get("groups", [])) >= 2 for m in prim)
    c["primary_2plus_groups"] += multi
    an = [a for m in prim for a in m.get("analyses", [])]
    c["primary_analysis"] += bool(an)
    c["primary_pvalue"] += any(a.get("pValue") not in (None, "") for a in an)
    c["primary_estimate_with_ci"] += any(a.get("paramValue") not in (None, "") and a.get("ciLowerLimit") not in (None, "") and a.get("ciUpperLimit") not in (None, "") for a in an)
    r = race.get(nct, False)
    c["race_reported"] += r
    two[(r, bool(an))] += 1
    c["primary_analysis_among_multi"] += bool(an) and multi
for k in sorted(c): print(f"{k:34s} {c[k]:5d}  {100*c[k]/c['n']:.1f}%")
print("2x2 (race reported, primary analysis):", dict(two))
print("among 2+ group primary:", c["primary_analysis_among_multi"], "/", c["primary_2plus_groups"])
