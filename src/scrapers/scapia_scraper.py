from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class ScapiaScraper(BaseScraper):
    """
    Scraper for Scapia careers page (Freshteam job board).

    The listing page at ``scapia.freshteam.com/jobs`` uses URL query
    parameters for department filtering — no dynamic filter interaction
    is needed.  Cards are ``<a class="heading show">`` elements nested
    inside ``<div class="job-list">``.  Each card has a teaser
    description but we enrich from the detail page.

    Detail pages include a JSON-LD ``JobPosting`` block with
    ``datePosted`` and ``description`` fields, plus a rich HTML
    description inside ``.job-details-content.content``.

    Expected listing card structure:

        <a class="heading show"
           data-portal-title="seniorsoftwareengineer-sde3/4"
           data-portal-location="Bengaluru, India"
           data-portal-job-type="2"
           href="/jobs/QIitGrd4a9hn/senior-software-engineer-sde3-4">
          <div class="row">
            <div class="job-list-info">
              <div class="job-title">Senior Software Engineer - SDE3/4</div>
              <div class="job-desc text">TEASER...</div>
            </div>
            <div class="job-location">
              <div class="location-info">Bengaluru\nFull Time</div>
            </div>
          </div>
        </a>

    Expected detail page structure:

        <div class="job-details-content content">
          <div><p>RICH HTML DESCRIPTION</p></div>
          ...
        </div>
        <script type="application/ld+json">
          { "datePosted": "...", "title": "...", "description": "..." }
        </script>
    """

    # ---- Card selectors ----
    CARD_SELECTOR = "div.job-list a.heading.show"
    TITLE_SELECTOR = ".job-title"
    LOCATION_SELECTOR = ".location-info"
    JOB_CARD_SELECTORS = [
        "div.job-list a.heading.show",
        "div.job-list .job-title",
    ]

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = "div.job-details-content.content"
    JSONLD_SELECTOR = "script[type='application/ld+json']"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)

            # Wait for cards to appear.
            await self._wait_for_cards(page)

            soup = await self._get_soup(page)
            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No Scapia job cards found.")
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
                        posted_date = detail_data.get("posted_date", "")
                        detail_description = detail_data.get("description", "")

                        if posted_date or detail_description:
                            job = Job(
                                job_id=job.job_id,
                                company=job.company,
                                title=job.title,
                                location=job.location,
                                url=job.url,
                                source_url=job.source_url,
                                posted_date=posted_date or job.posted_date,
                                description=detail_description,
                                scraped_at=datetime.now(timezone.utc).isoformat(),
                                extracted_experience_parts="",
                            )
                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich Scapia detail page %s: %s",
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
        link = card.get("href", "")
        if not link:
            return None

        # Make relative URLs absolute.
        if link.startswith("/"):
            link = f"https://scapia.freshteam.com{link}"

        title = self._extract_title(card)
        location = self._extract_location(card)
        job_id = self._extract_job_id(link)

        if not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Scapia"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date="",
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        if el:
            return self._clean_text(el.get_text())
        return ""

    def _extract_location(self, card: Tag) -> str:
        """Extract location from .location-info, taking the first line
        (before any <br> or newline)."""
        el = card.select_one(self.LOCATION_SELECTOR)
        if not el:
            return ""

        text = el.get_text("\n", strip=True)
        # The first line is the city / location.
        return text.split("\n")[0].strip()

    def _extract_job_id(self, url: str) -> str:
        """
        Freshteam job URLs: /jobs/QIitGrd4a9hn/senior-software-engineer-sde3-4

        The segment after /jobs/ is the opaque job ID.
        """
        if not url:
            return ""

        match = re.search(r"/jobs/([^/]+)", url, flags=re.IGNORECASE)
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
        Open a job detail page and extract posted_date (from JSON-LD)
        and the full HTML description.
        """
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)

            await detail_page.goto(
                job_url,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await detail_page.wait_for_timeout(3000)

            posted_date = ""
            description = ""

            # 1. Try JSON-LD for datePosted + description.
            try:
                jsonld_text = await detail_page.locator(
                    self.JSONLD_SELECTOR
                ).first.text_content(timeout=5000)

                if jsonld_text:
                    data = json.loads(jsonld_text)
                    posted_date = data.get("datePosted", "")

                    # The JSON-LD description is HTML-encoded, but
                    # sometimes contains a richer block.  Only use it
                    # as a fallback.
                    jsonld_desc = data.get("description", "")
                    if jsonld_desc and len(jsonld_desc) > 200:
                        description = jsonld_desc
            except Exception:
                pass

            # 2. Extract rich HTML description from the page body.
            try:
                desc_el = detail_page.locator(
                    self.DETAIL_DESCRIPTION_SELECTOR
                ).first
                await desc_el.wait_for(state="visible", timeout=10000)

                # Get all the HTML content inside the content div.
                # Exclude the application form and script tags.
                html_content = await desc_el.evaluate("""
                    (el) => {
                        // Remove script tags and the application form.
                        const clone = el.cloneNode(true);
                        clone.querySelectorAll('script, .application-form').forEach(n => n.remove());
                        return clone.innerHTML;
                    }
                """)

                html_content = self._clean_text(html_content)

                # Prefer the page HTML if it's richer than the JSON-LD description.
                if html_content and len(html_content) > len(description):
                    description = html_content
            except Exception:
                pass

            return {
                "posted_date": posted_date,
                "description": description,
            }

        finally:
            try:
                await detail_page.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        return " ".join(text.split()).strip()
