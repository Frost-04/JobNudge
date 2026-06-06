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


class OracleScraper(BaseScraper):
    """
    Scraper for Oracle Careers job search pages.

    Oracle uses the same Oracle Cloud Candidate Experience platform as
    JPMorgan Chase.  The DOM structure is nearly identical.

    Expected search card structure:

        ul#panel-list.jobs-grid__list
          li[data-qa="searchResultItem"]
            a.job-grid-item__link[href*="/job/{id}/"]
            span.job-tile__title
            posting-locations  (location info in primaryLocation / aria-label)
            p.job-grid-item__description

    Expected detail page / overlay structure:

        h1.job-details__title
        div.job-details__subtitle  (posting-locations)
        ul.job-meta__list
          li.job-meta__item
            span.job-meta__title
            span.job-meta__subitem
        div.job-details__description-content  (one per section:
            Job Description / Responsibilities / Qualifications / About Us)
    """

    JOB_CARD_SELECTORS = [
        "ul#panel-list li[data-qa='searchResultItem']",
        "li[data-qa='searchResultItem']",
        "a.job-grid-item__link[href*='/job/']",
    ]

    CARD_SELECTOR = "ul#panel-list li[data-qa='searchResultItem'], li[data-qa='searchResultItem']"
    LINK_SELECTOR = "a.job-grid-item__link[href*='/job/'], a[href*='/job/']"
    TITLE_SELECTOR = "span.job-tile__title"
    DESCRIPTION_SELECTOR = "p.job-grid-item__description"

    # Oracle card job-info items (locations, etc.)
    JOB_INFO_ITEM_SELECTOR = "li.job-list-item__job-info-item"
    JOB_INFO_VALUE_SELECTOR = "div.job-list-item__job-info-value"

    DETAIL_TITLE_SELECTOR = "h1.job-details__title"
    DETAIL_SUBTITLE_SELECTOR = "div.job-details__subtitle"
    DETAIL_META_ITEM_SELECTOR = "li.job-meta__item"
    DETAIL_META_TITLE_SELECTOR = "span.job-meta__title"
    DETAIL_META_VALUE_SELECTOR = "span.job-meta__subitem"
    DETAIL_DESCRIPTION_SELECTOR = "div.job-details__description-content"
    DETAIL_SECTION_SELECTOR = "div.job-details__section"

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
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_job_ids:
                    continue

                if job.url in seen_urls:
                    continue

                # Enrich card data by opening the direct job URL.
                # Oracle Career pages support deep-linking to job detail pages
                # and the detail page has the same DOM as the overlay.
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
                            "Failed to enrich Oracle job detail page %s: %s",
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

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

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
            company=self.company_config.get("name", "Oracle"),
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

        return self._make_oracle_job_url(source_url, str(href))

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        Oracle Careers URLs look like:

        https://careers.oracle.com/en/sites/jobsearch/job/327846/?...
        """

        if link:
            job_id = self._extract_oracle_job_id_from_url(link)

            if job_id:
                return job_id

        # Aria-labelledby on the anchor often holds the numeric job id.
        labelled_link = card.select_one("[aria-labelledby]")

        if labelled_link:
            aria_labelledby = labelled_link.get("aria-labelledby")

            if aria_labelledby and str(aria_labelledby).isdigit():
                return str(aria_labelledby)

        # search-result-item-header[id] also holds the numeric job id.
        header = card.select_one("search-result-item-header[id]")

        if header:
            header_id = header.get("id")

            if header_id and str(header_id).isdigit():
                return str(header_id)

        return extract_job_id(link) if link else ""

    def _extract_location(self, card: Tag) -> str:
        """
        Card job info values normally appear in this order:
        1. Locations
        2. Job Function
        3. Job Family

        Location can include hidden/tooltip text such as:
        aria-label="Locations,India,BENGALURU, KARNATAKA, India,..."
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
        # "Locations,India,BENGALURU, KARNATAKA, India,..."
        for el in node.select("[aria-label]"):
            aria_label = self._clean_text(str(el.get("aria-label", "")))

            if not aria_label:
                continue

            # The tooltip anchor aria-label starts with "Locations,".
            if aria_label.lower().startswith("locations,"):
                raw_locations = aria_label.split(",", 1)[1]
                locations.extend(self._split_location_text(raw_locations))

            # Alternative: the count span's aria-label has all locations.
            # e.g. "India,BENGALURU, KARNATAKA, India,..."
            if not locations and aria_label and "," in aria_label:
                # Check if this looks like a location string (contains city/country).
                parts = [p.strip() for p in aria_label.split(",")]
                if len(parts) >= 2:
                    locations.extend(self._split_location_text(aria_label))

        return self._dedupe_preserve_order(locations)

    def _extract_posted_date(self, card: Tag) -> str:
        """
        Cards in the Oracle listing do not always show a posted date.
        Similar-job cards can show:
        Posted on 03/06/2026
        """

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
                    self.DETAIL_SECTION_SELECTOR,
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
        """
        Oracle detail pages have multiple description-content blocks:
        - Job Description
        - Responsibilities
        - Qualifications
        - About Us

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
        Job model has no separate metadata fields, so preserve useful Oracle
        detail fields inside description.
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

    def _make_oracle_job_url(self, source_url: str, href: str) -> str:
        href = html.unescape(href).strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        parsed_source = urlparse(source_url)
        origin = f"{parsed_source.scheme}://{parsed_source.netloc}"

        return make_absolute_url(origin, href)

    def _extract_oracle_job_id_from_url(self, url: str) -> str:
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
        Handles both:
        - "BENGALURU, KARNATAKA, India"
        - "India,BENGALURU, KARNATAKA, India,HYDERABAD, TELANGANA, India"

        Split after each "India".
        """

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

            job_url = self._make_oracle_job_url(source_url, str(href))
            job_id = self._extract_oracle_job_id_from_url(job_url)

            if job_id and job_id in seen_job_ids:
                continue

            if job_url in seen_urls:
                continue

            card = anchor.find_parent("li", attrs={"data-qa": "searchResultItem"})

            title = ""
            location = ""
            posted_date = ""
            description = ""

            if card:
                title = self._extract_title(card)
                location = self._extract_location(card)
                posted_date = self._extract_posted_date(card)
                description = self._extract_short_description(card)

            if not title:
                title = self._clean_text(anchor.get_text())

            if not title:
                continue

            if job_id:
                seen_job_ids.add(job_id)

            seen_urls.add(job_url)

            results.append(
                Job(
                    job_id=job_id,
                    company=self.company_config.get("name", "Oracle"),
                    title=title,
                    location=location,
                    url=job_url,
                    source_url=source_url,
                    posted_date=posted_date or None,
                    description=description or None,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    extracted_experience_parts="",
                )
            )

        return results
