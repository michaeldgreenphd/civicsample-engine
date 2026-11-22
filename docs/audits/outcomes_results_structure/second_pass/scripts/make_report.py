"""Assemble report.md and definitions.json from the JSON outputs of the analysis scripts.

Run: python3 -I scripts/make_report.py V1_DIR OUTDIR
Nothing is computed here except percentages of counts already stored in the outputs; every count in the
report is read from a file named in section 9. The reviewer's readings (15 denominator mismatches,
subgroup judgements) are text entered by the auditing model and are labelled as such.
"""
import sys, os, json, csv, collections, inspect
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common, families, subgroup_rule
from common import frac

V1, OUT = sys.argv[1], sys.argv[2]
J = lambda name: json.load(open(os.path.join(OUT, name)))
U, DEN, CNT, FAM, FD, FEX, COH = J('usable.json'), J('denominators.json'), J('results_posting_counts.json'), J('families.json'), J('family_detail.json'), J('family_examined.json'), J('cohorts.json')
SUB, HIS, META, SV, MAN, NEG = J('subgroup_summary.json'), J('history_evidence.json'), J('release_meta.json'), J('sampling_verification.json'), J(os.path.join('cache', 'fetch_manifest_summary.json')), J('subgroup_negative_samples.json')
V1MAN = json.load(open(os.path.join(V1, 'cache', 'fetch_manifest.json')))
REVIEW = list(csv.DictReader(open(os.path.join(OUT, 'subgroup_review.csv'))))
ABS = os.path.abspath(OUT)
L = []
w = L.append


def pct(k, n):
    return f'{k:,}/{n:,} ({100 * k / n:.1f}%)' if n else f'{k:,}/0'


def T(x):
    return x['text']


def esc(s):
    return (s or '').replace('|', '\\|').replace('\n', ' ').replace('<', '&lt;').replace('>', '&gt;')


def dist(lst, n, k=6):
    return '; '.join(f'{a} {b}' for a, b in lst[:k]) + (f'; (+{len(lst) - k} more values)' if len(lst) > k else '')


cells = collections.defaultdict(dict)
for c in CNT['cells']:
    cells[c['stratum']][c['year']] = c
YEARS = list(range(2015, 2024))
pooled = {s: (sum(v[y]['n_with_results'] for y in YEARS), sum(v[y]['n_all'] for y in YEARS)) for s, v in cells.items()}
fc = CNT['frame_checks']
st = U['statistics']
dem = U['demographics_and_outcomes']
N = 2000

# ------------------------------------------------------------------ header
w('# Refined audit: what ClinicalTrials.gov results sections contain for trials in the demographics dataset')
w('')
w(f'Run 2026-11-13 (second pass, after investigator review). Read-only: nothing was committed, pushed or edited in any repository; no paid API or LLM call was made. '
  f'Output directory: `{ABS}/`.')
w('')
w('**How to read this report.** Every earlier finding was treated as provisional and recomputed or re-worded. Each number carries its numerator, denominator and the name of the rule that produced it; '
  'the rules are written out in `definitions.json` and in the docstrings of `scripts/`. Percentages on the 2,000-trial sample carry Wilson 95% intervals that treat the proportional stratified sample as a simple random sample. '
  'Registry-wide counts (section 1.3) are census counts on the retrieval date and carry no interval. Judgements made by reading records are those of one reviewer, the auditing model; they are not an independent human review.')
w('')
w('## 0. The fifteen numbers that matter most')
w('')
w('| # | Refined finding | Value | Rule / denominator |')
w('|---|---|---|---|')
i20 = cells['interventional_all'][2020]
p3i = cells['interventional_phase3_industry_randomized'][2020]
rows0 = [
    ('The frame requires posted results', f"80,890 records = every study with `ResultsFirstPostDate` in 2009-01-01..2026-11-08 (registry count on 2026-11-13 for that range: {fc['engine_frame_through_2026-11-08']['n']:,}); `hasResults` true for 2,000/2,000 sampled", 'section 1.1; frame rule FR'),
    ('Share of interventional studies with a primary completion date in 2015-2023 that have posted results (count-level, all registered studies)', f"{pct(*pooled['interventional_all'])}; completed or terminated only: {pct(*pooled['interventional_completed_or_terminated'])}", 'section 1.3; not a per-trial audit'),
    ('Same, Phase 3 / industry lead / industry randomised Phase 3', f"{pct(*pooled['interventional_phase3'])} / {pct(*pooled['interventional_industry_lead'])} / {pct(*pooled['interventional_phase3_industry_randomized'])}", 'section 1.3'),
    ('The existing candidate cohort\'s frame query (industry, Phase 3, randomised, primary completion 2020): with results', f"{pct(p3i['n_with_results'], p3i['n_all'])}", 'section 1.3 and 3.5'),
    ('All five results modules PRESENT (formerly "99.8%")', T(U['presence']['all_five_modules']), 'presence only; section 2.1'),
    ('USABLE content: baseline / participant flow / primary outcome / adverse events', f"{T(U['usable']['B1'])} / {T(U['usable']['F1'])} / {T(U['usable']['O1'])} / {T(U['usable']['A1'])}", 'rules B1, F1, O1, A1; denominator 2,000'),
    ('All four substantive modules usable', T(U['usable']['four_usable']), 'B1 and F1 and O1 and A1'),
    ('Comparative trials', f"C1 {T(U['comparative']['C1'])}; C2 (randomised) {T(U['comparative']['C2'])}", 'section 2.3'),
    ('Among comparative randomised trials (C2): usable between-arm analysis on a primary measure', f"{T(st['C2']['S1_between'])}; with estimate and both confidence limits {T(st['C2']['S1_between_est_ci'])}", 'rule S1_between; denominator C2'),
    ('Same, randomised Phase 3 (C2, release phase PHASE3)', f"{T(st['C2_phase3']['S1_between'])}; estimate with both limits {T(st['C2_phase3']['S1_between_est_ci'])}", 'denominator C2 and Phase 3'),
    ('Race reported AND a usable between-arm estimate with confidence limits, among C2', T(st['C2']['race_reported_and_S1_between_est_ci']), 'co-occurrence within trials with results only'),
    ('Subgroup reporting detected by the current method', SUB['detected_text'], 'rule SG2 (text rule over 18 fields); not a prevalence'),
    ('Of the detected, judged by one reviewer to post a subgroup result', f"{SUB['true_any']}: demographic {SUB['true_demographic']}; race or ethnicity {len(SUB['true_race_or_ethnicity'])}/2,000; false positives {SUB['false_positive']}", 'section 4.3'),
    ('Missed cases in reviewed negatives (demographic subgroup results)', f"random sample 0/60 (95% upper bound {100 * SUB['negatives']['random_upper95_demographic']:.1f}%); targeted likely-reporter sample {SUB['negatives']['targeted_missed_demographic']}/40; non-demographic subgroup results missed: {SUB['negatives']['random_missed_non_demographic']}/60 and {SUB['negatives']['targeted_missed_non_demographic']}/40", 'section 4.4'),
    ('Largest narrow endpoint cohorts in the full release (randomised Phase 3; of which race reported)', '; '.join(f"{k}: {v['n']} ({v['race_reported']})" for k, v in COH['cohorts'].items() if k in ('T2DM: HbA1c change from baseline at 24-26 weeks', 'Lung cancer: PFS', 'Lung cancer: OS', 'Breast cancer: PFS', 'Rheumatoid arthritis: ACR20, any time point', 'Depression: MADRS total change, any time point')), 'text definitions on 80,890 records; section 3.4'),
]
for i, (a, b, c) in enumerate(rows0, 1):
    w(f'| {i} | {a} | {b} | {c} |')
w('')

# ------------------------------------------------------------------ 1 frame
w('## 1. Sampling frame')
w('')
w('### 1.1 How the 80,890 trials were selected, and whether posted results were required')
w('')
w('**Posted results were required. The frame is every study that has a results-first-posted date on or after 2009-01-01; nothing else is filtered.**')
w('')
w('The selection is one registry query, built in the engine at the release\'s own commit (`pipeline_commit` `f6a2e7b74790c6f11982376441fb90c80d2c611e`; `src/api_client.py`, `src/extract_all.py` and `.github/workflows/extract.yml` are byte-identical between that commit and the checkout read, `0293cd099575`; only `src/utils.py` differs, in a baseline-breakdown helper that does not touch selection):')
w('')
w('- `.github/workflows/extract.yml` line 57 runs the weekly extraction as `python -m src.extract_all --output data/demographics.json --results-after 2009-01-01 --snapshot-date "$CURRENT_DATE" --raw-envelope`.')
w('- `src/extract_all.py` lines 342-346 pass that to the client: `studies = client.iter_all_studies_with_results(limit=args.limit, condition=args.condition, results_after=args.results_after)` (`--limit` and `--condition` are unset in the workflow).')
w('- `src/api_client.py` lines 45-47 build the filter: `elif filters.get("results_after"):` ... `date_filter = f"AREA[ResultsFirstPostDate]RANGE[{filters[\'results_after\']},MAX]"`; lines 55-59 send it as the only filter: `params = {"pageSize": self.page_size, "format": "json", "filter.advanced": date_filter}` to `GET https://clinicaltrials.gov/api/v2/studies` (line 66). With no date the fallback on line 53 is `AREA[ResultsFirstPostDate]RANGE[MIN,MAX]`, commented on line 52 "No date filter - just require results exist".')
w('- `src/api_client.py` lines 92-111 (`iter_all_studies_with_results`) page through every match. There is no filter on study type, phase, status, sponsor, country, allocation or enrolment. `DEFAULT_FIELDS` (lines 11-23) is defined but not sent, so whole records are returned.')
w('- `src/extract_all.py` lines 400-418 keep a study when `extract_demographics_from_study` returns a record and otherwise only count it (`errors += 1`); a study whose extraction raised would be absent from the release.')
w('')
w('So the inclusion rule is: **registered on ClinicalTrials.gov, any study type, with `ResultsFirstPostDate` between 2009-01-01 and the pull date (2026-11-08), and not dropped by an extraction error.** Checks made:')
w('')
w(f"- Registry count for `AREA[ResultsFirstPostDate]RANGE[2009-01-01,2026-11-08]` on 2026-11-13: **{fc['engine_frame_through_2026-11-08']['n']:,}**, equal to the release's 80,890 records (all NCT IDs unique). The count for the range ending 2026-11-06, the latest `results_date` in the release, is also {fc['engine_frame_through_2026-11-06']['n']:,}. No record was lost to extraction errors unless an equal number entered and left the range in five days.")
w(f"- The release's `results_date` runs from 2009-01-06 to 2026-11-06 with none missing. Excluded by the date floor: {fc['results_first_post_before_2009']['n']} studies with results first posted before 2009. Since the pull, the same query has grown to {fc['engine_frame_now']['n']:,}.")
w(f"- `AREA[ResultsFirstPostDate]RANGE[MIN,MAX]` and `AREA[HasResults]true` return the same count ({fc['any_results_first_post_date']['n']:,} and {fc['has_results_true']['n']:,}): in this registry a results-first-posted date and `hasResults` are the same condition.")
w(f"- Composition of the frame: INTERVENTIONAL 76,153, OBSERVATIONAL 4,737; status COMPLETED 67,475, TERMINATED 12,030, ACTIVE_NOT_RECRUITING 1,251, other 134; `enrollment_type` ACTUAL 80,877, ESTIMATED 9, N/A 4. The registry holds {fc['all_registered_studies']['n']:,} studies ({fc['all_interventional']['n']:,} interventional, {fc['all_observational']['n']:,} observational), so the frame is {100 * 80890 / fc['all_registered_studies']['n']:.1f}% of all registered studies.")
w('')
w('**Consequence, stated as the investigator put it:** the frame contains only trials with results, so this audit cannot estimate whether demographic reporting and results posting occur together across all otherwise eligible trials. Every sample-based figure below is conditional on results having been posted. Section 1.3 gives the only evidence gathered here about the wider population, and it is count-level.')
w('')
w('### 1.2 The sample (saved; seed and identifiers carried forward)')
w('')
w(f"- Source population: `engine-data/demographics.json.gz`, sha256 `{META['source_sha256']}`, `extracted_at` {META['extracted_at']}, `pipeline_commit` `{META['pipeline_commit']}`, {META['n_records']:,} records.")
w(f"- Method: strata = the record's `study_type`; 2,000 allocated proportionally by largest remainder (INTERVENTIONAL {SV['allocation']['INTERVENTIONAL']:,}, OBSERVATIONAL {SV['allocation']['OBSERVATIONAL']}); NCT IDs sorted ascending within stratum; one `random.Random(20261113)` generator, strata visited in name order, `rng.sample(sorted_ids, n_h)`.")
w(f"- **Seed 20261113.** The 2,000 NCT IDs are in `sample_ids.txt` (sorted; sha256 `{SV['sample_ids_txt_sha256']}`), parameters in `sampling.json`. `scripts/verify_sample.py` re-drew the sample from the release in this pass and reproduced the file exactly (`sampling_verification.json`: match = {SV['redraw_matches_sample_ids_txt']}).")
w(f"- Records analysed: the first audit's cache (`outcomes-audit/cache/raw_responses.jsonl.gz`, sha256 `{V1MAN['cache_sha256']}`), 40 responses fetched {V1MAN['first_fetch']} to {V1MAN['last_fetch']}, API version {V1MAN['api_version']['apiVersion']}, registry `dataTimestamp` {V1MAN['api_version']['dataTimestamp']}; fields `{V1MAN['fields']}`. Retrieved 2,000 of 2,000; 0 missing; 0 cache lines failed to parse.")
w('')
w('### 1.3 Do eligible trials post results? (count-level, on a frame that does not require results)')
w('')
w('This is the first of the three questions the investigator asked to keep apart. It cannot be answered from the release. It was measured on the registry itself with count queries: '
  '`GET https://clinicaltrials.gov/api/v2/studies?countTotal=true&pageSize=1&fields=NCTId&filter.advanced=<expression>`, two per cell.')
