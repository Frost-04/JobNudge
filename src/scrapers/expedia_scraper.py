from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class ExpediaScraper(BaseScraper):
    """
    Scraper for Expedia Group Careers job search pages.

    Expected search page structure:

        ul#results-list > li.Results__list__item
          div.Results__list__content
            a.view-job-button[href]
              h3.Results__list__title.h4.text-blue-2    (job title)
              h4.Results__list__location.h5              (location)

    Expected detail page structure:

        div.Desc__copy.text-body                         (full job description)

    Job IDs are extracted from the URL pattern:
        /job/{slug}/{locations}/R-{id}/
    """

    # ---- Card selectors ----
    CARD_SELECTOR = "li.Results__list__item"
    TITLE_SELECTOR = "h3.Results__list__title"
    LOCATION_SELECTOR = "h4.Results__list__location"
    LINK_SELECTOR = "a.view-job-button"

    # ---- Detail page selectors ----
    DETAIL_DESC_SELECTOR = "div.Desc__copy.text-body"

    # ---- Pagination ----
    PAGINATION_SELECTOR = "nav.pagination a[href]"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            page_num = 1

            while True:
                # Wait for job cards to render.
                try:
                    await page.wait_for_selector(self.CARD_SELECTOR, timeout=15000)
                except Exception:
                    pass

                soup = await self._get_soup(page)

                cards = soup.select(self.CARD_SELECTOR)

                if not cards:
                    self.logger.warning(
                        "No Expedia Group job cards found on page %d.", page_num
                    )
                    break

                self.logger.info(
                    "Expedia Group page %d: %d cards found.", page_num, len(cards)
                )

                for card in cards:
                    if len(jobs) >= max_jobs:
                        break

                    job = self._parse_card(card, source_url)

                    if job is None:
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
                            description = await self._scrape_detail_page(job.url)

                            if description:
                                job.description = description
                        except Exception as exc:
                            self.logger.warning(
                                "Failed to scrape Expedia detail page %s: %s",
                                job.url,
                                exc,
                            )

                    if job.job_id:
                        seen_ids.add(job.job_id)
                    seen_urls.add(job.url)

                    jobs.append(job)

                if len(jobs) >= max_jobs:
                    break

                # Try to go to the next page.
                next_url = self._get_next_page_url(soup, source_url, page_num)

                if not next_url:
                    break

                page_num += 1

                try:
                    await page.goto(next_url, wait_until="domcontentloaded", timeout=60000)
                except Exception as exc:
                    self.logger.warning(
                        "Failed to navigate to Expedia page %d: %s", page_num, exc
                    )
                    break

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card, source_url)
        title = self._extract_title(card)
        location = self._extract_location(card)

        if not link or not title:
            return None

        job_id = self._extract_job_id(link)

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Expedia Group"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_title(self, card: Tag) -> str:
        """Extract job title from the card's <h3> element."""
        h3 = card.select_one(self.TITLE_SELECTOR)

        if h3:
            return self._clean_text(h3.get_text())

        # Fallback: any h3 in the card.
        for h in card.select("h3"):
            text = self._clean_text(h.get_text())

            if text:
                return text

        return ""

    def _extract_location(self, card: Tag) -> str:
        """Extract location from the card's <h4> element."""
        h4 = card.select_one(self.LOCATION_SELECTOR)

        if h4:
            return self._clean_text(h4.get_text())

        # Fallback: any h4 in the card.
        for h in card.select("h4"):
            text = self._clean_text(h.get_text())

            if text:
                return text

        return ""

    def _extract_link(self, card: Tag, source_url: str) -> str:
        """Extract absolute job URL from the card's anchor tag."""
        anchor = card.select_one(self.LINK_SELECTOR)

        if not anchor:
            return ""

        href = anchor.get("href")

        if not href:
            return ""

        return self._make_job_url(source_url, str(href))

    def _extract_job_id(self, url: str) -> str:
        """
        Expedia job URLs:
        https://careers.expediagroup.com/job/data-scientist-iii/bangalore/R-106144/
        """
        if not url:
            return ""

        match = re.search(r"/R-(\d+)", url)

        if match:
            return f"R-{match.group(1)}"

        return ""

    def _make_job_url(self, source_url: str, href: str) -> str:
        """Build absolute URL from relative href."""
        from urllib.parse import urlparse

        href = href.strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        parsed = urlparse(source_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        if href.startswith("/"):
            return f"{origin}{href}"

        return f"{origin}/{href}"

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def _get_next_page_url(self, soup, source_url: str, current_page: int) -> str:
        """Extract the URL for the next page from the pagination nav."""
        pagination_links = soup.select(self.PAGINATION_SELECTOR)

        for link in pagination_links:
            href = link.get("href")

            if not href:
                continue

            href_str = str(href)

            # Look for the "Next jobs" link (has mypage param).
            if "mypage=" in href_str:
                # Extract page number.
                page_match = re.search(r"mypage=(\d+)", href_str)

                if page_match:
                    next_page = int(page_match.group(1))

                    if next_page > current_page:
                        return self._make_job_url(source_url, href_str)

        return ""

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def _get_detail_page(self) -> Page:
        """Return a new page for detail scraping, creating a fresh context if needed."""
        if self.context:
            try:
                return await self.context.new_page()
            except Exception:
                self.logger.debug(
                    "Shared browser context is no longer usable; creating a fresh one."
                )
                await self.close_browser()

        return await self.new_page()

    async def _scrape_detail_page(self, job_url: str) -> str | None:
        """Open the job detail page and extract the description."""
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)

            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for the description to load.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_DESC_SELECTOR, timeout=10000
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            return self._extract_description(soup)

        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        """Extract the full job description from the detail page."""
        desc_el = soup.select_one(self.DETAIL_DESC_SELECTOR)

        if not desc_el:
            return ""

        return self._clean_multiline_text(desc_el.get_text())

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        return " ".join(text.split()).strip()

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        if not text:
            return ""
        lines = [
            " ".join(line.split())
            for line in text.splitlines()
            if line.strip()
        ]
        return "\n".join(lines)
