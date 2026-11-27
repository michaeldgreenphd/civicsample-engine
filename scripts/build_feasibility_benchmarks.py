#!/usr/bin/env python3
"""Build operational feasibility, accrual velocity, and early-termination benchmarks (manual).

Examples:
  python3 scripts/build_feasibility_benchmarks.py --demographics data/demographics.json.gz \
      --condition-mesh data/condition_mesh.jsonl.gz --out build/feasibility
  python3 scripts/build_feasibility_benchmarks.py --records-dir /path/to/ctgov_v2_records \
      --out build/feasibility
  python3 scripts/build_feasibility_benchmarks.py --archive /path/to/full_records.jsonl.gz \
      --out build/feasibility

Never scheduled by `.github/workflows/extract.yml`. Refuses site-projected
`demographics.part*.json.gz` files, refuses an `--archive` response envelope that failed, was altered
or did not return exactly the trials it requested, refuses a `--condition-mesh` sidecar that lacks a line for any
cohort trial or (with `--demographics`) whose lines' `extracted_at` / `pipeline_commit` differ from
the full records', validates the staged family with `feasibility.artifacts.check_contract`
before writing, and stages atomically into `--out`.
"""
from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from feasibility import artifacts, benchmarks, metrics, termination  # noqa: E402
from src import condition_mesh as cmesh  # noqa: E402
from src import full_records  # noqa: E402

DEFAULT_REVIEWS_PATH = ROOT / "feasibility" / "termination_reviews.csv"
DEFAULT_RULES_PATH = ROOT / "sponsors" / "company_aliases.csv"
DEFAULT_OUT_PATH = ROOT / "build" / "feasibility"

_PART_RE = re.compile(r"demographics\.part\d*\.json(?:\.gz)?$", re.IGNORECASE)


