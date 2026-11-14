"""What the CURRENT record can and cannot say about departures from prospectively specified outcomes.

Run: python3 -I scripts/history_evidence.py V1_DIR OUTDIR
Current records hold one outcome list (the first audit found registered and posted lists identical),
so outcome switching cannot be assessed from them. This script only measures which of the inputs such
an assessment would need are visible in the current record of each of the 2,000 sampled trials:
 - registration timing: `protocolSection.statusModule.studyFirstSubmitDate` against
   `startDateStruct.date` and `primaryCompletionDateStruct.date`. A month-precision date (YYYY-MM) is
   compared at month precision, so "registered in the start month" counts as not after start.
 - whether the record was updated after results were first posted
   (`lastUpdatePostDateStruct.date` > `resultsFirstPostDateStruct.date`).
 - whether a protocol and/or statistical analysis plan is posted
   (`documentSection.largeDocumentModule.largeDocs[].hasProtocol / hasSap`), fetched here with
   `fields=NCTId,DocumentSection` (40 requests of 50 IDs, cached in cache/document_section.jsonl.gz).
No registry history version, protocol or SAP was opened: this is an availability count only.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common, ctgov
from common import frac

V1, OUT = sys.argv[1], sys.argv[2]
S, _ = common.load_sample(V1)
ids = [l.strip() for l in open(os.path.join(V1, 'sample_ids.txt')) if l.strip()]
cache = ctgov.Cache(OUT, 'document_section.jsonl.gz')
docs, failures = {}, []
for i in range(0, len(ids), 50):
    batch = ids[i:i + 50]
    url = ctgov.studies_url({'filter.ids': ','.join(batch), 'pageSize': 50, 'fields': 'NCTId,DocumentSection'})
    try:
        body, rec = cache.get(url, f'documents:{i // 50}')
    except RuntimeError as e:
        failures.append({'batch': i // 50, 'error': str(e)})
        continue
    for s in body.get('studies', []):
        docs[s['protocolSection']['identificationModule']['nctId']] = ((s.get('documentSection') or {}).get('largeDocumentModule') or {}).get('largeDocs') or []


def cmp_dates(a, b):
    """-1/0/1 comparing a to b at the coarser of the two precisions; None if either is missing."""
    if not a or not b:
        return None
    k = min(len(a), len(b))
    a, b = a[:k], b[:k]
    return (a > b) - (a < b)


rows = []
for n in ids:
    st = S[n]['protocolSection'].get('statusModule') or {}
    first = st.get('studyFirstSubmitDate')
    start = (st.get('startDateStruct') or {}).get('date')
    pcd = (st.get('primaryCompletionDateStruct') or {}).get('date')
    rfp = (st.get('resultsFirstPostDateStruct') or {}).get('date')
    lup = (st.get('lastUpdatePostDateStruct') or {}).get('date')
    d = docs.get(n)
    rows.append({'nct_id': n, 'first_submit': first, 'start': start, 'primary_completion': pcd,
                 'registered_vs_start': cmp_dates(first, start), 'registered_vs_primary_completion': cmp_dates(first, pcd),
                 'updated_after_results': cmp_dates(lup, rfp), 'docs_retrieved': d is not None,
                 'has_protocol': any(x.get('hasProtocol') for x in d or []), 'has_sap': any(x.get('hasSap') for x in d or []),
                 'results_first_post_year': (rfp or '')[:4]})
N = len(rows)
known = [r for r in rows if r['registered_vs_start'] is not None]
got = [r for r in rows if r['docs_retrieved']]
late = [r for r in rows if r['results_first_post_year'] >= '2017']
out = {'n': N, 'document_fetch_failures': failures, 'documents_retrieved': len(got),
       'start_date_missing': N - len(known),
       'registered_after_start': frac(sum(1 for r in known if r['registered_vs_start'] == 1), len(known)),
       'registered_on_or_before_start': frac(sum(1 for r in known if r['registered_vs_start'] <= 0), len(known)),
       'registered_after_primary_completion': frac(sum(1 for r in rows if r['registered_vs_primary_completion'] == 1), sum(1 for r in rows if r['registered_vs_primary_completion'] is not None)),
       'updated_after_results_first_posted': frac(sum(1 for r in rows if r['updated_after_results'] == 1), sum(1 for r in rows if r['updated_after_results'] is not None)),
       'protocol_posted': frac(sum(r['has_protocol'] for r in got), len(got)), 'sap_posted': frac(sum(r['has_sap'] for r in got), len(got)),
       'protocol_and_sap_posted': frac(sum(r['has_protocol'] and r['has_sap'] for r in got), len(got)),
       'protocol_posted_results_2017_or_later': frac(sum(r['has_protocol'] for r in late), len(late)),
       'sap_posted_results_2017_or_later': frac(sum(r['has_sap'] for r in late), len(late)),
       'prospective_and_protocol_and_sap': frac(sum(1 for r in rows if r['registered_vs_start'] is not None and r['registered_vs_start'] <= 0 and r['has_protocol'] and r['has_sap']), N)}
json.dump(out, open(os.path.join(OUT, 'history_evidence.json'), 'w'), indent=1)
with open(os.path.join(OUT, 'per_trial_history_evidence.jsonl'), 'w') as f:
    for r in rows:
        f.write(json.dumps(r) + '\n')
print(json.dumps(out, indent=1))
