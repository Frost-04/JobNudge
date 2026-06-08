from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class BloombergScraper(BaseScraper):
    """
    Scraper for Bloomberg Careers (Avature platform) job search pages.

    Expected search card structure:

        article.article--result
          div.article__header
            div.article__header__text
              h3.article__header__text__title
                a.link[href="/careers/JobDetail/{slug}/{jobId}"]
              div.article__header__text__subtitle
                span.list-item-location
          div.article__footer
            a.button--primary[href]    (detail link)
            a.button--secondary[href]  (SaveJob?jobId=...)

    Expected detail page structure:

        div.article__content__view__field.field--rich-text
          div.article__content__view__field__value
            div, strong, ul, li, br   (rich HTML description)
    """

    # ---- Card selectors ----
    CARD_SELECTOR = "article.article--result"
    TITLE_SELECTOR = "h3.article__header__text__title a.link"
    LOCATION_SELECTOR = "span.list-item-location"
    SAVE_LINK_SELECTOR = "a.button--secondary[href*='SaveJob']"

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = "div.article__content__view__field.field--rich-text div.article__content__view__field__value"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            await self._wait_for_results(page)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No Bloomberg job cards found.")
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
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
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
                            "Failed to enrich Bloomberg job detail %s: %s",
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
        link_el = card.select_one(self.TITLE_SELECTOR)

        if not link_el:
            return None

        href = link_el.get("href")

        if not href:
            return None

        url = str(href)

        title = self._clean_text(link_el.get_text())

        if not title:
            return None

        # Job ID from URL path (last segment after /)
        job_id = self._extract_job_id_from_url(url)

        if not job_id:
            # Fallback: SaveJob?jobId=... link
            save_el = card.select_one(self.SAVE_LINK_SELECTOR)
            if save_el:
                save_href = save_el.get("href", "")
                job_id = self._extract_job_id_from_save_url(str(save_href))

        # Location
        location_el = card.select_one(self.LOCATION_SELECTOR)
        location = self._clean_text(location_el.get_text()) if location_el else ""

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Bloomberg"),
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

            # Wait for the description content to load.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_DESCRIPTION_SELECTOR,
                    timeout=15000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            desc_container = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)

            if not desc_container:
                return ""

            return self._extract_description(desc_container)

        finally:
            await detail_page.close()

    def _extract_description(self, container: Tag) -> str:
        """Extract clean description text from the rich-text container."""
        # Remove script/style tags.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        # Collect text from all elements, preserving structure.
        sections: list[str] = []

        for child in container.children:
            if not hasattr(child, "name"):
                continue

            tag_name = child.name

            if tag_name in ("h1", "h2", "h3", "h4"):
                heading = self._clean_text(child.get_text())
                if heading:
                    sections.append(heading)
            elif tag_name in ("p", "ul", "ol"):
                text = self._clean_multiline_text(child.get_text())
                if text:
                    sections.append(text)
            elif tag_name == "div":
                # Recurse into divs to find structured content.
                text = self._extract_div_contents(child)
                if text:
                    sections.append(text)
            else:
                text = self._clean_text(child.get_text())
                if text:
                    sections.append(text)

        return "\n\n".join(sections)

    def _extract_div_contents(self, container: Tag) -> str:
        """Recurse into div children, collecting structured text."""
        parts: list[str] = []

        for child in container.children:
            if not hasattr(child, "name"):
                if child.string:
                    text = self._clean_text(str(child.string))
                    if text:
                        parts.append(text)
                continue

            tag_name = child.name

            if tag_name in ("strong", "b"):
                label = self._clean_text(child.get_text())
                if label:
                    parts.append(label)
            elif tag_name == "br":
                if parts and parts[-1] != "\n":
                    parts.append("\n")
            elif tag_name in ("ul", "ol"):
                items: list[str] = []
                for li in child.select("li"):
                    li_text = self._clean_multiline_text(li.get_text())
                    if li_text:
                        items.append(f"- {li_text}")
                if items:
                    parts.append("\n".join(items))
            elif tag_name == "div":
                inner = self._extract_div_contents(child)
                if inner:
                    parts.append(inner)
            elif tag_name == "a":
                link_text = self._clean_text(child.get_text())
                href = child.get("href", "")
                if link_text:
                    if href and not href.startswith("#"):
                        parts.append(f"{link_text} ({href})")
                    else:
                        parts.append(link_text)
            else:
                text = self._clean_text(child.get_text())
                if text:
                    parts.append(text)

        result = " ".join(
            p for p in parts if p != "\n"
        )

        # Re-inject line breaks where <br> tags were found.
        parts_with_breaks: list[str] = []
        for p in parts:
            if p == "\n":
                if parts_with_breaks:
                    parts_with_breaks.append("\n\n")
            else:
                parts_with_breaks.append(p)

        return "".join(parts_with_breaks).strip()

    # ------------------------------------------------------------------
    # Job ID extraction
    # ------------------------------------------------------------------

    def _extract_job_id_from_url(self, url: str) -> str:
        """
        Extract job ID from Avature detail URL:

        https://bloomberg.avature.net/careers/JobDetail/{slug}/{jobId}
        """
        if not url:
            return ""

        # Last path segment is the numeric job ID.
        match = re.search(r"/(\d+)(?:\?|$)", url)

        if match:
            return match.group(1)

        return ""

    def _extract_job_id_from_save_url(self, url: str) -> str:
        """
        Extract job ID from Avature Save URL:

        https://bloomberg.avature.net/careers/SaveJob?jobId=19839
        """
        if not url:
            return ""

        match = re.search(r"jobId=(\d+)", url)

        if match:
            return match.group(1)

        return ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _wait_for_results(self, page: Page) -> None:
        """Wait for the job results container or first card to appear."""
        selectors = [
            "div.results--listed",
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

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        """Normalize multiline text preserving line breaks."""
        if not text:
            return ""
        lines = [
            " ".join(line.split())
            for line in text.splitlines()
            if line.strip()
        ]
        return "\n".join(lines)
