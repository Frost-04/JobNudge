from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class WellsFargoScraper(BaseScraper):
    """
    Scraper for Wells Fargo job search pages.

    The listing page at ``wellsfargojobs.com/en/jobs/`` accepts query
    parameters for country and team filtering.  No Playwright filter
    interaction is needed — the URL already scopes the results.

    Expected listing card structure:

        div#js-job-search-results
          div.card.card-job
            div.card-body
              h2.card-title
                a.stretched-link[href^="/en/jobs/r-NNN/"]
              div.card-job-actions.js-job[data-id="r-NNN"][data-jobtitle="..."]
              ul.list-inline.job-meta
                li.list-inline-item          (location — contains map-marker svg)
                li.list-inline-item          (department — contains briefcase svg)

    Expected detail page structure:

        article.cms-content              (full job description)

    Pagination: clicks "Next" link (``a[aria-label="Next"]``) until all
    pages are scraped.
    """

    # ---- Card / listing selectors ----
    CARD_SELECTOR = "div.card.card-job"
    LINK_SELECTOR = "h2.card-title a.stretched-link"
    TITLE_SELECTOR = "h2.card-title a.stretched-link"
    JOB_ACTION_SELECTOR = "div.card-job-actions.js-job"
    META_SELECTOR = "ul.list-inline.job-meta"
    LOCATION_ITEM_SELECTOR = "ul.list-inline.job-meta > li.list-inline-item:first-child"

    JOB_CARD_SELECTORS = [
        "div.card.card-job",
        "div#js-job-search-results",
        "a.stretched-link[href*='/en/jobs/']",
    ]

    # ---- Pagination ----
    NEXT_BUTTON_SELECTOR = "a[aria-label='Next']:not(.disabled)"
    # ---- Detail page ----
    DETAIL_WAIT_SELECTORS = [
        "article.cms-content",
        "div.cms-content",
        "div.job-description",
    ]
    DETAIL_DESCRIPTION_SELECTOR = "article.cms-content, div.cms-content"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for cards to render.
            selector = await self._wait_for_any_selector(
                page, self.JOB_CARD_SELECTORS
            )

            if not selector:
                self.logger.warning(
                    "Wells Fargo: no card selectors matched, trying fallback."
                )
                return await self._fallback_links(page, source_url, max_jobs)

            seen_job_ids: set[str] = set()
            seen_urls: set[str] = set()

            # Paginate through all result pages.
            while len(jobs) < max_jobs:
                soup = await self._get_soup(page)
                cards = soup.select(self.CARD_SELECTOR)

                if not cards:
                    break

                for card in cards:
                    if len(jobs) >= max_jobs:
                        break

                    job = self._parse_card(card, source_url)

                    if not job:
                        continue

                    if job.job_id and job.job_id in seen_job_ids:
                        continue

                    if job.url in seen_urls:
                        continue

                    # Enrich from detail page (non-excluded only).
                    if self._should_exclude(job.title):
                        self.logger.debug(
                            "Skipping detail enrichment for excluded role: %s",
                            job.title,
                        )
                    else:
                        try:
                            detail_data = await self._scrape_detail_page(job.url)

                            detail_description = detail_data.get("description", "")

                            if detail_description:
                                job = Job(
                                    job_id=job.job_id,
                                    company=job.company,
                                    title=job.title,
                                    location=job.location,
                                    url=job.url,
                                    source_url=job.source_url,
                                    posted_date=job.posted_date,
                                    description=detail_description,
                                    scraped_at=datetime.now(timezone.utc).isoformat(),
                                    extracted_experience_parts="",
                                )
                        except Exception as exc:
                            self.logger.warning(
                                "Failed to enrich Wells Fargo detail page %s: %s",
                                job.url,
                                exc,
                            )

                    if job.job_id:
                        seen_job_ids.add(job.job_id)
                    seen_urls.add(job.url)
                    jobs.append(job)

                # Try to click "Next" for pagination.
                if not await self._click_next_page(page):
                    break

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    async def _click_next_page(self, page: Page) -> bool:
        """Click the "Next" pagination button.  Returns False if no more pages."""
        try:
            next_btn = page.locator(self.NEXT_BUTTON_SELECTOR)

            if await next_btn.count() > 0:
                await next_btn.first.click(timeout=5000)
                # Wait for new cards to load.
                await page.wait_for_selector(
                    self.CARD_SELECTOR, timeout=15000
                )
                await page.wait_for_timeout(1000)
                return True
        except Exception:
            pass

        return False

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card, source_url)
        title = self._extract_title(card)
        job_id = self._extract_job_id(card, link)
        location = self._extract_location(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Wells Fargo"),
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
        el = card.select_one(self.LINK_SELECTOR)

        if not el:
            return ""

        href = el.get("href")

        if not href:
            return ""

        return make_absolute_url(source_url, str(href))

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)

        if el:
            title = self._clean_text(el.get_text())

            if title:
                return title

        # Fallback: use data-jobtitle attribute.
        actions = card.select_one(self.JOB_ACTION_SELECTOR)

        if actions and actions.get("data-jobtitle"):
            return self._clean_text(str(actions.get("data-jobtitle")))

        return ""

    def _extract_job_id(self, card: Tag, link: str) -> str:
        # Primary: data-id attribute on the js-job div.
        actions = card.select_one(self.JOB_ACTION_SELECTOR)

        if actions and actions.get("data-id"):
            return self._clean_text(str(actions.get("data-id")))

        # Fallback: URL path like /en/jobs/r-533359/...
        match = re.search(r"/jobs/([^/?#]+)/", link, flags=re.IGNORECASE)

        if match:
            return self._clean_text(match.group(1))

        return extract_job_id(link) or ""

    def _extract_location(self, card: Tag) -> str:
        # The first li.list-inline-item inside ul.job-meta contains the location.
        meta = card.select_one(self.META_SELECTOR)

        if not meta:
            return ""

        items = meta.select("li.list-inline-item")

        if not items:
            return ""

        # First list item = location (has map-marker svg).
        location = self._clean_location_text(items[0].get_text())

        return location

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

            await self._wait_for_any_selector(
                detail_page, self.DETAIL_WAIT_SELECTORS
            )

            soup = await self._get_soup(detail_page)

            description = self._extract_detail_description(soup)

            result: dict[str, str] = {}

            if description:
                result["description"] = description

            return result

        finally:
            await detail_page.close()

    def _extract_detail_description(self, soup) -> str:
        container = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)

        if not container:
            return ""

        for unwanted in container.select("script, style, noscript, svg, button"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _wait_for_any_selector(
        self, page: Page, selectors: list[str]
    ) -> str | None:
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

    def _clean_location_text(self, text: str) -> str:
        text = self._clean_text(text)

        if not text:
            return ""

        lower_text = text.lower()
        noise_values = {
            "location",
            "locations",
            "save",
            "saved",
            "remove",
            "technology",
        }

        if lower_text in noise_values:
            return ""

        return text

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        text = html.unescape(text)
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        if not text:
            return ""
        text = html.unescape(text)
        text = text.replace("\xa0", " ")

        lines: list[str] = []
        previous_line = ""

        for line in text.splitlines():
            clean_line = " ".join(line.split())

            if not clean_line:
                continue

            if clean_line == previous_line:
                continue

            lines.append(clean_line)
            previous_line = clean_line

        return "\n".join(lines).strip()
