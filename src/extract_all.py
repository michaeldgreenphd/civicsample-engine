"""
Extract all demographics data from ClinicalTrials.gov

This orchestrator script extracts race, ethnicity, sex, and gender data
from clinical trials and saves them to a unified JSON file.

Sex and gender are produced twice during the migration to the manuscript's
parser (sex_gender_parser.py, vendored unchanged in src/):

  study["sex"], study["gender"]   the legacy extractors (src/sex_extractor.py,
                                  src/gender_extractor.py). They serve the
                                  dashboard's sg=v1 path until cutover and can
                                  be switched off with --no-legacy-sex-gender.
  study["sex_gender"]             the new row (src/sex_gender_table.py): the
                                  parser's counts, five-state status, outcomes,
                                  provenance, and enrollment_minus_parsed. It
                                  has no denominator balancing.

Alongside the output JSON the run writes sex_gender_raw_measures.jsonl.gz:
one line per trial with the SELECTED sex/gender measures verbatim, so any
later rule bump can re-parse every retained snapshot without a registry pull
(scripts/build_sex_gender_table.py --from-raw).
"""
import argparse
import gzip
import json
import logging
from pathlib import Path
from typing import Optional
from tqdm import tqdm

from src.api_client import CTGovAPIClient
from src.utils import get_study_metadata, save_json, get_baseline_measures, get_overall_group_id, extract_demographic_breakdown
from src.race_extractor import extract_race_data
from src.ethnicity_extractor import extract_ethnicity_data
from src.sex_extractor import extract_sex_data
from src.gender_extractor import extract_gender_data
from src.condition_classifier import classify_conditions
from src.pubmed_fetcher import PubMedFetcher
from src import sex_gender_table as sgt

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

RAW_MEASURES_FILENAME = "sex_gender_raw_measures.jsonl.gz"


def extract_demographics_from_study(study: dict, pubmed_fetcher: Optional[PubMedFetcher] = None,
                                    snapshot_date: Optional[str] = None, legacy_sex_gender: bool = True,
                                    refetched: bool = False) -> Optional[dict]:
    """
    Extract all demographic data from a single study.

    Args:
        study: Study data from ClinicalTrials.gov API
        pubmed_fetcher: Optional PubMedFetcher instance for finding publications
        snapshot_date: YYYY-MM-DD stamped on the sex_gender row (default: today, UTC)
        legacy_sex_gender: run the legacy sex/gender extractors (sg=v1) as well
        refetched: the record came from a per-study re-fetch (recorded on the row)

    Returns:
        Dictionary containing study metadata and all demographic data
    """
    try:
        metadata = get_study_metadata(study)
        race_data = extract_race_data(study)
        ethnicity_data = extract_ethnicity_data(study)

        # ── Legacy sex/gender extraction (sg=v1), kept until cutover ──
        # Each extractor uses the same iterative loop pattern as Race and
        # Ethnicity: exhaustively checks Standard → Customized → Combined
        # tables before declaring "Not Reported". Not called when the legacy
        # path is switched off; the keys are then None, never absent.
        sex_data = extract_sex_data(study) if legacy_sex_gender else None
        gender_data = extract_gender_data(study) if legacy_sex_gender else None

        # ── Manuscript parser (sg=v2): one flat row, no balancing ──
        # A parser failure on one record is that record's parse_error state
        # (outcomes None), never a dropped trial: the study and its raw
        # measures are retained and the audit counts it.
        snap = snapshot_date or sgt.today_utc()
        try:
            sex_gender_row = sgt.build_row(study, snap, refetched=refetched)
        except Exception as e:  # noqa: BLE001 - the state exists for exactly this
            nct = ((study.get("protocolSection") or {}).get("identificationModule") or {}).get("nctId")
            enr = (((study.get("protocolSection") or {}).get("designModule") or {}).get("enrollmentInfo") or {}).get("count")
            logger.error(f"[{nct}] sex/gender parser failed ({type(e).__name__}: {e}); row filed as parse_error")
            sex_gender_row = sgt.parse_error_row(nct, enr, snap)

        # Extract demographic breakdowns for interactive display
        baseline_measures = get_baseline_measures(study)
        overall_group_id = get_overall_group_id(study)
        race_breakdown = extract_demographic_breakdown(baseline_measures, "race", overall_group_id)
        ethnicity_breakdown = extract_demographic_breakdown(baseline_measures, "ethnicity", overall_group_id)
        sex_breakdown = extract_demographic_breakdown(baseline_measures, "sex", overall_group_id)

        # If no references found and PubMed fetcher provided, try PubMed
        if pubmed_fetcher and not metadata.get("references"):
            nct_id = metadata.get("nct_id")
            if nct_id:
                try:
                    pubmed_refs = pubmed_fetcher.get_publications_for_study(nct_id)
                    if pubmed_refs:
                        metadata["references"] = pubmed_refs
                        logger.info(f"Found {len(pubmed_refs)} PubMed reference(s) for {nct_id}")
                except Exception as e:
                    logger.warning(f"PubMed fetch failed for {nct_id}: {str(e)}")

        # Classify conditions into hierarchical categories
        condition_info = classify_conditions(metadata.get("conditions", []))

        return {
            **metadata,
            "primary_condition": condition_info["primary_condition"],
            "secondary_condition": condition_info["secondary_condition"],
            "condition_classifications": condition_info["all_classifications"],
            "race": race_data,
            "ethnicity": ethnicity_data,
            "sex": sex_data,
            "gender": gender_data,
            # Only the lean subset rides in the parts; the full row is rebuilt
            # from the retained raw measures into sex_gender_parsed.csv.gz.
            "sex_gender": sgt.lean_row(sex_gender_row),
            # Add breakdowns for interactive dashboard
            "raceBreakdown": race_breakdown,
            "ethnicityBreakdown": ethnicity_breakdown,
            "sexBreakdown": sex_breakdown
        }
    except Exception as e:
        logger.error(f"Error extracting from study {study.get('protocolSection', {}).get('identificationModule', {}).get('nctId', 'UNKNOWN')}: {str(e)}")
        return None

