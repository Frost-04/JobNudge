from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class CrowdstrikeScraper(BaseScraper):
    """Scraper for CrowdStrike careers (Workday platform, wd5).

    The search results page renders job cards in a standard Workday listing
    with a right-side detail panel. Clicking a card loads the description
    on the right — we scrape detail pages directly instead.

    Job cards:

        li.css-1q2dra3
          a[data-automation-id="jobTitle"][href]
          [data-automation-id="locations"] dd.css-129m7dg
          [data-automation-id="postedOn"] dd.css-129m7dg
          [data-automation-id="subtitle"] li.css-h2nt8k   (RXXXXX)

    Detail page:

        [data-automation-id="jobPostingDescription"]
          p, b, ul, li
    """

    # ---- Listing page selectors ----
    CARD_SELECTOR = "li.css-1q2dra3"
    TITLE_SELECTOR = 'a[data-automation-id="jobTitle"]'
    LOCATION_SELECTOR = "[data-automation-id=\"locations\"] dd"
    POSTED_SELECTOR = "[data-automation-id=\"postedOn\"] dd"
    JOB_ID_SELECTOR = "[data-automation-id=\"subtitle\"] li"

    # ---- Detail page selectors ----
    DETAIL_CONTENT_SELECTOR = '[data-automation-id="jobPostingDescription"]'

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        try:
            await page.goto(source_url, wait_until="networkidle", timeout=120000)

            # Wait for job cards to appear.
            await self._wait_for_results(page)

            # Extra settle time for SPA render.
            await asyncio.sleep(3)

            soup = await self._get_soup(page)
            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No CrowdStrike job cards found.")
                return jobs

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
                            "Failed to enrich CrowdStrike job detail %s: %s",
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

        href = title_el.get("href")
        if not href:
            return None

        url = make_absolute_url(source_url, str(href))

        # Job ID from the subtitle list items (e.g. "R29064").
        job_id = ""
        subtitle_items = card.select(self.JOB_ID_SELECTOR)
        for item in subtitle_items:
            text = self._clean_text(item.get_text())
            # CrowdStrike R- requisition IDs (e.g. R29064)
            if text.upper().startswith("R") and any(ch.isdigit() for ch in text):
                job_id = text
                break

        if not job_id:
            job_id = extract_job_id(url)

        # Location from the locations dd.
        location = ""
        loc_els = card.select(self.LOCATION_SELECTOR)
        if loc_els:
            location = self._clean_text(loc_els[0].get_text())

        # Posted date from the postedOn dd.
        posted_date = None
        posted_els = card.select(self.POSTED_SELECTOR)
        if posted_els:
            posted_raw = self._clean_text(posted_els[0].get_text())
            if posted_raw:
                posted_date = posted_raw

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "CrowdStrike"),
            title=title,
            location=location,
            url=url,
            source_url=source_url,
            posted_date=posted_date,
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
                    "Shared browser context is no longer usable; creating a fresh one."
                )
                await self.close_browser()
        return await self.new_page()

    async def _scrape_detail_page(self, job_url: str) -> str:
        """Navigate to a Workday job detail page and extract the description."""
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(15000)
            await detail_page.goto(
                job_url, wait_until="domcontentloaded", timeout=90000
            )

            # Wait for the description container.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_CONTENT_SELECTOR, timeout=30000,
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
        """Extract clean description text from a Workday detail page."""
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

    async def _wait_for_results(self, page: Page) -> None:
        """Wait for the first job card to appear."""
        try:
            await page.wait_for_selector(self.CARD_SELECTOR, timeout=60000)
        except Exception:
            pass
