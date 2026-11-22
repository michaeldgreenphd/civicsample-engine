#!/usr/bin/env python3
"""Write the fictional outcomes artifact families to tests/fixtures/outcomes/<mode>/.

The site's reader tests use copies of these (tests/fixtures/outcomes/ in the site repository,
with this repository's commit recorded in SOURCE.json there). Deterministic: rerunning on the
same code gives the same bytes.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests' / 'outcomes'))

import fixture as F  # noqa: E402
from outcomes import artifacts, attribution, contract, evidence  # noqa: E402


def family(mode: str) -> dict[str, object]:
    studies = F.studies()
    facts = [evidence.facts(s) for s in studies]
    eligible = [f for f in facts if not evidence.eligibility(f)]
    with tempfile.TemporaryDirectory() as d:
        rules = Path(d) / 'rules.csv'
        rules.write_text(F.RULES_CSV)
        attr = attribution.attribute(eligible, rules)
    links = {f['nct_id']: {'in_dataset': f['nct_id'] == 'NCT99000001', 'sex_reported': True, 'race_reported': True,
                           'ethnicity_reported': False, 'in_industry_demographics_cohort': f['nct_id'] == 'NCT99000001'}
             for f in eligible}
    link_summary = {'state': 'COMPUTED', 'site_snapshot_date': '2026-11-08', 'n_in_dataset': 1,
                    'n_in_industry_demographics_cohort': 1}
    exp = F.export(mode)
    raw = json.dumps(exp, sort_keys=True).encode()
    meta = {'source': 'fictional', 'query': evidence.FRAME_QUERY, 'n_records': len(studies),
            'sha256_uncompressed': 'a' * 64, 'retrieved_at': '2026-11-09T12:00:00+00:00'}
    files = artifacts.build(studies, meta, F.CUTOFF, artifacts.read_export(raw), hashlib.sha256(raw).hexdigest(),
                            attr, (links, link_summary), {'repository': 'civicsample-engine', 'commit': 'fixture'},
                            generated_at='2026-11-09T12:00:00+00:00')
    problems = contract.check(files)
    if problems:
        raise SystemExit('\n'.join(problems))
    return files


def main() -> None:
    for mode in artifacts.DISPLAY_MODES:
        out = ROOT / 'tests' / 'fixtures' / 'outcomes' / mode
        shutil.rmtree(out, ignore_errors=True)
        artifacts.write(family(mode), out)
        print(f'{mode}: {sorted(p.relative_to(out).as_posix() for p in out.rglob("*.json"))}')


if __name__ == '__main__':
    main()
