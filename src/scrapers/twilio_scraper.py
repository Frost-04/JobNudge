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


class TwilioScraper(BaseScraper):
    """
    Scraper for Twilio Careers search result pages (Phenom People platform).

    Expected listing card structure:

    div[data-test-id="job-listing"]
      a[href^="/careers/job/"]
        div.title-1aNJK              (CSS-module hashed)
        div.fieldValue-3kEar         (first = department, second = location)
        div.subData-13Lm1            (posted date)

    Expected detail structure:

    div.detailContainer-2qNET
      div.detailLabel-2AsIg   -> Date posted / Department / Location
      div.detailValue-3NGwm

    div#job-description-container
    """

    JOB_CARD_SELECTORS = [
        'div[data-test-id="job-listing"]',
        'a[href*="/careers/job/"]',
    ]

    CARD_SELECTOR = 'div[data-test-id="job-listing"]'
    LINK_SELECTOR = 'a[href*="/careers/job/"]'
    TITLE_SELECTOR = 'div[class^="title-"], div[class*=" title-"]'
    DEPARTMENT_SELECTOR = 'div[class^="fieldValue-"], div[class*=" fieldValue-"]'
    LOCATION_SELECTOR = 'div[class^="fieldValue-"], div[class*=" fieldValue-"]'
    POSTED_SELECTOR = 'div[class^="subData-"], div[class*=" subData-"]'

    DETAIL_CONTAINER_SELECTOR = 'div[class^="detailContainer-"], div[class*=" detailContainer-"]'
    DETAIL_LABEL_SELECTOR = 'div[class^="detailLabel-"], div[class*=" detailLabel-"]'
    DETAIL_VALUE_SELECTOR = 'div[class^="detailValue-"], div[class*=" detailValue-"]'
    DESCRIPTION_SELECTOR = '#job-description-container'

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

                # Enrich each card by opening its job details page.
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
                else:
                    try:
                        detail_data = await self._scrape_detail_page(job.url)

                        detail_posted_date = detail_data.get("date posted", "")
                        detail_description = detail_data.get("description", "")

                        metadata_description = self._format_detail_metadata(detail_data)

                        combined_description = self._join_description_parts(
                            metadata_description,
                            detail_description,
                        )

                        job = Job(
                            job_id=job.job_id,
                            company=job.company,
                            title=job.title,
                            location=job.location,
                            url=job.url,
                            source_url=job.source_url,
                            posted_date=detail_posted_date or job.posted_date,
                            description=combined_description or job.description,
                            scraped_at=datetime.now(timezone.utc).isoformat(),
                            extracted_experience_parts="",
                        )

                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich Twilio job detail page %s: %s",
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
        posted_date = self._extract_posted_date(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Twilio"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=posted_date or None,
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

        return self._make_twilio_job_url(source_url, str(href))

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        # Fallback: Twilio also exposes title in aria-label:
        # aria-label="View job: Software Architect (L6)"
        link = card.select_one(self.LINK_SELECTOR)

        if link:
            aria_label = link.get("aria-label")

            if aria_label:
                return self._clean_text(str(aria_label).replace("View job:", ""))

        return ""

    def _extract_location(self, card: Tag) -> str:
        """
        Twilio cards have two fieldValue divs:
        - First: department (e.g. "Engineering", "Data Science")
        - Second: location (e.g. "Remote - India")

        We take the second fieldValue as the location.
        """
        field_values = card.select(self.LOCATION_SELECTOR)

        if len(field_values) >= 2:
            text = self._clean_text(field_values[1].get_text())

            if text:
                return text

        return ""

    def _extract_posted_date(self, card: Tag) -> str:
        el = card.select_one(self.POSTED_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        Twilio listing href looks like: /careers/job/1099553538803
        Card id can also look like: job-card-1099553538803-job-list
        """

        if link:
            job_id = self._extract_twilio_job_id_from_url(link)

            if job_id:
                return job_id

        link_el = card.select_one(self.LINK_SELECTOR)

        if link_el:
            element_id = link_el.get("id")

            if element_id:
                match = re.search(r"job-card-(\d+)-job-list", str(element_id))

                if match:
                    return match.group(1)

        return extract_job_id(link) if link else ""

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
                    self.DETAIL_CONTAINER_SELECTOR,
                    'div[data-test-id="job-listing"]',
                ],
            )

            soup = await self._get_soup(detail_page)

            detail_data = self._extract_detail_metadata(soup)
            description = self._extract_description(soup)

            if description:
                detail_data["description"] = description

            return detail_data

        finally:
            await detail_page.close()

    def _extract_detail_metadata(self, soup) -> dict[str, str]:
        detail_data: dict[str, str] = {}

        for container in soup.select(self.DETAIL_CONTAINER_SELECTOR):
            label_el = container.select_one(self.DETAIL_LABEL_SELECTOR)
            value_el = container.select_one(self.DETAIL_VALUE_SELECTOR)

            label = self._clean_text(label_el.get_text() if label_el else "")
            value = self._clean_text(value_el.get_text() if value_el else "")

            if not label or not value:
                continue

            detail_data[label.lower()] = value

        return detail_data

    def _extract_description(self, soup) -> str:
        container = soup.select_one(self.DESCRIPTION_SELECTOR)

        if not container:
            return ""

        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    def _format_detail_metadata(self, detail_data: dict[str, str]) -> str:
        if not detail_data:
            return ""

        lines: list[str] = []

        preferred_order = [
            "date posted",
            "department",
            "location",
        ]

        for key in preferred_order:
            value = detail_data.get(key)

            if value:
                label = key.title()
                lines.append(f"{label}: {value}")

        return "\n".join(lines)

    def _join_description_parts(self, *parts: str) -> str:
        cleaned_parts = [part.strip() for part in parts if part and part.strip()]
        return "\n\n".join(cleaned_parts)

    def _make_twilio_job_url(self, source_url: str, href: str) -> str:
        href = html.unescape(href).strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        parsed_source = urlparse(source_url)
        origin = f"{parsed_source.scheme}://{parsed_source.netloc}"

        if href.startswith("/careers/job/"):
            return f"{origin}{href}"

        if href.startswith("careers/job/"):
            return f"{origin}/{href}"

        return make_absolute_url(source_url, href)

    def _extract_twilio_job_id_from_url(self, url: str) -> str:
        if not url:
            return ""

        match = re.search(r"/careers/job/(\d+)", url, flags=re.IGNORECASE)

        if match:
            return match.group(1)

        return extract_job_id(url) or ""

    def _clean_location_text(self, text: str) -> str:
        text = self._clean_text(text)

        if not text:
            return ""

        if text.lower() in ("location", "locations", "remote"):
            return ""

        return text

    async def _fallback_links(self, page: Page, source_url: str, max_jobs: int) -> list[Job]:
        soup = await self._get_soup(page)

        jobs: list[Job] = []
        seen_urls: set[str] = set()

        for link in soup.select(self.LINK_SELECTOR):
            if len(jobs) >= max_jobs:
                break

            href = link.get("href")

            if not href or "/careers/job/" not in str(href):
                continue

            url = self._make_twilio_job_url(source_url, str(href))

            if url in seen_urls:
                continue

            seen_urls.add(url)

            title = self._clean_text(link.get_text())
            job_id = self._extract_twilio_job_id_from_url(url)

            jobs.append(Job(
                job_id=job_id,
                company=self.company_config.get("name", "Twilio"),
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
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        if not text:
            return ""
        text = html.unescape(text)
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
