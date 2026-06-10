from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class UiPathScraper(BaseScraper):
    """
    Scraper for UiPath Careers job search page.

    The page at https://www.uipath.com/careers/jobs uses server-rendered HTML
    with a keyword search input and a department dropdown that filter results
    in-place (page reload or AJAX).  The scraper types "India" into the search
    field and selects "Engineering" from the dropdown before parsing.

    Search input:
        <input type="text" class="search-record" id="searchRecord"
               placeholder="Explore roles by keyword, location, or team ...">

    Department dropdown:
        <select name="department" id="department">
          <option value="Engineering">Engineering</option>
          ...

    Job cards:
        <div class="job-list-item">        (skip .heading variant)
            <a href="/careers/jobs/data-scientist/r14757"></a>
            <div class="job-info">
                <div class="column job-title">Data Scientist</div>
                <div class="column reqid"><span>Requisition ID:</span> R14757</div>
                <div class="column department"><span>Department:</span> Engineering</div>
                <div class="column location"><span>Location: </span> India, Jaipur</div>
                <div class="column workplace"><span>Workplace Type: </span> On-site</div>
            </div>
        </div>

    Detail page (opens on click — new page navigation):
        <div class="job-desc" id="jobOverview">
          <h1>Life at UiPath</h1>
          <p>...</p>
          ...
        </div>

    Job IDs are extracted from the URL path:
        /careers/jobs/data-scientist/r14757  →  R14757
    """

    # ---- Page interaction selectors ----
    SEARCH_INPUT = "input#searchRecord"
    DEPARTMENT_DROPDOWN = "select#department"

    # ---- Card selectors ----
    CARD_SELECTOR = "div.job-list-item:not(.heading)"
    TITLE_SELECTOR = "div.column.job-title"
    REQID_SELECTOR = "div.column.reqid"
    LOCATION_SELECTOR = "div.column.location"
    LINK_SELECTOR = "a[href^='/careers/jobs/']"

    # ---- Detail page selectors ----
    DETAIL_CONTENT_SELECTOR = "div#jobOverview"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        # Read optional filter config from companies.yaml
        search_keyword = self.company_config.get("search_keyword", "India")
        filter_department = self.company_config.get("filter_department", "Engineering")

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # ---- Step 1: Select department first (may trigger page reload) ----
            await self._apply_department_filter(page, filter_department)

            # ---- Step 2: Type search keyword ----
            await self._apply_search(page, search_keyword)

            # ---- Step 3: Wait for filtered results to render ----
            await self._wait_for_results(page)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)
            if not cards:
                self.logger.warning("No UiPath job cards found after filtering.")
                return jobs

            seen_ids: set[str] = set()
            seen_urls: set[str] = set()

            for card in cards[:max_jobs]:
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_ids:
                    continue
                if job.url in seen_urls:
                    continue

                # ---- Step 4: Enrich with detail page ----
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
                else:
                    try:
                        detail_desc = await self._scrape_detail_page(job.url)
                        if detail_desc:
                            job = Job(
                                job_id=job.job_id,
                                company=job.company,
                                title=job.title,
                                location=job.location,
                                url=job.url,
                                source_url=job.source_url,
                                posted_date=job.posted_date,
                                description=detail_desc,
                                scraped_at=datetime.now(timezone.utc).isoformat(),
                                extracted_experience_parts="",
                            )
                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich UiPath job detail %s: %s",
                            job.url,
                            exc,
                        )

                if job.job_id:
                    seen_ids.add(job.job_id)
                seen_urls.add(job.url)
                jobs.append(job)

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Filter interactions
    # ------------------------------------------------------------------

    async def _apply_search(self, page: Page, keyword: str) -> None:
        """Type the search keyword into the #searchRecord input and press Enter to trigger filtering."""
        search_input = page.locator(self.SEARCH_INPUT)
        if not await search_input.count():
            self.logger.warning("Search input #searchRecord not found.")
            return

        await search_input.fill(keyword)
        # Press Enter to trigger the search/filter action
        await search_input.press("Enter")
        # Wait for the page to reload or AJAX results to render.
        await page.wait_for_timeout(3000)

    async def _apply_department_filter(self, page: Page, department: str) -> None:
        """Select the department from the #department dropdown."""
        dropdown = page.locator(self.DEPARTMENT_DROPDOWN)
        if not await dropdown.count():
            self.logger.warning("Department dropdown #department not found.")
            return

        await dropdown.select_option(label=department)
        # Wait for the filtered results to render (page may reload or AJAX).
        await page.wait_for_timeout(3000)

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        # Link from the overlay anchor.
        link_el = card.select_one(self.LINK_SELECTOR)
        if not link_el:
            return None

        href = link_el.get("href")
        if not href:
            return None

        url = make_absolute_url(source_url, str(href))

        # Title from .column.job-title.
        title_el = card.select_one(self.TITLE_SELECTOR)
        title = self._clean_text(title_el.get_text()) if title_el else ""

        if not title:
            return None

        # Job ID from the URL path: /careers/jobs/data-scientist/r14757 → R14757
        job_id = self._extract_uipath_job_id(str(href))
        if not job_id:
            # Fallback: try reqid column text
            reqid_el = card.select_one(self.REQID_SELECTOR)
            if reqid_el:
                reqid_text = self._clean_text(reqid_el.get_text())
                job_id = self._strip_label(reqid_text, "Requisition ID")

        # Location from .column.location, stripping "Location:" label.
        location_el = card.select_one(self.LOCATION_SELECTOR)
        location = ""
        if location_el:
            loc_text = self._clean_text(location_el.get_text())
            location = self._strip_label(loc_text, "Location")
            location = self._clean_location_text(location)

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "UiPath"),
            title=title,
            location=location,
            url=url,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    @staticmethod
    def _extract_uipath_job_id(href: str) -> str:
        """Extract job ID from UiPath URL paths like /careers/jobs/data-scientist/r14757."""
        match = re.search(r"/careers/jobs/[^/]+/([rR]\d+)", href)
        if match:
            return match.group(1).upper()
        return ""

    @staticmethod
    def _strip_label(text: str, label: str) -> str:
        """Remove a label prefix like 'Requisition ID:' or 'Location:' from text."""
        pattern = re.compile(rf"^\s*{re.escape(label)}\s*:?\s*", re.IGNORECASE)
        return pattern.sub("", text).strip()

    @staticmethod
    def _clean_location_text(location: str) -> str:
        """Filter out noise from location text."""
        if not location:
            return ""
        # Remove department suffixes like "Bangalore - Engineering" → "Bangalore"
        location = re.sub(r"\s*-\s*Engineering\s*$", "", location, flags=re.IGNORECASE).strip()
        # Remove trailing "Remote" if it's standalone noise
        if location.lower() in ("remote", "location", "locations", ""):
            return ""
        return location

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

    async def _scrape_detail_page(self, job_url: str) -> str:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)

            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for the job overview content to load.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_CONTENT_SELECTOR,
                    timeout=15000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            job_overview = soup.select_one(self.DETAIL_CONTENT_SELECTOR)
            if not job_overview:
                return ""

            return self._extract_description(job_overview)

        finally:
            await detail_page.close()

    def _extract_description(self, container: Tag) -> str:
        """
        Extract clean description text from ``#jobOverview``.

        Preserves structure by keeping heading-like elements (h1, strong)
        as section markers and collecting paragraph / list content beneath.
        """
        # Remove script/style tags.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        sections: list[str] = []
        current_lines: list[str] = []

        for child in container.descendants:
            if not hasattr(child, "name"):
                continue

            tag_name = child.name

            # Headings act as section dividers.
            if tag_name in ("h1", "h2", "h3", "h4"):
                if current_lines:
                    sections.append(" ".join(current_lines))
                    current_lines = []
                heading = self._clean_text(child.get_text())
                if heading:
                    sections.append(heading)

            elif tag_name == "strong":
                parent_tag = child.parent.name if child.parent and hasattr(child.parent, "name") else ""
                # <p><strong>What you'll do at UiPath</strong></p> → section header
                if parent_tag == "p":
                    if current_lines:
                        sections.append(" ".join(current_lines))
                        current_lines = []
                    label = self._clean_text(child.get_text())
                    if label:
                        sections.append(label)

            elif tag_name in ("p", "li"):
                text = self._clean_text(child.get_text())
                if text:
                    current_lines.append(text)

        if current_lines:
            sections.append(" ".join(current_lines))

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _wait_for_results(self, page: Page) -> None:
        """Wait for job cards to appear after filtering."""
        selectors = [
            self.CARD_SELECTOR,
            "div#job-postings",
        ]
        timeout_ms = self._to_ms(
            self.settings.get("run", {}).get("page_load_timeout_seconds"),
            45000,
        )

        for selector in selectors:
            try:
                await page.wait_for_selector(selector, timeout=timeout_ms)
                return
            except Exception:
                continue

    @staticmethod
    def _clean_text(text: str) -> str:
        """Normalize whitespace in a string."""
        if not text:
            return ""
        return " ".join(text.split()).strip()
