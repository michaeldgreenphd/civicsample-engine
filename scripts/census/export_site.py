#!/usr/bin/env python3
"""Cut a census benchmark build down to the files the site reads.

  python3 scripts/census/export_site.py build/census ../clinical-trial-populations/data/census

A pure selection of an existing build: nothing is recomputed. The site gets one file per
dimension holding the pooled areas for the "one listed facility" site basis (both referral
variants), the summary of counts, and a manifest that names the build it came from and the
sha256 of every file written. The per-trial file and the self-described-only variant stay
with the build: the first is too large to serve and the second has too few areas to show.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

SITE_SCHEMA_VERSION = 1
DIMENSIONS = ('race', 'ethnicity', 'sex')
SITE_BASIS = 'one_listed_facility'
MANIFEST_KEYS = ('method_version', 'generated_at', 'producer', 'source_release', 'parameters', 'windows', 'product',
                 'population_basis', 'comparison_label', 'default_level', 'tables', 'margin_of_error_level', 'limitations')


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export(build: Path, out: Path) -> dict[str, Any]:
    run = json.loads((build / 'run.json').read_text())
    for name, digest in run['files'].items():        # refuse a build whose files were altered
        if name != 'run.json' and sha256(build / name) != digest:
            raise ValueError(f'{name}: bytes do not match the build manifest')
    areas = json.loads((build / 'areas.json').read_text())['areas']
    run_id = f"census-{run['source_release']['tag']}-{(run['producer']['commit'] or 'unknown')[:10]}"
    stage = Path(tempfile.mkdtemp(prefix='census_site_', dir=out.parent))
    for dim in DIMENSIONS:
        rows = [{k: v for k, v in a.items() if k not in ('dimension', 'site_basis')}
                for a in areas if a['dimension'] == dim and a['site_basis'] == SITE_BASIS]
        (stage / f'areas.{dim}.json').write_text(json.dumps(
            {'schema_version': SITE_SCHEMA_VERSION, 'run_id': run_id, 'dimension': dim, 'site_basis': SITE_BASIS,
             'areas': rows}, separators=(',', ':'), sort_keys=True) + '\n')
    summary = json.loads((build / 'summary.json').read_text())
    (stage / 'summary.json').write_text(json.dumps({**summary, 'run_id': run_id}, indent=1, sort_keys=True) + '\n')
    manifest = {'schema_version': SITE_SCHEMA_VERSION, 'run_id': run_id, 'manual_run': True, 'site_basis': SITE_BASIS,
                **{k: run[k] for k in MANIFEST_KEYS},
                'registry_retrieved_at': run['registry_archive']['retrieved_at'],
                'n_sources': len(run['sources']), 'build_files': run['files']}
    manifest['files'] = {p.name: sha256(p) for p in sorted(stage.iterdir())}
    (stage / 'run.json').write_text(json.dumps(manifest, indent=1, sort_keys=True) + '\n')
    if out.exists():
        shutil.rmtree(out)
    shutil.move(str(stage), str(out))
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('build', type=Path)
    ap.add_argument('out', type=Path)
    args = ap.parse_args()
    m = export(args.build, args.out)
    print(f"wrote {args.out} ({m['run_id']}): " + ', '.join(sorted(m['files'])))
    return 0


if __name__ == '__main__':
    sys.exit(main())
