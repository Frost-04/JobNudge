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


class WaymoScraper(BaseScraper):
    """
    Scraper for Waymo job board (careers.withwaymo.com).

    Custom Bootstrap-based job board with server-rendered cards and
    detail pages. Cards may lazy-load on scroll, so we scroll to the
    bottom and back up before parsing.

    Expected listing structure:

    div.row.job-search-results-card-row
      article.col-12.job-search-results-card-col
        div.card.job-search-results-card
          div.card-body.job-search-results-card-body
            h3.card-title.job-search-results-card-title
              a[href]                 (title text + detail URL)
            div.job-component-details
              li.job-component-location span    (location)
            p.card-text.job-search-results-summary  (truncated summary)

    Expected detail page structure:

    div.job-description
      div.content-intro              (company intro)
      p / ul / li                    (role description)
      div.content-pay-transparency   (salary info — removed)

    Job IDs come from the detail URL — either a trailing UUID or the
    last path segment (slug).
    """

    CARD_SELECTOR = 'article.job-search-results-card-col'

    JOB_CARD_SELECTORS = [
        'article.job-search-results-card-col',
        'div.job-search-results-card-row',
        'div.job-search-results-card',
    ]

    TITLE_SELECTOR = 'h3.job-search-results-card-title a'
    LOCATION_SELECTOR = 'li.job-component-location span'
    SUMMARY_SELECTOR = 'p.job-search-results-summary'

    DESCRIPTION_SELECTOR = 'div.job-description'

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            # Scroll to trigger lazy-loaded cards, then back up.
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(2000)

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
                            "Failed to enrich Waymo job detail page %s: %s",
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
            company=self.company_config.get("name", "Waymo"),
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
        el = card.select_one(self.TITLE_SELECTOR)

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

        return ""

    def _extract_location(self, card: Tag) -> str:
        el = card.select_one(self.LOCATION_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        return ""

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        Waymo job URLs come in two forms:

        1. With UUID suffix:
           /jobs/senior-ml-compiler-engineer-compute-bengaluru-karnataka-india-7807dd1b-0481-4cad-8cfd-c4d460766f1a

        2. Slug-only:
           /jobs/software-engineer-post-silicon-testing-bengaluru-karnataka-india

        Prefer the UUID if present, otherwise use the last path segment.
        """
        if not link:
            return ""

        # Try to extract a UUID from the URL.
        uuid_match = re.search(
            r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
            link,
            flags=re.IGNORECASE,
        )

        if uuid_match:
            return uuid_match.group(1)

        # Fall back to the last path segment.
        parsed = urlparse(link)
        path = parsed.path.rstrip("/")

        if path:
            segments = path.split("/")
            return segments[-1] if segments else ""

        return ""

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

        # Remove pay transparency section (salary info not needed).
        for pay_section in container.select("div.content-pay-transparency"):
            pay_section.decompose()

        # Remove non-description elements.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    def _format_detail_metadata(self, detail_data: dict[str, str]) -> str:
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

    async def _fallback_links(self, page: Page, source_url: str, max_jobs: int) -> list[Job]:
        """
        Fallback: extract jobs from anchor links when card selectors fail.
        """
        soup = await self._get_soup(page)

        jobs: list[Job] = []
        seen_urls: set[str] = set()

        for link in soup.select('a[href*="/jobs/"]'):
            if len(jobs) >= max_jobs:
                break

            href = link.get("href")

            if not href or "/jobs/" not in str(href):
                continue

            # Skip non-job links (e.g., navigation, pagination).
            href_str = str(href)

            if any(skip in href_str for skip in ["/search?", "/jobs/search"]):
                continue

            url = self._make_job_url(source_url, href_str)

            if url in seen_urls:
                continue

            seen_urls.add(url)

            title = self._clean_text(link.get_text())

            if not title or len(title) < 5:
                continue

            job_id = self._extract_job_id(None, url)

            jobs.append(Job(
                job_id=job_id,
                company=self.company_config.get("name", "Waymo"),
                title=title,
                location="",
                url=url,
                source_url=source_url,
                posted_date=None,
                description=None,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                extracted_experience_parts="",
            ))

        return jobs

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
            clean_line = WaymoScraper._clean_text(line)

            if clean_line:
                lines.append(clean_line)

        return "\n".join(lines)
