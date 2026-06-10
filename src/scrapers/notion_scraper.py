from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id


class NotionScraper(BaseScraper):
    """
    Scraper for Notion careers page (Next.js SPA → AshbyHQ detail pages).

    The listing page at ``notion.com/careers`` uses URL hash parameters
    for filtering — no dynamic filter interaction is needed.  Jobs are
    grouped into ``<section>`` elements by department.  Cards are
    ``<a>`` tags linking to ``jobs.ashbyhq.com/notion/{uuid}``.

    Detail pages (AshbyHQ) have the full description inside
    ``div._descriptionText_5yu8i_201``.

    **Multi-URL support**: The config can specify either a single ``url``
    or a list ``urls``.  All URLs are scraped with cross-URL deduplication.

    Expected listing card structure:

        <li class="openPositions_jobsListItem__0mSS9">
          <a href="https://jobs.ashbyhq.com/notion/UUID"
             class="jobPosting_jobLink__VOc2Y">
            <div class="jobPosting_jobTitle__AbyvH">TITLE</div>
            <div class="jobPosting_jobLocation__Q1A3S">LOCATION</div>
          </a>
        </li>

    Expected detail page structure:

        <div class="_descriptionText_5yu8i_201">
          <h1>Who We Are</h1>
          ... RICH HTML DESCRIPTION
        </div>
    """

    # ---- Card selectors ----
    CARD_SELECTOR = "a.jobPosting_jobLink__VOc2Y, a[href*='jobs.ashbyhq.com/notion/']"
    JOB_CARD_SELECTORS = [
        "a.jobPosting_jobLink__VOc2Y",
        "a[href*='jobs.ashbyhq.com/notion/']",
        "section[id*='open-positions'] ul li a",
    ]

    # ---- Detail page selectors (AshbyHQ) ----
    DETAIL_DESCRIPTION_SELECTOR = "div._descriptionText_5yu8i_201"

    # ------------------------------------------------------------------
    #  Multi-URL support
    # ------------------------------------------------------------------

    def _get_urls(self) -> list[str]:
        """Return all URLs to scrape — supports single ``url`` or list ``urls``."""
        urls = self.company_config.get("urls")

        if urls and isinstance(urls, list):
            return [str(u).strip() for u in urls if str(u).strip()]

        url = self.company_config.get("url", "")
        return [url] if url else []

    # ------------------------------------------------------------------
    #  Main entry point
    # ------------------------------------------------------------------

    async def scrape(self) -> list[Job]:
        urls = self._get_urls()
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        all_jobs: list[Job] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        for source_url in urls:
            if len(all_jobs) >= max_jobs:
                break

            jobs = await self._scrape_single_url(source_url, max_jobs, seen_ids, seen_urls)
            all_jobs.extend(jobs)

        return all_jobs[:max_jobs]

    async def _scrape_single_url(
        self, source_url: str, max_jobs: int,
        seen_ids: set[str], seen_urls: set[str],
    ) -> list[Job]:
        page = await self.new_page()
        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)

            # Scroll to trigger lazy rendering if any.
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1000)
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(1000)
            except Exception:
                pass

            # Wait for cards to appear.
            await self._wait_for_cards(page)
            await page.wait_for_timeout(1000)

            soup = await self._get_soup(page)
            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning(
                    "No Notion job cards found for URL: %s", source_url
                )
                return jobs

            for card in cards:
                if len(jobs) + len(seen_ids) >= max_jobs:
                    break

                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_ids:
                    continue
                if job.url in seen_urls:
                    continue

                # Enrich from detail page (non-excluded roles only).
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
                            "Failed to enrich Notion detail page %s: %s",
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
        link = card.get("href", "")
        if not link:
            return None

        # Card is the <a> itself or the <a> is a child.
        # Look for the title div directly under this element.
        title_el = card.select_one("div.jobPosting_jobTitle__AbyvH")
        if title_el:
            title = self._clean_text(title_el.get_text())
        else:
            # Fallback: find any div with jobTitle class.
            title_el = card.select_one("[class*='jobTitle']")
            title = self._clean_text(title_el.get_text()) if title_el else ""

        location_el = card.select_one("div.jobPosting_jobLocation__Q1A3S")
        if location_el:
            location = self._clean_text(location_el.get_text())
        else:
            location_el = card.select_one("[class*='jobLocation']")
            location = self._clean_text(location_el.get_text()) if location_el else ""

        if not title:
            return None

        # AshbyHQ job URLs: jobs.ashbyhq.com/notion/UUID
        job_id = self._extract_job_id(link)

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Notion"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date="",
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_job_id(self, url: str) -> str:
        """AshbyHQ URLs: ...jobs.ashbyhq.com/notion/UUID"""
        if not url:
            return ""

        match = re.search(r"/notion/([a-f0-9-]{36})", url, flags=re.IGNORECASE)
        if match:
            return match.group(1)

        return extract_job_id(url) or ""

    # ------------------------------------------------------------------
    # Wait helpers
    # ------------------------------------------------------------------

    async def _wait_for_cards(self, page: Page) -> None:
        """Wait for at least one card selector to match on the page."""
        for selector in self.JOB_CARD_SELECTORS:
            try:
                await page.wait_for_selector(selector, timeout=15000)
                return
            except Exception:
                continue

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
        """
        Open an AshbyHQ job detail page and extract the full description.
        """
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)

            await detail_page.goto(
                job_url,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await detail_page.wait_for_timeout(3000)

            description = ""
            try:
                desc_el = detail_page.locator(
                    self.DETAIL_DESCRIPTION_SELECTOR
                ).first
                await desc_el.wait_for(state="visible", timeout=15000)
                html_content = await desc_el.evaluate("el => el.innerHTML")
                description = self._clean_text(html_content)
            except Exception:
                # Fallback: any div with _descriptionText
                try:
                    desc_el = detail_page.locator(
                        "div[class*='_descriptionText']"
                    ).first
                    if await desc_el.is_visible(timeout=3000):
                        html_content = await desc_el.evaluate("el => el.innerHTML")
                        description = self._clean_text(html_content)
                except Exception:
                    pass

            return {"description": description}

        finally:
            try:
                await detail_page.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        return " ".join(text.split()).strip()