def now_utc_iso() -> str:
    """Current UTC wall-clock timestamp in ISO-8601 seconds format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Repository directories whose loaded modules count as producing code.
PRODUCER_DIRS: tuple[str, ...] = ("feasibility", "src", "sponsors", "scripts")
# This family's scripts, hashed whether or not the build imports them: the contract checker and the
# site exporter read and re-emit the artifacts this digest stamps.
FAMILY_SCRIPTS: tuple[str, ...] = (
    "scripts/build_feasibility_benchmarks.py",
    "scripts/check_feasibility_contract.py",
    "scripts/export_feasibility_site.py",
)
# Repository data files the producing code reads that no command-line flag names, so `run.json`
# `inputs` records no SHA-256 for them. `condition_ontology.json` decides `indication.ontology_*`:
# `src/condition_classifier.py` reads it, and so does the keyword fallback in
# `feasibility/benchmarks.py` when rapidfuzz is absent. (`--rules` and `--reviews` are hashed
# under `inputs` instead.)
PRODUCER_DATA_FILES: tuple[str, ...] = ("condition_ontology.json",)


def _loaded_repo_modules(root: Path) -> dict[str, Path]:
    """Module name -> resolved source file for every loaded module under `PRODUCER_DIRS`."""
    root_resolved = root.resolve()
    loaded: dict[str, Path] = {}
    for name, module in list(sys.modules.items()):
        fname = getattr(module, "__file__", None)
        if not isinstance(fname, str) or not fname.endswith(".py"):
            continue  # builtins, extension modules, namespace packages
        path = Path(fname).resolve()
        if not path.is_relative_to(root_resolved):
            continue
        if path.relative_to(root_resolved).parts[0] in PRODUCER_DIRS:
            loaded[name] = path
    return loaded


def _imported_module_names(path: Path, package: str | None) -> set[str]:
    """Every module name an import statement in `path` can bind, parent packages included.

    `from pkg import name` yields both `pkg` and `pkg.name` because `name` may be a submodule;
    names that are not loaded modules are simply never matched. Relative imports resolve against
    `package`; imports inside functions and `try` blocks count, since the build may run them.
    """
    tree = ast.parse(path.read_bytes(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                if not package:
                    continue  # a relative import outside a package cannot run
                base = importlib.util.resolve_name("." * node.level + base, package)
            targets = [base] + [f"{base}.{alias.name}" for alias in node.names]
        for target in targets:
            parts = target.split(".")
            names.update(".".join(parts[: i + 1]) for i in range(len(parts)))
    return names


def _import_closure(root: Path) -> list[str]:
    """Paths relative to `root` of this script and every repository module it transitively imported.

    Method: the repo-local entries of `sys.modules` (files under `PRODUCER_DIRS`) are the modules
    actually loaded. Starting from this script, follow each file's import statements to the loaded
    modules they name. Following imports, rather than taking every repo-local entry, keeps the list
    independent of what else the process loaded (a test session imports many unrelated modules), so
    the digest depends only on code that produced the artifacts. An optional import that failed
    (`sponsors` without pandas, `src.condition_classifier` without rapidfuzz) is not loaded and so
    not hashed; the code path that ran instead is in `feasibility/benchmarks.py`, which is.
    """
    loaded = _loaded_repo_modules(root)
    entry = Path(__file__).resolve()
    seen: set[Path] = set()
    pending: list[tuple[Path, str | None]] = [(entry, __package__)]
    while pending:
        path, package = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        for name in _imported_module_names(path, package):
            dep = loaded.get(name)
            if dep is not None and dep not in seen:
                pending.append((dep, getattr(sys.modules[name], "__package__", None)))
    # relative_to raises if `root` is not the checkout this script was imported from.
    return [p.relative_to(root.resolve()).as_posix() for p in seen]


def producer_paths(root: Path = ROOT) -> list[str]:
    """Sorted repository-relative paths of every file that determines the artifacts.

    The union of: every file under `feasibility/` (code, and the default curated
    `termination_reviews.csv`); `FAMILY_SCRIPTS`; `PRODUCER_DATA_FILES`; and the build's import
    closure (`_import_closure`), which brings in `src/full_records.py`, `src/condition_mesh.py`,
    `src/condition_classifier.py` and the `sponsors/` attribution modules. `root` must be the
    checkout this script was imported from. Call it after the build has run, so modules imported at
    run time are loaded.
    """
    pkg_files = [
        str(p.relative_to(root).as_posix())
        for p in (root / "feasibility").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    ]
    present_scripts = [s for s in FAMILY_SCRIPTS if (root / s).is_file()]
    return sorted(
        set(pkg_files) | set(present_scripts) | set(PRODUCER_DATA_FILES) | set(_import_closure(root))
    )


def source_digest(root: Path = ROOT, paths: list[str] | None = None) -> str:
    """SHA-256 over the producing files (default `producer_paths(root)`) read from `root`, path by path."""
    digest = hashlib.sha256()
    for rel in producer_paths(root) if paths is None else paths:
        digest.update(rel.encode("utf-8") + b"\0" + (root / rel).read_bytes() + b"\0")
    return digest.hexdigest()


def producer_metadata(root: Path = ROOT) -> dict[str, Any]:
    """Git revision, dirty state (when git is accessible), and deterministic code digest.

    The digest and the scoped `git status` dirty check cover the same list, `producer_paths(root)`.
    """
    paths = producer_paths(root)
    commit: str | None = None
    dirty: bool | None = None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            commit = proc.stdout.strip()
            st = subprocess.run(
                ["git", "status", "--porcelain", "--", *paths],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            if st.returncode == 0:
                dirty = bool(st.stdout.strip())
    except OSError:
        commit = None
        dirty = None

    return {
        "repository": "civicsample-engine",
        "commit": commit,
        "uncommitted_changes": dirty,
        "source_sha256": source_digest(root, paths),
    }


_NCT_ID_RE = re.compile(r"NCT[0-9]{8}")  # used with fullmatch; ASCII digits only (`\d` matches any script's)

# CT.gov API v2's phase values (the `Phase` enum). `metrics.normalize_phase` keeps any other token as
# a stratum of its own (`PHASE_3` beside `PHASE3`), so raw phases and weekly phase strings must hold
# only these. A value CT.gov adds later surfaces as a named refusal and needs adding here.
CTGOV_PHASES = frozenset({"NA", "EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4"})
# CT.gov's overall status values. `extract_trial_metrics` and the termination layer group trials by
# exact value, so any other string (`"terminated"`) fell outside every group: a stopped trial left the
# stopped and resolved benchmarks with nothing reported. The 18,973 distinct trials in the stored local
# records hold the first 13 (the 80,890 weekly records 6 of them); TEMPORARILY_NOT_AVAILABLE, an
# expanded-access status, is in no stored record and is kept so that a real one is not refused.
CTGOV_OVERALL_STATUSES = frozenset({
    "ACTIVE_NOT_RECRUITING", "APPROVED_FOR_MARKETING", "AVAILABLE", "COMPLETED", "ENROLLING_BY_INVITATION",
    "NO_LONGER_AVAILABLE", "NOT_YET_RECRUITING", "RECRUITING", "SUSPENDED", "TERMINATED", "UNKNOWN",
    "WITHDRAWN", "WITHHELD", "TEMPORARILY_NOT_AVAILABLE",
})
# Enrollment types `metrics.normalize_enrollment` reads (ANTICIPATED is the legacy name of ESTIMATED);
# any other value became `MISSING_TYPE` and left the trial out of the enrollment and facility metrics.
# The stored records hold ACTUAL and ESTIMATED; the weekly records also carry `src/utils.py`'s "N/A"
# for a record with no type (4 of 80,890).
CTGOV_ENROLLMENT_TYPES = frozenset({"ACTUAL", "ESTIMATED", "ANTICIPATED"})
WEEKLY_ENROLLMENT_TYPES = CTGOV_ENROLLMENT_TYPES | {"N/A"}


def _study_nct_id(study: dict[str, Any]) -> str | None:
    """NCT id of a CT.gov API v2 study (`protocolSection.identificationModule.nctId`) or weekly record."""
    if isinstance(study.get("nct_id"), str):
        return study["nct_id"]
    ps = study.get("protocolSection")
    ident = ps.get("identificationModule") if isinstance(ps, dict) else None
    nct = ident.get("nctId") if isinstance(ident, dict) else None
    return nct if isinstance(nct, str) else None


def _study_objects(items: list[Any], where: str) -> list[dict[str, Any]]:
    """The entries of a `studies` batch, refusing the batch if any entry is not a study object.

    Dropping a `null`, string or list entry and keeping the rest would shrink the cohort and every
    denominator below the input batch with nothing said, so one malformed entry refuses the batch.
    """
    bad = [i for i, s in enumerate(items) if not isinstance(s, dict)]
    if bad:
        raise ValueError(
            f"{where}: `studies` batch holds {len(bad)} entr{'y' if len(bad) == 1 else 'ies'} that "
            f"are not study objects (first at index {bad[0]}: {type(items[bad[0]]).__name__})"
        )
    return items


# Protocol modules ClinicalTrials.gov requires on every registration (identification, overall
# status, sponsor, conditions, study type), so a complete API v2 record always carries them. A record
# without one was fetched with a `fields=` projection (or truncated): building from it would silently
# read the missing modules as "no conditions" (indication Other), "no sponsor" (MISSING_CLASS) and so
# on, and the contract would still pass. Absence of these modules is therefore refused, not
# benchmarked. (`contactsLocationsModule` and `derivedSection` are legitimately absent on some
# registrations and are not required here.)
REQUIRED_PROTOCOL_MODULES: tuple[str, ...] = (
    "identificationModule",
    "statusModule",
    "sponsorCollaboratorsModule",
    "conditionsModule",
    "designModule",
)

# Fields CT.gov holds on every registration, inside modules a projection can keep as shells: the brief
# title, the overall status, the record-verification date ("Record Verification Date", a required
# data element) and the study type. A field projection that names a module's fields one by one
# (`fields=NCTId,OverallStatus,LeadSponsorName,Condition,StudyType`) still yields all five module
# objects but drops most of these, so a record without any of them is refused as projected. Among the
# stored CT.gov caches, the device-linkage projections lack `statusVerifiedDate` and the outcomes
# second-pass projection lacks `briefTitle`; the unprojected records carry all four. This cannot
# catch a projection that requests whole modules and omits others entirely (contactsLocationsModule,
# derivedSection): such an absence is indistinguishable from a registration without them. The
# payloads of the conditions and sponsor modules are checked separately (below).
REQUIRED_RECORD_FIELDS: tuple[tuple[str, str], ...] = (
    ("identificationModule", "briefTitle"),
    ("statusModule", "overallStatus"),
    ("statusModule", "statusVerifiedDate"),
    ("designModule", "studyType"),
)

# The conditions and lead-sponsor payloads. CT.gov requires every registration to list at least one
# condition and a lead sponsor, and the build reads both: the conditions place a trial in its
# indication, and the lead sponsor's class is its `by_sponsor_class` stratum and its name the
# sponsor attribution. `feasibility.metrics.extract_trial_metrics` and `build_trial_records` read a
# gap as no conditions (indication Other), a lead sponsor "Unknown" (unreviewed) and the class
# `MISSING_CLASS`; a `conditions` string is read character by character, and a `leadSponsor` that is
# not an object raises. So a module kept without its payload (`"conditionsModule": {}`, `conditions`
# that is not a list, a `leadSponsor` without a `name`) is refused like a missing module:
# `conditionsModule.conditions` must be a non-empty list of non-blank strings, and
# `sponsorCollaboratorsModule.leadSponsor` an object whose `REQUIRED_LEAD_SPONSOR_FIELDS` are
# non-blank strings. The class is required on evidence: the unprojected stored records carry one,
# and none of the 80,890 trials in the weekly full records extracted 2026-11-08 fell back to the
# extractor's "Unknown" class, while the device-linkage projection that did not request
# `LeadSponsorClass` kept `leadSponsor` with its name alone. In the 2026-06-19 AACT sponsors fixture
# all 976 lead rows without a class are named "[Redacted]"; the fixture holds no study status, so
# that they are withheld trials is inferred, not checked. The one such record cached from the API
# (NCT04060524, `WITHHELD`, in the device-linkage acquisition cache) came back without a study type
# either, so it was refused already. Same rule as the DAP benchmarks (#106).
REQUIRED_LEAD_SPONSOR_FIELDS: tuple[str, ...] = ("name", "class")


def _missing_condition_and_sponsor_payloads(ps: dict[str, Any]) -> list[str]:
    """Name the conditions and lead-sponsor payloads the protocol section `ps` lacks (see the note
    above).

    A module that is absent or not an object is reported by the module check, not again here. A
    `conditions` list with any blank or non-string entry is refused whole: a registration never holds
    one, and keeping the rest would hide the malformation.
    """
    missing: list[str] = []
    cond_mod = ps.get("conditionsModule")
    if isinstance(cond_mod, dict):
        conditions = cond_mod.get("conditions")
        if not (
            isinstance(conditions, list)
            and conditions
            and all(isinstance(c, str) and c.strip() for c in conditions)
        ):
            missing.append("conditionsModule.conditions")
    sponsor_mod = ps.get("sponsorCollaboratorsModule")
    if isinstance(sponsor_mod, dict):
        lead = sponsor_mod.get("leadSponsor")
        if not isinstance(lead, dict):
            missing.append("sponsorCollaboratorsModule.leadSponsor")
        else:
            missing += [
                f"sponsorCollaboratorsModule.leadSponsor.{field}"
                for field in REQUIRED_LEAD_SPONSOR_FIELDS
                if not (isinstance(lead.get(field), str) and lead[field].strip())
            ]
    return missing


# Parts the build reads that are optional on a registration but must have their API v2 type when
# present: `(module, field, expected type)`, with `module` None for a field of `protocolSection`.
# `feasibility.metrics.extract_trial_metrics` reads them with `.get` or iterates them, so a list
# date struct or a string `locations` raised there and one record stopped the whole build. A part
# of the wrong type is refused by name, like a missing module, rather than read as absent.
TYPED_OPTIONAL_PARTS: tuple[tuple[str | None, str, type], ...] = (
    ("statusModule", "startDateStruct", dict),
    ("statusModule", "primaryCompletionDateStruct", dict),
    ("statusModule", "completionDateStruct", dict),
    ("designModule", "enrollmentInfo", dict),
    ("sponsorCollaboratorsModule", "collaborators", list),
    (None, "contactsLocationsModule", dict),
    ("contactsLocationsModule", "locations", list),
)


# A date struct's `type`, when present, must be one of these. `metrics.compute_duration` excludes
# ESTIMATED and ANTICIPATED windows and reads any other value it cannot clean (a number, `null`,
# `"BANANA"`) as no type, which an API v2 window then treats like an untyped date. A type that is
# absent is a real registry state, not refused: 955 of the 3,371 distinct trials in the stored
# unprojected CT.gov records carry a start date with no type (the other values are ACTUAL and
# ESTIMATED only), and how an untyped window should count is left to the owner.
DATE_STRUCT_TYPES = frozenset({"ACTUAL", "ESTIMATED", "ANTICIPATED"})
DATE_STRUCTS = ("startDateStruct", "primaryCompletionDateStruct", "completionDateStruct")


# Lists among `TYPED_OPTIONAL_PARTS` whose entries the build reads: every entry must be an object
# and the named fields strings when present. `extract_trial_metrics` skipped a non-object entry, so a
# `null` among the locations lowered `n_listed_facilities` (and the footprint tier and
# participants-per-facility built on it) with nothing reported; a skipped collaborator dropped out of
# the sponsor attribution the same way.
LIST_ENTRY_FIELDS: dict[tuple[str, str], tuple[str, ...]] = {
    ("sponsorCollaboratorsModule", "collaborators"): ("name", "class"),
    ("contactsLocationsModule", "locations"): ("country",),
}


def _mistyped_optional_parts(ps: dict[str, Any]) -> list[str]:
    """Name the `TYPED_OPTIONAL_PARTS` present in the protocol section `ps` with the wrong type, the
    malformed entries of the `LIST_ENTRY_FIELDS` lists, and phases that are not a list of non-blank
    strings. A part is present when its key is, so an explicit null is refused like any other wrong
    type (the 5,330 stored unprojected records hold no explicit null anywhere)."""
    out: list[str] = []
    status_mod = ps.get("statusModule")
    if isinstance(status_mod, dict) and isinstance(status_mod.get("overallStatus"), str):
        if status_mod["overallStatus"] not in CTGOV_OVERALL_STATUSES:
            out.append("statusModule.overallStatus (a CT.gov status value)")
    # `extract_trial_metrics` turned a non-string `whyStopped` into None, so `["Low recruitment"]` was
    # published as `missing_why_stopped`. All 459 stored `whyStopped` values are strings.
    if isinstance(status_mod, dict) and "whyStopped" in status_mod and not isinstance(status_mod["whyStopped"], str):
        out.append("statusModule.whyStopped (a string)")
    design = ps.get("designModule")
    enrollment = design.get("enrollmentInfo") if isinstance(design, dict) else None
    if isinstance(enrollment, dict) and "type" in enrollment:
        # checked as a string first: a list or object is unhashable
        if not (isinstance(enrollment["type"], str) and enrollment["type"] in CTGOV_ENROLLMENT_TYPES):
            out.append("designModule.enrollmentInfo.type (ACTUAL, ESTIMATED or ANTICIPATED)")
    if isinstance(design, dict) and "phases" in design:
        # normalize_phase turned every entry into text with str(), so [7] or [null] became a phase
        # stratum of its own, and kept any other string (`PHASE_3`) as one too; the stored records
        # hold only CT.gov's six phase values (`CTGOV_PHASES`).
        phases = design["phases"]
        if not (isinstance(phases, list) and all(isinstance(p, str) and p in CTGOV_PHASES for p in phases)):
            out.append("designModule.phases (a list of CT.gov phase values)")
    for mod, field, kind in TYPED_OPTIONAL_PARTS:
        holder = ps if mod is None else ps.get(mod)
        if not isinstance(holder, dict) or field not in holder:
            continue
        where = field if mod is None else f"{mod}.{field}"
        if not isinstance(holder[field], kind):
            out.append(f"{where} ({'an object' if kind is dict else 'a list'})")
            continue
        if field in DATE_STRUCTS and "type" in holder[field]:
            date_type = holder[field]["type"]  # checked as a string first: a list or object is unhashable
            if not (isinstance(date_type, str) and date_type in DATE_STRUCT_TYPES):
                out.append(f"{where}.type (ACTUAL, ESTIMATED or ANTICIPATED)")
        entry_fields = LIST_ENTRY_FIELDS.get((mod, field)) if mod is not None else None
        if entry_fields is None:
            continue
        for i, entry in enumerate(holder[field]):
            if not isinstance(entry, dict):
                out.append(f"{where}[{i}] (an object)")
                continue
            # Key presence, not a non-null value: an explicit null read as a facility without a
            # country or a collaborator without a name.
            out += [
                f"{where}[{i}].{name} (a string)"
                for name in entry_fields
                if name in entry and not isinstance(entry[name], str)
            ]
            # A collaborator without a name was read as "" and dropped by the sponsor adapter, so a
            # company's collaborator and any-role counts fell with nothing reported. All 61,241
            # collaborator entries in the stored local files carry a non-blank name.
            # (A name of another type is reported just above.)
            name = entry.get("name")
            if (mod, field) == ("sponsorCollaboratorsModule", "collaborators") and (
                "name" not in entry or (isinstance(name, str) and not name.strip())
            ):
                out.append(f"{where}[{i}].name (a non-blank string)")
    return out


def _require_complete_records(studies: list[dict[str, Any]], source: str) -> None:
    """Refuse API v2 study records missing any `REQUIRED_PROTOCOL_MODULES` (see the notes above),
    any `REQUIRED_RECORD_FIELDS` inside them, their conditions or their lead sponsor's
    `REQUIRED_LEAD_SPONSOR_FIELDS`, any of the `TYPED_OPTIONAL_PARTS` present with the wrong type,
    or whose results part is incomplete.

    A complete API v2 record always carries the top-level boolean `hasResults`, and a record with
    `hasResults: true` carries its posted `resultsSection`. A record without the flag, with a
    non-boolean one, or with `hasResults: true` but no (or an empty) `resultsSection` was projected
    or truncated (a direct `--records-dir`/`--archive` study object has no request URL to show a
    `fields=` projection). Built from it, the stopped-study results state would read
    `results_not_posted` and retention `no_participant_flow_module` while the contract passed, so it
    is refused like a missing protocol module.
    """
    incomplete: dict[str, list[str]] = {}
    for idx, study in enumerate(studies):
        ps = study.get("protocolSection")
        if not isinstance(ps, dict):
            incomplete[_study_nct_id(study) or f"index {idx}"] = ["protocolSection"]
            continue
        missing = [m for m in REQUIRED_PROTOCOL_MODULES if not isinstance(ps.get(m), dict)]
        missing += [
            f"{mod}.{field}" for mod, field in REQUIRED_RECORD_FIELDS
            if isinstance(ps.get(mod), dict)
            and not (isinstance(ps[mod].get(field), str) and ps[mod][field].strip())
        ]
        missing += _missing_condition_and_sponsor_payloads(ps)
        missing += _mistyped_optional_parts(ps)
        has_results = study.get("hasResults")
        if not isinstance(has_results, bool):
            missing.append("hasResults (a boolean)")
        elif has_results:
            results_sec = study.get("resultsSection")
            if not isinstance(results_sec, dict) or not results_sec:
                missing.append("resultsSection (hasResults is true)")
        elif study.get("resultsSection"):
            # The flag says no results were posted, yet a section is present: the result readers
            # disagree on it (one trusts the flag, another reads the section), so the record is
            # contradictory and refused. None of the 5,330 stored unprojected records has this shape.
            missing.append("resultsSection (hasResults is false)")
        if missing:
            incomplete[_study_nct_id(study) or f"index {idx}"] = missing
    if incomplete:
        first = next(iter(incomplete))
        raise ValueError(
            f"{source}: {len(incomplete)} of {len(studies)} study records lack required protocol modules "
            f"or results parts (first: {first} lacks {', '.join(incomplete[first])}); these look field-projected or "
            "truncated, so the build is refused rather than reading the gaps as absent data"
        )


def _requested_nct_ids(envelope: dict[str, Any]) -> list[str] | None:
    """NCT ids a cached request asked for: its `ids` field, else the `url`'s `filter.ids` or its
    single-study path (`/studies/NCT########`)."""
    ids = envelope.get("ids")
    if isinstance(ids, list) and ids and all(isinstance(i, str) for i in ids):
        return ids
    url = envelope.get("url")
    if isinstance(url, str):
        parts = urlsplit(url)
        values = parse_qs(parts.query).get("filter.ids")
        if values:
            return [i for v in values for i in v.split(",") if i]
        last = parts.path.rstrip("/").rsplit("/", 1)[-1]
        if _NCT_ID_RE.fullmatch(last):
            return [last]
    return None


def _response_envelope_studies(obj: dict[str, Any], where: str) -> list[dict[str, Any]]:
    """Studies from one cached HTTP response envelope, refusing a failed, altered or short one.

    Method: the cached-response formats in this repository
    (`docs/audits/outcomes_results_structure/first_pass/scripts/fetch.py`, `.../second_pass/scripts/
    ctgov.py`) write one line per request with its `status`, `body_sha256` and unparsed `body`;
    the first also records the requested `ids`, and both put them in the `url` as `filter.ids`. A line
    whose request failed, whose body was altered, or that answered fewer or other trials than it
    asked for would otherwise contribute fewer studies with nothing said, and the build would publish
    benchmarks whose cohort and denominators silently lack those trials. So `status` must be 200,
    the body must be text and `body_sha256` a 64-hex hash matching it, the body must hold at least one study,
    and the studies' NCT ids must equal the requested ids exactly: none missing, extra or repeated.
    An envelope that names no requested ids is refused, since a short answer could not be detected.
    `nextPageToken` is not consulted: CT.gov returns one on a full page even when `filter.ids` has
    been answered completely (all 40 responses in the first-pass cache carry one, each with its 50
    requested trials).
    """
    status = obj.get("status")
    if status != 200:
        raise ValueError(
            f"{where}: cached response status {status!r}, not 200; a failed request is not an empty batch"
        )
    url = obj.get("url")
    projected = parse_qs(urlsplit(url).query).get("fields") if isinstance(url, str) else None
    if projected:
        raise ValueError(
            f"{where}: response was requested with fields={','.join(projected)}; a field-projected record "
            "lacks the modules these benchmarks read (conditions, sponsors, locations), and would publish "
            "every trial as Other / no sites / no sponsor class. Use complete study records."
        )
    # Both cached-response formats store the unparsed body text with its sha256, so both are required: a
    # line without the hash, or with a parsed body, was read without showing that its records are the
    # cached response. Every line of the four outcomes-audit caches carries a text body and its matching
    # hash. (A line with no `body` key is not taken for an envelope and is refused by the loader.)
    body = obj.get("body")
    want_sha = obj.get("body_sha256")
    if not isinstance(body, str):
        raise ValueError(f"{where}: response body is {type(body).__name__}, not the unparsed response text")
    if not (isinstance(want_sha, str) and re.fullmatch(r"[0-9a-f]{64}", want_sha)):
        raise ValueError(f"{where}: body_sha256 {want_sha!r} is not a 64-hex sha256 of the response body")
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != want_sha:
        raise ValueError(f"{where}: response body does not match its body_sha256")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError as exc:
            raise ValueError(f"{where}: response body is not JSON") from exc
    studies: list[dict[str, Any]] = []
    if isinstance(body, dict) and "protocolSection" in body:
        studies = [body]
    elif isinstance(body, dict) and isinstance(body.get("studies"), list):
        studies = _study_objects(body["studies"], where)
    if not studies:
        raise ValueError(f"{where}: response envelope holds zero studies")

    requested = _requested_nct_ids(obj)
    if requested is None:
        raise ValueError(
            f"{where}: response envelope names no requested NCT ids (no `ids`, no `filter.ids` in "
            "`url`), so a response missing trials could not be detected"
        )
    returned = [_study_nct_id(s) for s in studies]
    problems: list[str] = []
    if any(n is None or not _NCT_ID_RE.fullmatch(n) for n in returned):
        problems.append("a study without a valid NCT id")
    named = [n for n in returned if n is not None]
    if len(set(named)) != len(named):
        problems.append("repeated NCT ids")
    missing = sorted(set(requested) - set(named))
    extra = sorted(set(named) - set(requested))
    if missing:
        problems.append(f"{len(missing)} requested trial(s) missing (first: {', '.join(missing[:5])})")
    if extra:
        problems.append(f"{len(extra)} unrequested trial(s) (first: {', '.join(extra[:5])})")
    if problems:
        raise ValueError(
            f"{where}: response for {len(set(requested))} requested trials returned "
            f"{len(studies)} studies with {'; '.join(problems)}"
        )
    return studies


def _extract_studies_from_json_obj(obj: Any, where: str) -> list[dict[str, Any]]:
    """Unwrap one study or a batch of studies from direct CT.gov v2 JSON or cached envelopes.

    `where` names the file (and line) for error messages. A batch or envelope that yields zero
    studies raises `ValueError`: an archive line or records file holding no study is a failed or
    truncated fetch, not an empty part of the cohort.
    """
    if not isinstance(obj, dict):
        raise ValueError(f"{where}: expected JSON object for study record or envelope")
    if "protocolSection" in obj or "nct_id" in obj:
        return [obj]
    if isinstance(obj.get("study"), dict):
        return [obj["study"]]
    if "body" in obj:
        return _response_envelope_studies(obj, where)
    if isinstance(obj.get("studies"), list):
        studies = _study_objects(obj["studies"], where)
        if not studies:
            raise ValueError(f"{where}: `studies` batch holds zero study records")
        return studies
    raise ValueError(f"{where}: unrecognized study JSON shape with keys {sorted(obj.keys())[:8]}")


# `gzip.decompress` raises `gzip.BadGzipFile` (an `OSError`) for bytes that are not gzip, `EOFError` for a
# truncated file and `zlib.error` for corrupt data; `main` catches only `ValueError`, so such an input
# printed a traceback instead of REFUSED. Each input is decompressed through here.
_GZIP_ERRORS: tuple[type[BaseException], ...] = (OSError, EOFError, zlib.error)


def _decompressed(raw: bytes, name: str) -> bytes:
    """`raw`, gunzipped when `name` ends in `.gz`; data that is not whole gzip is refused by name."""
    if not name.endswith(".gz"):
        return raw
    try:
        return gzip.decompress(raw)
    except _GZIP_ERRORS as exc:
        raise ValueError(f"{name}: not readable gzip data ({type(exc).__name__}: {exc})") from exc


def load_studies_input(
    *,
    demographics_path: Path | None,
    records_dir: Path | None,
    archive_path: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load studies and return `(studies, records_input_metadata)`."""
    if records_dir is not None:
        if not records_dir.is_dir():
            raise ValueError(f"{records_dir}: --records-dir is not an existing directory")
        files = sorted(
            p
            for p in records_dir.iterdir()
            if p.is_file() and (p.name.endswith(".json") or p.name.endswith(".json.gz"))
        )
        if not files:
            raise ValueError(f"{records_dir}: holds no .json or .json.gz study files")
        dir_hash = hashlib.sha256()
        studies: list[dict[str, Any]] = []
        for fp in files:
            raw = fp.read_bytes()
            dir_hash.update(fp.name.encode("utf-8") + b"\0" + raw + b"\0")
            text = _decompressed(raw, fp.name).decode("utf-8")
            obj = json.loads(text)
            studies.extend(_extract_studies_from_json_obj(obj, fp.name))
        if not studies:
            raise ValueError(f"{records_dir}: decoded zero study records")
        _require_complete_records(studies, f"{records_dir}")
        return studies, {
            "mode": "records_dir",
            "source": records_dir.name,
            "n_files": len(files),
            "n_records": len(studies),
            "sha256": dir_hash.hexdigest(),
            "source_extracted_at": None,
            "source_pipeline_commit": None,
        }

    if archive_path is not None:
        if not archive_path.is_file():
            raise ValueError(f"{archive_path}: --archive file does not exist")
        if _PART_RE.search(archive_path.name):
            raise ValueError(
                f"{archive_path}: site-projected demographics.part*.json.gz is not a full-record archive"
            )
        raw_bytes = archive_path.read_bytes()
        file_sha = hashlib.sha256(raw_bytes).hexdigest()
        uncompressed = _decompressed(raw_bytes, archive_path.name)
        studies = []
        # Split strictly on ASCII LF (0x0A), never str.splitlines(), so U+2028 / U+2029 / \x85
        # inside ensure_ascii=False JSON string values are never split mid-string.
        for line_no, raw_line in enumerate(uncompressed.split(b"\n"), start=1):
            if not raw_line.strip():
                continue
            try:
                obj = json.loads(raw_line.rstrip(b"\r").decode("utf-8"))
            except ValueError as exc:
                raise ValueError(f"{archive_path.name}:{line_no}: invalid JSON line") from exc
            studies.extend(_extract_studies_from_json_obj(obj, f"{archive_path.name}:{line_no}"))
        if not studies:
            raise ValueError(f"{archive_path}: archive holds zero study records")
        _require_complete_records(studies, archive_path.name)
        return studies, {
            "mode": "jsonl_archive",
            "source": archive_path.name,
            "n_records": len(studies),
            "sha256": file_sha,
            "source_extracted_at": None,
            "source_pipeline_commit": None,
        }

    target = demographics_path or Path(full_records.DEFAULT_PATH)
    if _PART_RE.search(target.name):
        raise ValueError(
            f"{target}: refusing site-projected demographics.part*.json.gz; pass full records instead"
        )
    try:
        records, extracted_at, pipe_commit = full_records.load(target)
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc
    except _GZIP_ERRORS as exc:
        # `full_records.load` reads through `gzip.open`, which raises the same errors as it reads.
        raise ValueError(f"{target.name}: not readable gzip data ({type(exc).__name__}: {exc})") from exc
    # The weekly container's run stamps are the only record of which extraction run, and which
    # pipeline revision, produced these records; the build and its site export carry them as
    # `source_extracted_at` / `source_pipeline_commit`. A container without both is refused whether
    # or not a MeSH sidecar is supplied (the sidecar check needs them too).
    missing = [
        name for name, value in (("extracted_at", extracted_at), ("pipeline_commit", pipe_commit))
        if value is None or (isinstance(value, str) and not value.strip())
    ]
    if missing:
        raise ValueError(
            f"{target}: weekly full records carry no {' or '.join(missing)}, so the build could not "
            "name the extraction run that produced them"
        )
    # Present is not enough: the stamps must have the producer's format (a whole UTC timestamp and
    # a 40-hex revision; see artifacts.weekly_source_stamp_problems), or they name no run.
    malformed = artifacts.weekly_source_stamp_problems(extracted_at, pipe_commit)
    if malformed:
        raise ValueError(
            f"{target}: weekly full-record run stamps cannot name the extraction run that produced "
            f"them ({'; '.join(malformed)})"
        )
    _require_weekly_phases(records, str(target))
    _require_weekly_lists(records, str(target))
    file_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    return records, {
        "mode": "weekly_full_records",
        "source": target.name,
        "n_records": len(records),
        "sha256": file_sha,
        "source_extracted_at": extracted_at,
        "source_pipeline_commit": pipe_commit,
    }


