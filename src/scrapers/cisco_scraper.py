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


class CiscoScraper(BaseScraper):
    """
    Scraper for Cisco Careers search results pages.

    Cisco uses the Phenom People platform with ``data-ph-at-id``
    attributes (same family as Synopsys / Intuit / Palo Alto Networks).
    Filter checkboxes trigger ambient AJAX re-renders without changing
    the URL.  A sort dropdown controls result ordering.

    Expected listing card structure:

        div[data-ph-at-id="jobs-list"][role="listitem"]
          a[data-ph-at-id="job-link"][aria-label]          → title
          div[data-ph-at-id="job-info"]                    → location (JS-populated)
          div[data-ph-at-id="jobdescription-text"]         → card-level description

    Expected detail page structure:

        div.phw-job-description                            → full description
    """

    # ---- Card selectors ----
    CARD_SELECTOR = 'div[data-ph-at-id="jobs-list"]'
    LINK_SELECTOR = 'a[data-ph-at-id="job-link"]'
    TITLE_SELECTOR = 'a[data-ph-at-id="job-link"]'
    LOCATION_SELECTORS = [
        'div[data-ph-at-id="job-info"]',
        'div._jw-job-info_1ik5l_27',
    ]
    CARD_DESC_SELECTOR = 'div[data-ph-at-id="jobdescription-text"]'

    # ---- Detail page selectors ----
    DETAIL_DESC_SELECTOR = 'div.phw-job-description'

    # ---- Filter selectors ----
    FILTER_CHECKBOX_TEMPLATE = 'input[data-ph-at-id="facet-checkbox"][data-ph-at-text="{text}"]'
    FILTER_COUNTRY_CHECKBOX = 'input[data-ph-at-facetkey="facet-country"]'
    FILTER_CATEGORY_CHECKBOX = 'input[data-ph-at-facetkey="facet-category"]'

    SORT_DROPDOWN = 'select#sortselect'

    # ---- Filter configuration ----
    FILTER_COUNTRY = "India"
    FILTER_CATEGORIES = [
        "Product and Engineering",
        "Internships, Apprenticeships, and Co-Ops",
    ]
    SORT_VALUE = "Most recent"

    # ---- Popup / intercept ----
    POPUP_SELECTORS = [
        "div.system-ialert-close-button",
        "div.system-ialert-remove-button",
        "button#system-ialert-close-button",
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

            # ---- Step 1: Apply Country filter (India) ----
            await self._click_filter_checkbox(page, self.FILTER_COUNTRY)

            # ---- Step 2: Apply Job Category filters ----
            for category in self.FILTER_CATEGORIES:
                await self._click_filter_checkbox(page, category)

            # ---- Step 3: Sort by Most Recent ----
            await self._select_sort(page, self.SORT_VALUE)

            # ---- Step 4: Wait for AJAX results ----
            await page.wait_for_timeout(4000)

            # Wait for job cards to appear.
            try:
                await page.wait_for_selector(self.CARD_SELECTOR, timeout=15000)
            except Exception:
                pass

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)
            self.logger.info("Cisco: %d cards found.", len(cards))

            if not cards:
                self.logger.warning("No Cisco job cards found after filtering.")
                return jobs

            for card in cards[:max_jobs]:
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_ids:
                    continue
                if job.url in seen_urls:
                    continue

                # Enrich with detail page for excluded titles, keep card
                # description for non-excluded titles.
                if self._should_exclude(job.title):
                    self.logger.debug(
                        "Skipping detail enrichment for: %s", job.title
                    )
                else:
                    # Try detail page for additional location/metadata enrichment.
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
                            "Failed to enrich Cisco job detail page %s: %s",
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

    async def _dismiss_popups(self, page: Page) -> None:
        """Dismiss cookie / alert popups if present."""
        for selector in self.POPUP_SELECTORS:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await page.wait_for_timeout(500)
            except Exception:
                continue

    async def _click_filter_checkbox(self, page: Page, filter_text: str) -> None:
        """
        Click a Phenom People filter checkbox by its ``data-ph-at-text``
        attribute value.  Uses JS evaluation to bypass visibility checks.
        """
        await page.evaluate(
            """
            (text) => {
                const checkboxes = document.querySelectorAll(
                    'input[data-ph-at-id="facet-checkbox"]'
                );
                for (const cb of checkboxes) {
                    if (cb.getAttribute('data-ph-at-text') === text) {
                        if (!cb.checked) {
                            cb.click();
                        }
                        return;
                    }
                }
            }
            """,
            filter_text,
        )
        # Wait for ambient AJAX to complete.
        await page.wait_for_timeout(2500)

    async def _select_sort(self, page: Page, sort_value: str) -> None:
        """Select the sort dropdown option by its visible text."""
        await page.evaluate(
            """
            (value) => {
                const select = document.querySelector('select#sortselect');
                if (!select) return;
                const options = select.options;
                for (let i = 0; i < options.length; i++) {
                    if (options[i].textContent.trim() === value) {
                        select.value = options[i].value;
                        select.dispatchEvent(new Event('change', { bubbles: true }));
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
        job_id = self._extract_job_id_from_url(link)
        location = self._extract_location(card)
        description = self._extract_card_description(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Cisco"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=description or None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag, source_url: str) -> str:
        """Extract absolute job URL from the job-link anchor."""
        anchor = card.select_one(self.LINK_SELECTOR)

        if not anchor:
            return ""

        href = anchor.get("href")

        if not href:
            return ""

        return self._make_job_url(source_url, str(href))

    def _extract_title(self, card: Tag) -> str:
        """
        Extract job title from aria-label or link text.
        Cisco cards use aria-label on the link for the title.
        """
        anchor = card.select_one(self.TITLE_SELECTOR)

        if not anchor:
            return ""

        # Prefer aria-label.
        aria_label = anchor.get("aria-label")

        if aria_label:
            return self._clean_text(str(aria_label))

        # Fallback to link text.
        return self._clean_text(anchor.get_text())

    def _extract_location(self, card: Tag) -> str:
        """
        Extract location from job-info divs.
        Cisco populates these dynamically via JS; they may contain
        'Location: Bangalore, India' or similar.
        """
        # Noise values to skip.
        noise_prefixes = [
            "available in",      # "Available in 2 locations"
            "multiple locations",
            "job type",
            "experience",
            "category",
            "department",
            "posted",
        ]

        for selector in self.LOCATION_SELECTORS:
            els = card.select(selector)

            for el in els:
                text = el.get_text(strip=True)

                if not text:
                    continue

                text_lower = text.lower()

                # Skip noise values.
                if any(text_lower.startswith(n) for n in noise_prefixes):
                    continue

                # Check for location prefix pattern.
                if text_lower.startswith("location"):
                    parts = text.split(":", 1)

                    if len(parts) > 1:
                        return self._clean_text(parts[1])

                # Accept plain location text (e.g. "Bangalore, India").
                if "," in text or "india" in text_lower:
                    return self._clean_text(text)

        return ""

    def _extract_card_description(self, card: Tag) -> str:
        """Extract the in-card description snippet."""
        el = card.select_one(self.CARD_DESC_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        return ""

    # ------------------------------------------------------------------
    # Job ID extraction
    # ------------------------------------------------------------------

    def _extract_job_id_from_url(self, url: str) -> str:
        """
        Cisco job URLs:
        https://careers.cisco.com/global/en/job/2015350/Software-QA-Engineer
        """
        if not url:
            return ""

        match = re.search(r"/job/(\d+)", url)

        if match:
            return match.group(1)

        return extract_job_id(url) or ""

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def _make_job_url(self, source_url: str, href: str) -> str:
        href = html.unescape(href).strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        parsed_source = urlparse(source_url)
        origin = f"{parsed_source.scheme}://{parsed_source.netloc}"

        if href.startswith("/"):
            return f"{origin}{href}"

        return f"{origin}/{href}"

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def _get_detail_page(self) -> Page:
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

            # Wait for the job description to appear.
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

            # Try to extract location from detail page metadata.
            location = self._extract_detail_location(soup)

            if location:
                detail_data["location"] = location

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

    def _extract_detail_location(self, soup) -> str:
        """
        Cisco detail pages may have location in meta tags or
        structured data sections.
        """
        # Try meta tags first.
        for meta in soup.select('meta[name="job-location"], meta[property="job-location"]'):
            content = meta.get("content", "")

            if content:
                return self._clean_text(content)

        # Look for location patterns in the job description container.
        container = soup.select_one(self.DETAIL_DESC_SELECTOR)

        if not container:
            return ""

        # Some Cisco pages have location in a specific element before
        # the description.  Try common patterns.
        for selector in [
            'div[class*="location"]',
            'span[class*="location"]',
            'p[class*="location"]',
        ]:
            el = container.parent.select_one(selector) if container.parent else None

            if el:
                text = self._clean_text(el.get_text())

                if text and not text.lower().startswith("location"):
                    return text

        return ""

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

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

    def _dedupe_preserve_order(self, items: list[Job]) -> list[Job]:
        seen: set[str] = set()
        result: list[Job] = []

        for job in items:
            key = job.url or job.job_id

            if key and key in seen:
                continue

            if key:
                seen.add(key)

            result.append(job)

        return result
