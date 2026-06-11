from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class HarnessScraper(BaseScraper):
    """
    Scraper for Harness careers page (Greenhouse embedded widget).

    Harness uses a custom page at harness.io/company/jobs that embeds
    Greenhouse's job widget.  The page has two <select> dropdowns for
    department and location, a "View More Positions" button to load
    additional cards, and simple card markup for each job.

    Flow:
        1. Navigate to the URL.
        2. Select "Engineering" from the department dropdown.
        3. Select "Bangalore, India" from the location dropdown.
        4. Click "View More Positions" up to 5 times (or until hidden).
        5. Parse visible job cards and enrich via the apply page.

    Filter markup:

        <div class="gh-filter">
          <select id="all_departments">
            <option value="4049679007">Engineering (25)</option>
          </select>
          <select id="all_locations">
            <option value="4021657007">Bangalore, India (26)</option>
          </select>
        </div>

    Job card markup:

        <div class="card">
          <div>
            <div class="department">Engineering, Software Development</div>
            <div class="title">Staff Software Engineer - Data Platform</div>
            <div class="location">Bengaluru, Karnataka, India</div>
          </div>
          <a href="https://www.harness.io/company/jobs/apply?gh_jid=4917794007">
            <button class="applybtn">Apply</button>
          </a>
        </div>

    Detail / apply page:
        The apply URL is /company/jobs/apply?gh_jid=XXXXXXXXXX.
        Description content lives in the Greenhouse application form.
    """

    # ---- Filter selectors ----
    DEPARTMENT_DROPDOWN = "select#all_departments"
    LOCATION_DROPDOWN = "select#all_locations"

    # Dropdown option values
    DEPARTMENT_VALUE = "4049679007"    # Engineering
    LOCATION_VALUE = "4021657007"      # Bangalore, India

    # ---- Load-more button ----
    VIEW_MORE_BUTTON = "button.gh-view-more"
    MAX_VIEW_MORE_CLICKS = 5

    # ---- Card selectors ----
    CARD_SELECTOR = "div.card"
    TITLE_SELECTOR = "div.title"
    DEPARTMENT_SELECTOR = "div.department"
    LOCATION_SELECTOR = "div.location"
    LINK_SELECTOR = "a[href]"

    # ---- Initial load wait selectors ----
    JOB_CARD_SELECTORS = [
        "div.card",
        "div.gh-main",
        "div.gh-filter",
    ]

    # ---- Detail page selectors ----
    DETAIL_SELECTORS = [
        "div#content",
        "div.job-description",
        "div.section-wrapper",
        "div.app-title",
        "h1",
    ]

    BASE_URL = "https://www.harness.io"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            # ---- Step 1: Navigate to the jobs page ----
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            selector = await self._wait_for_any_selector(page, self.JOB_CARD_SELECTORS)
            if not selector:
                self.logger.warning("Harness: could not detect job cards on page.")
                return jobs

            # ---- Step 2: Apply department filter ----
            await self._select_dropdown_option(page, self.DEPARTMENT_DROPDOWN, self.DEPARTMENT_VALUE, "Engineering")

            # ---- Step 3: Apply location filter ----
            await self._select_dropdown_option(page, self.LOCATION_DROPDOWN, self.LOCATION_VALUE, "Bangalore, India")

            # Give the widget time to filter
            await page.wait_for_timeout(2000)

            # ---- Step 4: Click "View More Positions" until exhausted ----
            for _ in range(self.MAX_VIEW_MORE_CLICKS):
                btn = page.locator(self.VIEW_MORE_BUTTON)
                if not await btn.count():
                    break
                try:
                    is_visible = await btn.is_visible()
                    if not is_visible:
                        break
                    await btn.click()
                    await page.wait_for_timeout(1500)
                except Exception:
                    break

            # Final settle
            await page.wait_for_timeout(1000)

            # ---- Step 5: Parse cards ----
            soup = await self._get_soup(page)
            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("Harness: no job cards found after filtering.")
                return jobs

            seen_ids: set[str] = set()
            seen_urls: set[str] = set()

            for card in cards[:max_jobs]:
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_ids:
                    continue
                if job.url in seen_urls:
                    continue

                # ---- Step 6: Enrich via detail page ----
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
                else:
                    try:
                        detail_data = await self._scrape_detail_page(job.url)
                        description = detail_data.get("description", "")

                        if description:
                            job = Job(
                                job_id=job.job_id,
                                company=job.company,
                                title=job.title,
                                location=job.location,
                                url=job.url,
                                source_url=job.source_url,
                                posted_date=job.posted_date,
                                description=description,
                                scraped_at=datetime.now(timezone.utc).isoformat(),
                                extracted_experience_parts="",
                            )
                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich Harness job detail %s: %s",
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
    # Dropdown interaction
    # ------------------------------------------------------------------

    async def _select_dropdown_option(
        self, page: Page, dropdown_selector: str, value: str, label: str
    ) -> None:
        """Select an option in a <select> dropdown by its value attribute."""
        dropdown = page.locator(dropdown_selector)
        if not await dropdown.count():
            self.logger.warning("Harness dropdown '%s' not found.", dropdown_selector)
            return

        try:
            await dropdown.select_option(value=value)
            await page.wait_for_timeout(1500)
        except Exception as exc:
            self.logger.warning(
                "Harness: failed to select '%s' in '%s': %s", label, dropdown_selector, exc
            )

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        title = self._extract_title(card)
        link = self._extract_link(card)
        job_id = self._extract_job_id(link)
        location = self._extract_location(card)
        department = self._extract_department(card)

        if not title and not link:
            return None

        # Use department as fallback title if title is empty
        if not title:
            title = department or "Unknown"

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Harness"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=department or None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        if el:
            return self._clean_text(el.get_text())
        return ""

    def _extract_department(self, card: Tag) -> str:
        el = card.select_one(self.DEPARTMENT_SELECTOR)
        if el:
            return self._clean_text(el.get_text())
        return ""

    def _extract_location(self, card: Tag) -> str:
        el = card.select_one(self.LOCATION_SELECTOR)
        if el:
            return self._clean_text(el.get_text())
        return ""

    def _extract_link(self, card: Tag) -> str:
        el = card.select_one(self.LINK_SELECTOR)
        if not el:
            return ""

        href = el.get("href")
        if not href:
            return ""

        href = str(href).strip()

        if href.startswith("http"):
            return href

        if href.startswith("/"):
            return f"{self.BASE_URL}{href}"

        return f"{self.BASE_URL}/{href}"

    def _extract_job_id(self, link: str) -> str:
        """
        Extract the Greenhouse job ID from the ``gh_jid`` query parameter.

        URL format: /company/jobs/apply?gh_jid=4917794007
        """
        if not link:
            return "0"

        try:
            parsed = urlparse(link)
            params = parse_qs(parsed.query)
            jid_list = params.get("gh_jid", [])
            if jid_list and jid_list[0]:
                return jid_list[0]
        except Exception:
            pass

        return extract_job_id(link)

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def _get_detail_page(self) -> Page:
        """Return a new page, creating a fresh context if needed."""
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
        Navigate to the Greenhouse apply page and extract the job description.
        """
        detail_page = await self._get_detail_page()
        detail_data: dict[str, str] = {}

        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)

            await self._wait_for_any_selector(detail_page, list(self.DETAIL_SELECTORS))

            soup = await self._get_soup(detail_page)

            description = self._extract_description(soup)
            if description:
                detail_data["description"] = description

            return detail_data

        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        """
        Try multiple selectors to find the job description on the apply page.
        Greenhouse apply pages typically have the description inside #content
        or a dedicated description container.
        """
        for selector in self.DETAIL_SELECTORS:
            container = soup.select_one(selector)
            if container:
                # Remove script/style/noscript tags
                for unwanted in container.select("script, style, noscript, nav, footer"):
                    unwanted.decompose()

                text = container.get_text(separator="\n")
                text = self._clean_multiline_text(text)

                # If we got meaningful content (more than just a heading), return it
                if len(text) > 50:
                    return text

        # Fallback: grab the whole body's text content
        body = soup.select_one("body")
        if body:
            for unwanted in body.select("script, style, noscript, nav, footer, header"):
                unwanted.decompose()
            text = body.get_text(separator="\n")
            return self._clean_multiline_text(text)

        return ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _wait_for_any_selector(self, page: Page, selectors: list[str]) -> str | None:
        timeout_ms = self._to_ms(
            self.settings.get("run", {}).get("page_load_timeout_seconds"),
            45000,
        )

        for selector in selectors:
            try:
                await page.wait_for_selector(selector, timeout=timeout_ms)
                return selector
            except Exception:
                continue

        return None

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _clean_multiline_text(self, text: str) -> str:
        if not text:
            return ""
        text = html.unescape(text)
        lines = [line.strip() for line in text.split("\n")]
        lines = [line for line in lines if line]
        return "\n".join(lines)
