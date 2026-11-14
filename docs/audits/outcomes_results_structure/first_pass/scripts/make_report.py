"""Render report.md from analysis.json, sampling.json and cache/fetch_manifest.json.

Run: python3 -I make_report.py OUTDIR
Every number in the report is read from those files at render time; the only hand-entered
content is prose, the AACT mapping (documentation knowledge, labelled) and REVIEW (the auditor's
reading of each flagged subgroup measure, labelled as a judgement).
"""
import json, sys, os
OUT = sys.argv[1]
A = json.load(open(os.path.join(OUT, 'analysis.json')))
SM = json.load(open(os.path.join(OUT, 'sampling.json')))
FM = json.load(open(os.path.join(OUT, 'cache', 'fetch_manifest.json')))
N = A['meta']['n_sample']

def f(x):
    return f"{x['k']:,}/{x['n']:,} ({x['pct']}%)"
def fc(x):
    return f"{x['k']:,}/{x['n']:,} ({x['pct']}%; 95% CI {x['ci95'][0]}-{x['ci95'][1]})"
def esc(s):
    return str(s).replace('|', '\\|').replace('\n', ' ').replace('<', '&lt;').replace('>', '&gt;')
def cut(s, n):
    s = str(s); return s if len(s) <= n else s[:n - 1] + '…'
def tdist(d, col, top=None, denom_label=None):
    rows = d['rows'][:top] if top else d['rows']
    out = [f"| {col} | n | % of {denom_label or format(d['n'], ',')} |", '|---|---:|---:|']
    out += [f"| {esc(r['value'])} | {r['k']:,} | {r['pct']} |" for r in rows]
    return '\n'.join(out)
def tfrac(pairs, col='Item', denom='Denominator'):
    out = [f"| {col} | n / {denom} | % | 95% CI |", '|---|---:|---:|---|']
    out += [f"| {esc(k)} | {v['k']:,} / {v['n']:,} | {v['pct']} | {v['ci95'][0]}-{v['ci95'][1]} |" for k, v in pairs]
    return '\n'.join(out)

M, R, P, AN, SG, AE, PF, BL, CO, CP = (A[k] for k in ('modules', 'registered_vs_posted', 'primary_measures', 'analyses', 'subgroups', 'adverse_events', 'participant_flow', 'baseline', 'cooccurrence', 'comparability'))
ri = R['registered_vs_posted_raw_identity']
anyM, priM = SG['trials_any_measure'], SG['trials_primary_measure']

# The auditor's reading of each flagged trial's representative measure (a JUDGEMENT, not a measurement).
REVIEW = {
 'NCT00261443': ('yes', 'AE counts by age, sex and race (White / Non-White); subgroup n only as text inside the class titles'),
 'NCT00461305': ('yes', 'post-hoc efficacy by age (<30 / >=30) alongside non-demographic subgroups'),
 'NCT00980005': ('yes', 'immunogenicity by age stratum, with per-class denominators'),
 'NCT01060059': ('partly', 'counts by sex as a "factor associated with treatment choice"; not an efficacy result'),
 'NCT01256658': ('yes', 'haemoglobin by age group; subgroup n only as text inside the class titles'),
 'NCT02165826': ('no', 'population-PK / simulated values at covariate levels (Age=40, Sex=female, Asian / non-Asian): model output, not observed subgroup results'),
 'NCT02246439': ('yes', 'primary efficacy outcome by age (<18 / >=18) with per-class denominators'),
 'NCT02304406': ('yes', 'biomarker prevalence by gender (classes) and by race (title; classes White/Caucasian, Arabic, Others)'),
 'NCT02588664': ('no', 'three measures titled "Subgroup Analysis: Race / Sex / Age" with reportingStatus POSTED but no data table'),
 'NCT02688764': ('yes', 'post-hoc change in serum phosphorus by age group, single arm'),
 'NCT03500094': ('yes', 'primary PK parameters by age band, single arm'),
 'NCT03722810': ('partly', 'title cue only; the arms are Female / Male students, so sex is the study design, not a subgroup of a trial'),
 'NCT03993119': ('partly', 'observational; title cue only; arms are "Sex: Male/Female" and age bands; descriptive treatment patterns'),
 'NCT04024228': ('yes', 'primary immunogenicity outcome by age stratum (60-64, >=65)'),
 'NCT04105244': ('yes', 'primary outcome by sex x time point, with per-class denominators'),
 'NCT04259762': ('partly', 'sex- and age-specific screening tests (eligibility-defined rows), not a subgroup contrast of one outcome'),
 'NCT04323137': ('yes', 'post-hoc vaccination rate by race (five OMB categories), ethnicity and gender'),
 'NCT04354155': ('yes', 'dose requirement by age group (children <12 / >=12 years)'),
 'NCT04587167': ('yes', 'vaccination by sex; levels are categories, so no per-level denominators'),
 'NCT04907253': ('yes', 'primary biomarker outcome by sex x time point'),
 'NCT05009251': ('yes', 'vaccination rate by gender, race and ethnicity (other pre-specified)'),
 'NCT05329194': ('yes', 'exacerbation rate in listed subgroups, including adolescents and Black or African American'),
 'NCT05838664': ('partly', 'number of participants in each subgroup (elderly / non-elderly ...): subgroup sizes, not an outcome by subgroup'),
}
flag = SG['flagged_trials_all']
assert {h['nct_id'] for h in flag} == set(REVIEW), 'REVIEW must cover exactly the flagged trials: ' + str({h['nct_id'] for h in flag} ^ set(REVIEW))
rv = {v: sum(1 for x in REVIEW.values() if x[0] == v) for v in ('yes', 'partly', 'no')}
yes_ids = {k for k, v in REVIEW.items() if v[0] == 'yes'}
yes_race_eth = sorted(h['nct_id'] for h in flag if h['nct_id'] in yes_ids and set(h['dims_tier1_trial'] + h['dims_tier2_trial']) & {'race', 'ethnicity'})
_pvf = {r['value']: r['k'] for r in AN['pValue_form']['rows']}
PV_N = AN['field_presence_among_primary_analyses']['pValue']['k']
PV_UB = {'k': _pvf.get('upper bound (<, <=)', 0), 'pct': round(100 * _pvf.get('upper bound (<, <=)', 0) / PV_N, 1)}
PV_OP = {'k': PV_N - _pvf.get('plain number', 0), 'pct': round(100 * (PV_N - _pvf.get('plain number', 0)) / PV_N, 1)}
assert sum(v for k, v in _pvf.items() if k != '(absent)') == PV_N
yes_primary = sorted(h['nct_id'] for h in flag if h['nct_id'] in yes_ids and 'PRIMARY' in h['flagged_types'])

L = []
w = L.append
w(f"""# What ClinicalTrials.gov results sections contain for trials in the demographics dataset

Read-only audit, run 2026-11-13. Nothing was committed, pushed or edited in any repository; no paid API
or LLM call was made. Everything below is **measured on a seeded sample of {N:,} trials** unless it is
tagged **[inferred]** (my interpretation of a measurement), **[heuristic]** (a rule-based classification
defined in `scripts/analyze.py`) or **[documentation knowledge]** (recalled, not checked against data).
Percentages always carry their denominator; 95% intervals are Wilson intervals.

Output directory: `docs/audits/outcomes_results_structure/first_pass/`

## 0. The numbers that matter most

| # | Finding | Value |
|---|---|---|
| 1 | Sampled trials returned by the API with `hasResults` true | {f(A['meta']['hasResults_true'])} |
| 2 | All five results modules present | {f(M['all_five'])}; the only gap is `adverseEventsModule` in {N - M['adverseEventsModule']['k']} trials |
| 3 | Registered outcome list vs posted outcome measures | identical in title, time frame, description and order for {ri['PRIMARY_title_identical']:,}/{ri['PRIMARY_pairs']:,} primary and {ri['SECONDARY_title_identical']:,}/{ri['SECONDARY_pairs']:,} secondary pairs: the current record holds one list, not a prior registration to compare against |
| 4 | Trials with at least one PRIMARY measure that carries data | {fc(R['trials_with_posted_PRIMARY_carrying_data'])} |
| 5 | Trials whose primary measure(s) report 2 or more groups | {fc(P['trials_with_primary_having_2plus_groups'])} |
| 6 | Trials with at least one statistical analysis on a primary measure | {fc(AN['trials_with_primary_analysis']['of_all_sampled'])}; {f(AN['trials_with_primary_analysis']['of_trials_with_2plus_group_primary'])} of trials with a 2+ group primary; {f(AN['by_phase_frame']['PHASE3']['primary_analysis'])} of Phase 3 |
| 7 | Trials with a primary analysis carrying a p-value | {fc(AN['trial_level_of_all_sampled']['primary_analysis_with_pValue'])} |
| 8 | Trials with a primary analysis carrying an estimate and both CI limits | {fc(AN['trial_level_of_all_sampled']['primary_analysis_with_estimate_and_both_CI_limits'])} |
| 9 | Distinct estimate-type strings among primary analyses | {AN['estimate_paramType_top20']['distinct'] - 1} distinct non-blank `paramType` strings across {AN['n_primary_analyses']:,} analyses; {AN['field_presence_among_primary_analyses']['paramType']['n'] - AN['field_presence_among_primary_analyses']['paramType']['k']:,} analyses ({round(100 - AN['field_presence_among_primary_analyses']['paramType']['pct'], 1)}%) have none |
| 10 | Trials where a between-arm effect could be computed from arm-level primary data **[inferred definition, section 3.3]** | {fc(P['arm_level_effect_computable']['any'])} |
| 11 | Trials with demographic subgroup levels in any outcome measure's class/category titles **[heuristic]** | {fc(anyM['tier1_levels']['any_dimension'])}; race or ethnicity: {fc(anyM['tier1_levels']['race_or_ethnicity'])} |
| 12 | Same, PRIMARY measures only **[heuristic]** | {fc(priM['tier1_levels']['any_dimension'])}; race or ethnicity: {fc(priM['tier1_levels']['race_or_ethnicity'])} in class/category titles, plus {priM['supplementary_arm_titles_tier1']['race']['k']} trial whose arms are race-defined cohorts |
| 13 | Race reported in our record AND a primary measure with a statistical analysis | {fc(CO['race_reported_x_primary_analysis']['pct_both_of_all'])} (race reported in {f(CO['frame_counts']['race_reported'])}) |
| 14 | Serious-adverse-event counts with numbers at risk for every group | {f(AE['all_groups_have_seriousNumAtRisk'])}; all-cause mortality table in {f(AE['all_groups_have_deathsNumAtRisk'])} ({f(AE['deaths_table_by_results_year']['2021-2026'])} of results first posted 2021-2026) |
| 15 | Baseline total equals participant-flow STARTED total / equals actual enrollment | {f({'k': BL['baseline_vs_flow_STARTED']['rows'][0]['k'], 'n': BL['baseline_vs_flow_STARTED']['n'], 'pct': BL['baseline_vs_flow_STARTED']['rows'][0]['pct']})} / {next(f"{r['k']:,}/{BL['baseline_total_over_enrollment_actual']['n']:,} ({r['pct']}%)" for r in BL['baseline_total_over_enrollment_actual']['rows'] if r['value'] == '=1.00')} |
| 16 | Primary measure titles used by only one sampled trial | {f(CP['titles_used_by_one_trial_only'])} of distinct normalised titles; the 15 most common titles cover {f(CP['trials_covered_by_top15_titles'])} of trials |
""")

