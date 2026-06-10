from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class RubrikScraper(BaseScraper):
    """
    Scraper for Rubrik careers pages.

    Rubrik's career site uses a custom styled location dropdown that filters
    job cards to show only positions for the selected office.  Jobs are
    grouped under location headings.

    Search page structure:

        div.careers_section_listing_container[data-index]
          h4.careers_section_listing_location_title    (e.g. "Bangalore, India Office")
          p.careers_section_listing_location_openings  (e.g. "5 Open Positions")
          div.careers_section_listing_job_item
            a.careers_section_listing_job_item_anchor[href]
              p.careers_section_listing_job_item_title  (title)

    Detail page structure:

        div.careers_section_job_summary_content.enocoded_html
          h2 (Job Summary)
          div (rich HTML description)

    Unique techniques:
    - Multi-URL scraping (Engineering + IT Services departments)
    - Custom styled dropdown selection via page.evaluate (hidden <select>)
    - Location from section heading
    - Job ID from reqId query parameter
    """

    BASE_URL = "https://www.rubrik.com"

    # ---- Dropdown selectors ----
    HIDDEN_SELECT_SELECTOR = "select.select_dropdown.styled_select_dropdown"
    LOCATION_OPTION_VALUE = "Bangalore, India Office"

    # ---- Card selectors ----
    CARD_SELECTOR = "div.careers_section_listing_job_item"
    CONTAINER_SELECTOR = "div.careers_section_listing_container"
    LOCATION_HEADING_SELECTOR = "h4.careers_section_listing_location_title"
    TITLE_SELECTOR = "p.careers_section_listing_job_item_title"
    LINK_SELECTOR = "a.careers_section_listing_job_item_anchor"

    # ---- Detail page selectors ----
    DESCRIPTION_SELECTOR = "div.careers_section_job_summary_content"

    # ------------------------------------------------------------------
    # Multi-URL support
    # ------------------------------------------------------------------

    def _get_urls(self) -> list[str]:
        """Return all URLs to scrape — supports single ``url`` or list ``urls``."""
        urls = self.company_config.get("urls")

        if urls and isinstance(urls, list):
            return [str(u).strip() for u in urls if str(u).strip()]

        url = self.company_config.get("url", "")
        return [url] if url else []

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def scrape(self) -> list[Job]:
        urls = self._get_urls()
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        all_jobs: list[Job] = []
        seen_job_ids: set[str] = set()
        seen_urls: set[str] = set()

        for source_url in urls:
            if len(all_jobs) >= max_jobs:
                break

            jobs = await self._scrape_single_url(
                source_url, max_jobs, seen_job_ids, seen_urls,
            )
            all_jobs.extend(jobs)

        return all_jobs[:max_jobs]

    async def _scrape_single_url(
        self, source_url: str, max_jobs: int,
        seen_job_ids: set[str], seen_urls: set[str],
    ) -> list[Job]:
        page = await self.new_page()
        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # ---- Select "Bangalore, India Office" from the custom dropdown ----
            try:
                await page.wait_for_selector(self.HIDDEN_SELECT_SELECTOR, timeout=15000)
            except Exception:
                self.logger.warning("Dropdown not found on Rubrik page: %s", source_url)
                return jobs

            await self._select_location(page)

            # ---- Wait for Bangalore container to appear ----
            try:
                await page.wait_for_selector(self.CARD_SELECTOR, timeout=15000)
            except Exception:
                self.logger.warning(
                    "No job cards found on Rubrik page after location filter: %s",
                    source_url,
                )
                return jobs

            # Short delay for any JS re-render
            await page.wait_for_timeout(2000)

            soup = await self._get_soup(page)

            # Find the Bangalore container
            location_name = self._find_bangalore_heading(soup)
            bangalore_container = self._find_bangalore_container(soup)

            if not bangalore_container:
                self.logger.warning(
                    "No Bangalore container found on: %s", source_url,
                )
                return jobs

            cards = bangalore_container.select(self.CARD_SELECTOR)
            self.logger.info(
                "Found %d card(s) in '%s' on %s",
                len(cards), location_name, source_url,
            )

            for card in cards[:max_jobs]:
                if len(jobs) >= max_jobs:
                    break

                job = self._parse_card(card, source_url, location_name)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_job_ids:
                    continue
                if job.url in seen_urls:
                    continue

                # Skip detail enrichment for senior-level roles
                if self._should_exclude(job.title):
                    self.logger.debug(
                        "Skipping detail enrichment for: %s", job.title,
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
                            "Failed to enrich Rubrik detail page %s: %s",
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
    # Dropdown interaction
    # ------------------------------------------------------------------

    async def _select_location(self, page: Page) -> None:
        """
        Select 'Bangalore, India Office' from the custom styled dropdown.

        The original <select> is hidden (class 'select-hidden') and a styled
        UI overlays it.  We set the hidden select's value via JS and dispatch
        a change event so the custom select library picks it up.
        """
        self.logger.info(
            "Selecting location: '%s'",
            self.LOCATION_OPTION_VALUE,
        )
        try:
            await page.evaluate(
                """
                (value) => {
                    const selects = document.querySelectorAll(
                        'select.select_dropdown.styled_select_dropdown'
                    );
                    for (const sel of selects) {
                        for (const opt of sel.options) {
                            if (opt.value === value) {
                                sel.value = value;
                                sel.dispatchEvent(new Event('change', { bubbles: true }));
                                sel.dispatchEvent(new Event('input', { bubbles: true }));
                                return true;
                            }
                        }
                    }
                    return false;
                }
                """,
                self.LOCATION_OPTION_VALUE,
            )
        except Exception as exc:
            self.logger.warning("Could not set location dropdown via JS: %s", exc)

        # Wait for the page to re-render after selection
        await page.wait_for_timeout(3000)

    # ------------------------------------------------------------------
    # Bangalore container detection
    # ------------------------------------------------------------------

    def _find_bangalore_heading(self, soup: BeautifulSoup) -> str:
        """Find the Bangalore location heading text."""
        for heading in soup.select(self.LOCATION_HEADING_SELECTOR):
            text = heading.get_text(strip=True).lower()
            if "bangalore" in text:
                return heading.get_text(strip=True)
        return "Bangalore, India Office"

    def _find_bangalore_container(self, soup: BeautifulSoup) -> Tag | None:
        """Find the container div that holds Bangalore jobs."""
        for container in soup.select(self.CONTAINER_SELECTOR):
            heading = container.select_one(self.LOCATION_HEADING_SELECTOR)
            if heading and "bangalore" in heading.get_text(strip=True).lower():
                return container

        # Fallback: find any container that has job items
        containers = soup.select(self.CONTAINER_SELECTOR)
        for container in containers:
            if container.select_one(self.CARD_SELECTOR):
                return container

        return None

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(
        self, card: Tag, source_url: str, location_name: str = "",
    ) -> Job | None:
        link = self._extract_link(card)
        title = self._extract_title(card)
        job_id = self._extract_job_id(card, link)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Rubrik"),
            title=title,
            location=location_name,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag) -> str:
        link_el = card.select_one(self.LINK_SELECTOR)
        if not link_el:
            return ""
        href = link_el.get("href")
        if not href:
            return ""
        href = str(href).strip()
        if href.startswith("/"):
            return f"{self.BASE_URL}{href}"
        return href

    def _extract_title(self, card: Tag) -> str:
        title_el = card.select_one(self.TITLE_SELECTOR)
        if title_el:
            return self._clean_text(title_el.get_text())
        return ""

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        Extract job ID from the URL's reqId query parameter:
        /company/careers/departments/job.7270376.1929?reqId=INSW9693
        """
        if link:
            match = re.search(r"[?&]reqId=([^&]+)", link)
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

            # Wait for the job summary content to appear
            try:
                await detail_page.wait_for_selector(
                    self.DESCRIPTION_SELECTOR, timeout=15000,
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

        The description is in div.careers_section_job_summary_content.
        Contains h2, p, ul>li elements with job details, qualifications,
        and company boilerplate.
        """
        container = soup.select_one(self.DESCRIPTION_SELECTOR)
        if not container:
            return ""

        # Remove script/style/noscript tags
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        text = html.unescape(text)
        text = text.replace("\xa0", " ").replace("\r", " ").replace("\n", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        if not text:
            return ""
        text = html.unescape(text)
        text = text.replace("\xa0", " ").replace("\r", "")
        text = re.sub(r"\n{3,}", "\n\n", text)
        lines = [line.strip() for line in text.split("\n")]
        text = "\n".join(lines)
        return text.strip()