def _require_weekly_phases(records: list[dict[str, Any]], source: str) -> None:
    """Refuse weekly full records whose `phase` string holds anything but CT.gov's phase values.

    Weekly records never pass the raw-record checks, so a `"phase": "BANANA"` (or a non-string) would
    become a stratum of its own. The value must be exactly `""` (no phase stated) or, split on `,` and
    `/` as `metrics.normalize_phase` splits it (`"PHASE1, PHASE2"`), consist only of `CTGOV_PHASES`
    values: an empty piece (`","`, `"PHASE2,"`, `" "`) is refused too, since `normalize_phase` drops
    it and would read a separator-only value as no phase. A missing key is refused by
    `_require_weekly_lists` (`src/utils.py` writes `phase` on every record). The 80,890 records of the
    2026-11-08 weekly extraction hold only the six values, two multi-phase forms and `""`.
    """
    bad: list[str] = []
    for idx, rec in enumerate(records):
        if not isinstance(rec, dict) or "phase" not in rec:
            continue
        phase = rec["phase"]
        if not isinstance(phase, str):
            pieces = None
        else:
            pieces = [] if phase == "" else [t.strip() for t in re.split(r"[,/]", phase)]
        if pieces is None or any(t not in CTGOV_PHASES for t in pieces):
            bad.append(f"{rec.get('nct_id') or f'index {idx}'} has phase {phase!r}")
    if bad:
        raise ValueError(
            f"{source}: {len(bad)} of {len(records)} weekly records carry a phase outside CT.gov's values "
            f"(first: {bad[0]})"
        )


