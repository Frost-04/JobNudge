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


class AmericanExpressScraper(BaseScraper):
    """
    Scraper for American Express Careers job search pages.

    American Express uses the Oracle HCM Cloud Candidate Experience
    platform, same as JPMorgan Chase and Oracle.  The DOM structure is
    nearly identical.

    Expected search card structure:

        ul#panel-list.jobs-list__list
          li
            div.search-results.job-tile.job-list-item
              a.job-list-item__link[href*="/job/{id}/"]
              search-result-item-header[id="{job_id}"]

    Note: Card data (title, location, etc.) may live inside a shadow DOM
    of <search-result-item-header>.  This scraper takes the conservative
    approach of extracting only the job URL from each card and then
    navigating to the detail page for all metadata.

    Expected detail page structure:

        h1.job-details__title
        div.job-details__subtitle  (posting-locations)
        ul.job-meta__list
          li.job-meta__item
            span.job-meta__title
            span.job-meta__subitem
        div.job-details__description-content.basic-formatter
    """

    # ---- Card selectors ----
    JOB_CARD_SELECTORS = [
        "ul#panel-list li",
        "a.job-list-item__link[href*='/job/']",
    ]

    CARD_SELECTOR = "ul#panel-list li"
    LINK_SELECTOR = "a.job-list-item__link[href*='/job/']"

    # ---- Detail page selectors ----
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

            # Oracle Candidate Experience pages can keep background requests open,
            # so wait for cards instead of relying on networkidle.
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
                link = self._extract_link(card, source_url)
                job_id = self._extract_job_id(card, link)

                if not link:
                    continue

                if job_id and job_id in seen_job_ids:
                    continue

                if link in seen_urls:
                    continue

                # Enrich by opening the direct job detail URL.
                # Card-level extraction is limited (shadow DOM), so we get
                # title, location, description, and metadata from the detail page.
                try:
                    detail_data = await self._scrape_detail_page(link)

                    title = detail_data.get("title", "")
                    location = detail_data.get("location", "")
                    posted_date = detail_data.get("posting date", "")
                    description = detail_data.get("description", "")

                    if not title:
                        self.logger.debug("Skipping job with no title: %s", link)
                        continue

                    # Skip detail enrichment (and the entire job) for excluded titles.
                    if self._should_exclude(title):
                        self.logger.debug("Skipping excluded role: %s", title)
                        if job_id:
                            seen_job_ids.add(job_id)
                        seen_urls.add(link)
                        continue

                    metadata_description = self._format_detail_metadata(detail_data)

                    combined_description = self._join_description_parts(
                        metadata_description,
                        description,
                    )

                    job = Job(
                        job_id=job_id,
                        company=self.company_config.get("name", "American Express"),
                        title=title,
                        location=location,
                        url=link,
                        source_url=source_url,
                        posted_date=posted_date or None,
                        description=combined_description or None,
                        scraped_at=datetime.now(timezone.utc).isoformat(),
                        extracted_experience_parts="",
                    )

                except Exception as exc:
                    self.logger.warning(
                        "Failed to scrape American Express detail page %s: %s",
                        link,
                        exc,
                    )
                    continue

                if job_id:
                    seen_job_ids.add(job_id)

                seen_urls.add(link)
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

    # ------------------------------------------------------------------
    # Card parsing (light DOM only — URL + job ID)
    # ------------------------------------------------------------------

    def _extract_link(self, card: Tag, source_url: str) -> str:
        el = card.select_one(self.LINK_SELECTOR)

        if not el:
            return ""

        href = el.get("href")

        if not href:
            return ""

        return self._make_amex_job_url(source_url, str(href))

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        American Express job URLs look like:

        https://careers.americanexpress.com/en/sites/CX_1/job/26007060/?...

        Also available via search-result-item-header[id].
        """

        if link:
            job_id = self._extract_amex_job_id_from_url(link)

            if job_id:
                return job_id

        # search-result-item-header[id] holds the numeric job id.
        header = card.select_one("search-result-item-header[id]")

        if header:
            header_id = header.get("id")

            if header_id and str(header_id).isdigit():
                return str(header_id)

        return extract_job_id(link) if link else ""

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def _get_detail_page(self) -> Page:
        """Return a new page for detail scraping.

        Tries the shared ``self.context`` first.  If that context has been
        closed or is otherwise unusable the old browser stack is torn down
        and a fresh one is spun up so that one bad detail page cannot poison
        all subsequent enrichments.
        """
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

    def _extract_locations_from_posting_locations(self, node: Tag) -> list[str]:
        locations: list[str] = []

        # Primary visible location.
        primary_span = node.select_one("span[data-bind*='primaryLocation']")

        if primary_span:
            primary_text = self._clean_location_text(primary_span.get_text())

            if primary_text:
                locations.append(primary_text)

        # Secondary locations are often available in aria-label:
        for el in node.select("[aria-label]"):
            aria_label = self._clean_text(str(el.get("aria-label", "")))

            if not aria_label:
                continue

            if aria_label.lower().startswith("locations,"):
                raw_locations = aria_label.split(",", 1)[1]
                locations.extend(self._split_location_text(raw_locations))

        return self._dedupe_preserve_order(locations)

    def _extract_detail_description(self, soup) -> str:
        """
        American Express detail pages have description-content blocks.
        Collect them all and join with section separators.
        """
        containers = soup.select(self.DETAIL_DESCRIPTION_SELECTOR)

        if not containers:
            return ""

        sections: list[str] = []

        for container in containers:
            for unwanted in container.select("script, style, noscript"):
                unwanted.decompose()

            text = container.get_text(separator="\n")
            cleaned = self._clean_multiline_text(text)

            if cleaned:
                sections.append(cleaned)

        return "\n\n".join(sections)

    def _format_detail_metadata(self, detail_data: dict[str, str]) -> str:
        """
        Preserve useful American Express detail fields inside description.
        """

        if not detail_data:
            return ""

        lines: list[str] = []

        preferred_order = [
            "job identification",
            "job category",
            "posting date",
            "role",
            "job type",
            "years",
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

    def _make_amex_job_url(self, source_url: str, href: str) -> str:
        href = html.unescape(href).strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        parsed_source = urlparse(source_url)
        origin = f"{parsed_source.scheme}://{parsed_source.netloc}"

        return make_absolute_url(origin, href)

    def _extract_amex_job_id_from_url(self, url: str) -> str:
        if not url:
            return ""

        match = re.search(r"/job/(\d+)", url, flags=re.IGNORECASE)

        if match:
            return match.group(1)

        return extract_job_id(url) or ""

    # ------------------------------------------------------------------
    # Location helpers
    # ------------------------------------------------------------------

    def _split_location_text(self, text: str) -> list[str]:
        """
        Handles formatted location strings from aria-label.
        """

        text = self._clean_location_text(text)

        if not text:
            return []

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

    # ------------------------------------------------------------------
    # Text cleaning
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

        lines: list[str] = []

        for line in text.splitlines():
            clean_line = self._clean_text(line)

            if clean_line:
                lines.append(clean_line)

        return "\n".join(lines).strip()

    # ------------------------------------------------------------------
    # General helpers
    # ------------------------------------------------------------------

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

    async def _fallback_links(
        self,
        page: Page,
        source_url: str,
        max_jobs: int,
    ) -> list[Job]:
        """
        Fallback for Oracle Cloud DOM changes.
        Scans all job links and builds basic Job objects.
        """

        soup = await self._get_soup(page)

        anchors = soup.select("a[href*='/job/']")

        results: list[Job] = []
        seen_job_ids: set[str] = set()
        seen_urls: set[str] = set()

        for anchor in anchors[:max_jobs]:
            href = anchor.get("href")

            if not href:
                continue

            job_url = self._make_amex_job_url(source_url, str(href))
            job_id = self._extract_amex_job_id_from_url(job_url)

            if job_id and job_id in seen_job_ids:
                continue

            if job_url in seen_urls:
                continue

            # Get title from anchor text if available.
            title = self._clean_text(anchor.get_text())

            if not title:
                continue

            if job_id:
                seen_job_ids.add(job_id)

            seen_urls.add(job_url)

            results.append(
                Job(
                    job_id=job_id,
                    company=self.company_config.get("name", "American Express"),
                    title=title,
                    location="",
                    url=job_url,
                    source_url=source_url,
                    posted_date=None,
                    description=None,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    extracted_experience_parts="",
                )
            )

        return results