w(f"""## 1. Sample

- **Frame:** `engine-data/demographics.json.gz`, `extracted_at` {SM['extracted_at']}, `pipeline_commit` `{SM['pipeline_commit'][:12]}`, {SM['population_n']:,} records, all NCT IDs unique (file sha256 `{SM['source_sha256'][:16]}…`). The weekly cohort is every study with a results-first-post date (`src/api_client.py`: `AREA[ResultsFirstPostDate]RANGE[MIN,MAX]`), so every record has posted results by construction.
- **Strata:** the record's own `study_type`: {', '.join(f'{k} {v:,}' for k, v in SM['population_by_study_type'].items())}.
- **Allocation:** proportional to stratum size, {N:,} total, largest-remainder rounding. Exact quotas {', '.join(f'{k} {v}' for k, v in SM['exact_quota'].items())} gave **{', '.join(f'{k} {v:,}' for k, v in SM['sample_by_study_type'].items())}**.
- **Draw:** NCT IDs sorted ascending within each stratum; one `random.Random({SM['seed']})` generator, strata visited in name order, `rng.sample(sorted_ids, n_h)`. Script: `scripts/draw_sample.py`. Result: `sample_ids.txt` (sorted, sha256 `{SM['sample_ids_sha256'][:16]}…`), parameters in `sampling.json`.
- Because allocation is proportional the sample is self-weighting: unweighted sample proportions estimate proportions over the {SM['population_n']:,} records. The observational stratum is small (n = {SM['sample_by_study_type'].get('OBSERVATIONAL')}); anything said about observational studies alone is imprecise.

## 2. Fetch

- `GET https://clinicaltrials.gov/api/v2/studies?filter.ids=<50 ids>&pageSize=50&fields={FM['fields']}`; {len(open(os.path.join(OUT, 'sample_ids.txt')).read().split()) // 50} requests, one at a time, at least 0.5 s apart (2 requests/second ceiling), retry with backoff on 429/5xx. {FM['retries_this_run']} retries were needed.
- Fetched {FM['first_fetch']} to {FM['last_fetch']}; API version {FM['api_version'].get('apiVersion')}, `dataTimestamp` {FM['api_version'].get('dataTimestamp')}.
- **{FM['returned']:,} of {FM['requested']:,} IDs returned**, {len(FM['missing'])} missing, {len(FM['unexpected'])} unexpected. Every response carried a `nextPageToken` although each already contained its whole batch; coverage was verified by ID, not by the token.
- Raw responses (unparsed bodies with request metadata) are in `cache/raw_responses.jsonl.gz` (sha256 `{FM['cache_sha256'][:16]}…`); `scripts/analyze.py` reads only the cache, so the audit re-runs offline.
- Frame versus live registry, five days apart: `study_type` disagreed for {A['meta']['study_type_frame_vs_api_disagree']} of {N:,}; enrollment count equal for {f(BL['frame_enrollment_equals_api_enrollment'])}.

## 3. What the results sections contain

### 3.1 Module presence (denominator: {N:,} sampled trials)

{tfrac([(m, M[m]) for m in ('participantFlowModule', 'baselineCharacteristicsModule', 'outcomeMeasuresModule', 'adverseEventsModule', 'moreInfoModule')] + [('all five', M['all_five']), ('moreInfoModule.limitationsAndCaveats text', M['moreInfo_limitationsAndCaveats'])], 'Module', str(N))}

"Present" means the key exists and is non-empty. By study type the only difference is the adverse-events module: {f(M['by_study_type']['INTERVENTIONAL']['adverseEventsModule'])} interventional, {f(M['by_study_type']['OBSERVATIONAL']['adverseEventsModule'])} observational. Module presence is near-universal because the dataset is defined by having posted results; it says nothing about trials without results.

### 3.2 Registered outcomes versus posted outcome measures

Registered outcomes (`protocolSection.outcomesModule`), per trial (denominator {N:,}):

| | trials with any | total | median | p75 | p95 | max |
|---|---:|---:|---:|---:|---:|---:|
| primary | {f(R['trials_with_registered']['primary'])} | {R['registered_counts_summary']['total_registered_primary']:,} | {R['registered_counts_summary']['primary']['median']} | {R['registered_counts_summary']['primary']['p75']} | {R['registered_counts_summary']['primary']['p95']} | {R['registered_counts_summary']['primary']['max']} |
| secondary | {f(R['trials_with_registered']['secondary'])} | {R['registered_counts_summary']['total_registered_secondary']:,} | {R['registered_counts_summary']['secondary']['median']} | {R['registered_counts_summary']['secondary']['p75']} | {R['registered_counts_summary']['secondary']['p95']} | {R['registered_counts_summary']['secondary']['max']} |
| other | {f(R['trials_with_registered']['other'])} | {R['registered_counts_summary']['total_registered_other']:,} | {R['registered_counts_summary']['other']['median']} | {R['registered_counts_summary']['other']['p75']} | {R['registered_counts_summary']['other']['p95']} | {R['registered_counts_summary']['other']['max']} |

Posted outcome measures (`resultsSection.outcomeMeasuresModule.outcomeMeasures`), {P['n_all_measures']:,} in total:

| type | measures | % of {P['n_all_measures']:,} | trials with any (of {N:,}) | median per trial | p95 | max |
|---|---:|---:|---:|---:|---:|---:|
""" + '\n'.join(f"| {t} | {next((r['k'] for r in R['posted_measures_by_type']['rows'] if r['value'] == t), 0):,} | {next((r['pct'] for r in R['posted_measures_by_type']['rows'] if r['value'] == t), 0)} | {f(R['trials_with_posted_type'][t])} | {R['posted_per_trial'][t]['summary']['median']} | {R['posted_per_trial'][t]['summary']['p95']} | {R['posted_per_trial'][t]['summary']['max']} |" for t in ('PRIMARY', 'SECONDARY', 'OTHER_PRE_SPECIFIED', 'POST_HOC')) + f"""

Primary measures per trial (posted): {', '.join(f"{r['value']}: {r['k']:,} ({r['pct']}%)" for r in sorted(P['primary_measures_per_trial']['rows'], key=lambda r: ['1', '2', '3', '4-5', '6-10', '11-20', '21+', '0'].index(r['value'])))}.

**How often a registered primary has a posted measure: always, and by construction.**

- Count of registered primaries equals count of posted PRIMARY measures in {next(r['k'] for r in R['count_registered_primary_vs_posted_PRIMARY']['rows'] if r['value'] == 'equal'):,}/{N:,} trials; the one exception (NCT01881828) posts the same title twice.
- Compared position by position as raw strings, title, time frame and description are identical for {ri['PRIMARY_title_identical']:,}/{ri['PRIMARY_pairs']:,} primary pairs, {ri['SECONDARY_title_identical']:,}/{ri['SECONDARY_pairs']:,} secondary pairs and {ri['OTHER_PRE_SPECIFIED_title_identical']:,}/{ri['OTHER_PRE_SPECIFIED_pairs']:,} other pairs (time frame and description counts are the same as the title counts in each row); {ri.get('PRIMARY_trials_with_count_mismatch', 0)} trial's primary list and {ri.get('SECONDARY_trials_with_count_mismatch', 0)} trial's secondary list differ in length and are left out of the pairwise count.
- **[inferred]** In a record with results the protocol-section outcome list and the results outcome list are one list. The current record therefore cannot show whether the primary outcome changed after registration; that needs the record's version history, which this audit did not fetch. Our dataset's `primary_endpoint` is the first of that list ({f(CP['frame_primary_endpoint_equals_first_registered_primary'])} equal to the first registered primary after normalisation).

What does vary is whether the posted primary measure holds data:

- {f(P['carries_data'])} of PRIMARY measures carry at least one value. The {P['carries_data']['n'] - P['carries_data']['k']} that do not: {'; '.join(f"{r['value']}: {r['k']}" for r in R['primary_measures_without_data']['rows'])}.
- {fc(R['trials_with_a_primary_measure_without_data'])} of trials have at least one PRIMARY measure without data; {fc(R['trials_with_no_primary_measure_carrying_data'])} have no PRIMARY measure with data at all.
- `reportingStatus` across all {P['n_all_measures']:,} measures: {', '.join(f"{r['value']} {r['k']:,} ({r['pct']}%)" for r in P['reportingStatus_all_types']['rows'])}.

### 3.3 PRIMARY posted outcome measures

Denominator for measure-level rows: **{P['n_primary_measures']:,} PRIMARY measures** in {P['n_trials_with_primary']:,} trials. A few trials post very many primary measures (maximum {R['posted_per_trial']['PRIMARY']['summary']['max']}), so measure-level shares over-represent them; trial-level counts (trials with at least one primary measure of that kind, denominator {N:,}; a trial can appear in several rows) are given beside them.

**paramType**

| paramType | measures | % of {P['n_primary_measures']:,} | trials with any | % of {N:,} |
|---|---:|---:|---:|---:|
""" + '\n'.join(f"| {r['value']} | {r['k']:,} | {r['pct']} | {next((t['k'] for t in P['paramType_trials']['rows'] if t['value'] == r['value']), 0):,} | {next((t['pct'] for t in P['paramType_trials']['rows'] if t['value'] == r['value']), 0)} |" for r in P['paramType']['rows']) + f"""

"(absent)" is the measures without a data table (section 3.2).

**unitOfMeasure, top 25** (normalised: lower-case, punctuation to spaces; {P['unitOfMeasure_top25']['distinct']:,} distinct normalised strings, {P['unitOfMeasure_raw_distinct']:,} distinct raw strings)

| unit (normalised) | measures | % of {P['n_primary_measures']:,} | | unit, by trials | trials with any | % of {N:,} |
|---|---:|---:|---|---|---:|---:|
""" + '\n'.join(f"| {esc(a['value'])} | {a['k']:,} | {a['pct']} | | {esc(b['value'])} | {b['k']:,} | {b['pct']} |" for a, b in zip(P['unitOfMeasure_top25']['rows'], P['unitOfMeasure_top25_trials']['rows'])) + f"""

The left and right halves are separate rankings. "milligrams per cubic centimeter" is in the measure-level top 25 but not the trial-level one: it comes from few trials with many measures. Synonyms are not merged ("score on a scale", "units on a scale", "scores on a scale"; "mm hg", "mmhg").

**dispersionType**

| dispersionType | measures | % of {P['n_primary_measures']:,} | trials with any | % of {N:,} |
|---|---:|---:|---:|---:|
""" + '\n'.join(f"| {r['value']} | {r['k']:,} | {r['pct']} | {next((t['k'] for t in P['dispersionType_trials']['rows'] if t['value'] == r['value']), 0):,} | {next((t['pct'] for t in P['dispersionType_trials']['rows'] if t['value'] == r['value']), 0)} |" for r in P['dispersionType']['rows']) + f"""

**Groups (arms), classes and categories**

| | distribution (measures, % of {P['n_primary_measures']:,}) |
|---|---|
| groups per measure | {'; '.join(f"{r['value']}: {r['k']:,} ({r['pct']}%)" for r in sorted(P['n_groups']['rows'], key=lambda r: r['value']))} |
| classes per measure | {'; '.join(f"{r['value']}: {r['k']:,} ({r['pct']}%)" for r in sorted(P['n_classes']['rows'], key=lambda r: ['0', '1', '2', '3-5', '6-10', '11+'].index(r['value'])))} |
| most categories in any class | {'; '.join(f"{r['value']}: {r['k']:,} ({r['pct']}%)" for r in sorted(P['max_categories_per_class']['rows'], key=lambda r: ['0', '1', '2', '3-5', '6+'].index(r['value'])))} |
| table shape | {'; '.join(f"{r['value']}: {r['k']:,} ({r['pct']}%)" for r in P['structure']['rows'])} |

- Any class title: {f(P['any_class_title'])}; any category title: {f(P['any_category_title'])}. Classes, not categories, are where rows of a primary result live (time points, components, subgroups).
- Trial level, largest number of groups in any primary measure (denominator {N:,}): {'; '.join(f"{r['value']}: {r['k']:,} ({r['pct']}%)" for r in sorted(P['trials_max_groups_in_primary']['rows'], key=lambda r: r['value']))}. **{fc(P['trials_with_primary_having_2plus_groups'])} have a primary measure with 2 or more groups.**

**Denominators, time frame, status**

{tfrac([('measure-level `denoms` present', P['measure_level_denoms_present']), ('class-level `denoms` present', P['class_level_denoms_present']), ('any `denoms` present', P['any_denoms_present']), ('a denominator unit other than participants', P['measures_with_non_participant_denom_unit']), ('`timeFrame` present', P['timeFrame_present']), ('`description` present', P['description_present']), ('`populationDescription` present', P['populationDescription_present']), ('`anticipatedPostingDate` present', P['anticipatedPostingDate_present'])], 'PRIMARY measures', str(P['n_primary_measures']))}

- Denominator units ({P['denom_units']['n']:,} measure-level denominator rows): {', '.join(f"{r['value']} {r['k']:,} ({r['pct']}%)" for r in P['denom_units']['rows'][:6])}; {P['denom_units']['distinct']} distinct units in all.
- `reportingStatus` of PRIMARY measures: {', '.join(f"{r['value']} {r['k']:,} ({r['pct']}%)" for r in P['reportingStatus']['rows'])}.

**Arm-level effect computability [inferred definition].** A trial counts when a primary measure has 2 or more groups, data, denominators, and either (binary) `COUNT_OF_PARTICIPANTS`, or `NUMBER`/`COUNT_OF_UNITS` with a participant-count unit that is not a percentage; or (continuous) a mean-type `paramType` with an SD, SE or CI dispersion. This is read from metadata only; values were not validated.

{tfrac([('either kind', P['arm_level_effect_computable']['any']), ('binary counts', P['arm_level_effect_computable']['binary_counts']), ('continuous mean with dispersion', P['arm_level_effect_computable']['continuous_mean_with_dispersion']), ('either kind, among trials with a 2+ group primary', P['arm_level_effect_computable']['any_among_trials_with_2plus_group_primary'])], 'Trials', 'see row')}

### 3.4 Statistical analyses attached to primary outcomes

**Share of trials with at least one analysis on a PRIMARY measure**

{tfrac([('all sampled trials', AN['trials_with_primary_analysis']['of_all_sampled']), ('trials with a PRIMARY measure carrying data', AN['trials_with_primary_analysis']['of_trials_with_posted_PRIMARY_data']), ('trials with a 2+ group primary measure', AN['trials_with_primary_analysis']['of_trials_with_2plus_group_primary']), ('trials whose primary measures are single-group only', AN['trials_with_primary_analysis']['of_trials_with_single_group_primary_only']), ('any analysis on any measure type, all sampled trials', AN['trials_with_any_measure_analysis'])], 'Denominator group', 'group')}

Measure level: {f(AN['primary_measures_with_analysis']['of_all_primary_measures'])} of PRIMARY measures have an analysis ({f(AN['primary_measures_with_analysis']['of_primary_measures_with_2plus_groups'])} of those with 2+ groups); by type, {', '.join(f"{t} {f(AN['measures_with_analysis_by_type'][t])}" for t in ('PRIMARY', 'SECONDARY', 'OTHER_PRE_SPECIFIED', 'POST_HOC'))}. Measures that have any carry a median of {AN['analyses_per_primary_measure_having_any']['median']} (p95 {AN['analyses_per_primary_measure_having_any']['p95']}, max {AN['analyses_per_primary_measure_having_any']['max']}).

By stratum (trials with a primary analysis / trials in stratum; phase and sponsor class are our record's fields):

| stratum | with primary analysis | 2+ group primary |
|---|---:|---:|
""" + '\n'.join(f"| study type {k} | {f(v['primary_analysis'])} | {f(v['two_plus_group_primary'])} |" for k, v in AN['by_study_type'].items())
 + '\n' + '\n'.join(f"| phase {k} | {f(v['primary_analysis'])} | {f(v['two_plus_group_primary'])} |" for k, v in AN['by_phase_frame'].items())
 + '\n' + '\n'.join(f"| sponsor class {k} | {f(v['primary_analysis'])} | {f(v['two_plus_group_primary'])} |" for k, v in AN['by_sponsor_class_frame'].items())
 + '\n' + '\n'.join(f"| results first posted {k} | {f(v['primary_analysis'])} | {f(v['two_plus_group_primary'])} |" for k, v in AN['by_results_first_post_year'].items()) + f"""

Phase "(blank)" is mostly the observational stratum. The share with a primary analysis is lower among recently posted results ({AN['by_results_first_post_year']['2021-2026']['primary_analysis']['pct']}% for 2021-2026 against {AN['by_results_first_post_year']['2008-2012']['primary_analysis']['pct']}% for 2008-2012); this is unadjusted for phase and sponsor mix.

**Field presence** (denominator: {AN['n_primary_analyses']:,} analyses attached to PRIMARY measures)

{tfrac([(k, AN['field_presence_among_primary_analyses'][k]) for k in ('pValue', 'statisticalMethod', 'paramType', 'paramValue', 'ciPctValue', 'ciLowerLimit', 'ciUpperLimit', 'ciNumSides', 'dispersionType', 'nonInferiorityType', 'testedNonInferiority', 'groupDescription', 'pValueComment', 'estimateComment', 'nonInferiorityComment')] + [('estimate (paramType and paramValue)', AN['combo_presence_among_primary_analyses']['estimate(paramType+paramValue)']), ('estimate with both CI limits', AN['combo_presence_among_primary_analyses']['estimate+both_CI_limits']), ('p-value but no estimate', AN['combo_presence_among_primary_analyses']['pValue_only_no_estimate']), ('neither p-value nor estimate', AN['combo_presence_among_primary_analyses']['neither_pValue_nor_estimate'])], 'Field', str(AN['n_primary_analyses']))}

- p-value form: {', '.join(f"{r['value']} {r['k']:,} ({r['pct']}%)" for r in AN['pValue_form']['rows'])}. Of the {PV_N:,} p-values present, {PV_UB['k']:,} ({PV_UB['pct']}%) are upper bounds such as "<0.001" and {PV_OP['k']:,} ({PV_OP['pct']}%) carry some operator rather than being a plain number.
- `nonInferiorityType`: {', '.join(f"{r['value']} {r['k']:,} ({r['pct']}%)" for r in AN['nonInferiorityType']['rows'])}.
- `ciPctValue`: {', '.join(f"{r['value']} {r['k']:,} ({r['pct']}%)" for r in AN['ciPctValue']['rows'][:4])}; `ciNumSides`: {', '.join(f"{r['value']} {r['k']:,} ({r['pct']}%)" for r in AN['ciNumSides']['rows'])}.
- Groups compared per analysis (`groupIds`): {', '.join(f"{r['value']} group{'s' if r['value'] != '1' else ''}: {r['k']:,} ({r['pct']}%)" for r in sorted(AN['n_groups_compared']['rows'], key=lambda r: r['value']))}. One-group analyses are within-group tests or estimates, not between-arm comparisons.

Trial level, among the {AN['trials_with_primary_analysis']['of_all_sampled']['k']} trials with a primary analysis:

{tfrac([('a p-value', AN['trial_level_among_trials_with_primary_analysis']['has_pValue']), ('a statistical method', AN['trial_level_among_trials_with_primary_analysis']['has_statisticalMethod']), ('an estimate', AN['trial_level_among_trials_with_primary_analysis']['has_estimate']), ('an estimate with both CI limits', AN['trial_level_among_trials_with_primary_analysis']['has_estimate_with_both_CI_limits']), ('type exactly SUPERIORITY', AN['trial_level_among_trials_with_primary_analysis']['has_type_SUPERIORITY_exactly']), ('a non-inferiority or equivalence type', AN['trial_level_among_trials_with_primary_analysis']['has_non_inferiority_or_equivalence_type'])], 'At least one primary analysis with', str(AN['trials_with_primary_analysis']['of_all_sampled']['k']))}

**Top 20 estimate types** (`analyses[].paramType`, raw strings, denominator {AN['n_primary_analyses']:,}; {AN['estimate_paramType_top20']['distinct'] - 1} distinct non-blank strings, {AN['estimate_paramType_top20_casefolded']['distinct'] - 1} after case and punctuation folding)

{tdist({'n': AN['n_primary_analyses'], 'rows': [r for r in AN['estimate_paramType_top20']['rows'] if r['value'] != '(absent)'][:20]}, 'estimate paramType')}

**[inferred]** The strings look like a pick-list plus free text: one concept appears under many strings ("LS Mean Difference", "LSM difference", "Least Squares Mean Difference"; "Geometric Mean Ratio", "GMT Ratio", "GLS Mean Ratio", "Ratio of adjusted geometric means").

**Top statistical methods** (same denominator): {', '.join(f"{r['value']} {r['k']} ({r['pct']}%)" for r in AN['statisticalMethod_top15']['rows'] if r['value'] != '(absent)')}; {AN['statisticalMethod_top15']['distinct'] - 1} distinct strings.

### 3.5 Demographic subgroup reporting inside outcome measures [heuristic]

Rule, applied to every one of the {SG['measures']['n_all_measures']:,} posted measures (full definition in `scripts/analyze.py`, section 3e):

- **Levels (Tier 1):** the measure's class titles and category titles themselves show two or more levels of a dimension: a female-term title and a different male-term title; two or more race levels (OMB categories, or X / non-X); a Hispanic/Latino title among two or more level titles; two or more age-level titles (explicit age wording, or year/month bands when the measure title says "age", or different life stages such as children / adults). A measure titled "subgroup…" needs only one such level. Donor attributes, "white blood cell", "Child-Pugh", instrument names and bare time points are excluded.
- **Title cue only (Tier 2):** the measure title says "by sex", "by race", "age group", "subgroup analysis: sex" and the like, without Tier 1 levels.
- **Arm titles (supplementary):** the same level rule applied to the measure's group (arm) titles. The request named titles, classes and categories; arms are reported separately because age- or sex-defined cohorts are the commonest way these dimensions appear.
- **Mention only:** any sex, race, ethnicity or age-group word anywhere in a title or level title. Not subgroup reporting; shown as a loose ceiling.

Trials with at least one such measure (denominator {N:,} in every cell):

| | sex | race | ethnicity | age | any dimension | race or ethnicity |
|---|---:|---:|---:|---:|---:|---:|
| **Any measure:** levels (Tier 1) | {f(anyM['tier1_levels']['sex'])} | {f(anyM['tier1_levels']['race'])} | {f(anyM['tier1_levels']['ethnicity'])} | {f(anyM['tier1_levels']['age'])} | {fc(anyM['tier1_levels']['any_dimension'])} | {fc(anyM['tier1_levels']['race_or_ethnicity'])} |
| Any measure: levels or title cue | {f(anyM['tier1_or_tier2']['sex'])} | {f(anyM['tier1_or_tier2']['race'])} | {f(anyM['tier1_or_tier2']['ethnicity'])} | {f(anyM['tier1_or_tier2']['age'])} | {fc(anyM['tier1_or_tier2']['any_dimension'])} | |
| Any measure: arm titles (supplementary) | {f(anyM['supplementary_arm_titles_tier1']['sex'])} | {f(anyM['supplementary_arm_titles_tier1']['race'])} | {f(anyM['supplementary_arm_titles_tier1']['ethnicity'])} | {f(anyM['supplementary_arm_titles_tier1']['age'])} | {f(anyM['supplementary_arm_titles_tier1']['any_dimension'])} | |
| Any measure: levels, title cue or arm titles | | | | | {fc(SG['union_tier1_tier2_arm']['any_measure'])} | {fc(SG['union_tier1_tier2_arm']['any_measure_race_or_ethnicity'])} |
| Any measure: mention only (ceiling) | {f(anyM['mention_only_upper_bound']['sex'])} | {f(anyM['mention_only_upper_bound']['race'])} | {f(anyM['mention_only_upper_bound']['ethnicity'])} | {f(anyM['mention_only_upper_bound']['age'])} | {f(anyM['mention_only_upper_bound']['any_dimension'])} | |
| **PRIMARY measures:** levels (Tier 1) | {f(priM['tier1_levels']['sex'])} | {f(priM['tier1_levels']['race'])} | {f(priM['tier1_levels']['ethnicity'])} | {f(priM['tier1_levels']['age'])} | {fc(priM['tier1_levels']['any_dimension'])} | {fc(priM['tier1_levels']['race_or_ethnicity'])} |
| PRIMARY: levels or title cue | {f(priM['tier1_or_tier2']['sex'])} | {f(priM['tier1_or_tier2']['race'])} | {f(priM['tier1_or_tier2']['ethnicity'])} | {f(priM['tier1_or_tier2']['age'])} | {fc(priM['tier1_or_tier2']['any_dimension'])} | |
| PRIMARY: arm titles (supplementary) | {f(priM['supplementary_arm_titles_tier1']['sex'])} | {f(priM['supplementary_arm_titles_tier1']['race'])} | {f(priM['supplementary_arm_titles_tier1']['ethnicity'])} | {f(priM['supplementary_arm_titles_tier1']['age'])} | {f(priM['supplementary_arm_titles_tier1']['any_dimension'])} | |
| PRIMARY: levels, title cue or arm titles | | | | | {fc(SG['union_tier1_tier2_arm']['primary_measure'])} | |
| PRIMARY: mention only (ceiling) | {f(priM['mention_only_upper_bound']['sex'])} | {f(priM['mention_only_upper_bound']['race'])} | {f(priM['mention_only_upper_bound']['ethnicity'])} | {f(priM['mention_only_upper_bound']['age'])} | {f(priM['mention_only_upper_bound']['any_dimension'])} | |

Measure level: {SG['measures']['tier1_measures']} of {SG['measures']['n_all_measures']:,} measures meet Tier 1 ({SG['measures']['tier1_primary_measures']} of them PRIMARY; by dimension, a measure can count in several: {', '.join(f'{k} {v}' for k, v in sorted(SG['measures']['tier1_by_dimension'].items()))}); {SG['measures']['tier2_only_measures']} more have a title cue only. By study type, Tier 1 trials: {', '.join(f'{k} {f(v)}' for k, v in SG['tier1_trials_by_study_type'].items())}.

**Subgroup denominators** (denominator: the {SG['tier1_measure_denominators']['n_tier1_measures']} Tier 1 measures):

{tfrac([('demographic levels are classes (rather than categories)', SG['tier1_measure_denominators']['levels_in_class']), ('every demographic-level class has its own `denoms`', SG['tier1_measure_denominators']['all_levels_have_class_denoms']), ('those class denominators differ between levels', SG['tier1_measure_denominators']['class_denoms_differ_between_levels']), ('levels are categories (the schema has no per-category denominator)', SG['tier1_measure_denominators']['levels_in_category']), ('measure-level `denoms` present', SG['tier1_measure_denominators']['measure_level_denoms_present'])], 'Tier 1 measures', str(SG['tier1_measure_denominators']['n_tier1_measures']))}

So per-subgroup denominators are given in {SG['tier1_measure_denominators']['all_levels_have_class_denoms']['pct']}% of subgroup measures; in the rest the subgroup n is either absent or typed into the class title as text ("Male Participants (n=70, 81)"). {f(SG['tier1_trials_with_analysis_on_a_subgroup_measure'])} of Tier 1 trials attach a statistical analysis to a flagged measure.

**Every flagged trial** (Tier 1 or title cue; {len(flag)} trials, one representative measure each, PRIMARY preferred; the full list of {SG['measures']['tier1_measures'] + SG['measures']['tier2_only_measures']} flagged measures is in `subgroup_hits.jsonl`). "Reading" is my judgement of whether the measure is an observed outcome reported by participant subgroup: yes / partly / no.

| # | NCT ID | type | measure title | class / category titles (first 6) | dimensions | per-class denoms | reading |
|---:|---|---|---|---|---|---|---|
""" + '\n'.join(
    f"| {n} | {h['nct_id']} | {h['type']} | {esc(cut(h['title'], 120))} | {esc('; '.join(cut(x, 38) for x in ([y for d in h['matched_levels'].values() for y in d] or h['class_titles'] + h['category_titles'])[:6]) or '(none)')} | {', '.join(h['dims_tier1_trial']) or '-'}{' (title cue: ' + ', '.join(h['dims_tier2_trial']) + ')' if h['dims_tier2_trial'] else ''} | {('yes, differing' if h['class_denoms']['denoms_differ_between_levels'] else 'yes, identical' if h['class_denoms']['all_levels_have_class_denoms'] else 'no (levels are categories)' if h['class_denoms']['levels_in'] == 'category' else 'no') if h['class_denoms'] else 'n/a'} | **{REVIEW[h['nct_id']][0]}**: {esc(REVIEW[h['nct_id']][1])} |"
    for n, h in enumerate(flag, 1)) + f"""

My reading of the {len(flag)}: {rv['yes']} yes, {rv['partly']} partly, {rv['no']} no. Counting only "yes", {rv['yes']}/{N:,} ({round(100 * rv['yes'] / N, 1)}%) of sampled trials post an observed outcome by a demographic subgroup in any measure; {len(yes_race_eth)}/{N:,} ({round(100 * len(yes_race_eth) / N, 2)}%) do so by race or ethnicity ({', '.join(yes_race_eth)}); {len(yes_primary)}/{N:,} have a flagged PRIMARY measure ({', '.join(yes_primary)}), none of them by race or ethnicity. This reading is one reviewer's (the auditing model's) and is not a second measurement.

**Trials flagged only through arm titles** ({len(SG['arm_level_only_trials'])} trials; the arms themselves are sex-, age- or race-defined cohorts)

| # | NCT ID | type | measure title | group (arm) titles (first 4) | dimension | measures with such arms |
|---:|---|---|---|---|---|---:|
""" + '\n'.join(f"| {len(flag) + n} | {h['nct_id']} | {h['type']} | {esc(cut(h['title'], 100))} | {esc('; '.join(cut(x, 42) for x in h['group_titles'][:4]))} | {', '.join(h['dims_trial'])} | {h['n_measures_with_arm_strata']} |" for n, h in enumerate(SG['arm_level_only_trials'], 1)) + f"""

One of these (NCT03098836) has race-defined cohorts ("Caucasian", "African American"); it is the only sampled trial the rule found whose arms are race groups, and so the only one reporting its primary outcome by race.

**What the rule leaves out: 15 "mention only" measures** (seeded draw, one per trial, from {SG['mention_only_measures_unflagged']:,} unflagged measures that mention a demographic word)

| NCT ID | type | mention | measure title | first level titles |
|---|---|---|---|---|
""" + '\n'.join(f"| {m['nct_id']} | {m['type']} | {', '.join(m['mention_dims'])} | {esc(cut(m['title'], 110))} | {esc('; '.join(cut(x, 30) for x in m['level_titles'][:3]) or '(none)')} |" for m in SG['mention_only_examples_15']) + f"""

These are mostly instrument names and single-sex or single-age study populations, with some acronym and laboratory-term collisions ("MALE", "white blood cell"). Subgroup results can also sit where this audit did not look: outcome descriptions, analysis `groupDescription` text, and posted protocol/SAP/CSR documents.

### 3.6 Adverse events (denominator {N:,} unless stated)

{tfrac([('`adverseEventsModule` present', AE['module_present']), ('`seriousEvents` rows present', AE['seriousEvents_nonempty']), ('`otherEvents` rows present', AE['otherEvents_nonempty']), ('both', AE['both']), ('either', AE['either']), ('module present, no event rows of either kind', AE['module_present_but_no_event_rows']), ('every group has `seriousNumAtRisk`', AE['all_groups_have_seriousNumAtRisk']), ('every group has `seriousNumAffected`', AE['all_groups_have_seriousNumAffected']), ('every group has `otherNumAtRisk`', AE['all_groups_have_otherNumAtRisk']), ('every group has `otherNumAffected`', AE['all_groups_have_otherNumAffected']), ('every group has `deathsNumAtRisk` and `deathsNumAffected` (all-cause mortality)', AE['all_groups_have_deathsNumAtRisk']), ('`frequencyThreshold` present', AE['frequencyThreshold_present']), ('`timeFrame` present', AE['timeFrame_present']), ('`description` present', AE['description_present'])], 'Item', str(N))}

- Absence of event rows is a reported zero, not a gap: of the {AE['no_serious_rows_and_all_groups_report_0_serious_affected']['n']} trials with the module but no serious-event rows, {f(AE['no_serious_rows_and_all_groups_report_0_serious_affected'])} report 0 participants affected in every group; for other events, {f(AE['no_other_rows_and_all_groups_report_0_other_affected'])}.
- All-cause mortality table by year results were first posted: {', '.join(f'{k}: {f(v)}' for k, v in AE['deaths_table_by_results_year'].items())}.
- `frequencyThreshold` (percent, for other events): {', '.join(f"{r['value']}: {r['k']:,} ({r['pct']}%)" for r in AE['frequencyThreshold']['rows'][:6])}; {AE['frequencyThreshold']['distinct']} distinct values. A threshold of 5 means non-serious events under 5% in every arm are not listed, so other-event counts are not comparable between a threshold-0 and a threshold-5 trial.
- Event groups per trial: {'; '.join(f"{r['value']}: {r['k']:,} ({r['pct']}%)" for r in sorted(AE['eventGroups_per_trial']['rows'], key=lambda r: r['value']))}.
- Per event-term row: `numAtRisk` in {f(AE['serious_event_rows']['numAtRisk'])} serious rows and {f(AE['other_event_rows']['numAtRisk'])} other rows; `numAffected` {AE['serious_event_rows']['numAffected']['pct']}% and {AE['other_event_rows']['numAffected']['pct']}%; `numEvents` {AE['serious_event_rows']['numEvents']['pct']}% and {AE['other_event_rows']['numEvents']['pct']}%.
- Vocabulary per event term ({AE['sourceVocabulary_event_rows']['n']:,} terms): {', '.join(f"{r['value']} {r['pct']}%" for r in AE['sourceVocabulary_event_rows']['rows'])}. Assessment: {', '.join(f"{r['value']} {r['pct']}%" for r in AE['assessmentType_event_rows']['rows'])}.
- Event terms per trial: serious median {AE['event_terms_per_trial']['serious']['median']}, p95 {AE['event_terms_per_trial']['serious']['p95']}, max {AE['event_terms_per_trial']['serious']['max']}; other median {AE['event_terms_per_trial']['other']['median']}, p95 {AE['event_terms_per_trial']['other']['p95']}, max {AE['event_terms_per_trial']['other']['max']}.
- Adverse events are reported by arm only. No adverse-event field carries a demographic breakdown.

### 3.7 Participant flow (denominator {N:,} unless stated)

{tfrac([('module present', PF['module_present']), ('first period has STARTED', PF['first_period_STARTED']), ('first period has COMPLETED', PF['first_period_COMPLETED']), ('every period has STARTED and COMPLETED', PF['every_period_has_STARTED_and_COMPLETED']), ('STARTED total parsable as integers', PF['first_period_STARTED_total_parsable']), ('any milestone beyond STARTED / COMPLETED / NOT COMPLETED', PF['any_additional_milestone']), ('any NOT COMPLETED count above 0', PF['trials_with_any_NOT_COMPLETED_count_above_0']), ('any drop-withdraw reasons', PF['any_dropWithdraws']), ('drop-withdraw reasons, among trials with non-completers', PF['dropWithdraws_present_among_trials_with_noncompleters']), ('`recruitmentDetails` text', PF['recruitmentDetails_present']), ('`preAssignmentDetails` text', PF['preAssignmentDetails_present']), ('unit other than participants (`typeUnitsAnalyzed`)', PF['typeUnitsAnalyzed_present'])], 'Item', 'see row')}

- Periods per trial: {'; '.join(f"{r['value']}: {r['k']:,} ({r['pct']}%)" for r in sorted(PF['periods_per_trial']['rows'], key=lambda r: r['value']))}. Groups per trial: {'; '.join(f"{r['value']}: {r['k']:,} ({r['pct']}%)" for r in sorted(PF['groups_per_trial']['rows'], key=lambda r: r['value']))}.
- First-period COMPLETED / STARTED (n = {PF['first_period_completed_over_started']['n']:,} with STARTED above 0): median {PF['first_period_completed_over_started']['median']}, p25 {PF['first_period_completed_over_started']['p25']}, p75 {PF['first_period_completed_over_started']['p75']}, p05 {PF['first_period_completed_over_started']['p05']}. {f(PF['first_period_zero_completers'])} report zero completers in the first period (overall status: {', '.join(f'{k} {v}' for k, v in sorted(PF['first_period_zero_completers_by_status'].items(), key=lambda kv: -kv[1]))}), so COMPLETED / STARTED mixes attrition with early termination and with studies whose first period nobody was meant to 'complete'.
- Drop-withdraw reason types, by trials naming them (denominator {N:,}; case-folded): {', '.join(f"{r['value']} {r['k']} ({r['pct']}%)" for r in PF['dropWithdraw_reason_trials_top10']['rows'])}. {PF['dropWithdraw_reason_types_top15_casefolded']['distinct']:,} distinct reason strings over {PF['dropWithdraw_reason_types_top15_casefolded']['n']:,} reason rows: a standard list plus free text.
- Flow is by arm and period only; there is no demographic breakdown of who withdrew.

### 3.8 Baseline

**Baseline total.** Taken from the baseline `denoms` row in participants: the group whose title contains "total" where there is one, else the only group, else a last group equal to the sum of the others (a total under another name), else the sum of groups. This is the project's documented selection rule for AACT baselines. Matching only the exact title "Total" and summing otherwise double-counts records whose total group is titled "Total Title" or "Pooled …" (4 trials in this sample, found on a first pass). Method used: {', '.join(f"{r['value']} {r['k']:,} ({r['pct']}%)" for r in BL['baseline_total_method']['rows'])}; parsable for {f(BL['baseline_total_parsable'])}. In {BL['total_group_position']['rows'][0]['k']:,} of {BL['total_group_position']['n']:,} records with a Total-titled group it is the last, highest-numbered `BG` group, which agrees with the schema fact the project already documents for AACT.

**Baseline total versus participant-flow STARTED** (sum over groups of the first period's STARTED; denominator {BL['baseline_vs_flow_STARTED']['n']:,}): {', '.join(f"{r['value']} {r['k']:,} ({r['pct']}%)" for r in BL['baseline_vs_flow_STARTED']['rows'])}. Where they differ (n = {BL['baseline_over_started_when_different']['n']}), baseline / STARTED has median {BL['baseline_over_started_when_different']['median']}, p25 {BL['baseline_over_started_when_different']['p25']}, p75 {BL['baseline_over_started_when_different']['p75']}, p05 {BL['baseline_over_started_when_different']['p05']}, p95 {BL['baseline_over_started_when_different']['p95']}.

**Baseline total versus actual enrollment.** `enrollmentInfo.type` is ACTUAL for {next(r['k'] for r in BL['enrollment_type']['rows'] if r['value'] == 'ACTUAL'):,}/{N:,}. Distribution of baseline_total / enrollment_actual (denominator {BL['baseline_total_over_enrollment_actual']['n']:,}):

| baseline_total / enrollment_actual | trials | % |
|---|---:|---:|
""" + '\n'.join(f"| {esc(r['value'])} | {r['k']:,} | {r['pct']} |" for r in BL['baseline_total_over_enrollment_actual']['rows']) + f"""

Quantiles: p05 {BL['baseline_total_over_enrollment_actual']['summary']['p05']}, p25 {BL['baseline_total_over_enrollment_actual']['summary']['p25']}, median {BL['baseline_total_over_enrollment_actual']['summary']['median']}, p75 {BL['baseline_total_over_enrollment_actual']['summary']['p75']}, p95 {BL['baseline_total_over_enrollment_actual']['summary']['p95']}, max {BL['baseline_total_over_enrollment_actual']['summary']['max']}. The {len(BL['ratio_above_1_5'])} trials above 1.5 are listed below; they are registry-side oddities (a Total larger than enrollment, external or historical comparison groups counted in baseline, enrollment counted in clusters), not parsing failures. Enrollment versus STARTED: {', '.join(f"{r['value']} {r['k']:,} ({r['pct']}%)" for r in BL['enrollment_actual_vs_flow_STARTED']['rows'])} (denominator {BL['enrollment_actual_vs_flow_STARTED']['n']:,}).

| NCT ID | baseline total | enrollment (ACTUAL) | flow STARTED | flow unit | baseline groups |
|---|---:|---:|---:|---|---|
{chr(10).join(f"| {r['nct_id']} | {r['baseline_total']:,} | {r['enrollment_actual']:,} | {r['flow_started']:,} | {r['flow_unit'] or 'participants'} | {esc('; '.join(r['baseline_groups']))} |" for r in BL['ratio_above_1_5'])}

So the demographic composition in our records describes the enrolled population exactly in {next(r['pct'] for r in BL['baseline_total_over_enrollment_actual']['rows'] if r['value'] == '=1.00')}% of trials (baseline total equal to actual enrollment) and, in most of the rest, a subset of those who started (baseline / STARTED median {BL['baseline_over_started_when_different']['median']} where the two differ).

**Baseline `populationDescription`:** present for {fc(BL['populationDescription_present'])}; {BL['populationDescription_distinct']} distinct texts. It is present for {f(BL['pd_present_by_baseline_vs_started']['different'])} of trials whose baseline total differs from STARTED and {f(BL['pd_present_by_baseline_vs_started']['equal'])} of those where it is equal. A measure-level population description exists on at least one baseline measure in {f(BL['measure_level_populationDescription_present_any'])}.

Populations named **[heuristic keyword tags; a text can carry several; denominator {BL['populationDescription_tags']['n']} descriptions]**:

| tag | descriptions | % |
|---|---:|---:|
""" + '\n'.join(f"| {esc(r['value'])} | {r['k']} | {r['pct']} |" for r in BL['populationDescription_tags']['rows']) + f"""

30 distinct descriptions (seeded draw from the {BL['populationDescription_distinct']} distinct texts; truncated at 300 characters):

""" + '\n'.join(f"{n}. {h['nct_id']}: {esc(h['text'])}" for n, h in enumerate(BL['populationDescription_sample30'], 1)) + f"""

**Baseline demographic measures in the API record** (by measure title, denominator {N:,}): {', '.join(f'{k} {f(v)}' for k, v in BL['api_baseline_measure_titles'].items())}. Our record's `race.reported` is true for {f(CO['frame_counts']['race_reported'])}; no trial has it true without a race-titled baseline measure in the API ({BL['frame_race_reported_vs_api_race_title']['frame_only']} of {N:,}). The {BL['frame_race_reported_vs_api_race_title']['api_only']} trials with a race-titled measure but `race.reported` false are mostly the dedicated non-collection measure: {', '.join(f"{esc(r['value'])} {r['k']}" for r in BL['api_only_race_measure_titles']['rows'])}. "Race and Ethnicity Not Collected" is filed by {f(BL['api_measure_Race_and_Ethnicity_Not_Collected'])}.

### 3.9 Co-occurrence: race reported x primary measure with a statistical analysis

Rows: `race.reported` in our record. Columns: at least one posted PRIMARY outcome measure with at least one entry in `analyses`. Denominator {CO['race_reported_x_primary_analysis']['n']:,}.

| | primary analysis: yes | primary analysis: no | row total | % yes in row |
|---|---:|---:|---:|---:|
| race reported: yes | {CO['race_reported_x_primary_analysis']['a1_b1']:,} | {CO['race_reported_x_primary_analysis']['a1_b0']:,} | {CO['race_reported_x_primary_analysis']['a1_b1'] + CO['race_reported_x_primary_analysis']['a1_b0']:,} | {CO['race_reported_x_primary_analysis']['pct_b_given_a1']['pct']} (CI {CO['race_reported_x_primary_analysis']['pct_b_given_a1']['ci95'][0]}-{CO['race_reported_x_primary_analysis']['pct_b_given_a1']['ci95'][1]}) |
| race reported: no | {CO['race_reported_x_primary_analysis']['a0_b1']:,} | {CO['race_reported_x_primary_analysis']['a0_b0']:,} | {CO['race_reported_x_primary_analysis']['a0_b1'] + CO['race_reported_x_primary_analysis']['a0_b0']:,} | {CO['race_reported_x_primary_analysis']['pct_b_given_a0']['pct']} (CI {CO['race_reported_x_primary_analysis']['pct_b_given_a0']['ci95'][0]}-{CO['race_reported_x_primary_analysis']['pct_b_given_a0']['ci95'][1]}) |
| column total | {CO['race_reported_x_primary_analysis']['a1_b1'] + CO['race_reported_x_primary_analysis']['a0_b1']:,} | {CO['race_reported_x_primary_analysis']['a1_b0'] + CO['race_reported_x_primary_analysis']['a0_b0']:,} | {CO['race_reported_x_primary_analysis']['n']:,} | |

Both conditions hold for **{fc(CO['race_reported_x_primary_analysis']['pct_both_of_all'])}** of sampled trials (crude odds ratio {CO['race_reported_x_primary_analysis']['odds_ratio']}). Scaled to the {SM['population_n']:,} records that is roughly {round(SM['population_n'] * CO['race_reported_x_primary_analysis']['pct_both_of_all']['ci95'][0] / 100, -2):,.0f} to {round(SM['population_n'] * CO['race_reported_x_primary_analysis']['pct_both_of_all']['ci95'][1] / 100, -2):,.0f} trials **[inferred: sample proportion and its interval applied to the frame size]**.

Variants (same construction; "both" is the count meeting both conditions):

| second condition / restriction | both / denominator | % | % among race-reported | % among race-not-reported |
|---|---:|---:|---:|---:|
""" + '\n'.join(f"| {lab} | {CO[k]['a1_b1']:,} / {CO[k]['n']:,} | {CO[k]['pct_both_of_all']['pct']} | {CO[k]['pct_b_given_a1']['pct']} | {CO[k]['pct_b_given_a0']['pct']} |" for lab, k in [
    ('primary analysis; interventional only', 'race_reported_x_primary_analysis__interventional_only'),
    ('primary analysis; trials with a 2+ group primary only', 'race_reported_x_primary_analysis__2plus_group_primary_only'),
    ('primary analysis with estimate and both CI limits', 'race_reported_x_primary_analysis_with_estimate_and_CI'),
    ('PRIMARY measure carrying data', 'race_reported_x_posted_primary_with_data'),
    ('arm-level effect computable [inferred definition, 3.3]', 'race_reported_x_arm_level_effect_computable')]) + f"""
| ethnicity reported x primary analysis | {CO['ethnicity_reported_x_primary_analysis']['a1_b1']:,} / {CO['ethnicity_reported_x_primary_analysis']['n']:,} | {CO['ethnicity_reported_x_primary_analysis']['pct_both_of_all']['pct']} | {CO['ethnicity_reported_x_primary_analysis']['pct_b_given_a1']['pct']} (among ethnicity-reported) | {CO['ethnicity_reported_x_primary_analysis']['pct_b_given_a0']['pct']} |
| race and ethnicity both reported x primary analysis | {CO['race_and_ethnicity_reported_x_primary_analysis']['a1_b1']:,} / {CO['race_and_ethnicity_reported_x_primary_analysis']['n']:,} | {CO['race_and_ethnicity_reported_x_primary_analysis']['pct_both_of_all']['pct']} | {CO['race_and_ethnicity_reported_x_primary_analysis']['pct_b_given_a1']['pct']} (among both-reported) | {CO['race_and_ethnicity_reported_x_primary_analysis']['pct_b_given_a0']['pct']} |

Our record: race reported {f(CO['frame_counts']['race_reported'])}, ethnicity reported {f(CO['frame_counts']['ethnicity_reported'])}, sex reported {f(CO['frame_counts']['sex_reported'])}. The association between race reporting and having a primary analysis is weak and unadjusted for phase, sponsor class and posting year, all of which move analysis presence (section 3.4); it should not be read as a relationship.
""")

