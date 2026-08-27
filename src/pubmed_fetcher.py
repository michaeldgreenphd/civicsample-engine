"""
PubMed API Client for finding publications linked to clinical trials

Uses the NCBI E-utilities API to search for publications that reference
a specific NCT ID.
"""
import time
import requests
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional
import logging

logger = logging.getLogger(__name__)

class PubMedFetcher:
    """Fetch publication data from PubMed API."""

    BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(self, rate_limit_delay: float = 0.34):
        """
        Initialize PubMed fetcher.

        Args:
            rate_limit_delay: Delay between requests in seconds.
                             NCBI allows up to 3 requests/second without API key.
        """
        self.rate_limit_delay = rate_limit_delay
        self._last_request_time = 0
        self.session = requests.Session()

    def _rate_limit(self):
        """Enforce rate limiting between API calls."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.rate_limit_delay:
            time.sleep(self.rate_limit_delay - elapsed)
        self._last_request_time = time.time()

    def search_by_nct_id(self, nct_id: str) -> List[str]:
        """
        Search PubMed for publications referencing a specific NCT ID.

        Args:
            nct_id: The NCT ID to search for (e.g., "NCT01234567")

        Returns:
            List of PMIDs found
        """
        self._rate_limit()

        try:
            params = {
                "db": "pubmed",
                "term": nct_id,
                "retmode": "json",
                "retmax": 100  # Max results to retrieve
            }

            response = self.session.get(
                f"{self.BASE_URL}/esearch.fcgi",
                params=params,
                timeout=10
            )
            response.raise_for_status()

            data = response.json()
            pmids = data.get("esearchresult", {}).get("idlist", [])

            if pmids:
                logger.info(f"Found {len(pmids)} PubMed article(s) for {nct_id}")

            return pmids

        except Exception as e:
            logger.warning(f"Error searching PubMed for {nct_id}: {str(e)}")
            return []

    def fetch_article_details(self, pmids: List[str]) -> List[Dict]:
        """
        Fetch details for a list of PMIDs.

        Args:
            pmids: List of PubMed IDs

        Returns:
            List of article detail dictionaries
        """
        if not pmids:
            return []

        self._rate_limit()

        try:
            params = {
                "db": "pubmed",
                "id": ",".join(pmids),
                "retmode": "xml"
            }

            response = self.session.get(
                f"{self.BASE_URL}/esummary.fcgi",
                params=params,
                timeout=10
            )
            response.raise_for_status()

            # Parse XML response
            root = ET.fromstring(response.content)
            articles = []

            for doc_sum in root.findall(".//DocSum"):
                pmid = doc_sum.find("Id").text if doc_sum.find("Id") is not None else None

                title = ""
                authors = ""
                journal = ""
                pub_date = ""

                for item in doc_sum.findall("Item"):
                    name = item.get("Name")
                    if name == "Title":
                        title = item.text or ""
                    elif name == "AuthorList":
                        author_items = item.findall("Item")
                        authors = ", ".join([a.text for a in author_items[:3] if a.text])
                        if len(author_items) > 3:
                            authors += ", et al."
                    elif name == "Source":
                        journal = item.text or ""
                    elif name == "PubDate":
                        pub_date = item.text or ""

                if pmid:
                    articles.append({
                        "pmid": pmid,
                        "title": title,
                        "authors": authors,
                        "journal": journal,
                        "pub_date": pub_date,
                        "citation": f"{authors} {title} {journal}. {pub_date}".strip()
                    })

            return articles

        except Exception as e:
            logger.warning(f"Error fetching article details: {str(e)}")
            return []

    def get_publications_for_study(self, nct_id: str) -> List[Dict]:
        """
        Get all publication details for a study NCT ID.

        Args:
            nct_id: The NCT ID to search for

        Returns:
            List of publication dictionaries with pmid, title, authors, etc.
        """
        pmids = self.search_by_nct_id(nct_id)
        if not pmids:
            return []

        articles = self.fetch_article_details(pmids)

        # Add source and type information
        for article in articles:
            article["source"] = "pubmed"
            article["type"] = "DERIVED"

        return articles
