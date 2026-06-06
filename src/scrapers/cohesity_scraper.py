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


class CohesityScraper(BaseScraper):
    """
    Scraper for Cohesity Careers job search page.

    Cohesity's job board at cohesity.com/careers/open-positions/ uses a
    JavaScript-powered SPA with client-side filtering.  All job cards are
    present in the initial HTML but are shown/hidden via inline ``display``
    styles when the user interacts with the search box, department dropdown,
    and location dropdown.

    This scraper uses Playwright to:

    1. Type a search keyword ("Software Engineer") and click Search.
    2. Click the "Engineering" department filter.
    3. Click the "India" location filter.
    4. Parse only the visible cards (``style="display: block;"``).
    5. Navigate to each job's detail page for full description extraction.

    Filter section:

        <input id="search">   → keyword search box
        <span class="filter-click">Engineering</span>  → department picker
        <span class="filter-click">India</span>         → location picker

    Job cards:

        <div class="list-card"
             data-department="Engineering"
             data-title="senior software engineer"
             data-country="India"
             data-primarylocation="Bangalore - India (Office)"
             style="display: block;">

          <div class="list-card__content">
            <div class="title">
              <p class="p-xl bold">
                Senior Software Engineer | Bangalore - India (Office)
              </p>
            </div>
            <div class="btn-wrap">
              <div class="button button--style-tertiary-green">
                <a href="/careers/open-positions/?gh_jid=...&type=wd">
                  Learn more
                </a>
              </div>
            </div>
          </div>
        </div>

    Detail page:

        <div class="cmp-career-details__content-details">
          <p>...</p>
          <ul>...</ul>
        </div>

    Job IDs come from the ``gh_jid`` query parameter in the "Learn more" URL.
    """

    # ---- Filter selectors ----
    SEARCH_INPUT = "input#search"
    SEARCH_BUTTON = "button#btn-search"
    DEPARTMENT_FILTER = ".department-box .filters.department"
    DEPARTMENT_DROPDOWN = "#departments"
    DEPARTMENT_CLICK = 'span.filter-click'
    LOCATION_FILTER = ".location-box .filters.location"
    LOCATION_DROPDOWN = "#locations"
    LOCATION_CLICK = 'span.filter-click'

    # ---- Card selectors ----
    CARD_SELECTOR = "div.list-card"
    CARD_CONTENT = "div.list-card__content"
    TITLE_SELECTOR = "p.p-xl.bold"
    LEARN_MORE_SELECTOR = "div.btn-wrap a"

    # ---- Detail page selectors ----
    DETAIL_CONTENT = "div.cmp-career-details__content-details"

    # ---- Fallback selectors ----
    FALLBACK_LINK_SELECTORS = [
        "div.list-card__content a[href]",
        "a[href*='gh_jid']",
    ]

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        # Read optional filter config from companies.yaml
        search_keyword = self.company_config.get("search_keyword", "Software Engineer")
        filter_department = self.company_config.get("filter_department", "Engineering")
        filter_location = self.company_config.get("filter_location", "India")

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # ---- Step 1: Apply search keyword ----
            await self._apply_search(page, search_keyword)

            # ---- Step 2: Apply department filter ----
            await self._apply_department_filter(page, filter_department)

            # ---- Step 3: Apply location filter ----
            await self._apply_location_filter(page, filter_location)

            # ---- Step 4: Wait for filtered results ----
            await page.wait_for_timeout(2000)

            soup = await self._get_soup(page)

            all_cards = soup.select(self.CARD_SELECTOR)
            if not all_cards:
                self.logger.warning("No Cohesity job cards found on page.")
                return jobs

            # Only process visible cards (style="display: block;" or no display:none)
            visible_cards = self._filter_visible_cards(all_cards)

            if not visible_cards:
                self.logger.warning("No visible Cohesity job cards after filtering.")
                return jobs

            seen_ids: set[str] = set()
            seen_urls: set[str] = set()

            for card in visible_cards[:max_jobs]:
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_ids:
                    continue
                if job.url in seen_urls:
                    continue

                # ---- Step 5: Enrich with detail page ----
                try:
                    detail_data = await self._scrape_detail_page(job.url)
                    description = detail_data.get("description", "")

                    job = Job(
                        job_id=job.job_id,
                        company=job.company,
                        title=job.title,
                        location=job.location,
                        url=job.url,
                        source_url=job.source_url,
                        posted_date=job.posted_date,
                        description=description or job.description,
                        scraped_at=datetime.now(timezone.utc).isoformat(),
                        extracted_experience_parts="",
                    )
                except Exception as exc:
                    self.logger.warning(
                        "Failed to enrich Cohesity job detail %s: %s",
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
    # Filter interactions
    # ------------------------------------------------------------------

    async def _apply_search(self, page: Page, keyword: str) -> None:
        """Type a keyword into the search box and click the Search button."""
        search_input = page.locator(self.SEARCH_INPUT)
        if not await search_input.count():
            self.logger.warning("Cohesity search input not found.")
            return

        await search_input.fill(keyword)
        await page.wait_for_timeout(300)

        search_button = page.locator(self.SEARCH_BUTTON)
        if await search_button.count():
            await search_button.click()
            await page.wait_for_timeout(2000)

    async def _apply_department_filter(self, page: Page, department: str) -> None:
        """
        Click the department dropdown trigger, then click the target
        department item inside the dropdown using JavaScript for reliability.
        """
        # Click the filter trigger to open the department dropdown
        filter_click = page.locator(self.DEPARTMENT_FILTER + " " + self.DEPARTMENT_CLICK)
        if await filter_click.count():
            await filter_click.click()
            await page.wait_for_timeout(800)

        # Click the specific department in the dropdown via JS
        dept_item = page.locator(
            f'{self.DEPARTMENT_DROPDOWN} div.departments:text-is("{department}")'
        )
        if await dept_item.count():
            await dept_item.evaluate("element => element.click()")
            await page.wait_for_timeout(2000)
        else:
            self.logger.warning(
                "Cohesity department '%s' not found in dropdown.", department
            )

    async def _apply_location_filter(self, page: Page, location: str) -> None:
        """
        Click the location dropdown trigger, then click the target
        location country item inside the dropdown using JavaScript for reliability.
        """
        # Click the filter trigger to open the location dropdown
        filter_click = page.locator(self.LOCATION_FILTER + " " + self.LOCATION_CLICK)
        if await filter_click.count():
            await filter_click.click()
            await page.wait_for_timeout(800)

        # Click the specific country in the dropdown via JS
        loc_item = page.locator(
            f'{self.LOCATION_DROPDOWN} div.locations.country:text-is("{location}")'
        )
        if await loc_item.count():
            await loc_item.evaluate("element => element.click()")
            await page.wait_for_timeout(2000)
        else:
            self.logger.warning(
                "Cohesity location '%s' not found in dropdown.", location
            )

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _filter_visible_cards(self, cards: list[Tag]) -> list[Tag]:
        """Return only cards that are not hidden via inline display:none."""
        visible: list[Tag] = []
        for card in cards:
            style = card.get("style", "")
            if "display: none" in str(style):
                continue
            if "display:none" in str(style):
                continue
            visible.append(card)
        return visible

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        title = self._extract_title_and_location(card)
        if not title:
            return None

        # Title text format: "Job Title | Location"
        title_text, location_text = self._split_title_and_location(title)

        link = self._extract_link(card, source_url)
        if not link:
            return None

        job_id = self._extract_job_id_from_card(card, link)

        # Fallback location from data attribute if not in title
        if not location_text:
            location_text = str(card.get("data-primarylocation", "")).strip()

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Cohesity"),
            title=title_text,
            location=location_text,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_title_and_location(self, card: Tag) -> str:
        """Extract the raw title text from the card, which includes location."""
        title_el = card.select_one(self.TITLE_SELECTOR)
        if title_el:
            return self._clean_text(title_el.get_text())
        return ""

    def _split_title_and_location(self, raw_title: str) -> tuple[str, str]:
        """
        Cohesity titles are formatted as "Job Title | Location".
        Split on the last '|' to separate the job title from location.
        """
        if " | " in raw_title:
            parts = raw_title.rsplit(" | ", 1)
            return parts[0].strip(), parts[1].strip()
        return raw_title.strip(), ""

    def _extract_link(self, card: Tag, source_url: str) -> str:
        """Extract the 'Learn more' link and convert to absolute URL."""
        link_el = card.select_one(self.LEARN_MORE_SELECTOR)
        if not link_el:
            return ""

        href = link_el.get("href")
        if not href:
            return ""

        return self._make_cohesity_job_url(source_url, str(href))

    def _extract_job_id_from_card(self, card: Tag, link: str) -> str:
        """
        Extract job ID from the 'gh_jid' query parameter in the URL.

        URL format: /careers/open-positions/?gh_jid=734a05d91a4010011c59bbf4f7c00000&type=wd
        """
        if not link:
            # Try data attributes on the card
            for attr in ("data-job-id", "data-gh-jid", "job-id"):
                val = card.get(attr)
                if val:
                    return str(val).strip()
            return "0"

        return self._extract_gh_jid(link)

    def _extract_gh_jid(self, url: str) -> str:
        """Extract the gh_jid query parameter from a Cohesity job URL."""
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            jid_list = params.get("gh_jid", [])
            if jid_list and jid_list[0]:
                return jid_list[0]
        except Exception:
            pass
        return extract_job_id(url)

    # ------------------------------------------------------------------
    # URL construction
    # ------------------------------------------------------------------

    def _make_cohesity_job_url(self, source_url: str, href: str) -> str:
        """Build an absolute job URL from a relative href."""
        if href.startswith("http"):
            return href

        parsed = urlparse(source_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        return base + href

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
        Navigate to the job detail page and extract the description.
        """
        detail_page = await self._get_detail_page()
        detail_data: dict[str, str] = {}

        try:
            detail_page.set_default_timeout(10000)

            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for the detail content to load
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_CONTENT,
                    timeout=15000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            detail_container = soup.select_one(self.DETAIL_CONTENT)
            if detail_container:
                description = self._extract_description(detail_container)
                if description:
                    detail_data["description"] = description

            return detail_data

        finally:
            await detail_page.close()

    def _extract_description(self, container: Tag) -> str:
        """
        Extract clean description text from the detail content div.

        Removes script/style tags, preserves paragraph and list structure.
        """
        # Remove script/style/noscript tags
        for unwanted in container.select("script, style, noscript, .cmp-link__screen-reader-only"):
            unwanted.decompose()

        # Remove the CTA container (Apply button etc.)
        cta = container.select_one("div.cmp-career-details__cta-container")
        if cta:
            cta.decompose()

        # Collect meaningful text blocks
        parts: list[str] = []

        for child in container.descendants:
            if not hasattr(child, "name"):
                continue

            tag_name = child.name

            if tag_name in ("p", "li"):
                text = self._clean_multiline_text(child.get_text(separator=" ", strip=True))
                if text:
                    parts.append(text)

            elif tag_name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                text = self._clean_multiline_text(child.get_text(separator=" ", strip=True))
                if text:
                    parts.append(f"\n{text}")

            elif tag_name in ("b", "strong"):
                # Inline bold text: include as part of parent flow
                pass

        result = "\n".join(parts)
        result = html.unescape(result)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip()

    # ------------------------------------------------------------------
    # Fallback link extraction
    # ------------------------------------------------------------------

    async def _fallback_links(
        self, page: Page, source_url: str, max_jobs: int
    ) -> list[Job]:
        """Fallback: extract any links containing gh_jid from the page."""
        jobs: list[Job] = []
        soup = await self._get_soup(page)

        all_links = soup.select("a[href*='gh_jid']")
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()

        for link_el in all_links[:max_jobs]:
            href = link_el.get("href")
            if not href:
                continue

            url = self._make_cohesity_job_url(source_url, str(href))
            if url in seen_urls:
                continue
            seen_urls.add(url)

            job_id = self._extract_gh_jid(url)
            if job_id in seen_ids:
                continue
            if job_id:
                seen_ids.add(job_id)

            # Try to get title from parent card
            title = "Unknown"
            location = ""
            card = link_el.find_parent("div", class_="list-card")
            if card:
                raw_title = self._extract_title_and_location(card)
                title, location = self._split_title_and_location(raw_title)

            jobs.append(Job(
                job_id=job_id,
                company=self.company_config.get("name", "Cohesity"),
                title=title,
                location=location,
                url=url,
                source_url=source_url,
                posted_date=None,
                description=None,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                extracted_experience_parts="",
            ))

        return jobs

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_text(text: str) -> str:
        """Collapse whitespace and strip."""
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        """Collapse internal whitespace but preserve line boundaries."""
        if not text:
            return ""
        text = html.unescape(text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n", "\n", text)
        return text.strip()

    @staticmethod
    def _dedupe_preserve_order(items: list) -> list:
        """Remove duplicates while preserving insertion order."""
        seen: set = set()
        result: list = []
        for item in items:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result
