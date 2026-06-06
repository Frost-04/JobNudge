from __future__ import annotations

from abc import ABC, abstractmethod
import logging
import re
from typing import Any

from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Locator, Page, async_playwright

from src.models.job import Job


class BaseScraper(ABC):
    def __init__(self, company_config: dict[str, Any], settings: dict[str, Any]) -> None:
        self.company_config = company_config
        self.settings = settings
        self.playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.logger = logging.getLogger("job_alert_bot")

    def _to_ms(self, value: float | int | None, default_ms: int) -> int:
        if value is None:
            return default_ms
        if value > 1000:
            return int(value)
        return int(value * 1000)

    async def create_browser_context(self) -> BrowserContext:
        if self.context:
            return self.context

        run_settings = self.settings.get("run", {})
        headless = bool(run_settings.get("headless", True))
        browser_channel = run_settings.get("browser_channel", "chrome")

        self.playwright = await async_playwright().start()

        launch_kwargs: dict = {
            "headless": headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        }

        # Use the installed Chrome channel for better site compatibility (e.g. Salesforce).
        # Falls back to bundled Chromium if the channel is unavailable.
        if browser_channel:
            try:
                self.browser = await self.playwright.chromium.launch(
                    channel=browser_channel, **launch_kwargs
                )
            except Exception:
                self.logger.warning(
                    "Chrome channel '%s' unavailable, falling back to bundled Chromium",
                    browser_channel,
                )
                self.browser = await self.playwright.chromium.launch(**launch_kwargs)
        else:
            self.browser = await self.playwright.chromium.launch(**launch_kwargs)
        self.context = await self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/142.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        # Mask navigator.webdriver to avoid bot detection.
        await self.context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            """
        )
        return self.context

    async def new_page(self) -> Page:
        context = await self.create_browser_context()
        page = await context.new_page()

        run_settings = self.settings.get("run", {})
        request_timeout_ms = self._to_ms(run_settings.get("request_timeout_seconds"), 30000)
        page_load_timeout_ms = self._to_ms(run_settings.get("page_load_timeout_seconds"), 45000)
        page.set_default_timeout(request_timeout_ms)
        page.set_default_navigation_timeout(page_load_timeout_ms)

        return page

    async def safe_get_text(self, locator: Locator) -> str:
        try:
            text = await locator.inner_text()
            return text.strip()
        except Exception:
            return ""

    async def _get_soup(self, page: Page) -> BeautifulSoup:
        """Parse the current page content into a BeautifulSoup object.

        Use this after Playwright has finished waiting for dynamic content
        so all HTML extraction happens in-process via BS4 selectors instead
        of making many round-trip Playwright locator calls.
        """
        html_content = await page.content()
        return BeautifulSoup(html_content, "html.parser")

    async def close_browser(self) -> None:
        """Tear down context → browser → playwright.

        Each step is wrapped so a failure in one (e.g. a context that was
        already closed by a navigation error) does not prevent the remaining
        resources from being released.
        """
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None

    # ------------------------------------------------------------------
    # Title exclusion — skip detail-page enrichment for senior-level roles
    # to reduce network load and speed up scraping.
    # ------------------------------------------------------------------

    EXCLUDE_TITLE_WORDS: list[str] = [
        "principal",
        "senior",
        "iii",
        "staff",
        "sr.",
        "sr",
        "lead",
    ]

    def _should_exclude(self, title: str) -> bool:
        """Return True if *title* contains a word that marks it as a
        senior / staff / principal role whose detail page we should skip."""
        if not title:
            return False
        title_lower = title.lower()
        for word in self.EXCLUDE_TITLE_WORDS:
            if re.search(r"\b" + re.escape(word) + r"\b", title_lower):
                return True
        return False

    # ------------------------------------------------------------------

    @abstractmethod
    async def scrape(self) -> list[Job]:
        raise NotImplementedError
