from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class GoldmanSachsScraper(BaseScraper):
    """
    Scraper for Goldman Sachs Careers (higher.gs.com).

    Search URL pattern:
        https://higher.gs.com/results?EXPERIENCE_LEVEL=...&JOB_FUNCTION=...&LOCATION=...&page=1&sort=POSTED_DATE

    Expected search card structure:

        div.gs-uitk-mb-2
          div.d-flex.justify-content-between.border-bottom.gs-uitk-mb-2.gs-uitk-pb-1
            div
              a.text-decoration-none[href="/roles/{id}"]
                span.gs-uitk-c-nv7fiq--text-root  (title)
                div.d-flex
                  div[data-testid="location"]  (city · country)
                  div.d-flex.align-items-center  (· level)
            div.d-none.d-md-block
              span.gs-tag
                button[data-cy="gs-tag__button"]  (job function)

    Expected detail page structure:
        https://higher.gs.com/roles/{id}
        div[data-testid="job-description-html"]  (full description)
    """

    BASE_URL = "https://higher.gs.com"

    CARD_CONTAINER_SELECTOR = "div.gs-uitk-mb-2"
    CARD_SELECTOR = "div.d-flex.justify-content-between.border-bottom"
    LINK_SELECTOR = "a.text-decoration-none[href^='/roles/']"
    TITLE_SELECTOR = "span.gs-uitk-c-nv7fiq--text-root"
    LOCATION_SELECTOR = "div[data-testid='location']"
    TAG_BUTTON_SELECTOR = "button[data-cy='gs-tag__button']"
    DETAIL_DESCRIPTION_SELECTOR = "div[data-testid='job-description-html']"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for job cards to appear.
            try:
                await page.wait_for_selector(
                    self.CARD_SELECTOR, timeout=30000
                )
            except Exception:
                self.logger.warning(
                    "Goldman Sachs: card selector not found, trying fallback."
                )
                return await self._fallback_links(page, source_url, max_jobs)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("Goldman Sachs: no cards found.")
                return await self._fallback_links(page, source_url, max_jobs)

            seen_urls: set[str] = set()

            for card in cards[:max_jobs]:
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.url in seen_urls:
                    continue

                # Enrich with detail page data.
                try:
                    detail_data = await self._scrape_detail_page(job.url)

                    detail_title = detail_data.get("title", "")
                    detail_location = detail_data.get("location", "")
                    detail_description = detail_data.get("description", "")

                    # Only use detail location if it's meaningful (not just icon noise).
                    location = detail_location
                    if not location or self._is_icon_noise(location.split(",")[0].strip()):
                        location = job.location

                    job = Job(
                        job_id=job.job_id,
                        company=job.company,
                        title=detail_title or job.title,
                        location=location,
                        url=job.url,
                        source_url=job.source_url,
                        posted_date=job.posted_date,
                        description=detail_description or job.description,
                        scraped_at=datetime.now(timezone.utc).isoformat(),
                        matched_keywords=[],
                    )

                except Exception as exc:
                    self.logger.warning(
                        "Failed to enrich Goldman Sachs detail page %s: %s",
                        job.url,
                        exc,
                    )

                seen_urls.add(job.url)
                jobs.append(job)

            if not jobs:
                jobs = await self._fallback_links(page, source_url, max_jobs)

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card, source_url)
        title = self._extract_title(card)
        job_id = self._extract_job_id(link)
        location = self._extract_location(card)
        level = self._extract_level(card)
        job_function = self._extract_job_function(card)

        if not link or not title:
            return None

        # Build a richer card-level description with available metadata.
        description_parts: list[str] = []

        if job_function:
            description_parts.append(f"Job Function: {job_function}")

        if level:
            description_parts.append(f"Level: {level}")

        card_description = "\n".join(description_parts) if description_parts else ""

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Goldman Sachs"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=card_description or None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            matched_keywords=[],
        )

    def _extract_link(self, card: Tag, source_url: str) -> str:
        el = card.select_one(self.LINK_SELECTOR)

        if not el:
            return ""

        href = el.get("href")

        if not href:
            return ""

        href = str(href).strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        return f"{self.BASE_URL}{href}"

    def _extract_title(self, card: Tag) -> str:
        anchor = card.select_one(self.LINK_SELECTOR)

        if not anchor:
            return ""

        title_el = anchor.select_one(self.TITLE_SELECTOR)
        return self._clean_text(title_el.get_text() if title_el else "")

    def _extract_job_id(self, link: str) -> str:
        """Extract numeric role ID from /roles/{id} URLs."""
        if not link:
            return ""

        match = re.search(r"/roles/(\d+)", link)
        return match.group(1) if match else ""

    # Known Material Icons / icon text to filter out of location fields.
    _ICON_NOISE = {
        "location_on", "location", "place", "pin_drop",
        "share", "bookmark", "bookmark_border",
        "chevron_right", "chevron_left", "more_horiz",
        "search", "close", "menu", "arrow_drop_down",
        "favorite", "favorite_border", "star", "star_border",
    }

    def _is_icon_noise(self, text: str) -> bool:
        return text.lower() in self._ICON_NOISE

    def _extract_location(self, card: Tag) -> str:
        location_el = card.select_one(self.LOCATION_SELECTOR)

        if not location_el:
            return ""

        # Location looks like: <span>Bengaluru</span><span>·</span><span>India</span>
        spans = location_el.select("span")

        parts: list[str] = []

        for span in spans:
            text = self._clean_text(span.get_text())

            if text and text != "·" and not self._is_icon_noise(text):
                parts.append(text)

        return ", ".join(parts)

    def _extract_level(self, card: Tag) -> str:
        """
        Level appears after location in a div with class 'd-flex align-items-center'.
        The text looks like "· Analyst" or "· Associate".

        We look for the second 'd-flex align-items-center' div inside the
        location/level row (the first is the location wrapper).
        """
        anchor = card.select_one(self.LINK_SELECTOR)

        if not anchor:
            return ""

        # The level is in the div.d-flex > div.d-flex.align-items-center (the second one,
        # after the location div).
        d_flex = anchor.select_one("div.d-flex")

        if not d_flex:
            return ""

        level_divs = d_flex.select("div.d-flex.align-items-center")

        # Skip the location div, get the level div.
        for div in level_divs:
            text = self._clean_text(div.get_text())

            if text and text != "·":
                # Strip leading dot/separator.
                text = re.sub(r"^·\s*", "", text).strip()

                if text:
                    return text

        return ""

    def _extract_job_function(self, card: Tag) -> str:
        tag_button = card.select_one(self.TAG_BUTTON_SELECTOR)
        return self._clean_text(tag_button.get_text() if tag_button else "")

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def _scrape_detail_page(self, job_url: str) -> dict[str, str]:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)

            await detail_page.goto(
                job_url, wait_until="domcontentloaded", timeout=60000
            )

            # Wait for the job description to load.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_DESCRIPTION_SELECTOR, timeout=15000
                )
            except Exception:
                self.logger.debug(
                    "Goldman Sachs detail page: description selector not found for %s",
                    job_url,
                )

            soup = await self._get_soup(detail_page)

            title = self._extract_detail_title(soup)
            location = self._extract_detail_location(soup)
            description = self._extract_detail_description(soup)

            return {
                "title": title,
                "location": location,
                "description": description,
            }

        finally:
            await detail_page.close()

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

    def _extract_detail_title(self, soup) -> str:
        """
        On the detail page the title is usually in a heading or large text
        near the top. Try a few selectors.
        """
        # Goldman Sachs detail page typically has the role title as the page title
        # or as a prominent heading.
        selectors = [
            "h1",
            "[data-testid='job-title']",
            "head title",
        ]

        for sel in selectors:
            el = soup.select_one(sel)

            if el:
                text = self._clean_text(el.get_text())

                if text and len(text) > 3:
                    # If it's the <title> tag, strip common suffixes.
                    if sel == "head title":
                        text = re.sub(
                            r"\s*[-–|]\s*Goldman\s*Sachs.*$",
                            "",
                            text,
                            flags=re.IGNORECASE,
                        ).strip()

                    return text

        return ""

    def _extract_detail_location(self, soup) -> str:
        """
        Extract location from the detail page. Goldman Sachs detail pages
        usually have location info in metadata or near the title.
        """
        # First try explicit location testid.
        location_selectors = [
            "[data-testid='location']",
            "[data-testid='job-location']",
        ]

        for sel in location_selectors:
            el = soup.select_one(sel)

            if not el:
                continue

            spans = el.select("span")
            parts: list[str] = []

            for span in spans:
                text = self._clean_text(span.get_text())

                if text and text != "·" and not self._is_icon_noise(text):
                    parts.append(text)

            if parts:
                return ", ".join(parts)

        # Fallback: try to find location text that mentions known cities.
        body_text = soup.get_text()
        known_cities = [
            'Bengaluru', 'Hyderabad', 'Mumbai', 'Bangalore',
            'Delhi', 'Gurugram', 'Pune', 'Chennai', 'Kolkata',
        ]
        found: list[str] = []
        for city in known_cities:
            if city in body_text:
                found.append(f"{city}, India")
        if found:
            return ", ".join(found[:3])

        return ""

    def _extract_detail_description(self, soup) -> str:
        container = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)

        if not container:
            return ""

        # Remove script/style/noscript tags.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    # ------------------------------------------------------------------
    # Fallback
    # ------------------------------------------------------------------

    async def _fallback_links(
        self,
        page: Page,
        source_url: str,
        max_jobs: int,
    ) -> list[Job]:
        """
        Fallback: scan all /roles/ links on the page.
        """
        soup = await self._get_soup(page)

        anchors = soup.select("a[href*='/roles/']")

        results: list[Job] = []
        seen_urls: set[str] = set()
        seen_job_ids: set[str] = set()

        for anchor in anchors[:max_jobs]:
            href = anchor.get("href")

            if not href:
                continue

            href_str = str(href).strip()

            if href_str.startswith("http://") or href_str.startswith("https://"):
                job_url = href_str
            else:
                job_url = f"{self.BASE_URL}{href_str}"

            job_id = self._extract_job_id(job_url)

            if job_id and job_id in seen_job_ids:
                continue

            if job_url in seen_urls:
                continue

            title = self._clean_text(anchor.get_text())

            if not title:
                continue

            if job_id:
                seen_job_ids.add(job_id)

            seen_urls.add(job_url)

            # Try to get detail page data.
            description = ""

            try:
                detail_data = await self._scrape_detail_page(job_url)
                detail_title = detail_data.get("title", "")
                detail_location = detail_data.get("location", "")
                detail_description = detail_data.get("description", "")

                if detail_title:
                    title = detail_title

                if detail_description:
                    description = detail_description

                location = detail_location
            except Exception:
                location = ""

            results.append(
                Job(
                    job_id=job_id,
                    company=self.company_config.get("name", "Goldman Sachs"),
                    title=title,
                    location=location,
                    url=job_url,
                    source_url=source_url,
                    posted_date=None,
                    description=description or None,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    matched_keywords=[],
                )
            )

        return results

    # ------------------------------------------------------------------
    # Text cleaning
    # ------------------------------------------------------------------

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)

        return text.strip()

    def _clean_multiline_text(self, text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        text = text.replace("\xa0", " ")

        lines: list[str] = []

        for line in text.splitlines():
            clean_line = self._clean_text(line)

            if clean_line:
                lines.append(clean_line)

        return "\n".join(lines).strip()