def _measurements_present_in_search(study: dict) -> bool:
    """True if any demographic measure in the raw study has at least one measurement entry.

    The search endpoint may return category titles with empty measurement arrays
    for studies whose baseline data hasn't been entered yet — these are genuine
    data gaps, not truncation.  When measurements ARE present (even with 'NA' or
    '0' values) the data is complete; a re-fetch would return the same result.
    Gender-titled tables are included so a stripped gender table is refetched too.
    """
    baseline = (study.get("resultsSection", {})
                .get("baselineCharacteristicsModule", {}))
    for m in baseline.get("measures", []):
        title = m.get("title", "").lower()
        if not any(kw in title for kw in ("race", "ethnicity", "sex", "gender")):
            continue
        for cls in m.get("classes", []):
            for cat in cls.get("categories", []):
                if cat.get("measurements"):
                    return True
            if cls.get("measurements"):
                return True
    return False

def _reported_demographics_are_empty(result: dict) -> bool:
    """True when every reported demographic has all-zero counts.

    The /studies search endpoint sometimes strips the measurements arrays
    out of large or complex studies while keeping the class/category titles.
    When that happens *every* reported demographic shows zero simultaneously.
    Returning True triggers a per-study re-fetch via /studies/{nctId}.
    """
    reported = 0
    empty = 0
    for key in ("race", "ethnicity", "sex"):
        demo = result.get(key) or {}
        if not demo.get("reported"):
            continue
        reported += 1
        totals = demo.get("omb_totals") or demo.get("totals", {})
        if totals and all(v == 0 for v in totals.values()):
            empty += 1
    return reported > 0 and reported == empty

def _log_raw_baseline(study: dict, nct_id: str):
    """Dump a diagnostic summary of demographic measures from the raw API response.

    Only called when a study still shows all-zero counts after an individual
    re-fetch; output appears in GitHub Actions logs for post-mortem analysis.
    """
    import json as _json
    baseline = (study.get("resultsSection", {})
                .get("baselineCharacteristicsModule", {}))
    groups = baseline.get("groups", [])
    logger.warning(f"[{nct_id}] Groups: {_json.dumps(groups)}")
    for m in baseline.get("measures", []):
        title = m.get("title", "")
        if not any(kw in title.lower() for kw in ("race", "ethnicity", "sex", "gender")):
            continue
        logger.warning(f"[{nct_id}] Measure: {title} | paramType: {m.get('paramType')}")
        for cls in m.get("classes", []):
            for cat in cls.get("categories", []):
                measurements = cat.get("measurements", [])
                vals = [x.get("value") for x in measurements]
                logger.warning(f"[{nct_id}]   {cls.get('title','')} > {cat.get('title','')}: "
                               f"{len(measurements)} measurement(s), values={vals}")


