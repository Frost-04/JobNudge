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


class MeeshoScraper(BaseScraper):
    """
    Scraper for Meesho careers page (Tailwind + server-rendered grid cards).

    Expected listing structure:

    section.py-5.bg-white > div.open-positions
      div.job
        a[href^="/jobs/"]
          div.grid.grid-cols-12
            div.col-span-5                       (title)
            div.col-span-4                       (team / department)
            div.col-span-3 > div.flex > span     (last span = location)

    Expected detail page structure:

    section.job-description                      (full description)

    Job IDs:
    Meesho uses UUIDs in the query parameter: ?id=040a5a75-28d2-4b56-...
    The scraper extracts the UUID as the job ID.
    """

    CARD_SELECTOR = 'div.job'

    JOB_CARD_SELECTORS = [
        'div.job',
        'div.open-positions',
        'a[href^="/jobs/"]',
    ]

    TITLE_SELECTOR = 'div.col-span-5'
    TEAM_SELECTOR = 'div.col-span-4'
    LOCATION_SELECTOR = 'div.col-span-3 span:last-child'
    LINK_SELECTOR = 'a[href^="/jobs/"]'

    DESCRIPTION_SELECTOR = 'section.job-description'

    BASE_URL = 'https://www.meesho.io'

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
                            "Failed to enrich Meesho job detail page %s: %s",
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
        link = self._extract_link(card)
        title = self._extract_title(card)
        job_id = self._extract_job_id(link)
        location = self._extract_location(card)
        team = self._extract_team(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Meesho"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=team or None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag) -> str:
        el = card.select_one(self.LINK_SELECTOR)

        if not el:
            return ""

        href = el.get("href")

        if not href:
            return ""

        href = str(href).strip()

        if href.startswith("http"):
            return href

        if href.startswith("/"):
            return f"{self.BASE_URL}{href}"

        return f"{self.BASE_URL}/{href}"

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

    def _extract_team(self, card: Tag) -> str:
        el = card.select_one(self.TEAM_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        return ""

    def _extract_job_id(self, link: str) -> str:
        """
        Meesho URLs: /jobs/architect---android?id=040a5a75-28d2-4b56-904b-071c41d4ea90

        Extract the UUID from the ?id= query parameter.
        """
        if not link:
            return ""

        match = re.search(r"[?&]id=([a-f0-9-]+)", link, flags=re.IGNORECASE)

        if match:
            return match.group(1)

        return extract_job_id(link) or ""

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
                    'section.job-description',
                    'h3',
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

        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    def _format_detail_metadata(self, detail_data: dict[str, str]) -> str:
        return ""

    def _join_description_parts(self, *parts: str) -> str:
        cleaned_parts = [part.strip() for part in parts if part and part.strip()]
        return "\n\n".join(cleaned_parts)

    async def _fallback_links(self, page: Page, source_url: str, max_jobs: int) -> list[Job]:
        soup = await self._get_soup(page)

        jobs: list[Job] = []
        seen_urls: set[str] = set()

        for link in soup.select('a[href^="/jobs/"]'):
            if len(jobs) >= max_jobs:
                break

            href = link.get("href")

            if not href:
                continue

            href = str(href).strip()

            if href.startswith("/"):
                url = f"{self.BASE_URL}{href}"
            elif href.startswith("http"):
                url = href
            else:
                url = f"{self.BASE_URL}/{href}"

            if url in seen_urls:
                continue

            seen_urls.add(url)

            title = self._clean_text(link.get_text())
            job_id = self._extract_job_id(url)

            jobs.append(Job(
                job_id=job_id,
                company=self.company_config.get("name", "Meesho"),
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

        lines = []
        for line in text.splitlines():
            clean_line = self._clean_text(line)

            if clean_line:
                lines.append(clean_line)

        return "\n".join(lines).strip()

    def _dedupe_preserve_order(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []

        for value in values:
            normalized = value.lower().strip()

            if not normalized or normalized in seen:
                continue

            seen.add(normalized)
            result.append(value)

        return result
