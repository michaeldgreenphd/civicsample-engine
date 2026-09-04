"""AGENTS.md and CLAUDE.md assert facts about this repository. Nothing checked
them, and they went stale six times in two days — each time because a claim was
verified against a tree that then moved underneath it.

These tests are the check. They do not review the prose; they assert that every
path, identifier and command the two documents name still resolves, that the
documented check block is the one CI actually runs, and that neither file
points at work that has not landed. A rename or a moved mechanism turns CI red
instead of waiting for a reviewer to notice.

The site repo does the same for its geography plumbing in
tests/repo_wiring.test.mjs.
"""
import ast
import pathlib
import re
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[1]
DOCS = ["AGENTS.md", "CLAUDE.md"]

# Words that appear in backticks as prose, not as code the repo must contain.
PROSE = {
    "main", "true", "false", "not_applicable", "attribute", "exclude",
    "attributed", "unreviewed", "lead", "collaborator", "bash", "json",
    "shared", "note", "status", "pattern", "canonical", "any",
}

# Column names from AACT, the upstream registry dump. They are cited in the
# docs as schema facts a reader has to know, and they correctly appear nowhere
# in this repository's own source. Anything added here should be a name this
# repository consumes but does not define — if it is ours, fix the doc instead.
EXTERNAL_SCHEMA = {"number_analyzed", "baseline_measurements", "result_groups"}


def _tracked():
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True)
    return [p for p in out.stdout.split("\n") if p]


def _doc_text():
    return {d: (REPO / d).read_text() for d in DOCS}


def _backticked(text):
    return re.findall(r"`([^`\n]+)`", text)


def test_docs_exist():
    for d in DOCS:
        assert (REPO / d).is_file(), f"{d} is missing; the other tests here assert its claims"


def test_every_path_the_docs_name_resolves():
    """A path in backticks must exist. This is what a reader will try to open."""
    tracked = set(_tracked())
    dirs = {str(pathlib.PurePosixPath(p).parent) + "/" for p in tracked}
    missing = []
    for doc, text in _doc_text().items():
        for tok in _backticked(text):
            tok = tok.strip()
            looks_like_path = "/" in tok or re.search(r"\.(py|mjs|js|json|csv|yml|txt|md)$", tok)
            if not looks_like_path or " " in tok:
                continue
            if tok in tracked or tok in dirs or (REPO / tok).exists():
                continue
            # A bare filename is fine if exactly one tracked file has that name.
            if sum(1 for p in tracked if p.endswith("/" + tok)) >= 1:
                continue
            missing.append(f"{doc}: `{tok}`")
    assert not missing, "paths named in the docs that do not resolve:\n  " + "\n  ".join(missing)


def test_every_identifier_the_docs_name_exists_in_the_source():
    """A constant or function named in backticks must appear in tracked source.

    This is the check that would have caught FIXTURE_RULES_SHA256 surviving in
    the prose after the sponsor loop replaced it.
    """
    sources = [p for p in _tracked() if p.endswith((".py", ".mjs", ".js", ".json", ".yml"))]
    haystack = "\n".join((REPO / p).read_text(errors="ignore") for p in sources)
    missing = []
    for doc, text in _doc_text().items():
        for tok in _backticked(text):
            tok = tok.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{3,}", tok):
                continue
            if tok in PROSE or tok.lower() in PROSE or tok in EXTERNAL_SCHEMA:
                continue
            if re.search(r"\b" + re.escape(tok) + r"\b", haystack):
                continue
            missing.append(f"{doc}: `{tok}`")
    assert not missing, (
        "identifiers named in the docs that appear nowhere in tracked source:\n  "
        + "\n  ".join(missing)
        + "\n(rename it in the docs, or add it to PROSE if it is an English word)"
    )


def test_documented_checks_are_the_checks_ci_runs():
    """The block under 'Running the checks' must be exactly ci.yml's run steps.

    Documentation that tells a contributor to run something CI does not is how
    a green local run becomes a red pull request.
    """
    agents = (REPO / "AGENTS.md").read_text()
    block = re.search(r"```bash\n(.*?)```", agents, re.S)
    assert block, "AGENTS.md has no ```bash block; the checks section is the contract with ci.yml"
    documented = [
        re.sub(r"\s*#.*$", "", ln).strip()
        for ln in block.group(1).strip().split("\n")
        if ln.strip()
    ]

    ci = (REPO / ".github/workflows/ci.yml").read_text()
    ran = [
        re.sub(r"\s*#.*$", "", m).strip()
        for m in re.findall(r"^\s*run:\s*(.+)$", ci, re.M)
    ]

    assert documented == ran, (
        "AGENTS.md's check block has drifted from .github/workflows/ci.yml\n"
        f"  documented: {documented}\n"
        f"  ci.yml:     {ran}"
    )


def test_docs_do_not_point_at_work_that_has_not_landed():
    """No branch names, pull request numbers, or 'not on main yet'.

    A reference to in-flight work is true only until it merges, and these files
    are read by agents and reviewers who have no way to know which it is.
    """
    patterns = [
        (r"the open `[^`]+` pull request", "names an open pull request"),
        (r"is not on `?main`? yet", "says something is not on main yet"),
        (r"\bPR #\d+|\(#\d+\)", "cites a pull request number"),
    ]
    hits = []
    for doc, text in _doc_text().items():
        for pat, why in patterns:
            for m in re.finditer(pat, text):
                hits.append(f"{doc}: {why} — {m.group(0)!r}")
    assert not hits, (
        "the docs reference work that may not have landed:\n  " + "\n  ".join(hits)
        + "\nState the invariant instead; a branch name expires when it merges."
    )


def test_python_symbols_the_docs_attribute_to_a_file_are_defined_there():
    """`SYMBOL` named next to `path/to/file.py` must actually be defined there."""
    problems = []
    for doc, text in _doc_text().items():
        flat = re.sub(r"\s+", " ", text)
        for sym, path in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)` in `([a-z][A-Za-z0-9_/]*\.py)`", flat):
            f = REPO / path
            if not f.is_file():
                problems.append(f"{doc}: `{path}` does not exist (claimed to define `{sym}`)")
                continue
            tree = ast.parse(f.read_text())
            defined = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    defined.add(node.name)
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    defined.add(node.id)
            if sym not in defined:
                problems.append(f"{doc}: `{sym}` is not defined in `{path}`")
    assert not problems, "docs attribute a symbol to the wrong file:\n  " + "\n  ".join(problems)
