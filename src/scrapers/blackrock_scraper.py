from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class BlackRockScraper(BaseScraper):
    """
    Scraper for BlackRock careers page (iCIMS / custom job board).

    The listing page at ``careers.blackrock.com`` uses server‑side
    faceted search.  Filters are applied by clicking checkbox inputs
    and selecting a sort option.

    **Filter interaction**:
    - Team checkboxes: ``input.filter-checkbox[data-id="Engineering"]``
      and ``input.filter-checkbox[data-id="Technology"]``.
    - Sort: ``select[id^='search-results-enhanced-sort-criteria']``
      set to ``"2"`` (Date Updated).

    Expected listing card structure:

        <li class="section3__search-results-li">
          <a class="section3__search-results-a"
             href="/job/mumbai/.../45831/93947096912"
             data-job-id="93947096912">
            <h2 class="section3__job-title">TITLE</h2>
            <span class="section3__job-location section3__job-information">
              <span class="section3__job-info">LOCATION</span>
            </span>
            <span class="job-category section3__job-information">
              <span class="section3__job-info">CATEGORY</span>
            </span>
          </a>
        </li>

    Expected detail page structure:

        <div class="ats-description">
          <p>... RICH HTML DESCRIPTION</p>
        </div>
    """

    # ---- Filter selectors ----
    TEAM_FILTER_TEMPLATE = "input.filter-checkbox[data-id='{team}']"
    TEAMS_TO_CHECK = ["Engineering", "Technology"]
    SORT_SELECT = "select[id^='search-results-enhanced-sort-criteria']"

    # ---- Card selectors ----
    CARD_SELECTOR = "li.section3__search-results-li"
    LINK_SELECTOR = "a.section3__search-results-a"
    TITLE_SELECTOR = "h2.section3__job-title"
    LOCATION_SELECTOR = "span.section3__job-location span.section3__job-info"
    CATEGORY_SELECTOR = "span.job-category span.section3__job-info"
    JOB_CARD_SELECTORS = [
        "li.section3__search-results-li",
        "a.section3__search-results-a",
        "h2.section3__job-title",
    ]

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = "div.ats-description"

    # ---- Pagination ----
    NEXT_BUTTON_SELECTORS = [
        "a.pagination-next:not(.disabled)",
        "a[aria-label='Next page']:not(.disabled)",
        "a[rel='next']:not(.disabled)",
        "a.search-results-next:not(.disabled)",
    ]

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        filter_teams = self.company_config.get("filter_teams", self.TEAMS_TO_CHECK)
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)

            # --- Apply Team filters: Engineering, Technology ---
            await self._apply_team_filters(page)

            # --- Apply Sort: Date Updated (value="2") ---
            await self._apply_sort(page)

            # Wait for filtered/sorted results to reload.
            await page.wait_for_timeout(3000)

            # Wait for cards to appear.
            await self._wait_for_cards(page)

            page_num = 0

            while True:
                page_num += 1
                soup = await self._get_soup(page)
                cards = soup.select(self.CARD_SELECTOR)

                if not cards:
                    self.logger.warning(
                        "No BlackRock job cards found on page %d.", page_num
                    )
                    break

                for card in cards:
                    if len(jobs) >= max_jobs:
                        break

                    job = self._parse_card(card, source_url, filter_teams)

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
                                "Failed to enrich BlackRock detail page %s: %s",
                                job.url,
                                exc,
                            )

                    if job.job_id:
                        seen_ids.add(job.job_id)
                    seen_urls.add(job.url)
                    jobs.append(job)

                if len(jobs) >= max_jobs:
                    break

                # Try pagination.
                if not await self._click_next_page(page):
                    break

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Filter interaction
    # ------------------------------------------------------------------

    async def _apply_team_filters(self, page: Page) -> None:
        """Check the Engineering and Technology team checkboxes."""
        for team in self.TEAMS_TO_CHECK:
            try:
                selector = self.TEAM_FILTER_TEMPLATE.format(team=team)
                checkbox = page.locator(selector)

                if not await checkbox.is_visible(timeout=3000):
                    self.logger.debug(
                        "Team checkbox not visible: %s", team
                    )
                    continue

                is_checked = await checkbox.is_checked()

                if is_checked:
                    self.logger.debug("Team filter already checked: %s", team)
                    continue

                # Click the label containing the team name text.
                # The label structure is: <label> <span class="filter__facet-name">TEAM</span> </label>
                label = page.locator(
                    f"label:has(span.filter__facet-name:text-is('{team}'))"
                ).first

                if await label.is_visible(timeout=1000):
                    await label.click(timeout=3000)
                else:
                    # Fall back: click the label directly after the checkbox.
                    checkbox_label = page.locator(selector + " + label").first
                    if await checkbox_label.is_visible(timeout=1000):
                        await checkbox_label.click(timeout=3000)
                    else:
                        await checkbox.check(timeout=3000, force=True)
                        await checkbox.dispatch_event("change")

                # Wait for AJAX reload of filtered results.
                await page.wait_for_timeout(3000)

                self.logger.debug("Team filter applied: %s", team)
            except Exception as exc:
                self.logger.warning(
                    "Could not apply team filter '%s': %s", team, exc
                )

    async def _apply_sort(self, page: Page) -> None:
        """Select 'Date Updated' from the sort dropdown."""
        try:
            await page.select_option(self.SORT_SELECT, "2", timeout=5000)
            await page.wait_for_timeout(2000)
            self.logger.debug("Applied sort: Date Updated")
        except Exception as exc:
            self.logger.warning("Could not apply sort: %s", exc)

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(
        self, card: Tag, source_url: str, filter_teams: list[str] | None = None
    ) -> Job | None:
        # The card <li> contains the <a> element.
        link_el = card.select_one(self.LINK_SELECTOR)

        if not link_el:
            return None

        link = link_el.get("href", "")
        if not link:
            return None

        # Make relative URLs absolute.
        if link.startswith("/"):
            link = f"https://careers.blackrock.com{link}"

        # Job ID from data-job-id.
        job_id = link_el.get("data-job-id", "")

        # Title.
        title_el = card.select_one(self.TITLE_SELECTOR)
        title = self._clean_text(title_el.get_text()) if title_el else ""

        if not title:
            return None

        # Location.
        loc_el = card.select_one(self.LOCATION_SELECTOR)
        location = self._clean_text(loc_el.get_text()) if loc_el else ""

        # Category / Team.
        cat_el = card.select_one(self.CATEGORY_SELECTOR)
        category = self._clean_text(cat_el.get_text()) if cat_el else ""

        # Post-scrape team filter.
        if filter_teams:
            matches = any(
                team.lower() == category.lower() or team.lower() in category.lower()
                for team in filter_teams
            )
            if not matches:
                self.logger.debug(
                    "Skipping non-matching team: %s (category=%s)",
                    title, category,
                )
                return None

        return Job(
            job_id=str(job_id),
            company=self.company_config.get("name", "BlackRock"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date="",
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

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
    # Pagination
    # ------------------------------------------------------------------

    async def _click_next_page(self, page: Page) -> bool:
        """
        Try to navigate to the next page of results.
        """
        for selector in self.NEXT_BUTTON_SELECTORS:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=2000):
                    await btn.click(timeout=5000)
                    await page.wait_for_timeout(3000)
                    await self._wait_for_cards(page)
                    return True
            except Exception:
                continue

        # Try "Load More" / "Show More" / "View More".
        try:
            load_more = page.locator(
                "button:has-text('Load More'), "
                "a:has-text('Load More'), "
                "button:has-text('Show More'), "
                "a:has-text('Show More'), "
                "button:has-text('View More'), "
                "a:has-text('View More')"
            ).first
            if await load_more.is_visible(timeout=2000):
                await load_more.click(timeout=5000)
                await page.wait_for_timeout(3000)
                await self._wait_for_cards(page)
                return True
        except Exception:
            pass

        return False

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
        Open a job detail page and extract the full description.
        """
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)

            await detail_page.goto(
                job_url,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await detail_page.wait_for_timeout(3000)

            description = ""
            try:
                desc_el = detail_page.locator(
                    self.DETAIL_DESCRIPTION_SELECTOR
                ).first
                await desc_el.wait_for(state="visible", timeout=15000)
                html_content = await desc_el.evaluate("el => el.innerHTML")
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