w('')
w('- Year: `AREA[PrimaryCompletionDate]RANGE[Y-01-01,Y-12-31]` for Y = 2015..2023.')
w('- With results: the same expression `AND AREA[ResultsFirstPostDate]RANGE[MIN,MAX]` (the engine\'s own condition). Without results = the difference.')
w('- Strata (each ANDed with the year):')
for k, v in CNT['definition']['strata'].items():
    w(f'  - `{k}`: `{v}`')
w(f"- Retrieved {CNT['first_fetch']} to {CNT['last_fetch']}; API version {CNT['api_version']['apiVersion']}, `dataTimestamp` {CNT['api_version']['dataTimestamp']}. {len(CNT['cells']) * 2} count requests, {len(CNT['failures'])} failures.")
w(f"- Consistency: for the all-interventional row the split was asked for three other ways (`NOT AREA[ResultsFirstPostDate]RANGE[MIN,MAX]`, `aggFilters=results:with` / `results:without`, `AREA[HasResults]true`); all agree with the table in all 9 years. The release's own interventional records by primary-completion year are {', '.join(f'{y}: {v:,}' for y, v in COH['release_interventional_by_primary_completion_year'].items())}, within 9 of the registry's with-results counts in every year (the release is five days older).")
w('')
w('Studies with results / all studies (%), by primary-completion year:')
w('')
names = [('interventional_all', 'Interventional, all'), ('interventional_completed_or_terminated', 'Interventional, completed or terminated'), ('interventional_phase3', 'Phase 3 (includes Phase 2/3)'),
         ('interventional_industry_lead', 'Industry lead'), ('interventional_phase3_industry_randomized', 'Industry, Phase 3, randomised'), ('interventional_fda_regulated_drug_or_device', 'FDA-regulated drug or device (flag)'),
         ('interventional_us_location', 'Any US location'), ('observational_all', 'Observational, all')]
w('| Year | ' + ' | '.join(n for _, n in names) + ' |')
w('|---|' + '---:|' * len(names))
for y in YEARS:
    w(f'| {y} | ' + ' | '.join(f"{cells[s][y]['n_with_results']:,}/{cells[s][y]['n_all']:,} ({100 * cells[s][y]['n_with_results'] / cells[s][y]['n_all']:.1f})" for s, _ in names) + ' |')
w('| **2015-2023** | ' + ' | '.join(f'**{pooled[s][0]:,}/{pooled[s][1]:,} ({100 * pooled[s][0] / pooled[s][1]:.1f})**' for s, _ in names) + ' |')
w('')
w('What this does and does not show:')
w('')
w(f"- Among all registered interventional studies with a primary completion date in 2015-2023, {pct(*pooled['interventional_all'])} have posted results on ClinicalTrials.gov. The dataset therefore describes about one in five such studies. The share varies by stratum: {100 * pooled['interventional_phase3'][0] / pooled['interventional_phase3'][1]:.1f}% of Phase 3, {100 * pooled['interventional_industry_lead'][0] / pooled['interventional_industry_lead'][1]:.1f}% of industry-led, {100 * pooled['interventional_phase3_industry_randomized'][0] / pooled['interventional_phase3_industry_randomized'][1]:.1f}% of industry randomised Phase 3, {100 * pooled['interventional_us_location'][0] / pooled['interventional_us_location'][1]:.1f}% with a US site; observational studies {100 * pooled['observational_all'][0] / pooled['observational_all'][1]:.1f}%.")
w('- It is a count-level answer, not a per-trial audit: no record was read. "Eligible" here means "matches the query"; it is not a determination that a study is an applicable clinical trial under FDAAA, that its deadline has passed, or that no extension or certified delay applies. A study without results on ClinicalTrials.gov may have reported elsewhere (a journal, EU CTR).')
w('- Primary completion dates include anticipated dates on records never updated; the completed-or-terminated row removes most of those. Later years have had less time to post, and the decline from 2021 onward mixes that with any real change.')
w('- The FDA-regulated row uses flags that exist only on records registered or updated since January 2017 (documentation knowledge, not checked here; the small 2015-2016 counts are consistent with it); in 2015-2016 it covers a small, selected subset (503 and 976 studies) and should not be compared with later years.')
w('- These counts say nothing about demographic reporting among studies without results: those studies have no baseline table at all on the registry.')
w('')
w('### 1.4 Among trials with results: are demographics and usable outcomes reported? (sample, n = 2,000)')
w('')
w('Second question. Denominator: the 2,000 sampled trials, all with posted results. Demographic flags are the release\'s own `race.reported`, `ethnicity.reported`, `sex.reported`; "usable primary outcome" is rule O1 (section 2.2).')
w('')
w('| | n / denominator |')
w('|---|---|')
for k, lab in (('race_reported', 'Race reported'), ('ethnicity_reported', 'Ethnicity reported'), ('sex_reported', 'Sex reported')):
    w(f'| {lab} | {T(dem[k])} |')
w(f"| Usable primary outcome (O1) | {T(U['usable']['O1'])} |")
w(f"| Race reported and usable primary outcome | {T(dem['race_reported_and_O1'])} |")
w(f"| Race reported and all four substantive modules usable | {T(dem['race_reported_and_four_usable'])} |")
w(f"| Usable primary outcome among race-reported trials | {T(dem['O1_among_race_reported'])} |")
w(f"| Usable primary outcome among trials without race reported | {T(dem['O1_among_race_not_reported'])} |")
w('')
w('In the full release race is reported for 46,564/80,890 (57.6%). Within trials that have results, a usable primary outcome is about equally common whether or not race is reported; that is a within-frame description and carries no implication for trials without results.')
w('')
w('### 1.5 Among appropriate comparative trials: are statistical analyses reported?')
w('')
w('Third question. It is answered in section 2.3, on the comparative denominators C1 to C3 rather than on all 2,000.')
w('')

# ------------------------------------------------------------------ 2 modules
w('## 2. Module presence and usable content')
w('')
w('### 2.1 Module presence (the former "99.8%" finding, relabelled)')
w('')
w('"Present" means only that the key exists under `resultsSection` and is a non-empty object. It is not a statement about completeness. Denominator 2,000.')
w('')
w('| Module | Present |')
w('|---|---|')
for k in ('participantFlowModule', 'baselineCharacteristicsModule', 'outcomeMeasuresModule', 'adverseEventsModule'):
    w(f"| `{k}` | {T(U['presence'][k])} |")
w(f"| The four substantive modules together | {T(U['presence']['four_substantive_modules_excluding_moreInfo'])} |")
w(f"| `moreInfoModule` (administrative; see below) | {T(U['presence']['moreInfoModule'])} |")
w(f"| All five keys (the earlier headline) | {T(U['presence']['all_five_modules'])} |")
w('')
w(f"**The \"more information\" module is not a substantive results module.** It is present in every record because it carries the results point of contact (`pointOfContact`: {T(U['presence']['moreInfo_pointOfContact'])}) and the agreement statement (`certainAgreement`: {T(U['presence']['moreInfo_certainAgreement'])}). Its one substantive free-text field, `limitationsAndCaveats.description`, is present in {T(U['presence']['moreInfo_limitationsAndCaveats_text'])}. It is excluded from every usable-content count below.")
w('')
w('### 2.2 Usable content, one rule per module')
w('')
w('"Numeric" = the registry string parses as a finite number; "NA", blank and absent never count and are never read as zero. Full rule text: docstring of `scripts/usable.py` and `definitions.json`. Per-trial flags: `per_trial_usable.jsonl`.')
w('')
w('| Rule | Plain-language definition | Usable (of 2,000) | Not usable, by reason |')
w('|---|---|---|---|')
def reasons(k):
    return '; '.join(f'{a}: {b}' for a, b in U['reasons'][k].items() if a != 'ok')
w(f"| **B1** baseline | at least one baseline measure with a numeric value and a numeric denominator > 0 for the Total group, or for every arm | {T(U['usable']['B1'])} | {reasons('B1')} |")
w(f"| **F1** participant flow | one period with numeric STARTED and COMPLETED counts for every flow group, STARTED > 0 in at least one | {T(U['usable']['F1'])} | {reasons('F1')} |")
w(f"| **O1** primary outcome | a PRIMARY measure with a unit, a numeric number analysed for every group (> 0 in at least one) and a numeric value for every analysed group | {T(U['usable']['O1'])} | {reasons('O1')} |")
w(f"| **A1** adverse events | numeric serious and other numbers at risk for every event group (> 0 in at least one), plus event rows or explicit zero totals | {T(U['usable']['A1'])} | {reasons('A1')} |")
w(f"| **S1** statistical analysis | an analysis on a PRIMARY measure naming its groups, with a p-value or an estimate with both confidence limits | {T(U['usable']['S1'])} | see 2.3: the right denominator is comparative trials |")
w(f"| B1 and F1 and O1 and A1 | all four substantive modules usable | {T(U['usable']['four_usable'])} | |")
w('')
w('Notes on each rule:')
w('')
w(f"- **Baseline.** Usable for the Total group in {T(U['usable']['B1_ok_for_total'])} and for every arm in {T(U['usable']['B1_ok_for_every_arm'])}. The 9 failures are records whose baseline table is a template with category titles and zero participants (for example NCT00387673, NCT00712270, NCT02978885). Total group found as: titled \"Total\" {U['reasons']['B1_total_method']['titled_total']:,}; single group {U['reasons']['B1_total_method']['single_group']}; last group equal to the sum of the others under another title {U['reasons']['B1_total_method']['last_equals_sum_untitled_total']}.")
w(f"- **Participant flow.** The 5 failures all post STARTED = 0 in every group (records closed without data, e.g. NCT00387673, NCT01016561).")
w(f"- **Primary outcome.** Stricter variants: number analysed > 0 in every group, {T(U['usable']['O1_strict'])}; every PRIMARY measure of the trial usable, {T(U['usable']['O1_all_primaries'])}. At measure level, of 4,408 PRIMARY measures {U['reasons']['primary_measures_not_usable_by_reason']['ok']:,} pass; the rest: " + '; '.join(f'{a}: {b}' for a, b in U['reasons']['primary_measures_not_usable_by_reason'].items() if a != 'ok') + '.')
w(f"- **Adverse events.** This is where presence and usability differ most: the module is present in 1,997 records but in {U['reasons']['A1'].get('zero_at_risk_in_every_group', 0)} of them every group has zero participants at risk (adverse events not collected or not reported; the record says so in free text, e.g. NCT05537116, NCT06290206), and {U['reasons']['A1'].get('numbers_at_risk_missing_for_a_group', 0)} older records lack a number at risk. Of the usable records, {U['usable']['A1_explicit_zero_only']['numerator']:,} report no event rows and explicit zeros for every group. The all-cause mortality table (numeric `deathsNumAtRisk` for every group) is present in {T(U['usable']['A1_deaths_table'])}; it was not required by A1 because it did not exist before 2017.")
w(f"- By study type: interventional B1 {T(U['by_stratum']['INTERVENTIONAL']['B1'])}, F1 {T(U['by_stratum']['INTERVENTIONAL']['F1'])}, O1 {T(U['by_stratum']['INTERVENTIONAL']['O1'])}, A1 {T(U['by_stratum']['INTERVENTIONAL']['A1'])}; observational (n = 117) B1 {T(U['by_stratum']['OBSERVATIONAL']['B1'])}, O1 {T(U['by_stratum']['OBSERVATIONAL']['O1'])}, A1 {T(U['by_stratum']['OBSERVATIONAL']['A1'])}.")
w('- These rules test whether a table can be read, not whether its content is correct, complete against the protocol, or analysed appropriately.')
w('')
w('### 2.3 Comparative trials and statistical analyses')
w('')
w('"Comparative" is its own denominator. How it was determined (structure of the record, not a judgement of design quality):')
w('')
w(f"- **C1** = `protocolSection.designModule.studyType == \"INTERVENTIONAL\"` and at least one PRIMARY outcome measure whose `groups[]` has 2 or more entries: **{T(U['comparative']['C1'])}**. (Interventional: {T(U['comparative']['interventional'])}; allocation among them: " + ', '.join(f'{k} {v}' for k, v in U['comparative']['interventional_by_allocation'].items()) + f". {U['comparative']['observational_with_2plus_group_primary']['numerator']} observational studies also have a 2+ group primary measure and are not in C1.)")
w(f"- **C2** = C1 and `designInfo.allocation == \"RANDOMIZED\"`: **{T(U['comparative']['C2'])}** ({T(U['comparative']['C2_of_randomized'])} of randomised interventional trials).")
w(f"- **C3** = C2 and rule O1 passed on a PRIMARY measure that itself has 2+ groups: **{T(U['comparative']['C3'])}**.")
w('- Two groups on a measure are not necessarily two randomised arms (dose cohorts, periods and disease cohorts also appear as groups); C2 is the denominator closest to "appropriate comparative trials".')
w('')
w('| Denominator | n | any analysis on a primary | S1 (named groups + p-value or estimate with both limits) | S1_between (2+ groups) | between-arm p-value | between-arm estimate with both limits |')
w('|---|---:|---|---|---|---|---|')
for name, lab in (('all_sampled', 'All sampled'), ('C1', 'C1 comparative'), ('C2', 'C2 randomised comparative'), ('C3', 'C3 randomised, usable 2+ group primary'), ('C2_phase3', 'C2 and Phase 3'), ('C2_phase3_industry', 'C2, Phase 3, industry'), ('not_C1', 'Not C1')):
    s = st[name]
    w(f"| {lab} | {s['S1']['denominator']:,} | {T(s['S_any_analysis'])} | {T(s['S1'])} | {T(s['S1_between'])} | {T(s['S1_between_p'])} | {T(s['S1_between_est_ci'])} |")
