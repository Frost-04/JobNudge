from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class DynatraceScraper(BaseScraper):
    """
    Scraper for Dynatrace careers (www.dynatrace.com/careers/jobs/).

    Dynatrace uses a custom component-based page with data-slot/data-testid
    attributes. The listing is pre-filtered via the ?country=India query
    parameter. Job cards are server-rendered anchor tags.

    Expected listing card structure:

        a[data-dt-name="bobr-base-card"][data-slot="card"]
          div[data-slot="card-header"]
            p[data-slot="card-eyebrow"]              (department e.g. "SALES AND BUSINESS DEVELOPMENT")
            h2[data-slot="card-title"]                (job title)
          div[data-slot="card-footer"]
            div[data-slot="flex"]
              p[data-slot="text"] × N                (location parts e.g. "Bengaluru, ", "India")

    Expected detail page structure:

        div[data-slot="text"].job-detail-text         (full description HTML)
    """

    # ---- Listing page selectors ----
    CARD_SELECTOR = "a[data-dt-name='bobr-base-card'][data-slot='card']"
    TITLE_SELECTOR = "h2[data-slot='card-title']"
    DEPARTMENT_SELECTOR = "p[data-slot='card-eyebrow']"
    LOCATION_SELECTOR = "div[data-slot='card-footer'] p[data-slot='text']"

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = "div[data-slot='text'].job-detail-text"

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
                self.logger.warning("No Dynatrace job cards found")
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
                            "Failed to enrich Dynatrace detail page %s: %s",
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
        job_id = self._extract_job_id(link)
        location = self._extract_location(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Dynatrace"),
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
        href = card.get("href", "")
        if not href:
            return ""
        href = str(href)
        if href.startswith("http://") or href.startswith("https://"):
            return href
        # Make absolute if relative.
        if href.startswith("/"):
            return f"https://www.dynatrace.com{href}"
        return f"https://www.dynatrace.com/{href}"

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_location(self, card: Tag) -> str:
        """Extract location from the card footer.

        Location text is split across multiple p[data-slot='text']
        elements (e.g. "Bengaluru, " + "India"). We join them together.
        """
        els = card.select(self.LOCATION_SELECTOR)
        parts = []
        for el in els:
            text = self._clean_text(el.get_text())
            if text and text not in ("See more",):
                parts.append(text)
        # Join and clean up spaces around commas.
        combined = "".join(parts)
        combined = re.sub(r"\s*,\s*", ", ", combined)
        combined = re.sub(r",+", ",", combined)
        return combined.strip(", ")

    def _extract_job_id(self, url: str) -> str:
        """Extract the job ID from the URL.

        Example:
            https://www.dynatrace.com/careers/jobs/1355664400/  →  "1355664400"
        """
        if not url:
            return ""
        match = re.search(r"/jobs/(\d+)/", url)
        return match.group(1) if match else ""

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
