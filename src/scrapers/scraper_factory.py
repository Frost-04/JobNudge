from __future__ import annotations

import logging
from typing import Any

from src.scrapers.amazon_scraper import AmazonScraper
from src.scrapers.base_scraper import BaseScraper
from src.scrapers.google_scraper import GoogleScraper


def get_scraper(
    company_config: dict[str, Any], settings: dict[str, Any]
) -> BaseScraper | None:
    scraper_name = str(company_config.get("scraper", "")).lower()

    if scraper_name == "google":
        return GoogleScraper(company_config, settings)
    if scraper_name == "amazon":
        return AmazonScraper(company_config, settings)

    logging.getLogger("job_alert_bot").warning(
        "Unsupported scraper: %s", company_config.get("scraper")
    )
    return None
