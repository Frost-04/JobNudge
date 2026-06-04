from __future__ import annotations

import logging
from typing import Any

from src.scrapers.amazon_scraper import AmazonScraper
from src.scrapers.base_scraper import BaseScraper
from src.scrapers.goldman_sachs_scraper import GoldmanSachsScraper
from src.scrapers.google_scraper import GoogleScraper
from src.scrapers.intuit_scraper import IntuitScraper
from src.scrapers.jpmorganchase_scraper import JPMorganChaseScraper
from src.scrapers.microsoft_scraper import MicrosoftScraper
from src.scrapers.oracle_scraper import OracleScraper
from src.scrapers.qualcomm_scraper import QualcommScraper
from src.scrapers.salesforce_scraper import SalesforceScraper
from src.scrapers.servicenow_scraper import ServiceNowScraper


def get_scraper(
    company_config: dict[str, Any], settings: dict[str, Any]
) -> BaseScraper | None:
    scraper_name = str(company_config.get("scraper", "")).lower()

    if scraper_name == "google":
        return GoogleScraper(company_config, settings)
    if scraper_name == "amazon":
        return AmazonScraper(company_config, settings)
    if scraper_name == "microsoft":
        return MicrosoftScraper(company_config, settings)
    if scraper_name == "jpmorgan":
        return JPMorganChaseScraper(company_config, settings)
    if scraper_name == "salesforce":
        return SalesforceScraper(company_config, settings)
    if scraper_name == "oracle":
        return OracleScraper(company_config, settings)
    if scraper_name == "servicenow":
        return ServiceNowScraper(company_config, settings)
    if scraper_name == "intuit":
        return IntuitScraper(company_config, settings)
    if scraper_name == "qualcomm":
        return QualcommScraper(company_config, settings)
    if scraper_name == "goldman_sachs":
        return GoldmanSachsScraper(company_config, settings)

    logging.getLogger("job_alert_bot").warning(
        "Unsupported scraper: %s", company_config.get("scraper")
    )
    return None
