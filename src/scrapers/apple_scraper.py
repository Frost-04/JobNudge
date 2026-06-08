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


class AppleScraper(BaseScraper):
    """
    Scraper for Apple Jobs search result pages.

    Expected listing card structure (React accordion):

    ul#search-job-list[aria-label="Job Opportunities"]
      li.rc-accordion-item
        div.rc-accordion-button
          button[aria-label^="Role description:"]  → job title
          (element IDs contain the job ID)
        div.rc-accordion-content
          (summary/description content loaded dynamically)

    Job IDs are embedded in element IDs like:
      search-search-job-title-200657773-1052-7
      button-search-job-list-0

    Detail page URL pattern:
      https://jobs.apple.com/en-in/details/{job_id}

    Expected detail structure:
      #jobdetails-jobsummary          → Summary (posted date, weekly hours, role number)
      #jobdetails-jobdescription      → Description
      #jobdetails-responsibilities    → Responsibilities
      #jobdetails-minimumqualifications → Minimum Qualifications
      #jobdetails-preferredqualifications → Preferred Qualifications
    """

    # ---- Card selectors ----
    RESULTS_CONTAINER = 'ul#search-job-list'
    CARD_SELECTOR = 'li.rc-accordion-item'
    TITLE_BUTTON_SELECTOR = 'button[aria-label^="Role description:"]'

    # Job card selectors for waiting (tried in order)
    JOB_CARD_SELECTORS = [
        'ul#search-job-list li.rc-accordion-item',
        'li.rc-accordion-item',
        'button[aria-label^="Role description:"]',
    ]

    # ---- Detail page selectors ----
    DETAIL_SUMMARY_SELECTOR = '#jobdetails-jobsummary'
    DETAIL_DESCRIPTION_SELECTOR = '#jobdetails-jobdescription'
    DETAIL_RESPONSIBILITIES_SELECTOR = '#jobdetails-responsibilities'
    DETAIL_MIN_QUAL_SELECTOR = '#jobdetails-minimumqualifications'
    DETAIL_PREF_QUAL_SELECTOR = '#jobdetails-preferredqualifications'
    DETAIL_POSTED_DATE_SELECTOR = 'time#jobdetails-jobpostdate'
    DETAIL_ROLE_NUMBER_SELECTOR = '#jobdetails-jobnumber'

    # All detail section selectors for waiting
    DETAIL_SECTION_SELECTORS = [
        DETAIL_SUMMARY_SELECTOR,
        DETAIL_DESCRIPTION_SELECTOR,
        DETAIL_RESPONSIBILITIES_SELECTOR,
        DETAIL_MIN_QUAL_SELECTOR,
        DETAIL_PREF_QUAL_SELECTOR,
    ]

    # ---- Location fallback ----
    DEFAULT_LOCATION = "India"

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

                        detail_posted_date = detail_data.get("posted_date", "")
                        detail_description = detail_data.get("description", "")
                        detail_location = detail_data.get("location", "")
                        detail_role_number = detail_data.get("role_number", "")

                        # Prefer detail-page role number over card-extracted job ID
                        effective_job_id = detail_role_number or job.job_id

                        # Prefer detail location over card location
                        effective_location = detail_location or job.location

                        job = Job(
                            job_id=effective_job_id,
                            company=job.company,
                            title=job.title,
                            location=effective_location,
                            url=job.url,
                            source_url=job.source_url,
                            posted_date=detail_posted_date or job.posted_date,
                            description=detail_description or job.description,
                            scraped_at=datetime.now(timezone.utc).isoformat(),
                            extracted_experience_parts="",
                        )

                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich Apple job detail page %s: %s",
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
        title = self._extract_title(card)
        job_id = self._extract_job_id_from_card(card)
        location = self._extract_location(card)

        if not title:
            return None

        if not job_id:
            return None

        url = self._make_apple_job_url(source_url, job_id)

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Apple"),
            title=title,
            location=location,
            url=url,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_title(self, card: Tag) -> str:
        """Extract title from the button's aria-label.

        Pattern: aria-label="Role description: Lead Graph Engineer"
        """
        button = card.select_one(self.TITLE_BUTTON_SELECTOR)

        if not button:
            return ""

        aria_label = button.get("aria-label")

        if not aria_label:
            return ""

        # Strip the "Role description: " prefix
        title = str(aria_label).replace("Role description:", "").strip()
        return self._clean_text(title)

    def _extract_job_id_from_card(self, card: Tag) -> str:
        """Extract job ID from element IDs in the card.

        Pattern: search-search-job-title-200657773-1052-7
        The job ID is the numeric-hyphen portion: 200657773-1052
        """
        # Strategy 1: Look for IDs matching the search-job-title pattern
        for el in card.select('[id*="search-search-job-title-"]'):
            element_id = el.get("id", "")
            match = re.search(r"search-search-job-title-(\d+-\d+)", str(element_id))
            if match:
                return match.group(1)

        # Strategy 2: Look for IDs matching the search-job-content pattern
        for el in card.select('[id*="search-search-job-content-"]'):
            element_id = el.get("id", "")
            # Pattern: search-search-job-content-200657773-1052 (no trailing -N)
            match = re.search(r"search-search-job-content-(\d+-\d+)", str(element_id))
            if match:
                return match.group(1)

        # Strategy 3: Look for IDs matching button-search-job-list-N
        button = card.select_one('button[id*="button-search-job-list-"]')
        if button:
            button_id = button.get("id", "")
            match = re.search(r"button-search-job-list-(\d+)", str(button_id))
            if match:
                return match.group(1)

        return ""

    def _extract_location(self, card: Tag) -> str:
        """Try to extract location from the card.

        Apple cards load location dynamically via JavaScript.
        The .job-title-location div has a span.a11y (label text)
        and a span#search-store-name-container (actual city).
        When get_text() is called on the parent, it may return
        "LocationHyderabad" (concatenated label + value).
        """
        # Strategy 1: Try the search-store-name-container span directly
        store_span = card.select_one('#search-store-name-container-7, [id^="search-store-name-container"]')
        if store_span:
            text = self._clean_text(store_span.get_text())
            if text and text.lower() not in ("location", "locations", "remote"):
                return text

        # Strategy 2: Try the job-title-location container
        loc_el = card.select_one('.job-title-location')
        if loc_el:
            text = self._clean_text(loc_el.get_text())
            if text:
                # Strip "Location" prefix if present (from a11y label)
                text = re.sub(r'^Location\s*', '', text)
                text = self._clean_text(text)
                if text and text.lower() not in ("location", "locations", "remote"):
                    return text

        # Strategy 3: Try any span with aria-label
        for span in card.select('span.a11y'):
            aria_label = span.get("aria-label")
            if aria_label:
                text = self._clean_text(str(aria_label))
                if text and text.lower() not in ("location", "locations", "remote"):
                    return text

        # Fallback: default to India since the search URL filters to India
        return self.DEFAULT_LOCATION

    # ------------------------------------------------------------------
    # URL construction
    # ------------------------------------------------------------------

    def _make_apple_job_url(self, source_url: str, job_id: str) -> str:
        """Construct the detail page URL from the job ID.

        Pattern: https://jobs.apple.com/en-in/details/200657773-1052
        """
        parsed_source = urlparse(source_url)
        origin = f"{parsed_source.scheme}://{parsed_source.netloc}"

        # Extract the locale prefix from the source URL
        # e.g., /en-in/search → /en-in
        path_parts = parsed_source.path.split("/")
        locale_prefix = ""
        if len(path_parts) >= 2:
            locale_prefix = f"/{path_parts[1]}"

        if not locale_prefix or locale_prefix == "/":
            locale_prefix = "/en-in"

        return f"{origin}{locale_prefix}/details/{job_id}"

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def _get_detail_page(self) -> Page:
        """Return a new page for detail scraping, creating a fresh context if needed."""
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

            # Wait for any detail section to appear
            await self._wait_for_any_selector(detail_page, self.DETAIL_SECTION_SELECTORS)

            soup = await self._get_soup(detail_page)

            detail_data: dict[str, str] = {}

            # Extract posted date
            posted_date = self._extract_posted_date_from_detail(soup)
            if posted_date:
                detail_data["posted_date"] = posted_date

            # Extract role number
            role_number = self._extract_role_number_from_detail(soup)
            if role_number:
                detail_data["role_number"] = role_number

            # Extract location from detail page
            location = self._extract_location_from_detail(soup)
            if location:
                detail_data["location"] = location

            # Extract full description
            description = self._extract_description(soup)
            if description:
                detail_data["description"] = description

            return detail_data

        finally:
            await detail_page.close()

    def _extract_posted_date_from_detail(self, soup) -> str:
        """Extract the posted date from the detail page.

        <time id="jobdetails-jobpostdate" datetime="2026-05-29">May 29, 2026</time>
        """
        el = soup.select_one(self.DETAIL_POSTED_DATE_SELECTOR)

        if not el:
            return ""

        # Prefer the datetime attribute (ISO format)
        datetime_attr = el.get("datetime")
        if datetime_attr:
            return str(datetime_attr).strip()

        # Fallback to text content
        return self._clean_text(el.get_text())

    def _extract_role_number_from_detail(self, soup) -> str:
        """Extract the role number from the detail page.

        <strong id="jobdetails-jobnumber" itemprop="identifier">200663815-1052</strong>
        """
        el = soup.select_one(self.DETAIL_ROLE_NUMBER_SELECTOR)

        if not el:
            return ""

        return self._clean_text(el.get_text())

    def _extract_location_from_detail(self, soup) -> str:
        """Extract location from the detail page if available.

        Apple detail pages may have location info in the summary section
        or in a dedicated location element. Check several possible selectors.
        """
        # Check for location in the summary section
        summary = soup.select_one(self.DETAIL_SUMMARY_SELECTOR)
        if summary:
            # Look for a "Location" label in the summary aside
            for div in summary.select('div.t-body-reduced-tight'):
                text = self._clean_text(div.get_text())
                if text.lower().startswith("location"):
                    # Extract the value part after "Location"
                    parts = text.split(":", 1)
                    if len(parts) > 1:
                        loc = parts[1].strip()
                        if loc:
                            return loc
                    # Maybe the strong element has the location
                    strong = div.select_one("strong")
                    if strong:
                        loc = self._clean_text(strong.get_text())
                        if loc:
                            return loc

        return ""

    def _extract_description(self, soup) -> str:
        """Extract full description from the detail page.

        Combines: Summary, Description, Responsibilities,
        Minimum Qualifications, and Preferred Qualifications.
        """
        sections: list[str] = []

        # 1. Summary
        summary = soup.select_one(self.DETAIL_SUMMARY_SELECTOR)
        if summary:
            text = self._clean_multiline_text(self._get_section_text(summary))
            if text:
                sections.append(f"Summary:\n{text}")

        # 2. Description
        desc = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)
        if desc:
            text = self._clean_multiline_text(self._get_section_text(desc))
            if text:
                sections.append(f"Description:\n{text}")

        # 3. Responsibilities
        resp = soup.select_one(self.DETAIL_RESPONSIBILITIES_SELECTOR)
        if resp:
            text = self._clean_multiline_text(self._get_section_text(resp))
            if text:
                sections.append(f"Responsibilities:\n{text}")

        # 4. Minimum Qualifications
        min_qual = soup.select_one(self.DETAIL_MIN_QUAL_SELECTOR)
        if min_qual:
            text = self._clean_multiline_text(self._get_section_text(min_qual))
            if text:
                sections.append(f"Minimum Qualifications:\n{text}")

        # 5. Preferred Qualifications
        pref_qual = soup.select_one(self.DETAIL_PREF_QUAL_SELECTOR)
        if pref_qual:
            text = self._clean_multiline_text(self._get_section_text(pref_qual))
            if text:
                sections.append(f"Preferred Qualifications:\n{text}")

        return "\n\n".join(sections)

    @staticmethod
    def _get_section_text(section: Tag) -> str:
        """Get clean text from a detail section, handling the two-column layout.

        Each section has:
          aside.column.large-3 → heading (h2)
          div.column.large-9   → content

        We strip the heading from the combined text to avoid duplication
        since we add our own section labels.
        """
        # Remove script/style tags
        for unwanted in section.select("script, style, noscript"):
            unwanted.decompose()

        # Get the content column
        content_col = section.select_one("div.column.large-9")
        if content_col:
            return content_col.get_text(separator="\n")

        # Fallback to the whole section
        return section.get_text(separator="\n")

    # ------------------------------------------------------------------
    # Fallback
    # ------------------------------------------------------------------

    async def _fallback_links(self, page: Page, source_url: str, max_jobs: int) -> list[Job]:
        """Fallback: extract jobs from buttons with aria-label when card selectors fail."""
        soup = await self._get_soup(page)

        jobs: list[Job] = []
        seen_job_ids: set[str] = set()

        for button in soup.select(self.TITLE_BUTTON_SELECTOR):
            if len(jobs) >= max_jobs:
                break

            aria_label = button.get("aria-label")
            if not aria_label:
                continue

            title = self._clean_text(str(aria_label).replace("Role description:", ""))

            if not title:
                continue

            # Try to find the job ID from sibling elements
            parent_item = button.find_parent("li", class_="rc-accordion-item")
            job_id = ""
            if parent_item:
                job_id = self._extract_job_id_from_card(parent_item)

            if not job_id:
                continue

            if job_id in seen_job_ids:
                continue

            seen_job_ids.add(job_id)

            url = self._make_apple_job_url(source_url, job_id)

            jobs.append(Job(
                job_id=job_id,
                company=self.company_config.get("name", "Apple"),
                title=title,
                location=self.DEFAULT_LOCATION,
                url=url,
                source_url=source_url,
                posted_date=None,
                description=None,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                extracted_experience_parts="",
            ))

        return jobs

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------

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
            clean_line = " ".join(line.split()).strip()
            if clean_line:
                lines.append(clean_line)
        return "\n".join(lines).strip()

    @staticmethod
    def _dedupe_preserve_order(values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            normalized = value.lower().strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(value)
        return result
