from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class OmnissaScraper(BaseScraper):
    """
    Scraper for Omnissa Careers job listing page.

    Omnissa's career page at www.omnissa.com/careers/jobs/ is a server-rendered
    page where filters are set via URL query parameters (location, department,
    job_type) and all matching cards render on a single page — no pagination,
    no AJAX filter interactions needed.

    Card structure (CSS-module hashed classes):

        div._jobRow_xrffz_162
          div._jobColumn_xrffz_174._longColumn_xrffz_192
            a[href^="/careers/jobs/"]
              h6[title="Job Title"]
          div._jobColumn_xrffz_174._locationColumn_xrffz_186
            div[title="Bengaluru, India"]

    Detail page structure:

        div.list-styling
          p, ul, li  (rich HTML description)
    """

    # ---- Card selectors (CSS-module prefix matching) ----
    CARD_SELECTOR = 'div[class^="_jobRow_"]'
    TITLE_LINK_SELECTOR = 'a[href^="/careers/jobs/"]'
    TITLE_SELECTOR = "h6"
    LOCATION_CONTAINER_SELECTOR = 'div[class*="_locationColumn_"]'

    # ---- Detail page selectors ----
    DETAIL_CONTENT_SELECTOR = "div.list-styling"

    # ------------------------------------------------------------------
    # Main scrape entry point
    # ------------------------------------------------------------------

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for job cards to appear.
            await self._wait_for_results(page)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No Omnissa job cards found.")
                return jobs

            self.logger.info("Found %d Omnissa job cards.", len(cards))

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
                            "Failed to enrich Omnissa job detail %s: %s",
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
        # Find the job link and title.
        link_el = card.select_one(self.TITLE_LINK_SELECTOR)
        if not link_el:
            return None

        href = link_el.get("href")
        if not href:
            return None

        url = self._make_omnissa_job_url(source_url, str(href))

        # Title from <h6> element (or its title attribute as fallback).
        title_el = link_el.select_one(self.TITLE_SELECTOR)
        title = ""
        if title_el:
            title = title_el.get("title", "").strip() or self._clean_text(
                title_el.get_text()
            )
        if not title:
            return None

        # Job ID from URL slug: /careers/jobs/india-devops-engineer
        job_id = self._extract_job_id_from_url(url)

        # Location from the location column div's title attribute.
        location = self._extract_location(card)

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Omnissa"),
            title=title,
            location=location,
            url=url,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_job_id_from_url(self, url: str) -> str:
        """Extract job ID from URL path segment.

        URLs look like: /careers/jobs/india-devops-engineer
        The last path segment serves as a unique identifier.
        Falls back to extract_job_id utility for any numeric IDs.
        """
        if not url:
            return ""

        # Use the URL path slug as the job ID.
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        segments = path.split("/")
        if segments:
            slug = segments[-1]
            if slug:
                return slug

        # Fallback to numeric extraction.
        return extract_job_id(url)

    def _extract_location(self, card: Tag) -> str:
        """Extract location from the location column's title attribute."""
        location_col = card.select_one(self.LOCATION_CONTAINER_SELECTOR)
        if not location_col:
            return ""

        # The inner div has a title attribute with the full location.
        inner_div = location_col.select_one("div")
        if inner_div:
            loc = inner_div.get("title", "").strip()
            if loc:
                return loc

        # Fallback: text content.
        return self._clean_text(location_col.get_text())

    def _make_omnissa_job_url(self, source_url: str, href: str) -> str:
        """Construct an absolute job URL from a relative href."""
        if href.startswith("http"):
            return href

        parsed = urlparse(source_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        return base + href

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def _get_detail_page(self) -> Page:
        """Return a new page, creating a fresh context if needed."""
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

            await detail_page.goto(
                job_url, wait_until="domcontentloaded", timeout=60000
            )

            # Wait for the description content to load.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_CONTENT_SELECTOR,
                    timeout=15000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            container = soup.select_one(self.DETAIL_CONTENT_SELECTOR)
            if not container:
                return ""

            return self._extract_description(container)

        finally:
            await detail_page.close()

    def _extract_description(self, container: Tag) -> str:
        """
        Extract clean description text from ``div.list-styling``.

        Preserves structure by keeping bold-text paragraphs (``<b>``) as
        section markers and collecting paragraph / list content beneath.
        """
        # Remove script/style tags.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        sections: list[str] = []
        current_lines: list[str] = []

        for child in container.descendants:
            if not hasattr(child, "name"):
                continue

            tag_name = child.name

            # Headings act as section dividers.
            if tag_name in ("h2", "h3", "h4"):
                if current_lines:
                    sections.append(" ".join(current_lines))
                    current_lines = []
                heading = self._clean_text(child.get_text())
                if heading:
                    sections.append(heading)

            elif tag_name == "b":
                parent_tag = (
                    child.parent.name
                    if child.parent and hasattr(child.parent, "name")
                    else ""
                )
                # <p><b>Section Title</b></p> → treat as section header.
                if parent_tag == "p":
                    if current_lines:
                        sections.append(" ".join(current_lines))
                        current_lines = []
                    label = self._clean_text(child.get_text())
                    if label:
                        sections.append(label)

            elif tag_name in ("p", "li"):
                text = self._clean_text(child.get_text())
                if text:
                    current_lines.append(text)

        if current_lines:
            sections.append(" ".join(current_lines))

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _wait_for_results(self, page: Page) -> None:
        """Wait for the first job card or results list to appear."""
        selectors = [
            self.CARD_SELECTOR,
            'div[class*="_bottomContainerWrapper_"]',
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
