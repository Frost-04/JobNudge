from __future__ import annotations

from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper


class ConcertAIScraper(BaseScraper):
    """
    Scraper for Concert AI careers page (Phenom People / SAP SuccessFactors).

    The listing page at ``careers.concertai.com/us/en/search-results`` is an
    Aurelia SPA with facet-menu filters for Country and a sort dropdown.
    Cards are ``<li class="jobs-list-item">`` elements whose ``<a>`` tags
    carry rich ``data-ph-at-*`` attributes (title, location, job id, posted
    date, category, type).

    Detail pages are standard Phenom job-details views with the full
    description inside ``div.jd-info[data-ph-at-id="jobdescription-text"]``.

    **Filter strategy**: The country facet-menu is an Aurelia accordion
    widget that is difficult to interact with headlessly.  Instead we
    scrape all cards and post‑filter by the ``filter_location`` config
    field (default ``"India"``).  The sort dropdown (``#sortselect``)
    is a plain ``<select>`` and is set to "Most recent" via
    ``page.select_option``.

    Expected listing card structure:

        <li class="jobs-list-item">
          <a data-ph-at-id="job-link"
             data-ph-at-job-title-text="Associate Software Engineer"
             data-ph-at-job-location-text="Bengaluru, Karnataka, India"
             data-ph-at-job-id-text="P-100849"
             data-ph-at-job-post-date-text="2026-06-10T00:00:00.000+0000"
             data-ph-at-job-category-text="CancerLinQ"
             data-ph-at-job-type-text="Full Time"
             href=".../job/P-100849/Associate-Software-Engineer">
            <div class="job-title"><span>TITLE</span></div>
          </a>
          <p class="job-info">
            <span class="job-location">LOCATION</span>
            <span class="job-category">CATEGORY</span>
            <span class="jobId">P-100849</span>
          </p>
          <p class="job-description">DESCRIPTION TEASER</p>
        </li>

    Expected detail page structure:

        <div class="job-description">
          <div class="jd-info au-target"
               data-ph-at-id="jobdescription-text">
            RICH HTML DESCRIPTION
          </div>
        </div>
    """

    # ---- Sort selector ----
    SORT_SELECT = "#sortselect"

    # ---- Card selectors ----
    CARD_SELECTOR = "li.jobs-list-item a[data-ph-at-id='job-link']"
    JOB_CARD_SELECTORS = [
        "li.jobs-list-item a[data-ph-at-id='job-link']",
        "li.jobs-list-item .job-title",
    ]

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = "div.jd-info[data-ph-at-id='jobdescription-text']"

    # ---- Pagination ----
    NEXT_BUTTON_SELECTORS = [
        "a[aria-label='Next']:not(.disabled)",
        "button[aria-label='Next']:not(.disabled)",
        "a.pagination-next:not(.disabled)",
        "li.next a:not(.disabled)",
    ]

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)

            # --- Apply Sort: Most recent ---
            await self._apply_sort(page, "Most recent")

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
                        "No Concert AI job cards found on page %d.", page_num
                    )
                    break

                for card in cards:
                    if len(jobs) >= max_jobs:
                        break

                    job = self._parse_card(card, source_url)

                    if not job:
                        continue

                    # ---- Post-scrape location filter ----
                    filter_loc = self.company_config.get("filter_location", "India")
                    if filter_loc:
                        location_text = (job.location or "").lower()
                        if filter_loc.lower() not in location_text:
                            self.logger.debug(
                                "Skipping non-%s job: %s | %s",
                                filter_loc, job.title, job.location,
                            )
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
                                "Failed to enrich Concert AI detail page %s: %s",
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

    async def _apply_sort(self, page: Page, sort_value: str) -> None:
        """Select a sort option from the sort dropdown."""
        try:
            await page.select_option(self.SORT_SELECT, sort_value, timeout=5000)
            await page.wait_for_timeout(1500)
            self.logger.debug("Applied sort: %s", sort_value)
        except Exception as exc:
            self.logger.warning(
                "Could not apply sort '%s': %s", sort_value, exc
            )

    # ------------------------------------------------------------------
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = card.get("href", "")
        if not link:
            return None

        # Make relative URLs absolute.
        if link.startswith("/"):
            link = f"https://careers.concertai.com{link}"

        # Primary extraction from data-ph-at-* attributes.
        title = (card.get("data-ph-at-job-title-text") or "").strip()
        if not title:
            # Fallback: try the <span> inside .job-title.
            title_el = card.select_one(".job-title span, .job-title")
            if title_el:
                title = self._clean_text(title_el.get_text())

        location = (card.get("data-ph-at-job-location-text") or "").strip()
        job_id = (card.get("data-ph-at-job-id-text") or "").strip()
        posted_date = (card.get("data-ph-at-job-post-date-text") or "").strip()

        if not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Concert AI"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=posted_date,
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
                await page.wait_for_selector(selector, timeout=10000)
                return
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    async def _click_next_page(self, page: Page) -> bool:
        """
        Try to navigate to the next page of results.

        Returns True if the page was advanced, False if there are no more pages.
        """
        # Try standard "Next" button selectors.
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

        # Try "Load More" / "Show More" pattern.
        try:
            load_more = page.locator(
                "button:has-text('Load More'), "
                "a:has-text('Load More'), "
                "button:has-text('Show More'), "
                "a:has-text('Show More')"
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
        """Open a job detail page and extract the full description."""
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)

            await detail_page.goto(
                job_url,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await detail_page.wait_for_timeout(2000)

            description = ""
            try:
                desc_el = detail_page.locator(
                    self.DETAIL_DESCRIPTION_SELECTOR
                ).first
                await desc_el.wait_for(state="visible", timeout=10000)
                description = self._clean_text(await desc_el.inner_html())
            except Exception:
                try:
                    # Fallback: any div.jd-info inside .job-description.
                    fallback_el = detail_page.locator(
                        "div.job-description div.jd-info"
                    ).first
                    description = self._clean_text(
                        await fallback_el.inner_html()
                    )
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
