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


class OktaScraper(BaseScraper):
    """
    Scraper for Okta job board (www.okta.com/company/careers/job-listing/).

    Custom Drupal-based job board with server-rendered listing cards and
    separate detail pages for each job.

    Expected listing structure:

    div.CareersView__content
      h3 (department heading, e.g. "Engineering")
      div.views-row (even/odd)
        div.views-field-title
          span.field-content
            a[href]                          (title + detail URL)
        div.views-field-field-job-location
          div.field-content                  (location, e.g. "Bengaluru, India")

    Expected detail page structure:

    article.Job__content[role="article"]
      p, ul, etc.                            (full job description)

    Job IDs come from the numeric suffix in the URL path (e.g. "7919336").
    Detail pages are at relative URLs like /company/careers/engineering/{slug}-{id}/.
    """

    CARD_SELECTOR = 'div.views-row'

    JOB_CARD_SELECTORS = [
        'div.views-row',
        'div.CareersView__content',
    ]

    TITLE_SELECTOR = 'div.views-field-title span.field-content a'
    LOCATION_SELECTOR = 'div.views-field-field-job-location div.field-content'

    DESCRIPTION_SELECTOR = 'article.Job__content'

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            # Scroll to trigger any lazy-loaded content.
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
                            "Failed to enrich Okta job detail page %s: %s",
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
            company=self.company_config.get("name", "Okta"),
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

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        Extract job ID from the URL path.
        Okta URLs look like: /company/careers/engineering/{slug}-{id}/
        The job ID is the numeric suffix after the last hyphen, before the trailing slash.
        Examples:
          /company/careers/engineering/director-of-engineering-developer-integrations-auth0-7919336/
          /company/careers/engineering/engineering-architect-dxaxux-7783603/
        """
        if link:
            # Match the last numeric segment before an optional trailing slash.
            match = re.search(r'-(\d+)/?$', link)
            if match:
                return match.group(1)

        # Fallback: try to find any numeric ID in the card's title link href.
        title_el = card.select_one(self.TITLE_SELECTOR)
        if title_el:
            href = str(title_el.get("href", ""))
            match = re.search(r'-(\d+)/?$', href)
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
                    'article',
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

        for link in soup.select('div.views-field-title span.field-content a[href*="/company/careers/"]'):
            if len(jobs) >= max_jobs:
                break

            href = link.get("href")

            if not href:
                continue

            href_str = str(href)

            if "/job-listing/" in href_str:
                continue

            job_url = self._make_job_url(source_url, href_str)

            if job_url in seen_urls:
                continue

            seen_urls.add(job_url)

            title = self._clean_text(link.get_text())
            job_id_match = re.search(r'-(\d+)/?$', href_str)
            job_id = job_id_match.group(1) if job_id_match else ""

            if not title:
                continue

            if job_id and job_id in seen_ids:
                continue

            if job_id:
                seen_ids.add(job_id)

            job = Job(
                job_id=job_id,
                company=self.company_config.get("name", "Okta"),
                title=title,
                location="",
                url=job_url,
                source_url=source_url,
                posted_date=None,
                description=None,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                extracted_experience_parts="",
            )

            # Enrich if not excluded.
            if not self._should_exclude(title):
                try:
                    detail_data = await self._scrape_detail_page(job_url)
                    detail_desc = detail_data.get("description", "")
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
                        "Failed to enrich Okta fallback detail page %s: %s",
                        job_url,
                        exc,
                    )

            jobs.append(job)

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
