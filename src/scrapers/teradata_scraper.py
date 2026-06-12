from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class TeradataScraper(BaseScraper):
    """
    Scraper for Teradata Careers job search pages (custom React SPA).

    Expected search card structure:

        article[data-testid="job-card"]
          div[aria-label^="Job Title - Job ID"]
            h4[data-cy="job-title"]
              a.titleLink-*[href="/jobs/{id}/{slug}"]
            p[data-cy="job-location"]
            div.postedMessage-*                         ("Posted 3 days ago")
            div[data-cy="job-description"]               (FULL description in card)

    Cards contain the full job description — no detail page enrichment needed.
    """

    # ---- Listing page selectors ----
    CARD_SELECTOR = 'article[data-testid="job-card"]'
    TITLE_SELECTOR = 'h4[data-cy="job-title"] a'
    TITLE_FALLBACK_SELECTOR = 'h4[data-cy="job-title"]'
    LOCATION_SELECTOR = 'p[data-cy="job-location"]'
    POSTED_SELECTOR = 'div[class*="postedMessage-"]'
    DESCRIPTION_SELECTOR = 'div[data-cy="job-description"]'

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            await self._wait_for_results(page)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No Teradata job cards found.")
                return jobs

            seen_ids: set[str] = set()
            seen_urls: set[str] = set()

            for card in cards[:max_jobs]:
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_ids:
                    continue

                if job.url in seen_urls:
                    continue

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
        # Title + link from the anchor inside h4.
        link_el = card.select_one(self.TITLE_SELECTOR)
        if not link_el:
            # Fallback: h4 alone (no anchor).
            title_el = card.select_one(self.TITLE_FALLBACK_SELECTOR)
            if not title_el:
                return None
            title_text = self._clean_text(title_el.get_text())
            url = source_url
        else:
            href = link_el.get("href")
            if not href:
                return None
            url = make_absolute_url(source_url, str(href))
            title_text = self._clean_text(link_el.get_text())

        if not title_text:
            return None

        # Title format: "Senior AI Engineer - 219800"
        # Extract clean title and job ID.
        title, job_id = self._parse_title_and_id(title_text)

        # Fallback: extract job ID from URL path.
        if not job_id and url:
            job_id = self._extract_job_id_from_url(url)

        # Location from the dedicated paragraph.
        location = ""
        loc_el = card.select_one(self.LOCATION_SELECTOR)
        if loc_el:
            location = self._clean_text(loc_el.get_text())

        # Posted date from the postedMessage div.
        posted_date: str | None = None
        posted_els = card.select(self.POSTED_SELECTOR)
        for el in posted_els:
            text = self._clean_text(el.get_text())
            if text and text.lower().startswith("posted"):
                posted_date = text
                break

        # Description — cards already contain the full description.
        description = ""
        desc_el = card.select_one(self.DESCRIPTION_SELECTOR)
        if desc_el:
            for unwanted in desc_el.select("script, style, noscript"):
                unwanted.decompose()
            description = desc_el.get_text(separator="\n")
            description = self._clean_multiline_text(description)

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Teradata"),
            title=title,
            location=location,
            url=url,
            source_url=source_url,
            posted_date=posted_date,
            description=description or None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    # ------------------------------------------------------------------
    # Title / Job ID parsing
    # ------------------------------------------------------------------

    def _parse_title_and_id(self, title_text: str) -> tuple[str, str]:
        """
        Parse title text like "Senior AI Engineer - 219800" into (title, job_id).

        The job ID suffix is separated by " - " followed by a numeric ID.
        """
        # Pattern: "Title - NNNNNN" at the end
        match = re.search(r"\s*-\s*(\d{4,})\s*$", title_text)
        if match:
            job_id = match.group(1)
            clean_title = title_text[: match.start()].strip()
            return clean_title, job_id

        return title_text, ""

    def _extract_job_id_from_url(self, url: str) -> str:
        """Extract job ID from URL path like /jobs/219800/senior-ai-engineer."""
        if not url:
            return ""
        match = re.search(r"/jobs/(\d+)", url)
        if match:
            return match.group(1)
        return ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _wait_for_results(self, page: Page) -> None:
        """Wait for job cards to appear."""
        selectors = [
            self.CARD_SELECTOR,
            'div[class*="searchResults-"]',
        ]
        timeout_ms = self._to_ms(
            self.settings.get("run", {}).get("page_load_timeout_seconds"),
            45000,
        )

        for selector in selectors:
            try:
                await page.wait_for_selector(selector, timeout=timeout_ms)
                return
            except Exception:
                continue

    @staticmethod
    def _clean_text(text: str) -> str:
        """Normalize whitespace in a string."""
        if not text:
            return ""
        import html as html_mod

        text = html_mod.unescape(text)
        text = text.replace("\xa0", " ")
        return " ".join(text.split()).strip()

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        """Normalize whitespace while preserving line breaks."""
        if not text:
            return ""
        import html as html_mod

        text = html_mod.unescape(text)
        text = text.replace("\xa0", " ")
        lines = [" ".join(line.split()).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line)