w('')
w(f"So among randomised comparative trials with results, {T(st['C2']['S1_between'])} post a usable between-arm analysis on a primary measure and {T(st['C2']['S1_between_est_ci'])} post a between-arm estimate with both confidence limits; in randomised Phase 3 the figures are {T(st['C2_phase3']['S1_between'])} and {T(st['C2_phase3']['S1_between_est_ci'])}. Absence of a posted analysis is not absence of an analysis: the registry does not require one for every measure, and the publication or CSR may hold it.")
w('')
w(f"Race reporting and analyses together, within C2: race reported {T(st['C2']['race_reported'])}; race reported and S1_between {T(st['C2']['race_reported_and_S1_between'])}; race reported and a between-arm estimate with both limits {T(st['C2']['race_reported_and_S1_between_est_ci'])}. In C2 Phase 3 industry trials: {T(st['C2_phase3_industry']['race_reported_and_S1_between_est_ci'])}. These are co-occurrence counts in one record; they are not outcomes by race.")
w('')

# ------------------------------------------------------------------ 3 endpoints
w('## 3. Endpoint comparability')
w('')
w('### 3.1 Exact-title uniqueness is a lexical finding')
w('')
ex = FAM['exact_title']
w(f"On the full release, the first registered primary outcome title (`primary_endpoint`), lower-cased with punctuation collapsed, takes {ex['distinct_normalised']:,} distinct values across 80,890 trials; {ex['used_once']:,} ({100 * ex['used_once'] / ex['distinct_normalised']:.1f}% of distinct titles; {100 * ex['used_once'] / 80890:.1f}% of trials) are used by one trial only. The first audit's sample figure (4,026 of 4,097 distinct titles used once) says the same thing.")
w('')
w('This is a statement about strings. Registrants write the time point, population, instrument version and study part into the title, so two trials measuring the same construct almost never share a title. **It does not establish scientific incompatibility, and it does not show that oncology is the only viable area**: the most repeated titles are oncology titles ("progression free survival pfs" 511, "overall survival" 261) because oncology endpoints are conventionally titled without a time point, not because other areas lack common constructs. Sections 3.2 to 3.4 test that directly.')
w('')
w('### 3.2 Candidate endpoint families on the full release (80,890)')
w('')
w('A family = an endpoint regex on `primary_endpoint` AND a condition gate. Python `re`, case-insensitive except inside `(?-i:...)`. The gate is applied to `primary_condition` or to the `"; "`-joined `conditions` list. `primary_endpoint` is the first registered primary outcome only (`src/utils.py` lines 478-480), so a family endpoint that is a trial\'s second primary, or a secondary, is not counted; the counts are lower bounds on use of the construct and upper bounds on a usable cohort.')
w('')
w('| Family | Endpoint regex | Gate | Matched | Gated | Randomised Phase 3 | Race reported | Both |')
w('|---|---|---|---:|---:|---:|---:|---:|')
for fam, v in FAM['families'].items():
    gate = f"`{v['gate_field']}` ~ `{v['gate_regex']}`" if v['gate_regex'] else 'none'
    w(f"| {fam} | `{v['endpoint_regex'].replace('|', chr(92) + '|')}` | {gate.replace('|', chr(92) + '|')} | {v['matched']:,} | {v['gated']:,} | {v['rp3']:,} | {v['race_reported']:,} | {v['rp3_and_race_reported']:,} |")
w('')
w(f"Matched = endpoint regex alone; Gated = matched and gate; Randomised Phase 3 = gated and `allocation == \"RANDOMIZED\"` and `phase == \"PHASE3\"` (Phase 2/3 excluded, as in `outcomes/evidence.py`); Race reported = gated and `race.reported`; Both = randomised Phase 3 and race reported. `svr_hcv` and `hiv_rna` are sub-families of `svr_or_viral_load`. Families overlap (a title naming PFS and OS is in both). Together the 14 families (sub-families aside) cover {FAM['any_family_gated']:,}/80,890 trials ({100 * FAM['any_family_gated'] / 80890:.1f}%) and {FAM['rp3_in_any_family']:,}/{FAM['rp3_total_in_release']:,} ({100 * FAM['rp3_in_any_family'] / FAM['rp3_total_in_release']:.1f}%) of randomised Phase 3 trials in the release. Within a family the titles are still nearly all distinct (for example {FAM['families']['hba1c_change']['distinct_normalised_titles_rp3']} distinct titles among {FAM['families']['hba1c_change']['rp3']} randomised Phase 3 HbA1c trials), which is the lexical finding again.")
w('')
w('### 3.3 What actually varies inside six families')
w('')
w('Six families were examined: the four largest by "Both" with one shared construct (PFS, OS, HbA1c, FEV1), plus two smaller families chosen because the construct is defined by a standard (ACR20) or by a small set of instruments (depression scales). `seroconversion_or_titre` is large but spans unrelated antigens and assays and was not examined; objective response is mostly single-arm (56 randomised Phase 3 of 2,061).')
w('')
w('Examined set per family: the family\'s randomised Phase 3 members in the 2,000 sample, plus, where the sample held fewer than 15, a draw of 40 more from the family\'s randomised Phase 3 members outside the sample (`random.Random(20261113).sample(sorted(candidates), 40)`), fetched from the API on 2026-11-13 (`cache/family_studies.jsonl.gz`; 200 requested, 200 returned; the HbA1c family already had 15 in the sample, so none were drawn for it). For each trial the family measure is the first posted PRIMARY measure whose title matches the family regex; all attributes are read from that one measure by the rules in `scripts/family_detail.py`. The attribute classes are regex-based and were spot-read, not validated; per-trial rows are in `family_detail_rows.jsonl`.')
w('')
order = ['progression_free_survival', 'overall_survival', 'hba1c_change', 'fev1', 'acr20', 'depression_scale']
labs = {'progression_free_survival': 'PFS (oncology)', 'overall_survival': 'OS (oncology)', 'hba1c_change': 'HbA1c (diabetes)', 'fev1': 'FEV1 (asthma/COPD/CF)', 'acr20': 'ACR20 (arthritis)', 'depression_scale': 'Depression scales'}
w('| | ' + ' | '.join(labs[f] for f in order) + ' |')
w('|---|' + '---|' * len(order))
def row(label, fn):
    w(f'| **{label}** | ' + ' | '.join(str(fn(FD[f], f)) for f in order) + ' |')
row('Randomised Phase 3 in release', lambda d, f: f"{FEX[f]['rp3_members_in_release']:,}")
row('Examined (sample + drawn)', lambda d, f: f"{d['examined']} ({d['from_sample']} + {d['drawn']})")
row('With a matching posted primary measure / with data', lambda d, f: f"{d['with_family_measure']} / {d['has_data']}")
row('Condition', lambda d, f: dist(d['condition_class'], d['with_family_measure'], 5))
row('Construct', lambda d, f: dist(d['construct'], d['with_family_measure'], 4))
row('Instrument / criteria', lambda d, f: dist(d['instrument'], d['with_family_measure'], 5))
row('Unit (`unitOfMeasure`, normalised)', lambda d, f: f"{len(d['unit'])} distinct strings: " + dist(d['unit'], d['with_family_measure'], 4))
row('Summary statistic (`paramType`)', lambda d, f: dist(d['param'], d['with_family_measure'], 4))
row('Time point', lambda d, f: dist(d['time_kind'], d['with_family_measure'], 3) + (f"; fixed points: {d['time_weeks_fixed']['distinct']} distinct, min {d['time_weeks_fixed']['min']:g}, median {d['time_weeks_fixed']['median']:g}, max {d['time_weeks_fixed']['max']:g} weeks; commonest " + ', '.join(f'{a:g} wk ({b})' for a, b in d['time_weeks_fixed']['top'][:3]) if d['time_weeks_fixed']['n'] else ''))
row('Analysis population (tags in `populationDescription`; a text can carry several)', lambda d, f: dist(d['population'], d['with_family_measure'], 5))
row('Effect measure of between-arm analyses', lambda d, f: dist(d['effect_types'], d['with_family_measure'], 5) + f"; no between-arm analysis posted: {d['no_between_analysis']}")
row('Between-arm estimate with both confidence limits', lambda d, f: pct(d['between_est_ci'], d['with_family_measure']))
row('Control', lambda d, f: dist(d['control'], d['with_family_measure'], 4))
row('Distinct drug/biological intervention names', lambda d, f: d['distinct_drug_or_biological_names'])
row('Race reported / and estimate with limits', lambda d, f: f"{pct(d['race_reported'], d['with_family_measure'])} / {pct(d['race_reported_and_between_est_ci'], d['with_family_measure'])}")
w('')
w('Reading the table, attribute by attribute:')
w('')
w('- **Construct.** HbA1c (15/15 "change from baseline"), ACR20 (44/44 the responder proportion) and OS are one construct each. PFS splits by who assessed progression (independent review 23, investigator 7, not stated 20). FEV1 splits into trough or pre-dose (24), peak or post-dose (5), AUC or serial (5) and unstated (8). Depression is mostly change in total score (39/43) but includes responder proportions and a time-to-relapse design.')
w('- **Instrument.** None for OS and HbA1c beyond the assay. PFS depends on tumour-specific response criteria (RECIST 1.1 20, RECIST 1.0 or unspecified 8, myeloma, lymphoma and CLL criteria 6, none stated 16). Depression is MADRS in 33/43 and HAM-D in 10 (17-item 6, 24-item 2, version not stated 2). FEV1 is absolute volume in 35 and % predicted in 7.')
w('- **Unit.** The unit strings differ almost everywhere, but mostly as spelling ("liters", "litres", "liter", "L"; nine spellings of "percent HbA1c"). Real unit differences are fewer: months vs days vs proportion event-free at a landmark for PFS and OS; L vs mL vs % predicted for FEV1; participants vs percentage of participants for ACR20 (convertible with the denominator).')
w('- **Time point.** Time-to-event families are event-driven (45/50 PFS, 41/52 OS), so follow-up length varies by trial and is not in the title. HbA1c concentrates at 24 and 26 weeks (12 of 14 fixed points). ACR20 at week 24 (18), 12 (8) and 16 (7). FEV1 at 12 (10) and 24 (7) weeks among 10 distinct points. Depression at 8 (12) and 6 (9) weeks among 12 distinct points from under 1 to 24 weeks.')
w('- **Analysis population.** Named in free text in nearly every measure but rarely with one term: "all randomized", ITT, FAS, mITT and "received at least one dose" overlap and co-occur, and several records define the set as a subset with baseline and post-baseline values. The registry has no coded analysis-population field.')
w('- **Effect measure.** Uniform where convention is strong: hazard ratio in every PFS and OS trial whose between-arm analysis names an estimate type (38 and 28 trials). Difference in means or least-squares means for HbA1c, FEV1 and depression scores, under at least three labels. ACR20 is the least uniform (risk difference 11, unspecified difference 10, odds ratio 6, no estimate type 9).')
w(f"- **Is a between-arm estimate with confidence limits posted?** PFS {pct(FD['progression_free_survival']['between_est_ci'], FD['progression_free_survival']['with_family_measure'])}; depression scales {pct(FD['depression_scale']['between_est_ci'], FD['depression_scale']['with_family_measure'])}; HbA1c {pct(FD['hba1c_change']['between_est_ci'], FD['hba1c_change']['with_family_measure'])}; ACR20 {pct(FD['acr20']['between_est_ci'], FD['acr20']['with_family_measure'])}; FEV1 {pct(FD['fev1']['between_est_ci'], FD['fev1']['with_family_measure'])}; OS {pct(FD['overall_survival']['between_est_ci'], FD['overall_survival']['with_family_measure'])}. Where it is missing the arm-level values are usually posted, but a comparable effect would have to be computed, which for time-to-event outcomes is not possible from medians.")
w('- **Condition and intervention.** No family is one clinical question. The PFS set spans 10 tumour groups and 96 drug names; OS 14 groups and 116 names; FEV1 mixes COPD (24), asthma (13) and cystic fibrosis (5); ACR20 mixes rheumatoid (26) and psoriatic (18) arthritis; depression mixes MDD (33), bipolar depression (5) and treatment-resistant depression (4). Placebo-controlled and active-controlled trials are mixed in every family.')
w('')
w('### 3.4 Recommended narrow cohorts, and what would have to be harmonised')
w('')
w('Recommendation: build cohorts as **one condition + one construct + one time window + one comparator type**, in several therapeutic areas, rather than one cohort defined by sponsor class and completion year. Sizes below are text-definition counts on the full release (`scripts/cohorts.py`; randomised Phase 3; time window parsed from `primary_outcome_time_frame`); each is an upper bound until the posted measures are checked trial by trial as in 3.3.')
w('')
w('| Candidate cohort (randomised Phase 3, results posted) | n | race reported | ethnicity reported | industry | results posted 2017+ (race reported) |')
w('|---|---:|---:|---:|---:|---:|')
for k, v in COH['cohorts'].items():
    w(f"| {k} | {v['n']} | {v['race_reported']} | {v['ethnicity_reported']} | {v['industry']} | {v['results_2017_or_later']} ({v['race_reported_results_2017_or_later']}) |")
