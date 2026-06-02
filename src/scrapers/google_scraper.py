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


class GoogleScraper(BaseScraper):
    """
    Scraper for Google Careers search result pages.

    Expected card structure:

    ul.spHGqe
      li.lLd3Je
        div[jsdata="Aiqs8c;{job_id};$..."]
          h3.QJPWVe
          div.wVoYLb span.pwO9Dc.vo5qdf span.r0wTof
          div.Xsxa1e
          a.WpHeLc[href*='jobs/results/']
    """

    JOB_CARD_SELECTORS = [
        "ul.spHGqe > li.lLd3Je",
        "li.lLd3Je",
        "a[href*='jobs/results/']",
    ]

    TITLE_SELECTOR = "h3.QJPWVe"
    LINK_SELECTOR = "a.WpHeLc[href*='jobs/results/'], a[href*='jobs/results/']"
    DESCRIPTION_SELECTOR = "div.Xsxa1e"

    # Restrict to wVoYLb to avoid duplicate location text from EAcu5e.
    LOCATION_ITEM_SELECTOR = "div.wVoYLb span.pwO9Dc.vo5qdf span.r0wTof"

    CITY_HINTS = [
        "india",
        "bengaluru",
        "bangalore",
        "hyderabad",
        "pune",
        "chennai",
        "gurgaon",
        "gurugram",
        "noida",
        "mumbai",
        "delhi",
        "remote",
    ]

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        page.set_default_timeout(5000)

        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))
        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            # Do not use networkidle for Google Careers.
            # Google pages often keep background network activity open.
            selector = await self._wait_for_any_selector(page, self.JOB_CARD_SELECTORS)

            if not selector:
                jobs = await self._fallback_links(page, source_url, max_jobs)
                return jobs

            # If selector matched anchors, fallback is more appropriate.
            if selector == "a[href*='jobs/results/']":
                jobs = await self._fallback_links(page, source_url, max_jobs)
                return jobs

            # Parse the fully-rendered page with BeautifulSoup.
            soup = await self._get_soup(page)

            # Try selectors in priority order.
            cards = soup.select("ul.spHGqe > li.lLd3Je")
            if not cards:
                cards = soup.select("li.lLd3Je")

            if cards:
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
        # This timeout is per selector, so keep it short.
        timeout_ms = 7000

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
        posted_date = self._extract_posted_date(card)
        description = self._extract_description(card)

        if not title or not location:
            card_text = card.get_text()

            if not title:
                title = self._guess_title(card_text)

            if not location:
                location = self._guess_location(card_text)

        if not posted_date:
            card_text = card.get_text()
            posted_date = self._guess_posted_date(card_text)

        if not link or not title:
            return None

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Google"),
            title=title,
            location=location or "",
            url=link,
            source_url=source_url,
            posted_date=posted_date or None,
            description=description or None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            matched_keywords=[],
        )

    def _extract_job_id(self, card: Tag, link: str) -> str:
        """
        Google exposes the job id in jsdata:

            jsdata="Aiqs8c;104752603035771590;$15"

        It is also present in the job URL:

            jobs/results/104752603035771590-software-engineer-ii
        """

        node = card.select_one("[jsdata^='Aiqs8c;']")
        if node:
            jsdata = node.get("jsdata")
            if jsdata:
                match = re.search(r"Aiqs8c;(\d+);", str(jsdata))
                if match:
                    return match.group(1)

        return self._extract_google_job_id_from_url(link)

    def _extract_title(self, card: Tag) -> str:
        el = card.select_one(self.TITLE_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _extract_link(self, card: Tag, source_url: str) -> str:
        el = card.select_one(self.LINK_SELECTOR)
        if not el:
            return ""

        href = el.get("href")
        if not href:
            return ""

        return self._make_google_job_url(source_url, str(href))

    def _extract_location(self, card: Tag) -> str:
        locations: list[str] = []

        for item in card.select(self.LOCATION_ITEM_SELECTOR):
            text = self._clean_location_text(item.get_text())
            if text:
                locations.append(text)

        unique_locations = self._dedupe_preserve_order(locations)
        return ", ".join(unique_locations)

    def _extract_posted_date(self, card: Tag) -> str:
        """
        Google result cards in the provided HTML do not expose posted date.

        Avoid broad :has-text selectors here because they are slow on Google's
        large dynamic DOM.
        """
        return ""

    def _extract_description(self, card: Tag) -> str:
        el = card.select_one(self.DESCRIPTION_SELECTOR)
        return self._clean_text(el.get_text() if el else "")

    def _make_google_job_url(self, source_url: str, href: str) -> str:
        """
        In your HTML, Google job href is relative:

            jobs/results/104752603035771590-software-engineer-ii?...

        Correct full URL should be:

            https://www.google.com/about/careers/applications/jobs/results/...
        """

        href = html.unescape(href).strip()

        if href.startswith("http://") or href.startswith("https://"):
            return href

        parsed_source = urlparse(source_url)
        origin = f"{parsed_source.scheme}://{parsed_source.netloc}"

        if href.startswith("/about/careers/applications/"):
            return f"{origin}{href}"

        if href.startswith("about/careers/applications/"):
            return f"{origin}/{href}"

        if href.startswith("/jobs/results/"):
            return f"{origin}/about/careers/applications{href}"

        if href.startswith("jobs/results/"):
            return f"{origin}/about/careers/applications/{href}"

        return make_absolute_url(source_url, href)

    def _extract_google_job_id_from_url(self, url: str) -> str:
        if not url:
            return ""

        match = re.search(
            r"/jobs/results/(\d+)(?:[-/?#]|$)",
            url,
            flags=re.IGNORECASE,
        )

        if match:
            return match.group(1)

        job_id = extract_job_id(url)
        return job_id or ""

    def _guess_title(self, text: str) -> str:
        lines = self._split_lines(text)

        for line in lines:
            clean_line = self._clean_text(line)

            if not clean_line:
                continue

            if self._looks_like_noise(clean_line):
                continue

            if self._looks_like_location(clean_line):
                continue

            if self._looks_like_posted_date(clean_line):
                continue

            return clean_line

        return ""

    def _guess_location(self, text: str) -> str:
        lines = self._split_lines(text)
        locations: list[str] = []

        for line in lines:
            clean_line = self._clean_location_text(line)

            if clean_line and self._looks_like_location(clean_line):
                locations.append(clean_line)

        return ", ".join(self._dedupe_preserve_order(locations))

    def _guess_posted_date(self, text: str) -> str:
        lines = self._split_lines(text)

        for line in lines:
            clean_line = self._clean_text(line)

            if self._looks_like_posted_date(clean_line):
                return clean_line

        return ""

    def _clean_location_text(self, text: str) -> str:
        text = self._clean_text(text)

        if not text:
            return ""

        # Google sometimes renders second location as "; Pune, Maharashtra, India"
        text = text.lstrip(";").strip()

        if not text:
            return ""

        if self._looks_like_noise(text):
            return ""

        if self._looks_like_posted_date(text):
            return ""

        return text

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""

        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _split_lines(self, text: str) -> list[str]:
        if not text:
            return []

        return [self._clean_text(line) for line in text.splitlines() if self._clean_text(line)]

    def _looks_like_location(self, text: str) -> bool:
        lower_text = text.lower()

        if self._looks_like_noise(text):
            return False

        if self._looks_like_posted_date(text):
            return False

        return any(city in lower_text for city in self.CITY_HINTS)

    def _looks_like_posted_date(self, text: str) -> bool:
        lower_text = text.lower().strip()

        return (
            lower_text.startswith("posted")
            or lower_text.endswith("ago")
            or " ago" in lower_text
            or bool(re.search(r"\b\d+\s+(day|days|week|weeks|month|months)\b", lower_text))
        )

    def _looks_like_noise(self, text: str) -> bool:
        lower_text = text.lower().strip()

        noise_values = {
            "",
            "|",
            "google",
            "learn more",
            "copy link",
            "email a friend",
            "share",
            "bookmark",
            "jobs search results",
            "turn on job alerts",
            "turn on job alerts for this search",
        }

        if lower_text in noise_values:
            return True

        if lower_text.startswith("minimum qualifications"):
            return True

        if lower_text.startswith("preferred qualifications"):
            return True

        if lower_text.startswith("experience completing work"):
            return True

        if lower_text.startswith("showing "):
            return True

        return False

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

    async def _fallback_links(self, page: Page, source_url: str, max_jobs: int) -> list[Job]:
        """Fallback: scan the full page DOM for job result links via BS4."""

        soup = await self._get_soup(page)
        anchors = soup.select("a[href*='jobs/results/']")

        results: list[Job] = []
        seen_job_ids: set[str] = set()
        seen_urls: set[str] = set()

        for anchor in anchors[:max_jobs]:
            href = anchor.get("href")
            if not href:
                continue

            job_url = self._make_google_job_url(source_url, str(href))
            job_id = self._extract_google_job_id_from_url(job_url)

            if job_id and job_id in seen_job_ids:
                continue

            if job_url in seen_urls:
                continue

            title = ""
            location = ""
            posted_date = ""
            description = ""

            # Walk up to the parent li.lLd3Je card for richer extraction.
            card = anchor.find_parent("li", class_="lLd3Je")
            if card:
                title = self._extract_title(card)
                location = self._extract_location(card)
                posted_date = self._extract_posted_date(card)
                description = self._extract_description(card)

                card_job_id = self._extract_job_id(card, job_url)
                if card_job_id:
                    job_id = card_job_id

                parent_text = ""
                if not title or not location:
                    parent_text = card.get_text()

                if not title:
                    title = self._guess_title(parent_text)

                if not location:
                    location = self._guess_location(parent_text)

                if not posted_date:
                    posted_date = self._guess_posted_date(parent_text)
            else:
                title = self._clean_text(anchor.get_text())

            if not title:
                continue

            if job_id:
                seen_job_ids.add(job_id)

            seen_urls.add(job_url)

            results.append(
                Job(
                    job_id=job_id,
                    company=self.company_config.get("name", "Google"),
                    title=title,
                    location=location or "",
                    url=job_url,
                    source_url=source_url,
                    posted_date=posted_date or None,
                    description=description or None,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    matched_keywords=[],
                )
            )

        return results