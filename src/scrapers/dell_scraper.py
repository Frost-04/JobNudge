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


class DellScraper(BaseScraper):
    """
    Scraper for Dell Careers Oracle Cloud Candidate Experience pages.

    Expected search card structure:

    ul#panel-list.jobs-list__list
      li[data-qa="searchResultItem"]
        a.job-list-item__link[href*="/job/"]
        span.job-tile__title
        posting-locations > span[data-bind="html: primaryLocation"]
        .job-list-item__job-info-value (posting date)

    Expected detail page / overlay structure:

    div.job-details__description-content
    """

    JOB_CARD_SELECTORS = [
        "ul#panel-list li[data-qa='searchResultItem']",
        "li[data-qa='searchResultItem']",
        "a[href*='/job/']",
    ]

    CARD_SELECTOR = "ul#panel-list li[data-qa='searchResultItem'], li[data-qa='searchResultItem']"
    LINK_SELECTOR = "a.job-list-item__link[href*='/job/'], a[href*='/job/']"
    TITLE_SELECTOR = "span.job-tile__title"
    LOCATION_SELECTOR = "posting-locations span[data-bind*='primaryLocation']"
    POSTED_DATE_SELECTOR = "li.job-list-item__job-info-item .job-list-item__job-info-value"
    JOB_INFO_ITEM_SELECTOR = "li.job-list-item__job-info-item"
    JOB_INFO_VALUE_SELECTOR = "div.job-list-item__job-info-value"

    DETAIL_DESCRIPTION_SELECTOR = "div.job-details__description-content"

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

                # Enrich card data by opening the direct job URL.
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
                else:
                    try:
                        detail_data = await self._scrape_detail_page(job.url)

                        detail_description = detail_data.get("description", "")

                        job = Job(
                            job_id=job.job_id,
                            company=job.company,
                            title=job.title,
                            location=job.location,
                            url=job.url,
                            source_url=job.source_url,
                            posted_date=job.posted_date,
                            description=detail_description or job.description,
                            scraped_at=datetime.now(timezone.utc).isoformat(),
                            extracted_experience_parts="",
                        )

                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich Dell job detail page %s: %s",
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
            company=self.company_config.get("name", "Dell"),
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

        return self._make_dell_job_url(source_url, str(href))

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        Dell Oracle Cloud URLs look like:

        https://iawmqy.fa.ocs.oraclecloud.com/.../job/R282624/...

        The R-prefixed ID is in the URL path.
        """

        if link:
            job_id = self._extract_dell_job_id_from_url(link)

            if job_id:
                return job_id

        labelled_link = card.select_one("[aria-labelledby]")

        if labelled_link:
            aria_labelledby = labelled_link.get("aria-labelledby")

            if aria_labelledby:
                return str(aria_labelledby)

        header = card.select_one("search-result-item-header[id]")

        if header:
            header_id = header.get("id")

            if header_id:
                return str(header_id)

        return extract_job_id(link) if link else ""

    def _extract_location(self, card: Tag) -> str:
        """
        Location is in the <posting-locations> custom element:
        <span data-bind="html: primaryLocation">Bengaluru, Karnataka, India</span>
        """
        el = card.select_one(self.LOCATION_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        return ""

    def _extract_posted_date(self, card: Tag) -> str:
        """
        Dell cards have job info items like:
        <li class="job-list-item__job-info-item">
            <div class="job-list-item__job-info-label ...">Posting Date</div>
            <div class="job-list-item__job-info-value">04/06/2026</div>
        </li>

        Only the second .job-list-item__job-info-value is the posting date
        (first is location). We look for the one whose parent li has the
        posting-date label.
        """
        # Try to find the date via the label text first
        for item in card.select(self.JOB_INFO_ITEM_SELECTOR):
            label = item.select_one(".job-list-item__job-info-label")
            if label and "date" in label.get_text().lower():
                value_el = item.select_one(self.JOB_INFO_VALUE_SELECTOR)
                if value_el:
                    return self._clean_text(value_el.get_text())

        # Fallback: all job-info-values, return the one that looks like a date
        values = card.select(self.JOB_INFO_VALUE_SELECTOR)
        for val in values:
            text = self._clean_text(val.get_text())
            if re.match(r"\d{2}/\d{2}/\d{4}", text):
                return text

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

            # Wait for the description container to load.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_DESCRIPTION_SELECTOR,
                    timeout=15000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            detail_data: dict[str, str] = {}

            desc_container = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)

            if desc_container:
                detail_data["description"] = self._extract_description(desc_container)

            return detail_data

        finally:
            await detail_page.close()

    def _extract_description(self, container: Tag) -> str:
        """Extract clean description text from the Oracle Cloud description container."""
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    def _format_detail_metadata(self, detail_data: dict[str, str]) -> str:
        if not detail_data:
            return ""
        return ""

    def _join_description_parts(self, *parts: str) -> str:
        cleaned_parts = [part.strip() for part in parts if part and part.strip()]
        return "\n\n".join(cleaned_parts)

    def _make_dell_job_url(self, source_url: str, href: str) -> str:
        href = html.unescape(href).strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        parsed_source = urlparse(source_url)
        origin = f"{parsed_source.scheme}://{parsed_source.netloc}"

        if href.startswith("/"):
            return f"{origin}{href}"

        return make_absolute_url(source_url, href)

    def _extract_dell_job_id_from_url(self, url: str) -> str:
        """
        Dell Oracle Cloud URLs: .../job/R282624/?...
        """
        if not url:
            return ""

        match = re.search(r"/job/([^/?#]+)", url, flags=re.IGNORECASE)

        if match:
            return match.group(1)

        return extract_job_id(url) or ""

    async def _fallback_links(self, page: Page, source_url: str, max_jobs: int) -> list[Job]:
        soup = await self._get_soup(page)

        jobs: list[Job] = []
        seen_urls: set[str] = set()

        for link in soup.select(self.LINK_SELECTOR):
            if len(jobs) >= max_jobs:
                break

            href = link.get("href")

            if not href or "/job/" not in str(href):
                continue

            url = self._make_dell_job_url(source_url, str(href))

            if url in seen_urls:
                continue

            seen_urls.add(url)

            title = self._clean_text(link.get_text())
            job_id = self._extract_dell_job_id_from_url(url)

            jobs.append(Job(
                job_id=job_id,
                company=self.company_config.get("name", "Dell"),
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
