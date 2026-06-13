from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class ZetaScraper(BaseScraper):
    """
    Scraper for Zeta careers (www.zeta.tech/in/careers/work-with-us/).

    Zeta uses a custom WordPress-style ATS with a traditional server-rendered
    listing page. Jobs are listed as cards inside an unordered list.

    Expected listing card structure:

        ul.departments__list
          li
            a.apply-card.link[href*="/in/careers/job-description/"]
              div.card-header
                div.title                 (department e.g. "Omega")
                div.type                  (job type e.g. "Full-time")
              div.sub-title.ibm-plex      (job title)
              div.footer
                span.dot                  (location)

    Expected detail page structure:

        p.description                     (full description HTML)
    """

    # ---- Listing page selectors ----
    CARD_SELECTOR = "a.apply-card.link"
    TITLE_SELECTOR = "div.sub-title.ibm-plex"
    LOCATION_SELECTOR = "span.dot"
    LINK_SELECTOR = "a.apply-card.link"

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = "p.description"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for job cards to render.
            try:
                await page.wait_for_selector(self.CARD_SELECTOR, timeout=15000)
            except Exception:
                pass

            # Brief settle for any late-loading elements.
            await page.wait_for_timeout(3000)

            soup = await self._get_soup(page)
            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No Zeta job cards found")
                return jobs

            seen_job_ids: set[str] = set()
            seen_urls: set[str] = set()

            for card in cards[:max_jobs]:
                job = self._parse_card(card, source_url)

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
                                source_url=job.source_url,
                                posted_date=job.posted_date,
                                description=description,
                                scraped_at=datetime.now(timezone.utc).isoformat(),
                                extracted_experience_parts="",
                            )
                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich Zeta detail page %s: %s",
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

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card, source_url)
        title = self._extract_title(card)
        job_id = self._extract_job_id(link)
        location = self._extract_location(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Zeta"),
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
        href = card.get("href", "")
        if not href:
            return ""

        return self._make_absolute_url(source_url, str(href))

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_location(self, card: Tag) -> str:
        el = card.select_one(self.LOCATION_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_job_id(self, url: str) -> str:
        """Extract the UUID from a Zeta job URL.

        Example:
            /in/careers/job-description/?a21f18a4-c899-4743-9264-e506990bcdcb
            → "a21f18a4-c899-4743-9264-e506990bcdcb"
        """
        if not url:
            return ""
        # Match the UUID query parameter value
        match = re.search(r"[?&]([a-f0-9-]{32,36})\b", url)
        return match.group(1) if match else ""

    def _make_absolute_url(self, source_url: str, href: str) -> str:
        if href.startswith("http://") or href.startswith("https://"):
            return href
        parsed = urlparse(source_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if href.startswith("/"):
            return f"{base}{href}"
        return f"{base}/{href}"

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

            # Wait for the description to appear.
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
