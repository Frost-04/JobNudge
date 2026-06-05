from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import extract_job_id, make_absolute_url


class SalesforceScraper(BaseScraper):
    """
    Scraper for Salesforce Careers job search pages.

    Expected search result structure:
        div.jobs-grid-wrapper
        div.card.card-job > div.card-body
        p.card-subtitle
        h3.card-title > a.stretched-link.js-view-job[href*='/company/careers/jobs/']
        div.card-job-actions.js-job[data-id][data-jobtitle]
        ul.locations > li.list-inline-item

    Expected detail page structure:
        article.cms-content
        div.job-description-cms

    Salesforce opens each card as a normal detail page, so this scraper first
    parses the rendered listing page and then enriches each job by navigating
    directly to the job URL and extracting the full description.
    """

    JOB_CARD_SELECTORS = [
        "div.card.card-job",
        "div.card-body",
        "a.js-view-job[href*='/company/careers/jobs/']",
    ]

    CARD_SELECTOR = "div.card.card-job"
    LINK_SELECTOR = "h3.card-title a.stretched-link.js-view-job[href], a.js-view-job[href]"
    TITLE_SELECTOR = "h3.card-title a.stretched-link.js-view-job, a.js-view-job"
    TEAM_SELECTOR = "p.card-subtitle"
    JOB_ACTION_SELECTOR = "div.card-job-actions.js-job"
    LOCATION_SELECTOR = "ul.locations li.list-inline-item"

    DETAIL_WAIT_SELECTORS = [
        "article.cms-content div.job-description-cms",
        "article.cms-content",
        "div.job-description-cms",
    ]
    DETAIL_DESCRIPTION_SELECTOR = "article.cms-content div.job-description-cms, div.job-description-cms, article.cms-content"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))
        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Salesforce careers pages render cards client-side; wait for cards instead of relying only on networkidle.
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

                # Enrich with full detail page description.
                try:
                    detail_data = await self._scrape_detail_page(job.url)
                    detail_description = detail_data.get("description", "")
                    detail_title = detail_data.get("title", "")

                    job = Job(
                        job_id=job.job_id,
                        company=job.company,
                        title=detail_title or job.title,
                        location=job.location,
                        url=job.url,
                        source_url=job.source_url,
                        posted_date=job.posted_date,
                        description=detail_description or job.description,
                        scraped_at=datetime.now(timezone.utc).isoformat(),
                        extracted_experience_parts="",
                    )
                except Exception as exc:
                    self.logger.warning(
                        "Failed to enrich Salesforce job detail page %s: %s",
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
        location = self._extract_location(card)
        team = self._extract_team(card)

        if not link or not title:
            return None

        # Store lightweight listing metadata in description until detail enrichment replaces it.
        short_description = self._format_listing_metadata(team)

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Salesforce"),
            title=title,
            location=location,
            url=link,
            source_url=source_url,
            posted_date=None,
            description=short_description or None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    def _extract_link(self, card: Tag, source_url: str) -> str:
        el = card.select_one(self.LINK_SELECTOR)
        if not el:
            return ""
        href = el.get("href")
        if not href:
            return ""
        return make_absolute_url(source_url, str(href))

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        if el:
            return self._clean_text(el.get_text())

        actions = card.select_one(self.JOB_ACTION_SELECTOR)
        if actions and actions.get("data-jobtitle"):
            return self._clean_text(str(actions.get("data-jobtitle")))

        return ""

    def _extract_job_id(self, card: Tag, link: str) -> str:
        actions = card.select_one(self.JOB_ACTION_SELECTOR)
        if actions and actions.get("data-id"):
            return self._clean_text(str(actions.get("data-id")))

        # Salesforce URLs look like:
        # /company/careers/jobs/JR282676/software-engineering-architect/
        match = re.search(r"/jobs/([^/?#]+)/", link, flags=re.IGNORECASE)
        if match:
            return self._clean_text(match.group(1))

        return extract_job_id(link) if link else ""

    def _extract_location(self, card: Tag) -> str:
        locations: list[str] = []
        for item in card.select(self.LOCATION_SELECTOR):
            text = self._clean_location_text(item.get_text())
            if text:
                locations.append(text)
        return ", ".join(self._dedupe_preserve_order(locations))

    def _extract_team(self, card: Tag) -> str:
        el = card.select_one(self.TEAM_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    async def _get_detail_page(self) -> Page:
        """Return a new page for detail scraping, recreating the browser stack if needed."""
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
        detail_page = await self._get_detail_page()
        try:
            detail_page.set_default_timeout(10000)
            await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)
            await self._wait_for_any_selector(detail_page, self.DETAIL_WAIT_SELECTORS)

            soup = await self._get_soup(detail_page)
            title = self._extract_detail_title(soup)
            description = self._extract_detail_description(soup)

            result: dict[str, str] = {}
            if title:
                result["title"] = title
            if description:
                result["description"] = description
            return result
        finally:
            await detail_page.close()

    def _extract_detail_title(self, soup) -> str:
        # Detail title is usually outside the provided article snippet; support common Salesforce headings.
        selectors = [
            "h1",
            "h2.job-title",
            "h1.job-title",
            "[data-job-title]",
        ]
        for selector in selectors:
            el = soup.select_one(selector)
            if el:
                text = self._clean_text(el.get_text())
                if text and text.lower() not in {"description", "jobs", "careers"}:
                    return text
        return ""

    def _extract_detail_description(self, soup) -> str:
        container = soup.select_one(self.DETAIL_DESCRIPTION_SELECTOR)
        if not container:
            return ""

        for unwanted in container.select("script, style, noscript, svg, button"):
            unwanted.decompose()

        text = container.get_text(separator="\n")
        return self._clean_multiline_text(text)

    def _format_listing_metadata(self, team: str) -> str:
        lines: list[str] = []
        if team:
            lines.append(f"Team: {team}")
        return "\n".join(lines)

    def _clean_location_text(self, text: str) -> str:
        text = self._clean_text(text)
        if not text:
            return ""

        lower_text = text.lower()
        noise_values = {
            "location",
            "locations",
            "save",
            "saved",
            "remove",
        }
        if lower_text in noise_values:
            return ""

        # Sometimes multiple Salesforce locations appear joined with slashes in visible text.
        text = re.sub(r"\s*/\s*", " / ", text)
        return text

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

        lines: list[str] = []
        previous_line = ""
        for line in text.splitlines():
            clean_line = self._clean_text(line)
            if not clean_line:
                continue
            # Avoid consecutive duplicates caused by nested headings/spans.
            if clean_line == previous_line:
                continue
            lines.append(clean_line)
            previous_line = clean_line

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

    async def _fallback_links(
        self,
        page: Page,
        source_url: str,
        max_jobs: int,
    ) -> list[Job]:
        """Fallback for unexpected Salesforce DOM changes."""
        soup = await self._get_soup(page)
        anchors = soup.select("a[href*='/company/careers/jobs/'], a[href*='/careers/jobs/']")

        results: list[Job] = []
        seen_job_ids: set[str] = set()
        seen_urls: set[str] = set()

        for anchor in anchors[:max_jobs]:
            href = anchor.get("href")
            if not href:
                continue

            job_url = make_absolute_url(source_url, str(href))
            job_id = self._extract_salesforce_job_id_from_url(job_url)

            if job_id and job_id in seen_job_ids:
                continue
            if job_url in seen_urls:
                continue

            card = anchor.find_parent("div", class_=lambda c: c and "card-job" in c)
            title = self._clean_text(anchor.get_text())
            location = ""
            description = ""

            if card:
                title = self._extract_title(card) or title
                location = self._extract_location(card)
                description = self._format_listing_metadata(self._extract_team(card))
                card_job_id = self._extract_job_id(card, job_url)
                if card_job_id:
                    job_id = card_job_id

            if not title:
                continue

            if job_id:
                seen_job_ids.add(job_id)
            seen_urls.add(job_url)

            results.append(
                Job(
                    job_id=job_id,
                    company=self.company_config.get("name", "Salesforce"),
                    title=title,
                    location=location,
                    url=job_url,
                    source_url=source_url,
                    posted_date=None,
                    description=description or None,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    extracted_experience_parts="",
                )
            )

        return results

    def _extract_salesforce_job_id_from_url(self, url: str) -> str:
        if not url:
            return ""
        match = re.search(r"/jobs/([^/?#]+)/", url, flags=re.IGNORECASE)
        if match:
            return self._clean_text(match.group(1))
        return extract_job_id(url) or ""
