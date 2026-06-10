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


class DPWorldScraper(BaseScraper):
    """
    Scraper for DP World Oracle Cloud Candidate Experience pages.

    DP World uses the same Oracle Cloud platform as JPMorgan Chase.
    Cards are in ``li[data-qa="searchResultItem"]`` grid layout.

    **Detail extraction via overlay** — the standalone job page does not
    reliably populate the Knockout.js ``data-bind="html: pageData().job.description"``
    binding.  Instead we click the card body div (not the ``<a>`` link) on
    the listing page to open the job-details overlay, extract the full
    description, then close the overlay with Escape.

    Expected search card structure:

        li[data-qa="searchResultItem"]
          a.job-grid-item__link[href*="/job/{id}/"]
          div.job-grid-item__link  (data-bind="click: openJobPreview")
            span.job-tile__title

    Expected overlay structure:

        h1.job-details__title
        ul.job-meta__list
          li.job-meta__item
            span.job-meta__title / span.job-meta__subitem
        div.job-details__description-content.basic-formatter
          (full rich HTML description)
    """

    JOB_CARD_SELECTORS = [
        "ul#panel-list li[data-qa='searchResultItem']",
        "li[data-qa='searchResultItem']",
        "a.job-grid-item__link[href*='/job/']",
    ]

    CARD_SELECTOR = "ul#panel-list li[data-qa='searchResultItem'], li[data-qa='searchResultItem']"
    LINK_SELECTOR = "a.job-grid-item__link[href*='/job/'], a[href*='/job/']"
    TITLE_SELECTOR = "span.job-tile__title"

    JOB_INFO_ITEM_SELECTOR = "li.job-list-item__job-info-item"
    JOB_INFO_VALUE_SELECTOR = "div.job-list-item__job-info-value"

    # Overlay selectors (used on detail pages).
    DETAIL_TITLE = "h1.job-details__title"
    DETAIL_META_ITEM = "li.job-meta__item"
    DETAIL_META_TITLE = "span.job-meta__title"
    DETAIL_META_VALUE = "span.job-meta__subitem"
    DETAIL_DESCRIPTION = "div.job-details__description-content"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)

            selector = await self._wait_for_any_selector(page, self.JOB_CARD_SELECTORS)

            if not selector:
                return await self._fallback_links(page, source_url, max_jobs)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                return await self._fallback_links(page, source_url, max_jobs)

            seen_job_ids: set[str] = set()
            seen_urls: set[str] = set()

            for idx, card in enumerate(cards[:max_jobs]):
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_job_ids:
                    continue

                if job.url in seen_urls:
                    continue

                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
                    job.description = None
                else:
                    try:
                        detail_data = await self._scrape_detail_page(job.job_id, job.url)

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
                            "Failed to enrich DP World overlay for card %d: %s", idx, exc
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
            self.settings.get("run", {}).get("page_load_timeout_seconds"), 45000
        )
        for selector in selectors:
            try:
                await page.wait_for_selector(selector, timeout=timeout_ms)
                return selector
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # Card parsing (BS4 from listing page soup)
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card, source_url)
        title = self._extract_title(card)
        job_id = self._extract_job_id(card, link)
        location = self._extract_location(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "DP World"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date="",
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
        return self._make_dp_world_job_url(source_url, str(href))

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_job_id(self, card: Tag, link: str) -> str:
        if link:
            job_id = self._extract_dp_world_job_id_from_url(link)
            if job_id:
                return job_id

        labelled_link = card.select_one("[aria-labelledby]")
        if labelled_link:
            aria_id = labelled_link.get("aria-labelledby")
            if aria_id and str(aria_id).isdigit():
                return str(aria_id)

        header = card.select_one("search-result-item-header[id]")
        if header:
            header_id = header.get("id")
            if header_id and str(header_id).isdigit():
                return str(header_id)

        return extract_job_id(link) if link else ""

    def _extract_location(self, card: Tag) -> str:
        locations: list[str] = []

        posting_locations = card.select_one("posting-locations")
        if posting_locations:
            locations.extend(self._extract_locations_from_posting_locations(posting_locations))

        if not locations:
            info_values = card.select(self.JOB_INFO_VALUE_SELECTOR)
            if info_values:
                text = self._clean_location_text(info_values[0].get_text())
                if text:
                    locations.extend(self._split_location_text(text))

        return ", ".join(self._dedupe_preserve_order(locations))

    def _extract_locations_from_posting_locations(self, node: Tag) -> list[str]:
        locations: list[str] = []

        primary_span = node.select_one("span[data-bind*='primaryLocation']")
        if primary_span:
            text = self._clean_location_text(primary_span.get_text())
            if text:
                locations.append(text)

        for el in node.select("[aria-label]"):
            aria_label = self._clean_text(str(el.get("aria-label", "")))
            if not aria_label:
                continue
            if aria_label.lower().startswith("locations,"):
                raw = aria_label.split(",", 1)[1]
                locations.extend(self._split_location_text(raw))

        return self._dedupe_preserve_order(locations)

    # ------------------------------------------------------------------
    # Detail page enrichment (direct page navigation + BS4)
    # ------------------------------------------------------------------

    async def _get_detail_page(self) -> Page:
        if self.context:
            try:
                return await self.context.new_page()
            except Exception:
                self.logger.debug("Context stale; creating fresh one.")
                await self.close_browser()
        return await self.new_page()

    async def _scrape_detail_page(self, job_id: str, job_url: str) -> dict[str, str]:
        """Navigate to the job detail page and extract metadata + description."""

        detail_page = await self._get_detail_page()
        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)

            await self._wait_for_any_selector(
                detail_page,
                [self.DETAIL_TITLE, self.DETAIL_META_ITEM, self.DETAIL_DESCRIPTION],
            )

            soup = await self._get_soup(detail_page)
            detail_data = self._extract_detail_metadata(soup)

            title = self._clean_text(
                soup.select_one(self.DETAIL_TITLE).get_text() if soup.select_one(self.DETAIL_TITLE) else ""
            )
            description = self._extract_detail_description(soup)

            if title:
                detail_data["title"] = title
            if description:
                detail_data["description"] = description

            return detail_data
        finally:
            await detail_page.close()

    def _extract_detail_metadata(self, soup) -> dict[str, str]:
        detail_data: dict[str, str] = {}
        for item in soup.select(self.DETAIL_META_ITEM):
            label_el = item.select_one(self.DETAIL_META_TITLE)
            value_el = item.select_one(self.DETAIL_META_VALUE)
            label = self._clean_text(label_el.get_text() if label_el else "")
            value = ""
            if value_el:
                pin_items = value_el.select(".job-meta__pin-item")
                if pin_items:
                    locs = [
                        self._clean_location_text(p.get_text())
                        for p in pin_items
                        if self._clean_location_text(p.get_text())
                    ]
                    value = ", ".join(self._dedupe_preserve_order(locs))
                else:
                    value = self._clean_text(value_el.get_text())
            if label and value:
                detail_data[label.lower()] = value
        return detail_data

    def _extract_detail_description(self, soup) -> str:
        container = soup.select_one(self.DETAIL_DESCRIPTION)
        if not container:
            return ""
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()
        text = container.get_text(separator="\n")
        text = self._clean_multiline_text(text)
        return self._strip_map_legend(text)

    def _strip_map_legend(self, text: str) -> str:
        """Remove Oracle Cloud map boilerplate from description text."""
        if not text:
            return ""
        # "© MapTiler © OpenStreetMap contributors"
        text = re.sub(r"\s*©\s*\S+\s*(©\s*\S+\s*contributors)?", "", text)
        # "Legend Jobs at a location … Copy to Clipboard"
        text = re.sub(
            r"\s*Legend\s+Jobs\s+at\s+a\s+location\s+.*?Copy\s+to\s+Clipboard\s*",
            "", text, flags=re.IGNORECASE | re.DOTALL,
        )
        return text.strip()

    # ------------------------------------------------------------------
    # Metadata formatting
    # ------------------------------------------------------------------

    def _format_detail_metadata(self, detail_data: dict[str, str]) -> str:
        if not detail_data:
            return ""

        lines: list[str] = []
        preferred_order = [
            "job identification", "job category", "business unit",
            "posting date", "apply before", "job schedule", "locations",
        ]
        for key in preferred_order:
            value = detail_data.get(key)
            if value:
                lines.append(f"{key.title()}: {value}")
        return "\n".join(lines)

    def _join_description_parts(self, *parts: str) -> str:
        cleaned = [p.strip() for p in parts if p and p.strip()]
        return "\n\n".join(cleaned)

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def _make_dp_world_job_url(self, source_url: str, href: str) -> str:
        href = html.unescape(href).strip()
        if href.startswith("http://") or href.startswith("https://"):
            return href
        parsed = urlparse(source_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if href.startswith("/hcmUI/"):
            return f"{origin}{href}"
        if href.startswith("hcmUI/"):
            return f"{origin}/{href}"
        return make_absolute_url(source_url, href)

    def _extract_dp_world_job_id_from_url(self, url: str) -> str:
        if not url:
            return ""
        match = re.search(r"/job/(\d+)", url, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        return extract_job_id(url) or ""

    # ------------------------------------------------------------------
    # Text helpers
    # ------------------------------------------------------------------

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
        lines = [self._clean_text(line) for line in text.splitlines()]
        return "\n".join(line for line in lines if line).strip()

    def _clean_location_text(self, text: str) -> str:
        text = self._clean_text(text)
        if not text:
            return ""
        lower = text.lower()
        if lower in {"locations", "location", "and more"}:
            return ""
        if lower.startswith("locations,"):
            text = text.split(",", 1)[1].strip()
        return text

    def _split_location_text(self, text: str) -> list[str]:
        text = self._clean_location_text(text)
        if not text:
            return []
        text = re.sub(r"\band\s+\d+\s+more\b", "", text, flags=re.IGNORECASE)
        text = self._clean_text(text)
        if not text:
            return []
        parts = re.split(r"(?<=India),\s*", text)
        return [loc for loc in (self._clean_location_text(p) for p in parts) if loc]

    def _dedupe_preserve_order(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for v in values:
            if v and v not in seen:
                seen.add(v)
                result.append(v)
        return result
