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


class LowesScraper(BaseScraper):
    """
    Scraper for Lowe's Careers search results pages.

    Lowe's uses the Phenom People platform with ``data-ph-at-id``
    attributes (same family as Cisco / Synopsys / Intuit / Palo Alto
    Networks).  Filter checkboxes trigger ambient AJAX re-renders
    without changing the URL.  A sub-search textbox filters the
    already-loaded results client-side.  A sort dropdown controls
    result ordering.

    Expected listing card structure:

        ul[data-ph-at-id="jobs-list"]
          li[data-ph-at-id="jobs-list-item"]
            a[data-ph-at-id="job-link"][aria-label]          → title + job ID
            span.job-location                                → location
            p[data-ph-at-id="jobdescription-text"]           → card description

    Expected detail page structure:

        div[data-ph-at-id="jobdescription-text"]             → full description
    """

    # ---- Card selectors ----
    CARD_SELECTOR = 'li[data-ph-at-id="jobs-list-item"]'
    LINK_SELECTOR = 'a[data-ph-at-id="job-link"]'
    TITLE_SELECTOR = 'a[data-ph-at-id="job-link"]'
    LOCATION_SELECTOR = 'span.job-location'
    CARD_DESC_SELECTOR = 'p[data-ph-at-id="jobdescription-text"]'

    # ---- Detail page selectors ----
    DETAIL_DESC_SELECTOR = 'div[data-ph-at-id="jobdescription-text"]'

    # ---- Filter selectors ----
    FILTER_CHECKBOX_TEMPLATE = 'input[data-ph-at-id="facet-checkbox"][data-ph-at-text="{text}"]'
    SUB_SEARCH_TEXTBOX = 'input[data-ph-at-id="sub-search-textbox"]'
    SUB_SEARCH_BUTTON = 'button[data-ph-at-id="sub-search-textbox-button"]'
    SORT_DROPDOWN = 'select#sortselect'

    # ---- Filter configuration ----
    FILTER_CATEGORY = "Technology"
    SUB_SEARCH_TEXT = "Engineer"
    SORT_VALUE = "Most recent"

    # ---- Popup / intercept ----
    POPUP_SELECTORS = [
        "div.system-ialert-close-button",
        "div.system-ialert-remove-button",
        "button#system-ialert-close-button",
    ]

    # ---- Job ID regex ----
    _JR_ID_RE = re.compile(r"(JR-?\d+)", re.IGNORECASE)

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

            # ---- Step 1: Apply Category filter (Technology) ----
            await self._click_filter_checkbox(page, self.FILTER_CATEGORY)

            # ---- Step 2: Type "Engineer" in sub-search and submit ----
            await self._apply_sub_search(page, self.SUB_SEARCH_TEXT)

            # ---- Step 3: Sort by Most Recent ----
            await self._select_sort(page, self.SORT_VALUE)

            # ---- Step 4: Wait for results ----
            await page.wait_for_timeout(4000)

            # Wait for job cards to appear.
            try:
                await page.wait_for_selector(self.CARD_SELECTOR, timeout=15000)
            except Exception:
                pass

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)
            self.logger.info("Lowe's: %d cards found.", len(cards))

            if not cards:
                self.logger.warning("No Lowe's job cards found after filtering.")
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
                            "Failed to enrich Lowe's job detail page %s: %s",
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

    async def _apply_sub_search(self, page: Page, search_text: str) -> None:
        """
        Type text into the sub-search textbox and click the Go button
        to filter the already-loaded results client-side.
        """
        try:
            # Type the search text.
            search_input = page.locator(self.SUB_SEARCH_TEXTBOX)
            if await search_input.is_visible(timeout=5000):
                await search_input.fill(search_text)
                await page.wait_for_timeout(500)

                # Click the Go button.
                go_button = page.locator(self.SUB_SEARCH_BUTTON)
                if await go_button.is_visible(timeout=3000):
                    await go_button.click()
                    await page.wait_for_timeout(2000)
        except Exception:
            self.logger.debug("Sub-search interaction failed, continuing...")

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
        job_id = self._extract_job_id(card, link)
        location = self._extract_location(card)
        description = self._extract_card_description(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Lowe's"),
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
        Extract job title from aria-label of the job-link anchor.
        Lowe's aria-labels look like:
        "Software Engineer_Full Stack_Reactjs Job ID is JR-02521928"

        We strip the " Job ID is JR-..." suffix to get the clean title.
        """
        anchor = card.select_one(self.TITLE_SELECTOR)

        if not anchor:
            return ""

        # Prefer aria-label.
        aria_label = anchor.get("aria-label")

        if isinstance(aria_label, str) and aria_label.strip():
            # Remove the " Job ID is JR-..." suffix.
            title = re.sub(r"\s+Job\s+ID\s+is\s+JR-?\d+", "", aria_label, flags=re.IGNORECASE)
            return self._clean_text(title)

        # Fallback to link text.
        return self._clean_text(anchor.get_text())

    def _extract_location(self, card: Tag) -> str:
        """Extract location from span.job-location."""
        el = card.select_one(self.LOCATION_SELECTOR)

        if el:
            # The sr-only span contains "Location:", get only the visible text.
            for sr_span in el.select("span.sr-only"):
                sr_span.decompose()

            # Also remove icon elements.
            for icon in el.select("i.icon"):
                icon.decompose()

            return self._clean_text(el.get_text())

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

    def _extract_job_id(self, card: Tag, url: str) -> str:
        """
        Lowe's job IDs are JR-prefixed:
        - aria-label: "Software Engineer Job ID is JR-02521928"
        - URL: /job/JR-02521928/Software-Engineer
        """
        # Try aria-label first.
        anchor = card.select_one(self.TITLE_SELECTOR)
        if anchor:
            aria_label = anchor.get("aria-label")
            if isinstance(aria_label, str):
                match = self._JR_ID_RE.search(aria_label)
                if match:
                    return match.group(1)

        # Try URL.
        if url:
            match = self._JR_ID_RE.search(url)
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

    async def _scrape_detail_page(self, job_url: str) -> str:
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

            desc_container = soup.select_one(self.DETAIL_DESC_SELECTOR)
            if not desc_container:
                return ""

            return self._extract_description(desc_container)

        finally:
            await detail_page.close()

    def _extract_description(self, container: Tag) -> str:
        """Extract clean description text preserving section structure."""
        # Remove script/style tags.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        sections: list[str] = []
        current_lines: list[str] = []

        for child in container.descendants:
            if not hasattr(child, "name"):
                continue

            tag_name = child.name

            # Headings mark new sections.
            if tag_name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                if current_lines:
                    sections.append("\n".join(current_lines))
                    current_lines = []

                heading_text = self._clean_text(child.get_text())
                if heading_text:
                    sections.append(heading_text)

            elif tag_name == "p":
                text = self._clean_text(child.get_text())
                if text:
                    current_lines.append(text)

            elif tag_name == "li":
                text = self._clean_text(child.get_text())
                if text:
                    current_lines.append(f"• {text}")

        # Flush remaining lines.
        if current_lines:
            sections.append("\n".join(current_lines))

        result = "\n\n".join(sections)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip()

    # ------------------------------------------------------------------
    # Text utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        """Collapse whitespace and strip."""
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()
