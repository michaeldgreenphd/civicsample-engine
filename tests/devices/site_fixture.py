#!/usr/bin/env python3
"""Write the fictional device-linkage family the SITE's tests read.

  python3 tests/devices/site_fixture.py OUT_DIR
  python3 tests/devices/site_fixture.py ../clinical-trial-populations/tests/fixtures/devices

Runs the whole path on the fictional records of tests/devices/fixture.py (no network): acquire
against the stub, freeze, build, the producer contract, then scripts/export_device_links_site.py.
So the site's fixture is what the engine writes, not a hand copy of it. Every number, name and
sentence in it is invented.

OUT_DIR receives the seven exported files and SOURCE.json, which names this script, the engine
commit and the run id, so a reader of the site repository can tell where the fixture came from.
Other files in OUT_DIR are left alone.

One review row is applied (an accepted link on K990001), so the site can be tested on a published
human review as well as on unreviewed records; the real build has none. The clock is pinned, so
running this twice gives the same bytes.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import export_device_links_site as site  # noqa: E402
# The fictional records, loaded by path under their own name (tests/outcomes has a fixture.py too).
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location('devices_fixture', Path(__file__).resolve().parent / 'fixture.py')
F = sys.modules.setdefault('devices_fixture', importlib.util.module_from_spec(_spec))
if not hasattr(F, 'ROWS'):
    _spec.loader.exec_module(F)
from devices import acquire, artifacts, contract  # noqa: E402

REVIEW_NOTE = 'Fictional review: the summary names this study as the clinical study of the device.'


def build(tmp: Path) -> Path:
    """The fictional build, written to tmp/build with one review applied; returns that path."""
    acquire.now = lambda: F.GENERATED_AT            # retrieval times would otherwise be the wall clock
    acquired = F.acquire_all(tmp)
    extractions = F.pilot_extractions(tmp / 'pilot.json')
    first = F.build_family(tmp, acquired, extractions=extractions)
    # A review is keyed on the link id, which is a hash of what the evidence is; read it from a
    # first build rather than writing the hash down here.
    (reviewed,) = [x['link_id'] for x in first['links.json']['links']
                   if x['source']['id'] == 'K990001' and x['automated_validation'] == 'identifier_confirmed']
    curated = tmp / 'curated_reviewed'
    F.write_curated(curated, reviews=[[reviewed, 'accepted', 'explicitly_cited_supporting_study', '2026-11-13',
                                       REVIEW_NOTE]])
    files = F.build_family(tmp, acquired, curated=curated, extractions=extractions)
    problems = contract.check(files)
    if problems:
        raise ValueError(f'the fictional build fails the producer contract: {problems[:3]}')
    artifacts.write(files, tmp / 'build')
    return tmp / 'build'


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def source(manifest: dict[str, object]) -> dict[str, object]:
    """What SOURCE.json says: how the fixture was made and from which engine revision. The
    commit is absent (None), not guessed, when this is not a git checkout."""
    status = _git('status', '--porcelain')
    return {
        'about': 'A fictional device-linkage family in site schema 1 (K99xxxx, DEN99xxxx, P99xxxx, NCT990000xx). '
                 'No real device, company, study or sentence. One fictional review is applied so that a published '
                 'human review can be tested; the real build has none.',
        'written_by': 'civicsample-engine tests/devices/site_fixture.py',
        'command': 'python3 tests/devices/site_fixture.py ../clinical-trial-populations/tests/fixtures/devices',
        'path': 'tests/devices/fixture.py (stub acquisition) -> devices/artifacts.py build -> devices/contract.py -> '
                'scripts/export_device_links_site.py',
        'engine_commit': _git('rev-parse', 'HEAD'),
        'engine_uncommitted_changes': bool(status) if status is not None else None,
        'run_id': manifest['run_id'],
        'do_not_edit': 'Regenerate with the command above; the files are checked against run.json by the site tests.',
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('out', type=Path)
    args = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / 'site'
        manifest = site.export(build(Path(tmp)), staged)
        args.out.mkdir(parents=True, exist_ok=True)
        for name in [*sorted(manifest['files']), 'run.json']:
            shutil.copyfile(staged / name, args.out / name)
    (args.out / 'SOURCE.json').write_text(json.dumps(source(manifest), indent=1) + '\n', encoding='utf-8')
    print(f"wrote {args.out} ({manifest['run_id']}): " + ', '.join(sorted(manifest['files'])))
    return 0


if __name__ == '__main__':
    sys.exit(main())
