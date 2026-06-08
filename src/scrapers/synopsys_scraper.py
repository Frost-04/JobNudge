from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class SynopsysScraper(BaseScraper):
    """
    Scraper for Synopsys Careers job search pages.

    Synopsys uses the Phenom People platform (same as Intuit / Optum /
    Palo Alto Networks / Target) with AJAX-powered faceted filters.
    Filter checkboxes and sort dropdown trigger ambient AJAX re-renders
    without changing the URL.

    Filter sections:

        section[data-filter-id="1"]          → Category checkboxes
        section[data-filter-id="5"]          → Sub Category checkboxes

    Each checkbox is:

        <input class="filter-checkbox" data-display="Engineering"
               data-facet-type="1" data-id="8675488" ...>

    Sort dropdown:

        <select data-search-results-sort-enhanced>
          <option value="13">Most Recent</option>

    Results list (Phenom People standard):

        <section id="search-results-list">
          <ul>
            <li>
              <a href="/job/..." data-job-id="...">
                <h2>Title</h2>
                <span class="job-location">Location</span>
              </a>
            </li>

    Detail page:

        <div class="ats-description">...</div>
    """

    # ---- Filter & sort selectors ----
    RESULTS_CONTAINER = "section#search-results-list"
    CARD_SELECTOR = "section#search-results-list > ul > li"
    LINK_SELECTOR = "a[data-job-id]"
    TITLE_SELECTOR = "h2"
    LOCATION_SELECTOR = "span.job-location"

    FILTER_CATEGORY_SECTION = 'section[data-filter-id="1"]'
    FILTER_COUNTRY_SECTION = 'section[data-filter-id="2"]'
    FILTER_SUBCATEGORY_SECTION = 'section[data-filter-id="5"]'
    FILTER_CHECKBOX = "input.filter-checkbox"
    FILTER_EXPAND_BUTTON = "button.expandable-parent"

    SORT_SELECT = "select[data-search-results-sort-enhanced]"

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = "div.ats-description"

    # ---- Popup / intercept ----
    POPUP_SELECTORS = [
        "div.system-ialert-close-button",
        "div.system-ialert-remove-button",
        "button#system-ialert-close-button",
    ]

    # ---- Filter configuration ----
    FILTER_CATEGORY = "Engineering"
    FILTER_COUNTRY = "India"

    FILTER_SUBCATEGORIES = [
        "AI",
        "Applications Engineering",
        "Dev Ops Engineering",
        "Machine Learning",
        "Quality Engineering",
        "Software Engineering",
        "Software Specialization",
    ]

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # ---- Step 0: Dismiss cookie / alert popups ----
            await self._dismiss_popups(page)

            # ---- Step 1: Apply "Engineering" category filter ----
            await self._apply_filter(
                page, self.FILTER_CATEGORY, self.FILTER_CATEGORY_SECTION
            )

            # ---- Step 2: Apply "India" country filter ----
            await self._apply_filter(
                page, self.FILTER_COUNTRY, self.FILTER_COUNTRY_SECTION
            )

            # ---- Step 3: Apply Sub Category filters ----
            for subcategory in self.FILTER_SUBCATEGORIES:
                await self._apply_filter(
                    page, subcategory, self.FILTER_SUBCATEGORY_SECTION
                )

            # ---- Step 4: Sort by "Most Recent" ----
            await self._select_sort_by(page, "Most Recent")

            # ---- Step 5: Wait for AJAX results ----
            await self._wait_for_results(page)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)
            if not cards:
                self.logger.warning("No Synopsys job cards found after filtering.")
                return jobs

            self.logger.info("Synopsys: %d cards found.", len(cards))

            for card in cards[:max_jobs]:
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_ids:
                    continue
                if job.url in seen_urls:
                    continue

                # ---- Enrich with detail page ----
                if self._should_exclude(job.title):
                    self.logger.debug(
                        "Skipping detail enrichment for: %s", job.title
                    )
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
                            "Failed to enrich Synopsys job detail %s: %s",
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
        Click a filter checkbox by its ``data-display`` attribute.

        Expands the filter section first if collapsed, then finds the
        checkbox whose ``data-display`` matches ``filter_name`` and
        clicks it via JS (bypassing Playwright visibility checks that
        may fail due to popups or overlapping UI).
        """
        # Expand the filter section via JS click.
        await page.evaluate(
            """
            (sectionId) => {
                const section = document.querySelector(sectionId);
                if (!section) return;
                const toggle = section.querySelector('button.expandable-parent');
                if (toggle && toggle.getAttribute('aria-expanded') !== 'true') {
                    toggle.click();
                }
            }
            """,
            section_selector,
        )
        await page.wait_for_timeout(500)

        # Click the target checkbox via JS using data-display.
        await page.evaluate(
            """
            (filterName) => {
                const checkboxes = document.querySelectorAll('input.filter-checkbox');
                for (const cb of checkboxes) {
                    if (cb.getAttribute('data-display') === filterName) {
                        if (!cb.checked) {
                            cb.click();
                        }
                        break;
                    }
                }
            }
            """,
            filter_name,
        )
        # Wait for the ambient AJAX search to complete.
        await page.wait_for_timeout(2500)

    # ------------------------------------------------------------------
    # Sort interaction
    # ------------------------------------------------------------------

    async def _select_sort_by(self, page: Page, sort_label: str) -> None:
        """Select a sort option by its visible label text via JS."""
        await page.evaluate(
            """
            (label) => {
                const select = document.querySelector('select[data-search-results-sort-enhanced]');
                if (!select) return;
                const options = select.options;
                for (let i = 0; i < options.length; i++) {
                    if (options[i].textContent.trim() === label) {
                        select.value = options[i].value;
                        select.dispatchEvent(new Event('change', { bubbles: true }));
                        break;
                    }
                }
            }
            """,
            sort_label,
        )
        # Wait for the AJAX re-sort to complete.
        await page.wait_for_timeout(2500)

    # ------------------------------------------------------------------
    # Popup dismissal
    # ------------------------------------------------------------------

    async def _dismiss_popups(self, page: Page) -> None:
        """Dismiss cookie consent and alert popups via JS."""
        await page.evaluate("""
            document.querySelectorAll(
                '.system-ialert, .system-ialert-css, [id*="ialert"], [class*="cookie-consent"]'
            ).forEach(el => el.remove());
        """)
        await page.wait_for_timeout(500)

        # Also try clicking close buttons.
        for sel in self.POPUP_SELECTORS:
            try:
                btn = page.locator(sel)
                if await btn.count():
                    await btn.evaluate("el => el.click()")
                    await page.wait_for_timeout(500)
                    break
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link_el = card.select_one(self.LINK_SELECTOR)
        if not link_el:
            return None

        href = link_el.get("href")
        if not href:
            return None

        url = make_absolute_url(source_url, str(href))

        # Job ID from data-job-id attribute.
        job_id = str(link_el.get("data-job-id", "")).strip()

        # Title from <h2>.
        title_el = card.select_one(self.TITLE_SELECTOR)
        title = self._clean_text(title_el.get_text()) if title_el else ""
        if not title:
            return None

        # Location from span.job-location.
        location_el = card.select_one(self.LOCATION_SELECTOR)
        location = self._clean_text(location_el.get_text()) if location_el else ""

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Synopsys"),
            title=title,
            location=location,
            url=url,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
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

            # Wait for the description to load.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_DESCRIPTION_SELECTOR, timeout=15000
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            desc_el = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)
            if not desc_el:
                return ""

            return self._extract_description(desc_el)

        finally:
            await detail_page.close()

    def _extract_description(self, container: Tag) -> str:
        """
        Extract clean description text from the ats-description div.

        Preserves structure by treating strong/p/b tags as section markers
        and collecting paragraph / list content beneath them.
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
            if tag_name in ("h2", "h3", "h4"):
                if current_lines:
                    sections.append(" ".join(current_lines))
                    current_lines = []
                heading = self._clean_text(child.get_text())
                if heading:
                    sections.append(heading)

            elif tag_name in ("strong", "b"):
                parent_tag = (
                    child.parent.name
                    if child.parent and hasattr(child.parent, "name")
                    else ""
                )
                # <p><strong>Responsibilities</strong></p> → section header.
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
        """Wait for the job results list to appear after filtering/sorting."""
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
