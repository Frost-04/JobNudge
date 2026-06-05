from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class QualcommScraper(BaseScraper):
    """
    Scraper for Qualcomm Careers search result pages.

    Qualcomm uses the **Phenom People** career-platform — the same platform
    as Microsoft Careers.  The DOM structure, CSS-module class naming, and
    detail-page layout are nearly identical.

    Listing card:

        div[data-test-id="job-listing"]
          a[href^="/careers/job/"]
            div.title-1aNJK
            div.fieldValue-3kEar       (location)
            div.fieldValue-3kEar       (job family)
            div.subData-13Lm1          (posted date)

    Detail page:

        div.detailContainer-2qNET
          div.detailLabel-2AsIg  → Job ID / Date Posted / …
          div.detailValue-3NGwm

        div#job-description-container
    """

    # ---- Card selectors (same platform as Microsoft) ----
    JOB_CARD_SELECTORS = [
        'div[data-test-id="job-listing"]',
        'a[href*="/careers/job/"]',
    ]

    CARD_SELECTOR = 'div[data-test-id="job-listing"]'
    LINK_SELECTOR = 'a[href*="/careers/job/"]'
    TITLE_SELECTOR = 'div[class^="title-"], div[class*=" title-"]'
    LOCATION_SELECTOR = 'div[class^="fieldValue-"], div[class*=" fieldValue-"]'
    POSTED_SELECTOR = 'div[class^="subData-"], div[class*=" subData-"]'

    # ---- Detail page selectors ----
    DETAIL_CONTAINER_SELECTOR = 'div[class^="detailContainer-"], div[class*=" detailContainer-"]'
    DETAIL_LABEL_SELECTOR = 'div[class^="detailLabel-"], div[class*=" detailLabel-"]'
    DETAIL_VALUE_SELECTOR = 'div[class^="detailValue-"], div[class*=" detailValue-"]'
    DESCRIPTION_SELECTOR = "#job-description-container"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

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

                # Enrich with detail page: metadata + description.
                try:
                    detail_data = await self._scrape_detail_page(job.url)

                    detail_posted_date = detail_data.get("date posted", "")
                    detail_description = detail_data.get("description", "")

                    metadata_desc = self._format_detail_metadata(detail_data)

                    combined = self._join_parts(metadata_desc, detail_description)

                    job = Job(
                        job_id=job.job_id,
                        company=job.company,
                        title=job.title,
                        location=job.location,
                        url=job.url,
                        source_url=job.source_url,
                        posted_date=detail_posted_date or job.posted_date,
                        description=combined or job.description,
                        scraped_at=datetime.now(timezone.utc).isoformat(),
                        extracted_experience_parts="",
                    )
                except Exception as exc:
                    self.logger.warning(
                        "Failed to enrich Qualcomm job detail %s: %s", job.url, exc
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
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card, source_url)
        title = self._extract_title(card)
        job_id = self._extract_job_id(card, link)
        location = self._extract_location(card)
        posted_date = self._extract_posted_date(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Qualcomm"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=posted_date or None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag, source_url: str) -> str:
        el = card.select_one(self.LINK_SELECTOR)
        if not el:
            return ""
        href = el.get("href")
        if not href:
            return ""
        return self._make_qualcomm_job_url(source_url, str(href))

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        if el:
            return self._clean_text(el.get_text())

        # Fallback: aria-label="View job: Engineer, Lead- Video Firmware"
        link = card.select_one(self.LINK_SELECTOR)
        if link:
            aria = link.get("aria-label")
            if aria:
                return self._clean_text(str(aria).replace("View job:", ""))
        return ""

    def _extract_location(self, card: Tag) -> str:
        """The first fieldValue is location (has a map-marker icon sibling)."""
        values = card.select(self.LOCATION_SELECTOR)
        if values:
            return self._clean_text(values[0].get_text())
        return ""

    def _extract_posted_date(self, card: Tag) -> str:
        el = card.select_one(self.POSTED_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """Extract from URL /careers/job/446718629365 or element id job-card-NNN-job-list."""
        if link:
            job_id = self._extract_qualcomm_job_id_from_url(link)
            if job_id:
                return job_id

        link_el = card.select_one(self.LINK_SELECTOR)
        if link_el:
            elem_id = link_el.get("id")
            if elem_id:
                m = re.search(r"job-card-(\d+)-job-list", str(elem_id))
                if m:
                    return m.group(1)

        return extract_job_id(link) if link else ""

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def _get_detail_page(self) -> Page:
        if self.context:
            try:
                return await self.context.new_page()
            except Exception:
                self.logger.debug("Shared browser context is no longer usable; creating a fresh one.")
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
                    self.DETAIL_CONTAINER_SELECTOR,
                    'div[data-test-id="job-listing"]',
                ],
            )

            soup = await self._get_soup(detail_page)

            detail_data = self._extract_detail_metadata(soup)
            description = self._extract_description(soup)
            if description:
                detail_data["description"] = description

            return detail_data
        finally:
            await detail_page.close()

    def _extract_detail_metadata(self, soup) -> dict[str, str]:
        detail_data: dict[str, str] = {}
        for container in soup.select(self.DETAIL_CONTAINER_SELECTOR):
            label_el = container.select_one(self.DETAIL_LABEL_SELECTOR)
            value_el = container.select_one(self.DETAIL_VALUE_SELECTOR)
            label = self._clean_text(label_el.get_text() if label_el else "")
            value = self._clean_text(value_el.get_text() if value_el else "")
            if label and value:
                detail_data[label.lower()] = value
        return detail_data

    def _extract_description(self, soup) -> str:
        container = soup.select_one(self.DESCRIPTION_SELECTOR)
        if not container:
            return ""
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()
        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    def _format_detail_metadata(self, detail_data: dict[str, str]) -> str:
        if not detail_data:
            return ""
        lines: list[str] = []
        preferred = [
            "job id",
            "date posted",
            "company",
            "job area",
            "work site",
            "travel",
            "employment type",
        ]
        for key in preferred:
            value = detail_data.get(key)
            if value:
                lines.append(f"{key.title()}: {value}")
        # Include any remaining keys not in preferred order.
        for key, value in detail_data.items():
            if key not in {"description"} and key not in [k for k in preferred]:
                lines.append(f"{key.title()}: {value}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _wait_for_any_selector(self, page: Page, selectors: list[str]) -> str | None:
        timeout_ms = self._to_ms(
            self.settings.get("run", {}).get("page_load_timeout_seconds"), 45000
        )
        for sel in selectors:
            try:
                await page.wait_for_selector(sel, timeout=timeout_ms)
                return sel
            except Exception:
                continue
        return None

    async def _fallback_links(self, page: Page, source_url: str, max_jobs: int) -> list[Job]:
        """Fallback: scan all <a> tags matching the job URL pattern."""
        soup = await self._get_soup(page)
        anchors = soup.select('a[href*="/careers/job/"]')
        jobs: list[Job] = []
        seen: set[str] = set()

        for a in anchors[:max_jobs]:
            href = a.get("href")
            if not href:
                continue
            url = self._make_qualcomm_job_url(source_url, str(href))
            if url in seen:
                continue
            seen.add(url)
            job_id = self._extract_qualcomm_job_id_from_url(url)

            jobs.append(Job(
                job_id=job_id,
                company=self.company_config.get("name", "Qualcomm"),
                title=self._clean_text(a.get_text()),
                location="",
                url=url,
                source_url=source_url,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                extracted_experience_parts="",
            ))

        return jobs

    def _make_qualcomm_job_url(self, source_url: str, href: str) -> str:
        import html as _html
        href = _html.unescape(href).strip()
        if href.startswith(("http://", "https://")):
            return href
        parsed = urlparse(source_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if href.startswith("/careers/job/"):
            return f"{origin}{href}"
        if href.startswith("careers/job/"):
            return f"{origin}/{href}"
        return make_absolute_url(source_url, href)

    def _extract_qualcomm_job_id_from_url(self, url: str) -> str:
        if not url:
            return ""
        m = re.search(r"/careers/job/(\d+)", url, re.IGNORECASE)
        if m:
            return m.group(1)
        return extract_job_id(url) or ""

    def _clean_text(self, text: str) -> str:
        import html as _html
        if not text:
            return ""
        text = _html.unescape(text)
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _clean_multiline_text(self, text: str) -> str:
        if not text:
            return ""
        text = re.sub(r"\n{3,}", "\n\n", text)
        lines = [line.strip() for line in text.split("\n")]
        deduped: list[str] = []
        prev = None
        for line in lines:
            if line and line == prev:
                continue
            deduped.append(line)
            prev = line
        return "\n".join(deduped).strip()

    def _join_parts(self, *parts: str) -> str:
        cleaned = [p.strip() for p in parts if p and p.strip()]
        return "\n\n".join(cleaned)
