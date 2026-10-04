"""split_data.py and the run stamps it publishes.

The site keys every data URL by the latest run's extracted_at, read from
data/run.json, so a returning browser keeps a run's files and fetches new
ones as soon as a new run (a same-day re-run included) is published. These
tests pin that split_data.py writes run.json with the same stamps as the
parts it cuts, and that the weekly job publishes it with them.
"""
from __future__ import annotations

import gzip
import json
import os
import pathlib
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAMPS = {"extracted_at": "2026-09-27T11:49:53.609081+00:00", "pipeline_commit": "12b9b652ad5fdf3b05653b30ecf47297ae87a479"}


def _split(tmp_path: pathlib.Path, n: int) -> subprocess.CompletedProcess[str]:
    (tmp_path / "data").mkdir()
    records = [{"nct_id": f"NCT{i:08d}"} for i in range(n)]
    (tmp_path / "data" / "demographics.json").write_text(json.dumps({**STAMPS, "data": records}, indent=2))
    return subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "split_data.py")],
                          cwd=tmp_path, capture_output=True, text=True)


def test_split_writes_the_run_stamps_next_to_the_parts(tmp_path: pathlib.Path) -> None:
    r = _split(tmp_path, 20)
    assert r.returncode == 0, r.stderr
    run = json.loads((tmp_path / "data" / "run.json").read_text())
    assert run == {**STAMPS, "total_parts": 8, "studies": 20}
    seen = []
    for i in range(1, 9):
        with gzip.open(tmp_path / "data" / f"demographics.part{i}.json.gz", "rt") as f:
            part = json.load(f)
        assert (part["extracted_at"], part["pipeline_commit"]) == (run["extracted_at"], run["pipeline_commit"])
        assert (part["part"], part["total_parts"]) == (i, run["total_parts"])
        seen += [s["nct_id"] for s in part["data"]]
    assert len(seen) == run["studies"] == len(set(seen))


def test_the_weekly_job_publishes_the_run_stamps_with_the_parts() -> None:
    wf = open(os.path.join(ROOT, ".github", "workflows", "extract.yml")).read()
    m = re.search(r"\n      - name: Publish artifacts, archive snapshot, and push to the site\n(.*?)(?=\n      - name: )", wf, re.S)
    assert m, "extract.yml lost the publish step"
    step = m.group(1)
    assert "cp data/run.json site/data/run.json" in step
    assert re.search(r"git add data/demographics\.part\*\.json\.gz [^\n]*data/run\.json", step), "run.json is not staged with the parts"
    assert step.index("cp data/run.json") < step.index("git commit")
