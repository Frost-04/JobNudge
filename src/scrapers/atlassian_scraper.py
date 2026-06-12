from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class AtlassianScraper(BaseScraper):
    """
    Scraper for Atlassian Careers all-jobs page.

    Atlassian uses a custom server-rendered page with department-grouped
    ``<table>`` elements.  Jobs are loaded lazily via XHR as the user scrolls.

    Listing page structure:

        h3                                (department name)
        table > tbody
          tr > th                         (header row: Position, Location)
          tr > td > a[href*="/details/"]  (job title + detail URL)
          tr > td                         (location text)

    Job IDs are numeric: ``/company/careers/details/25144`` → ``25144``.

    Detail page:

        div.column.colspan-10.text-left.push.push-1   (description container)
          p, strong, ul, li, br
    """

    # ---- Listing page selectors ----
    CARD_ROW_SELECTOR = 'table tbody tr'
    TITLE_LINK_SELECTOR = 'a[href*="/details/"]'

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = 'div.column.colspan-10.text-left.push.push-1'

    # Job ID pattern from URL
    JOB_ID_PATTERN = re.compile(r'/details/(\d+)')

    # Number of scroll attempts to load lazy-rendered rows
    MAX_SCROLL_ATTEMPTS = 10

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            # networkidle is required — domcontentloaded fires before the XHR
            # that fetches the job rows completes.
            await page.goto(source_url, wait_until="networkidle", timeout=120000)

            # Settle for React hydration and initial XHR.
            await asyncio.sleep(5)

            # Scroll to trigger lazy-loading of all job rows.
            await self._scroll_to_load_all(page)

            soup = await self._get_soup(page)

            # Parse department-grouped tables.
            # Each table has rows; the first row is a header (th).
            # Data rows have <a href*="/details/">.
            rows = soup.select(self.CARD_ROW_SELECTOR)

            if not rows:
                self.logger.warning("No Atlassian table rows found.")
                return jobs

            seen_ids: set[str] = set()
            seen_urls: set[str] = set()

            for row in rows:
                if len(jobs) >= max_jobs:
                    break

                job = self._parse_row(row, source_url)
                if not job:
                    continue

                if job.job_id and job.job_id in seen_ids:
                    continue
                if job.url in seen_urls:
                    continue

                # Enrich with detail page description.
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
                else:
                    try:
                        detail_desc = await self._scrape_detail_page(job.url)
                        if detail_desc:
                            job = Job(
                                job_id=job.job_id,
                                company=job.company,
                                title=job.title,
                                location=job.location,
                                url=job.url,
                                source_url=job.source_url,
                                posted_date=job.posted_date,
                                description=detail_desc,
                                scraped_at=datetime.now(timezone.utc).isoformat(),
                                extracted_experience_parts="",
                            )
                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich Atlassian job detail %s: %s",
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
    # Lazy-load scrolling
    # ------------------------------------------------------------------

    async def _scroll_to_load_all(self, page: Page) -> None:
        """Repeatedly scroll to page bottom until no new job links appear."""
        prev_count = 0
        for attempt in range(self.MAX_SCROLL_ATTEMPTS):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)

            curr_count = await page.evaluate(
                'document.querySelectorAll("a[href*=\\"/details/\\"]").length'
            )
            if curr_count == prev_count and curr_count > 0:
                self.logger.debug(
                    "Scroll settled at %d job links (attempt %d)",
                    curr_count,
                    attempt + 1,
                )
                return
            prev_count = curr_count

        self.logger.debug(
            "Scroll finished with %d total job links after %d attempts",
            prev_count,
            self.MAX_SCROLL_ATTEMPTS,
        )

    # ------------------------------------------------------------------
    # Row parsing
    # ------------------------------------------------------------------

    def _parse_row(self, row: Tag, source_url: str) -> Job | None:
        """Parse a ``<tr>`` into a Job.  Skips header rows (those with ``<th>``)."""
        # Skip header rows.
        if row.select_one("th"):
            return None

        cells = row.select("td")
        if len(cells) < 2:
            return None

        # First cell: title + link.
        title_cell = cells[0]
        link = title_cell.select_one(self.TITLE_LINK_SELECTOR)
        if not link:
            return None

        title = self._clean_text(link.get_text())
        if not title:
            return None

        href = link.get("href")
        if not href:
            return None

        url = make_absolute_url(source_url, str(href))

        # Second cell: location.
        location = self._clean_text(cells[1].get_text())

        # Job ID from URL.
        job_id = ""
        m = self.JOB_ID_PATTERN.search(url)
        if m:
            job_id = m.group(1)

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Atlassian"),
            title=title,
            location=location,
            url=url,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

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

    async def _scrape_detail_page(self, job_url: str) -> str:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)

            await detail_page.goto(job_url, wait_until="networkidle", timeout=120000)

            # Wait for the description container.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_DESCRIPTION_SELECTOR,
                    timeout=20000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            desc_container = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)
            if not desc_container:
                return ""

            return self._extract_description(desc_container)

        finally:
            await detail_page.close()

    def _extract_description(self, container: Tag) -> str:
        """Extract clean description text from the Atlassian detail container."""
        # Remove script/style tags.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        # Collect content preserving section structure.
        sections: list[str] = []
        current_section: list[str] = []

        for child in container.children:
            if not hasattr(child, "name"):
                continue

            tag_name = child.name

            if tag_name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                if current_section:
                    sections.append("\n".join(current_section))
                    current_section = []

                heading = self._clean_text(child.get_text())
                if heading:
                    sections.append(heading)
            elif tag_name == "br":
                # Treat <br> as paragraph break.
                if current_section:
                    sections.append("\n".join(current_section))
                    current_section = []
            elif tag_name in ("p", "ul", "ol", "li"):
                text = self._clean_text(child.get_text())
                if text:
                    current_section.append(text)
            else:
                text = self._clean_text(child.get_text())
                if text:
                    current_section.append(text)

        if current_section:
            sections.append("\n".join(current_section))

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        """Normalize whitespace in a string."""
        if not text:
            return ""
        return " ".join(text.split()).strip()
