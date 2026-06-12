from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class IndeedScraper(BaseScraper):
    """
    Scraper for Indeed company job pages.

    Expected listing URL pattern:
        https://in.indeed.com/cmp/{Company}/jobs?q=...&start=0

    Cards are server-rendered with data-jk attributes:

        div.cardOutline (class includes "job_{jk}")
          h3.jobTitle > a.jcs-JobTitle[data-jk] > span[id^="jobTitle-"]
          div[data-testid="text-location"]
          div.salary-snippet-container span

    Detail pages use the viewjob endpoint:
        https://in.indeed.com/viewjob?jk={jk}
            div#jobDescriptionText
            div[data-testid="jobDetailDescription"]

    Pagination is driven by the ``start`` query parameter (0, 10, 20, …).
    """

    # ── Card selectors ────────────────────────────────────────────────
    CARD_SELECTOR = 'div.cardOutline[class*="job_"]'
    TITLE_SELECTOR = 'h3.jobTitle a.jcs-JobTitle span'
    LINK_SELECTOR = 'a[data-jk]'
    LOCATION_SELECTOR = 'div[data-testid="text-location"]'
    SALARY_SELECTOR = 'div.salary-snippet-container span'

    # ── Detail page selectors ─────────────────────────────────────────
    DETAIL_DESCRIPTION_SELECTORS = [
        'div#jobDescriptionText',
        'div[data-testid="jobDetailDescription"]',
    ]

    # ── Pagination ────────────────────────────────────────────────────
    PAGE_SIZE = 10
    MAX_PAGES = 5

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []
        seen_jk: set[str] = set()

        try:
            for page_num in range(self.MAX_PAGES):
                start_val = page_num * self.PAGE_SIZE
                page_url = self._build_page_url(source_url, start_val)

                self.logger.info("Indeed page %d: %s", page_num + 1, page_url)

                try:
                    # Indeed keeps persistent connections — never use networkidle.
                    await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)

                    # Give the SPA time to hydrate and render job cards
                    await page.wait_for_timeout(5000)

                    # Verify cards are present via JS (more reliable than wait_for_selector on Indeed)
                    card_count = await page.evaluate(
                        "() => document.querySelectorAll('a[data-jk]').length"
                    )
                    if card_count == 0:
                        self.logger.warning(
                            "Indeed page %d: no a[data-jk] elements found after load", page_num + 1
                        )
                        # Try fallback link scan before giving up
                        fallback = await self._fallback_links(page, source_url, max_jobs - len(jobs))
                        jobs.extend(fallback)
                        break

                except Exception:
                    self.logger.warning("Failed to load Indeed page %d", page_num + 1)
                    break

                soup = await self._get_soup(page)
                cards = soup.select(self.CARD_SELECTOR)

                if not cards:
                    self.logger.info("No more Indeed cards (end of pagination).")
                    break

                page_had_new_jobs = False

                for card in cards:
                    if len(jobs) >= max_jobs:
                        break

                    job = self._parse_card(card, source_url)

                    if not job or not job.url:
                        continue

                    # Deduplicate by jk value
                    jk = self._extract_jk_from_card(card)
                    if jk and jk in seen_jk:
                        continue

                    if jk:
                        seen_jk.add(jk)

                    page_had_new_jobs = True

                    # Skip detail enrichment for senior/staff/principal roles
                    if self._should_exclude(job.title):
                        self.logger.debug("Skipping detail enrichment for: %s", job.title)
                    else:
                        try:
                            detail_data = await self._scrape_detail_page(jk or job.job_id)
                            detail_desc = detail_data.get("description", "")
                            ref_id = detail_data.get("reference_id", "")

                            if detail_desc or ref_id:
                                final_id = ref_id or job.job_id
                                job = Job(
                                    job_id=final_id,
                                    company=job.company,
                                    title=job.title,
                                    location=job.location,
                                    url=job.url,
                                    source_url=job.source_url,
                                    posted_date=job.posted_date,
                                    description=detail_desc or job.description,
                                    scraped_at=datetime.now(timezone.utc).isoformat(),
                                    extracted_experience_parts="",
                                )

                        except Exception as exc:
                            self.logger.warning(
                                "Failed to enrich Indeed detail for %s: %s",
                                job.title,
                                exc,
                            )

                    jobs.append(job)

                if not page_had_new_jobs:
                    break

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def _build_page_url(self, source_url: str, start_val: int) -> str:
        """Insert or update the ``start`` query parameter."""
        parsed = urlparse(source_url)
        query_parts = parsed.query.split("&") if parsed.query else []

        new_parts = []
        found_start = False
        for part in query_parts:
            if part.startswith("start="):
                new_parts.append(f"start={start_val}")
                found_start = True
            else:
                new_parts.append(part)

        if not found_start:
            new_parts.append(f"start={start_val}")

        new_query = "&".join(new_parts)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{new_query}"

    def _make_viewjob_url(self, jk: str) -> str:
        """Build a viewjob detail URL from a job key."""
        return f"https://in.indeed.com/viewjob?jk={jk}"

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        title = self._extract_title(card)
        link = self._extract_link(card)
        location = self._extract_location(card)
        salary = self._extract_salary(card)
        jk = self._extract_jk_from_card(card)

        if not title:
            return None

        # Build a usable URL even when the raw href is a tracking redirect
        url = link or (self._make_viewjob_url(jk) if jk else "")

        return Job(
            job_id=jk or "",
            company=self.company_config.get("name", "Indeed"),
            title=title,
            location=location,
            url=url,
            source_url=source_url,
            posted_date=None,
            description=salary or None,  # store salary as initial description
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        if el:
            return self._clean_text(el.get_text())
        # Fallback: title from aria-label on the anchor
        link = card.select_one(self.LINK_SELECTOR)
        if link:
            aria = link.get("aria-label", "")
            if aria:
                # "full details of Site Reliability Engineer"
                return self._clean_text(aria.replace("full details of", ""))
        return ""

    def _extract_link(self, card: Tag) -> str:
        el = card.select_one(self.LINK_SELECTOR)
        if not el:
            return ""
        href = str(el.get("href", "")).strip()
        if not href:
            return ""
        # Indeed tracking links are already absolute
        if href.startswith("http"):
            return html.unescape(href)
        return make_absolute_url("https://in.indeed.com", href)

    def _extract_location(self, card: Tag) -> str:
        el = card.select_one(self.LOCATION_SELECTOR)
        if el:
            return self._clean_text(el.get_text())
        return ""

    def _extract_salary(self, card: Tag) -> str:
        el = card.select_one(self.SALARY_SELECTOR)
        if el:
            return self._clean_text(el.get_text())
        return ""

    def _extract_jk_from_card(self, card: Tag) -> str:
        """Extract the Indeed job key (jk) from a card."""
        # Primary: data-jk attribute on the anchor
        link = card.select_one(self.LINK_SELECTOR)
        if link:
            jk = link.get("data-jk", "")
            if jk:
                return str(jk).strip()

        # Fallback: class list contains "job_{jk}"
        classes = card.get("class", [])
        for cls in classes:
            match = re.match(r"job_([a-f0-9]+)", cls)
            if match:
                return match.group(1)

        return ""

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def _get_detail_page(self) -> Page:
        if self.context:
            try:
                return await self.context.new_page()
            except Exception:
                self.logger.debug("Stale browser context; recreating.")
                await self.close_browser()
        return await self.new_page()

    async def _scrape_detail_page(self, jk: str) -> dict[str, str]:
        """Open the viewjob page and extract description + reference ID."""
        if not jk:
            return {}

        detail_page = await self._get_detail_page()
        try:
            detail_page.set_default_timeout(10000)
            viewjob_url = self._make_viewjob_url(jk)

            await detail_page.goto(viewjob_url, wait_until="domcontentloaded", timeout=30000)

            # Give SPA time to render
            await detail_page.wait_for_timeout(3000)

            # Verify description loaded
            desc_exists = await detail_page.evaluate(
                "() => !!(document.querySelector('#jobDescriptionText') || document.querySelector('[data-testid=\"jobDetailDescription\"]'))"
            )
            if not desc_exists:
                self.logger.debug("Indeed detail: no description element found for %s", jk)

            soup = await self._get_soup(detail_page)
            description = self._extract_description(soup)
            ref_id = self._extract_reference_id(soup)

            return {
                "description": description,
                "reference_id": ref_id,
            }

        except Exception:
            return {}

        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        for selector in self.DETAIL_DESCRIPTION_SELECTORS:
            container = soup.select_one(selector)
            if container:
                # Remove scripts/styles
                for unwanted in container.select("script, style, noscript"):
                    unwanted.decompose()
                text = container.get_text(separator="\n")
                return self._clean_multiline_text(text)
        return ""

    def _extract_reference_id(self, soup) -> str:
        """Extract Reference ID from the description text (e.g. 'Reference ID: 46741')."""
        for selector in self.DETAIL_DESCRIPTION_SELECTORS:
            container = soup.select_one(selector)
            if container:
                text = container.get_text()
                match = re.search(r"Reference\s+ID:\s*(\d+)", text, re.IGNORECASE)
                if match:
                    return match.group(1)
        return ""

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""
        text = html.unescape(text)
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _clean_multiline_text(self, text: str) -> str:
        if not text:
            return ""
        text = html.unescape(text)
        text = text.replace("\xa0", " ")
        # Normalise whitespace but preserve paragraph breaks
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _dedupe_preserve_order(self, items: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for item in items:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    # ------------------------------------------------------------------
    # Fallback link scan
    # ------------------------------------------------------------------

    async def _fallback_links(
        self, page: Page, source_url: str, max_jobs: int
    ) -> list[Job]:
        """Extract jobs from raw anchor tags when card selectors fail."""
        soup = await self._get_soup(page)
        anchors = soup.select('a[data-jk]')

        jobs: list[Job] = []
        seen: set[str] = set()

        for a in anchors[:max_jobs]:
            jk = str(a.get("data-jk", "")).strip()
            if not jk or jk in seen:
                continue
            seen.add(jk)

            title = self._clean_text(a.get_text())
            if not title:
                aria = a.get("aria-label", "")
                title = self._clean_text(aria.replace("full details of", ""))

            if not title:
                continue

            href = str(a.get("href", ""))
            url = href if href.startswith("http") else self._make_viewjob_url(jk)

            jobs.append(Job(
                job_id=jk,
                company=self.company_config.get("name", "Indeed"),
                title=title,
                location="",
                url=url,
                source_url=source_url,
                posted_date=None,
                description=None,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                extracted_experience_parts="",
            ))

        return jobs
