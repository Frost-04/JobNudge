from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class CapcoScraper(BaseScraper):
    """
    Scraper for Capco jobs via the Greenhouse embed board + react-select filters.

    Capco's custom listing page (capco.com/Careers/Job-Search) has
    hard-to-automate custom dropdowns. Instead, this scraper uses the
    Greenhouse **embed** board with react-select filters — same pattern
    as Tower Research.

        Embed listing:
            https://job-boards.greenhouse.io/embed/job_board?for=capco

    Listing approach:
        1. Navigate to the embed board.
        2. Select "Tech & Engineering" via ``#department-filter`` react-select.
        3. Select "India - Bengaluru" via ``#office-filter`` react-select.
        4. Parse filtered ``tr.job-post`` cards.

    Detail approach:
        1. Navigate to standard Greenhouse detail page:
           ``https://job-boards.greenhouse.io/capco/jobs/{job_id}``
        2. Extract description from ``div.job__description.body``.

    Listing card (Greenhouse embed layout):
        tr.job-post
          td.cell > a[href*="/jobs/"]
            p.body.body--medium               (job title)
            p.body__secondary.body--metadata  (location)
    """

    EMBED_LISTING_URL = "https://job-boards.greenhouse.io/embed/job_board?for=capco"
    DETAIL_BASE = "https://job-boards.greenhouse.io"

    CARD_SELECTOR = "tr.job-post"
    TITLE_SELECTOR = "p.body.body--medium"
    LOCATION_SELECTOR = "p.body__secondary.body--metadata"
    LINK_SELECTOR = 'td.cell > a[href*="/jobs/"]'
    DETAIL_DESCRIPTION_SELECTOR = "div.job__description.body"

    DEPARTMENT_INPUT_ID = "#department-filter"
    OFFICE_INPUT_ID = "#office-filter"

    FILTER_DEPARTMENT = "Tech & Engineering"
    FILTER_OFFICE = "India - Bengaluru"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", self.EMBED_LISTING_URL)
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            # ── Listing: embed board + filter interaction ──
            await page.goto(
                self.EMBED_LISTING_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await page.wait_for_timeout(5000)

            # Apply Department filter.
            await self._select_react_option(
                page, self.DEPARTMENT_INPUT_ID, self.FILTER_DEPARTMENT,
            )
            await page.wait_for_timeout(800)

            # Apply Office filter.
            await self._select_react_option(
                page, self.OFFICE_INPUT_ID, self.FILTER_OFFICE,
            )
            await page.wait_for_timeout(3000)

            soup = await self._get_soup(page)
            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No Capco job cards after filtering.")
                return jobs

            seen_job_ids: set[str] = set()
            seen_urls: set[str] = set()

            for card in cards[:max_jobs]:
                job = self._parse_card(card, source_url)
                if not job:
                    continue
                if job.job_id and job.job_id in seen_job_ids:
                    continue
                if job.url in seen_urls:
                    continue

                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail for: %s", job.title)
                else:
                    try:
                        detail = await self._scrape_detail_page(job.url)
                        if detail.get("description"):
                            job = Job(
                                job_id=job.job_id,
                                company=job.company,
                                title=job.title,
                                location=job.location,
                                url=job.url,
                                source_url=job.source_url,
                                posted_date=job.posted_date,
                                description=detail["description"],
                                scraped_at=datetime.now(timezone.utc).isoformat(),
                                extracted_experience_parts="",
                            )
                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich %s: %s", job.url, exc,
                        )

                if job.job_id:
                    seen_job_ids.add(job.job_id)
                seen_urls.add(job.url)
                jobs.append(job)

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # React-select interaction (embed layout, no iframe)
    # ------------------------------------------------------------------

    async def _select_react_option(
        self, page: Page, input_selector: str, label: str,
    ) -> None:
        """Open a react-select, click the option, let it auto-close."""
        try:
            await page.click(input_selector, timeout=10000)
            await page.wait_for_timeout(1000)
        except Exception:
            self.logger.debug("Could not click react-select %s", input_selector)
            return

        try:
            await page.wait_for_selector('[role="listbox"]', timeout=5000)
        except Exception:
            self.logger.debug("react-select listbox did not appear for %s", input_selector)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
            return

        try:
            option = page.locator(f'[role="option"]:has-text("{label}")').first
            await option.click(timeout=5000)
            await page.wait_for_timeout(400)
        except Exception:
            self.logger.debug("Could not select option: %s", label)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
            return

        await page.wait_for_timeout(300)

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card)
        title = self._extract_title(card)
        job_id = self._extract_job_id_from_url(link)
        location = self._extract_location(card)
        if not link or not title:
            return None
        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Capco"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag) -> str:
        anchor = card.select_one(self.LINK_SELECTOR)
        if not anchor:
            return ""
        href = anchor.get("href", "")
        if href and not href.startswith(("http://", "https://")):
            href = f"{self.DETAIL_BASE}{href}"
        return str(href)

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        return self._clean_text(el.get_text()) if el else ""

    def _extract_location(self, card: Tag) -> str:
        el = card.select_one(self.LOCATION_SELECTOR)
        return self._clean_text(el.get_text()) if el else ""

    def _extract_job_id_from_url(self, url: str) -> str:
        if not url:
            return ""
        m = re.search(r'/jobs/(\d+)', url)
        return m.group(1) if m else ""

    # ------------------------------------------------------------------
    # Detail page (standard Greenhouse, no iframe)
    # ------------------------------------------------------------------

    async def _get_detail_page(self):
        if self.context:
            try:
                return await self.context.new_page()
            except Exception:
                self.logger.debug("Discarding stale browser context.")
                await self.close_browser()
        return await self.new_page()

    async def _scrape_detail_page(self, job_url: str) -> dict[str, str]:
        detail_page = await self._get_detail_page()
        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(
                job_url, wait_until="domcontentloaded", timeout=60000,
            )
            await detail_page.wait_for_timeout(2000)
            soup = await self._get_soup(detail_page)
            result: dict[str, str] = {}
            if soup:
                desc = self._extract_description(soup)
                if desc:
                    result["description"] = desc
            return result
        finally:
            await detail_page.close()

    def _extract_description(self, soup: BeautifulSoup) -> str:
        el = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)
        if el:
            for tag in el.select("script, style, noscript, nav, header, footer"):
                tag.decompose()
            return self._clean_multiline_text(el.get_text(separator="\n"))
        return ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""
        return " ".join(text.split()).strip()

    def _clean_multiline_text(self, text: str) -> str:
        if not text:
            return ""
        lines = [line.strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line)
