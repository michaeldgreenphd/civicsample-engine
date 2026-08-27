"""
Extract all demographics data from ClinicalTrials.gov

This orchestrator script extracts race, ethnicity, sex, and gender data
from clinical trials and saves them to a unified JSON file.
"""
import argparse
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

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def extract_demographics_from_study(study: dict, pubmed_fetcher: Optional[PubMedFetcher] = None) -> dict:
    """
    Extract all demographic data from a single study.

    Args:
        study: Study data from ClinicalTrials.gov API
        pubmed_fetcher: Optional PubMedFetcher instance for finding publications

    Returns:
        Dictionary containing study metadata and all demographic data
    """
    try:
        metadata = get_study_metadata(study)
        race_data = extract_race_data(study)
        ethnicity_data = extract_ethnicity_data(study)

        # ── Unified Sex/Gender extraction ──
        # Each extractor now uses the same iterative loop pattern as Race
        # and Ethnicity: exhaustively checks Standard → Customized →
        # Combined tables before declaring "Not Reported".
        # Combined table row-level routing is handled inside each extractor.
        sex_data = extract_sex_data(study)
        gender_data = extract_gender_data(study)

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
    """
    baseline = (study.get("resultsSection", {})
                .get("baselineCharacteristicsModule", {}))
    for m in baseline.get("measures", []):
        title = m.get("title", "").lower()
        if not any(kw in title for kw in ("race", "ethnicity", "sex")):
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
        demo = result.get(key, {})
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
        if not any(kw in title.lower() for kw in ("race", "ethnicity", "sex")):
            continue
        logger.warning(f"[{nct_id}] Measure: {title} | paramType: {m.get('paramType')}")
        for cls in m.get("classes", []):
            for cat in cls.get("categories", []):
                measurements = cat.get("measurements", [])
                vals = [x.get("value") for x in measurements]
                logger.warning(f"[{nct_id}]   {cls.get('title','')} > {cat.get('title','')}: "
                               f"{len(measurements)} measurement(s), values={vals}")

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
    args = parser.parse_args()

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
    if args.limit:
        logger.info(f"Limiting to {args.limit} studies")
    if args.condition:
        logger.info(f"Filtering by condition: {args.condition}")
    if args.fetch_pubmed:
        logger.info("PubMed fetching enabled for studies without references")

    results = []
    errors = 0

    studies = client.iter_all_studies_with_results(
        limit=args.limit,
        condition=args.condition,
        results_after=args.results_after
    )

    refetch_count = 0
    for study in tqdm(studies, desc="Extracting demographics"):
        result = extract_demographics_from_study(study, pubmed_fetcher=pubmed_fetcher)

        # The search endpoint occasionally omits measurement values for large
        # studies while keeping category titles intact.  Detect this and
        # re-fetch the individual study to get the complete record.
        # Guard: if the search result already contains measurement entries
        # (even 'NA' or '0') the data is present — skip the costly re-fetch.
        if (result
                and _reported_demographics_are_empty(result)
                and not _measurements_present_in_search(study)):
            nct_id = result.get("nct_id")
            logger.warning(f"[{nct_id}] All reported demographics are zero — re-fetching individually")
            try:
                full_study = client.get_study(nct_id)
                result = extract_demographics_from_study(full_study, pubmed_fetcher=pubmed_fetcher)
                refetch_count += 1
                if result and _reported_demographics_are_empty(result):
                    logger.warning(f"[{nct_id}] Still all-zero after individual fetch — logging raw baseline")
                    _log_raw_baseline(full_study, nct_id)
                else:
                    logger.info(f"[{nct_id}] Refetch populated demographics successfully")
            except Exception as e:
                logger.error(f"[{nct_id}] Individual re-fetch failed: {e}")

        if result:
            results.append(result)
        else:
            errors += 1

    # Save results
    output_path = Path(args.output)
    if not output_path.suffix:
        output_path = output_path / "demographics.json"

    logger.info(f"Saving {len(results)} studies to {output_path}")
    save_json(results, output_path)

    # Summary statistics
    logger.info("\n=== Extraction Summary ===")
    logger.info(f"Total studies extracted: {len(results)}")
    logger.info(f"Errors encountered: {errors}")

    if results:
        race_reporting = sum(1 for r in results if r.get("race", {}).get("reported"))
        ethnicity_reporting = sum(1 for r in results if r.get("ethnicity", {}).get("reported"))
        sex_reporting = sum(1 for r in results if r.get("sex", {}).get("reported"))
        gender_reporting = sum(1 for r in results if r.get("gender", {}).get("reported"))

        logger.info(f"\nReporting rates:")
        logger.info(f"  Race: {race_reporting} ({race_reporting/len(results)*100:.1f}%)")
        logger.info(f"  Ethnicity: {ethnicity_reporting} ({ethnicity_reporting/len(results)*100:.1f}%)")
        logger.info(f"  Sex: {sex_reporting} ({sex_reporting/len(results)*100:.1f}%)")
        logger.info(f"  Gender: {gender_reporting} ({gender_reporting/len(results)*100:.1f}%)")

        both_race_ethnicity = sum(1 for r in results if r.get("race", {}).get("reported") and r.get("ethnicity", {}).get("reported"))
        logger.info(f"  Both race & ethnicity: {both_race_ethnicity} ({both_race_ethnicity/len(results)*100:.1f}%)")

    if refetch_count:
        logger.info(f"\nStudies re-fetched individually (empty measurements): {refetch_count}")

    logger.info("\nExtraction complete!")

if __name__ == "__main__":
    main()
