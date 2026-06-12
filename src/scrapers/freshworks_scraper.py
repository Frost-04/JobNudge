from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id


class FreshworksScraper(BaseScraper):
    """Scraper for Freshworks careers via SmartRecruiters platform.

    The search page groups jobs by location in sections:

        section.openings-section.opening--grouped
          header > h3.opening-title          (e.g. "Bengaluru, India")
          ul.opening-jobs
            li.opening-job.job
              a.js-job-ad-link[href]         (link to detail page)
                h4.details-title.job-title   (job title)
                p.job-desc > span            (e.g. "Full-time")

    Some sections have a "Show more jobs" link:
        li.js-more-container > a.js-more

    Detail page:
        div[itemprop="description"]
          section.job-section
            div.wysiwyg                      (description content)

    Unique techniques:
    - Location-filtered scraping: only sections whose heading contains "India"
    - "Show more jobs" click loop (up to 5 clicks) to reveal all jobs
    """

    # ---- Card selectors ----
    SECTION_SELECTOR = "section.openings-section.opening--grouped"
    LOCATION_HEADING_SELECTOR = "h3.opening-title"
    JOB_ITEM_SELECTOR = "li.opening-job.job:not(.js-more-container)"
    SHOW_MORE_SELECTOR = "a.js-more"
    TITLE_SELECTOR = "h4.details-title.job-title"
    LINK_SELECTOR = "a.js-job-ad-link"

    # ---- Detail page selectors ----
    DETAIL_CONTAINER_SELECTOR = 'div[itemprop="description"]'
    DESCRIPTION_SELECTOR = "div.wysiwyg"

    # Job ID pattern: numeric portion after /Freshworks/ in URL
    JOB_ID_PATTERN = re.compile(r"/Freshworks/(\d+)")

    # Max "Show more jobs" clicks per section
    MAX_SHOW_MORE_CLICKS = 5

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for sections to render.
            await page.wait_for_selector(self.SECTION_SELECTOR, timeout=30000)

            # ---- Step 1: Find India sections ----
            soup = await self._get_soup(page)
            sections = soup.select(self.SECTION_SELECTOR)

            india_headings: list[str] = []
            for section in sections:
                heading_el = section.select_one(self.LOCATION_HEADING_SELECTOR)
                if heading_el and "india" in heading_el.get_text(strip=True).lower():
                    india_headings.append(heading_el.get_text(strip=True))
                    self.logger.info(
                        "Found India section: '%s'",
                        heading_el.get_text(strip=True),
                    )

            if not india_headings:
                self.logger.warning("No India location sections found on page.")
                return jobs

            self.logger.info("Found %d India section(s)", len(india_headings))

            # ---- Step 2: Click "Show more jobs" for each India section ----
            all_sections = page.locator(self.SECTION_SELECTOR)
            section_count = await all_sections.count()

            for i in range(section_count):
                section_loc = all_sections.nth(i)
                heading_loc = section_loc.locator(self.LOCATION_HEADING_SELECTOR)
                if await heading_loc.count() == 0:
                    continue
                heading_text = (await heading_loc.first.inner_text()).strip()
                if "india" not in heading_text.lower():
                    continue

                self.logger.info("Expanding 'Show more' in: %s", heading_text)
                click_count = 0
                last_job_count = await section_loc.locator(self.JOB_ITEM_SELECTOR).count()

                while click_count < self.MAX_SHOW_MORE_CLICKS:
                    show_more = section_loc.locator(self.SHOW_MORE_SELECTOR)
                    if await show_more.count() == 0:
                        break
                    try:
                        is_visible = await show_more.first.is_visible(timeout=2000)
                    except Exception:
                        is_visible = False
                    if not is_visible:
                        break

                    await show_more.first.click()
                    click_count += 1
                    await page.wait_for_timeout(1500)

                    current_count = await section_loc.locator(self.JOB_ITEM_SELECTOR).count()
                    self.logger.debug(
                        "  Click #%d: jobs went from %d to %d",
                        click_count, last_job_count, current_count,
                    )
                    if current_count <= last_job_count:
                        break
                    last_job_count = current_count

                self.logger.info(
                    "  Finished expanding: %d jobs visible after %d clicks",
                    last_job_count, click_count,
                )

            # ---- Step 3: Re-parse BS4 soup to get all expanded jobs ----
            soup = await self._get_soup(page)
            sections = soup.select(self.SECTION_SELECTOR)

            seen_job_ids: set[str] = set()
            seen_urls: set[str] = set()

            for section in sections:
                heading_el = section.select_one(self.LOCATION_HEADING_SELECTOR)
                if not heading_el or "india" not in heading_el.get_text(strip=True).lower():
                    continue

                location_name = heading_el.get_text(strip=True)
                job_items = section.select(self.JOB_ITEM_SELECTOR)

                self.logger.info(
                    "Parsing %d jobs from section: %s", len(job_items), location_name,
                )

                for card in job_items:
                    if len(jobs) >= max_jobs:
                        break

                    job = self._parse_card(card, source_url, location_name)
                    if not job:
                        continue

                    if job.job_id and job.job_id in seen_job_ids:
                        continue
                    if job.url in seen_urls:
                        continue

                    # Skip detail enrichment for senior-level roles.
                    if self._should_exclude(job.title):
                        self.logger.debug(
                            "Skipping detail enrichment for: %s", job.title
                        )
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
                                "Failed to enrich Freshworks detail page %s: %s",
                                job.url,
                                exc,
                            )

                    if job.job_id:
                        seen_job_ids.add(job.job_id)
                    seen_urls.add(job.url)
                    jobs.append(job)

                if len(jobs) >= max_jobs:
                    break

            self.logger.info("Total Freshworks jobs scraped: %d", len(jobs))
            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(
        self, card: Tag, source_url: str, location_name: str = ""
    ) -> Job | None:
        link = self._extract_link(card)
        title = self._extract_title(card)
        job_id = self._extract_job_id(link)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Freshworks"),
            title=title,
            location=location_name,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag) -> str:
        link_el = card.select_one(self.LINK_SELECTOR)
        if not link_el:
            return ""
        href = link_el.get("href")
        if not href:
            return ""
        return str(href).strip()

    def _extract_title(self, card: Tag) -> str:
        title_el = card.select_one(self.TITLE_SELECTOR)
        if title_el:
            return self._clean_text(title_el.get_text())
        return ""

    def _extract_job_id(self, link: str) -> str:
        """Extract the numeric SmartRecruiters job ID from the URL.

        URLs look like:
        https://jobs.smartrecruiters.com/Freshworks/744000131815889-senior-...
        The numeric portion after /Freshworks/ is the job ID.
        """
        if link:
            m = self.JOB_ID_PATTERN.search(link)
            if m:
                return m.group(1)
        return extract_job_id(link)

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def _get_detail_page(self) -> Page:
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
            detail_page.set_default_timeout(10000)
            await detail_page.goto(
                job_url, wait_until="domcontentloaded", timeout=60000
            )

            # Wait for description container.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_CONTAINER_SELECTOR, timeout=20000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)
            return self._extract_description(soup)

        finally:
            await detail_page.close()

    def _extract_description(self, soup: BeautifulSoup) -> str:
        """Extract the full job description from the detail page.

        Combines all wysiwyg sections (Company Description, Job Description,
        Qualifications, Additional Information).
        """
        container = soup.select_one(self.DETAIL_CONTAINER_SELECTOR)
        if not container:
            return ""

        parts: list[str] = []
        for wysiwyg in container.select(self.DESCRIPTION_SELECTOR):
            for unwanted in wysiwyg.select("script, style, noscript, button"):
                unwanted.decompose()

            text = wysiwyg.get_text(separator="\n")
            cleaned = self._clean_multiline_text(text)
            if cleaned:
                parts.append(cleaned)

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ").replace("\r", " ").replace("\n", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        if not text:
            return ""
        text = text.replace("\xa0", " ").replace("\r", "")
        text = re.sub(r"\n{3,}", "\n\n", text)
        lines = [line.strip() for line in text.split("\n")]
        text = "\n".join(lines)
        return text.strip()