# The weekly list fields `metrics.extract_trial_row` reads, the type of their entries, and the entry
# fields it reads (each a string when present). `src/utils.py` writes all four as lists.
WEEKLY_LIST_FIELDS: dict[str, tuple[type, tuple[str, ...]]] = {
    "study_sites": (dict, ("country",)),
    "countries": (dict, ("country",)),
    "collaborators": (dict, ("name", "class")),
    "conditions": (str, ()),
}
# Entry fields `src/utils.py` always writes as non-blank strings: it keeps a site, and a country entry,
# only for a location with a country, so a blank one would be counted as a facility no source lists.
WEEKLY_REQUIRED_ENTRY_FIELDS: dict[str, tuple[str, ...]] = {
    "study_sites": ("country",),
    "countries": ("country",),
    # An unnamed collaborator dropped out of sponsor attribution; all 48,077 weekly ones carry a name.
    "collaborators": ("name",),
}
# The scalar weekly fields the build reads, the type `src/utils.py` writes for each, and whether it is
# never blank (dates and `why_stopped` are "" when CT.gov has none). A missing or mistyped one was read
# as absent: an "Unknown" sponsor, no enrollment, no dates. `primary_condition`/`secondary_condition`
# are left out on purpose: when either is missing or blank the build classifies `conditions` itself.
WEEKLY_SCALAR_FIELDS: dict[str, tuple[type, bool]] = {
    "nct_id": (str, True),
    "phase": (str, False),  # its values are checked by `_require_weekly_phases`
    "brief_title": (str, False),
    "study_type": (str, True),
    "status": (str, True),
    "why_stopped": (str, False),
    "start_date": (str, False),
    "primary_completion_date": (str, False),
    "completion_date": (str, False),
    "results_date": (str, False),  # "" or a date, checked in `_weekly_list_problem`
    "enrollment": (int, False),
    "enrollment_type": (str, False),
    "lead_sponsor_name": (str, True),
    "sponsor_class": (str, True),
}


