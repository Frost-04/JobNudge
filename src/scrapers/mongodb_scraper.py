from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class MongoDbScraper(BaseScraper):
    """Scraper for MongoDB Careers (https://www.mongodb.com/company/careers/see-jobs).

    MongoDB uses a custom React SPA built on their "Flora" design system
    (``automation-testid="flora-*"`` attributes).  Filters are checkboxes
    grouped by Location and Department; selecting them triggers a
    client-side re-render of the job list.

    Each card is an ``<a>`` element linking to a separate detail page
    hosted on ``mongodb.com/careers/job/?gh_jid=...``.

    Card structure
    --------------
    Each ``a[automation-testid="flora-ListItem"]`` contains::

        <a automation-testid="flora-ListItem"
           href="https://www.mongodb.com/careers/job/?gh_jid=7704173">
          <div class="css-163xkmn">
            <div class="css-1b0tnck">
              <span class="css-qq83g2">Senior Staff Engineer</span>
              <div class="css-1hlwqw9">
                <div class="css-2qtvkz">
                  <div class="css-1xm35xc">Bengaluru</div>
                </div>
                <div class="css-1slgwea">
                  <div class="css-1mnmcp3">Full-time</div>
                </div>
              </div>
            </div>
          </div>
        </a>

    Detail enrichment
    -----------------
    Each card links to a separate page where the job description lives
    inside ``div.job-description``.  The page is heavy, so generous
    navigation timeouts are used.

    Anti-bot: No Cloudflare/Akamai observed.
    """

    # ---- Card selectors ----
    CARD_SELECTOR = 'a[automation-testid="flora-ListItem"]'
    CARD_TITLE_SELECTOR = "span.css-qq83g2"
    CARD_LOCATION_SELECTOR = "div.css-1xm35xc"

    # ---- Detail page selectors ----
    DETAIL_DESC_SELECTOR = "div.job-description"

    # Job ID pattern from URL query parameter
    JOB_ID_PATTERN = re.compile(r"gh_jid=(\d+)")

    # ---- Filter checkbox IDs ----
    LOCATION_FILTERS = [
        "location-Bengaluru",
        "location-Gurugram",
        "location-India",
        "location-Mumbai",
    ]
    DEPARTMENT_FILTERS = [
        "department-1",  # Campus
        "department-3",  # Engineering
    ]

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            # ---- Navigate to job board ----
            # Heavy SPA — use generous timeouts.
            await page.goto(source_url, wait_until="domcontentloaded", timeout=90000)

            # Wait for initial cards to appear.
            await page.wait_for_selector(self.CARD_SELECTOR, timeout=60000)

            # Extra settle time for the heavy React SPA.
            await asyncio.sleep(6)

            # ---- Apply location filters ----
            for loc_id in self.LOCATION_FILTERS:
                try:
                    label = page.locator(f'label[for="{loc_id}"]')
                    if await label.count() > 0:
                        await label.first.scroll_into_view_if_needed()
                        await label.first.click()
                        await asyncio.sleep(0.5)
                except Exception as exc:
                    self.logger.warning(
                        "Failed to click location filter '%s': %s", loc_id, exc
                    )

            # ---- Apply department filters ----
            for dept_id in self.DEPARTMENT_FILTERS:
                try:
                    label = page.locator(f'label[for="{dept_id}"]')
                    if await label.count() > 0:
                        await label.first.scroll_into_view_if_needed()
                        await label.first.click()
                        await asyncio.sleep(0.5)
                except Exception as exc:
                    self.logger.warning(
                        "Failed to click department filter '%s': %s", dept_id, exc
                    )

            # Let React re-render with active filters.
            await asyncio.sleep(4)

            # ---- Extract job cards ----
            soup = await self._get_soup(page)
            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No MongoDB job cards found after filtering.")
                return jobs

            seen_ids: set[str] = set()

            for card in cards:
                if len(jobs) >= max_jobs:
                    break

                # --- Title ---
                title_el = card.select_one(self.CARD_TITLE_SELECTOR)
                title = title_el.get_text(strip=True) if title_el else ""

                # --- Location ---
                location_el = card.select_one(self.CARD_LOCATION_SELECTOR)
                location = location_el.get_text(strip=True) if location_el else ""

                # --- URL & Job ID ---
                href = (card.get("href") or "").strip()
                url = href  # Cards have absolute URLs.

                job_id = ""
                if url:
                    m = self.JOB_ID_PATTERN.search(url)
                    if m:
                        job_id = m.group(1)

                if not title:
                    continue
                if job_id and job_id in seen_ids:
                    continue

                job = Job(
                    job_id=job_id,
                    company=self.company_config.get("name", "MongoDB"),
                    title=title,
                    location=location,
                    url=url,
                    source_url=source_url,
                    posted_date=None,
                    description=None,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    extracted_experience_parts="",
                )

                # ---- Enrich via detail page ----
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
                elif url:
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
                            "Failed to enrich MongoDB job detail %s: %s",
                            job.url,
                            exc,
                        )

                if job_id:
                    seen_ids.add(job_id)
                jobs.append(job)

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Detail page enrichment
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

    async def _scrape_detail_page(self, job_url: str) -> str:
        """Navigate to a job detail page and extract the description."""
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(15000)

            await detail_page.goto(
                job_url, wait_until="domcontentloaded", timeout=90000
            )

            # Wait for description container.  Heavy page — may take time.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_DESC_SELECTOR,
                    timeout=30000,
                )
            except Exception:
                pass

            # Extra settle time for lazy-loaded content.
            await asyncio.sleep(2)

            soup = await self._get_soup(detail_page)

            desc_container = soup.select_one(self.DETAIL_DESC_SELECTOR)
            if not desc_container:
                return ""

            return self._extract_description(desc_container)

        finally:
            await detail_page.close()

    def _extract_description(self, container: Tag) -> str:
        """Extract clean description text from the MongoDB detail page."""
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        sections: list[str] = []
        current_section: list[str] = []

        for child in container.children:
            if not hasattr(child, "name"):
                continue

            tag_name = child.name

            if tag_name in ("h1", "h2", "h3", "h4", "h5", "h6"):
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

    @staticmethod
    def _clean_text(text: str) -> str:
        """Normalize whitespace in a string."""
        if not text:
            return ""
        return " ".join(text.split()).strip()
