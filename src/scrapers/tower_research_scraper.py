from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class TowerResearchScraper(BaseScraper):
    """
    Scraper for Tower Research Capital job board (tower-research.com).

    Tower Research embeds a Greenhouse job board via an iframe on their
    WordPress page. We use the **direct Greenhouse embed URL** for the
    listing (avoids iframe issues) and the **WordPress detail page** for
    full job descriptions (where the ``job_app`` iframe contains the
    rich HTML).

    Listing approach:
        1. Navigate to::
               https://job-boards.greenhouse.io/embed/job_board?for=towerresearchcapital
        2. Interact with react-select filters for Department (multi) and Office.
        3. Parse the re-rendered ``tr.job-post`` cards.

    Detail approach:
        1. Navigate to::
               https://www.tower-research.com/open-positions/?gh_jid=XXXXXXX
        2. Wait for the ``job_app`` Greenhouse iframe.
        3. Extract description from the iframe body.

    Listing card (Greenhouse table layout):

        tr.job-post
          td.cell > a[href*="?gh_jid="]
            p.body.body--medium               (job title)
            p.body__secondary.body--metadata  (location)
    """

    EMBED_LISTING_URL = "https://job-boards.greenhouse.io/embed/job_board?for=towerresearchcapital"
    DETAIL_BASE = "https://www.tower-research.com"

    CARD_SELECTOR = 'tr.job-post'
    TITLE_SELECTOR = 'p.body.body--medium'
    LOCATION_SELECTOR = 'p.body__secondary.body--metadata'

    DEPARTMENT_INPUT_ID = '#department-filter'
    OFFICE_INPUT_ID = '#office-filter'

    FILTER_DEPARTMENTS = [
        "Application Reliability Engineering",
        "Core AI and Machine Learning",
        "Core Engineering",
        "Infrastructure Engineering",
        "On-Campus Recruiting",
    ]
    FILTER_OFFICE = "Gurgaon"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            # ── Listing: direct Greenhouse embed + filter interaction ──
            await page.goto(self.EMBED_LISTING_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)

            # Apply Department filters (multi-select — reopen for each).
            for dept in self.FILTER_DEPARTMENTS:
                await self._select_react_option(page, self.DEPARTMENT_INPUT_ID, dept)
                await page.wait_for_timeout(800)  # let table re-render

            # Apply Office filter.
            await self._select_react_option(page, self.OFFICE_INPUT_ID, self.FILTER_OFFICE)
            await page.wait_for_timeout(3000)

            soup = await self._get_soup(page)
            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No Tower Research job cards after filtering.")
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

                # Enrich from WordPress detail page.
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
                else:
                    try:
                        detail_data = await self._scrape_detail_page(job.url)
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
                            "Failed to enrich Tower Research detail page %s: %s",
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
    # React-select interaction (on the embed page, no iframe)
    # ------------------------------------------------------------------

    async def _select_react_option(
        self, page: Page, input_selector: str, label: str
    ) -> None:
        """Open a react-select dropdown, click an option by label, then close it."""
        try:
            await page.click(input_selector, timeout=10000)
            await page.wait_for_timeout(1000)
        except Exception:
            self.logger.debug("Could not click react-select %s", input_selector)
            return

        try:
            await page.wait_for_selector('[role="listbox"]', timeout=5000)
        except Exception:
            self.logger.debug("react-select listbox did not appear for %s", input_selector)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
            return

        try:
            option = page.locator(f'[role="option"]:has-text("{label}")').first
            await option.click(timeout=5000)
            await page.wait_for_timeout(400)
        except Exception:
            self.logger.debug("Could not select option: %s", label)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
            return

        # Dropdown auto-closes on selection; small settle time.
        await page.wait_for_timeout(300)

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card)
        title = self._extract_title(card)
        job_id = self._extract_job_id_from_url(link)
        location = self._extract_location(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Tower Research"),
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
        anchor = card.select_one('a[href*="?gh_jid="]') or card.select_one('a[href]')
        if not anchor:
            return ""

        href = anchor.get("href")
        if not href:
            return ""

        return self._make_detail_url(str(href))

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        if el:
            return self._clean_text(el.get_text())
        return ""

    def _extract_location(self, card: Tag) -> str:
        el = card.select_one(self.LOCATION_SELECTOR)
        if el:
            return self._clean_text(el.get_text())
        return ""

    def _extract_job_id_from_url(self, url: str) -> str:
        if not url:
            return ""
        match = re.search(r'[?&]gh_jid=(\d+)', url)
        if match:
            return match.group(1)
        return ""

    # ------------------------------------------------------------------
    # Detail page (WordPress page + Greenhouse job_app iframe)
    # ------------------------------------------------------------------

    def _make_detail_url(self, href: str) -> str:
        href = html.unescape(href).strip()
        if href.startswith(("http://", "https://")):
            return href
        if href.startswith("/"):
            return f"{self.DETAIL_BASE}{href}"
        return f"{self.DETAIL_BASE}/{href}"

    async def _get_detail_page(self) -> Page:
        if self.context:
            try:
                return await self.context.new_page()
            except Exception:
                self.logger.debug("Discarding stale browser context.")
                await self.close_browser()
        return await self.new_page()

    async def _scrape_detail_page(self, job_url: str) -> dict[str, str]:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)
            await detail_page.wait_for_timeout(8000)

            # Find the Greenhouse job_app iframe.
            frame = None
            for f in detail_page.frames:
                if 'greenhouse' in f.url.lower() and 'job_app' in f.url:
                    frame = f
                    break

            # Also try by name as fallback.
            if not frame:
                frame = detail_page.frame(name='grnhse_iframe')

            if frame:
                try:
                    await frame.wait_for_selector('h3', timeout=10000)
                except Exception:
                    pass

                detail_html = await frame.content()
                soup = BeautifulSoup(detail_html, "html.parser")
            else:
                soup = await self._get_soup(detail_page)

            detail_data: dict[str, str] = {}
            description = self._extract_description(soup) if soup else ""
            if description:
                detail_data["description"] = description

            return detail_data

        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        """Extract description from the Greenhouse job_app iframe content."""
        for unwanted in soup.select('script, style, noscript, nav, header, footer'):
            unwanted.decompose()

        # Anchor on "Responsibilities" h3.
        for h3 in soup.find_all('h3'):
            h3_text = h3.get_text(strip=True).lower()
            if 'responsibilities' in h3_text:
                parent = h3.parent
                for _ in range(4):
                    if parent and parent.name == 'div':
                        text = parent.get_text(separator="\n")
                        if len(text) > 300:
                            return self._clean_multiline_text(text)
                    parent = parent.parent if parent else None
                break

        body = soup.find('body')
        if body:
            return self._clean_multiline_text(body.get_text(separator="\n"))
        return ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
