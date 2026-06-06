from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class AristaScraper(BaseScraper):
    """
    Scraper for Arista Networks Careers engineering page.

    Arista's career page at www.arista.com/en/careers/engineering is a
    simple server-rendered page with client-side JS filters.  The scraper
    loads the page once per target location, applies the location filter
    and search, then parses the resulting job table.

    Because the page does not support URL-based filtering, we reload it
    for each of the configured locations and deduplicate across runs.

    Filter/search UI:

        div.srSearchOption#facet_location
          span[data-filter-value="Bengaluru"]

        input#filter-by     (search box)
        input.srSearchButton (Search button)

    Results table:

        table.srJobList > tbody > tr (srJobListJobOdd / srJobListJobEven)
          td.srJobListJobTitle    → title
          td.srJobListLocation    → location (text inside span)
          onclick="window.open('https://jobs.smartrecruiters.com/...')" → URL

    Detail page (SmartRecruiters):

        div[itemprop="description"]
          section.job-section > div.wysiwyg
    """

    # ---- Page / filter selectors ----
    SOURCE_URL = "https://www.arista.com/en/careers/engineering"

    SEARCH_INPUT = "input#filter-by"
    SEARCH_BUTTON = "input.srSearchButton"
    SEARCH_TEXT = "Software Engineer"

    LOCATION_FILTER_CONTAINER = "div#facet_location"
    LOCATION_FILTER_ITEM = 'span[data-filter-value="{location}"]'

    # ---- Results table selectors ----
    TABLE_SELECTOR = "table.srJobList"
    ROW_SELECTOR = "table.srJobList tbody tr"

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = 'div[itemprop="description"]'

    # ---- Target locations ----
    TARGET_LOCATIONS = ["Bengaluru", "Pune", "Chennai"]

    async def scrape(self) -> list[Job]:
        source_url = self.SOURCE_URL
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        all_jobs: list[Job] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        page = await self.new_page()

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
        """Load the page, apply location filter + search, parse results."""
        jobs: list[Job] = []

        await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

        # ---- Step 1: Click location filter ----
        location_clicked = await self._click_location_filter(page, location)
        if not location_clicked:
            self.logger.warning("Could not apply location filter '%s' for Arista.", location)
            return jobs

        # ---- Step 2: Type search text and click Search ----
        await self._type_search(page)
        await self._click_search(page)

        # ---- Step 3: Wait for table to render ----
        try:
            await page.wait_for_selector(self.ROW_SELECTOR, timeout=10000)
        except Exception:
            self.logger.warning("Job table not found for Arista location '%s'.", location)
            return jobs

        soup = await self._get_soup(page)

        rows = soup.select(self.ROW_SELECTOR)
        if not rows:
            return jobs

        for row in rows:
            if len(jobs) + len(seen_urls) >= max_jobs:
                break

            job = self._parse_row(row, source_url, location)
            if not job:
                continue

            if job.job_id and job.job_id in seen_ids:
                continue
            if job.url in seen_urls:
                continue

            # ---- Step 4: Enrich with detail page ----
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
                    "Failed to enrich Arista job detail %s: %s",
                    job.url,
                    exc,
                )

            if job.job_id:
                seen_ids.add(job.job_id)
            seen_urls.add(job.url)
            jobs.append(job)

        return jobs

    # ------------------------------------------------------------------
    # Filter interactions
    # ------------------------------------------------------------------

    async def _click_location_filter(self, page: Page, location: str) -> bool:
        """Click the location filter span for the given location."""
        selector = self.LOCATION_FILTER_ITEM.format(location=location)

        # Wait for the filter container to be present
        try:
            await page.wait_for_selector('div#facet_location span.srSearchOptionText', timeout=10000)
        except Exception:
            pass

        locator = page.locator(selector)
        if not await locator.count():
            self.logger.warning(
                "Arista location filter '%s' not found in the DOM.", location
            )
            return False

        try:
            # The location list is scrollable and the target span may be
            # scrolled out of view.  Use page.evaluate to click it reliably.
            await page.evaluate(
                """(locName) => {
                    const spans = document.querySelectorAll('span[data-filter-value]');
                    for (const span of spans) {
                        if (span.getAttribute('data-filter-value') === locName) {
                            span.scrollIntoView({ block: 'center' });
                            span.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                location,
            )
            await page.wait_for_timeout(1500)
            return True
        except Exception as exc:
            self.logger.warning(
                "Could not click Arista location filter '%s': %s", location, exc
            )
            return False

    async def _type_search(self, page: Page) -> None:
        """Type the search text into the filter input."""
        search_input = page.locator(self.SEARCH_INPUT)
        if not await search_input.count():
            self.logger.warning("Arista search input not found.")
            return

        try:
            # Clear and type
            await search_input.click()
            await search_input.fill("")
            await search_input.type(self.SEARCH_TEXT, delay=50)
        except Exception as exc:
            self.logger.warning("Could not type in Arista search input: %s", exc)

    async def _click_search(self, page: Page) -> None:
        """Click the Search button."""
        search_btn = page.locator(self.SEARCH_BUTTON)
        if not await search_btn.count():
            self.logger.warning("Arista search button not found.")
            return

        try:
            await search_btn.click()
            await page.wait_for_timeout(2000)
        except Exception as exc:
            self.logger.warning("Could not click Arista search button: %s", exc)

    # ------------------------------------------------------------------
    # Row parsing
    # ------------------------------------------------------------------

    def _parse_row(self, row: Tag, source_url: str, location_context: str) -> Job | None:
        title = self._extract_title_from_row(row)
        link = self._extract_link_from_row(row)
        location = self._extract_location_from_row(row, location_context)
        job_id = self._extract_job_id_from_row(row, link)

        if not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Arista Networks"),
            title=title,
            location=location,
            url=link or "",
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_title_from_row(self, row: Tag) -> str:
        cell = row.select_one("td.srJobListJobTitle")
        if cell:
            return self._clean_text(cell.get_text())
        return ""

    def _extract_link_from_row(self, row: Tag) -> str:
        # URL is in the inline onclick: window.open("https://...")
        onclick = row.get("onclick", "")
        match = re.search(r"window\.open\([\"']([^\"']+)[\"']\)", onclick)
        if match:
            return html.unescape(match.group(1).strip())
        return ""

    def _extract_location_from_row(self, row: Tag, location_context: str) -> str:
        cell = row.select_one("td.srJobListLocation")
        if not cell:
            return location_context

        # Get text from the span, stripping any SVG/title elements (Hybrid icon)
        span = cell.select_one("span")
        if span:
            # Remove SVG elements
            for svg in span.select("svg"):
                svg.decompose()
            text = self._clean_text(span.get_text())
            if text:
                return text

        # Fallback: use the context location
        return f"{location_context}, India"

    def _extract_job_id_from_row(self, row: Tag, link: str) -> str:
        # SmartRecruiters URL: .../AristaNetworks/744000129697871-...
        if link:
            match = re.search(r"/(\d{10,})(?:-|/|$)", link)
            if match:
                return match.group(1)

        # Fallback: from onclick
        onclick = row.get("onclick", "")
        match = re.search(r"/(\d{10,})(?:-|/|$)", onclick)
        if match:
            return match.group(1)

        return ""

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def _get_detail_page(self) -> Page:
        """Return a new page for detail scraping."""
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

        sections: list[str] = []

        for section in container.select("section.job-section"):
            # Grab the section heading
            heading_el = section.select_one("h2.title")
            heading = self._clean_text(heading_el.get_text()) if heading_el else ""

            # Grab the content
            content_el = section.select_one("div.wysiwyg")
            content = ""
            if content_el:
                for unwanted in content_el.select("script, style, noscript"):
                    unwanted.decompose()
                content = content_el.get_text(separator="\n")
                content = self._clean_multiline_text(content)

            if heading and content:
                sections.append(f"{heading}\n{content}")
            elif content:
                sections.append(content)
            elif heading:
                sections.append(heading)

        return "\n\n".join(sections)

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
