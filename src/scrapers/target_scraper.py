from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class TargetScraper(BaseScraper):
    """
    Scraper for Target India Careers job search pages.

    Server-rendered HTML.  Each card links to a separate detail page
    that contains the full job description.  Only the first page is scraped.

    Listing card structure:

        #search-results-list ul > li
          a[data-job-id][href^="/job/"]
            h2
            span.sr-facet (job id, job family)

    Detail page structure:

        div.ats-description
    """

    RESULTS_CONTAINER = "section#search-results-list"
    CARD_SELECTOR = "#search-results-list ul > li a[data-job-id]"
    TITLE_SELECTOR = "h2"
    JOB_ID_FACET_SELECTOR = "span.sr-facet"

    DETAIL_DESCRIPTION_SELECTOR = "div.ats-description"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for results to render
            try:
                await page.wait_for_selector(self.RESULTS_CONTAINER, timeout=15000)
            except Exception:
                self.logger.warning("No results container found for Target")

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No Target job cards found")

            for card in cards[:max_jobs]:
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_ids:
                    continue
                if job.url in seen_urls:
                    continue

                # Enrich with detail page description
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
                            "Failed to enrich Target job detail %s: %s",
                            job.url,
                            exc,
                        )

                if job.job_id:
                    seen_ids.add(job.job_id)
                seen_urls.add(job.url)
                jobs.append(job)

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        title_el = card.select_one(self.TITLE_SELECTOR)
        if not title_el:
            return None

        title = self._clean_text(title_el.get_text())
        if not title:
            return None

        href = card.get("href")
        if not href:
            return None

        url = self._make_target_job_url(source_url, str(href))

        # Job ID: prefer the R-number from sr-facet, fallback to data-job-id
        job_id = self._extract_target_job_id(card)

        # Location from URL path: /job/bengaluru/...
        location = self._extract_location_from_url(url)

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Target"),
            title=title,
            location=location,
            url=url,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_target_job_id(self, card: Tag) -> str:
        """Extract job ID preferring the R-number from sr-facet text."""
        facets = card.select(self.JOB_ID_FACET_SELECTOR)
        for facet in facets:
            text = self._clean_text(facet.get_text())
            # Look for "job id: R0000439188" pattern
            match = re.search(r"job id:\s*(R\d+)", text, re.IGNORECASE)
            if match:
                return match.group(1)

        # Fallback: data-job-id attribute (numeric)
        data_job_id = card.get("data-job-id")
        if data_job_id:
            return str(data_job_id)

        return ""

    def _extract_location_from_url(self, url: str) -> str:
        """Extract location from URL path: /job/bengaluru/title/34386/id"""
        if not url:
            return ""

        match = re.search(r"/job/([^/]+)/", url)
        if match:
            city = match.group(1).replace("-", " ").title()
            return city

        return ""

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

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

    async def _scrape_detail_page(self, job_url: str) -> str:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)

            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_DESCRIPTION_SELECTOR,
                    timeout=15000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            desc_container = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)
            if not desc_container:
                return ""

            return self._extract_description(desc_container)

        finally:
            await detail_page.close()

    def _extract_description(self, container: Tag) -> str:
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    def _make_target_job_url(self, source_url: str, href: str) -> str:
        href = href.strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        parsed_source = urlparse(source_url)
        origin = f"{parsed_source.scheme}://{parsed_source.netloc}"

        if href.startswith("/"):
            return f"{origin}{href}"

        return make_absolute_url(source_url, href)

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        if not text:
            return ""
        lines = [line.strip() for line in text.splitlines()]
        lines = [line for line in lines if line]
        return "\n".join(lines)

    @staticmethod
    def _dedupe_preserve_order(items: list) -> list:
        seen: set[str] = set()
        result: list = []
        for item in items:
            key = str(item).lower()
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result
