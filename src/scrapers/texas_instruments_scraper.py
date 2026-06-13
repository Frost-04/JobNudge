from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class TexasInstrumentsScraper(BaseScraper):
    """
    Scraper for Texas Instruments Oracle Cloud Candidate Experience pages.

    Texas Instruments uses the Oracle HCM Cloud Candidate Experience
    platform (same as Chubb, JPMorgan Chase, American Express).

    Expected listing card structure (LIST view):

        ul#panel-list.jobs-list__list
          li[data-qa="searchResultItem"]
            div.job-tile.job-list-item
              a.job-list-item__link[href*="/job/{id}/"]
              span.job-tile__title
              posting-locations span[data-bind*="primaryLocation"]
              div.job-list-item__job-info-label--posting-date
              div.job-list-item__job-info-value (posted date)

    Expected detail page / overlay structure:

        h1.job-details__title
        div.job-details__description-content.basic-formatter

    Detail pages open as a popup/overlay. Direct URL access is attempted
    first; if blocked, falls back to card-level data.
    """

    # ---- Listing page selectors ----
    JOB_CARD_SELECTORS = [
        "ul#panel-list li[data-qa='searchResultItem']",
        "li[data-qa='searchResultItem']",
        "a[href*='/job/']",
    ]

    CARD_SELECTOR = (
        "ul#panel-list li[data-qa='searchResultItem'], "
        "li[data-qa='searchResultItem']"
    )
    LINK_SELECTOR = "a.job-list-item__link[href*='/job/'], a[href*='/job/']"
    TITLE_SELECTOR = "span.job-tile__title"

    # ---- Detail page selectors ----
    DETAIL_TITLE_SELECTOR = "h1.job-details__title"
    DETAIL_DESCRIPTION_SELECTOR = "div.job-details__description-content"

    # TI's Oracle Cloud instance is unusually slow.  To keep scraping
    # practical we cap at 15 jobs from the top of the listing.
    MAX_JOBS = 15

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")

        jobs: list[Job] = []

        try:
            # Oracle Cloud pages: use "commit" (fastest) — "networkidle" and
            # "domcontentloaded" can hang due to analytics/chat resources.
            await page.goto(source_url, wait_until="commit", timeout=30000)

            # Give Knockout.js time to render the full card DOM.
            # TI's Oracle Cloud instance is slower — needs ~12s to render cards.
            await page.wait_for_timeout(15000)

            # Verify cards are present via JS evaluation.
            card_count = await page.evaluate(
                "() => document.querySelectorAll('span.job-tile__title').length"
            )
            if card_count == 0:
                self.logger.warning(
                    "No Texas Instruments job cards found after render"
                )
                return await self._fallback_links(page, source_url, self.MAX_JOBS)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                return await self._fallback_links(page, source_url, self.MAX_JOBS)

            seen_job_ids: set[str] = set()
            seen_urls: set[str] = set()

            for card in cards[:self.MAX_JOBS]:
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_job_ids:
                    continue

                if job.url in seen_urls:
                    continue

                # Enrich with detail page data.
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
                            "Failed to enrich TI detail page %s: %s",
                            job.url,
                            exc,
                        )

                if job.job_id:
                    seen_job_ids.add(job.job_id)

                seen_urls.add(job.url)
                jobs.append(job)

            if not jobs:
                jobs = await self._fallback_links(page, source_url, self.MAX_JOBS)

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card, source_url)
        title = self._extract_title(card)
        job_id = self._extract_job_id(card, link)
        location = self._extract_location(card)
        posted_date = self._extract_posted_date_from_card(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Texas Instruments"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=posted_date or None,
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

        return self._make_job_url(source_url, str(href))

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """Extract job ID from URL path /job/{id}/ or aria-labelledby/srch id."""
        if link:
            job_id = self._extract_job_id_from_url(link)
            if job_id:
                return job_id

        labelled_link = card.select_one("[aria-labelledby]")
        if labelled_link:
            aria_labelledby = labelled_link.get("aria-labelledby")
            if aria_labelledby and str(aria_labelledby).isdigit():
                return str(aria_labelledby)

        header = card.select_one("search-result-item-header[id]")
        if header:
            header_id = header.get("id")
            if header_id and str(header_id).isdigit():
                return str(header_id)

        return extract_job_id(link) if link else ""

    def _extract_location(self, card: Tag) -> str:
        """Primary location from posting-locations web component."""
        posting_locations = card.select_one("posting-locations")

        if posting_locations:
            primary_span = posting_locations.select_one(
                "span[data-bind*='primaryLocation']"
            )
            if primary_span:
                location_text = self._clean_location_text(primary_span.get_text())
                if location_text:
                    return location_text

        return ""

    def _extract_posted_date_from_card(self, card: Tag) -> str:
        """Extract posted date from the card's job-info items.

        Looks for div.job-list-item__job-info-label--posting-date
        within an li.job-list-item__job-info-item, then finds the
        sibling div.job-list-item__job-info-value.
        """
        label_el = card.select_one(
            "div.job-list-item__job-info-label--posting-date"
        )
        if label_el:
            # Navigate up to the li parent, then find the value div within it.
            parent_li = label_el.find_parent("li", class_="job-list-item__job-info-item")
            if parent_li:
                value_el = parent_li.select_one("div.job-list-item__job-info-value")
                if value_el:
                    date_text = self._clean_text(value_el.get_text())
                    match = re.search(r"\d{1,2}/\d{1,2}/\d{4}", date_text)
                    if match:
                        return match.group(0)

        # Fallback: regex over full card text.
        text = self._clean_text(card.get_text(" "))
        match = re.search(
            r"Posting\s+Date\s+(\d{1,2}/\d{1,2}/\d{4})",
            text,
            flags=re.IGNORECASE,
        )
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
                self.logger.debug(
                    "Shared browser context is no longer usable; creating a fresh one."
                )
                await self.close_browser()

        return await self.new_page()

    async def _scrape_detail_description(self, job_url: str) -> str:
        """Open the job detail page and extract the full description."""
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(15000)

            await detail_page.goto(job_url, wait_until="commit", timeout=15000)

            # Wait for Knockout to render the detail panel.
            # TI's Oracle Cloud instance is slower — needs ~9s to render.
            await detail_page.wait_for_timeout(10000)

            soup = await self._get_soup(detail_page)

            return self._extract_detail_description(soup)

        finally:
            await detail_page.close()

    def _extract_detail_description(self, soup: Tag) -> str:
        """Extract clean description text from the description container."""
        desc_el = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)
        if not desc_el:
            return ""

        # Remove unwanted elements.
        for tag in desc_el.select("script, style, img, svg"):
            tag.decompose()

        # Gather text from heading, paragraph, list, and bold elements.
        parts: list[str] = []
        for el in desc_el.select("h1, h2, h3, h4, h5, h6, p, ul, ol, li, b, strong"):
            text = self._clean_text(el.get_text())
            if not text:
                continue
            if el.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                text = f"\n{text}\n"
            elif el.name in ("li",):
                text = f"- {text}"
            elif el.name in ("b", "strong"):
                text = f"\n{text}"
            parts.append(text)

        return "\n".join(parts) if parts else self._clean_text(desc_el.get_text())

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _clean_location_text(text: str) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _make_job_url(self, source_url: str, href: str) -> str:
        """Resolve relative or absolute href against the source URL."""
        if href.startswith("http"):
            return href
        parsed = urlparse(source_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if href.startswith("/"):
            return f"{base}{href}"
        return f"{base}/{href}"

    def _extract_job_id_from_url(self, url: str) -> str:
        """Extract the numeric job ID from a URL path like /job/25010909/."""
        match = re.search(r"/job/(\d+)", url)
        if match:
            return match.group(1)
        return ""

    async def _fallback_links(
        self, page: Page, source_url: str, max_jobs: int
    ) -> list[Job]:
        """Fallback: grab any anchor with /job/ in its href."""
        soup = await self._get_soup(page)
        links = soup.select("a[href*='/job/']")

        jobs: list[Job] = []
        seen: set[str] = set()

        for link in links[:max_jobs]:
            href = link.get("href", "")
            if not href:
                continue

            url = self._make_job_url(source_url, href)
            if url in seen:
                continue
            seen.add(url)

            job_id = self._extract_job_id_from_url(url)
            title = self._clean_text(link.get_text())

            if not title:
                continue

            jobs.append(
                Job(
                    job_id=job_id,
                    company=self.company_config.get("name", "Texas Instruments"),
                    title=title,
                    location="",
                    url=url,
                    source_url=source_url,
                    posted_date=None,
                    description=None,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    extracted_experience_parts="",
                )
            )

        return jobs