def _needs_refetch(result: Optional[dict], study: dict, raw: dict) -> str:
    """Why a record should be re-fetched individually, or "" if it should not.

    legacy   every reported legacy demographic is all-zero and the search record
             carries no measurement entries (the pre-existing trigger)
    sg_v2    the parser filed the trial uninformative / no_measurements and the
             selected measures carry no measurement object (sex_gender_table
             .needs_refetch). This applies to every trial in every pull; the
             pipeline pulls the whole registry each week, so it is not limited
             to trials whose lastUpdatePostDate moved.
    """
    if not result:
        return ""
    if _reported_demographics_are_empty(result) and not _measurements_present_in_search(study):
        return "legacy"
    if sgt.needs_refetch(result.get("sex_gender") or {}, raw):
        return "sg_v2"
    return ""


def write_raw_measures(raw_records: list, path: Path) -> None:
    """One JSON object per line: nct_id, enrollment, snapshot_date, selected measures."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", compresslevel=9) as fh:
        for rec in raw_records:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Extract demographics data from ClinicalTrials.gov"
    )
    parser.add_argument(
        "--output", "-o",
        required=True,
        help="Output file path (will be saved as JSON)"
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=None,
        help="Limit number of studies to extract (for testing)"
    )
    parser.add_argument(
        "--condition", "-c",
        default=None,
        help="Filter by medical condition"
    )
    parser.add_argument(
        "--results-after",
        default=None,
        help="Filter by results posted after date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--fetch-pubmed",
        action="store_true",
        help="Fetch publications from PubMed for studies without references"
    )
    parser.add_argument(
        "--snapshot-date",
        default=None,
        help="YYYY-MM-DD stamped on every sex_gender row and raw-measure record (default: today, UTC)"
    )
    parser.add_argument(
        "--no-legacy-sex-gender",
        action="store_true",
        help="Do not run the legacy sex/gender extractors (sg=v1); study['sex'] and study['gender'] become null"
    )
    args = parser.parse_args()
    snapshot_date = args.snapshot_date or sgt.today_utc()
    legacy = not args.no_legacy_sex_gender

    # Initialize API client
    logger.info("Initializing ClinicalTrials.gov API client...")
    client = CTGovAPIClient(page_size=100, rate_limit_delay=0.5)

    # Initialize PubMed fetcher if requested
    pubmed_fetcher = None
    if args.fetch_pubmed:
        logger.info("Initializing PubMed fetcher...")
        pubmed_fetcher = PubMedFetcher(rate_limit_delay=0.34)

    # Extract data
    logger.info("Starting extraction...")
    logger.info(f"Snapshot date: {snapshot_date}; sex_gender parser rules: {sgt.sgp.PARSER_RULES_VERSION} "
                f"(module {sgt.sgp.__version__}); legacy sex/gender extractors: {'on' if legacy else 'off'}")
    if args.limit:
        logger.info(f"Limiting to {args.limit} studies")
    if args.condition:
        logger.info(f"Filtering by condition: {args.condition}")
    if args.fetch_pubmed:
        logger.info("PubMed fetching enabled for studies without references")

    results = []
    raw_records = []
    errors = 0

    studies = client.iter_all_studies_with_results(
        limit=args.limit,
        condition=args.condition,
        results_after=args.results_after
    )

    refetch_count = 0
    refetch_reasons = {"legacy": 0, "sg_v2": 0}
    still_empty_after_refetch = 0
    # One timestamp for the whole pull: stamped on the output container and on
    # every retained raw-measure record, so a table rebuilt from the raw
    # records carries the extraction it came from.
    extracted_at = sgt.now_utc_iso()
    for study in tqdm(studies, desc="Extracting demographics"):
        result = extract_demographics_from_study(study, pubmed_fetcher=pubmed_fetcher,
                                                 snapshot_date=snapshot_date, legacy_sex_gender=legacy)
        raw = sgt.select_raw_measures(study)
        raw["refetched"] = False

        # The search endpoint occasionally omits measurement values for large
        # studies while keeping category titles intact.  Detect this and
        # re-fetch the individual study to get the complete record.
        # Guard: if the search result already contains measurement entries
        # (even 'NA' or '0') the data is present — skip the costly re-fetch.
        why = _needs_refetch(result, study, raw)
        if why:
            nct_id = result.get("nct_id")
            logger.warning(f"[{nct_id}] re-fetching individually ({why}: measurement arrays absent)")
            try:
                full_study = client.get_study(nct_id)
                result = extract_demographics_from_study(full_study, pubmed_fetcher=pubmed_fetcher,
                                                         snapshot_date=snapshot_date, legacy_sex_gender=legacy,
                                                         refetched=True)
                raw = sgt.select_raw_measures(full_study)
                raw["refetched"] = True          # survives the rebuild from raw measures
                refetch_count += 1
                refetch_reasons[why] += 1
                if _needs_refetch(result, full_study, raw):
                    # Still empty: a posted-but-empty template is a real registry
                    # state. The row stays uninformative / no_measurements with
                    # refetched = True; the audit lists it.
                    still_empty_after_refetch += 1
                    logger.warning(f"[{nct_id}] still no measurement arrays after individual fetch — "
                                   f"kept as parsed (uninformative), refetched=True; logging raw baseline")
                    _log_raw_baseline(full_study, nct_id)
                else:
                    logger.info(f"[{nct_id}] Refetch populated demographics successfully")
            except Exception as e:
                logger.error(f"[{nct_id}] Individual re-fetch failed: {e}")

        if result:
            results.append(result)
            raw["snapshot_date"] = snapshot_date
            raw["extracted_at"] = extracted_at
            raw_records.append(raw)
        else:
            errors += 1

    # Save results
    output_path = Path(args.output)
    if not output_path.suffix:
        output_path = output_path / "demographics.json"

    logger.info(f"Saving {len(results)} studies to {output_path}")
    save_json(results, output_path, extracted_at=extracted_at)

    raw_path = output_path.parent / RAW_MEASURES_FILENAME
    logger.info(f"Saving {len(raw_records)} raw sex/gender measure records to {raw_path}")
    write_raw_measures(raw_records, raw_path)

    # Summary statistics
    logger.info("\n=== Extraction Summary ===")
    logger.info(f"Total studies extracted: {len(results)}")
    logger.info(f"Errors encountered: {errors}")

    if results:
        race_reporting = sum(1 for r in results if (r.get("race") or {}).get("reported"))
        ethnicity_reporting = sum(1 for r in results if (r.get("ethnicity") or {}).get("reported"))
        sex_reporting = sum(1 for r in results if (r.get("sex") or {}).get("reported"))
        gender_reporting = sum(1 for r in results if (r.get("gender") or {}).get("reported"))

        logger.info(f"\nReporting rates (legacy sg=v1 extractors):")
        logger.info(f"  Race: {race_reporting} ({race_reporting/len(results)*100:.1f}%)")
        logger.info(f"  Ethnicity: {ethnicity_reporting} ({ethnicity_reporting/len(results)*100:.1f}%)")
        logger.info(f"  Sex: {sex_reporting} ({sex_reporting/len(results)*100:.1f}%)")
        logger.info(f"  Gender: {gender_reporting} ({gender_reporting/len(results)*100:.1f}%)")

        both_race_ethnicity = sum(1 for r in results if (r.get("race") or {}).get("reported") and (r.get("ethnicity") or {}).get("reported"))
        logger.info(f"  Both race & ethnicity: {both_race_ethnicity} ({both_race_ethnicity/len(results)*100:.1f}%)")

        sc = sgt.status_counts(r["sex_gender"] for r in results if r.get("sex_gender"))
        logger.info(f"\nSex/gender table (sg=v2, rules {sgt.sgp.PARSER_RULES_VERSION}):")
        logger.info(f"  status: {sc['status']}")
        logger.info(f"  outcomes: {sc['outcomes']}")
        logger.info(f"  refetched rows: {sc['refetched']}; is_participant_count False: {sc['is_participant_count_false']}")

    if refetch_count:
        logger.info(f"\nStudies re-fetched individually: {refetch_count} {refetch_reasons}; "
                    f"still without measurement arrays afterwards: {still_empty_after_refetch}")

    logger.info("\nExtraction complete!")

if __name__ == "__main__":
    main()
