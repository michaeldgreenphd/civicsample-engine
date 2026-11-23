"""Multi-site Diversity Action Plan (FDORA) stratification and sponsor peer benchmarks.

Joins study records (weekly ``src/full_records.py`` containers or raw
ClinicalTrials.gov API v2 study documents), optional ``condition_mesh.jsonl.gz``
(``src/condition_mesh.py``), ``condition_ontology.json``, and read-only
curated ``sponsors/`` attribution.

Zero RNG, zero network calls, zero substring matching in sponsor attribution.
"""
from __future__ import annotations

import calendar
import gzip
import hashlib
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

import numpy as np
from rapidfuzz import fuzz, process

from dap import UNIT_BASIS
from dap.demographics import (
    ETHNICITY_CLASSIFIED_KEYS,
    RACE_CLASSIFIED_KEYS,
    SEX_GENDER_STATES,
    TABLE_STATUSES,
    attach_reference_medians,
    extract_demographics,
)
from dap.geography import (
    SITE_FOOTPRINT_CLASSES,
    US_SITE_SHARE_BINS,
    extract_geography,
)
from sponsors import adapter, audit_states, sponsor_roles
from src import condition_mesh as cmesh
from src import full_records

ROOT: Path = Path(__file__).resolve().parents[1]
DEFAULT_ONTOLOGY_PATH: Path = ROOT / "condition_ontology.json"
DEFAULT_SPONSOR_RULES_PATH: Path = ROOT / "sponsors" / "company_aliases.csv"
DEFAULT_MIN_CELL_TRIALS: int = 5

# Statutory / regulatory milestones:
# - 42 CFR Part 11 (FDAAA 801 Final Rule) effective date: 2017-01-18
# - Food and Drug Omnibus Reform Act of 2022 (FDORA, Pub. L. 117-328,
#   Section 3601 Diversity Action Plans) enactment date: 2022-12-29
FINAL_RULE_EFFECTIVE_DATE: date = date(2017, 1, 18)
FDORA_ENACTMENT_DATE: date = date(2022, 12, 29)

REGULATORY_ERAS: tuple[str, ...] = (
    "pre_2017_final_rule",
    "post_2017_pre_fdora",
    "post_2022_fdora",
    "era_indeterminate",
)

SPONSOR_REVIEW_STATES: tuple[str, ...] = (
    "attributed",
    "reviewed_excluded",
    "unreviewed",
)

STRATUM_KINDS: tuple[str, ...] = (
    "ontology_footprint",
    "ontology_us_share_bin",
    "mesh_footprint",
    "mesh_us_share_bin",
    "indication_us_reference",
)

MESH_STATES_EXTENDED: tuple[str, ...] = tuple(cmesh.STATES) + ("mesh_sidecar_not_provided",)

# Run stamps ``src/extract_all.py`` writes on every ``condition_mesh.jsonl.gz``
# line; ``load_condition_mesh`` keeps them so ``check_condition_mesh_provenance``
# can tie the sidecar to the study records it annotates.
MESH_RUN_STAMP_KEYS: tuple[str, ...] = ("snapshot_date", "extracted_at", "pipeline_commit")

_SITE_PART_NAME_RX: re.Pattern[str] = re.compile(
    r"^(?:demographics|studies_tab)\.part\d+\.json(?:\.gz)?$",
    re.IGNORECASE,
)
_PARTIAL_DATE_RX: re.Pattern[str] = re.compile(
    r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$"
)
_PUNCT_RE: re.Pattern[str] = re.compile(r"[^\w\s/-]")
# ASCII digits only: Python's \d also matches other scripts' digits (NCT followed by Arabic-Indic
# digits would pass), which no ClinicalTrials.gov ID, join or link uses.
_NCT_ID_RX: re.Pattern[str] = re.compile(r"NCT[0-9]{8}")
# Ontology keywords this short collide with fragments of unrelated words when
# tested as substrings: 'ra' in 'Trauma', 'all' in 'Falls', 'ms' in
# 'Behavioral Symptoms', 'skin' in 'Dyskinesia', 'burn' in 'Burnout'. They
# therefore match only standing alone. Longer keywords keep the substring test
# because compounds carry real matches ('sarcoma' in 'Gliosarcoma', 'leukemia'
# in 'Erythroleukemia'); measured on 142,369 published condition mentions,
# nearly all wrong substring matches came from keywords of <= 4 characters.
# The same rule applies in src/condition_classifier.py; the two must agree.
_SHORT_KEYWORD_MAX_LEN: int = 4


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file on disk."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class OntologyClassifier:
    """Deterministic condition classifier bound to an explicit ``condition_ontology.json`` file.

    Reproduces ``src/condition_classifier.py`` keyword + ``rapidfuzz``
    matching without mutating module-level globals when custom ontology files
    are supplied in tests. Keywords are tried longest-first; a keyword of
    ``_SHORT_KEYWORD_MAX_LEN`` characters or fewer matches the normalized
    condition only as a whole word or phrase with an optional ``s``/``es``
    plural suffix, a longer one as a substring (see ``_SHORT_KEYWORD_MAX_LEN``).
    """

    def __init__(self, ontology_path: Path = DEFAULT_ONTOLOGY_PATH, fuzzy_threshold: int = 85) -> None:
        self.ontology_path = ontology_path
        self.fuzzy_threshold = fuzzy_threshold
        raw = ontology_path.read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        doc = json.loads(raw.decode("utf-8"))
        self.version = str((doc.get("metadata") or {}).get("version") or "unknown")
        self._flat_lookup: dict[str, tuple[str, str]] = {}
        for primary, secondaries in (doc.get("categories") or {}).items():
            for secondary, keywords in secondaries.items():
                for kw in keywords:
                    self._flat_lookup[str(kw).lower()] = (str(primary), str(secondary))
        self._all_keywords: list[str] = sorted(
            self._flat_lookup.keys(),
            key=lambda s: (-len(s), s),
        )
        # One precompiled boundary pattern per short keyword: not preceded by a
        # word character, optionally followed by a plural "s"/"es", then not
        # followed by a word character ("ms" matches "MS", never "Symptoms").
        self._short_keyword_rx: dict[str, re.Pattern[str]] = {
            kw: re.compile(r"(?<!\w)" + re.escape(kw) + r"(?:s|es)?(?!\w)")
            for kw in self._all_keywords
            if len(kw) <= _SHORT_KEYWORD_MAX_LEN
        }

    @staticmethod
    def _normalize(text: str) -> str:
        cleaned = _PUNCT_RE.sub(" ", text.lower().strip())
        return " ".join(cleaned.split())

    def classify_one(self, condition: str) -> tuple[str, str]:
        """Classify one condition string into ``(primary, secondary)``."""
        if not condition or not condition.strip():
            return ("Other", "Uncategorized")
        norm = self._normalize(condition)
        if not norm:
            return ("Other", "Uncategorized")
        for kw in self._all_keywords:
            short_rx = self._short_keyword_rx.get(kw)
            if short_rx is not None:
                hit = short_rx.search(norm) is not None
            else:
                hit = kw in norm
            if hit:
                return self._flat_lookup[kw]
        match = process.extractOne(
            norm,
            self._all_keywords,
            scorer=fuzz.token_set_ratio,
            score_cutoff=self.fuzzy_threshold,
        )
        if match is not None:
            return self._flat_lookup[str(match[0])]
        return ("Other", "Uncategorized")

    def classify(self, conditions: Iterable[str]) -> dict[str, Any]:
        """Classify a trial's condition list deterministically."""
        cond_list = [str(c) for c in conditions if isinstance(c, str) and c.strip()]
        if not cond_list:
            return {
                "primary_condition": "Other",
                "secondary_condition": "Uncategorized",
                "all_classifications": [],
            }
        all_cls: list[dict[str, str]] = []
        primary_counts: dict[str, int] = {}
        primary_to_secondary: dict[str, str] = {}
        for cond in cond_list:
            pri, sec = self.classify_one(cond)
            all_cls.append({"condition": cond, "primary": pri, "secondary": sec})
            primary_counts[pri] = primary_counts.get(pri, 0) + 1
            if pri not in primary_to_secondary:
                primary_to_secondary[pri] = sec
        best_primary = max(primary_counts, key=lambda p: (primary_counts[p], p))
        return {
            "primary_condition": best_primary,
            "secondary_condition": primary_to_secondary[best_primary],
            "all_classifications": all_cls,
        }


