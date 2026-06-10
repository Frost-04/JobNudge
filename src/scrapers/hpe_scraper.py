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


class HPEScraper(BaseScraper):
    """
    Scraper for HPE Careers (Phenom People platform) job search pages.

    HPE uses the same Phenom People platform as Cisco / Synopsys / Intuit.
    Filter checkboxes trigger AJAX re-renders; a sort dropdown controls
    result ordering.  Card data lives in ``data-ph-at-*`` attributes.

    Filter / sort interaction:
      1. Click Category accordion → check "Engineering & QA"
      2. Click Country accordion → check "India"
      3. Sort by "Most recent"

    Job cards:

        div[data-ph-at-id="jobs-list-item"]
          a[data-ph-at-id="job-link"]
            data-ph-at-job-title-text   → title
            data-ph-at-job-id-text      → numeric job ID
            href                        → detail page URL
          div[data-ph-at-id="jobdescription-text"]
            data-ph-at-job-title-text  → snippet description

    Detail page:

        div.phw-job-description         → full description
    """

    # ---- Card selectors ----
    CARD_SELECTOR = 'div[data-ph-at-id="jobs-list-item"]'
    LINK_SELECTOR = 'a[data-ph-at-id="job-link"]'
    CARD_DESC_SELECTOR = 'div[data-ph-at-id="jobdescription-text"]'

    # ---- Detail page selectors ----
    DETAIL_DESC_SELECTOR = 'div.phw-job-description'

    # ---- Filter selectors ----
    FILTER_CHECKBOX_SELECTOR = 'input[data-ph-at-id="facet-checkbox"]'
    SORT_DROPDOWN = 'select#sortselect'

    # ---- Filter configuration ----
    FILTER_COUNTRY = "India"
    FILTER_CATEGORY = "Engineering & QA"
    SORT_VALUE = "Most recent"

    # ---- Popup dismiss ----
    POPUP_SELECTORS = [
        'button[data-ph-at-id="gdpr-consent-btn"]',
        'button#onetrust-accept-btn-handler',
        'button[aria-label="Accept All Cookies"]',
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

            # ---- Dismiss cookie / consent popups ----
            await self._dismiss_popups(page)

            # ---- Step 1: Open Category accordion & click checkbox ----
            await self._open_accordion_and_check(page, "Category", self.FILTER_CATEGORY)

            # ---- Step 2: Open Country accordion & click checkbox ----
            await self._open_accordion_and_check(page, "Country", self.FILTER_COUNTRY)

            # ---- Step 3: Sort by Most Recent ----
            await self._select_sort(page, self.SORT_VALUE)

            # Wait for AJAX results.
            await page.wait_for_timeout(4000)

            # Wait for job cards.
            try:
                await page.wait_for_selector(self.CARD_SELECTOR, timeout=15000)
            except Exception:
                pass

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No HPE job cards found after filtering.")
                return jobs

            for card in cards[:max_jobs]:
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_ids:
                    continue
                if job.url in seen_urls:
                    continue

                # Enrich with detail page for full description and location.
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
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
                                posted_date=job.posted_date,
                                description=detail_description or job.description,
                                scraped_at=datetime.now(timezone.utc).isoformat(),
                                extracted_experience_parts="",
                            )
                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich HPE job detail %s: %s",
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
        """Dismiss cookie / consent popups if present."""
        for selector in self.POPUP_SELECTORS:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=3000):
                    await btn.click(force=True)
                    await page.wait_for_timeout(1000)
            except Exception:
                continue

    async def _open_accordion_and_check(
        self, page: Page, accordion_label: str, checkbox_text: str
    ) -> None:
        """
        Expand a Phenom People filter accordion and check a specific checkbox.
        Uses JS to synchronously click the accordion button, then waits for
        the accordion body to render before clicking the checkbox.
        """
        # Step 1: Click the accordion button to expand.
        await page.evaluate(
            """
            (label) => {
                const buttons = document.querySelectorAll(
                    'button[data-ph-at-id="facet-heading-link"]'
                );
                for (const btn of buttons) {
                    if (btn.getAttribute('data-ph-at-text') === label) {
                        const expanded = btn.getAttribute('aria-expanded');
                        if (expanded !== 'true') {
                            btn.click();
                        }
                        break;
                    }
                }
            }
            """,
            accordion_label,
        )
        await page.wait_for_timeout(2000)

        # Step 2: Click the checkbox (now visible after accordion expands).
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
            checkbox_text,
        )
        # Wait for AJAX reload after checkbox click.
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
        await page.wait_for_timeout(2500)

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        anchor = card.select_one(self.LINK_SELECTOR)
        if not anchor:
            return None

        href = anchor.get("href")
        if not href:
            return None

        url = self._make_job_url(source_url, str(href))

        # Title from data-ph-at-job-title-text attribute.
        title = anchor.get("data-ph-at-job-title-text", "")
        title = self._clean_text(title)
        if not title:
            return None

        # Job ID from data-ph-at-job-id-text attribute.
        job_id = anchor.get("data-ph-at-job-id-text", "")
        job_id = self._clean_text(job_id)

        if not job_id:
            job_id = self._extract_job_id_from_url(url)

        # Card-level description snippet.
        description = ""
        desc_el = card.select_one(self.CARD_DESC_SELECTOR)
        if desc_el:
            description = desc_el.get("data-ph-at-job-title-text", "")
            description = self._clean_text(description)

        # Cards don't show location; default to India (filtered by country).
        location = "India"

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "HPE"),
            title=title,
            location=location,
            url=url,
            source_url=source_url,
            posted_date=None,
            description=description or None,
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
                self.logger.debug(
                    "Shared browser context is no longer usable; creating a fresh one."
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

            # Wait for job description to load.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_DESC_SELECTOR, timeout=15000
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            detail_data: dict[str, str] = {}

            # Extract full description.
            desc_container = soup.select_one(self.DETAIL_DESC_SELECTOR)
            if desc_container:
                detail_data["description"] = self._extract_description(desc_container)

            # Extract location from detail page.
            location = self._extract_detail_location(soup)
            if location:
                detail_data["location"] = location

            return detail_data

        finally:
            await detail_page.close()

    def _extract_description(self, container: Tag) -> str:
        """Extract clean description text from HPE detail page."""
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    def _extract_detail_location(self, soup) -> str:
        """
        Try to extract location from HPE detail page.
        HPE detail pages don't have structured location metadata;
        we default to 'India' since we filtered by country.
        """
        # Try meta tags first.
        for meta in soup.select(
            'meta[name="job-location"], meta[property="job-location"]'
        ):
            content = meta.get("content", "")
            if content:
                return self._clean_text(content)

        # Noise words that indicate a map widget or template element,
        # not an actual location.
        noise_words = (
            "map", "loading", "address1", "address2", "zipcode",
            "zip code", "explore", "hide map", "show map",
            "locationmap", "{{", "}}",
        )

        # Look for structured location blocks.
        for selector in [
            'div[class*="location"]',
            'span[class*="location"]',
            'p[class*="location"]',
        ]:
            el = soup.select_one(selector)
            if el:
                text = self._clean_text(el.get_text())
                text_lower = text.lower()
                # Skip map widget / template boilerplate.
                if text and len(text) > 2:
                    if text_lower == "location":
                        continue
                    if any(w in text_lower for w in noise_words):
                        continue
                    # Strip "Location:" prefix if present.
                    if text_lower.startswith("location:"):
                        text = text[len("location:"):].strip()
                    return text

        # Fallback: India (we filtered by country).
        return "India"

    # ------------------------------------------------------------------
    # Job ID extraction
    # ------------------------------------------------------------------

    def _extract_job_id_from_url(self, url: str) -> str:
        """
        HPE job URLs:
        https://careers.hpe.com/us/en/job/1207611/Data-Engineer
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
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        return " ".join(text.split()).strip()

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        if not text:
            return ""
        lines = [line.strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line)
