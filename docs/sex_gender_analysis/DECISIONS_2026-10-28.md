# Owner decisions on the sex/gender analysis rules, 2026-10-28

Recorded by the owner on the private decisions page between 22:38 and 22:41 UTC on
2026-10-28, and implemented here. Rule ids are those of `RULE_SOURCE_MATRIX.md`.

| Rule | Decision | What the code does |
|---|---|---|
| R-IC-01 incomplete composition | Keep "not evaluated" for this version | `incomplete_composition` stays null with its reason; no record claims complete composition it was not checked for |
| R-PO-01 patients-only (E7) | No general rule | `patients_only.status` stays `not_evaluated`; the parser's count-driving choice is never rewritten |
| R-FS-01 FINAL versus as-run | Stay as-run, labelled | `layer = as_run`; no hand-reconciled value is published |
| R-GC-04 which sum the 10% exclusion compares | The D3 total | As implemented |
| R-GC-05 category vocabulary parity with A20 | Hold the D3/D5 table off the site until parity is shown | The engine still derives and publishes the categories, flagged R-GC-05; the site neither reads nor shows them (clinical-trial-populations, commit 1167884) |

## Effect on the published blocks

Each decision keeps the behaviour the layer already had, so **no count changes**.
What changes is what each block says about it:

- `unresolved` now lists only rules that are still open, so only R-GC-05 remains,
  on blocks whose gender categories were derived or excluded. Before, every block
  also listed R-FS-01 and R-PO-01, and blocks reporting sex listed R-IC-01. Those
  three are now decided limitations, stated on the fields they affect.
- The summary's `sexGenderAnalysis.unresolvedRules` names only R-GC-05. A new
  `decidedRules` entry gives the four decided rules with their decision.
- `RULES_VERSION` moves from `analysis@2026-10-28 draft1 / …` to
  `analysis@2026-10-28 decided (R-IC-01, R-PO-01, R-FS-01, R-GC-04; R-GC-05 open) / …`.
  Blocks from the two versions differ only in `unresolved` and `rules_version`.

On the 15 fictional fixture cases, every case's `unresolved` list drops R-FS-01,
R-PO-01 and, where present, R-IC-01. The four cases with derived or excluded
categories keep R-GC-05. Every other expected value is unchanged.

## Still open

- R-GC-05 needs the A20 derivation script, to test the reconstructed vocabulary
  against it. Showing D3/D5 on the site waits on that test.
