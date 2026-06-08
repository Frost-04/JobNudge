from __future__ import annotations

import logging
from typing import Any

from src.scrapers.airbnb_scraper import AirbnbScraper
from src.scrapers.arista_scraper import AristaScraper
from src.scrapers.amd_scraper import AmdScraper
from src.scrapers.amazon_scraper import AmazonScraper
from src.scrapers.base_scraper import BaseScraper
from src.scrapers.cohesity_scraper import CohesityScraper
from src.scrapers.ciena_scraper import CienaScraper
from src.scrapers.goldman_sachs_scraper import GoldmanSachsScraper
from src.scrapers.google_scraper import GoogleScraper
from src.scrapers.ibm_scraper import IbmScraper
from src.scrapers.infineon_scraper import InfineonScraper
from src.scrapers.intel_scraper import IntelScraper
from src.scrapers.intuit_scraper import IntuitScraper
from src.scrapers.jpmorganchase_scraper import JPMorganChaseScraper
from src.scrapers.kotak_scraper import KotakScraper
from src.scrapers.linkedin_scraper import LinkedInScraper
from src.scrapers.meesho_scraper import MeeshoScraper
from src.scrapers.mercari_scraper import MercariScraper
from src.scrapers.merck_scraper import MerckScraper
from src.scrapers.microsoft_scraper import MicrosoftScraper
from src.scrapers.netapp_scraper import NetAppScraper
from src.scrapers.nutanix_scraper import NutanixScraper
from src.scrapers.nvidia_scraper import NvidiaScraper
from src.scrapers.omnissa_scraper import OmnissaScraper
from src.scrapers.optum_scraper import OptumScraper
from src.scrapers.palo_alto_scraper import PaloAltoScraper
from src.scrapers.oracle_scraper import OracleScraper
from src.scrapers.qualcomm_scraper import QualcommScraper
from src.scrapers.dell_scraper import DellScraper
from src.scrapers.docusign_scraper import DocusignScraper
from src.scrapers.ebay_scraper import EbayScraper
from src.scrapers.ericsson_scraper import EricssonScraper
from src.scrapers.exl_scraper import EXLScraper
from src.scrapers.quince_scraper import QuinceScraper
from src.scrapers.redhat_scraper import RedhatScraper
from src.scrapers.rippling_scraper import RipplingScraper
from src.scrapers.salesforce_scraper import SalesforceScraper
from src.scrapers.sandisk_scraper import SandiskScraper
from src.scrapers.servicenow_scraper import ServiceNowScraper
from src.scrapers.stripe_scraper import StripeScraper
from src.scrapers.target_scraper import TargetScraper
from src.scrapers.tesco_scraper import TescoScraper
from src.scrapers.twilio_scraper import TwilioScraper


def get_scraper(
    company_config: dict[str, Any], settings: dict[str, Any]
) -> BaseScraper | None:
    scraper_name = str(company_config.get("scraper", "")).lower()

    if scraper_name == "airbnb":
        return AirbnbScraper(company_config, settings)
    if scraper_name == "arista":
        return AristaScraper(company_config, settings)
    if scraper_name == "cohesity":
        return CohesityScraper(company_config, settings)
    if scraper_name == "ciena":
        return CienaScraper(company_config, settings)
    if scraper_name == "google":
        return GoogleScraper(company_config, settings)
    if scraper_name == "amd":
        return AmdScraper(company_config, settings)
    if scraper_name == "amazon":
        return AmazonScraper(company_config, settings)
    if scraper_name == "microsoft":
        return MicrosoftScraper(company_config, settings)
    if scraper_name == "nvidia":
        return NvidiaScraper(company_config, settings)
    if scraper_name == "palo_alto":
        return PaloAltoScraper(company_config, settings)
    if scraper_name == "jpmorgan":
        return JPMorganChaseScraper(company_config, settings)
    if scraper_name == "kotak":
        return KotakScraper(company_config, settings)
    if scraper_name == "linkedin":
        return LinkedInScraper(company_config, settings)
    if scraper_name == "meesho":
        return MeeshoScraper(company_config, settings)
    if scraper_name == "mercari":
        return MercariScraper(company_config, settings)
    if scraper_name == "merck":
        return MerckScraper(company_config, settings)
    if scraper_name == "netapp":
        return NetAppScraper(company_config, settings)
    if scraper_name == "salesforce":
        return SalesforceScraper(company_config, settings)
    if scraper_name == "sandisk":
        return SandiskScraper(company_config, settings)
    if scraper_name == "oracle":
        return OracleScraper(company_config, settings)
    if scraper_name == "servicenow":
        return ServiceNowScraper(company_config, settings)
    if scraper_name == "ibm":
        return IbmScraper(company_config, settings)
    if scraper_name == "infineon":
        return InfineonScraper(company_config, settings)
    if scraper_name == "intel":
        return IntelScraper(company_config, settings)
    if scraper_name == "intuit":
        return IntuitScraper(company_config, settings)
    if scraper_name == "qualcomm":
        return QualcommScraper(company_config, settings)
    if scraper_name == "goldman_sachs":
        return GoldmanSachsScraper(company_config, settings)
    if scraper_name == "quince":
        return QuinceScraper(company_config, settings)
    if scraper_name == "redhat":
        return RedhatScraper(company_config, settings)
    if scraper_name == "rippling":
        return RipplingScraper(company_config, settings)
    if scraper_name == "dell":
        return DellScraper(company_config, settings)
    if scraper_name == "docusign":
        return DocusignScraper(company_config, settings)
    if scraper_name == "ebay":
        return EbayScraper(company_config, settings)
    if scraper_name == "ericsson":
        return EricssonScraper(company_config, settings)
    if scraper_name == "exl":
        return EXLScraper(company_config, settings)
    if scraper_name == "nutanix":
        return NutanixScraper(company_config, settings)
    if scraper_name == "omnissa":
        return OmnissaScraper(company_config, settings)
    if scraper_name == "optum":
        return OptumScraper(company_config, settings)
    if scraper_name == "stripe":
        return StripeScraper(company_config, settings)
    if scraper_name == "target":
        return TargetScraper(company_config, settings)
    if scraper_name == "tesco":
        return TescoScraper(company_config, settings)
    if scraper_name == "twilio":
        return TwilioScraper(company_config, settings)

    logging.getLogger("job_alert_bot").warning(
        "Unsupported scraper: %s", company_config.get("scraper")
    )
    return None
