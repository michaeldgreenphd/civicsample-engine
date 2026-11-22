"""Stage 1 of the fictional demonstration: fictional API-v2-shaped studies ->
extract_demographics_from_study -> demographics.json + raw measures + raw envelope,
stamped the way src/extract_all.main stamps them. No registry call is made."""
import gzip, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from src.extract_all import extract_demographics_from_study, write_raw_measures, RAW_MEASURES_FILENAME, RAW_ENVELOPE_FILENAME
from src import sex_gender_table as sgt
from src.utils import save_json, pipeline_commit

out = Path(sys.argv[1]); snapshot_date = sys.argv[2]
cases = json.load(open(Path(__file__).resolve().parents[3] / "tests" / "sex_gender_analysis" / "fixtures" / "cases.json"))["cases"]
CONDS = [["Fictional asthma"], ["Fictional type 2 diabetes"], ["Fictional breast cancer"], ["Fictional hypertension"], ["Fictional depression"]]
COUNTRIES = [("United States", "Boston", 42.36, -71.06), ("Canada", "Toronto", 43.65, -79.38), ("Germany", "Berlin", 52.52, 13.40)]

def study(i, case):
    nct = case["nct_id"]; year = 2012 + (i % 12)
    country, city, lat, lon = COUNTRIES[i % 3]
    measures = list(case["measures"])
    # a race measure so the Overview's race/ethnicity counts have something fictional to count
    measures.append({"title": "Race (NIH/OMB)", "paramType": "COUNT_OF_PARTICIPANTS", "unitOfMeasure": "Participants",
                     "classes": [{"categories": [
                         {"title": "White", "measurements": [{"groupId": "BG002", "value": str(max(1, int(case["enrollment"] * 0.6)))}]},
                         {"title": "Black or African American", "measurements": [{"groupId": "BG002", "value": str(max(1, int(case["enrollment"] * 0.3)))}]},
                         {"title": "Unknown or Not Reported", "measurements": [{"groupId": "BG002", "value": str(max(0, case["enrollment"] - max(1, int(case["enrollment"] * 0.6)) - max(1, int(case["enrollment"] * 0.3))))}]}]}]})
    return {"protocolSection": {
        "identificationModule": {"nctId": nct, "briefTitle": f"Fictional trial {i + 1} ({nct})", "officialTitle": f"Fictional trial {i + 1}"},
        "statusModule": {"overallStatus": "COMPLETED", "startDateStruct": {"date": f"{year}-03"},
                         "primaryCompletionDateStruct": {"date": f"{year + 2}-06"}, "completionDateStruct": {"date": f"{year + 2}-09"},
                         "studyFirstPostDateStruct": {"date": f"{year}-02-01"}, "resultsFirstPostDateStruct": {"date": f"{year + 3}-01-15"},
                         "lastUpdatePostDateStruct": {"date": f"{year + 3}-02-01"}},
        "sponsorCollaboratorsModule": {"leadSponsor": {"name": f"Fictional Sponsor {i % 4 + 1}", "class": ["INDUSTRY", "NIH", "OTHER", "OTHER_GOV"][i % 4]}},
        "conditionsModule": {"conditions": CONDS[i % 5]},
        "designModule": {"studyType": "INTERVENTIONAL", "phases": [["PHASE2", "PHASE3", "PHASE1"][i % 3]],
                         "enrollmentInfo": {"count": case["enrollment"], "type": "ACTUAL"}},
        "eligibilityModule": {"sex": ["ALL", "FEMALE", "MALE"][i % 3], "minimumAge": "18 Years", "maximumAge": "75 Years",
                              **({"genderBased": True, "genderDescription": "Fictional gender-based eligibility text"} if i % 4 == 0 else {})},
        "contactsLocationsModule": {"locations": [{"facility": f"Fictional Site {i + 1}", "city": city, "country": country, "geoPoint": {"lat": lat, "lon": lon}}]}},
        "resultsSection": {"baselineCharacteristicsModule": {
            "groups": [{"id": "BG000", "title": "Arm A"}, {"id": "BG001", "title": "Arm B"}, {"id": "BG002", "title": "Total"}],
            "denoms": [{"units": "Participants", "counts": [{"groupId": "BG000", "value": str(case["enrollment"] // 2)},
                                                            {"groupId": "BG001", "value": str(case["enrollment"] - case["enrollment"] // 2)},
                                                            {"groupId": "BG002", "value": str(case["enrollment"])}]}],
            "measures": measures}}}

studies = [study(i, c) for i, c in enumerate(cases)]
(out / "fictional_studies.json").write_text(json.dumps(studies, indent=1))
extracted_at = sgt.now_utc_iso(); commit = pipeline_commit()
results, raws, envs = [], [], []
for st in studies:
    r = extract_demographics_from_study(st, snapshot_date=snapshot_date)
    assert r is not None, st["protocolSection"]["identificationModule"]["nctId"]
    raw = sgt.select_raw_measures(st); raw["refetched"] = False
    env = sgt.select_raw_envelope(st); env["refetched"] = False
    for d in (raw, env):
        d["snapshot_date"] = snapshot_date; d["extracted_at"] = extracted_at; d["pipeline_commit"] = commit
    results.append(r); raws.append(raw); envs.append(env)
save_json(results, out / "demographics.json", extracted_at=extracted_at)
write_raw_measures(raws, out / RAW_MEASURES_FILENAME)
write_raw_measures(envs, out / RAW_ENVELOPE_FILENAME)
print(json.dumps({"studies": len(results), "extracted_at": extracted_at, "pipeline_commit": commit,
                  "keys_on_record": sorted(results[0].keys()),
                  "sex_gender_analysis_on_record": results[0].get("sex_gender_analysis", {}).get("schema_version"),
                  "eligibility_on_record": results[0].get("eligibility")}, indent=1))