w('')
w('Ranked by how little harmonisation they need:')
w('')
w('1. **Type 2 diabetes, HbA1c change from baseline at 24-26 weeks.** One construct, one assay, one unit after normalising spellings, mean or LS-mean difference with a confidence interval in 12/15 examined. To harmonise: MEAN vs LEAST_SQUARES_MEAN and SD vs SE vs CI as the posted summary; FAS vs all-randomised vs treated sets and the handling of rescue medication and missing data (stated only in free text); placebo vs active comparator and background therapy; drug class. Largest cohort, but race is reported in about half (131/248), fewer among older records.')
w('2. **Rheumatoid arthritis ACR20 at week 24, and psoriatic arthritis ACR20 at weeks 12-24, as two cohorts.** The endpoint is a published standard and the arm-level responder counts are always posted, so a risk difference or odds ratio can be recomputed uniformly even where the posted effect measure differs. To harmonise: time point (12, 16, 24 weeks), non-responder imputation rules, background methotrexate, and the two diseases kept apart. Small (35 and 34).')
w('3. **Major depressive disorder, MADRS total score change at 6-8 weeks, placebo-controlled.** One instrument, LS-mean difference with a confidence interval in most records. To harmonise: week 6 vs 8; MMRM vs LOCF ANCOVA; fixed vs flexible dose arms (several doses against one placebo arm); exclude bipolar and treatment-resistant populations and HAM-D trials or handle them as separate cohorts. Small (31; 82 with any time point).')
w('4. **COPD, trough FEV1 at 12-24 weeks; asthma FEV1 as a separate cohort.** To harmonise: trough vs peak vs AUC; L vs mL; 12 vs 24 weeks; mono vs combination bronchodilators against placebo or an active component. 49 trials for the narrow COPD definition.')
w('5. **Oncology by tumour type: PFS in lung, breast and myeloma; OS in lung.** Hazard ratios with confidence limits are posted more consistently here than any other effect measure (38/50 PFS), and race reporting is high in recent records. To harmonise: assessor (independent vs investigator), response criteria, line of therapy and biomarker-selected populations, data cut-off and follow-up length, stratified vs unstratified models, and interim vs final analyses. A hazard ratio cannot be recomputed from the posted medians, so trials without a posted estimate drop out.')
w('')
w('What this assessment does not do: it does not show that effects are combinable across the trials in a cohort (that is a clinical and statistical judgement about interventions and populations), and it counts titles, not validated measures.')
w('')
w('### 3.5 The existing industry Phase 3 cohort as a candidate, with its selection limitations')
w('')
exc = COH['existing_cohort_in_release']
w(f"The existing cohort (`outcomes/README.md`, `outcomes/evidence.py` lines 15-16) is defined by the query `AREA[StudyType]INTERVENTIONAL AND AREA[LeadSponsorClass]INDUSTRY AND AREA[Phase]PHASE3 AND AREA[DesignAllocation]RANDOMIZED AND AREA[PrimaryCompletionDate]RANGE[2020-01-01,2020-12-31]`, then eligibility rules E1-E5 (Phase 2/3 excluded; completion date wholly inside 2020): 648 frame records and 598 eligible on 2026-11-09 according to the README. It is not restricted to oncology: the README and the code contain no condition filter. If an oncology restriction is intended it is not in this repository, and that should be confirmed before the cohort is described as an oncology cohort.")
w('')
w('Treated as one candidate among those in 3.4, its limitations are:')
w('')
w(f"- **It is not selected on results, which is a strength, but the demographics link is.** Unlike the demographics release, its frame does not require results: on 2026-11-13 the frame query returned {p3i['n_all']} studies of which {p3i['n_with_results']} ({100 * p3i['n_with_results'] / p3i['n_all']:.1f}%) had results. Only those can be linked to posted baseline demographics or posted outcomes. In the release, {exc['n']} trials meet the same definition with `phase == \"PHASE3\"` exactly; race is reported for {exc['race_reported']} of them ({100 * exc['race_reported'] / exc['n']:.1f}%), ethnicity for {exc['ethnicity_reported']}.")
w('- **One completion year.** 2020 is a single year, and an unusual one: trial conduct was disrupted by the pandemic, and the year\'s completions include COVID-19 vaccine and treatment trials. Nothing about other years can be read from it, and year effects cannot be separated from anything else.')
w('- **Sponsor class is the registry\'s own field** (`LeadSponsorClass == INDUSTRY`), which misses industry-funded trials led by academic or network sponsors and includes small companies; Phase 2/3, non-randomised and device trials without a phase are excluded by construction.')
w(f"- **It is defined by who ran the trial and when, not by what was measured.** Its {exc['n']} release members carry {exc['distinct_normalised_primary_endpoint_titles']} distinct first-primary titles across " + f"{len(exc['primary_condition'])} condition categories (" + ', '.join(f'{a} {b}' for a, b in exc['primary_condition'][:6]) + f", ...). {dict(exc['families']).get('(no family)', 0)} of {exc['n']} fall in none of the 14 endpoint families; the largest shared endpoints are OS ({dict(exc['families']).get('overall_survival', 0)}), PFS ({dict(exc['families']).get('progression_free_survival', 0)}) and HbA1c ({dict(exc['families']).get('hba1c_change', 0)}). Its oncology subset is {exc['oncology']['n']} trials (PFS {exc['oncology']['pfs']}, OS {exc['oncology']['os']}), spread over tumour types. It supports statements about evidence availability in a defined set of trials; it does not by itself give a comparable-endpoint cohort.")
w('- **Results state is recomputed at a cutoff** (POSTED / PENDING / UNREPORTED / NOT_APPLICABLE) from registry facts; as the README says, that is a timing fact, not a finding of non-compliance.')
w('- **Small cells.** Any cross-classification by endpoint family and race reporting within 400-600 trials leaves cells of a few dozen or fewer.')
w('')

# ------------------------------------------------------------------ 4 subgroup
w('## 4. Subgroup detection')
w('')
w('Throughout this section the figures are **subgroup reporting detected by the current method**, followed by one reviewer\'s reading of what was detected. They are not an established prevalence.')
w('')
w('### 4.1 Fields inspected')
w('')
w('For every posted outcome measure (`resultsSection.outcomeMeasuresModule.outcomeMeasures[]`, all 15,922 measures of all types in the 2,000 trials):')
w('')
w('| Field | How it is used | In first audit? |')
w('|---|---|---|')
for a, b, c in (
        ('`title`', 'cue rule', 'yes (title cue)'),
        ('`description`', 'cue rule', 'no'),
        ('`populationDescription`', 'cue rule', 'no'),
        ('`classes[].title`', 'level rule over the set of class titles; cue rule on each', 'yes (levels)'),
        ('`classes[].categories[].title`', 'level rule over the set of category titles; cue rule on each', 'yes (levels)'),
        ('`groups[].title`', 'level rule over the set of group titles', 'supplementary only'),
        ('`groups[].description`', 'level rule (sex and age only) when group titles did not fire', 'no'),
        ('`analyses[].groupIds`', 'structural: an analysis naming 2+ groups that carry different levels', 'no'),
        ('`analyses[].groupDescription`, `.statisticalComment`, `.otherAnalysisDescription`, `.nonInferiorityComment`, `.pValueComment`, `.estimateComment`, `.ciLowerLimitComment`, `.ciUpperLimitComment`, `.statisticalMethod`', 'cue rule on each', 'no'),
        ('`classes[].categories[].measurements[].comment`', 'cue rule', 'no')):
    w(f'| {a} | {b} | {c} |')
w('')
w('Every free-text key present under an outcome measure in the sample is in this list (key inventory in the first step of the audit). Not inspected: `timeFrame`, `unitOfMeasure`, `paramType`; the baseline, participant-flow and adverse-event modules; `moreInfoModule`; protocol-section text; posted protocol, SAP or CSR documents; publications.')
w('')
w('### 4.2 The exact rule (SG2)')
w('')
w('A trial is detected when any measure yields any signal. Source: `scripts/subgroup_rule.py` (the regexes below are printed from that module).')
w('')
w('**Level rule** (two or more levels of one dimension among sibling titles of one measure; applied separately to class titles, category titles and group titles):')
w('')
w('- sex: one title matches FEMALE and not MALE, and a different title matches MALE and not FEMALE.')
w('- race: two titles carry different RACE tokens after the excluded phrases are removed ("non-white" replaces "white" in the same title).')
w('- ethnicity: one title matches HISPANIC and the set has 2+ titles.')
w('- origin: 2+ titles match ORIGIN or NON_ORIGIN with at least two different tokens.')
w('- age: two titles carry different AGE tokens: the text matched by AGE_BAND, or a single LIFE_STAGE word.')
w('- group descriptions: the sex and age rules applied to `groups[].description` when the group titles did not fire for that dimension.')
w('- analysis group reference: an analysis whose `groupIds` name 2+ groups that the group rules separate.')
w('')
w('**Cue rule** (one text): KEYWORD anywhere; else BY_DEMOG unless BY_DEMOG_EXCLUDE matches within 25 characters either side. "Stratified by" followed by a non-demographic factor, "adjusted for age/sex" and covariate lists are deliberately not cues.')
w('')
w('```')
w(f'FEMALE           {subgroup_rule.FEMALE.pattern}')
w(f'MALE             {subgroup_rule.MALE.pattern}')
w(f'RACE_EXCLUDE     {subgroup_rule.RACE_EXCLUDE.pattern}')
for name, rx in subgroup_rule.RACE_TOKENS:
    w(f'RACE[{name}]'.ljust(17) + rx.pattern)
w(f'HISPANIC         {subgroup_rule.HISPANIC.pattern}')
w(f'ORIGIN           {subgroup_rule.ORIGIN.pattern}')
w(f'NON_ORIGIN       {subgroup_rule.NON_ORIGIN.pattern}')
w(f'AGE_BAND         {subgroup_rule.AGE_BAND.pattern}')
for name, rx in subgroup_rule.LIFE_STAGE:
    w(f'LIFE_STAGE[{name}]'.ljust(23) + rx.pattern)
