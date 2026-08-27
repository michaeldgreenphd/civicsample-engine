"""
ClinicalTrials.gov API v2 Client
"""
import time
import requests
from typing import Iterator, Dict, Any, Optional, List

class CTGovAPIClient:
    BASE_URL = "https://clinicaltrials.gov/api/v2"

    DEFAULT_FIELDS = [
        "NCTId",
        "BriefTitle",
        "StudyType",
        "Phase",
        "EnrollmentCount",
        "OverallStatus",
        "ResultsFirstPostDate",
        "LastUpdatePostDate",
        "LeadSponsorClass",
        "LocationCountry",
        "ResultsSection"
    ]

    def __init__(self, page_size: int = 100, rate_limit_delay: float = 0.5):
        self.page_size = min(page_size, 100)
        self.rate_limit_delay = rate_limit_delay
        self.session = requests.Session()
        self._last_request_time = 0

    def _rate_limit(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self.rate_limit_delay:
            time.sleep(self.rate_limit_delay - elapsed)
        self._last_request_time = time.time()

    def search_studies(self, page_token: Optional[str] = None, **filters) -> Dict[str, Any]:
        self._rate_limit()

        # Build advanced filter for studies with results
        # Include date range if specified
        if filters.get("results_after") and filters.get("results_before"):
            # Both start and end date provided
            date_filter = f"AREA[ResultsFirstPostDate]RANGE[{filters['results_after']},{filters['results_before']}]"
        elif filters.get("results_after"):
            # Only start date - from specified date to MAX
            date_filter = f"AREA[ResultsFirstPostDate]RANGE[{filters['results_after']},MAX]"
        elif filters.get("results_before"):
            # Only end date - from MIN to specified date
            date_filter = f"AREA[ResultsFirstPostDate]RANGE[MIN,{filters['results_before']}]"
        else:
            # No date filter - just require results exist
            date_filter = "AREA[ResultsFirstPostDate]RANGE[MIN,MAX]"

        params = {
            "pageSize": self.page_size,
            "format": "json",
            "filter.advanced": date_filter
        }

        if page_token:
            params["pageToken"] = page_token
        if filters.get("condition"):
            params["query.cond"] = filters["condition"]

        response = self.session.get(f"{self.BASE_URL}/studies", params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def get_study(self, nct_id: str) -> Dict[str, Any]:
        """Fetch a single study by NCT ID.

        The paginated /studies search endpoint occasionally returns the
        category/class structure without the nested measurements arrays for
        large or complex studies.  Fetching the study individually always
        returns the complete record.
        """
        self._rate_limit()
        response = self.session.get(
            f"{self.BASE_URL}/studies/{nct_id}",
            params={"format": "json"},
            timeout=60
        )
        response.raise_for_status()
        data = response.json()
        # /studies/{nctId} returns the study object directly; guard against
        # the unlikely case where it arrives wrapped in {"studies": [...]}
        if "studies" in data:
            return data["studies"][0]
        return data

    def iter_all_studies_with_results(self, limit: Optional[int] = None, **filters) -> Iterator[Dict]:
        page_token = None
        count = 0

        while True:
            data = self.search_studies(page_token=page_token, **filters)
            studies = data.get("studies", [])

            if not studies:
                break

            for study in studies:
                yield study
                count += 1
                if limit and count >= limit:
                    return

            page_token = data.get("nextPageToken")
            if not page_token:
                break
