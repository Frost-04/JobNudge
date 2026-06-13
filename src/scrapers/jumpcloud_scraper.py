from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class JumpCloudScraper(BaseScraper):
    """
    Scraper for JumpCloud careers (jobs.lever.co/jumpcloud).

    JumpCloud uses Lever.co's ATS which serves server-rendered HTML.
    The listing page is pre-filtered via ?location=Bangalore&department=Engineering
    query parameters. Cards are structured div.posting elements.

    Expected listing card structure:

        div.posting[data-qa-posting-id="..."]
          a.posting-title[href]
            h5[data-qa="posting-name"]              (job title)
            div.posting-categories
              span.sort-by-location.location         (location e.g. "Bangalore, India - Remote")

    Expected detail page structure:

        div.section.page-centered[data-qa="job-description"]   (full description HTML)
    """

    # ---- Listing page selectors ----
    CARD_SELECTOR = "div.posting[data-qa-posting-id]"
    TITLE_SELECTOR = "h5[data-qa='posting-name']"
    LOCATION_SELECTOR = "span.sort-by-location.posting-category.small-category-label.location"
    LINK_SELECTOR = "a.posting-title"

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = "div.section.page-centered[data-qa='job-description']"

    # ---- Company domain for relative URL resolution ----
    BASE_DOMAIN = "https://jobs.lever.co"

    # ------------------------------------------------------------------
    # Main scrape method
    # ------------------------------------------------------------------

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for the page to settle.
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)

            # Wait for job cards to render.
            try:
                await page.wait_for_selector(self.CARD_SELECTOR, timeout=15000)
            except Exception:
                pass

            soup = await self._get_soup(page)
            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No JumpCloud job cards found")
                return jobs

            seen_job_ids: set[str] = set()
            seen_urls: set[str] = set()

            for card in cards[:max_jobs]:
                job = self._parse_card(card)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_job_ids:
                    continue

                if job.url in seen_urls:
                    continue

                # Enrich with detail page description.
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
                    job.description = None
                else:
                    try:
                        description = await self._scrape_detail_description(job.url)
                        if description:
                            job = Job(
                                job_id=job.job_id,
                                company=job.company,
                                title=job.title,
                                location=job.location,
                                url=job.url,
                                source_url=source_url,
                                posted_date=job.posted_date,
                                description=description,
                                scraped_at=datetime.now(timezone.utc).isoformat(),
                                extracted_experience_parts="",
                            )
                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich JumpCloud detail page %s: %s",
                            job.url,
                            exc,
                        )

                if job.job_id:
                    seen_job_ids.add(job.job_id)

                seen_urls.add(job.url)
                jobs.append(job)

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag) -> Job | None:
        link = self._extract_link(card)
        title = self._extract_title(card)
        job_id = self._extract_job_id(card)
        location = self._extract_location(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "JumpCloud"),
            title=title,
            location=location,
            url=link,
            source_url=self.company_config.get("url", ""),
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag) -> str:
        link_el = card.select_one(self.LINK_SELECTOR)
        if not link_el:
            return ""
        href = link_el.get("href", "")
        if not href:
            return ""
        href = str(href)
        if href.startswith("http://") or href.startswith("https://"):
            return href
        # Make absolute if relative.
        if href.startswith("/"):
            return f"{self.BASE_DOMAIN}{href}"
        return f"{self.BASE_DOMAIN}/{href}"

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_location(self, card: Tag) -> str:
        el = card.select_one(self.LOCATION_SELECTOR)
        if not el:
            return ""
        return self._clean_text(el.get_text())

    def _extract_job_id(self, card: Tag) -> str:
        """Extract the job ID from the data-qa-posting-id attribute."""
        job_id = card.get("data-qa-posting-id", "")
        return str(job_id).strip() if job_id else ""

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

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

    async def _scrape_detail_description(self, job_url: str) -> str:
        """Open the job detail page and extract the full description."""
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(
                job_url, wait_until="domcontentloaded", timeout=15000
            )

            # Wait for the page to settle.
            try:
                await detail_page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass

            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_DESCRIPTION_SELECTOR, timeout=10000
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

        # Remove non-description elements.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        lines = [
            " ".join(line.split())
            for line in text.splitlines()
            if line.strip()
        ]
        return "\n".join(lines)
