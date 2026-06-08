from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class MediatekScraper(BaseScraper):
    """
    Scraper for MediaTek Careers job search pages.

    Expected search card structure:

        a[href*="/en/jobs/"]
          span.h6                          (category/department)
          h1.h3                            (job title)
          span.border.rounded-lg...        (badge: experience, education, location)

    Expected detail page structure:

        section
          h1.h2                           (job title)
          div (labeled metadata: Category, Location, Experience, Education)
          div > h3 (Job Description) > p  (description)
          div > h3 (Main Requirements) > ol (requirements)
    """

    # ---- Card selectors ----
    CARD_SELECTOR = "a[href*='/en/jobs/']"
    TITLE_SELECTOR = "h1.h3"
    BADGE_SELECTOR = "span.border.rounded-lg, span.border.xl\\:rounded-lg, span[class*='rounded-lg']"

    # ---- Detail page selectors ----
    DETAIL_TITLE_SELECTOR = "h1.h2"
    DETAIL_SECTION_SELECTOR = "section"
    DETAIL_METADATA_SELECTOR = "div.flex.flex-col.xl\\:flex-row.justify-between > div"
    DETAIL_DESC_SELECTOR = "div h3 + p"
    DETAIL_REQUIREMENTS_SELECTOR = "ol"

    # Words that indicate a badge is NOT a location.
    LOCATION_EXCLUDE_WORDS = [
        "year", "years", "degree", "bachelor", "master", "phd",
        "more than", "less than", "doctorate",
    ]

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

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No MediaTek job cards found.")
                return jobs

            seen_ids: set[str] = set()
            seen_urls: set[str] = set()

            for card in cards[:max_jobs]:
                href = card.get("href")

                if not href or "/en/jobs/" not in str(href):
                    continue

                job_url = self._make_job_url(source_url, str(href))
                job_id = self._extract_job_id_from_url(job_url)
                title = self._extract_card_title(card)
                location = self._extract_card_location(card)

                if not title:
                    continue

                if job_id and job_id in seen_ids:
                    continue

                if job_url in seen_urls:
                    continue

                # Enrich with detail page description.
                if self._should_exclude(title):
                    self.logger.debug("Skipping detail enrichment for: %s", title)
                    description = None
                else:
                    try:
                        detail_data = await self._scrape_detail_page(job_url)

                        detail_title = detail_data.get("title", "")
                        detail_location = detail_data.get("location", "")
                        detail_description = detail_data.get("description", "")

                        if detail_title:
                            title = detail_title

                        if detail_location:
                            location = detail_location

                        description = detail_description or None

                    except Exception as exc:
                        self.logger.warning(
                            "Failed to scrape MediaTek detail page %s: %s",
                            job_url,
                            exc,
                        )
                        description = None

                if job_id:
                    seen_ids.add(job_id)
                seen_urls.add(job_url)

                jobs.append(Job(
                    job_id=job_id,
                    company=self.company_config.get("name", "MediaTek"),
                    title=title,
                    location=location,
                    url=job_url,
                    source_url=source_url,
                    posted_date=None,
                    description=description,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    extracted_experience_parts="",
                ))

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _extract_card_title(self, card: Tag) -> str:
        """Extract job title from the card's <h1> element."""
        h1 = card.select_one(self.TITLE_SELECTOR)

        if h1:
            return self._clean_text(h1.get_text())

        # Fallback: try any h1 in the card.
        for h in card.select("h1"):
            text = self._clean_text(h.get_text())

            if text:
                return text

        return ""

    def _extract_card_location(self, card: Tag) -> str:
        """
        Extract location from badge spans.
        Badges include experience ("More than 4 Years Work Expe."),
        education ("Bachelor's Degree"), and location ("Bangalore").
        Filter out non-location badges.
        """
        badges = card.select(self.BADGE_SELECTOR)

        for badge in badges:
            text = self._clean_text(badge.get_text())

            if not text:
                continue

            # Skip experience/education badges.
            if self._is_location_badge(text):
                return text

        return ""

    def _is_location_badge(self, text: str) -> bool:
        """Return True if the badge text looks like a location, not experience/education."""
        text_lower = text.lower()

        for word in self.LOCATION_EXCLUDE_WORDS:
            if word in text_lower:
                return False

        return True

    def _extract_job_id_from_url(self, url: str) -> str:
        """
        MediaTek job URLs:
        https://careers.mediatek.com/en/jobs/MTB120260224000?query=...
        """
        if not url:
            return ""

        match = re.search(r"/en/jobs/([A-Za-z0-9]+)", url, flags=re.IGNORECASE)

        if match:
            return match.group(1)

        return ""

    def _make_job_url(self, source_url: str, href: str) -> str:
        """Build absolute URL from relative href."""
        from urllib.parse import urlparse

        href = href.strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        parsed = urlparse(source_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        if href.startswith("/"):
            return f"{origin}{href}"

        return f"{origin}/{href}"

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

    async def _scrape_detail_page(self, job_url: str) -> dict[str, str]:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)

            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for the page content to load.
            selectors = [
                self.DETAIL_TITLE_SELECTOR,
                self.DETAIL_SECTION_SELECTOR,
                "h1",
            ]

            for selector in selectors:
                try:
                    await detail_page.wait_for_selector(selector, timeout=10000)
                    break
                except Exception:
                    continue

            soup = await self._get_soup(detail_page)

            detail_data: dict[str, str] = {}

            title = self._extract_detail_title(soup)
            location = self._extract_detail_location(soup)
            description = self._extract_detail_description(soup)

            if title:
                detail_data["title"] = title

            if location:
                detail_data["location"] = location

            if description:
                detail_data["description"] = description

            return detail_data

        finally:
            await detail_page.close()

    def _extract_detail_title(self, soup) -> str:
        el = soup.select_one(self.DETAIL_TITLE_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        # Fallback: any h1 in the section.
        section = soup.select_one("section")

        if section:
            h1 = section.select_one("h1")

            if h1:
                return self._clean_text(h1.get_text())

        return ""

    def _extract_detail_location(self, soup) -> str:
        """
        Detail page has labeled metadata sections:
        Category / Location / Experience / Education
        Each is a div with an orange-label span followed by an h2.
        """
        section = soup.select_one("section")

        if not section:
            return ""

        # Find metadata container: div with orange labels
        for div in section.select("div > div"):
            # Look for a span with "Location" label.
            label_el = div.select_one("span.text-orange-200, span[class*='orange']")

            if label_el:
                label_text = self._clean_text(label_el.get_text())

                if label_text.lower() == "location":
                    h2 = div.select_one("h2, h5")

                    if h2:
                        return self._clean_text(h2.get_text())

        # Fallback: find the "Location" heading and get next sibling.
        for h2_label in soup.select("h2, h5, span"):
            text = self._clean_text(h2_label.get_text())

            if text.lower() == "location":
                # The location value is in a sibling h2/h5.
                parent = h2_label.parent

                if parent:
                    value_el = parent.select_one("h2.text-charcoal-black, h5.text-charcoal-black")

                    if value_el and value_el != h2_label:
                        return self._clean_text(value_el.get_text())

        return ""

    def _extract_detail_description(self, soup) -> str:
        """
        Extract job description and requirements from the detail page.
        Description is in a <p> after an <h3> containing "Job Description".
        Requirements are in an <ol> after an <h3> containing "Requirements".
        """
        sections: list[str] = []

        section = soup.select_one("section")

        if not section:
            return ""

        # Find all h3 tags and collect content after them.
        for h3 in section.select("h3"):
            heading = self._clean_text(h3.get_text())

            if not heading:
                continue

            sections.append(heading)

            # Get content between this h3 and the next h3.
            content_parts: list[str] = []

            sibling = h3.find_next_sibling()

            while sibling and sibling.name != "h3":
                if sibling.name in ("p", "div", "ol", "ul"):
                    text = self._clean_multiline_text(sibling.get_text())

                    if text:
                        content_parts.append(text)

                sibling = sibling.find_next_sibling()

            if content_parts:
                sections.append("\n".join(content_parts))

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        return " ".join(text.split()).strip()

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        if not text:
            return ""
        lines = [
            " ".join(line.split())
            for line in text.splitlines()
            if line.strip()
        ]
        return "\n".join(lines)
