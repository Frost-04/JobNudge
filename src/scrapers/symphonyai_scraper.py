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


class SymphonyAIScraper(BaseScraper):
    """
    Scraper for SymphonyAI Careers (iCIMS Angular Material platform).

    Expected listing card structure (Angular Material expansion panels):

    mat-expansion-panel.search-result-item[data-index]
      mat-expansion-panel-header
        mat-panel-title
          p.job-title
            a.job-title-link[href^="/jobs/"]
              span[itemprop="title"]
          p.req-id
            span
        mat-panel-description
          .description-container
            span.label-value.location
            span.categories.label-value

    Expected detail page structure:

    div.column.left
      h1[itemprop="title"] a              — title
      ul.meta-data-options
        li#header-locations                — location
        li#header-categories               — category
        li#header-req_id                   — req ID
      descriptions-body
        article#description-body[itemprop="description"]  — description
    """

    CARD_SELECTOR = 'mat-expansion-panel.search-result-item, .search-result-item'

    JOB_CARD_SELECTORS = [
        'mat-expansion-panel.search-result-item',
        '.search-result-item',
        'a.job-title-link[href*="/jobs/"]',
    ]

    TITLE_LINK_SELECTOR = 'a.job-title-link'
    TITLE_TEXT_SELECTOR = 'a.job-title-link span[itemprop="title"]'
    REQ_ID_SELECTOR = 'p.req-id span'
    LOCATION_SELECTOR = 'span.label-value.location'
    CATEGORY_SELECTOR = 'span.categories.label-value'

    DESCRIPTION_SELECTOR = 'article#description-body, [itemprop="description"]'

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

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

                        detail_posted_date = detail_data.get("date posted", "")
                        detail_description = detail_data.get("description", "")

                        metadata_description = self._format_detail_metadata(detail_data)

                        combined_description = self._join_description_parts(
                            metadata_description,
                            detail_description,
                        )

                        job = Job(
                            job_id=job.job_id,
                            company=job.company,
                            title=job.title,
                            location=detail_data.get("location", job.location),
                            url=job.url,
                            source_url=job.source_url,
                            posted_date=detail_posted_date or job.posted_date,
                            description=combined_description or job.description,
                            scraped_at=datetime.now(timezone.utc).isoformat(),
                            extracted_experience_parts="",
                        )

                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich SymphonyAI job detail page %s: %s",
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
        job_id = self._extract_job_id_from_card(card, link)
        location = self._extract_location(card)
        category = self._extract_category(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "SymphonyAI"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=category or None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag, source_url: str) -> str:
        el = card.select_one(self.TITLE_LINK_SELECTOR)

        if not el:
            return ""

        href = el.get("href")

        if not href:
            return ""

        return self._make_symphonyai_job_url(source_url, str(href))

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_TEXT_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        # Fallback: try the link itself
        link = card.select_one(self.TITLE_LINK_SELECTOR)

        if link:
            return self._clean_text(link.get_text())

        return ""

    def _extract_location(self, card: Tag) -> str:
        """
        SymphonyAI location looks like multi-line: "Bengaluru, Karnataka\nIN\n"
        We take the first meaningful line.
        """
        el = card.select_one(self.LOCATION_SELECTOR)

        if not el:
            return ""

        raw_text = el.get_text()

        lines = [
            line.strip()
            for line in raw_text.splitlines()
            if line.strip()
        ]

        if not lines:
            return ""

        return self._clean_text(lines[0])

    def _extract_category(self, card: Tag) -> str:
        el = card.select_one(self.CATEGORY_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        return ""

    def _extract_job_id_from_card(self, card: Tag, link: str) -> str:
        """
        SymphonyAI job URLs look like: /jobs/3060?lang=en-us
        The req-id span also contains the numeric ID.
        """

        if link:
            job_id = self._extract_symphonyai_job_id_from_url(link)

            if job_id:
                return job_id

        # Fallback: extract from req-id span
        el = card.select_one(self.REQ_ID_SELECTOR)

        if el:
            text = self._clean_text(el.get_text())

            if text and text.isdigit():
                return text

        return extract_job_id(link) if link else ""

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
                    'descriptions-body',
                    '.main-description-section',
                    'h1[itemprop="title"]',
                ],
            )

            soup = await self._get_soup(detail_page)

            detail_data: dict[str, str] = {}

            # Extract metadata fields from detail page
            location_el = soup.select_one('li#header-locations')
            if location_el:
                detail_data["location"] = self._clean_text(location_el.get_text())

            category_el = soup.select_one('li#header-categories')
            if category_el:
                detail_data["category"] = self._clean_text(category_el.get_text())

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
        """Format metadata from the detail page as a prefix to the description."""
        parts = []

        category = detail_data.get("category", "")
        if category:
            parts.append(f"Department: {category}")

        return "\n".join(parts)

    def _join_description_parts(self, *parts: str) -> str:
        cleaned_parts = [part.strip() for part in parts if part and part.strip()]
        return "\n\n".join(cleaned_parts)

    def _make_symphonyai_job_url(self, source_url: str, href: str) -> str:
        href = html.unescape(href).strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        parsed_source = urlparse(source_url)
        origin = f"{parsed_source.scheme}://{parsed_source.netloc}"

        if href.startswith("/jobs/"):
            return f"{origin}{href}"

        if href.startswith("jobs/"):
            return f"{origin}/{href}"

        return make_absolute_url(source_url, href)

    def _extract_symphonyai_job_id_from_url(self, url: str) -> str:
        if not url:
            return ""

        match = re.search(r"/jobs/(\d+)", url, flags=re.IGNORECASE)

        if match:
            return match.group(1)

        return extract_job_id(url) or ""

    async def _fallback_links(self, page: Page, source_url: str, max_jobs: int) -> list[Job]:
        """
        Fallback: extract jobs from title links when card selectors fail.
        """
        soup = await self._get_soup(page)

        jobs: list[Job] = []
        seen_urls: set[str] = set()

        for link in soup.select(self.TITLE_LINK_SELECTOR):
            if len(jobs) >= max_jobs:
                break

            href = link.get("href")

            if not href or "/jobs/" not in str(href):
                continue

            url = self._make_symphonyai_job_url(source_url, str(href))

            if url in seen_urls:
                continue

            seen_urls.add(url)

            title = self._clean_text(link.get_text())

            if not title:
                continue

            job_id = self._extract_symphonyai_job_id_from_url(url)

            jobs.append(
                Job(
                    job_id=job_id,
                    company=self.company_config.get("name", "SymphonyAI"),
                    title=title,
                    location="",
                    url=url,
                    source_url=source_url,
                    posted_date=None,
                    description=None,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    extracted_experience_parts="",
                )
            )

        return jobs

    # ------------------------------------------------------------------
    # Text helpers
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
        text = re.sub(r" +\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