w(f"""## 4. AACT mapping

**Everything in this section is [documentation knowledge]** (the AACT schema and data dictionary as I recall them) except where marked "measured" or "read in the repo". AACT was not downloaded and no snapshot is in either repository, so no column name here was checked against data. Verify each against the AACT data dictionary for the snapshot actually used before writing code.

**What the repositories actually read (read in the repo).**

- `clinical-trial-populations/data/geo/geo_rep_2026-06-19_run063/` contains one R script, `geo_reader.R`. It reads the run's own shipped CSVs (`geo_representation_long_2026-06-19.csv`, `d2_display_rule_vocabulary.csv`, `b1_view_definitions.csv`), not AACT. `data/geo/README.md` says each run directory "ships its complete pipeline" (`refresh.R`, `run_all.R`, `build_part*.R`), but those scripts are not in the checked-out run directory, so which AACT tables the pipeline reads could not be confirmed from code.
- The only AACT flat files named anywhere in that directory are `countries.txt` and `facilities.txt` (in `metric_dictionary.csv`); the README adds that both derive from `protocolSection.contactsLocationsModule.locations` plus `derivedSection.miscInfoModule.removedCountries`. The pinned snapshot date is 2026-06-19 (`active_run.json`).
- The engine's sponsor layer (`civicsample-engine/sponsors/`) names `sponsors.txt`. No code in either repository reads an AACT outcome, analysis, milestone or adverse-event table.
- The session's AACT baseline skill records facts verified on the 2026-06-19 snapshot (not re-verified here): the baseline Total is the highest-numbered `BG` group; `baseline_measurements.number_analyzed` is a denominator id, not a count; `studies.txt` has malformed rows that shift columns.

**Module to table map.**

| API v2 path | AACT table(s) | Notes |
|---|---|---|
| `protocolSection.outcomesModule` (primary / secondary / other) | `design_outcomes` (`outcome_type`, `measure`, `time_frame`, `description`) | In a record with results this list is identical to the posted list (measured, section 3.2), so `design_outcomes` and `outcomes` will carry the same titles. |
| `resultsSection.outcomeMeasuresModule.outcomeMeasures[]` | `outcomes` (`outcome_type`, `title`, `description`, `time_frame`, `population`, `units`, `param_type`, `dispersion_type`, `units_analyzed`, anticipated posting date) | One row per measure. |
| `…outcomeMeasures[].groups[]` | `result_groups` with `result_type` = Outcome (`ctgov_group_code` OG000…, `title`, `description`) | `result_groups` also holds Baseline (BG), Participant Flow (FG) and Reported Event (EG) groups; codes are not shared across types. |
| `…classes[].categories[].measurements[]` | `outcome_measurements` (`classification` = class title, `category` = category title, `param_value`, `param_value_num`, `dispersion_value`, `dispersion_lower_limit`, `dispersion_upper_limit`, `explanation_of_na`, `ctgov_group_code`) | The nested class / category hierarchy is flattened to two text columns. |
| `…outcomeMeasures[].denoms[]` and `…classes[].denoms[]` | `outcome_counts` (`scope`, `units`, `count`, `ctgov_group_code`) | Whether class-level denominators are kept, and how `scope` distinguishes them, I could not confirm; class-level denominators matter for subgroup rows (measured: present in {P['class_level_denoms_present']['pct']}% of primary measures and {SG['tier1_measure_denominators']['all_levels_have_class_denoms']['pct']}% of flagged subgroup measures). |
| `…outcomeMeasures[].analyses[]` | `outcome_analyses` (`non_inferiority_type`, `param_type`, `param_value`, `dispersion_type`, `dispersion_value`, `p_value_modifier`, `p_value`, `ci_n_sides`, `ci_percent`, `ci_lower_limit`, `ci_upper_limit`, `method`, description / comment columns) | AACT splits the p-value into a modifier and a number; the API gives one string (measured: {PV_UB['k']:,} of {PV_N:,} posted primary p-values, {PV_UB['pct']}%, are written as an upper bound such as "<0.001"). |
| `…analyses[].groupIds[]` | `outcome_analysis_groups` (`outcome_analysis_id`, `result_group_id`, `ctgov_group_code`) | Needed to tell between-arm from one-group analyses (measured: {next(r['pct'] for r in AN['n_groups_compared']['rows'] if r['value'] == '1')}% of primary analyses name one group). |
| `resultsSection.baselineCharacteristicsModule.measures[]…` | `baseline_measurements` (`title`, `classification`, `category`, `units`, `param_type`, `param_value`, dispersion columns, `number_analyzed`, `ctgov_group_code`) | `number_analyzed` is not a count (skill). |
| `…baselineCharacteristicsModule.denoms[]` | `baseline_counts` (`units`, `scope`, `count`, `ctgov_group_code`) | The overall baseline N per group. |
| `…baselineCharacteristicsModule.groups[]` | `result_groups`, `result_type` = Baseline | Total = highest-numbered BG code (skill; measured in the API: {BL['total_group_position']['rows'][0]['k']:,} of {BL['total_group_position']['n']:,} Total-titled groups are last). |
| `…baselineCharacteristicsModule.populationDescription` | a study-level text column (I recall `studies.baseline_population`) | Column name uncertain. |
| `resultsSection.participantFlowModule.periods[].milestones[]` | `milestones` (`period`, `title`, `count`, `ctgov_group_code`, `description`) | STARTED / COMPLETED / NOT COMPLETED are rows by `title`. |
| `…periods[].dropWithdraws[]` | `drop_withdrawals` (`period`, `reason`, `count`, `ctgov_group_code`) | |
| `…participantFlowModule.recruitmentDetails`, `preAssignmentDetails`, `groups[]` | `participant_flows` (text columns); `result_groups`, `result_type` = Participant Flow | |
| `resultsSection.adverseEventsModule.seriousEvents[]` / `otherEvents[]` | `reported_events` (`event_type` serious / other, `organ_system`, `adverse_event_term`, `subjects_affected`, `subjects_at_risk`, `event_count`, `frequency_threshold`, `time_frame`, `vocab`, `assessment`, `ctgov_group_code`) | One row per term per group; large. |
| `…adverseEventsModule.eventGroups[]` totals (serious / other / deaths affected and at risk) | `reported_event_totals` (`event_type`, `classification`, `subjects_affected`, `subjects_at_risk`); `result_groups`, `result_type` = Reported Event | Per-arm totals without summing term rows. |
| `resultsSection.moreInfoModule` | `result_agreements`, `result_contacts`; limitations text in a study-level column | Column for limitations uncertain. |
| `protocolSection.statusModule`, `designModule` | `studies` (dates, `overall_status`, `enrollment`, `enrollment_type`, `phase`, `study_type`), `designs` | `studies.txt` malformed rows (skill). |
| `documentSection.largeDocumentModule` | `provided_documents` (`has_protocol`, `has_sap`, `url`) | What the Industry Outcomes beta reads from the API today. |
| derived conveniences | `calculated_values` (counts of outcomes to measure, `were_results_reported`, `number_of_sae_subjects`, …) | AACT-computed; no API field. |

**What the API has that AACT lacks or blurs.**

- The nested shape itself: which class a category sits in, and which denominator belongs to which class, without reconstruction from text columns.
- `reportingStatus` per measure (measured: {next(r['pct'] for r in P['reportingStatus_all_types']['rows'] if r['value'] == 'NOT_POSTED')}% of all measures are NOT_POSTED shells), `denomUnitsSelected`, `calculatePct`, `typeUnitsAnalyzed`. I could not confirm which of these AACT carries.
- Freshness to the day and a `dataTimestamp`; a targeted fetch of {N:,} studies took {N // 50} requests.
- A stated correspondence to the registry's own field paths; AACT's dictionary leaves many path cells blank (skill).

**What AACT has that the API lacks.**

- The whole registry as flat files for a pinned snapshot date, which is how this project already pins the geography layer; bulk joins across about 80,000 results records without paging.
- Numeric casts beside the raw strings (`param_value_num`, `dispersion_value_num`), the split p-value, and `calculated_values`.
- One `result_groups` table spanning all four result types.

**What neither has.**

- Record history (the originally registered outcomes), which section 3.2 shows is the only place outcome changes could be seen.
- Any link between baseline demographic categories and outcome rows. The only axis that cross-classifies a result is the arm (`ctgov_group_code` / `groupId`); subgroup results exist only where a registrant typed subgroup names into class, category or arm titles (measured: {anyM['tier1_levels']['any_dimension']['pct']}% of trials by class or category titles, {SG['union_tier1_tier2_arm']['any_measure']['pct']}% counting title cues and arm titles).
- Participant-level data, and the prespecified decision rule (alpha, multiplicity, hierarchy) behind an analysis.
""")

