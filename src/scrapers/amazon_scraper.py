from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class AmazonScraper(BaseScraper):
    """
    Scraper for Amazon Jobs result pages.

    Expected Amazon job card structure:

    div.job-tile
      div.job[data-job-id]
        h3.job-title > a.job-link
        div.location-and-id li.text-nowrap
        span.posting-date
        div.qualifications-preview
    """

    JOB_CARD_SELECTORS = [
        "div.job-tile div.job[data-job-id]",
        "div.job-tile",
    ]

    JOB_LINK_SELECTOR = "h3.job-title a.job-link"
    TITLE_SELECTOR = "h3.job-title a.job-link"
    POSTED_SELECTOR = "span.posting-date"
    DESCRIPTION_SELECTOR = "div.qualifications-preview"
    LOCATION_ITEM_SELECTOR = "div.location-and-id li.text-nowrap"
    EXTRA_LOCATION_BUTTON_SELECTOR = "div.location-and-id button.popover-button[data-content]"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))
        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded")
            await page.wait_for_load_state("networkidle")

            # Wait for at least one card selector to appear (confirms JS rendered).
            await self._wait_for_any_selector(page, self.JOB_CARD_SELECTORS)

            # Parse the fully-rendered page with BeautifulSoup.
            # This replaces per-element Playwright locator calls with a single
            # page.content() + in-process BS4 traversal — much faster.
            soup = await self._get_soup(page)

            # Try the preferred selector first, then the fallback.
            cards = soup.select("div.job-tile div.job[data-job-id]")
            if not cards:
                cards = soup.select("div.job-tile")

            if cards:
                seen_job_ids: set[str] = set()
                seen_urls: set[str] = set()

                for card in cards[:max_jobs]:
                    job = self._parse_card(card, source_url)

                    if not job:
                        continue

                    # Prefer dedupe by job_id. Fallback to url.
                    if job.job_id and job.job_id in seen_job_ids:
                        continue

                    if job.url in seen_urls:
                        continue

                    if job.job_id:
                        seen_job_ids.add(job.job_id)

                    seen_urls.add(job.url)
                    jobs.append(job)

            if not jobs:
                jobs = await self._fallback_links(page, source_url, max_jobs)

            return jobs

        finally:
            await self.close_browser()

    async def _wait_for_any_selector(self, page: Page, selectors: list[str]) -> str | None:
        timeout_ms = self._to_ms(
            self.settings.get("run", {}).get("page_load_timeout_seconds"),
            45000,
        )

        for selector in selectors:
            try:
                await page.wait_for_selector(selector, timeout=timeout_ms)
                return selector
            except Exception:
                continue

        return None

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card, source_url)
        title = self._extract_title(card)
        job_id = self._extract_job_id(card, link)
        location = self._extract_location(card)
        posted_date = self._extract_posted_date(card)
        description = self._extract_description(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Amazon"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=posted_date or None,
            description=description or None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        Amazon exposes job id directly:

            <div class="job" data-job-id="10432823">

        If not available, fallback to extracting it from the URL.
        """

        job_id = card.get("data-job-id")

        if job_id:
            return self._clean_text(str(job_id))

        # If selector matched div.job-tile instead of div.job[data-job-id],
        # the data-job-id may be on a child div.job.
        nested = card.select_one("div.job[data-job-id]")
        if nested:
            nested_job_id = nested.get("data-job-id")
            if nested_job_id:
                return self._clean_text(str(nested_job_id))

        return extract_job_id(link) if link else ""

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_link(self, card: Tag, source_url: str) -> str:
        el = card.select_one(self.JOB_LINK_SELECTOR)
        if not el:
            return ""

        href = el.get("href")
        if not href:
            return ""

        return make_absolute_url(source_url, str(href))

    def _extract_posted_date(self, card: Tag) -> str:
        el = card.select_one(self.POSTED_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_description(self, card: Tag) -> str:
        el = card.select_one(self.DESCRIPTION_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_location(self, card: Tag) -> str:
        """
        Extract visible and hidden Amazon job locations.

        Visible locations:
            <li class="text-nowrap">Bengaluru, KA, IND</li>

        Hidden popover locations:
            data-content="&lt;ul&gt;&lt;li&gt;Delhi, IND&lt;/li&gt;..."
        """

        locations: list[str] = []

        for item in card.select(self.LOCATION_ITEM_SELECTOR):
            text = self._clean_location_text(item.get_text())
            if text:
                locations.append(text)

        for button in card.select(self.EXTRA_LOCATION_BUTTON_SELECTOR):
            raw_content = button.get("data-content")
            if raw_content:
                hidden_locations = self._extract_locations_from_popover(str(raw_content))
                locations.extend(hidden_locations)

        unique_locations = self._dedupe_preserve_order(locations)

        return ", ".join(unique_locations)

    def _extract_locations_from_popover(self, raw_content: str) -> list[str]:
        """Parse hidden popover HTML with BeautifulSoup instead of regex.

        Amazon stores extra locations as encoded HTML in a data-content
        attribute like: &lt;ul&gt;&lt;li&gt;Delhi, IND&lt;/li&gt;...
        """
        decoded = html.unescape(raw_content)
        soup = BeautifulSoup(decoded, "html.parser")

        locations: list[str] = []
        for li in soup.find_all("li"):
            text = self._clean_location_text(li.get_text())
            if text:
                locations.append(text)

        return locations

    def _clean_location_text(self, text: str) -> str:
        text = self._clean_text(text)

        if not text:
            return ""

        lower_text = text.lower()

        if text == "|":
            return ""

        if lower_text.startswith("job id"):
            return ""

        if "other location" in lower_text:
            return ""

        return text

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _dedupe_preserve_order(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []

        for value in values:
            normalized = value.lower().strip()

            if not normalized or normalized in seen:
                continue

            seen.add(normalized)
            result.append(value)

        return result

    async def _fallback_links(self, page: Page, source_url: str, max_jobs: int) -> list[Job]:
        """
        Fallback for unexpected page changes.

        Uses BS4 to walk the full page DOM looking for job links,
        then navigates up to find parent cards for richer extraction.
        """

        soup = await self._get_soup(page)
        anchors = soup.select("h3.job-title a.job-link[href*='/jobs/'], a[href*='/en/jobs/']")

        results: list[Job] = []
        seen_job_ids: set[str] = set()
        seen_urls: set[str] = set()

        for anchor in anchors[:max_jobs]:
            href = anchor.get("href")
            if not href or not self._is_job_href(str(href)):
                continue

            job_url = make_absolute_url(source_url, str(href))
            job_id = extract_job_id(job_url)

            if job_id and job_id in seen_job_ids:
                continue

            if job_url in seen_urls:
                continue

            title = self._clean_text(anchor.get_text())
            if not title:
                continue

            location = ""
            posted_date = ""
            description = ""

            # Walk up to the parent job-tile card for richer extraction.
            card = anchor.find_parent("div", class_="job-tile")
            if card:
                card_job_id = self._extract_job_id(card, job_url)
                if card_job_id:
                    job_id = card_job_id
                location = self._extract_location(card)
                posted_date = self._extract_posted_date(card)
                description = self._extract_description(card)

            if job_id:
                seen_job_ids.add(job_id)

            seen_urls.add(job_url)

            results.append(
                Job(
                    job_id=job_id,
                    company=self.company_config.get("name", "Amazon"),
                    title=title,
                    location=location,
                    url=job_url,
                    source_url=source_url,
                    posted_date=posted_date or None,
                    description=description or None,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    extracted_experience_parts="",
                )
            )

        return results

    def _is_job_href(self, href: str) -> bool:
        if "/jobs/" not in href:
            return False

        if "amazon.jobs" in href:
            return True

        return href.startswith("/en/jobs/") or href.startswith("/jobs/")