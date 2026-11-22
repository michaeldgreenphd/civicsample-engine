# Fictional producer-to-browser demonstration (2026-10-28)

Every input is fictional: the fifteen trials are `tests/sex_gender_analysis/fixtures/cases.json`
wrapped as API v2 study documents by `make_fictional_pull.py`; the approved
enrichment export was written by the private repository's promotion tooling from
its fictional fixtures. No registry call, no provider call, no paid workflow,
no real snapshot was touched; the site clone used below is disposable.

Run from the engine root with the venv active, `D` an empty output directory
and `SITE` the frontend checkout (with the matching frontend branch applied).

```bash
python docs/sex_gender_analysis/demo/make_fictional_pull.py "$D" 2026-10-28
python scripts/build_sex_gender_table.py --from-raw "$D/sex_gender_raw_measures.jsonl.gz" \
    --out "$D/sex_gender_parsed.csv.gz" --meta "$D/sex_gender_parsed_meta.json" --write-back "$D/demographics.json"
python scripts/build_sex_gender_analysis.py --from-envelope "$D/sex_gender_raw_envelope.jsonl.gz" \
    --out "$D/sex_gender_analysis.csv.gz" --meta "$D/sex_gender_analysis_meta.json" --write-back "$D/demographics.json"
python -m src.enrichment_import --export tests/enrichment/fixtures/approved_export_from_private_tool_fictional.json \
    --demographics "$D/demographics.json" --snapshot-date 2026-10-28 --out "$D/demographics.json" --report "$D/enrichment_report.json"
python scripts/generate_mobile_data.py --demographics "$D/demographics.json" --table "$D/sex_gender_parsed.csv.gz" \
    --analysis-table "$D/sex_gender_analysis.csv.gz" --out "$D/dashboard-summary.json"
python scripts/generate_industry_sponsors.py "$D" --demographics "$D/demographics.json"
python scripts/write_sex_gender_methods.py --meta "$D/sex_gender_parsed_meta.json" --side-by-side /nonexistent --out-dir "$D/sex_gender"
```

Then the integration block of `verify.md` (clone the site, `split_data.py`
against the clone's `tests/record_contract.json`, prune, copy the dataset and
the generated files in, `check_site_contract.py --latest 2026-10-28`,
`first_view_parity.mjs`), serve the clone with `python -m http.server`, and run
`served_site_capture.cjs <shots-dir>` with `CHARTJS_VENDOR_DIR` pointing at local
copies of `chart.js@4.5.1` and `chartjs-plugin-datalabels@2.0.0` (the sandbox
cannot reach the CDN; the capture serves the same pinned versions from disk).
The capture opens `/?sg=v2`, the site's opt-in flag for the parser-v2 tabs.

## What the run produced (2026-10-28, engine c8eeb3b + this branch)

| step | result |
|---|---|
| extraction | 15 records; every record carries `sex_gender`, `sex_gender_analysis` (`sex_gender_analysis/1.0`) and `eligibility` (`eligibility/1.0`); raw measures and raw envelope stamped with the same `extracted_at` and `pipeline_commit` |
| parser table | 15 rows; the baseline comparison reports the expected large deviations from the real-data baseline (fictional input) and does not fail in non-strict mode |
| analysis sidecar | reporting dimension sex_only 6, both 2, gender_only 2, neither 5, parse_error 0; neither reasons 2/2/1; gender categories derived 2, not_applicable 11, excluded 2 |
| enrichment import | applied 3, quarantined 0; re-applying: applied 0, unchanged 3; a mismatched `--snapshot-date` is refused and writes nothing; canonical `conditions` unchanged |
| mobile summary | `sexGenderAnalysis` block present between `sexGender` and `firstView` |
| split + contract | 8 whole-record parts, `run.json` dated 2026-10-28; `check_site_contract`: ok, 15 records |
| first_view_parity | ok, no mismatches |
| served browser | desktop 1366px and phone 390px: Sex tab renders the reporting-dimension table, Gender tab the D3/D5 table (12 and 11 participants over 2 derived trials), the record pop-up the "Newer analysis" line, the study detail the "Additional reviewed evidence" section (phone: "Not included in the phone view", the detail class is not in the summary); no page errors |
| site tests in the clone | 468 of 475 pass; the 7 failures also fail with the committed frontend on this fictional dataset (industry-sponsor tests over a 4-trial sponsor file and a pruned snapshot, and one phone pop-up test over rows without a population field): a property of the fictional data, not of the change |
| real published pair | `check_site_contract --latest 2026-10-25`: ok, 80,614 records; `first_view_parity` with the real parts and the new app.js/index.html: ok; the real snapshot at 390px under `?sg=v2` shows "Newer analysis unavailable" on both tabs |
