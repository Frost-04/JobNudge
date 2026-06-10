from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class MetaScraper(BaseScraper):
    """
    Scraper for Meta careers via metacareers.com.

    The search page shows job cards as <a> tags wrapping the full card:

        <a href="/profile/job_details/25141192702199005" role="link" target="_blank">
          <h3>Partner Engineer, Generative AI</h3>
          ...
          <span>Bangalore, India +1 locations</span>
          <span>⋅</span>
          <span>Software Engineering</span>
          ...

    Detail page (opens in new tab) contains structured sections:
        <h1> (title)
        <span> (summary after "Apply now" button)
        <h2>Responsibilities</h2> > <ul><li><span>
        <h2>Minimum Qualifications</h2> > <ul><li><span>
        <h2>Preferred Qualifications</h2> > <ul><li><span>
        <h2>About Meta</h2> > <span>

    Unique techniques:
    - Hashed CSS classes (Facebook's x-prefixed utility classes) — rely on
      structural tag selectors rather than class names
    - Location extraction via "⋅" separator parent div
    - Detail-page enrichment with structured h2-section parsing
    - No custom include filter needed (URL already pre-filters by team/office)
    """

    BASE_URL = "https://www.metacareers.com"

    # ---- Card selectors (tag-based, avoiding hashed classes) ----
    CARD_LINK_SELECTOR = 'a[href*="/profile/job_details/"]'
    CARD_TITLE_SELECTOR = "h3"

    # ---- Detail page selectors ----
    DETAIL_TITLE_SELECTOR = "h1"
    DETAIL_SECTION_HEADING_SELECTOR = "h2"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for job cards to appear
            try:
                await page.wait_for_selector(
                    self.CARD_LINK_SELECTOR, timeout=30000,
                )
            except Exception:
                self.logger.warning(
                    "No job cards found on Meta careers page."
                )
                return jobs

            soup = await self._get_soup(page)

            # Find all job card links
            card_links = soup.select(self.CARD_LINK_SELECTOR)

            if not card_links:
                self.logger.warning("No job card links found on Meta page.")
                return jobs

            self.logger.info("Found %d job card(s) on Meta page", len(card_links))

            seen_job_ids: set[str] = set()
            seen_urls: set[str] = set()

            for card_link in card_links[:max_jobs]:
                job = self._parse_card(card_link, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_job_ids:
                    continue
                if job.url in seen_urls:
                    continue

                # Skip detail enrichment for senior-level roles
                if self._should_exclude(job.title):
                    self.logger.debug(
                        "Skipping detail enrichment for: %s", job.title
                    )
                else:
                    try:
                        detail_data = await self._scrape_detail_page(job.url)
                        description = detail_data.get("description", "")
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
                            "Failed to enrich Meta detail page %s: %s",
                            job.url,
                            exc,
                        )

                if job.job_id:
                    seen_job_ids.add(job.job_id)
                seen_urls.add(job.url)
                jobs.append(job)

                if len(jobs) >= max_jobs:
                    break

            self.logger.info("Total Meta jobs scraped: %d", len(jobs))
            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card)
        title = self._extract_title(card)
        job_id = self._extract_job_id(card, link)
        location = self._extract_location(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Meta"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag) -> str:
        """Extract and resolve the job detail URL from the card <a> tag."""
        # The card itself IS the <a> tag
        if card.name == "a":
            href = card.get("href")
        else:
            link_el = card.select_one(self.CARD_LINK_SELECTOR)
            href = link_el.get("href") if link_el else ""

        if not href:
            return ""

        href = str(href).strip()

        # Resolve relative URLs
        if href.startswith("/"):
            return f"{self.BASE_URL}{href}"

        return href

    def _extract_title(self, card: Tag) -> str:
        """Extract job title from <h3> inside the card."""
        title_el = card.select_one(self.CARD_TITLE_SELECTOR)
        if title_el:
            return self._clean_text(title_el.get_text())
        return ""

    def _extract_location(self, card: Tag) -> str:
        """
        Extract location from the card.

        Meta cards have a metadata row with spans separated by "⋅":
            <span>Bangalore, India +1 locations</span>
            <span>⋅</span>
            <span>Software Engineering</span>

        We find the "⋅" separator span, go to its parent div, and take
        the first span child as the location.
        """
        # Find separator spans containing "⋅"
        sep_spans = card.find_all("span", string=lambda t: t and "⋅" in t)

        for sep_span in sep_spans:
            parent = sep_span.parent
            if parent and parent.name == "div":
                first_span = parent.find("span")
                if first_span:
                    text = self._clean_text(first_span.get_text())
                    # Remove trailing "+N locations" / "+N more" noise
                    text = re.sub(r"\s*\+\d+\s*(locations?|more)\s*$", "", text, flags=re.IGNORECASE)
                    if text:
                        return text

        # Fallback: take the first non-empty, non-separator span text
        all_spans = card.select("span")
        for span in all_spans:
            text = self._clean_text(span.get_text())
            if text and text != "⋅" and len(text) > 2:
                # Check if it looks like a location (contains comma or city name)
                if "," in text or any(
                    city in text.lower()
                    for city in ["bangalore", "bengaluru", "mumbai", "gurgaon",
                                 "hyderabad", "delhi", "india", "remote"]
                ):
                    text = re.sub(
                        r"\s*\+\d+\s*(locations?|more)\s*$", "", text,
                        flags=re.IGNORECASE,
                    )
                    return text

        return ""

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        Extract job ID from the URL path:
        /profile/job_details/25141192702199005
        """
        if link:
            match = re.search(r"/job_details/(\d+)", link)
            if match:
                return match.group(1)

        if link:
            return extract_job_id(link)

        return ""

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

    async def _scrape_detail_page(self, job_url: str) -> dict[str, str]:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for the detail content to render
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_TITLE_SELECTOR, timeout=15000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            detail_data: dict[str, str] = {}
            description = self._extract_description(soup)
            if description:
                detail_data["description"] = description

            return detail_data

        finally:
            await detail_page.close()

    def _extract_description(self, soup: BeautifulSoup) -> str:
        """
        Extract the full job description from the detail page.

        Meta detail pages have a structured layout:
        1. Summary paragraph (span after "Apply now" button, before first <hr>)
        2. <h2>Responsibilities</h2> > <ul><li><span>
        3. <h2>Minimum Qualifications</h2> > <ul><li><span>
        4. <h2>Preferred Qualifications</h2> > <ul><li><span>
        5. <h2>About Meta</h2> > <span>
        6. <h2>Equal Employment Opportunity</h2> > <span>
        """
        parts: list[str] = []

        # ---- Extract summary (text before first <hr> after the h1) ----
        h1 = soup.select_one(self.DETAIL_TITLE_SELECTOR)
        if h1:
            hr_after_h1 = h1.find_next("hr")
            if hr_after_h1:
                summary_texts: list[str] = []
                sibling = h1.find_next_sibling()
                while sibling and sibling != hr_after_h1:
                    # Skip the "Apply now" button text and bookmark
                    text = sibling.get_text(separator=" ", strip=True)
                    if text and text.lower() not in ("apply now", ""):
                        summary_texts.append(text)
                    sibling = sibling.find_next_sibling()
                if summary_texts:
                    parts.append("Summary\n" + " ".join(summary_texts))

        # ---- Extract h2 sections ----
        for h2 in soup.select(self.DETAIL_SECTION_HEADING_SELECTOR):
            section_title = h2.get_text(strip=True)
            content_parts: list[str] = []

            sibling = h2.find_next_sibling()
            while sibling and sibling.name not in ("h2", "hr"):
                # For <ul> elements, extract each <li> as a bullet
                if sibling.name == "ul":
                    for li in sibling.select("li"):
                        text = li.get_text(separator=" ", strip=True)
                        if text:
                            content_parts.append(f"• {text}")
                elif sibling.name in ("div", "span"):
                    text = sibling.get_text(separator="\n", strip=True)
                    cleaned = self._clean_multiline_text(text)
                    if cleaned:
                        content_parts.append(cleaned)
                else:
                    text = sibling.get_text(separator=" ", strip=True)
                    if text:
                        content_parts.append(text)

                sibling = sibling.find_next_sibling()

            if content_parts:
                parts.append(f"{section_title}\n" + "\n".join(content_parts))

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ").replace("\r", " ").replace("\n", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ").replace("\r", "")
        text = re.sub(r"\n{3,}", "\n\n", text)
        lines = [line.strip() for line in text.split("\n")]
        text = "\n".join(lines)
        return text.strip()
