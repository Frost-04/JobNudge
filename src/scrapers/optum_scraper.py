from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class OptumScraper(BaseScraper):
    """
    Scraper for Optum (UnitedHealth Group) Careers job search pages.

    Optum's job board at careers.unitedhealthgroup.com uses the Phenom People
    platform with AJAX-powered faceted filters.  Changing category checkboxes
    triggers an ambient AJAX search that re-renders the results list without
    changing the URL.  Therefore this scraper uses Playwright interactions to
    tick the "Technology" category checkbox and sort by "Date Posted" before
    parsing.

    Filter section (``#category-filters-section``):

        section[data-filter-id="1"]  → Job Category checkboxes

    Each checkbox is:

        <input class="filter-checkbox" data-display="Technology"
               data-facet-type="1" data-id="81405" ...>

    Sort dropdown:

        <select class="fa-select" data-search-results-sort-enhanced="true">
          <option value="7">Date Posted</option>

    Results list:

        <section id="search-results-list">
          <ul>
            <li>
              <a href="/job/pune/..." class="brand-facet brand-facet__optum"
                 data-job-id="95609137376">
                <div>
                  <h2>Technical Product Manager</h2>
                  <span class="job-id job-info">2363603</span>
                  <span class="job-location">Pune, Maharashtra</span>
                </div>
              </a>
            </li>

    Detail page:

        <div class="jd-wrapper">
          <p>Description content...</p>
          <ul>...</ul>
        </div>

    Pagination:

        <a class="next" href="/search-jobs/results?p=2">Next</a>
    """

    # ---- Filter & sort selectors ----
    RESULTS_CONTAINER = "section#search-results-list"
    CARD_SELECTOR = "#search-results-list > ul > li"
    JOB_LINK_SELECTOR = "a.brand-facet"
    TITLE_SELECTOR = "h2"
    JOB_ID_SPAN_SELECTOR = "span.job-id.job-info"
    LOCATION_SELECTOR = "span.job-location"

    FILTER_CATEGORY_SECTION = "section#category-filters-section"
    FILTER_CHECKBOX = "input.filter-checkbox"
    FILTER_EXPAND_BUTTON = "button.expandable-parent"

    SORT_SELECT = "select.fa-select[data-search-results-sort-enhanced]"

    PAGINATION_NEXT = "a.next:not(.disabled)"

    # ---- Detail page selectors ----
    DETAIL_CONTENT_SELECTOR = "div.jd-wrapper"

    # ---- Popup / intercept ----
    POPUP_SELECTORS = [
        "div.system-ialert-close-button",
        "div.system-ialert-remove-button",
        "button#system-ialert-close-button",
    ]

    # ------------------------------------------------------------------
    # Main scrape entry point
    # ------------------------------------------------------------------

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        filter_category = self.company_config.get("filter_category", "Technology")

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # ---- Step 0: Dismiss cookie consent popup ----
            await self._dismiss_popups(page)

            # ---- Step 1: Apply "Technology" category filter ----
            await self._apply_filter(page, filter_category, self.FILTER_CATEGORY_SECTION)

            # ---- Step 2: Sort by "Date Posted" ----
            await self._select_sort_by(page, "Date Posted")

            # ---- Step 3: Wait for AJAX results to render ----
            await self._wait_for_results(page)

            # ---- Step 4: Paginate through pages ----
            page_num = 1
            while len(jobs) < max_jobs:
                self.logger.info("Scraping Optum page %d", page_num)

                soup = await self._get_soup(page)

                cards = soup.select(self.CARD_SELECTOR)
                if not cards:
                    self.logger.warning(
                        "No Optum job cards found on page %d.", page_num
                    )
                    break

                for card in cards:
                    if len(jobs) >= max_jobs:
                        break

                    job = self._parse_card(card, source_url)

                    if not job:
                        continue

                    if job.job_id and job.job_id in seen_ids:
                        continue
                    if job.url in seen_urls:
                        continue

                    # ---- Enrich with detail page description ----
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
                                "Failed to enrich Optum job detail %s: %s",
                                job.url,
                                exc,
                            )

                    if job.job_id:
                        seen_ids.add(job.job_id)
                    seen_urls.add(job.url)
                    jobs.append(job)

                # ---- Navigate to next page ----
                if len(jobs) >= max_jobs:
                    break

                # Re-dismiss popups (they may have reappeared).
                await self._dismiss_popups(page)

                # Use JS click to bypass popup interception.
                has_next = await page.evaluate(
                    "() => { const btn = document.querySelector('a.next:not(.disabled)'); return !!btn; }"
                )
                if not has_next:
                    break

                try:
                    await page.evaluate(
                        "() => { const btn = document.querySelector('a.next:not(.disabled)'); if (btn) btn.click(); }"
                    )
                    await page.wait_for_timeout(2500)
                    await self._wait_for_results(page)
                    page_num += 1
                except Exception as exc:
                    self.logger.warning(
                        "Failed to navigate to next Optum page: %s", exc
                    )
                    break

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

        Uses ``page.evaluate`` to click the checkbox label directly,
        bypassing Playwright visibility/interception checks that can
        fail due to popups or overlapping elements.
        """
        # Ensure the filter section is expanded via JS click.
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

        # Click the target checkbox via JS.
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
        # Wait for the ambient AJAX search to complete and re-render results.
        await page.wait_for_timeout(2500)

    # ------------------------------------------------------------------
    # Sort interaction
    # ------------------------------------------------------------------

    async def _select_sort_by(self, page: Page, sort_label: str) -> None:
        """Select a sort option by its visible label text via JS."""
        await page.evaluate(
            """
            (label) => {
                const select = document.querySelector('select.fa-select[data-search-results-sort-enhanced]');
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
            document.querySelectorAll('.system-ialert, .system-ialert-css, [id*="ialert"], [class*="cookie-consent"]')
                .forEach(el => el.remove());
        """)
        await page.wait_for_timeout(500)

        # Also try clicking close buttons
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
        # Find the job link element.
        link_el = card.select_one(self.JOB_LINK_SELECTOR)
        if not link_el:
            return None

        href = link_el.get("href")
        if not href:
            return None

        url = self._make_optum_job_url(source_url, str(href))

        # Title from <h2>.
        title_el = card.select_one(self.TITLE_SELECTOR)
        title = self._clean_text(title_el.get_text()) if title_el else ""
        if not title:
            return None

        # Job ID from data-job-id attribute on the <a> tag (preferred).
        job_id = ""
        data_job_id = link_el.get("data-job-id")
        if data_job_id:
            job_id = str(data_job_id).strip()

        # Fallback: req ID from span.job-id (e.g. "2363603").
        if not job_id:
            job_id_span = card.select_one(self.JOB_ID_SPAN_SELECTOR)
            if job_id_span:
                job_id = self._clean_text(job_id_span.get_text())

        # Fallback: extract from URL path.
        if not job_id:
            job_id = extract_job_id(url)

        # Location from span.job-location.
        location_el = card.select_one(self.LOCATION_SELECTOR)
        location = self._clean_text(location_el.get_text()) if location_el else ""

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Optum"),
            title=title,
            location=location,
            url=url,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _make_optum_job_url(self, source_url: str, href: str) -> str:
        """Construct an absolute job URL from a relative href."""
        if href.startswith("http"):
            return href

        parsed = urlparse(source_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        return base + href

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

            await detail_page.goto(
                job_url, wait_until="domcontentloaded", timeout=60000
            )

            # Wait for the job description wrapper to load.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_CONTENT_SELECTOR,
                    timeout=15000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            jd_wrapper = soup.select_one(self.DETAIL_CONTENT_SELECTOR)
            if not jd_wrapper:
                return ""

            return self._extract_description(jd_wrapper)

        finally:
            await detail_page.close()

    def _extract_description(self, container: Tag) -> str:
        """
        Extract clean description text from ``div.jd-wrapper``.

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

            # Headings act as section dividers.
            if tag_name in ("h2", "h3", "h4"):
                if current_lines:
                    sections.append(" ".join(current_lines))
                    current_lines = []
                heading = self._clean_text(child.get_text())
                if heading:
                    sections.append(heading)

            elif tag_name == "strong":
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
        """Wait for the job results list or first card to appear after filtering."""
        selectors = [
            f"{self.RESULTS_CONTAINER} {self.JOB_LINK_SELECTOR}",
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
