from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class JfrogScraper(BaseScraper):
    """Scraper for JFrog careers (custom Greenhouse front-end).

    The listing page at join.jfrog.com/positions/?gh_office=44918 shows job
    cards filtered by office.  Cards for non-selected locations are hidden
    with the ``d-none`` CSS class — we skip those.

    Job cards:

        a.green-job-square.grid-item  (omit .d-none cards)
          data-greenhouse-id="7160107"
          href="/job/7160107-developer-support-engineer/"
          h3  →  title

    Detail page:

        div.col-lg-6.content
          p, strong, ul, li
    """

    # ---- Listing page selectors ----
    CARD_SELECTOR = 'a.green-job-square.grid-item:not(.d-none)'
    TITLE_SELECTOR = 'h3'

    # ---- Detail page selectors ----
    DETAIL_CONTENT_SELECTOR = 'div.col-lg-6.content'

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="networkidle", timeout=120000)

            # Wait for job cards to appear.
            try:
                await page.wait_for_selector(
                    'a.green-job-square.grid-item', timeout=30000,
                )
            except Exception:
                pass

            # Extra settle time for JS filtering.
            await asyncio.sleep(2)

            soup = await self._get_soup(page)
            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No visible JFrog job cards found.")
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

                # Enrich with detail page description.
                if self._should_exclude(job.title):
                    self.logger.debug(
                        "Skipping detail enrichment for: %s", job.title,
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
                            "Failed to enrich JFrog job detail %s: %s",
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

        title = self._clean_text(title_el.get_text())
        if not title:
            return None

        href = card.get("href")
        if not href:
            return None

        url = make_absolute_url(source_url, str(href))

        # Job ID from the data-greenhouse-id attribute.
        job_id = card.get("data-greenhouse-id", "").strip()

        # All visible cards are India-based (filtered by ?gh_office=44918).
        location = "India"

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "JFrog"),
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
        if self.context:
            try:
                return await self.context.new_page()
            except Exception:
                self.logger.debug(
                    "Shared browser context is no longer usable; creating a fresh one.",
                )
                await self.close_browser()
        return await self.new_page()

    async def _scrape_detail_page(self, job_url: str) -> str:
        """Navigate to a JFrog job detail page and extract the description."""
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(15000)
            await detail_page.goto(
                job_url, wait_until="domcontentloaded", timeout=60000,
            )

            # Wait for the description container.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_CONTENT_SELECTOR, timeout=15000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            desc_container = soup.select_one(self.DETAIL_CONTENT_SELECTOR)
            if not desc_container:
                return ""

            return self._extract_description(desc_container)

        finally:
            await detail_page.close()

    def _extract_description(self, container: Tag) -> str:
        """Extract clean description text from a JFrog detail page."""
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
            elif tag_name in ("b", "strong"):
                text = self._clean_text(child.get_text())
                if text:
                    current_section.append(text)
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
