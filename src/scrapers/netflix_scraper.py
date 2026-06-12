from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from bs4 import Tag

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class NetflixScraper(BaseScraper):
    """Scraper for Netflix Careers (https://explore.jobs.netflix.net).

    Netflix uses a custom SPA where job cards are listed on the left side
    and clicking a card loads the full job description on the right side
    (no separate page navigation).

    Card structure
    --------------
    Each ``div.card.position-card.pointer`` (``role="button"``) contains::

        <div role="button" class="card position-card pointer"
             data-test-id="position-card-0">
          <div class="position-title-container">
            <div class="position-title line-clamp line-clamp-2">
              Creative Technology Systems and Operations Specialist ...
            </div>
          </div>
          <p class="position-location" id="position-location-0">
            <i class="fa fal fa-map-marker-alt"></i>Hyderabad, India
          </p>
          <div class="row flexbox">
            <span id="position-department-0">Engineering Operations</span>
          </div>
        </div>

    Detail enrichment
    -----------------
    Clicking a card loads the detail panel on the right side containing:

    * ``div.custom-jd-container`` — fields like Job Requisition ID (e.g. JR39168),
      Teams, and Work Type.
    * ``div.position-job-description`` — the full job description HTML.

    The page is heavy (many assets) so generous navigation / selector
    timeouts and extra settle delays are used throughout.
    """

    # ---- Card selectors ----
    CARD_SELECTOR = 'div.position-card[role="button"]'
    CARD_TITLE_SELECTOR = "div.position-title"
    CARD_LOCATION_SELECTOR = "p.position-location"
    CARD_DEPARTMENT_SELECTOR = "span[id^='position-department-']"

    # ---- Detail panel selectors ----
    DETAIL_DESC_SELECTOR = "div.position-job-description"
    DETAIL_REQ_CONTAINER_SELECTOR = "div.custom-jd-container"

    # Job ID format: JR followed by digits (e.g. JR39168)
    JOB_ID_PATTERN = re.compile(r"JR\d+")

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            # ---- Navigate to job board ----
            # Use domcontentloaded + generous timeout for this heavy SPA.
            await page.goto(source_url, wait_until="domcontentloaded", timeout=90000)

            # ---- Wait for cards to render ----
            await page.wait_for_selector(self.CARD_SELECTOR, timeout=60000)

            # Extra settle time — the SPA loads many assets and may still be
            # hydrating React components.
            await asyncio.sleep(5)

            # ---- Extract cards via BS4 ----
            soup = await self._get_soup(page)
            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No Netflix job cards found.")
                return jobs

            seen_ids: set[str] = set()

            for idx, card in enumerate(cards):
                if len(jobs) >= max_jobs:
                    break

                # --- Card fields ---
                title_el = card.select_one(self.CARD_TITLE_SELECTOR)
                title = title_el.get_text(strip=True) if title_el else ""

                location = ""
                location_el = card.select_one(self.CARD_LOCATION_SELECTOR)
                if location_el:
                    # Remove <i> icon text from location.
                    for icon in location_el.select("i"):
                        icon.decompose()
                    location = location_el.get_text(strip=True)

                department = ""
                dept_el = card.select_one(self.CARD_DEPARTMENT_SELECTOR)
                if dept_el:
                    department = dept_el.get_text(strip=True)

                if not title:
                    continue

                # --- Click card → load detail panel ---
                job_id = ""
                description = None

                if not self._should_exclude(title):
                    try:
                        # Click the card by its data-test-id attribute.
                        card_test_id = f'div[data-test-id="position-card-{idx}"]'
                        card_locator = page.locator(card_test_id).first
                        await card_locator.scroll_into_view_if_needed()
                        await card_locator.click()

                        # Wait for the description panel to populate.
                        await page.wait_for_selector(
                            self.DETAIL_DESC_SELECTOR, timeout=30000
                        )
                        await asyncio.sleep(1)

                        detail_soup = await self._get_soup(page)

                        # Extract requisition ID (e.g. JR39168).
                        job_id = self._extract_job_id(detail_soup)

                        # Extract description text.
                        desc_el = detail_soup.select_one(self.DETAIL_DESC_SELECTOR)
                        if desc_el:
                            description = self._extract_description(desc_el)
                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich Netflix job detail for '%s': %s",
                            title,
                            exc,
                        )

                job = Job(
                    job_id=job_id,
                    company=self.company_config.get("name", "Netflix"),
                    title=title,
                    location=location,
                    url=source_url,
                    source_url=source_url,
                    posted_date=None,
                    description=description,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    extracted_experience_parts="",
                )

                if job_id and job_id in seen_ids:
                    continue

                if job_id:
                    seen_ids.add(job_id)
                jobs.append(job)

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Detail panel helpers
    # ------------------------------------------------------------------

    def _extract_job_id(self, soup) -> str:
        """Extract the requisition ID (e.g. JR39168) from the detail panel.

        The ``div.custom-jd-container`` contains one or more
        ``div.custom-jd-field`` blocks, each with an ``<h4>`` label and a
        ``<div>`` value.  We look for the one labelled
        "Job Requisition ID".
        """
        req_container = soup.select_one(self.DETAIL_REQ_CONTAINER_SELECTOR)
        if not req_container:
            return ""

        fields = req_container.select("div.custom-jd-field")
        for field in fields:
            h4 = field.select_one("h4")
            if h4 and "job requisition id" in h4.get_text(strip=True).lower():
                # The value is in a sibling <div> (not the <h4>).
                value_divs = [
                    d for d in field.select("div") if d.name != "h4"
                ]
                if not value_divs:
                    # <div> may be a direct child.
                    value_divs = [
                        child for child in field.children
                        if hasattr(child, "name") and child.name == "div"
                    ]
                for div in value_divs:
                    text = div.get_text(strip=True)
                    m = self.JOB_ID_PATTERN.search(text)
                    if m:
                        return m.group(0)

        return ""

    def _extract_description(self, container: Tag) -> str:
        """Extract clean description text from the Netflix detail panel.

        Preserves structure: headings become section breaks, paragraphs /
        list items are grouped.
        """
        # Remove script / style / noscript tags.
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
