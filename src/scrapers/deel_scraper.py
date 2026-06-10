from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class DeelScraper(BaseScraper):
    """
    Scraper for Deel careers page (Next.js / MUI SPA).

    The listing page at ``deel.com/careers`` uses URL query parameters
    (``?location=india&department=engineering``) for filtering — no
    dynamic filter interaction is needed.  Jobs are grouped into
    accordion sections by department; the ``department`` param ensures
    the Engineering section is expanded.

    Each card is an ``<a>`` linking to ``/careers/job/?ats_id={uuid}``.
    The ``ats_id`` query parameter serves as the job ID.  Cards carry
    rich metadata (title, category, location, employment type, salary)
    as child elements.

    Detail pages have the full job description inside a
    ``div.ordered-list.bullet-list`` element.

    Expected listing card structure:

        <li>
          <a href="/careers/job/?ats_id=ef447ade-...">
            <span class="sr-only">Apply to Full Stack Developer</span>
            <p class="... line-clamp-2 ... font-semibold">TITLE</p>
            <div class="flex flex-wrap items-center gap-x-nano ...">
              <span>... text-content-accessory">CATEGORY</span>
              <span>... text-content-accessory">LOCATION</span>
              <span>... text-content-accessory">TYPE</span>
              <span>... text-content-accessory">SALARY</span>
            </div>
          </a>
        </li>

    Expected detail page structure:

        <div class="min-w-0 w-full">
          <div class="ordered-list bullet-list">
            <p><strong>...</strong></p>
            ... RICH HTML DESCRIPTION
          </div>
        </div>
    """

    # ---- Card selectors ----
    CARD_SELECTOR = "a[href*='ats_id=']"
    META_SELECTOR = "span[class*='text-content-accessory']"
    JOB_CARD_SELECTORS = [
        "a[href*='ats_id=']",
        "a[href*='/careers/job/']",
    ]

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = "div.min-w-0.w-full div.ordered-list.bullet-list"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)

            # Scroll down to trigger lazy-loaded accordion content, then
            # back up and wait for cards to render.
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(3000)

            # Try clicking the Engineering accordion if it's collapsed.
            try:
                eng_btn = page.locator("button[id*='career-dept-engineering']")
                if await eng_btn.is_visible(timeout=3000):
                    expanded = await eng_btn.get_attribute("aria-expanded")
                    if expanded != "true":
                        await eng_btn.click(timeout=5000)
                        await page.wait_for_timeout(3000)
            except Exception:
                pass

            # Wait for cards to appear (Next.js SPA — content loads dynamically).
            await self._wait_for_cards(page)

            # Extra settle time for dynamic rendering.
            await page.wait_for_timeout(2000)

            soup = await self._get_soup(page)
            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                cards = soup.select("a[href*='/careers/job/']")

            if not cards:
                self.logger.warning("No Deel job cards found.")
                return jobs

            for card in cards[:max_jobs]:
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_ids:
                    continue
                if job.url in seen_urls:
                    continue

                # Enrich from detail page (non-excluded roles only).
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
                            "Failed to enrich Deel detail page %s: %s",
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
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = card.get("href", "")
        if not link:
            return None

        # Make relative URLs absolute.
        if link.startswith("/"):
            link = f"https://www.deel.com{link}"

        title = self._extract_title(card)
        location, category, job_type = self._extract_meta(card)
        job_id = self._extract_job_id(link)

        if not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Deel"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date="",
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_title(self, card: Tag) -> str:
        """Extract job title from the semibold paragraph."""
        # Primary: the <p> with line-clamp-2 and font-semibold
        el = card.select_one(
            "p.mb-quark.line-clamp-2, p.line-clamp-2"
        )
        if el:
            title = self._clean_text(el.get_text())
            if title:
                return title

        # Fallback: any <p> with font-semibold class
        for p in card.select("p"):
            classes = p.get("class", [])
            if isinstance(classes, list):
                if "font-semibold" in classes:
                    return self._clean_text(p.get_text())
            elif isinstance(classes, str):
                if "font-semibold" in classes:
                    return self._clean_text(p.get_text())

        # Last resort: the sr-only span text (minus "Apply to " prefix)
        sr_span = card.select_one("span.sr-only")
        if sr_span:
            text = sr_span.get_text(strip=True)
            if text.startswith("Apply to "):
                text = text[len("Apply to "):]
            if text:
                return text

        return ""

    def _extract_meta(self, card: Tag) -> tuple[str, str, str]:
        """
        Extract location, category, and job type from the meta spans.

        Returns ``(location, category, job_type)`` tuple.

        The meta spans appear in order: category, location, job type, salary.
        We pick the first 3 meaningful ones.
        """
        spans = card.select(self.META_SELECTOR)
        texts: list[str] = []
        for span in spans:
            text = self._clean_text(span.get_text())
            if text and text not in ("•",):
                texts.append(text)

        # First is category (e.g., "Engineering"), second is location,
        # third is employment type (e.g., "Full-time").
        category = texts[0] if len(texts) > 0 else ""
        location = texts[1] if len(texts) > 1 else ""
        job_type = texts[2] if len(texts) > 2 else ""

        # If the "location" looks like a job type (Full-time, Part-time, etc.)
        # shift everything.
        job_type_keywords = {"full-time", "part-time", "contract", "intern", "internship"}
        if location.lower() in job_type_keywords:
            location, job_type = "", location

        return (location, category, job_type)

    def _extract_job_id(self, url: str) -> str:
        """
        Extract the ``ats_id`` query parameter from the job URL.

        Example: ``/careers/job/?ats_id=ef447ade-39c7-...`` → ``ef447ade-39c7-...``
        """
        if not url:
            return ""

        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            ats_ids = params.get("ats_id", [])
            if ats_ids:
                return ats_ids[0]
        except Exception:
            pass

        return ""

    # ------------------------------------------------------------------
    # Wait helpers
    # ------------------------------------------------------------------

    async def _wait_for_cards(self, page: Page) -> None:
        """Wait for at least one card selector to match on the page."""
        for selector in self.JOB_CARD_SELECTORS:
            try:
                await page.wait_for_selector(selector, timeout=15000)
                return
            except Exception:
                continue

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

    async def _scrape_detail_page(self, job_url: str) -> dict[str, str]:
        """
        Open a job detail page and extract the full HTML description.
        """
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)

            await detail_page.goto(
                job_url,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await detail_page.wait_for_timeout(4000)

            description = ""
            try:
                desc_el = detail_page.locator(
                    self.DETAIL_DESCRIPTION_SELECTOR
                ).first
                await desc_el.wait_for(state="visible", timeout=15000)

                # Get the inner HTML of the description container.
                html_content = await desc_el.evaluate("el => el.innerHTML")
                description = self._clean_text(html_content)
            except Exception:
                # Fallback: any ordered-list bullet-list.
                try:
                    fallback = detail_page.locator(
                        "div.ordered-list.bullet-list"
                    ).first
                    if await fallback.is_visible(timeout=3000):
                        html_content = await fallback.evaluate("el => el.innerHTML")
                        description = self._clean_text(html_content)
                except Exception:
                    pass

            return {"description": description}

        finally:
            try:
                await detail_page.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        return " ".join(text.split()).strip()
