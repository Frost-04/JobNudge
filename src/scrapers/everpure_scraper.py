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


class EverpureScraper(BaseScraper):
    """
    Scraper for EverPure (Pure Storage) Careers opportunities page.

    Expected listing card structure (server-rendered <li> cards):

        li.list-item[data-department="engineering"][data-location="..."]
          div.container
            div.title
              span.job-requisition-id  ("ID: 17702")
              a[href^="https://job-boards.greenhouse.io/purestorage/jobs/"]
            div.dept
              span.department-title   ("Engineering")
            div.job
              span.location-title     ("Bangalore, India")

    Expected detail page structure (on greenhouse.io):

        div.job__description.body    (full job description)
    """

    # ---- Card selectors ----
    CARD_SELECTOR = "li.list-item"

    # "Member of Technical Staff" is Pure Storage's standard IC title —
    # it is NOT a senior/staff-level role. Remove "staff" from the
    # exclusion list so these jobs get full description enrichment.
    EXCLUDE_TITLE_WORDS: list[str] = [
        "principal",
        "senior",
        "manager",
        "iii",
        "sr.",
        "sr",
        "lead",
    ]

    JOB_CARD_SELECTORS = [
        "li.list-item",
        "li[data-department]",
        "div.title a[href*='greenhouse.io/purestorage/jobs/']",
    ]

    TITLE_LINK_SELECTOR = "div.title a"
    TITLE_TEXT_SELECTOR = "div.title a"
    LOCATION_SELECTOR = "span.location-title"
    DEPARTMENT_SELECTOR = "span.department-title"
    JOB_ID_SELECTOR = "span.job-requisition-id"

    # ---- Detail page selectors ----
    DESCRIPTION_SELECTOR = "div.job__description.body, #content"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Give the JS filter time to apply (hash-based filtering).
            await page.wait_for_timeout(3000)

            selector = await self._wait_for_any_selector(page, self.JOB_CARD_SELECTORS)

            if not selector:
                self.logger.warning(
                    "EverPure: no card selectors matched, trying fallback."
                )
                return await self._fallback_links(page, source_url, max_jobs)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)
            self.logger.info("EverPure: found %d cards with selector '%s'", len(cards), self.CARD_SELECTOR)

            if not cards:
                # Try a broader selector as fallback.
                cards = soup.select("li[data-department]")
                self.logger.info("EverPure: fallback found %d cards with 'li[data-department]'", len(cards))

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

                # Enrich by opening the greenhouse.io detail page.
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
                                posted_date=detail_data.get("date posted") or job.posted_date,
                                description=detail_description,
                                scraped_at=datetime.now(timezone.utc).isoformat(),
                                extracted_experience_parts="",
                            )

                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich EverPure job detail page %s: %s",
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

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card)
        title = self._extract_title(card)
        job_id = self._extract_job_id_from_card(card)
        location = self._extract_location(card)
        department = self._extract_department(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "EverPure"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=department or None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag) -> str:
        """Extract absolute job URL from the card's anchor tag (greenhouse.io)."""
        anchor = card.select_one(self.TITLE_LINK_SELECTOR)

        if not anchor:
            return ""

        href = anchor.get("href")

        if not href:
            return ""

        href = html.unescape(str(href)).strip()

        # Greenhouse links are already absolute.
        return href

    def _extract_title(self, card: Tag) -> str:
        """Extract job title from the anchor text."""
        el = card.select_one(self.TITLE_TEXT_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        # Fallback: any anchor in the card's title div.
        for link in card.select("div.title a"):
            text = self._clean_text(link.get_text())

            if text:
                return text

        return ""

    def _extract_location(self, card: Tag) -> str:
        """Extract location from span.location-title."""
        el = card.select_one(self.LOCATION_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        return ""

    def _extract_department(self, card: Tag) -> str:
        """Extract department from span.department-title."""
        el = card.select_one(self.DEPARTMENT_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        return ""

    def _extract_job_id_from_card(self, card: Tag) -> str:
        """Extract job ID from span.job-requisition-id, e.g. 'ID: 17702'."""
        el = card.select_one(self.JOB_ID_SELECTOR)

        if el:
            raw = el.get_text(strip=True)
            # "ID: 17702" → "17702"
            match = re.search(r"(\d+)", raw)

            if match:
                return match.group(1)

        return ""

    # ------------------------------------------------------------------
    # Detail page enrichment (greenhouse.io)
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

    async def _scrape_detail_page(self, job_url: str) -> dict[str, str]:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)

            await self._wait_for_any_selector(
                detail_page,
                [
                    self.DESCRIPTION_SELECTOR,
                    "#content",
                    "div.job__description",
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

    # ------------------------------------------------------------------
    # Fallback
    # ------------------------------------------------------------------

    async def _fallback_links(self, page: Page, source_url: str, max_jobs: int) -> list[Job]:
        """Fallback: extract jobs from greenhouse links when card selectors fail."""
        soup = await self._get_soup(page)

        jobs: list[Job] = []
        seen_urls: set[str] = set()
        seen_job_ids: set[str] = set()

        for link in soup.select("a[href*='greenhouse.io/purestorage/jobs/']"):
            if len(jobs) >= max_jobs:
                break

            href = html.unescape(str(link.get("href", ""))).strip()

            if not href or href in seen_urls:
                continue

            title = self._clean_text(link.get_text())

            if not title:
                continue

            # Try to extract job ID from the greenhouse URL.
            job_id = ""
            match = re.search(r"/jobs/(\d+)", href)

            if match:
                job_id = match.group(1)

            if job_id and job_id in seen_job_ids:
                continue

            job = Job(
                job_id=job_id,
                company=self.company_config.get("name", "EverPure"),
                title=title,
                location="",
                url=href,
                source_url=source_url,
                posted_date=None,
                description=None,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                extracted_experience_parts="",
            )

            # Enrich from detail page.
            if not self._should_exclude(title):
                try:
                    detail_data = await self._scrape_detail_page(href)

                    detail_description = detail_data.get("description", "")

                    if detail_description:
                        job = Job(
                            job_id=job.job_id,
                            company=job.company,
                            title=job.title,
                            location=job.location,
                            url=job.url,
                            source_url=job.source_url,
                            posted_date=detail_data.get("date posted") or job.posted_date,
                            description=detail_description,
                            scraped_at=datetime.now(timezone.utc).isoformat(),
                            extracted_experience_parts="",
                        )
                except Exception as exc:
                    self.logger.warning(
                        "Failed to enrich EverPure fallback detail page %s: %s", href, exc
                    )

            if job_id:
                seen_job_ids.add(job_id)

            seen_urls.add(href)
            jobs.append(job)

        return jobs

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

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

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _clean_multiline_text(self, text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        lines = [line.strip() for line in text.splitlines()]
        lines = [line for line in lines if line]
        return "\n".join(lines)

    def _dedupe_preserve_order(self, items: list[Job]) -> list[Job]:
        seen: set[str] = set()
        result: list[Job] = []

        for job in items:
            key = job.url or job.job_id

            if key and key in seen:
                continue

            if key:
                seen.add(key)

            result.append(job)

        return result
