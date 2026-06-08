from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class MastercardScraper(BaseScraper):
    """
    Scraper for Mastercard Careers job search pages (Phenom People platform).

    Mastercard's job board at careers.mastercard.com uses the Phenom People
    ATS with Aurelia.js for the frontend.  Jobs are filtered via checkbox
    facets (Category=Engineering, Country=India) and a sort dropdown
    (Most recent) — all trigger ambient AJAX re-renders without changing the
    URL.  The scraper uses Playwright to apply filters before parsing cards.

    Filters panel:
        div.phs-filter-panels
          div[data-ph-at-id="facet-category"]
          div[data-ph-at-id="facet-country"]

    Sort dropdown:
        select#sortselect > option[value="Most recent"]

    Results list:
        ul[data-ph-at-id="jobs-list"] > li[data-ph-at-id="jobs-list-item"]
          a[data-ph-at-id="job-link"]
            data-ph-at-job-title-text     → title
            data-ph-at-job-id-text        → job ID (e.g. "MASRUSR279916EXTERNALENUS")
            data-ph-at-job-location-text  → location
            data-ph-at-job-post-date-text → posted date (ISO 8601)
            href                          → absolute job URL

    Detail page (opens in new tab — we navigate directly):
        div.jd-info[data-ph-at-id="jobdescription-text"]
    """

    # ---- Filter selectors ----
    FILTER_CATEGORY_CHECKBOX = (
        'input[data-ph-at-facetkey="facet-category"][data-ph-at-text="Engineering"]'
    )
    FILTER_COUNTRY_CHECKBOX = (
        'input[data-ph-at-facetkey="facet-country"][data-ph-at-text="India"]'
    )
    SORT_DROPDOWN = "select#sortselect"
    SORT_OPTION_VALUE = "Most recent"

    # ---- Listing card selectors ----
    CARD_SELECTOR = 'li[data-ph-at-id="jobs-list-item"]'
    JOB_LINK_SELECTOR = 'a[data-ph-at-id="job-link"]'

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = 'div.jd-info[data-ph-at-id="jobdescription-text"]'

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # ---- Step 1: Apply Category filter (Engineering) ----
            await self._apply_checkbox_filter(page, self.FILTER_CATEGORY_CHECKBOX, "Engineering")

            # ---- Step 2: Apply Country filter (India) ----
            await self._apply_checkbox_filter(page, self.FILTER_COUNTRY_CHECKBOX, "India")

            # ---- Step 3: Apply Sort by "Most recent" ----
            await self._apply_sort_filter(page)

            # ---- Step 4: Wait for cards to render after filters ----
            await self._wait_for_cards(page)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)
            if not cards:
                self.logger.warning("No Mastercard job cards found after filtering.")
                return await self._fallback_links(page, source_url, max_jobs)

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

                # ---- Step 5: Enrich with detail page ----
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
                            "Failed to enrich Mastercard job detail %s: %s",
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

    async def _apply_checkbox_filter(
        self, page: Page, checkbox_selector: str, label: str
    ) -> None:
        """Click a single checkbox facet using JS to bypass CSS-hidden checkboxes."""
        # Ensure the filters panel has rendered
        try:
            await page.wait_for_selector(
                'div.phs-filter-panels, div.phs-facet-results, select#sortselect',
                timeout=15000,
            )
        except Exception:
            pass

        checkbox_input = page.locator(checkbox_selector)
        if not await checkbox_input.count():
            # Try clicking "Filter" toggle to reveal facets on mobile
            filter_toggle = page.locator(
                'a[data-ph-at-id="mobile-facet-filter-menu-link"]'
            )
            if await filter_toggle.count():
                await filter_toggle.click()
                await page.wait_for_timeout(1500)

        if not await checkbox_input.count():
            self.logger.warning(
                "%s checkbox not found on Mastercard.", label
            )
            return

        # Check if already selected
        is_checked = await checkbox_input.get_attribute("aria-checked")
        if is_checked == "true":
            self.logger.debug("%s checkbox already selected.", label)
            return

        # Phenom People hides native checkboxes and uses Aurelia
        # change.delegate handlers on labels.  Click via JS for reliability.
        try:
            await page.evaluate(
                """(selector) => {
                    const input = document.querySelector(selector);
                    if (input) {
                        const label = input.closest('label');
                        if (label) label.click();
                    }
                }""",
                checkbox_selector,
            )
            # Let Aurelia re-render the results list.
            await page.wait_for_timeout(3000)
        except Exception as exc:
            self.logger.warning(
                "Failed to click %s filter via JS: %s", label, exc
            )

    async def _apply_sort_filter(self, page: Page) -> None:
        """Select 'Most recent' in the sort dropdown."""
        try:
            await page.wait_for_selector(self.SORT_DROPDOWN, timeout=15000)
        except Exception:
            pass

        dropdown = page.locator(self.SORT_DROPDOWN)
        if not await dropdown.count():
            self.logger.warning("Sort dropdown not found on Mastercard.")
            return

        try:
            await page.evaluate(
                """(data) => {
                    const select = document.querySelector(data.selector);
                    if (select) {
                        select.value = data.value;
                        select.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""",
                {"selector": self.SORT_DROPDOWN, "value": self.SORT_OPTION_VALUE},
            )
            await page.wait_for_timeout(3000)
        except Exception as exc:
            self.logger.warning("Failed to set sort via JS: %s", exc)

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link_el = card.select_one(self.JOB_LINK_SELECTOR)
        if not link_el:
            return None

        title = self._extract_title(link_el, card)
        if not title:
            return None

        job_url = self._extract_link_url(link_el)
        if not job_url:
            return None

        job_id = self._extract_job_id(link_el)
        location = self._extract_location(link_el, card)
        posted_date = self._extract_posted_date(link_el)

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Mastercard"),
            title=title,
            location=location,
            url=job_url,
            source_url=source_url,
            posted_date=posted_date or None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link_url(self, link_el: Tag) -> str:
        href = link_el.get("href")
        if not href:
            return ""

        href = html.unescape(str(href)).strip()
        if href.startswith("http://") or href.startswith("https://"):
            return href

        return make_absolute_url(self.company_config.get("url", ""), href)

    def _extract_title(self, link_el: Tag, card: Tag) -> str:
        # Primary: data-ph-at-job-title-text attribute
        title = link_el.get("data-ph-at-job-title-text")
        if title:
            return self._clean_text(str(title))

        # Fallback: .job-title span
        title_span = card.select_one("span.job-title span, div.job-title span")
        if title_span:
            return self._clean_text(title_span.get_text())

        # Fallback: aria-label without the "Job ID is ..." suffix
        aria = link_el.get("aria-label")
        if aria:
            cleaned = re.sub(r"\s*Job\s+ID\s+is\s+\S+\s*$", "", str(aria)).strip()
            if cleaned:
                return self._clean_text(cleaned)

        return ""

    def _extract_location(self, link_el: Tag, card: Tag) -> str:
        # Primary: data-ph-at-job-location-text attribute
        location = link_el.get("data-ph-at-job-location-text")
        if location:
            return self._clean_text(str(location))

        # Fallback: .job-location span
        loc_span = card.select_one("span.job-location")
        if loc_span:
            text = self._clean_text(loc_span.get_text())
            text = text.replace("Location", "").strip()
            if text:
                return text

        return ""

    def _extract_job_id(self, link_el: Tag) -> str:
        # Primary: data-ph-at-job-id-text attribute
        job_id = link_el.get("data-ph-at-job-id-text")
        if job_id:
            return str(job_id).strip()

        # Fallback: from URL path (/job/MASRUSR.../ or /job/R00.../)
        href = link_el.get("href")
        if href:
            match = re.search(r"/job/([A-Za-z0-9_-]+)/", str(href))
            if match:
                return match.group(1)

        return ""

    def _extract_posted_date(self, link_el: Tag) -> str | None:
        # Primary: data-ph-at-job-post-date-text attribute (ISO 8601)
        date_text = link_el.get("data-ph-at-job-post-date-text")
        if date_text:
            return str(date_text).strip()
        return None

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def _get_detail_page(self) -> Page:
        """Return a new page for detail scraping."""
        if self.context:
            try:
                return await self.context.new_page()
            except Exception:
                self.logger.debug(
                    "Shared browser context is no longer usable; recreating."
                )
                await self.close_browser()

        return await self.new_page()

    async def _scrape_detail_page(self, job_url: str) -> str:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(job_url, wait_until="networkidle", timeout=60000)

            await detail_page.wait_for_selector(
                self.DETAIL_DESCRIPTION_SELECTOR,
                timeout=15000,
            )

            soup = await self._get_soup(detail_page)
            return self._extract_description(soup)

        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        container = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)
        if not container:
            return ""

        # Remove script/style tags.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    # ------------------------------------------------------------------
    # Wait helpers
    # ------------------------------------------------------------------

    async def _wait_for_cards(self, page: Page) -> None:
        """Wait for the jobs list to appear after filtering."""
        selectors = [
            self.CARD_SELECTOR,
            'ul[data-ph-at-id="jobs-list"]',
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
    # Fallback
    # ------------------------------------------------------------------

    async def _fallback_links(
        self, page: Page, source_url: str, max_jobs: int
    ) -> list[Job]:
        """Fallback: extract job links from raw anchor tags."""
        jobs: list[Job] = []
        soup = await self._get_soup(page)
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        for link in soup.select(self.JOB_LINK_SELECTOR):
            if len(jobs) >= max_jobs:
                break

            href = link.get("href")
            if not href:
                continue
            href = str(href)

            if "/job/" not in href:
                continue

            job_url = make_absolute_url(source_url, html.unescape(href).strip())

            title = link.get("data-ph-at-job-title-text") or self._clean_text(
                link.get_text()
            )
            job_id = link.get("data-ph-at-job-id-text") or ""
            location = link.get("data-ph-at-job-location-text") or ""

            if not job_url or not title:
                continue
            if job_url in seen_urls:
                continue
            if job_id and job_id in seen_ids:
                continue

            if job_id:
                seen_ids.add(job_id)
            seen_urls.add(job_url)

            jobs.append(
                Job(
                    job_id=job_id,
                    company=self.company_config.get("name", "Mastercard"),
                    title=title,
                    location=location,
                    url=job_url,
                    source_url=source_url,
                    posted_date=None,
                    description=None,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    extracted_experience_parts="",
                )
            )

        return jobs

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
        # Remove consecutive duplicate lines (common with nested HTML).
        deduped: list[str] = []
        for line in lines:
            if deduped and line == deduped[-1]:
                continue
            deduped.append(line)
        return "\n".join(deduped)
