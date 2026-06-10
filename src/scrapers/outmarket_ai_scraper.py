from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class OutmarketAIScraper(BaseScraper):
    """
    Scraper for Outmarket AI careers page (Framer site).

    The listing page at ``outmarket.ai/careers`` is a Framer-built site.
    Cards are rendered as images (CSS backgrounds) with NO text content
    in the DOM — only ``<a href="./careers/{slug}">`` links are available.

    All job data (title, description) comes from detail page enrichment.

    Expected listing card structure:

        a[href^="./careers/"]
          (empty children — titles are CSS background images)

    Expected detail page structure:

        div[data-framer-name="Description"]
          div[data-framer-component-type="RichTextContainer"]
            h4, p, ul, li   (rich-text job description)
    """

    # ---- Card / listing selectors ----
    CARD_SELECTOR = "a[href^='./careers/']"
    JOB_CARD_SELECTORS = [
        "a[href^='./careers/']",
        "div[data-framer-name]",
    ]

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = (
        "div[data-framer-name='Description'] "
        "div[data-framer-component-type='RichTextContainer']"
    )
    DETAIL_DESCRIPTION_FALLBACK = (
        "div[data-framer-component-type='RichTextContainer']"
    )
    DETAIL_TITLE_SELECTORS = [
        "h1",
        "h2",
        "div[data-framer-name='Title']",
        "[data-framer-name='Title']",
    ]

    DETAIL_WAIT_SELECTORS = [
        "div[data-framer-name='Description']",
        "div[data-framer-component-type='RichTextContainer']",
        "article",
        "h1",
    ]

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Framer sites are client-rendered; give them extra time.
            await page.wait_for_timeout(3000)

            try:
                await page.wait_for_selector(self.CARD_SELECTOR, timeout=15000)
            except Exception:
                pass

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No Outmarket AI job cards found.")
                return jobs

            seen_slugs: set[str] = set()

            for card in cards[:max_jobs]:
                href = card.get("href")

                if not href:
                    continue

                # Resolve relative URL to absolute.
                job_url = urljoin(source_url, str(href))
                slug = self._extract_slug(str(href))

                if not slug:
                    continue

                if slug in seen_slugs:
                    continue

                seen_slugs.add(slug)

                # Derive a fallback title from the URL slug.
                fallback_title = self._slug_to_title(slug)

                # Enrich from detail page to get the real title + description.
                try:
                    detail_data = await self._scrape_detail_page(job_url)

                    title = detail_data.get("title", "") or fallback_title
                    description = detail_data.get("description", "")

                    if self._should_exclude(title):
                        self.logger.debug(
                            "Skipping detail enrichment for excluded role: %s",
                            title,
                        )
                        description = None

                except Exception as exc:
                    self.logger.warning(
                        "Failed to scrape Outmarket AI detail page %s: %s",
                        job_url,
                        exc,
                    )
                    title = fallback_title
                    description = None

                job = Job(
                    job_id=slug,
                    company=self.company_config.get("name", "Outmarket AI"),
                    title=title,
                    location="India",
                    url=job_url,
                    source_url=source_url,
                    posted_date=None,
                    description=description,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    extracted_experience_parts="",
                )

                jobs.append(job)

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Card parsing (URL-only — no text in cards)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_slug(href: str) -> str:
        """Extract the URL slug from a relative career link.

        Example: './careers/head-of-revenue-operations-gtm-ai-architect'
          → 'head-of-revenue-operations-gtm-ai-architect'
        """
        parts = href.rstrip("/").rsplit("/", 1)

        if len(parts) == 2:
            return parts[1]

        return ""

    @staticmethod
    def _slug_to_title(slug: str) -> str:
        """Convert a URL slug to a human-readable title.

        Example: 'head-of-revenue-operations-gtm-ai-architect'
          → 'Head Of Revenue Operations Gtm Ai Architect'
        """
        return (
            slug.replace("-", " ")
            .replace("_", " ")
            .title()
        )

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
                    "Shared context unusable; creating a fresh one."
                )
                await self.close_browser()

        return await self.new_page()

    async def _scrape_detail_page(self, job_url: str) -> dict[str, str]:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)

            await detail_page.goto(
                job_url, wait_until="domcontentloaded", timeout=60000
            )

            # Framer needs extra render time.
            await detail_page.wait_for_timeout(2000)

            for sel in self.DETAIL_WAIT_SELECTORS:
                try:
                    await detail_page.wait_for_selector(sel, timeout=10000)
                    break
                except Exception:
                    continue

            soup = await self._get_soup(detail_page)

            title = self._extract_detail_title(soup)
            description = self._extract_description(soup)

            result: dict[str, str] = {}

            if title:
                result["title"] = title

            if description:
                result["description"] = description

            return result

        finally:
            await detail_page.close()

    def _extract_detail_title(self, soup) -> str:
        """Try to find a heading that looks like the job title."""
        for selector in self.DETAIL_TITLE_SELECTORS:
            el = soup.select_one(selector)

            if el:
                text = self._clean_text(el.get_text())

                # Filter out generic headings.
                if (
                    text
                    and text.lower()
                    not in {
                        "careers",
                        "jobs",
                        "open positions",
                        "about outmarket ai",
                        "about the role",
                        "key responsibilities",
                    }
                    and len(text) < 150
                ):
                    return text

        return ""

    def _extract_description(self, soup) -> str:
        # Primary: RichTextContainer inside the Description section.
        container = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)

        # Fallback: any RichTextContainer with substantial content.
        if not container:
            for candidate in soup.select(self.DETAIL_DESCRIPTION_FALLBACK):
                text = candidate.get_text(separator="\n", strip=True)

                # Must have enough content to be a real job description.
                if len(text) > 200:
                    container = candidate
                    break

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

        lines: list[str] = []
        previous_line = ""

        for line in text.splitlines():
            clean_line = " ".join(line.split())

            if not clean_line:
                continue

            if clean_line == previous_line:
                continue

            lines.append(clean_line)
            previous_line = clean_line

        return "\n".join(lines).strip()
