"""Cohort-agnostic sharded outcomes and provenance layout (version 1).

This module defines the static file layout for ClinicalTrials.gov primary
outcome reporting, document readability/redaction profiles, decision-rule
locations, and longitudinal provenance:

  outcomes_bundles/
    index.json             lightweight multi-cohort catalog (one row per trial,
                           sorted ascending by nct_id, top-level "data" last)
    shards/0.json.gz       per-trial unified bundles for shard n, where
      .. {N-1}.json.gz     n = int(nct_id[3:]) % N (unpadded; default N = 256)

Every shard file is always written (with "data": {} when empty) so browser
clients computing ``Number(nctId.slice(3)) % shards`` never encounter a 404
and can distinguish "loaded, none" from "not loaded".

Invariants enforced here:
- Exact shard-function parity with ``src/site_layout.py::shard_of``
  (``int(nct_id[3:]) % shards`` for ``NCT`` + 8 digits).
- Deterministic encoding across Python 3.11, 3.12, and 3.13: compact ASCII
  JSON, top-level ``"data"`` last for O(1) KB header reads, and gzip level 9
  with ``mtime=0`` and OS byte ``0xff`` (``gzip.GzipFile`` over ``BytesIO``,
  mirroring ``src/archive_records.py::encode``).
- Hard 6-vector ``in_audit`` holdout firewall (``AuditFirewallError``):
  1. ``in_audit`` must be a strict ``bool`` (missing/``None`` never defaults).
  2. ``in_audit is True`` forbids any ``decision_rule_locations`` (even ``{}``).
  3. ``in_audit is True`` forbids LLM/assessment/extra keys anywhere in rows
     or shard records.
  4. ``in_audit is True`` forbids any ``document_profile`` or PDF-scraped
     document metadata (``version_label``, ``version_date_as_printed``,
     ``sha256``) inside ``longitudinal_provenance``.
  5. ``in_audit is True`` requires ``document_badge`` and ``census_badge``
     to remain strictly ``"in_audit_holdout"`` with ``None``/``False`` counts.
  6. Cross-stream JSONL/mapping guard rejects any holdout ``nct_id`` present
     in document or location inputs BEFORE creating or touching ``out_dir``:
     every key must be a strict ID and outside the holdout, and every record
     is read with ``check_side_input_identity`` against its key.
  The holdout flag is read under every spelling Module 4 reads
  (``inAudit``, ``IN_AUDIT``, ``in-audit``, ``is_in_audit``) and every key
  spelling a holdout marker (``in_audit_holdout``: ``is_holdout_flag_key``);
  only the exact ``in_audit`` of the manifest (or the ``in_audit`` argument)
  admits a trial, and every other spelling, anywhere in a record, can only
  refuse.
- Absence is preserved as ``None`` (never coerced to ``0`` or ``""``).
- Zero interference with ``src/dataset_folder.py::present_files`` (which only
  inspects ``demographics.part*.json.gz``, ``studies_tab.part*.json.gz``,
  ``detail/``, and ``run.json``).

Rules this module shares rather than restates:
- The status-to-citation rule and the 6-key citation shape are Module 4's
  (``src.citation_verifier``: ``CITATION_REQUIRED_STATUSES``,
  ``CITATION_FORBIDDEN_STATUSES``, ``validate_status_citations``,
  ``validate_citation_shape``), applied both when rows are normalized and when
  a tree on disk is checked, so the two can never disagree. Trial IDs are
  checked with Module 4's ``validate_strict_nct_id`` (ASCII ``^NCT[0-9]{8}$``;
  ``\\d`` would also admit non-ASCII digits, which ``int()`` reads as the same
  trial). This module imports nothing else from another module.

Holdout stubs: an ``in_audit`` trial is written as its index row (fields that
come 1:1 from the trial/manifest row, with masked badges and
``has_reported_results`` ``None``) plus a shard record holding only
``nct_id``, ``in_audit``, ``cohorts`` and ``shard``; its ``reported_results``,
``document_profile``, ``decision_rule_locations`` and
``longitudinal_provenance`` are ``None``. Any side input for such a trial,
passed in or carried inline in its own record (with or without its section
wrapper), is refused rather than dropped; the public section builders refuse
a holdout's input too, and every refusal about a holdout withholds its ID.
An in-scope trial's record may carry a side section inline at its top level,
which is then published (and must equal the same section given as a side
input); a section key the builder does not read is refused.

Provenance is carried, never dropped or assembled: ``index.json`` holds the
build's own ``extracted_at`` (when the inputs were extracted, never the
build time) and ``source_pipeline_commit``, ``input_stamps`` (each input
container's own stamps, by input) and ``component_versions`` (one version
per component; inputs of several versions are refused by the CLI). Each
section keeps its source's stamps: a document profile its indexer version,
extraction time and commit, every Module 1 document field, a Module 2
record its ``analysis_populations`` (parser version, vocabulary hash,
snapshot date). A trial's ``primary_outcomes_count`` is its registered
outcomes' when it has any (``primary_outcomes_count_of``); a stated count
they contradict is refused, and location rows are bounded by it.

Every input is read as ``plain_data`` where it is taken (a dataclass by its
fields, any Sequence as a list), so the identity walk, the holdout-flag
reading and Module 4's audit-ID scan read exactly what is published.
``validate_outcomes_tree`` reads every in-scope record with the builder's
own input rules and normalizers, so a tree on disk is held to what a build
would accept.

Writing (``write_outcomes_tree``) is allowlisted: the target must be absent,
an empty directory, or a bundle this module wrote (identified by
``outcomes_bundle.json``, which names the layout and its version), and may
not sit in any of the site's data families. See ``check_output_target`` for
every refusal, and ``_swap_into_target`` for the swap and its rollback.
"""
from __future__ import annotations

import copy
import dataclasses
import gzip
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import subprocess
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.citation_verifier import (
    CITATION_FORBIDDEN_STATUSES,
    CITATION_REQUIRED_KEYS,
    CITATION_REQUIRED_STATUSES,
    LOCATION_REQUIRED_KEYS,
    UNREADABLE_SUBTYPES,
    CitationVerificationError,
    validate_citation_shape,
    validate_status_citations,
    validate_strict_nct_id,
)
# Module 4's readings of trial IDs and holdout flags, shared rather than
# restated (red-team m3 round 1 found this module reading fewer spellings than
# Module 4 does): the holdout flag under any spelling a reader takes for it
# (_is_in_audit_key: inAudit, IN_AUDIT, in-audit, is_in_audit), the URL keys it
# reads trial IDs from, an NCT token in every reading (as written,
# percent-decoded, NFKC-folded; and the loose readings: separators, look-alike
# letters), and its audit-ID scan over every key, string and split
# key/value reading. They are private to Module 4; importing them keeps one
# definition of each reading.
from src.citation_verifier import (
    _URL_FIELD_KEYS as _M4_URL_FIELD_KEYS,
    _is_in_audit_key,
    _iter_embedded_nct_tokens,
    _iter_loose_nct_candidates,
    _scan_strings_for_audit_nct_tokens,
    _strip_invisible,
    _text_names_audit_trial,
)

LAYOUT_VERSION = 1
SHARD_KEY = "nct_number_mod"
DEFAULT_SHARDS = 256
DEFAULT_OUTCOMES_SHARDS = DEFAULT_SHARDS
MAX_SHARDS = 1024

DEFAULT_OUT_DIR_NAME = "outcomes_bundles"
INDEX_FILE = "index.json"
SHARDS_DIR = "shards"
SHARD_FILE_TEMPLATE = "shards/{n}.json.gz"
SHARD_CLASS = "outcomes_detail"
SECTION_LABEL_REPORTED_RESULTS = "Primary results as posted"

# Markers identifying other live data families that write_outcomes_tree must
# never write into or under. run.json, summary.json, records and records.json
# mark site/data/ itself and its outcomes, results_structure, census and
# devices families. The geography, sponsor and sex/gender families carry none
# of those (red-team m3 round 1 wrote bundles and reports into copies of them),
# so each is named by the files it does carry: geo/ by active_run.json, a
# frozen geography run folder by CONTRACT.md and geo_dictionary.csv, sponsors/
# by bridge_meta.json and bridge.csv.gz, sex_gender/ by methods.json, and the
# audit folders of both by audit_summary.json. An index.json that is not a
# bundle's own index (a directory without BUNDLE_MARKER_FILE) is refused the
# same way. A report may not take any of these names either.
FOREIGN_DIRECTORY_MARKERS: tuple[str, ...] = (
    "run.json",
    "summary.json",
    "records",
    "records.json",
    "active_run.json",
    "CONTRACT.md",
    "geo_dictionary.csv",
    "bridge_meta.json",
    "bridge.csv.gz",
    "methods.json",
    "audit_summary.json",
)

# The site's data families, by folder name. A path with a folder of one of
# these names directly under a folder named "data" (data/geo/..., data/
# sponsors/...) is refused whatever files it holds, so a family whose marker
# files are absent (or a family added later under one of these names) is not
# written into either.
SITE_DATA_FAMILIES: tuple[str, ...] = (
    "outcomes",
    "results_structure",
    "sponsors",
    "geo",
    "sex_gender",
    "census",
    "devices",
)

# The bundle marker: write_outcomes_tree writes this file into every bundle,
# and a non-empty directory is overwritten only when it holds exactly this
# content. It names the layout and its version, so a bundle of another layout
# version is not overwritten by this one either.
BUNDLE_MARKER_FILE = "outcomes_bundle.json"
BUNDLE_MARKER: dict[str, Any] = {"layout": DEFAULT_OUT_DIR_NAME, "layout_version": LAYOUT_VERSION}

# The staging marker: written first into <out_dir>.partial when this module
# creates it. A .partial without it was not made by this module and is never
# deleted; one with it is this module's leftover staging directory.
STAGING_MARKER_FILE = "outcomes_bundle_staging.json"
STAGING_MARKER: dict[str, Any] = {"staging_for": DEFAULT_OUT_DIR_NAME, "layout_version": LAYOUT_VERSION}

# Inside <out_dir>.partial: the previous shards while the new ones are swapped
# in, and a hard link to (or copy of) the previous index.json, so that an
# interrupt landing after the index rename can still put the previous index
# back. If putting either back fails, it stays here and every later run
# refuses until a person has dealt with it.
PREVIOUS_SHARDS_DIR = "_previous_shards"
PREVIOUS_INDEX_FILE = "_previous_index.json"
FAILED_NEW_SHARDS_DIR = "_failed_new_shards"
# Where hard links are not supported, the previous index is byte-copied here
# first and renamed to PREVIOUS_INDEX_FILE only once the copy is whole, so an
# interrupt during the copy (red-team m3 round 3) leaves no half-written file
# under the name a later run reads as a backup the target needs.
PREVIOUS_INDEX_COPY_FILE = "_previous_index.json.copying"

# Inside <out_dir>.partial: written by the swap right after its last rename,
# naming the digest of the bundle it installed (_tree_digest). While the
# target still holds exactly that bundle, whatever else .partial holds
# (_previous_shards, _previous_index.json) is the superseded version, not a
# failed restore's backup: an interrupt during the cleanup after a completed
# swap (red-team m3 round 2) left them behind, and the next run refused with an
# instruction to move the old version back over the new one.
SWAP_COMPLETE_FILE = "_swap_complete.json"

# Size guardrails matching scripts/check_site_contract.py (telemetry only;
# never modifies tests/data_budget.json).
PART_HARD_LIMIT_BYTES = 100 * 1024 * 1024
TREE_HARD_LIMIT_BYTES = 1_000_000_000
TREE_WARN_BYTES = 900_000_000

# ASCII digits only: ``\d`` also matches other scripts' digits, which int()
# reads as the same number, so "NCT" + eight Arabic-Indic digits would shard
# (and could pass for) an existing trial. Validation goes through Module 4's
# validate_strict_nct_id; this pattern is kept for callers that only test.
NCT_RE = re.compile(r"NCT[0-9]{8}", re.ASCII)
COMMIT_RE = re.compile(r"[0-9a-f]{7,40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COHORT_RE = re.compile(r"[a-z][a-z0-9_]{1,63}")
# ASCII digits only (red-team m3 round 3: ``\d`` admitted "1٣.json.gz", which
# no bundle names). A name matching this is still a shard of the bundle only
# when its index is below the bundle's own shard count (_check_bundle_entries).
SHARD_FILENAME_RE = re.compile(r"(0|[1-9][0-9]*)\.json\.gz", re.ASCII)
# Any NCT-like token, in any script's digits and with separators, for
# withholding trial IDs from refusal messages (withhold_nct_ids).
_NCT_LIKE_TOKEN_RE = re.compile(r"nct[\W_]{0,3}\d{8}", re.IGNORECASE)

STANDARD_COHORTS: tuple[str, ...] = (
    "industry_phase3",
    "nih",
    "cooperative_group",
    "academic",
    "us_federal_other",
    "non_profit_foundation",
    "all_large_docs",
)

# Maps Module 2 (sponsors/ecosystem.py) sector names into canonical cohort slugs.
ECOSYSTEM_SECTOR_TO_COHORT: dict[str, str] = {
    "nih": "nih",
    "us_federal_other": "us_federal_other",
    "nctn_cooperative_group": "cooperative_group",
    "cooperative_group": "cooperative_group",
    "academic_medical_center": "academic",
    "academic": "academic",
    "non_profit_foundation": "non_profit_foundation",
}

ELEMENTS: tuple[str, ...] = (
    "primary_endpoint_analysed",
    "analysis_population",
    "hypothesis_type",
    "alpha_and_sidedness",
    "margin",
    "interim_boundary",
    "multiplicity",
)
_ELEMENT_ORDER: dict[str, int] = {name: i for i, name in enumerate(ELEMENTS)}

LOCATION_STATUSES: tuple[str, ...] = (
    "FOUND",
    "NOT_FOUND",
    "NOT_APPLICABLE",
    "AMBIGUOUS",
    "UNREADABLE",
    "NO_DOCUMENT",
)

# CITATION_REQUIRED_STATUSES and CITATION_FORBIDDEN_STATUSES are imported from
# src.citation_verifier above: Module 4 holds the one definition of which
# statuses need a citation and which forbid one.

CENSUS_STATUSES: tuple[str, ...] = (
    "complete",
    "partial",
    "not_started",
    "no_document",
)

# Module 1's document and trial readability vocabulary (src/document_indexer.py
# READABILITY_STATUSES, restated: this module imports only Module 4).
# ``extraction_failed`` is a document whose bytes were obtained but whose pages
# no provider could extract; it is kept apart from ``download_failed`` (bytes
# never obtained). Red-team m3 round 3: it was refused here, so a Module 1
# profile holding one could not be published at all.
READABILITY_STATUSES: tuple[str, ...] = (
    "machine_readable",
    "partial_image_or_redacted",
    "image_only_scan",
    "garbled_cmap",
    "extraction_failed",
    "download_failed",
    "no_document",
)

# CT.gov API v2 OutcomeMeasureType values (the vocabulary of
# outcomes/results_structure.py OUTCOME_TYPES and Module 2's
# OUTCOME_MEASURE_TYPES), read case-insensitively but never stripped: a value
# outside it ("PRIMARY " or "primary" + U+200B) is refused, not read as a
# secondary outcome and skipped (red-team m3 round 3).
OUTCOME_MEASURE_TYPES: tuple[str, ...] = ("PRIMARY", "SECONDARY", "OTHER_PRE_SPECIFIED", "POST_HOC")

READABILITY_BADGE_STATUSES: tuple[str, ...] = (
    *READABILITY_STATUSES,
    "not_indexed",
    "in_audit_holdout",
)

REDACTION_FLAGS: tuple[str, ...] = (
    "cci",
    "ppd",
    "white_box_or_blank_stats",
)

CENSUS_BADGE_STATUSES: tuple[str, ...] = (
    "complete",
    "partial",
    "not_started",
    "no_document",
    "in_audit_holdout",
)

DATE_PRECISIONS: tuple[str, ...] = (
    "day",
    "month",
    "year",
    "unknown",
)

DOC_TYPES: tuple[str, ...] = (
    "protocol",
    "sap",
    "combined",
)

ALLOWED_PASSES: tuple[str, ...] = (
    "primary",
    "second",
    "ocr_recovery",
    "adjudication",
)

# The keys a decision-rule location row may carry on input: Module 4's
# LOCATION_REQUIRED_KEYS (nct_id, primary_outcome_index, primary_outcome_title,
# element, status, citations, pages_searched, pages_not_searched, note, worker,
# pass, model_id, prompt_version; required by Module 4 for a census row, read
# here when present) and the two optional keys Module 4 reads from a location
# (text_source, unreadable_subtype). A holdout-flag key is read too (it must
# be False) and not published. Any other key would be dropped, so it is
# refused (red-team m3 round 3: model_id, prompt_version and an unknown
# confidence were dropped without a word).
LOCATION_ROW_INPUT_KEYS: frozenset[str] = frozenset({*LOCATION_REQUIRED_KEYS, "text_source", "unreadable_subtype"})

EFFECTIVE_SOURCE_PASSES: tuple[str, ...] = (
    "primary",
    "ocr_recovery",
    "adjudication",
)

# Exact shard test vectors shared with src/site_layout.py.
SHARD_VECTORS: tuple[dict[str, Any], ...] = (
    {"nct_id": "NCT01975376", "shards": 256, "shard": 80},
    {"nct_id": "NCT00663858", "shards": 256, "shard": 50},
    {"nct_id": "NCT01174160", "shards": 256, "shard": 144},
    {"nct_id": "NCT01174160", "shards": 128, "shard": 16},
)

INDEX_HEADERS: tuple[str, ...] = (
    "extracted_at",
    "source_pipeline_commit",
    "layout_version",
    "layout",
    "cohort_counts",
    "total_trials",
    "audit_holdout_trials",
    "component_versions",
    "input_stamps",
    "data",
)

# The header stamps an input container may carry (a JSON object holding one
# row list beside them), and the inputs that may carry them. They are
# published in index.json's ``input_stamps`` under the input's name (the
# locations input, which may be several files, as one entry per file that
# has any), as given: each input's extraction time and pipeline commit stay
# with that input, and none is dropped (skeptic review of m3: every
# container's stamps were read, and only the trials or manifest container's
# extracted_at, chosen by precedence, reached the tree).
INPUT_STAMP_KEYS: tuple[str, ...] = (
    "extracted_at",
    "pipeline_commit",
    "source_pipeline_commit",
    "indexer_rules_version",
    "run_id",
    "schema_version",
)
INPUT_STAMP_SOURCES: tuple[str, ...] = (
    "cohort_manifest",
    "trials",
    "documents",
    "locations",
    "provenance",
    "population_parsed",
)

SHARD_HEADERS: tuple[str, ...] = (
    "extracted_at",
    "source_pipeline_commit",
    "layout_version",
    "class",
    "shard",
    "shards",
    "key",
    "data",
)

INDEX_ROW_KEYS: tuple[str, ...] = (
    "nct_id",
    "brief_title",
    "study_type",
    "phase",
    "enrollment",
    "enrollment_type",
    "primary_completion_date",
    "results_first_post_date",
    "lead_sponsor_name",
    "sponsor_class",
    "ecosystem_category",
    "primary_condition",
    "cohorts",
    "in_audit",
    "protocol",
    "sap",
    "primary_outcomes_count",
    "has_reported_results",
    "document_badge",
    "census_badge",
)

SHARD_RECORD_KEYS: tuple[str, ...] = (
    "nct_id",
    "in_audit",
    "cohorts",
    "shard",
    "reported_results",
    "document_profile",
    "decision_rule_locations",
    "longitudinal_provenance",
)

# The 6 citation keys are Module 4's (one definition, as for the status rules).
CITATION_KEYS: tuple[str, ...] = CITATION_REQUIRED_KEYS

FORBIDDEN_AUDIT_KEYS: frozenset[str] = frozenset({
    "llm_assessment",
    "llm_assessments",
    "llm_extraction",
    "paper_extraction",
    "fda_extraction",
    "ai_provider",
    "model_id",
    "prompt_sha256",
    "worker",
    "worker_notes",
    "second_pass",
    "ocr_recovery_pass",
    "adjudication_pass",
    "effective_pass",
    "citations",
    "locations",
})

FORBIDDEN_RAW_TEXT_KEYS: frozenset[str] = frozenset({
    "page_texts",
    "raw_pages",
    "pages_text",
    "full_text",
})


def _folded_key(key: Any) -> str:
    """Fold a key the way Module 4 folds the holdout-flag key.

    Method: NFKC folding, invisible and format characters removed
    (``_strip_invisible``), case folded, and everything but the letters a-z
    dropped. ``nct_id``, ``nctId``, ``NCT-ID``, ``"nct id"`` and ``nct`` +
    U+200B + ``_id`` all fold to ``nctid``; ``in_audit`` and ``inAudit`` to
    ``inaudit``. The fold is used only to recognise a key, never shown.
    """
    text = key if isinstance(key, str) else str(key)
    folded = _strip_invisible(unicodedata.normalize("NFKC", text)).casefold()
    return re.sub(r"[^a-z]", "", folded)


def _folded_forms(*spellings: str) -> frozenset[str]:
    """The folded forms (``_folded_key``) of ``spellings``.

    Every key-form set below is built through this, because a key is
    compared only after it is folded: a listed spelling that is not its own
    fold could never match (skeptic review of m3: ``"sha256"`` was listed as
    written, folds to ``"sha"``, and a PDF's sha256 in a trial's record was
    dropped without a word).
    """
    return frozenset(_folded_key(spelling) for spelling in spellings)


# Keys naming a shard section or a side input, folded (``_folded_key``). A
# holdout's stub carries none of these, so one found anywhere in a holdout's
# own --trials or manifest record is holdout material in the input, refused
# (AuditFirewallError) rather than dropped. CT.gov's own schema has none of
# them, so the whole record is read, its native sections included.
HOLDOUT_INLINE_SECTION_FORMS: frozenset[str] = _folded_forms(
    "reportedresults",
    "documentprofile",
    "decisionrulelocations",
    "longitudinalprovenance",
    "analysispopulationparsed",
    "parsedpopulations",
    "populationparsed",
)

# Keys of a section's content (folded), as a side input carries them inside
# its wrapper: a Module 1 profile's documents and summary, Module 4's
# provenance lists and milestones, Module 2's outcomes, the location passes,
# the results block's lists, raw page text. Found in a holdout's own record
# outside CT.gov's native sections, such a key is holdout material without its
# wrapper (red-team m3 round 2: a top-level documents, large_docs,
# registry_milestones, outcomes or primary_pass was dropped without a word),
# refused like the wrapped forms. Inside the native sections CT.gov's own
# schema uses some of these spellings (documentSection's largeDocs folds to
# largedocs), so those sections are read for the wrapped forms and the
# vector-3 keys instead.
HOLDOUT_SIDE_CONTENT_FORMS: frozenset[str] = _folded_forms(
    "documents",
    "readabilitysummary",
    "redactionflags",
    "indexerrulesversion",
    "excludedicfcount",
    "largedocs",
    "registrymilestones",
    "posteddocuments",
    "publications",
    "linkedpublications",
    "datedeltas",
    "primarycompletiontoresultsdelta",
    "provenancerulesversion",
    "provenanceschemaversion",
    "outcomes",
    "parserrulesversion",
    "primarypass",
    "secondpass",
    "ocrrecoverypass",
    "adjudicationpass",
    "effectivepass",
    "censusstatus",
    "elementsexpectedperoutcome",
    "primaryoutcomemeasures",
    "registeredprimaryoutcomes",
    "resultsposted",
    "analysispopulations",
    "pagetexts",
    "rawpages",
    "pagestext",
    "fulltext",
)

# Keys of one side row (folded), as a Module 1 document row, a PDF inspection
# row or a decision-rule location row carries them: a document's PDF-scraped
# and inspection fields, and a location row's cell, search and model fields.
# CT.gov's own schema uses none of them, so found in a trial's own record,
# CT.gov's native sections included, such a key is side material however its
# rows are wrapped (red-team m3 round 3: rows under a wrapper no list names,
# such as pdf_index or rule_census, and PDF-scraped sha256, version_label and
# version_date_as_printed on CT.gov's own largeDocs, were dropped without a
# word). Keys CT.gov's schema does use (filename, url, status, title, note)
# are not listed; a side row carries listed keys beside them.
SIDE_ROW_KEY_FORMS: frozenset[str] = _folded_forms(
    "sha256",
    "physicalpages",
    "pageswithouttext",
    "readabilitystatus",
    "redactionmarkers",
    "versionlabel",
    "versiondateasprinted",
    "pagesselected",
    "pagesdeferred",
    "httpstatus",
    "doctype",
    "retrievedat",
    "downloaderror",
    "extractionerror",
    "unreadablesubtype",
    "unreadablerecoverybucket",
    "unreadablestatisticalpages",
    "apidocdate",
    "uploadtiming",
    "cachehit",
    "profileversion",
    "primaryoutcomeindex",
    "primaryoutcometitle",
    "element",
    "pagessearched",
    "pagesnotsearched",
    "textsource",
    "sourcepass",
    "promptversion",
)

# The side-content forms CT.gov's own schema uses inside its native sections
# (documentSection.largeDocumentModule.largeDocs folds to ``largedocs``); every
# other form of HOLDOUT_SIDE_CONTENT_FORMS and SIDE_ROW_KEY_FORMS is read there
# too (red-team m3 round 3: a primary_pass inside protocolSection.outcomesModule
# and a documents list inside resultsSection were dropped without a word).
_CTGOV_NATIVE_CONTENT_FORMS: frozenset[str] = _folded_forms("largedocs")

# The positions at which an in-scope trial's own record may carry a section
# inline, and the builder reads it there: the three side sections and a
# pre-built reported_results at the top level, and a measure's
# analysis_population_parsed inside a PRIMARY CT.gov outcome measure or a
# pre-built primary measure. A section key anywhere else would be dropped, so
# it is refused (red-team m3 round 2: a top-level document_profile was
# dropped without a word).
INLINE_SIDE_SECTIONS: tuple[str, ...] = (
    "document_profile",
    "decision_rule_locations",
    "longitudinal_provenance",
)

# Folded key forms read as naming a trial (``_folded_key``): one ID, or a list
# of IDs. These are Module 4's named keys (nct_id, nctId, trial_nct_id;
# nct_ids, trial_nct_ids) under any case, separator or invisible character.
_TRIAL_ID_KEY_FORMS: frozenset[str] = _folded_forms("nctid", "trialnctid")
_TRIAL_ID_LIST_KEY_FORMS: frozenset[str] = _folded_forms("nctids", "trialnctids")

# Index-row fields that hold one scalar each: text (str or None), a whole
# number (int, never bool, or None), a flag (bool) or a tri-state flag (bool
# or None). The builder never writes an object or a list into any of them, and
# --check refuses one (an object there would carry structured content, such
# as results, past a holdout's stub).
_INDEX_ROW_TEXT_FIELDS: tuple[str, ...] = (
    "brief_title",
    "study_type",
    "phase",
    "enrollment_type",
    "primary_completion_date",
    "results_first_post_date",
    "lead_sponsor_name",
    "sponsor_class",
    "ecosystem_category",
    "primary_condition",
)
_INDEX_ROW_INT_FIELDS: tuple[str, ...] = ("enrollment", "primary_outcomes_count")
_INDEX_ROW_BOOL_FIELDS: tuple[str, ...] = ("protocol", "sap")
_INDEX_ROW_TRISTATE_FIELDS: tuple[str, ...] = ("has_reported_results",)


class OutcomesLayoutError(ValueError):
    """The outcomes layout, a shard, or a trial record violates the contract."""


class AuditFirewallError(OutcomesLayoutError):
    """Raised when an ``in_audit == True`` holdout trial leaks protected data."""


class RetainedBackupError(OutcomesLayoutError):
    """The swap into the target failed and the previous shards could not be put
    back; they are kept in ``<out_dir>.partial/_previous_shards`` (named in the
    message) and every later write refuses until a person has dealt with them.
    """


def withhold_nct_ids(text: str) -> str:
    """Replace every NCT-like token in ``text`` with a placeholder.

    Used on refusal messages that concern the ``in_audit`` holdout: such a
    message may be about a holdout trial, and a holdout trial's ID is not to be
    printed or logged. The pattern is deliberately loose (any script's digits,
    up to three separator characters after ``NCT``), so a malformed rendering
    of an ID is withheld too.
    """
    return _NCT_LIKE_TOKEN_RE.sub("<NCT id withheld>", text)


def is_holdout_flag_key(key: Any) -> bool:
    """True when ``key`` spells the holdout flag as a reader takes it.

    Method: Module 4's ``_is_in_audit_key`` (``in_audit``, ``inAudit``,
    ``IN_AUDIT``, ``in-audit``, ``is_in_audit``), the one definition of that
    reading, plus any key whose folded form (``_folded_key``: NFKC, invisible
    characters removed, case folded, letters a-z only) contains ``holdout``:
    ``in_audit_holdout`` is the very status the badges use for a holdout, and
    a record carrying ``in_audit_holdout``, ``inAuditHoldout`` or
    ``audit_holdout`` true says it is one (red-team m3 round 2: such a key set
    true on an ordinary trial's record was published verbatim). Only the
    exact ``in_audit`` of the manifest admits a trial; every key this reads
    can only refuse. CT.gov's own schema has no key containing ``holdout``.

    Red-team m3 round 3 set ``held_out``, ``heldOut``, ``is_held_out`` and
    ``in_audit_set`` true on an ordinary trial's record and its provenance,
    and each was published verbatim, so a folded form containing ``heldout``
    or ``inaudit`` is read as a holdout marker too. CT.gov's schema has no
    key containing either.
    """
    if type(key) is not str:
        return False
    folded = _folded_key(key)
    return _is_in_audit_key(key) or any(marker in folded for marker in ("holdout", "heldout", "inaudit"))


def plain_data(value: Any, label: str = "input") -> Any:
    """Return ``value`` as plain JSON-shaped data, refusing what has no JSON reading.

    Method: every reader in this module (the identity walk, the holdout-flag
    reading, Module 4's audit-ID scan, the normalizers) enters dicts, lists
    and tuples. Red-team m3 round 2 passed a Module 2 dataclass and a
    ``collections.UserList`` through all of them unread, and both were
    published. Converting an input once, where it is taken, means the data
    checked is the data published. A Mapping becomes a dict (its keys must be
    strings, as JSON's are), any other Sequence but ``str``/``bytes``/
    ``bytearray`` a list, a dataclass instance a dict of its fields
    (``dataclasses.fields``, recursively); ``str``, ``int``, ``float``,
    ``bool`` and ``None`` are kept. Anything else (bytes, a set, an arbitrary
    object) has no single JSON reading and is refused, never stringified or
    skipped. A float that is not finite (NaN, Infinity) is refused too: JSON
    has no such number, Python's json writes it as a bare ``NaN`` token, and a
    browser's ``JSON.parse`` then rejects the whole shard and every trial in
    it (red-team m3 round 3). The message names the position's type, never a
    value.
    """
    if isinstance(value, float) and not math.isfinite(value):
        raise OutcomesLayoutError(
            f"{label}: holds a non-finite number (NaN or Infinity), which JSON cannot represent; refused"
        )
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise OutcomesLayoutError(
                    f"{label}: a mapping key is a {type(key).__name__}, not a string; JSON keys are strings"
                )
            out[key] = plain_data(item, label)
        return out
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: plain_data(getattr(value, field.name), label) for field in dataclasses.fields(value)}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [plain_data(item, label) for item in value]
    raise OutcomesLayoutError(
        f"{label}: holds a {type(value).__name__}, which has no JSON reading; refused, never stringified or skipped"
    )


def withhold_if_names(text: str, nct_ids: AbstractSet[str]) -> str:
    """Return ``text``, or ``withhold_nct_ids(text)`` when it names any of ``nct_ids``.

    Method: a refusal about an ordinary trial may name it, but one that names
    a holdout trial (in any reading Module 4 takes for an ID:
    ``_text_names_audit_trial``, which includes lowercase, padded, percent-
    encoded and look-alike renderings) has every NCT-like token withheld, so
    the holdout's ID is never printed. ``nct_ids`` must hold strict IDs.
    """
    if nct_ids and _text_names_audit_trial(text, frozenset(nct_ids)):
        return withhold_nct_ids(text)
    return text


def strict_nct_id(value: Any, label: str = "nct_id") -> str:
    """Return ``value`` if it is an ASCII ``^NCT[0-9]{8}$`` string, else raise.

    Delegates the decision to Module 4's ``validate_strict_nct_id`` (no
    stripping, no case folding, no non-ASCII digits). The refusal names the
    position (NCT-like tokens withheld) and the value's type only, never the
    value: Module 4's message quotes a value that holds no NCT-like token
    (its ``repr``), and a holdout's record, or the side record of a trial
    outside the manifest, placed results text under a trial-ID key, which the
    refusal printed (skeptic review of m3).
    """
    try:
        return validate_strict_nct_id(value, context=label)
    except CitationVerificationError:
        raise OutcomesLayoutError(
            f"{withhold_nct_ids(label)} must be a strict ASCII ^NCT[0-9]{{8}}$ trial ID, got "
            f"{type(value).__name__} that is not one (value not shown)"
        ) from None


# ── sharding and naming ───────────────────────────────────────────────────────

def _validate_shards(shards: Any) -> int:
    """Validate shard count as a strict int (rejecting bool) in 1..MAX_SHARDS."""
    if type(shards) is not int or not 1 <= shards <= MAX_SHARDS:
        raise OutcomesLayoutError(
            f"shards must be a whole number from 1 to {MAX_SHARDS}, got {shards!r}"
        )
    return shards


def outcomes_shard_of(nct_id: Any, shards: int = DEFAULT_OUTCOMES_SHARDS) -> int:
    """Return ``int(nct_id[3:]) % shards`` matching ``src/site_layout.py::shard_of``."""
    n_shards = _validate_shards(shards)
    checked = strict_nct_id(nct_id, "nct_id (no outcomes shard can hold it)")
    return int(checked[3:]) % n_shards


# Alias matching architecture spec #61 naming.
shard_of = outcomes_shard_of


def shard_relpath(shard: int) -> str:
    """Relative path inside an outcomes tree: ``shards/<n>.json.gz`` (unpadded)."""
    if type(shard) is not int or shard < 0:
        raise OutcomesLayoutError(f"shard index must be a non-negative int, got {shard!r}")
    return SHARD_FILE_TEMPLATE.replace("{n}", str(shard))


def layout_header(shards: int = DEFAULT_OUTCOMES_SHARDS) -> dict[str, Any]:
    """The ``layout`` metadata block written into ``outcomes_bundles/index.json``."""
    n_shards = _validate_shards(shards)
    return {
        "version": LAYOUT_VERSION,
        "index_file": INDEX_FILE,
        "shards_dir": SHARDS_DIR,
        "shard_file": SHARD_FILE_TEMPLATE,
        "shards": n_shards,
        "key": SHARD_KEY,
    }


# ── deterministic encoding and gzip header inspection ─────────────────────────

