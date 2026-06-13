from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import Tag
from playwright.async_api import BrowserContext, Page, async_playwright

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class DBScraper(BaseScraper):
    """
    Scraper for Deutsche Bank job board (Vue.js SPA with hash routing).

    The listing page is a Vue SPA at careers.db.com/professionals/search-roles/
    with hash-based routing (#/professional/results/...). Job cards are rendered
    inside div.yello-search-result containers.

    Expected listing structure:

    div.yello-search-result
      div.detail-info            (summary text like "8 suitable results found")
      div
        a[href*="/professional/job/"]   (job link, id="69169")
          div.detail-entry
            h2                   (job title — e.g. "Test Automation Engineer, AS")
            div                  (location — e.g. "Location: Pune")

    Expected detail page structure:

    div#db-jobad
      h1                         (job title)
      div#headerbox
        table
          td > strong            ("Job ID:", "Listed:", "Location:", etc.)
      h2                         (section headings)
      p, ul                      (description content)
    """

    # NOTE: Anti-bot init scripts (navigator.webdriver masking, etc.)
    # break this Vue SPA. Override create_browser_context to skip them.

    async def create_browser_context(self) -> BrowserContext:
        """Create a browser context WITHOUT anti-bot init scripts that
        break Deutsche Bank's Vue.js SPA."""
        if self.context:
            return self.context

        run_settings = self.settings.get("run", {})
        headless = bool(run_settings.get("headless", True))
        browser_channel = run_settings.get("browser_channel", "chrome")

        self.playwright = await async_playwright().start()

        launch_kwargs: dict = {
            "headless": headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        }

        if browser_channel:
            try:
                self.browser = await self.playwright.chromium.launch(
                    channel=browser_channel, **launch_kwargs
                )
            except Exception:
                self.logger.warning(
                    "Chrome channel '%s' unavailable, falling back to bundled Chromium",
                    browser_channel,
                )
                self.browser = await self.playwright.chromium.launch(**launch_kwargs)
        else:
            self.browser = await self.playwright.chromium.launch(**launch_kwargs)

        self.context = await self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/142.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        # NOTE: No add_init_script — anti-bot masking breaks Vue.js rendering.
        return self.context

    CARD_SELECTOR = 'a[href*="/professional/job/"]'

    JOB_CARD_SELECTORS = [
        'a[href*="/professional/job/"]',
        'div.yello-search-result',
    ]

    TITLE_SELECTOR = 'h2'
    LOCATION_SELECTOR = 'div.detail-entry div'

    DESCRIPTION_SELECTOR = 'div#db-jobad'

    BASE_PATH = "/professionals/search-roles/"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            # Vue SPA with hash routing — domcontentloaded + settle.
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5)

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

                # Enrich by opening the job detail page.
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
                else:
                    try:
                        detail_data = await self._scrape_detail_page(job.url)

                        detail_description = detail_data.get("description", "")
                        detail_location = detail_data.get("location", "")
                        detail_posted_date = detail_data.get("posted_date", "")
                        detail_job_id = detail_data.get("job_id", "")

                        if detail_description or detail_location:
                            job = Job(
                                job_id=detail_job_id or job.job_id,
                                company=job.company,
                                title=detail_data.get("title", job.title),
                                location=detail_location or job.location,
                                url=job.url,
                                source_url=job.source_url,
                                posted_date=detail_posted_date or job.posted_date,
                                description=detail_description,
                                scraped_at=datetime.now(timezone.utc).isoformat(),
                                extracted_experience_parts="",
                            )

                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich DB job detail page %s: %s",
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
            company=self.company_config.get("name", "Deutsche Bank"),
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

        href = str(href)

        # Hash-based SPA URL: #/professional/job/69169
        # Build the full URL preserving the hash.
        if href.startswith("#"):
            parsed = urlparse(source_url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            return f"{origin}{self.BASE_PATH}{href}"

        return self._make_job_url(source_url, href)

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        return ""

    def _extract_location(self, card: Tag) -> str:
        el = card.select_one(self.LOCATION_SELECTOR)

        if el:
            text = self._clean_text(el.get_text())

            # Strip "Location: " prefix.
            if text.lower().startswith("location:"):
                return text.split(":", 1)[1].strip()

            return text

        return ""

    def _extract_job_id(self, card: Tag, link: str) -> str:
        # Job ID is the id attribute on the <a> tag.
        job_id = card.get("id")

        if job_id:
            return str(job_id)

        # Fallback: extract from URL
        return self._extract_job_id_from_url(link)

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
            # Vue SPA — domcontentloaded + settle for hash-route rendering.
            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5)

            await self._wait_for_any_selector(
                detail_page,
                [
                    self.DESCRIPTION_SELECTOR,
                    'div#db-jobad',
                    'div#headerbox',
                    'h1',
                ],
            )

            soup = await self._get_soup(detail_page)

            detail_data: dict[str, str] = {}

            # Extract metadata from headerbox table.
            headerbox = soup.select_one('div#headerbox')

            if headerbox:
                # Go through all table cells.
                for td in headerbox.select('td'):
                    strong = td.select_one('strong')

                    if not strong:
                        continue

                    label = self._clean_text(strong.get_text()).rstrip(":")
                    # Get text after the <strong> tag.
                    value = self._clean_text(td.get_text()).replace(label + ":", "").strip() if label else ""

                    if label == "Job ID":
                        detail_data["job_id"] = value
                    elif label == "Listed":
                        detail_data["posted_date"] = value
                    elif label == "Location":
                        if not detail_data.get("location"):
                            detail_data["location"] = value

            # Extract title
            title_el = soup.select_one('div#db-jobad h1')

            if title_el:
                detail_data["title"] = self._clean_text(title_el.get_text())

            # Extract description (the full #db-jobad excluding #headerbox).
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

        # Remove the headerbox area (metadata table) from description.
        headerbox = container.select_one('div#headerbox')

        if headerbox:
            headerbox.decompose()

        # Remove non-description elements.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    def _make_job_url(self, source_url: str, href: str) -> str:
        href = html.unescape(href).strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        # Handle hash-based URLs.
        if href.startswith("#"):
            parsed = urlparse(source_url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            return f"{origin}{self.BASE_PATH}{href}"

        parsed_source = urlparse(source_url)
        origin = f"{parsed_source.scheme}://{parsed_source.netloc}"

        if href.startswith("/"):
            return f"{origin}{href}"

        return make_absolute_url(source_url, href)

    def _format_detail_metadata(self, detail_data: dict[str, str]) -> str:
        """DB detail pages have structured data extracted separately."""
        return ""

    def _join_description_parts(self, *parts: str) -> str:
        cleaned_parts = [part.strip() for part in parts if part and part.strip()]
        return "\n\n".join(cleaned_parts)

    def _extract_job_id_from_url(self, url: str) -> str:
        """
        DB job URLs look like:

        https://careers.db.com/professionals/search-roles/#/professional/job/69169

        Extract the numeric ID from the URL path.
        """
        if not url:
            return ""

        # Extract numeric ID from the hash fragment.
        match = re.search(r"/job/(\d+)", url)

        if match:
            return match.group(1)

        return ""

    async def _fallback_links(self, page: Page, source_url: str, max_jobs: int) -> list[Job]:
        """
        Fallback: extract jobs from anchor links when card selectors fail.
        """
        soup = await self._get_soup(page)

        jobs: list[Job] = []
        seen_urls: set[str] = set()

        for link in soup.select('a[href*="/professional/job/"]'):
            if len(jobs) >= max_jobs:
                break

            href = link.get("href")

            if not href or "/professional/job/" not in str(href):
                continue

            url = self._make_job_url(source_url, str(href))

            if url in seen_urls:
                continue

            seen_urls.add(url)

            title = ""
            title_el = link.select_one("h2")

            if title_el:
                title = self._clean_text(title_el.get_text())

            if not title:
                title = self._clean_text(link.get_text())

            job_id = self._extract_job_id(link, url)

            location = ""
            loc_el = link.select_one("div.detail-entry div")

            if loc_el:
                text = self._clean_text(loc_el.get_text())

                if text.lower().startswith("location:"):
                    location = text.split(":", 1)[1].strip()
                else:
                    location = text

            jobs.append(Job(
                job_id=job_id,
                company=self.company_config.get("name", "Deutsche Bank"),
                title=title,
                location=location,
                url=url,
                source_url=source_url,
                posted_date=None,
                description=None,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                extracted_experience_parts="",
            ))

        return jobs

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)

        return text.strip()

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        text = text.replace("\xa0", " ")

        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                lines.append(stripped)

        return "\n".join(lines)
