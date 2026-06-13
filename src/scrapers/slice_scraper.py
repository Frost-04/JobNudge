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


class SliceScraper(BaseScraper):
    """
    Scraper for slice bank careers page (Next.js SPA).

    The page at slice.bank.in/careers/open-positions is a React-based SPA
    with filter buttons for departments. The "Software Engineering" button
    is clicked to filter results.

    Card structure (after filtering):

        a[href="/careers/open-positions/{slug}"]
          span.font-medium.text-black         (job title)
          span:nth-child(2)                   (department - "Software Engineering")
          span:nth-child(3)                   (location - e.g. "Bangalore")
          span:nth-child(4)                   (type - e.g. "Full-time")

    Detail page structure:

        div.mx-auto.w-full.max-w-4xl
          section > h2       (section heading)
          section > p, ul    (description content)
    """

    # ---- Filter selectors ----
    FILTER_BUTTON = 'button:has(span:not(.hidden)):text-is("Software Engineering")'

    # ---- Card selectors ----
    CARD_SELECTOR = 'a[href^="/careers/open-positions/"][aria-label]'

    JOB_CARD_SELECTORS = [
        'a[href^="/careers/open-positions/"][aria-label]',
        'a[href*="/careers/open-positions/"]',
    ]

    TITLE_SELECTOR = 'span.font-medium.text-black'

    # ---- Detail page selectors ----
    DETAIL_SELECTOR = 'div.mx-auto.w-full.max-w-4xl'

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            # SPA — use domcontentloaded + settle.
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)

            # ---- Step 1: Click "Software Engineering" filter if not already active ----
            await self._apply_software_engineering_filter(page)

            await page.wait_for_timeout(2000)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No slice job cards found.")
                return jobs

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

                # Enrich by opening the detail page.
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
                            "Failed to enrich slice job detail page %s: %s",
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

    async def _apply_software_engineering_filter(self, page: Page) -> None:
        """
        Click the "Software Engineering" department filter button.
        The button text contains just "Software Engineering" — locate by
        searching all filter buttons for matching text.
        """
        try:
            # Find all department filter buttons.
            buttons = await page.query_selector_all(
                'div.space-y-8 div.flex.flex-col.gap-1\\.5 button'
            )

            for button in buttons:
                text = await button.inner_text()
                text = text.strip()

                if text == "Software Engineering":
                    # Check if already active (has bg-darkOrchid class)
                    class_attr = await button.get_attribute("class") or ""

                    if "bg-darkOrchid" in class_attr:
                        self.logger.info(
                            "Software Engineering filter already active"
                        )
                    else:
                        await button.evaluate("el => el.click()")
                        self.logger.info("Clicked Software Engineering filter")
                        await page.wait_for_timeout(2000)

                    return

            self.logger.warning("Software Engineering filter button not found")

        except Exception as exc:
            self.logger.warning(
                "Failed to apply Software Engineering filter: %s", exc
            )

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card, source_url)
        title = self._extract_title(card)
        job_id = self._extract_job_id(card, link)
        location = self._extract_location(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "slice"),
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

        return self._make_job_url(source_url, href)

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        # Fallback: aria-label attribute, extract title after colon.
        aria_label = card.get("aria-label", "")

        if aria_label and ":" in aria_label:
            title_part = aria_label.split(":", 1)[0].strip()

            if title_part:
                return self._clean_text(title_part)

        return ""

    def _extract_location(self, card: Tag) -> str:
        # Location is the 3rd span child (index 2) in the grid row.
        spans = card.select(
            'span.min-w-0'
        )

        if len(spans) >= 3:
            return self._clean_text(spans[2].get_text())

        return ""

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        slice job URLs look like:

        https://slice.bank.in/careers/open-positions/sde-2-backend

        Extract the URL slug.
        """
        if not link:
            return ""

        parsed = urlparse(link)
        path = parsed.path.strip("/")

        if path:
            segments = path.split("/")
            return segments[-1] if segments else path

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
                    "Shared browser context is no longer usable; discarding and creating a fresh one."
                )
                await self.close_browser()

        return await self.new_page()

    async def _scrape_detail_page(self, job_url: str) -> dict[str, str]:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)
            # Next.js SPA — networkidle needed for full client-side render.
            await detail_page.goto(job_url, wait_until="networkidle", timeout=60000)

            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_SELECTOR,
                    timeout=15000,
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

    def _extract_description(self, soup) -> str:
        # There are TWO div.mx-auto.w-full.max-w-4xl containers:
        # the first is card metadata header, the second has the description sections.
        containers = soup.select(self.DETAIL_SELECTOR)

        if not containers:
            return ""

        # Take the LAST matching container (the one with description sections).
        container = containers[-1]

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

        for link in soup.select('a[href*="/careers/open-positions/"]'):
            if len(jobs) >= max_jobs:
                break

            href = link.get("href")

            if not href or "/careers/open-positions/" not in str(href):
                continue

            url = self._make_job_url(source_url, str(href))

            if url in seen_urls:
                continue

            seen_urls.add(url)

            title = ""
            title_el = link.select_one("span.font-medium.text-black")

            if title_el:
                title = self._clean_text(title_el.get_text())

            if not title:
                aria_label = link.get("aria-label", "")

                if aria_label and ":" in aria_label:
                    title = self._clean_text(aria_label.split(":", 1)[0].strip())

            if not title:
                title = self._clean_text(link.get_text())

            location = ""
            spans = link.select("span.min-w-0")

            if len(spans) >= 3:
                location = self._clean_text(spans[2].get_text())

            job_id = self._extract_job_id(link, url)

            jobs.append(Job(
                job_id=job_id,
                company=self.company_config.get("name", "slice"),
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

    def _make_job_url(self, source_url: str, href: str) -> str:
        href = html.unescape(href).strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        parsed_source = urlparse(source_url)
        origin = f"{parsed_source.scheme}://{parsed_source.netloc}"

        if href.startswith("/"):
            return f"{origin}{href}"

        return make_absolute_url(source_url, href)

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
