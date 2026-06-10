from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class PublicisSapientScraper(BaseScraper):
    """
    Scraper for Publicis Sapient careers page.

    The listing page at ``careers.publicissapient.com/job-search`` uses URL
    query parameters for country and team filtering — no dynamic filter
    interaction is needed.  Cards are ``div.job-content`` elements nested
    inside ``div.job-content-container``.

    Expected listing card structure:

        <div class="job-content-container">
          <div class="job-content">
            <div class="job-content-header">
              <span class="job-content-teams">Technology &amp; Engineering | Full-time</span>
            </div>
            <div class="job-content-name">
              <a href="/job-details/2026-145046-senior-systems-engineer-bengaluru?trid=..."
                 title="Senior Systems Engineer">Senior Systems Engineer</a>
            </div>
            <div class="job-content-city-country">Bengaluru, Karnataka, India</div>
          </div>
        </div>

    Expected detail page structure:

        <div class="job-details-content content">
          <div class="add-half-top-module-margin">
            <h2>Job Description</h2>
            <div><p>RICH HTML DESCRIPTION</p>...</div>
          </div>
          <div class="add-half-top-module-margin">
            <h2>Qualifications</h2>
            ...
          </div>
        </div>
    """

    BASE_URL = "https://careers.publicissapient.com"

    # ---- Card selectors ----
    CARD_SELECTOR = "div.job-content-container div.job-content"
    TITLE_SELECTOR = "div.job-content-name a"
    LOCATION_SELECTOR = "div.job-content-city-country"
    JOB_CARD_SELECTORS = [
        "div.job-content-container div.job-content",
        "div.job-content-name a",
    ]

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = "div.job-details-content.content"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)

            # Wait for cards to appear.
            await self._wait_for_cards(page)

            soup = await self._get_soup(page)
            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No Publicis Sapient job cards found.")
                return jobs

            for card in cards[:max_jobs]:
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_ids:
                    continue
                if job.url in seen_urls:
                    continue

                # Enrich from detail page (non-excluded roles only).
                if self._should_exclude(job.title):
                    self.logger.debug(
                        "Skipping detail enrichment for excluded role: %s",
                        job.title,
                    )
                else:
                    try:
                        detail_data = await self._scrape_detail_page(job.url)
                        detail_description = detail_data.get("description", "")

                        if detail_description:
                            job = Job(
                                job_id=job.job_id,
                                company=job.company,
                                title=job.title,
                                location=job.location,
                                url=job.url,
                                source_url=job.source_url,
                                posted_date=job.posted_date,
                                description=detail_description,
                                scraped_at=datetime.now(timezone.utc).isoformat(),
                                extracted_experience_parts="",
                            )
                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich Publicis Sapient detail page %s: %s",
                            job.url,
                            exc,
                        )

                if job.job_id:
                    seen_ids.add(job.job_id)
                seen_urls.add(job.url)
                jobs.append(job)

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        title_el = card.select_one(self.TITLE_SELECTOR)
        if not title_el:
            return None

        link = title_el.get("href", "")
        if not link:
            return None

        # Make relative URLs absolute.
        if link.startswith("/"):
            link = f"{self.BASE_URL}{link}"

        title = self._clean_text(title_el.get_text())

        location = ""
        location_el = card.select_one(self.LOCATION_SELECTOR)
        if location_el:
            location = self._clean_text(location_el.get_text())

        job_id = self._extract_job_id(link)

        if not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Publicis Sapient"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date="",
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_job_id(self, url: str) -> str:
        """
        Publicis Sapient job URLs:
            /job-details/2026-145046-senior-systems-engineer-bengaluru?trid=...

        The segment after /job-details/ up to the query string is the job ID.
        We use the numeric portion (e.g. 2026-145046).
        """
        if not url:
            return ""

        # Match the /job-details/{slug} path segment.
        match = re.search(r"/job-details/([^?]+)", url, flags=re.IGNORECASE)
        if match:
            return match.group(1)

        return ""

    # ------------------------------------------------------------------
    # Wait helpers
    # ------------------------------------------------------------------

    async def _wait_for_cards(self, page: Page) -> None:
        """Wait for at least one card selector to match on the page."""
        for selector in self.JOB_CARD_SELECTORS:
            try:
                await page.wait_for_selector(selector, timeout=10000)
                return
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        return " ".join(text.split()).strip()

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def _get_detail_page(self) -> Page:
        """Return a new page for detail scraping."""
        if self.context:
            try:
                return await self.context.new_page()
            except Exception:
                self.logger.debug(
                    "Shared browser context is no longer usable; creating a fresh one."
                )
                await self.close_browser()

        return await self.new_page()

    async def _scrape_detail_page(self, job_url: str) -> dict[str, str]:
        """
        Open a job detail page and extract the full HTML description
        from ``div.job-details-content.content``.
        """
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)

            await detail_page.goto(
                job_url,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await detail_page.wait_for_timeout(4000)

            description = ""

            # Extract rich HTML description from the page body.
            try:
                desc_el = detail_page.locator(
                    self.DETAIL_DESCRIPTION_SELECTOR
                ).first
                await desc_el.wait_for(state="visible", timeout=10000)

                html_content = await desc_el.evaluate("""
                    (el) => {
                        const clone = el.cloneNode(true);
                        clone.querySelectorAll('script, style').forEach(n => n.remove());
                        return clone.innerHTML;
                    }
                """)

                description = self._clean_text(html_content)
            except Exception:
                pass

            return {
                "description": description,
            }

        finally:
            try:
                await detail_page.close()
            except Exception:
                pass
