from __future__ import annotations

from abc import ABC, abstractmethod
import logging
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
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=headless)
        self.context = await self.browser.new_context()
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
        if self.context:
            await self.context.close()
            self.context = None
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    @abstractmethod
    async def scrape(self) -> list[Job]:
        raise NotImplementedError
