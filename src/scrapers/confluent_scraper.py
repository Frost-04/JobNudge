from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class ConfluentScraper(BaseScraper):
    """
    Scraper for Confluent job board (careers.confluent.io).

    Custom Next.js/React board with server-rendered listing cards and
    separate detail pages for each job.

    Expected listing structure:

    div.flex.w-full.flex-col.bg-white                         (listing container)
      div[border-t border-gray-400/25 py-[1.74rem]]           (each card)
        a[href="/jobs/job/{uuid}"]
          div > p.font-inter.text-[1.125rem]                  (job title)
        p.font-inter.text-[1rem].text-[#686983]               (location)

    Expected detail page structure:

    div.job-description                                        (full description)

    Job IDs are UUIDs from the URL path
    (e.g. /jobs/job/5c5eb5ed-e880-447e-a76e-e056ce2acfb9).
    """

    CARD_LINK_SELECTOR = 'a[href*="/jobs/job/"]'

    JOB_CARD_SELECTORS = [
        'a[href*="/jobs/job/"]',
        'div.job-description',
        'h1',
    ]

    TITLE_SELECTORS = [
        'a[href*="/jobs/job/"] p',
        'p.font-inter.flex-wrap',
    ]

    LOCATION_SELECTORS = [
        'p.font-inter.text-\\[1rem\\]',
        'p.text-\\[\\#686983\\]',
    ]

    DESCRIPTION_SELECTOR = 'div.job-description'

    MAX_PAGES = 20

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []
        seen_job_ids: set[str] = set()
        seen_urls: set[str] = set()
        current_page = 1

        try:
            while current_page <= self.MAX_PAGES and len(jobs) < max_jobs:
                page_url = self._build_page_url(source_url, current_page)

                await page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(3000)

                soup = await self._get_soup(page)

                # Find all job links on the current page.
                links = soup.select(self.CARD_LINK_SELECTOR)

                if not links:
                    # No more cards on this page — we're done.
                    break

                for link in links:
                    if len(jobs) >= max_jobs:
                        break

                    job = self._parse_card(link, source_url)

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
                                "Failed to enrich Confluent job detail page %s: %s",
                                job.url,
                                exc,
                            )

                    if job.job_id:
                        seen_job_ids.add(job.job_id)

                    seen_urls.add(job.url)
                    jobs.append(job)

                current_page += 1

            return jobs

        finally:
            await self.close_browser()

    def _build_page_url(self, source_url: str, page_num: int) -> str:
        """Append or replace the page query parameter."""
        if page_num <= 1:
            return source_url

        parsed = urlparse(source_url)
        query = parse_qs(parsed.query, keep_blank_values=True)

        # Flatten query dict back to list of tuples for urlencode.
        query_pairs = []
        for key, values in query.items():
            for v in values:
                query_pairs.append((key, v))

        # Replace or add the page parameter.
        query_pairs = [(k, v) for k, v in query_pairs if k != 'page']
        query_pairs.append(('page', str(page_num)))

        new_query = urlencode(query_pairs, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    def _parse_card(self, link: Tag, source_url: str) -> Job | None:
        url = self._extract_link(link, source_url)
        title = self._extract_title(link)
        job_id = self._extract_job_id_from_url(url)
        location = self._extract_location(link)

        if not url or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Confluent"),
            title=title,
            location=location,
            url=url,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, link: Tag, source_url: str) -> str:
        href = link.get("href")

        if not href:
            return ""

        href_str = str(href)

        if href_str.startswith("http://") or href_str.startswith("https://"):
            return href_str

        # Relative URL — resolve against careers.confluent.io.
        parsed_source = urlparse(source_url)
        origin = f"{parsed_source.scheme}://{parsed_source.netloc}"

        if href_str.startswith("/"):
            return f"{origin}{href_str}"

        return make_absolute_url(source_url, href_str)

    def _extract_title(self, link: Tag) -> str:
        """Extract title from the first <p> inside the anchor."""
        # The title is in the first <p> inside the <a>.
        title_p = link.select_one('p')

        if title_p:
            return self._clean_text(title_p.get_text())

        # Fallback: get direct text of the anchor (excludes nested spans like "NEW").
        direct_text = " ".join(
            child.get_text(strip=True)
            for child in link.children
            if hasattr(child, 'get_text')
        )
        if direct_text:
            return self._clean_text(direct_text)

        return ""

    def _extract_job_id_from_url(self, url: str) -> str:
        """Extract UUID job ID from the URL path (e.g. /jobs/job/{uuid})."""
        if not url:
            return ""

        match = re.search(r'/jobs/job/([a-f0-9-]+)', url, re.IGNORECASE)
        if match:
            return match.group(1)

        return ""

    def _extract_location(self, link: Tag) -> str:
        """Extract location from the <p> sibling after the anchor."""
        # The location is the <p> element immediately after the <a> tag,
        # with the distinctive text-[#686983] color class.
        parent = link.parent

        if parent:
            # Find all <p> elements in the parent that come after the link.
            location_p = link.find_next_sibling('p')
            if location_p:
                return self._clean_text(location_p.get_text())

            # Fallback: look for any p with the gray color in the parent.
            for p in parent.select('p.text-\\[\\#686983\\]'):
                return self._clean_text(p.get_text())

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

    def _extract_description(self, soup) -> str:
        container = soup.select_one(self.DESCRIPTION_SELECTOR)

        if not container:
            return ""

        # Remove non-description elements.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""
        return " ".join(text.split()).strip()

    def _clean_multiline_text(self, text: str) -> str:
        if not text:
            return ""
        lines = [line.strip() for line in text.split("\n")]
        lines = [line for line in lines if line]
        return "\n".join(lines)
