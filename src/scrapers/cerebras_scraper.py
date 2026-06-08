from __future__ import annotations

import copy
import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id


class CerebrasScraper(BaseScraper):
    """
    Scraper for Cerebras Systems job board (custom listing + Greenhouse detail).

    The listing page at `cerebras.ai/open-positions` is a custom SPA that
    fetches jobs from Greenhouse and renders them with Tailwind CSS classes.
    Cards use:
        a.flex.gap-6[href*="greenhouse.io/cerebrassystems/jobs/"]
          h4.font-semibold              (title, JS-rendered)
          p.text-muted-foreground       (location, JS-rendered)

    Card content may be JS-rendered (empty in static HTML), so this scraper
    extracts only URLs from the listing and enriches all data from the
    Greenhouse detail page.

    Expected detail page structure (standard Greenhouse):

        h1.job__title                  (job title)
        div.job__header                (header with location metadata)
          span.location
        div.job__description.body      (full rich-text description)
    """

    # ---- Card selectors ----
    CARD_SELECTOR = "a[href*='greenhouse.io/cerebrassystems/jobs/']"

    # ---- Detail page selectors ----
    DETAIL_TITLE_SELECTOR = "div.job__title"
    DETAIL_LOCATION_SELECTOR = "div.job__location"
    DETAIL_DESCRIPTION_SELECTOR = "div.job__description.body"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for job cards to render (JS may populate them).
            try:
                await page.wait_for_selector(self.CARD_SELECTOR, timeout=15000)
            except Exception:
                pass

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No Cerebras job cards found.")
                return jobs

            seen_ids: set[str] = set()
            seen_urls: set[str] = set()

            for card in cards[:max_jobs]:
                href = card.get("href")

                if not href:
                    continue

                job_url = str(href)
                job_id = self._extract_job_id_from_url(job_url)

                if job_id and job_id in seen_ids:
                    continue

                if job_url in seen_urls:
                    continue

                # Try card-level extraction first (if JS rendered the text).
                card_title = self._extract_card_title(card)

                # Enrich from detail page.
                try:
                    detail_data = await self._scrape_detail_page(job_url)

                    title = detail_data.get("title", "") or card_title
                    location = detail_data.get("location", "")
                    description = detail_data.get("description", "")

                    if not title:
                        self.logger.debug("Skipping job with no title: %s", job_url)
                        continue

                    if self._should_exclude(title):
                        self.logger.debug("Skipping excluded role: %s", title)
                        if job_id:
                            seen_ids.add(job_id)
                        seen_urls.add(job_url)
                        continue

                    job = Job(
                        job_id=job_id,
                        company=self.company_config.get("name", "Cerebras Systems"),
                        title=title,
                        location=location,
                        url=job_url,
                        source_url=source_url,
                        posted_date=None,
                        description=description or None,
                        scraped_at=datetime.now(timezone.utc).isoformat(),
                        extracted_experience_parts="",
                    )

                except Exception as exc:
                    self.logger.warning(
                        "Failed to scrape Cerebras detail page %s: %s",
                        job_url,
                        exc,
                    )
                    continue

                if job_id:
                    seen_ids.add(job_id)
                seen_urls.add(job_url)
                jobs.append(job)

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Card parsing (light DOM — URL + fallback title)
    # ------------------------------------------------------------------

    def _extract_card_title(self, card: Tag) -> str:
        """Try to extract title from the card's <h4> (may be JS-rendered)."""
        h4 = card.select_one("h4")

        if h4:
            text = self._clean_text(h4.get_text())

            if text:
                return text

        return ""

    def _extract_job_id_from_url(self, url: str) -> str:
        """
        Greenhouse job URLs:
        https://job-boards.greenhouse.io/cerebrassystems/jobs/7486714003
        """
        if not url:
            return ""

        match = re.search(r"/jobs/(\d+)", url, flags=re.IGNORECASE)

        if match:
            return match.group(1)

        return extract_job_id(url) or ""

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

    async def _scrape_detail_page(self, job_url: str) -> dict[str, str]:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)

            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for any of the detail content selectors.
            selectors = [
                self.DETAIL_DESCRIPTION_SELECTOR,
                self.DETAIL_TITLE_SELECTOR,
                "div.job__description",
                "h1",
            ]

            for selector in selectors:
                try:
                    await detail_page.wait_for_selector(selector, timeout=10000)
                    break
                except Exception:
                    continue

            soup = await self._get_soup(detail_page)

            detail_data: dict[str, str] = {}

            title = self._extract_detail_title(soup)
            location = self._extract_detail_location(soup)
            description = self._extract_description(soup)

            if title:
                detail_data["title"] = title

            if location:
                detail_data["location"] = location

            if description:
                detail_data["description"] = description

            return detail_data

        finally:
            await detail_page.close()

    def _extract_detail_title(self, soup) -> str:
        el = soup.select_one(self.DETAIL_TITLE_SELECTOR)

        if el:
            # div.job__title contains both the title text and child div.job__location.
            # Clone the element and remove the location child to get clean title.
            title_el = copy.copy(el)
            location_child = title_el.select_one("div.job__location")

            if location_child:
                location_child.decompose()

            title = self._clean_text(title_el.get_text())

            if title:
                return title

        # Fallback: any h1
        h1 = soup.select_one("h1")

        if h1:
            return self._clean_text(h1.get_text())

        return ""

    def _extract_detail_location(self, soup) -> str:
        el = soup.select_one(self.DETAIL_LOCATION_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        # Fallback: any span with "location" class
        for span in soup.select("span.location, span[class*='location']"):
            text = self._clean_text(span.get_text())

            if text and text.lower() not in ("location", "locations"):
                return text

        return ""

    def _extract_description(self, soup) -> str:
        container = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)

        if not container:
            return ""

        # Remove non-description elements.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

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
