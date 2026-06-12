from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class PhonePeScraper(BaseScraper):
    """
    Scraper for PhonePe job board (www.phonepe.com/careers/job-openings/).

    Custom React-based listing page with a sidebar department filter.
    Clicking "Engineering" filters the job cards in-place. Each card
    links to a standard Greenhouse detail page.

    Listing workflow:
      1. Navigate to the listing URL.
      2. Click the "Engineering" sidebar filter item.
      3. Wait for filtered cards to render.
      4. Parse a.card elements.

    Expected listing structure:

    div.job-content
      div.job-cards
        a.card[href="https://boards.greenhouse.io/phonepe/jobs/{id}"]
          div.card_location            (e.g. "Pune - Amar Business Zone")
          div.card_department          (e.g. "Engineering")
          div.card_title               (e.g. "Software Engineer - iOS")
          div.card_type                (e.g. "Full time")
          div.card_date                (e.g. "15 days ago")

    Sidebar filter:

    div.desktop-sidebar
      ul
        li.sidebar-item               ("All Departments", "Engineering", ...)

    Expected detail page structure (Greenhouse standard):

    div.job__description.body          (full rich-text description)

    Job IDs come from the Greenhouse URL path
    (e.g. /phonepe/jobs/7653436003 → 7653436003).
    """

    SIDEBAR_ITEM_SELECTOR = 'li.sidebar-item'
    DEPARTMENT_FILTER = "Engineering"

    CARD_SELECTOR = 'a.card'

    JOB_CARD_SELECTORS = [
        'a.card',
        'div.job-cards',
        'div.job-content',
    ]

    TITLE_SELECTOR = 'div.card_title'
    LOCATION_SELECTOR = 'div.card_location'
    DEPARTMENT_SELECTOR = 'div.card_department'

    DESCRIPTION_SELECTOR = 'div.job__description.body'

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            # Step 1: Click the "Engineering" sidebar filter.
            filter_clicked = await self._click_department_filter(page)

            if not filter_clicked:
                self.logger.warning(
                    "Could not click PhonePe Engineering sidebar filter. "
                    "Proceeding with unfiltered listing."
                )

            # Step 2: Wait for filtered cards to render.
            await page.wait_for_timeout(3000)

            # Step 3: Wait for card elements.
            selector = await self._wait_for_any_selector(page, self.JOB_CARD_SELECTORS)

            if not selector:
                return await self._fallback_links(page, source_url, max_jobs)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                return await self._fallback_links(page, source_url, max_jobs)

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

                # Enrich by opening the Greenhouse detail page.
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
                else:
                    try:
                        detail_data = await self._scrape_detail_page(job.url)

                        detail_description = detail_data.get("description", "")

                        if detail_description:
                            job = Job(
                                job_id=job.job_id,
                                company=job.company,
                                title=job.title,
                                location=job.location,
                                url=job.url,
                                source_url=job.source_url,
                                posted_date=job.posted_date,
                                description=detail_description,
                                scraped_at=datetime.now(timezone.utc).isoformat(),
                                extracted_experience_parts="",
                            )

                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich PhonePe job detail page %s: %s",
                            job.url,
                            exc,
                        )

                if job.job_id:
                    seen_job_ids.add(job.job_id)

                seen_urls.add(job.url)
                jobs.append(job)

            if not jobs:
                jobs = await self._fallback_links(page, source_url, max_jobs)

            return jobs

        finally:
            await self.close_browser()

    async def _click_department_filter(self, page: Page) -> bool:
        """Click the 'Engineering' sidebar item to filter job cards."""
        try:
            # Wait for the React page to fully render.
            await page.wait_for_timeout(5000)

            # Use page.evaluate to force-click: find the sidebar item by
            # text content, scroll it into view, and click it.
            clicked = await page.evaluate(
                """(dept) => {
                    const items = document.querySelectorAll('li.sidebar-item');
                    for (const item of items) {
                        const text = (item.textContent || '').trim();
                        if (text === dept) {
                            item.scrollIntoView({ block: 'center' });
                            item.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                self.DEPARTMENT_FILTER,
            )

            if clicked:
                await page.wait_for_timeout(3000)
                self.logger.info(
                    "Successfully clicked PhonePe Engineering sidebar filter."
                )
                return True
            else:
                self.logger.warning(
                    "PhonePe Engineering sidebar item not found in DOM."
                )
                return False

        except Exception as exc:
            self.logger.warning(
                "Failed to click PhonePe department filter '%s': %s",
                self.DEPARTMENT_FILTER,
                exc,
            )
            return False

    async def _wait_for_any_selector(self, page: Page, selectors: list[str]) -> str | None:
        timeout_ms = self._to_ms(
            self.settings.get("run", {}).get("page_load_timeout_seconds"),
            45000,
        )

        for selector in selectors:
            try:
                await page.wait_for_selector(selector, timeout=timeout_ms)
                return selector
            except Exception:
                continue

        return None

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card, source_url)
        title = self._extract_title(card)
        job_id = self._extract_job_id(card, link)
        location = self._extract_location(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "PhonePe"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag, source_url: str) -> str:
        href = card.get("href")

        if not href:
            return ""

        href_str = str(href)

        if href_str.startswith("http://") or href_str.startswith("https://"):
            return href_str

        parsed_source = urlparse(source_url)
        origin = f"{parsed_source.scheme}://{parsed_source.netloc}"

        if href_str.startswith("/"):
            return f"{origin}{href_str}"

        return make_absolute_url(source_url, href_str)

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        return ""

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        Extract job ID from the Greenhouse URL path.
        URLs look like: https://boards.greenhouse.io/phonepe/jobs/7653436003
        """
        if link:
            match = re.search(r'/jobs/(\d+)', link)
            if match:
                return match.group(1)

        href = card.get("href")
        if href:
            match = re.search(r'/jobs/(\d+)', str(href))
            if match:
                return match.group(1)

        return ""

    def _extract_location(self, card: Tag) -> str:
        el = card.select_one(self.LOCATION_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        return ""

    async def _get_detail_page(self) -> Page:
        if self.context:
            try:
                return await self.context.new_page()
            except Exception:
                self.logger.debug(
                    "Shared browser context is no longer usable; discarding and creating a fresh one."
                )
                await self.close_browser()

        return await self.new_page()

    async def _scrape_detail_page(self, job_url: str) -> dict[str, str]:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)

            await self._wait_for_any_selector(
                detail_page,
                [
                    self.DESCRIPTION_SELECTOR,
                    'div.job__description',
                    'h1',
                ],
            )

            soup = await self._get_soup(detail_page)

            detail_data: dict[str, str] = {}
            description = self._extract_description(soup)

            if description:
                detail_data["description"] = description

            return detail_data

        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        container = soup.select_one(self.DESCRIPTION_SELECTOR)

        if not container:
            return ""

        # Remove non-description elements.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    def _make_job_url(self, source_url: str, href: str) -> str:
        href = html.unescape(href).strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        parsed_source = urlparse(source_url)
        origin = f"{parsed_source.scheme}://{parsed_source.netloc}"

        if href.startswith("/"):
            return f"{origin}{href}"

        return make_absolute_url(source_url, href)

    async def _fallback_links(self, page: Page, source_url: str, max_jobs: int) -> list[Job]:
        """
        Fallback: extract jobs from anchor links when card selectors fail.
        """
        soup = await self._get_soup(page)

        jobs: list[Job] = []
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()

        for link in soup.select('a.card[href*="boards.greenhouse.io/phonepe/jobs/"]'):
            if len(jobs) >= max_jobs:
                break

            href = link.get("href")

            if not href:
                continue

            href_str = str(href)
            job_url = self._make_job_url(source_url, href_str)

            if job_url in seen_urls:
                continue

            seen_urls.add(job_url)

            title = ""
            title_el = link.select_one(self.TITLE_SELECTOR)
            if title_el:
                title = self._clean_text(title_el.get_text())

            if not title:
                continue

            job_id_match = re.search(r'/jobs/(\d+)', href_str)
            job_id = job_id_match.group(1) if job_id_match else ""

            if job_id and job_id in seen_ids:
                continue

            if job_id:
                seen_ids.add(job_id)

            location = ""
            location_el = link.select_one(self.LOCATION_SELECTOR)
            if location_el:
                location = self._clean_text(location_el.get_text())

            job = Job(
                job_id=job_id,
                company=self.company_config.get("name", "PhonePe"),
                title=title,
                location=location,
                url=job_url,
                source_url=source_url,
                posted_date=None,
                description=None,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                extracted_experience_parts="",
            )

            # Enrich if not excluded.
            if not self._should_exclude(title):
                try:
                    detail_data = await self._scrape_detail_page(job_url)
                    detail_desc = detail_data.get("description", "")
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
                        "Failed to enrich PhonePe fallback detail page %s: %s",
                        job_url,
                        exc,
                    )

            jobs.append(job)

        return jobs

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""
        return " ".join(text.split()).strip()

    def _clean_multiline_text(self, text: str) -> str:
        if not text:
            return ""
        lines = [line.strip() for line in text.split("\n")]
        lines = [line for line in lines if line]
        return "\n".join(lines)