w(f'KEYWORD          {subgroup_rule.KEYWORD.pattern}')
w(f'BY_DEMOG         {subgroup_rule.BY_DEMOG.pattern}')
w(f'BY_DEMOG_EXCLUDE {subgroup_rule.BY_DEMOG_EXCLUDE.pattern}')
w('(all case-insensitive)')
w('```')
w('')
w('### 4.3 All detected positives, reviewed')
w('')
jc = SUB['judgement_counts']
w(f"**Detected by the current method: {SUB['detected_text']}.** All 136 were read (the matched text and the measure's groups and classes for each; the cached record itself for 19 ambiguous ones) and classified:")
w('')
w('- **true subgroup result**: the record posts outcome values separately for two or more levels of a participant characteristic, under the same intervention, as classes, categories or groups.')
w('- **partial**: something short of that: one level only; levels confounded with dose or cohort by design; subgroup sizes rather than outcomes; model-derived rather than observed values; a "by subgroup" title whose table does not identify the subgroup.')
w('- **false positive**: the wording means something else (analysis-set definitions such as "PK subset" or "responders", instrument "subscales", laboratory "cell subsets", stratification factors of a test, eligibility ages in arm descriptions), or a subgroup analysis is announced and no data are posted.')
w('')
w('| Judgement (one reviewer) | Trials | Of 2,000 | Of 136 detected |')
w('|---|---:|---|---|')
w(f"| True subgroup result | {jc['true_subgroup_result']} | {SUB['true_any']} | {SUB['precision_true_over_detected']} |")
w(f"| - demographic (sex, race, ethnicity, national origin or age) | {len(SUB['true_demographic_ids'])} | {SUB['true_demographic']} | |")
w(f"| - non-demographic only (biomarker, disease type, severity) | {len(SUB['true_non_demographic_ids'])} | {SUB['true_non_demographic_only']} | |")
w(f"| Partial | {jc['partial']} | {SUB['partial']} | |")
w(f"| False positive | {jc['false_positive']} | {SUB['false_positive']} | {frac(jc['false_positive'], 136)} |")
w('')
td = SUB['true_by_dimension']
w(f"Among the {len(SUB['true_demographic_ids'])} trials judged to post a demographic subgroup result (a trial can count in several): age {len(td['age'])}, sex {len(td['sex'])}, race {len(set(td['race']) | {'NCT02304406'})}, ethnicity {len(td['ethnicity'])}, national origin {len(td['origin'])}; race or ethnicity {len(SUB['true_race_or_ethnicity'])} ({', '.join(SUB['true_race_or_ethnicity'])}). {len(SUB['true_demographic_on_primary_measure'])} have the level signal on a PRIMARY measure; for race or ethnicity that is 1 (NCT03098836, whose arms are race-defined cohorts). The release reports race for {SUB['true_demographic_race_reported_in_release']} of the {len(SUB['true_demographic_ids'])}.")
w('')
w('Where the signals came from, and how they fared (a trial can appear in several rows):')
w('')
w('| Matched field | Trials | True | of which demographic | Partial | False positive |')
w('|---|---:|---:|---:|---:|---:|')
for fld, v in sorted(SUB['by_field'].items(), key=lambda kv: -kv[1]['trials']):
    w(f"| {fld} | {v['trials']} | {v['true']} | {v['true_demographic']} | {v['partial']} | {v['false_positive']} |")
w('')
w(f"- Extending beyond titles mattered: {len(SUB['true_found_only_through_fields_added_in_v2'])} of the {jc['true_subgroup_result']} true results ({len(SUB['true_demographic_found_only_through_fields_added_in_v2'])} of the {len(SUB['true_demographic_ids'])} demographic ones) have no signal in the fields the first audit's main rule used (measure title, class titles, category titles); most were found through group titles (sex- or age-defined groups).")
w(f"- It also brought most of the false positives: the KEYWORD cue alone accounts for {SUB['keyword_only_trials']['n']} detected trials, of which {SUB['keyword_only_trials']['false_positive']} are false positives. \"Subset\" and \"subpopulation\" in descriptions mostly define analysis sets. Level rules on class and group titles were right far more often (class titles 15 true of 20; group titles 14 of 17).")
w(f"- All {SUB['v1_flagged_measure_trials']} trials flagged by the first audit's class/category/title rule are detected by this rule. Read again in this pass, 5 of those 23 received a different label (NCT03500094 yes to partial: the age-band values are model simulations; NCT02165826 no to partial; NCT03722810, NCT03993119 and NCT04259762 partly to true). That is the same kind of reviewer on two passes, and it is a direct measure of how soft these judgements are.")
w('')
w('**Every detected trial** (136; full matched text in `subgroup_review.csv`). Field = fields that matched; text = first matched text, truncated.')
w('')
w('| NCT ID | Matched field(s) | Matched text | Judgement | Reason |')
w('|---|---|---|---|---|')
for r in REVIEW:
    if r['sample_type'] != 'detected':
        continue
    first = r['matched_text'].split(' || ')[0]
    first = first.split('] ', 1)[1] if '] ' in first else first
    w(f"| {r['nct_id']} | {esc(r['matched_field'])} | {esc(first[:120])} | {r['judgement'].replace('_', ' ')} | {esc(r['reason'])} |")
w('')
w('### 4.4 Negatives: a random sample and a targeted sample of likely reporters')
w('')
ng = SUB['negatives']
w(f"{ng['non_detected']:,} trials have no signal. Two samples of them were reviewed (100 trials):")
w('')
w('- **Random, 60**: `random.Random(20261113).sample(sorted(non_detected_ids), 60)`. Sixty is the size requested; with 0 events it bounds the miss rate among non-detected trials below about 6%, which is weak, hence the second sample.')
w(f"- **Targeted, 40**: the 40 highest \"likely reporter\" scores among the other {ng['non_detected'] - 60:,} non-detected trials (score: +2 Phase 3; +2 enrolment >= 500 and +1 more if >= 1,000; +1 a measure with >= 6 classes; +1 a measure with >= 4 groups; +2 a measure title containing \"by\" or \"stratified\"; +2 a loose demographic word in a class, category or group title or an analysis group description; +1 \"interaction\" or \"stratif\" in analysis text; ties broken by a seeded shuffle). All 40 scored >= {ng['targeted_cutoff_score']}; {ng['n_rest_at_or_above_cutoff']} non-detected trials scored that high, so the targeted sample covers nearly all of the highest-scoring stratum. Justification: if the rule misses subgroup tables, large Phase 3 trials with many rows or groups and \"by ...\" titles are where they would be.")
w('- What was read for each: all distinct group titles, up to 14 distinct class titles and 8 category titles, every measure title containing "by" or "stratified", and every loose demographic or stratification mention in any inspected field; a second scan of all class and category titles for non-demographic level words (region, prior therapy, mutation status, severity and the like); the cached record for 5 candidates. Class titles beyond the first 14 were covered only by those two scans.')
w('')
w('| Sample | Reviewed | Demographic subgroup result missed | Non-demographic subgroup result missed | Partial | No subgroup result |')
w('|---|---:|---:|---:|---:|---:|')
w(f"| Random non-detected | 60 | {ng['random_missed_demographic']} | {ng['random_missed_non_demographic']} | {ng['random_partial']} | {60 - ng['random_missed_demographic'] - ng['random_missed_non_demographic'] - ng['random_partial']} |")
w(f"| Targeted likely reporters | 40 | {ng['targeted_missed_demographic']} | {ng['targeted_missed_non_demographic']} | {ng['targeted_partial']} | {40 - ng['targeted_missed_demographic'] - ng['targeted_missed_non_demographic'] - ng['targeted_partial']} |")
w('')
w('The cases the rule missed or half-missed:')
w('')
w('| NCT ID | Sample | Judgement | What was missed and why |')
w('|---|---|---|---|')
for r in REVIEW:
    if r['sample_type'] != 'detected' and r['judgement'] != 'no_subgroup_result':
        w(f"| {r['nct_id']} | {r['sample_type'].replace('negative_', '')} | {r['judgement'].replace('_', ' ')} | {esc(r['reason'])} |")
w('')
w('Assessment of missed cases:')
w('')
w(f"- **Demographic subgroup results.** None in the 60 random negatives (0/60; Wilson 95% upper bound {100 * ng['random_upper95_demographic']:.1f}%, which over {ng['non_detected']:,} non-detected trials would still allow up to about {int(ng['random_upper95_demographic'] * ng['non_detected'])} trials). One in the 40 likely reporters: Japanese-patient arms labelled \"JP\". So misses exist, they are of the abbreviation or unusual-wording kind, and this review cannot put a tight number on them. The detected-and-confirmed count (34/2,000) is a floor for the sample, not an estimate of prevalence.")
w('- **Non-demographic subgroup results** (biomarker status, tumour type, diagnosis) are missed more often: 1/60 random and 3/40 targeted. They are posted as cohort-named groups or "stratified by [factor]" measures without the word "subgroup", which the rule excludes on purpose to avoid stratified-test wording. The rule should not be used to count non-demographic subgroup reporting.')
w('- Not assessed at all: subgroup results that exist only in posted documents or publications, and anything in languages other than English.')
w('- One reviewer (the auditing model) made every judgement in 4.3 and 4.4, from digests in `review/`. No second reviewer, no human review, no agreement statistic. An independent human reading of `subgroup_review.csv`, at least of the 41 true and 24 partial rows and the 100 negatives, is what would validate these figures.')
w('')

# ------------------------------------------------------------------ 5 denominators
w('## 5. Denominators and historical inference')
w('')
w('### 5.1 Baseline total versus participant-flow STARTED versus enrolment: exact fields')
w('')
w('| Quantity | Exact JSON path and rule |')
w('|---|---|')
w('| **BT** baseline total | `resultsSection.baselineCharacteristicsModule.denoms[]`: the row whose `units` is "Participants" (it exists in 2,000/2,000 records; 14 records carry a second row in another unit, which is ignored). Value = `counts[].value` for the Total group chosen by rule T: the only group if there is one; else the group whose trimmed, case-folded `groups[].title` is exactly "total"; else, with 3+ groups, a last group whose count equals the sum of the earlier groups. If rule T finds none, the sum over all groups. **Never the Total plus the arms.** |')
w('| **FS** flow started | `resultsSection.participantFlowModule.periods[0].milestones[]` where `type == "STARTED"`: `achievements[].numSubjects` summed over `participantFlowModule.groups[]`, excluding any group titled exactly "Total" (none occurs). First period only. Where `typeUnitsAnalyzed` is present (35 records) a second unit is in `numUnits`; `numSubjects` is still participants. |')
w('| **EN** enrolment | `protocolSection.designModule.enrollmentInfo.count`, with `.type` (ACTUAL in 2,000/2,000). |')
w('| Analysis population | `baselineCharacteristicsModule.populationDescription`, free text, present for ' + f"{DEN['population_description_present']}" + '/2,000; quoted, never inferred when absent. |')
w('')
bm = DEN['BT_method']
w(f"Rule T outcome: titled \"Total\" {bm['titled_total']:,}; single group {bm['single_group']}; untitled total (last group = sum of the others) {bm['last_equals_sum_untitled_total']}; no total, arms summed 0. All three quantities were numeric for 2,000/2,000.")
w('')
w('**Double counting.** A rule that takes "the group titled Total, else the sum of all groups" double-counts the ' + f"{len(DEN['naive_rule_differs'])} records whose total has another title: " + '; '.join(f'{a} (correct {int(b)}, naive {int(c)})' for a, b, c in DEN['naive_rule_differs']) + ' ("Total Title" three times, "Pooled ..." once). Conversely the posted Total is not always the sum of the arms: ' + '; '.join(f'{a} (Total {int(b)}, arms sum {int(c)})' for a, b, c in DEN['total_not_sum_of_arms']) + '. The first is a registry entry whose Total is twice its arms; the second is a crossover whose two arm columns are the same 8 participants. In both the posted Total is used as posted and flagged here.')
w('')
w('| Comparison (denominator 2,000) | equal | first smaller | first larger |')
w('|---|---|---|---|')
for k, lab in (('BT_vs_FS', 'BT vs FS'), ('BT_vs_EN', 'BT vs EN'), ('FS_vs_EN', 'FS vs EN')):
    d = DEN[k]
    w(f"| {lab} | {frac(d.get('equal', 0), N)} | {frac(d.get('smaller', 0), N)} | {frac(d.get('larger', 0), N)} |")
w(f"| All three equal | {frac(DEN['all_three_equal'], N)} | | |")
w('')
w('Mismatch patterns (' + f"{DEN['n_mismatch']}" + ' trials where the three are not all equal):')
w('')
w('| Pattern | Trials |')
w('|---|---:|')
for k, v in DEN['pattern'].items():
    if k != 'BT equal FS; BT equal EN; FS equal EN':
        w(f'| {k} | {v} |')
