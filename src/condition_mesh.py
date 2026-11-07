"""ClinicalTrials.gov's MeSH indexing of each trial's conditions, kept beside the
full records rather than in them.

WHAT IT IS. ClinicalTrials.gov maps every study's free-text conditions to MeSH
descriptors and publishes the result under
derivedSection.conditionBrowseModule: `meshes` are the descriptors matched to
the conditions, `ancestors` the descriptors above them in the MeSH tree, each
as {"id": "D......", "term": "..."}. The registry derives these, not the
sponsor. The API v2 search endpoint this pipeline reads returns only those two
lists (no browseLeaves or browseBranches), checked on 200 studies with results
on 2026-11-07: 173 carried the module with descriptors, 27 had no module.

WHY A SEPARATE FILE. While the site's record contract keeps its split layout
off, every field of a study record ships in the site's whole-record parts,
which are close to their size budget. The descriptors are wanted for labelling
therapeutic areas against the MeSH schema, not by the dashboard, so they go to
condition_mesh.jsonl.gz beside the week's output (one line per trial, joinable
on nct_id with the full records) and from there to the week's data-* release.

ABSENCE IS RECORDED, NOT FLATTENED. Each line carries a `state`:
  indexed      the module is present with at least one descriptor
  not_indexed  the module is present, its `meshes` missing or empty
  no_module    derivedSection has no conditionBrowseModule key
  no_derived   the study has no derivedSection key
  malformed    derivedSection or the module is present but not an object
               (null included), or `meshes` or
               `ancestors` is not a list of {"id": str, "term": str};
               `problem` names which
`meshes` and `ancestors` hold the registry's lists verbatim when they are
well-formed lists and null otherwise, so "no list" never reads as "an empty
list". A line never invents a descriptor the registry did not give.
"""
from __future__ import annotations

from typing import Any

FILENAME = "condition_mesh.jsonl.gz"
SCHEMA_VERSION = "1.0"
STATES = ("indexed", "not_indexed", "no_module", "no_derived", "malformed")


def _terms(value: Any) -> list[dict[str, Any]] | None:
    """The registry's list verbatim if every entry has a string id and term;
    None when the value is not such a list."""
    if not isinstance(value, list):
        return None
    for entry in value:
        if not (isinstance(entry, dict) and isinstance(entry.get("id"), str)
                and isinstance(entry.get("term"), str)):
            return None
    return value


def _nct_id(study: dict[str, Any]) -> str | None:
    return ((study.get("protocolSection") or {}).get("identificationModule") or {}).get("nctId")


def select_condition_mesh(study: dict[str, Any]) -> dict[str, Any]:
    """One line of condition_mesh.jsonl.gz for an API v2 study document.

    The caller adds the run stamps (snapshot_date, extracted_at,
    pipeline_commit), as it does for the raw sex/gender measures."""
    line: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "nct_id": _nct_id(study),
                            "state": None, "meshes": None, "ancestors": None}
    # Absence is the key missing; a key present with null (or any non-object)
    # is malformed, so a broken payload never reads as "the registry gave none".
    if "derivedSection" not in study:
        line["state"] = "no_derived"
        return line
    derived = study["derivedSection"]
    if not isinstance(derived, dict):
        line["state"], line["problem"] = "malformed", "derivedSection is not an object"
        return line
    if "conditionBrowseModule" not in derived:
        line["state"] = "no_module"
        return line
    module = derived["conditionBrowseModule"]
    if not isinstance(module, dict):
        line["state"], line["problem"] = "malformed", "conditionBrowseModule is not an object"
        return line
    problems = []
    for key in ("meshes", "ancestors"):
        if key not in module:
            continue  # stays null: the registry gave no list
        terms = _terms(module[key])
        if terms is None:
            problems.append(f"{key} is not a list of id/term pairs")
        line[key] = terms
    if problems:
        line["state"], line["problem"] = "malformed", "; ".join(problems)
    else:
        line["state"] = "indexed" if line["meshes"] else "not_indexed"
    return line
