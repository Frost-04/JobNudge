from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class OpenAIScraper(BaseScraper):
    """
    Scraper for OpenAI jobs (powered by AshbyHQ ATS).

    OpenAI's main careers page (``openai.com/careers/search/``) is protected
    by Cloudflare Turnstile and cannot be scraped headlessly.  Instead, we
    use the AshbyHQ public board at ``jobs.ashbyhq.com/openai`` which is
    accessible without anti-bot challenges.

    All jobs are parsed from the listing; India-based roles are filtered
    client-side before detail-page enrichment.

    Expected listing structure:

        h2.ashby-department-heading          (department name)
        div.ashby-job-posting-brief-list
          a[href="/openai/{uuid}"]
            div.ashby-job-posting-brief
              h3.ashby-job-posting-brief-title     (job title)
              div.ashby-job-posting-brief-details   (location + metadata)

    Expected detail page structure:

        div._descriptionText_5yu8i_201        (full rich-text description)
    """

    CARD_SELECTOR = 'div.ashby-job-posting-brief-list a[href*="/openai/"]'

    JOB_CARD_SELECTORS = [
        'div.ashby-job-posting-brief-list a[href*="/openai/"]',
        'a[href*="/openai/"][href$="/"]',
        'h3.ashby-job-posting-brief-title',
    ]

    TITLE_SELECTOR = 'h3.ashby-job-posting-brief-title'
    DETAILS_SELECTOR = 'div.ashby-job-posting-brief-details'

    DESCRIPTION_SELECTOR = 'div._descriptionText_5yu8i_201'

    INDIA_KEYWORDS = [
        "india", "bangalore", "bengaluru", "mumbai", "delhi",
        "hyderabad", "pune", "chennai", "gurgaon", "gurugram",
        "noida", "remote india",
    ]

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = "https://jobs.ashbyhq.com/openai"
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)

            selector = await self._wait_for_any_selector(page, self.JOB_CARD_SELECTORS)

            if not selector:
                return await self._fallback_links(page, source_url, max_jobs)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                return await self._fallback_links(page, source_url, max_jobs)

            all_jobs: list[Job] = []
            seen_urls: set[str] = set()

            # Phase 1: Parse ALL cards — India filter happens afterward,
            # so we must scan the entire listing (no max_jobs limit here).
            for card in cards:
                job = self._parse_card(card, source_url)

                if not job or not job.url:
                    continue

                if job.url in seen_urls:
                    continue

                seen_urls.add(job.url)
                all_jobs.append(job)

            india_jobs = [j for j in all_jobs if self._is_india_location(j.location)]

            self.logger.info(
                "OpenAI: %d total jobs, %d India matches",
                len(all_jobs),
                len(india_jobs),
            )

            seen_job_ids: set[str] = set()

            for job in india_jobs:
                if job.job_id and job.job_id in seen_job_ids:
                    continue

                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
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
                            "Failed to enrich OpenAI job detail page %s: %s",
                            job.url,
                            exc,
                        )

                if job.job_id:
                    seen_job_ids.add(job.job_id)

                jobs.append(job)

            return jobs

        finally:
            await self.close_browser()

    def _is_india_location(self, location: str) -> bool:
        if not location:
            return False
        loc_lower = location.lower()
        for keyword in self.INDIA_KEYWORDS:
            if keyword in loc_lower:
                return True
        return False

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
        location = self._extract_location(card)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "OpenAI"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag, source_url: str) -> str:
        href = card.get("href")
        if not href:
            return ""
        return self._make_job_url(source_url, str(href))

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        if el:
            return self._clean_text(el.get_text())
        h3 = card.find("h3")
        if h3:
            return self._clean_text(h3.get_text())
        return ""

    def _extract_location(self, card: Tag) -> str:
        """
        Location is inside the details div, formatted as:
        "Department • City, Country • Employment Type • ..."

        The department is always first; location appears after it.
        Employment type ("Full time", "Part time") and salary ("$...") follow.
        """
        details = card.select_one(self.DETAILS_SELECTOR)
        if not details:
            return ""

        full_text = self._clean_text(details.get_text())

        if not full_text:
            return ""

        # Split by bullet separator
        parts = [p.strip() for p in full_text.split("•")]

        if len(parts) < 2:
            return full_text

        # Skip first part (department), collect location parts
        location_parts = []
        for part in parts[1:]:
            part_lower = part.lower()

            # Stop at employment type or salary
            if any(
                kw in part_lower
                for kw in ["full time", "part time", "contract", "intern",
                          "temporary", "full-time", "part-time", "$"]
            ):
                break

            location_parts.append(part)

        if location_parts:
            return " • ".join(location_parts)

        # Fallback: return everything after department
        return " • ".join(parts[1:])

    def _extract_job_id(self, card: Tag, link: str) -> str:
        if link:
            parsed = urlparse(link)
            uuid_match = re.search(
                r"/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
                parsed.path,
            )
            if uuid_match:
                return uuid_match.group(1)
        return ""

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
        detail_page = await self._get_detail_page()
        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)
            await detail_page.wait_for_timeout(4000)

            await self._wait_for_any_selector(
                detail_page,
                [self.DESCRIPTION_SELECTOR, 'h1', 'article', 'div[class*="description"]'],
            )

            soup = await self._get_soup(detail_page)
            detail_data: dict[str, str] = {}
            description = self._extract_description(soup)
            if description:
                detail_data["description"] = description
            return detail_data
        finally:
            await detail_page.close()

    def _extract_description(self, soup) -> str:
        container = soup.select_one(self.DESCRIPTION_SELECTOR)
        if not container:
            for cls_pattern in ['description', 'Description', 'job-description', 'posting-body']:
                container = soup.select_one(f'div[class*="{cls_pattern}"]')
                if container:
                    break
        if not container:
            return ""

        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    def _format_detail_metadata(self, detail_data: dict[str, str]) -> str:
        return ""

    def _join_description_parts(self, *parts: str) -> str:
        cleaned_parts = [part.strip() for part in parts if part and part.strip()]
        return "\n\n".join(cleaned_parts)

    def _make_job_url(self, source_url: str, href: str) -> str:
        href = html.unescape(href).strip()
        if href.startswith("http://") or href.startswith("https://"):
            return href
        origin = "https://jobs.ashbyhq.com"
        if href.startswith("/"):
            return f"{origin}{href}"
        return make_absolute_url(source_url, href)

    async def _fallback_links(self, page: Page, source_url: str, max_jobs: int) -> list[Job]:
        soup = await self._get_soup(page)
        jobs: list[Job] = []
        seen_urls: set[str] = set()

        for link in soup.select('a[href*="/openai/"]'):
            if len(jobs) >= max_jobs:
                break
            href = link.get("href")
            if not href or "/openai/" not in str(href):
                continue
            url = self._make_job_url(source_url, str(href))
            if url in seen_urls:
                continue
            seen_urls.add(url)

            title = ""
            title_el = link.select_one("h3, h2, [class*='title']")
            if title_el:
                title = self._clean_text(title_el.get_text())
            if not title:
                title = self._clean_text(link.get_text())

            job_id = self._extract_job_id(link, url)
            jobs.append(Job(
                job_id=job_id,
                company=self.company_config.get("name", "OpenAI"),
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

    @staticmethod
    def _clean_text(text: str) -> str:
        if not text:
            return ""
        text = html.unescape(text)
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        if not text:
            return ""
        text = html.unescape(text)
        text = text.replace("\xa0", " ")
        lines = []
        for line in text.splitlines():
            clean_line = OpenAIScraper._clean_text(line)
            if clean_line:
                lines.append(clean_line)
        return "\n".join(lines)
