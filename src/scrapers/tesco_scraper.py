from __future__ import annotations

from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class TescoScraper(BaseScraper):
    """
    Scraper for Tesco Careers job search page.

    Tesco's career page at careers.tesco.com uses a wizard-style filter form
    with Select2-enhanced multi-select dropdowns for "Our businesses" and
    "Our teams".  Filters are applied client-side via Select2 — not URL
    parameters.  After clicking "Search", results render as ``article.article--result``
    cards.  Each card links to a separate detail page with labeled field sections.

    Filter form:

        <select id="12328" multiple>       → "Our businesses" (Select2)
          <option value="587056">Tesco India</option>
        <select id="59964" multiple>       → "Our teams" (Select2)
          <option value="4196244">Technology</option>
          <option value="4196261">Internships</option>
          <option value="4196262">Graduates</option>
        <button id="270-submit">Search</button>

    Sort dropdown (already defaulted to "Recently added"):

        li.sort__item--active[data-sortby="postedDate"]

    Card structure:

        article.article--result
          h3.article__header__text__title > a.link[href*="/JobDetail/"]
          span.list-item-location
          div.article__content  (preview snippet)

    Detail page structure:

        article.article--details
          div.article__content__view__field__label   → section header
          div.article__content__view__field__value   → section content
    """

    # ---- Form selectors ----
    BUSINESS_SELECT_ID = "12328"            # "Our businesses" — multi-select
    TEAMS_SELECT_ID = "59964"              # "Our teams" — multi-select
    SEARCH_BUTTON_SELECTOR = "button.submitButton"

    # ---- Business / team option values ----
    BUSINESS_TESCO_INDIA = "587056"
    TEAM_TECHNOLOGY = "4196244"
    TEAM_INTERNSHIPS = "4196261"
    TEAM_GRADUATES = "4196262"

    # ---- Card selectors ----
    RESULTS_CONTAINER = "div.results--listed"
    CARD_SELECTOR = "article.article--result"
    TITLE_LINK_SELECTOR = "h3.article__header__text__title a.link"
    LOCATION_SELECTOR = "span.list-item-location"
    BUSINESS_SPAN_SELECTOR = "span.list-item-legalEntity"

    # ---- Detail page selectors ----
    DETAIL_ARTICLE_SELECTOR = "article.article--details"
    DETAIL_FIELD_LABEL = "div.article__content__view__field__label"
    DETAIL_FIELD_VALUE = "div.article__content__view__field__value"

    # ------------------------------------------------------------------
    # Main scrape entry point
    # ------------------------------------------------------------------

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # ---- Step 1: Apply Select2 filters via JS ----
            await self._apply_select2_filters(page)

            # ---- Step 2: Click Search ----
            await self._click_search(page)

            # ---- Step 3: Wait for results ----
            await self._wait_for_results(page)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No Tesco job cards found.")
                return jobs

            self.logger.info("Found %d Tesco job cards.", len(cards))

            for card in cards[:max_jobs]:
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_ids:
                    continue
                if job.url in seen_urls:
                    continue

                # Enrich with detail page description.
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
                            "Failed to enrich Tesco job detail %s: %s",
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
    # Select2 filter interaction
    # ------------------------------------------------------------------

    async def _apply_select2_filters(self, page: Page) -> None:
        """
        Select filter values by setting the underlying <select> options
        and triggering change events that Select2 recognizes.

        Works by:
        1. Setting ``selected`` on the native <option> elements
        2. Calling jQuery ``.val([...]).trigger('change')`` for Select2
        3. Also dispatching a native change event as fallback
        """
        await page.evaluate(
            """
            () => {
                const $ = window.jQuery;
                if (!$) return;

                // --- "Our businesses": Tesco India ---
                const bizSelect = document.getElementById('12328');
                if (bizSelect) {
                    for (const opt of bizSelect.options) {
                        opt.selected = (opt.value === '587056');
                    }
                }
                // Use jQuery to trigger Select2 update.
                try { $('#12328').val(['587056']).trigger('change'); } catch(e) {}

                // --- "Our teams": Technology + Internships + Graduates ---
                const teamSelect = document.getElementById('59964');
                if (teamSelect) {
                    const target = ['4196244', '4196261', '4196262'];
                    for (const opt of teamSelect.options) {
                        opt.selected = target.includes(opt.value);
                    }
                }
                try { $('#59964').val(['4196244','4196261','4196262']).trigger('change'); } catch(e) {}
            }
            """
        )
        await page.wait_for_timeout(1500)

    async def _click_search(self, page: Page) -> None:
        """Click the Search button to submit the filter form."""
        try:
            search_btn = page.locator(self.SEARCH_BUTTON_SELECTOR)
            if await search_btn.count():
                await search_btn.click()
                await page.wait_for_timeout(3000)
            else:
                # Try JS click as fallback.
                await page.evaluate(
                    "() => { const btn = document.querySelector('button.submitButton'); if (btn) btn.click(); }"
                )
                await page.wait_for_timeout(3000)
        except Exception as exc:
            self.logger.warning("Failed to click Tesco Search button: %s", exc)

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        # Title from h3 > a.link.
        title_el = card.select_one(self.TITLE_LINK_SELECTOR)
        if not title_el:
            return None

        href = title_el.get("href")
        if not href:
            return None

        title = self._clean_text(title_el.get_text())
        if not title:
            return None

        url = make_absolute_url(source_url, str(href))

        # Job ID from URL: /careers/JobDetail/Title/121218 → 121218
        job_id = self._extract_tesco_job_id(url)

        # Location from span.list-item-location.
        location_el = card.select_one(self.LOCATION_SELECTOR)
        location = self._clean_text(location_el.get_text()) if location_el else ""

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Tesco"),
            title=title,
            location=location,
            url=url,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    @staticmethod
    def _extract_tesco_job_id(url: str) -> str:
        """Extract numeric job ID from Tesco URL.

        URL format: /careers/JobDetail/Title/121218
        """
        import re
        match = re.search(r"/JobDetail/[^/]+/(\d+)", url)
        if match:
            return match.group(1)

        # Generic fallback.
        return extract_job_id(url)

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

    async def _scrape_detail_page(self, job_url: str) -> str:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)

            await detail_page.goto(
                job_url, wait_until="domcontentloaded", timeout=60000
            )

            # Wait for the detail article to load.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_ARTICLE_SELECTOR,
                    timeout=15000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            article = soup.select_one(self.DETAIL_ARTICLE_SELECTOR)
            if not article:
                return ""

            return self._extract_description(article)

        finally:
            await detail_page.close()

    def _extract_description(self, article: Tag) -> str:
        """
        Extract description from labeled field pairs.

        Structure:
            div.article__content__view__field
              div.article__content__view__field__label   → "About the role"
              div.article__content__view__field__value   → "...content..."
        """
        sections: list[str] = []

        for field in article.select("div.article__content__view__field"):
            label_el = field.select_one(self.DETAIL_FIELD_LABEL)
            value_el = field.select_one(self.DETAIL_FIELD_VALUE)

            if not label_el or not value_el:
                continue

            label = self._clean_text(label_el.get_text())
            if not label:
                continue

            # Clean the value content — remove scripts/styles, get text.
            for unwanted in value_el.select("script, style, noscript"):
                unwanted.decompose()

            value = self._clean_text(value_el.get_text())
            if not value:
                continue

            sections.append(f"{label}\n{value}")

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _wait_for_results(self, page: Page) -> None:
        """Wait for the first job card or results container to appear."""
        selectors = [
            self.CARD_SELECTOR,
            self.RESULTS_CONTAINER,
        ]
        timeout_ms = self._to_ms(
            self.settings.get("run", {}).get("page_load_timeout_seconds"),
            45000,
        )

        for selector in selectors:
            try:
                await page.wait_for_selector(selector, timeout=timeout_ms)
                return
            except Exception:
                continue

    @staticmethod
    def _clean_text(text: str) -> str:
        """Normalize whitespace in a string."""
        if not text:
            return ""
        return " ".join(text.split()).strip()
