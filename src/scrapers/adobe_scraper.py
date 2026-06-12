from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class AdobeScraper(BaseScraper):
    """
    Scraper for Adobe Careers job listing page.

    Adobe uses Phenom People with the Aurelia framework.  Job cards are
    ``<li class="jobs-list-item">`` elements inside a ``<ul>``.  Each card
    contains an ``<a data-ph-at-id="job-link">`` with rich data attributes:

    - ``data-ph-at-job-title-text``   – job title
    - ``data-ph-at-job-id-text``      – job ID (R-prefixed, e.g. R167740)
    - ``data-ph-at-job-location-text``– location
    - ``data-ph-at-job-post-date-text``– posted date (ISO 8601)
    - ``data-ph-at-job-category-text``– category
    - ``href``                        – detail page URL

    The detail page (``/us/en/job/{id}/{slug}``) renders:

        div.jd-info[data-ph-at-id="jobdescription-text"]

    Filters are applied via URL query parameters because Phenom People
    reflects active facet selections in the URL after the initial page load.
    """

    # ---- Card selectors (Phenom People / Aurelia) ----
    JOB_LINK_SELECTOR = 'a[data-ph-at-id="job-link"]'

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = 'div.jd-info[data-ph-at-id="jobdescription-text"]'

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 200))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="networkidle", timeout=90000)

            # Extra settle for Aurelia to render all cards.
            await asyncio.sleep(5)

            card_data = await page.evaluate('''() => {
                const links = document.querySelectorAll(
                    'a[data-ph-at-id="job-link"]'
                );
                return Array.from(links).map(link => ({
                    title: link.getAttribute('data-ph-at-job-title-text') || '',
                    jobId: link.getAttribute('data-ph-at-job-id-text') || '',
                    location: link.getAttribute('data-ph-at-job-location-text') || '',
                    postedDate: link.getAttribute('data-ph-at-job-post-date-text') || '',
                    category: link.getAttribute('data-ph-at-job-category-text') || '',
                    url: link.getAttribute('href') || '',
                }));
            }''')

            if not card_data:
                self.logger.warning("No Adobe job cards found.")
                return jobs

            seen_ids: set[str] = set()

            for data in card_data:
                if len(jobs) >= max_jobs:
                    break

                title = (data.get("title") or "").strip()
                job_id = (data.get("jobId") or "").strip()
                location = (data.get("location") or "").strip()
                posted_date_raw = (data.get("postedDate") or "").strip()
                url = (data.get("url") or "").strip()

                if not title:
                    continue
                if job_id and job_id in seen_ids:
                    continue

                # Parse ISO date if present.
                posted_date = None
                if posted_date_raw:
                    try:
                        posted_date = datetime.fromisoformat(
                            posted_date_raw
                        ).isoformat()
                    except ValueError:
                        posted_date = None

                # Normalize date for older Python versions (no Z suffix).
                if posted_date and posted_date.endswith("+0000"):
                    posted_date = posted_date.replace("+0000", "+00:00")

                job = Job(
                    job_id=job_id,
                    company=self.company_config.get("name", "Adobe"),
                    title=title,
                    location=location,
                    url=url,
                    source_url=source_url,
                    posted_date=posted_date,
                    description=None,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    extracted_experience_parts="",
                )

                # Enrich with detail page description.
                if self._should_exclude(job.title):
                    self.logger.debug(
                        "Skipping detail enrichment for: %s", job.title
                    )
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
                            "Failed to enrich Adobe job detail %s: %s",
                            job.url,
                            exc,
                        )

                if job_id:
                    seen_ids.add(job_id)
                jobs.append(job)

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def _scrape_detail_page(self, job_url: str) -> str:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(15000)

            await detail_page.goto(
                job_url, wait_until="domcontentloaded", timeout=60000
            )

            # Wait for the description container.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_DESCRIPTION_SELECTOR,
                    timeout=20000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            desc_container = soup.select_one(
                self.DETAIL_DESCRIPTION_SELECTOR
            )
            if not desc_container:
                return ""

            return self._extract_description(desc_container)

        finally:
            await detail_page.close()

    def _extract_description(self, container: Tag) -> str:
        """Extract clean description text from the Adobe detail page."""
        # Remove script/style tags.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

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

    async def _get_detail_page(self) -> Page:
        """Return a new page for detail scraping."""
        if self.context:
            try:
                return await self.context.new_page()
            except Exception:
                self.logger.debug(
                    "Shared browser context is no longer usable; "
                    "creating a fresh one."
                )
                await self.close_browser()

        return await self.new_page()

    @staticmethod
    def _clean_text(text: str) -> str:
        """Normalize whitespace in a string."""
        if not text:
            return ""
        return " ".join(text.split()).strip()
