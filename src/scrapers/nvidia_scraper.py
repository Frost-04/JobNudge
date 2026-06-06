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


class NvidiaScraper(BaseScraper):
    """
    Scraper for NVIDIA Careers search result pages.

    Expected listing card structure:

    div.cardContainer-GcY1a[data-test-id="job-listing"]
      a.r-link.card-F1ebU[href^="/careers/job/"]
        div.title-1aNJK
        div.fieldValue-3kEar  (first = requisition ID, second = location)

    Expected detail structure:

    div.detailContainer-2qNET
      div.detailLabel-2AsIg
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
    FIELD_VALUE_SELECTOR = 'div[class^="fieldValue-"], div[class*=" fieldValue-"]'

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
                            "Failed to enrich NVIDIA job detail page %s: %s",
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
        posted_date = ""  # NVIDIA cards don't show date; enriched from detail page

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "NVIDIA"),
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

        return self._make_nvidia_job_url(source_url, str(href))

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        # Fallback: NVIDIA exposes title in aria-label:
        # aria-label="View job: Verification Engineer - GPU Fullchip"
        link = card.select_one(self.LINK_SELECTOR)

        if link:
            aria_label = link.get("aria-label")

            if aria_label:
                return self._clean_text(str(aria_label).replace("View job:", ""))

        return ""

    def _extract_location(self, card: Tag) -> str:
        """
        NVIDIA cards have multiple fieldValue elements.
        The first is the requisition ID (e.g., JR2019166).
        The second is the location (e.g., India, Bengaluru).

        We skip values that look like requisition IDs (JR followed by digits).
        """
        locations: list[str] = []

        for item in card.select(self.FIELD_VALUE_SELECTOR):
            text = self._clean_location_text(item.get_text())

            if text:
                locations.append(text)

        return ", ".join(self._dedupe_preserve_order(locations))

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        NVIDIA listing href looks like:

        /careers/job/893395449453

        Card anchor id looks like:

        job-card-893395449453-job-list

        Also the first fieldValue contains the requisition ID like JR2019166.
        We prefer the numeric ID from the URL, falling back to the requisition ID.
        """

        if link:
            job_id = self._extract_nvidia_job_id_from_url(link)

            if job_id:
                return job_id

        link_el = card.select_one(self.LINK_SELECTOR)

        if link_el:
            element_id = link_el.get("id")

            if element_id:
                match = re.search(r"job-card-(\d+)-job-list", str(element_id))

                if match:
                    return match.group(1)

        # Fallback: extract requisition ID from field values (e.g., JR2019166)
        for item in card.select(self.FIELD_VALUE_SELECTOR):
            text = self._clean_text(item.get_text())

            if text and re.match(r"^JR\d+$", text):
                return text

        return extract_job_id(link) if link else ""

    async def _get_detail_page(self) -> Page:
        """Return a new page for detail scraping."""
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

        # Remove non-description elements
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    def _format_detail_metadata(self, detail_data: dict[str, str]) -> str:
        """Format NVIDIA detail fields for inclusion in description."""

        if not detail_data:
            return ""

        lines: list[str] = []

        preferred_order = [
            "job requisition id",
            "job category",
            "time type",
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

    def _make_nvidia_job_url(self, source_url: str, href: str) -> str:
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

    def _extract_nvidia_job_id_from_url(self, url: str) -> str:
        if not url:
            return ""

        match = re.search(r"/careers/job/(\d+)", url, flags=re.IGNORECASE)

        if match:
            return match.group(1)

        return extract_job_id(url) or ""

    async def _fallback_links(self, page: Page, source_url: str, max_jobs: int) -> list[Job]:
        """
        Fallback: extract jobs from anchor links when card selectors fail.
        """
        soup = await self._get_soup(page)

        jobs: list[Job] = []
        seen_urls: set[str] = set()

        for link in soup.select(self.LINK_SELECTOR):
            if len(jobs) >= max_jobs:
                break

            href = link.get("href")

            if not href or "/careers/job/" not in str(href):
                continue

            url = self._make_nvidia_job_url(source_url, str(href))

            if url in seen_urls:
                continue

            seen_urls.add(url)

            title = self._clean_text(link.get_text())
            aria_label = link.get("aria-label")

            if not title and aria_label:
                title = self._clean_text(str(aria_label).replace("View job:", ""))

            job_id = self._extract_nvidia_job_id_from_url(url)

            jobs.append(Job(
                job_id=job_id,
                company=self.company_config.get("name", "NVIDIA"),
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

    def _clean_location_text(self, text: str) -> str:
        text = self._clean_text(text)

        if not text:
            return ""

        lower_text = text.lower()

        noise_values = {
            "location",
            "locations",
            "remote",
        }

        if lower_text in noise_values:
            return ""

        # Skip requisition IDs like JR2019166
        if re.match(r"^JR\d+$", text):
            return ""

        return text

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