def _weekly_list_problem(rec: dict[str, Any]) -> str | None:
    """Why one weekly record cannot be read whole, or `None`.

    Scalars: every key in `WEEKLY_SCALAR_FIELDS` must be present with its type (a bool is not an
    enrollment), and non-blank where the producer never writes it blank. `results_date` must be
    exactly "" or a date `metrics.parse_date_bounds` reads: the weekly projection carries no
    `resultsSection`, so `termination.classify_stopped_study_results_state` and the flow status read
    any non-empty value as posted results, and `"N/A"`, `" "` or `"2024-02-30"` moved a stopped trial
    from `results_not_posted` to `results_posted_no_primary_denom`. Lists, as follows.

    `extract_trial_row` keeps only object sites, countries and collaborators and only string
    conditions, so a `null` or mistyped entry would drop out: fewer listed facilities, another
    footprint tier and more participants per facility, with the build still passing its contract. A
    key must be present (`src/utils.py` always writes all four, so a missing one, read as an empty
    list, would turn a sited trial into one with no listed sites) and hold a list, explicit `null`
    included, whose entries have their type, with the fields read strings when present.
    `conditions` must be non-empty, as on the raw path, since an empty list reads as indication
    Other. All 80,890 records of the 2026-11-08 weekly extraction carry the four keys as lists of such
    entries (1,485,097 sites, 194,168 countries, 48,077 collaborators, 142,169 conditions), none with
    an empty `conditions`, and every site and country entry carries a non-blank `country`; all 80,890
    carry every scalar field with its type (no blank status, study type, sponsor name or class), and
    every `results_date` is a full date (the pull holds only trials with posted results).
    """
    for key, (scalar_type, non_blank) in WEEKLY_SCALAR_FIELDS.items():
        if key not in rec:
            return f"has no {key} (src/utils.py always writes it)"
        value = rec[key]
        if not isinstance(value, scalar_type) or (isinstance(value, bool) and scalar_type is int):
            return f"{key} is {type(value).__name__}, not {scalar_type.__name__}"
        if non_blank and not value.strip():
            return f"{key} is blank"
    if rec["status"] not in CTGOV_OVERALL_STATUSES:
        return f"status {rec['status']!r} is not a CT.gov status value"
    if rec["enrollment_type"] not in WEEKLY_ENROLLMENT_TYPES:
        return f"enrollment_type {rec['enrollment_type']!r} is not ACTUAL, ESTIMATED, ANTICIPATED or N/A"
    results_date = rec["results_date"]
    if results_date != "" and metrics.parse_date_bounds(results_date)["state"] != "ok":
        return f"results_date {results_date!r} is neither \"\" nor a date (any other value reads as posted results)"
    # The cached indication labels are optional (the build classifies `conditions` when either is missing
    # or blank), but `src/utils.py` writes both as strings, and the build uses them whenever both are
    # non-blank, so a pair the classifier cannot give was published as an indication stratum. A present
    # label must be a string, and a pair of non-blank labels one of `benchmarks.indication_pairs()`.
    labels = [rec.get(key) for key in ("primary_condition", "secondary_condition")]
    for key in ("primary_condition", "secondary_condition"):
        if key in rec and not isinstance(rec[key], str):
            return f"{key} is {type(rec[key]).__name__}, not str"
    if all(isinstance(label, str) and label.strip() for label in labels):
        pair = (labels[0].strip(), labels[1].strip())
        if pair not in benchmarks.indication_pairs():
            return (
                f"primary_condition/secondary_condition {pair!r} is not a condition_ontology.json category "
                f"nor the classifier's {benchmarks.UNCLASSIFIED_INDICATION!r}"
            )
    for key, (entry_type, fields) in WEEKLY_LIST_FIELDS.items():
        if key not in rec:
            return f"has no {key} (src/utils.py always writes it)"
        value = rec[key]
        if not isinstance(value, list):
            return f"{key} is {type(value).__name__}, not a list"
        if entry_type is str and not value:
            return f"{key} is empty"
        for i, entry in enumerate(value):
            if not isinstance(entry, entry_type):
                return f"{key}[{i}] is {type(entry).__name__}, not {'an object' if entry_type is dict else 'a string'}"
            if entry_type is str and not entry.strip():
                return f"{key}[{i}] is blank"
            for field in fields:
                if field in entry and not isinstance(entry[field], str):
                    return f"{key}[{i}].{field} is {type(entry[field]).__name__}, not a string"
            for field in WEEKLY_REQUIRED_ENTRY_FIELDS.get(key, ()):
                if not (isinstance(entry.get(field), str) and entry[field].strip()):
                    why = (
                        "an unnamed collaborator drops out of sponsor attribution" if key == "collaborators"
                        else "src/utils.py keeps only sites with one"
                    )
                    return f"{key}[{i}].{field} is missing or blank ({why})"
    # `src/utils.py` derives both lists from the same locations: `countries` is the set of their
    # non-blank countries, and `study_sites` keeps exactly the locations with one. `extract_trial_metrics`
    # reads `countries` whenever it is non-empty, so a partial list published a lower `n_countries`
    # than the sites show (on 76c9d39, sites in two countries beside `countries: [United States]`
    # published 1, and the contract passed). In all 80,890 records of the 2026-11-08 extraction the two
    # country sets are equal (4,375 records have neither).
    listed = {entry["country"] for entry in rec["countries"]}
    at_sites = {entry["country"] for entry in rec["study_sites"]}
    if listed != at_sites:
        return (
            f"countries {sorted(listed)} differ from the study_sites countries {sorted(at_sites)} "
            "(src/utils.py derives both from the same locations)"
        )
    return None


