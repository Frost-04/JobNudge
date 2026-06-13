from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class CleverTapScraper(BaseScraper):
    """
    Scraper for CleverTap careers.

    The job board is hosted at ``careers.kula.ai/clevertap`` (Kula.ai ATS) and
    embedded on the main ``clevertap.com/current-openings/`` page via an iframe.
    We scrape the direct Kula.ai URL to avoid iframe complications.

    The listing page uses Chakra UI accordion panels for each department.
    The Engineering department must be expanded (click the toggle button) to
    reveal job cards.  A location "All locations" dropdown is also available
    but not required since all India jobs are shown by default.

    Listing card structure (inside expanded accordion panel):

        div.chakra-accordion__panel
          div.chakra-stack                          (wrapper)
            div.chakra-stack                        (job card)
              div.chakra-stack
                p.chakra-text                        (job title)
                div
                  p.chakra-text                      (meta: "Engineering \u2022 Mumbai, Maharashtra, India \u2022 Full Time \u2022 On-Site")
              a.chakra-link[href="/clevertap/12616/"]  (Apply Now link)

    Detail page structure (``careers.kula.ai/clevertap/{id}``):

        div[role="tabpanel"]
          div
            span > p, p.dir-auto, p.dir-ltr          (job description paragraphs)
    """

    # ---- Listing page selectors ----
    ENGINEERING_BUTTON_SELECTOR = "button.chakra-accordion__button"
    CARD_LINK_SELECTOR = "div.chakra-accordion__panel a.chakra-link"

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = "div[role='tabpanel']"

    # ---- Base domain for URL resolution (Kula.ai ATS) ----
    BASE_DOMAIN = "https://careers.kula.ai"

    # ------------------------------------------------------------------
    # Main scrape method
    # ------------------------------------------------------------------

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for the page to settle.
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)

            # ---- Expand the Engineering accordion ----
            await self._expand_engineering_accordion(page)

            # Wait for the accordion panel to render with job cards.
            try:
                await page.wait_for_selector(self.CARD_LINK_SELECTOR, timeout=15000)
            except Exception:
                pass

            await page.wait_for_timeout(2000)

            soup = await self._get_soup(page)

            # Get job links from ONLY the Engineering accordion panel.
            cards = self._get_engineering_cards(soup)

            if not cards:
                self.logger.warning("No CleverTap job cards found")
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
                            "Failed to enrich CleverTap detail page %s: %s",
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
    # Accordion interaction
    # ------------------------------------------------------------------

    async def _expand_engineering_accordion(self, page: Page) -> None:
        """Click the Engineering accordion button to expand it if not already open."""
        try:
            eng_button = page.locator(
                self.ENGINEERING_BUTTON_SELECTOR,
                has_text="Engineering",
            )
            if await eng_button.count() > 0:
                # Check if already expanded.
                expanded = await eng_button.get_attribute("aria-expanded")
                if expanded == "true":
                    self.logger.info("Engineering accordion already expanded")
                    return

                await eng_button.click()
                self.logger.info("Clicked Engineering accordion button")
                await page.wait_for_timeout(2000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
            else:
                self.logger.warning("Engineering accordion button not found")
        except Exception as exc:
            self.logger.warning(
                "Failed to expand Engineering accordion: %s", exc
            )

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _get_engineering_cards(soup) -> list[Tag]:
        """Extract only job links from the Engineering accordion panel."""
        for item in soup.select("div.chakra-accordion__item"):
            btn = item.select_one("button.chakra-accordion__button")
            if btn and "Engineering" in btn.get_text():
                panel = item.select_one("div.chakra-accordion__panel")
                if panel:
                    return panel.select("a.chakra-link")
        return []

    def _parse_card(self, card_link: Tag) -> Job | None:
        """Parse a single job card from the accordion panel.

        *card_link* is the ``a.chakra-link`` element representing
        the "Apply Now" link inside a job card.
        """
        link = self._extract_link(card_link)
        title = self._extract_title(card_link)
        job_id = self._extract_job_id(card_link)
        location = self._extract_location(card_link)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "CleverTap"),
            title=title,
            location=location,
            url=link,
            source_url=self.company_config.get("url", ""),
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag) -> str:
        href = card.get("href", "")
        if not href:
            return ""
        href = str(href)
        if href.startswith("http://") or href.startswith("https://"):
            return href
        if href.startswith("/"):
            return f"{self.BASE_DOMAIN}{href}"
        return f"{self.BASE_DOMAIN}/{href}"

    def _extract_title(self, card: Tag) -> str:
        """Get the job title from the card.

        The title is the first ``p.chakra-text`` found by walking up
        from the ``a.chakra-link`` to its parent stack.
        """
        parent = card.parent
        if parent:
            title_el = parent.select_one("p.chakra-text")
            if title_el:
                return self._clean_text(title_el.get_text())
        return ""

    def _extract_location(self, card: Tag) -> str:
        """Extract location from the meta text line.

        The meta text has the format::

            Engineering \u2022 Mumbai, Maharashtra, India \u2022 Full Time \u2022 On-Site

        We extract the second ``\u2022``-separated segment as the location.
        """
        parent = card.parent
        if parent:
            for p in parent.select("p.chakra-text"):
                text = p.get_text()
                if "\u2022" in text:
                    parts = [part.strip() for part in text.split("\u2022")]
                    # parts[0] = department, parts[1] = location, parts[2] = type, parts[3] = work-model
                    if len(parts) >= 2:
                        loc = parts[1].strip()
                        if loc:
                            return loc
                    return text
        return ""

    def _extract_job_id(self, card: Tag) -> str:
        """Extract job ID from the URL path.

        URL pattern: ``/clevertap/12616/``
        """
        link = self._extract_link(card)
        if not link:
            return ""
        match = re.search(r"/clevertap/(\d+)", link)
        if match:
            return match.group(1)
        return ""

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

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

            # Wait for the page to settle.
            try:
                await detail_page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass

            await detail_page.wait_for_timeout(2000)

            soup = await self._get_soup(detail_page)
            return self._extract_description(soup)

        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        """Extract description text from the job detail page.

        Kula.ai detail pages use a ``div[role='tabpanel']`` containing
        the full job description in structured HTML (``p`` tags).
        """
        # Try the tabpanel container first.
        container = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)
        if container:
            for unwanted in container.select("script, style, noscript"):
                unwanted.decompose()
            text = container.get_text(separator="\n")
            cleaned = self._clean_multiline_text(text)
            if len(cleaned) > 100:
                return cleaned

        # Broad fallback: collect substantial <p> tag text.
        all_p = soup.select("p")
        texts = [
            self._clean_text(p.get_text())
            for p in all_p
            if len(p.get_text(strip=True)) > 30
        ]
        if texts:
            return "\n".join(texts)

        return ""
