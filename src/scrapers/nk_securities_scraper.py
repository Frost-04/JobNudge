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


class NKSecuritiesScraper(BaseScraper):
    """
    Scraper for NK Securities Research job board (powered by Greenhouse).

    Standard Greenhouse board at job-boards.eu.greenhouse.io/nksecuritiesresearch.
    The listing uses embed-style CSS classes (body.body--medium /
    body__secondary.body--metadata) on the standard board URL.

    Expected listing structure:

    div.job-posts
      div.job-posts--table--department
        h3.section-header                  (department name)
        div.job-posts--table
          table > tbody
            tr.job-post
              td.cell > a[href]
                p.body.body--medium        (job title)
                p.body__secondary.body--metadata   (location)

    Expected detail page structure:

    div.job__description.body              (full rich-text description)

    Job IDs come from the numeric path in the detail URL
    (e.g. /nksecuritiesresearch/jobs/4567094101 → 4567094101).
    """

    CARD_SELECTOR = 'tr.job-post'

    JOB_CARD_SELECTORS = [
        'tr.job-post',
        'div.job-posts',
        'div.job-posts--table',
    ]

    TITLE_SELECTOR = 'p.body.body--medium'
    LOCATION_SELECTOR = 'p.body__secondary.body--metadata'
    LINK_SELECTOR = 'td.cell > a[href*="/jobs/"]'

    DESCRIPTION_SELECTOR = 'div.job__description.body'

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
                            "Failed to enrich NK Securities job detail page %s: %s",
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
            company=self.company_config.get("name", "NK Securities Research"),
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
        el = card.select_one(self.LINK_SELECTOR)

        if not el:
            return ""

        href = el.get("href")

        if not href:
            return ""

        href_str = str(href)

        # Links are already full URLs in the standard board.
        if href_str.startswith("http://") or href_str.startswith("https://"):
            return href_str

        return self._make_job_url(source_url, href_str)

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        # Fallback: get text from the anchor, excluding location <p>
        link = card.select_one(self.LINK_SELECTOR)

        if link:
            title_el = link.select_one(self.TITLE_SELECTOR)

            if title_el:
                return self._clean_text(title_el.get_text())

        return ""

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        Extract job ID from the URL path.
        Standard Greenhouse URLs: /nksecuritiesresearch/jobs/4567094101
        """
        if link:
            match = re.search(r'/jobs/(\d+)', link)
            if match:
                return match.group(1)

            # Fallback: extract any numeric ID from the URL.
            result = extract_job_id(link)
            if result:
                return result

        # Fallback: parse from the card's anchor href.
        link_el = card.select_one(self.LINK_SELECTOR)

        if link_el:
            href = str(link_el.get("href", ""))
            match = re.search(r'/jobs/(\d+)', href)
            if match:
                return match.group(1)

        return ""

    def _extract_location(self, card: Tag) -> str:
        el = card.select_one(self.LOCATION_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

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
                    'div.job__description',
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

        # Remove non-description elements.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

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
        seen_ids: set[str] = set()

        for link in soup.select('td.cell > a[href*="/jobs/"]'):
            if len(jobs) >= max_jobs:
                break

            href = link.get("href")

            if not href:
                continue

            href_str = str(href)
            job_url = self._make_job_url(source_url, href_str)

            if job_url in seen_urls:
                continue

            seen_urls.add(job_url)

            # Try to parse title from the link.
            title_parts = []
            for child in link.children:
                if isinstance(child, Tag):
                    text = self._clean_text(child.get_text())
                    if text:
                        title_parts.append(text)

            title = title_parts[0] if title_parts else ""
            location = title_parts[1] if len(title_parts) > 1 else ""

            job_id = ""
            match = re.search(r'/jobs/(\d+)', href_str)
            if match:
                job_id = match.group(1)
            else:
                result = extract_job_id(href_str)
                if result:
                    job_id = result

            if job_id and job_id in seen_ids:
                continue

            if job_id:
                seen_ids.add(job_id)

            if not title:
                continue

            if self._should_exclude(title):
                self.logger.debug("Skipping detail enrichment for: %s", title)
            else:
                try:
                    detail_data = await self._scrape_detail_page(job_url)

                    detail_description = detail_data.get("description", "")

                    if detail_description:
                        job = Job(
                            job_id=job_id,
                            company=self.company_config.get("name", "NK Securities Research"),
                            title=title,
                            location=location,
                            url=job_url,
                            source_url=source_url,
                            posted_date=None,
                            description=detail_description,
                            scraped_at=datetime.now(timezone.utc).isoformat(),
                            extracted_experience_parts="",
                        )
                        jobs.append(job)
                        continue

                except Exception as exc:
                    self.logger.warning(
                        "Failed to enrich NK Securities fallback detail page %s: %s",
                        job_url,
                        exc,
                    )

            jobs.append(
                Job(
                    job_id=job_id,
                    company=self.company_config.get("name", "NK Securities Research"),
                    title=title,
                    location=location,
                    url=job_url,
                    source_url=source_url,
                    posted_date=None,
                    description=None,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    extracted_experience_parts="",
                )
            )

        return jobs

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
