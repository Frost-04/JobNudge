from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import Tag
from playwright.async_api import Page

from src.models.job import Job
from src.scrapers.base_scraper import BaseScraper
from src.utils.url_utils import make_absolute_url


class EAScraper(BaseScraper):
    """
    Scraper for Electronic Arts Careers (Avature platform) job search pages.

    Expected search card structure:

        article.article--result
          div.article__header
            div.article__header__text
              h3.article__header__text__title
                a.link_result[href]       (absolute detail URL)
              div.article__header__text__subtitle
                span.list-item-location    ("Hyderabad, India")
                span.list-item-id          ("Role ID 214248")
                span.list-item-workerType
                span.list-item-department

    Expected detail page structure:

        div.article__content__view__field
          div.article__content__view__field__value   (rich HTML description)
    """

    # ---- Card selectors ----
    CARD_SELECTOR = "article.article--result"
    TITLE_SELECTOR = "h3.article__header__text__title a.link_result"
    LOCATION_SELECTOR = "span.list-item-location"
    JOB_ID_SELECTOR = "span.list-item-id"

    # ---- Detail page selectors ----
    DETAIL_CONTAINER = "div.article__content"

    async def scrape(self) -> list[Job]:
        page = await self.new_page()
        source_url = self.company_config.get("url", "")
        max_jobs = int(self.settings.get("run", {}).get("max_jobs_per_company", 100))

        jobs: list[Job] = []

        try:
            await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

            await self._wait_for_results(page)

            soup = await self._get_soup(page)

            cards = soup.select(self.CARD_SELECTOR)

            if not cards:
                self.logger.warning("No EA job cards found.")
                return jobs

            seen_ids: set[str] = set()
            seen_urls: set[str] = set()

            for card in cards[:max_jobs]:
                job = self._parse_card(card, source_url)

                if not job:
                    continue

                if job.job_id and job.job_id in seen_ids:
                    continue

                if job.url in seen_urls:
                    continue

                # Enrich with detail page (description + posted date).
                if self._should_exclude(job.title):
                    self.logger.debug("Skipping detail enrichment for: %s", job.title)
                else:
                    try:
                        detail_data = await self._scrape_detail_page(job.url)
                        if detail_data:
                            job = Job(
                                job_id=job.job_id,
                                company=job.company,
                                title=job.title,
                                location=job.location,
                                url=job.url,
                                source_url=job.source_url,
                                posted_date=detail_data.get("posted_date") or job.posted_date,
                                description=detail_data.get("description") or "",
                                scraped_at=datetime.now(timezone.utc).isoformat(),
                                extracted_experience_parts="",
                            )
                    except Exception as exc:
                        self.logger.warning(
                            "Failed to enrich EA job detail %s: %s",
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
    # Card parsing
    # ------------------------------------------------------------------

    def _parse_card(self, card: Tag, source_url: str) -> Job | None:
        link_el = card.select_one(self.TITLE_SELECTOR)

        if not link_el:
            return None

        href = link_el.get("href")

        if not href:
            return None

        url = str(href)

        title = self._clean_text(link_el.get_text())

        if not title:
            return None

        # Job ID from URL path (last numeric segment) or from list-item-id span.
        job_id = self._extract_job_id_from_url(url)

        if not job_id:
            job_id_el = card.select_one(self.JOB_ID_SELECTOR)
            if job_id_el:
                job_id = self._extract_job_id_from_text(
                    self._clean_text(job_id_el.get_text())
                )

        # Location from the list-item-location span.
        location_el = card.select_one(self.LOCATION_SELECTOR)
        location = self._clean_text(location_el.get_text()) if location_el else ""

        return Job(
            job_id=job_id,
            company=self.company_config.get("name", "Electronic Arts"),
            title=title,
            location=location,
            url=url,
            source_url=source_url,
            posted_date=None,
            description=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            extracted_experience_parts="",
        )

    # ------------------------------------------------------------------
    # Detail page enrichment
    # ------------------------------------------------------------------

    async def _get_detail_page(self) -> Page:
        """Return a new page for detail scraping, creating a fresh context if needed."""
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

            # Wait for the detail container to load.
            try:
                await detail_page.wait_for_selector(
                    self.DETAIL_CONTAINER,
                    timeout=15000,
                )
            except Exception:
                pass

            soup = await self._get_soup(detail_page)

            detail_data: dict[str, str] = {}

            # Extract posted date from metadata field pairs.
            for label_name in ("Posted since", "Date", "Posting date"):
                posted = self._extract_metadata(soup, label_name)
                if posted:
                    detail_data["posted_date"] = posted
                    break

            # Extract description from all field values, skipping boilerplate.
            detail_data["description"] = self._extract_description_from_fields(soup)

            return detail_data

        finally:
            await detail_page.close()

    def _extract_metadata(self, soup: Tag, label: str) -> str:
        """Extract a metadata value by its label text from detail field pairs."""
        for field in soup.select("div.article__content__view__field"):
            label_el = field.select_one("div.article__content__view__field__label")
            if label_el:
                label_text = self._clean_text(label_el.get_text())
                if label_text.lower() == label.lower():
                    value_el = field.select_one("div.article__content__view__field__value")
                    if value_el:
                        return self._clean_text(value_el.get_text())
        return ""

    def _extract_description_from_fields(self, soup: Tag) -> str:
        """
        Extract the job description from Avature detail page fields.

        The detail page has multiple field__value divs. The first is typically
        a company intro blurb, and later ones contain the actual job description
        with rich HTML (p, ul, li, etc.). We skip any field whose content is
        short boilerplate and concatenate the rest.
        """
        all_values = soup.select("div.article__content__view__field__value")

        description_parts: list[str] = []

        for value_el in all_values:
            text = self._clean_text(value_el.get_text())
            if not text:
                continue

            # Skip short boilerplate snippets (company tagline, empty fields).
            if len(text) < 100:
                # Short text might be a heading — keep if it looks like a section title.
                if len(text) > 15:
                    description_parts.append(text)
                continue

            # For rich HTML content, run structured extraction.
            rich_text = self._extract_rich_description(value_el)
            if rich_text:
                description_parts.append(rich_text)

        return "\n\n".join(description_parts)

    def _extract_rich_description(self, container: Tag) -> str:
        """Extract clean description text from a rich-text Avature field."""
        # Remove script/style tags.
        for unwanted in container.select("script, style, noscript"):
            unwanted.decompose()

        sections: list[str] = []

        for child in container.children:
            if not hasattr(child, "name"):
                continue

            tag_name = child.name

            if tag_name in ("h1", "h2", "h3", "h4"):
                heading = self._clean_text(child.get_text())
                if heading:
                    sections.append(heading)
            elif tag_name in ("p", "ul", "ol"):
                text = self._clean_multiline_text(child.get_text())
                if text:
                    sections.append(text)
            elif tag_name == "div":
                inner = self._extract_div_contents(child)
                if inner:
                    sections.append(inner)
            else:
                text = self._clean_text(child.get_text())
                if text:
                    sections.append(text)

        return "\n\n".join(sections)

    def _extract_div_contents(self, container: Tag) -> str:
        """Recurse into div children, collecting structured text."""
        parts: list[str] = []

        for child in container.children:
            if not hasattr(child, "name"):
                if child.string:
                    text = self._clean_text(str(child.string))
                    if text:
                        parts.append(text)
                continue

            tag_name = child.name

            if tag_name in ("strong", "b"):
                label = self._clean_text(child.get_text())
                if label:
                    parts.append(label)
            elif tag_name == "br":
                if parts and parts[-1] != "\n":
                    parts.append("\n")
            elif tag_name in ("ul", "ol"):
                items: list[str] = []
                for li in child.select("li"):
                    li_text = self._clean_multiline_text(li.get_text())
                    if li_text:
                        items.append(f"- {li_text}")
                if items:
                    parts.append("\n".join(items))
            elif tag_name == "div":
                inner = self._extract_div_contents(child)
                if inner:
                    parts.append(inner)
            elif tag_name == "a":
                link_text = self._clean_text(child.get_text())
                href = child.get("href", "")
                if link_text:
                    if href and not href.startswith("#"):
                        parts.append(f"{link_text} ({href})")
                    else:
                        parts.append(link_text)
            else:
                text = self._clean_text(child.get_text())
                if text:
                    parts.append(text)

        result = " ".join(
            p for p in parts if p != "\n"
        )

        # Re-inject line breaks where <br> tags were found.
        parts_with_breaks: list[str] = []
        for p in parts:
            if p == "\n":
                if parts_with_breaks:
                    parts_with_breaks.append("\n\n")
            else:
                parts_with_breaks.append(p)

        return "".join(parts_with_breaks).strip()

    # ------------------------------------------------------------------
    # Job ID extraction
    # ------------------------------------------------------------------

    def _extract_job_id_from_url(self, url: str) -> str:
        """
        Extract job ID from Avature detail URL:

        https://jobs.ea.com/en_US/careers/JobDetail/SRE-III/214248
        """
        if not url:
            return ""

        # Last path segment is the numeric job ID.
        match = re.search(r"/(\d+)(?:\?|$)", url)

        if match:
            return match.group(1)

        return ""

    def _extract_job_id_from_text(self, text: str) -> str:
        """
        Extract job ID from text like "Role ID 214248".
        """
        if not text:
            return ""

        match = re.search(r"(\d+)", text)

        if match:
            return match.group(1)

        return ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _wait_for_results(self, page: Page) -> None:
        """Wait for the job results container or first card to appear."""
        selectors = [
            "div.results--listed",
            self.CARD_SELECTOR,
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

    @staticmethod
    def _clean_multiline_text(text: str) -> str:
        """Normalize whitespace while preserving line breaks."""
        if not text:
            return ""
        import html as html_mod

        text = html_mod.unescape(text)
        text = text.replace("\xa0", " ")
        lines = [" ".join(line.split()).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line)
