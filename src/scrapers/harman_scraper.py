from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class HarmanScraper(BaseScraper):
    """
    Scraper for Harman Careers (Avature platform) job search pages.

    Expected search card structure:

        article.article--result
          div.article__header
            div.article__header__text
              h3.article__header__text__title
                a.link[href="/en_US/careers/JobDetail/{slug}/{jobId}"]
              div.article__header__text__subtitle
                span.list-item-location     "Location: Bangalore ..."
                span.list-item-ref          "Ref # R-52750-2026"
                span.list-item-posted       "Date Posted: 11-May-2026"

    Expected detail page structure:

        div.article__content__view
          div.article__content__view__field
            div.article__content__view__field__value
              (rich HTML description - first field)
          (subsequent fields are company boilerplate - skipped)
    """

    # ---- Card selectors ----
    CARD_SELECTOR = "article.article--result"
    TITLE_SELECTOR = "h3.article__header__text__title a.link"
    LOCATION_SELECTOR = "span.list-item-location"
    REF_SELECTOR = "span.list-item-ref"
    POSTED_SELECTOR = "span.list-item-posted"

    # ---- Detail page selectors ----
    DETAIL_VALUE_SELECTOR = "div.article__content__view__field__value"

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
                self.logger.warning("No Harman job cards found.")
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
                            "Failed to enrich Harman job detail %s: %s",
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

        # Job ID from URL path (last numeric segment).
        job_id = self._extract_job_id_from_url(url)

        if not job_id:
            # Fallback: Ref # R-52750-2026 or numeric from ref span
            ref_el = card.select_one(self.REF_SELECTOR)
            if ref_el:
                ref_text = self._clean_text(ref_el.get_text())
                # Try "Ref # R-52750-2026" → "R-52750-2026"
                ref_match = re.search(r"R[-]?\d+[-]?\d*", ref_text, re.IGNORECASE)
                if ref_match:
                    job_id = ref_match.group(0)
                else:
                    # Try numeric-only
                    num_match = re.search(r"(\d+)", ref_text)
                    if num_match:
                        job_id = num_match.group(1)

        # Location — clean up the label prefix
        location = ""
        loc_el = card.select_one(self.LOCATION_SELECTOR)
        if loc_el:
            raw_location = self._clean_text(loc_el.get_text())
            location = self._clean_location_text(raw_location)

        # Posted date — "Date Posted: 11-May-2026"
        posted_date: str | None = None
        posted_el = card.select_one(self.POSTED_SELECTOR)
        if posted_el:
            raw = self._clean_text(posted_el.get_text())
            # Remove "Date Posted:" or "Date Posted:" prefix
            date_match = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4})", raw)
            if date_match:
                posted_date = date_match.group(1)
            else:
                posted_date = raw

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Harman"),
            title=title,
            location=location,
            url=url,
            source_url=source_url,
            posted_date=posted_date,
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

            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_VALUE_SELECTOR,
                    timeout=15000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            # Harman detail: first field value has the job description,
            # subsequent ones are company boilerplate (About HARMAN, recruitment scams).
            all_values = soup.select(self.DETAIL_VALUE_SELECTOR)

            if not all_values:
                return ""

            for value_el in all_values:
                text = self._clean_text(value_el.get_text())
                # Skip short boilerplate (< 100 chars), empty fields.
                if len(text) < 100:
                    continue
                return self._extract_description(value_el)

            return ""

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
            elif tag_name == "font":
                # Avature detail pages often use <font> tags for styling.
                font_text = self._clean_text(child.get_text())
                if font_text:
                    parts.append(font_text)
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

    @staticmethod
    def _extract_job_id_from_url(url: str) -> str:
        """
        Extract job ID from Avature detail URL:

        https://jobsearch.harman.com/en_US/careers/JobDetail/{slug}/{jobId}
        """
        if not url:
            return ""

        # Last path segment is the numeric job ID.
        match = re.search(r"/(\d+)(?:\?|$)", url)

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
    def _clean_location_text(location: str) -> str:
        """Clean up Harman location text to extract meaningful location."""
        if not location:
            return ""

        # Remove "Location:" prefix.
        location = re.sub(r"^Location:\s*", "", location, flags=re.IGNORECASE).strip()

        # If it starts with "IN_" (country code prefix like "IN_Bangalore_..."),
        # extract the city name.
        in_match = re.search(r"IN_([A-Za-z]+)", location)
        if in_match:
            return in_match.group(1)

        # For formats like "Bangalore - Karnataka, India - Kalyani Platina"
        # extract just "Bangalore, Karnataka, India" or similar.
        city_match = re.search(r"([A-Za-z\s]+-[A-Za-z\s,]+India)", location)
        if city_match:
            return city_match.group(1).strip()

        return location

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
            " ".join(line.split()).strip()
            for line in text.split("\n")
        ]
        return "\n".join(line for line in lines if line).strip()
