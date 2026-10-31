"""Runs tests/gemini_workflow.test.mjs, the Gemini PR review workflow's checks.

That file is shared verbatim with clinical-trial-populations, where `npm test`
runs it. It executes the workflow's two github-script steps against a mocked
GitHub API, so it needs Node; CI sets Node up before this suite. Running it
from pytest keeps it inside the check block AGENTS.md and ci.yml agree on.
"""
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKS = ROOT / "tests" / "gemini_workflow.test.mjs"


def test_gemini_workflow_checks_pass() -> None:
    node = shutil.which("node")
    if not node:
        pytest.fail("node is not on PATH: the Gemini workflow checks run its JavaScript "
                    "(CI sets node up before the Python suite)")
    r = subprocess.run([node, "--test", str(CHECKS)], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, f"gemini_workflow.test.mjs failed:\n{r.stdout[-6000:]}\n{r.stderr[-2000:]}"
