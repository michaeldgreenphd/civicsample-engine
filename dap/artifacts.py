"""Artifact family builder, writer, and producer contract checker for DAP benchmarks.

Schema version 1 files written to an output directory:
  - ``run.json``         manifest with deterministic ``run_identity`` sha256,
                         input/code provenance, parameters, vocabularies,
                         limitations, and byte-level ``files`` sha256s
  - ``summary.json``     denominators, headline metrics, and partition
                         breakdowns where EVERY count carries ``value``,
                         ``numerator``, ``denominator``, and ``definition``
  - ``strata.json``      indication x phase x regulatory_era x site_footprint
                         (and US site share bin / MeSH) benchmark cells
  - ``sponsors.json``    canonical (``current_owner``) and ``as_registered``
                         company peer scorecards and 3-state attribution counts
  - ``trials.jsonl.gz``  per-trial geography, two-denominator demographics,
                         5-state ``sex_gender``, regulatory era, indication,
                         and sponsor attribution, sorted by ``nct_id``
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from dap import METHOD_VERSION, SCHEMA_VERSION, UNIT_BASIS
from src import holdout_exclusion as HX
from dap.benchmarks import (
    MESH_STATES_EXTENDED,
    REGULATORY_ERAS,
    SPONSOR_REVIEW_STATES,
    STRATUM_KINDS,
    snapshot_date_problem,
    weekly_source_stamp_problem,
)
from dap.demographics import (
    ETHNICITY_CLASSIFIED_KEYS,
    RACE_CLASSIFIED_KEYS,
    SEX_GENDER_STATES,
    TABLE_STATUSES,
)
from dap.geography import (
    SITE_FOOTPRINT_CLASSES,
    US_SITE_SHARE_BINS,
    classify_us_site_share_bin,
)

JSON_FILES: tuple[str, ...] = ("summary.json", "strata.json", "sponsors.json")
TRIALS_FILE: str = "trials.jsonl.gz"
SIBLING_FILES: tuple[str, ...] = ("summary.json", "strata.json", "sponsors.json", "trials.jsonl.gz")

RUN_ID_RX: re.Pattern[str] = re.compile(r"^dap-\d{4}-\d{2}-\d{2}-[0-9a-f]{10}$")
HEX64_RX: re.Pattern[str] = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RX: re.Pattern[str] = re.compile(r"^[0-9a-f]{40}$")

COUNTING_UNITS: dict[str, str] = {
    "trial": "One distinct ClinicalTrials.gov study record (keyed by nct_id).",
    "facility": (
        "One location entry in protocolSection.contactsLocationsModule.locations "
        "(or weekly study_sites); strictly listed facilities, never participant counts."
    ),
    "participant_in_baseline_table": (
        "One participant counted in a trial's baseline characteristics measure "
        "(Denominator A includes unknown and unmapped rows; Denominator B restricts "
        "to classified categories)."
    ),
    "stratum_cell": (
        "One distinct stratification cell in strata.json; percentiles are withheld "
        "whenever qualifying trials < min_cell_trials."
    ),
    "sponsor_literal": (
        "One distinct literal sponsor or collaborator string across input trials, "
        "partitioned into attributed, reviewed_excluded, and unreviewed."
    ),
}

LIMITATIONS: tuple[str, ...] = (
    (
        "Facility share (us_facility_share) counts listed facilities with a country "
        "(unit_basis = listed_facilities_not_participants), not US participant enrollment. "
        "A multinational trial with 25% US sites may enrol more or fewer than 25% of its "
        "participants in the United States."
    ),
    (
        "Baseline race and ethnicity tables describe the trial's baseline analysis "
        "population across all arms combined, which can differ from registered enrollment."
    ),
    (
        "Combined race/ethnicity tables (where Hispanic/Latino or combined labels appear "
        "inside the race measure) are quarantined from pooled OMB race distributions "
        "because their race rows exclude or split Hispanic participants."
    ),
    (
        "Single-category ethnicity tables (reporting only one of Hispanic or Latino / "
        "Not Hispanic or Latino) retain Denominator A as a floor and withhold Denominator B "
        "(of_classified) to avoid dividing one category by itself."
    ),
    (
        "Denominator B (of_classified) is a second view among participants with a reported, "
        "classifiable category, not an unbiased recovery of missing race or ethnicity."
    ),
    (
        "Regulatory era classification uses exact partial-date intervals without inventing "
        "-01 days; month- or year-precision dates straddling 2017-01-18 or 2022-12-29 are "
        "classified as era_indeterminate. Nothing here is a legal determination of 42 CFR "
        "Part 11 or FDORA Section 3601 statutory applicability or compliance."
    ),
)


def sha256_bytes(data: bytes) -> str:
    """Return hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def dump(obj: Any) -> bytes:
    """Canonical compact UTF-8 JSON with sorted keys and trailing newline."""
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def dump_manifest(obj: dict[str, Any]) -> bytes:
    """Human-readable indented UTF-8 JSON for ``run.json``."""
    return (json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")


def encode_trials_gzip(trial_rows: list[dict[str, Any]]) -> bytes:
    """Deterministic gzip (``mtime=0``) over ``nct_id``-sorted JSONL rows."""
    raw = b"".join(dump(row) for row in trial_rows)
    return gzip.compress(raw, mtime=0)


def run_identity(
    run_manifest: dict[str, Any],
    json_files: dict[str, dict[str, Any]],
    trial_rows: list[dict[str, Any]],
) -> str:
    """Ten-hex deterministic content identity across manifest, JSON siblings, and trial rows.

    Excludes ``run_id``, ``generated_at``, and ``files`` so two builds of the
    same inputs and code produce identical ``run_identity`` at any wall-clock
    time, while any change to inputs, parameters, code digest, or published
    outputs yields a distinct ``run_identity``.
    """
    run_core = {
        k: v for k, v in run_manifest.items() if k not in ("run_id", "generated_at", "files")
    }
    json_digests = {
        name: sha256_bytes(dump({k: v for k, v in obj.items() if k != "run_id"}))
        for name, obj in sorted(json_files.items())
    }
    trials_digest = sha256_bytes(
        b"".join(dump({k: v for k, v in row.items() if k != "run_id"}) for row in trial_rows)
    )
    return sha256_bytes(dump([run_core, json_digests, trials_digest]))[:10]


# Why a sex/gender composition may be withheld (`demographics.extract_sex_gender`,
# `demographics._withhold_sex_composition`); `None` when it is not.
WHOLE_POPULATION_GROUP_PROBLEMS: frozenset[str] = frozenset(
    {"multi_group_baseline_without_total_group", "several_groups_titled_as_whole_population"}
)
# A whole-population group whose sex entries are blank beside an arm's counts, or numeric but not the
# entries the parser took, or a customized class pooling several categories' numbers, when those counts do
# not make up the table's Participants denominator (`demographics._whole_population_sex_problem`), and the
# first of several classes of a standard-layout sex measure, whose counts do not make it up either
# (`demographics._sex_counts_from_one_class_of_several`).
WHOLE_POPULATION_SEX_UNREAD: frozenset[str] = frozenset(
    {
        *WHOLE_POPULATION_GROUP_PROBLEMS,
        "whole_population_sex_counts_missing",
        "sex_counts_differ_from_whole_population",
        "sex_counts_pooled_across_categories",
        "sex_counts_from_one_class_of_several",
    }
)
SEX_COMPOSITION_WITHHELD_REASONS: frozenset[str | None] = frozenset(
    {None, "participant_counts_not_whole", *WHOLE_POPULATION_SEX_UNREAD}
)


def _count_item(
    metric_id: str,
    label: str,
    numerator: int,
    denominator_key: str,
    denominators: dict[str, dict[str, Any]],
    definition: str,
) -> dict[str, Any]:
    """Construct a count object carrying ``value``, ``numerator``, ``denominator``, ``definition``."""
    denom_n = int(denominators[denominator_key]["n"])
    share: float | None = round(numerator / denom_n, 6) if denom_n > 0 else None
    return {
        "id": metric_id,
        "label": label,
        "value": numerator,
        "numerator": numerator,
        "denominator": denominator_key,
        "denominator_n": denom_n,
        "share": share,
        "definition": definition,
    }


def summarise(
    trial_rows: list[dict[str, Any]],
    strata: list[dict[str, Any]],
    sponsors: dict[str, Any],
) -> dict[str, Any]:
    """Build ``summary.json`` where every count carries ``value``, ``numerator``, ``denominator``, ``definition``."""
    posted = [t for t in trial_rows if t["demographics"]["results_posted"]]
    multinat_us = [
        t for t in trial_rows
        if t["geography"]["site_footprint_class"] == "multinational_with_us_sites"
    ]
    us_site = [
        t for t in trial_rows
        if t["geography"]["site_footprint_class"] in ("us_only", "multinational_with_us_sites")
    ]
    posted_us_site = [t for t in us_site if t["demographics"]["results_posted"]]
    ont_fp_strata = [s for s in strata if s["stratum_kind"] == "ontology_footprint"]

    lit_states = sponsors["attribution_summary"]["literal_counts_by_review_state"]
    total_literals = sum(int(lit_states[s]) for s in SPONSOR_REVIEW_STATES)

    denominators: dict[str, dict[str, Any]] = {
        "all_trials": {
            "n": len(trial_rows),
            "definition": "All study records included in this DAP benchmark build.",
        },
        "posted_results_trials": {
            "n": len(posted),
            "definition": "Study records in this build with posted results on ClinicalTrials.gov.",
        },
        "multinational_with_us_sites_trials": {
            "n": len(multinat_us),
            "definition": (
                "Study records with listed facilities in both the United States (or US territories) "
                "and at least one non-US country."
            ),
        },
        "us_site_trials": {
            "n": len(us_site),
            "definition": (
                "Study records whose site_footprint_class is us_only or multinational_with_us_sites "
                "(>= 1 confirmed US facility and no unlocated one-sided facilities)."
            ),
        },
        "posted_us_site_trials": {
            "n": len(posted_us_site),
            "definition": "US-site trials (us_only or multinational_with_us_sites) with posted results.",
        },
        "sponsor_literals": {
            "n": total_literals,
            "definition": (
                "Distinct non-empty literal lead sponsor and collaborator names present in this build."
            ),
        },
        "ontology_footprint_strata": {
            "n": len(ont_fp_strata),
            "definition": (
                "Distinct ontology indication x phase x regulatory_era x site_footprint_class cells."
            ),
        },
        "all_strata": {
            "n": len(strata),
            "definition": "All stratification cells emitted across ontology, US-share-bin, MeSH, and reference views.",
        },
    }

    n_quarantined_posted = sum(
        1 for t in posted if t["demographics"]["race_table_status"] == "combined_race_ethnicity_quarantined"
    )
    n_single_eth_posted = sum(
        1 for t in posted if t["demographics"]["ethnicity_table_status"] == "single_category_ethnicity"
    )
    n_omitted_race_us = sum(
        1 for t in posted_us_site if t["demographics"]["us_site_omission_audit"]["omitted_race_table"] is True
    )
    n_omitted_eth_us = sum(
        1 for t in posted_us_site if t["demographics"]["us_site_omission_audit"]["omitted_ethnicity_table"] is True
    )
    n_omitted_any_us = sum(
        1 for t in posted_us_site
        if t["demographics"]["us_site_omission_audit"]["omitted_race_or_ethnicity_table"] is True
    )
    n_omitted_both_us = sum(
        1 for t in posted_us_site
        if t["demographics"]["us_site_omission_audit"]["omitted_both_race_and_ethnicity_tables"] is True
    )
    n_omitted_sex_us = sum(
        1 for t in posted_us_site if t["demographics"]["us_site_omission_audit"]["omitted_sex_table"] is True
    )

    metrics: list[dict[str, Any]] = [
        _count_item(
            "results_posted",
            "Trials with posted results",
            len(posted),
            "all_trials",
            denominators,
            "Study records carrying posted ClinicalTrials.gov results.",
        ),
        _count_item(
            "results_not_posted",
            "Trials without posted results",
            len(trial_rows) - len(posted),
            "all_trials",
            denominators,
            "Study records with no posted results section on ClinicalTrials.gov.",
        ),
        _count_item(
            "us_site_trials",
            "Trials with confirmed US site footprint",
            len(us_site),
            "all_trials",
            denominators,
            "Trials classified as us_only or multinational_with_us_sites (listed facilities basis).",
        ),
        _count_item(
            "posted_us_site_trials",
            "US-site trials with posted results",
            len(posted_us_site),
            "us_site_trials",
            denominators,
            "US-site trials (us_only or multinational_with_us_sites) that have posted results.",
        ),
        _count_item(
            "combined_race_ethnicity_quarantined",
            "Posted trials with combined race/ethnicity table quarantined",
            n_quarantined_posted,
            "posted_results_trials",
            denominators,
            (
                "Posted-results trials where Hispanic/Latino or combined race+ethnicity labels appear "
                "inside the race measure; quarantined and excluded from pooled OMB race distributions."
            ),
        ),
        _count_item(
            "single_category_ethnicity",
            "Posted trials with single-category ethnicity table",
            n_single_eth_posted,
            "posted_results_trials",
            denominators,
            (
                "Posted-results trials reporting only one OMB ethnicity category row (e.g. Hispanic or "
                "Latino without Not Hispanic or Latino); Denominator B (of_classified) is withheld."
            ),
        ),
        _count_item(
            "us_site_omitted_race_table",
            "Posted US-site trials omitting a race table",
            n_omitted_race_us,
            "posted_us_site_trials",
            denominators,
            "Trials with >= 1 confirmed US facility and posted results that lack a baseline race table.",
        ),
        _count_item(
            "us_site_omitted_ethnicity_table",
            "Posted US-site trials omitting an ethnicity table",
            n_omitted_eth_us,
            "posted_us_site_trials",
            denominators,
            "Trials with >= 1 confirmed US facility and posted results that lack a baseline ethnicity table.",
        ),
        _count_item(
            "us_site_omitted_race_or_ethnicity_table",
            "Posted US-site trials omitting race or ethnicity table",
            n_omitted_any_us,
            "posted_us_site_trials",
            denominators,
            "Posted US-site trials lacking a baseline race table, an ethnicity table, or both.",
        ),
        _count_item(
            "us_site_omitted_both_race_and_ethnicity_tables",
            "Posted US-site trials omitting both race and ethnicity tables",
            n_omitted_both_us,
            "posted_us_site_trials",
            denominators,
            "Posted US-site trials lacking both a baseline race table and a baseline ethnicity table.",
        ),
        _count_item(
            "us_site_omitted_sex_table",
            "Posted US-site trials omitting a sex/gender measure",
            n_omitted_sex_us,
            "posted_us_site_trials",
            denominators,
            "Posted US-site trials whose sex_gender status is not_reported.",
        ),
        _count_item(
            "sex_gender_reported_sex_true",
            "Posted trials reporting biological sex",
            sum(1 for t in posted if t["demographics"]["sex_gender"]["reported_sex"] is True),
            "posted_results_trials",
            denominators,
            "Posted-results trials with sex_gender.reported_sex == True from the vendored manuscript parser.",
        ),
        _count_item(
            "sex_gender_reported_gender_true",
            "Posted trials reporting gender identity categories",
            sum(1 for t in posted if t["demographics"]["sex_gender"]["reported_gender"] is True),
            "posted_results_trials",
            denominators,
            "Posted-results trials with category-based sex_gender.reported_gender == True.",
        ),
        _count_item(
            "sex_gender_non_participant_units",
            "Posted trials with non-participant sex/gender units",
            sum(1 for t in posted if t["demographics"]["sex_gender"]["is_participant_count"] is False),
            "posted_results_trials",
            denominators,
            "Posted-results trials with COUNT_OF_UNITS / non-participant sex tables (kept in reporting counts, excluded from composition).",
        ),
        _count_item(
            "sex_gender_composition_withheld_whole_population_unread",
            "Posted trials whose sex/gender composition is withheld for want of whole-population counts",
            sum(
                1 for t in posted
                if t["demographics"]["sex_gender"].get("composition_withheld_reason") in WHOLE_POPULATION_SEX_UNREAD
            ),
            "posted_results_trials",
            denominators,
            "Posted-results trials whose baseline table has no group, or more than one, titled as the whole "
            "population, or whose whole-population sex entries are blank beside an arm's counts, so the parser's "
            "total may be an arm (kept in reporting counts, excluded from composition).",
        ),
        _count_item(
            "sex_gender_participant_counts_not_whole",
            "Posted trials whose sex/gender participant counts are not whole",
            sum(
                1 for t in posted
                if t["demographics"]["sex_gender"].get("composition_withheld_reason") == "participant_counts_not_whole"
            ),
            "posted_results_trials",
            denominators,
            "Posted-results trials whose sex table reads as participant counts that are not whole numbers, such as "
            "percentages entered as counts (kept in reporting counts, excluded from composition).",
        ),
        _count_item(
            "trials_any_attributed_sponsor",
            "Trials with at least one attributed canonical sponsor",
            sum(1 for t in trial_rows if bool(t["sponsors"]["current_owner"]["any"])),
            "all_trials",
            denominators,
            "Distinct trials matched to >= 1 canonical company (lead or collaborator) via sponsors/company_aliases.csv.",
        ),
        _count_item(
            "mesh_indexed_trials",
            "Trials with CT.gov MeSH condition descriptors",
            sum(1 for t in trial_rows if t["indication"]["mesh_state"] == "indexed"),
            "all_trials",
            denominators,
            "Trials carrying >= 1 MeSH descriptor in condition_mesh sidecar or derivedSection.conditionBrowseModule.",
        ),
        _count_item(
            "ontology_strata_withheld_small_cell",
            "Ontology footprint strata withheld for small cell size",
            sum(1 for s in ont_fp_strata if s["pooled"]["withheld_small_cell"] is True),
            "ontology_footprint_strata",
            denominators,
            "Ontology footprint cells with n_trials < min_cell_trials whose percentiles are withheld.",
        ),
        _count_item(
            "all_strata_withheld_small_cell",
            "All strata withheld for small cell size",
            sum(1 for s in strata if s["pooled"]["withheld_small_cell"] is True),
            "all_strata",
            denominators,
            "All emitted stratum cells with n_trials < min_cell_trials whose percentiles are withheld.",
        ),
    ]

    footprint_defs = {
        "us_only": "Trials with >= 1 listed facility, all in the US or US territories, and zero facilities missing country.",
        "multinational_with_us_sites": "Trials with >= 1 listed US facility and >= 1 listed non-US facility.",
        "ex_us_only": "Trials with >= 1 listed facility, zero US facilities, and zero facilities missing country.",
        "facilities_missing_country": "Trials with listed facilities where missing country prevents establishing footprint.",
        "no_listed_facilities": "Trials with zero listed facilities in contactsLocationsModule.locations.",
        "footprint_unknown": (
            "Trials whose site list cannot establish a footprint: weekly full records (study_sites) drop "
            "every location without a country and keep no total count. Raw CT.gov API v2 records are "
            "needed for site-mix classes."
        ),
    }
    share_bin_defs = {
        "us_share_lt_25": "Multinational US-site trials with 0 < us_facility_share < 0.25 (listed facilities basis).",
        "us_share_25_to_lt_50": "Multinational US-site trials with 0.25 <= us_facility_share < 0.50 (listed facilities basis).",
        "us_share_50_to_lt_75": "Multinational US-site trials with 0.50 <= us_facility_share < 0.75 (listed facilities basis).",
        "us_share_gte_75": "Multinational US-site trials with 0.75 <= us_facility_share < 1.00 (listed facilities basis).",
    }
    era_defs = {
        "pre_2017_final_rule": "Enrollment start (or bounding primary completion) wholly before 2017-01-18 (42 CFR Part 11 effective date).",
        "post_2017_pre_fdora": "Enrollment start wholly within 2017-01-18 through 2022-12-28.",
        "post_2022_fdora": "Enrollment start wholly on or after 2022-12-29 (FDORA Section 3601 enactment date).",
        "era_indeterminate": "Enrollment start missing, unparsable, or partial date straddling 2017-01-18 or 2022-12-29.",
    }
    table_status_defs = {
        "omb_compatible": "Posted baseline table whose rows map cleanly to standard NIH/OMB categories without overlap.",
        "combined_race_ethnicity_quarantined": "Baseline race measure containing Hispanic/Latino or combined labels; quarantined from OMB pooling.",
        "single_category_ethnicity": "Baseline ethnicity measure reporting only one OMB category row; Denominator B is withheld.",
        "non_standard_categories_present": "Baseline table containing unmapped, quarantined, extra non-OMB, or double-counted categories.",
        "whole_population_group_unidentified": (
            "Baseline table with several groups and none recognised as the whole population (no Total group); "
            "no counts or shares are read, since any one group would be an arm."
        ),
        "table_absent_in_posted_results": "Trial has posted results with no usable baseline table for this dimension.",
        "results_not_posted": "Trial has no posted results section on ClinicalTrials.gov.",
    }
    sg_defs = {
        "reported": "Vendored sex_gender_parser status reported: informative sex and/or gender category counts present.",
        "explicit_unknown_only": "Vendored sex_gender_parser status explicit_unknown_only: positive Unknown count and zero in all other buckets.",
        "uninformative": "Vendored sex_gender_parser status uninformative: selected measure has zero/empty/unusable counts.",
        "not_reported": "Vendored sex_gender_parser status not_reported: no sex/gender baseline measure selected.",
        "parse_error": "Vendored sex_gender_parser status parse_error: measure payload could not be parsed.",
    }
    sponsor_state_defs = {
        "attributed": "Matched by a curated status=attribute rule in sponsors/company_aliases.csv.",
        "reviewed_excluded": "Matched by a curated status=exclude rule in sponsors/company_aliases.csv (human reviewed, not attributed).",
        "unreviewed": "Not matched by any attribute or exclude rule in sponsors/company_aliases.csv.",
    }
    mesh_defs = {
        "indexed": "CT.gov conditionBrowseModule present with >= 1 MeSH descriptor.",
        "not_indexed": "CT.gov conditionBrowseModule present with empty/absent meshes list.",
        "no_module": "derivedSection present without conditionBrowseModule.",
        "no_derived": "Raw study document has no derivedSection key.",
        "malformed": "derivedSection or conditionBrowseModule malformed.",
        "mesh_sidecar_not_provided": "Weekly record evaluated without a condition_mesh.jsonl.gz sidecar.",
    }

    breakdowns: dict[str, list[dict[str, Any]]] = {
        "site_footprint_class": [
            _count_item(
                f"footprint:{fp}",
                fp,
                sum(1 for t in trial_rows if t["geography"]["site_footprint_class"] == fp),
                "all_trials",
                denominators,
                footprint_defs[fp],
            )
            for fp in SITE_FOOTPRINT_CLASSES
        ],
        "multinational_us_site_share_bin": [
            _count_item(
                f"us_share_bin:{b}",
                b,
                sum(1 for t in multinat_us if t["geography"]["us_site_share_bin"] == b),
                "multinational_with_us_sites_trials",
                denominators,
                share_bin_defs[b],
            )
            for b in US_SITE_SHARE_BINS
        ],
        "regulatory_era": [
            _count_item(
                f"era:{era}",
                era,
                sum(1 for t in trial_rows if t["regulatory_era"]["regulatory_era"] == era),
                "all_trials",
                denominators,
                era_defs[era],
            )
            for era in REGULATORY_ERAS
        ],
        "race_table_status": [
            _count_item(
                f"race_status:{st}",
                st,
                sum(1 for t in trial_rows if t["demographics"]["race_table_status"] == st),
                "all_trials",
                denominators,
                table_status_defs[st],
            )
            for st in TABLE_STATUSES
        ],
        "ethnicity_table_status": [
            _count_item(
                f"ethnicity_status:{st}",
                st,
                sum(1 for t in trial_rows if t["demographics"]["ethnicity_table_status"] == st),
                "all_trials",
                denominators,
                table_status_defs[st],
            )
            for st in TABLE_STATUSES
        ],
        "sex_gender_status": [
            _count_item(
                f"sex_gender_status:{st}",
                st,
                sum(1 for t in trial_rows if t["demographics"]["sex_report_status"] == st),
                "all_trials",
                denominators,
                sg_defs[st],
            )
            for st in SEX_GENDER_STATES
        ],
        "lead_sponsor_review_state": [
            _count_item(
                f"lead_sponsor_state:{st}",
                st,
                sum(1 for t in trial_rows if t["sponsors"]["lead"]["review_state"] == st),
                "all_trials",
                denominators,
                sponsor_state_defs[st],
            )
            for st in SPONSOR_REVIEW_STATES
        ],
        "sponsor_literal_review_state": [
            _count_item(
                f"sponsor_literal_state:{st}",
                st,
                int(lit_states[st]),
                "sponsor_literals",
                denominators,
                sponsor_state_defs[st],
            )
            for st in SPONSOR_REVIEW_STATES
        ],
        "condition_mesh_state": [
            _count_item(
                f"mesh_state:{st}",
                st,
                sum(1 for t in trial_rows if t["indication"]["mesh_state"] == st),
                "all_trials",
                denominators,
                mesh_defs[st],
            )
            for st in MESH_STATES_EXTENDED
        ],
    }

    return {
        "unit_basis": UNIT_BASIS,
        "denominators": denominators,
        "metrics": metrics,
        "breakdowns": breakdowns,
    }


def utc_timestamp_date(generated_at: Any) -> str:
    """Validate a whole ISO-8601 UTC timestamp and return its ``YYYY-MM-DD`` date.

    ``generated_at`` is published and its date enters ``run_id``, so the whole
    value is parsed, not just a prefix (as feasibility's ``utc_timestamp_date``):
    an impossible date (``2026-99-99``), trailing text, a date without a time, a
    naive time or a non-zero UTC offset is refused rather than published. The
    offset is also checked as text: RFC 3339 writes ``-00:00`` for an unknown
    local offset, which ``fromisoformat`` reads as zero, so only ``Z`` or
    ``+00:00`` is accepted.
    """
    if not isinstance(generated_at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T.+", generated_at):
        raise ValueError(f"generated_at must be an ISO-8601 UTC timestamp, got {generated_at!r}")
    try:
        parsed = datetime.fromisoformat(generated_at)
    except ValueError as exc:
        raise ValueError(f"generated_at must be an ISO-8601 UTC timestamp, got {generated_at!r}") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
        or not re.search(r"(?:Z|\+00:00)\Z", generated_at)
    ):
        raise ValueError(f"generated_at must carry a UTC offset (Z or +00:00), got {generated_at!r}")
    return generated_at[:10]


# `benchmarks.check_condition_mesh_provenance`'s check for a sidecar matched to a raw container's own
# extraction stamps; its snapshot_date is then that extraction's.
MESH_STAMPS_MATCHED_RAW = "study_records_run_stamps_matched_and_cohort_covered"


def resolve_snapshot_date(
    explicit: str | None,
    *,
    default: str,
    records: list[dict[str, Any]],
    study_meta: dict[str, Any],
    mesh_meta: dict[str, Any] | None,
) -> str:
    """The snapshot date to record, taken from the inputs' own stamps when they are weekly full records.

    Weekly full records (``input_format == "full_records"``) arrive with their sex/gender rows already
    parsed, and ``extract_sex_gender`` reuses those rows, so the build date says nothing about them. The
    pull's date is read from what the inputs record:

      - ``snapshot_date`` on the reused ``sex_gender`` rows. ``src/extract_all.py`` publishes the rows
        through ``sgt.lean_row``, which drops it, so real weekly rows carry none (none of the 80,890 in
        the pull extracted 2026-11-08); where rows do carry one, every value present must agree;
      - the condition_mesh sidecar's ``snapshot_date``, which ``src/extract_all.py`` writes on every
        line from the same ``--snapshot-date``, and which ``check_condition_mesh_provenance`` has
        already tied to the records' pull by ``extracted_at`` and ``pipeline_commit``;
      - ``explicit`` (``--snapshot-date``).

    Each value given must be a real date and all must agree. With none, the build is refused rather
    than stamped with the build date. ``extracted_at`` is not used: the weekly job takes the snapshot
    date from the runner's date when the job starts and ``extracted_at`` later, so they can differ.

    Other inputs (raw CT.gov v2 studies) are parsed in this build, so their rows take this value:
    ``explicit`` or, without it, ``default`` (the ``--generated-at`` date). A raw ``studies`` or
    ``records`` container that carries the extraction's stamps, with a sidecar
    ``check_condition_mesh_provenance`` has matched to them, is that extraction: the sidecar's
    ``snapshot_date`` is its date, as for weekly records, and an ``explicit`` date must agree with it. The
    build date had been recorded instead, beside the sidecar's own date in ``inputs.condition_mesh``.
    """
    if explicit is not None:
        problem = snapshot_date_problem(explicit)
        if problem:
            raise ValueError(f"--snapshot-date: {problem}")
    if study_meta.get("input_format") != "full_records":
        matched = (mesh_meta or {}).get("provenance_check") == MESH_STAMPS_MATCHED_RAW
        mesh_date = (mesh_meta or {}).get("snapshot_date") if matched else None
        if mesh_date is None:
            return explicit if explicit is not None else default
        problem = snapshot_date_problem(mesh_date)
        if problem:
            raise ValueError(f"{(mesh_meta or {}).get('path')}: condition_mesh {problem}")
        if explicit is not None and explicit != mesh_date:
            raise ValueError(
                f"{study_meta.get('path')}: snapshot dates disagree: the condition_mesh sidecar gives "
                f"{mesh_date!r}; --snapshot-date gives {explicit!r}"
            )
        return mesh_date

    where = study_meta.get("path")
    row_values: dict[str, str] = {}
    for rec in records:
        row = rec.get("sex_gender")
        if not (isinstance(row, dict) and row.get("sex_report_status") in SEX_GENDER_STATES):
            continue  # not reused: extract_sex_gender parses it under the resolved date
        if row.get("snapshot_date") is None:
            continue
        problem = snapshot_date_problem(row.get("snapshot_date"))
        if problem:
            raise ValueError(f"{where}: {rec.get('nct_id')}: sex_gender.{problem}")
        row_values.setdefault(row["snapshot_date"], str(rec.get("nct_id")))
    if len(row_values) > 1:
        shown = ", ".join(f"{v!r} ({n})" for v, n in sorted(row_values.items())[:3])
        raise ValueError(
            f"{where}: sex_gender rows carry {len(row_values)} different snapshot_date values ({shown}); "
            "weekly full records must come from one pull"
        )

    candidates: list[tuple[str, str]] = []
    if row_values:
        candidates.append(("the sex_gender rows", next(iter(row_values))))
    mesh_value = (mesh_meta or {}).get("snapshot_date")
    if mesh_value is not None:
        problem = snapshot_date_problem(mesh_value)
        if problem:
            raise ValueError(f"{(mesh_meta or {}).get('path')}: condition_mesh {problem}")
        candidates.append(("the condition_mesh sidecar", mesh_value))
    if explicit is not None:
        candidates.append(("--snapshot-date", explicit))
    if not candidates:
        raise ValueError(
            f"{where}: weekly full records carry no snapshot date (their sex_gender rows drop it) and no "
            "condition_mesh sidecar gives one; pass --snapshot-date with the weekly pull's date, or the "
            "pull's --condition-mesh sidecar, rather than recording the build date"
        )
    if len({value for _, value in candidates}) > 1:
        shown = "; ".join(f"{source} gives {value!r}" for source, value in candidates)
        raise ValueError(f"{where}: snapshot dates disagree: {shown}")
    return candidates[0][1]


def build_artifact_family(
    benchmark_result: dict[str, Any],
    *,
    study_input_meta: dict[str, Any],
    mesh_input_meta: dict[str, Any] | None,
    producer: dict[str, Any],
    min_cell_trials: int,
    generated_at: str,
    snapshot_date: str,
    holdout_exclusion: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the complete schema-version-1 DAP artifact family in memory.

    ``holdout_exclusion`` is the stamp of the in_audit holdout exclusion the
    study records were loaded with (``HoldoutExclusion.stamp()``,
    ``src/holdout_exclusion.py``); the holdouts are already gone from
    ``benchmark_result``. It is recorded in run.json as given (rules version,
    index sha256, counts; no id) and enters the run identity.
    """
    date_part = utc_timestamp_date(generated_at)
    problem = snapshot_date_problem(snapshot_date)
    if problem:
        raise ValueError(problem)
    stamp_issues = HX.stamp_problems(holdout_exclusion)
    if stamp_issues:
        raise ValueError(stamp_issues[0])

    base_trials: list[dict[str, Any]] = [
        {"schema_version": SCHEMA_VERSION, **row}
        for row in benchmark_result["trial_rows"]
    ]
    strata = benchmark_result["strata"]
    sponsors = benchmark_result["sponsors"]
    summary_body = summarise(base_trials, strata, sponsors)

    json_files: dict[str, dict[str, Any]] = {
        "summary.json": {"schema_version": SCHEMA_VERSION, **summary_body},
        "strata.json": {
            "schema_version": SCHEMA_VERSION,
            "unit_basis": UNIT_BASIS,
            "min_cell_trials": min_cell_trials,
            "stratum_kinds": list(STRATUM_KINDS),
            # MeSH descriptor IDs carried under more than one term; their cells
            # show the display term benchmarks._mesh_display_terms chose. Normally {}.
            "mesh_descriptor_term_conflicts": benchmark_result["mesh_descriptor_term_conflicts"],
            "strata": strata,
        },
        "sponsors.json": {
            "schema_version": SCHEMA_VERSION,
            "unit_basis": UNIT_BASIS,
            "min_cell_trials": min_cell_trials,
            **sponsors,
        },
    }

    run_manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "method_version": METHOD_VERSION,
        "scheduled": False,
        "unit_basis": UNIT_BASIS,
        "producer": producer,
        HX.STAMP_KEY: dict(holdout_exclusion),
        "parameters": {
            "min_cell_trials": min_cell_trials,
            "snapshot_date": snapshot_date,
            "final_rule_effective_date": "2017-01-18",
            "fdora_enactment_date": "2022-12-29",
        },
        "counting_units": COUNTING_UNITS,
        "inputs": {
            "study_records": study_input_meta,
            "condition_mesh": mesh_input_meta,
            "condition_ontology": benchmark_result["ontology_meta"],
            "sponsor_rules": {
                "file": benchmark_result["sponsor_meta"]["rules_file"],
                "sha256": benchmark_result["sponsor_meta"]["rules_sha256"],
            },
        },
        "vocabularies": {
            "site_footprint_classes": list(SITE_FOOTPRINT_CLASSES),
            "us_site_share_bins": list(US_SITE_SHARE_BINS),
            "regulatory_eras": list(REGULATORY_ERAS),
            "table_statuses": list(TABLE_STATUSES),
            "sex_gender_states": list(SEX_GENDER_STATES),
            "sponsor_review_states": list(SPONSOR_REVIEW_STATES),
            "stratum_kinds": list(STRATUM_KINDS),
            "mesh_states": list(MESH_STATES_EXTENDED),
        },
        "limitations": list(LIMITATIONS),
    }

    ident = run_identity(run_manifest, json_files, base_trials)
    run_id = f"dap-{date_part}-{ident}"

    stamped_json: dict[str, dict[str, Any]] = {
        name: {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            **{k: v for k, v in obj.items() if k != "schema_version"},
        }
        for name, obj in json_files.items()
    }
    stamped_trials: list[dict[str, Any]] = [
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            **{k: v for k, v in row.items() if k != "schema_version"},
        }
        for row in base_trials
    ]

    raw_bytes: dict[str, bytes] = {
        name: dump(obj) for name, obj in stamped_json.items()
    }
    raw_bytes[TRIALS_FILE] = encode_trials_gzip(stamped_trials)

    final_run: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": generated_at,
        **{k: v for k, v in run_manifest.items() if k != "schema_version"},
        "files": {name: sha256_bytes(raw_bytes[name]) for name in sorted(SIBLING_FILES)},
    }
    raw_bytes["run.json"] = dump_manifest(final_run)

    return {
        "run.json": final_run,
        **stamped_json,
        "trial_rows": stamped_trials,
        "_bytes": raw_bytes,
    }


def load_dir(path: Path) -> dict[str, Any]:
    """Load an artifact directory from disk for contract checking."""
    out: dict[str, Any] = {"_bytes": {}, "_extra_files": []}
    if not path.is_dir():
        return out
    expected_all = set(SIBLING_FILES) | {"run.json"}
    for entry in sorted(path.iterdir()):
        if not entry.is_file() or entry.is_symlink():
            out["_extra_files"].append(entry.name)
            continue
        raw = entry.read_bytes()
        out["_bytes"][entry.name] = raw
        if entry.name not in expected_all:
            out["_extra_files"].append(entry.name)
            continue
        if entry.name == TRIALS_FILE:
            # Split strictly on 0x0A (b"\n"), NEVER str.splitlines(), which splits
            # mid-JSON-string on U+2028, U+2029, \x85, \x0b, and \x0c when ensure_ascii=False.
            decompressed = gzip.decompress(raw)
            out["trial_rows"] = [
                json.loads(raw_line.decode("utf-8"))
                for raw_line in decompressed.split(b"\n")
                if raw_line.strip()
            ]
        else:
            out[entry.name] = json.loads(raw.decode("utf-8"))
    return out


def _check_distribution_obj(dist: Any, where: str, min_cell: int, problems: list[str]) -> None:
    """Validate small-cell withholding and percentile monotonicity on one distribution dict."""
    if not isinstance(dist, dict):
        problems.append(f"{where}: distribution is not an object")
        return
    n_t = dist.get("n_trials")
    withheld = dist.get("withheld_small_cell")
    pcts = dist.get("percentiles")
    mean_v = dist.get("trial_equal_mean")
    w_v = dist.get("participant_weighted_share")
    if not isinstance(n_t, int) or n_t < 0:
        problems.append(f"{where}: n_trials must be a non-negative integer")
        return
    if n_t < min_cell:
        if withheld is not True or pcts is not None or mean_v is not None or w_v is not None:
            problems.append(
                f"{where}: cell with n_trials={n_t} < {min_cell} must set withheld_small_cell=True and withhold percentiles"
            )
    else:
        if withheld is not False or not isinstance(pcts, dict):
            problems.append(f"{where}: cell with n_trials={n_t} >= {min_cell} must publish percentiles")
            return
        vals = [pcts.get(k) for k in ("p10", "p25", "p50", "p75", "p90")]
        if not all(isinstance(v, (int, float)) for v in vals):
            problems.append(f"{where}: missing numeric p10..p90")
        elif not (vals[0] <= vals[1] <= vals[2] <= vals[3] <= vals[4]):
            problems.append(f"{where}: percentiles are not non-decreasing ({vals})")


REFERENCE_DIMENSIONS: tuple[str, ...] = ("race", "ethnicity", "sex_gender")


def _carries_reference_median(demo: dict[str, Any], dim: str) -> bool:
    """Whether a trial's race, ethnicity or sex_gender block was compared with a reference median."""
    block = demo.get(dim) if isinstance(demo.get(dim), dict) else {}
    entries = block.get("of_parsed_total" if dim == "sex_gender" else "of_baseline_total")
    return isinstance(entries, dict) and any(
        isinstance(e, dict) and e.get("reference_median") is not None for e in entries.values()
    )


def check_contract(loaded: dict[str, Any]) -> list[str]:
    """Validate all structural, cryptographic, and analytical invariants on a DAP artifact family."""
    p: list[str] = []
    extra = loaded.get("_extra_files") or []
    if extra:
        p.append(f"unexpected files or non-regular entries in directory: {sorted(extra)}")

    run = loaded.get("run.json")
    if not isinstance(run, dict):
        return p + ["run.json is missing or not an object"]
    if run.get("schema_version") != SCHEMA_VERSION:
        p.append(f"run.json: schema_version {run.get('schema_version')!r} != {SCHEMA_VERSION}")
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_RX.match(run_id):
        p.append(f"run.json: malformed run_id {run_id!r}")
    # generated_at is outside run_identity and run.json is outside its own files
    # hashes, so an edited value would pass every other check: parse it whole
    # again and require its date to be the run_id's (build_artifact_family
    # derives the run_id date from it).
    try:
        generated_date = utc_timestamp_date(run.get("generated_at"))
    except ValueError as exc:
        p.append(f"run.json: {exc}")
    else:
        if isinstance(run_id, str) and RUN_ID_RX.match(run_id) and run_id[4:14] != generated_date:
            p.append(f"run.json: run_id date {run_id[4:14]} != generated_at date {generated_date}")
    if run.get("scheduled") is not False:
        p.append("run.json: scheduled must be False (manual build)")
    if run.get("unit_basis") != UNIT_BASIS:
        p.append(f"run.json: unit_basis must be {UNIT_BASIS!r}")

    prod = run.get("producer") or {}
    if not HEX64_RX.match(str(prod.get("source_sha256") or "")):
        p.append("run.json: producer.source_sha256 must be a 64-hex sha256")
    # Git provenance is null (never a guessed false) when git was unavailable; such a
    # build is valid for local diagnostics and only the site exporter refuses it.
    commit = prod.get("commit", "<missing>")
    if commit is not None and not (isinstance(commit, str) and GIT_COMMIT_RX.match(commit)):
        p.append(f"run.json: producer.commit must be a 40-hex git commit or null (got {commit!r})")
    dirty = prod.get("uncommitted_changes", "<missing>")
    if dirty is not None and not isinstance(dirty, bool):
        p.append(f"run.json: producer.uncommitted_changes must be true, false or null (got {dirty!r})")
    # The in_audit holdout exclusion stamp (src/holdout_exclusion.py): without a valid one the build was
    # made before the exclusion or without --holdout-index.
    p.extend(HX.stamp_problems(run.get(HX.STAMP_KEY)))

    inputs = run.get("inputs") or {}
    for req_in in ("study_records", "condition_ontology", "sponsor_rules"):
        meta = inputs.get(req_in) or {}
        if not HEX64_RX.match(str(meta.get("sha256") or "")):
            p.append(f"run.json: inputs.{req_in}.sha256 must be a 64-hex sha256")
    if inputs.get("condition_mesh") is not None:
        if not HEX64_RX.match(str((inputs["condition_mesh"] or {}).get("sha256") or "")):
            p.append("run.json: inputs.condition_mesh.sha256 must be a 64-hex sha256")
    # A build records its source container's stamps; each is null or well formed
    # (`weekly_source_stamp_problem`), and only the site exporter refuses a null one on a weekly build.
    # The check covers every input format: a raw container's stamps reach the site manifest too.
    study_in = inputs.get("study_records") or {}
    if isinstance(study_in, dict):
        stamp_problem = weekly_source_stamp_problem(study_in.get("extracted_at"), study_in.get("pipeline_commit"))
        if stamp_problem:
            p.append(f"run.json: inputs.study_records {stamp_problem}")

    listed = run.get("files")
    if not isinstance(listed, dict) or set(listed) != set(SIBLING_FILES):
        p.append(f"run.json: files inventory must equal {sorted(SIBLING_FILES)}")
        return p

    raw_bytes = loaded.get("_bytes") or {}
    for name in SIBLING_FILES:
        if name not in raw_bytes:
            p.append(f"{name}: missing from directory")
            continue
        actual_sha = sha256_bytes(raw_bytes[name])
        if actual_sha != listed.get(name):
            p.append(f"{name}: sha256 {actual_sha} != run.json {listed.get(name)}")

    for jname in JSON_FILES:
        jobj = loaded.get(jname)
        if not isinstance(jobj, dict):
            p.append(f"{jname}: missing or not an object")
            continue
        if jobj.get("schema_version") != SCHEMA_VERSION or jobj.get("run_id") != run_id:
            p.append(f"{jname}: schema_version or run_id mismatch with run.json")

    trial_rows = loaded.get("trial_rows")
    if not isinstance(trial_rows, list) or not trial_rows:
        return p + ["trials.jsonl.gz: missing or empty"]
    # load_dir accepts any JSON value per line (null, a string, a list); every
    # check below, run_identity included, reads rows as objects, so a family
    # with a non-object row is reported and refused here.
    non_objects = [i for i, row in enumerate(trial_rows) if not isinstance(row, dict)]
    if non_objects:
        first = non_objects[0]
        return p + [
            f"trials.jsonl.gz: {len(non_objects)} row(s) are not JSON objects "
            f"(first: row {first + 1} is {type(trial_rows[first]).__name__})"
        ]

    if all(isinstance(loaded.get(j), dict) for j in JSON_FILES):
        expected_ident = run_identity(
            run,
            {j: loaded[j] for j in JSON_FILES},
            trial_rows,
        )
        if isinstance(run_id, str) and run_id[-10:] != expected_ident:
            p.append(
                f"run.json: run_id suffix {run_id[-10:]!r} != recomputed run_identity {expected_ident!r}"
            )

    # Only a real integer >= 1 is a threshold. A string, float, bool (an int
    # subclass) or absent value is reported as a contract problem rather than
    # coerced or raised out of the checker; the remaining checks then run
    # against the default threshold so every other problem is still reported.
    raw_min_cell = (run.get("parameters") or {}).get("min_cell_trials")
    if isinstance(raw_min_cell, int) and not isinstance(raw_min_cell, bool) and raw_min_cell >= 1:
        min_cell = raw_min_cell
    else:
        p.append(
            f"run.json: parameters.min_cell_trials must be an integer >= 1 (got {raw_min_cell!r})"
        )
        min_cell = 5
    snap_value = (run.get("parameters") or {}).get("snapshot_date")
    snap_problem = snapshot_date_problem(snap_value)
    if snap_problem:
        p.append(f"run.json: parameters.{snap_problem}")
    # Weekly full records, and a raw container whose stamps the sidecar matched: the sidecar's snapshot_date
    # is the extraction's own stamp (resolve_snapshot_date), so a manifest giving another date contradicts itself.
    inputs_in = run.get("inputs") if isinstance(run.get("inputs"), dict) else {}
    study_in = inputs_in.get("study_records")
    mesh_in = inputs_in.get("condition_mesh")
    if (
        not snap_problem
        and isinstance(study_in, dict)
        and isinstance(mesh_in, dict)
        and (
            study_in.get("input_format") == "full_records"
            or mesh_in.get("provenance_check") == MESH_STAMPS_MATCHED_RAW
        )
        and mesh_in.get("snapshot_date") is not None
        and mesh_in.get("snapshot_date") != snap_value
    ):
        p.append(
            f"run.json: parameters.snapshot_date {snap_value!r} differs from inputs.condition_mesh."
            f"snapshot_date {mesh_in.get('snapshot_date')!r}, the extraction's own stamp"
        )

    # Check per-trial invariants
    nct_ids: list[str] = []
    for row in trial_rows:
        nct = row.get("nct_id")
        if not isinstance(nct, str) or not nct:
            p.append("trials.jsonl.gz: trial row missing nct_id")
            continue
        if not re.fullmatch(r"NCT[0-9]{8}", nct):  # ASCII digits only (\d matches other scripts)
            # The published key every NCT join and link uses; the build refuses such input, and an
            # edited trials file is refused here.
            p.append(f"trials.jsonl.gz: trial row nct_id {nct!r} is not NCT followed by eight digits")
        nct_ids.append(nct)
        if row.get("schema_version") != SCHEMA_VERSION or row.get("run_id") != run_id:
            p.append(f"{nct}: trial row schema_version or run_id mismatch")

        geo = row.get("geography") or {}
        if geo.get("unit_basis") != UNIT_BASIS:
            p.append(f"{nct}: geography.unit_basis != {UNIT_BASIS!r}")
        fp = geo.get("site_footprint_class")
        if fp not in SITE_FOOTPRINT_CLASSES:
            p.append(f"{nct}: invalid site_footprint_class {fp!r}")
        sbin = geo.get("us_site_share_bin")
        if fp == "multinational_with_us_sites":
            # dap.geography assigns this class only with >= 1 US and >= 1 non-US
            # facility, so its counts are integers with 0 < n_us < n_with_country.
            # Anything else (a string, float, bool, null or missing count) is a
            # contract problem, never converted, and never passed to
            # classify_us_site_share_bin, which raises outside that range.
            n_us = geo.get("n_us_facilities")
            n_with = geo.get("n_facilities_with_country")
            counts_ok = (
                all(isinstance(v, int) and not isinstance(v, bool) for v in (n_us, n_with))
                and 0 < n_us < n_with
            )
            if sbin not in US_SITE_SHARE_BINS:
                p.append(f"{nct}: multinational_with_us_sites missing valid us_site_share_bin")
            elif not counts_ok:
                p.append(
                    f"{nct}: multinational_with_us_sites needs integer facility counts with "
                    f"0 < n_us_facilities < n_facilities_with_country (got {n_us!r} / {n_with!r})"
                )
            else:
                want_bin = classify_us_site_share_bin(n_us, n_with)
                if sbin != want_bin:
                    p.append(f"{nct}: us_site_share_bin {sbin!r} != {want_bin!r}")
        elif sbin is not None:
            p.append(f"{nct}: non-multinational footprint {fp!r} carries us_site_share_bin {sbin!r}")

        demo = row.get("demographics") or {}
        for dim, keys in (("race", RACE_CLASSIFIED_KEYS), ("ethnicity", ETHNICITY_CLASSIFIED_KEYS)):
            dblock = demo.get(dim) or {}
            st = dblock.get("table_status")
            if st not in TABLE_STATUSES or demo.get(f"{dim}_table_status") != st:
                p.append(f"{nct}: invalid or mismatched {dim}_table_status {st!r}")
            if st == "combined_race_ethnicity_quarantined":
                if (
                    dblock.get("usable_for_omb_pooling") is not False
                    or dblock.get("counts") is not None
                    or dblock.get("of_baseline_total") is not None
                    or dblock.get("of_classified") is not None
                ):
                    p.append(f"{nct}: combined_race_ethnicity_quarantined must exclude counts/shares from OMB pooling")
            if st == "whole_population_group_unidentified" and (
                dblock.get("usable_for_omb_pooling") is not False
                or any(
                    dblock.get(k) is not None
                    for k in ("counts", "baseline_total", "of_baseline_total", "of_classified")
                )
            ):
                p.append(f"{nct}: whole_population_group_unidentified must carry no counts, total or shares")
            if st == "single_category_ethnicity" and dblock.get("of_classified") is not None:
                p.append(f"{nct}: single_category_ethnicity must withhold of_classified as None")
            if dblock.get("classified_total") == 0 and dblock.get("of_classified") is not None:
                p.append(f"{nct}: {dim} classified_total == 0 must withhold of_classified as None")

            of_b = dblock.get("of_baseline_total")
            if isinstance(of_b, dict):
                b_tot = int(dblock.get("baseline_total") or 0)
                c_tot = int(dblock.get("classified_total") or 0)
                unk_c = int(dblock.get("unknown_count") or 0)
                unm_c = int(dblock.get("unmapped_count") or 0)
                if c_tot + unk_c + unm_c != b_tot or b_tot <= 0:
                    p.append(f"{nct}: {dim} denominators do not sum to baseline_total")
                for k in keys:
                    entry = of_b.get(k) or {}
                    fl = entry.get("floor")
                    ce = entry.get("ceiling")
                    ub = entry.get("unknown_bounds")
                    ref_m = entry.get("reference_median")
                    ds = entry.get("direction_settled")
                    if ub != [fl, ce] or not (isinstance(fl, (int, float)) and isinstance(ce, (int, float))):
                        p.append(f"{nct}: {dim}.{k} malformed unknown_bounds")
                    elif not (-1e-12 <= fl <= ce <= 1.0 + 1e-12):
                        p.append(f"{nct}: {dim}.{k} bounds [{fl}, {ce}] outside [0, 1]")
                    if ref_m is None:
                        if ds is not None:
                            p.append(f"{nct}: {dim}.{k} direction_settled must be None when reference_median is None")
                    else:
                        want_ds = bool(fl > ref_m or ce < ref_m)
                        if ds is not want_ds:
                            p.append(f"{nct}: {dim}.{k} direction_settled {ds!r} != {want_ds!r}")

        sg = demo.get("sex_gender") or {}
        sg_st = sg.get("sex_report_status")
        if sg_st not in SEX_GENDER_STATES or demo.get("sex_report_status") != sg_st:
            p.append(f"{nct}: invalid sex_report_status {sg_st!r}")
        if sg_st == "parse_error" and any(
            sg.get(k) is not None for k in ("reported_sex", "reported_gender", "reported_both")
        ):
            p.append(f"{nct}: parse_error sex_gender row must carry None outcomes")
        if sg.get("is_participant_count") is False and (
            sg.get("percent_female") is not None or sg.get("female_share") is not None
        ):
            p.append(f"{nct}: non-participant sex_gender row must stay out of composition")
        # A participant-count table whose counts are not whole carries no composition, and says why
        # (`extract_sex_gender`); a build before that rule published NCT01507090's 51.7/48.3 as
        # percent_female 51.7 beside a Denominator B counted from the rounded 52 and 48.
        not_whole = sg.get("is_participant_count") is True and any(
            isinstance(sg.get(k), (int, float)) and not isinstance(sg.get(k), bool) and not float(sg[k]).is_integer()
            for k in ("n_female", "n_male", "n_unknown", "n_gender_diverse", "n_ambiguous_gender")
        )
        # A table with no one whole-population group withholds it too, naming the group problem
        # (`demographics._withhold_sex_composition`); the groups are not in the trial row, so that reason
        # is checked only for its value, and counts that are not whole need some reason.
        withheld = sg.get("composition_withheld_reason")
        if withheld not in SEX_COMPOSITION_WITHHELD_REASONS or (not_whole and withheld is None) or (
            withheld == "participant_counts_not_whole" and not not_whole
        ):
            p.append(f"{nct}: sex_gender composition_withheld_reason {withheld!r} does not follow from its counts")
        if withheld is not None and any(
            sg.get(k) is not None for k in ("percent_female", "female_share", "of_parsed_total", "of_classified")
        ):
            p.append(f"{nct}: sex_gender row with a withheld composition must stay out of composition")
        if isinstance(sg.get("of_parsed_total"), dict):
            parsed_sum = int(
                round(
                    sum(
                        float(sg.get(k) or 0.0)
                        for k in ("n_female", "n_male", "n_unknown", "n_gender_diverse", "n_ambiguous_gender")
                    )
                )
            )
            fem_b = (sg["of_parsed_total"].get("female") or {}).get("baseline_total")
            if fem_b != parsed_sum:
                p.append(f"{nct}: sex_gender parsed denominator folded enrollment_minus_parsed")

        sp = row.get("sponsors") or {}
        lead = sp.get("lead") or {}
        if lead.get("review_state") not in SPONSOR_REVIEW_STATES:
            p.append(f"{nct}: invalid lead sponsor review_state {lead.get('review_state')!r}")
        if lead.get("review_state") in ("reviewed_excluded", "unreviewed") and (
            lead.get("canonical_companies") or lead.get("registered_entities")
        ):
            p.append(f"{nct}: non-attributed lead sponsor carries canonical/entity attribution")
        for col in sp.get("collaborators") or []:
            if col.get("review_state") not in SPONSOR_REVIEW_STATES:
                p.append(f"{nct}: invalid collaborator review_state")
            if col.get("review_state") in ("reviewed_excluded", "unreviewed") and (
                col.get("canonical_companies") or col.get("registered_entities")
            ):
                p.append(f"{nct}: non-attributed collaborator carries canonical/entity attribution")

    if nct_ids != sorted(nct_ids) or len(set(nct_ids)) != len(nct_ids):
        p.append("trials.jsonl.gz: nct_ids are not unique and strictly sorted")

    # Check strata invariants
    strata_doc = loaded.get("strata.json") or {}
    strata_list = strata_doc.get("strata")
    if not isinstance(strata_list, list):
        p.append("strata.json: strata must be a list")
        strata_list = []
    ont_fp_sum = sum(
        int((s.get("pooled") or {}).get("n_trials") or 0)
        for s in strata_list
        if s.get("stratum_kind") == "ontology_footprint"
    )
    if ont_fp_sum != len(trial_rows):
        p.append(
            f"strata.json: ontology_footprint n_trials sum ({ont_fp_sum}) != trial count ({len(trial_rows)})"
        )
    multinat_count = sum(
        1 for r in trial_rows if r["geography"]["site_footprint_class"] == "multinational_with_us_sites"
    )
    ont_bin_sum = sum(
        int((s.get("pooled") or {}).get("n_trials") or 0)
        for s in strata_list
        if s.get("stratum_kind") == "ontology_us_share_bin"
    )
    if ont_bin_sum != multinat_count:
        p.append(
            f"strata.json: ontology_us_share_bin n_trials sum ({ont_bin_sum}) != multinational count ({multinat_count})"
        )

    id_counts = Counter(str(s.get("stratum_id")) for s in strata_list)
    dup_ids = sorted(i for i, n in id_counts.items() if n > 1)
    if dup_ids:
        p.append(f"strata.json: {len(dup_ids)} stratum_id(s) are not unique (first: {dup_ids[0]})")

    # Each dimension's reference source (`benchmarks.build_benchmarks` resolves it per dimension): a stratum
    # of this build, named wherever that dimension carries a reference median.
    for row in trial_rows:
        demo = row.get("demographics") if isinstance(row.get("demographics"), dict) else {}
        refs = demo.get("reference_strata")
        if not isinstance(refs, dict) or set(refs) != set(REFERENCE_DIMENSIONS):
            p.append(f"{row.get('nct_id')}: demographics.reference_strata must name {', '.join(REFERENCE_DIMENSIONS)}")
            continue
        for dim in REFERENCE_DIMENSIONS:
            src = refs[dim]
            if src is None:
                if _carries_reference_median(demo, dim):
                    p.append(
                        f"{row.get('nct_id')}: {dim} carries a reference_median but reference_strata.{dim} is None"
                    )
            elif not (
                isinstance(src, dict)
                and set(src) == {"stratum_id", "withheld_small_cell"}
                and isinstance(src["withheld_small_cell"], bool)
                and isinstance(src["stratum_id"], str)
                and src["stratum_id"] in id_counts
            ):
                p.append(f"{row.get('nct_id')}: reference_strata.{dim} must name a stratum of strata.json")

    for s in strata_list:
        sid = s.get("stratum_id")
        if s.get("stratum_kind") not in STRATUM_KINDS:
            p.append(f"{sid}: unknown stratum_kind {s.get('stratum_kind')!r}")
        pooled = s.get("pooled") or {}
        if pooled.get("unit_basis") != UNIT_BASIS:
            p.append(f"{sid}: pooled.unit_basis != {UNIT_BASIS!r}")
        _check_distribution_obj(pooled.get("us_facility_share_distribution"), f"{sid}.us_fac", min_cell, p)
        for cat in RACE_CLASSIFIED_KEYS:
            _check_distribution_obj(
                ((pooled.get("race") or {}).get("of_baseline_total") or {}).get(cat),
                f"{sid}.race.A.{cat}",
                min_cell,
                p,
            )
            _check_distribution_obj(
                ((pooled.get("race") or {}).get("of_classified") or {}).get(cat),
                f"{sid}.race.B.{cat}",
                min_cell,
                p,
            )
        for cat in ETHNICITY_CLASSIFIED_KEYS:
            _check_distribution_obj(
                ((pooled.get("ethnicity") or {}).get("of_baseline_total") or {}).get(cat),
                f"{sid}.ethnicity.A.{cat}",
                min_cell,
                p,
            )
            _check_distribution_obj(
                ((pooled.get("ethnicity") or {}).get("of_classified") or {}).get(cat),
                f"{sid}.ethnicity.B.{cat}",
                min_cell,
                p,
            )
        _check_distribution_obj(
            (pooled.get("sex_gender") or {}).get("female_share_of_mf"),
            f"{sid}.sex_gender.female",
            min_cell,
            p,
        )

    # Check sponsors invariants
    sponsors_doc = loaded.get("sponsors.json") or {}
    for view_key in ("current_owner", "as_registered"):
        for sc in sponsors_doc.get(view_key) or []:
            label = sc.get("company") or sc.get("entity")
            for role, rblock in (sc.get("by_role") or {}).items():
                if rblock.get("unit_basis") != UNIT_BASIS:
                    p.append(f"sponsors.{view_key}.{label}.{role}: missing unit_basis")
                for bkey, bval in (rblock.get("indication_matched_benchmarking") or {}).items():
                    nb = bval.get("n_benchmarked_trials")
                    if isinstance(nb, int) and nb < min_cell:
                        if (
                            bval.get("withheld_small_cell") is not True
                            or bval.get("peer_percentile_rank") is not None
                            or bval.get("delta_vs_peer_median") is not None
                        ):
                            p.append(
                                f"sponsors.{view_key}.{label}.{role}.{bkey}: small cell failed to withhold percentiles"
                            )

    # Check summary.json structure AND exact recount parity
    summary_doc = loaded.get("summary.json")
    if isinstance(summary_doc, dict):
        denoms = summary_doc.get("denominators") or {}
        for dname, dval in denoms.items():
            if not isinstance(dval, dict) or not isinstance(dval.get("n"), int) or not str(dval.get("definition") or "").strip():
                p.append(f"summary.json: denominator {dname!r} lacks integer n and definition")

        all_count_objs: list[tuple[str, Any]] = [
            (f"metrics[{i}]", m) for i, m in enumerate(summary_doc.get("metrics") or [])
        ]
        for bname, blist in (summary_doc.get("breakdowns") or {}).items():
            if not isinstance(blist, list):
                p.append(f"summary.json: breakdown {bname!r} is not a list")
                continue
            for i, item in enumerate(blist):
                all_count_objs.append((f"breakdowns.{bname}[{i}]", item))

        seen_ids: set[str] = set()
        for loc, item in all_count_objs:
            if not isinstance(item, dict):
                p.append(f"summary.json {loc}: count entry is not an object")
                continue
            cid = item.get("id")
            if cid in seen_ids:
                p.append(f"summary.json {loc}: duplicate metric id {cid!r}")
            if isinstance(cid, str):
                seen_ids.add(cid)
            for req_key in ("value", "numerator", "denominator", "definition"):
                if req_key not in item:
                    p.append(f"summary.json {loc}: missing required key {req_key!r}")
            num = item.get("numerator")
            val = item.get("value")
            dkey = item.get("denominator")
            defn = item.get("definition")
            if not isinstance(num, int) or isinstance(num, bool) or val != num:
                p.append(f"summary.json {loc}: value ({val!r}) and numerator ({num!r}) must be equal integers")
            if dkey not in denoms:
                p.append(f"summary.json {loc}: unknown denominator {dkey!r}")
            elif isinstance(num, int) and not (0 <= num <= int(denoms[dkey]["n"])):
                p.append(f"summary.json {loc}: numerator {num} outside 0..{denoms[dkey]['n']}")
            if not isinstance(defn, str) or not defn.strip():
                p.append(f"summary.json {loc}: empty definition")

        if isinstance(loaded.get("sponsors.json"), dict) and "attribution_summary" in loaded["sponsors.json"]:
            recounted = summarise(
                trial_rows,
                strata_list,
                {
                    "attribution_summary": loaded["sponsors.json"]["attribution_summary"],
                    "current_owner": loaded["sponsors.json"].get("current_owner") or [],
                    "as_registered": loaded["sponsors.json"].get("as_registered") or [],
                },
            )
            actual_body = {
                k: v for k, v in summary_doc.items() if k not in ("schema_version", "run_id")
            }
            if dump(actual_body) != dump(recounted):
                p.append("summary.json: counts do not match recomputation from trials/strata/sponsors")

    return p


def replace_refusal(out: Path, expected_names: set[str]) -> str | None:
    """Return a refusal message when ``out`` is unsafe to replace, or ``None`` when safe."""
    if not out.exists() and not out.is_symlink():
        return None
    if out.is_symlink() or not out.is_dir():
        return f"{out} is not a directory"
    entries = list(out.iterdir())
    if not entries:
        return None
    odd = sorted(p.name for p in entries if p.is_symlink() or not p.is_file() or p.name not in expected_names)
    if odd:
        return f"{out} holds entries that are not this family's files ({odd[:5]})"
    missing = sorted(expected_names - {p.name for p in entries})
    if missing:
        return f"{out} lacks {missing[:5]}, so it is not a complete DAP benchmark family"
    try:
        run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"{out}/run.json is not readable JSON"
    run_id = run.get("run_id") if isinstance(run, dict) else None
    if not (isinstance(run_id, str) and run_id.startswith("dap-")):
        return f"{out}/run.json does not name a DAP benchmark run"
    return None


def write_artifact_family(built: dict[str, Any], out: Path) -> None:
    """Atomically stage and swap a verified DAP artifact family into ``out``."""
    expected_names = set(SIBLING_FILES) | {"run.json"}
    refusal = replace_refusal(out, expected_names)
    if refusal is not None:
        raise ValueError(f"{refusal}; refusing to replace it")

    raw_bytes: dict[str, bytes] = built["_bytes"]
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f"{out.name}.staging.", dir=out.parent))
    try:
        for name in sorted(expected_names):
            (stage / name).write_bytes(raw_bytes[name])
        old: Path | None = None
        if out.exists():
            old = Path(tempfile.mkdtemp(prefix=f"{out.name}.replaced.", dir=out.parent))
            out.rename(old / out.name)
        try:
            stage.rename(out)
        except OSError:
            if old is not None and (old / out.name).exists():
                (old / out.name).rename(out)
                old.rmdir()
            raise
        if old is not None:
            shutil.rmtree(old)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
