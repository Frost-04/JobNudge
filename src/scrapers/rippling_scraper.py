from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class RipplingScraper(BaseScraper):
    """
    Scraper for Rippling Careers open roles page.

    Rippling loads **all** job cards in the DOM at once and uses client-side
    JavaScript to show/hide them based on ``<select>`` dropdown values.
    BS4 sees the entire DOM — including hidden non-Engineering cards — so
    this scraper uses ``page.evaluate`` to extract data **only from visible**
    cards after applying dropdown filters.

    Filter dropdowns (set via JS + dispatchEvent('change')):
        selects[0]  →  Department ("Engineering")
        selects[1]  →  Location ("Bangalore, India")

    Cards (visible only when parent wrapper is not display:none):
        a[href*="ats.rippling.com"]
          p.font-medium              → title
          p[class*="pl-8"]           → location text

    Detail page:
        div.ATS_htmlPreview → rich HTML job description
    """

    DEPT_VALUE = "Engineering"
    LOCATION_VALUE = "Bangalore, India"
    CARD_LINK_SELECTOR = 'a[href*="ats.rippling.com"]'
    DETAIL_DESCRIPTION_SELECTOR = "div.ATS_htmlPreview"

    EXCLUDE_TITLE_WORDS = [
        "senior",
        "director",
        "principal",
        "manager",
        "staff",
        "executive",
        "analyst",
    ]

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # ---- Apply both dropdown filters via JS ----
            await self._apply_filters(page)

            # ---- Wait for cards ----
            try:
                await page.wait_for_selector(self.CARD_LINK_SELECTOR, timeout=15000)
            except Exception:
                self.logger.warning("No Rippling card links found.")
                return jobs

            # ---- Extract data from VISIBLE cards only (not BS4) ----
            card_data = await self._extract_visible_cards(page)

            if not card_data:
                self.logger.warning("No visible Rippling cards after filtering.")
                return jobs

            seen_urls: set[str] = set()

            for data in card_data:
                if len(jobs) >= max_jobs:
                    break

                title = self._clean_text(data.get("title", ""))
                url = data.get("url", "")
                location = self._clean_text(data.get("location", ""))

                if not url or not title:
                    continue
                if url in seen_urls:
                    continue

                if self._should_exclude(title):
                    self.logger.debug("Skipping excluded title: %s", title)
                    continue

                job_id = self._extract_job_id(url)

                job = Job(
                    job_id=job_id,
                    company=self.company_config.get("name", "Rippling"),
                    title=title,
                    location=location,
                    url=url,
                    source_url=source_url,
                    posted_date=None,
                    description=None,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    extracted_experience_parts="",
                )

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
                        "Failed to enrich Rippling detail %s: %s", job.url, exc
                    )

                seen_urls.add(job.url)
                jobs.append(job)

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Filter application
    # ------------------------------------------------------------------

    async def _apply_filters(self, page: Page) -> None:
        """Set Location via Playwright's native select_option."""
        loc_select = page.locator("select").nth(1)
        if await loc_select.count():
            try:
                await loc_select.select_option(label=self.LOCATION_VALUE, timeout=10000)
            except Exception as exc:
                self.logger.warning("Could not select location: %s", exc)
        await page.wait_for_timeout(3000)

    # ------------------------------------------------------------------
    # Visible-card extraction (Playwright, not BS4)
    # ------------------------------------------------------------------

    async def _extract_visible_cards(self, page: Page) -> list[dict]:
        """
        Extract data from Engineering cards in Bangalore.
        Rippling's dropdowns don't always fire reliably, so we filter by
        the embedded department text (2nd <p>) and location text.
        """
        return await page.evaluate(
            """() => {
                const cards = document.querySelectorAll('a[href*="ats.rippling.com"]');
                const results = [];
                for (const a of cards) {
                    const allP = a.querySelectorAll('p');
                    // 2nd <p> is the department label
                    let dept = '';
                    if (allP.length >= 2) dept = allP[1].textContent.trim();
                    if (dept !== 'Engineering') continue;

                    const titleEl = a.querySelector('p.font-medium');
                    const title = titleEl ? titleEl.textContent.trim() : '';
                    const locEl = a.querySelector('p[class*="pl-8"]');
                    const location = locEl ? locEl.textContent.trim() : '';

                    // Also ensure Bangalore location
                    if (!location.includes('Bangalore')) continue;

                    results.push({ url: a.href, title: title, location: location });
                }
                return results;
            }"""
        )

    # ------------------------------------------------------------------
    # Title exclusion
    # ------------------------------------------------------------------

    def _should_exclude(self, title: str) -> bool:
        title_lower = title.lower()
        for word in self.EXCLUDE_TITLE_WORDS:
            if re.search(r"\b" + re.escape(word) + r"\b", title_lower):
                return True
        return False

    def _extract_job_id(self, url: str) -> str:
        m = re.search(
            r"/jobs/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
            url,
        )
        return m.group(1) if m else ""

    # ------------------------------------------------------------------
    # Detail page
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
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_DESCRIPTION_SELECTOR, timeout=15000,
                )
            except Exception:
                pass
            soup = await self._get_soup(detail_page)
            return self._extract_description(soup)
        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        container = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)
        if not container:
            return ""
        for unwanted in container.select("script, style, noscript, meta"):
            unwanted.decompose()
        return self._clean_multiline_text(container.get_text(separator="\n"))

    # ------------------------------------------------------------------
    # Text utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        text = html.unescape(text or "").replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()

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
