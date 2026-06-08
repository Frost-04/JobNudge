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


class MerckScraper(BaseScraper):
    """
    Scraper for Merck Group Careers (Aurelia-powered SPA).

    Flow:

    1. Navigate to the search-results page.
    2. Select "Most recent" from the sort dropdown.
    3. Check "India" in the Location facet.
    4. Check "Information Technology" in the Functional Area facet.
    5. Wait for AJAX results to render.
    6. Parse job cards (rich data-ph-at-* attributes on link elements).
    7. Open each job's detail page for description enrichment.

    Expected card HTML:

        li[data-ph-at-id="jobs-list-item"]
          a[data-ph-at-id="job-link"]
            @data-ph-at-job-title-text       -> title
            @data-ph-at-job-id-text          -> job ID
            @data-ph-at-job-location-text    -> location
            @data-ph-at-job-post-date-text   -> posted date
            @href                             -> /global/en/job/{id}/...

    Expected detail page:

        div.jd-info[data-ph-at-id="jobdescription-text"]
    """

    # ---- Page interaction selectors ----
    SORT_DROPDOWN = "select#sortselect"
    RESULTS_LIST = "ul[data-ph-at-id='jobs-list']"
    CARD_SELECTOR = "li[data-ph-at-id='jobs-list-item']"
    LINK_SELECTOR = "a[data-ph-at-id='job-link']"

    # ---- Detail page selectors ----
    DESCRIPTION_SELECTOR = "div.jd-info[data-ph-at-id='jobdescription-text']"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))
        filter_location = self.company_config.get("filter_location", "India")
        filter_category = self.company_config.get("filter_category", "Information Technology")

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2000)

            # ---- Step 1: Set sort to "Most recent" ----
            try:
                await page.select_option(self.SORT_DROPDOWN, "Most recent")
                await page.wait_for_timeout(2000)
            except Exception:
                self.logger.warning("Merck sort dropdown not found or not selectable.")

            # ---- Step 2: Apply Location filter (India) ----
            await self._apply_filter(page, "facet-country", filter_location)

            # ---- Step 3: Apply Functional Area filter (Information Technology) ----
            await self._apply_filter(page, "facet-category", filter_category)

            # ---- Step 4: Wait for results ----
            await self._wait_for_results(page)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No Merck job cards found after filtering.")
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
                            "Failed to enrich Merck job detail %s: %s",
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

    async def _apply_filter(self, page: Page, facet_key: str, filter_name: str) -> None:
        """Check a facet checkbox by its data-ph-at-facetkey and data-ph-at-text attributes.

        The Merck site uses Aurelia data-* attributes:

            <input data-ph-at-facetkey="facet-country" data-ph-at-text="India" ...>

        If the accordion section is collapsed, expand it first.
        """
        # Find the accordion button for this facet section.
        # Accordion buttons have id="LocationAccordion", "FunctionalAreaAccordion", etc.
        # and data-ph-at-text matching the facet label.
        accordion_map = {
            "facet-country": "LocationAccordion",
            "facet-category": "FunctionalAreaAccordion",
            "facet-state": "StateAccordion",
            "facet-city": "CityAccordion",
            "facet-jobLevelId": "CareerLevelAccordion",
            "facet-type": "WorkingTimeModelAccordion",
        }

        accordion_id = accordion_map.get(facet_key, "")

        if accordion_id:
            try:
                toggle = page.locator(f"button#{accordion_id}")

                if await toggle.count():
                    expanded = await toggle.get_attribute("aria-expanded")

                    if expanded != "true":
                        await toggle.click(force=True)
                        await page.wait_for_timeout(800)
            except Exception:
                pass

        # Find and check the checkbox using data-ph-at-text.
        checkbox = page.locator(
            f'input[data-ph-at-facetkey="{facet_key}"][data-ph-at-text="{filter_name}"]'
        )

        if not await checkbox.count():
            self.logger.warning(
                "Merck filter not found: %s -> %s", facet_key, filter_name
            )
            return

        is_checked = await checkbox.is_checked()

        if is_checked:
            self.logger.debug("Merck filter already active: %s", filter_name)
            return

        try:
            await checkbox.check(force=True, timeout=5000)
        except Exception:
            await checkbox.dispatch_event("click")

        self.logger.debug("Merck filter activated: %s", filter_name)
        await page.wait_for_timeout(2000)

    async def _wait_for_results(self, page: Page) -> None:
        """Wait for AJAX results to populate after filter interactions."""
        try:
            await page.wait_for_selector(self.LINK_SELECTOR, timeout=15000)
        except Exception:
            pass

        await page.wait_for_timeout(1000)

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link_el = card.select_one(self.LINK_SELECTOR)

        if not link_el:
            return None

        # Extract data from data-ph-at-* attributes (most reliable).
        title = link_el.get("data-ph-at-job-title-text", "")
        job_id = link_el.get("data-ph-at-job-id-text", "")
        location = link_el.get("data-ph-at-job-location-text", "")
        posted_date_raw = link_el.get("data-ph-at-job-post-date-text", "")

        # Clean up.
        title = self._clean_text(str(title)) if title else ""
        job_id = str(job_id).strip() if job_id else ""
        location = self._clean_location_text(str(location)) if location else ""
        posted_date = self._extract_date(str(posted_date_raw)) if posted_date_raw else ""

        href = link_el.get("href", "")

        if not href:
            return None

        job_url = self._make_merck_job_url(str(href))

        # Fallback: if data attributes are missing, try DOM selectors.
        if not title:
            title_span = card.select_one("div.job-title span")

            if title_span:
                title = self._clean_text(title_span.get_text())

        if not location:
            location_span = card.select_one("span.cityStateCountry")

            if location_span:
                location = self._clean_location_text(location_span.get_text())

        if not title or not job_url:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Merck"),
            title=title,
            location=location,
            url=job_url,
            source_url=source_url,
            posted_date=posted_date or None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_date(self, raw: str) -> str:
        """Convert ISO date to readable format: 2026-06-05T00:00:00.000+0000 -> 2026-06-05"""
        if not raw:
            return ""

        match = re.match(r"(\d{4}-\d{2}-\d{2})", raw)

        if match:
            return match.group(1)

        return raw.strip()

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
                    self.DESCRIPTION_SELECTOR,
                    timeout=15000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            return self._extract_description(soup)

        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        container = soup.select_one(self.DESCRIPTION_SELECTOR)

        if not container:
            return ""

        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    # ------------------------------------------------------------------
    # URL construction
    # ------------------------------------------------------------------

    def _make_merck_job_url(self, href: str) -> str:
        href = html.unescape(href).strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        origin = "https://careers.merckgroup.com"

        if href.startswith("/global/"):
            return f"{origin}{href}"

        if href.startswith("global/"):
            return f"{origin}/{href}"

        return f"{origin}/{href.lstrip('/')}"

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------

    def _clean_location_text(self, text: str) -> str:
        text = self._clean_text(text)

        if not text:
            return ""

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
