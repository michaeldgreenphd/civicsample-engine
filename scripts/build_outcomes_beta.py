#!/usr/bin/env python3
"""Build the Industry Outcomes beta artifacts by hand (never scheduled).

  python3 scripts/build_outcomes_beta.py --fetch --assessments /private/path/export.json \
      --site ../clinical-trial-populations --out ../clinical-trial-populations/data/outcomes

  --fetch             fetch the frame from the ClinicalTrials.gov API and freeze it under
                      build/outcomes_cache/ (default when --archive is not given)
  --archive PATH      reuse a frozen archive (its .meta.json must match its bytes)
  --assessments PATH  the private repository's allowlisted export (export_assessments.py);
                      read at run time, never copied into this repository
  --site PATH         a site checkout, for the demographics link (read only)
  --out PATH          where to write; default build/outcomes
  --cutoff DATE       results-availability cutoff; default today (UTC)

Writes nothing unless the staged family passes outcomes/contract.py. A provisional_preview
export gives a preview build: never commit or publish it (--publishable checks refuse it).
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from datetime import datetime, timezone, date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from outcomes import acquire, artifacts, attribution, contract, evidence, link  # noqa: E402


def producer() -> dict[str, object]:
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True).stdout.strip() or None
    dirty = bool(subprocess.run(['git', 'status', '--porcelain', '--', 'outcomes', 'scripts/build_outcomes_beta.py',
                                 'sponsors/company_aliases.csv'], cwd=ROOT, capture_output=True, text=True).stdout.strip())
    return {'repository': 'civicsample-engine', 'commit': commit, 'uncommitted_changes': dirty}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument('--fetch', action='store_true')
    src.add_argument('--archive', type=Path)
    ap.add_argument('--assessments', type=Path)
    ap.add_argument('--site', type=Path)
    ap.add_argument('--out', type=Path, default=ROOT / 'build' / 'outcomes')
    ap.add_argument('--cutoff', type=date.fromisoformat)
    args = ap.parse_args(argv)
    cutoff = args.cutoff or datetime.now(timezone.utc).date()
    if args.archive:
        studies, meta = acquire.read_archive(args.archive)
    else:
        studies = acquire.fetch(evidence.FRAME_QUERY)
        path = ROOT / 'build' / 'outcomes_cache' / f'frame-{cutoff.isoformat()}.jsonl.gz'
        meta = acquire.write_archive(studies, path, evidence.FRAME_QUERY)
        print(f'fetched {len(studies)} records -> {path.relative_to(ROOT)}')
    export = export_sha = None
    if args.assessments:
        raw = args.assessments.read_bytes()
        export, export_sha = artifacts.read_export(raw), hashlib.sha256(raw).hexdigest()
    facts = [evidence.facts(s) for s in studies]
    eligible_ids = sorted(f['nct_id'] for f in facts if not evidence.eligibility(f))
    attr = attribution.attribute([f for f in facts if f['nct_id'] in set(eligible_ids)])
    demo = link.link(eligible_ids, args.site)
    try:
        files = artifacts.build(studies, meta, cutoff, export, export_sha, attr, demo, producer())
    except artifacts.BuildError as exc:
        print(f'refused: {exc}', file=sys.stderr)
        return 1
    problems = contract.check(files)
    if problems:
        print('contract problems; nothing written:', *problems, sep='\n  ', file=sys.stderr)
        return 1
    artifacts.write(files, args.out)
    run = files['run.json']
    print(f"{run['run_id']}: {run['cohort']['n_eligible']} eligible of {run['cohort']['n_frame']} frame records; "
          f"display {run['display_mode']}; {run['assessments']['n_human_reviewed']} human-reviewed, "
          f"{run['assessments']['n_machine_provisional']} machine-provisional -> {args.out}")
    if run['display_mode'] != 'evidence_only':
        print('PREVIEW BUILD: contains machine assessments. Do not commit or publish it.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
