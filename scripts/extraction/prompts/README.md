# Extraction prompts

The instructions sent to the models live here as plain text — one file per
extraction stream. **To change what the models are asked to extract, edit
the text file. No Python required.**

| File | Stream | Script that uses it |
|---|---|---|
| `fda_demographics.txt` | FDA device decision summaries | `../extract_fda_demographics.py` |
| `trial_papers.txt` | Clinical-trial manuscripts | `../extract_trial_papers.py` |
| `paper_ses.txt` | AI/ML validation papers (SES) | `../extract_paper_ses.py` |

## How it works

Each script reads its prompt file once at startup and appends the
document's text after it (delimited by `--- PAGE N ---` markers), so the
file you edit is the complete instruction the model sees before the
document. There are no placeholders or variables inside the prompt — what
you write is exactly what is sent.

## Making a change

1. Edit the `.txt` file on a branch and open a pull request. The prompt is
   version-controlled like code, so every change has an author, a date,
   and a diff — and can be reverted.
2. To try it before running everything: from the Actions tab, run
   **Run Extraction Pipelines** with `run_mode: pilot-test` and just the
   stream you changed. Pilot mode caps the run at a handful of documents
   (8 FDA / 8 AI/ML / 20 trials), so it's cheap and takes minutes.
3. Check the output the pilot commits (`data/*_extracted*.json`) and the
   run's log in Google Drive. The token-metrics files record which prompt
   run produced them (`run_info`: timestamp, code commit, workflow run).

## One caution

The extraction scripts parse the model's reply against a fixed set of
field names (the tool schema defined in each script). Rewording
instructions, adding guidance, or changing what counts as evidence is
safe. **Adding or renaming output fields** needs a matching change in the
script's schema — ask for that in the PR and it can be done alongside your
prompt edit.