def _require_weekly_lists(records: list[dict[str, Any]], source: str) -> None:
    """Refuse weekly full records missing a field the build reads, or holding a malformed one."""
    bad = [
        f"{rec.get('nct_id') or f'index {idx}'} {problem}"
        for idx, rec in enumerate(records)
        if isinstance(rec, dict) and (problem := _weekly_list_problem(rec))
    ]
    if bad:
        raise ValueError(
            f"{source}: {len(bad)} of {len(records)} weekly records carry a missing or malformed field "
            f"(first: {bad[0]})"
        )


def condition_mesh_line_problem(row: dict[str, Any]) -> str | None:
    """Why one sidecar line breaks the `src/condition_mesh.py` line contract, or `None`.

    The contract is what `select_condition_mesh` writes: `schema_version` is
    `cmesh.SCHEMA_VERSION`; `state` is one of `cmesh.STATES`; `meshes` and `ancestors` are both
    present, each null or a list of `{"id": str, "term": str}` (`cmesh._terms`, the producer's own
    rule); and the state agrees with its payload:
      indexed              `meshes` holds at least one descriptor
      not_indexed          `meshes` is null or empty
      no_module, no_derived  `meshes` and `ancestors` are both null
      malformed            a non-empty `problem` string; either list may be null
    Only `malformed` carries `problem`. A line outside this contract (a newer producer's state, a
    descriptor list of the wrong shape) would be joined onto its trial and then fall outside the
    summary's state partition or contribute no descriptors to the MeSH strata.
    """
    if row.get("schema_version") != cmesh.SCHEMA_VERSION:
        return f"schema_version {row.get('schema_version')!r} is not {cmesh.SCHEMA_VERSION!r}"
    state = row.get("state")
    if state not in cmesh.STATES:
        return f"state {state!r} is not one of {list(cmesh.STATES)}"
    for key in ("meshes", "ancestors"):
        if key not in row:
            return f"no {key} key (null records an absent list)"
        if row[key] is not None and cmesh._terms(row[key]) is None:
            return f"{key} is neither null nor a list of id/term pairs"
    if state == "indexed" and not row["meshes"]:
        return "state indexed without at least one meshes descriptor"
    if state == "not_indexed" and row["meshes"]:
        return "state not_indexed with meshes descriptors"
    if state in ("no_module", "no_derived") and (row["meshes"] is not None or row["ancestors"] is not None):
        return f"state {state} with a meshes or ancestors list"
    if state == "malformed":
        if not isinstance(row.get("problem"), str) or not row["problem"].strip():
            return "state malformed without a problem description"
    elif "problem" in row:
        return f"state {state} carries a problem field"
    return None