def parse_partial_date_bounds(raw: str | None) -> tuple[date, date, str] | None:
    """Parse ``YYYY-MM-DD``, ``YYYY-MM``, or ``YYYY`` into exact ``(min_date, max_date, precision)``.

    Never fabricates ``-01`` days for partial dates: ``YYYY-MM`` spans the full
    calendar month and ``YYYY`` spans ``YYYY-01-01`` through ``YYYY-12-31``.
    Returns ``None`` when missing or malformed.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    m = _PARTIAL_DATE_RX.match(text)
    if not m:
        return None
    year = int(m.group(1))
    month_s, day_s = m.group(2), m.group(3)
    try:
        if month_s is None:
            return date(year, 1, 1), date(year, 12, 31), "year"
        month = int(month_s)
        if day_s is None:
            last_day = calendar.monthrange(year, month)[1]
            return date(year, month, 1), date(year, month, last_day), "month"
        exact = date(year, month, int(day_s))
        return exact, exact, "day"
    except ValueError:
        return None


def classify_regulatory_era(record: dict[str, Any]) -> dict[str, Any]:
    """Classify a trial's regulatory era from exact partial-date intervals.

    Uses ``start_date`` (when enrollment commences, the statutory trigger for
    FDORA Diversity Action Plans under 21 U.S.C. 355(z)(3)). If ``start_date``
    is absent, a ``primary_completion_date`` wholly before ``2017-01-18``
    bounds enrollment start to ``pre_2017_final_rule``; any partial date whose
    ``[min_date, max_date]`` interval straddles ``2017-01-18`` or ``2022-12-29``
    is classified as ``era_indeterminate`` rather than inventing a day.
    """
    protocol = record.get("protocolSection")
    if isinstance(protocol, dict):
        status_mod = protocol.get("statusModule") or {}
        start_struct = status_mod.get("startDateStruct") or {}
        pcomp_struct = status_mod.get("primaryCompletionDateStruct") or {}
        start_raw = start_struct.get("date")
        start_type = start_struct.get("type")
        pcomp_raw = pcomp_struct.get("date")
    else:
        start_raw = record.get("start_date")
        start_type = record.get("start_date_type")
        pcomp_raw = record.get("primary_completion_date")

    parsed_start = parse_partial_date_bounds(start_raw)
    if parsed_start is not None:
        lo, hi, precision = parsed_start
        if hi < FINAL_RULE_EFFECTIVE_DATE:
            era = "pre_2017_final_rule"
            reason = "start_date_wholly_before_2017_01_18"
        elif lo >= FINAL_RULE_EFFECTIVE_DATE and hi < FDORA_ENACTMENT_DATE:
            era = "post_2017_pre_fdora"
            reason = "start_date_wholly_within_2017_01_18_to_2022_12_28"
        elif lo >= FDORA_ENACTMENT_DATE:
            era = "post_2022_fdora"
            reason = "start_date_on_or_after_2022_12_29"
        elif lo < FINAL_RULE_EFFECTIVE_DATE <= hi:
            era = "era_indeterminate"
            reason = "partial_start_date_straddles_2017_01_18_cutoff"
        else:
            era = "era_indeterminate"
            reason = "partial_start_date_straddles_2022_12_29_cutoff"
        return {
            "regulatory_era": era,
            "date_basis": "start_date",
            "date_raw": str(start_raw).strip(),
            "date_type": str(start_type) if start_type else None,
            "date_precision": precision,
            "date_min": lo.isoformat(),
            "date_max": hi.isoformat(),
            "reason": reason,
        }

    parsed_pcomp = parse_partial_date_bounds(pcomp_raw)
    if parsed_pcomp is not None and parsed_pcomp[1] < FINAL_RULE_EFFECTIVE_DATE:
        lo, hi, precision = parsed_pcomp
        return {
            "regulatory_era": "pre_2017_final_rule",
            "date_basis": "bounded_by_primary_completion_date",
            "date_raw": str(pcomp_raw).strip(),
            "date_type": None,
            "date_precision": precision,
            "date_min": lo.isoformat(),
            "date_max": hi.isoformat(),
            "reason": "missing_start_date_primary_completion_wholly_before_2017_01_18",
        }

    has_raw_start = isinstance(start_raw, str) and bool(start_raw.strip())
    return {
        "regulatory_era": "era_indeterminate",
        "date_basis": None,
        "date_raw": str(start_raw).strip() if has_raw_start else None,
        "date_type": str(start_type) if start_type else None,
        "date_precision": None,
        "date_min": None,
        "date_max": None,
        "reason": "unparsable_start_date" if has_raw_start else "missing_start_date",
    }


def normalize_phase(record: dict[str, Any]) -> str:
    """Normalize CT.gov study phase(s) across raw API v2 and weekly records."""
    protocol = record.get("protocolSection")
    raw_phases: list[str] = []
    if isinstance(protocol, dict):
        phases = (protocol.get("designModule") or {}).get("phases")
        if isinstance(phases, list):
            raw_phases = [str(p).strip().upper() for p in phases if str(p).strip()]
    elif isinstance(record.get("phases"), list):
        raw_phases = [str(p).strip().upper() for p in record["phases"] if str(p).strip()]
    elif isinstance(record.get("phase"), str) and record["phase"].strip():
        raw_phases = [
            p.strip().upper().replace(" ", "_")
            for p in re.split(r"[,;/]", record["phase"])
            if p.strip()
        ]

    if not raw_phases:
        return "PHASE_NOT_STATED"

    cleaned: list[str] = []
    for tok in raw_phases:
        t = re.sub(r"[\s\-]+", "_", tok)
        if t in ("N/A", "NOT_APPLICABLE"):
            t = "NA"
        if t not in cleaned:
            cleaned.append(t)
    order = {"EARLY_PHASE1": 0, "PHASE1": 1, "PHASE2": 2, "PHASE3": 3, "PHASE4": 4, "NA": 5}
    cleaned.sort(key=lambda x: (order.get(x, 99), x))
    return "_".join(cleaned)


def _as_dict(value: Any) -> dict[str, Any]:
    """``value`` when it is a JSON object, else ``{}`` (a missing or malformed part reads as empty)."""
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    """``value`` when it is a JSON array, else ``[]`` (a string is never iterated as characters)."""
    return value if isinstance(value, list) else []


def _extract_core_metadata(record: dict[str, Any]) -> dict[str, Any]:
    """Extract common trial identification, study type, conditions, and sponsor fields.

    Loaders call this on raw records before ``_require_complete_records`` has checked them (to name
    a record in an error, or to reconcile returned NCT ids), so every part is read type-safely: a
    module, sponsor or enrollment value that is not an object (``"identificationModule":
    "projected"``) reads as empty instead of raising ``AttributeError``, and the completeness check
    then refuses the record by name.
    """
    protocol = record.get("protocolSection")
    if isinstance(protocol, dict):
        id_mod = _as_dict(protocol.get("identificationModule"))
        design_mod = _as_dict(protocol.get("designModule"))
        status_mod = _as_dict(protocol.get("statusModule"))
        cond_mod = _as_dict(protocol.get("conditionsModule"))
        sponsor_mod = _as_dict(protocol.get("sponsorCollaboratorsModule"))
        lead = _as_dict(sponsor_mod.get("leadSponsor"))
        enr_info = _as_dict(design_mod.get("enrollmentInfo"))
        raw_collabs = _as_list(sponsor_mod.get("collaborators"))
        collaborators = [
            {
                "name": str(c.get("name") or ""),
                "class": str(c.get("class") or ""),
            }
            for c in raw_collabs
            if isinstance(c, dict)
        ]
        enr_raw = enr_info.get("count")
        enrollment: int | None = None
        if isinstance(enr_raw, int) and not isinstance(enr_raw, bool):
            enrollment = enr_raw
        elif isinstance(enr_raw, (float, str)) and str(enr_raw).strip().isdigit():
            enrollment = int(str(enr_raw).strip())
        return {
            "nct_id": str(id_mod.get("nctId") or "").strip(),
            "brief_title": id_mod.get("briefTitle"),
            "study_type": design_mod.get("studyType") or None,
            "overall_status": status_mod.get("overallStatus") or None,
            "enrollment": enrollment,
            "enrollment_type": enr_info.get("type") or None,
            "conditions": [str(c) for c in _as_list(cond_mod.get("conditions")) if isinstance(c, str)],
            "lead_sponsor_name": lead.get("name") if lead.get("name") is not None else "Unknown",
            "sponsor_class": lead.get("class") if lead.get("class") is not None else "Unknown",
            "collaborators": collaborators,
        }

    enr_val = record.get("enrollment")
    enrollment_w: int | None = None
    if isinstance(enr_val, int) and not isinstance(enr_val, bool):
        enrollment_w = enr_val
    elif isinstance(enr_val, (float, str)) and str(enr_val).strip().isdigit():
        enrollment_w = int(str(enr_val).strip())

    enr_type = record.get("enrollment_type")
    if enr_type == "N/A" or enr_type == "":
        enr_type = None

    raw_collabs_w = record.get("collaborators") or []
    collaborators_w = [
        {
            "name": str(c.get("name") or ""),
            "class": str(c.get("class") or ""),
        }
        for c in raw_collabs_w
        if isinstance(c, dict)
    ]
    return {
        "nct_id": str(record.get("nct_id") or "").strip(),
        "brief_title": record.get("brief_title") or None,
        "study_type": record.get("study_type") or None,
        "overall_status": record.get("status") or record.get("overall_status") or None,
        "enrollment": enrollment_w,
        "enrollment_type": enr_type,
        "conditions": [str(c) for c in (record.get("conditions") or []) if isinstance(c, str)],
        "lead_sponsor_name": (
            record["lead_sponsor_name"] if record.get("lead_sponsor_name") is not None else "Unknown"
        ),
        "sponsor_class": (
            record["sponsor_class"] if record.get("sponsor_class") is not None else "Unknown"
        ),
        "collaborators": collaborators_w,
    }


def _require_study_objects(entries: list[Any], where: str) -> list[dict[str, Any]]:
    """Return ``entries`` when every element is a JSON object; refuse the whole input otherwise.

    Dropping a malformed element (``null``, a string, a list) would publish
    benchmarks for a smaller cohort while the input sha256 still covers it and
    ``n_records`` pretends it never existed, so the input is refused, naming
    the count and the first bad index.
    """
    bad = [i for i, entry in enumerate(entries) if not isinstance(entry, dict)]
    if bad:
        raise ValueError(
            f"{where}: {len(bad)} of {len(entries)} entries are not study objects "
            f"(first: index {bad[0]} is {type(entries[bad[0]]).__name__}); refusing the input"
        )
    return entries


# Protocol modules ClinicalTrials.gov requires on every registration (identification,
# overall status, sponsor, conditions, study type), so a complete API v2 record always
# carries them. A record without one was fetched with a ``fields=`` projection or
# truncated: building from it would read the gaps as "no conditions" (ontology Other),
# "no sponsor" (lead "Unknown", unreviewed) and so on while the contract still passed.
# Their absence is refused, not benchmarked. ``contactsLocationsModule`` and
# ``derivedSection`` are legitimately absent on some registrations ("no listed
# facilities", "no_derived") and are not required. Same rule as the feasibility
# benchmarks (#107).
REQUIRED_PROTOCOL_MODULES: tuple[str, ...] = (
    "identificationModule",
    "statusModule",
    "sponsorCollaboratorsModule",
    "conditionsModule",
    "designModule",
)

# Fields CT.gov holds on every registration, inside modules a projection can keep as shells: the
# brief title, the overall status, the record-verification date (a required data element) and the
# study type. A field-by-field projection (``fields=NCTId,OverallStatus,LeadSponsorName,Condition,
# StudyType``) keeps all five module objects but drops most of these, so a record without any of
# them is refused. Among the stored CT.gov caches, the device-linkage projections lack
# ``statusVerifiedDate`` and the outcomes second-pass projection lacks ``briefTitle``; the
# unprojected records carry all four. A projection that requests whole modules and omits others
# entirely (``contactsLocationsModule``, ``derivedSection``) cannot be told from a registration
# without them. The payloads of the conditions and sponsor modules are checked separately (below).
# Same rule as the feasibility benchmarks (#107).
REQUIRED_RECORD_FIELDS: tuple[tuple[str, str], ...] = (
    ("identificationModule", "briefTitle"),
    ("statusModule", "overallStatus"),
    ("statusModule", "statusVerifiedDate"),
    ("designModule", "studyType"),
)

# The conditions and lead-sponsor payloads. CT.gov requires every registration to list at least one
# condition and a lead sponsor, and the build reads both: the conditions place a trial in its
# ontology indication, and the lead sponsor's name and class are its sponsor attribution
# (``lead.literal_name``, ``lead.agency_class``). ``_extract_core_metadata`` reads a gap as no
# conditions (ontology Other) and an "Unknown" lead, so a module kept without its payload
# (``"conditionsModule": {}``, ``conditions`` that is not a list, a ``leadSponsor`` without a
# ``name``) is refused like a missing module: ``conditionsModule.conditions`` must be a non-empty
# list of non-blank strings, and ``sponsorCollaboratorsModule.leadSponsor`` an object whose
# ``REQUIRED_LEAD_SPONSOR_FIELDS`` are non-blank strings. The class is required on evidence: the
# unprojected stored records carry one, and none of the 80,890 trials in the weekly full records
# extracted 2026-11-08 fell back to the extractor's "Unknown" class, while the device-linkage
# projection that did not request ``LeadSponsorClass`` kept ``leadSponsor`` with its name alone. In
# the 2026-06-19 AACT sponsors fixture all 976 lead rows without a class are named "[Redacted]"; the
# fixture holds no study status, so that they are withheld trials is inferred, not checked. The one
# such record cached from the API (NCT04060524, ``WITHHELD``, in the device-linkage acquisition
# cache) came back without a study type either, so it was refused already. Same rule as the
# feasibility benchmarks (#107).
REQUIRED_LEAD_SPONSOR_FIELDS: tuple[str, ...] = ("name", "class")


def _missing_condition_and_sponsor_payloads(protocol: dict[str, Any]) -> list[str]:
    """Name the conditions and lead-sponsor payloads ``protocol`` lacks (see the note above).

    A module that is absent or not an object is reported by the module check, not again here. A
    ``conditions`` list with any blank or non-string entry is refused whole: a registration never
    holds one, and keeping the rest would hide the malformation.
    """
    missing: list[str] = []
    cond_mod = protocol.get("conditionsModule")
    if isinstance(cond_mod, dict):
        conditions = cond_mod.get("conditions")
        if not (
            isinstance(conditions, list)
            and conditions
            and all(isinstance(c, str) and c.strip() for c in conditions)
        ):
            missing.append("conditionsModule.conditions")
    sponsor_mod = protocol.get("sponsorCollaboratorsModule")
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


# Parts of a raw record that DAP reads, or hands to the shared race, ethnicity and sex extractors
# (`src/`), without checking their type. A mutation fuzz of the build (each value of a complete
# record replaced by a list, object, number, boolean, null or blank) found every one of these raising
# AttributeError, TypeError or KeyError, so one malformed record stopped the whole build with a
# traceback. A part of the wrong type is refused by name, like a missing module, rather than read as
# absent; the shared extractors are not changed.
#   * `TYPED_PROTOCOL_PARTS`: (module, field, type) that must have that type when present; module
#     None names a field of `protocolSection` itself.
#   * `LIST_ENTRY_FIELDS`: lists among those whose entries must be objects, with the named fields
#     strings when present. `geography.extract_raw_locations` skipped a non-object location and read
#     a non-object `contactsLocationsModule` as no locations, so a malformed record lowered or zeroed
#     its listed facilities (and moved its footprint class) with nothing reported; a non-string
#     `country` read as a facility without a country, and a non-string collaborator name became the
#     text of a list.
#   * `BASELINE_SHAPE`: the baseline table, when present. Each key names a list of objects; inside
#     an object, `str` means a string when present, `REQUIRED_STR` a string that must be present,
#     and a nested dict another list of objects.
TYPED_PROTOCOL_PARTS: tuple[tuple[str | None, str, type], ...] = (
    ("statusModule", "startDateStruct", dict),
    ("statusModule", "primaryCompletionDateStruct", dict),
    ("designModule", "enrollmentInfo", dict),
    ("sponsorCollaboratorsModule", "collaborators", list),
    (None, "contactsLocationsModule", dict),
    ("contactsLocationsModule", "locations", list),
)
LIST_ENTRY_FIELDS: dict[tuple[str, str], tuple[str, ...]] = {
    ("sponsorCollaboratorsModule", "collaborators"): ("name", "class"),
    ("contactsLocationsModule", "locations"): ("country", "state"),
}
REQUIRED_STR = "required string"
# The phase values a CT.gov registration holds. The 5,330 stored unprojected records use exactly these;
# any other value (a typo such as PHASE_3, or a value CT.gov might add) is refused by name rather than
# published as a phase stratum of its own, so a new CT.gov value would show up as a refusal here.
CTGOV_PHASES: frozenset[str] = frozenset({"NA", "EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4"})
BASELINE_SHAPE: dict[str, Any] = {
    "groups": {"title": str},
    "denoms": {"units": REQUIRED_STR, "counts": {}},
    "measures": {
        "title": REQUIRED_STR,
        "paramType": str,
        "unitOfMeasure": str,
        "classes": {"categories": {"title": str, "measurements": {}}},
    },
}


def _object_list_problems(value: Any, path: str, fields: dict[str, Any]) -> list[str]:
    """Problems with `value` as a list of objects whose fields follow `fields` (see `BASELINE_SHAPE`)."""
    if not isinstance(value, list):
        return [f"{path} (a list)"]
    out: list[str] = []
    for i, item in enumerate(value):
        where = f"{path}[{i}]"
        if not isinstance(item, dict):
            out.append(f"{where} (an object)")
            continue
        for name, spec in fields.items():
            if isinstance(spec, dict):
                if name in item:
                    out += _object_list_problems(item[name], f"{where}.{name}", spec)
            elif spec == REQUIRED_STR or name in item:
                if not isinstance(item.get(name), str):
                    out.append(f"{where}.{name} (a string)")
    return out


def _mistyped_parts(study: dict[str, Any]) -> list[str]:
    """Name the parts of `study` with the wrong type: the `TYPED_PROTOCOL_PARTS` and their
    `LIST_ENTRY_FIELDS` entries, the phases, the NCT ID, and the baseline table (`BASELINE_SHAPE`)."""
    protocol = study.get("protocolSection")
    out: list[str] = []
    if isinstance(protocol, dict):
        for mod, field, kind in TYPED_PROTOCOL_PARTS:
            holder = protocol if mod is None else protocol.get(mod)
            if not isinstance(holder, dict) or field not in holder:
                continue
            where = field if mod is None else f"{mod}.{field}"
            if not isinstance(holder[field], kind):
                out.append(f"{where} ({'an object' if kind is dict else 'a list'})")
                continue
            entry_fields = LIST_ENTRY_FIELDS.get((mod, field)) if mod is not None else None
            for i, entry in enumerate(holder[field] if entry_fields else []):
                if not isinstance(entry, dict):
                    out.append(f"{where}[{i}] (an object)")
                    continue
                # Key presence, not a non-null value: an explicit null read as a facility without a
                # country or a collaborator without a name.
                out += [
                    f"{where}[{i}].{name} (a string)"
                    for name in entry_fields or ()
                    if name in entry and not isinstance(entry[name], str)
                ]
        # normalize_phase turned any entry into text with str() and kept any token, so [7], [null] or
        # ["PHASE_3"] became a phase stratum of its own. Phases must be CT.gov's values (CTGOV_PHASES).
        design = protocol.get("designModule")
        if isinstance(design, dict) and "phases" in design:
            phases = design["phases"]
            if not (isinstance(phases, list) and all(isinstance(p, str) and p in CTGOV_PHASES for p in phases)):
                out.append("designModule.phases (a list of CT.gov phase values)")
        # The trial's key in every published file: a number such as 123 became the string "123".
        ident = protocol.get("identificationModule")
        if isinstance(ident, dict):
            nct = ident.get("nctId")
            if not (isinstance(nct, str) and _NCT_ID_RX.fullmatch(nct)):
                out.append("identificationModule.nctId (NCT followed by eight digits)")
    results = study.get("resultsSection")
    if isinstance(results, dict) and "baselineCharacteristicsModule" in results:
        root = "resultsSection.baselineCharacteristicsModule"
        module = results["baselineCharacteristicsModule"]
        if not isinstance(module, dict):
            return out + [f"{root} (an object)"]
        for name, fields in BASELINE_SHAPE.items():
            if name in module:
                out += _object_list_problems(module[name], f"{root}.{name}", fields)
    return out


def _require_weekly_phases(records: list[dict[str, Any]], source: str) -> None:
    """Refuse weekly full records whose `phase` is not CT.gov phase values.

    The weekly extractor writes a record's phases as one string, CT.gov's values joined with ", "
    ("PHASE1, PHASE2"), or "" when none is stated; all 80,890 records of the 2026-11-08 extraction
    have that form. These records never pass `_require_complete_records`, so a hand-built or edited
    one with "phase": "BANANA" became a phase stratum of its own. The value must be exactly "" or,
    split the way `normalize_phase` splits it, consist only of `CTGOV_PHASES` values: an empty piece
    (`","`, `" / "`, `"PHASE1,,PHASE2"`) is refused too, since `normalize_phase` would drop it and read
    a separator-only value as no phase stated. A missing key still reads as no phase.
    """
    bad: list[str] = []
    for idx, rec in enumerate(records):
        if "phase" not in rec:
            continue
        phase = rec["phase"]
        if not isinstance(phase, str):
            pieces = None
        else:
            pieces = [] if phase == "" else [t.strip() for t in re.split(r"[,;/]", phase)]
        if pieces is None or any(t not in CTGOV_PHASES for t in pieces):
            bad.append(f"{rec.get('nct_id') or f'index {idx}'} has phase {phase!r}")
    if bad:
        raise ValueError(
            f"{source}: {len(bad)} of {len(records)} weekly records carry a phase outside CT.gov's values "
            f"(first: {bad[0]})"
        )


def _require_complete_records(studies: list[dict[str, Any]], source: str) -> None:
    """Refuse raw API v2 study records that are incomplete (see the notes above).

    A record must carry the ``REQUIRED_PROTOCOL_MODULES``, the ``REQUIRED_RECORD_FIELDS`` inside them,
    a non-empty ``conditionsModule.conditions`` list and a lead sponsor with its
    ``REQUIRED_LEAD_SPONSOR_FIELDS``, no part of the wrong type (``_mistyped_parts``), and a boolean
    top-level ``hasResults``, with a non-empty
    ``resultsSection`` whenever it is true. A record with ``hasResults: true`` but no results section
    was projected or truncated: built from it, its baseline table would read as never posted
    (``no_results_posted``) while the contract passed. Same rule as the feasibility benchmarks (#107).
    """
    incomplete: dict[str, list[str]] = {}
    for idx, study in enumerate(studies):
        protocol = study.get("protocolSection")
        key = _extract_core_metadata(study)["nct_id"] or f"index {idx}"
        if not isinstance(protocol, dict):
            incomplete[key] = ["protocolSection"]
            continue
        missing = [m for m in REQUIRED_PROTOCOL_MODULES if not isinstance(protocol.get(m), dict)]
        missing += [
            f"{mod}.{field}" for mod, field in REQUIRED_RECORD_FIELDS
            if isinstance(protocol.get(mod), dict)
            and not (isinstance(protocol[mod].get(field), str) and protocol[mod][field].strip())
        ]
        missing += _missing_condition_and_sponsor_payloads(protocol)
        missing += _mistyped_parts(study)
        has_results = study.get("hasResults")
        if not isinstance(has_results, bool):
            missing.append("hasResults (a boolean)")
        elif has_results:
            results_sec = study.get("resultsSection")
            if not isinstance(results_sec, dict) or not results_sec:
                missing.append("resultsSection (hasResults is true)")
        elif "resultsSection" in study:
            # The flag says no results were posted, yet a section is present. demographics.
            # is_results_posted reads any dict-valued section, an empty {} included, as posted
            # results, so the record is contradictory and refused. All 822 stored unprojected
            # records with hasResults false lack the key altogether.
            missing.append("resultsSection (hasResults is false)")
        if missing:
            incomplete[key] = missing
    if incomplete:
        first = next(iter(incomplete))
        raise ValueError(
            f"{source}: {len(incomplete)} of {len(studies)} study records lack required protocol modules "
            f"or fields (first: {first} lacks {', '.join(incomplete[first])}); these look field-projected or "
            "truncated, so the build is refused rather than reading the gaps as absent data"
        )


def _requested_nct_ids(envelope: dict[str, Any]) -> list[str] | None:
    """NCT ids a cached request asked for: its ``ids`` field, else the ``url``'s
    ``filter.ids`` or its single-study path (``/studies/NCT########``)."""
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
        if _NCT_ID_RX.fullmatch(last):
            return [last]
    return None


def _response_log_studies(envelope: dict[str, Any], where: str) -> list[dict[str, Any]]:
    """Studies from one cached CT.gov API v2 response line, refusing a failed, altered or short one.

    Method (as feasibility benchmarks' ``_response_envelope_studies``): a
    cached-response log stores one line per request with its ``status``,
    ``body_sha256`` and unparsed ``body``, and the requested ids in ``ids``
    and/or the ``url``'s ``filter.ids``. A request made with a ``fields=``
    projection is refused outright: its records lack the modules these
    benchmarks read. (The audit caches under
    ``docs/audits/outcomes_results_structure/`` are such projections and are not
    DAP inputs.) A line whose request failed,
    whose body is null, blank, altered or holds no study, or that answered
    fewer or other trials than it asked for would otherwise contribute nothing
    with nothing said while the input sha256 still covers it. So ``status``
    must be 200, ``body_sha256`` (when present) must match the body text, the
    body must hold at least one study object, every study a valid NCT id, and
    the returned ids must equal the requested ids: none missing, extra or
    repeated. A line naming no requested ids is refused, as a short answer
    could not be detected.
    """
    status = envelope.get("status")
    if status != 200:
        raise ValueError(
            f"{where}: cached response status {status!r}, not 200; a failed request is not an empty batch"
        )
    url = envelope.get("url")
    projected = parse_qs(urlsplit(url).query).get("fields") if isinstance(url, str) else None
    if projected:
        raise ValueError(
            f"{where}: response was requested with fields={','.join(projected)}; a field-projected record "
            "lacks the modules these benchmarks read (conditions, sponsors, locations, derivedSection) and "
            "would publish every trial as Other / no listed facilities / no sponsor. Use complete study records."
        )
    body = envelope.get("body")
    want_sha = envelope.get("body_sha256")
    if want_sha is not None:
        if not isinstance(body, str) or hashlib.sha256(body.encode("utf-8")).hexdigest() != want_sha:
            raise ValueError(f"{where}: response body does not match its body_sha256")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError as exc:
            raise ValueError(f"{where}: response body is not JSON") from exc
    if isinstance(body, dict) and "protocolSection" in body:
        studies = [body]
    elif isinstance(body, dict) and isinstance(body.get("studies"), list):
        studies = _require_study_objects(body["studies"], f"{where}: body.studies")
    else:
        studies = []
    if not studies:
        raise ValueError(f"{where}: response body holds no study ({type(body).__name__})")

    requested = _requested_nct_ids(envelope)
    if requested is None:
        raise ValueError(
            f"{where}: response names no requested NCT ids (no ids, no filter.ids in url), "
            "so a response missing trials could not be detected"
        )
    returned = [_extract_core_metadata(s)["nct_id"] for s in studies]
    problems: list[str] = []
    if any(not _NCT_ID_RX.fullmatch(n) for n in returned):
        problems.append("a study without a valid NCT id")
    if len(set(returned)) != len(returned):
        problems.append("repeated NCT ids")
    missing = sorted(set(requested) - set(returned))
    extra = sorted(set(returned) - set(requested))
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


def load_study_records(
    path: Path,
    *,
    source_kind: str = "auto",
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load study records from weekly full-record JSON(.gz), a directory of raw
    CT.gov v2 JSON(.gz) files, or a JSONL(.gz) archive of CT.gov v2 studies /
    response logs.

    Refuses ``demographics.part*.json.gz`` / ``studies_tab.part*.json.gz`` site
    projections outright per ``AGENTS.md``.
    """
    if _SITE_PART_NAME_RX.match(path.name):
        raise ValueError(
            f"{path.name}: site dataset parts cannot substitute for full study records"
        )
    if not path.exists():
        if source_kind == "full_records":
            full_records.load(path)
        raise ValueError(f"{path}: input path does not exist")

    if path.is_dir():
        candidates = sorted(
            p for p in path.iterdir()
            if p.is_file() and (p.name.endswith(".json") or p.name.endswith(".json.gz"))
            and not _SITE_PART_NAME_RX.match(p.name)
        )
        if not candidates:
            raise ValueError(f"{path}: directory contains no .json or .json.gz study files")
        h = hashlib.sha256()
        studies: list[dict[str, Any]] = []
        for fpath in candidates:
            raw = fpath.read_bytes()
            h.update(fpath.name.encode("utf-8") + b"\0" + raw + b"\0")
            text = gzip.decompress(raw).decode("utf-8") if fpath.name.endswith(".gz") else raw.decode("utf-8")
            doc = json.loads(text)
            if isinstance(doc, dict) and isinstance(doc.get("study"), dict):
                doc = doc["study"]
            if not isinstance(doc, dict):
                raise ValueError(f"{fpath}: expected study JSON object")
            studies.append(doc)
        _require_complete_records(studies, str(path))
        if limit is not None and limit > 0:
            studies = studies[:limit]
        return studies, {
            "input_format": "records_directory",
            "path": path.name,
            "n_files": len(candidates),
            "n_records": len(studies),
            "sha256": h.hexdigest(),
            "extracted_at": None,
            "pipeline_commit": None,
            "limit_applied": limit,
        }

    if source_kind == "full_records":
        recs, extracted_at, pipeline_commit = full_records.load(path)
        recs = _require_study_objects(recs, f"{path}: data")
        _require_weekly_phases(recs, f"{path}: data")
        digest = sha256_file(path)
        if limit is not None and limit > 0:
            recs = recs[:limit]
        return recs, {
            "input_format": "full_records",
            "path": path.name,
            "n_records": len(recs),
            "sha256": digest,
            "extracted_at": extracted_at,
            "pipeline_commit": pipeline_commit,
            "limit_applied": limit,
        }

    digest = sha256_file(path)
    is_jsonl = path.name.endswith(".jsonl") or path.name.endswith(".jsonl.gz")
    opener = gzip.open if path.name.endswith(".gz") else open

    if is_jsonl:
        first_line_by_nct: dict[str, int] = {}
        ordered: list[dict[str, Any]] = []
        # Read in binary mode so line splitting occurs strictly on 0x0A (b"\n"),
        # never splitting mid-string on U+2028, U+2029, \x85, or bare \r.
        with opener(path, "rb") as fh:
            for line_no, raw_line in enumerate(fh, start=1):
                if not raw_line.strip():
                    continue
                obj = json.loads(raw_line.decode("utf-8"))
                if not isinstance(obj, dict):
                    raise ValueError(f"{path}:{line_no}: expected JSON object")
                # Frozen CT.gov API v2 response-log lines (`{"body": ...}`): every
                # line must be a whole, successful answer (_response_log_studies),
                # and a trial returned by two lines is refused, not deduplicated.
                if "body" in obj and "protocolSection" not in obj and "nct_id" not in obj:
                    for s in _response_log_studies(obj, f"{path}:{line_no}"):
                        nct = _extract_core_metadata(s)["nct_id"]
                        if nct in first_line_by_nct:
                            raise ValueError(
                                f"{path}:{line_no}: {nct} was already returned on line {first_line_by_nct[nct]}"
                            )
                        first_line_by_nct[nct] = line_no
                        ordered.append(s)
                    continue
                ordered.append(obj)
        if not ordered:
            raise ValueError(f"{path}: holds no study records")
        _require_complete_records(ordered, str(path))
        if limit is not None and limit > 0:
            ordered = ordered[:limit]
        return ordered, {
            "input_format": "jsonl_studies",
            "path": path.name,
            "n_records": len(ordered),
            "sha256": digest,
            "extracted_at": None,
            "pipeline_commit": None,
            "limit_applied": limit,
        }

    with opener(path, "rt", encoding="utf-8") as fh:
        container = json.load(fh)

    if isinstance(container, dict) and "data" in container:
        recs, extracted_at, pipeline_commit = full_records.load(path)
        recs = _require_study_objects(recs, f"{path}: data")
        _require_weekly_phases(recs, f"{path}: data")
        if limit is not None and limit > 0:
            recs = recs[:limit]
        return recs, {
            "input_format": "full_records",
            "path": path.name,
            "n_records": len(recs),
            "sha256": digest,
            "extracted_at": extracted_at,
            "pipeline_commit": pipeline_commit,
            "limit_applied": limit,
        }

    if isinstance(container, dict) and isinstance(container.get("studies"), list):
        studies_list = _require_study_objects(container["studies"], f"{path}: studies")
    elif isinstance(container, dict) and isinstance(container.get("records"), list):
        studies_list = _require_study_objects(container["records"], f"{path}: records")
    elif isinstance(container, dict) and "protocolSection" in container:
        studies_list = [container]
    elif isinstance(container, list):
        studies_list = _require_study_objects(container, f"{path}: top-level list")
    else:
        raise ValueError(f"{path}: unrecognized study container format")

    if not studies_list:
        raise ValueError(f"{path}: holds no study records")
    _require_complete_records(studies_list, str(path))
    if limit is not None and limit > 0:
        studies_list = studies_list[:limit]
    return studies_list, {
        "input_format": "json_studies",
        "path": path.name,
        "n_records": len(studies_list),
        "sha256": digest,
        "extracted_at": container.get("extracted_at") if isinstance(container, dict) else None,
        "pipeline_commit": container.get("pipeline_commit") if isinstance(container, dict) else None,
        "limit_applied": limit,
    }


def condition_mesh_line_problem(row: dict[str, Any]) -> str | None:
    """Why one sidecar line breaks the ``src/condition_mesh.py`` contract, or ``None``.

    The contract is what ``select_condition_mesh`` writes: ``schema_version``
    is ``cmesh.SCHEMA_VERSION``; ``meshes`` and ``ancestors`` are both present,
    each null or a list of ``{"id": str, "term": str}`` (``cmesh._terms``, the
    producer's own rule); and the state matches its payload:
      indexed      ``meshes`` holds at least one descriptor
      not_indexed  ``meshes`` is null or empty
      no_module,
      no_derived   ``meshes`` and ``ancestors`` are both null
      malformed    a non-empty ``problem`` string; either list may be null
    Only ``malformed`` carries ``problem``. A line that disagrees would be
    counted under its declared state while contributing other descriptors.
    """
    if row.get("schema_version") != cmesh.SCHEMA_VERSION:
        return f"schema_version {row.get('schema_version')!r} is not {cmesh.SCHEMA_VERSION!r}"
    for key in ("meshes", "ancestors"):
        if key not in row:
            return f"no {key} key (null records an absent list)"
        if row[key] is not None and cmesh._terms(row[key]) is None:
            return f"{key} is neither null nor a list of id/term pairs"
    state = row["state"]
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


def load_condition_mesh(
    path: Path | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """Load optional ``condition_mesh.jsonl.gz`` sidecar keyed by ``nct_id``.

    Every line must keep the ``src/condition_mesh.py`` contract
    (``condition_mesh_line_problem``); the first that does not raises.
    Each entry keeps the line's ``MESH_RUN_STAMP_KEYS`` (``None`` when a line
    carries none) next to its MeSH fields; ``check_condition_mesh_provenance``
    checks them against the study records before the build uses the sidecar.
    """
    if path is None:
        return {}, None
    if not path.exists():
        raise ValueError(f"{path}: condition_mesh file not found")
    digest = sha256_file(path)
    opener = gzip.open if path.name.endswith(".gz") else open
    by_nct: dict[str, dict[str, Any]] = {}
    state_counts: Counter[str] = Counter()
    with opener(path, "rb") as fh:
        for line_no, raw_line in enumerate(fh, start=1):
            if not raw_line.strip():
                continue
            row = json.loads(raw_line.decode("utf-8"))
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: expected JSON object")
            nct_id = row.get("nct_id")
            state = row.get("state")
            if not isinstance(nct_id, str) or not nct_id:
                raise ValueError(f"{path}:{line_no}: missing nct_id")
            if state not in cmesh.STATES:
                raise ValueError(f"{path}:{line_no}: invalid condition_mesh state {state!r}")
            line_problem = condition_mesh_line_problem(row)
            if line_problem is not None:
                raise ValueError(
                    f"{path}:{line_no}: {nct_id}: condition_mesh line breaks the "
                    f"src/condition_mesh.py contract: {line_problem}"
                )
            if nct_id in by_nct:
                raise ValueError(f"{path}:{line_no}: duplicate condition_mesh nct_id {nct_id}")
            by_nct[nct_id] = {
                "state": state,
                "meshes": row.get("meshes"),
                "ancestors": row.get("ancestors"),
                **({"problem": row["problem"]} if "problem" in row else {}),
                **{k: row.get(k) for k in MESH_RUN_STAMP_KEYS},
            }
            state_counts[state] += 1
    return by_nct, {
        "path": path.name,
        "sha256": digest,
        "n_lines": len(by_nct),
        "state_counts": {s: state_counts.get(s, 0) for s in cmesh.STATES},
    }


def check_condition_mesh_provenance(
    mesh_sidecar: dict[str, dict[str, Any]],
    mesh_meta: dict[str, Any] | None,
    records: list[dict[str, Any]],
    study_meta: dict[str, Any],
) -> dict[str, Any] | None:
    """Refuse a ``condition_mesh`` sidecar that does not belong to the study records.

    Returns ``mesh_meta`` extended with the sidecar's run stamps and the check
    applied (recorded as ``run.json`` ``inputs.condition_mesh``), or ``None``
    when no sidecar was supplied; raises ``ValueError`` naming the mismatch.

    Every input format:
      - coverage: the sidecar holds a line for every cohort trial. A missing
        line would otherwise fall back, unannounced, to the record's own
        ``derivedSection`` or to ``mesh_sidecar_not_provided``. Lines for
        trials outside the cohort are allowed (``--limit`` cuts the records,
        not the sidecar) and counted in ``n_lines_outside_cohort``.
      - one run: every line carries the same ``MESH_RUN_STAMP_KEYS`` triple;
        a file mixing extraction runs is refused.

    Weekly full records (``input_format == "full_records"``) also carry the
    pull's stamps: ``src/extract_all.py`` writes the same ``extracted_at`` and
    ``pipeline_commit`` on the ``demographics.json`` container and on every
    sidecar line, and the sex/gender write-back keeps the container's. So
    every line's pair must equal the records' pair; a sidecar from another
    week or commit is refused, and so are records missing either stamp, since
    nothing could then show the two files are one pull. The container has no
    ``snapshot_date``; the sidecar's is recorded, not matched.

    Other inputs (a records directory, JSON or JSONL archives of API v2
    studies) carry no extraction stamps to match, so only coverage and the
    one-run check apply; the sidecar's stamps are recorded as found (``None``
    when its lines carry none) and ``provenance_reason`` says why.
    """
    if mesh_meta is None:
        return None
    where = mesh_meta.get("path")

    # Coverage. Empty nct_ids are skipped here; build_benchmarks refuses them.
    cohort = sorted({_extract_core_metadata(r)["nct_id"] for r in records} - {""})
    missing = [n for n in cohort if n not in mesh_sidecar]
    if missing:
        raise ValueError(
            f"{where}: condition_mesh sidecar has no line for {len(missing)} of "
            f"{len(cohort)} cohort trials (first: {', '.join(missing[:5])}); "
            "it must cover every study record it annotates"
        )

    weekly = study_meta.get("input_format") == "full_records"
    if weekly:
        rec_pair = (study_meta.get("extracted_at"), study_meta.get("pipeline_commit"))
        if not all(isinstance(s, str) and s for s in rec_pair):
            raise ValueError(
                f"{study_meta.get('path')}: full records carry no extracted_at/pipeline_commit "
                f"(got {rec_pair!r}); cannot show the condition_mesh sidecar {where} is from the same pull"
            )
        stale = [
            n for n in sorted(mesh_sidecar)
            if (mesh_sidecar[n].get("extracted_at"), mesh_sidecar[n].get("pipeline_commit")) != rec_pair
        ]
        if stale:
            first = mesh_sidecar[stale[0]]
            raise ValueError(
                f"{where}: {len(stale)} of {len(mesh_sidecar)} condition_mesh lines are not from the "
                f"study records' pull: {stale[0]} has extracted_at={first.get('extracted_at')!r}, "
                f"pipeline_commit={first.get('pipeline_commit')!r}, but {study_meta.get('path')} has "
                f"extracted_at={rec_pair[0]!r}, pipeline_commit={rec_pair[1]!r}"
            )

    triples = sorted(
        {tuple(entry.get(k) for k in MESH_RUN_STAMP_KEYS) for entry in mesh_sidecar.values()},
        key=repr,
    )
    if len(triples) != 1:
        raise ValueError(
            f"{where}: condition_mesh lines carry {len(triples)} different "
            f"({', '.join(MESH_RUN_STAMP_KEYS)}) stamps, e.g. {triples[0]!r} and {triples[1]!r}; "
            "a sidecar must come from one extraction run"
        )
    stamps = dict(zip(MESH_RUN_STAMP_KEYS, triples[0]))

    if weekly:
        check = "full_records_run_stamps_matched_and_cohort_covered"
        reason = "every line's extracted_at and pipeline_commit equal the full records' container stamps"
    else:
        check = "cohort_covered_and_one_run_only"
        reason = (
            f"{study_meta.get('input_format')} input carries no extraction stamps to match; "
            "checked that every cohort trial has a line and all lines carry one run's stamps"
        )
    return {
        **mesh_meta,
        **stamps,
        "provenance_check": check,
        "provenance_reason": reason,
        "n_cohort_trials": len(cohort),
        "n_lines_outside_cohort": len(set(mesh_sidecar) - set(cohort)),
    }


def _resolve_trial_mesh(
    record: dict[str, Any],
    nct_id: str,
    mesh_sidecar: dict[str, dict[str, Any]],
    has_sidecar: bool,
) -> dict[str, Any]:
    """Resolve MeSH descriptors for one trial from sidecar or raw ``derivedSection``."""
    if nct_id in mesh_sidecar:
        entry = mesh_sidecar[nct_id]
        return {
            "source": "condition_mesh_sidecar",
            "state": entry["state"],
            "meshes": entry.get("meshes"),
            "ancestors": entry.get("ancestors"),
            **({"problem": entry["problem"]} if "problem" in entry else {}),
        }
    if "derivedSection" in record or "protocolSection" in record:
        selected = cmesh.select_condition_mesh(record)
        return {
            "source": "study_derived_section",
            "state": selected["state"],
            "meshes": selected["meshes"],
            "ancestors": selected["ancestors"],
            **({"problem": selected["problem"]} if "problem" in selected else {}),
        }
    return {
        "source": "sidecar_missing_for_trial" if has_sidecar else "sidecar_not_provided",
        "state": "mesh_sidecar_not_provided",
        "meshes": None,
        "ancestors": None,
    }


def attribute_sponsors(
    core_records: list[dict[str, Any]],
    rules_path: Path = DEFAULT_SPONSOR_RULES_PATH,
) -> tuple[dict[str, dict[str, Any]], Any, dict[str, Any]]:
    """Attribute lead and collaborator sponsors via ``sponsors/`` with zero substring matching.

    Strictly preserves the three disjoint review states (``attributed``,
    ``reviewed_excluded``, ``unreviewed``) from ``sponsors.audit_states.partition``.
    """
    rules_sha = sha256_file(rules_path)
    rules = sponsor_roles.load_rules(str(rules_path))
    frame, adapter_log = adapter.records_to_frame(core_records)
    states = audit_states.partition(frame, rules)
    index = sponsor_roles.build_index(frame, rules)

    matched = audit_states.rule_matches(frame, rules)
    canon_by_lit: dict[str, list[str]] = {}
    entity_by_lit: dict[str, str] = {}
    shared_by_lit: dict[str, bool] = {}
    for _, mrow in matched.iterrows():
        lit_name = str(mrow["name"])
        canon = str(mrow["canonical"])
        canon_list = canon_by_lit.setdefault(lit_name, [])
        if canon not in canon_list:
            canon_list.append(canon)
        entity_by_lit[lit_name] = sponsor_roles.parsed_entity(lit_name)
        if str(mrow["shared"]) == "yes":
            shared_by_lit[lit_name] = True
    for lit_list in canon_by_lit.values():
        lit_list.sort()

    def _state_of(literal: str) -> str:
        if literal in states["attributed"]:
            return "attributed"
        if literal in states["reviewed_excluded"]:
            return "reviewed_excluded"
        return "unreviewed"

    # Build trial-level lookup from `index` so `current_owner` and `as_registered`
    # sets match `sponsor_roles.trials()` 1-for-1.
    owner_by_nct_role: dict[tuple[str, str], set[str]] = {}
    entity_by_nct_role: dict[tuple[str, str], set[str]] = {}
    for _, idx_row in index.iterrows():
        nct = str(idx_row["nct_id"])
        role = str(idx_row["role"])
        owner_by_nct_role.setdefault((nct, role), set()).add(str(idx_row["canonical"]))
        entity_by_nct_role.setdefault((nct, role), set()).add(str(idx_row["entity"]))

    per_trial: dict[str, dict[str, Any]] = {}
    lead_state_counts: Counter[str] = Counter()
    for rec in core_records:
        nct_id = rec["nct_id"]
        lead_lit_raw = rec.get("lead_sponsor_name")
        lead_lit = "" if lead_lit_raw is None else str(lead_lit_raw)
        lead_state = _state_of(lead_lit)
        lead_state_counts[lead_state] += 1
        lead_canons = list(canon_by_lit.get(lead_lit, []))
        lead_entities = [entity_by_lit[lead_lit]] if lead_lit in entity_by_lit else []

        collab_entries: list[dict[str, Any]] = []
        for collab in rec.get("collaborators") or []:
            cname_raw = collab.get("name")
            if cname_raw is None or str(cname_raw).strip() == "":
                continue
            cname = str(cname_raw)
            cstate = _state_of(cname)
            collab_entries.append(
                {
                    "literal_name": cname,
                    "agency_class": str(collab.get("class") or ""),
                    "review_state": cstate,
                    "canonical_companies": list(canon_by_lit.get(cname, [])),
                    "registered_entities": [entity_by_lit[cname]] if cname in entity_by_lit else [],
                    "shared": bool(shared_by_lit.get(cname, False)),
                }
            )

        owners_lead = sorted(owner_by_nct_role.get((nct_id, "lead"), set()))
        owners_collab = sorted(owner_by_nct_role.get((nct_id, "collaborator"), set()))
        owners_any = sorted(set(owners_lead) | set(owners_collab))

        ents_lead = sorted(entity_by_nct_role.get((nct_id, "lead"), set()))
        ents_collab = sorted(entity_by_nct_role.get((nct_id, "collaborator"), set()))
        ents_any = sorted(set(ents_lead) | set(ents_collab))

        per_trial[nct_id] = {
            "lead": {
                "literal_name": lead_lit,
                "agency_class": str(rec.get("sponsor_class") or ""),
                "review_state": lead_state,
                "canonical_companies": lead_canons,
                "registered_entities": lead_entities,
                "shared": bool(shared_by_lit.get(lead_lit, False)),
            },
            "collaborators": collab_entries,
            "current_owner": {
                "any": owners_any,
                "lead": owners_lead,
                "collaborator": owners_collab,
            },
            "as_registered": {
                "any": ents_any,
                "lead": ents_lead,
                "collaborator": ents_collab,
            },
        }

    meta = {
        "rules_file": rules_path.name,
        "rules_sha256": rules_sha,
        "matching_method": "list_driven_exact_and_subsidiary_rules_zero_substring_matching",
        "adapter_log": adapter_log.to_dict(),
        "literal_counts_by_review_state": {
            "attributed": len(states["attributed"]),
            "reviewed_excluded": len(states["reviewed_excluded"]),
            "unreviewed": len(states["unreviewed"]),
        },
        "trial_lead_counts_by_review_state": {
            s: lead_state_counts.get(s, 0) for s in SPONSOR_REVIEW_STATES
        },
        "n_distinct_canonical_companies": int(index["canonical"].nunique()) if not index.empty else 0,
        "n_distinct_registered_entities": int(index["entity"].nunique()) if not index.empty else 0,
        "n_trials_any_attributed": int(index["nct_id"].nunique()) if not index.empty else 0,
    }
    return per_trial, index, meta


def summarize_share_distribution(
    entries: list[tuple[float, int, int]],
    *,
    min_cell_trials: int = DEFAULT_MIN_CELL_TRIALS,
) -> dict[str, Any]:
    """Compute trial-equally-weighted percentiles (primary) and participant-weighted share (secondary).

    ``entries`` holds ``(trial_share, category_numerator, trial_denominator)``
    for each qualifying trial in the cell. When ``len(entries) < min_cell_trials``,
    percentiles and means are withheld as ``None`` while exact ``n_trials`` and
    ``withheld_small_cell = True`` are reported.
    """
    if min_cell_trials < 1:
        raise ValueError(f"min_cell_trials must be >= 1, got {min_cell_trials}")
    n_trials = len(entries)
    n_participants = sum(den for _, _, den in entries)
    if n_trials < min_cell_trials:
        return {
            "n_trials": n_trials,
            "n_participants": n_participants,
            "min_cell_trials": min_cell_trials,
            "withheld_small_cell": True,
            "percentiles": None,
            "trial_equal_mean": None,
            "participant_weighted_share": None,
        }

    arr = np.array([val for val, _, _ in entries], dtype=float)
    p10, p25, p50, p75, p90 = np.percentile(arr, [10, 25, 50, 75, 90], method="linear")
    tot_num = sum(num for _, num, _ in entries)
    weighted: float | None = (tot_num / n_participants) if n_participants > 0 else None
    return {
        "n_trials": n_trials,
        "n_participants": n_participants,
        "min_cell_trials": min_cell_trials,
        "withheld_small_cell": False,
        "percentiles": {
            "p10": round(float(p10), 6),
            "p25": round(float(p25), 6),
            "p50": round(float(p50), 6),
            "p75": round(float(p75), 6),
            "p90": round(float(p90), 6),
        },
        "trial_equal_mean": round(float(arr.mean()), 6),
        "participant_weighted_share": round(weighted, 6) if weighted is not None else None,
    }


def _pool_trial_group(
    trials: list[dict[str, Any]],
    *,
    min_cell_trials: int,
) -> dict[str, Any]:
    """Pool race, ethnicity, sex/gender, and reporting status distributions for a group of trials."""
    n_total = len(trials)
    n_posted = sum(1 for t in trials if t["demographics"]["results_posted"])

    race_status_counts = {
        s: sum(1 for t in trials if t["demographics"]["race_table_status"] == s)
        for s in TABLE_STATUSES
    }
    eth_status_counts = {
        s: sum(1 for t in trials if t["demographics"]["ethnicity_table_status"] == s)
        for s in TABLE_STATUSES
    }
    sg_status_counts = {
        s: sum(1 for t in trials if t["demographics"]["sex_report_status"] == s)
        for s in SEX_GENDER_STATES
    }

    # Pool OMB-compatible race distributions (quarantined combined tables are
    # strictly excluded and tallied in `n_combined_race_ethnicity_quarantined`).
    race_denom_a_trials = [
        t for t in trials
        if t["demographics"]["race"]["usable_for_omb_pooling"]
        and isinstance(t["demographics"]["race"].get("of_baseline_total"), dict)
    ]
    race_denom_b_trials = [
        t for t in race_denom_a_trials
        if isinstance(t["demographics"]["race"].get("of_classified"), dict)
    ]

    race_of_baseline: dict[str, dict[str, Any]] = {}
    race_of_classified: dict[str, dict[str, Any]] = {}
    for cat in RACE_CLASSIFIED_KEYS:
        a_entries = [
            (
                float(t["demographics"]["race"]["of_baseline_total"][cat]["floor"]),
                int(t["demographics"]["race"]["counts"][cat]),
                int(t["demographics"]["race"]["baseline_total"]),
            )
            for t in race_denom_a_trials
        ]
        b_entries = [
            (
                float(t["demographics"]["race"]["of_classified"][cat]),
                int(t["demographics"]["race"]["counts"][cat]),
                int(t["demographics"]["race"]["classified_total"]),
            )
            for t in race_denom_b_trials
        ]
        race_of_baseline[cat] = summarize_share_distribution(a_entries, min_cell_trials=min_cell_trials)
        race_of_classified[cat] = summarize_share_distribution(b_entries, min_cell_trials=min_cell_trials)

    # Pool ethnicity distributions: `single_category_ethnicity` participates in
    # Denominator A (floor) and is strictly withheld from Denominator B.
    eth_denom_a_trials = [
        t for t in trials
        if t["demographics"]["ethnicity"]["usable_for_omb_pooling"]
        and isinstance(t["demographics"]["ethnicity"].get("of_baseline_total"), dict)
    ]
    eth_denom_b_trials = [
        t for t in eth_denom_a_trials
        if isinstance(t["demographics"]["ethnicity"].get("of_classified"), dict)
    ]

    eth_of_baseline: dict[str, dict[str, Any]] = {}
    eth_of_classified: dict[str, dict[str, Any]] = {}
    for cat in ETHNICITY_CLASSIFIED_KEYS:
        a_entries = [
            (
                float(t["demographics"]["ethnicity"]["of_baseline_total"][cat]["floor"]),
                int(t["demographics"]["ethnicity"]["counts"][cat]),
                int(t["demographics"]["ethnicity"]["baseline_total"]),
            )
            for t in eth_denom_a_trials
        ]
        b_entries = [
            (
                float(t["demographics"]["ethnicity"]["of_classified"][cat]),
                int(t["demographics"]["ethnicity"]["counts"][cat]),
                int(t["demographics"]["ethnicity"]["classified_total"]),
            )
            for t in eth_denom_b_trials
        ]
        eth_of_baseline[cat] = summarize_share_distribution(a_entries, min_cell_trials=min_cell_trials)
        eth_of_classified[cat] = summarize_share_distribution(b_entries, min_cell_trials=min_cell_trials)

    # Sex composition: participant-count trials with reported_sex and f + m > 0.
    sex_eligible = [
        t for t in trials
        if t["demographics"]["sex_gender"].get("female_share") is not None
    ]
    sex_entries = [
        (
            float(t["demographics"]["sex_gender"]["female_share"]),
            int(round(float(t["demographics"]["sex_gender"]["n_female"] or 0.0))),
            int(
                round(
                    float(t["demographics"]["sex_gender"]["n_female"] or 0.0)
                    + float(t["demographics"]["sex_gender"]["n_male"] or 0.0)
                )
            ),
        )
        for t in sex_eligible
    ]
    female_dist = summarize_share_distribution(sex_entries, min_cell_trials=min_cell_trials)

    # Facility share distribution across country-bearing trials in the group
    fac_entries = [
        (
            float(t["geography"]["us_facility_share"]),
            int(t["geography"]["n_us_facilities"]),
            int(t["geography"]["n_facilities_with_country"]),
        )
        for t in trials
        if t["geography"]["us_facility_share"] is not None
    ]
    us_fac_dist = summarize_share_distribution(fac_entries, min_cell_trials=min_cell_trials)

    return {
        "n_trials": n_total,
        "n_posted_results": n_posted,
        "min_cell_trials": min_cell_trials,
        "withheld_small_cell": n_total < min_cell_trials,
        "unit_basis": UNIT_BASIS,
        "us_facility_share_distribution": us_fac_dist,
        "race": {
            "table_status_counts": race_status_counts,
            "n_combined_race_ethnicity_quarantined": race_status_counts[
                "combined_race_ethnicity_quarantined"
            ],
            "n_pooled_denominator_a": len(race_denom_a_trials),
            "n_pooled_denominator_b": len(race_denom_b_trials),
            "of_baseline_total": race_of_baseline,
            "of_classified": race_of_classified,
        },
        "ethnicity": {
            "table_status_counts": eth_status_counts,
            "n_single_category_ethnicity": eth_status_counts["single_category_ethnicity"],
            "n_pooled_denominator_a": len(eth_denom_a_trials),
            "n_pooled_denominator_b": len(eth_denom_b_trials),
            "of_baseline_total": eth_of_baseline,
            "of_classified": eth_of_classified,
        },
        "sex_gender": {
            "status_counts": sg_status_counts,
            "n_reported_sex_true": sum(
                1 for t in trials if t["demographics"]["sex_gender"].get("reported_sex") is True
            ),
            "n_reported_gender_true": sum(
                1 for t in trials if t["demographics"]["sex_gender"].get("reported_gender") is True
            ),
            "n_participant_count_false": sum(
                1 for t in trials if t["demographics"]["sex_gender"].get("is_participant_count") is False
            ),
            "female_share_of_mf": female_dist,
        },
    }


def _stratum_id(kind: str, parts: Iterable[str | None]) -> str:
    joined = "|".join("" if p is None else p for p in parts)
    digest = hashlib.sha256(f"{kind}|{joined}".encode("utf-8")).hexdigest()[:12]
    return f"stratum:{kind}:{digest}"


def _mesh_display_terms(
    trial_rows: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """One display term per MeSH descriptor ID, and every ID seen under more than one term.

    Counts, per ID, the indexed trials carrying each term. The display term is
    the one most trials carry, ties going to the lexicographically first, so the
    choice is deterministic. IDs carried under several terms are returned with
    all of them, sorted, so the conflict is recorded rather than hidden.
    """
    counts: dict[str, Counter[str]] = {}
    for t in trial_rows:
        if t["indication"]["mesh_state"] == "indexed" and t["indication"]["meshes"]:
            for mid, mterm in {(m["id"], m["term"]) for m in t["indication"]["meshes"]}:
                counts.setdefault(mid, Counter())[mterm] += 1
    display = {
        mid: sorted(c.items(), key=lambda item: (-item[1], item[0]))[0][0]
        for mid, c in counts.items()
    }
    conflicts = {mid: sorted(c) for mid, c in sorted(counts.items()) if len(c) > 1}
    return display, conflicts


def _extract_medians_from_pooled(pooled: dict[str, Any]) -> tuple[dict[str, float], dict[str, float], float | None]:
    """Extract non-withheld ``p50`` medians from a pooled stratum dict."""
    race_meds: dict[str, float] = {}
    for cat, dist in pooled["race"]["of_baseline_total"].items():
        if not dist["withheld_small_cell"] and isinstance(dist.get("percentiles"), dict):
            race_meds[cat] = float(dist["percentiles"]["p50"])
    eth_meds: dict[str, float] = {}
    for cat, dist in pooled["ethnicity"]["of_baseline_total"].items():
        if not dist["withheld_small_cell"] and isinstance(dist.get("percentiles"), dict):
            eth_meds[cat] = float(dist["percentiles"]["p50"])
    fem_dist = pooled["sex_gender"]["female_share_of_mf"]
    fem_med: float | None = (
        float(fem_dist["percentiles"]["p50"])
        if not fem_dist["withheld_small_cell"] and isinstance(fem_dist.get("percentiles"), dict)
        else None
    )
    return race_meds, eth_meds, fem_med


def _percentile_rank_against(values: list[float], x: float) -> float:
    """Mid-rank empirical percentile in ``[0.0, 1.0]`` of ``x`` against peer ``values``."""
    n = len(values)
    below = sum(1 for v in values if v < x)
    equal = sum(1 for v in values if v == x)
    return round((below + 0.5 * equal) / n, 6)


def _build_role_scorecard(
    company_trials: list[dict[str, Any]],
    *,
    ref_by_indication_phase: dict[tuple[str, str], dict[str, Any]],
    peer_values_by_indication_phase: dict[tuple[str, str], dict[str, list[float]]],
    min_cell_trials: int,
) -> dict[str, Any]:
    """Build one sponsor role scope (``any``, ``lead``, or ``collaborator``) scorecard."""
    n_trials = len(company_trials)
    footprint_counts = {
        fp: sum(1 for t in company_trials if t["geography"]["site_footprint_class"] == fp)
        for fp in SITE_FOOTPRINT_CLASSES
    }
    footprint_shares = {
        fp: round(cnt / n_trials, 6) if n_trials > 0 else None
        for fp, cnt in footprint_counts.items()
    }
    multinat = [
        t for t in company_trials
        if t["geography"]["site_footprint_class"] == "multinational_with_us_sites"
    ]
    n_multinat = len(multinat)
    bin_counts = {
        b: sum(1 for t in multinat if t["geography"]["us_site_share_bin"] == b)
        for b in US_SITE_SHARE_BINS
    }
    bin_shares = {
        b: round(cnt / n_multinat, 6) if n_multinat > 0 else None
        for b, cnt in bin_counts.items()
    }

    era_counts = {
        e: sum(1 for t in company_trials if t["regulatory_era"]["regulatory_era"] == e)
        for e in REGULATORY_ERAS
    }

    us_site_trials = [
        t for t in company_trials
        if t["geography"]["site_footprint_class"] in ("us_only", "multinational_with_us_sites")
    ]
    posted_us = [t for t in us_site_trials if t["demographics"]["results_posted"]]

    completeness = {
        "n_us_site_trials": len(us_site_trials),
        "n_posted_us_site_trials": len(posted_us),
        "race_by_status": {
            s: sum(1 for t in us_site_trials if t["demographics"]["race_table_status"] == s)
            for s in TABLE_STATUSES
        },
        "ethnicity_by_status": {
            s: sum(1 for t in us_site_trials if t["demographics"]["ethnicity_table_status"] == s)
            for s in TABLE_STATUSES
        },
        "sex_gender_by_status": {
            s: sum(1 for t in us_site_trials if t["demographics"]["sex_report_status"] == s)
            for s in SEX_GENDER_STATES
        },
        "n_posted_us_omitted_race_table": sum(
            1 for t in posted_us if t["demographics"]["us_site_omission_audit"]["omitted_race_table"] is True
        ),
        "n_posted_us_omitted_ethnicity_table": sum(
            1 for t in posted_us if t["demographics"]["us_site_omission_audit"]["omitted_ethnicity_table"] is True
        ),
        "n_posted_us_omitted_race_or_ethnicity_table": sum(
            1 for t in posted_us if t["demographics"]["us_site_omission_audit"]["omitted_race_or_ethnicity_table"] is True
        ),
        "n_posted_us_omitted_sex_table": sum(
            1 for t in posted_us if t["demographics"]["us_site_omission_audit"]["omitted_sex_table"] is True
        ),
        "n_posted_us_reported_sex_true": sum(
            1 for t in posted_us if t["demographics"]["sex_gender"]["reported_sex"] is True
        ),
        "n_posted_us_reported_gender_true": sum(
            1 for t in posted_us if t["demographics"]["sex_gender"]["reported_gender"] is True
        ),
    }

    us_pooled = _pool_trial_group(us_site_trials, min_cell_trials=min_cell_trials)

    # Indication-matched DAP peer benchmarking across US-site trials whose
    # (ontology_primary, phase) reference cell meets `min_cell_trials`.
    indication_benchmarks: dict[str, dict[str, Any]] = {}
    benchmark_targets: list[tuple[str, str, str]] = [
        ("race", cat, f"race:{cat}") for cat in RACE_CLASSIFIED_KEYS
    ] + [
        ("ethnicity", cat, f"ethnicity:{cat}") for cat in ETHNICITY_CLASSIFIED_KEYS
    ] + [("sex_gender", "female", "sex_gender:female")]

    for dim, cat, key_label in benchmark_targets:
        rank_entries: list[tuple[float, int, int]] = []
        delta_vals: list[float] = []
        n_above = 0
        n_below = 0
        n_unsettled = 0
        for t in us_site_trials:
            ip_key = (t["indication"]["primary_condition"], t["phase"])
            ref_cell = ref_by_indication_phase.get(ip_key)
            peer_map = peer_values_by_indication_phase.get(ip_key) or {}
            peers = peer_map.get(key_label) or []
            if ref_cell is None or len(peers) < min_cell_trials:
                continue

            if dim in ("race", "ethnicity"):
                d_block = t["demographics"][dim]
                if not d_block["usable_for_omb_pooling"] or not isinstance(d_block.get("of_baseline_total"), dict):
                    continue
                floor = float(d_block["of_baseline_total"][cat]["floor"])
                ceiling = float(d_block["of_baseline_total"][cat]["ceiling"])
                num = int(d_block["counts"][cat])
                den = int(d_block["baseline_total"])
                peer_p50 = float(ref_cell["pooled"][dim]["of_baseline_total"][cat]["percentiles"]["p50"])
            else:
                sg = t["demographics"]["sex_gender"]
                if sg.get("female_share") is None:
                    continue
                floor = float(sg["female_share"])
                ceiling = floor
                num = int(round(float(sg.get("n_female") or 0.0)))
                den = int(
                    round(float(sg.get("n_female") or 0.0) + float(sg.get("n_male") or 0.0))
                )
                peer_p50 = float(ref_cell["pooled"]["sex_gender"]["female_share_of_mf"]["percentiles"]["p50"])

            rank_entries.append((_percentile_rank_against(peers, floor), num, den))
            delta_vals.append(floor - peer_p50)
            if floor > peer_p50:
                n_above += 1
            elif ceiling < peer_p50:
                n_below += 1
            else:
                n_unsettled += 1

        rank_dist = summarize_share_distribution(rank_entries, min_cell_trials=min_cell_trials)
        if len(delta_vals) >= min_cell_trials:
            d_arr = np.array(delta_vals, dtype=float)
            dp10, dp25, dp50, dp75, dp90 = np.percentile(d_arr, [10, 25, 50, 75, 90], method="linear")
            delta_pcts: dict[str, float] | None = {
                "p10": round(float(dp10), 6),
                "p25": round(float(dp25), 6),
                "p50": round(float(dp50), 6),
                "p75": round(float(dp75), 6),
                "p90": round(float(dp90), 6),
            }
        else:
            delta_pcts = None

        indication_benchmarks[key_label] = {
            "n_benchmarked_trials": len(rank_entries),
            "min_cell_trials": min_cell_trials,
            "withheld_small_cell": len(rank_entries) < min_cell_trials,
            "n_above_peer_median_settled": n_above,
            "n_below_peer_median_settled": n_below,
            "n_direction_unsettled_or_equal": n_unsettled,
            "peer_percentile_rank": rank_dist["percentiles"],
            "delta_vs_peer_median": delta_pcts,
        }

    return {
        "n_trials": n_trials,
        "unit_basis": UNIT_BASIS,
        "footprint_mix": {
            "counts": footprint_counts,
            "shares": footprint_shares,
            "multinational_us_share_bin_counts": bin_counts,
            "multinational_us_share_bin_shares": bin_shares,
        },
        "regulatory_era_counts": era_counts,
        "us_site_reporting_completeness": completeness,
        "us_site_enrollment_distributions": {
            "race": us_pooled["race"],
            "ethnicity": us_pooled["ethnicity"],
            "sex_gender": us_pooled["sex_gender"],
        },
        "indication_matched_benchmarking": indication_benchmarks,
    }


def build_benchmarks(
    records: list[dict[str, Any]],
    *,
    mesh_sidecar: dict[str, dict[str, Any]] | None = None,
    has_mesh_sidecar: bool = False,
    ontology_path: Path = DEFAULT_ONTOLOGY_PATH,
    sponsor_rules_path: Path = DEFAULT_SPONSOR_RULES_PATH,
    min_cell_trials: int = DEFAULT_MIN_CELL_TRIALS,
    snapshot_date: str = "1970-01-01",
) -> dict[str, Any]:
    """Build trial rows, stratified benchmarks, and canonical/registered sponsor scorecards."""
    if not records:
        raise ValueError("records must be non-empty")
    if min_cell_trials < 1:
        raise ValueError(f"min_cell_trials must be >= 1, got {min_cell_trials}")

    classifier = OntologyClassifier(ontology_path)
    sidecar = mesh_sidecar or {}

    core_list: list[dict[str, Any]] = []
    seen_ncts: set[str] = set()
    ordered_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for idx, rec in enumerate(records):
        core = _extract_core_metadata(rec)
        nct_id = core["nct_id"]
        if not nct_id:
            raise ValueError(f"record at index {idx} is missing nct_id / protocolSection.identificationModule.nctId")
        if nct_id in seen_ncts:
            raise ValueError(f"duplicate nct_id {nct_id} in input study records")
        seen_ncts.add(nct_id)
        ordered_pairs.append((core, rec))

    # Deterministic sort by nct_id across the entire build.
    ordered_pairs.sort(key=lambda pair: pair[0]["nct_id"])
    core_list = [pair[0] for pair in ordered_pairs]

    sponsors_by_nct, sponsor_index, sponsor_meta = attribute_sponsors(core_list, sponsor_rules_path)

    trial_rows: list[dict[str, Any]] = []
    for core, rec in ordered_pairs:
        nct_id = core["nct_id"]
        geo = extract_geography(rec)
        demo = extract_demographics(rec, geography=geo, snapshot_date=snapshot_date)
        era = classify_regulatory_era(rec)
        phase = normalize_phase(rec)
        ont = classifier.classify(core["conditions"])
        mesh_info = _resolve_trial_mesh(rec, nct_id, sidecar, has_mesh_sidecar or bool(sidecar))
        mesh_ids = sorted(
            {
                str(m["id"])
                for m in (mesh_info.get("meshes") or [])
                if isinstance(m, dict) and isinstance(m.get("id"), str)
            }
        )
        mesh_terms = [
            {"id": str(m["id"]), "term": str(m["term"])}
            for m in (mesh_info.get("meshes") or [])
            if isinstance(m, dict) and isinstance(m.get("id"), str) and isinstance(m.get("term"), str)
        ]

        trial_rows.append(
            {
                "nct_id": nct_id,
                "brief_title": core["brief_title"],
                "study_type": core["study_type"],
                "phase": phase,
                "overall_status": core["overall_status"],
                "enrollment": core["enrollment"],
                "enrollment_type": core["enrollment_type"],
                "regulatory_era": era,
                "indication": {
                    "conditions": core["conditions"],
                    "primary_condition": ont["primary_condition"],
                    "secondary_condition": ont["secondary_condition"],
                    "all_classifications": ont["all_classifications"],
                    "mesh_state": mesh_info["state"],
                    "mesh_source": mesh_info["source"],
                    "mesh_descriptor_ids": mesh_ids,
                    "meshes": mesh_terms if mesh_info["meshes"] is not None else None,
                    **({"mesh_problem": mesh_info["problem"]} if "problem" in mesh_info else {}),
                },
                "geography": geo,
                "demographics": demo,
                "sponsors": sponsors_by_nct[nct_id],
            }
        )

    # Group trials into strata across the five deterministic stratum kinds.
    ontology_fp_groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    ontology_bin_groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    # MeSH cells are keyed by descriptor ID alone, as their stratum_id is: grouping
    # by (ID, term) split one descriptor into cells sharing a stratum_id whenever
    # records carried it under two terms (raw records pulled across a MeSH rename).
    mesh_fp_groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    mesh_bin_groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    indication_ref_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for t in trial_rows:
        pri = t["indication"]["primary_condition"]
        sec = t["indication"]["secondary_condition"]
        ph = t["phase"]
        era_key = t["regulatory_era"]["regulatory_era"]
        fp = t["geography"]["site_footprint_class"]
        sbin = t["geography"]["us_site_share_bin"]

        ontology_fp_groups.setdefault((pri, sec, ph, era_key, fp), []).append(t)
        if fp == "multinational_with_us_sites" and sbin is not None:
            ontology_bin_groups.setdefault((pri, sec, ph, era_key, sbin), []).append(t)

        if fp in ("us_only", "multinational_with_us_sites"):
            indication_ref_groups.setdefault((pri, ph), []).append(t)

        if t["indication"]["mesh_state"] == "indexed" and t["indication"]["meshes"]:
            seen_m: set[str] = set()
            for m in t["indication"]["meshes"]:
                mid = m["id"]
                if mid in seen_m:
                    continue
                seen_m.add(mid)
                mesh_fp_groups.setdefault((mid, pri, ph, era_key, fp), []).append(t)
                if fp == "multinational_with_us_sites" and sbin is not None:
                    mesh_bin_groups.setdefault((mid, pri, ph, era_key, sbin), []).append(t)

    strata_list: list[dict[str, Any]] = []
    primary_cell_by_key: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    ref_by_indication_phase: dict[tuple[str, str], dict[str, Any]] = {}
    peer_values_by_indication_phase: dict[tuple[str, str], dict[str, list[float]]] = {}

    for key in sorted(ontology_fp_groups):
        pri, sec, ph, era_key, fp = key
        sid = _stratum_id("ontology_footprint", key)
        pooled = _pool_trial_group(ontology_fp_groups[key], min_cell_trials=min_cell_trials)
        cell = {
            "stratum_id": sid,
            "stratum_kind": "ontology_footprint",
            "ontology_primary": pri,
            "ontology_secondary": sec,
            "mesh_descriptor_id": None,
            "mesh_descriptor_term": None,
            "phase": ph,
            "regulatory_era": era_key,
            "site_footprint_class": fp,
            "us_site_share_bin": None,
            "pooled": pooled,
        }
        primary_cell_by_key[key] = cell
        strata_list.append(cell)

    for key in sorted(ontology_bin_groups):
        pri, sec, ph, era_key, sbin = key
        sid = _stratum_id("ontology_us_share_bin", key)
        pooled = _pool_trial_group(ontology_bin_groups[key], min_cell_trials=min_cell_trials)
        strata_list.append(
            {
                "stratum_id": sid,
                "stratum_kind": "ontology_us_share_bin",
                "ontology_primary": pri,
                "ontology_secondary": sec,
                "mesh_descriptor_id": None,
                "mesh_descriptor_term": None,
                "phase": ph,
                "regulatory_era": era_key,
                "site_footprint_class": "multinational_with_us_sites",
                "us_site_share_bin": sbin,
                "pooled": pooled,
            }
        )

    mesh_terms, mesh_term_conflicts = _mesh_display_terms(trial_rows)
    for mkey in sorted(mesh_fp_groups):
        mid, pri, ph, era_key, fp = mkey
        mterm = mesh_terms[mid]
        sid = _stratum_id("mesh_footprint", (mid, pri, ph, era_key, fp))
        pooled = _pool_trial_group(mesh_fp_groups[mkey], min_cell_trials=min_cell_trials)
        strata_list.append(
            {
                "stratum_id": sid,
                "stratum_kind": "mesh_footprint",
                "ontology_primary": pri,
                "ontology_secondary": None,
                "mesh_descriptor_id": mid,
                "mesh_descriptor_term": mterm,
                "phase": ph,
                "regulatory_era": era_key,
                "site_footprint_class": fp,
                "us_site_share_bin": None,
                "pooled": pooled,
            }
        )

    for mkey in sorted(mesh_bin_groups):
        mid, pri, ph, era_key, sbin = mkey
        mterm = mesh_terms[mid]
        sid = _stratum_id("mesh_us_share_bin", (mid, pri, ph, era_key, sbin))
        pooled = _pool_trial_group(mesh_bin_groups[mkey], min_cell_trials=min_cell_trials)
        strata_list.append(
            {
                "stratum_id": sid,
                "stratum_kind": "mesh_us_share_bin",
                "ontology_primary": pri,
                "ontology_secondary": None,
                "mesh_descriptor_id": mid,
                "mesh_descriptor_term": mterm,
                "phase": ph,
                "regulatory_era": era_key,
                "site_footprint_class": "multinational_with_us_sites",
                "us_site_share_bin": sbin,
                "pooled": pooled,
            }
        )

    for rkey in sorted(indication_ref_groups):
        pri, ph = rkey
        group_trials = indication_ref_groups[rkey]
        sid = _stratum_id("indication_us_reference", rkey)
        pooled = _pool_trial_group(group_trials, min_cell_trials=min_cell_trials)
        rcell = {
            "stratum_id": sid,
            "stratum_kind": "indication_us_reference",
            "ontology_primary": pri,
            "ontology_secondary": None,
            "mesh_descriptor_id": None,
            "mesh_descriptor_term": None,
            "phase": ph,
            "regulatory_era": None,
            "site_footprint_class": "us_only_or_multinational_with_us_sites",
            "us_site_share_bin": None,
            "pooled": pooled,
        }
        ref_by_indication_phase[rkey] = rcell
        strata_list.append(rcell)

        # Precompute peer value arrays for deterministic mid-rank percentile calculation.
        peer_map: dict[str, list[float]] = {}
        for cat in RACE_CLASSIFIED_KEYS:
            peer_map[f"race:{cat}"] = [
                float(t["demographics"]["race"]["of_baseline_total"][cat]["floor"])
                for t in group_trials
                if t["demographics"]["race"]["usable_for_omb_pooling"]
                and isinstance(t["demographics"]["race"].get("of_baseline_total"), dict)
            ]
        for cat in ETHNICITY_CLASSIFIED_KEYS:
            peer_map[f"ethnicity:{cat}"] = [
                float(t["demographics"]["ethnicity"]["of_baseline_total"][cat]["floor"])
                for t in group_trials
                if t["demographics"]["ethnicity"]["usable_for_omb_pooling"]
                and isinstance(t["demographics"]["ethnicity"].get("of_baseline_total"), dict)
            ]
        peer_map["sex_gender:female"] = [
            float(t["demographics"]["sex_gender"]["female_share"])
            for t in group_trials
            if t["demographics"]["sex_gender"].get("female_share") is not None
        ]
        peer_values_by_indication_phase[rkey] = peer_map

    # Attach non-withheld reference medians onto each trial's Denominator A bounds
    # so `direction_settled` reflects its matched stratum median.
    for t in trial_rows:
        pkey = (
            t["indication"]["primary_condition"],
            t["indication"]["secondary_condition"],
            t["phase"],
            t["regulatory_era"]["regulatory_era"],
            t["geography"]["site_footprint_class"],
        )
        pcell = primary_cell_by_key[pkey]
        r_meds, e_meds, f_med = _extract_medians_from_pooled(pcell["pooled"])
        chosen_id = pcell["stratum_id"]
        withheld_flag = bool(pcell["pooled"]["withheld_small_cell"])

        if not r_meds and not e_meds and f_med is None:
            rkey = (t["indication"]["primary_condition"], t["phase"])
            fallback = ref_by_indication_phase.get(rkey)
            if fallback is not None and t["geography"]["site_footprint_class"] in (
                "us_only",
                "multinational_with_us_sites",
            ):
                r_meds, e_meds, f_med = _extract_medians_from_pooled(fallback["pooled"])
                if r_meds or e_meds or f_med is not None:
                    chosen_id = fallback["stratum_id"]
                    withheld_flag = False

        t["demographics"] = attach_reference_medians(
            t["demographics"],
            race_medians=r_meds or None,
            ethnicity_medians=e_meds or None,
            sex_female_median=f_med,
            reference_stratum_id=chosen_id,
            reference_withheld_small_cell=withheld_flag,
        )

    # Build canonical (`current_owner`) and `as_registered` company scorecards.
    trial_by_nct = {t["nct_id"]: t for t in trial_rows}
    canonicals = sorted(set(sponsor_index["canonical"].unique())) if not sponsor_index.empty else []
    entities = sorted(set(sponsor_index["entity"].unique())) if not sponsor_index.empty else []

    current_owner_scorecards: list[dict[str, Any]] = []
    for canon in canonicals:
        ent_df = sponsor_roles.entities(sponsor_index, canon)
        constituents = [
            {
                "entity": str(erow["entity"]),
                "n_trials": int(erow["n_trials"]),
                "n_lead": int(erow["n_lead"]),
                "n_collab": int(erow["n_collab"]),
            }
            for _, erow in ent_df.iterrows()
        ]
        by_role: dict[str, dict[str, Any]] = {}
        for role in ("any", "lead", "collaborator"):
            t_df = sponsor_roles.trials(sponsor_index, canon, role=role, view="current_owner")
            matched_ncts = sorted(set(str(n) for n in t_df["nct_id"]))
            role_trials = [trial_by_nct[n] for n in matched_ncts if n in trial_by_nct]
            by_role[role] = _build_role_scorecard(
                role_trials,
                ref_by_indication_phase=ref_by_indication_phase,
                peer_values_by_indication_phase=peer_values_by_indication_phase,
                min_cell_trials=min_cell_trials,
            )
        current_owner_scorecards.append(
            {
                "company": canon,
                "view": "current_owner",
                "constituent_entities": constituents,
                "by_role": by_role,
            }
        )

    as_registered_scorecards: list[dict[str, Any]] = []
    for ent in entities:
        sub_idx = sponsor_index[sponsor_index["entity"] == ent]
        owners = sorted(set(str(c) for c in sub_idx["canonical"].unique()))
        by_role_ent: dict[str, dict[str, Any]] = {}
        for role in ("any", "lead", "collaborator"):
            t_df = sponsor_roles.trials(sponsor_index, ent, role=role, view="as_registered")
            matched_ncts = sorted(set(str(n) for n in t_df["nct_id"]))
            role_trials = [trial_by_nct[n] for n in matched_ncts if n in trial_by_nct]
            by_role_ent[role] = _build_role_scorecard(
                role_trials,
                ref_by_indication_phase=ref_by_indication_phase,
                peer_values_by_indication_phase=peer_values_by_indication_phase,
                min_cell_trials=min_cell_trials,
            )
        as_registered_scorecards.append(
            {
                "entity": ent,
                "view": "as_registered",
                "canonical_owners": owners,
                "by_role": by_role_ent,
            }
        )

    return {
        "trial_rows": trial_rows,
        "strata": strata_list,
        "sponsors": {
            "attribution_summary": sponsor_meta,
            "current_owner": current_owner_scorecards,
            "as_registered": as_registered_scorecards,
        },
        "ontology_meta": {
            "file": ontology_path.name,
            "version": classifier.version,
            "sha256": classifier.sha256,
        },
        "sponsor_meta": sponsor_meta,
        "mesh_descriptor_term_conflicts": mesh_term_conflicts,
    }
