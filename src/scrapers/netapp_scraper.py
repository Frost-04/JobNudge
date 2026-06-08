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


class NetAppScraper(BaseScraper):
    """
    Scraper for NetApp Careers search-jobs page.

    NetApp's job board at careers.netapp.com uses server-rendered HTML with
    AJAX-powered checkbox filters.  Checking category/country boxes triggers
    an AJAX search that re-renders the results list without changing the URL.
    This scraper uses Playwright to tick the required checkboxes before parsing.

    Filter sections:

        section#category-filters-section[data-filter-id="1"]  → Job Category
        section#country-filters-section[data-filter-id="2"]   → Country

    Each checkbox is:

        <input class="filter-checkbox" data-display="Engineering"
               data-facet-type="1" ...>

    Results list:

        <div class="search-results-list-wrapper">
          <ul>
            <li>
              <a href="/job/bengaluru/..." data-job-id="96026199936">
                <h3>Business Analyst (Power BI)</h3>
                <span class="job-location job-default">Bengaluru, Karnataka, India</span>
              </a>
            </li>

    Detail page:

        <div class="ats-description">
          <p><b>Own Every Moment at NetApp</b></p>
          <div><h2>Job Summary</h2>...</div>
          <div><h2>Job Requirements</h2>...</div>
          <div><h2>Education</h2>...</div>
        </div>
    """

    # ---- Filter selectors ----
    FILTER_CATEGORY_SECTION = 'section#category-filters-section[data-filter-id="1"]'
    FILTER_COUNTRY_SECTION = 'section#country-filters-section[data-filter-id="2"]'
    FILTER_CHECKBOX = "input.filter-checkbox"
    FILTER_EXPAND_BUTTON = "button.expandable-parent"

    # ---- Results selectors ----
    RESULTS_LIST = "ul.search-results-list, div.search-results-list-wrapper ul"
    CARD_SELECTOR = "div.search-results-list-wrapper ul > li, ul.search-results-list > li"
    LINK_SELECTOR = "a[data-job-id]"
    TITLE_SELECTOR = "h3"
    LOCATION_SELECTOR = "span.job-location"

    # ---- Detail page selectors ----
    DETAIL_CONTENT_SELECTOR = "div.ats-description"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        filter_categories = self.company_config.get(
            "filter_categories",
            ["Engineering", "Information Technology", "Software Engineering", "Systems Engineering"],
        )
        filter_country = self.company_config.get("filter_country", "India")

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # ---- Step 1: Scroll to reveal filters (they render lazily) ----
            await page.evaluate("window.scrollTo(0, 300)")
            await page.wait_for_timeout(1000)

            # ---- Step 2: Apply filters via checkboxes ----
            for category in filter_categories:
                await self._apply_filter(page, category, self.FILTER_CATEGORY_SECTION)

            await self._apply_filter(page, filter_country, self.FILTER_COUNTRY_SECTION)

            # ---- Step 3: Wait for AJAX results ----
            await self._wait_for_results(page)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No NetApp job cards found after filtering.")
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
                            "Failed to enrich NetApp job detail %s: %s",
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
        """Click a filter checkbox by its ``data-display`` text.

        If the section is collapsed, expand it first.  Then find the checkbox
        whose label matches and check it using JavaScript if Playwright click
        is blocked by an overlay.
        """
        # Dismiss any blocking overlay (cookie banner, system alert, etc.).
        await self._dismiss_overlays(page)

        section = page.locator(section_selector)

        if not await section.count():
            self.logger.warning("Filter section not found: %s", section_selector)
            return

        # Expand the section if collapsed.
        toggle = section.locator(self.FILTER_EXPAND_BUTTON)

        if await toggle.count():
            expanded = await toggle.get_attribute("aria-expanded")

            if expanded != "true":
                await toggle.click(force=True)
                await page.wait_for_timeout(800)

        # Find the checkbox by its data-display attribute (label text).
        checkbox_locator = section.locator(
            f'input.filter-checkbox[data-display="{filter_name}"]'
        )

        if not await checkbox_locator.count():
            self.logger.warning(
                "Filter option not found: %s in %s",
                filter_name,
                section_selector,
            )
            return

        is_checked = await checkbox_locator.is_checked()

        if is_checked:
            self.logger.debug("Filter already active: %s", filter_name)
            return

        # Some checkboxes are hidden behind other elements even when the
        # section is expanded.  Use JavaScript dispatchEvent as a fallback.
        try:
            await checkbox_locator.check(force=True, timeout=5000)
        except Exception:
            await checkbox_locator.dispatch_event("click")

        self.logger.debug("Filter activated: %s", filter_name)
        await page.wait_for_timeout(1500)

    async def _dismiss_overlays(self, page: Page) -> None:
        """Dismiss cookie banners and system alerts that intercept clicks."""
        dismiss_selectors = [
            "#system-ialert",
            "button[id*='close']",
            "button[aria-label*='Close']",
            "button[aria-label*='Dismiss']",
        ]

        for selector in dismiss_selectors:
            try:
                overlay = page.locator(selector).first

                if await overlay.count() and await overlay.is_visible():
                    await overlay.click(force=True, timeout=3000)
                    await page.wait_for_timeout(300)
            except Exception:
                pass

    async def _wait_for_results(self, page: Page) -> None:
        """Wait for AJAX results to populate after filter interactions."""
        try:
            await page.wait_for_selector(self.LINK_SELECTOR, timeout=15000)
        except Exception:
            # Some pages render differently; try alternative selectors.
            pass

        await page.wait_for_timeout(1000)

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card)
        title = self._extract_title(card)
        job_id = self._extract_job_id_from_card(card)
        location = self._extract_location(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "NetApp"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag) -> str:
        el = card.select_one(self.LINK_SELECTOR)

        if not el:
            return ""

        href = el.get("href")
        href_str = str(href) if href else ""

        if not href_str:
            return ""

        return self._make_netapp_job_url(href_str)

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_location(self, card: Tag) -> str:
        el = card.select_one(self.LOCATION_SELECTOR)

        if not el:
            return ""

        return self._clean_location_text(el.get_text())

    def _extract_job_id_from_card(self, card: Tag) -> str:
        """NetApp cards have data-job-id on the <a> element."""
        el = card.select_one(self.LINK_SELECTOR)

        if el:
            job_id_attr = el.get("data-job-id")

            if job_id_attr:
                return str(job_id_attr)

        return ""

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def _get_detail_page(self) -> Page:
        if self.context:
            try:
                return await self.context.new_page()
            except Exception:
                self.logger.debug(
                    "Shared browser context is no longer usable; discarding "
                    "and creating a fresh one."
                )
                await self.close_browser()

        return await self.new_page()

    async def _scrape_detail_page(self, job_url: str) -> str:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)

            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_CONTENT_SELECTOR,
                    timeout=15000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            return self._extract_description(soup)

        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        container = soup.select_one(self.DETAIL_CONTENT_SELECTOR)

        if not container:
            return ""

        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        # Remove Apply/Save buttons row.
        buttons = container.select_one("div.job-description__buttons")

        if buttons:
            buttons.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    # ------------------------------------------------------------------
    # URL construction
    # ------------------------------------------------------------------

    def _make_netapp_job_url(self, href: str) -> str:
        href = html.unescape(href).strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        origin = "https://careers.netapp.com"

        if href.startswith("/job/"):
            return f"{origin}{href}"

        if href.startswith("job/"):
            return f"{origin}/{href}"

        return f"{origin}/{href.lstrip('/')}"

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------

    def _clean_location_text(self, text: str) -> str:
        text = self._clean_text(text)

        if not text:
            return ""

        # NetApp locations can have semicolons separating multiple entries:
        # "Singapore; Singapore, Singapore, Singapore; Mumbai, Maharashtra, India"
        # Replace semicolons with commas for consistent formatting.
        text = text.replace(";", ",")
        text = re.sub(r",\s*,", ",", text)

        lower_text = text.lower()

        noise_values = {
            "location",
            "locations",
            "remote",
        }

        if lower_text in noise_values:
            return ""

        return text

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        text = text.replace("\xa0", " ")
        text = text.replace("&amp;", "&")
        text = re.sub(r"\s+", " ", text)

        return text.strip()

    def _clean_multiline_text(self, text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        text = text.replace("\xa0", " ")
        text = text.replace("&amp;", "&")

        lines = []
        for line in text.splitlines():
            clean_line = self._clean_text(line)

            if clean_line:
                lines.append(clean_line)

        return "\n".join(lines).strip()
