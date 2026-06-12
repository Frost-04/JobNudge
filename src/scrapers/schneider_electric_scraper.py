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


class SchneiderElectricScraper(BaseScraper):
    """
    Scraper for Schneider Electric job board (careers.se.com).

    Angular Material SPA with mat-expansion-panel accordion cards.
    Cards are server-rendered in the initial HTML, but we use
    ``networkidle`` to ensure the Angular app has fully bootstrapped.

    Expected listing structure:

    mat-accordion.cards
      mat-expansion-panel.search-result-item[data-index]
        mat-expansion-panel-header
          mat-panel-title
            p.job-title
              a.job-title-link[href="/jobs/{id}"]
                span[itemprop="title"]        (job title)
            p.req-id span                    (numeric job ID)
          mat-panel-description
            div.description-container
              span.column
                div.job-result__location
                  span.label-value.location  (location text)

    Expected detail page structure:

    article#description-body.main-description-body[itemprop="description"]
      p / ul / li                            (full job description)
    """

    CARD_SELECTOR = 'mat-expansion-panel.search-result-item'

    JOB_CARD_SELECTORS = [
        'mat-expansion-panel.search-result-item',
        'mat-accordion.cards',
        'div.job-results-container',
    ]

    TITLE_SELECTOR = 'a.job-title-link span[itemprop="title"]'
    REQ_ID_SELECTOR = 'p.req-id span'
    LOCATION_SELECTOR = 'div.job-result__location span.label-value.location'

    DESCRIPTION_SELECTOR = 'article#description-body'

    LISTING_BASE = "https://careers.se.com"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="networkidle", timeout=120000)
            await page.wait_for_timeout(3000)

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
                            "Failed to enrich Schneider Electric detail page %s: %s",
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
            company=self.company_config.get("name", "Schneider Electric"),
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

        # Navigate up to the <a> parent
        anchor = el.find_parent("a")

        if not anchor:
            return ""

        href = anchor.get("href")

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
            text = el.get_text(separator=" ", strip=True)
            return self._clean_text(text)

        return ""

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        Schneider Electric cards include a Req ID span directly:

        <p class="req-id">
          Req ID:
          <span>116720</span>
        </p>

        This is preferred over parsing the URL since it's always present.
        """
        # Primary: parse Req ID from the card text.
        req_id_el = card.select_one(self.REQ_ID_SELECTOR)

        if req_id_el:
            req_id = req_id_el.get_text(strip=True)

            if req_id and req_id.isdigit():
                return req_id

        # Fallback: parse from the detail link URL.
        if link:
            # URL pattern: /jobs/116720?lang=en-us&...
            parsed = urlparse(link)
            path = parsed.path.rstrip("/")
            match = re.search(r"/jobs/(\d+)", path)

            if match:
                return match.group(1)

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
            await detail_page.goto(job_url, wait_until="networkidle", timeout=60000)

            await self._wait_for_any_selector(
                detail_page,
                [
                    self.DESCRIPTION_SELECTOR,
                    'article.main-description-body',
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
            # Try fallback selector.
            container = soup.select_one("article.main-description-body")

        if not container:
            return ""

        # Remove non-description elements.
        for unwanted in container.select("script, style, noscript, iframe"):
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

        if href.startswith("/"):
            return f"{self.LISTING_BASE}{href}"

        return f"{self.LISTING_BASE}/{href}"

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

            if not href:
                continue

            href_str = str(href)

            # Skip non-job-detail links.
            if not re.search(r"/jobs/\d+", href_str):
                continue

            url = self._make_job_url(source_url, href_str)

            if url in seen_urls:
                continue

            seen_urls.add(url)

            title_el = link.select_one('span[itemprop="title"]')

            if title_el:
                title = self._clean_text(title_el.get_text())
            else:
                title = self._clean_text(link.get_text())

            if not title or len(title) < 3:
                continue

            job_id = ""
            match = re.search(r"/jobs/(\d+)", href_str)

            if match:
                job_id = match.group(1)

            jobs.append(Job(
                job_id=job_id,
                company=self.company_config.get("name", "Schneider Electric"),
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
            clean_line = SchneiderElectricScraper._clean_text(line)

            if clean_line:
                lines.append(clean_line)

        return "\n".join(lines)
