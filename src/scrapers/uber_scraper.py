from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class UberScraper(BaseScraper):
    """
    Scraper for Uber Careers job listing page.

    Uber uses a custom React SPA with CSS-in-JS class names (``css-*`` prefix).
    The DOM structure is stable even if the hashed class names change:

    Each card is a ``div`` containing three ``div.css-bAjVIk`` key-value blocks:

        div.css-bAjVIk                     (Role)
          div.css-jmmBrd  → "Role"
          span.css-dCwqLp
            a.css-fYOjwv[href]  → job title + URL

        div.css-bAjVIk                     (Sub-Team)
          div.css-jmmBrd  → "Sub-Team"
          span.css-dCwqLp  → sub-team name

        div.css-bAjVIk                     (Location)
          div.css-jmmBrd  → "Location"
          div > span.css-dCwqLp  → city, country (may appear multiple times)

    Job IDs are numeric and extracted from the detail URL path:
    ``/careers/list/159996`` → ``159996``.

    The detail page (redirects from /careers/list/{id} to
    /global/en/careers/list/{id}/) renders:

        div.css-cvJeNJ                    (description container)
          p, h5, ul, li
    """

    # ---- Card selectors (CSS-in-JS — may change on redeploy) ----
    KV_BLOCK_SELECTOR = 'div.css-bAjVIk'
    KV_LABEL_SELECTOR = 'div.css-jmmBrd'
    KV_VALUE_SELECTOR = 'span.css-dCwqLp'
    TITLE_LINK_SELECTOR = 'a.css-fYOjwv'

    # ---- Detail page selectors ----
    DETAIL_DESCRIPTION_SELECTOR = 'div.css-cvJeNJ'

    # Job ID pattern from URL
    JOB_ID_PATTERN = re.compile(r'/careers/list/(\d+)')

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            await self._wait_for_results(page)

            # Extra settle time for the React SPA to finish rendering all cards.
            await asyncio.sleep(3)

            # Extract card data via page.evaluate for reliable JS-based extraction.
            card_data = await page.evaluate('''() => {
                const kvBlocks = document.querySelectorAll('div.css-bAjVIk');
                const cards = new Map();  // parent element → {role, subteam, location}

                kvBlocks.forEach(block => {
                    const parent = block.parentElement;
                    if (!parent || parent.tagName !== 'DIV') return;

                    if (!cards.has(parent)) {
                        cards.set(parent, { role: null, url: null, subteam: null, locations: [] });
                    }

                    const labelEl = block.querySelector('div.css-jmmBrd');
                    if (!labelEl) return;

                    const label = labelEl.textContent.trim().toLowerCase();
                    const valueEls = block.querySelectorAll('span.css-dCwqLp');

                    if (label === 'role') {
                        const link = block.querySelector('a.css-fYOjwv');
                        if (link) {
                            cards.get(parent).role = link.textContent.trim();
                            cards.get(parent).url = link.getAttribute('href');
                        } else if (valueEls.length > 0) {
                            cards.get(parent).role = valueEls[0].textContent.trim();
                        }
                    } else if (label === 'sub-team' && valueEls.length > 0) {
                        cards.get(parent).subteam = valueEls[0].textContent.trim();
                    } else if (label === 'location') {
                        valueEls.forEach(el => {
                            const text = el.textContent.trim();
                            if (text) cards.get(parent).locations.push(text);
                        });
                    }
                });

                return Array.from(cards.values());
            }''')

            if not card_data:
                self.logger.warning("No Uber job cards found.")
                return jobs

            seen_ids: set[str] = set()
            seen_urls: set[str] = set()

            for data in card_data:
                if len(jobs) >= max_jobs:
                    break

                title = (data.get("role") or "").strip()
                href = (data.get("url") or "").strip()
                locations = data.get("locations") or []

                if not title:
                    continue

                url = make_absolute_url(source_url, href) if href else ""

                # Extract job ID from URL.
                job_id = ""
                if url:
                    m = self.JOB_ID_PATTERN.search(url)
                    if m:
                        job_id = m.group(1)

                location = "; ".join(locations) if locations else ""

                if url and url in seen_urls:
                    continue
                if job_id and job_id in seen_ids:
                    continue

                job = Job(
                    job_id=job_id,
                    company=self.company_config.get("name", "Uber"),
                    title=title,
                    location=location,
                    url=url,
                    source_url=source_url,
                    posted_date=None,
                    description=None,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    extracted_experience_parts="",
                )

                # Enrich with detail page description.
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
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
                            "Failed to enrich Uber job detail %s: %s",
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

    async def _scrape_detail_page(self, job_url: str) -> str:
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(15000)

            # The detail page redirects from /careers/list/{id} to
            # /global/en/careers/list/{id}/ (or externally to iCIMS for
            # university roles).  Use a generous navigation timeout.
            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=90000)

            # Wait for the description container.  Skipped silently if
            # the page redirects to an external ATS (e.g. iCIMS).
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_DESCRIPTION_SELECTOR,
                    timeout=20000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            desc_container = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)
            if not desc_container:
                return ""

            return self._extract_description(desc_container)

        finally:
            await detail_page.close()

    def _extract_description(self, container: Tag) -> str:
        """Extract clean description text from the Uber detail page container."""
        # Remove script/style tags.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        # Collect content preserving section structure.
        sections: list[str] = []
        current_section: list[str] = []

        for child in container.children:
            if not hasattr(child, "name"):
                continue

            tag_name = child.name

            if tag_name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                if current_section:
                    sections.append("\n".join(current_section))
                    current_section = []

                heading = self._clean_text(child.get_text())
                if heading:
                    sections.append(heading)
            elif tag_name in ("p", "ul", "ol", "li"):
                text = self._clean_text(child.get_text())
                if text:
                    current_section.append(text)
            else:
                text = self._clean_text(child.get_text())
                if text:
                    current_section.append(text)

        if current_section:
            sections.append("\n".join(current_section))

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _wait_for_results(self, page: Page) -> None:
        """Wait for the first key-value block to appear."""
        try:
            await page.wait_for_selector(self.KV_BLOCK_SELECTOR, timeout=45000)
        except Exception:
            pass

    @staticmethod
    def _clean_text(text: str) -> str:
        """Normalize whitespace in a string."""
        if not text:
            return ""
        return " ".join(text.split()).strip()