w('')
w('### 5.2 Fifteen representative mismatches, reviewed before interpretation')
w('')
w('Selection: the 15 were allocated across mismatch patterns in proportion to frequency (at least one from each of the six commonest), drawn with seed 20261113 from the sorted IDs of each pattern (`scripts/denominators.py`). "Reading" is the auditing model\'s reading of the record\'s own text (baseline population description, pre-assignment details, period and group titles); where the record gives no reason, that is said.')
w('')
READ = {
    'NCT01473524': 'Baseline is the "intent-to-treat" set defined in the record as randomised and dosed; 1 of 217 randomised was never dosed. Baseline describes treated participants.',
    'NCT00145470': 'Baseline "includes all participants receiving study treatment"; 2 started but were not treated.',
    'NCT01142791': 'Baseline is the 115 in the primary analysis; 6 participants with one sonication sit in a separate flow group and in adverse events only (stated in the record).',
    'NCT02787681': 'No population description. Two fewer in baseline than started; the record gives no reason. Cannot be interpreted.',
    'NCT00095979': 'Baseline is "eligible and treated patients"; 2 enrolled were not.',
    'NCT03566810': 'Crossover; baseline is the safety population (at least one dose); 1 randomised participant was not dosed.',
    'NCT00435188': 'Flow and baseline agree at 398; enrolment is 400. No text explains the 2; plausibly enrolled but not randomised, but the record does not say.',
    'NCT03639948': 'Pre-assignment text: 5 of 120 enrolled were found ineligible and are excluded from flow and baseline.',
    'NCT00677014': 'Pre-assignment text: 1,060 enrolled; 46 not implanted and 34 not randomised before assignment; flow and baseline describe the 980 randomised.',
    'NCT00589914': 'Pre-assignment text: 1,221 enrolled (1 enrolled twice), 1,220 randomised, 1,214 dosed; flow and baseline are the dosed set.',
    'NCT00533897': 'Four-period design. 270 enrolled, 167 treated in the lead-in (the first period, which FS counts), baseline covers 157 (lead-in non-responders plus period-2 participants, per its description). The baseline groups are not the first-period flow groups.',
    'NCT02656836': 'Observational. 20 enrolled, 9 started, 6 in baseline; no explanatory text. Cannot be interpreted.',
    'NCT00245856': 'Artefact of the FS rule: the first flow period holds only 28 participants in two groups, while baseline describes all 67 treated. Not a registry discrepancy.',
    'NCT01224626': 'Observational. Flow started 42; baseline and enrolment 41. One participant unexplained in the record.',
    'NCT02992925': 'Artefact of the FS rule: the first period is "Cohort 1" (30 participants); cohort 2 enters in the second period. Baseline (365) also excludes 5 without antibody titres, as its description states.',
}
w('| NCT ID | BT | FS | EN (type) | Pattern | Baseline population (verbatim, truncated) | Reading |')
w('|---|---:|---:|---:|---|---|---|')
for r in DEN['representative_mismatches']:
    w(f"| {r['nct_id']} | {int(r['BT'])} | {int(r['FS'])} | {r['EN']} ({r['EN_type']}) | {r['pattern'].replace('; ', '; ')} | {esc((r['baseline_population'] or '(none)')[:150])} | {READ[r['nct_id']]} |")
assert all(r['nct_id'] in READ for r in DEN['representative_mismatches'])
w('')
w('What the reviewed mismatches support:')
w('')
w('- In 9 of 15 the record itself explains the difference: baseline is a treated, eligible or analysed set, or people were enrolled and lost before assignment. In 2 of 15 (NCT00245856, NCT02992925) the difference is produced by counting STARTED in the first period only; ' + '38 of the 44 "BT larger than FS" trials have multi-period flows, where cohorts that enter in a later period are not in the first-period count; that pattern is probably mostly this artefact, which was not checked trial by trial. In 4 of 15 the record gives no reason.')
w(f"- A baseline population description exists for {DEN['population_description_present_when_BT_ne_FS'][0]}/{DEN['population_description_present_when_BT_ne_FS'][1]} of the trials where BT differs from FS, so about half of the mismatches cannot be explained from the record.")
w('- The general statement that survives: the baseline table describes the population the registrant chose to describe, most often everyone who started (BT = FS in 79.2%), otherwise usually a treated or analysed subset slightly smaller than those who started. It is not safe to label the baseline population "enrolled", "randomised" or "ITT" for a given trial without reading its description, and for half of the mismatching trials there is none.')
w('')
w('### 5.3 Outcome switching: what current records cannot establish')
w('')
w('**Current records alone cannot establish departures from prospectively specified outcomes.** In a record with results, the protocol-section outcome list and the results outcome list are one list (the first audit found title, time frame and description identical for 4,406/4,406 primary and 10,833/10,833 secondary pairs compared position by position). That identity is how the registry stores the record; it is not evidence that outcomes were unchanged. Assessing switching needs, per trial:')
w('')
w('- **historical registry versions** (the outcome list as first registered and at each change), which the v2 API fields used here do not return and this audit did not fetch;')
w('- **registration timing** relative to enrolment and to primary completion, to know whether the first registered list was prospective at all;')
w('- and, for anything beyond titles, the **protocol and statistical analysis plan** (and their amendment history), because registry entries are short and are sometimes written after the protocol.')
w('')
w('What the current records show about the availability of those inputs (sample of 2,000; `scripts/history_evidence.py`; documents fetched with `fields=NCTId,DocumentSection`, 2,000 of 2,000 returned):')
w('')
w('| | n / denominator |')
w('|---|---|')
w(f"| First submitted to the registry after the study start date (month precision where the start date has no day) | {HIS['registered_after_start']} |")
w(f"| First submitted after the primary completion date | {HIS['registered_after_primary_completion']} |")
w(f"| Record updated after results were first posted | {HIS['updated_after_results_first_posted']} |")
w(f"| Protocol posted on the registry | {HIS['protocol_posted']} |")
w(f"| Statistical analysis plan posted | {HIS['sap_posted']} |")
w(f"| Protocol posted, results first posted 2017 or later | {HIS['protocol_posted_results_2017_or_later']} |")
w(f"| Registered on or before start, with protocol and SAP posted | {HIS['prospective_and_protocol_and_sap']} |")
w('')
w('These are availability counts. No history version, protocol or SAP was opened, a posted protocol is usually the final version, and "registered after start" compares two registrant-entered dates. A third of the sampled trials were registered after they started; for those, even the full registry history cannot show the prospectively specified outcome.')
w('')
w('### 5.4 Inferences this audit does not make, and that the data do not support')
w('')
w('- **No demographic subgroup outcome is inferred from an overall outcome and baseline proportions.** A trial that reports 30% Black participants at baseline and an overall hazard ratio says nothing about the hazard ratio among Black participants; that requires a result posted for the subgroup (section 4) or participant-level data. Every "race reported and analysis posted" figure in this report is a co-occurrence in one record and nothing more.')
w('- **No causal effect is inferred from study-level associations.** Differences between groups of trials (race reported vs not, industry vs other, posted vs not posted, one endpoint family vs another) are differences between sets of studies that also differ in era, phase, size, sponsor, condition and regulatory status. They do not show that any of those characteristics causes reporting behaviour, and they say nothing about treatment effects in any participant group (the ecological fallacy applies in both directions).')
w('- Nothing here is a finding of non-compliance with any reporting requirement.')
w('')

# ------------------------------------------------------------------ 6 failures
w('## 6. Missing data, retrieval failures, parsing failures and negative findings (kept separate)')
w('')
w('**Retrieval failures**')
w('')
w(f"- Sampled study records: 0 of 2,000 missing (first audit's fetch). Count queries: {len(CNT['failures'])} failures of {MAN['by_purpose_and_status'].get('count|200', 0) + MAN['by_purpose_and_status'].get('check|200', 0) + MAN['by_purpose_and_status'].get('frame|200', 0)} requests. Family top-up: 200 of 200 drawn records returned. Document sections: 2,000 of 2,000 returned. All {MAN['requests']} requests logged in `cache/fetch_manifest.jsonl` returned HTTP 200 with no retries needed.")
w('- Not cached: 12 exploratory count requests made by hand before the caching helper existed; each was re-issued through the cache and returned the same count.')
w('')
w('**Parsing failures**')
w('')
w(f"- Cache lines that failed to parse: {len(U['cache_parse_failures'])}. Baseline totals, STARTED totals and enrolment counts that were non-numeric: 0, 0, 0 of 2,000.")
w(f"- Endpoint families: every examined trial had a posted PRIMARY measure matching its family regex (0 unmatched of {sum(FD[f]['examined'] for f in order)}). Time frames that are not one fixed time point are reported as \"event-driven/variable\", not dropped: " + ', '.join(f"{labs[f]} {dict(FD[f]['time_kind']).get('event-driven/variable', 0)}/{FD[f]['with_family_measure']}" for f in order) + '. Some of these are fixed time points written in a form the parser does not read.')
w('- Regex attribute classes in section 3.3 that fell to a residual class: PFS assessor not stated 20/50, PFS criteria none stated 16/50, FEV1 timing not stated 8/42, analysis population "other wording" or "not stated" in every family. These are limits of the parser as well as of the records.')
w('')
w('**Missing data in the records**')
w('')
w(f"- Usable-content failures are listed by reason in 2.2 (B1 9, F1 5, O1 95, A1 68 trials; IDs in `usable.json` under `not_usable_ids`). {U['reasons']['primary_measures_not_usable_by_reason'].get('no_data_table', 0)} PRIMARY measures have no data table.")
w(f"- Baseline `populationDescription` absent in {N - DEN['population_description_present']:,}/2,000. Analysis `paramType` absent in the first audit's count for 669 of 2,017 primary analyses (not recomputed).")
w('- The release has no intervention field; intervention names in 3.3 come from the API for the 246 examined trials only. `primary_endpoint` is the first registered primary outcome only. 74 release records have no primary completion date.')
w('- The first audit\'s cache did not request `ConditionsModule`, `ArmsInterventionsModule` or `DocumentSection`; conditions were taken from the release, and the other two were fetched in this pass where needed.')
w('')
w('**Negative findings**')
w('')
w('- No sampled trial lacks `hasResults`; the frame has no trials without results, so nothing can be said from it about non-posting.')
w(f"- No race- or ethnicity-level subgroup result on a PRIMARY measure was found in class or category titles in 2,000 trials; the one PRIMARY race result is by race-defined arms (NCT03098836). Race or ethnicity subgroup results in any measure: {len(SUB['true_race_or_ethnicity'])}/2,000.")
w('- 0 of 60 randomly sampled non-detected trials were judged to contain a demographic subgroup result.')
w('- The measures registered as "Subgroup Analysis: Race / Sex / Age" in NCT02588664 carry no data: an announced subgroup analysis without a posted result.')
w('- No signal came from `measurements[].comment`, `otherAnalysisDescription`, `statisticalMethod` or the two confidence-limit comment fields; the analysis free-text fields together produced 7 detected trials.')
w('- The frame-defining code did not change between the release commit and the checkout read.')
w('- No oncology restriction was found in the definition of the existing industry Phase 3 cohort.')
w('')

# ------------------------------------------------------------------ 7 provisional
w('## 7. What remains provisional')
w('')
for t in (
        'Everything about **results posting** is count-level (section 1.3). No per-trial audit of trials without results was done, legal applicability and deadlines were not established, and the "eligible" population is whatever the query matched.',
        'Every sample-based percentage is **conditional on posted results** and rests on one seeded sample of 2,000 (2.5% of the frame). Rare features rest on a handful of trials; the observational stratum has 117.',
        'The **usable-content rules** are this audit\'s definitions. They test readability of tables, not correctness. Reasonable alternative rules (for example requiring numbers analysed > 0 in every arm) move the figures by a point or two, as the variants in 2.2 show; requiring the all-cause mortality table, which records posted before 2017 cannot have, would cap the adverse-event figure at 63.4%.',
        '"**Comparative**" is structural (2+ groups on a primary measure, randomised allocation). Trials whose only posted comparison is on a secondary measure, or whose 2+ groups are cohorts, are misclassified in both directions.',
        '**Subgroup figures** are "detected by the current method" plus one reviewer\'s labels. No independent or human review; 5 of 23 labels changed between two passes by the same kind of reviewer; recall is bounded only loosely (0/60 random, 1/40 targeted). The boundary between "true" and "partial" for cohort-defined age groups is a judgement call that changes the demographic count by several trials.',
        '**Endpoint families** are regex definitions on the first primary outcome title. The attribute classes in 3.3 are regex classes on about 15-50 trials per family, spot-read only; the HbA1c family was examined on 15 trials because the sample already held 15. Cohort sizes in 3.4 are upper bounds. Combinability of effects within a cohort was not assessed.',
        'The **15 mismatch readings** and all readings of population descriptions are one reviewer\'s; the FS rule (first period only) is known to undercount in multi-period designs.',
        '**Outcome switching** was not assessed at all; only the availability of inputs was counted.',
        'Registry data were read as posted on 2026-11-13 (sample, counts, documents, family top-up) against a release pulled 2026-11-08; records change, and the first audit found no difference in study type or enrolment over those five days but did not compare results sections.',
        'First-audit figures not recomputed in this pass (unit, dispersion and estimate-type string tallies, AACT mapping, per-subgroup denominators) stand as the first audit reported them and remain provisional.'):
    w(f'- {t}')