def canonicalize_nested(value: Any) -> Any:
    """Recursively sort every dict's keys while preserving list order."""
    if isinstance(value, dict):
        return {k: canonicalize_nested(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [canonicalize_nested(v) for v in value]
    if isinstance(value, tuple):
        return [canonicalize_nested(v) for v in value]
    return value


def encode_json_text(doc: Mapping[str, Any], *, trailing_newline: bool = False) -> str:
    """Encode a mapping as compact ASCII JSON preserving top-level key order.

    Top-level key order is intentionally preserved so ``"data"`` stays last
    for O(1) KB header reads; nested payloads are canonicalized separately.
    """
    text = json.dumps(doc, separators=(",", ":"), ensure_ascii=True)
    return text + "\n" if trailing_newline else text


def encode_gz_bytes(doc: Mapping[str, Any]) -> bytes:
    """Compress compact ASCII JSON with level 9, ``mtime=0``, and OS byte ``0xff``.

    ``gzip.GzipFile`` writes OS byte ``0xff`` ("unknown") identically on Python
    3.11, 3.12, and 3.13 (unlike ``gzip.compress`` on 3.11/3.12), matching
    ``src/archive_records.py::encode``.
    """
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as gz:
        gz.write(encode_json_text(doc).encode("ascii"))
    return buf.getvalue()


class RepeatedKeyError(AuditFirewallError):
    """A JSON object repeats a key (``loads_refusing_repeated_keys``)."""


def _refuse_repeated_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` building an object and refusing a repeated key.

    ``json.loads`` keeps the last of two equal keys, so
    ``{"in_audit": true, ..., "in_audit": false}`` reads as in scope to it and
    as a holdout to a reader that keeps the first. Which value such a file
    means is not determined, and a repeated ``in_audit`` or ``nct_id`` can
    hide a holdout trial, so it is refused as an audit firewall error. The key
    is named only when it holds no NCT-like ID.
    """
    built: dict[str, Any] = {}
    for key, value in pairs:
        if key in built:
            raise RepeatedKeyError(
                f"a JSON object repeats the key {withhold_nct_ids(repr(key))}; a JSON reader keeps one "
                f"value and drops the other, and a repeated in_audit or nct_id can hide a holdout trial"
            )
        built[key] = value
    return built


class NonFiniteNumberError(OutcomesLayoutError):
    """A JSON text holds ``NaN``, ``Infinity`` or ``-Infinity`` (``loads_refusing_repeated_keys``)."""


def _refuse_non_finite_constant(token: str) -> Any:
    """``parse_constant`` hook refusing ``NaN``, ``Infinity`` and ``-Infinity``.

    Python's json reads and writes these tokens; JSON does not have them, and
    a browser's ``JSON.parse`` rejects a whole shard holding one (red-team m3
    round 3: a NaN measurement was written into a shard and --check passed it).
    """
    raise NonFiniteNumberError(
        f"the JSON text holds the non-finite constant {token}, which is not JSON (a browser's JSON.parse "
        f"rejects the whole file); refused"
    )


def loads_refusing_repeated_keys(text: str) -> Any:
    """``json.loads`` that refuses any object repeating a key (``RepeatedKeyError``)
    and any non-finite constant (``NonFiniteNumberError``).

    The builder reads its inputs this way and ``validate_outcomes_tree`` reads
    a tree on disk this way, so a tree whose index or shard repeats a key
    (red-team m3 round 1: ``"in_audit":true,"in_audit":false`` on a holdout
    carrying a full record passed --check), or holds ``NaN``/``Infinity``
    (round 3), is refused on both sides.
    """
    return json.loads(text, object_pairs_hook=_refuse_repeated_keys, parse_constant=_refuse_non_finite_constant)


def inspect_gzip_header(raw: bytes) -> tuple[int, int]:
    """Return ``(mtime, os_byte)`` from a raw gzip byte stream."""
    if len(raw) < 10 or raw[:2] != b"\x1f\x8b":
        raise OutcomesLayoutError("file does not carry a valid gzip header")
    mtime = int.from_bytes(raw[4:8], "little")
    os_byte = raw[9]
    return mtime, os_byte


# ── provenance helpers ────────────────────────────────────────────────────────

# The engine checkout this module belongs to (src/outcomes_layout.py -> root).
ENGINE_ROOT = Path(__file__).resolve().parents[1]


def _git_environment() -> dict[str, str]:
    """The caller's environment without any ``GIT_*`` variable.

    Method: ``git -C <dir>`` still honours ``GIT_DIR``, ``GIT_WORK_TREE``,
    ``GIT_INDEX_FILE`` and the rest of git's repository-location variables,
    which a git hook (or any caller) may have set for another repository.
    With ``GIT_DIR`` naming another repository, ``--show-toplevel`` answers
    the ``-C`` directory and ``rev-parse HEAD`` that repository's commit, so
    the same-repository check passed and another repository's commit was
    stamped (red-team m3 round 1). Dropping every ``GIT_*`` variable makes git
    discover the repository from ``repo_root`` alone.
    """
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _git_output(repo_root: Path, *args: str) -> str | None:
    """Return ``git -C <repo_root> <args>`` stdout stripped, or ``None`` on any failure.

    Runs with ``_git_environment`` (no ``GIT_*`` variable), so the answer is
    about the repository at ``repo_root`` and nothing the environment names.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=True,
            env=_git_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return out or None


def resolve_pipeline_commit(
    explicit: str | None = None,
    *,
    repo_root: Path = ENGINE_ROOT,
) -> str | None:
    """Return the commit to stamp as ``source_pipeline_commit``, or ``None``.

    Method:
    1. An explicit value (``--pipeline-commit``) wins, validated against
       ``COMMIT_RE``.
    2. Otherwise ``git -C <repo_root> rev-parse HEAD``, where ``repo_root`` is
       this engine checkout (from ``__file__``), never the caller's working
       directory: run from inside another repository, a bare ``git rev-parse``
       would stamp that repository's commit. The answer counts only when
       ``git -C <repo_root> rev-parse --show-toplevel`` is ``repo_root``
       itself, so an engine copied into another repository without its own
       ``.git`` does not borrow the enclosing repository's commit. Both git
       calls run without the caller's ``GIT_*`` variables
       (``_git_environment``), so a ``GIT_DIR`` set for another repository
       (inside a git hook) is not followed.
    3. Anything else is unresolvable and returns ``None``; the caller refuses
       to build rather than stamp ``null`` (see scripts/build_outcomes_shards.py).

    ``GITHUB_SHA`` is not read: it names the commit of whichever repository's
    workflow is running, which need not be this engine checkout.
    """
    if explicit is not None:
        return validate_commit_stamp(explicit, allow_null=False, label="source_pipeline_commit")
    toplevel = _git_output(repo_root, "rev-parse", "--show-toplevel")
    if toplevel is None:
        return None
    try:
        same_repo = Path(toplevel).resolve() == Path(repo_root).resolve()
    except OSError:
        return None
    if not same_repo:
        return None
    head = _git_output(repo_root, "rev-parse", "HEAD")
    if head is not None and COMMIT_RE.fullmatch(head):
        return head
    return None


def validate_iso_timestamp(value: Any, label: str = "extracted_at") -> str:
    """Validate a non-empty ISO-8601 timestamp string."""
    if not isinstance(value, str) or not value.strip():
        raise OutcomesLayoutError(f"{label} must be a non-empty ISO-8601 timestamp string, got {type(value).__name__}")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OutcomesLayoutError(f"{label} is not a valid ISO-8601 timestamp (value not shown)") from exc
    return value


def validate_commit_stamp(
    value: Any,
    *,
    allow_null: bool = False,
    label: str = "source_pipeline_commit",
) -> str | None:
    """Validate a 7..40 lowercase hex git commit SHA (strict non-empty by default)."""
    if value is None:
        if allow_null:
            return None
        raise OutcomesLayoutError(
            f"{label} is missing/null (pass --pipeline-commit <sha> or allow_null_commit=True)"
        )
    if not isinstance(value, str) or not COMMIT_RE.fullmatch(value):
        raise OutcomesLayoutError(
            f"{label} must be 7..40 lowercase hex characters matching {COMMIT_RE.pattern}, got {type(value).__name__} "
            f"(value not shown)"
        )
    return value


def _strict_int_or_none(value: Any, label: str) -> int | None:
    """Return ``int`` or ``None``; explicitly rejects ``bool`` and floats.

    The message names the field and the type, never the value: a holdout's
    record placed results text in its enrollment, and the refusal printed it
    (red-team m3 round 3).
    """
    if value is None:
        return None
    if type(value) is not int:
        raise OutcomesLayoutError(f"{label} must be an int or None, got {type(value).__name__}")
    return value


def _require_strict_bool(value: Any, label: str) -> bool:
    """Require an explicit Python ``bool``; missing/``None`` never defaults.

    The message names the type, never the value (red-team m3 round 3: a
    holdout's ``protocol`` holding results text was printed in the refusal).
    """
    if type(value) is not bool:
        raise AuditFirewallError(
            f"{label} must be an explicit bool (True or False), got {type(value).__name__}"
        )
    return value


# ── 6-vector audit holdout firewall ──────────────────────────────────────────

# Native CT.gov API v2 top-level sections. Their own schema uses two keys that
# are also vector-3 keys, "locations" (contactsLocationsModule.locations) and
# "citations"; inside these sections only those two are allowed. Every other
# vector-3 key is read inside them too (red-team m3 round 2: the sections were
# skipped wholesale, so protocolSection.outcomesModule.llm_assessment on a
# holdout's record was dropped without a word).
_CTGOV_NATIVE_SECTIONS: frozenset[str] = frozenset({
    "protocolSection",
    "resultsSection",
    "annotationSection",
    "documentSection",
    "derivedSection",
})
_CTGOV_NATIVE_KEY_FORMS: frozenset[str] = frozenset({"locations", "citations"})


def _find_forbidden_keys_recursive(
    obj: Any,
    prefix: str = "",
    *,
    native_sections: AbstractSet[str] = frozenset(),
    allowed_forms: AbstractSet[str] = frozenset(),
) -> list[str]:
    """Positions of every key in ``FORBIDDEN_AUDIT_KEYS`` at any depth.

    Method: keys are compared folded (``_folded_key``: any case, separator or
    invisible character), so ``LLM_Assessment`` or ``modelId`` is the key it
    spells. Below a top-level key named in ``native_sections`` (CT.gov's own
    sections) the forms in ``_CTGOV_NATIVE_KEY_FORMS`` are allowed, since
    CT.gov's schema uses them there; every other forbidden key is reported
    there as anywhere else. Lists and tuples are entered item by item.
    """
    hits: list[str] = []
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            key_str = str(k)
            path = f"{prefix}.{key_str}" if prefix else key_str
            form = _folded_key(key_str)
            if form in _FORBIDDEN_AUDIT_KEY_FORMS and form not in allowed_forms:
                hits.append(path)
            child_allowed = allowed_forms
            if not prefix and key_str in native_sections:
                child_allowed = _CTGOV_NATIVE_KEY_FORMS
            hits.extend(_find_forbidden_keys_recursive(v, path, allowed_forms=child_allowed))
    elif isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            hits.extend(_find_forbidden_keys_recursive(item, f"{prefix}[{idx}]", allowed_forms=allowed_forms))
    return hits


def _has_pdf_scraped_value(val: Any) -> bool:
    """Return True if a metadata field holds a non-empty PDF-scraped value."""
    if val is None:
        return False
    if isinstance(val, str):
        return bool(val.strip())
    if isinstance(val, Mapping):
        # Module 4 ParsedDate dicts carry raw / iso_normalized / date_precision.
        raw = val.get("raw")
        iso = val.get("iso_normalized")
        if isinstance(raw, str) and raw.strip():
            return True
        if isinstance(iso, str) and iso.strip():
            return True
        return False
    return True


def _check_provenance_pdf_leak(nct_id: str, provenance: Any) -> None:
    """Vector 4b: reject PDF-scraped document fields on an audit holdout trial."""
    if not isinstance(provenance, Mapping):
        return
    for list_key in ("posted_documents", "large_docs"):
        docs = provenance.get(list_key)
        if not isinstance(docs, Sequence) or isinstance(docs, (str, bytes)):
            continue
        for idx, doc in enumerate(docs):
            if not isinstance(doc, Mapping):
                continue
            for scraped_key in ("version_label", "version_date_as_printed", "sha256"):
                if _has_pdf_scraped_value(doc.get(scraped_key)):
                    raise AuditFirewallError(
                        f"{nct_id}: in_audit holdout leak (vector 4) — longitudinal_provenance."
                        f"{list_key}[{idx}].{scraped_key} carries PDF-scraped metadata"
                    )


def assert_no_audit_leakage(
    *,
    nct_id: str,
    in_audit: Any,
    shard_record: Mapping[str, Any] | None = None,
    index_row: Mapping[str, Any] | None = None,
    document_profile: Any = None,
    decision_rule_locations: Any = None,
    longitudinal_provenance: Any = None,
    raw_payload: Mapping[str, Any] | None = None,
) -> None:
    """Enforce the 6-vector ``in_audit`` holdout firewall.

    Raises ``AuditFirewallError`` (a subclass of ``OutcomesLayoutError`` and
    ``ValueError``) immediately on any violation:
      1. ``in_audit`` is not a strict ``bool`` (never silently defaulted).
      2. ``in_audit is True`` with non-``None`` ``decision_rule_locations``.
      3. ``in_audit is True`` with any forbidden LLM/assessment key or extra
         key outside ``SHARD_RECORD_KEYS`` / ``INDEX_ROW_KEYS``.
      4. ``in_audit is True`` with non-``None`` ``document_profile`` or
         PDF-scraped ``version_label`` / ``version_date_as_printed`` / ``sha256``
         inside ``longitudinal_provenance``.
      5. ``in_audit is True`` with leaking ``document_badge`` or
         ``census_badge`` fields on ``index_row``.
    (Vector 6 — cross-stream JSONL/mapping rejection — is enforced by
    ``assert_no_audit_cross_stream_leakage`` prior to staging.)

    Every refusal has its NCT-like tokens withheld (``withhold_nct_ids``):
    it is about a trial that is, or may be, a holdout, and the library entry
    points that raise it (``validate_outcomes_tree`` with
    ``raise_on_audit_leakage``, ``write_outcomes_tree``,
    ``build_outcomes_documents``) printed the holdout's ID where only the CLI
    withheld it (red-team m3 round 3). No refusal quotes a value of the row or
    record, only its type: a holdout row's leaked content was printed on
    stderr and in --report.
    """
    try:
        _assert_no_audit_leakage(
            nct_id=nct_id,
            in_audit=in_audit,
            shard_record=shard_record,
            index_row=index_row,
            document_profile=document_profile,
            decision_rule_locations=decision_rule_locations,
            longitudinal_provenance=longitudinal_provenance,
            raw_payload=raw_payload,
        )
    except AuditFirewallError as exc:
        shown = withhold_nct_ids(str(exc))
        if shown == str(exc):
            raise
        raise type(exc)(shown) from None


def _assert_no_audit_leakage(
    *,
    nct_id: str,
    in_audit: Any,
    shard_record: Mapping[str, Any] | None,
    index_row: Mapping[str, Any] | None,
    document_profile: Any,
    decision_rule_locations: Any,
    longitudinal_provenance: Any,
    raw_payload: Mapping[str, Any] | None,
) -> None:
    """The body of ``assert_no_audit_leakage`` (see there); messages may name the trial."""
    # Vector 1: explicit bool required.
    is_holdout = _require_strict_bool(in_audit, f"{nct_id}.in_audit")

    if shard_record is not None:
        if "in_audit" in shard_record:
            rec_flag = _require_strict_bool(shard_record["in_audit"], f"{nct_id}.shard_record.in_audit")
            if rec_flag != is_holdout:
                raise AuditFirewallError(
                    f"{nct_id}: shard_record.in_audit ({rec_flag}) disagrees with in_audit ({is_holdout})"
                )
        extra_rec_keys = sorted(set(shard_record.keys()) - set(SHARD_RECORD_KEYS))
        if extra_rec_keys:
            if is_holdout:
                raise AuditFirewallError(
                    f"{nct_id}: in_audit holdout leak (vector 3) — unexpected shard_record keys: "
                    f"{', '.join(extra_rec_keys)}"
                )
            raise OutcomesLayoutError(
                f"{nct_id}: unexpected shard_record keys: {', '.join(extra_rec_keys)}"
            )

    if index_row is not None:
        if "in_audit" in index_row:
            row_flag = _require_strict_bool(index_row["in_audit"], f"{nct_id}.index_row.in_audit")
            if row_flag != is_holdout:
                raise AuditFirewallError(
                    f"{nct_id}: index_row.in_audit ({row_flag}) disagrees with in_audit ({is_holdout})"
                )
        extra_row_keys = sorted(set(index_row.keys()) - set(INDEX_ROW_KEYS))
        if extra_row_keys:
            if is_holdout:
                raise AuditFirewallError(
                    f"{nct_id}: in_audit holdout leak (vector 3) — unexpected index_row keys: "
                    f"{', '.join(extra_row_keys)}"
                )
            raise OutcomesLayoutError(
                f"{nct_id}: unexpected index_row keys: {', '.join(extra_row_keys)}"
            )

    if not is_holdout:
        return

    # Holdout stub: the shard record carries nothing beyond nct_id, in_audit,
    # cohorts and shard (the fields that mirror the index row 1:1). Results,
    # documents, locations and provenance are all None (vectors 2 and 4 below
    # name the location and document cases; these two cover the rest).
    if shard_record is not None:
        for stub_key in ("reported_results", "longitudinal_provenance"):
            if shard_record.get(stub_key) is not None:
                raise AuditFirewallError(
                    f"{nct_id}: in_audit holdout leak (stub) — shard_record.{stub_key} must be None, "
                    f"got {type(shard_record.get(stub_key)).__name__}"
                )
    if index_row is not None and index_row.get("has_reported_results") is not None:
        raise AuditFirewallError(
            f"{nct_id}: in_audit holdout leak (stub) — index_row.has_reported_results must be None, "
            f"got {type(index_row.get('has_reported_results')).__name__}"
        )

    # Vector 2: decision_rule_locations must be strictly None.
    eff_locations = (
        decision_rule_locations
        if decision_rule_locations is not None
        else (shard_record.get("decision_rule_locations") if shard_record is not None else None)
    )
    if eff_locations is not None:
        raise AuditFirewallError(
            f"{nct_id}: in_audit holdout leak (vector 2) — decision_rule_locations must be None, "
            f"got {type(eff_locations).__name__}"
        )

    # Vector 3: forbidden LLM / assessment / worker / citation keys anywhere,
    # CT.gov's native sections of a raw record included (where only CT.gov's
    # own "locations" and "citations" are allowed).
    for label, payload, native in (
        ("shard_record", shard_record, frozenset()),
        ("index_row", index_row, frozenset()),
        ("raw_payload", raw_payload, _CTGOV_NATIVE_SECTIONS),
    ):
        if payload is None:
            continue
        forbidden_hits = _find_forbidden_keys_recursive(plain_data(payload, label), native_sections=native)
        if forbidden_hits:
            raise AuditFirewallError(
                f"{nct_id}: in_audit holdout leak (vector 3) — forbidden key(s) in {label}: "
                f"{', '.join(forbidden_hits[:5])}"
            )

    # Vector 4: PDF inspection / document_profile block.
    eff_profile = (
        document_profile
        if document_profile is not None
        else (shard_record.get("document_profile") if shard_record is not None else None)
    )
    if eff_profile is not None:
        raise AuditFirewallError(
            f"{nct_id}: in_audit holdout leak (vector 4) — document_profile must be None, "
            f"got {type(eff_profile).__name__}"
        )
    eff_prov = (
        longitudinal_provenance
        if longitudinal_provenance is not None
        else (shard_record.get("longitudinal_provenance") if shard_record is not None else None)
    )
    # The PDF-scraped check names the field that leaks; a holdout stub carries
    # no provenance at all, so anything that passes it is still refused.
    _check_provenance_pdf_leak(nct_id, eff_prov)
    if eff_prov is not None:
        raise AuditFirewallError(
            f"{nct_id}: in_audit holdout leak (vector 4) — longitudinal_provenance must be None for a "
            f"holdout stub, got {type(eff_prov).__name__}"
        )

    # Vector 5: index badge leak block.
    if index_row is not None:
        doc_badge = index_row.get("document_badge")
        if not isinstance(doc_badge, Mapping):
            raise AuditFirewallError(
                f"{nct_id}: in_audit holdout leak (vector 5) — document_badge missing or not an object"
            )
        if doc_badge.get("readability_status") != "in_audit_holdout":
            raise AuditFirewallError(
                f"{nct_id}: in_audit holdout leak (vector 5) — document_badge.readability_status is not "
                f"'in_audit_holdout' (got {type(doc_badge.get('readability_status')).__name__})"
            )
        for hidden_field in ("redaction_flags", "doc_count", "total_physical_pages"):
            if doc_badge.get(hidden_field) is not None:
                raise AuditFirewallError(
                    f"{nct_id}: in_audit holdout leak (vector 5) — document_badge.{hidden_field} must be None, "
                    f"got {type(doc_badge.get(hidden_field)).__name__}"
                )

        census_badge = index_row.get("census_badge")
        if not isinstance(census_badge, Mapping):
            raise AuditFirewallError(
                f"{nct_id}: in_audit holdout leak (vector 5) — census_badge missing or not an object"
            )
        if census_badge.get("census_status") != "in_audit_holdout":
            raise AuditFirewallError(
                f"{nct_id}: in_audit holdout leak (vector 5) — census_badge.census_status is not "
                f"'in_audit_holdout' (got {type(census_badge.get('census_status')).__name__})"
            )
        if census_badge.get("status_counts") is not None:
            raise AuditFirewallError(
                f"{nct_id}: in_audit holdout leak (vector 5) — census_badge.status_counts must be None, "
                f"got {type(census_badge.get('status_counts')).__name__}"
            )
        if census_badge.get("has_second_pass") is not False:
            raise AuditFirewallError(
                f"{nct_id}: in_audit holdout leak (vector 5) — census_badge.has_second_pass must be False, "
                f"got {type(census_badge.get('has_second_pass')).__name__}"
            )
        # The masked badges are fixed: each must be exactly the masked badge,
        # so a key beyond it (red-team m3 round 2: document_badge.results
        # carrying a holdout's results passed --check) is a leak. Key names
        # are withheld, as they may be trial IDs.
        for badge_name, badge, masked in (
            ("document_badge", doc_badge, _MASKED_DOCUMENT_BADGE),
            ("census_badge", census_badge, _MASKED_CENSUS_BADGE),
        ):
            if dict(badge) != masked:
                extra = sorted(withhold_nct_ids(str(k)) for k in badge if k not in masked)
                missing = sorted(k for k in masked if k not in badge)
                raise AuditFirewallError(
                    f"{nct_id}: in_audit holdout leak (vector 5) — {badge_name} is not exactly the masked "
                    f"badge (extra key(s): {', '.join(extra[:5]) or 'none'}; missing: {', '.join(missing) or 'none'})"
                )


# Alias matching architecture spec #61 naming.
enforce_audit_firewall = assert_no_audit_leakage


def _strict_id_set(nct_ids: Any, label: str) -> frozenset[str]:
    """Validate a set of trial IDs: a set (not a str or mapping) of strict IDs."""
    if isinstance(nct_ids, (str, bytes, Mapping)) or not isinstance(nct_ids, AbstractSet):
        raise OutcomesLayoutError(f"{label} must be a set of trial IDs, got {type(nct_ids).__name__}")
    return frozenset(strict_nct_id(item, f"{label} entry") for item in nct_ids)


def assert_no_audit_cross_stream_leakage(
    audit_nct_ids: AbstractSet[str],
    *,
    documents_by_nct: Mapping[str, Any] | None = None,
    locations_by_nct: Mapping[str, Any] | None = None,
    profiles_by_nct: Mapping[str, Any] | None = None,
    provenance_by_nct: Mapping[str, Any] | None = None,
    population_parsed_by_nct: Mapping[str, Any] | None = None,
    allowed_nct_ids: AbstractSet[str] | None = None,
) -> None:
    """Vector 6: abort BEFORE touching output dirs if any audit NCT appears in
    any side stream (documents, locations, document profiles, provenance or
    parsed populations), as a key or anywhere in a record.

    A holdout trial is written as a stub (see the module notes), so a side
    record for one could only be dropped, and dropping it silently would hide
    that the input carried holdout material. It is refused instead.

    Method, in order (red-team m3 round 1 found the guard comparing exact keys
    only, so a lowercase or padded key, or a record naming a holdout under
    another trial's key, passed):
    1. every key of every stream must be a strict ASCII ``^NCT[0-9]{8}$`` ID
       (a malformed key is refused, never compared as written: ``nct...`` or
       ``"NCT... "`` is the same trial to a reader); the audit set is checked
       the same way;
    2. a key that is an audit trial is a leak (counts and stream names only in
       the message, never an ID);
    3. with ``allowed_nct_ids`` (the build's trials), a key outside it is
       refused by count, before any record is read (skeptic review of m3:
       the identity walk read a trial-absent record first, and its refusal
       printed the record's content);
    4. every record is read with ``check_side_input_identity`` against its own
       key, as a trial outside the holdout: a nested trial-ID key, URL or
       mapping key naming another trial, a holdout flag that is not false, or
       any key or string naming an audit trial in any reading is refused.
    The check runs whether or not the audit set is empty: a record naming
    another trial is refused either way.
    """
    audit = _strict_id_set(audit_nct_ids, "audit_nct_ids")
    allowed = None if allowed_nct_ids is None else _strict_id_set(allowed_nct_ids, "allowed_nct_ids")
    streams: list[tuple[str, Mapping[str, Any]]] = []
    for stream_name, mapping in (
        ("locations", locations_by_nct),
        ("documents", documents_by_nct),
        ("document_profiles", profiles_by_nct),
        ("provenance", provenance_by_nct),
        ("population_parsed", population_parsed_by_nct),
    ):
        if mapping is None:
            continue
        if not isinstance(mapping, Mapping):
            raise OutcomesLayoutError(
                f"{stream_name} stream must be a mapping keyed by trial ID, got {type(mapping).__name__}"
            )
        streams.append((stream_name, mapping))
    for stream_name, mapping in streams:
        for key in mapping:
            strict_nct_id(key, f"{stream_name} stream key")
    for stream_name, mapping in streams:
        leaked = sum(1 for nct in mapping if nct in audit)
        if leaked:
            raise AuditFirewallError(
                f"in_audit holdout leak (vector 6) — {leaked} audit holdout trial(s) present "
                f"in {stream_name} stream (IDs withheld); aborting before touching output directory"
            )
    if allowed is not None:
        for stream_name, mapping in streams:
            outside = sum(1 for nct in mapping if nct not in allowed)
            if outside:
                raise OutcomesLayoutError(
                    f"{stream_name} stream: {outside} trial(s) absent from the cohort manifest; a record for a "
                    f"trial outside the build is refused unread, not dropped (IDs withheld)"
                )
    for stream_name, mapping in streams:
        for key, record in mapping.items():
            check_side_input_identity(
                record,
                expected_nct_id=key,
                expected_in_audit=False,
                label=f"{stream_name} record",
                audit_nct_ids=audit,
            )


# ── side-input identity: nested trial IDs and in_audit flags ─────────────────

# Module 4's URL keys (url, source_url, pdf_url, document_url, uri), folded.
_URL_KEY_FORMS: frozenset[str] = frozenset(_folded_key(k) for k in _M4_URL_FIELD_KEYS)
# The vector-3 keys, folded (read in any case, separator or invisible character).
_FORBIDDEN_AUDIT_KEY_FORMS: frozenset[str] = frozenset(_folded_key(k) for k in FORBIDDEN_AUDIT_KEYS)


def _iter_mapping_entries(obj: Any, path: str = "") -> Iterator[tuple[str, Any, Any]]:
    """Yield ``(path, key, value)`` for every mapping entry at any depth.

    Every Mapping is entered, and every Sequence but a string (a list, a
    tuple, a ``collections.UserList``: red-team m3 round 2 hid a holdout's
    rows in a UserList, which a walk over lists and tuples alone skipped), so
    nesting hides nothing; callers pass ``plain_data`` so a dataclass is read
    as its fields. ``path`` is the position of the value
    (``documents[0].url``); it may contain a key that is itself a trial ID, so
    callers withhold it before showing it.
    """
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            sub = f"{path}.{key}" if path else str(key)
            yield sub, key, value
            yield from _iter_mapping_entries(value, sub)
    elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes, bytearray)):
        for idx, item in enumerate(obj):
            yield from _iter_mapping_entries(item, f"{path}[{idx}]")


def _refuse_other_trial_in_text(text: str, *, expected_nct_id: str, where: str) -> None:
    """Refuse ``text`` (a URL or a mapping key) naming any trial but ``expected_nct_id``.

    Method: Module 4's readings. Every token of ``_iter_embedded_nct_tokens``
    (as written, percent-decoded, NFKC-folded) must be strict as written and
    the expected trial; every bounded loose rendering of
    ``_iter_loose_nct_candidates`` (``NCT 9900 0058``, a look-alike letter)
    must be the expected trial. The message names the position only.
    """
    for token in _iter_embedded_nct_tokens(text):
        if _strict_nct_id_or_none(token) is None:
            raise OutcomesLayoutError(
                f"{where} holds a malformed trial ID (expected strict ^NCT[0-9]{{8}}$); refused, never "
                f"read as written"
            )
        if token != expected_nct_id:
            raise OutcomesLayoutError(
                f"{where} names a different trial than the record it is filed under; a record is never "
                f"re-attributed"
            )
    for candidate, bounded in _iter_loose_nct_candidates(text):
        if bounded and candidate != expected_nct_id:
            raise OutcomesLayoutError(
                f"{where} names a different trial than the record it is filed under; a record is never "
                f"re-attributed"
            )


def _strict_nct_id_or_none(value: Any) -> str | None:
    """``value`` when it is a strict ASCII ``^NCT[0-9]{8}$`` string, else ``None``."""
    if type(value) is str and len(value) == 11 and NCT_RE.fullmatch(value):
        return value
    return None


def _scan_for_audit_trials(obj: Any, *, audit_nct_ids: frozenset[str], label: str) -> None:
    """Refuse ``obj`` when any key, string or split key/value reading names an audit trial.

    Method: Module 4's ``_scan_strings_for_audit_nct_tokens`` (every reading:
    as written, percent-decoded, NFKC-folded, loose, split across a key and
    its value or neighbouring list items), re-raised as
    ``AuditFirewallError`` with every NCT-like token withheld.
    """
    if not audit_nct_ids:
        return
    try:
        # Module 4's walk enters mappings, lists and tuples only: the input is
        # read as plain data first, so a dataclass or a UserList is scanned.
        _scan_strings_for_audit_nct_tokens(plain_data(obj, label), context=label, audit_nct_ids=audit_nct_ids)
    except CitationVerificationError as exc:
        raise AuditFirewallError(withhold_nct_ids(str(exc))) from None


def refuse_audit_ids(obj: Any, *, audit_nct_ids: AbstractSet[str], label: str) -> None:
    """Refuse ``obj`` when any key or string in it names a trial of ``audit_nct_ids``.

    The public form of the audit-ID scan (``_scan_for_audit_trials``), for
    input that is not a trial's record or side input but reaches the output
    all the same: a manifest container's ``run_id`` is published in
    ``index.json`` (red-team m3 round 2: one naming a holdout, with its
    results, was published unscanned). Raises ``AuditFirewallError`` with
    every NCT-like token withheld.
    """
    _scan_for_audit_trials(obj, audit_nct_ids=_strict_id_set(audit_nct_ids, "audit_nct_ids"), label=label)


def _refuse_named_ids_and_flags(
    entries: Sequence[tuple[str, Any, Any]],
    *,
    expected_nct_id: str,
    expected_in_audit: bool,
    label: str,
) -> None:
    """Rules 1 and 2 of ``check_side_input_identity`` (and of trial records).

    1. Every key read as a trial ID (``_TRIAL_ID_KEY_FORMS``, any case,
       separator or invisible character) holds a strict ID equal to
       ``expected_nct_id``; every key read as a list of trial IDs
       (``_TRIAL_ID_LIST_KEY_FORMS``) holds a list of such IDs
       (``OutcomesLayoutError``: exit 1 in the CLI).
    2. Every key a reader takes for the holdout flag (``is_holdout_flag_key``:
       Module 4's ``in_audit``, ``inAudit``, ``IN_AUDIT``, ``in-audit``,
       ``is_in_audit``, and any key spelling a holdout marker such as
       ``in_audit_holdout``) holds the bool ``expected_in_audit``; a non-bool
       or a disagreeing value is a conflicting holdout flag, refused as
       ``AuditFirewallError`` (exit 2), never resolved either way.
    Rule 1 runs over every entry before rule 2, so a malformed or foreign ID
    is reported as such wherever it stands.
    """
    for where, key, value in entries:
        form = _folded_key(key)
        shown = f"{label}.{withhold_nct_ids(where)}"
        if form in _TRIAL_ID_KEY_FORMS:
            checked = strict_nct_id(value, shown)
            if checked != expected_nct_id:
                raise OutcomesLayoutError(
                    f"{shown} names a different trial than the record it is filed under; a nested row "
                    f"is never re-attributed"
                )
        elif form in _TRIAL_ID_LIST_KEY_FORMS:
            if not isinstance(value, (list, tuple)):
                raise OutcomesLayoutError(f"{shown} must be a list of trial IDs, got {type(value).__name__}")
            for idx, item in enumerate(value):
                if strict_nct_id(item, f"{shown}[{idx}]") != expected_nct_id:
                    raise OutcomesLayoutError(
                        f"{shown}[{idx}] names a different trial than the record it is filed under"
                    )
    for where, key, value in entries:
        if is_holdout_flag_key(key) and (type(value) is not bool or value is not expected_in_audit):
            raise AuditFirewallError(
                f"{label}.{withhold_nct_ids(where)} is a holdout flag holding "
                f"{type(value).__name__} {value if type(value) is bool else '(not a bool)'}, but this "
                f"trial is in_audit={expected_in_audit}; a conflicting holdout flag is refused"
            )


def check_side_input_identity(
    obj: Any,
    *,
    expected_nct_id: str,
    expected_in_audit: bool,
    label: str,
    audit_nct_ids: AbstractSet[str] = frozenset(),
) -> None:
    """Refuse a side input (document profile, locations, provenance, parsed
    populations) that names another trial or contradicts the trial's
    ``in_audit``.

    Method: a side record is filed under one trial and published in that
    trial's shard, so every place a reader takes for a trial ID must name
    that trial. Module 4's readings are applied (red-team m3 round 1 found
    this check reading only keys that folded to ``nctid`` and ``inaudit``),
    in this order, at any depth:
    1. trial-ID keys and trial-ID list keys hold ``expected_nct_id``
       (``_refuse_named_ids_and_flags`` rule 1; exit 1);
    2. holdout flags under any spelling hold ``expected_in_audit``
       (rule 2; ``AuditFirewallError``, exit 2);
    3. URL keys (Module 4's ``url``, ``source_url``, ``pdf_url``,
       ``document_url``, ``uri``, folded) name no trial but this one
       (``_refuse_other_trial_in_text``: a CT.gov large-document URL names its
       trial);
    4. no mapping key names another trial (``{"<other ID>": {...}}`` files
       material for that trial under this one);
    5. no key, string or split key/value reading names a trial of
       ``audit_nct_ids`` (other than this one) in any reading
       (``AuditFirewallError``).
    Free text that names a trial outside the holdout (a publication citing a
    related trial) is not refused: it is not filed as that trial's material.
    Messages name the position, never the ID found there. ``obj`` is read as
    ``plain_data`` (a dataclass by its fields, any Sequence as a list).
    """
    expected = strict_nct_id(expected_nct_id, f"{label}: expected nct_id")
    expected_flag = _require_strict_bool(expected_in_audit, f"{label}: expected in_audit")
    audit = _strict_id_set(audit_nct_ids, "audit_nct_ids")
    obj = plain_data(obj, label)
    entries = list(_iter_mapping_entries(obj))
    _refuse_named_ids_and_flags(entries, expected_nct_id=expected, expected_in_audit=expected_flag, label=label)
    for where, key, value in entries:
        if _folded_key(key) in _URL_KEY_FORMS and isinstance(value, str):
            _refuse_other_trial_in_text(value, expected_nct_id=expected, where=f"{label}.{withhold_nct_ids(where)}")
    for where, key, _value in entries:
        _refuse_other_trial_in_text(
            key if isinstance(key, str) else str(key),
            expected_nct_id=expected,
            where=f"{label}.{withhold_nct_ids(where)} (a key)",
        )
    _scan_for_audit_trials(obj, audit_nct_ids=audit - {expected}, label=label)


# CT.gov v2's position of another record's ID: the expanded-access record's
# NCT number, under exactly this route and spelling.
_EXPANDED_ACCESS_ID_ROUTE: tuple[str, ...] = ("protocolSection", "statusModule", "expandedAccessInfo")


def _without_expanded_access_record_id(trial: dict[str, Any], *, label: str) -> dict[str, Any]:
    """``trial`` without CT.gov's ``expandedAccessInfo.nctId`` (a strict ID or null, else refused).

    Method: the route is followed by structure (``_EXPANDED_ACCESS_ID_ROUTE``,
    each step an object), never by a path string a key could imitate; only
    the objects on the route are copied, the input is not changed. Any other
    spelling of the key there is still read by rule 1.
    """
    node: Any = trial
    for step in _EXPANDED_ACCESS_ID_ROUTE:
        node = node.get(step) if isinstance(node, dict) else None
    if not isinstance(node, dict) or "nctId" not in node:
        return trial
    if node["nctId"] is not None:
        strict_nct_id(node["nctId"], f"{label}.{'.'.join(_EXPANDED_ACCESS_ID_ROUTE)}.nctId")
    copied = dict(trial)
    parent = copied
    for step in _EXPANDED_ACCESS_ID_ROUTE[:-1]:
        parent[step] = dict(parent[step])
        parent = parent[step]
    parent[_EXPANDED_ACCESS_ID_ROUTE[-1]] = {k: v for k, v in node.items() if k != "nctId"}
    return copied


def check_trial_record_identity(
    trial: Mapping[str, Any],
    *,
    expected_nct_id: str,
    expected_in_audit: bool,
    audit_nct_ids: AbstractSet[str] = frozenset(),
    label: str = "trial record",
) -> None:
    """Refuse a trial record (a --trials or manifest row) that names another
    trial under a trial-ID key or carries a conflicting holdout flag.

    Method: rules 1 and 2 of ``check_side_input_identity`` at any depth (a
    nested object stamped with another trial's ID, or a nested ``in_audit``
    true in an in-scope trial's record, would otherwise be published in this
    trial's shard; red-team m3 round 1), then, for a trial outside the
    holdout, rule 5: no key or string names an audit trial in any reading. A
    CT.gov record's URLs and free text name related trials for good reasons,
    so rules 3 and 4 (other trials in URLs and keys) are not applied; Module 4
    likewise reads registry text for audit IDs only. A holdout's own record is
    not scanned for text: it is reduced to its stub and never published.

    One CT.gov v2 field is another record's ID by its schema:
    ``protocolSection.statusModule.expandedAccessInfo.nctId``, the NCT number
    of the trial's expanded-access record. It is a strict ID (or null), and
    rule 1 does not read it as this record's (skeptic review of m3: any trial
    with an expanded-access program refused the whole --trials build); the
    audit scan still reads it, so one naming a holdout is refused.
    """
    expected = strict_nct_id(expected_nct_id, f"{label}: expected nct_id")
    expected_flag = _require_strict_bool(expected_in_audit, f"{label}: expected in_audit")
    audit = _strict_id_set(audit_nct_ids, "audit_nct_ids")
    trial = plain_data(trial, label)
    _refuse_named_ids_and_flags(
        list(_iter_mapping_entries(_without_expanded_access_record_id(trial, label=label))),
        expected_nct_id=expected,
        expected_in_audit=expected_flag,
        label=label,
    )
    if not expected_flag:
        _scan_for_audit_trials(trial, audit_nct_ids=audit - {expected}, label=label)


def _refuse_holdout_flags(obj: Any, *, label: str) -> None:
    """Refuse ``obj`` when any holdout flag in it, at any depth and under any
    spelling a reader takes for one (``is_holdout_flag_key``), is not exactly
    False.

    Used by the public section builders, which build a section that only an
    in-scope trial carries: input that says it is a holdout's (or says
    anything but False) is refused as ``AuditFirewallError``. ``obj`` is read
    as ``plain_data``, so a dataclass or a UserList is read too (red-team m3
    round 2: a dataclass population with ``in_audit`` true was published).
    """
    for where, key, value in _iter_mapping_entries(plain_data(obj, label)):
        if is_holdout_flag_key(key) and value is not False:
            raise AuditFirewallError(
                f"{label}.{withhold_nct_ids(where)} is a holdout flag that is not false; a holdout's "
                f"stub carries no {label}, so this input is refused"
            )


# ── cohort derivation ─────────────────────────────────────────────────────────

def _is_phase3(phase: Any) -> bool:
    """True when phase indicates a Phase 3 (or Phase 2/3) clinical trial."""
    if isinstance(phase, str):
        norm = re.sub(r"[\s_\-/]+", "", phase.upper())
        return "PHASE3" in norm
    if isinstance(phase, Sequence) and not isinstance(phase, (str, bytes)):
        return any(_is_phase3(p) for p in phase)
    return False


def derive_cohorts(
    explicit_cohorts: Sequence[str] | None = None,
    *,
    ecosystem_category: str | None = None,
    sponsor_class: str | None = None,
    phase: Any = None,
    protocol: bool = False,
    sap: bool = False,
    include_all_large_docs: bool = True,
) -> list[str]:
    """Return a non-empty sorted unique list of valid ``COHORT_RE`` slugs.

    Combines any explicit cohort memberships with deterministic derivations from
    Module 2 ``ecosystem_category``, registry ``sponsor_class``/``phase``, and
    large-document presence (``protocol`` or ``sap`` -> ``"all_large_docs"``).

    A refusal names the position and the type, never the value: cohorts and
    ecosystem_category come from the trial's own record, and a holdout's
    record placed results text there, which the refusal printed (red-team m3
    round 3).
    """
    found: set[str] = set()
    if explicit_cohorts is not None:
        if isinstance(explicit_cohorts, (str, bytes)) or not isinstance(explicit_cohorts, Sequence):
            raise OutcomesLayoutError(
                f"cohorts must be a sequence of strings, got {type(explicit_cohorts).__name__}"
            )
        for pos, raw_slug in enumerate(explicit_cohorts):
            if not isinstance(raw_slug, str):
                raise OutcomesLayoutError(f"cohort slug at position {pos} must be a string, got {type(raw_slug).__name__}")
            slug = ECOSYSTEM_SECTOR_TO_COHORT.get(raw_slug, raw_slug)
            if not COHORT_RE.fullmatch(slug):
                raise OutcomesLayoutError(
                    f"invalid cohort slug at position {pos}: it must match {COHORT_RE.pattern} (value not shown)"
                )
            found.add(slug)

    if ecosystem_category is not None:
        mapped = ECOSYSTEM_SECTOR_TO_COHORT.get(ecosystem_category, ecosystem_category)
        if mapped == "industry":
            if _is_phase3(phase):
                found.add("industry_phase3")
        elif COHORT_RE.fullmatch(mapped):
            found.add(mapped)
        else:
            raise OutcomesLayoutError(
                f"invalid ecosystem_category slug: it must be a Module 2 sector or match {COHORT_RE.pattern} "
                f"(value not shown)"
            )

    if isinstance(sponsor_class, str):
        sc_upper = sponsor_class.strip().upper()
        if sc_upper == "INDUSTRY" and _is_phase3(phase):
            found.add("industry_phase3")
        elif sc_upper == "NIH":
            found.add("nih")
        elif sc_upper == "FED":
            found.add("us_federal_other")
        elif sc_upper == "NETWORK":
            found.add("cooperative_group")

    if include_all_large_docs and (protocol or sap):
        found.add("all_large_docs")

    if not found:
        raise OutcomesLayoutError(
            "trial resolved to zero cohorts; pass explicit cohorts (the builder's --manifest-cohort) "
            "or valid sponsor/document metadata"
        )
    return sorted(found)


# ── section 1: reported_results ───────────────────────────────────────────────

# The text fields of a published primary outcome measure, each with the
# spellings read on input (the published key, then CT.gov's camelCase one).
_MEASURE_FIELD_SPELLINGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("title", ("title",)),
    ("description", ("description",)),
    ("time_frame", ("time_frame", "timeFrame")),
    ("reporting_status", ("reporting_status", "reportingStatus")),
    ("param_type", ("param_type", "paramType")),
    ("dispersion_type", ("dispersion_type", "dispersionType")),
    ("unit_of_measure", ("unit_of_measure", "unitOfMeasure")),
    ("population_description", ("population_description", "populationDescription")),
    ("type_units_analyzed", ("type_units_analyzed", "typeUnitsAnalyzed")),
)
# The list fields of a published measure, copied verbatim.
_MEASURE_LIST_FIELDS: tuple[str, ...] = ("groups", "denoms", "classes", "analyses")
# Every key a pre-built primary measure may carry: the published keys and the
# camelCase spellings read for them. A pre-built block is the published shape,
# so any other key (red-team m3 round 3: an llm_assessment carrying results)
# would be dropped, and is refused. A CT.gov measure is a projection (its other
# CT.gov fields are not published by design) and is not held to this list.
_PREBUILT_MEASURE_KEYS: frozenset[str] = frozenset({
    "outcome_index",
    "analysis_population_parsed",
    *_MEASURE_LIST_FIELDS,
    *(spelling for _field, spellings in _MEASURE_FIELD_SPELLINGS for spelling in spellings),
})
# Every key a pre-built registered primary outcome may carry.
_PREBUILT_REGISTERED_KEYS: frozenset[str] = frozenset({"outcome_index", "measure", "time_frame", "timeFrame", "description"})


def _one_spelling_of(obj: Mapping[str, Any], spellings: Sequence[str], label: str) -> Any:
    """The value of a field given under one or more of ``spellings`` (None when absent).

    Given under two spellings, the two must be the same data, or the input
    does not say which it means and is refused, never resolved by precedence.
    """
    present = [key for key in spellings if key in obj]
    if not present:
        return None
    first = obj[present[0]]
    for other in present[1:]:
        if canonicalize_nested(obj[other]) != canonicalize_nested(first):
            raise OutcomesLayoutError(
                f"{label}: {present[0]} and {other} are both given and differ; conflicting spellings are "
                f"refused, not resolved"
            )
    return first


def _outcome_type_is_primary(value: Any, label: str) -> bool:
    """True for a CT.gov ``PRIMARY`` outcome type, False for another type or none.

    Method: ``None`` (not stated) is not primary. Anything else must be a
    string that is, ignoring letter case only, one of ``OUTCOME_MEASURE_TYPES``;
    nothing is stripped. A padded or zero-width spelling of ``PRIMARY`` was
    read as another type and its row skipped (red-team m3 round 3), so a
    value outside the vocabulary is refused. The value is never quoted.
    """
    if value is None:
        return False
    if type(value) is not str or value.upper() not in OUTCOME_MEASURE_TYPES:
        raise OutcomesLayoutError(
            f"{label} must be one of {OUTCOME_MEASURE_TYPES} (any letter case, nothing around it), got "
            f"{type(value).__name__} outside that vocabulary; refused rather than read as another type"
        )
    return value.upper() == "PRIMARY"


def _normalize_primary_outcome_measure(
    measure: Mapping[str, Any],
    *,
    outcome_index: int,
    parsed_pop: Mapping[str, Any] | None = None,
    prebuilt: bool = False,
) -> dict[str, Any]:
    """Project one CT.gov PRIMARY outcome measure verbatim (no rounding/coercion).

    Each text field is copied as given under its published or CT.gov
    spelling (``_one_spelling_of``: both given and different is refused).
    Each list field is copied as given; absent or null is an empty list, and
    any other value is refused rather than replaced by an empty list. A
    pre-built measure (``prebuilt``) may carry only ``_PREBUILT_MEASURE_KEYS``
    (and holdout flags, which ``extract_reported_results`` has read as
    False): any other key would be dropped, so it is refused.
    """
    where = f"primary outcome {outcome_index}"
    if prebuilt:
        unknown = sorted(
            withhold_nct_ids(str(key))
            for key in measure
            if key not in _PREBUILT_MEASURE_KEYS and not is_holdout_flag_key(key)
        )
        if unknown:
            raise OutcomesLayoutError(
                f"reported_results.primary_outcome_measures: {where} carries {len(unknown)} key(s) a published "
                f"measure does not have ({', '.join(unknown[:5])}); they would be dropped, so they are refused"
            )
    lists: dict[str, list[Any]] = {}
    for list_key in _MEASURE_LIST_FIELDS:
        value = measure.get(list_key)
        if value is None:
            lists[list_key] = []
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            lists[list_key] = list(value)
        else:
            raise OutcomesLayoutError(
                f"{where}: {list_key} must be a list or null, got {type(value).__name__}; it is never replaced "
                f"by an empty list"
            )

    # The measure's own analysis_population_parsed (carried inline) and the
    # supplied parsed population are two inputs for one field: when both are
    # given they must be the same object, or the inputs conflict and are
    # refused, never resolved (red-team m3 round 2: the supplied one was
    # published and the inline one dropped). An inline value that is not an
    # object is refused rather than published as null.
    inline_pop = measure.get("analysis_population_parsed")
    if inline_pop is not None and not isinstance(inline_pop, Mapping):
        raise OutcomesLayoutError(
            f"primary outcome {outcome_index}: analysis_population_parsed must be an object or null, got "
            f"{type(inline_pop).__name__}"
        )
    parsed_out: dict[str, Any] | None
    if parsed_pop is not None:
        parsed_out = canonicalize_nested(plain_data(parsed_pop, "parsed population"))
        if inline_pop is not None and canonicalize_nested(plain_data(inline_pop, "analysis_population_parsed")) != parsed_out:
            raise OutcomesLayoutError(
                f"primary outcome {outcome_index}: the measure carries analysis_population_parsed inline and the "
                f"parsed populations give a different one; conflicting inputs are refused, not resolved"
            )
    elif inline_pop is not None:
        parsed_out = canonicalize_nested(plain_data(inline_pop, "analysis_population_parsed"))
    else:
        parsed_out = None

    return canonicalize_nested({
        "outcome_index": outcome_index,
        **{field: _one_spelling_of(measure, spellings, where) for field, spellings in _MEASURE_FIELD_SPELLINGS},
        **lists,
        "analysis_population_parsed": parsed_out,
    })


def _extract_parsed_population_map(
    parsed_populations: Any,
) -> dict[int, dict[str, Any]]:
    """Index Module 2 ParsedOutcomePopulation outputs by outcome_index.

    Method: the input is read as ``plain_data`` (Module 2's dataclasses by
    their fields), then must be a mapping carrying an ``outcomes`` list, or a
    list of rows; every row must be an object. A row whose ``outcome_type`` is
    stated and is another CT.gov type is skipped on purpose: this section
    carries only the primary measures. An ``outcome_type`` outside CT.gov's
    vocabulary (``_outcome_type_is_primary``: a padded or zero-width
    ``PRIMARY``, red-team m3 round 3) is refused, never skipped. A primary row is keyed by its
    ``outcome_index`` (an int; any other value is refused), or by its
    position when it states none. Anything that would be dropped or
    resolved without a word is refused: input of another shape, a row that
    is not an object, and two rows for one index (red-team m3 round 2: the
    second silently replaced the first).
    """
    if parsed_populations is None:
        return {}
    plain = plain_data(parsed_populations, "parsed populations")
    items: list[Any]
    if isinstance(plain, dict):
        raw_outcomes = plain.get("outcomes")
        if not isinstance(raw_outcomes, list):
            raise OutcomesLayoutError(
                f"parsed populations must carry an 'outcomes' list, got {type(raw_outcomes).__name__}"
            )
        items = raw_outcomes
    elif isinstance(plain, list):
        items = plain
    else:
        raise OutcomesLayoutError(
            f"parsed populations must be an object with an 'outcomes' list or a list of rows, got "
            f"{type(plain).__name__}"
        )

    by_index: dict[int, dict[str, Any]] = {}
    for pos, row in enumerate(items):
        if not isinstance(row, dict):
            raise OutcomesLayoutError(f"parsed populations: row {pos} must be an object, got {type(row).__name__}")
        otype = row.get("outcome_type")
        if otype is not None and not _outcome_type_is_primary(otype, f"parsed populations: row {pos} outcome_type"):
            continue
        idx = row.get("outcome_index")
        if idx is not None and type(idx) is not int:
            raise OutcomesLayoutError(
                f"parsed populations: row {pos} outcome_index must be an int, got {type(idx).__name__}"
            )
        key = idx if idx is not None else pos
        if key in by_index:
            raise OutcomesLayoutError(
                f"parsed populations: two primary rows for outcome index {key}; conflicting inputs are refused, "
                f"not resolved"
            )
        by_index[key] = row
    return by_index


# Module 2's TrialAnalysisPopulations fields published with a trial's
# results, as ``reported_results.analysis_populations``: every field but
# ``nct_id`` (the trial's own, checked against it) and ``outcomes`` (each row
# is published with its measure as ``analysis_population_parsed``). The
# record's provenance (parser_rules_version, vocabulary_sha256,
# snapshot_date) stays with the trial it describes (skeptic review of m3: only
# ``outcomes`` was read, and the hash and snapshot date were dropped).
POPULATION_RECORD_FIELDS: frozenset[str] = frozenset({
    "snapshot_date",
    "enrollment",
    "has_results_section",
    "has_participant_flow",
    "n_outcomes_total",
    "n_primary_outcomes",
    "primary_population_classes",
    "parse_ok",
    "parser_rules_version",
    "vocabulary_sha256",
})


def _population_record_fields(fields: Mapping[str, Any], label: str) -> dict[str, Any] | None:
    """Module 2 trial-level fields, as given, or refused; None when there are none.

    Method: every key must be one of ``POPULATION_RECORD_FIELDS`` (any other
    would be dropped, so it is refused); ``primary_population_classes`` is a
    list of strings or null, every other value a scalar (a string, number or
    bool) or null, never an object. Messages name keys and types, never values.
    """
    unknown = sorted(withhold_nct_ids(str(key)) for key in fields if key not in POPULATION_RECORD_FIELDS)
    if unknown:
        raise OutcomesLayoutError(
            f"{label}: the record carries {len(unknown)} key(s) the builder does not publish "
            f"({', '.join(unknown[:5])}); they would be dropped, so they are refused"
        )
    for key, value in fields.items():
        if value is None:
            continue
        if key == "primary_population_classes":
            if not isinstance(value, list) or not all(type(item) is str for item in value):
                raise OutcomesLayoutError(f"{label}.{key} must be a list of strings or null, got {type(value).__name__}")
        elif type(value) not in (str, int, float, bool):
            raise OutcomesLayoutError(f"{label}.{key} must be a scalar or null, got {type(value).__name__}")
    return canonicalize_nested(dict(fields)) if fields else None


def _population_record(parsed_populations: Any) -> dict[str, Any] | None:
    """The trial-level fields of a Module 2 record (``_population_record_fields``).

    Method: an object carrying ``outcomes`` is a ``TrialAnalysisPopulations``
    record: its keys but ``outcomes``, ``nct_id`` and holdout flags (read by
    the caller, False) are its trial-level fields. A list of rows has none.
    """
    if not isinstance(parsed_populations, dict):
        return None
    fields = {
        key: value
        for key, value in parsed_populations.items()
        if key not in ("outcomes", "nct_id") and not is_holdout_flag_key(key)
    }
    return _population_record_fields(fields, "parsed populations")


def _attach_populations(
    measures: Sequence[tuple[Mapping[str, Any], int, tuple[int, ...]]],
    pop_by_idx: Mapping[int, Mapping[str, Any]],
    *,
    prebuilt: bool = False,
) -> list[dict[str, Any]]:
    """Project each primary measure with the parsed population filed for it.

    Method: ``measures`` holds ``(measure, outcome_index, keys)``, where
    ``keys`` are the parsed-population indexes that may name the measure, in
    order of preference (its position among all CT.gov measures, then among
    the primary ones; for a pre-built measure, its own index). The first key
    with a row is used. A row attached to two measures, or attached to none
    (an index the results do not have; red-team m3 round 2: a row for
    outcome 5 of a trial with one primary measure was dropped), is refused:
    a parsed population is published with its measure or the build stops.
    """
    used: dict[int, int] = {}
    projected: list[dict[str, Any]] = []
    for measure, outcome_index, keys in measures:
        parsed_pop: Mapping[str, Any] | None = None
        for key in keys:
            if key in pop_by_idx:
                if key in used:
                    raise OutcomesLayoutError(
                        f"parsed populations: the row for outcome index {key} would be attached to primary "
                        f"outcomes {used[key]} and {outcome_index}; refused as ambiguous"
                    )
                used[key] = outcome_index
                parsed_pop = pop_by_idx[key]
                break
        projected.append(
            _normalize_primary_outcome_measure(
                measure, outcome_index=outcome_index, parsed_pop=parsed_pop, prebuilt=prebuilt
            )
        )
    unused = sorted(set(pop_by_idx) - set(used))
    if unused:
        raise OutcomesLayoutError(
            f"parsed populations: {len(unused)} primary row(s) name an outcome index the trial's results do not "
            f"have (e.g. {unused[0]}); a row is refused, never dropped"
        )
    return projected


_PREBUILT_RESULTS_KEYS: frozenset[str] = frozenset({
    "section_label",
    "results_posted",
    "registered_primary_outcomes",
    "primary_outcome_measures",
    "analysis_populations",
})


def _prebuilt_indexes(rows: Sequence[Mapping[str, Any]], label: str) -> list[int]:
    """The outcome index of each pre-built row: its position, which a stated ``outcome_index`` must be.

    Method: the builder numbers registered primary outcomes and primary
    measures by their position in CT.gov's lists, so a block it writes holds
    ``0..n-1`` in order, and a pre-built block is held to that. A stated
    ``outcome_index`` must be a non-negative int (any other value is
    refused, never replaced by the row's position; red-team m3 round 3:
    ``"1"`` became 0 and two measures shared index 0) and the row's
    position: registered outcomes numbered 0 and 7, or a lone measure for
    outcome 9, name outcomes the list does not hold, and location rows were
    then bounded by the list's length, so rows for an absent outcome 1 were
    published and rows for the stated outcome 7 refused (skeptic review of
    m3). Such a block is refused.
    """
    indexes: list[int] = []
    for pos, row in enumerate(rows):
        if "outcome_index" in row:
            own = row["outcome_index"]
            if type(own) is not int or own < 0:
                raise OutcomesLayoutError(
                    f"{label}[{pos}].outcome_index must be a non-negative int, got {type(own).__name__}; it is "
                    f"never replaced by the row's position"
                )
            if own != pos:
                raise OutcomesLayoutError(
                    f"{label}[{pos}].outcome_index is {own}, not its position {pos}; primary outcomes are numbered "
                    f"by position (CT.gov's order), so an index elsewhere names an outcome the list does not hold "
                    f"and the block is refused"
                )
        indexes.append(pos)
    return indexes


def primary_outcomes_count_of(
    stated: int | None,
    reported_results: Mapping[str, Any],
    *,
    label: str = "trial",
) -> int | None:
    """A trial's primary outcome count: its registered outcomes' when it has any.

    Method: the count bounds the trial's location rows (an outcome index at
    or past it is refused) and sets the outcomes a complete census covers,
    so it is the trial's own wherever the trial says: the number of
    ``registered_primary_outcomes`` (numbered ``0..n-1``,
    ``_prebuilt_indexes``) when the results block has any, and a ``stated``
    count (the manifest row's ``primary_outcomes_count`` or
    ``n_primary_outcomes``) that differs from it is refused, never taken over
    it (skeptic review of m3: a live row's n_primary_outcomes 2 beside a
    record registering one outcome published location rows for outcome 1,
    and --check passed them). With no registered outcome, the stated count;
    else the number of posted primary measures; else None (unknown). The
    builder writes the result as the index row's ``primary_outcomes_count``,
    and --check recomputes it from the shard the same way.
    """
    registered = reported_results.get("registered_primary_outcomes")
    measures = reported_results.get("primary_outcome_measures")
    n_registered = len(registered) if isinstance(registered, list) else 0
    if n_registered:
        if stated is not None and stated != n_registered:
            raise OutcomesLayoutError(
                f"{label}: the stated primary outcome count ({stated}) is not the {n_registered} primary "
                f"outcome(s) the trial's record registers; a count the record contradicts is refused, never "
                f"taken over the record's own outcomes"
            )
        return n_registered
    if stated is not None:
        return stated
    return len(measures) if isinstance(measures, list) and measures else None


def _registered_measure_text(value: Any, where: str) -> str | None:
    """A registered primary outcome's ``measure``: a string as given, or None when absent.

    Method: absence is kept as None, never written as ``""``, and an object
    is refused, never published as its ``str()`` (red-team m3 round 3: a null
    measure became ``""`` and an object measure its text).
    """
    if value is None or type(value) is str:
        return value
    raise OutcomesLayoutError(
        f"{where}.measure must be a string or null, got {type(value).__name__}; an object is never published as "
        f"its text"
    )


def _list_of_objects(value: Any, label: str) -> list[Mapping[str, Any]]:
    """``value`` as a list of objects (absent is empty); anything else is refused, not skipped."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise OutcomesLayoutError(f"{label} must be a list, got {type(value).__name__}")
    for pos, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise OutcomesLayoutError(f"{label}[{pos}] must be an object, got {type(item).__name__}")
    return value


def extract_reported_results(
    study: Mapping[str, Any],
    *,
    population_parser: Callable[[Mapping[str, Any]], Any] | None = None,
    parsed_populations: Any = None,
) -> dict[str, Any]:
    """Build the factual ``reported_results`` block from a CT.gov study or pre-built block.

    Preserves verbatim strings (including ``pValue``, confidence intervals, and
    ``populationDescription``) without rounding or evaluative claims. Absence
    is kept as ``None``.

    A holdout's stub carries no extracted results, so a study that says it is
    a holdout's (a holdout flag under any spelling, at any depth, that is not
    exactly False: ``_refuse_holdout_flags``) is refused with
    ``AuditFirewallError`` before anything is read from it, as are the parsed
    populations supplied with it and the population parser's result
    (red-team m3 round 2: the parser ran after every check and its result was
    published unread). The study, the parsed populations and the parser's
    result are read as ``plain_data``, so a Module 2 dataclass is read by its
    fields.

    Nothing supplied is dropped or resolved without a word: a pre-built
    ``reported_results`` alongside the record's own CT.gov outcomes
    (``resultsSection`` or ``protocolSection.outcomesModule``), a pre-built
    block with a key this block does not have, a ``hasResults`` that
    disagrees with the pre-built ``results_posted``, a parsed population for
    an outcome index the results do not have, or one that differs from the
    measure's own inline ``analysis_population_parsed`` is refused, as is a
    pre-built outcome whose ``outcome_index`` is not its position
    (``_prebuilt_indexes``).

    A Module 2 ``TrialAnalysisPopulations`` record (an object with an
    ``outcomes`` list) has its trial-level fields, provenance included,
    published as ``analysis_populations`` (``POPULATION_RECORD_FIELDS``; a
    key outside them is refused); a list of rows, or no populations, gives
    None. A pre-built block's own ``analysis_populations`` and a supplied
    record must agree.
    """
    study = plain_data(study, "study")
    if not isinstance(study, dict):
        raise OutcomesLayoutError(f"study must be an object, got {type(study).__name__}")
    _refuse_holdout_flags(study, label="reported_results")
    if parsed_populations is not None:
        parsed_populations = plain_data(parsed_populations, "parsed populations")
        _refuse_holdout_flags(parsed_populations, label="reported_results")

    has_results_flag = study.get("hasResults")
    if has_results_flag is not None and type(has_results_flag) is not bool:
        raise OutcomesLayoutError(f"hasResults must be a bool or absent, got {type(has_results_flag).__name__}")

    protocol_sec = study.get("protocolSection") if isinstance(study.get("protocolSection"), Mapping) else {}
    pre = study.get("reported_results")
    if pre is not None:
        if not isinstance(pre, Mapping):
            raise OutcomesLayoutError(f"reported_results must be an object or null, got {type(pre).__name__}")
        if study.get("resultsSection") is not None or protocol_sec.get("outcomesModule") is not None:
            raise OutcomesLayoutError(
                "the record carries a pre-built reported_results and its own CT.gov outcomes (resultsSection or "
                "protocolSection.outcomesModule); one would be dropped, so conflicting inputs are refused"
            )
        unknown = sorted(withhold_nct_ids(str(k)) for k in pre if k not in _PREBUILT_RESULTS_KEYS)
        if unknown:
            raise OutcomesLayoutError(
                f"reported_results carries key(s) this block does not have ({', '.join(unknown[:5])}); they would "
                f"be dropped, so they are refused"
            )
        label = pre.get("section_label")
        if label is not None and label != SECTION_LABEL_REPORTED_RESULTS:
            raise OutcomesLayoutError(
                f"reported_results.section_label must be {SECTION_LABEL_REPORTED_RESULTS!r} when given"
            )
        reg = _list_of_objects(pre.get("registered_primary_outcomes"), "reported_results.registered_primary_outcomes")
        measures = _list_of_objects(pre.get("primary_outcome_measures"), "reported_results.primary_outcome_measures")
        results_posted = pre.get("results_posted")
        if results_posted is None:
            # Not stated: posted measures show results were posted; with none,
            # whether results exist is unknown, not "no".
            results_posted = True if measures else None
        elif type(results_posted) is not bool:
            raise OutcomesLayoutError(
                f"reported_results.results_posted must be a bool or None, got {type(results_posted).__name__}"
            )
        if has_results_flag is not None and results_posted is not None and has_results_flag is not results_posted:
            raise OutcomesLayoutError(
                "the record's hasResults disagrees with its pre-built reported_results.results_posted; "
                "conflicting inputs are refused, not resolved"
            )
        pop_by_idx = _extract_parsed_population_map(parsed_populations)
        supplied_record = _population_record(parsed_populations)
        pre_record_raw = pre.get("analysis_populations")
        if pre_record_raw is not None and not isinstance(pre_record_raw, Mapping):
            raise OutcomesLayoutError(
                f"reported_results.analysis_populations must be an object or null, got {type(pre_record_raw).__name__}"
            )
        pre_record = (
            _population_record_fields(pre_record_raw, "reported_results.analysis_populations")
            if pre_record_raw is not None
            else None
        )
        if pre_record is not None and supplied_record is not None and pre_record != supplied_record:
            raise OutcomesLayoutError(
                "reported_results.analysis_populations and the parsed populations' record differ; conflicting "
                "inputs are refused, not resolved"
            )
        population_record = pre_record if pre_record is not None else supplied_record
        indexed: list[tuple[Mapping[str, Any], int, tuple[int, ...]]] = []
        for own, m in zip(_prebuilt_indexes(measures, "reported_results.primary_outcome_measures"), measures):
            indexed.append((m, own, (own,)))
        norm_measures = _attach_populations(indexed, pop_by_idx, prebuilt=True)
        norm_reg: list[dict[str, Any]] = []
        for own, r in zip(_prebuilt_indexes(reg, "reported_results.registered_primary_outcomes"), reg):
            where = f"reported_results.registered_primary_outcomes[{own}]"
            unknown = sorted(
                withhold_nct_ids(str(key))
                for key in r
                if key not in _PREBUILT_REGISTERED_KEYS and not is_holdout_flag_key(key)
            )
            if unknown:
                raise OutcomesLayoutError(
                    f"{where} carries {len(unknown)} key(s) a registered outcome does not have "
                    f"({', '.join(unknown[:5])}); they would be dropped, so they are refused"
                )
            norm_reg.append(canonicalize_nested({
                "outcome_index": own,
                "measure": _registered_measure_text(r.get("measure"), where),
                "time_frame": _one_spelling_of(r, ("time_frame", "timeFrame"), where),
                "description": r.get("description"),
            }))
        return canonicalize_nested({
            "section_label": SECTION_LABEL_REPORTED_RESULTS,
            "results_posted": results_posted,
            "registered_primary_outcomes": norm_reg,
            "primary_outcome_measures": norm_measures,
            "analysis_populations": population_record,
        })

    results_sec = study.get("resultsSection") if isinstance(study.get("resultsSection"), Mapping) else {}

    outcomes_mod = (
        protocol_sec.get("outcomesModule")
        if isinstance(protocol_sec.get("outcomesModule"), Mapping)
        else {}
    )
    raw_primary_outcomes = outcomes_mod.get("primaryOutcomes")
    registered: list[dict[str, Any]] = []
    if isinstance(raw_primary_outcomes, Sequence) and not isinstance(raw_primary_outcomes, (str, bytes)):
        for idx, item in enumerate(raw_primary_outcomes):
            where = f"protocolSection.outcomesModule.primaryOutcomes[{idx}]"
            if not isinstance(item, Mapping):
                # Skipping it would renumber every later outcome's index.
                raise OutcomesLayoutError(f"{where} must be an object, got {type(item).__name__}")
            registered.append(
                canonicalize_nested({
                    "outcome_index": idx,
                    "measure": _registered_measure_text(item.get("measure"), where),
                    "time_frame": _one_spelling_of(item, ("timeFrame", "time_frame"), where),
                    "description": item.get("description"),
                })
            )

    if parsed_populations is None and population_parser is not None and results_sec:
        # The parser reads a deep copy: an edit it makes to the study it is
        # given is never read back into the block (red-team m3 round 3).
        parsed_populations = plain_data(population_parser(copy.deepcopy(study)), "population_parser result")
        _refuse_holdout_flags(parsed_populations, label="reported_results")
    pop_by_idx = _extract_parsed_population_map(parsed_populations)
    population_record = _population_record(parsed_populations)

    om_mod = (
        results_sec.get("outcomeMeasuresModule")
        if isinstance(results_sec.get("outcomeMeasuresModule"), Mapping)
        else {}
    )
    raw_measures = om_mod.get("outcomeMeasures")
    primary: list[tuple[Mapping[str, Any], int, tuple[int, ...]]] = []
    if isinstance(raw_measures, Sequence) and not isinstance(raw_measures, (str, bytes)):
        primary_pos = 0
        for raw_idx, measure in enumerate(raw_measures):
            where = f"resultsSection.outcomeMeasuresModule.outcomeMeasures[{raw_idx}]"
            if not isinstance(measure, Mapping):
                # Skipping it would renumber the measures a parsed row indexes.
                raise OutcomesLayoutError(f"{where} must be an object, got {type(measure).__name__}")
            if not _outcome_type_is_primary(measure.get("type"), f"{where}.type"):
                continue
            # A parsed row may index the measure among all of them or among
            # the primary ones; the first reading wins, and a row both
            # readings attach to two measures is refused (_attach_populations).
            primary.append((measure, primary_pos, (raw_idx, primary_pos)))
            primary_pos += 1
    primary_measures = _attach_populations(primary, pop_by_idx)

    # results_posted is tri-state. True when the record carries a
    # resultsSection or hasResults is true; False only when hasResults is
    # explicitly false; None when the input says neither, as for a manifest row
    # built without its CT.gov record (the live outcomes index), so that
    # absence of the record is not published as "no results posted".
    results_posted_ct: bool | None
    if results_sec or has_results_flag is True:
        results_posted_ct = True
    elif has_results_flag is False:
        results_posted_ct = False
    else:
        results_posted_ct = None
    return canonicalize_nested({
        "section_label": SECTION_LABEL_REPORTED_RESULTS,
        "results_posted": results_posted_ct,
        "registered_primary_outcomes": registered,
        "primary_outcome_measures": primary_measures,
        "analysis_populations": population_record,
    })


# ── section 2: document_profile (Module 1 + legacy documents.jsonl adapter) ──

def _reject_embedded_raw_text(obj: Mapping[str, Any], label: str) -> None:
    """Reject raw PDF page text arrays so outcomes shards stay compact."""
    for bad_key in FORBIDDEN_RAW_TEXT_KEYS:
        if bad_key in obj:
            raise OutcomesLayoutError(
                f"{label}: embedded raw PDF text key {bad_key!r} is forbidden in document_profile"
            )
    if "pages" in obj and isinstance(obj["pages"], Sequence) and not isinstance(obj["pages"], (str, bytes)):
        raise OutcomesLayoutError(
            f"{label}: embedded raw PDF text key 'pages' is forbidden in document_profile"
        )


def _summarize_doc_readability(docs: Sequence[Mapping[str, Any]]) -> str:
    """Compute trial readability_summary matching Module 1's rules exactly.

    Module 1's ``summarize_trial_readability``, in order: no documents ->
    ``no_document``; every one ``download_failed`` -> ``download_failed``;
    every one ``extraction_failed`` -> ``extraction_failed``; every one
    ``machine_readable`` -> ``machine_readable``; every one ``image_only_scan``
    or ``garbled_cmap`` -> ``garbled_cmap`` if any is garbled, else
    ``image_only_scan``; any other mixture -> ``partial_image_or_redacted``.
    """
    if not docs:
        return "no_document"
    statuses = [str(d["readability_status"]) for d in docs]
    if all(s == "download_failed" for s in statuses):
        return "download_failed"
    if all(s == "extraction_failed" for s in statuses):
        return "extraction_failed"
    if all(s == "machine_readable" for s in statuses):
        return "machine_readable"
    if all(s in {"image_only_scan", "garbled_cmap"} for s in statuses):
        return "garbled_cmap" if "garbled_cmap" in statuses else "image_only_scan"
    return "partial_image_or_redacted"


# Every key a document row may carry: Module 1's TrialDocumentRecord (the
# m1 branch's src/document_indexer.py), and the two provenance keys of its
# legacy census projection (to_legacy_census_document_row: model_id,
# profile_version). Each is published with the row (a key the row does not
# carry is not written), so a key outside this set would be dropped, and is
# refused (skeptic review of m3: api_doc_date, upload_timing,
# unreadable_statistical_pages, unreadable_recovery_bucket and cache_hit were
# dropped without a word). Holdout flags are read by the caller (False).
DOCUMENT_ROW_KEYS: frozenset[str] = frozenset({
    "nct_id",
    "filename",
    "doc_type",
    "url",
    "http_status",
    "download_error",
    "extraction_error",
    "sha256",
    "bytes",
    "physical_pages",
    "pages_without_text",
    "readability_status",
    "unreadable_recovery_bucket",
    "redaction_markers",
    "version_label",
    "version_date_as_printed",
    "api_doc_date",
    "upload_date",
    "upload_timing",
    "pages_selected",
    "pages_deferred",
    "unreadable_statistical_pages",
    "retrieved_at",
    "cache_hit",
    "model_id",
    "profile_version",
})
# Every key a document profile may carry: Module 1's TrialDocumentProfile.
DOCUMENT_PROFILE_KEYS: frozenset[str] = frozenset({
    "nct_id",
    "indexer_rules_version",
    "extracted_at",
    "pipeline_commit",
    "readability_summary",
    "redaction_flags",
    "excluded_icf_count",
    "document_badge",
    "documents",
})


def _normalize_single_document(
    raw_doc: Mapping[str, Any],
    *,
    expected_nct_id: str | None = None,
) -> dict[str, Any]:
    """Normalize one Module 1 TrialDocumentRecord or legacy documents.jsonl row.

    A row's own ``nct_id``, when present, must be a strict ASCII NCT ID and,
    when the caller knows the trial (``expected_nct_id``), that trial's: a
    document row naming another trial is refused, never filed under this one.

    Every key is published or refused, never dropped (``DOCUMENT_ROW_KEYS``):
    a text field is a string or null, never the ``str()`` of an object
    (skeptic review of m3: an object url was published as its text), and
    Module 1's ``api_doc_date``, ``upload_timing`` (an object whose
    ``date_precision`` values are Module 1's), ``unreadable_statistical_pages``
    (positive page numbers), ``unreadable_recovery_bucket`` (Module 4's
    ``UNREADABLE_SUBTYPES``), ``cache_hit`` (a bool) and the census row's
    ``model_id`` and ``profile_version`` are written when the row carries them.
    """
    _reject_embedded_raw_text(raw_doc, "document")
    unknown = sorted(
        withhold_nct_ids(str(key))
        for key in raw_doc
        if key not in DOCUMENT_ROW_KEYS and not is_holdout_flag_key(key)
    )
    if unknown:
        raise OutcomesLayoutError(
            f"document: the row carries {len(unknown)} key(s) a document row does not have "
            f"({', '.join(unknown[:5])}); they would be dropped, so they are refused"
        )
    if "nct_id" in raw_doc:
        row_nct = strict_nct_id(raw_doc["nct_id"], "document.nct_id")
        if expected_nct_id is not None and row_nct != expected_nct_id:
            raise OutcomesLayoutError(
                "document.nct_id names a different trial than the document profile it is in"
            )
    filename = raw_doc.get("filename")
    if not isinstance(filename, str) or not filename.strip() or "/" in filename or "\\" in filename:
        raise OutcomesLayoutError(f"document filename must be a safe basename, got {filename!r}")

    doc_type = raw_doc.get("doc_type")
    if doc_type not in DOC_TYPES:
        raise OutcomesLayoutError(f"{filename}: doc_type must be in {DOC_TYPES}, got {doc_type!r}")

    http_status = _strict_int_or_none(raw_doc.get("http_status"), f"{filename}.http_status")
    byte_count = _strict_int_or_none(raw_doc.get("bytes"), f"{filename}.bytes")
    physical_pages = _strict_int_or_none(raw_doc.get("physical_pages"), f"{filename}.physical_pages")
    if physical_pages is not None and physical_pages < 0:
        raise OutcomesLayoutError(f"{filename}: physical_pages cannot be negative ({physical_pages})")

    sha256 = raw_doc.get("sha256")
    if sha256 is not None:
        if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
            raise OutcomesLayoutError(f"{filename}: invalid sha256 {sha256!r}")

    def _int_list_or_none(val: Any, key_name: str) -> list[int] | None:
        if val is None:
            return None
        if not isinstance(val, Sequence) or isinstance(val, (str, bytes)):
            raise OutcomesLayoutError(f"{filename}.{key_name} must be a list of ints or None")
        out: list[int] = []
        for item in val:
            if type(item) is not int or item < 1:
                raise OutcomesLayoutError(
                    f"{filename}.{key_name} must contain positive ints, got {item!r}"
                )
            out.append(item)
        return sorted(set(out))

    pages_without_text = _int_list_or_none(raw_doc.get("pages_without_text"), "pages_without_text")
    pages_selected = _int_list_or_none(raw_doc.get("pages_selected"), "pages_selected")
    pages_deferred = _int_list_or_none(raw_doc.get("pages_deferred"), "pages_deferred")

    raw_markers = raw_doc.get("redaction_markers")
    if raw_markers is None:
        redaction_markers: list[str] = []
    elif isinstance(raw_markers, Sequence) and not isinstance(raw_markers, (str, bytes)):
        bad_markers = [m for m in raw_markers if m not in REDACTION_FLAGS]
        if bad_markers:
            raise OutcomesLayoutError(f"{filename}: unknown redaction_markers {bad_markers!r}")
        redaction_markers = sorted(set(str(m) for m in raw_markers))
    else:
        raise OutcomesLayoutError(f"{filename}.redaction_markers must be a list of flags")

    raw_status = raw_doc.get("readability_status")
    if raw_status is not None:
        if raw_status not in READABILITY_STATUSES:
            raise OutcomesLayoutError(
                f"{filename}: readability_status {raw_status!r} not in {READABILITY_STATUSES}"
            )
        readability_status = str(raw_status)
    else:
        # Infer deterministically for legacy rule-census documents.jsonl rows.
        if (http_status is not None and http_status != 200) or sha256 is None or physical_pages in (None, 0):
            readability_status = "download_failed"
        elif pages_without_text is not None and physical_pages is not None and len(pages_without_text) == physical_pages:
            readability_status = "image_only_scan"
        elif (pages_without_text and len(pages_without_text) > 0) or redaction_markers:
            readability_status = "partial_image_or_redacted"
        else:
            readability_status = "machine_readable"

    def _opt_str(val: Any, key_name: str = "text field") -> str | None:
        # A string as given ("" is absent), or null; never str() of an object.
        if val is None:
            return None
        if type(val) is not str:
            raise OutcomesLayoutError(f"{filename}.{key_name} must be a string or null, got {type(val).__name__}")
        return val if val != "" else None

    out: dict[str, Any] = {
        "filename": filename,
        "doc_type": doc_type,
        "url": _opt_str(raw_doc.get("url"), "url"),
        "http_status": http_status,
        "sha256": sha256,
        "bytes": byte_count,
        "physical_pages": physical_pages,
        "pages_without_text": pages_without_text,
        "readability_status": readability_status,
        "redaction_markers": redaction_markers,
        "version_label": _opt_str(raw_doc.get("version_label"), "version_label"),
        "version_date_as_printed": _opt_str(raw_doc.get("version_date_as_printed"), "version_date_as_printed"),
        "upload_date": _opt_str(raw_doc.get("upload_date"), "upload_date"),
        "pages_selected": pages_selected,
        "pages_deferred": pages_deferred,
        "retrieved_at": _opt_str(raw_doc.get("retrieved_at"), "retrieved_at"),
    }
    # Module 1 records why a document has no pages: download_error when its
    # bytes were never obtained (download_failed), extraction_error when they
    # were obtained but no page could be extracted (extraction_failed). The
    # reason is published with the state, never dropped (red-team m3 round 3:
    # download_failed was published without its reason). Each is a non-empty
    # string or null, and only with its own state (Module 1's
    # TrialDocumentRecord rule); the key is written when the row carries it.
    for reason_key, its_status in (("download_error", "download_failed"), ("extraction_error", "extraction_failed")):
        if reason_key not in raw_doc:
            continue
        reason = raw_doc[reason_key]
        if reason is not None:
            if type(reason) is not str or not reason.strip():
                raise OutcomesLayoutError(
                    f"{filename}.{reason_key} must be a non-empty string or null, got {type(reason).__name__}"
                )
            if readability_status != its_status:
                raise OutcomesLayoutError(
                    f"{filename}: {reason_key} is given but readability_status is not {its_status!r}; Module 1 "
                    f"records it only for that state, so the row contradicts itself"
                )
        out[reason_key] = reason
    if "api_doc_date" in raw_doc:
        out["api_doc_date"] = _opt_str(raw_doc["api_doc_date"], "api_doc_date")
    if "upload_timing" in raw_doc:
        timing = raw_doc["upload_timing"]
        if timing is not None:
            if not isinstance(timing, Mapping):
                raise OutcomesLayoutError(f"{filename}.upload_timing must be an object or null, got {type(timing).__name__}")
            _validate_date_precisions_recursive(timing, f"{filename}.upload_timing")
            timing = plain_data(timing, f"{filename}.upload_timing")
        out["upload_timing"] = timing
    if "unreadable_statistical_pages" in raw_doc:
        out["unreadable_statistical_pages"] = _int_list_or_none(
            raw_doc["unreadable_statistical_pages"], "unreadable_statistical_pages"
        )
    if "unreadable_recovery_bucket" in raw_doc:
        bucket = raw_doc["unreadable_recovery_bucket"]
        if bucket is not None and (type(bucket) is not str or bucket not in UNREADABLE_SUBTYPES):
            raise OutcomesLayoutError(
                f"{filename}.unreadable_recovery_bucket must be one of {UNREADABLE_SUBTYPES} or null "
                f"(value not shown)"
            )
        out["unreadable_recovery_bucket"] = bucket
    if "cache_hit" in raw_doc:
        cache_hit = raw_doc["cache_hit"]
        if cache_hit is not None and type(cache_hit) is not bool:
            raise OutcomesLayoutError(f"{filename}.cache_hit must be a bool or null, got {type(cache_hit).__name__}")
        out["cache_hit"] = cache_hit
    for provenance_key in ("model_id", "profile_version"):
        if provenance_key in raw_doc:
            stamp = raw_doc[provenance_key]
            if stamp is not None and (type(stamp) is not str or not stamp.strip()):
                raise OutcomesLayoutError(
                    f"{filename}.{provenance_key} must be a non-empty string or null, got {type(stamp).__name__}"
                )
            out[provenance_key] = stamp
    return canonicalize_nested(out)


def normalize_document_profile(
    raw: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    *,
    expected_nct_id: str | None = None,
) -> dict[str, Any] | None:
    """Normalize a Module 1 ``TrialDocumentProfile`` or legacy ``documents.jsonl`` rows.

    Rejects embedded raw PDF text arrays (``page_texts``, ``raw_pages``, etc.),
    and any profile or document row whose ``nct_id`` is not strict or, given
    ``expected_nct_id``, not that trial's.

    Also refused (red-team m3 round 3, where this public builder returned
    all three): input carrying a holdout flag under any spelling, at any depth,
    that is not exactly False (``_refuse_holdout_flags``; a holdout's stub has
    no document profile), and, when no trial is expected, document rows of
    more than one trial: the first trial named (the profile's own ``nct_id``,
    else the first row's) is the one every row must name.

    Module 1's own provenance stamps on a profile, ``extracted_at`` (an
    ISO-8601 timestamp) and ``pipeline_commit`` (a commit or null), and its
    ``excluded_icf_count`` (a non-negative int or null) are published with it
    when given, so the section cannot outrun its source unnoticed (round 3:
    they were dropped).

    A profile key outside Module 1's (``DOCUMENT_PROFILE_KEYS``) is refused,
    never dropped, as is a non-string ``indexer_rules_version``; Module 1's
    own ``document_badge``, when given, must be the badge the profile's
    documents give (``build_document_badge``, which the index row publishes),
    or the two disagree and are refused (skeptic review of m3: it was read
    and dropped). Each document row is held to ``DOCUMENT_ROW_KEYS``.
    """
    if raw is None:
        return None
    raw = plain_data(raw, "document_profile")
    _refuse_holdout_flags(raw, label="document_profile")
    if isinstance(raw, Mapping) and "nct_id" in raw:
        profile_nct = strict_nct_id(raw["nct_id"], "document_profile.nct_id")
        if expected_nct_id is not None and profile_nct != expected_nct_id:
            raise OutcomesLayoutError("document_profile.nct_id names a different trial")
        expected_nct_id = profile_nct

    indexer_rules_version: str | None = None
    explicit_summary: str | None = None
    explicit_flags: list[str] | None = None
    stamps: dict[str, Any] = {}
    raw_docs: Sequence[Mapping[str, Any]]

    module1_badge: Any = None
    if isinstance(raw, Mapping):
        _reject_embedded_raw_text(raw, "document_profile")
        unknown = sorted(
            withhold_nct_ids(str(key))
            for key in raw
            if key not in DOCUMENT_PROFILE_KEYS and not is_holdout_flag_key(key)
        )
        if unknown:
            raise OutcomesLayoutError(
                f"document_profile carries {len(unknown)} key(s) a Module 1 profile does not have "
                f"({', '.join(unknown[:5])}); they would be dropped, so they are refused"
            )
        if "documents" not in raw or not isinstance(raw["documents"], Sequence) or isinstance(raw["documents"], (str, bytes)):
            raise OutcomesLayoutError("document_profile mapping must contain a 'documents' list")
        raw_docs = raw["documents"]
        ver = raw.get("indexer_rules_version")
        if ver is not None and type(ver) is not str:
            raise OutcomesLayoutError(
                f"document_profile.indexer_rules_version must be a string or null, got {type(ver).__name__}"
            )
        indexer_rules_version = ver if isinstance(ver, str) and ver.strip() else None
        module1_badge = raw.get("document_badge")
        if "extracted_at" in raw:
            stamps["extracted_at"] = validate_iso_timestamp(raw["extracted_at"], "document_profile.extracted_at")
        if "pipeline_commit" in raw:
            stamps["pipeline_commit"] = validate_commit_stamp(
                raw["pipeline_commit"], allow_null=True, label="document_profile.pipeline_commit"
            )
        if "excluded_icf_count" in raw:
            icf = _strict_int_or_none(raw["excluded_icf_count"], "document_profile.excluded_icf_count")
            if icf is not None and icf < 0:
                raise OutcomesLayoutError("document_profile.excluded_icf_count cannot be negative")
            stamps["excluded_icf_count"] = icf
        if raw.get("readability_summary") is not None:
            rs = raw["readability_summary"]
            if rs not in READABILITY_STATUSES:
                raise OutcomesLayoutError(
                    f"document_profile.readability_summary {rs!r} not in {READABILITY_STATUSES}"
                )
            explicit_summary = str(rs)
        if raw.get("redaction_flags") is not None:
            rf = raw["redaction_flags"]
            if not isinstance(rf, Sequence) or isinstance(rf, (str, bytes)):
                raise OutcomesLayoutError("document_profile.redaction_flags must be a list")
            bad = [f for f in rf if f not in REDACTION_FLAGS]
            if bad:
                raise OutcomesLayoutError(f"document_profile.redaction_flags contains unknown flags {bad!r}")
            explicit_flags = sorted(set(str(f) for f in rf))
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        raw_docs = raw
    else:
        raise OutcomesLayoutError(
            f"document_profile must be a mapping, list of document rows, or None; got {type(raw).__name__}"
        )

    docs: list[dict[str, Any]] = []
    seen_filenames: set[str] = set()
    for item in raw_docs:
        if not isinstance(item, Mapping):
            raise OutcomesLayoutError("every item in document_profile.documents must be an object")
        if expected_nct_id is None and "nct_id" in item:
            # The first row naming a trial fixes the trial; every later row
            # must name the same one (_normalize_single_document).
            expected_nct_id = strict_nct_id(item["nct_id"], "document.nct_id")
        norm = _normalize_single_document(item, expected_nct_id=expected_nct_id)
        fn = norm["filename"]
        if fn in seen_filenames:
            raise OutcomesLayoutError(f"duplicate document filename in document_profile: {fn!r}")
        seen_filenames.add(fn)
        docs.append(norm)
    docs.sort(key=lambda d: d["filename"])

    union_flags = sorted({flag for d in docs for flag in d["redaction_markers"]} | set(explicit_flags or []))
    summary = explicit_summary if explicit_summary is not None else _summarize_doc_readability(docs)
    profile = canonicalize_nested({
        "indexer_rules_version": indexer_rules_version,
        "readability_summary": summary,
        "redaction_flags": union_flags,
        "documents": docs,
        **stamps,
    })
    if module1_badge is not None:
        # Module 1's own badge is the index row's document_badge, which the
        # builder computes from the profile: it must be that badge, or the
        # two sources disagree and are refused (it was dropped unread).
        badge = plain_data(module1_badge, "document_profile.document_badge")
        if isinstance(badge, dict) and isinstance(badge.get("redaction_flags"), list):
            badge = {**badge, "redaction_flags": sorted(badge["redaction_flags"], key=str)}
        if canonicalize_nested(badge) != canonicalize_nested(build_document_badge(in_audit=False, document_profile=profile)):
            raise OutcomesLayoutError(
                "document_profile.document_badge is not the badge its documents give (values not shown); "
                "conflicting inputs are refused, not resolved"
            )
    return profile


# ── section 3: decision_rule_locations (locations.jsonl adapter) ──────────────

def citation_problems(
    cit: Any,
    *,
    docs_by_filename: Mapping[str, Mapping[str, Any]] | None,
    pages_not_searched: Any,
) -> list[str]:
    """Every problem with one decision-rule citation ([] when it is sound).

    Method: the 6-key shape and quotation hygiene are Module 4's
    ``validate_citation_shape`` (optional strings may be ``None``), so this
    module cannot accept a citation the verifier refuses: ``None``, ``'x'``,
    ``{}``, a non-``.pdf`` filename, an ``int`` printed page, an ellipsis in any
    Unicode spelling. Only when the shape is sound are the two checks that need
    this module's context added: the citation must name a document of the
    trial's ``document_profile`` (same sha256, page within its page count),
    and it may not land on a page the row lists as not searched. The same
    function serves normalization (which raises) and the on-disk check
    (which reports), so the two cannot drift apart.
    """
    shape_errors = validate_citation_shape(cit, allow_none_optional_strings=True)
    if shape_errors:
        return list(shape_errors)
    errors: list[str] = []
    filename = cit["filename"]
    sha256 = cit["sha256"]
    physical_page = cit["physical_page"]
    if docs_by_filename is not None:
        if filename not in docs_by_filename:
            errors.append(f"citation references unknown document {filename!r} not in document_profile")
        else:
            doc = docs_by_filename[filename]
            if doc.get("sha256") != sha256:
                errors.append(
                    f"citation sha256 for {filename!r} ({sha256}) does not match document_profile "
                    f"({doc.get('sha256')})"
                )
            max_pages = doc.get("physical_pages")
            if type(max_pages) is int and not (1 <= physical_page <= max_pages):
                errors.append(
                    f"citation physical_page {physical_page} out of bounds 1..{max_pages} for {filename!r}"
                )
    if isinstance(pages_not_searched, Mapping):
        unsearched = pages_not_searched.get(filename)
        if isinstance(unsearched, Sequence) and physical_page in unsearched:
            errors.append(f"citation ({filename}, p.{physical_page}) falls inside pages_not_searched")
    elif isinstance(pages_not_searched, Sequence) and not isinstance(pages_not_searched, (str, bytes)):
        if physical_page in pages_not_searched:
            errors.append(f"citation ({filename}, p.{physical_page}) falls inside pages_not_searched")
    return errors


def _normalize_citation(
    raw_cit: Any,
    *,
    docs_by_filename: Mapping[str, Mapping[str, Any]] | None,
    pages_not_searched: Any,
) -> dict[str, Any]:
    """Validate one 6-key decision-rule citation (``citation_problems``) and
    return it with sorted keys; raise ``OutcomesLayoutError`` on any problem.
    """
    errors = citation_problems(
        raw_cit,
        docs_by_filename=docs_by_filename,
        pages_not_searched=pages_not_searched,
    )
    if errors:
        raise OutcomesLayoutError("citation refused: " + "; ".join(errors))
    return canonicalize_nested({key: raw_cit[key] for key in CITATION_REQUIRED_KEYS})


def _is_non_empty_unsearched(pages_not_searched: Any) -> bool:
    """True if pages_not_searched indicates any unsearched pages."""
    if pages_not_searched is None:
        return False
    if isinstance(pages_not_searched, Mapping):
        return any(bool(v) for v in pages_not_searched.values())
    if isinstance(pages_not_searched, Sequence) and not isinstance(pages_not_searched, (str, bytes)):
        return len(pages_not_searched) > 0
    return bool(pages_not_searched)


def _normalize_location_pass(
    rows: Sequence[Mapping[str, Any]],
    *,
    pass_label: str,
    docs_by_filename: Mapping[str, Mapping[str, Any]] | None,
    has_posted_documents: bool = False,
    pass_name: str | None = None,
    expected_nct_id: str | None = None,
    primary_outcomes_count: int | None = None,
) -> list[dict[str, Any]]:
    """Validate and sort one pass's decision-rule location rows.

    The status-to-citation rule is Module 4's ``validate_status_citations``
    with ``has_posted_documents`` set from the trial's own evidence of posted
    documents: a status in ``CITATION_REQUIRED_STATUSES`` needs a citation,
    one in ``CITATION_FORBIDDEN_STATUSES`` may carry none, ``NO_DOCUMENT`` is
    refused when documents are posted, and any citation is refused when none
    are. A row that states its own ``pass`` must agree with the pass it is
    filed under (``pass_name``), and a row's ``nct_id`` must be the trial's.

    A row's content is published as given or refused, never dropped or turned
    into its ``str()`` (red-team m3 round 3):
    - its keys must be Module 4's (``LOCATION_ROW_INPUT_KEYS``) or a holdout
      flag (read by the caller; it must be False and is not published); any
      other key would be dropped, so it is refused;
    - ``primary_outcome_title``, ``note`` and ``worker`` are a string or
      null, never an object;
    - Module 4's ``model_id`` and ``prompt_version`` (non-empty strings; the
      model id is the record of a non-deterministic step) and
      ``unreadable_subtype`` (Module 4's ``UNREADABLE_SUBTYPES`` or null) are
      published with the row when it carries them;
    - with ``primary_outcomes_count`` known, ``primary_outcome_index`` must be
      below it: a row for an outcome the trial does not have is refused, as a
      parsed population for one is.
    """
    has_posted_docs_flag = _require_strict_bool(
        has_posted_documents, f"{pass_label}.has_posted_documents"
    )
    effective_has_docs = has_posted_docs_flag or bool(docs_by_filename)
    seen_pairs: set[tuple[int, str]] = set()
    normalized: list[dict[str, Any]] = []
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            raise OutcomesLayoutError(f"{pass_label}: location row must be an object")
        unknown = sorted(
            withhold_nct_ids(str(key))
            for key in raw_row
            if key not in LOCATION_ROW_INPUT_KEYS and not is_holdout_flag_key(key)
        )
        if unknown:
            raise OutcomesLayoutError(
                f"{pass_label}: a location row carries {len(unknown)} key(s) a location row does not have "
                f"({', '.join(unknown[:5])}); they would be dropped, so they are refused"
            )
        if pass_name is not None and "pass" in raw_row and raw_row["pass"] != pass_name:
            raise OutcomesLayoutError(
                f"{pass_label}: a row stating pass {raw_row['pass']!r} is filed under {pass_name!r}; "
                f"a row is never re-filed into another pass"
            )
        if "nct_id" in raw_row:
            row_nct = strict_nct_id(raw_row["nct_id"], f"{pass_label}.nct_id")
            if expected_nct_id is not None and row_nct != expected_nct_id:
                raise OutcomesLayoutError(
                    f"{pass_label}: a location row names a different trial than its record"
                )
        outcome_idx = raw_row.get("primary_outcome_index")
        if type(outcome_idx) is not int or outcome_idx < 0:
            raise OutcomesLayoutError(
                f"{pass_label}: primary_outcome_index must be a non-negative int, got {type(outcome_idx).__name__}"
            )
        if primary_outcomes_count is not None and outcome_idx >= primary_outcomes_count:
            raise OutcomesLayoutError(
                f"{pass_label}: a location row names primary outcome index {outcome_idx}, but the trial has "
                f"{primary_outcomes_count} primary outcome(s); a row for an outcome the trial does not have is "
                f"refused, never published"
            )
        element = raw_row.get("element")
        if element not in _ELEMENT_ORDER:
            raise OutcomesLayoutError(f"{pass_label}: unknown element {element!r}")
        pair = (outcome_idx, str(element))
        if pair in seen_pairs:
            raise OutcomesLayoutError(
                f"{pass_label}: duplicate (primary_outcome_index, element) entry {pair}"
            )
        seen_pairs.add(pair)

        status = raw_row.get("status")
        if status not in LOCATION_STATUSES:
            raise OutcomesLayoutError(f"{pass_label}: unknown status {status!r}")

        pages_searched = raw_row.get("pages_searched")
        pages_not_searched = raw_row.get("pages_not_searched", [])
        has_unsearched = _is_non_empty_unsearched(pages_not_searched)

        if pages_searched == "all" and has_unsearched:
            raise OutcomesLayoutError(
                f"{pass_label} {pair}: pages_searched=='all' contradicts non-empty pages_not_searched"
            )
        if status == "NOT_FOUND" and has_unsearched:
            raise OutcomesLayoutError(
                f"{pass_label} {pair}: status 'NOT_FOUND' is forbidden when pages_not_searched is non-empty"
            )

        raw_citations = raw_row.get("citations", [])
        if not isinstance(raw_citations, Sequence) or isinstance(raw_citations, (str, bytes)):
            raise OutcomesLayoutError(f"{pass_label} {pair}: citations must be a list")
        citations = [
            _normalize_citation(
                c,
                docs_by_filename=docs_by_filename,
                pages_not_searched=pages_not_searched,
            )
            for c in raw_citations
        ]
        rule_errors = validate_status_citations(
            str(status), len(citations), has_posted_documents=effective_has_docs
        )
        if rule_errors:
            raise OutcomesLayoutError(
                f"{pass_label} {pair}: forbidden by the status-to-citation rule: " + "; ".join(rule_errors)
            )

        raw_text_source = raw_row.get("text_source")
        if raw_text_source is not None and not isinstance(raw_text_source, str):
            raise OutcomesLayoutError(
                f"{pass_label} {pair}: text_source must be a string or None, got {type(raw_text_source).__name__}"
            )
        text_source = raw_text_source.strip() if isinstance(raw_text_source, str) else ""

        texts: dict[str, str | None] = {}
        for text_key in ("primary_outcome_title", "note", "worker"):
            value = raw_row.get(text_key)
            if value is not None and type(value) is not str:
                raise OutcomesLayoutError(
                    f"{pass_label} {pair}: {text_key} must be a string or null, got {type(value).__name__}; an "
                    f"object is never published as its text"
                )
            texts[text_key] = value
        row_out: dict[str, Any] = {
            "primary_outcome_index": outcome_idx,
            "primary_outcome_title": texts["primary_outcome_title"],
            "element": str(element),
            "status": str(status),
            "citations": citations,
            "pages_searched": pages_searched,
            "pages_not_searched": pages_not_searched,
            "note": texts["note"],
            "worker": texts["worker"],
        }
        if text_source:
            row_out["text_source"] = text_source
        for model_key in ("model_id", "prompt_version"):
            if model_key in raw_row:
                value = raw_row[model_key]
                if type(value) is not str or not value.strip():
                    raise OutcomesLayoutError(
                        f"{pass_label} {pair}: {model_key} must be a non-empty string (Module 4's rule), got "
                        f"{type(value).__name__}"
                    )
                row_out[model_key] = value
        if "unreadable_subtype" in raw_row:
            subtype = raw_row["unreadable_subtype"]
            if subtype is not None and subtype not in UNREADABLE_SUBTYPES:
                raise OutcomesLayoutError(
                    f"{pass_label} {pair}: unreadable_subtype must be one of {UNREADABLE_SUBTYPES} or null"
                )
            row_out["unreadable_subtype"] = subtype
        normalized.append(canonicalize_nested(row_out))
    normalized.sort(key=lambda r: (r["primary_outcome_index"], _ELEMENT_ORDER[r["element"]]))
    return normalized


def _resolve_effective_pass(
    primary_pass: Sequence[Mapping[str, Any]],
    ocr_recovery_pass: Sequence[Mapping[str, Any]] | None,
    adjudication_pass: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Resolve each ``(primary_outcome_index, element)`` cell under precedence
    ``primary -> ocr_recovery -> adjudication`` without mutating any pass row.
    """
    resolved: dict[tuple[int, str], dict[str, Any]] = {}
    layers: tuple[tuple[str, Sequence[Mapping[str, Any]] | None], ...] = (
        ("primary", primary_pass),
        ("ocr_recovery", ocr_recovery_pass),
        ("adjudication", adjudication_pass),
    )
    for source_pass, rows in layers:
        if not rows:
            continue
        for row in rows:
            pair = (int(row["primary_outcome_index"]), str(row["element"]))
            resolved[pair] = canonicalize_nested({
                **dict(row),
                "source_pass": source_pass,
            })
    ordered_pairs = sorted(resolved, key=lambda p: (p[0], _ELEMENT_ORDER[p[1]]))
    return [resolved[pair] for pair in ordered_pairs]


# Pass sections a pre-grouped decision_rule_locations object may carry, and
# the pass name each one's rows belong to.
GROUPED_PASS_KEYS: dict[str, str] = {
    "primary_pass": "primary",
    "second_pass": "second",
    "ocr_recovery_pass": "ocr_recovery",
    "adjudication_pass": "adjudication",
}
# Everything else a pre-grouped object may carry. Any other key is refused:
# an unknown section (an "ocr_pass", an "adjudication" without "_pass") would
# otherwise be dropped without a word, and with it that pass's rows.
GROUPED_OTHER_KEYS: frozenset[str] = frozenset({"nct_id", "in_audit", "census_status"})

# The keys of the normalized block, as written into shards.
LOCATIONS_BLOCK_KEYS: frozenset[str] = frozenset({
    "census_status",
    "elements_expected_per_outcome",
    "primary_pass",
    "second_pass",
    "ocr_recovery_pass",
    "adjudication_pass",
    "effective_pass",
})


def census_status_from_effective_pass(
    effective_pass: Sequence[Mapping[str, Any]],
    primary_outcomes_count: int | None,
) -> str:
    """Compute a trial's census status from its resolved cells.

    Method: no cells -> ``not_started``; every cell ``NO_DOCUMENT`` ->
    ``no_document``; otherwise ``complete`` when every element of every
    primary outcome has a cell (outcomes ``0..primary_outcomes_count-1`` when
    the count is known, else every outcome that has any cell), and ``partial``
    when any is missing. The status is always computed, never taken from the
    input (a supplied one must agree, see ``normalize_decision_rule_locations``),
    and the on-disk check recomputes it the same way.
    """
    if not effective_pass:
        return "not_started"
    if all(r.get("status") == "NO_DOCUMENT" for r in effective_pass):
        return "no_document"
    got_pairs = {(r.get("primary_outcome_index"), r.get("element")) for r in effective_pass}
    if primary_outcomes_count is not None and primary_outcomes_count > 0:
        expected_pairs = {
            (idx, elem)
            for idx in range(primary_outcomes_count)
            for elem in ELEMENTS
        }
        return "complete" if expected_pairs.issubset(got_pairs) else "partial"
    by_outcome: dict[Any, set[Any]] = {}
    for idx, elem in got_pairs:
        by_outcome.setdefault(idx, set()).add(elem)
    all_full = bool(by_outcome) and all(elems == set(ELEMENTS) for elems in by_outcome.values())
    return "complete" if all_full else "partial"


def _rows_with_citations(passes: Mapping[str, Any]) -> int:
    """Count rows, over every pass list in ``passes``, that carry any citation."""
    count = 0
    for rows in passes.values():
        if isinstance(rows, list):
            count += sum(
                1 for r in rows
                if isinstance(r, Mapping) and isinstance(r.get("citations"), list) and r["citations"]
            )
    return count


def normalize_decision_rule_locations(
    raw: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    *,
    document_profile: Mapping[str, Any] | None = None,
    primary_outcomes_count: int | None = None,
    has_posted_documents: bool = False,
    expected_nct_id: str | None = None,
) -> dict[str, Any] | None:
    """Normalize decision-rule location records into the unified shard block.

    Accepts either a sequence of ``locations.jsonl`` rows (grouped by ``pass``:
    ``"primary"`` / ``"second"`` / ``"ocr_recovery"`` / ``"adjudication"``) or a
    pre-grouped dict containing ``primary_pass`` and optional ``second_pass``,
    ``ocr_recovery_pass``, and ``adjudication_pass``. Retains every pass
    append-only and computes deterministic ``effective_pass`` under precedence
    ``primary -> ocr_recovery -> adjudication``.

    Refusals beyond the per-row rules (``_normalize_location_pass``):
    - a pre-grouped object with any key outside ``GROUPED_PASS_KEYS`` and
      ``GROUPED_OTHER_KEYS`` (its section would be dropped);
    - a pre-grouped ``census_status`` that differs from the one computed from
      the rows (``census_status_from_effective_pass``): a supplied status is
      never trusted;
    - ``no_document`` with a citation in any pass, and ``no_document`` when the
      trial has posted documents;
    - input carrying a holdout flag (any spelling, any depth) that is not
      exactly False (``AuditFirewallError``): a holdout's stub has no
      locations block, so its rows are never normalized into one;
    - a row with a key Module 4's location rows do not have, an object in a
      text field, or (with ``primary_outcomes_count`` known, an int) an
      outcome index the trial does not have (``_normalize_location_pass``).
    """
    if primary_outcomes_count is not None and (type(primary_outcomes_count) is not int or primary_outcomes_count < 0):
        raise OutcomesLayoutError(
            f"decision_rule_locations.primary_outcomes_count must be a non-negative int or None, got "
            f"{type(primary_outcomes_count).__name__}"
        )
    has_posted_docs_flag = _require_strict_bool(
        has_posted_documents, "decision_rule_locations.has_posted_documents"
    )
    if raw is None:
        return None
    _refuse_holdout_flags(raw, label="decision_rule_locations")

    docs_by_filename: dict[str, Mapping[str, Any]] | None = None
    if isinstance(document_profile, Mapping):
        docs = document_profile.get("documents")
        if isinstance(docs, Sequence) and not isinstance(docs, (str, bytes)):
            docs_by_filename = {
                str(d["filename"]): d
                for d in docs
                if isinstance(d, Mapping) and isinstance(d.get("filename"), str)
            }
    effective_has_docs = has_posted_docs_flag or bool(docs_by_filename)

    raw_by_pass: dict[str, list[Mapping[str, Any]]] = {name: [] for name in GROUPED_PASS_KEYS}
    supplied_census_status: str | None = None

    if isinstance(raw, Mapping):
        unknown_keys = sorted(
            str(k) for k in raw if k not in GROUPED_PASS_KEYS and k not in GROUPED_OTHER_KEYS
        )
        if unknown_keys:
            raise OutcomesLayoutError(
                f"decision_rule_locations: unknown key(s) {unknown_keys} in a pre-grouped object; the "
                f"known passes are {sorted(GROUPED_PASS_KEYS)} (an unknown section would be dropped)"
            )
        if "nct_id" in raw:
            grouped_nct = strict_nct_id(raw["nct_id"], "decision_rule_locations.nct_id")
            if expected_nct_id is not None and grouped_nct != expected_nct_id:
                raise OutcomesLayoutError("decision_rule_locations.nct_id names a different trial")
            expected_nct_id = grouped_nct
        if "primary_pass" not in raw or not isinstance(raw["primary_pass"], Sequence) or isinstance(raw["primary_pass"], (str, bytes)):
            raise OutcomesLayoutError("decision_rule_locations dict must contain 'primary_pass' list")
        for pass_key in GROUPED_PASS_KEYS:
            val = raw.get(pass_key)
            if val is None:
                continue
            if not isinstance(val, Sequence) or isinstance(val, (str, bytes)):
                raise OutcomesLayoutError(f"decision_rule_locations.{pass_key} must be a list or None")
            raw_by_pass[pass_key].extend(val)
        cs = raw.get("census_status")
        if cs is not None:
            if cs not in CENSUS_STATUSES:
                raise OutcomesLayoutError(f"invalid decision_rule_locations.census_status: {cs!r}")
            supplied_census_status = str(cs)
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        if len(raw) == 0:
            return None
        pass_to_key = {name: key for key, name in GROUPED_PASS_KEYS.items()}
        for row in raw:
            if not isinstance(row, Mapping):
                raise OutcomesLayoutError("each row in locations must be an object")
            pass_name = row.get("pass", "primary")
            if pass_name not in ALLOWED_PASSES:
                raise OutcomesLayoutError(f"unknown location pass {pass_name!r}; expected one of {ALLOWED_PASSES}")
            raw_by_pass[pass_to_key[pass_name]].append(row)
    else:
        raise OutcomesLayoutError(
            f"decision_rule_locations must be a mapping, list of location rows, or None; got {type(raw).__name__}"
        )

    normalized_by_pass: dict[str, list[dict[str, Any]] | None] = {}
    for pass_key, pass_name in GROUPED_PASS_KEYS.items():
        rows = raw_by_pass[pass_key]
        if pass_key != "primary_pass" and not rows:
            normalized_by_pass[pass_key] = None
            continue
        normalized_by_pass[pass_key] = _normalize_location_pass(
            rows,
            pass_label=pass_key,
            docs_by_filename=docs_by_filename,
            has_posted_documents=effective_has_docs,
            pass_name=pass_name,
            expected_nct_id=expected_nct_id,
            primary_outcomes_count=primary_outcomes_count,
        )
    primary_pass = normalized_by_pass["primary_pass"] or []
    effective_pass = _resolve_effective_pass(
        primary_pass,
        normalized_by_pass["ocr_recovery_pass"],
        normalized_by_pass["adjudication_pass"],
    )

    census_status = census_status_from_effective_pass(effective_pass, primary_outcomes_count)
    if supplied_census_status is not None and supplied_census_status != census_status:
        raise OutcomesLayoutError(
            f"decision_rule_locations: supplied census_status {supplied_census_status!r} disagrees with "
            f"{census_status!r} computed from the rows; a supplied census_status is never trusted"
        )

    if census_status == "no_document" and effective_has_docs:
        raise OutcomesLayoutError(
            "decision_rule_locations: census_status 'no_document' is forbidden when posted document(s) are present"
        )
    if census_status == "no_document":
        cited_rows = _rows_with_citations(normalized_by_pass)
        if cited_rows:
            raise OutcomesLayoutError(
                f"decision_rule_locations: census_status 'no_document' is forbidden with citations "
                f"({cited_rows} row(s) in some pass cite a document)"
            )

    return canonicalize_nested({
        "census_status": census_status,
        "elements_expected_per_outcome": list(ELEMENTS),
        "primary_pass": primary_pass,
        "second_pass": normalized_by_pass["second_pass"],
        "ocr_recovery_pass": normalized_by_pass["ocr_recovery_pass"],
        "adjudication_pass": normalized_by_pass["adjudication_pass"],
        "effective_pass": effective_pass,
    })


# ── section 4: longitudinal_provenance (Module 4 adapter) ─────────────────────

def _validate_date_precisions_recursive(obj: Any, path: str = "longitudinal_provenance") -> None:
    """Validate every ``date_precision`` field is in ``DATE_PRECISIONS``."""
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            sub = f"{path}.{k}"
            if k == "date_precision" and v not in DATE_PRECISIONS:
                raise OutcomesLayoutError(
                    f"{sub} must be in {DATE_PRECISIONS}, got {v!r}"
                )
            _validate_date_precisions_recursive(v, sub)
    elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes)):
        for i, item in enumerate(obj):
            _validate_date_precisions_recursive(item, f"{path}[{i}]")


def normalize_longitudinal_provenance(
    raw: Mapping[str, Any] | None,
    *,
    in_audit: bool = False,
) -> dict[str, Any] | None:
    """Normalize Module 4 ``LongitudinalProvenanceRecord`` or direct provenance block.

    Bridges Module 4's ``large_docs`` / ``linked_publications`` /
    ``primary_completion_to_results_delta`` into the unified shard keys
    ``{registry_milestones, posted_documents, publications, date_deltas}``
    without dropping dated milestones or precision metadata.

    A holdout's stub carries no provenance: ``in_audit`` must be a strict
    bool, and ``in_audit=True`` with a record, or a record carrying a holdout
    flag (any spelling, any depth) that is not exactly False, is refused with
    ``AuditFirewallError`` (red-team m3 round 1: the holdout's registry
    milestones and posted documents were returned in full).
    """
    holdout = _require_strict_bool(in_audit, "longitudinal_provenance.in_audit")
    if raw is None:
        return None
    if holdout:
        raise AuditFirewallError(
            "in_audit holdout leak (stub) — a holdout trial carries no longitudinal_provenance; the "
            "record is refused, not normalized"
        )
    if not isinstance(raw, Mapping):
        raise OutcomesLayoutError(
            f"longitudinal_provenance must be an object or None, got {type(raw).__name__}"
        )
    _refuse_holdout_flags(raw, label="longitudinal_provenance")

    _validate_date_precisions_recursive(raw)

    # Support both Module 4 LongitudinalProvenanceRecord and direct Module 3 shape.
    registry_milestones = raw.get("registry_milestones")
    if not isinstance(registry_milestones, Mapping):
        raise OutcomesLayoutError("longitudinal_provenance must carry a 'registry_milestones' object")

    def _one_spelling(unified: str, module4: str, default: Any) -> Any:
        # A section may be given under its unified key or Module 4's; given
        # under both, the two must be the same data, or the record does not
        # say which it means and is refused (red-team m3 round 3: the unified
        # spelling was published and Module 4's dropped without a word).
        first, second = raw.get(unified), raw.get(module4)
        if first is not None and second is not None and canonicalize_nested(first) != canonicalize_nested(second):
            raise OutcomesLayoutError(
                f"longitudinal_provenance carries {unified} and {module4} and they differ; conflicting "
                f"spellings of one section are refused, not resolved by precedence"
            )
        if first is not None:
            return first
        return second if second is not None else default

    posted_docs_raw = _one_spelling("posted_documents", "large_docs", [])
    if not isinstance(posted_docs_raw, Sequence) or isinstance(posted_docs_raw, (str, bytes)):
        raise OutcomesLayoutError("longitudinal_provenance posted_documents/large_docs must be a list")

    pubs_raw = _one_spelling("publications", "linked_publications", [])
    if not isinstance(pubs_raw, Sequence) or isinstance(pubs_raw, (str, bytes)):
        raise OutcomesLayoutError("longitudinal_provenance publications/linked_publications must be a list")

    # Module 4's flat delta keys, bridged into date_deltas under these names.
    flat_deltas = {
        name: raw[flat_key]
        for name, flat_key in (
            ("primary_completion_to_results", "primary_completion_to_results_delta"),
            ("days_from_primary_completion_to_results", "days_from_primary_completion_to_results"),
        )
        if flat_key in raw
    }
    date_deltas_raw = raw.get("date_deltas")
    if date_deltas_raw is None:
        date_deltas_raw = flat_deltas
    elif not isinstance(date_deltas_raw, Mapping):
        raise OutcomesLayoutError("longitudinal_provenance.date_deltas must be an object")
    else:
        # Both spellings given: every flat delta must be in date_deltas as
        # the same data (round 3: date_deltas was published and the flat
        # delta it contradicted dropped).
        for name, value in flat_deltas.items():
            if name not in date_deltas_raw or canonicalize_nested(date_deltas_raw[name]) != canonicalize_nested(value):
                raise OutcomesLayoutError(
                    f"longitudinal_provenance carries date_deltas and Module 4's flat {name} delta, and "
                    f"date_deltas does not hold the same value; conflicting spellings are refused, not resolved"
                )

    out: dict[str, Any] = {
        "registry_milestones": dict(registry_milestones),
        "posted_documents": list(posted_docs_raw),
        "publications": list(pubs_raw),
        "date_deltas": dict(date_deltas_raw),
    }
    for meta_key in ("provenance_schema_version", "provenance_rules_version", "excluded_standalone_icf_count"):
        if meta_key in raw:
            out[meta_key] = raw[meta_key]
    # Module 4's own provenance stamps, published when given so the section
    # cannot outrun its source unnoticed (round 3: both were dropped).
    if "assembled_at" in raw:
        out["assembled_at"] = validate_iso_timestamp(raw["assembled_at"], "longitudinal_provenance.assembled_at")
    if "source_pipeline_commit" in raw:
        out["source_pipeline_commit"] = validate_commit_stamp(
            raw["source_pipeline_commit"], allow_null=True, label="longitudinal_provenance.source_pipeline_commit"
        )

    return canonicalize_nested(out)


# ── badges and trial bundle assembly ──────────────────────────────────────────

# A holdout's badges, exactly (vector 5 refuses any other content).
_MASKED_DOCUMENT_BADGE: dict[str, Any] = {
    "readability_status": "in_audit_holdout",
    "redaction_flags": None,
    "doc_count": None,
    "total_physical_pages": None,
}
_MASKED_CENSUS_BADGE: dict[str, Any] = {
    "census_status": "in_audit_holdout",
    "status_counts": None,
    "has_second_pass": False,
}


def build_document_badge(
    *,
    in_audit: bool,
    document_profile: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build ``index_row.document_badge`` with strict holdout masking.

    ``in_audit`` must be a strict bool (vector 1: never defaulted). Read by
    truthiness, ``None`` or ``0`` unmasked the badge (red-team m3 round 1).

    ``total_physical_pages`` is Module 1's badge rule: a total is stated only
    when every document's page count is known (0 for no documents). A
    download_failed or extraction_failed document has no count, and summing
    the others presented an undercount as the trial's total (red-team m3
    round 3), so the total is then None (unknown).
    """
    if _require_strict_bool(in_audit, "document_badge.in_audit"):
        return dict(_MASKED_DOCUMENT_BADGE)
    if document_profile is None:
        return {
            "readability_status": "not_indexed",
            "redaction_flags": None,
            "doc_count": None,
            "total_physical_pages": None,
        }
    docs = document_profile.get("documents", [])
    page_counts = [d.get("physical_pages") if isinstance(d, Mapping) else None for d in docs]
    total_pages: int | None = (
        sum(page_counts) if all(type(count) is int for count in page_counts) else None
    )
    return {
        "readability_status": str(document_profile["readability_summary"]),
        "redaction_flags": list(document_profile.get("redaction_flags", [])),
        "doc_count": len(docs),
        "total_physical_pages": total_pages,
    }


def build_census_badge(
    *,
    in_audit: bool,
    decision_rule_locations: Mapping[str, Any] | None,
    has_documents: bool,
) -> dict[str, Any]:
    """Build ``index_row.census_badge`` with strict holdout masking.

    ``in_audit`` must be a strict bool (vector 1: never defaulted), as for
    ``build_document_badge``.
    """
    if _require_strict_bool(in_audit, "census_badge.in_audit"):
        return dict(_MASKED_CENSUS_BADGE)
    if decision_rule_locations is None:
        return {
            "census_status": "not_started" if has_documents else "no_document",
            "status_counts": None,
            "has_second_pass": False,
        }
    effective_rows = decision_rule_locations.get("effective_pass")
    if (
        not isinstance(effective_rows, Sequence)
        or isinstance(effective_rows, (str, bytes))
        or not effective_rows
    ):
        effective_rows = decision_rule_locations.get("primary_pass", [])
    counts: dict[str, int] = {}
    for status in LOCATION_STATUSES:
        n = sum(1 for r in effective_rows if isinstance(r, Mapping) and r.get("status") == status)
        if n > 0:
            counts[status] = n
    census_status = str(decision_rule_locations["census_status"])
    return {
        "census_status": census_status,
        "status_counts": counts if census_status != "not_started" else None,
        "has_second_pass": bool(decision_rule_locations.get("second_pass")),
    }


def trial_nct_id(trial: Mapping[str, Any]) -> str:
    """The trial's ID from a flat row (``nct_id``) or a CT.gov study
    (``protocolSection.identificationModule.nctId``), strictly validated.

    When a record carries both, they must agree: a flat ``nct_id`` stamped
    onto another trial's CT.gov record is refused, not resolved either way.

    Method: every top-level key a reader takes for the record's ID is read,
    not only ``nct_id``: Module 4 reads a top-level ``nctId`` (and
    ``trial_nct_id``) as the record's ID too, so a record carrying ``nct_id``
    of one trial and ``nctId`` of another (red-team m3 round 1: built as the
    first, with full results, though the second was a holdout) is refused.
    Keys are recognised by ``_folded_key`` (any case, separator or invisible
    character). A key holding ``None`` is read as absent; every other value
    must be a strict ID, and all of them, with
    ``protocolSection.identificationModule.nctId``, must name one trial.
    """
    proto = trial.get("protocolSection") if isinstance(trial.get("protocolSection"), Mapping) else {}
    ident = proto.get("identificationModule") if isinstance(proto.get("identificationModule"), Mapping) else {}
    sources: list[tuple[str, Any]] = [
        (withhold_nct_ids(str(key)), value)
        for key, value in trial.items()
        if _folded_key(key) in _TRIAL_ID_KEY_FORMS and value is not None
    ]
    if ident.get("nctId") is not None:
        sources.append(("protocolSection.identificationModule.nctId", ident.get("nctId")))
    if not sources:
        raise OutcomesLayoutError("record carries no nct_id (neither nct_id nor identificationModule.nctId)")
    checked = [strict_nct_id(value, where) for where, value in sources]
    if len(set(checked)) > 1:
        raise OutcomesLayoutError(
            f"record's trial-ID fields ({', '.join(where for where, _ in sources)}) name different "
            f"trials; refused, not resolved either way"
        )
    return checked[0]


def _text_or_none(value: Any, label: str) -> str | None:
    """Return ``value`` when it is a ``str`` or ``None``, else raise.

    Index fields are scalars copied 1:1. ``str()`` of an object placed in one
    (``{"results": {"hr": "0.68"}}`` under ``brief_title``) published
    structured content, a holdout's results included, through a holdout's
    stub (red-team m3 round 1), so anything but a string or ``None`` is
    refused. The message names the field and the type, never the value.
    """
    if value is None or type(value) is str:
        return value
    raise OutcomesLayoutError(
        f"{label} must be a string or null, got {type(value).__name__}; an index field holds one "
        f"scalar copied as is, never the text of an object"
    )


def _study_scalar_fields(trial: Mapping[str, Any]) -> dict[str, Any]:
    """Extract lightweight trial-index metadata from either a flat manifest row
    or a full CT.gov API v2 study object. Missing values stay ``None``.

    Every text field is taken as written when it is a string and refused
    otherwise (``_text_or_none``): an object or list is never turned into its
    ``str()``. CT.gov's ``phases`` list is joined with ``/`` (each item must be
    a string) and its first condition is the primary condition (a string, or
    refused). ``brief_title`` falls back to ``title`` and then to ``""``, as
    before. Messages name the trial; ``build_trial_bundle`` withholds the ID
    when the trial is, or may be, a holdout.
    """
    proto = trial.get("protocolSection") if isinstance(trial.get("protocolSection"), Mapping) else {}
    ident = proto.get("identificationModule") if isinstance(proto.get("identificationModule"), Mapping) else {}
    design = proto.get("designModule") if isinstance(proto.get("designModule"), Mapping) else {}
    status_mod = proto.get("statusModule") if isinstance(proto.get("statusModule"), Mapping) else {}
    sponsor_mod = proto.get("sponsorCollaboratorsModule") if isinstance(proto.get("sponsorCollaboratorsModule"), Mapping) else {}
    lead_sponsor = sponsor_mod.get("leadSponsor") if isinstance(sponsor_mod.get("leadSponsor"), Mapping) else {}
    cond_mod = proto.get("conditionsModule") if isinstance(proto.get("conditionsModule"), Mapping) else {}
    enroll_info = design.get("enrollmentInfo") if isinstance(design.get("enrollmentInfo"), Mapping) else {}
    doc_sec = trial.get("documentSection") if isinstance(trial.get("documentSection"), Mapping) else {}
    large_doc_mod = doc_sec.get("largeDocumentModule") if isinstance(doc_sec.get("largeDocumentModule"), Mapping) else {}
    large_docs = large_doc_mod.get("largeDocs") if isinstance(large_doc_mod.get("largeDocs"), Sequence) else []

    nct_id = trial_nct_id(trial)
    brief_title = trial.get("brief_title")
    if brief_title is None:
        brief_title = ident.get("briefTitle")
    if brief_title is None:
        brief_title = trial.get("title")
    brief_title = _text_or_none(brief_title, f"{nct_id}.brief_title") or ""

    study_type = _text_or_none(
        trial.get("study_type") if "study_type" in trial else design.get("studyType"),
        f"{nct_id}.study_type",
    )

    phase = _text_or_none(trial.get("phase"), f"{nct_id}.phase")
    if phase is None and "phases" in design:
        phases = design.get("phases")
        if isinstance(phases, Sequence) and not isinstance(phases, (str, bytes)) and phases:
            phase = "/".join(
                str(_text_or_none(p, f"{nct_id}.designModule.phases[{i}]")) for i, p in enumerate(phases)
            )

    raw_enrollment = trial.get("enrollment") if "enrollment" in trial else enroll_info.get("count")
    enrollment = _strict_int_or_none(raw_enrollment, f"{nct_id}.enrollment")
    enrollment_type = _text_or_none(
        trial.get("enrollment_type") if "enrollment_type" in trial else enroll_info.get("type"),
        f"{nct_id}.enrollment_type",
    )

    def _date_struct(key_flat: str, key_struct: str) -> str | None:
        # A date is a string as written (blank is absent) or null; anything
        # else is refused by field and type, as every other text field is
        # (skeptic review of m3: a date object or number was published as
        # null without a word). CT.gov's struct is an object or null.
        if key_flat in trial:
            v = _text_or_none(trial[key_flat], f"{nct_id}.{key_flat}")
            return v if v is not None and v.strip() else None
        struct = status_mod.get(key_struct)
        if struct is None:
            return None
        if not isinstance(struct, Mapping):
            raise OutcomesLayoutError(
                f"{nct_id}.statusModule.{key_struct} must be an object or null, got {type(struct).__name__}"
            )
        v = _text_or_none(struct.get("date"), f"{nct_id}.statusModule.{key_struct}.date")
        return v if v is not None and v.strip() else None

    primary_completion_date = _date_struct("primary_completion_date", "primaryCompletionDateStruct")
    results_first_post_date = _date_struct("results_first_post_date", "resultsFirstPostDateStruct")

    if "lead_sponsor_name" in trial:
        lead_sponsor_name = trial.get("lead_sponsor_name")
    elif lead_sponsor.get("name") is not None:
        lead_sponsor_name = lead_sponsor.get("name")
    else:
        lead_sponsor_name = trial.get("sponsor")
    lead_sponsor_name = _text_or_none(lead_sponsor_name, f"{nct_id}.lead_sponsor_name")
    sponsor_class = _text_or_none(
        trial.get("sponsor_class") if "sponsor_class" in trial else lead_sponsor.get("class"),
        f"{nct_id}.sponsor_class",
    )
    ecosystem_category = _text_or_none(trial.get("ecosystem_category"), f"{nct_id}.ecosystem_category")

    primary_condition = _text_or_none(trial.get("primary_condition"), f"{nct_id}.primary_condition")
    if primary_condition is None:
        conds = cond_mod.get("conditions")
        if isinstance(conds, Sequence) and not isinstance(conds, (str, bytes)) and conds:
            primary_condition = _text_or_none(conds[0], f"{nct_id}.conditionsModule.conditions[0]")

    # The stated count, under either key (the live index writes
    # n_primary_outcomes, the number of registered primary outcomes). Both
    # are read: two different counts do not say which is meant, so they are
    # refused, never resolved by precedence (skeptic review of m3: 3 was
    # taken over 1 without a word). A count is a non-negative int or null.
    stated_counts: set[int] = set()
    for count_key in ("primary_outcomes_count", "n_primary_outcomes"):
        count = _strict_int_or_none(trial.get(count_key), f"{nct_id}.{count_key}")
        if count is not None and count < 0:
            raise OutcomesLayoutError(f"{nct_id}.{count_key} must be a non-negative int or null")
        if count is not None:
            stated_counts.add(count)
    if len(stated_counts) > 1:
        raise OutcomesLayoutError(
            f"{nct_id}: primary_outcomes_count and n_primary_outcomes are both given and differ (values not "
            f"shown); conflicting counts are refused, not resolved"
        )
    primary_outcomes_count = next(iter(stated_counts), None)

    large_has_protocol = any(
        isinstance(d, Mapping) and bool(d.get("hasProtocol"))
        for d in large_docs
    )
    large_has_sap = any(
        isinstance(d, Mapping) and bool(d.get("hasSap"))
        for d in large_docs
    )
    if "protocol" in trial:
        protocol_flag = _require_strict_bool(trial["protocol"], f"{nct_id}.protocol") or large_has_protocol
    else:
        protocol_flag = large_has_protocol
    if "sap" in trial:
        sap_flag = _require_strict_bool(trial["sap"], f"{nct_id}.sap") or large_has_sap
    else:
        sap_flag = large_has_sap

    return {
        "nct_id": nct_id,
        "brief_title": brief_title,
        "study_type": study_type,
        "phase": phase,
        "enrollment": enrollment,
        "enrollment_type": enrollment_type,
        "primary_completion_date": primary_completion_date,
        "results_first_post_date": results_first_post_date,
        "lead_sponsor_name": lead_sponsor_name,
        "sponsor_class": sponsor_class,
        "ecosystem_category": ecosystem_category,
        "primary_condition": primary_condition,
        "primary_outcomes_count": primary_outcomes_count,
        "protocol": protocol_flag,
        "sap": sap_flag,
    }


def _declared_in_scope(trial: Mapping[str, Any], in_audit: Any) -> bool:
    """True only when the caller and the record both say the trial is in scope.

    The ``in_audit`` argument is exactly False (or absent with the record's
    own ``in_audit`` exactly False), and every top-level holdout flag of the
    record, under any spelling a reader takes for one (``is_holdout_flag_key``),
    is exactly False. Anything else means the trial is, or may be, a holdout.
    """
    if not isinstance(trial, Mapping):
        return False
    if in_audit is not False and not (in_audit is None and trial.get("in_audit") is False):
        return False
    return all(value is False for key, value in trial.items() if is_holdout_flag_key(key))


def _route_text(route: Sequence[Any]) -> str:
    """A position (``a.b[0].c``) from a route of keys and list indexes, NCT-like tokens withheld."""
    text = ""
    for step in route:
        text += f"[{step}]" if type(step) is int else (f".{step}" if text else str(step))
    return withhold_nct_ids(text)


def _find_inline_sections(obj: Any) -> list[str]:
    """Positions, at any depth, of keys naming a shard section or side input
    (``HOLDOUT_INLINE_SECTION_FORMS``), with any NCT-like token withheld."""
    return [
        withhold_nct_ids(where)
        for where, key, _value in _iter_mapping_entries(plain_data(obj, "trial record"))
        if _folded_key(key) in HOLDOUT_INLINE_SECTION_FORMS
    ]


def _find_unwrapped_side_content(trial: Mapping[str, Any]) -> list[str]:
    """Positions of side content carried in a record without a wrapper the builder reads.

    Method: a walk over the whole record, following its structure (keys and
    list indexes), that reports every key whose folded form is a side
    section's content key (``HOLDOUT_SIDE_CONTENT_FORMS``) or a side row's
    own key (``SIDE_ROW_KEY_FORMS``), wherever it sits. Inside CT.gov's native
    sections (``_CTGOV_NATIVE_SECTIONS``) the forms CT.gov's own schema uses
    there (``_CTGOV_NATIVE_CONTENT_FORMS``: ``largeDocs``) are allowed; every
    other form is read there too (red-team m3 round 3: a primary_pass inside
    protocolSection.outcomesModule, a documents list inside resultsSection and
    PDF-scraped sha256/version_label on largeDocs were dropped without a word).
    The walk does not enter a key naming a whole section
    (``HOLDOUT_INLINE_SECTION_FORMS``): such a key is read where the builder
    reads it and refused anywhere else (``_find_inline_sections`` for a
    holdout, ``_unread_inline_sections`` for any other trial).

    A holdout's own record is reduced to its stub, so any hit is holdout
    material refused rather than dropped (round 2: a top-level documents or
    registry_milestones; round 3: rows under a wrapper no list names, such as
    pdf_index or rule_census). An ordinary trial's record is read the same
    way: the builder reads side content only from a section wrapper at the
    top of the record, so a hit would be dropped and is refused (round 3).
    Positions have NCT-like tokens withheld.
    """
    hits: list[str] = []

    def _walk(node: Any, route: tuple[Any, ...], native: bool) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                here = (*route, key)
                form = _folded_key(key)
                if form in HOLDOUT_INLINE_SECTION_FORMS:
                    continue
                if (form in HOLDOUT_SIDE_CONTENT_FORMS or form in SIDE_ROW_KEY_FORMS) and not (
                    native and form in _CTGOV_NATIVE_CONTENT_FORMS
                ):
                    hits.append(_route_text(here))
                _walk(value, here, native or (not route and key in _CTGOV_NATIVE_SECTIONS))
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                _walk(item, (*route, idx), native)

    _walk(plain_data(trial, "trial record"), (), False)
    return hits


def _is_primary_ctgov_measure(trial: Mapping[str, Any], index: Any) -> bool:
    """True when ``resultsSection.outcomeMeasuresModule.outcomeMeasures[index]`` is a PRIMARY measure."""
    results = trial.get("resultsSection")
    module = results.get("outcomeMeasuresModule") if isinstance(results, Mapping) else None
    measures = module.get("outcomeMeasures") if isinstance(module, Mapping) else None
    if type(index) is not int or not isinstance(measures, list) or not 0 <= index < len(measures):
        return False
    measure = measures[index]
    return isinstance(measure, Mapping) and str(measure.get("type") or "").upper() == "PRIMARY"


def _unread_inline_sections(trial: Mapping[str, Any]) -> list[str]:
    """Positions of section keys in an in-scope trial's record that the builder does not read.

    Method: a key whose folded form names a section (``HOLDOUT_INLINE_SECTION_FORMS``)
    is read in exactly these places, under exactly these spellings: a
    top-level ``document_profile``, ``decision_rule_locations`` or
    ``longitudinal_provenance`` (``INLINE_SIDE_SECTIONS``, published as that
    section) and a top-level pre-built ``reported_results``; an
    ``analysis_population_parsed`` inside a PRIMARY measure of
    ``resultsSection.outcomeMeasuresModule.outcomeMeasures`` or inside
    ``reported_results.primary_outcome_measures``. A section key anywhere
    else (nested, another spelling, a secondary measure) would be dropped
    without a word, so its position is returned for a refusal. The walk
    follows the structure itself (keys and indexes), never a path string a
    key could imitate. A side section's interior is its normalizer's to read.
    """
    unread: list[str] = []

    def _read_here(route: tuple[Any, ...], key: str) -> bool:
        # ``route`` is the position of the object holding ``key``.
        if not route:
            return key in INLINE_SIDE_SECTIONS or key == "reported_results"
        if key != "analysis_population_parsed":
            return False
        if len(route) == 4 and route[:3] == ("resultsSection", "outcomeMeasuresModule", "outcomeMeasures"):
            return _is_primary_ctgov_measure(trial, route[3])
        return len(route) == 3 and route[:2] == ("reported_results", "primary_outcome_measures") and type(route[2]) is int

    def _walk(node: Any, route: tuple[Any, ...]) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                here = (*route, key)
                if _folded_key(key) in HOLDOUT_INLINE_SECTION_FORMS:
                    if not _read_here(route, key):
                        unread.append(_route_text(here))
                        continue
                    if key != "reported_results":
                        continue
                _walk(value, here)
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                _walk(item, (*route, idx))

    _walk(trial, ())
    return unread


def _resolve_inline_section(trial: Mapping[str, Any], key: str, supplied: Any) -> Any:
    """The one value of a side section given inline in the record, as a side input, or both.

    Method: a ``None`` (or absent) value is no value. When both are given
    they must be the same data (compared as ``canonicalize_nested`` of their
    plain form); otherwise the inputs conflict and are refused, never
    resolved either way (red-team m3 round 2: the side input was published
    and the inline section dropped).
    """
    inline = trial.get(key)
    if inline is None:
        return supplied
    if supplied is None:
        return inline
    if canonicalize_nested(plain_data(inline, key)) != canonicalize_nested(plain_data(supplied, key)):
        raise OutcomesLayoutError(
            f"{key} is given inline in the trial's record and as a side input, and the two differ; "
            f"conflicting inputs are refused, not resolved"
        )
    return supplied


def _document_sha256_conflicts(profile: Any, provenance: Any) -> list[str]:
    """Filenames to which the document profile and the provenance give different sha256 values.

    Method: a posted document is one file, so Module 1's profile (its
    ``documents``) and Module 4's provenance (its ``posted_documents``) must
    agree on its bytes wherever both state them: for each filename in both
    with a non-null sha256 on each side, the two must be equal (red-team m3
    round 3: the shard said bbbb... in document_profile and aaaa... in
    longitudinal_provenance for one file, and --check passed it). A side that
    states no sha256 does not conflict. Returns the sorted conflicting names.
    """
    if not isinstance(profile, Mapping) or not isinstance(provenance, Mapping):
        return []
    profile_docs = profile.get("documents")
    posted = provenance.get("posted_documents")
    if not isinstance(profile_docs, list) or not isinstance(posted, list):
        return []
    by_name = {
        doc["filename"]: doc.get("sha256")
        for doc in profile_docs
        if isinstance(doc, Mapping) and isinstance(doc.get("filename"), str)
    }
    conflicting = {
        doc["filename"]
        for doc in posted
        if isinstance(doc, Mapping)
        and isinstance(doc.get("filename"), str)
        and by_name.get(doc["filename"]) is not None
        and doc.get("sha256") is not None
        and doc.get("sha256") != by_name[doc["filename"]]
    }
    return sorted(conflicting)


def _has_results_section(trial: Mapping[str, Any]) -> bool:
    """True when the record carries a non-empty CT.gov ``resultsSection`` object."""
    results = trial.get("resultsSection")
    return isinstance(results, Mapping) and bool(results)


def build_trial_bundle(
    trial: Mapping[str, Any],
    *,
    shards: int = DEFAULT_OUTCOMES_SHARDS,
    in_audit: Any = None,
    explicit_cohorts: Sequence[str] | None = None,
    document_profile: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    decision_rule_locations: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    longitudinal_provenance: Mapping[str, Any] | None = None,
    population_parser: Callable[[Mapping[str, Any]], Any] | None = None,
    parsed_populations: Any = None,
    audit_nct_ids: AbstractSet[str] = frozenset(),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Construct and audit-check ``(index_row, shard_record)`` for one trial.

    ``in_audit`` (when given) and the trial's own holdout flags, under every
    spelling a reader takes for one (``is_holdout_flag_key``), must agree;
    only the ``in_audit`` argument, or the record's exact ``in_audit`` key
    when no argument is given, admits a trial. A holdout trial (``in_audit``
    true) is built as a stub: its index row holds the fields that come 1:1
    from the trial/manifest row, masked badges and ``has_reported_results``
    ``None``; its shard record holds ``nct_id``, ``in_audit``, ``cohorts`` and
    ``shard`` with every section ``None``. Its results are not extracted, its
    population parser is not called, and any side input for it, passed in or
    carried in its own record (a section wrapper anywhere,
    ``HOLDOUT_INLINE_SECTION_FORMS``, or a section's content without its
    wrapper outside CT.gov's native sections, ``HOLDOUT_SIDE_CONTENT_FORMS``),
    is refused (``AuditFirewallError``), never dropped.

    For any other trial, a side section carried inline at the top of its
    record is published as that section (``_resolve_inline_section``: one
    also given as a side input must be the same data), a section key the
    builder does not read is refused (``_unread_inline_sections``), and the
    population parser runs before the checks, so its result is checked like
    ``parsed_populations``. Every input is read as ``plain_data`` first (a
    dataclass by its fields, a UserList as a list), so what is checked is
    what is published. Every record is checked with
    ``check_trial_record_identity`` (a nested trial-ID key names this trial;
    every nested holdout flag agrees), and for any other trial every side
    input with ``check_side_input_identity``; ``audit_nct_ids`` (the build's
    holdout set, when the caller knows it) adds the audit-ID scan to both.

    When the trial is, or may be, a holdout (``_declared_in_scope`` is
    false), or a message names a trial of ``audit_nct_ids``, every refusal
    has its NCT-like tokens withheld: a refusal about a holdout's row (an
    enrollment of the wrong type) printed its ID (red-team m3 round 1).
    """
    withhold = not _declared_in_scope(trial, in_audit)
    try:
        audit = _strict_id_set(audit_nct_ids, "audit_nct_ids")
    except OutcomesLayoutError as exc:
        raise type(exc)(withhold_nct_ids(str(exc))) from None
    try:
        return _build_trial_bundle(
            trial,
            shards=shards,
            in_audit=in_audit,
            explicit_cohorts=explicit_cohorts,
            document_profile=document_profile,
            decision_rule_locations=decision_rule_locations,
            longitudinal_provenance=longitudinal_provenance,
            population_parser=population_parser,
            parsed_populations=parsed_populations,
            audit_nct_ids=audit,
        )
    except OutcomesLayoutError as exc:
        message = str(exc)
        shown = withhold_nct_ids(message) if withhold else withhold_if_names(message, audit)
        if shown == message:
            raise
        raise type(exc)(shown) from None


def _build_trial_bundle(
    trial: Mapping[str, Any],
    *,
    shards: int,
    in_audit: Any,
    explicit_cohorts: Sequence[str] | None,
    document_profile: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    decision_rule_locations: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    longitudinal_provenance: Mapping[str, Any] | None,
    population_parser: Callable[[Mapping[str, Any]], Any] | None,
    parsed_populations: Any,
    audit_nct_ids: frozenset[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The body of ``build_trial_bundle`` (see there); messages may name the trial."""
    record = plain_data(trial, "trial record")
    if not isinstance(record, dict):
        raise OutcomesLayoutError(f"a trial record must be an object, got {type(record).__name__}")
    profile_in: Any = plain_data(document_profile, "document_profile")
    locations_in: Any = plain_data(decision_rule_locations, "decision_rule_locations")
    provenance_in: Any = plain_data(longitudinal_provenance, "longitudinal_provenance")
    populations_in: Any = plain_data(parsed_populations, "parsed populations")

    meta = _study_scalar_fields(record)
    nct_id = meta["nct_id"]
    shard_id = outcomes_shard_of(nct_id, shards)

    # The record's own top-level holdout flags, under every spelling: each a
    # strict bool, and all agreeing with the trial's in_audit. They can only
    # refuse; only the argument (or the exact in_audit key) admits.
    own_flags = [(key, value) for key, value in record.items() if is_holdout_flag_key(key)]
    for _key, value in own_flags:
        if type(value) is not bool:
            raise AuditFirewallError(
                f"{nct_id}: the record carries a holdout flag that is not a bool "
                f"({type(value).__name__}); its holdout status is unknown"
            )
    raw_in_audit = in_audit if in_audit is not None else record.get("in_audit")
    if type(raw_in_audit) is bool and any(value is not raw_in_audit for _key, value in own_flags):
        raise AuditFirewallError(
            f"{nct_id}: in_audit is {raw_in_audit} but the record's own holdout flag "
            f"(under some spelling of in_audit) disagrees; a conflicting holdout flag is refused"
        )
    # Run initial firewall check on raw inputs (Vectors 1, 2, 3, 4).
    assert_no_audit_leakage(
        nct_id=nct_id,
        in_audit=raw_in_audit,
        document_profile=profile_in,
        decision_rule_locations=locations_in,
        longitudinal_provenance=provenance_in,
        raw_payload=record if raw_in_audit is True else None,
    )
    is_holdout = raw_in_audit is True
    if is_holdout and populations_in is not None:
        raise AuditFirewallError(
            f"{nct_id}: in_audit holdout leak (stub) — parsed populations supplied for a holdout trial"
        )
    if is_holdout:
        inline = _find_inline_sections(record)
        if inline:
            raise AuditFirewallError(
                f"{nct_id}: in_audit holdout leak (stub) — the holdout's own record carries "
                f"{len(inline)} side section(s) inline (e.g. {', '.join(inline[:3])}); holdout material "
                f"in the input is refused, never dropped"
            )
        unwrapped = _find_unwrapped_side_content(record)
        if unwrapped:
            raise AuditFirewallError(
                f"{nct_id}: in_audit holdout leak (stub) — the holdout's own record carries section content "
                f"without its wrapper ({len(unwrapped)} key(s), e.g. {', '.join(unwrapped[:3])}); holdout "
                f"material in the input is refused, never dropped"
            )

    # The label names the trial, so a refusal says which record to look at;
    # build_trial_bundle withholds every ID in it when the trial is, or may
    # be, a holdout, or the message names one.
    check_trial_record_identity(
        record,
        expected_nct_id=nct_id,
        expected_in_audit=is_holdout,
        audit_nct_ids=audit_nct_ids,
        label=f"{nct_id} trial record",
    )
    if explicit_cohorts is not None:
        # Explicit cohorts are published in the index row and the shard, so
        # they are scanned like the record (red-team m3 round 3: a slug
        # spelling a holdout's ID was published; the CLI's --manifest-cohort
        # was caught only by the staged-tree check).
        _scan_for_audit_trials(explicit_cohorts, audit_nct_ids=audit_nct_ids - {nct_id}, label="explicit_cohorts")
    if not is_holdout:
        unread = _unread_inline_sections(record)
        if unread:
            raise OutcomesLayoutError(
                f"{nct_id}: the record carries {len(unread)} section key(s) where the builder does not read "
                f"them (e.g. {', '.join(unread[:3])}); they would be dropped, so they are refused"
            )
        unwrapped = _find_unwrapped_side_content(record)
        if unwrapped:
            raise OutcomesLayoutError(
                f"{nct_id}: the record carries side content outside a section wrapper the builder reads "
                f"({len(unwrapped)} key(s), e.g. {', '.join(unwrapped[:3])}); give it as document_profile, "
                f"decision_rule_locations or longitudinal_provenance (or as a side input); it would be dropped, "
                f"so it is refused"
            )
        profile_in = _resolve_inline_section(record, "document_profile", profile_in)
        locations_in = _resolve_inline_section(record, "decision_rule_locations", locations_in)
        provenance_in = _resolve_inline_section(record, "longitudinal_provenance", provenance_in)
        if populations_in is None and population_parser is not None and _has_results_section(record):
            # Called before the side-input checks, so its result is read like
            # any parsed populations (red-team m3 round 2). It is given a deep
            # copy of the record: an edit it makes to what it is given is
            # never read (red-team m3 round 3: the parser received the
            # builder's working record after the identity checks, and its
            # edit, a holdout's ID written into a measure, was published).
            populations_in = plain_data(population_parser(copy.deepcopy(record)), "population_parser result")
    side_inputs: tuple[tuple[str, Any], ...] = (
        ("document_profile", profile_in),
        ("decision_rule_locations", locations_in),
        ("longitudinal_provenance", provenance_in),
        ("population_parsed", populations_in),
    )
    for side_label, side_value in side_inputs:
        if side_value is not None:
            check_side_input_identity(
                side_value,
                expected_nct_id=nct_id,
                expected_in_audit=is_holdout,
                label=side_label,
                audit_nct_ids=audit_nct_ids,
            )

    cohorts = derive_cohorts(
        explicit_cohorts if explicit_cohorts is not None else record.get("cohorts"),
        ecosystem_category=meta["ecosystem_category"],
        sponsor_class=meta["sponsor_class"],
        phase=meta["phase"],
        protocol=meta["protocol"],
        sap=meta["sap"],
    )

    if is_holdout:
        # Stub: nothing beyond the 1:1 index fields. primary_outcomes_count is
        # the row's own count when it states one, never one counted from results.
        reported_results: dict[str, Any] | None = None
        primary_outcomes_count: int | None = meta["primary_outcomes_count"]
        norm_profile: dict[str, Any] | None = None
        norm_prov: dict[str, Any] | None = None
        norm_locations: dict[str, Any] | None = None
        has_docs = bool(meta["protocol"] or meta["sap"])
        has_reported_results: bool | None = None
    else:
        reported_results = extract_reported_results(record, parsed_populations=populations_in)
        primary_outcomes_count = primary_outcomes_count_of(
            meta["primary_outcomes_count"], reported_results, label=nct_id
        )

        norm_profile = normalize_document_profile(profile_in, expected_nct_id=nct_id)
        norm_prov = normalize_longitudinal_provenance(provenance_in, in_audit=False)
        conflicting = _document_sha256_conflicts(norm_profile, norm_prov)
        if conflicting:
            raise OutcomesLayoutError(
                f"{nct_id}: the document profile and the longitudinal provenance give {len(conflicting)} posted "
                f"document(s) different sha256 values (e.g. {conflicting[0]}); conflicting inputs are refused, "
                f"never both published"
            )
        has_docs = (
            meta["protocol"]
            or meta["sap"]
            or bool(norm_profile and norm_profile["documents"])
            or bool(norm_prov and norm_prov["posted_documents"])
        )
        norm_locations = normalize_decision_rule_locations(
            locations_in,
            document_profile=norm_profile,
            primary_outcomes_count=primary_outcomes_count,
            has_posted_documents=bool(has_docs),
            expected_nct_id=nct_id,
        )
        has_reported_results = reported_results["results_posted"]

    doc_badge = build_document_badge(in_audit=is_holdout, document_profile=norm_profile)
    census_badge = build_census_badge(
        in_audit=is_holdout,
        decision_rule_locations=norm_locations,
        has_documents=bool(has_docs),
    )

    index_row: dict[str, Any] = {
        "nct_id": nct_id,
        "brief_title": meta["brief_title"],
        "study_type": meta["study_type"],
        "phase": meta["phase"],
        "enrollment": meta["enrollment"],
        "enrollment_type": meta["enrollment_type"],
        "primary_completion_date": meta["primary_completion_date"],
        "results_first_post_date": meta["results_first_post_date"],
        "lead_sponsor_name": meta["lead_sponsor_name"],
        "sponsor_class": meta["sponsor_class"],
        "ecosystem_category": meta["ecosystem_category"],
        "primary_condition": meta["primary_condition"],
        "cohorts": cohorts,
        "in_audit": is_holdout,
        "protocol": meta["protocol"],
        "sap": meta["sap"],
        "primary_outcomes_count": primary_outcomes_count,
        "has_reported_results": has_reported_results,
        "document_badge": canonicalize_nested(doc_badge),
        "census_badge": canonicalize_nested(census_badge),
    }

    shard_record: dict[str, Any] = {
        "nct_id": nct_id,
        "in_audit": is_holdout,
        "cohorts": cohorts,
        "shard": shard_id,
        "reported_results": reported_results,
        "document_profile": norm_profile,
        "decision_rule_locations": norm_locations,
        "longitudinal_provenance": norm_prov,
    }

    # Final firewall verification on assembled row + shard record (all 5 in-record vectors).
    assert_no_audit_leakage(
        nct_id=nct_id,
        in_audit=is_holdout,
        shard_record=shard_record,
        index_row=index_row,
    )
    return index_row, shard_record


def _normalize_stamp_block(block: Any, label: str) -> dict[str, Any]:
    """One input's header stamps (``INPUT_STAMP_KEYS``), each checked and kept as given.

    Method: ``extracted_at`` is an ISO-8601 timestamp, ``pipeline_commit`` and
    ``source_pipeline_commit`` a 7..40 hex commit, ``schema_version`` an int
    or a string, the others strings; any may be null (stated as absent). Any
    other key, an object or list as a value, or an empty block is refused.
    Messages name the key, never the value.
    """
    if not isinstance(block, Mapping) or not block:
        raise OutcomesLayoutError(f"{label} must be a non-empty object of header stamps, got {type(block).__name__}")
    out: dict[str, Any] = {}
    for key, value in block.items():
        if key not in INPUT_STAMP_KEYS:
            raise OutcomesLayoutError(
                f"{label} carries a key that is not a header stamp ({withhold_nct_ids(repr(key))}); the stamps are "
                f"{list(INPUT_STAMP_KEYS)}"
            )
        if value is None:
            out[key] = None
        elif key == "extracted_at":
            out[key] = validate_iso_timestamp(value, f"{label}.extracted_at")
        elif key in ("pipeline_commit", "source_pipeline_commit"):
            out[key] = validate_commit_stamp(value, allow_null=True, label=f"{label}.{key}")
        elif key == "schema_version":
            if type(value) not in (int, str):
                raise OutcomesLayoutError(f"{label}.schema_version must be an int or a string, got {type(value).__name__}")
            out[key] = value
        elif type(value) is not str:
            raise OutcomesLayoutError(f"{label}.{key} must be a string or null, got {type(value).__name__}")
        else:
            out[key] = value
    return out


def normalize_input_stamps(stamps: Mapping[str, Any] | None) -> dict[str, Any]:
    """index.json's ``input_stamps``: each input's own container stamps, as given.

    Method: keys are ``INPUT_STAMP_SOURCES``; ``locations`` holds a non-empty
    list of stamp blocks (one per file that carries any), every other input
    one block (``_normalize_stamp_block``). ``None`` is ``{}`` (no input
    carried a stamp). The builder's own pair (``extracted_at``,
    ``source_pipeline_commit``) is the index header's; these are the inputs'.
    """
    if stamps is None:
        return {}
    plain = plain_data(stamps, "input_stamps")
    if not isinstance(plain, dict):
        raise OutcomesLayoutError(f"input_stamps must be an object, got {type(plain).__name__}")
    out: dict[str, Any] = {}
    for source, block in plain.items():
        if source not in INPUT_STAMP_SOURCES:
            raise OutcomesLayoutError(
                f"input_stamps names an input that is not one of {list(INPUT_STAMP_SOURCES)} "
                f"({withhold_nct_ids(repr(source))})"
            )
        label = f"input_stamps.{source}"
        if source == "locations":
            if not isinstance(block, list) or not block:
                raise OutcomesLayoutError(f"{label} must be a non-empty list of stamp blocks, got {type(block).__name__}")
            out[source] = [_normalize_stamp_block(item, f"{label}[{pos}]") for pos, item in enumerate(block)]
        else:
            out[source] = _normalize_stamp_block(block, label)
    return canonicalize_nested(out)


def build_outcomes_documents(
    bundles: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    *,
    extracted_at: str,
    source_pipeline_commit: str | None,
    shards: int = DEFAULT_OUTCOMES_SHARDS,
    component_versions: Mapping[str, str | None] | None = None,
    allow_null_commit: bool = False,
    input_stamps: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    """Assemble deterministic ``index.json`` and ``0..shards-1`` shard documents.

    Output is invariant to input ordering: ``index.json`` rows and every
    shard's ``data`` dict are sorted strictly ascending by ``nct_id``.
    Top-level header keys keep ``"data"`` last. ``input_stamps`` (each
    input container's own stamps, ``normalize_input_stamps``) is published
    in the index header beside the build's own ``extracted_at`` and
    ``source_pipeline_commit``.
    """
    n_shards = _validate_shards(shards)
    ts = validate_iso_timestamp(extracted_at, "extracted_at")
    commit = validate_commit_stamp(source_pipeline_commit, allow_null=allow_null_commit)
    stamps = normalize_input_stamps(input_stamps)

    by_nct: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for index_row_raw, shard_rec_raw in bundles:
        index_row = dict(index_row_raw)
        shard_rec = dict(shard_rec_raw)
        nct_id = index_row.get("nct_id")
        expected_shard = outcomes_shard_of(nct_id, n_shards)
        if shard_rec.get("nct_id") != nct_id:
            raise OutcomesLayoutError(
                f"mismatched nct_id between index_row ({nct_id!r}) and shard_record ({shard_rec.get('nct_id')!r})"
            )
        if shard_rec.get("shard") != expected_shard:
            raise OutcomesLayoutError(
                f"{nct_id}: shard_record.shard is {shard_rec.get('shard')!r}, expected {expected_shard}"
            )
        if nct_id in by_nct:
            raise OutcomesLayoutError(f"duplicate nct_id in outcomes build: {nct_id!r}")
        assert_no_audit_leakage(
            nct_id=str(nct_id),
            in_audit=index_row.get("in_audit"),
            shard_record=shard_rec,
            index_row=index_row,
        )
        by_nct[str(nct_id)] = (index_row, shard_rec)

    sorted_ncts = sorted(by_nct)
    index_rows: list[dict[str, Any]] = []
    cohort_counts: dict[str, int] = {}
    audit_holdout_trials = 0
    shard_buckets: dict[int, dict[str, dict[str, Any]]] = {n: {} for n in range(n_shards)}

    for nct_id in sorted_ncts:
        index_row, shard_rec = by_nct[nct_id]
        # Canonicalize key order inside rows while preserving exact schema.
        ordered_row = {k: canonicalize_nested(index_row[k]) for k in INDEX_ROW_KEYS}
        ordered_rec = {k: canonicalize_nested(shard_rec[k]) for k in SHARD_RECORD_KEYS}
        index_rows.append(ordered_row)
        if ordered_row["in_audit"]:
            audit_holdout_trials += 1
        for slug in ordered_row["cohorts"]:
            cohort_counts[slug] = cohort_counts.get(slug, 0) + 1
        shard_buckets[ordered_rec["shard"]][nct_id] = ordered_rec

    norm_versions: dict[str, str | None] = (
        {str(k): (str(v) if v is not None else None) for k, v in sorted(component_versions.items())}
        if component_versions is not None
        else {}
    )

    index_doc: dict[str, Any] = {
        "extracted_at": ts,
        "source_pipeline_commit": commit,
        "layout_version": LAYOUT_VERSION,
        "layout": layout_header(n_shards),
        "cohort_counts": {k: cohort_counts[k] for k in sorted(cohort_counts)},
        "total_trials": len(index_rows),
        "audit_holdout_trials": audit_holdout_trials,
        "component_versions": norm_versions,
        "input_stamps": stamps,
        "data": index_rows,
    }

    shard_docs: dict[int, dict[str, Any]] = {}
    for n in range(n_shards):
        shard_docs[n] = {
            "extracted_at": ts,
            "source_pipeline_commit": commit,
            "layout_version": LAYOUT_VERSION,
            "class": SHARD_CLASS,
            "shard": n,
            "shards": n_shards,
            "key": SHARD_KEY,
            "data": shard_buckets[n],
        }
    return index_doc, shard_docs


# ── validation telemetry and on-disk verifier ─────────────────────────────────

@dataclass(frozen=True)
class BudgetTelemetry:
    """Size telemetry for an outcomes tree (advisory budget reporting only)."""
    index_bytes: int
    total_shard_bytes: int
    total_bytes: int
    max_file_bytes: int
    max_file_path: str
    min_shard_bytes: int
    median_shard_bytes: int
    max_shard_bytes: int
    non_empty_shards: int
    empty_shards: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "index_bytes": self.index_bytes,
            "total_shard_bytes": self.total_shard_bytes,
            "total_bytes": self.total_bytes,
            "max_file_bytes": self.max_file_bytes,
            "max_file_path": self.max_file_path,
            "min_shard_bytes": self.min_shard_bytes,
            "median_shard_bytes": self.median_shard_bytes,
            "max_shard_bytes": self.max_shard_bytes,
            "non_empty_shards": self.non_empty_shards,
            "empty_shards": self.empty_shards,
        }


@dataclass(frozen=True)
class ValidationResult:
    """Result of ``validate_outcomes_tree``."""
    ok: bool
    problems: tuple[str, ...]
    warnings: tuple[str, ...]
    total_trials: int
    audit_holdout_trials: int
    shards: int
    audit_leakage_detected: bool
    budget: BudgetTelemetry | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "problems": list(self.problems),
            "warnings": list(self.warnings),
            "total_trials": self.total_trials,
            "audit_holdout_trials": self.audit_holdout_trials,
            "shards": self.shards,
            "audit_leakage_detected": self.audit_leakage_detected,
            "budget": self.budget.to_dict() if self.budget is not None else None,
        }


def _shard_has_posted_documents(
    idx_row: Mapping[str, Any] | None,
    document_profile: Any = None,
    longitudinal_provenance: Any = None,
) -> bool:
    """Return True if any on-disk source proves posted protocol/SAP document(s)."""
    has_profile_docs = (
        isinstance(document_profile, Mapping)
        and isinstance(document_profile.get("documents"), list)
        and len(document_profile["documents"]) > 0
    )
    has_manifest_docs = (
        isinstance(idx_row, Mapping)
        and (idx_row.get("protocol") is True or idx_row.get("sap") is True)
    )
    has_prov_docs = (
        isinstance(longitudinal_provenance, Mapping)
        and isinstance(longitudinal_provenance.get("posted_documents"), list)
        and len(longitudinal_provenance["posted_documents"]) > 0
    )
    return has_profile_docs or has_manifest_docs or has_prov_docs


def _validate_shard_decision_rule_locations(
    rel: str,
    nct_id: str,
    locs: Any,
    idx_row: Mapping[str, Any] | None,
    document_profile: Any = None,
    longitudinal_provenance: Any = None,
) -> list[str]:
    """Validate a non-audit shard record's ``decision_rule_locations`` block.

    Applies on disk the rules normalization applies on input, from the same
    functions: Module 4's ``validate_citation_shape`` on every citation (so
    ``[None]``, ``['x']`` and ``[{}]`` fail here too) plus this module's
    document cross-check (``citation_problems``), Module 4's
    ``validate_status_citations`` on every row of every pass and of
    ``effective_pass``, the census status recomputed from ``effective_pass``
    (``census_status_from_effective_pass``) rather than read, ``no_document``
    with any citation, and the block's exact key set.
    """
    errs: list[str] = []
    prefix = f"{rel}.data[{nct_id}].decision_rule_locations"
    if not isinstance(locs, Mapping):
        return [f"{prefix} must be an object or None"]

    block_keys = set(locs.keys())
    if block_keys != LOCATIONS_BLOCK_KEYS:
        errs.append(
            f"{prefix} key mismatch (missing={sorted(LOCATIONS_BLOCK_KEYS - block_keys)}, "
            f"extra={sorted(str(k) for k in block_keys - LOCATIONS_BLOCK_KEYS)})"
        )
    if locs.get("elements_expected_per_outcome") != list(ELEMENTS):
        errs.append(f"{prefix}.elements_expected_per_outcome is not {list(ELEMENTS)}")

    has_posted_docs = _shard_has_posted_documents(
        idx_row,
        document_profile=document_profile,
        longitudinal_provenance=longitudinal_provenance,
    )
    docs_by_filename: dict[str, Mapping[str, Any]] | None = None
    if isinstance(document_profile, Mapping) and isinstance(document_profile.get("documents"), list):
        docs_by_filename = {
            str(d["filename"]): d
            for d in document_profile["documents"]
            if isinstance(d, Mapping) and isinstance(d.get("filename"), str)
        }

    census_status = locs.get("census_status")
    if census_status not in CENSUS_STATUSES:
        errs.append(f"{prefix}.census_status is not one of {CENSUS_STATUSES} (value not shown)")
    elif census_status == "no_document" and has_posted_docs:
        errs.append(
            f"{prefix}.census_status 'no_document' is forbidden when posted document(s) are present"
        )

    primary_pass = locs.get("primary_pass")
    if not isinstance(primary_pass, list):
        errs.append(f"{prefix}.primary_pass must be a list")
        primary_pass = None

    optional_passes: dict[str, list[Any] | None] = {}
    for pass_key in ("second_pass", "ocr_recovery_pass", "adjudication_pass"):
        val = locs.get(pass_key)
        if val is not None and not isinstance(val, list):
            errs.append(f"{prefix}.{pass_key} must be a list or None")
            optional_passes[pass_key] = None
        else:
            optional_passes[pass_key] = val

    def _row_problems(where: str, row: Mapping[str, Any]) -> tuple[list[str], bool]:
        """Problems with one stored row, and whether its structure is sound."""
        row_errs: list[str] = []
        sound = True
        if type(row.get("primary_outcome_index")) is not int or row["primary_outcome_index"] < 0:
            row_errs.append(f"{where}.primary_outcome_index is invalid")
            sound = False
        if row.get("element") not in _ELEMENT_ORDER:
            row_errs.append(f"{where}.element is not one of the elements (value not shown)")
            sound = False
        status = row.get("status")
        status_ok = status in LOCATION_STATUSES
        if not status_ok:
            row_errs.append(f"{where}.status is not one of {LOCATION_STATUSES} (value not shown)")
            sound = False
        cits = row.get("citations")
        if not isinstance(cits, list):
            row_errs.append(f"{where}.citations must be a list")
            sound = False
        else:
            for c_idx, cit in enumerate(cits):
                for problem in citation_problems(
                    cit,
                    docs_by_filename=docs_by_filename,
                    pages_not_searched=row.get("pages_not_searched"),
                ):
                    row_errs.append(f"{where}.citations[{c_idx}]: {problem}")
            if status_ok:
                for problem in validate_status_citations(
                    str(status), len(cits), has_posted_documents=has_posted_docs
                ):
                    row_errs.append(f"{where} forbidden by the status-to-citation rule: {problem}")
        if "text_source" in row:
            ts_val = row["text_source"]
            if not isinstance(ts_val, str) or not ts_val.strip():
                row_errs.append(f"{where}.text_source must be a non-empty string")
                sound = False
        return row_errs, sound

    all_pass_rows_valid = True
    for pass_key, pass_rows in (
        ("primary_pass", primary_pass),
        ("second_pass", optional_passes["second_pass"]),
        ("ocr_recovery_pass", optional_passes["ocr_recovery_pass"]),
        ("adjudication_pass", optional_passes["adjudication_pass"]),
    ):
        if pass_rows is None:
            continue
        for idx, row in enumerate(pass_rows):
            if not isinstance(row, Mapping):
                errs.append(f"{prefix}.{pass_key}[{idx}] must be an object")
                all_pass_rows_valid = False
                continue
            row_errs, sound = _row_problems(f"{prefix}.{pass_key}[{idx}]", row)
            errs.extend(row_errs)
            all_pass_rows_valid = all_pass_rows_valid and sound

    effective_pass = locs.get("effective_pass")
    if not isinstance(effective_pass, list):
        errs.append(f"{prefix}.effective_pass must be a list")
    else:
        effective_sound = True
        for idx, row in enumerate(effective_pass):
            if not isinstance(row, Mapping):
                errs.append(f"{prefix}.effective_pass[{idx}] must be an object")
                effective_sound = False
                continue
            sp = row.get("source_pass")
            if sp not in EFFECTIVE_SOURCE_PASSES:
                errs.append(f"{prefix}.effective_pass[{idx}].source_pass is not one of {EFFECTIVE_SOURCE_PASSES}")
            row_errs, sound = _row_problems(f"{prefix}.effective_pass[{idx}]", row)
            errs.extend(row_errs)
            effective_sound = effective_sound and sound
        if primary_pass is not None and all_pass_rows_valid:
            expected_effective = _resolve_effective_pass(
                primary_pass,
                optional_passes["ocr_recovery_pass"],
                optional_passes["adjudication_pass"],
            )
            if effective_pass != expected_effective:
                errs.append(
                    f"{prefix}.effective_pass does not match primary -> ocr_recovery -> adjudication resolution"
                )
        if effective_sound and census_status in CENSUS_STATUSES:
            count = idx_row.get("primary_outcomes_count") if isinstance(idx_row, Mapping) else None
            recomputed = census_status_from_effective_pass(
                effective_pass, count if type(count) is int else None
            )
            if recomputed != census_status:
                errs.append(
                    f"{prefix}.census_status is {census_status!r}, but its effective_pass gives "
                    f"{recomputed!r}; a stored census_status is recomputed, never trusted"
                )
    if census_status == "no_document":
        cited_rows = _rows_with_citations({
            "primary_pass": primary_pass,
            **optional_passes,
            "effective_pass": effective_pass,
        })
        if cited_rows:
            errs.append(
                f"{prefix}.census_status 'no_document' is forbidden with citations "
                f"({cited_rows} row(s) cite a document)"
            )

    # The block must be exactly what the builder writes for its own passes:
    # normalizing the stored passes again (with the stored document profile,
    # the index row's primary_outcomes_count and the shard's document
    # evidence, as the build has them) gives the block back unchanged. A key
    # the builder never writes on a row (red-team m3 round 3: llm_assessment
    # on a primary_pass and an effective_pass row), an object in a text field,
    # or a row for an outcome the trial does not have is refused here as on
    # input. Messages name positions, never values.
    if not errs:
        count = idx_row.get("primary_outcomes_count") if isinstance(idx_row, Mapping) else None
        stored_passes: dict[str, Any] = {key: locs[key] for key in GROUPED_PASS_KEYS if key in locs}
        stored_passes["census_status"] = census_status
        try:
            again = normalize_decision_rule_locations(
                stored_passes,
                document_profile=document_profile if isinstance(document_profile, Mapping) else None,
                primary_outcomes_count=count if type(count) is int else None,
                has_posted_documents=has_posted_docs,
                expected_nct_id=nct_id,
            )
        except OutcomesLayoutError as exc:
            errs.append(f"{prefix} is not a block the builder writes: {withhold_nct_ids(str(exc))}")
        else:
            if canonicalize_nested(dict(locs)) != again:
                errs.append(
                    f"{prefix} is not what the builder writes for its passes (normalizing them again changes "
                    f"the block: a key or value the builder never writes)"
                )

    if idx_row is not None and not errs:
        expected_badge = canonicalize_nested(
            build_census_badge(
                in_audit=False,
                decision_rule_locations=locs,
                has_documents=has_posted_docs,
            )
        )
        if idx_row.get("census_badge") != expected_badge:
            errs.append(
                f"{nct_id}: the index row's census_badge does not match the one its shard's "
                f"decision_rule_locations give (values not shown)"
            )

    return errs


def _in_scope_record_problems(
    rel: str,
    nct_id: str,
    entry: Mapping[str, Any],
    idx_row: Mapping[str, Any],
    held: frozenset[str],
) -> tuple[list[str], bool]:
    """Read one in-scope trial's shard record and index row with the builder's own rules.

    Method (red-team m3 round 2: --check read none of these, so a holdout's
    URL, a nested ``in_audit`` true, a nested holdout ``nct_id``, raw page
    text or a badge the shard contradicts all checked "ok"):
    1. ``reported_results`` is read as a trial record is on input
       (``check_trial_record_identity``: trial-ID keys name this trial, every
       holdout flag is false) and each side section as a side input is
       (``check_side_input_identity``: also URLs and mapping keys), in the
       builder's order, so one value gets the exit a build gives it (skeptic
       review of m3: a document URL or nested nct_id naming the holdout was
       exit 1 in a build, from the identity rule, and exit 2 here, from the
       audit scan run first);
    2. no key or string of the record or its index row names a trial of
       ``held`` (the tree's holdouts and the manifest's) in any reading of
       Module 4's audit scan; a hit stops the reading of this record;
    3. each section is exactly what the builder writes for it: normalizing
       the stored section again (``extract_reported_results`` on the
       pre-built block, ``normalize_document_profile``,
       ``normalize_longitudinal_provenance``) gives it back unchanged, so a
       key or value the builder never writes (raw page text, an extra key)
       is refused; the decision-rule block has its own checks
       (``_validate_shard_decision_rule_locations``);
    4. the index row's ``document_badge``, ``has_reported_results`` and
       ``primary_outcomes_count`` are the ones the shard's sections give.
    Returns ``(problems, audit_leakage)``; messages name positions, not IDs.
    """
    problems: list[str] = []
    others = held - {nct_id}
    prefix = f"{rel}.data[{nct_id}]"
    leakage = False
    results = entry.get("reported_results")
    profile = entry.get("document_profile")
    provenance = entry.get("longitudinal_provenance")
    for section in ("reported_results", *INLINE_SIDE_SECTIONS):
        value = entry.get(section)
        if value is None:
            continue
        try:
            if section == "reported_results":
                check_trial_record_identity(
                    value, expected_nct_id=nct_id, expected_in_audit=False, audit_nct_ids=others,
                    label=f"{prefix}.{section}",
                )
            else:
                check_side_input_identity(
                    value, expected_nct_id=nct_id, expected_in_audit=False, label=f"{prefix}.{section}",
                    audit_nct_ids=others,
                )
        except AuditFirewallError as exc:
            leakage = True
            problems.append(withhold_nct_ids(str(exc)))
        except OutcomesLayoutError as exc:
            problems.append(str(exc))
    if problems:
        return problems, leakage
    for label, obj in ((prefix, entry), (f"{INDEX_FILE}.data[{nct_id}]", idx_row)):
        try:
            _scan_for_audit_trials(obj, audit_nct_ids=others, label=label)
        except AuditFirewallError as exc:
            return [withhold_nct_ids(str(exc))], True
        except OutcomesLayoutError as exc:
            return [str(exc)], False

    def _fixed_point(section: str, stored: Any, renormalize: Callable[[], Any]) -> bool:
        """Whether ``stored`` is what renormalizing it gives (a problem is recorded when not)."""
        nonlocal leakage
        try:
            again = renormalize()
        except AuditFirewallError as exc:
            leakage = True
            problems.append(f"{prefix}.{section}: {withhold_nct_ids(str(exc))}")
            return False
        except OutcomesLayoutError as exc:
            problems.append(f"{prefix}.{section} is not a section the builder writes: {exc}")
            return False
        if canonicalize_nested(stored) != again:
            problems.append(
                f"{prefix}.{section} is not what the builder writes for it (normalizing it again changes it: "
                f"a key or value the builder never writes)"
            )
            return False
        return True

    if not isinstance(results, Mapping):
        problems.append(f"{prefix}.reported_results must be an object for an in-scope trial")
    else:
        results_sound = _fixed_point(
            "reported_results", results, lambda: extract_reported_results({"reported_results": results})
        )
        if idx_row.get("has_reported_results") is not results.get("results_posted"):
            problems.append(
                f"{INDEX_FILE}.data[{nct_id}].has_reported_results does not match its shard's "
                f"reported_results.results_posted"
            )
        # The index row's count is the one the builder computes from the
        # shard's own outcomes (primary_outcomes_count_of), so location rows,
        # bounded by it, cannot name an outcome the shard does not register
        # (skeptic review of m3: the bound was the index row's count alone).
        stored_count = idx_row.get("primary_outcomes_count")
        if results_sound and (stored_count is None or type(stored_count) is int):
            try:
                expected_count = primary_outcomes_count_of(stored_count, results, label=f"{INDEX_FILE}.data[{nct_id}]")
            except OutcomesLayoutError as exc:
                problems.append(str(exc))
            else:
                if expected_count != stored_count:
                    problems.append(
                        f"{INDEX_FILE}.data[{nct_id}].primary_outcomes_count is not the count its shard's "
                        f"reported_results give (the builder counts the registered primary outcomes, else the "
                        f"posted primary measures)"
                    )
    if profile is not None:
        _fixed_point(
            "document_profile", profile, lambda: normalize_document_profile(profile, expected_nct_id=nct_id)
        )
    if provenance is not None:
        _fixed_point(
            "longitudinal_provenance", provenance,
            lambda: normalize_longitudinal_provenance(provenance, in_audit=False),
        )
    conflicting = _document_sha256_conflicts(profile, provenance)
    if conflicting:
        problems.append(
            f"{prefix}: document_profile and longitudinal_provenance give {len(conflicting)} posted document(s) "
            f"different sha256 values (e.g. {conflicting[0]}); the builder refuses such inputs"
        )
    if not problems and (profile is None or isinstance(profile, Mapping)):
        expected_badge = canonicalize_nested(build_document_badge(in_audit=False, document_profile=profile))
        if idx_row.get("document_badge") != expected_badge:
            problems.append(
                f"{INDEX_FILE}.data[{nct_id}].document_badge does not match its shard's document_profile "
                f"(the builder computes it from the profile)"
            )
    return problems, leakage


def validate_outcomes_tree(
    out_dir: str | os.PathLike[str],
    *,
    allow_null_commit: bool = False,
    raise_on_audit_leakage: bool = False,
    max_file_bytes: int = PART_HARD_LIMIT_BYTES,
    max_tree_bytes: int = TREE_HARD_LIMIT_BYTES,
    warn_tree_bytes: int = TREE_WARN_BYTES,
    cohort_in_audit: Mapping[str, bool] | None = None,
) -> ValidationResult:
    """Validate an on-disk outcomes tree (``index.json`` + ``shards/0..N-1.json.gz``).

    Verifies:
    - Exact header key lists with ``"data"`` last in both ``index.json`` and shards.
    - Non-empty ISO-8601 ``extracted_at`` and ``source_pipeline_commit`` stamps
      (strict by default; pass ``allow_null_commit=True`` only for explicit offline runs).
    - Exact shard inventory ``0..N-1.json.gz`` with zero missing or stray files.
    - Gzip header determinism: ``mtime == 0`` and OS byte ``0xff`` at index 9.
    - Sorted ascending ``nct_id`` order in ``index.json`` and every shard's ``data``.
    - Exact shard assignment (``outcomes_shard_of(nct_id, N) == n``) and
      1:1 bijection between ``index.json`` and all shard entries.
    - The bundle marker (``outcomes_bundle.json``) names this layout and version.
    - Re-runs the 6-vector ``assert_no_audit_leakage`` firewall on every trial
      (holdout stubs included); messages about holdout trials withhold IDs.
    - The decision-rule rules of ``_validate_shard_decision_rule_locations``,
      which are the same functions normalization uses.
    - Every in-scope record with the builder's own input rules and
      normalizers (``_in_scope_record_problems``), against the tree's holdout
      set (every trial whose index row or shard record is not exactly
      in_audit false) and the manifest's.
    - ``component_versions`` is an object of strings (or nulls), as the
      builder writes it, and the index header names no holdout trial.
    - With ``cohort_in_audit`` (the cohort manifest's trial -> in_audit, given
      to --check by --cohort-manifest): the tree holds exactly the manifest's
      trials, and every trial the manifest marks in_audit is a holdout stub
      (red-team m3 round 2: --check accepted the manifest and ignored it).
    - ``input_stamps`` is what the builder writes for the inputs' stamps
      (``normalize_input_stamps``), and each index row's
      ``primary_outcomes_count`` the one its shard's outcomes give.
    - ``<dir>.partial`` holds no backup retained by an interrupted or failed
      swap (``_retained_backups``): the tree beside one may mix two builds.
    - Computes budget telemetry and flags hard GitHub/Pages size limits without
      touching ``tests/data_budget.json``.
    """
    root = Path(out_dir)
    found_problems: list[str] = []
    warnings: list[str] = []
    audit_leakage = False

    if not root.is_dir():
        return ValidationResult(
            ok=False,
            problems=(f"{root} is not a directory",),
            warnings=(),
            total_trials=0,
            audit_holdout_trials=0,
            shards=0,
            audit_leakage_detected=False,
            budget=None,
        )

    marker_problem = _bundle_marker_problem(root)
    if marker_problem is not None:
        found_problems.append(marker_problem)

    # A swap interrupted where no rollback runs (SIGTERM, a cancelled job, a
    # second interrupt inside the rollback) leaves the previous shards or
    # index in <dir>.partial and the tree a mix of two builds (the previous
    # index over the new shards), which a build then refuses to replace
    # (check_output_target). Such a tree is not one the builder installed
    # whole, so it fails here too (skeptic review of m3: it checked "ok").
    staging = _staging_dir_for(Path(os.path.abspath(root)))
    if os.path.isdir(staging) and not os.path.islink(staging):
        retained = _retained_backups(Path(os.path.abspath(root)), staging)
        if retained:
            found_problems.append(
                f"{staging} holds {', '.join(p.name for p in retained)}, kept by an interrupted or failed swap: "
                f"{root} may hold an index and shards from two builds, so it is not a tree the builder installed "
                f"whole; put the backup back or set it aside (as the next build's refusal says) and rebuild"
            )

    index_path = root / INDEX_FILE
    if not index_path.is_file():
        return ValidationResult(
            ok=False,
            problems=(*found_problems, f"missing {INDEX_FILE} in {root}",),
            warnings=(),
            total_trials=0,
            audit_holdout_trials=0,
            shards=0,
            audit_leakage_detected=False,
            budget=None,
        )

    index_bytes = index_path.stat().st_size
    try:
        index_doc = loads_refusing_repeated_keys(index_path.read_text(encoding="utf-8"))
    except RepeatedKeyError as exc:
        # A repeated key may hide a holdout (a first-key reader sees in_audit
        # true on a record a last-key reader takes as in scope): a firewall
        # problem, as on input.
        if raise_on_audit_leakage:
            raise
        return ValidationResult(
            ok=False,
            problems=(*found_problems, f"{INDEX_FILE}: {exc}"),
            warnings=(),
            total_trials=0,
            audit_holdout_trials=0,
            shards=0,
            audit_leakage_detected=True,
            budget=None,
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return ValidationResult(
            ok=False,
            problems=(*found_problems, f"{INDEX_FILE} is not valid UTF-8 JSON ({exc})",),
            warnings=(),
            total_trials=0,
            audit_holdout_trials=0,
            shards=0,
            audit_leakage_detected=False,
            budget=None,
        )

    if not isinstance(index_doc, dict):
        return ValidationResult(
            ok=False,
            problems=(*found_problems, f"{INDEX_FILE} root must be a JSON object, got {type(index_doc).__name__}",),
            warnings=(),
            total_trials=0,
            audit_holdout_trials=0,
            shards=0,
            audit_leakage_detected=False,
            budget=None,
        )

    if tuple(index_doc.keys()) != INDEX_HEADERS:
        found_problems.append(
            f"{INDEX_FILE} top-level keys are {list(index_doc.keys())}, expected {list(INDEX_HEADERS)} with 'data' last"
        )

    extracted_at = index_doc.get("extracted_at")
    try:
        validate_iso_timestamp(extracted_at, f"{INDEX_FILE}.extracted_at")
    except OutcomesLayoutError as exc:
        found_problems.append(str(exc))

    commit = index_doc.get("source_pipeline_commit")
    try:
        validate_commit_stamp(commit, allow_null=allow_null_commit, label=f"{INDEX_FILE}.source_pipeline_commit")
    except OutcomesLayoutError as exc:
        found_problems.append(str(exc))

    if type(index_doc.get("layout_version")) is not int or index_doc.get("layout_version") != LAYOUT_VERSION:
        found_problems.append(
            f"{INDEX_FILE}.layout_version is not {LAYOUT_VERSION} (value not shown)"
        )

    layout = index_doc.get("layout")
    n_shards = 0
    if not isinstance(layout, dict):
        found_problems.append(f"{INDEX_FILE}.layout must be an object")
    else:
        raw_shards = layout.get("shards")
        try:
            n_shards = _validate_shards(raw_shards)
            if layout != layout_header(n_shards):
                found_problems.append(
                    f"{INDEX_FILE}.layout is not {layout_header(n_shards)!r} (value not shown)"
                )
        except OutcomesLayoutError as exc:
            found_problems.append(f"{INDEX_FILE}.layout: {exc}")

    # The builder writes component_versions as names to version strings (or
    # null); an object or list there would carry structured content (red-team
    # m3 round 2: a holdout's results passed --check under an extra key).
    versions = index_doc.get("component_versions")
    if not isinstance(versions, dict):
        found_problems.append(f"{INDEX_FILE}.component_versions must be an object")
    else:
        for name, value in versions.items():
            if value is not None and type(value) is not str:
                found_problems.append(
                    f"{INDEX_FILE}.component_versions[{withhold_nct_ids(repr(name))}] must be a string or null, "
                    f"got {type(value).__name__}"
                )

    # input_stamps is exactly what the builder writes for the inputs' stamps.
    stored_stamps = index_doc.get("input_stamps")
    if not isinstance(stored_stamps, dict):
        found_problems.append(f"{INDEX_FILE}.input_stamps must be an object")
    else:
        try:
            if normalize_input_stamps(stored_stamps) != stored_stamps:
                found_problems.append(
                    f"{INDEX_FILE}.input_stamps is not what the builder writes for the inputs' stamps "
                    f"(values not shown)"
                )
        except OutcomesLayoutError as exc:
            found_problems.append(f"{INDEX_FILE}.{exc}")

    # Validate index rows.
    rows = index_doc.get("data")
    index_by_nct: dict[str, dict[str, Any]] = {}
    recomputed_cohorts: dict[str, int] = {}
    recomputed_holdouts = 0
    # Trials whose index row or shard record does not say in_audit exactly
    # False (true, missing, or not a bool): every problem naming one has its
    # NCT-like tokens withheld on the way out, not only firewall problems
    # (red-team m3 round 1: an invalid cohorts list on a holdout's row was
    # reported with its ID).
    possibly_held: set[str] = set()

    if not isinstance(rows, list):
        found_problems.append(f"{INDEX_FILE}.data must be a list")
    else:
        prev_nct: str | None = None
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                found_problems.append(f"{INDEX_FILE}.data[{idx}] is not an object")
                continue
            nct_id = row.get("nct_id")
            try:
                strict_nct_id(nct_id, f"{INDEX_FILE}.data[{idx}].nct_id")
            except OutcomesLayoutError as exc:
                found_problems.append(str(exc))
                continue
            if row.get("in_audit") is not False:
                possibly_held.add(nct_id)
            if prev_nct is not None and nct_id <= prev_nct:
                found_problems.append(
                    f"{INDEX_FILE}.data is not strictly sorted ascending by nct_id ({prev_nct} before {nct_id})"
                )
            prev_nct = nct_id
            if nct_id in index_by_nct:
                found_problems.append(f"{INDEX_FILE}.data lists duplicate nct_id {nct_id}")
                continue
            if set(row.keys()) != set(INDEX_ROW_KEYS):
                missing = sorted(set(INDEX_ROW_KEYS) - set(row.keys()))
                extra = sorted(set(row.keys()) - set(INDEX_ROW_KEYS))
                found_problems.append(
                    f"{INDEX_FILE}.data[{nct_id}] key mismatch (missing={missing}, extra={extra})"
                )
            # Scalar fields hold one scalar each (the builder writes nothing
            # else): an object there would carry structured content, such as
            # a holdout's results, past the stub. The value is never quoted.
            for field in _INDEX_ROW_TEXT_FIELDS:
                if field in row and row[field] is not None and type(row[field]) is not str:
                    found_problems.append(
                        f"{INDEX_FILE}.data[{nct_id}].{field} must be a string or null, got "
                        f"{type(row[field]).__name__}"
                    )
            for field in _INDEX_ROW_INT_FIELDS:
                if field in row and row[field] is not None and type(row[field]) is not int:
                    found_problems.append(
                        f"{INDEX_FILE}.data[{nct_id}].{field} must be an int or null, got "
                        f"{type(row[field]).__name__}"
                    )
            for field in _INDEX_ROW_BOOL_FIELDS:
                if field in row and type(row[field]) is not bool:
                    found_problems.append(
                        f"{INDEX_FILE}.data[{nct_id}].{field} must be a bool, got {type(row[field]).__name__}"
                    )
            for field in _INDEX_ROW_TRISTATE_FIELDS:
                if field in row and row[field] is not None and type(row[field]) is not bool:
                    found_problems.append(
                        f"{INDEX_FILE}.data[{nct_id}].{field} must be a bool or null, got "
                        f"{type(row[field]).__name__}"
                    )
            try:
                assert_no_audit_leakage(
                    nct_id=nct_id,
                    in_audit=row.get("in_audit"),
                    index_row=row,
                )
            except AuditFirewallError as exc:
                audit_leakage = True
                if raise_on_audit_leakage:
                    raise
                found_problems.append(withhold_nct_ids(str(exc)))
            except OutcomesLayoutError as exc:
                found_problems.append(str(exc))

            if row.get("in_audit") is True:
                recomputed_holdouts += 1
            cohorts = row.get("cohorts")
            if (
                not isinstance(cohorts, list)
                or not cohorts
                or cohorts != sorted(set(cohorts))
                or not all(isinstance(c, str) and COHORT_RE.fullmatch(c) for c in cohorts)
            ):
                found_problems.append(
                    f"{INDEX_FILE}.data[{nct_id}].cohorts is invalid: it must be a non-empty sorted list of unique "
                    f"slugs matching {COHORT_RE.pattern} (got {type(cohorts).__name__}; value not shown)"
                )
            else:
                for c in cohorts:
                    recomputed_cohorts[c] = recomputed_cohorts.get(c, 0) + 1

            doc_badge = row.get("document_badge")
            if not isinstance(doc_badge, dict) or doc_badge.get("readability_status") not in READABILITY_BADGE_STATUSES:
                found_problems.append(f"{INDEX_FILE}.data[{nct_id}].document_badge is invalid")
            census_badge = row.get("census_badge")
            if not isinstance(census_badge, dict) or census_badge.get("census_status") not in CENSUS_BADGE_STATUSES:
                found_problems.append(f"{INDEX_FILE}.data[{nct_id}].census_badge is invalid")

            index_by_nct[nct_id] = row

        if index_doc.get("total_trials") != len(rows):
            found_problems.append(
                f"{INDEX_FILE}.total_trials is not {len(rows)}, the number of rows in data (value not shown)"
            )
        if index_doc.get("audit_holdout_trials") != recomputed_holdouts:
            found_problems.append(
                f"{INDEX_FILE}.audit_holdout_trials is not {recomputed_holdouts}, the number of holdout rows "
                f"(value not shown)"
            )
        expected_cohort_counts = {k: recomputed_cohorts[k] for k in sorted(recomputed_cohorts)}
        if index_doc.get("cohort_counts") != expected_cohort_counts:
            found_problems.append(
                f"{INDEX_FILE}.cohort_counts is not {expected_cohort_counts!r}, the counts of its rows' cohorts "
                f"(value not shown)"
            )

    # Validate shards directory and every shard file.
    shards_dir = root / SHARDS_DIR
    shard_sizes: list[int] = []
    max_file_size = index_bytes
    max_file_rel = INDEX_FILE
    non_empty_shards = 0
    empty_shards = 0
    seen_in_shards: dict[str, int] = {}
    shard_in_audit: dict[str, Any] = {}
    # In-scope records, read with the builder's rules once the tree's whole
    # holdout set is known (after every shard has been read).
    in_scope_records: list[tuple[str, str, dict[str, Any], Mapping[str, Any]]] = []

    if n_shards == 0:
        found_problems.append("cannot verify shards because layout.shards is invalid")
    elif not shards_dir.is_dir():
        found_problems.append(f"missing {SHARDS_DIR}/ directory in {root}")
    else:
        present_entries = sorted(os.listdir(shards_dir))
        expected_names = [f"{n}.json.gz" for n in range(n_shards)]
        expected_set = set(expected_names)
        present_set = set(present_entries)
        missing_shards = [name for name in expected_names if name not in present_set]
        stray_files = [name for name in present_entries if name not in expected_set]
        if missing_shards:
            found_problems.append(
                f"{SHARDS_DIR}/ lacks {len(missing_shards)} shard file(s) (e.g. {', '.join(missing_shards[:5])})"
            )
        if stray_files:
            found_problems.append(
                f"{SHARDS_DIR}/ contains unexpected file(s): {', '.join(stray_files[:5])}"
            )

        for n in range(n_shards):
            rel = shard_relpath(n)
            spath = root / rel
            if not spath.is_file():
                continue
            raw_bytes = spath.read_bytes()
            sz = len(raw_bytes)
            shard_sizes.append(sz)
            if sz > max_file_size:
                max_file_size = sz
                max_file_rel = rel
            if sz > max_file_bytes:
                found_problems.append(
                    f"{rel} is {sz:,} bytes, exceeding hard per-file limit {max_file_bytes:,}"
                )

            try:
                mtime, os_byte = inspect_gzip_header(raw_bytes)
                if mtime != 0:
                    found_problems.append(f"{rel} gzip header mtime is {mtime}, expected 0")
                if os_byte != 0xFF:
                    found_problems.append(f"{rel} gzip header OS byte is 0x{os_byte:02x}, expected 0xff")
                decompressed = gzip.decompress(raw_bytes).decode("utf-8")
                sdoc = loads_refusing_repeated_keys(decompressed)
            except RepeatedKeyError as exc:
                audit_leakage = True
                if raise_on_audit_leakage:
                    raise
                found_problems.append(f"{rel}: {exc}")
                continue
            except (OutcomesLayoutError, OSError, EOFError, UnicodeDecodeError, ValueError) as exc:
                found_problems.append(f"{rel} is not readable gzipped UTF-8 JSON ({exc})")
                continue

            if not isinstance(sdoc, dict):
                found_problems.append(f"{rel} root must be a JSON object")
                continue
            if tuple(sdoc.keys()) != SHARD_HEADERS:
                found_problems.append(
                    f"{rel} top-level keys are {list(sdoc.keys())}, expected {list(SHARD_HEADERS)} with 'data' last"
                )
            if sdoc.get("extracted_at") != extracted_at:
                found_problems.append(
                    f"{rel}.extracted_at differs from {INDEX_FILE}'s (values not shown)"
                )
            if sdoc.get("source_pipeline_commit") != commit:
                found_problems.append(
                    f"{rel}.source_pipeline_commit differs from {INDEX_FILE}'s (values not shown)"
                )
            if sdoc.get("layout_version") != LAYOUT_VERSION:
                found_problems.append(f"{rel}.layout_version is not {LAYOUT_VERSION} (value not shown)")
            if sdoc.get("class") != SHARD_CLASS:
                found_problems.append(f"{rel}.class is not {SHARD_CLASS!r} (value not shown)")
            if type(sdoc.get("shard")) is not int or sdoc.get("shard") != n:
                found_problems.append(f"{rel}.shard is not {n} (value not shown)")
            if type(sdoc.get("shards")) is not int or sdoc.get("shards") != n_shards:
                found_problems.append(f"{rel}.shards is not {n_shards} (value not shown)")
            if sdoc.get("key") != SHARD_KEY:
                found_problems.append(f"{rel}.key is not {SHARD_KEY!r} (value not shown)")

            sdata = sdoc.get("data")
            if not isinstance(sdata, dict):
                found_problems.append(f"{rel}.data must be a JSON object keyed by nct_id")
                continue

            if len(sdata) == 0:
                empty_shards += 1
            else:
                non_empty_shards += 1

            keys = list(sdata.keys())
            if keys != sorted(keys):
                found_problems.append(f"{rel}.data keys are not sorted ascending by nct_id")

            for nct_id, entry in sdata.items():
                try:
                    actual_shard = outcomes_shard_of(nct_id, n_shards)
                except OutcomesLayoutError as exc:
                    found_problems.append(f"{rel}: {exc}")
                    continue
                if actual_shard != n:
                    found_problems.append(
                        f"{rel} holds {nct_id}, which belongs in shard {actual_shard}"
                    )
                if nct_id in seen_in_shards:
                    found_problems.append(
                        f"{nct_id} appears in both shard {seen_in_shards[nct_id]} and shard {n}"
                    )
                else:
                    seen_in_shards[nct_id] = n

                if not isinstance(entry, dict):
                    possibly_held.add(nct_id)
                    found_problems.append(f"{rel}.data[{nct_id}] is not an object")
                    continue
                if entry.get("in_audit") is not False:
                    possibly_held.add(nct_id)
                shard_in_audit[nct_id] = entry.get("in_audit")
                if entry.get("nct_id") != nct_id:
                    found_problems.append(f"{rel}.data[{nct_id}].nct_id is not the key it is filed under (value not shown)")
                if entry.get("shard") != n:
                    found_problems.append(f"{rel}.data[{nct_id}].shard is not {n} (value not shown)")
                if set(entry.keys()) != set(SHARD_RECORD_KEYS):
                    missing = sorted(set(SHARD_RECORD_KEYS) - set(entry.keys()))
                    extra = sorted(set(entry.keys()) - set(SHARD_RECORD_KEYS))
                    found_problems.append(
                        f"{rel}.data[{nct_id}] key mismatch (missing={missing}, extra={extra})"
                    )

                idx_row = index_by_nct.get(nct_id)
                try:
                    assert_no_audit_leakage(
                        nct_id=nct_id,
                        in_audit=entry.get("in_audit"),
                        shard_record=entry,
                        index_row=idx_row,
                    )
                except AuditFirewallError as exc:
                    audit_leakage = True
                    if raise_on_audit_leakage:
                        raise
                    found_problems.append(withhold_nct_ids(str(exc)))
                except OutcomesLayoutError as exc:
                    found_problems.append(str(exc))

                if idx_row is not None:
                    if entry.get("in_audit") != idx_row.get("in_audit"):
                        found_problems.append(
                            f"{nct_id}: the shard record's in_audit differs from the index row's (values not shown)"
                        )
                    if entry.get("cohorts") != idx_row.get("cohorts"):
                        found_problems.append(
                            f"{nct_id}: the shard record's cohorts differ from the index row's (values not shown)"
                        )

                if entry.get("in_audit") is False and idx_row is not None and idx_row.get("in_audit") is False:
                    in_scope_records.append((rel, nct_id, entry, idx_row))

                if entry.get("in_audit") is False and entry.get("decision_rule_locations") is not None:
                    found_problems.extend(
                        _validate_shard_decision_rule_locations(
                            rel,
                            nct_id,
                            entry["decision_rule_locations"],
                            idx_row,
                            document_profile=entry.get("document_profile"),
                            longitudinal_provenance=entry.get("longitudinal_provenance"),
                        )
                    )
                elif entry.get("in_audit") is False and idx_row is not None:
                    has_posted_docs = _shard_has_posted_documents(
                        idx_row,
                        document_profile=entry.get("document_profile"),
                        longitudinal_provenance=entry.get("longitudinal_provenance"),
                    )
                    expected_badge = canonicalize_nested(
                        build_census_badge(
                            in_audit=False,
                            decision_rule_locations=None,
                            has_documents=has_posted_docs,
                        )
                    )
                    if idx_row.get("census_badge") != expected_badge:
                        found_problems.append(
                            f"{nct_id}: the index row's census_badge does not match the one its shard's "
                            f"document presence gives (values not shown)"
                        )

        # Index-to-shard bijection check.
        index_nct_set = set(index_by_nct)
        shard_nct_set = set(seen_in_shards)
        missing_from_shards = sorted(index_nct_set - shard_nct_set)
        orphan_in_shards = sorted(shard_nct_set - index_nct_set)
        if missing_from_shards:
            found_problems.append(
                f"{len(missing_from_shards)} index trial(s) missing from shards "
                f"(e.g. {', '.join(missing_from_shards[:5])})"
            )
        if orphan_in_shards:
            found_problems.append(
                f"{len(orphan_in_shards)} shard trial(s) missing from {INDEX_FILE} "
                f"(e.g. {', '.join(orphan_in_shards[:5])})"
            )

    # The tree's holdout set (index or shard not exactly in_audit false) and
    # the manifest's: every in-scope record and the index header are read
    # against it, and every problem naming one has its IDs withheld.
    manifest_held = frozenset(
        nct for nct, flag in (cohort_in_audit or {}).items() if flag is not False
    )
    held = frozenset(possibly_held) | manifest_held

    def _record_leak(exc: AuditFirewallError) -> None:
        nonlocal audit_leakage
        audit_leakage = True
        if raise_on_audit_leakage:
            raise exc
        found_problems.append(withhold_nct_ids(str(exc)))

    try:
        _scan_for_audit_trials(
            {key: value for key, value in index_doc.items() if key != "data"},
            audit_nct_ids=held,
            label=f"{INDEX_FILE} header",
        )
    except AuditFirewallError as exc:
        _record_leak(exc)
    for rel, nct_id, entry, idx_row in in_scope_records:
        record_problems, record_leak = _in_scope_record_problems(rel, nct_id, entry, idx_row, held)
        if record_leak:
            _record_leak(AuditFirewallError("; ".join(record_problems)))
        else:
            found_problems.extend(record_problems)

    if cohort_in_audit is not None:
        tree_ids = set(index_by_nct) | set(seen_in_shards)
        manifest_ids = set(cohort_in_audit)
        absent = manifest_ids - tree_ids
        unlisted = tree_ids - manifest_ids
        if absent or unlisted:
            found_problems.append(
                f"the tree does not hold exactly the cohort manifest's trials: {len(absent)} manifest trial(s) "
                f"absent from it, {len(unlisted)} trial(s) in it the manifest does not list (IDs withheld)"
            )
        not_stubs = sum(
            1
            for nct in manifest_held & tree_ids
            if (index_by_nct.get(nct) or {}).get("in_audit") is not True or shard_in_audit.get(nct) is not True
        )
        if not_stubs:
            _record_leak(
                AuditFirewallError(
                    f"in_audit holdout leak — {not_stubs} trial(s) the cohort manifest marks in_audit are not "
                    f"holdout stubs in this tree (IDs withheld)"
                )
            )
        masked = sum(
            1
            for nct, flag in cohort_in_audit.items()
            if flag is False and nct in tree_ids and (index_by_nct.get(nct) or {}).get("in_audit") is not False
        )
        if masked:
            found_problems.append(
                f"{masked} trial(s) the cohort manifest marks in scope are not in scope in this tree (IDs withheld)"
            )

    if index_bytes > max_file_bytes:
        found_problems.append(
            f"{INDEX_FILE} is {index_bytes:,} bytes, exceeding hard per-file limit {max_file_bytes:,}"
        )

    total_shard_bytes = sum(shard_sizes)
    total_bytes = index_bytes + total_shard_bytes
    if total_bytes > max_tree_bytes:
        found_problems.append(
            f"outcomes tree is {total_bytes:,} bytes, exceeding hard tree limit {max_tree_bytes:,}"
        )
    elif total_bytes > warn_tree_bytes:
        warnings.append(
            f"outcomes tree is {total_bytes:,} bytes, exceeding warning threshold {warn_tree_bytes:,}"
        )

    if shard_sizes:
        ordered_sizes = sorted(shard_sizes)
        budget: BudgetTelemetry | None = BudgetTelemetry(
            index_bytes=index_bytes,
            total_shard_bytes=total_shard_bytes,
            total_bytes=total_bytes,
            max_file_bytes=max_file_size,
            max_file_path=max_file_rel,
            min_shard_bytes=ordered_sizes[0],
            median_shard_bytes=ordered_sizes[len(ordered_sizes) // 2],
            max_shard_bytes=ordered_sizes[-1],
            non_empty_shards=non_empty_shards,
            empty_shards=empty_shards,
        )
    else:
        budget = None

    return ValidationResult(
        ok=len(found_problems) == 0,
        problems=tuple(withhold_if_names(problem, held) for problem in found_problems),
        warnings=tuple(warnings),
        total_trials=len(index_by_nct),
        audit_holdout_trials=recomputed_holdouts,
        shards=n_shards,
        audit_leakage_detected=audit_leakage,
        budget=budget,
    )


def problems(out_dir: str | os.PathLike[str], *, allow_null_commit: bool = False) -> list[str]:
    """Return every problem found in an outcomes directory ([] when valid)."""
    return list(validate_outcomes_tree(out_dir, allow_null_commit=allow_null_commit).problems)


# ── output allowlist (where a bundle, its staging dir or a report may go) ─────

def _marker_text(marker: Mapping[str, Any]) -> str:
    """The exact bytes (as text) a marker file holds: compact JSON plus newline."""
    return encode_json_text(dict(marker), trailing_newline=True)


def _holds_marker(directory: Path, name: str, marker: Mapping[str, Any]) -> bool:
    """True when ``directory/name`` is a regular file (not a symlink) holding
    exactly ``_marker_text(marker)``."""
    path = directory / name
    if os.path.islink(path) or not path.is_file():
        return False
    try:
        return path.read_text(encoding="utf-8") == _marker_text(marker)
    except (OSError, UnicodeDecodeError):
        return False


def is_bundle_dir(directory: Path) -> bool:
    """True when ``directory`` holds this layout's bundle marker."""
    return _holds_marker(directory, BUNDLE_MARKER_FILE, BUNDLE_MARKER)


def _bundle_marker_problem(root: Path) -> str | None:
    """A problem string when ``root`` lacks this layout's bundle marker, else None."""
    if is_bundle_dir(root):
        return None
    return (
        f"{root} lacks the bundle marker {BUNDLE_MARKER_FILE} holding {BUNDLE_MARKER!r} "
        f"(as a regular file); only a tree written by write_outcomes_tree is an outcomes bundle"
    )


def absolute_output_path(path: str | os.PathLike[str]) -> Path:
    """Return ``path`` made absolute against the working directory, refusing ``..``.

    The path is deliberately not resolved: resolving would hide the symlinks
    the guards below refuse. A ``..`` component is refused because collapsing
    it lexically gives the wrong directory when the component before it is a
    symlink, and keeping it would leave the ancestor checks reading the wrong
    chain.
    """
    raw = Path(os.fspath(path))
    if any(part == ".." for part in raw.parts):
        raise OutcomesLayoutError(f"refusing output path {raw}: it contains '..'")
    return raw if raw.is_absolute() else Path.cwd() / raw


def _foreign_entries(directory: Path) -> list[str]:
    """Names in ``directory`` that mark another data family's directory.

    The names of ``FOREIGN_DIRECTORY_MARKERS`` mark the site's dataset folder
    and each of its families (outcomes, results structure, census, devices,
    geography and its frozen run folders, sponsors, sex/gender and their audit
    folders); an ``index.json`` in a directory without this layout's bundle
    marker is someone else's index. Entries are tested with ``lexists`` so a
    dangling symlink counts too.
    """
    found = [name for name in FOREIGN_DIRECTORY_MARKERS if os.path.lexists(directory / name)]
    if os.path.lexists(directory / INDEX_FILE) and not is_bundle_dir(directory):
        found.append(f"{INDEX_FILE} (not an outcomes bundle index)")
    return found


def _family_folder(path: Path) -> Path | None:
    """The first of ``path`` and its ancestors that is a site data family folder
    by position (a ``SITE_DATA_FAMILIES`` name directly under a folder named
    ``data``), or ``None``."""
    for candidate in (path, *path.parents):
        if candidate.name in SITE_DATA_FAMILIES and candidate.parent.name == "data":
            return candidate
    return None


def _check_ancestors(path: Path, *, what: str) -> None:
    """Refuse when ``path`` sits in a site data family folder by position, or
    any ancestor of ``path`` is a symlink, marks another data family, is
    itself an outcomes bundle, or is a bundle's staging directory."""
    family = _family_folder(path)
    if family is not None:
        raise OutcomesLayoutError(
            f"refusing to write {what} {path}: it is inside {family}, a site data family folder "
            f"({family.name!r} under 'data'); point it at a dedicated directory outside any dataset folder"
        )
    for ancestor in path.parents:
        if os.path.islink(ancestor):
            raise OutcomesLayoutError(
                f"refusing to write {what} {path}: its ancestor {ancestor} is a symlink "
                f"(pass a path without symlinked components)"
            )
        if not ancestor.is_dir():
            continue
        foreign = _foreign_entries(ancestor)
        if foreign:
            raise OutcomesLayoutError(
                f"refusing to write {what} {path}: its ancestor {ancestor} is a foreign data "
                f"directory (found {', '.join(foreign)}); point it at a dedicated directory outside "
                f"any dataset folder"
            )
        if is_bundle_dir(ancestor):
            raise OutcomesLayoutError(
                f"refusing to write {what} {path}: its ancestor {ancestor} is an outcomes bundle"
            )
        if os.path.lexists(ancestor / STAGING_MARKER_FILE):
            raise OutcomesLayoutError(
                f"refusing to write {what} {path}: its ancestor {ancestor} is an outcomes bundle's staging "
                f"directory (it holds {STAGING_MARKER_FILE})"
            )


def _staging_dir_for(target: Path) -> Path:
    """``<target>.partial``, the staging directory beside the target."""
    return target.with_name(target.name + ".partial")


def _layout_shard_names(target: Path) -> frozenset[str] | None:
    """The shard file names the bundle at ``target`` names, from its own ``index.json``.

    Method: the index is read as the builder writes it
    (``loads_refusing_repeated_keys``), and its ``layout.shards`` must be a
    valid shard count N; the names are then exactly ``0.json.gz`` ..
    ``{N-1}.json.gz`` in ASCII digits. ``None`` when the index is absent,
    unreadable, or states no valid count.
    """
    index = target / INDEX_FILE
    try:
        if os.path.islink(index) or not index.is_file():
            return None
        doc = loads_refusing_repeated_keys(index.read_text(encoding="utf-8"))
        layout = doc.get("layout") if isinstance(doc, dict) else None
        count = _validate_shards(layout.get("shards") if isinstance(layout, dict) else None)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return frozenset(f"{n}.json.gz" for n in range(count))


def _check_bundle_entries(target: Path) -> None:
    """Refuse an existing bundle holding anything this module does not write
    where the swap would move or replace it.

    Method: the swap replaces ``index.json`` and moves ``shards/`` into the
    staging directory, which is then deleted. ``index.json`` must therefore be
    a regular file, and ``shards/`` a real directory holding only regular
    files the bundle's own layout names (``_layout_shard_names``: ``<n>.json.gz``
    in ASCII digits for n below the shard count its index states): anything
    else in it (a directory of operator notes, a symlink, a stray file; red-team
    m3 round 1; a name with another script's digits or an index beyond the
    count, round 3) was not written here and would be deleted with the old
    shards without a word, so the write is refused and the entry kept. A
    non-empty ``shards/`` beside an index that states no readable shard count
    is refused too. Sibling entries of the target are never touched and are
    not checked.
    """
    index = target / INDEX_FILE
    if os.path.lexists(index) and (os.path.islink(index) or not index.is_file()):
        raise OutcomesLayoutError(f"refusing to replace {index}: it is not a regular file")
    shards = target / SHARDS_DIR
    if not os.path.lexists(shards):
        return
    if os.path.islink(shards) or not shards.is_dir():
        raise OutcomesLayoutError(f"refusing to replace {shards}: it is not a directory this module wrote")
    with os.scandir(shards) as listing:
        entries = list(listing)
    named = _layout_shard_names(target)
    if named is None and entries:
        raise OutcomesLayoutError(
            f"refusing to replace {shards}: the bundle's {INDEX_FILE} does not state a shard count this module "
            f"can read, so which files in it are the bundle's shards cannot be told; they are deleted after the "
            f"swap, so nothing was changed"
        )
    foreign = sorted(
        entry.name
        for entry in entries
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False) or entry.name not in (named or ())
    )
    if foreign:
        raise OutcomesLayoutError(
            f"refusing to replace {shards}: it holds {len(foreign)} entr{'y' if len(foreign) == 1 else 'ies'} "
            f"this module never writes ({', '.join(withhold_nct_ids(name) for name in foreign[:5])}); the old "
            f"shards are deleted after the swap, so move them out first (nothing was changed)"
        )


def _tree_digest(root: Path) -> str | None:
    """sha256 over a bundle's ``index.json`` and every file of its ``shards/``, or None.

    Method: the index's bytes and each shard file's name and bytes, in name
    order, feed one sha256. ``None`` when the index is not a regular file,
    ``shards/`` is not a real directory, or it holds anything but regular
    files: such a tree is not one this module installed whole.
    """
    index = root / INDEX_FILE
    shards = root / SHARDS_DIR
    try:
        if os.path.islink(index) or not index.is_file() or os.path.islink(shards) or not shards.is_dir():
            return None
        digest = hashlib.sha256()
        digest.update(b"index\0" + hashlib.sha256(index.read_bytes()).digest())
        with os.scandir(shards) as listing:
            entries = sorted(listing, key=lambda entry: entry.name)
        for entry in entries:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                return None
            digest.update(
                b"shard\0" + entry.name.encode("utf-8") + b"\0" + hashlib.sha256(Path(entry.path).read_bytes()).digest()
            )
        return digest.hexdigest()
    except OSError:
        return None


def _swap_completed(target: Path, work: Path) -> bool:
    """True when ``work`` records a completed swap and ``target`` still holds exactly that bundle.

    Method: ``SWAP_COMPLETE_FILE`` is a regular file holding
    ``{"installed": <_tree_digest of the bundle the swap moved in>}``, and the
    target's own ``_tree_digest`` is that value. Then the target is the whole
    new bundle, and the previous version's shards and index left in ``work``
    are superseded leftovers, not a backup the target needs.
    """
    marker = work / SWAP_COMPLETE_FILE
    try:
        if os.path.islink(marker) or not marker.is_file():
            return False
        recorded = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    if not isinstance(recorded, dict) or set(recorded) != {"installed"} or not isinstance(recorded["installed"], str):
        return False
    return recorded["installed"] == _tree_digest(target)


def _retained_backups(target: Path, work: Path) -> list[Path]:
    """Backups in ``work`` left by an interrupted or failed swap that the target may still need.

    Nothing counts when the swap completed and the target still holds the
    bundle it installed (``_swap_completed``; red-team m3 round 2: an
    interrupted cleanup after a completed swap was reported as a failed
    restore, with an instruction to move the old version back over the new
    one). Otherwise ``_previous_shards`` always counts, and
    ``_previous_index.json`` counts unless the target's ``index.json`` holds
    the same bytes (then it is a leftover link or copy of the current index
    and nothing is lost by removing it). The completion record is read only
    when something would count (it digests the whole target).
    """
    found: list[Path] = []
    backup_shards = work / PREVIOUS_SHARDS_DIR
    if os.path.lexists(backup_shards):
        found.append(backup_shards)
    backup_index = work / PREVIOUS_INDEX_FILE
    if os.path.lexists(backup_index):
        live_index = target / INDEX_FILE
        same = False
        try:
            same = (
                not os.path.islink(backup_index)
                and backup_index.is_file()
                and live_index.is_file()
                and not os.path.islink(live_index)
                and backup_index.read_bytes() == live_index.read_bytes()
            )
        except OSError:
            same = False
        if not same:
            found.append(backup_index)
    if found and _swap_completed(target, work):
        return []
    return found


def check_output_target(out_dir: str | os.PathLike[str]) -> Path:
    """Refuse every output location this module may not write; return the
    absolute target when it may.

    Allowed: a target that is absent, an empty directory, or a bundle this
    module wrote (``is_bundle_dir``). Refused, before anything is created or
    deleted:
    - a target that is a symlink, a non-directory, a directory holding
      another family's markers (``_foreign_entries``), a bundle's staging
      directory (it holds ``STAGING_MARKER_FILE``), or a non-empty directory
      without the bundle marker; a bundle whose ``index.json``, ``shards`` or
      marker is a symlink, or whose ``shards/`` holds anything but the regular
      ``<n>.json.gz`` files its own layout names (``_check_bundle_entries``);
    - a target inside a site data family folder by position
      (``_family_folder``), and an ancestor that is a symlink, marks another
      family, or is a bundle;
    - a ``<target>.partial`` that is a symlink or not a directory, that holds
      a retained ``_previous_shards`` or ``_previous_index.json`` backup
      (``_retained_backups``, named in the refusal; L3), or that is non-empty
      without this module's staging marker. Only an empty ``.partial`` or one
      holding the staging marker is this module's to remove.
    """
    target = absolute_output_path(out_dir)
    _check_ancestors(target, what="outcomes bundle")

    if os.path.lexists(target):
        if os.path.islink(target):
            raise OutcomesLayoutError(f"refusing to write through symlinked output path {target}")
        if not target.is_dir():
            raise OutcomesLayoutError(f"refusing to overwrite non-directory output path {target}")
        if os.path.lexists(target / STAGING_MARKER_FILE):
            # A killed build's <out>.partial holds both markers and passed as
            # a bundle; a bundle built into it was deleted whole by the next
            # build of <out> as that build's own leftover (red-team m3 round 3).
            raise OutcomesLayoutError(
                f"refusing to write into {target}: it holds {STAGING_MARKER_FILE}, so it is another bundle's "
                f"staging directory (left by an interrupted build), not a bundle; the next build of that bundle "
                f"deletes it whole"
            )
        foreign = _foreign_entries(target)
        if any(not f.startswith(INDEX_FILE) for f in foreign):
            raise OutcomesLayoutError(
                f"refusing to overwrite foreign data directory {target}: found marker(s) "
                f"{', '.join(foreign)} (point --out-dir at a dedicated {DEFAULT_OUT_DIR_NAME!r} directory)"
            )
        if foreign:
            raise OutcomesLayoutError(
                f"refusing to overwrite foreign directory {target}: existing {INDEX_FILE} is not an "
                f"{DEFAULT_OUT_DIR_NAME} index (no {BUNDLE_MARKER_FILE} marker)"
            )
        if any(target.iterdir()) and not is_bundle_dir(target):
            raise OutcomesLayoutError(
                f"refusing to write into non-empty directory {target}: it holds no "
                f"{BUNDLE_MARKER_FILE} marker, so this module did not write it"
            )
        for managed in (INDEX_FILE, SHARDS_DIR, BUNDLE_MARKER_FILE):
            if os.path.islink(target / managed):
                raise OutcomesLayoutError(f"refusing to replace symlinked bundle entry {target / managed}")
        _check_bundle_entries(target)

    work = _staging_dir_for(target)
    if os.path.lexists(work):
        if os.path.islink(work):
            raise OutcomesLayoutError(f"refusing to use symlinked staging directory {work}")
        if not work.is_dir():
            raise OutcomesLayoutError(f"refusing to replace non-directory staging path {work}")
        retained = _retained_backups(target, work)
        if retained:
            raise RetainedBackupError(
                f"refusing to run: {', '.join(str(p) for p in retained)} hold(s) the previous bundle's "
                f"shards or index, retained after an earlier failed or interrupted restore. Move them back "
                f"into {target} ({SHARDS_DIR}/ and {INDEX_FILE}) or set them aside, and delete {work} by "
                f"hand; nothing was deleted"
            )
        if any(work.iterdir()) and not _holds_marker(work, STAGING_MARKER_FILE, STAGING_MARKER):
            raise OutcomesLayoutError(
                f"refusing to delete {work}: it is not empty and holds no {STAGING_MARKER_FILE} "
                f"marker, so this module did not create it"
            )
    return target


def check_report_path(
    report: str | os.PathLike[str],
    *,
    out_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Refuse a ``--report`` location this module may not write; return it absolute.

    The same allowlist as the bundle: no symlinked report path or ancestor, no
    ancestor that marks another data family or is a bundle; the report may not
    sit inside the bundle or its staging directory (it would be swapped away
    or flagged as a stray), may not be named like a data family's file
    (``run.json``, ``summary.json``, ``records.json``, ``index.json``), and may
    replace an existing file only when that file is a report this module
    wrote (``report_of`` names it).
    """
    path = absolute_output_path(report)
    if path.name in (*FOREIGN_DIRECTORY_MARKERS, INDEX_FILE, BUNDLE_MARKER_FILE, STAGING_MARKER_FILE):
        raise OutcomesLayoutError(f"refusing report path {path}: its name is a data family's file name")
    if out_dir is not None:
        target = absolute_output_path(out_dir)
        for inside in (target, _staging_dir_for(target)):
            if inside == path or inside in path.parents:
                raise OutcomesLayoutError(f"refusing report path {path}: it is inside {inside}")
    _check_ancestors(path, what="report")
    if os.path.lexists(path):
        if os.path.islink(path) or not path.is_file():
            raise OutcomesLayoutError(f"refusing report path {path}: it exists and is not a regular file")
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            existing = None
        if not isinstance(existing, dict) or existing.get("report_of") != REPORT_OF:
            raise OutcomesLayoutError(
                f"refusing to overwrite {path}: it is not a report this builder wrote "
                f"(no report_of={REPORT_OF!r})"
            )
    return path


# Stamped into every report so an existing report can be told from any other file.
REPORT_OF = "scripts/build_outcomes_shards.py"


def write_report(
    path: str | os.PathLike[str],
    payload: Mapping[str, Any],
    *,
    out_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Write a JSON report after ``check_report_path`` allows it.

    The write itself never follows a symlink (``_write_report_file``):
    red-team m3 round 3 turned the report path, or its parent directory, into
    a symlink into a data family right after the check, and ``write_text``
    followed it (a geography run pointer was overwritten with the report).
    """
    report_path = check_report_path(path, out_dir=out_dir)
    body = {"report_of": REPORT_OF, **dict(payload)}
    _write_report_file(report_path, (json.dumps(body, indent=2) + "\n").encode("utf-8"))
    return report_path


def _report_directory_problem(dir_fd: int) -> str | None:
    """Why the directory open at ``dir_fd`` may not hold a report, or None.

    The names ``check_report_path`` refuses in an ancestor, looked up in the
    directory itself (``lstat`` relative to the descriptor, which needs only
    search permission, as ``os.path.lexists`` does): another family's marker
    files, an ``index.json``, a bundle marker or a staging marker (by name
    only: a directory holding any of them is, or may be, a data family, a
    bundle or its staging directory). A name that cannot be looked up counts
    as absent, as for ``os.path.lexists``.
    """
    found: list[str] = []
    for name in (*FOREIGN_DIRECTORY_MARKERS, INDEX_FILE, BUNDLE_MARKER_FILE, STAGING_MARKER_FILE):
        try:
            os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            continue
        found.append(name)
    return f"it holds {', '.join(found)}" if found else None


def _check_existing_report(data: bytes, path: Path) -> None:
    """Refuse to replace ``path`` unless ``data`` (its bytes) is a report this module wrote."""
    try:
        existing = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        existing = None
    if not isinstance(existing, dict) or existing.get("report_of") != REPORT_OF:
        raise OutcomesLayoutError(
            f"refusing to overwrite {path}: it is not a report this builder wrote (no report_of={REPORT_OF!r})"
        )


def _write_report_file(path: Path, data: bytes) -> None:
    """Write ``data`` to the absolute ``path``, never through a symlink.

    Method, where ``_FD_SAFE``: the report's directory is reached from the
    filesystem root one component at a time, each opened with
    ``O_NOFOLLOW | O_DIRECTORY`` relative to the previous one (a missing one
    is created first, as ``mkdir -p`` would), so a component swapped for a
    symlink after ``check_report_path`` is refused rather than followed; each
    directory is looked up again for the names ``check_report_path`` refuses
    (``_report_directory_problem``). In the report's directory, an
    existing entry under the report's name must be a regular file holding a
    report this module wrote (read through ``O_NOFOLLOW``); the report is
    written to a new temporary file there (``O_EXCL | O_NOFOLLOW``) and renamed
    over the name, which replaces the directory entry and never follows a
    symlink. Elsewhere each ancestor is checked with ``lstat`` right before
    the same temporary-file write, which narrows the window without closing it.
    """
    temp_name = f".{path.name}.writing-{os.getpid()}"
    if not _FD_SAFE:
        path.parent.mkdir(parents=True, exist_ok=True)
        for ancestor in (path.parent, *path.parent.parents):
            if os.path.islink(ancestor):
                raise OutcomesLayoutError(f"refusing to write report {path}: {ancestor} is a symlink now")
        if os.path.lexists(path):
            if os.path.islink(path) or not path.is_file():
                raise OutcomesLayoutError(f"refusing to write report {path}: it is not a regular file now")
            _check_existing_report(path.read_bytes(), path)
        temp = path.with_name(temp_name)
        # Created outside the guard: when the name already exists (left by
        # another run), ``open`` refuses and that file, which this call did
        # not create, is never removed.
        fh = open(temp, "xb")
        try:
            with fh:
                fh.write(data)
            os.replace(temp, path)
        except BaseException:
            try:
                temp.unlink()
            except OSError:
                pass
            raise
        return
    # O_PATH (Linux) opens a directory that grants search but not read
    # permission, as walking a path does; elsewhere O_RDONLY.
    dir_flags = getattr(os, "O_PATH", os.O_RDONLY) | _O_DIRECTORY
    parts = path.parent.parts
    fd = os.open(parts[0], dir_flags)
    try:
        walked = Path(parts[0])
        for name in parts[1:]:
            walked = walked / name
            try:
                try:
                    next_fd = os.open(name, dir_flags | _O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    try:
                        os.mkdir(name, dir_fd=fd)
                    except FileExistsError:
                        pass
                    next_fd = os.open(name, dir_flags | _O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                raise OutcomesLayoutError(
                    f"refusing to write report {path}: {walked} is not a real directory now (a symlink or a file; "
                    f"{exc.strerror}); nothing was written"
                ) from None
            os.close(fd)
            fd = next_fd
            problem = _report_directory_problem(fd)
            if problem is not None:
                raise OutcomesLayoutError(f"refusing to write report {path}: {walked} {problem}; nothing was written")
        try:
            st = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            st = None
        if st is not None:
            if not stat.S_ISREG(st.st_mode):
                raise OutcomesLayoutError(
                    f"refusing to write report {path}: it is not a regular file now (a symlink or another kind of "
                    f"entry); nothing was written"
                )
            existing_fd = os.open(path.name, os.O_RDONLY | _O_NOFOLLOW, dir_fd=fd)
            with os.fdopen(existing_fd, "rb") as fh:
                _check_existing_report(fh.read(), path)
        out = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o666, dir_fd=fd)
        try:
            with os.fdopen(out, "wb") as fh:
                fh.write(data)
            os.rename(temp_name, path.name, src_dir_fd=fd, dst_dir_fd=fd)
        except BaseException:
            try:
                os.unlink(temp_name, dir_fd=fd)
            except OSError:
                pass
            raise
    finally:
        os.close(fd)


# ── atomic staging + swap writer ──────────────────────────────────────────────

_O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY: int = getattr(os, "O_DIRECTORY", 0)

# Where directories can be opened without following a symlink and worked on by
# descriptor (Linux, macOS), the staging directory is written into and removed
# through a descriptor, so a path swapped for a symlink after its check is
# never followed. Elsewhere each step re-checks the directory's identity first,
# which narrows the window without closing it.
_FD_SAFE: bool = bool(
    _O_NOFOLLOW
    and _O_DIRECTORY
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.rmdir in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.scandir in os.supports_fd
    and getattr(shutil.rmtree, "avoids_symlink_attacks", False)
)


def _directory_identity(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of the real directory at ``path`` (not followed), or
    ``None`` when nothing is there; anything else at ``path`` is refused."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        raise OutcomesLayoutError(f"refusing to write {path}: it is not a directory (or is a symlink)")
    return (st.st_dev, st.st_ino)


def _is_same_directory(path: Path, identity: tuple[int, int]) -> bool:
    """True when ``path`` is still the real directory ``identity`` names (never a symlink)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == identity


def _require_same_directory(path: Path, identity: tuple[int, int]) -> None:
    """Refuse to rename into ``path`` unless it is still the directory checked."""
    if not _is_same_directory(path, identity):
        raise OutcomesLayoutError(
            f"refusing to write into {path}: it is no longer the directory that was checked (replaced, "
            f"or turned into a symlink, while the staged tree was prepared)"
        )


def _open_checked_directory(path: Path, identity: tuple[int, int]) -> int:
    """A descriptor for the real directory at ``path``, which must still be ``identity``.

    Opened with ``O_NOFOLLOW | O_DIRECTORY`` (a symlink at ``path`` is never
    followed) and compared by ``fstat``, so whatever is done through the
    descriptor happens in the directory that was checked, whatever ``path``
    names by then.
    """
    try:
        fd = os.open(path, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    except OSError as exc:
        raise OutcomesLayoutError(
            f"refusing to work in {path}: it cannot be opened as a real directory ({exc.strerror})"
        ) from None
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != identity:
        os.close(fd)
        raise OutcomesLayoutError(f"refusing to work in {path}: it is no longer the directory that was checked")
    return fd


def _write_new_files(directory: Path, identity: tuple[int, int], files: Iterable[tuple[str, bytes]]) -> None:
    """Create each named file in the staging directory ``directory`` (still ``identity``).

    Each file is created new (``O_EXCL``: nothing existing is overwritten) and
    never through a symlink, by descriptor where ``_FD_SAFE``: a staging
    directory swapped for a symlink into a data family after its creation
    is not written through (the family's ``index.json`` would otherwise be
    replaced).
    """
    if _FD_SAFE:
        fd = _open_checked_directory(directory, identity)
        try:
            for name, data in files:
                out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o666, dir_fd=fd)
                with os.fdopen(out, "wb") as fh:
                    fh.write(data)
        finally:
            os.close(fd)
        return
    for name, data in files:
        _require_same_directory(directory, identity)
        with open(directory / name, "xb") as fh:
            fh.write(data)


def _make_staged_subdirectory(directory: Path, identity: tuple[int, int], name: str) -> tuple[int, int]:
    """Create ``directory/name`` inside the checked staging directory; return its identity."""
    if _FD_SAFE:
        fd = _open_checked_directory(directory, identity)
        try:
            os.mkdir(name, dir_fd=fd)
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        finally:
            os.close(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise OutcomesLayoutError(f"{directory / name} is not the directory just created")
        return (st.st_dev, st.st_ino)
    _require_same_directory(directory, identity)
    (directory / name).mkdir()
    created = _directory_identity(directory / name)
    if created is None:
        raise OutcomesLayoutError(f"{directory / name} vanished right after it was created")
    return created


def _remove_own_staging_dir(work: Path, identity: tuple[int, int] | None) -> None:
    """Delete this module's staging directory, markers last, never through a symlink.

    Method: ``identity`` is the directory's ``(st_dev, st_ino)`` when
    ``check_output_target`` accepted it or this call created it, or ``None``
    when nothing was there then. ``work`` is read with ``lstat`` (never
    followed): absent, there is nothing to do; a symlink, a file, a directory
    other than the recorded one, or one that appeared since the check is
    refused and nothing is deleted (red-team m3 round 2: a ``.partial``
    turned into a symlink into a family folder had that folder emptied,
    before staging and in the cleanup after a refused swap). Where
    ``_FD_SAFE`` the entries are removed through a descriptor opened with
    ``O_NOFOLLOW`` and compared with ``identity``, subdirectories with
    ``shutil.rmtree(dir_fd=...)``, which follows no symlink; the directory
    itself is removed by name from its parent after its identity is checked
    once more. Order: every other entry, then ``SWAP_COMPLETE_FILE``, then
    the staging marker, so an interrupted removal leaves the markers that
    say what the leftovers are.
    """
    try:
        st = os.lstat(work)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        raise OutcomesLayoutError(
            f"refusing to delete {work}: it is not a real directory (a symlink or a file is there now); "
            f"nothing was deleted"
        )
    if identity is None or (st.st_dev, st.st_ino) != identity:
        raise OutcomesLayoutError(
            f"refusing to delete {work}: it is not the staging directory that was checked (replaced, or created "
            f"since the check); nothing was deleted"
        )
    last = (SWAP_COMPLETE_FILE, STAGING_MARKER_FILE)
    if not _FD_SAFE:
        with os.scandir(work) as listing:
            entries = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in listing]
        for name, is_dir in entries:
            if name in last:
                continue
            _require_same_directory(work, identity)
            if is_dir:
                shutil.rmtree(work / name)
            else:
                os.unlink(work / name)
        for name in last:
            if os.path.lexists(work / name):
                os.unlink(work / name)
        _require_same_directory(work, identity)
        os.rmdir(work)
        return
    fd = _open_checked_directory(work, identity)
    try:
        with os.scandir(fd) as listing:
            entries = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in listing]
        for name, is_dir in entries:
            if name in last:
                continue
            if is_dir:
                shutil.rmtree(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)
        for name in last:
            try:
                os.unlink(name, dir_fd=fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(fd)
    parent_fd = os.open(work.parent, os.O_RDONLY | _O_DIRECTORY)
    try:
        now = os.stat(work.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(now.st_mode) or (now.st_dev, now.st_ino) != identity:
            raise OutcomesLayoutError(
                f"refusing to remove {work}: it was replaced while its entries were removed"
            )
        os.rmdir(work.name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _cleanup_staging(target: Path, work: Path, identity: tuple[int, int]) -> str | None:
    """Remove the staging directory unless it holds what the target may still need.

    Method: ``_retained_backups`` is read first (red-team m3 round 2: a second
    interrupt during the rollback propagated, and the cleanup deleted
    ``_previous_shards`` while the target held the previous index over the
    new shards, or no shards at all). When it names anything, the directory
    is kept and every later write refuses (``check_output_target``) until a
    person has dealt with it. Returns the problem to report (kept, or not
    removed), or ``None``. ``KeyboardInterrupt`` and ``SystemExit``
    propagate.
    """
    try:
        retained = _retained_backups(target, work)
    except OSError as exc:
        return f"the staging directory {work} is kept: whether it holds a needed backup could not be read ({exc})"
    if retained:
        return (
            f"the staging directory {work} is kept: {', '.join(str(p) for p in retained)} hold(s) the previous "
            f"bundle's shards or index, which {target} may still need; every later write refuses until a person "
            f"has put them back into {target} or set them aside"
        )
    try:
        _remove_own_staging_dir(work, identity)
    except (OSError, OutcomesLayoutError) as exc:
        return f"staging directory {work} was not removed ({exc})"
    return None


def _keep_previous_index(live_index: Path, backup: Path) -> None:
    """Keep the previous ``index.json`` at ``backup``: a hard link to it (the
    target keeps its index throughout), or a byte copy where hard links are not
    supported.

    Method: the copy is written to ``PREVIOUS_INDEX_COPY_FILE`` beside
    ``backup`` and renamed to ``backup`` only once it is whole; on any
    exception during the copy, ``KeyboardInterrupt`` and ``SystemExit``
    included, the partial copy is removed and the exception propagates.
    Red-team m3 round 3 interrupted the copy half-way: the truncated file
    under ``backup`` was then read as a backup the target needed, the
    interrupt was reported as a failed restore, and the next run was told to
    install it over the intact index. A leftover copy under the temporary
    name is never read as a backup, and the staging directory it is in is
    removed as usual.
    """
    try:
        os.link(live_index, backup, follow_symlinks=False)
        return
    except OSError:
        pass
    copying = backup.with_name(PREVIOUS_INDEX_COPY_FILE)
    try:
        shutil.copyfile(live_index, copying, follow_symlinks=False)
    except BaseException:
        try:
            copying.unlink()
        except OSError:
            pass
        raise
    os.replace(copying, backup)


def _swap_into_target(
    target: Path,
    work: Path,
    pre_identity: tuple[int, int] | None,
    work_identity: tuple[int, int],
) -> None:
    """Move the staged, validated tree from ``work`` into ``target``; on any
    exception put ``target`` back exactly as it was, then re-raise.

    Method:
    1. Re-check (the staged tree took time to write and validate, and the
       target or an ancestor may have been swapped meanwhile; red-team m3
       round 1 wrote through a target that became a symlink after the check):
       ``check_output_target`` again, then the target must be the directory
       that was checked (``pre_identity``, compared by device and inode with
       ``lstat``, so a symlink never matches), or, when it was absent, is
       created with ``os.mkdir``, which refuses anything that has appeared
       there, a symlink included; ``work`` must still be the staging
       directory this call created (``work_identity``). Every rename is
       preceded by the same identity checks of both (``_require_same_directory``).
       The window between that check and the rename is not closed:
       ``os.replace`` here takes paths, not directory handles.
    2. The swap, one rename per step: the bundle marker (when the target has
       none), a hard link to the previous ``index.json`` kept as
       ``_previous_index.json`` (``_keep_previous_index``; the target keeps
       its index), the old ``shards/`` moved to ``_previous_shards``, the new
       ``shards/`` moved in, the new ``index.json`` moved in; then
       ``SWAP_COMPLETE_FILE`` records the digest of the bundle installed
       (``_tree_digest``, taken from the staged tree before the swap), so
       leftovers of an interrupted cleanup are known for superseded ones.
    3. On any exception, ``KeyboardInterrupt`` and ``SystemExit`` included,
       the rollback reads the filesystem rather than flags set after each
       rename: an interrupt can land after a rename has completed and before
       the next statement runs (red-team m3 round 1: the previous index was
       overwritten and kept nowhere, or the target was left with mixed
       versions). The completion record goes first. A step is undone when its
       effect is there: the new index when the staged one is gone (the
       previous index moved back, or the new one removed when there was
       none), the new shards when the staged ones are gone, the previous
       shards when they are in ``_previous_shards`` and the target has none,
       the marker when this call moved it in, the target when this call
       created it and it is empty. Each undo, its identity checks included,
       runs under its own guard, so a second interrupt during one (red-team
       m3 round 2) is recorded and the rest still run. An undo is skipped
       (and reported) when the target, or for one that touches ``work`` the
       staging directory, is no longer the directory written into. If the
       previous shards or index cannot be put back they stay in ``work``,
       which is kept, and ``RetainedBackupError`` names them; every later
       call refuses until a person has dealt with them. Otherwise the
       original exception propagates, with any rollback problem as a note.
    """
    check_output_target(target)
    _require_same_directory(work, work_identity)
    dst_index = target / INDEX_FILE
    dst_shards = target / SHARDS_DIR
    dst_marker = target / BUNDLE_MARKER_FILE
    work_index = work / INDEX_FILE
    work_shards = work / SHARDS_DIR
    work_marker = work / BUNDLE_MARKER_FILE
    backup_shards = work / PREVIOUS_SHARDS_DIR
    backup_index = work / PREVIOUS_INDEX_FILE
    done_record = work / SWAP_COMPLETE_FILE
    installed = _tree_digest(work)
    if installed is None:
        raise OutcomesLayoutError(f"the staged tree in {work} cannot be read whole; {target} untouched")

    if pre_identity is not None:
        _require_same_directory(target, pre_identity)
    target_had_marker = pre_identity is not None and is_bundle_dir(target)
    identity: tuple[int, int] | None = pre_identity
    try:
        if pre_identity is None:
            try:
                os.mkdir(target)
            except FileExistsError:
                raise OutcomesLayoutError(
                    f"refusing to write {target}: it was absent when checked and something now exists "
                    f"there (a directory or symlink made meanwhile); nothing was written"
                ) from None
            identity = _directory_identity(target)
            if identity is None:
                raise OutcomesLayoutError(f"{target} vanished right after it was created; nothing was written")
        if not target_had_marker:
            _require_same_directory(target, identity)
            _require_same_directory(work, work_identity)
            os.replace(work_marker, dst_marker)
        if os.path.lexists(dst_index):
            _require_same_directory(work, work_identity)
            _keep_previous_index(dst_index, backup_index)
        if os.path.lexists(dst_shards):
            _require_same_directory(target, identity)
            _require_same_directory(work, work_identity)
            os.replace(dst_shards, backup_shards)
        _require_same_directory(target, identity)
        _require_same_directory(work, work_identity)
        os.replace(work_shards, dst_shards)
        _require_same_directory(target, identity)
        _require_same_directory(work, work_identity)
        os.replace(work_index, dst_index)
        _write_new_files(
            work, work_identity, [(SWAP_COMPLETE_FILE, encode_json_text({"installed": installed}, trailing_newline=True).encode("ascii"))]
        )
    except BaseException as swap_exc:
        undo_failures: list[str] = []

        def _undo(description: str, action: Callable[[], None], *, uses_work: bool) -> None:
            try:
                if identity is None or not _is_same_directory(target, identity):
                    undo_failures.append(
                        f"{description} skipped: {target} is not the directory this call wrote into"
                    )
                    return
                if uses_work and not _is_same_directory(work, work_identity):
                    undo_failures.append(
                        f"{description} skipped: {work} is not the staging directory this call created"
                    )
                    return
                action()
            except BaseException as undo_exc:  # noqa: BLE001 - recorded and reported below
                undo_failures.append(f"{description} failed ({type(undo_exc).__name__}: {undo_exc})")

        if os.path.lexists(done_record):
            _undo("removing the completion record", lambda: done_record.unlink(), uses_work=True)
        if not os.path.lexists(work_index):
            if os.path.lexists(backup_index):
                _undo("moving the previous index back", lambda: os.replace(backup_index, dst_index), uses_work=True)
            else:
                _undo("removing the new index", lambda: dst_index.unlink(), uses_work=False)
        if not os.path.lexists(work_shards) and os.path.lexists(dst_shards):
            _undo(
                "moving the new shards out",
                lambda: os.replace(dst_shards, work / FAILED_NEW_SHARDS_DIR),
                uses_work=True,
            )
        if os.path.lexists(backup_shards) and not os.path.lexists(dst_shards):
            _undo("moving the previous shards back", lambda: os.replace(backup_shards, dst_shards), uses_work=True)
        if not target_had_marker and not os.path.lexists(work_marker) and os.path.lexists(dst_marker):
            _undo("removing the marker this call installed", lambda: dst_marker.unlink(), uses_work=False)
        if pre_identity is None and os.path.isdir(target) and not os.path.islink(target):
            try:
                if (identity is None or _is_same_directory(target, identity)) and not any(os.scandir(target)):
                    target.rmdir()
            except BaseException as undo_exc:  # noqa: BLE001 - recorded and reported below
                undo_failures.append(f"removing the target this call created failed ({undo_exc})")

        retained = _retained_backups(target, work)
        if retained:
            raise RetainedBackupError(
                f"writing {target} failed ({type(swap_exc).__name__}: {swap_exc}) and the previous "
                f"bundle could not be put back ({'; '.join(undo_failures) or 'restore did not run'}). "
                f"It is retained at {', '.join(str(p) for p in retained)}: move it back into {target} "
                f"({SHARDS_DIR}/ to {dst_shards}, {PREVIOUS_INDEX_FILE} to {dst_index}) by hand, then delete "
                f"{work}; until then every write to {target} refuses"
            ) from swap_exc
        if undo_failures:
            swap_exc.add_note("rollback problems: " + "; ".join(undo_failures))
        raise


def write_outcomes_tree(
    out_dir: str | os.PathLike[str],
    index_doc: Mapping[str, Any],
    shard_docs: Mapping[int, Mapping[str, Any]],
    *,
    allow_null_commit: bool = False,
) -> ValidationResult:
    """Atomically write and verify an outcomes tree via ``<out_dir>.partial``.

    Method:
    1. ``check_output_target`` refuses every location this module may not
       write (L1) and a retained backup from an earlier failed restore (L3),
       before anything is created or deleted; the identities (device and
       inode, or absence) of the target and of ``<out_dir>.partial`` are
       recorded right after. The audit firewall runs over every row and
       shard next, still before touching the filesystem.
    2. A leftover ``.partial`` is removed only when it is still the directory
       that was checked (``_remove_own_staging_dir``: never through a
       symlink). The tree, its bundle marker and a staging marker are then
       written into a freshly created ``<out_dir>.partial``, by descriptor
       and never over an existing file (``_write_new_files``), and checked
       with ``validate_outcomes_tree``.
    3. The swap (``_swap_into_target``): the target is re-checked and must
       still be the directory recorded in step 1, and ``.partial`` the one
       created in step 2; then the bundle marker (when the target has none),
       ``shards/`` (the old one moved to ``.partial/_previous_shards``, the
       previous ``index.json`` kept as a hard link in
       ``.partial/_previous_index.json``), then ``index.json``, then the
       completion record. Any exception in this window, ``KeyboardInterrupt``
       and ``SystemExit`` included, and one landing just after a rename, rolls
       the target back to its exact previous state before it propagates. If
       the previous shards or index cannot be put back they stay in the
       staging directory, which is kept, and ``RetainedBackupError`` names
       them (every later call refuses until a person has dealt with them).
    4. On the way out the staging directory is removed, markers last
       (``_cleanup_staging``), unless it still holds a backup the target may
       need: then it is kept, whatever exception is propagating. A problem
       with it is returned as a warning, or added as a note to the exception
       propagating, never hidden. Sibling files in the target are never
       touched.
    """
    target = check_output_target(out_dir)
    pre_identity = _directory_identity(target)
    work = _staging_dir_for(target)
    work_checked = _directory_identity(work)

    layout = index_doc.get("layout")
    if not isinstance(layout, Mapping):
        raise OutcomesLayoutError("index_doc.layout must be an object")
    n_shards = _validate_shards(layout.get("shards"))

    # Pre-flight holdout firewall check BEFORE touching filesystem.
    rows = index_doc.get("data")
    if not isinstance(rows, list):
        raise OutcomesLayoutError("index_doc.data must be a list")
    rows_by_nct: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise OutcomesLayoutError("every index_doc.data item must be an object")
        nct_id = str(row.get("nct_id") or "")
        assert_no_audit_leakage(nct_id=nct_id, in_audit=row.get("in_audit"), index_row=row)
        rows_by_nct[nct_id] = row

    if set(shard_docs.keys()) != set(range(n_shards)):
        raise OutcomesLayoutError(
            f"shard_docs must cover exact shard indices 0..{n_shards - 1}"
        )
    for n in range(n_shards):
        sdoc = shard_docs[n]
        sdata = sdoc.get("data")
        if not isinstance(sdata, Mapping):
            raise OutcomesLayoutError(f"shard {n} data must be an object")
        for nct_id, entry in sdata.items():
            if not isinstance(entry, Mapping):
                raise OutcomesLayoutError(f"shard {n} entry {nct_id!r} must be an object")
            assert_no_audit_leakage(
                nct_id=str(nct_id),
                in_audit=entry.get("in_audit"),
                shard_record=entry,
                index_row=rows_by_nct.get(str(nct_id)),
            )

    # check_output_target accepted any existing .partial as this module's own
    # (empty, or holding the staging marker, and no retained backup); it is
    # removed only if it is still that directory.
    _remove_own_staging_dir(work, work_checked)
    work.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir()
    work_identity = _directory_identity(work)
    if work_identity is None:
        raise OutcomesLayoutError(f"{work} vanished right after it was created; nothing was written")
    preserve_work = False
    failure: BaseException | None = None
    cleanup_problem: str | None = None

    try:
        _write_new_files(
            work,
            work_identity,
            [
                (STAGING_MARKER_FILE, _marker_text(STAGING_MARKER).encode("utf-8")),
                (BUNDLE_MARKER_FILE, _marker_text(BUNDLE_MARKER).encode("utf-8")),
                (INDEX_FILE, encode_json_text(index_doc, trailing_newline=True).encode("utf-8")),
            ],
        )
        shards_identity = _make_staged_subdirectory(work, work_identity, SHARDS_DIR)
        _write_new_files(
            work / SHARDS_DIR,
            shards_identity,
            ((f"{n}.json.gz", encode_gz_bytes(shard_docs[n])) for n in range(n_shards)),
        )

        result = validate_outcomes_tree(
            work,
            allow_null_commit=allow_null_commit,
            raise_on_audit_leakage=True,
        )
        if not result.ok:
            raise OutcomesLayoutError(
                f"staged outcomes tree failed validation; {target} untouched: {'; '.join(result.problems)}"
            )
        try:
            _swap_into_target(target, work, pre_identity, work_identity)
        except RetainedBackupError:
            preserve_work = True
            raise
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if not preserve_work:
            cleanup_problem = _cleanup_staging(target, work, work_identity)
            if cleanup_problem is not None and failure is not None:
                failure.add_note(cleanup_problem)

    if cleanup_problem is not None:
        result = dataclasses.replace(result, warnings=(*result.warnings, cleanup_problem))
    return result
