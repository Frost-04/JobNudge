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


class KotakScraper(BaseScraper):
    """
    Scraper for Kotak Mahindra Bank Oracle Cloud Candidate Experience pages.

    Kotak uses the same Oracle Cloud platform as JPMorgan Chase, with cards
    in ``ul#panel-list > li[data-qa="searchResultItem"]`` and detail pages
    navigated via direct job URLs.

    Expected search card structure:

    ul#panel-list
      li[data-qa="searchResultItem"]
        a.job-list-item__link[href*="/job/{id}/"]
        span.job-tile__title
        posting-locations > span[data-bind*="primaryLocation"]
        p.job-list-item__description

    Expected detail page structure:

    h1.job-details__title
    div.job-details__subtitle  → posting-locations
    div.job-details__description-content.basic-formatter
    ul.job-meta__list
      li.job-meta__item
        span.job-meta__title   (label)
        span.job-meta__subitem  (value)
    """

    JOB_CARD_SELECTORS = [
        "ul#panel-list li[data-qa='searchResultItem']",
        "li[data-qa='searchResultItem']",
        "a.job-list-item__link[href*='/job/']",
    ]

    CARD_SELECTOR = "ul#panel-list li[data-qa='searchResultItem'], li[data-qa='searchResultItem']"
    LINK_SELECTOR = "a.job-list-item__link[href*='/job/'], a[href*='/job/']"
    TITLE_SELECTOR = "span.job-tile__title"
    DESCRIPTION_SELECTOR = "p.job-list-item__description"

    JOB_INFO_ITEM_SELECTOR = "li.job-list-item__job-info-item"
    JOB_INFO_VALUE_SELECTOR = "div.job-list-item__job-info-value"

    DETAIL_TITLE_SELECTOR = "h1.job-details__title"
    DETAIL_SUBTITLE_SELECTOR = "div.job-details__subtitle"
    DETAIL_META_ITEM_SELECTOR = "li.job-meta__item"
    DETAIL_META_TITLE_SELECTOR = "span.job-meta__title"
    DETAIL_META_VALUE_SELECTOR = "span.job-meta__subitem"
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

                        detail_title = detail_data.get("title", "")
                        detail_location = detail_data.get("location", "")
                        detail_posted_date = detail_data.get("posting date", "")
                        detail_description = detail_data.get("description", "")

                        metadata_description = self._format_detail_metadata(detail_data)

                        combined_description = self._join_description_parts(
                            metadata_description,
                            detail_description,
                        )

                        job = Job(
                            job_id=job.job_id,
                            company=job.company,
                            title=detail_title or job.title,
                            location=detail_location or job.location,
                            url=job.url,
                            source_url=job.source_url,
                            posted_date=detail_posted_date or job.posted_date,
                            description=combined_description or job.description,
                            scraped_at=datetime.now(timezone.utc).isoformat(),
                            extracted_experience_parts="",
                        )

                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich Kotak job detail page %s: %s",
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
        description = self._extract_short_description(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Kotak Mahindra Bank"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=posted_date or None,
            description=description or None,
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

        return self._make_kotak_job_url(source_url, str(href))

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        Kotak Oracle Cloud URLs look like:

        https://hcbt.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/208863/?...
        """

        if link:
            job_id = self._extract_kotak_job_id_from_url(link)

            if job_id:
                return job_id

        labelled_link = card.select_one("[aria-labelledby]")

        if labelled_link:
            aria_labelledby = labelled_link.get("aria-labelledby")

            if aria_labelledby and str(aria_labelledby).isdigit():
                return str(aria_labelledby)

        header = card.select_one("search-result-item-header[id]")

        if header:
            header_id = header.get("id")

            if header_id and str(header_id).isdigit():
                return str(header_id)

        return extract_job_id(link) if link else ""

    def _extract_location(self, card: Tag) -> str:
        """
        Card job info values normally appear with Labels like Locations, Posting Date.
        """

        locations: list[str] = []

        # Prefer explicit posting-locations block.
        posting_locations = card.select_one("posting-locations")

        if posting_locations:
            locations.extend(self._extract_locations_from_posting_locations(posting_locations))

        # Fallback to first job-info value.
        if not locations:
            info_values = card.select(self.JOB_INFO_VALUE_SELECTOR)

            if info_values:
                text = self._clean_location_text(info_values[0].get_text())

                if text:
                    locations.extend(self._split_location_text(text))

        return ", ".join(self._dedupe_preserve_order(locations))

    def _extract_locations_from_posting_locations(self, node: Tag) -> list[str]:
        locations: list[str] = []

        # Primary visible location.
        primary_span = node.select_one("span[data-bind*='primaryLocation']")

        if primary_span:
            primary_text = self._clean_location_text(primary_span.get_text())

            if primary_text:
                locations.append(primary_text)

        # Secondary locations are often available in aria-label:
        # "Locations,Mumbai, Maharashtra, India,Bengaluru, Karnataka, India"
        for el in node.select("[aria-label]"):
            aria_label = self._clean_text(str(el.get("aria-label", "")))

            if not aria_label:
                continue

            if aria_label.lower().startswith("locations,"):
                raw_locations = aria_label.split(",", 1)[1]
                locations.extend(self._split_location_text(raw_locations))

        return self._dedupe_preserve_order(locations)

    def _extract_posted_date(self, card: Tag) -> str:
        """
        Cards show posting date in a div with class job-list-item__job-info-value
        after a label "Posting Date". Find it by looking for the value following
        the posting-date label.
        """
        # Try to find the posting date label and its associated value
        info_items = card.select(self.JOB_INFO_ITEM_SELECTOR)

        for item in info_items:
            label_el = item.select_one("div.job-list-item__job-info-label")
            if not label_el:
                continue

            label_text = self._clean_text(label_el.get_text())
            if "posting date" not in label_text.lower():
                continue

            value_el = item.select_one(self.JOB_INFO_VALUE_SELECTOR)
            if value_el:
                return self._clean_text(value_el.get_text())

        # Fallback: regex on full card text
        text = self._clean_text(card.get_text(" "))

        match = re.search(
            r"Posted\s+on\s+(\d{1,2}/\d{1,2}/\d{4})",
            text,
            flags=re.IGNORECASE,
        )

        if match:
            return match.group(1)

        return ""

    def _extract_short_description(self, card: Tag) -> str:
        el = card.select_one(self.DESCRIPTION_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

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
                    self.DETAIL_TITLE_SELECTOR,
                    self.DETAIL_DESCRIPTION_SELECTOR,
                    self.DETAIL_META_ITEM_SELECTOR,
                ],
            )

            soup = await self._get_soup(detail_page)

            detail_data = self._extract_detail_metadata(soup)

            title = self._extract_detail_title(soup)
            location = self._extract_detail_location(soup)
            description = self._extract_detail_description(soup)

            if title:
                detail_data["title"] = title

            if location:
                detail_data["location"] = location

            if description:
                detail_data["description"] = description

            return detail_data

        finally:
            await detail_page.close()

    def _extract_detail_title(self, soup) -> str:
        el = soup.select_one(self.DETAIL_TITLE_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_detail_location(self, soup) -> str:
        """
        Prefer full address from Job Information > Locations.
        Fallback to subtitle location.
        """

        metadata = self._extract_detail_metadata(soup)

        detail_location = metadata.get("locations", "")

        if detail_location:
            return detail_location

        subtitle = soup.select_one(self.DETAIL_SUBTITLE_SELECTOR)

        if subtitle:
            locations = self._extract_locations_from_posting_locations(subtitle)

            if locations:
                return ", ".join(locations)

            return self._clean_location_text(subtitle.get_text())

        return ""

    def _extract_detail_metadata(self, soup) -> dict[str, str]:
        detail_data: dict[str, str] = {}

        for item in soup.select(self.DETAIL_META_ITEM_SELECTOR):
            label_el = item.select_one(self.DETAIL_META_TITLE_SELECTOR)
            value_el = item.select_one(self.DETAIL_META_VALUE_SELECTOR)

            label = self._clean_text(label_el.get_text() if label_el else "")
            value = ""

            if value_el:
                # Locations can contain multiple pin items.
                pin_items = value_el.select(".job-meta__pin-item")

                if pin_items:
                    locations = [
                        self._clean_location_text(pin.get_text())
                        for pin in pin_items
                        if self._clean_location_text(pin.get_text())
                    ]
                    value = ", ".join(self._dedupe_preserve_order(locations))
                else:
                    value = self._clean_text(value_el.get_text())

            if not label or not value:
                continue

            detail_data[label.lower()] = value

        return detail_data

    def _extract_detail_description(self, soup) -> str:
        container = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)

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
            "job identification",
            "job category",
            "posting date",
            "apply before",
            "degree level",
            "job schedule",
            "locations",
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

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def _make_kotak_job_url(self, source_url: str, href: str) -> str:
        href = html.unescape(href).strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        parsed_source = urlparse(source_url)
        origin = f"{parsed_source.scheme}://{parsed_source.netloc}"

        if href.startswith("/hcmUI/"):
            return f"{origin}{href}"

        if href.startswith("hcmUI/"):
            return f"{origin}/{href}"

        return make_absolute_url(source_url, href)

    def _extract_kotak_job_id_from_url(self, url: str) -> str:
        if not url:
            return ""

        match = re.search(r"/job/(\d+)", url, flags=re.IGNORECASE)

        if match:
            return match.group(1)

        return extract_job_id(url) or ""

    # ------------------------------------------------------------------
    # Fallback
    # ------------------------------------------------------------------

    async def _fallback_links(
        self, page: Page, source_url: str, max_jobs: int
    ) -> list[Job]:
        jobs: list[Job] = []
        soup = await self._get_soup(page)

        all_links = soup.select("a[href*='/job/']")
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()

        for link_el in all_links[:max_jobs]:
            href = link_el.get("href")
            if not href:
                continue

            url = self._make_kotak_job_url(source_url, str(href))
            if url in seen_urls:
                continue
            seen_urls.add(url)

            job_id = self._extract_kotak_job_id_from_url(url)
            if job_id in seen_ids:
                continue
            if job_id:
                seen_ids.add(job_id)

            jobs.append(Job(
                job_id=job_id,
                company=self.company_config.get("name", "Kotak Mahindra Bank"),
                title="Unknown",
                location="",
                url=url,
                source_url=source_url,
                posted_date=None,
                description=None,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                extracted_experience_parts="",
            ))

        return jobs

    # ------------------------------------------------------------------
    # Text / location utilities
    # ------------------------------------------------------------------

    def _split_location_text(self, text: str) -> list[str]:
        text = self._clean_location_text(text)

        if not text:
            return []

        # Remove UI text like "and 1 more" but preserve actual locations.
        text = re.sub(r"\band\s+\d+\s+more\b", "", text, flags=re.IGNORECASE)
        text = self._clean_text(text)

        if not text:
            return []

        parts = re.split(r"(?<=India),\s*", text)

        locations = [self._clean_location_text(part) for part in parts]
        return [location for location in locations if location]

    def _clean_location_text(self, text: str) -> str:
        text = self._clean_text(text)

        if not text:
            return ""

        lower_text = text.lower()

        noise_values = {
            "locations",
            "location",
            "and more",
        }

        if lower_text in noise_values:
            return ""

        if lower_text.startswith("locations,"):
            text = text.split(",", 1)[1].strip()

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

        lines: list[str] = []

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