def load_condition_mesh_sidecar(
    path: Path | None,
    *,
    records_meta: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]] | None, dict[str, Any] | None]:
    """Load optional `condition_mesh.jsonl.gz` sidecar keyed by `nct_id`, refusing another run's.

    Method: `src/extract_all.py` stamps every sidecar line with the same `extracted_at` and
    `pipeline_commit` it writes on that week's `demographics.json` container. When the records
    come from `--demographics` (`records_meta["mode"] == "weekly_full_records"`), both container
    stamps must be present and every line must carry exactly those values; otherwise the sidecar
    is from another week (or partly from one) and its descriptors would be joined onto the wrong
    registry state, so this raises `ValueError` naming the first mismatching line. The matched
    stamps are recorded in `run.json` (`inputs.condition_mesh`). Every line must also keep the
    producer's line contract (`condition_mesh_line_problem`). `--records-dir` and `--archive`
    inputs carry no run stamps to compare against, so only cohort coverage is required for them
    (`require_condition_mesh_coverage`), and `provenance_check` says which check ran.
    """
    if path is None:
        return None, None
    if not path.is_file():
        raise ValueError(f"{path}: --condition-mesh file does not exist")
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    uncompressed = _decompressed(raw, path.name)

    weekly = records_meta.get("mode") == "weekly_full_records"
    expected: dict[str, Any] = {
        "extracted_at": records_meta.get("source_extracted_at"),
        "pipeline_commit": records_meta.get("source_pipeline_commit"),
    }
    if weekly:
        absent = [k for k, v in expected.items() if not isinstance(v, str) or not v.strip()]
        if absent:
            raise ValueError(
                f"{records_meta.get('source')}: full records carry no {' or '.join(absent)}, so "
                f"--condition-mesh {path.name} cannot be matched to the run that wrote them"
            )

    by_nct: dict[str, dict[str, Any]] = {}
    for line_no, raw_line in enumerate(uncompressed.split(b"\n"), start=1):
        if not raw_line.strip():
            continue
        obj = json.loads(raw_line.rstrip(b"\r").decode("utf-8"))
        if not isinstance(obj, dict):
            # A JSON scalar or array line raised AttributeError at .get, a traceback rather than a refusal.
            raise ValueError(
                f"{path.name}:{line_no}: condition_mesh sidecar line is a JSON {type(obj).__name__}, not an object"
            )
        nct = obj.get("nct_id")
        if not isinstance(nct, str) or not nct.strip():
            raise ValueError(f"{path.name}:{line_no}: missing nct_id in condition_mesh sidecar")
        if nct in by_nct:
            raise ValueError(f"{path.name}:{line_no}: duplicate nct_id {nct} in condition_mesh sidecar")
        line_problem = condition_mesh_line_problem(obj)
        if line_problem is not None:
            raise ValueError(
                f"{path.name}:{line_no}: {nct}: condition_mesh line breaks the src/condition_mesh.py "
                f"contract: {line_problem}"
            )
        if weekly:
            for stamp, want in expected.items():
                if obj.get(stamp) != want:
                    raise ValueError(
                        f"{path.name}:{line_no}: {nct} {stamp} {obj.get(stamp)!r} != "
                        f"{records_meta.get('source')} {stamp} {want!r}; the condition_mesh sidecar "
                        "is not from the run that wrote these full records"
                    )
        by_nct[nct] = obj
    return by_nct, {
        "file": path.name,
        "n_rows": len(by_nct),
        "sha256": sha,
        "provenance_check": (
            "weekly_run_stamps_and_cohort_coverage" if weekly else "cohort_coverage_only"
        ),
        "extracted_at": expected["extracted_at"] if weekly else None,
        "pipeline_commit": expected["pipeline_commit"] if weekly else None,
    }


