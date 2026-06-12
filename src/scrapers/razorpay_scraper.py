from __future__ import annotations

import asyncio
import html as html_mod
import json
import re
import urllib.request
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class RazorpayScraper(BaseScraper):
    """Scraper for Razorpay Jobs page (https://razorpay.com/jobs/jobs-all/).

    Razorpay uses a custom React job board backed by Greenhouse ATS.
    Filters are applied client-side by clicking checkboxes for Locations
    and Role.  Job cards are ``<a>`` elements linking to Greenhouse-hosted
    detail pages (``/jobs/jobs-all/detail/?gh_jid=...``).

    Card structure
    --------------
    Each ``a.styles_container__LrNWu`` contains three ``col-md-4`` divs::

        <a href="/jobs/jobs-all/detail/?gh_jid=4684254005"
           class="row styles_container__LrNWu">
          <div class="... col-md-4">
            <span class="styles_jobTitle__ZewFx">Title</span>
          </div>
          <div class="... col-md-4">
            <span class="styles_jobDept__cpd2J">Department</span>
          </div>
          <div class="... col-md-4">
            <span class="styles_jobDept__cpd2J">
              <svg>...</svg>Location / Department
            </span>
          </div>
        </a>

    Detail enrichment
    -----------------
    Job descriptions are fetched from the Greenhouse API
    (``boards-api.greenhouse.io/v1/boards/razorpaysoftwareprivatelimited/jobs/{id}``)
    instead of scraping the detail page, which loads content dynamically
    inside an iframe.
    """

    CARD_SELECTOR = "a.styles_container__LrNWu"
    JOB_ID_PATTERN = re.compile(r"gh_jid=(\d+)")
    GREENHOUSE_BOARD = "razorpaysoftwareprivatelimited"
    GREENHOUSE_API = (
        "https://boards-api.greenhouse.io/v1/boards/"
        + GREENHOUSE_BOARD
        + "/jobs/{job_id}"
    )

    LOCATION_FILTERS = [
        "Bengaluru",
        "Mumbai",
        "Bengaluru; Gurugram",
        "Gurugram",
        "Kolkata",
        "Gurugram; Mumbai",
    ]
    ROLE_FILTERS = ["Engineering", "Backend"]

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for job cards to appear.
            await page.wait_for_selector(self.CARD_SELECTOR, timeout=30000)

            # ---- Apply location filters ----
            for loc_id in self.LOCATION_FILTERS:
                try:
                    checkbox = page.locator(f'input[id="{loc_id}"]')
                    if await checkbox.count() > 0:
                        await checkbox.scroll_into_view_if_needed()
                        await checkbox.click()
                        await asyncio.sleep(0.3)
                except Exception as exc:
                    self.logger.warning(
                        "Failed to click location filter '%s': %s", loc_id, exc
                    )

            # ---- Apply role filters ----
            for role_id in self.ROLE_FILTERS:
                try:
                    checkbox = page.locator(f'input[id="{role_id}"]')
                    if await checkbox.count() > 0:
                        await checkbox.scroll_into_view_if_needed()
                        await checkbox.click()
                        await asyncio.sleep(0.3)
                except Exception as exc:
                    self.logger.warning(
                        "Failed to click role filter '%s': %s", role_id, exc
                    )

            # Let React re-render with active filters.
            await asyncio.sleep(3)

            # ---- Extract job cards ----
            soup = await self._get_soup(page)
            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No Razorpay job cards found after filtering.")
                return jobs

            seen_ids: set[str] = set()
            seen_urls: set[str] = set()

            for card in cards:
                if len(jobs) >= max_jobs:
                    break

                title_el = card.select_one("span.styles_jobTitle__ZewFx")
                dept_els = card.select("span.styles_jobDept__cpd2J")

                title = title_el.get_text(strip=True) if title_el else ""
                department = dept_els[0].get_text(strip=True) if len(dept_els) > 0 else ""

                # Third column contains location text (with an SVG icon inside the span).
                location_raw = ""
                if len(dept_els) > 1:
                    loc_span = dept_els[1]
                    # Remove SVG content from the text.
                    for svg in loc_span.select("svg"):
                        svg.decompose()
                    location_raw = loc_span.get_text(strip=True)

                href = (card.get("href") or "").strip()
                url = make_absolute_url(source_url, href)

                # Extract Greenhouse job ID from query string.
                job_id = ""
                if url:
                    m = self.JOB_ID_PATTERN.search(url)
                    if m:
                        job_id = m.group(1)

                if not title:
                    continue
                if url and url in seen_urls:
                    continue
                if job_id and job_id in seen_ids:
                    continue

                job = Job(
                    job_id=job_id,
                    company=self.company_config.get("name", "Razorpay"),
                    title=title,
                    location=location_raw,
                    url=url,
                    source_url=source_url,
                    posted_date=None,
                    description=None,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    extracted_experience_parts="",
                )

                # ---- Enrich via Greenhouse API ----
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
                elif job_id:
                    try:
                        detail_desc = self._fetch_from_api(job_id)
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
                            "Failed to enrich Razorpay job detail %s: %s",
                            job.url,
                            exc,
                        )

                if job_id:
                    seen_ids.add(job_id)
                seen_urls.add(job.url)
                jobs.append(job)

            return jobs

        finally:
            await self.close_browser()

    # ------------------------------------------------------------------
    # Greenhouse API enrichment
    # ------------------------------------------------------------------

    def _fetch_from_api(self, job_id: str) -> str:
        """Fetch job description from the Greenhouse public API.

        Returns the plain-text content of the job description or an
        empty string on failure.
        """
        api_url = self.GREENHOUSE_API.format(job_id=job_id)

        try:
            req = urllib.request.Request(
                api_url,
                headers={"User-Agent": "JobNudge/1.0"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            self.logger.warning("Greenhouse API error for job %s: %s", job_id, exc)
            return ""

        content_html = data.get("content", "")
        if not content_html:
            return ""

        # The API returns HTML-escaped markup.  Unescape once so BS4
        # can parse it.
        content_html = html_mod.unescape(content_html)
        soup = BeautifulSoup(content_html, "html.parser")

        # Remove script/style tags.
        for unwanted in soup.select("script, style, noscript"):
            unwanted.decompose()

        return soup.get_text(separator="\n", strip=True)
