from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class DatabricksScraper(BaseScraper):
    """Scraper for Databricks careers page (custom design).

    The careers page renders all jobs in department-grouped sections.
    Each department has an ``<h2>`` heading followed by a bordered ``<div>``
    containing ``<a>`` cards. Clicking a card opens the detail page in
    a new tab — we scrape detail pages directly.

    Job cards:

        div#jobWrap[data-cy="joblist"]
          h2                                   (department name)
          div.border-gray-lines.border
            a[href*="/company/careers/"][aria-label]
              span.text-1\\.5                   (title)
              span.border-l-gray-lines          (location)

    Job IDs are numeric and appear at the end of the ``aria-label``
    (e.g. "Director of Engineering 7896551002") and also in the URL slug.

    Detail page:

        div.rich-text-blog.rich-text-body.b2.w-full
          p, strong, ul, li
    """

    # ---- Listing page selectors ----
    CARD_SELECTOR = '#jobWrap a[href*="/company/careers/"]'
    TITLE_SPAN_SELECTOR = "span"  # first span in the <a>
    LOCATION_SPAN_SELECTOR = "span.border-l-gray-lines"

    # ---- Detail page selectors ----
    DETAIL_CONTENT_SELECTOR = "div.rich-text-blog.rich-text-body.b2.w-full"

    # Job ID pattern from aria-label or URL
    JOB_ID_FROM_URL = re.compile(r"-(\d{6,})$")  # trailing digits in URL slug
    JOB_ID_FROM_ARIA = re.compile(r"(\d{6,})$")  # trailing digits in aria-label

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="networkidle", timeout=120000)

            # Wait for job cards to render.
            try:
                await page.wait_for_selector(self.CARD_SELECTOR, timeout=60000)
            except Exception:
                self.logger.warning("No Databricks job cards found.")
                return jobs

            soup = await self._get_soup(page)
            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No Databricks job cards in parsed HTML.")
                return jobs

            self.logger.info("Found %d Databricks job cards.", len(cards))

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
                            "Failed to enrich Databricks job detail %s: %s",
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
        """Parse a Databricks job card ``<a>`` element."""
        href = card.get("href")
        if not href:
            return None

        url = make_absolute_url(source_url, str(href))

        # Title: first <span> child of the <a>.
        title = ""
        spans = card.select("span")
        if spans:
            title = self._clean_text(spans[0].get_text())

        if not title:
            return None

        # Location: second <span> (has border-l-gray-lines class).
        location = ""
        if len(spans) >= 2:
            location = self._clean_text(spans[1].get_text())

        # Job ID: prefer aria-label trailing number, fall back to URL slug.
        job_id = ""
        aria_label = card.get("aria-label", "")
        if aria_label:
            m = self.JOB_ID_FROM_ARIA.search(aria_label)
            if m:
                job_id = m.group(1)

        if not job_id:
            m = self.JOB_ID_FROM_URL.search(url)
            if m:
                job_id = m.group(1)

        # Try the P- prefix pattern from detail pages (e.g. "P-1384").
        # Not available at card level, but we don't need it here.

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Databricks"),
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
        """Navigate to a Databricks job detail page and extract the description."""
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(15000)
            await detail_page.goto(
                job_url, wait_until="domcontentloaded", timeout=90000
            )

            # Wait for the description container.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_CONTENT_SELECTOR, timeout=30000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            desc_container = soup.select_one(self.DETAIL_CONTENT_SELECTOR)
            if not desc_container:
                return ""

            return self._extract_description(desc_container)

        finally:
            await detail_page.close()

    def _extract_description(self, container: Tag) -> str:
        """Extract clean description text from a Databricks detail page."""
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        sections: list[str] = []
        current_section: list[str] = []

        for child in container.children:
            if not hasattr(child, "name"):
                continue

            tag_name = child.name

            if tag_name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                if current_section:
                    sections.append("\n".join(current_section))
                    current_section = []
                heading = self._clean_text(child.get_text())
                if heading:
                    sections.append(heading)
            elif tag_name in ("b", "strong"):
                text = self._clean_text(child.get_text())
                if text:
                    current_section.append(text)
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

    @staticmethod
    def _clean_text(text: str) -> str:
        """Normalize whitespace in a string."""
        if not text:
            return ""
        return " ".join(text.split()).strip()
