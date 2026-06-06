from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class IbmScraper(BaseScraper):
    """
    Scraper for IBM Careers search page.

    IBM's career page at careers.ibm.com uses URL query parameters for all
    filters (keyword, level, country, sort).  No Playwright interactions
    are needed — just navigate and parse the Carbon Design System cards.

    Card structure (Carbon Design System bx--card):

        div.bx--card-group__cards__col[role="region"][aria-label="Title"]
          a.bx--card-group__card[href^="https://careers.ibm.com/...?jobId="]
            div.bx--card__eyebrow     → category
            div.bx--card__heading     → title
            div.ibm--card__copy__inner → "Level<br>City, IN" or "Level<br>Multiple Cities"

    Detail page (.section__content):

        article.article--details:first-of-type
          div.article__content__view__field
            div.article__content__view__field__label → section heading
            div.article__content__view__field__value → section body

        details.article--details.article--collapsible  → boilerplate sections (skipped)
    """

    SOURCE_URL = (
        "https://www.ibm.com/in-en/careers/search"
        "?size=20"
        "&field_keyword_08[0]=Software%20Engineering"
        "&field_keyword_18[0]=Entry%20Level"
        "&field_keyword_05[0]=India"
        "&sort=dcdate_desc"
    )

    CARD_SELECTOR = 'div.bx--card-group__cards__col[role="region"]'
    LINK_SELECTOR = 'a.bx--card-group__card'
    EYEBROW_SELECTOR = 'div.bx--card__eyebrow'
    HEADING_SELECTOR = 'div.bx--card__heading'
    COPY_INNER_SELECTOR = 'div.ibm--card__copy__inner'

    DETAIL_CONTAINER = "div.section__content"
    DETAIL_MAIN_ARTICLE = "article.article--details:first-of-type"
    DETAIL_FIELD_SELECTOR = "div.article__content__view__field"
    DETAIL_FIELD_LABEL = "div.article__content__view__field__label"
    DETAIL_FIELD_VALUE = "div.article__content__view__field__value"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.SOURCE_URL
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            try:
                await page.wait_for_selector(self.CARD_SELECTOR, timeout=15000)
            except Exception:
                self.logger.warning("No IBM job cards found.")
                return jobs

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)
            if not cards:
                return jobs

            seen_urls: set[str] = set()
            seen_ids: set[str] = set()

            for card in cards[:max_jobs]:
                link_el = card.select_one(self.LINK_SELECTOR)
                if not link_el:
                    continue

                href = link_el.get("href")
                if not href:
                    continue
                url = str(href).strip()

                title = self._extract_title(card)
                location = self._extract_location(card)
                job_id = self._extract_job_id(url)

                if not url or not title:
                    continue
                if job_id and job_id in seen_ids:
                    continue
                if url in seen_urls:
                    continue

                job = Job(
                    job_id=job_id,
                    company=self.company_config.get("name", "IBM"),
                    title=title,
                    location=location,
                    url=url,
                    source_url=source_url,
                    posted_date=None,
                    description=None,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    extracted_experience_parts="",
                )

                # ---- Enrich with detail page ----
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
                else:
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
                            "Failed to enrich IBM detail %s: %s", job.url, exc
                        )

                if job_id:
                    seen_ids.add(job_id)
                seen_urls.add(job.url)
                jobs.append(job)

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _extract_title(self, card) -> str:
        el = card.select_one(self.HEADING_SELECTOR)
        return self._clean_text(el.get_text()) if el else ""

    def _extract_location(self, card) -> str:
        """Extract location from ibm--card__copy__inner.
        Contains "Entry Level<br>Bangalore, IN" or "Entry Level<br>Multiple Cities".
        We take the text after the last <br>.
        """
        el = card.select_one(self.COPY_INNER_SELECTOR)
        if not el:
            return ""
        # After BS4 parsing, <br> becomes \n.  Take the last non-empty line.
        text = el.get_text(separator="\n")
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        return lines[-1] if lines else ""

    def _extract_job_id(self, url: str) -> str:
        match = re.search(r"[?&]jobId=(\d+)", url)
        return match.group(1) if match else ""

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def _get_detail_page(self) -> Page:
        if self.context:
            try:
                return await self.context.new_page()
            except Exception:
                self.logger.debug("Stale context, recreating.")
                await self.close_browser()
        return await self.new_page()

    async def _scrape_detail_page(self, job_url: str) -> str:
        if not job_url:
            return ""

        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)

            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_MAIN_ARTICLE, timeout=15000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)
            return self._extract_description(soup)

        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        """Extract description from the first main article (skipping
        collapsible boilerplate sections like ABOUT BUSINESS UNIT)."""
        article = soup.select_one(self.DETAIL_MAIN_ARTICLE)
        if not article:
            return ""

        sections: list[str] = []

        for field in article.select(self.DETAIL_FIELD_SELECTOR):
            label_el = field.select_one(self.DETAIL_FIELD_LABEL)
            value_el = field.select_one(self.DETAIL_FIELD_VALUE)

            label = self._clean_text(label_el.get_text()) if label_el else ""
            value = ""
            if value_el:
                for unwanted in value_el.select("script, style, noscript"):
                    unwanted.decompose()
                value = value_el.get_text(separator="\n")
                value = self._clean_multiline_text(value)

            if label and value:
                sections.append(f"{label}\n{value}")
            elif value:
                sections.append(value)

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Text utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        text = html.unescape(text or "").replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        text = html.unescape(text or "").replace("\xa0", " ")
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        deduped: list[str] = []
        for line in lines:
            if deduped and line == deduped[-1]:
                continue
            deduped.append(line)
        return "\n".join(deduped)
