from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class PayuScraper(BaseScraper):
    """
    Scraper for PayU careers (careers.payu.in).

    PayU uses a custom ATS (Taleo-like) with a traditional server-rendered
    table layout. This is NOT an Oracle Cloud SPA — no Knockout.js hydration
    needed.

    Expected listing card structure:

        <table id="searchresults">
          <tbody>
            <tr class="data-row">
              <td class="colFacility">
                <span class="jobFacility">1214</span>
              </td>
              <td class="colTitle">
                <span class="jobTitle hidden-phone">
                  <a href="/PayU/job/Gurgaon-Software-Engineer/3504980/"
                     class="jobTitle-link">Software Engineer</a>
                </span>
              </td>
              <td class="colLocation hidden-phone">
                <span class="jobLocation">Gurgaon, IN</span>
              </td>
              <td class="colDate hidden-phone">
                <span class="jobDate">12 Jun 2026</span>
              </td>
            </tr>
          </tbody>
        </table>

    Expected detail page structure:

        <span class="jobdescription">
          <p><span>Requirements<br>...</span></p>
        </span>

    Job ID is extracted from the URL path's last numeric segment
    (e.g., /PayU/job/Gurgaon-Software-Engineer/3504980/ → "3504980").
    """

    # ---- Listing page selectors ----
    CARD_SELECTOR = "tr.data-row"
    TITLE_SELECTOR = "a.jobTitle-link"
    LOCATION_SELECTOR = "span.jobLocation"
    DATE_SELECTOR = "span.jobDate"
    LINK_SELECTOR = "a.jobTitle-link"

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = "span.jobdescription"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            # PayU's job board is a traditional server-rendered page.
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Brief settle for any late-loading elements.
            await page.wait_for_timeout(3000)

            soup = await self._get_soup(page)
            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No PayU job cards found")
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
                            "Failed to enrich PayU detail page %s: %s",
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
        job_id = self._extract_job_id_from_url(link)
        location = self._extract_location(card)
        posted_date = self._extract_date(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "PayU"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=posted_date,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag, source_url: str) -> str:
        el = card.select_one(self.LINK_SELECTOR)
        if not el:
            return ""

        href = el.get("href", "")
        if not href:
            return ""

        return self._make_absolute_url(source_url, href)

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_location(self, card: Tag) -> str:
        el = card.select_one(self.LOCATION_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_date(self, card: Tag) -> str:
        el = card.select_one(self.DATE_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_job_id_from_url(self, url: str) -> str:
        """Extract the numeric job ID from a PayU job URL.

        Example:
            /PayU/job/Gurgaon-Software-Engineer/3504980/ → "3504980"
        """
        if not url:
            return ""
        # Match the last numeric segment before the trailing slash
        match = re.search(r"/(\d+)/?$", url)
        return match.group(1) if match else ""

    @staticmethod
    def _make_absolute_url(source_url: str, href: str) -> str:
        if href.startswith("http"):
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
                    self.DETAIL_DESCRIPTION_SELECTOR,
                    state="attached",
                    timeout=8000,
                )
            except Exception:
                self.logger.debug(
                    "Description selector not found for PayU job: %s", job_url
                )

            await detail_page.wait_for_timeout(2000)

            soup = await self._get_soup(detail_page)
            desc_el = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)

            if desc_el:
                return self._clean_text(desc_el.get_text())

            return ""
        finally:
            await detail_page.close()
