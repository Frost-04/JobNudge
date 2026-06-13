from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class WhatfixScraper(BaseScraper):
    """
    Scraper for Whatfix Careers job search pages.

    Uses the Trakstar ATS platform (hire.trakstar.com).

    The search results page renders job cards:

        div.js-card.list-item.list-item-clickable.js-careers-page-job-list-item
          h3.js-job-list-opening-name          (title)
          div.js-job-list-opening-loc           (location with city/state/country spans)
          div.rb-text-4                         (department)
          div.js-job-list-opening-meta span     (employment type)
          div[data-href="/jobs/{code}/"] or a   (link)

    The detail page contains:

        div.jobdesciption                       (rich-text job description)
    """

    # ---- Card selectors ----
    CARD_SELECTOR = "div.js-card.list-item-clickable"
    TITLE_SELECTOR = "h3.js-job-list-opening-name"
    LOCATION_SELECTOR = "div.js-job-list-opening-loc"
    DEPARTMENT_SELECTOR = "div.rb-text-4"
    EMPLOYMENT_TYPE_SELECTOR = "div.js-job-list-opening-meta"

    # ---- Detail page selectors ----
    DETAIL_CONTENT_SELECTOR = "div.jobdesciption"

    # ---- URL pattern ----
    BASE_URL = "https://whatfix101.hire.trakstar.com"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for job cards to appear.
            try:
                await page.wait_for_selector(self.CARD_SELECTOR, timeout=15000)
            except Exception:
                self.logger.warning("No Whatfix job cards found.")
                return jobs

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No Whatfix job cards found after parsing.")
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
                            "Failed to enrich Whatfix job detail %s: %s",
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
        # Extract link from data-href attribute on the card or from inner a tag.
        link = self._extract_link(card, source_url)
        if not link:
            return None

        title = self._extract_title(card)
        if not title:
            return None

        job_id = self._extract_job_id(card, link)
        location = self._extract_location(card)

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Whatfix"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag, source_url: str) -> str | None:
        # Try data-href on the card div first.
        data_href = card.get("data-href")
        if data_href:
            return make_absolute_url(source_url, str(data_href))

        # Fallback to inner a tag.
        a_tag = card.select_one("a[href]")
        if a_tag:
            href = a_tag.get("href")
            if href:
                return make_absolute_url(source_url, str(href))

        return None

    def _extract_title(self, card: Tag) -> str | None:
        title_el = card.select_one(self.TITLE_SELECTOR)
        if not title_el:
            return None
        return self._clean_text(title_el.get_text())

    def _extract_job_id(self, card: Tag, url: str) -> str:
        # Job ID from URL path: /jobs/{code}/
        match = re.search(r"/jobs/([^/]+)", url)
        if match:
            return match.group(1)
        return extract_job_id(url)

    def _extract_location(self, card: Tag) -> str:
        loc_el = card.select_one(self.LOCATION_SELECTOR)
        if not loc_el:
            return ""

        parts: list[str] = []
        city_el = loc_el.select_one("span.meta-job-location-city")
        state_el = loc_el.select_one("span.meta-job-location-state")
        country_el = loc_el.select_one("span.meta-job-location-country")

        if city_el:
            city_text = self._clean_text(city_el.get_text())
            if city_text:
                parts.append(city_text)
        if state_el:
            state_text = self._clean_text(state_el.get_text())
            if state_text:
                parts.append(state_text)
        if country_el:
            country_text = self._clean_text(country_el.get_text())
            if country_text:
                parts.append(country_text)

        if parts:
            return ", ".join(parts)

        # Fallback: just get all text from the location element.
        full_text = self._clean_text(loc_el.get_text())
        if full_text:
            return full_text

        return ""

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
                    self.DETAIL_CONTENT_SELECTOR,
                    timeout=15000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            desc_el = soup.select_one(self.DETAIL_CONTENT_SELECTOR)
            if not desc_el:
                return ""

            return self._extract_description(desc_el)

        finally:
            await detail_page.close()

    def _extract_description(self, desc_el: Tag) -> str:
        """Extract clean description text from the div.jobdesciption element."""
        # Remove script/style tags.
        for unwanted in desc_el.select("script, style, noscript"):
            unwanted.decompose()

        # Collect content preserving section structure.
        sections: list[str] = []
        current_section: list[str] = []

        for child in desc_el.children:
            if not hasattr(child, "name"):
                continue

            tag_name = child.name

            if tag_name in ("h1", "h2", "h3", "h4", "strong"):
                # Flush current section.
                if current_section:
                    sections.append("\n".join(current_section))
                    current_section = []

                heading = self._clean_text(child.get_text())
                if heading:
                    sections.append(heading)
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
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        """Clean up whitespace and HTML entities in text."""
        import html

        text = html.unescape(text)
        text = text.replace("\xa0", " ")
        text = " ".join(text.split())
        return text.strip()