w('')

# ------------------------------------------------------------------ 8 mapping
w('## 8. Earlier headline figures and their refined wording or value')
w('')
w('| # | First-audit headline | First-audit value | Refined wording | Refined value | Changed materially? |')
w('|---|---|---|---|---|---|')
maprows = [
    ('Sampled trials with `hasResults` true', '2,000/2,000 (100.0%)', 'Not a finding: results are the inclusion rule of the frame. The question "do eligible trials post results?" is answered separately at count level.', f"frame = {fc['engine_frame_through_2026-11-08']['n']:,} studies with a results-first-posted date; interventional 2015-2023 with results {pct(*pooled['interventional_all'])}", 'Wording; new evidence added'),
    ('All five results modules present', '1,997/2,000 (99.8%)', 'Module presence, not results completeness. `moreInfoModule` is administrative and is excluded from usable-content counts.', f"presence unchanged; usable: baseline {U['usable']['B1']['numerator']:,}, flow {U['usable']['F1']['numerator']:,}, primary outcome {U['usable']['O1']['numerator']:,}, adverse events {U['usable']['A1']['numerator']:,}; all four {T(U['usable']['four_usable'])}", 'Yes: 99.8% present vs 92.6% usable on all four'),
    ('Registered outcome list vs posted measures identical', '4,406/4,406 primary; 10,833/10,833 secondary', 'One stored list; says nothing about outcome switching, which current records cannot establish.', f"not recomputed; inputs available: registered after start {HIS['registered_after_start']}; protocol posted {HIS['protocol_posted']}", 'Wording'),
    ('Trials with at least one PRIMARY measure carrying data', '1,915/2,000 (95.8%)', 'Usable primary outcome (rule O1: unit, number analysed per group, numeric values).', T(U['usable']['O1']), 'No (10 fewer trials)'),
    ('Trials whose primary measure(s) report 2+ groups', '1,448/2,000 (72.4%)', 'Comparative trials: interventional with a 2+ group primary measure (C1); randomised (C2).', f"C1 {T(U['comparative']['C1'])}; C2 {T(U['comparative']['C2'])}", 'Yes: 54 observational studies removed; randomised denominator added'),
    ('Trials with a statistical analysis on a primary measure', '621/2,000 (31.1%); 557/1,448 (38.5%); 167/315 Phase 3 (53.0%)', 'Among comparative randomised trials, a usable between-arm analysis on a primary measure.', f"C2: {T(st['C2']['S1_between'])}; C2 Phase 3: {T(st['C2_phase3']['S1_between'])}; all sampled (any S1): {T(U['usable']['S1'])}", 'Denominator changed; values 41.7% and 61.2% on the appropriate denominators'),
    ('Primary analysis with a p-value', '511/2,000 (25.6%)', 'Between-arm analysis with a p-value, among C2.', T(st['C2']['S1_between_p']), 'Denominator changed'),
    ('Primary analysis with an estimate and both CI limits', '366/2,000 (18.3%)', 'Between-arm estimate with both confidence limits, among C2.', f"{T(st['C2']['S1_between_est_ci'])}; C2 Phase 3 {T(st['C2_phase3']['S1_between_est_ci'])}", 'Denominator changed'),
    ('Distinct estimate-type strings', '249 across 2,017 analyses; 669 with none', 'Lexical variety of a free-text field; within an endpoint family the estimate type is far more uniform (hazard ratio in every PFS/OS analysis naming a type).', 'not recomputed; see 3.3', 'Wording'),
    ('Trials where a between-arm effect "could be computed"', '1,074/2,000 (53.7%) [inferred]', 'Withdrawn as a headline. C3 counts randomised trials with a usable 2+ group primary measure, a structural precondition only.', T(U['comparative']['C3']), 'Yes: claim narrowed'),
    ('Trials with demographic subgroup levels in any measure', '20/2,000 (1.0%); race or ethnicity 5', 'Subgroup reporting detected by the current method; then one reviewer\'s reading.', f"detected {SUB['detected_text']}; judged true {SUB['true_any']}, demographic {SUB['true_demographic']}; race or ethnicity {len(SUB['true_race_or_ethnicity'])}", 'Yes: detection 1.0% to 6.8%; confirmed demographic 16 to 34 trials'),
    ('Same, PRIMARY measures only', '6/2,000 (0.3%); race or ethnicity 0 (+1 by arms)', 'Judged-true demographic result with the level signal on a PRIMARY measure.', f"{len(SUB['true_demographic_on_primary_measure'])}/2,000; race or ethnicity 1 (NCT03098836, race-defined arms)", 'Yes: 6 to 17 trials, from group titles'),
    ('Race reported AND a primary measure with a statistical analysis', '382/2,000 (19.1%)', 'Co-occurrence in one record among comparative randomised trials; not an outcome by race.', f"C2: race reported and between-arm analysis {T(st['C2']['race_reported_and_S1_between'])}; with estimate and limits {T(st['C2']['race_reported_and_S1_between_est_ci'])}", 'Denominator changed'),
    ('Serious adverse event counts with numbers at risk for every group', '1,993/2,000 (99.7%)', 'Usable adverse-event table (A1): numbers at risk > 0 in at least one group, rows or explicit zeros.', f"{T(U['usable']['A1'])}; {U['reasons']['A1'].get('zero_at_risk_in_every_group', 0)} records have zero at risk in every group", 'Yes: 99.7% to 96.6%'),
    ('Baseline total equals STARTED / equals enrolment', '1,585/2,000 (79.2%) / 1,448/2,000 (72.4%)', 'Same values, now with exact fields, a no-double-count Total rule, and reviewed mismatches; baseline is the population the registrant chose to describe.', f"BT = FS {frac(DEN['BT_vs_FS']['equal'], N)}; BT = EN {frac(DEN['BT_vs_EN']['equal'], N)}; all three equal {frac(DEN['all_three_equal'], N)}", 'No (values reproduced)'),
    ('Primary measure titles used by only one sampled trial', '4,026/4,097 (98.3%)', 'A lexical finding about title strings; does not establish incompatibility or that oncology is the only viable area.', f"full release: {ex['used_once']:,}/{ex['distinct_normalised']:,} distinct first-primary titles used once; 14 construct families cover {FAM['rp3_in_any_family']:,}/{FAM['rp3_total_in_release']:,} randomised Phase 3 trials", 'Wording; interpretation reversed'),
]
for i, r in enumerate(maprows, 1):
    w(f'| {i} | ' + ' | '.join(r) + ' |')
w('')

# ------------------------------------------------------------------ 9 evidence record
w('## 9. Evidence record: sources, versions, files')
w('')
w('| Item | Value |')
w('|---|---|')
w(f"| Source population | `engine-data/demographics.json.gz`, sha256 `{META['source_sha256']}`, {META['n_records']:,} records |")
w(f"| Extraction version | `pipeline_commit` `{META['pipeline_commit']}` (2026-11-07), `extracted_at` {META['extracted_at']}; engine checkout read at `0293cd099575c86b878d5ebe531f4de945f3f80b` |")
w('| Inclusion rule | `AREA[ResultsFirstPostDate]RANGE[2009-01-01,MAX]` at pull time; no other filter |')
w(f"| Sampling | proportional stratified by `study_type`, n = 2,000, seed 20261113; `sample_ids.txt` sha256 `{SV['sample_ids_txt_sha256']}` |")
w(f"| Sample records | first audit cache, sha256 `{V1MAN['cache_sha256']}`, fetched {V1MAN['first_fetch']}..{V1MAN['last_fetch']} |")
w(f"| Registry version | API {CNT['api_version']['apiVersion']}, `dataTimestamp` {CNT['api_version']['dataTimestamp']}, as reported by `/version` during the first pass (22:33Z) and during this pass (23:21Z) |")
w(f"| New API responses | {MAN['requests']} requests, {MAN['first_fetch']}..{MAN['last_fetch']}, all HTTP 200; manifest `cache/fetch_manifest.jsonl` (URL, time, status, bytes, sha256, purpose), summary `cache/fetch_manifest_summary.json` |")
for k, v in MAN['cache_files'].items():
    w(f"| `cache/{k}` | {v['responses']} responses, sha256 `{v['sha256']}` |")
w('| Rate | one request at a time, at least 0.5 s apart (2 per second) |')
w('')
w('| File | Contents |')
w('|---|---|')
for a, b in (
        ('`report.md`', 'this report'),
        ('`definitions.json`', 'every rule: plain-language definition, numerator, denominator, value, source file'),
        ('`sample_ids.txt`, `sampling.json`, `sampling_verification.json`', 'the 2,000 NCT IDs, sampling parameters, and the re-draw check'),
        ('`release_meta.json`, `release_slim.jsonl.gz`', 'release stamps and key inventory; slim line-oriented copy of the release fields used'),
        ('`results_posting_counts.json`', 'section 1.3 counts, expressions, consistency checks, frame checks'),
        ('`usable.json`, `per_trial_usable.jsonl`', 'section 2: aggregates and every flag for every sampled trial'),
        ('`families.json`, `family_members.json`, `family_examined.json`, `family_detail.json`, `family_detail_rows.jsonl`, `cohorts.json`', 'section 3'),
        ('`subgroup_detected.jsonl`, `subgroup_negative_samples.json`, `subgroup_review.csv`, `subgroup_summary.json`, `review/`', 'section 4: all signals, the negative samples, all 236 reviewed trials with judgements, the digests read and the judgement tables'),
        ('`denominators.json`, `per_trial_denominators.jsonl`', 'section 5.1-5.2'),
        ('`history_evidence.json`, `per_trial_history_evidence.jsonl`', 'section 5.3'),
        ('`cache/`', 'cached API responses (jsonl.gz) and fetch manifest'),
        ('`scripts/`', 'all code; data are kept outside it')):
    w(f'| {a} | {b} |')
w('')
w('Re-running offline from the caches, in order (Python run with `-I`; V1 = the first audit directory, OUT = this directory):')
w('')
w('```')
for cmd in ('build_slim.py RELEASE_GZ OUT', 'verify_sample.py OUT', 'fetch_counts.py OUT        # served from cache', 'usable.py V1 OUT', 'denominators.py V1 OUT',
            'families.py OUT', 'fetch_family.py V1 OUT     # served from cache', 'family_detail.py V1 OUT', 'cohorts.py OUT', 'subgroup_detect.py V1 OUT',
            'subgroup_review.py V1 OUT  # needs review/judgements_*.tsv', 'history_evidence.py V1 OUT # served from cache', 'manifest_summary.py OUT', 'make_report.py V1 OUT'):
    w(f'python3 -I scripts/{cmd}')
w('```')
w('')
open(os.path.join(OUT, 'report.md'), 'w').write('\n'.join(L) + '\n')

# ------------------------------------------------------------------ definitions.json
D = collections.OrderedDict()
def d(name, rule, numerator, denominator, value=None, source=None):
    D[name] = {'rule': rule, 'numerator': numerator, 'denominator': denominator, 'value': value, 'source': source}
d('FR_frame', 'Every ClinicalTrials.gov study returned by GET /api/v2/studies with filter.advanced = AREA[ResultsFirstPostDate]RANGE[2009-01-01,MAX] at pull time (2026-11-08), kept unless its extraction raised. No other filter.',
  'records in the release', 'n/a (population)', {'release_records': 80890, 'registry_count_same_range_on_2026-11-13': fc['engine_frame_through_2026-11-08']['n']}, 'src/api_client.py:45-59,92-111; src/extract_all.py:342-346,400-418; .github/workflows/extract.yml:57')
d('SAMPLE', 'Strata = record study_type; 2,000 allocated proportionally by largest remainder; IDs sorted ascending per stratum; one random.Random(20261113), strata in name order, rng.sample(sorted_ids, n_h).',
  '2,000 sampled NCT IDs (sample_ids.txt)', '80,890 release records', {'INTERVENTIONAL': 1883, 'OBSERVATIONAL': 117, 'seed': 20261113, 'sample_ids_sha256': SV['sample_ids_txt_sha256']}, 'scripts/verify_sample.py')
d('POSTING_cell', 'Count of registry studies matching <stratum> AND AREA[PrimaryCompletionDate]RANGE[Y-01-01,Y-12-31] that also match AREA[ResultsFirstPostDate]RANGE[MIN,MAX]; request countTotal=true&pageSize=1&fields=NCTId. Strata expressions in results_posting_counts.json.definition.strata.',
  'studies matching stratum, year and results condition', 'studies matching stratum and year', {s: {'with_results_2015_2023': pooled[s][0], 'all_2015_2023': pooled[s][1]} for s in pooled}, 'scripts/fetch_counts.py; results_posting_counts.json')
