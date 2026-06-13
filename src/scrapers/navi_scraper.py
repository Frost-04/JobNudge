from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class NaviScraper(BaseScraper):
    """
    Scraper for NAVI careers (Turbohire platform).

    NAVI uses a Turbohire React SPA that loads job data via a REST API
    at ``careerpagev2/filteredjobs``.  We intercept that API response to
    get all job data (including full HTML descriptions) in one shot —
    no DOM parsing or click-through enrichment needed.

    API fields used:
        JobTitle      → title
        JobCode       → job_id (e.g. "NL-29752", "N-63157")
        Location      → JSON string parsed for address
        PublishedDate → posted_date
        JobDescV2     → description (HTML, cleaned to text)
    """

    MAX_CARDS = 10
    API_PATTERN = "careerpagev2/filteredjobs"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")

        jobs: list[Job] = []

        # Container for the API response.
        api_result: dict | None = None

        async def _capture_api(response):
            """Capture the filteredjobs API response."""
            nonlocal api_result
            if self.API_PATTERN in response.url and api_result is None:
                try:
                    data = await response.json()
                    if isinstance(data, dict) and "Result" in data:
                        api_result = data
                        self.logger.debug(
                            "Captured NAVI API: %d total jobs",
                            data.get("Total", 0),
                        )
                except Exception:
                    pass

        page.on("response", _capture_api)

        try:
            await page.goto(
                source_url, wait_until="domcontentloaded", timeout=60000
            )

            # Wait for the API call to complete (poll up to 60s).
            for _ in range(60):
                if api_result is not None:
                    break
                await page.wait_for_timeout(1000)

            if api_result is None:
                self.logger.warning("NAVI API response not captured")
                return jobs

            raw_jobs: list[dict] = api_result.get("Result", [])

            seen_job_ids: set[str] = set()
            enriched: list[Job] = []

            for raw in raw_jobs[: self.MAX_CARDS]:
                job = self._parse_job(raw, source_url)
                if not job:
                    continue
                if job.job_id and job.job_id in seen_job_ids:
                    continue
                if job.job_id:
                    seen_job_ids.add(job.job_id)
                enriched.append(job)

            return enriched

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Job parsing from API data
    # ------------------------------------------------------------------

    def _parse_job(self, raw: dict, source_url: str) -> Job | None:
        title = self._clean_text(raw.get("JobTitle", ""))
        if not title:
            return None

        job_code = self._clean_text(raw.get("JobCode", ""))

        # Location is a JSON string like:
        #   [{"Address":"Bengaluru, Karnataka, India","PlaceId":null,...}]
        location_raw = raw.get("Location", "")
        location = self._extract_location(location_raw) if location_raw else ""

        # PublishedDate is ISO format.
        posted_date = None
        published = raw.get("PublishedDate", "")
        if published:
            try:
                dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                posted_date = dt.strftime("%d %b %Y")
            except (ValueError, TypeError):
                posted_date = published

        # Build description from the HTML description field.
        description = None
        if not self._should_exclude(title):
            html_desc = raw.get("JobDescV2", "")
            if html_desc:
                description = self._html_to_text(html_desc)
        else:
            self.logger.debug("Excluding NAVI job: %s", title)

        return Job(
            job_id=job_code,
            company=self.company_config.get("name", "NAVI"),
            title=title,
            location=location,
            url=source_url,
            source_url=source_url,
            posted_date=posted_date,
            description=description,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    @staticmethod
    def _extract_location(location_raw: str) -> str:
        """Parse the JSON location field and return the address string."""
        try:
            locs = json.loads(location_raw)
            if isinstance(locs, list) and locs:
                return locs[0].get("Address", "")
        except (json.JSONDecodeError, TypeError, IndexError):
            pass
        # Fallback: strip JSON artifacts.
        cleaned = re.sub(r'[\[\]{}"\\]', "", location_raw)
        return cleaned.strip()

    @staticmethod
    def _html_to_text(html: str) -> str:
        """Convert HTML description to clean text."""
        soup = BeautifulSoup(html, "html.parser")
        return soup.get_text(separator=" ", strip=True)

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()
