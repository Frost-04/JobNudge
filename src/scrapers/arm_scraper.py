from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class ArmScraper(BaseScraper):
    """
    Scraper for ARM Careers job search pages.

    ARM uses the Phenom People platform with AJAX-powered faceted
    filters.  Changing category/country checkboxes triggers an ambient
    AJAX re-render without changing the URL.  A sort dropdown controls
    result ordering.

    Filter sections:

        section#country-filters-section[data-filter-id="2"]
        section#category-filters-section[data-filter-id="1"]

    Each checkbox:

        <input class="filter-checkbox" data-display="India"
               data-facet-type="2" ...>

    Results list:

        <ul id="search-results-jobs">
          <li class="job-card ...">
            <a class="job-card__title" data-job-id="96199783792"
               href="/job/bengaluru/...">Senior DevOps Engineer</a>
            <span class="location">Bengaluru, India</span>
            <span class="category">Software Engineering</span>
          </li>

    Detail page:

        <div class="ats-description">
          <p>...</p>
          <h2>Key responsibilities include:</h2>
          <ul>...</ul>
        </div>
    """

    # ---- Card selectors ----
    RESULTS_LIST = "ul#search-results-jobs"
    CARD_SELECTOR = "ul#search-results-jobs li.job-card"
    TITLE_SELECTOR = "a.job-card__title"
    LOCATION_SELECTOR = "span.location"

    # ---- Filter selectors ----
    FILTER_EXPAND_BUTTON = "button.expandable-parent"
    FILTER_CHECKBOX = "input.filter-checkbox"
    SORT_DROPDOWN = 'select[data-search-results-sort-enhanced="true"]'

    # ---- Detail page selectors ----
    DETAIL_DESC_SELECTOR = "div.ats-description"

    # ---- Filter configuration ----
    FILTER_COUNTRY = "India"
    FILTER_CATEGORY = "Software Engineering"
    SORT_VALUE = "Date Posted"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # ---- Step 0: Dismiss cookie / alert / survey popups ----
            await self._dismiss_popups(page)

            # ---- Step 1: Apply Country filter (India) ----
            await self._click_filter_checkbox(
                page, self.FILTER_COUNTRY, 'section#country-filters-section'
            )

            # ---- Step 2: Apply Category filter (Software Engineering) ----
            await self._click_filter_checkbox(
                page, self.FILTER_CATEGORY, 'section#category-filters-section'
            )

            # ---- Step 3: Sort by Date Posted ----
            await self._select_sort(page, self.SORT_VALUE)

            # ---- Step 4: Wait for AJAX results ----
            await page.wait_for_timeout(4000)

            # Wait for job cards.
            try:
                await page.wait_for_selector(self.CARD_SELECTOR, timeout=15000)
            except Exception:
                pass

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)
            self.logger.info("ARM: %d cards found.", len(cards))

            if not cards:
                self.logger.warning("No ARM job cards found after filtering.")
                return jobs

            for card in cards[:max_jobs]:
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_ids:
                    continue
                if job.url in seen_urls:
                    continue

                # Enrich with detail page for non-excluded titles.
                if self._should_exclude(job.title):
                    self.logger.debug(
                        "Skipping detail enrichment for: %s", job.title
                    )
                else:
                    try:
                        detail_data = await self._scrape_detail_page(job.url)

                        detail_description = detail_data.get("description", "")
                        detail_location = detail_data.get("location", "")

                        if detail_description or detail_location:
                            job = Job(
                                job_id=job.job_id,
                                company=job.company,
                                title=job.title,
                                location=detail_location or job.location,
                                url=job.url,
                                source_url=job.source_url,
                                posted_date=detail_data.get("date posted") or job.posted_date,
                                description=detail_description or job.description,
                                scraped_at=datetime.now(timezone.utc).isoformat(),
                                extracted_experience_parts="",
                            )
                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich ARM job detail page %s: %s",
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
    # Popup dismissal (JS-based — removes overlays from DOM)
    # ------------------------------------------------------------------

    async def _dismiss_popups(self, page: Page) -> None:
        """Remove blocking overlays (system alerts, surveys) from the DOM."""
        await page.evaluate(
            """
            () => {
                // Remove system iAlert popup.
                const alert = document.getElementById('system-ialert');
                if (alert) alert.remove();

                // Remove survey dialog.
                const survey = document.getElementById('survale-survey-dialog');
                if (survey) survey.remove();

                // Remove any other modals / overlays that might block clicks.
                const modals = document.querySelectorAll(
                    '[role="dialog"][aria-modal="true"], '
                    + '.system-ialert-css, '
                    + 'dialog[open]'
                );
                modals.forEach(m => m.remove());
            }
            """
        )
        await page.wait_for_timeout(500)

    # ------------------------------------------------------------------
    # Filter interaction (all via JS to bypass overlay interception)
    # ------------------------------------------------------------------

    async def _click_filter_checkbox(
        self, page: Page, filter_text: str, section_selector: str
    ) -> None:
        """
        Click a filter checkbox by its ``data-display`` text.

        Uses JS evaluation to bypass any overlay interception.
        First ensures the section is expanded, then checks the
        target checkbox.
        """
        # Use JS to expand the section and check the checkbox.
        await page.evaluate(
            """
            (data) => {
                const section = document.querySelector(data.sectionSelector);
                if (!section) return;

                // Expand the section if collapsed.
                const toggle = section.querySelector('button.expandable-parent');
                if (toggle) {
                    const expanded = toggle.getAttribute('aria-expanded');
                    if (expanded !== 'true') {
                        toggle.click();
                    }
                }

                // Find and check the target checkbox.
                const checkboxes = section.querySelectorAll(
                    'input.filter-checkbox'
                );
                for (const cb of checkboxes) {
                    if (cb.getAttribute('data-display') === data.filterText) {
                        if (!cb.checked) {
                            cb.click();
                        }
                        return;
                    }
                }
            }
            """,
            {
                "sectionSelector": section_selector,
                "filterText": filter_text,
            },
        )
        # Wait for ambient AJAX to complete.
        await page.wait_for_timeout(2500)

    async def _select_sort(self, page: Page, sort_value: str) -> None:
        """Select the sort dropdown by visible option text."""
        await page.evaluate(
            """
            (value) => {
                const select = document.querySelector(
                    'select[data-search-results-sort-enhanced="true"]'
                );
                if (!select) return;
                const options = select.options;
                for (let i = 0; i < options.length; i++) {
                    if (options[i].textContent.trim() === value) {
                        select.value = options[i].value;
                        select.dispatchEvent(
                            new Event('change', { bubbles: true })
                        );
                        return;
                    }
                }
            }
            """,
            sort_value,
        )
        await page.wait_for_timeout(2000)

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card, source_url)
        title = self._extract_title(card)
        job_id = self._extract_job_id(card)
        location = self._extract_location(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "ARM"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag, source_url: str) -> str:
        anchor = card.select_one(self.TITLE_SELECTOR)
        if not anchor:
            return ""

        href = anchor.get("href")
        if not href:
            return ""

        return make_absolute_url(source_url, str(href))

    def _extract_title(self, card: Tag) -> str:
        anchor = card.select_one(self.TITLE_SELECTOR)
        if not anchor:
            return ""

        return self._clean_text(anchor.get_text())

    def _extract_job_id(self, card: Tag) -> str:
        """
        ARM job IDs come from the ``data-job-id`` attribute on the
        title link element.
        """
        anchor = card.select_one(self.TITLE_SELECTOR)
        if not anchor:
            return ""

        job_id = str(anchor.get("data-job-id", "")).strip()
        if job_id:
            return job_id

        # Fallback: extract from URL path.
        href = anchor.get("href", "")
        if href:
            return extract_job_id(str(href)) or ""

        return ""

    def _extract_location(self, card: Tag) -> str:
        loc_el = card.select_one(self.LOCATION_SELECTOR)
        if loc_el:
            return self._clean_text(loc_el.get_text())

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
                    "Shared browser context is no longer usable; "
                    "creating a fresh one."
                )
                await self.close_browser()

        return await self.new_page()

    async def _scrape_detail_page(self, job_url: str) -> dict[str, str]:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(
                job_url, wait_until="domcontentloaded", timeout=60000
            )

            # Wait for the description container.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_DESC_SELECTOR, timeout=10000
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            detail_data: dict[str, str] = {}

            description = self._extract_description(soup)
            if description:
                detail_data["description"] = description

            return detail_data

        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        container = soup.select_one(self.DETAIL_DESC_SELECTOR)
        if not container:
            return ""

        # Remove non-description elements.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _clean_multiline_text(self, text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        lines = [line.strip() for line in text.splitlines()]
        lines = [line for line in lines if line]
        return "\n".join(lines)