w(f"""## 5. Comparability of primary endpoints

Denominator: {CP['n_trials_with_primary_title']:,} trials with at least one titled PRIMARY measure. Titles are normalised by lower-casing and turning punctuation into spaces; no synonym merging.

- {CP['distinct_normalised_primary_titles']:,} distinct normalised titles across {P['n_primary_measures']:,} PRIMARY measures; {f(CP['titles_used_by_one_trial_only'])} of titles are used by exactly one sampled trial.
- {fc(CP['trials_sharing_exact_title_with_another_sampled_trial'])} of trials share an exact primary title with any other sampled trial; {fc(CP['trials_sharing_exact_title_and_unit'])} share title and unit.
- Within our `primary_condition` grouping, {fc(CP['within_group_sharing_overall'])} share a title with another sampled trial in the same group.

**The 15 most common primary measure titles**

| normalised title | trials | % of {CP['n_trials_with_primary_title']:,} |
|---|---:|---:|
""" + '\n'.join(f"| {esc(r['title'])} | {r['trials']} | {r['pct_of_trials_with_primary']} |" for r in CP['top15_titles']) + f"""

Together they cover {fc(CP['trials_covered_by_top15_titles'])} of trials. Several are the same concept under different strings: progression-free survival and overall survival each appear twice, response rate three times, adverse events five times.

**By `primary_condition`** (our record's grouping)

| group | trials | distinct primary titles | trials sharing a title within group | % | most shared title (trials) | most common unit (% of trials) |
|---|---:|---:|---:|---:|---|---|
""" + '\n'.join(f"| {esc(r['group'])} | {r['trials']} | {r['distinct_titles']} | {r['trials_sharing_a_title_within_group']} | {r['pct_sharing']} | {esc(r['top_title']) + (' (' + str(r['top_title_trials']) + ')' if r['top_title_trials'] > 1 else '')} | {esc(r['top_unit'])} ({r['pct_top_unit']}) |" for r in CP['by_primary_condition']) + f"""

Oncology is the only group with real title concentration ({next(r['pct_sharing'] for r in CP['by_primary_condition'] if r['group'] == 'Oncology')}% of its trials share a title). The most common unit is a count of participants in {sum(1 for r in CP['by_primary_condition'] if r['top_unit'] == 'participants')} of {len(CP['by_primary_condition'])} groups, which is a unit of reporting, not an endpoint.

**Endpoint families [heuristic keyword match on primary titles; a trial can match several]** (denominator {CP['endpoint_families']['n']:,})

{tdist(CP['endpoint_families'], 'family')}

Within Oncology (n = {CP['endpoint_families_oncology']['n']}): {', '.join(f"{r['value']} {r['k']} ({r['pct']}%)" for r in CP['endpoint_families_oncology']['rows'])}.

Scale caveat: the sample is {round(100 * N / SM['population_n'], 1)}% of the dataset, so "shares a title with another sampled trial" understates sharing in the full dataset (a title seen once here could have dozens of users there). The scale-free readings are the share of trials covered by the commonest titles and families, and the ratio of distinct titles to measures.

## 6. What this supports and does not support

**Supports**

- **A trial-level outcomes layer from structured fields, for nearly every record.** Results modules are present in {M['all_five']['pct']}% of trials; participant flow, per-arm serious-adverse-event totals with denominators, and completion counts are near-universal and uniformly structured.
- **Safety and attrition as cross-trial outcomes.** STARTED and COMPLETED exist for every sampled trial; serious-event numbers affected and at risk for every arm in {AE['all_groups_have_seriousNumAtRisk']['pct']}%; an all-cause mortality table in {AE['all_groups_have_deathsNumAtRisk']['pct']}% overall and {AE['deaths_table_by_results_year']['2021-2026']['pct']}% of results first posted since 2021. **[inferred]** These are the outcome quantities whose structure is the same in every record, whatever the condition.
- **Describing statistical reporting itself.** Whether a primary analysis, p-value, estimate or CI was posted is measurable for every trial, and can be tabulated against whether race, ethnicity and sex were reported.
- **Efficacy linkage within a narrow, pre-chosen slice.** {P['arm_level_effect_computable']['any']['pct']}% of trials have arm-level primary data from which an effect could in principle be computed **[inferred]**, and {AN['trial_level_of_all_sampled']['primary_analysis_with_estimate_and_both_CI_limits']['pct']}% post an estimate with a CI. **[inferred]** A shared endpoint exists only where one dominates a field; in this sample that is oncology survival and response endpoints.
- **Using the baseline population as the composition denominator**, with a stated caveat: baseline total equals STARTED in {BL['baseline_vs_flow_STARTED']['rows'][0]['pct']}% of trials.

**Does not support**

- **Outcomes by race, ethnicity or sex within a trial.** Structured subgroup results exist in about {anyM['tier1_levels']['any_dimension']['pct']}% of trials for any dimension and {anyM['tier1_levels']['race_or_ethnicity']['pct']}% for race or ethnicity ({anyM['tier1_levels']['race_or_ethnicity']['k']} trials; upper CI bound {anyM['tier1_levels']['race_or_ethnicity']['ci95'][1]}%); no sampled trial reports a primary outcome by race or ethnicity in class or category titles, and {priM['supplementary_arm_titles_tier1']['race']['k']} of {N:,} does so through race-defined arms (NCT03098836). Any linkage built on these fields is therefore between a trial's demographic *composition* and its *trial-level* outcome: an ecological comparison, not evidence about how any group fared.
- **Pooling efficacy across trials by endpoint.** {CP['titles_used_by_one_trial_only']['pct']}% of primary titles are unique to one sampled trial, units run to {P['unitOfMeasure_top25']['distinct']} distinct strings, and estimate types to {AN['estimate_paramType_top20']['distinct'] - 1}.
- **A registry-wide "met its primary endpoint" flag from structured fields.** Only {AN['trial_level_of_all_sampled']['primary_analysis_with_pValue']['pct']}% of trials post a primary p-value, {PV_OP['pct']}% of posted primary p-values carry an operator rather than a plain number, and no field states the prespecified threshold, direction or multiplicity rule **[inferred]**. This is the gap the Industry Outcomes beta fills by reading protocols and SAPs for 598 trials.
- **Detecting outcome switching** from the current record (section 3.2).
- **Claims about trials without posted results.** The dataset contains none.
- **Comparing other-adverse-event rates across trials** without handling the reporting threshold (0 in {next(r['pct'] for r in AE['frequencyThreshold']['rows'] if r['value'] == '0')}%, 5 in {next(r['pct'] for r in AE['frequencyThreshold']['rows'] if r['value'] == '5')}%).

## 7. Limitations of this audit

1. **Sample size.** {N:,} of {SM['population_n']:,} ({round(100 * N / SM['population_n'], 1)}%). Proportions near 30-70% are known to about ±2 points; rare features are not: the race-or-ethnicity subgroup count rests on {anyM['tier1_levels']['race_or_ethnicity']['k']} trials, and the observational stratum has {SM['sample_by_study_type'].get('OBSERVATIONAL')}. Intervals are Wilson intervals that treat the proportional stratified sample as a simple random sample and ignore the finite-population correction (both slightly conservative).
2. **One draw.** A different seed would move every figure within its interval; nothing was re-drawn.
3. **Frame versus fetch date.** IDs come from the {SM['extracted_at'][:10]} release; the registry was read on {FM['first_fetch'][:10]}. Records may have changed in between (study type and enrollment did not, section 2).
4. **Registered-versus-posted** compares the current record with itself. No version history was fetched.
5. **Subgroup detection is a regular-expression heuristic** over English titles, class titles and category titles only. It does not read outcome descriptions, analysis descriptions, population descriptions or posted documents. Recall is unknown; the "mention only" row bounds it loosely from above. Precision was judged by the auditing model on all {len(flag)} flagged trials ({rv['yes']} yes, {rv['partly']} partly, {rv['no']} no), not by an independent human. The rule was tightened once after reading its first output (dropping "Child-Pugh", repeated single life-stage words and donor attributes), so it is fitted to this sample.
6. **"Carries data", "effect computable" and field presence are structural.** No value was checked for plausibility, no p-value or CI parsed and validated, and the project's AACT baseline notes record a trial (NCT01507090) whose metadata says "count" while the values are percentages.
7. **Measure-level distributions over-weight trials with many measures**; trial-level counts are given for the main ones only.
8. **Baseline total** relies on a group titled "Total" (or a single group, or a last group equal to the sum of the others); {next((r['k'] for r in BL['baseline_total_method']['rows'] if r['value'].startswith('sum of groups')), 0)} trials fell through to summing groups. STARTED totals sum the first period's groups, which is right for parallel and crossover-sequence designs but was not checked design by design.
9. **Population-description tags and endpoint families are keyword matches**; {next(r['pct'] for r in BL['populationDescription_tags']['rows'] if r['value'] == '(no tag matched)')}% of descriptions and {next(r['pct'] for r in CP['endpoint_families']['rows'] if r['value'] == '(no family matched)')}% of trials matched no tag or family.
10. **Title comparability is exact-string after light normalisation**; it under-counts conceptually identical endpoints, and within-sample sharing under-counts sharing in the full dataset (section 5).
11. **The AACT mapping is recalled, not verified.** Column names, especially for `outcome_counts.scope`, baseline population text and limitations text, need checking against the dictionary.
12. **`phase` and `sponsor_class` strata use our record's values**, not re-derived ones.
13. **Not done:** no secondary-outcome deep dive beyond counts and analysis presence; no check of value-level agreement between API and AACT; no look at `documentSection` (protocol/SAP availability) for the sample.

## 8. Context read before measuring

- `civicsample-engine` at `76c8a93`: `outcomes/README.md`, `outcomes/evidence.py`. The Industry Outcomes beta is manual, covers industry Phase 3 randomized trials with primary completion in 2020 (648 frame records, 598 eligible on 2026-11-09), and reads `protocolSection`, `documentSection` and `hasResults` only; it does not read `resultsSection`. `src/utils.py` takes `primary_endpoint` and `secondary_outcomes` from `protocolSection.outcomesModule`.
- `clinical-trial-populations` at `73e5d58`: `data/outcomes/summary.json` (598 eligible; results posted 416; protocol or SAP posted 418; in the demographics dataset 416; audit collection 23; human-reviewed assessments 0), `industry_outcomes.js` (presents those files, computes no aggregate), `data/geo/README.md`.
- `civicsample-verify-ai-private`, `origin/main` at `808ad3a`: only the most recent outcomes status file was read (`docs/outcomes-review/checkpoint-20261109/STATUS-20261110.md`). It records that, as of 2026-11-10, the outcomes packet experiment had not been activated and no provider call had been made. Nothing else from that repository is reproduced here.

## 9. Files and re-running

| file | what it is |
|---|---|
| `report.md` | this report (rendered by `scripts/make_report.py` from the JSON files) |
| `sample_ids.txt`, `sampling.json` | the 2,000 IDs and the sampling parameters |
| `sample_frame.json` | the sampled records' fields used here, copied from the release |
| `cache/raw_responses.jsonl.gz`, `cache/fetch_manifest.json` | raw API responses and fetch record |
| `analysis.json` | every count in this report with its denominator and interval |
| `subgroup_hits.jsonl` | all {SG['measures']['tier1_measures'] + SG['measures']['tier2_only_measures']} flagged subgroup measures |
| `scripts/draw_sample.py`, `fetch.py`, `analyze.py`, `crosscheck.py`, `make_report.py` | the pipeline; `explore_keys.py`, `inspect_data.py`, `probe_*.py`, `show.py` are the exploratory helpers used along the way |

Cross-check: `scripts/crosscheck.py` recomputes twelve headline counts by a separately written code path and compares them with `analysis.json`; all twelve agree (it caught one inconsistency during the audit, a data-presence count that had wrongly required `reportingStatus` = POSTED, which was fixed before this report was rendered).

Offline re-run (no network): `python3 -I scripts/analyze.py <outdir> && python3 -I scripts/make_report.py <outdir>`. Re-drawing needs the release file: `python3 -I scripts/draw_sample.py <demographics.json.gz> <outdir>`; re-fetching: `python3 -I scripts/fetch.py <outdir>` (skips batches already cached).
""")
open(os.path.join(OUT, 'report.md'), 'w').write('\n'.join(L))
print('wrote report.md', sum(len(x) for x in L), 'chars')
