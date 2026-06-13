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


class HevoDataScraper(BaseScraper):
    """
    Scraper for HevoData careers page.

    Hevodata's careers page at hevodata.com/careers/ is a custom page where
    all jobs are loaded in the HTML but filtered client-side. Two filters
    need to be selected via Playwright clicks:

    1. Location: "Bengaluru, India" radio button
    2. Teams: "Engineering" list item

    Selecting one filter may reload the page or re-render internally, so
    we wait between clicks.

    Card structure (after filtering):

        div.opening-card-wrapper[data-department="oc-engineering"]
          div.opening-card
            h5.card-position[data-position]   (job title)
            div.card-loc                       (location)
            a[href*="jobs.lever.co/hevodata/"] (apply link)

    Detail pages are on Lever.co at jobs.lever.co/hevodata/{uuid}.
    Description is inside div[data-qa="job-description"].
    """

    # ---- Filter selectors (desktop) ----
    LOCATION_RADIO = '#v_location_bengaluru'
    TEAMS_ITEM = 'ul.category-list.category-list-v li[data-for="oc-engineering"]'

    # ---- Card selectors ----
    CARD_SELECTOR = 'div.opening-card-wrapper[data-department="oc-engineering"]'

    JOB_CARD_SELECTORS = [
        'div.opening-card-wrapper[data-department="oc-engineering"]',
        'div.opening-card',
        'div.page-body-right',
    ]

    TITLE_SELECTOR = 'h5.card-position'
    LOCATION_SELECTOR = 'div.card-loc'
    LINK_SELECTOR = 'a[href*="jobs.lever.co/hevodata/"]'

    # ---- Detail page selectors ----
    DESCRIPTION_SELECTOR = 'div[data-qa="job-description"]'

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            # Using domcontentloaded + settle — networkidle hangs on persistent connections.
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)

            # ---- Step 1: Apply location filter ----
            await self._apply_location_filter(page)

            # ---- Step 2: Apply team filter ----
            await self._apply_team_filter(page)

            # ---- Step 3: Wait for filtered results ----
            await page.wait_for_timeout(2000)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No HevoData cards found after filtering.")
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

                # Enrich by opening the Lever.co detail page.
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
                else:
                    self.logger.info("Enriching: %s", job.title)
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
                            "Failed to enrich HevoData job detail page %s: %s",
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

    # ------------------------------------------------------------------
    # Filter interaction
    # ------------------------------------------------------------------

    async def _apply_location_filter(self, page: Page) -> None:
        """Scroll down to filters and click Bengaluru location radio button."""
        try:
            # Scroll to the filters section.
            await page.evaluate(
                'document.querySelector("#open_positions").scrollIntoView({behavior: "instant", block: "start"})'
            )
            await page.wait_for_timeout(1000)

            # Try desktop radio first.
            loc_radio = page.locator(self.LOCATION_RADIO)

            if await loc_radio.count() > 0:
                await loc_radio.evaluate("el => el.click()")
                self.logger.info("Clicked Bengaluru location radio (desktop)")
                await page.wait_for_timeout(2000)
                return

            # Fallback: try mobile radio.
            mobile_radio = page.locator('#h_location_bengaluru')

            if await mobile_radio.count() > 0:
                await mobile_radio.evaluate("el => el.click()")
                self.logger.info("Clicked Bengaluru location radio (mobile)")
                await page.wait_for_timeout(2000)
                return

            self.logger.warning("HevoData Bengaluru location radio not found")

        except Exception as exc:
            self.logger.warning("Failed to apply HevoData location filter: %s", exc)

    async def _apply_team_filter(self, page: Page) -> None:
        """Click the Engineering team filter item."""
        try:
            # Try desktop team list first.
            team_item = page.locator(self.TEAMS_ITEM)

            if await team_item.count() > 0:
                await team_item.evaluate("el => el.click()")
                self.logger.info("Clicked Engineering team (desktop)")
                await page.wait_for_timeout(2000)
                return

            # Fallback: try mobile team list.
            mobile_item = page.locator(
                'ul.category-list.category-list-h li[data-for="oc-engineering"]'
            )

            if await mobile_item.count() > 0:
                await mobile_item.evaluate("el => el.click()")
                self.logger.info("Clicked Engineering team (mobile)")
                await page.wait_for_timeout(2000)
                return

            self.logger.warning("HevoData Engineering team filter not found")

        except Exception as exc:
            self.logger.warning("Failed to apply HevoData team filter: %s", exc)

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card, source_url)
        title = self._extract_title(card)
        job_id = self._extract_job_id(link)
        location = self._extract_location(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "HevoData"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_title(self, card: Tag) -> str:
        # Prefer data-position attribute.
        title_el = card.select_one(self.TITLE_SELECTOR)

        if title_el:
            data_pos = title_el.get("data-position")

            if data_pos:
                return self._clean_text(str(data_pos))

            return self._clean_text(title_el.get_text())

        return ""

    def _extract_location(self, card: Tag) -> str:
        el = card.select_one(self.LOCATION_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        return ""

    def _extract_link(self, card: Tag, source_url: str) -> str:
        el = card.select_one(self.LINK_SELECTOR)

        if not el:
            return ""

        href = el.get("href")

        if not href:
            return ""

        return str(href).strip()

    def _extract_job_id(self, link: str) -> str:
        """
        Lever job URLs look like:

        https://jobs.lever.co/hevodata/928a2640-ec50-4f00-860a-c3ca75c38acd

        Extract the UUID.
        """
        if not link:
            return ""

        # Extract UUID from Lever URL.
        match = re.search(r"/hevodata/([a-f0-9-]+)", link, flags=re.IGNORECASE)

        if match:
            return match.group(1)

        return ""

    # ------------------------------------------------------------------
    # Detail page enrichment (Lever.co)
    # ------------------------------------------------------------------

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
            self.logger.info("Loading detail page: %s", job_url[:80])
            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)
            self.logger.info("Detail page loaded, waiting for selector")

            try:
                await detail_page.wait_for_selector(
                    self.DESCRIPTION_SELECTOR,
                    timeout=8000,
                )
                self.logger.info("Found description selector")
            except Exception:
                self.logger.info("Description selector not found, trying fallback")
                try:
                    await detail_page.wait_for_selector(
                        'div.section.page-centered',
                        timeout=10000,
                    )
                except Exception:
                    self.logger.info("Fallback selector also not found")

            soup = await self._get_soup(detail_page)

            detail_data: dict[str, str] = {}
            description = self._extract_description(soup)

            if description:
                detail_data["description"] = description
                self.logger.info("Description extracted: %d chars", len(description))
            else:
                self.logger.info("No description found")

            return detail_data

        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        container = soup.select_one(self.DESCRIPTION_SELECTOR)

        if not container:
            return ""

        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    # ------------------------------------------------------------------
    # Fallback
    # ------------------------------------------------------------------

    async def _fallback_links(self, page: Page, source_url: str, max_jobs: int) -> list[Job]:
        soup = await self._get_soup(page)

        jobs: list[Job] = []
        seen_urls: set[str] = set()

        for link in soup.select('a[href*="jobs.lever.co/hevodata/"]'):
            if len(jobs) >= max_jobs:
                break

            href = link.get("href")

            if not href or "jobs.lever.co/hevodata/" not in str(href):
                continue

            url = str(href).strip()

            if url in seen_urls:
                continue

            seen_urls.add(url)

            title = ""
            parent_card = link.find_parent("div", class_="opening-card-wrapper")

            if parent_card:
                title_el = parent_card.select_one("h5.card-position")

                if title_el:
                    data_pos = title_el.get("data-position")

                    if data_pos:
                        title = self._clean_text(str(data_pos))
                    else:
                        title = self._clean_text(title_el.get_text())

                loc_el = parent_card.select_one("div.card-loc")
                location = self._clean_text(loc_el.get_text()) if loc_el else ""
            else:
                location = ""

            if not title:
                title = self._clean_text(link.get_text())

            job_id = self._extract_job_id(url)

            jobs.append(Job(
                job_id=job_id,
                company=self.company_config.get("name", "HevoData"),
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

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

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
