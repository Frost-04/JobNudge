from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import datetime, timezone

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class SwiggyScraper(BaseScraper):
    """
    Scraper for Swiggy careers page (Angular 9 shell + AngularJS 1.x table).

    The Swiggy career page at ``careers.swiggy.com`` embeds a MyNextHire
    (MNH) AngularJS app inside an Angular 9 shell.  Job data is fetched via
    an API endpoint and rendered into an HTML table.

    **Architecture:**

    1. Angular 9 shell renders the filter/search UI.
    2. AngularJS app (loaded from ``swiggy.mynexthire.com``) fetches job data
       from ``POST /employer/careers/reqlist/get``.
    3. The API response contains full job details including ``jdDisplay``
       (the rich-text job description).

    Because the API returns the full description inline, this scraper does
    **not** open individual detail pages.

    **API response format:**

    .. code-block:: json

        {
          "reqDetailsBOList": [
            {
              "reqId": 26579,
              "reqTitle": "Senior Manager - Data Science",
              "location": "Sumadhura Capitol Towers",
              "buName": "Technology",
              "expMin": 10.0,
              "expMax": 12.0,
              "jdDisplay": "Job Title: ...\\n\\nAbout Swiggy..."
            }
          ]
        }

    **Detail page URL pattern (for reference):**

        https://careers.swiggy.com/#/careers/jd/{reqId}
    """

    # ---- API endpoint ----
    REQLIST_API_URL = "https://swiggy.mynexthire.com/employer/careers/reqlist/get"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            # Navigate to the page and wait for the AngularJS API call.
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for the API response that contains job data.
            api_response_body: str | None = None
            try:
                async with page.expect_response(
                    lambda resp: "careers/reqlist/get" in resp.url,
                    timeout=30000,
                ) as resp_info:
                    pass  # The response will be captured
                response = await resp_info.value
                api_response_body = await response.text()
            except Exception:
                # Fallback: wait longer for slower networks
                await asyncio.sleep(10)

            if not api_response_body:
                self.logger.warning("Swiggy: failed to capture API response.")
                return jobs

            # Parse the API response.
            job_list = self._parse_api_response(api_response_body)

            if not job_list:
                self.logger.warning("Swiggy: no jobs found in API response.")
                return jobs

            seen_ids: set[str] = set()

            for job_data in job_list[:max_jobs]:
                job = self._parse_job_from_api(job_data, source_url)
                if not job:
                    continue

                if job.job_id and job.job_id in seen_ids:
                    continue

                seen_ids.add(job.job_id)
                jobs.append(job)

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # API response parsing
    # ------------------------------------------------------------------

    def _parse_api_response(self, body: str) -> list[dict]:
        """Parse the JSON API response into a list of job dicts."""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.logger.warning("Swiggy: failed to parse API JSON.")
            return []

        job_list = data.get("reqDetailsBOList", [])
        if not isinstance(job_list, list):
            return []

        # Filter to Technology only (the URL param already does this server-side,
        # but double-check).
        return [
            j for j in job_list
            if isinstance(j, dict) and j.get("buName") == "Technology"
        ]

    def _parse_job_from_api(self, job_data: dict, source_url: str) -> Job | None:
        """Convert a single API job object into a Job model."""
        req_id = str(job_data.get("reqId", ""))
        title = str(job_data.get("reqTitle", "")).strip()
        location = str(job_data.get("location", "")).strip()
        jd_display = str(job_data.get("jdDisplay", "")).strip()

        if not title:
            return None

        # Build detail URL for reference.
        detail_url = self._build_detail_url(source_url, req_id)

        # Clean the description from API.
        description = self._clean_multiline_text(jd_display) if jd_display else ""

        # Check exclusion BEFORE keeping description (skip for senior roles).
        if self._should_exclude(title):
            description = ""
            self.logger.debug("Skipping description for excluded role: %s", title)

        return Job(
            job_id=req_id,
            company=self.company_config.get("name", "Swiggy"),
            title=title,
            location=location,
            url=detail_url,
            source_url=source_url,
            posted_date=None,
            description=description,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _build_detail_url(self, source_url: str, job_id: str) -> str:
        """Build the Angular SPA detail page URL."""
        base = source_url.split("#")[0].rstrip("/")
        return f"{base}/#/careers/jd/{job_id}"

    # ------------------------------------------------------------------
    # Text utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)

        return text.strip()

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        text = text.replace("\xa0", " ")

        lines = []
        for line in text.splitlines():
            clean_line = SwiggyScraper._clean_text(line)
            if clean_line:
                lines.append(clean_line)

        return "\n".join(lines)
