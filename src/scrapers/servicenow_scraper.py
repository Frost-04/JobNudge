from __future__ import annotations

from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class ServiceNowScraper(BaseScraper):
    """
    Scraper for ServiceNow Careers job search pages.

    The search results page renders job cards:

        div#js-job-search-results.grid.job-listing
          div.card.card-job
            div.card-body
              h2.card-title > a.stretched-link.js-view-job[href]
              div.card-job-actions.js-job[data-id][data-jobtitle]
              ul.list-inline.job-meta
                li.list-inline-item (location)

    The detail page contains:

        article.cms-content
          h3 (section headers)
          p, ul (content)
    """

    # ---- Card selectors ----
    RESULTS_CONTAINER = "div#js-job-search-results"
    CARD_SELECTOR = "div.card.card-job"
    TITLE_LINK_SELECTOR = "a.stretched-link.js-view-job"
    JOB_ACTIONS_SELECTOR = "div.card-job-actions.js-job"
    LOCATION_SELECTOR = "ul.list-inline.job-meta li.list-inline-item"

    # ---- Detail page selectors ----
    DETAIL_CONTENT_SELECTOR = "article.cms-content"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for job cards to appear.
            await self._wait_for_results(page)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No ServiceNow job cards found.")
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

                # Enrich with detail page description.
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
                        "Failed to enrich ServiceNow job detail %s: %s",
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
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link_el = card.select_one(self.TITLE_LINK_SELECTOR)
        if not link_el:
            return None

        href = link_el.get("href")
        if not href:
            return None

        url = make_absolute_url(source_url, str(href))
        title = self._clean_text(link_el.get_text())

        if not title:
            return None

        # Job ID from data-id attribute on the js-job div.
        job_id = ""
        actions_el = card.select_one(self.JOB_ACTIONS_SELECTOR)
        if actions_el:
            data_id = actions_el.get("data-id")
            if data_id:
                job_id = str(data_id).strip()

        if not job_id:
            job_id = extract_job_id(url)

        # Location from list-inline-item(s).
        location_items = card.select(self.LOCATION_SELECTOR)
        locations: list[str] = []
        for item in location_items:
            loc_text = self._clean_text(item.get_text())
            if loc_text:
                locations.append(loc_text)
        location = ", ".join(locations)

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "ServiceNow"),
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
        """Return a new page for detail scraping, creating a fresh context if needed."""
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

            # Wait for the article content to load.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_CONTENT_SELECTOR,
                    timeout=15000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            article = soup.select_one(self.DETAIL_CONTENT_SELECTOR)
            if not article:
                return ""

            return self._extract_description(article)

        finally:
            await detail_page.close()

    def _extract_description(self, article: Tag) -> str:
        """Extract clean description text from the article.cms-content element."""
        # Remove script/style tags.
        for unwanted in article.select("script, style, noscript"):
            unwanted.decompose()

        # Collect content preserving section structure.
        sections: list[str] = []
        current_section: list[str] = []

        for child in article.children:
            if not hasattr(child, "name"):
                continue

            tag_name = child.name

            if tag_name in ("h1", "h2", "h3", "h4"):
                # Flush current section.
                if current_section:
                    sections.append("\n".join(current_section))
                    current_section = []

                heading = self._clean_text(child.get_text())
                if heading:
                    sections.append(heading)
            elif tag_name in ("p", "ul", "ol", "li"):
                text = self._clean_text(child.get_text())
                if text:
                    current_section.append(text)
            else:
                text = self._clean_text(child.get_text())
                if text:
                    current_section.append(text)

        if current_section:
            sections.append("\n".join(current_section))

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _wait_for_results(self, page: Page) -> None:
        """Wait for the job results container or first card to appear."""
        selectors = [
            self.RESULTS_CONTAINER,
            self.CARD_SELECTOR,
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

    @staticmethod
    def _clean_text(text: str) -> str:
        """Normalize whitespace in a string."""
        if not text:
            return ""
        return " ".join(text.split()).strip()