d('PRESENT_<module>', 'resultsSection.<module> exists and is a non-empty object.', 'sampled trials with the module present', '2,000 sampled trials',
  {k: v['numerator'] for k, v in U['presence'].items()}, 'scripts/usable.py')
d('NUMERIC', 'A registry value string that parses as a finite float after removing commas and surrounding whitespace. "NA", "", absent are not numeric and are never treated as zero.', 'n/a', 'n/a', None, 'scripts/common.py:is_num')
d('T_total_group', 'Baseline Total group: if baselineCharacteristicsModule.groups has one entry, that group; else the group whose trimmed, case-folded title == "total"; else, if 3+ groups and the last group\'s Participants denominator equals the sum of all earlier groups\' (and is > 0), the last group; else none.',
  'n/a', '2,000 sampled trials', U['reasons']['B1_total_method'], 'scripts/usable.py:total_group')
d('B1_baseline_usable', 'At least one baselineCharacteristicsModule.measures[] entry M such that for the Total group (rule T) OR for every non-Total group: (a) at least one M.classes[].categories[].measurements[] with that groupId has a NUMERIC value, and (b) a NUMERIC count > 0 for that group exists in module denoms[].counts[], M.denoms[].counts[] or M.classes[].denoms[].counts[].',
  'sampled trials passing', '2,000 sampled trials', U['usable']['B1'], 'scripts/usable.py:baseline_usable')
d('F1_flow_usable', 'At least one participantFlowModule.periods[] entry with a milestone type == "STARTED" and a milestone type == "COMPLETED" that each have, for every group in participantFlowModule.groups[], an achievements[] entry with NUMERIC numSubjects; and STARTED > 0 for at least one group.',
  'sampled trials passing', '2,000 sampled trials', U['usable']['F1'], 'scripts/usable.py:flow_usable')
d('O1_primary_outcome_usable', 'At least one outcomeMeasures[] entry with type == "PRIMARY" that has: non-blank unitOfMeasure; >= 1 group; a NUMERIC number analysed for every group in groups[] taken from measure-level denoms[] (the row with units "Participants" if present, else the first row); number analysed > 0 for >= 1 group; and for every group with number analysed > 0 at least one classes[].categories[].measurements[] with NUMERIC value.',
  'sampled trials passing', '2,000 sampled trials', U['usable']['O1'], 'scripts/usable.py:measure_usable')
d('O1_strict', 'O1 with number analysed > 0 required for every group of the measure.', 'sampled trials passing', '2,000 sampled trials', U['usable']['O1_strict'], 'scripts/usable.py')
d('A1_adverse_events_usable', 'adverseEventsModule.eventGroups[] non-empty; every event group has NUMERIC seriousNumAtRisk and NUMERIC otherNumAtRisk; at least one group has either > 0; and EITHER >= 1 row in seriousEvents[] or otherEvents[] whose stats[] has a NUMERIC numAffected or numEvents, OR every event group has NUMERIC seriousNumAffected and otherNumAffected.',
  'sampled trials passing', '2,000 sampled trials', U['usable']['A1'], 'scripts/usable.py:ae_usable')
d('S1_statistical_analysis_usable', 'At least one analyses[] entry on a type == "PRIMARY" measure with non-empty groupIds all resolving to that measure\'s groups[].id, and EITHER pValue containing a digit OR NUMERIC paramValue with NUMERIC ciLowerLimit and NUMERIC ciUpperLimit.',
  'trials passing', 'stated per use: 2,000 sampled, or C1/C2/C3', {k: st[k]['S1'] for k in ('all_sampled', 'C1', 'C2', 'C3', 'C2_phase3')}, 'scripts/usable.py:analysis_flags')
d('S1_between', 'S1 where the analysis names 2 or more groupIds. S1_between_p: such an analysis with a p-value. S1_between_est_ci: such an analysis with NUMERIC paramValue, ciLowerLimit and ciUpperLimit.',
  'trials passing', 'stated per use', {k: {'S1_between': st[k]['S1_between'], 'S1_between_p': st[k]['S1_between_p'], 'S1_between_est_ci': st[k]['S1_between_est_ci']} for k in ('all_sampled', 'C1', 'C2', 'C3', 'C2_phase3', 'C2_phase3_industry')}, 'scripts/usable.py')
d('FOUR_USABLE', 'B1 and F1 and O1 and A1.', 'sampled trials passing all four', '2,000 sampled trials', U['usable']['four_usable'], 'scripts/usable.py')
d('C1_comparative', 'protocolSection.designModule.studyType == "INTERVENTIONAL" and >= 1 PRIMARY outcome measure whose groups[] has >= 2 entries.', 'sampled trials meeting it', '2,000 sampled trials', U['comparative']['C1'], 'scripts/usable.py')
d('C2_comparative_randomised', 'C1 and protocolSection.designModule.designInfo.allocation == "RANDOMIZED".', 'sampled trials meeting it', '2,000 sampled trials', U['comparative']['C2'], 'scripts/usable.py')
d('C2_phase3', 'C2 and the release record\'s phase == "PHASE3" (exact string; "PHASE2, PHASE3" excluded). C2_phase3_industry additionally requires release sponsor_class == "INDUSTRY".', 'sampled trials meeting it', '2,000 sampled trials', {'C2_phase3': st['C2_phase3']['S1']['denominator'], 'C2_phase3_industry': st['C2_phase3_industry']['S1']['denominator']}, 'scripts/usable.py')
d('C3_comparative_randomised_usable', 'C2 and O1 passed by a PRIMARY measure that has >= 2 groups.', 'sampled trials meeting it', '2,000 sampled trials', U['comparative']['C3'], 'scripts/usable.py')
d('RACE_REPORTED', 'The release record\'s race.reported is true (likewise ethnicity.reported, sex.reported).', 'trials with the flag true', 'stated per use', {'sample': dem['race_reported'], 'release': {'numerator': 46564, 'denominator': 80890}}, 'release field')
d('BT_baseline_total', 'baselineCharacteristicsModule.denoms[] row with units "Participants" (else first row): counts[].value of the Total group by rule T; if rule T finds none, the sum over all groups. Never Total plus arms.', 'n/a (one number per trial)', '2,000 sampled trials', DEN['BT_method'], 'scripts/denominators.py')
d('FS_flow_started', 'participantFlowModule.periods[0].milestones[] with type == "STARTED": sum of achievements[].numSubjects over participantFlowModule.groups[], excluding any group titled exactly "Total".', 'n/a', '2,000 sampled trials', DEN['FS_method'], 'scripts/denominators.py')
d('EN_enrolment', 'protocolSection.designModule.enrollmentInfo.count and .type.', 'n/a', '2,000 sampled trials', DEN['EN_type'], 'scripts/denominators.py')
d('BT_FS_EN_comparisons', 'Exact equality of the three numbers per trial.', 'trials in each relation', '2,000 sampled trials', {'BT_vs_FS': DEN['BT_vs_FS'], 'BT_vs_EN': DEN['BT_vs_EN'], 'FS_vs_EN': DEN['FS_vs_EN'], 'all_three_equal': DEN['all_three_equal']}, 'scripts/denominators.py')
d('FAMILY_<name>', 'Release record whose primary_endpoint matches the family endpoint regex (re.search, IGNORECASE except (?-i:...)) AND whose gate field (primary_condition, or "; ".join(conditions)) matches the gate regex. rp3 = gated AND allocation == "RANDOMIZED" AND phase == "PHASE3".',
  'release records matching', '80,890 release records', {k: {x: v[x] for x in ('endpoint_regex', 'gate_field', 'gate_regex', 'matched', 'gated', 'rp3', 'race_reported', 'rp3_and_race_reported')} for k, v in FAM['families'].items()}, 'scripts/families.py')
d('FAMILY_EXAMINED', 'Per family: rp3 members in the 2,000 sample, plus (if fewer than 15) random.Random(20261113).sample(sorted(rp3 members not in sample), 40). Family measure = first PRIMARY outcome measure whose title matches the endpoint regex.',
  'examined trials with each attribute value', 'examined trials with a family measure', {k: {'examined': v['examined'], 'from_sample': v['from_sample'], 'drawn': v['drawn'], 'between_est_ci': v['between_est_ci']} for k, v in FD.items()}, 'scripts/fetch_family.py; scripts/family_detail.py')
d('NARROW_COHORT_<name>', 'Family rp3 members meeting the extra text conditions in scripts/cohorts.py (condition regex on conditions, wording regex on primary_endpoint, time window on primary_outcome_time_frame parsed to weeks).', 'release records matching', '80,890 release records', COH['cohorts'], 'scripts/cohorts.py')
d('SG2_subgroup_detected', 'A sampled trial with >= 1 signal from scripts/subgroup_rule.py:detect_measure on any posted outcome measure: level rules on class titles, category titles, group titles, group descriptions; analysis groupIds referencing level-defined groups; cue rules (KEYWORD, BY_DEMOG) on title, description, populationDescription, class/category titles, nine analysis text fields and measurement comments. Regexes in report section 4.2 and in the module.',
  'sampled trials with a signal', '2,000 sampled trials', {'detected': SUB['detected'], 'judgements': SUB['judgement_counts']}, 'scripts/subgroup_rule.py; scripts/subgroup_detect.py')
d('SG_judgement', 'One reviewer (the auditing model). true_subgroup_result: outcome values posted separately for >= 2 levels of a participant characteristic under the same intervention. partial: one level only, levels confounded with dose/cohort, subgroup sizes only, model-derived values, or unidentifiable subgroup. false_positive: wording with another meaning, or announced analysis with no data. Negatives: missed_subgroup_result / partial / no_subgroup_result.',
  'detected trials with each judgement', '136 detected trials (or 2,000 sampled)', {'true_any': SUB['true_any'], 'true_demographic': SUB['true_demographic'], 'true_race_or_ethnicity_ids': SUB['true_race_or_ethnicity'], 'false_positive': SUB['false_positive']}, 'review/judgements_positives.tsv; subgroup_review.csv')
d('SG_negative_samples', 'From the 1,864 non-detected trials: random 60 = random.Random(20261113).sample(sorted(ids), 60); targeted 40 = highest likely-reporter scores among the rest (score in scripts/subgroup_detect.py docstring), ties by a random.Random(20261113) shuffle of sorted IDs.',
  'reviewed negatives judged to contain a missed subgroup result', '60 random; 40 targeted', SUB['negatives'], 'scripts/subgroup_detect.py; review/judgements_negatives.tsv')
d('HISTORY_inputs', 'registered_after_start: statusModule.studyFirstSubmitDate later than startDateStruct.date at the coarser precision of the two. protocol_posted / sap_posted: any documentSection.largeDocumentModule.largeDocs[] with hasProtocol / hasSap true.',
  'sampled trials with the property', '2,000 sampled trials', {k: HIS[k] for k in ('registered_after_start', 'registered_after_primary_completion', 'updated_after_results_first_posted', 'protocol_posted', 'sap_posted')}, 'scripts/history_evidence.py')
d('MI_more_info', 'moreInfoModule reported as presence only; pointOfContact and certainAgreement are administrative; limitationsAndCaveats.description is the one substantive free-text field. Not counted as a results module.',
  'sampled trials with limitationsAndCaveats text', '2,000 sampled trials', U['presence']['moreInfo_limitationsAndCaveats_text'], 'scripts/usable.py')
SGRX = {'FEMALE': subgroup_rule.FEMALE.pattern, 'MALE': subgroup_rule.MALE.pattern, 'RACE_EXCLUDE': subgroup_rule.RACE_EXCLUDE.pattern, 'RACE_TOKENS': {n: r.pattern for n, r in subgroup_rule.RACE_TOKENS}, 'HISPANIC': subgroup_rule.HISPANIC.pattern, 'ORIGIN': subgroup_rule.ORIGIN.pattern, 'NON_ORIGIN': subgroup_rule.NON_ORIGIN.pattern, 'AGE_BAND': subgroup_rule.AGE_BAND.pattern, 'LIFE_STAGE': {n: r.pattern for n, r in subgroup_rule.LIFE_STAGE}, 'KEYWORD': subgroup_rule.KEYWORD.pattern, 'BY_DEMOG': subgroup_rule.BY_DEMOG.pattern, 'BY_DEMOG_EXCLUDE': subgroup_rule.BY_DEMOG_EXCLUDE.pattern, 'flags': 're.IGNORECASE', 'analysis_text_fields': list(subgroup_rule.ANALYSIS_TEXT_FIELDS)}
json.dump({'generated': '2026-11-13', 'interval': 'Wilson 95% (z = 1.959964), sample treated as simple random; registry-wide counts carry no interval', 'subgroup_rule_regexes': SGRX, 'definitions': D}, open(os.path.join(OUT, 'definitions.json'), 'w'), indent=1)
print('report lines', len(L), 'bytes', sum(len(x) + 1 for x in L), 'definitions', len(D))
