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


class ByteDanceScraper(BaseScraper):
    """
    Scraper for ByteDance job board at joinbytedance.com (Next.js SPA).

    The search URL is pre-filtered for India (location_code_list=CT_44).
    The job section takes time to load and requires scrolling down to trigger
    rendering. Both the listing and detail pages are slow-loading SPAs.

    Card structure:

        a[href*="/search/{numeric_id}"]
          div.flex.flex-col
            span.bd-title                (job title)
            div.flex.items-start
              span.tt-text               (department)
              span.tt-text               (location)
              span.tt-text               (employment type)

    Detail page structure:

        div.px-[100px]
          div.flex.flex-col.gap-[52px]
            p.bd-title                   (section heading: "Responsibilities", etc.)
            p.whitespace-pre-line        (section content)
    """

    CARD_SELECTOR = 'a[href*="/search/"][href*="joinbytedance.com"]'

    JOB_CARD_SELECTORS = [
        'a[href*="/search/"]',
        'span.bd-title',
        'div.flex.flex-col',
    ]

    TITLE_SELECTOR = 'span.bd-title'

    # ---- Detail page selectors ----
    WAIT_DETAIL_SELECTORS = [
        'p.whitespace-pre-line',
        'p.bd-title',
        'button[data-testid="button"]',
    ]

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            # SPA with slow-loading content — use networkidle.
            await page.goto(source_url, wait_until="networkidle", timeout=90000)
            # Additional settle time for dynamic content.
            await page.wait_for_timeout(5000)

            # Scroll down to trigger job section rendering.
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(3000)

            # Scroll further if needed.
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
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

                # Enrich with detail page.
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
                            "Failed to enrich ByteDance job detail page %s: %s",
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
            90000,
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
            company=self.company_config.get("name", "ByteDance"),
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
        href = card.get("href")

        if not href:
            return ""

        return str(href).strip()

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        return ""

    def _extract_location(self, card: Tag) -> str:
        """
        Location is in the third span.tt-text within the card.

        The card has:
          span.tt-text   (department)
          span.tt-text   (location - e.g. "Gurgaon")
          span.tt-text   (employment type)
        """
        tt_spans = card.select("span.tt-text")

        if len(tt_spans) >= 2:
            return self._clean_text(tt_spans[1].get_text())

        return ""

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        ByteDance job URLs look like:

        https://joinbytedance.com/search/7309000517027596594

        Extract the numeric ID from the URL.
        """
        if not link:
            return ""

        match = re.search(r"/search/(\d+)", link)

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
            # Detail page is also a slow SPA — networkidle needed.
            await detail_page.goto(job_url, wait_until="networkidle", timeout=90000)
            await detail_page.wait_for_timeout(3000)

            try:
                await detail_page.wait_for_selector(
                    self.WAIT_DETAIL_SELECTORS[0],
                    timeout=30000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            detail_data: dict[str, str] = {}
            description = self._extract_description(soup)

            if description:
                detail_data["description"] = description

            return detail_data

        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        """
        Extract description from all section content.

        Sections have:
          p.bd-title (section heading like "Responsibilities", "Qualifications")
          p.whitespace-pre-line (section content)
        """
        parts: list[str] = []

        # Find all section blocks: div.flex.flex-col.gap-[52px] with pt-[80px] parent.
        # Can't use bracket class selectors in BS4 either, so find by structure.
        for section in soup.select("div.flex.flex-col"):
            heading = section.select_one("p.bd-title")

            if not heading:
                continue

            heading_text = self._clean_text(heading.get_text())
            content_parts: list[str] = [heading_text]

            for content_p in section.select("p.whitespace-pre-line"):
                text = self._clean_multiline_text(content_p.get_text())

                if text:
                    content_parts.append(text)

            if len(content_parts) > 1:
                parts.append("\n".join(content_parts))

        if not parts:
            # Fallback: grab text from all whitespace-pre-line elements.
            for p in soup.select("p.whitespace-pre-line"):
                text = self._clean_multiline_text(p.get_text())

                if text:
                    parts.append(text)

        return "\n\n".join(parts)

    async def _fallback_links(self, page: Page, source_url: str, max_jobs: int) -> list[Job]:
        soup = await self._get_soup(page)

        jobs: list[Job] = []
        seen_urls: set[str] = set()

        for link in soup.select('a[href*="/search/"][href*="joinbytedance.com"]'):
            if len(jobs) >= max_jobs:
                break

            href = link.get("href")

            if not href or "/search/" not in str(href):
                continue

            url = str(href).strip()

            if url in seen_urls:
                continue

            seen_urls.add(url)

            title = ""
            title_el = link.select_one("span.bd-title")

            if title_el:
                title = self._clean_text(title_el.get_text())

            if not title:
                title = self._clean_text(link.get_text())

            location = ""
            tt_spans = link.select("span.tt-text")

            if len(tt_spans) >= 2:
                location = self._clean_text(tt_spans[1].get_text())

            job_id = self._extract_job_id(link, url)

            jobs.append(Job(
                job_id=job_id,
                company=self.company_config.get("name", "ByteDance"),
                title=title,
                location=location,
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
            stripped = line.strip()
            if stripped:
                lines.append(stripped)

        return "\n".join(lines)
