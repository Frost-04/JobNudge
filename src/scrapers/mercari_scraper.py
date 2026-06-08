from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class MercariScraper(BaseScraper):
    """
    Scraper for Mercari India careers page (Workable-powered job board).

    Expected flow:

    1. Navigate to ``about.in.mercari.com/joinus/``
    2. Type a search keyword into the filter input
    3. Wait for client-side filtering to hide/show ``li.whr-item`` cards
    4. Parse visible cards
    5. Open each card's workable.com detail page for description enrichment

    Expected listing card structure (post-filter):

        ul.whr-items
          li.whr-item                        ← visible (NO display:none)
            h3.whr-title
              a[href*="apply.workable.com/j/"]
            ul.whr-info
              li.whr-location
                span  → "Location:"
                text  → "India, Karnataka, Bengaluru"
              li.whr-date
                span  → "Creation date:"
                text  → "2025-07-28"

    Expected detail page (apply.workable.com):

        section[data-ui="job-description"]
          h2#job-description-title
          div  (contains <h3> sections with About Us / Work Responsibilities etc.)
    """

    # ---- Search page selectors ----
    SEARCH_INPUT = "input.form-control[placeholder*='Search']"
    RESULTS_LIST = "ul.whr-items"
    CARD_SELECTOR = "li.whr-item"

    TITLE_SELECTOR = "h3.whr-title a"
    LOCATION_SELECTOR = "li.whr-location"
    DATE_SELECTOR = "li.whr-date"

    # ---- Detail page selectors ----
    DESCRIPTION_SELECTOR = "section[data-ui='job-description']"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))
        search_keyword = self.company_config.get("search_keyword", "software engineer")

        jobs: list[Job] = []

        try:
            # ---- Step 1: Load the page ----
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # ---- Step 2: Type the search keyword ----
            search_input = page.locator(self.SEARCH_INPUT)

            try:
                await search_input.wait_for(state="visible", timeout=10000)
                # Clear any pre-filled value first.
                await search_input.fill("")
                await search_input.type(search_keyword, delay=50)
                await page.wait_for_timeout(1500)  # Let client-side filter run.
            except Exception:
                self.logger.warning(
                    "Mercari search input not found or not interactable; "
                    "parsing all visible cards."
                )

            # ---- Step 3: Parse cards ----
            soup = await self._get_soup(page)

            all_cards = soup.select(self.CARD_SELECTOR)

            if not all_cards:
                self.logger.warning("No Mercari job cards found after filter.")
                return jobs

            # Only keep visible cards (those NOT having style="display: none").
            visible_cards: list[Tag] = []
            for card in all_cards:
                style = (card.get("style") or "").replace(" ", "")
                if "display:none" not in style:
                    visible_cards.append(card)

            if not visible_cards:
                self.logger.warning(
                    "All Mercari cards are hidden after filter; "
                    "check search keyword or page structure."
                )
                return jobs

            seen_job_ids: set[str] = set()
            seen_urls: set[str] = set()

            for card in visible_cards[:max_jobs]:
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_job_ids:
                    continue

                if job.url in seen_urls:
                    continue

                # ---- Step 4: Enrich with detail page ----
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
                            "Failed to enrich Mercari job detail %s: %s",
                            job.url,
                            exc,
                        )

                if job.job_id:
                    seen_job_ids.add(job.job_id)

                seen_urls.add(job.url)
                jobs.append(job)

            return jobs

        finally:
            await self.close_browser()

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card)
        title = self._extract_title(card)
        job_id = self._extract_job_id(card, link)
        location = self._extract_location(card)
        posted_date = self._extract_posted_date(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Mercari"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=posted_date or None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)

        if not el:
            return ""

        href = el.get("href")

        return str(href) if href else ""

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_location(self, card: Tag) -> str:
        el = card.select_one(self.LOCATION_SELECTOR)

        if not el:
            return ""

        full_text = self._clean_text(el.get_text())

        # Strip "Location:" prefix.
        location = re.sub(r"^Location:\s*", "", full_text, flags=re.IGNORECASE)

        return self._clean_location_text(location)

    def _extract_posted_date(self, card: Tag) -> str:
        el = card.select_one(self.DATE_SELECTOR)

        if not el:
            return ""

        full_text = self._clean_text(el.get_text())

        # Strip "Creation date:" prefix.
        date = re.sub(r"^Creation date:\s*", "", full_text, flags=re.IGNORECASE)

        return date.strip()

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        Mercari/Workable job URLs look like:
        https://apply.workable.com/j/BF081E58B1
        """
        if link:
            match = re.search(r"/j/([A-Za-z0-9]+)", link)

            if match:
                return match.group(1)

        return extract_job_id(link) if link else ""

    async def _get_detail_page(self) -> Page:
        if self.context:
            try:
                return await self.context.new_page()
            except Exception:
                self.logger.debug(
                    "Shared browser context is no longer usable; discarding "
                    "and creating a fresh one."
                )
                await self.close_browser()

        return await self.new_page()

    async def _scrape_detail_page(self, job_url: str) -> str:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)

            try:
                await detail_page.wait_for_selector(
                    self.DESCRIPTION_SELECTOR,
                    timeout=15000,
                )
            except Exception:
                # Some workable pages may not have the description section.
                pass

            soup = await self._get_soup(detail_page)

            return self._extract_description(soup)

        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        container = soup.select_one(self.DESCRIPTION_SELECTOR)

        if not container:
            return ""

        for unwanted in container.select("script, style, noscript, img"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    def _clean_location_text(self, text: str) -> str:
        text = self._clean_text(text)

        if not text:
            return ""

        lower_text = text.lower()

        noise_values = {
            "location",
            "locations",
            "remote",
        }

        if lower_text in noise_values:
            return ""

        return text

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        text = text.replace("\xa0", " ")
        text = text.replace("&amp;", "&")
        text = re.sub(r"\s+", " ", text)

        return text.strip()

    def _clean_multiline_text(self, text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        text = text.replace("\xa0", " ")
        text = text.replace("&amp;", "&")

        lines = []
        for line in text.splitlines():
            clean_line = self._clean_text(line)

            if clean_line:
                lines.append(clean_line)

        return "\n".join(lines).strip()
