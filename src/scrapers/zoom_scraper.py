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


class ZoomScraper(BaseScraper):
    """
    Scraper for Zoom Careers job search pages (custom platform).

    The search results page renders job cards:

        div.row.job-search-results-card-row
          article.job-search-results-card-col
            div.card.job-search-results-card
              h3.card-title a[href*="/jobs/"]
              li.job-component-requisition-identifier span    (R-prefixed job ID)
              li.job-component-location span                  (location)
              li.job-component-remote span                    (Remote/On-site)
              li.job-component-category span                  (department)
              p.card-text.job-search-results-summary          (summary text)

    The detail page renders:

        div.job-description                                   (full description)
          h2, div, ul, li, p
    """

    CARD_SELECTOR = 'article.job-search-results-card-col'

    JOB_CARD_SELECTORS = [
        'article.job-search-results-card-col',
        'div.card.job-search-results-card',
        'h3.card-title a[href*="/jobs/"]',
    ]

    TITLE_SELECTOR = 'h3.card-title a'
    LOCATION_SELECTOR = 'li.job-component-location span'
    JOB_ID_SELECTOR = 'li.job-component-requisition-identifier span'
    SUMMARY_SELECTOR = 'p.card-text.job-search-results-summary'

    DESCRIPTION_SELECTOR = 'div.job-description'

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

            for card in cards:
                if len(jobs) >= max_jobs:
                    break

                job = self._parse_card(card, source_url)

                if not job or not job.url:
                    continue

                if job.job_id and job.job_id in seen_job_ids:
                    continue

                if job.url in seen_urls:
                    continue

                if job.job_id:
                    seen_job_ids.add(job.job_id)
                seen_urls.add(job.url)

                # Enrich with detail page description.
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
                            "Failed to enrich Zoom job detail page %s: %s",
                            job.url,
                            exc,
                        )

                jobs.append(job)

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
        link = self._extract_link(card)
        title = self._extract_title(card)
        job_id = self._extract_job_id(card, link)
        location = self._extract_location(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Zoom"),
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
        el = card.select_one(self.TITLE_SELECTOR)
        if not el:
            return ""
        href = el.get("href")
        if not href:
            return ""
        href = str(href).strip()
        if href.startswith("http://") or href.startswith("https://"):
            return href
        origin = "https://careers.zoom.us"
        if href.startswith("/"):
            return f"{origin}{href}"
        return f"{origin}/{href}"

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        if el:
            return self._clean_text(el.get_text())
        return ""

    def _extract_location(self, card: Tag) -> str:
        """Extract location from the location icon-and-text span."""
        el = card.select_one(self.LOCATION_SELECTOR)
        if el:
            return self._clean_text(el.get_text())
        return ""

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """Extract R-prefixed job ID from the requisition identifier span."""
        span = card.select_one(self.JOB_ID_SELECTOR)
        if span:
            text = self._clean_text(span.get_text())
            if text and text.upper().startswith("R") and any(ch.isdigit() for ch in text):
                return text

        # Fallback: extract from URL.
        match = re.search(r"/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", link)
        if match:
            return match.group(1)

        return extract_job_id(link) or ""

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

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
                    'div[class*="job-description"]',
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
            for cls_pattern in ['job-description', 'description', 'Description']:
                container = soup.select_one(f'div[class*="{cls_pattern}"]')
                if container:
                    break

        if not container:
            return ""

        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    # ------------------------------------------------------------------
    # Fallback
    # ------------------------------------------------------------------

    async def _fallback_links(self, page: Page, source_url: str, max_jobs: int) -> list[Job]:
        soup = await self._get_soup(page)
        jobs: list[Job] = []
        seen_urls: set[str] = set()

        for link in soup.select('a[href*="/jobs/"]'):
            if len(jobs) >= max_jobs:
                break
            href = link.get("href")
            if not href:
                continue
            href = str(href).strip()
            if href.startswith("/"):
                url = f"https://careers.zoom.us{href}"
            elif href.startswith("https://"):
                url = href
            else:
                url = f"https://careers.zoom.us/{href}"

            if url in seen_urls:
                continue
            seen_urls.add(url)

            title = self._clean_text(link.get_text())
            job_id = ""
            m = re.search(r"/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", url)
            if m:
                job_id = m.group(1)

            jobs.append(Job(
                job_id=job_id,
                company=self.company_config.get("name", "Zoom"),
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

    # ------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------

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
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
