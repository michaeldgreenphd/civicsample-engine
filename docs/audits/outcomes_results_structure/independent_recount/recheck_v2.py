"""Independent recount of the refined audit's comparative figures from the cached registry
responses. Written from the wording in definitions.json, without reading the audit's code."""
import gzip, json, re, sys, collections
cache, release = sys.argv[1], sys.argv[2]
studies = {}
with gzip.open(cache) as fh:
    for line in fh:
        obj = json.loads(line)
        for s in json.loads(obj["body"])["studies"]:
            studies[s["protocolSection"]["identificationModule"]["nctId"]] = s
rel = {r["nct_id"]: r for r in json.load(gzip.open(release))["data"]}
def numeric(x):
    try: float(str(x).replace(",", "")); return True
    except (TypeError, ValueError): return False
c = collections.Counter()
for nct, s in studies.items():
    p = s["protocolSection"]; d = p.get("designModule") or {}
    prim = [m for m in ((s.get("resultsSection") or {}).get("outcomeMeasuresModule") or {}).get("outcomeMeasures", []) if m.get("type") == "PRIMARY"]
    c1 = d.get("studyType") == "INTERVENTIONAL" and any(len(m.get("groups") or []) >= 2 for m in prim)
    c2 = c1 and (d.get("designInfo") or {}).get("allocation") == "RANDOMIZED"
    between = est = False
    for m in prim:
        ids = {g.get("id") for g in m.get("groups") or []}
        for a in m.get("analyses") or []:
            g = a.get("groupIds") or []
            if len(g) < 2 or not all(x in ids for x in g): continue
            has_p = bool(re.search(r"\d", str(a.get("pValue") or "")))
            has_ci = numeric(a.get("paramValue")) and numeric(a.get("ciLowerLimit")) and numeric(a.get("ciUpperLimit"))
            between |= has_p or has_ci; est |= has_ci
    r = rel.get(nct, {})
    c["n"] += 1; c["C1"] += c1; c["C2"] += bool(c2)
    if c2:
        c["C2_between"] += between; c["C2_est_ci"] += est
        ph3 = r.get("phase") == "PHASE3"
        c["C2_phase3"] += ph3; c["C2_phase3_between"] += ph3 and between; c["C2_phase3_est_ci"] += ph3 and est
        c["C2_race_and_est_ci"] += est and bool((r.get("race") or {}).get("reported"))
for k in sorted(c): print(f"{k:22s} {c[k]}")
