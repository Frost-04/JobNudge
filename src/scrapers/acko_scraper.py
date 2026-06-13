from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class AckoScraper(BaseScraper):
    """
    Scraper for ACKO careers.

    ACKO uses a Kula.ai-powered career page (careers.kula.ai/acko) built
    with Chakra UI (React/Emotion). The listing is a client-rendered SPA
    with a department filter dropdown.

    We navigate directly to the Kula listing URL for simplicity (avoiding
    the outer ACKO page that embeds it in an iframe).

    Expected listing card structure:

        div.chakra-card
          span.chakra-stack > span         (department)
          div.chakra-stack
            p.chakra-text.css-f8zk62        (job title)
          div.chakra-stack
            div.chakra-stack
              p.chakra-text.css-de2tee      (location)
            div.chakra-stack
              p.chakra-text.css-de2tee      (employment type)
              p.chakra-text.css-de2tee      (work mode)
          a.chakra-link[href*="/acko/"]     (apply link → detail page)

    Expected detail page structure:
        div.css-91q6on                      (full description HTML)
    """

    # ---- Listing page selectors ----
    CARD_SELECTOR = "div.chakra-card"
    TITLE_SELECTOR = "p.chakra-text.css-f8zk62"
    LOCATION_SELECTOR = "p.chakra-text.css-de2tee"
    LINK_SELECTOR = "a.chakra-link[href*='/acko/']"

    # ---- Filter / dropdown selectors ----
    FILTER_BUTTON_SELECTOR = "button.chakra-menu__menu-button"

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = "div.css-91q6on"

    # ------------------------------------------------------------------
    # Main scrape method
    # ------------------------------------------------------------------

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = "https://careers.kula.ai/acko?jobs=true"
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for the Chakra UI SPA to hydrate.
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            await page.wait_for_timeout(5000)

            # Select the "Technology" department filter.
            await self._select_filter(page, "Technology")

            # Wait for job cards to render after filtering.
            try:
                await page.wait_for_selector(self.CARD_SELECTOR, timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)

            soup = await self._get_soup(page)
            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No ACKO job cards found")
                return jobs

            seen_job_ids: set[str] = set()
            seen_urls: set[str] = set()

            for card in cards[:max_jobs]:
                job = self._parse_card(card)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_job_ids:
                    continue

                if job.url in seen_urls:
                    continue

                # Enrich with detail page description.
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
                    job.description = None
                else:
                    try:
                        description = await self._scrape_detail_description(job.url)
                        if description:
                            job = Job(
                                job_id=job.job_id,
                                company=job.company,
                                title=job.title,
                                location=job.location,
                                url=job.url,
                                source_url=source_url,
                                posted_date=job.posted_date,
                                description=description,
                                scraped_at=datetime.now(timezone.utc).isoformat(),
                                extracted_experience_parts="",
                            )
                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich ACKO detail page %s: %s",
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
    # Filter interaction
    # ------------------------------------------------------------------

    async def _select_filter(self, page: Page, department: str) -> None:
        """Open the department filter dropdown and click the given option."""
        try:
            await page.wait_for_selector(
                self.FILTER_BUTTON_SELECTOR, timeout=10000
            )
            # Click the first filter button (department, not location).
            filter_buttons = await page.query_selector_all(
                self.FILTER_BUTTON_SELECTOR
            )
            if not filter_buttons:
                return

            await filter_buttons[0].click()
            await page.wait_for_timeout(2000)

            # Find and click the target department option.
            option = await page.query_selector(
                f"button[role='menuitemradio']:has-text('{department}')"
            )
            if option:
                await option.click()
                self.logger.info("Selected filter: %s", department)
                await page.wait_for_timeout(3000)
            else:
                await page.keyboard.press("Escape")
                self.logger.debug("Filter option '%s' not found", department)
        except Exception as exc:
            self.logger.debug("Filter interaction issue: %s", exc)

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag) -> Job | None:
        link = self._extract_link(card)
        title = self._extract_title(card)
        job_id = self._extract_job_id(link)
        location = self._extract_location(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "ACKO"),
            title=title,
            location=location,
            url=link,
            source_url="https://careers.kula.ai/acko?jobs=true",
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag) -> str:
        el = card.select_one(self.LINK_SELECTOR)
        if not el:
            return ""
        href = el.get("href", "")
        if not href:
            return ""
        # Convert relative href to absolute Kula URL.
        if str(href).startswith("/"):
            return f"https://careers.kula.ai{href}"
        if str(href).startswith("http://") or str(href).startswith("https://"):
            return str(href)
        return f"https://careers.kula.ai/{href}"

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_location(self, card: Tag) -> str:
        """Extract location from the card.

        Location is in p.chakra-text.css-de2tee elements. We skip
        non-location values (employment type like "Full Time", work mode
        like "• On-Site").
        """
        skip_values = {
            "Full Time", "Part Time", "Contract",
            "On-Site", "Remote", "Hybrid",
        }
        els = card.select(self.LOCATION_SELECTOR)
        for el in els:
            text = self._clean_text(el.get_text())
            if text and not text.startswith("•") and text not in skip_values:
                return text
        return ""

    def _extract_job_id(self, url: str) -> str:
        """Extract the job ID from the URL.

        Example:
            https://careers.kula.ai/acko/37587/?jobs=true  →  "37587"
        """
        if not url:
            return ""
        match = re.search(r"/acko/(\d+)/", url)
        return match.group(1) if match else ""

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

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

    async def _scrape_detail_description(self, job_url: str) -> str:
        """Open the job detail page and extract the full description."""
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(
                job_url, wait_until="domcontentloaded", timeout=15000
            )

            # Wait for the SPA to render the description.
            try:
                await detail_page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass

            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_DESCRIPTION_SELECTOR, timeout=10000
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)
            return self._extract_description(soup)

        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        container = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)

        if not container:
            return ""

        # Remove non-description elements.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        lines = [
            " ".join(line.split())
            for line in text.splitlines()
            if line.strip()
        ]
        return "\n".join(lines)
