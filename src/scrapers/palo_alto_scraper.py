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


class PaloAltoScraper(BaseScraper):
    """
    Scraper for Palo Alto Networks Careers job search pages.

    Palo Alto Networks uses the same job-board platform as Intuit
    (search-jobs pattern) with server-rendered HTML and AJAX-powered
    faceted filters.  Filter checkboxes and sort dropdown trigger
    ambient AJAX re-renders without changing the URL.

    The page has a cookie/alert popup (``#system-ialert``) that must
    be dismissed before interacting with filter controls.

    Filter sections:
        section#category-filters-section  → Department checkboxes
        section#country-filters-section   → Country checkboxes

    Sort dropdown:
        select.section29__sort-wrapper-select
            option[value="1"]  → Date Posted

    Results list:
        section#search-results-list
          ul.section29__search-results-ul
            li.section29__search-results-li
              a.section29__search-results-link[data-job-id]
                h2.section29__search-results-job-title  → title
                span.section29__result-location         → location
                span.section29__result-category         → category

    Detail page:
        div.ats-description                            → full description
    """

    # ---- Popup / intercept ----
    POPUP_SELECTORS = [
        "div.system-ialert-close-button",
        "div.system-ialert-remove-button",
        "button#system-ialert-close-button",
    ]

    # ---- Filter selectors ----
    FILTER_DEPARTMENT_SECTION = 'section#category-filters-section'
    FILTER_COUNTRY_SECTION = 'section#country-filters-section'
    FILTER_EXPAND_BUTTON = "button.expandable-parent"
    FILTER_CHECKBOX = "input.filter-checkbox"
    FILTER_FACET_NAME = "span.filter__facet-name"

    FILTER_DEPARTMENT = "Product Engineering"
    FILTER_COUNTRY = "India"

    # ---- Sort dropdown ----
    SORT_DROPDOWN = "select.section29__sort-wrapper-select"
    SORT_VALUE_DATE_POSTED = "1"

    # ---- Results selectors ----
    RESULTS_CONTAINER = "section#search-results-list"
    CARD_SELECTOR = "li.section29__search-results-li"
    LINK_SELECTOR = "a.section29__search-results-link"
    TITLE_SELECTOR = "h2.section29__search-results-job-title"
    LOCATION_SELECTOR = "span.section29__result-location"

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = "div.ats-description"
    DETAIL_FALLBACK_SELECTORS = [
        "div.ats-description",
        "section[class*='job-description']",
        "div[class*='description']",
    ]

    # ---- Multi-location support ----
    TARGET_LOCATIONS = ["Bengaluru", "Hyderabad", "Pune"]

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        all_jobs: list[Job] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        try:
            for location in self.TARGET_LOCATIONS:
                location_jobs = await self._scrape_for_location(
                    page, location, source_url, max_jobs, seen_ids, seen_urls
                )
                all_jobs.extend(location_jobs)

                if len(all_jobs) >= max_jobs:
                    break

            return all_jobs[:max_jobs]

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Per-location scrape
    # ------------------------------------------------------------------

    async def _scrape_for_location(
        self,
        page: Page,
        location: str,
        source_url: str,
        max_jobs: int,
        seen_ids: set[str],
        seen_urls: set[str],
    ) -> list[Job]:
        jobs: list[Job] = []

        await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

        # ---- Dismiss cookie popup ----
        await self._dismiss_popups(page)

        # ---- Apply Department filter ----
        await self._apply_filter(page, self.FILTER_DEPARTMENT, self.FILTER_DEPARTMENT_SECTION)

        # ---- Apply Country filter ----
        await self._apply_filter(page, self.FILTER_COUNTRY, self.FILTER_COUNTRY_SECTION)

        # ---- Apply City filter for this location ----
        await self._apply_city_filter(page, location)

        # ---- Set sort to Date Posted ----
        await self._apply_sort(page)

        # ---- Wait for results ----
        await self._wait_for_results(page)

        soup = await self._get_soup(page)

        cards = soup.select(self.CARD_SELECTOR)
        if not cards:
            self.logger.warning(
                "No Palo Alto job cards found for location '%s'.", location
            )
            return jobs

        for card in cards:
            if len(jobs) + len(seen_urls) >= max_jobs:
                break

            job = self._parse_card(card, source_url)
            if not job:
                continue

            if job.job_id and job.job_id in seen_ids:
                continue
            if job.url in seen_urls:
                continue

            # ---- Enrich with detail page ----
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
                        "Failed to enrich Palo Alto job detail %s: %s",
                        job.url,
                        exc,
                    )

            if job.job_id:
                seen_ids.add(job.job_id)
            seen_urls.add(job.url)
            jobs.append(job)

        return jobs

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
    # Filter interactions
    # ------------------------------------------------------------------

    async def _apply_filter(
        self, page: Page, filter_name: str, section_selector: str
    ) -> None:
        """Click a filter checkbox by its ``data-display`` text."""
        section = page.locator(section_selector)
        if not await section.count():
            self.logger.warning("Filter section not found: %s", section_selector)
            return

        # Expand the section if collapsed
        toggle = section.locator(self.FILTER_EXPAND_BUTTON)
        if await toggle.count():
            expanded = await toggle.get_attribute("aria-expanded")
            if expanded != "true":
                try:
                    await toggle.evaluate("el => el.click()")
                except Exception:
                    pass
                await page.wait_for_timeout(500)

        # Find the checkbox for the given filter name and click its label
        try:
            await page.evaluate(
                """(data) => {
                    const inputs = document.querySelectorAll('input.filter-checkbox');
                    for (const inp of inputs) {
                        if (inp.getAttribute('data-display') === data.name) {
                            const label = inp.closest('label');
                            if (label) {
                                label.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }""",
                {"name": filter_name},
            )
            await page.wait_for_timeout(2000)
        except Exception as exc:
            self.logger.warning(
                "Could not click filter '%s': %s", filter_name, exc
            )

    async def _apply_city_filter(self, page: Page, city: str) -> None:
        """Click the City filter checkbox for a specific city."""
        city_section = 'section#city-filters-section'
        section = page.locator(city_section)
        if not await section.count():
            return

        # Expand if needed
        toggle = section.locator(self.FILTER_EXPAND_BUTTON)
        if await toggle.count():
            expanded = await toggle.get_attribute("aria-expanded")
            if expanded != "true":
                try:
                    await toggle.evaluate("el => el.click()")
                except Exception:
                    pass
                await page.wait_for_timeout(500)

        try:
            await page.evaluate(
                """(city) => {
                    const inputs = document.querySelectorAll(
                        'section#city-filters-section input.filter-checkbox'
                    );
                    for (const inp of inputs) {
                        const label = inp.closest('label');
                        if (!label) continue;
                        const nameSpan = label.querySelector('span.filter__facet-name');
                        if (nameSpan && nameSpan.textContent.trim() === city) {
                            label.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                city,
            )
            await page.wait_for_timeout(2000)
        except Exception as exc:
            self.logger.warning(
                "Could not click city filter '%s': %s", city, exc
            )

    async def _apply_sort(self, page: Page) -> None:
        """Select 'Date Posted' in the sort dropdown."""
        try:
            await page.evaluate("""
                const sel = document.querySelector('select.section29__sort-wrapper-select');
                if (sel) {
                    sel.value = '1';
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                }
            """)
            await page.wait_for_timeout(2000)
        except Exception as exc:
            self.logger.warning("Could not set sort to Date Posted: %s", exc)

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

        job_id = str(link_el.get("data-job-id", "")).strip()

        title_el = card.select_one(self.TITLE_SELECTOR)
        title = self._clean_text(title_el.get_text()) if title_el else ""
        if not title:
            return None

        loc_el = card.select_one(self.LOCATION_SELECTOR)
        location = self._clean_text(loc_el.get_text()) if loc_el else ""

        if not job_id:
            job_id = extract_job_id(url)

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Palo Alto Networks"),
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
        if self.context:
            try:
                return await self.context.new_page()
            except Exception:
                self.logger.debug("Stale context, recreating.")
                await self.close_browser()
        return await self.new_page()

    async def _scrape_detail_page(self, job_url: str) -> str:
        if not job_url:
            return ""

        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)

            # Dismiss popups on detail page too
            await detail_page.evaluate("""
                document.querySelectorAll('.system-ialert, .system-ialert-css, [id*="ialert"]')
                    .forEach(el => el.remove());
            """)

            # Wait for description
            for sel in self.DETAIL_FALLBACK_SELECTORS:
                try:
                    await detail_page.wait_for_selector(sel, timeout=10000)
                    break
                except Exception:
                    continue

            soup = await self._get_soup(detail_page)
            return self._extract_description(soup)

        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        for sel in self.DETAIL_FALLBACK_SELECTORS:
            container = soup.select_one(sel)
            if container:
                # Remove scripts/styles
                for unwanted in container.select("script, style, noscript"):
                    unwanted.decompose()
                text = container.get_text(separator="\n")
                return self._clean_multiline_text(text)
        return ""

    # ------------------------------------------------------------------
    # Wait helpers
    # ------------------------------------------------------------------

    async def _wait_for_results(self, page: Page) -> None:
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

    # ------------------------------------------------------------------
    # Text utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        text = html.unescape(text or "").replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        text = html.unescape(text or "").replace("\xa0", " ")
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        deduped: list[str] = []
        for line in lines:
            if deduped and line == deduped[-1]:
                continue
            deduped.append(line)
        return "\n".join(deduped)
