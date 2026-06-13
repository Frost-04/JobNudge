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


class RokuScraper(BaseScraper):
    """
    Scraper for Roku job board (custom server-rendered table).

    The listing page is a simple HTML table with <tr role="link"> rows.
    The URL is pre-filtered to show only India locations with Data Science,
    Internships, and Software Engineering categories.

    Expected listing structure:

    table.table
      thead
        th.job-search-results-title
        th.job-search-results-location
        th.job-search-results-string_field_1
      tbody
        tr[role="link"][data-job-url]
          td.job-search-results-title
            a[href]              (job title text)
          td.job-search-results-location
            ul > li              (location text)
          td.job-search-results-string_field_1   (job category)

    Expected detail page structure:

    div.job-description          (full rich-text job description)
    """

    CARD_SELECTOR = 'tr[role="link"]'

    JOB_CARD_SELECTORS = [
        'tr[role="link"]',
        'table.table tbody tr',
        'table.table',
    ]

    TITLE_SELECTOR = 'td.job-search-results-title a'
    LOCATION_SELECTOR = 'td.job-search-results-location ul li'
    LINK_SELECTOR = 'td.job-search-results-title a'

    DESCRIPTION_SELECTOR = 'div.job-description'

    # Roku pages can have many results; cap at 15 as requested.
    PAGE_MAX_JOBS = 15

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))
        max_jobs = min(max_jobs, self.PAGE_MAX_JOBS)

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            selector = await self._wait_for_any_selector(page, self.JOB_CARD_SELECTORS)

            if not selector:
                return await self._fallback_links(page, source_url, max_jobs)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                return await self._fallback_links(page, source_url, max_jobs)

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

                # Enrich by opening the job detail page.
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
                else:
                    try:
                        detail_data = await self._scrape_detail_page(job.url)

                        detail_description = detail_data.get("description", "")

                        if detail_description:
                            job = Job(
                                job_id=job.job_id,
                                company=job.company,
                                title=job.title,
                                location=job.location,
                                url=job.url,
                                source_url=job.source_url,
                                posted_date=job.posted_date,
                                description=detail_description,
                                scraped_at=datetime.now(timezone.utc).isoformat(),
                                extracted_experience_parts="",
                            )

                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich Roku job detail page %s: %s",
                            job.url,
                            exc,
                        )

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

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Roku"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag, source_url: str) -> str:
        # Prefer data-job-url attribute on the tr element.
        href = card.get("data-job-url")

        if href:
            return self._make_job_url(source_url, str(href))

        el = card.select_one(self.LINK_SELECTOR)

        if not el:
            return ""

        href = el.get("href")

        if not href:
            return ""

        return self._make_job_url(source_url, str(href))

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        # Fallback: aria-label on the anchor
        link = card.select_one(self.LINK_SELECTOR)

        if link:
            aria_label = link.get("aria-label", "")

            if aria_label and "Title:" in aria_label:
                return self._clean_text(aria_label.replace("Title:", "").strip())

        return ""

    def _extract_location(self, card: Tag) -> str:
        el = card.select_one(self.LOCATION_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        # Fallback: aria-label on the li
        loc_li = card.select_one('td.job-search-results-location li')

        if loc_li:
            aria_label = loc_li.get("aria-label", "")

            if aria_label and "Location:" in aria_label:
                return self._clean_text(aria_label.replace("Location:", "").strip())

        return ""

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        Roku job URLs look like:

        https://www.weareroku.com/jobs/senior-software-engineer-bengaluru-553ccc2b-2c6e-4031-84e0-5aad2ef57b60
        https://www.weareroku.com/jobs/sr-manager-machine-learning-bengaluru-karnataka-india

        Extract the last path segment as the job ID, stripping the trailing UUID if present.
        """
        if not link:
            return ""

        return self._extract_job_id_from_url(link)

    async def _get_detail_page(self) -> Page:
        if self.context:
            try:
                return await self.context.new_page()
            except Exception:
                self.logger.debug(
                    "Shared browser context is no longer usable; discarding and creating a fresh one."
                )
                await self.close_browser()

        return await self.new_page()

    async def _scrape_detail_page(self, job_url: str) -> dict[str, str]:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)

            await self._wait_for_any_selector(
                detail_page,
                [
                    self.DESCRIPTION_SELECTOR,
                    'div.job-description',
                    'h1',
                    'h2',
                ],
            )

            soup = await self._get_soup(detail_page)

            detail_data: dict[str, str] = {}
            description = self._extract_description(soup)

            if description:
                detail_data["description"] = description

            return detail_data

        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        container = soup.select_one(self.DESCRIPTION_SELECTOR)

        if not container:
            return ""

        # Remove non-description elements
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    def _format_detail_metadata(self, detail_data: dict[str, str]) -> str:
        """Roku detail pages embed everything in the description div."""
        return ""

    def _join_description_parts(self, *parts: str) -> str:
        cleaned_parts = [part.strip() for part in parts if part and part.strip()]
        return "\n\n".join(cleaned_parts)

    def _make_job_url(self, source_url: str, href: str) -> str:
        href = html.unescape(href).strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        parsed_source = urlparse(source_url)
        origin = f"{parsed_source.scheme}://{parsed_source.netloc}"

        if href.startswith("/"):
            return f"{origin}{href}"

        return make_absolute_url(source_url, href)

    def _extract_job_id_from_url(self, url: str) -> str:
        """
        Roku URLs end with a descriptive slug, optionally followed by a UUID.
        Use the full URL path as the job ID.
        """
        if not url:
            return ""

        # Extract the path and use it as a unique identifier.
        parsed = urlparse(url)
        path = parsed.path.strip("/")

        if path:
            # Take last 2 segments of the path for brevity.
            segments = path.split("/")

            if len(segments) >= 2:
                return f"{segments[-2]}/{segments[-1]}"

            return segments[-1]

        return url

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)

        return text.strip()

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        text = text.replace("\xa0", " ")

        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                lines.append(stripped)

        return "\n".join(lines)
