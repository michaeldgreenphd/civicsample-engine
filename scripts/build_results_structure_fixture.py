#!/usr/bin/env python3
"""Write the fictional results-structure artifact family to tests/fixtures/results_structure/.

The site's reader tests use a copy of it (tests/fixtures/results_structure/ in the site
repository, with this repository's commit recorded in SOURCE.json there). Deterministic:
rerunning on the same code gives the same bytes. Every trial, count and review row is invented
(tests/outcomes/rs_fixture.py).
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests' / 'outcomes'))

import rs_fixture as F  # noqa: E402
from outcomes import acquire  # noqa: E402
from outcomes import results_structure as RS  # noqa: E402
from outcomes import results_structure_build as B  # noqa: E402


def family() -> dict[str, object]:
    """The fixture family: a frame of eight ids, of which seven are requested (six retrieved, one
    not) and one is excluded with a stated reason."""
    studies = F.studies()
    raw = acquire.encode(studies)
    text = F.reviews_csv()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / 'subgroup_reviews.csv'
        path.write_text(text)
        reviews = RS.load_reviews(path)
    scope = B.build_scope(F.FRAME, F.REQUESTED, F.EXCLUDED, F.EXCLUDE_REASON)
    records, problems = B.build_records(studies, F.REQUESTED, {'NCT99000003': 'not returned by the registry for filter.ids'},
                                        reviews, frozenset(F.EXCLUDED))
    acquisition = {'source': 'fictional', 'endpoint': None, 'request': 'fictional records, no request made',
                   'retrieved_from': F.GENERATED_AT, 'retrieved_to': F.GENERATED_AT, 'retrieved_at': F.GENERATED_AT,
                   'api_version': 'fictional', 'data_timestamp': '2026-11-14T10:00:00',
                   'n_requested': len(F.FRAME), 'n_records': len(studies), 'n_requests': 0, 'n_studies_requests': 0,
                   'n_retries': 0, 'n_failed_requests': 0, 'min_seconds_between_requests': B.MIN_DELAY,
                   'requested_ids_sha256': hashlib.sha256('\n'.join(F.FRAME).encode()).hexdigest(),
                   'sha256_uncompressed': hashlib.sha256(raw).hexdigest(), 'archive': None}
    files = B.build(records, problems, acquisition, {'repository': 'civicsample-engine', 'commit': 'fixture'},
                    {'file': 'fictional review rows', 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'n_rows': 2},
                    {'source': 'fixture', 'n_requested': len(F.REQUESTED), 'exclude_reason': F.EXCLUDE_REASON},
                    scope, generated_at=F.GENERATED_AT)
    problems_found = B.check(files)
    if problems_found:
        raise SystemExit('\n'.join(problems_found))
    return files


def main() -> None:
    out = ROOT / 'tests' / 'fixtures' / 'results_structure'
    B.write(family(), out)
    print(sorted(p.name for p in out.glob('*.json')))


if __name__ == '__main__':
    main()