def require_condition_mesh_coverage(
    mesh_by_nct: dict[str, dict[str, Any]],
    cohort_nct_ids: list[str],
    sidecar_name: str,
) -> dict[str, int]:
    """Require the sidecar to hold a line for every cohort trial; return coverage counts.

    A cohort trial with no sidecar line would silently fall back to the record's own
    `derivedSection` (API v2) or to `condition_mesh.state = "not_provided"` (weekly records),
    mixing two sources or reading as "no MeSH" in the MeSH strata. Raises `ValueError` naming the
    count and the first missing NCT ids instead. Sidecar lines outside the cohort are harmless
    (they are never joined) and are counted in `n_rows_outside_cohort`.
    """
    cohort = set(cohort_nct_ids)
    missing = sorted(cohort - set(mesh_by_nct))
    if missing:
        raise ValueError(
            f"{sidecar_name}: condition_mesh sidecar has no line for {len(missing)} of "
            f"{len(cohort)} cohort trials (first: {', '.join(missing[:5])})"
        )
    return {
        "n_cohort_trials_covered": len(cohort),
        "n_rows_outside_cohort": len(set(mesh_by_nct) - cohort),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument(
        "--demographics",
        type=Path,
        help="Weekly full-record demographics.json or demographics.json.gz (via src/full_records.py).",
    )
    src.add_argument(
        "--records-dir",
        type=Path,
        help="Directory of full ClinicalTrials.gov API v2 .json or .json.gz study records.",
    )
    src.add_argument(
        "--archive",
        "--records-jsonl",
        dest="archive",
        type=Path,
        help="Frozen .jsonl or .jsonl.gz archive of ClinicalTrials.gov API v2 study records.",
    )
    ap.add_argument(
        "--condition-mesh",
        type=Path,
        help="Optional condition_mesh.jsonl.gz sidecar file; must cover every cohort trial and, with "
        "--demographics, carry that run's extracted_at and pipeline_commit on every line.",
    )
    ap.add_argument(
        "--reviews",
        type=Path,
        default=DEFAULT_REVIEWS_PATH,
        help="Curated human review override CSV (default: feasibility/termination_reviews.csv).",
    )
    ap.add_argument(
        "--rules",
        type=Path,
        default=DEFAULT_RULES_PATH,
        help="Curated sponsor alias rules CSV (default: sponsors/company_aliases.csv, read-only).",
    )
    ap.add_argument(
        "--min-cell-trials",
        type=int,
        default=benchmarks.DEFAULT_MIN_CELL_TRIALS,
        help="Minimum trials/eligible observations required before showing cell percentiles (default: 5).",
    )
    ap.add_argument(
        "--generated-at",
        help="Optional ISO-8601 UTC timestamp to pin for deterministic byte-identical rebuilds.",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_PATH,
        help="Output artifact directory (default: build/feasibility).",
    )
    args = ap.parse_args(argv)

    if args.min_cell_trials < 1:
        print("REFUSED: --min-cell-trials must be >= 1", file=sys.stderr)
        return 1

    try:
        reviews = termination.load_termination_reviews(args.reviews)
        if not args.rules.is_file():
            raise ValueError(f"{args.rules}: sponsor rules CSV not found")
        studies, records_meta = load_studies_input(
            demographics_path=args.demographics,
            records_dir=args.records_dir,
            archive_path=args.archive,
        )
        mesh_by_nct, mesh_meta = load_condition_mesh_sidecar(
            args.condition_mesh, records_meta=records_meta
        )

        trials, sponsor_meta, review_meta = benchmarks.build_trial_records(
            studies,
            reviews=reviews,
            rules_path=args.rules,
            condition_mesh_by_nct=mesh_by_nct,
        )
        if mesh_by_nct is not None and mesh_meta is not None:
            mesh_meta.update(
                require_condition_mesh_coverage(
                    mesh_by_nct, [t["nct_id"] for t in trials], mesh_meta["file"]
                )
            )

        inputs_meta: dict[str, Any] = {
            "records": records_meta,
            "sponsor_rules": {
                "file": args.rules.name,
                "sha256": sponsor_meta["rules_sha256"],
            },
            "termination_reviews": {
                "file": args.reviews.name,
                "n_rows": len(reviews),
                "sha256": hashlib.sha256(args.reviews.read_bytes()).hexdigest(),
            },
            "condition_mesh": mesh_meta,
        }

        parsed_family, raw_bytes = artifacts.build_artifact_family(
            trials=trials,
            sponsor_meta=sponsor_meta,
            review_meta=review_meta,
            inputs_meta=inputs_meta,
            producer_meta=producer_metadata(),
            generated_at=args.generated_at or now_utc_iso(),
            min_cell_trials=args.min_cell_trials,
        )

        problems = artifacts.check_contract(parsed_family, raw_bytes)
        if problems:
            print(
                f"CONTRACT FAILED ({len(problems)} problem(s)); nothing written:",
                *problems[:40],
                sep="\n  ",
                file=sys.stderr,
            )
            return 1

        artifacts.write_artifact_family(raw_bytes, args.out)
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    run = parsed_family["run.json"]
    den = parsed_family["summary.json"]["denominators"]
    print(
        f"{run['run_id']}: {den['trials']['n']} trials "
        f"({den['resolved_trials']['n']} resolved, {den['stopped_trials']['n']} stopped, "
        f"{den['actual_enrollment_trials']['n']} ACTUAL enrollment) -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
