# Port check: production rules against the audit's cached records

`outcomes/results_structure.py` is a port of the audit scripts in
`../second_pass/scripts/` (`usable.py`, `subgroup_rule.py`, `denominators.py`).
This directory holds a comparison of the two on the same input: the audit's
2,000 cached registry responses, `../first_pass/cache/raw_responses.jsonl.gz`
(sha256 `a9e4577735b5f3ece14ff672a5db0f4ca70be283f237bb996c09328c3569510a`).

**This checks the port. It does not check the scientific adequacy of the
rules.** Both sides apply the same definitions to the same bytes, so agreement
shows that the production code computes what the audit computed. Whether two
groups on a primary measure means a comparative trial, whether an estimate
with both limits is a usable effect estimate, and whether the text rule finds
the subgroup results that exist are questions this comparison cannot answer.

Run on 2026-11-14 with method version `results-structure-1`. No network call.

## Headline figures

| Figure | Audit | Production | Difference |
|---|---:|---:|---:|
| B1 baseline usable | 1,991 | 1,991 | 0 |
| F1 participant flow usable | 1,995 | 1,995 | 0 |
| O1 primary outcome usable | 1,905 | 1,905 | 0 |
| A1 adverse events usable | 1,932 | 1,932 | 0 |
| All four usable | 1,852 | 1,852 | 0 |
| C1 | 1,394 | 1,394 | 0 |
| C2 | 1,134 | 1,134 | 0 |
| Between-arm analysis among C2 | 473 | 473 | 0 |
| Between-arm estimate with both limits among C2 | 310 | 310 | 0 |
| SG2: trials with at least one signal | 136 | 136 | 0 |

Denominator for every row is the 2,000 sampled trials, except the two
between-arm rows, whose denominator is the 1,134 C2 trials.

## Trial-by-trial comparison

The headline counts could agree while individual trials differ, so the script
also compares each trial with the audit's per-trial tables.

- **Flags and reasons** (`../second_pass/per_trial_usable.jsonl.gz`): B1, F1,
  O1 and A1 with their reasons, the four module-presence flags, all four
  usable, C1, C2, S1, S1_between, S1_between_p and S1_between_est_ci. 0 of
  2,000 trials differ on any of the 19 fields.
- **Denominators** (`../second_pass/per_trial_denominators.jsonl.gz`):
  baseline total and its method, the sum of the arms, number started and its
  reason, enrolment and its type, and the three pairwise relations. 0 of 2,000
  trials differ on any of the 10 fields.
- **Subgroup signals** (`../second_pass/subgroup_detected.jsonl`): the list of
  (measure index, field, dimension, text) for every trial. 788 signals on each
  side; 0 trials have a differing list.
- **Parsing failures:** none in the 2,000 records.

`comparison.json` holds every count, with up to ten example NCT ids for any
field that differs (none does).

## What the production module adds to the audit rules

These do not change which trials pass a rule or which signals fire; they are
listed so the comparison is not read as covering them.

- **Signal dimension.** The audit rule names sex, race, ethnicity, age and
  origin for level rules, and only the kind of cue (`keyword`,
  `by_demographic`) for cue rules. The production module maps each signal to
  age, sex, race, ethnicity or other: origin and keyword become other; a
  `by_demographic` cue takes the demographic word in its matched text. On the
  2,000 records the 788 signals map to age 345, sex 66, race 15, ethnicity 4,
  other 358. The mapping has no audit counterpart to compare with.
- **Subgroup states.** With no human review rows, a trial with a signal is
  `processing_failed_or_indeterminate` for the signal's dimension, and a
  signal with dimension other leaves all four dimensions indeterminate. On the
  2,000 records: 136 trials indeterminate at study level and 1,864 with no
  signal; by dimension, indeterminate for age 123, sex 107, race 95,
  ethnicity 92. No trial is in the detected state.
- **P-value form.** Among between-arm analyses on primary measures: 853 plain
  numbers (37 of them posted with a leading equals sign), 344 bounds such as
  `<0.001`, 353 with no p-value. Rule S1 itself is unchanged: a p-value counts
  when the string contains a digit.
- **Verbatim matched text and field paths** on each signal. The audit's
  display text (truncated titles joined with ` | `) is what is compared above.

The audit's judgements of the 136 detected trials were made by one automated
reader. They are not loaded by the production module and are not human
reviews.

## Re-running

From the repository root:

```bash
python docs/audits/outcomes_results_structure/port_check/port_check.py
```

It rewrites `comparison.json`, prints the headline table, and exits 1 if any
headline figure, per-trial field or signal list differs.
