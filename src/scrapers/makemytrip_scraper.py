from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class MakemytripScraper(BaseScraper):
    """Scraper for MakeMyTrip Careers job board.

    The SPA at careers.makemytrip.com/prod/jobs loads all job cards into
    the DOM and uses JS to show/hide by category.  Rather than fighting
    the React filter tabs, this scraper parses ALL cards with BS4 and
    filters by the category badge (span.bs-btn with 'Technology' text).
    """

    CARD_SELECTOR = 'a.bs-card.typ-opening-card'
    TITLE_SELECTOR = 'h2.title'
    LOCATION_SELECTOR = 'div.state'
    CATEGORY_SELECTOR = 'span.bs-btn'
    POSTED_SELECTOR = 'p.smallText'
    DETAIL_CONTENT_SELECTOR = 'div.jobDescContainer'
    FILTER_CATEGORY = 'Technology'

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get('url', '')
        max_jobs = int(self.settings.get('run', {}).get('max_jobs_per_company', 100))
        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until='networkidle', timeout=60000)

            # Click the Technology filter — the page uses React so we need a
            # real DOM click, not JS dispatch.  Use force to bypass visibility.
            try:
                tech_li = page.locator('ul.typ-list li').filter(has_text='Technology')
                if await tech_li.count() > 0:
                    await tech_li.first.scroll_into_view_if_needed()
                    await page.wait_for_timeout(500)
                    await tech_li.first.click(force=True, timeout=10000)
                    await page.wait_for_timeout(3000)
            except Exception as exc:
                self.logger.warning('Technology filter click failed: %s', exc)

            # Wait for cards after filtering.
            try:
                await page.wait_for_selector(self.CARD_SELECTOR, timeout=15000)
            except Exception:
                pass

            soup = await self._get_soup(page)
            cards = soup.select(self.CARD_SELECTOR)
            if not cards:
                self.logger.warning('No MakeMyTrip job cards found.')
                return jobs

            seen_ids: set[str] = set()
            seen_urls: set[str] = set()

            for card in cards:
                if len(jobs) >= max_jobs:
                    break
                job = self._parse_card(card, source_url)
                if not job:
                    continue
                category = self._extract_category(card)
                if self.FILTER_CATEGORY not in category:
                    continue
                if job.job_id and job.job_id in seen_ids:
                    continue
                if job.url in seen_urls:
                    continue

                if self._should_exclude(job.title):
                    self.logger.debug('Skipping detail enrichment for: %s', job.title)
                else:
                    try:
                        detail_desc = await self._scrape_detail_page(job.url)
                        if detail_desc:
                            job = Job(job_id=job.job_id, company=job.company, title=job.title, location=job.location, url=job.url, source_url=job.source_url, posted_date=job.posted_date, description=detail_desc, scraped_at=datetime.now(timezone.utc).isoformat(), extracted_experience_parts='')
                    except Exception as exc:
                        self.logger.warning('Failed to enrich MakeMyTrip job detail %s: %s', job.url, exc)

                if job.job_id:
                    seen_ids.add(job.job_id)
                seen_urls.add(job.url)
                jobs.append(job)
            return jobs
        finally:
            await self.close_browser()

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        href = card.get('href')
        if not href:
            return None
        url = make_absolute_url(source_url, str(href))
        title_el = card.select_one(self.TITLE_SELECTOR)
        title = self._clean_text(title_el.get_text()) if title_el else ''
        if not title:
            return None
        job_id = self._extract_job_id(url)
        location = ''
        state_el = card.select_one(self.LOCATION_SELECTOR)
        if state_el:
            location = self._clean_text(state_el.get_text())
            # Clean up "India |Bangalore" → "India, Bangalore"
            location = location.replace(' |', ', ').replace('|', ', ')
        posted_date: str | None = None
        posted_el = card.select_one(self.POSTED_SELECTOR)
        if posted_el:
            posted_date = self._clean_text(posted_el.get_text())
        return Job(job_id=job_id, company=self.company_config.get('name', 'MakeMyTrip'), title=title, location=location, url=url, source_url=source_url, posted_date=posted_date, description=None, scraped_at=datetime.now(timezone.utc).isoformat(), extracted_experience_parts='')

    def _extract_category(self, card: Tag) -> str:
        cat_el = card.select_one(self.CATEGORY_SELECTOR)
        if cat_el:
            return self._clean_text(cat_el.get_text())
        return ''

    async def _get_detail_page(self) -> Page:
        if self.context:
            try:
                return await self.context.new_page()
            except Exception:
                self.logger.debug('Shared browser context is no longer usable; creating a fresh one.')
                await self.close_browser()
        return await self.new_page()

    async def _scrape_detail_page(self, job_url: str) -> str:
        detail_page = await self._get_detail_page()
        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(job_url, wait_until='domcontentloaded', timeout=60000)
            try:
                await detail_page.wait_for_selector(self.DETAIL_CONTENT_SELECTOR, timeout=15000)
            except Exception:
                pass
            soup = await self._get_soup(detail_page)
            desc_container = soup.select_one(self.DETAIL_CONTENT_SELECTOR)
            if not desc_container:
                return ''
            return self._extract_description(desc_container)
        finally:
            await detail_page.close()

    def _extract_description(self, container: Tag) -> str:
        for unwanted in container.select('script, style, noscript'):
            unwanted.decompose()
        sections: list[str] = []
        current_section: list[str] = []
        for child in container.children:
            if not hasattr(child, 'name'):
                continue
            tag_name = child.name
            if tag_name in ('h1', 'h2', 'h3', 'h4'):
                if current_section:
                    sections.append('\n'.join(current_section))
                    current_section = []
                heading = self._clean_text(child.get_text())
                if heading:
                    sections.append(heading)
            elif tag_name in ('p', 'li'):
                text = self._clean_text(child.get_text())
                if text:
                    current_section.append(text)
            elif tag_name in ('ul', 'ol'):
                if current_section:
                    sections.append('\n'.join(current_section))
                    current_section = []
                items: list[str] = []
                for li in child.select('li'):
                    li_text = self._clean_text(li.get_text())
                    if li_text:
                        items.append('- ' + li_text)
                if items:
                    sections.append('\n'.join(items))
            else:
                text = self._clean_text(child.get_text())
                if text:
                    current_section.append(text)
        if current_section:
            sections.append('\n'.join(current_section))
        return '\n\n'.join(sections)

    def _extract_job_id(self, url: str) -> str:
        if not url:
            return ''
        match = re.search(r'/opportunity/([a-f0-9]+)/', url)
        if match:
            return match.group(1)
        return ''

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ''
        return ' '.join(text.split()).strip()
