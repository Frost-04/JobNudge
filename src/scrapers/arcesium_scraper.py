from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id


class ArcesiumScraper(BaseScraper):
    """
    Scraper for Arcesium careers page (custom listing + Greenhouse detail).

    The listing page at ``arcesium.com/careers#open-positions`` is a Next.js
    SPA with react-select filters for Country and Department.  Cards are
    server‑rendered ``<a>`` tags that link directly to Greenhouse job detail
    pages.

    Each card is accompanied by a JSON‑LD ``<script>`` tag that contains
    ``datePosted`` and other metadata.

    Expected listing card structure:

        <script type="application/ld+json">
          {"title":"...","datePosted":"...","jobLocation":{"address":{"addressLocality":"..."}}}
        </script>
        <a class="flex flex-col gap-4 ..." href="https://job-boards.greenhouse.io/arcesiumllc/jobs/NNN">
          <p class="job-title text-[18px] font-semibold text-black">TITLE</p>
          <span class="inline-flex ...">Technology</span>
          <div class="flex flex-row gap-6">
            <div class="flex flex-row gap-2">
              <div>LOCATION</div>
            </div>
          </div>
        </a>

    Expected detail page structure (standard Greenhouse):

        div.job__description.body              (full rich-text description)
    """

    # ---- React-Select filter selectors ----
    COUNTRY_FILTER_CONTAINER = "div.filter-location div[class*='-container']"
    DEPARTMENT_FILTER_CONTAINER = "div.filter-department div[class*='-container']"

    # ---- Card selectors ----
    CARD_SELECTOR = "a[href*='greenhouse.io/arcesiumllc/jobs/']"
    TITLE_SELECTOR = "p.job-title"
    LOCATION_SELECTOR = "div.flex.flex-row.gap-6 div.flex.flex-row.gap-2 > div"
    JOB_CARD_SELECTORS = [
        "a[href*='greenhouse.io/arcesiumllc/jobs/']",
        "p.job-title",
    ]
    JSONLD_SELECTOR = "script[type='application/ld+json']"

    # ---- Detail page selectors (Greenhouse) ----
    DETAIL_TITLE_SELECTOR = "h1.job__title, div.job__title"
    DETAIL_LOCATION_SELECTOR = "div.job__location"
    DETAIL_DESCRIPTION_SELECTOR = "div.job__description.body"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for the page to fully render (Next.js SPA).
            await page.wait_for_timeout(3000)

            # --- Apply Country filter: "India" ---
            await self._apply_react_select_filter(
                page,
                self.COUNTRY_FILTER_CONTAINER,
                "India",
            )

            # --- Apply Department filter: "Technology" ---
            await self._apply_react_select_filter(
                page,
                self.DEPARTMENT_FILTER_CONTAINER,
                "Technology",
            )

            # Wait for cards to reload after filters.
            await page.wait_for_timeout(2000)

            # Wait for cards to appear.
            try:
                await page.wait_for_selector(self.CARD_SELECTOR, timeout=15000)
            except Exception:
                pass

            soup = await self._get_soup(page)

            # Build a title → posted_date map from JSON-LD scripts on the page.
            posted_date_map = self._build_jsonld_date_map(soup)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No Arcesium job cards found.")
                return jobs

            seen_ids: set[str] = set()
            seen_urls: set[str] = set()

            for card in cards[:max_jobs]:
                job = self._parse_card(card, source_url, posted_date_map)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_ids:
                    continue

                if job.url in seen_urls:
                    continue

                # Enrich from Greenhouse detail page (non-excluded only).
                if self._should_exclude(job.title):
                    self.logger.debug(
                        "Skipping detail enrichment for excluded role: %s",
                        job.title,
                    )
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
                            "Failed to enrich Arcesium detail page %s: %s",
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
    # React-Select filter interaction
    # ------------------------------------------------------------------

    async def _apply_react_select_filter(
        self,
        page: Page,
        container_selector: str,
        option_text: str,
    ) -> None:
        """Open a react-select dropdown and click the matching option."""
        try:
            # Click the react-select container to open the dropdown.
            container = page.locator(container_selector).first
            await container.click(timeout=5000)
            await page.wait_for_timeout(500)

            # Find and click the option with the matching text.
            option = page.locator(
                f"div[id^='react-select-'][id*='-option-']",
                has_text=option_text,
            ).first
            await option.click(timeout=5000)
            await page.wait_for_timeout(500)

            self.logger.debug("Applied filter: %s", option_text)
        except Exception as exc:
            self.logger.warning(
                "Could not apply filter '%s': %s",
                option_text,
                exc,
            )

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(
        self,
        card: Tag,
        source_url: str,
        posted_date_map: dict[str, str],
    ) -> Job | None:
        link = self._extract_link(card)
        title = self._extract_title(card)
        job_id = self._extract_job_id(link)
        location = self._extract_location(card)
        posted_date = posted_date_map.get(title, "")

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Arcesium"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=posted_date,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag) -> str:
        href = card.get("href")

        if not href:
            return ""

        return str(href)

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        return ""

    def _extract_job_id(self, url: str) -> str:
        """Greenhouse job URLs: ...greenhouse.io/arcesiumllc/jobs/5093923007"""
        if not url:
            return ""

        match = re.search(r"/jobs/(\d+)", url, flags=re.IGNORECASE)

        if match:
            return match.group(1)

        return extract_job_id(url) or ""

    def _extract_location(self, card: Tag) -> str:
        els = card.select(self.LOCATION_SELECTOR)

        # The card has multiple divs inside the location row — the last
        # text-bearing div is the location.
        for el in els:
            text = self._clean_text(el.get_text())

            if text and text.lower() not in ("location", "locations", "remote"):
                return text

        return ""

    def _build_jsonld_date_map(self, soup) -> dict[str, str]:
        """Parse all JSON-LD script tags and build a title → datePosted map."""
        date_map: dict[str, str] = {}

        for script in soup.select(self.JSONLD_SELECTOR):
            try:
                data = json.loads(script.string or "")
                title = data.get("title", "")
                date_posted = data.get("datePosted", "")

                if title and date_posted:
                    date_map[title] = date_posted
            except (json.JSONDecodeError, AttributeError):
                continue

        return date_map

    # ------------------------------------------------------------------
    # Detail page enrichment (Greenhouse)
    # ------------------------------------------------------------------

    async def _get_detail_page(self) -> Page:
        """Return a new page for detail scraping."""
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

            await detail_page.goto(
                job_url,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            # Wait for any detail content selector.
            detail_selectors = [
                self.DETAIL_DESCRIPTION_SELECTOR,
                self.DETAIL_TITLE_SELECTOR,
                "div.job__description",
                "h1",
            ]

            for sel in detail_selectors:
                try:
                    await detail_page.wait_for_selector(sel, timeout=10000)
                    break
                except Exception:
                    continue

            soup = await self._get_soup(detail_page)

            detail_data: dict[str, str] = {}

            description = self._extract_description(soup)

            if description:
                detail_data["description"] = description

            return detail_data

        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        container = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)

        if not container:
            return ""

        # Remove scripts and styles.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

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
