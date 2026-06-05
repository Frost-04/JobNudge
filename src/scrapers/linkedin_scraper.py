from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class LinkedInScraper(BaseScraper):
    """
    Scraper for LinkedIn Jobs search result pages.

    Expected listing card structure:

    ul.jobs-search__results-list > li
      div.job-search-card[data-entity-urn="urn:li:jobPosting:4417493327"]
        a.base-card__full-link[href]       (overlay link to job detail)
        h3.base-search-card__title          (job title)
        h4.base-search-card__subtitle       (company name)
        span.job-search-card__location      (location)
        time.job-search-card__listdate      (posted date, has datetime attr)

    Expected detail page structure:

    section.show-more-less-html
      div.show-more-less-html__markup       (truncated description)
      button.show-more-less-html__button--more  ("Show more" button)

    After clicking "Show more", the section gets the class
    ``show-more-less-html--more`` and the markup div loses the
    ``--clamp-after-5`` modifier, revealing the full description.

    Technique — "Show More" expansion:
    LinkedIn truncates job descriptions with a CSS clamp.  The scraper
    clicks the "Show more" button (if visible), waits for the section's
    class to change to ``show-more-less-html--more``, then extracts the
    now-fully-visible description text via BS4.
    """

    CARD_SELECTOR = 'div.job-search-card'

    JOB_CARD_SELECTORS = [
        'div.job-search-card',
        'ul.jobs-search__results-list li',
        'a.base-card__full-link[href*="/jobs/view/"]',
    ]

    TITLE_SELECTOR = 'h3.base-search-card__title'
    COMPANY_SELECTOR = 'h4.base-search-card__subtitle'
    LOCATION_SELECTOR = 'span.job-search-card__location'
    POSTED_SELECTOR = 'time.job-search-card__listdate'

    # Detail page selectors
    DESCRIPTION_CONTAINER = 'section.show-more-less-html'
    DESCRIPTION_MARKUP = 'div.show-more-less-html__markup'
    SHOW_MORE_BUTTON = 'button.show-more-less-html__button--more'

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            selector = await self._wait_for_any_selector(page, self.JOB_CARD_SELECTORS)

            if not selector:
                return await self._fallback_links(page, source_url, max_jobs)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                return await self._fallback_links(page, source_url, max_jobs)

            seen_job_ids: set[str] = set()
            seen_urls: set[str] = set()

            for card in cards[:max_jobs]:
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_job_ids:
                    continue

                if job.url in seen_urls:
                    continue

                # Enrich by opening the job detail page and clicking "Show more".
                try:
                    detail_data = await self._scrape_detail_page(job.url)

                    detail_posted_date = detail_data.get("date posted", "")
                    detail_description = detail_data.get("description", "")

                    metadata_description = self._format_detail_metadata(detail_data)

                    combined_description = self._join_description_parts(
                        metadata_description,
                        detail_description,
                    )

                    job = Job(
                        job_id=job.job_id,
                        company=job.company,
                        title=job.title,
                        location=job.location,
                        url=job.url,
                        source_url=job.source_url,
                        posted_date=detail_posted_date or job.posted_date,
                        description=combined_description or job.description,
                        scraped_at=datetime.now(timezone.utc).isoformat(),
                        extracted_experience_parts="",
                    )

                except Exception as exc:
                    self.logger.warning(
                        "Failed to enrich LinkedIn job detail page %s: %s",
                        job.url,
                        exc,
                    )

                if job.job_id:
                    seen_job_ids.add(job.job_id)

                seen_urls.add(job.url)
                jobs.append(job)

            if not jobs:
                jobs = await self._fallback_links(page, source_url, max_jobs)

            return jobs

        finally:
            await self.close_browser()

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

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link = self._extract_link(card, source_url)
        title = self._extract_title(card)
        job_id = self._extract_job_id(card, link)
        company = self._extract_company(card)
        location = self._extract_location(card)
        posted_date = self._extract_posted_date(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=company or self.company_config.get("name", "LinkedIn"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=posted_date or None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag, source_url: str) -> str:
        el = card.select_one('a.base-card__full-link')

        if not el:
            return ""

        href = el.get("href")

        if not href:
            return ""

        return self._make_linkedin_job_url(source_url, str(href))

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        # Fallback: sr-only span inside the full-link anchor
        full_link = card.select_one('a.base-card__full-link')

        if full_link:
            sr_only = full_link.select_one('span.sr-only')

            if sr_only:
                return self._clean_text(sr_only.get_text())

        return ""

    def _extract_company(self, card: Tag) -> str:
        el = card.select_one(self.COMPANY_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        return ""

    def _extract_location(self, card: Tag) -> str:
        el = card.select_one(self.LOCATION_SELECTOR)

        if el:
            return self._clean_text(el.get_text())

        return ""

    def _extract_posted_date(self, card: Tag) -> str:
        el = card.select_one(self.POSTED_SELECTOR)

        if el:
            # Prefer the ISO datetime attribute
            datetime_attr = el.get("datetime")

            if datetime_attr:
                return str(datetime_attr)

            # Fallback to text like "2 weeks ago"
            return self._clean_text(el.get_text())

        return ""

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        LinkedIn job IDs come from the data-entity-urn attribute:

        data-entity-urn="urn:li:jobPosting:4417493327"

        Fallback: extract from the detail page URL.
        """

        entity_urn = card.get("data-entity-urn")

        if entity_urn:
            match = re.search(r"urn:li:jobPosting:(\d+)", str(entity_urn))

            if match:
                return match.group(1)

        if link:
            return self._extract_linkedin_job_id_from_url(link)

        return extract_job_id(link) if link else ""

    async def _get_detail_page(self) -> Page:
        if self.context:
            try:
                return await self.context.new_page()
            except Exception:
                self.logger.debug(
                    "Shared browser context is no longer usable; discarding and creating a fresh one."
                )
                await self.close_browser()

        return await self.new_page()

    async def _scrape_detail_page(self, job_url: str) -> dict[str, str]:
        """
        Navigate to the LinkedIn job detail page, click "Show more" if present,
        then extract the full job description.
        """
        detail_page = await self._get_detail_page()

        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for either the description container or the show-more section
            await self._wait_for_any_selector(
                detail_page,
                [
                    self.DESCRIPTION_CONTAINER,
                    self.DESCRIPTION_MARKUP,
                    'h1',
                ],
            )

            # Click "Show more" button to expand the full description.
            await self._click_show_more(detail_page)

            soup = await self._get_soup(detail_page)

            detail_data: dict[str, str] = {}

            description = self._extract_description(soup)

            if description:
                detail_data["description"] = description

            return detail_data

        finally:
            await detail_page.close()

    async def _click_show_more(self, page: Page) -> None:
        """
        LinkedIn clips job descriptions after ~5 lines.  Click the
        "Show more" button to reveal the full text.

        The section transitions from:
            section.show-more-less-html
        to:
            section.show-more-less-html.show-more-less-html--more
        """
        try:
            # Check if the "Show more" button is visible.
            button = page.locator(self.SHOW_MORE_BUTTON)

            if await button.count() > 0 and await button.is_visible():
                await button.click()

                # Wait for the expanded state.
                await page.wait_for_selector(
                    'section.show-more-less-html--more',
                    timeout=5000,
                )
        except Exception:
            # "Show more" may not be present for all job postings.
            pass

    def _extract_description(self, soup) -> str:
        # After clicking "Show more", the markup div loses the clamp class.
        # Try the unclamped version first, then fall back to any markup div.
        container = soup.select_one(
            'section.show-more-less-html--more div.show-more-less-html__markup'
        )

        if not container:
            container = soup.select_one(f'div.{self.DESCRIPTION_MARKUP.split(".")[-1]}')

        if not container:
            container = soup.select_one('section.show-more-less-html')

        if not container:
            return ""

        # Remove non-description elements
        for unwanted in container.select("script, style, noscript, button, icon, svg"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    def _format_detail_metadata(self, detail_data: dict[str, str]) -> str:
        """LinkedIn detail pages embed everything in the description markup."""
        return ""

    def _join_description_parts(self, *parts: str) -> str:
        cleaned_parts = [part.strip() for part in parts if part and part.strip()]
        return "\n\n".join(cleaned_parts)

    def _make_linkedin_job_url(self, source_url: str, href: str) -> str:
        href = html.unescape(href).strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        parsed_source = urlparse(source_url)
        origin = f"{parsed_source.scheme}://{parsed_source.netloc}"

        if href.startswith("/jobs/view/"):
            return f"{origin}{href}"

        if href.startswith("jobs/view/"):
            return f"{origin}/{href}"

        return make_absolute_url(source_url, href)

    def _extract_linkedin_job_id_from_url(self, url: str) -> str:
        """
        LinkedIn job detail URLs look like:

        https://in.linkedin.com/jobs/view/senior-enterprise-engineer-at-linkedin-4410582759
        """
        if not url:
            return ""

        # Extract the trailing numeric ID
        match = re.search(r"-(\d+)(?:\?|$)", url)

        if match:
            return match.group(1)

        return extract_job_id(url) or ""

    async def _fallback_links(self, page: Page, source_url: str, max_jobs: int) -> list[Job]:
        """
        Fallback: extract jobs from anchor links when card selectors fail.
        """
        soup = await self._get_soup(page)

        jobs: list[Job] = []
        seen_urls: set[str] = set()

        for link in soup.select('a.base-card__full-link[href*="/jobs/view/"]'):
            if len(jobs) >= max_jobs:
                break

            href = link.get("href")

            if not href:
                continue

            url = self._make_linkedin_job_url(source_url, str(href))

            if url in seen_urls:
                continue

            seen_urls.add(url)

            # Get title from sr-only span or parent card title
            title = ""
            sr_only = link.select_one("span.sr-only")

            if sr_only:
                title = self._clean_text(sr_only.get_text())

            if not title:
                # Try walking up to the card to get the title
                card = link.find_parent("div", class_=lambda c: c and "job-search-card" in c)

                if card:
                    title_el = card.select_one("h3.base-search-card__title")

                    if title_el:
                        title = self._clean_text(title_el.get_text())

            job_id = self._extract_linkedin_job_id_from_url(url)

            jobs.append(Job(
                job_id=job_id,
                company=self.company_config.get("name", "LinkedIn"),
                title=title,
                location="",
                url=url,
                source_url=source_url,
                posted_date=None,
                description=None,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                extracted_experience_parts="",
            ))

        return jobs

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)

        return text.strip()

    def _clean_multiline_text(self, text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        text = text.replace("\xa0", " ")

        lines = []
        for line in text.splitlines():
            clean_line = self._clean_text(line)

            if clean_line:
                lines.append(clean_line)

        return "\n".join(lines).strip()

    def _dedupe_preserve_order(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []

        for value in values:
            normalized = value.lower().strip()

            if not normalized or normalized in seen:
                continue

            seen.add(normalized)
            result.append(value)

        return result
