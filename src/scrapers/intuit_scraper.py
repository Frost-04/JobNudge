from __future__ import annotations

from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class IntuitScraper(BaseScraper):
    """
    Scraper for Intuit Careers job search pages.

    Intuit's job board at jobs.intuit.com uses server-rendered HTML with
    AJAX-powered faceted filters.  Changing category/country checkboxes
    triggers an ambient AJAX search that re-renders the results list
    without changing the URL.  Therefore this scraper uses Playwright
    interactions to tick the required filter checkboxes before parsing.

    Filter section (``#refined-search``):

        section[data-filter-id="1"]  → Job Category checkboxes
        section[data-filter-id="2"]  → Country checkboxes

    Each checkbox is:

        <input class="filter-checkbox" data-display="Software Engineering"
               data-facet-type="1" ...>

    Results list:

        <section id="search-results-list">
          <ul class="search-list">
            <li data-intuit-jobid="22281" data-category="Software Engineering">
              <a href="/job/bengaluru/..." class="sr-item">
                <h2>Staff Software Engineer</h2>
                <span class="job-location">Bangalore, India</span>
              </a>
            </li>

    Detail page:

        <div id="job-team">
          <h2>Job Overview</h2>
          <p>...</p>
          <p class="Responsibilities"><strong>Responsibilities</strong></p>
          <ul>...</ul>
          <p class="Qualifications"><strong>Qualifications</strong></p>
          <ul>...</ul>
        </div>
    """

    # ---- Filter selectors ----
    RESULTS_CONTAINER = "section#search-results-list"
    RESULTS_LIST = "ul.search-list"
    CARD_SELECTOR = "li[data-intuit-jobid]"
    JOB_TITLE_SELECTOR = "h2"
    JOB_LOCATION_SELECTOR = "span.job-location"
    JOB_LINK_SELECTOR = "a.sr-item"

    FILTER_CATEGORY_SECTION = 'section[data-filter-id="1"]'
    FILTER_COUNTRY_SECTION = 'section[data-filter-id="2"]'
    FILTER_CHECKBOX = "input.filter-checkbox"
    FILTER_EXPAND_BUTTON = "button.expandable-parent"

    # ---- Detail page selectors ----
    DETAIL_CONTENT_SELECTOR = "div#job-team"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        # ---- Read optional filter config from companies.yaml ----
        filter_category = self.company_config.get("filter_category", "Software Engineering")
        filter_country = self.company_config.get("filter_country", "India")

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # ---- Step 1: Apply filters via checkboxes ----
            await self._apply_filter(page, filter_category, self.FILTER_CATEGORY_SECTION)
            await self._apply_filter(page, filter_country, self.FILTER_COUNTRY_SECTION)

            # ---- Step 2: Wait for AJAX results to render ----
            await self._wait_for_results(page)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)
            if not cards:
                self.logger.warning("No Intuit job cards found after filtering.")
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

                # ---- Step 3: Enrich with detail page ----
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
                            matched_keywords=[],
                        )
                except Exception as exc:
                    self.logger.warning(
                        "Failed to enrich Intuit job detail %s: %s",
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
    # Filter interaction
    # ------------------------------------------------------------------

    async def _apply_filter(
        self, page: Page, filter_name: str, section_selector: str
    ) -> None:
        """
        Click a filter checkbox by its ``data-display`` text.

        If the section is collapsed, expand it first by clicking the
        toggle button.  Then find the checkbox whose label matches
        ``filter_name`` and click it only if not already checked.
        """
        # Ensure the filter section is expanded.
        section = page.locator(section_selector)
        if not await section.count():
            self.logger.warning("Filter section not found: %s", section_selector)
            return

        toggle = section.locator(self.FILTER_EXPAND_BUTTON)
        if await toggle.count():
            expanded = await toggle.get_attribute("aria-expanded")
            if expanded != "true":
                await toggle.click()
                await page.wait_for_timeout(500)

        # Build a Playwright locator for the checkbox.
        # The label contains the filter display name in a span.filter__facet-name.
        checkbox_row = section.locator("li").filter(
            has=page.locator(f'span.filter__facet-name:text-is("{filter_name}")')
        )
        if not await checkbox_row.count():
            self.logger.warning(
                "Filter '%s' not found in section %s", filter_name, section_selector
            )
            return

        checkbox = checkbox_row.locator("input.filter-checkbox")
        if not await checkbox.count():
            return

        is_checked = await checkbox.is_checked()
        if not is_checked:
            await checkbox.check()
            # Wait for the ambient AJAX search to complete and re-render results.
            await page.wait_for_timeout(2000)

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        # Job ID from data-intuit-jobid attribute.
        job_id = str(card.get("data-intuit-jobid", "")).strip()

        # Link and title.
        link_el = card.select_one(self.JOB_LINK_SELECTOR)
        if not link_el:
            return None

        href = link_el.get("href")
        if not href:
            return None

        url = make_absolute_url(source_url, str(href))

        title_el = card.select_one(self.JOB_TITLE_SELECTOR)
        title = self._clean_text(title_el.get_text()) if title_el else ""

        if not title:
            return None

        # Location.
        location_el = card.select_one(self.JOB_LOCATION_SELECTOR)
        location = self._clean_text(location_el.get_text()) if location_el else ""

        if not job_id:
            job_id = extract_job_id(url)

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Intuit"),
            title=title,
            location=location,
            url=url,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            matched_keywords=[],
        )

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def _get_detail_page(self) -> Page:
        """Return a new page, creating a fresh context if needed."""
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

            # Wait for the job detail content to load.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_CONTENT_SELECTOR,
                    timeout=15000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            job_team = soup.select_one(self.DETAIL_CONTENT_SELECTOR)
            if not job_team:
                return ""

            return self._extract_description(job_team)

        finally:
            await detail_page.close()

    def _extract_description(self, container: Tag) -> str:
        """
        Extract clean description text from ``#job-team``.

        Preserves structure by keeping heading-like elements (h2, strong)
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

            # Headings and strong-text paragraphs act as section dividers.
            if tag_name in ("h2", "h3", "h4"):
                if current_lines:
                    sections.append(" ".join(current_lines))
                    current_lines = []
                heading = self._clean_text(child.get_text())
                if heading:
                    sections.append(heading)

            elif tag_name == "strong":
                parent_tag = child.parent.name if child.parent and hasattr(child.parent, "name") else ""
                # <p class="Responsibilities"><strong>Responsibilities</strong></p>
                # Treat these as section headers.
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
        """Wait for the job results list or first card to appear after filtering."""
        selectors = [
            f"{self.RESULTS_CONTAINER} {self.CARD_SELECTOR}",
            self.RESULTS_CONTAINER,
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
