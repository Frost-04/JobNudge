from __future__ import annotations

import logging
from typing import Any

from src.scrapers.airbnb_scraper import AirbnbScraper
from src.scrapers.american_express_scraper import AmericanExpressScraper
from src.scrapers.apple_scraper import AppleScraper
from src.scrapers.arcesium_scraper import ArcesiumScraper
from src.scrapers.arista_scraper import AristaScraper
from src.scrapers.arm_scraper import ArmScraper
from src.scrapers.blackrock_scraper import BlackRockScraper
from src.scrapers.bloomberg_scraper import BloombergScraper
from src.scrapers.booking_holdings_scraper import BookingHoldingsScraper
from src.scrapers.cerebras_scraper import CerebrasScraper
from src.scrapers.cisco_scraper import CiscoScraper
from src.scrapers.mediatek_scraper import MediatekScraper
from src.scrapers.amd_scraper import AmdScraper
from src.scrapers.amazon_scraper import AmazonScraper
from src.scrapers.base_scraper import BaseScraper
from src.scrapers.cohesity_scraper import CohesityScraper
from src.scrapers.ciena_scraper import CienaScraper
from src.scrapers.concert_ai_scraper import ConcertAIScraper
from src.scrapers.goldman_sachs_scraper import GoldmanSachsScraper
from src.scrapers.google_scraper import GoogleScraper
from src.scrapers.harness_scraper import HarnessScraper
from src.scrapers.hpe_scraper import HPEScraper
from src.scrapers.ibm_scraper import IbmScraper
from src.scrapers.infineon_scraper import InfineonScraper
from src.scrapers.inmobi_scraper import InmobiScraper
from src.scrapers.intel_scraper import IntelScraper
from src.scrapers.intuit_scraper import IntuitScraper
from src.scrapers.ixigo_scraper import IxigoScraper
from src.scrapers.jpmorganchase_scraper import JPMorganChaseScraper
from src.scrapers.kotak_scraper import KotakScraper
from src.scrapers.linkedin_scraper import LinkedInScraper
from src.scrapers.lowes_scraper import LowesScraper
from src.scrapers.makemytrip_scraper import MakemytripScraper
from src.scrapers.meesho_scraper import MeeshoScraper
from src.scrapers.mastercard_scraper import MastercardScraper
from src.scrapers.meta_scraper import MetaScraper
from src.scrapers.mercari_scraper import MercariScraper
from src.scrapers.merck_scraper import MerckScraper
from src.scrapers.microsoft_scraper import MicrosoftScraper
from src.scrapers.netapp_scraper import NetAppScraper
from src.scrapers.notion_scraper import NotionScraper
from src.scrapers.nutanix_scraper import NutanixScraper
from src.scrapers.nvidia_scraper import NvidiaScraper
from src.scrapers.omnissa_scraper import OmnissaScraper
from src.scrapers.optum_scraper import OptumScraper
from src.scrapers.outmarket_ai_scraper import OutmarketAIScraper
from src.scrapers.publicis_sapient_scraper import PublicisSapientScraper
from src.scrapers.palo_alto_scraper import PaloAltoScraper
from src.scrapers.oracle_scraper import OracleScraper
from src.scrapers.qualcomm_scraper import QualcommScraper
from src.scrapers.deel_scraper import DeelScraper
from src.scrapers.dell_scraper import DellScraper
from src.scrapers.dp_world_scraper import DPWorldScraper
from src.scrapers.docusign_scraper import DocusignScraper
from src.scrapers.ebay_scraper import EbayScraper
from src.scrapers.ericsson_scraper import EricssonScraper
from src.scrapers.everpure_scraper import EverpureScraper
from src.scrapers.expedia_scraper import ExpediaScraper
from src.scrapers.exl_scraper import EXLScraper
from src.scrapers.quince_scraper import QuinceScraper
from src.scrapers.redhat_scraper import RedhatScraper
from src.scrapers.rippling_scraper import RipplingScraper
from src.scrapers.rubrik_scraper import RubrikScraper
from src.scrapers.salesforce_scraper import SalesforceScraper
from src.scrapers.scapia_scraper import ScapiaScraper
from src.scrapers.samsung_scraper import SamsungScraper
from src.scrapers.sandisk_scraper import SandiskScraper
from src.scrapers.siemens_scraper import SiemensScraper
from src.scrapers.western_digital_scraper import WesternDigitalScraper
from src.scrapers.servicenow_scraper import ServiceNowScraper
from src.scrapers.stripe_scraper import StripeScraper
from src.scrapers.synopsys_scraper import SynopsysScraper
from src.scrapers.target_scraper import TargetScraper
from src.scrapers.tesco_scraper import TescoScraper
from src.scrapers.thoughtspot_scraper import ThoughtspotScraper
from src.scrapers.twilio_scraper import TwilioScraper
from src.scrapers.uipath_scraper import UiPathScraper
from src.scrapers.visa_scraper import VisaScraper
from src.scrapers.wells_fargo_scraper import WellsFargoScraper


def get_scraper(
    company_config: dict[str, Any], settings: dict[str, Any]
) -> BaseScraper | None:
    scraper_name = str(company_config.get("scraper", "")).lower()

    if scraper_name == "airbnb":
        return AirbnbScraper(company_config, settings)
    if scraper_name == "apple":
        return AppleScraper(company_config, settings)
    if scraper_name == "american_express":
        return AmericanExpressScraper(company_config, settings)
    if scraper_name == "bloomberg":
        return BloombergScraper(company_config, settings)
    if scraper_name == "blackrock":
        return BlackRockScraper(company_config, settings)
    if scraper_name == "booking_holdings":
        return BookingHoldingsScraper(company_config, settings)
    if scraper_name == "cerebras":
        return CerebrasScraper(company_config, settings)
    if scraper_name == "cisco":
        return CiscoScraper(company_config, settings)
    if scraper_name == "mediatek":
        return MediatekScraper(company_config, settings)
    if scraper_name == "arcesium":
        return ArcesiumScraper(company_config, settings)
    if scraper_name == "arista":
        return AristaScraper(company_config, settings)
    if scraper_name == "arm":
        return ArmScraper(company_config, settings)
    if scraper_name == "cohesity":
        return CohesityScraper(company_config, settings)
    if scraper_name == "ciena":
        return CienaScraper(company_config, settings)
    if scraper_name == "concert_ai":
        return ConcertAIScraper(company_config, settings)
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
    if scraper_name == "lowes":
        return LowesScraper(company_config, settings)
    if scraper_name == "meesho":
        return MeeshoScraper(company_config, settings)
    if scraper_name == "makemytrip":
        return MakemytripScraper(company_config, settings)
    if scraper_name == "mastercard":
        return MastercardScraper(company_config, settings)
    if scraper_name == "meta":
        return MetaScraper(company_config, settings)
    if scraper_name == "mercari":
        return MercariScraper(company_config, settings)
    if scraper_name == "merck":
        return MerckScraper(company_config, settings)
    if scraper_name == "netapp":
        return NetAppScraper(company_config, settings)
    if scraper_name == "salesforce":
        return SalesforceScraper(company_config, settings)
    if scraper_name == "scapia":
        return ScapiaScraper(company_config, settings)
    if scraper_name == "samsung":
        return SamsungScraper(company_config, settings)
    if scraper_name == "sandisk":
        return SandiskScraper(company_config, settings)
    if scraper_name == "western_digital":
        return WesternDigitalScraper(company_config, settings)
    if scraper_name == "oracle":
        return OracleScraper(company_config, settings)
    if scraper_name == "servicenow":
        return ServiceNowScraper(company_config, settings)
    if scraper_name == "harness":
        return HarnessScraper(company_config, settings)
    if scraper_name == "hpe":
        return HPEScraper(company_config, settings)
    if scraper_name == "ibm":
        return IbmScraper(company_config, settings)
    if scraper_name == "infineon":
        return InfineonScraper(company_config, settings)
    if scraper_name == "inmobi":
        return InmobiScraper(company_config, settings)
    if scraper_name == "intel":
        return IntelScraper(company_config, settings)
    if scraper_name == "intuit":
        return IntuitScraper(company_config, settings)
    if scraper_name == "ixigo":
        return IxigoScraper(company_config, settings)
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
    if scraper_name == "rubrik":
        return RubrikScraper(company_config, settings)
    if scraper_name == "dell":
        return DellScraper(company_config, settings)
    if scraper_name == "deel":
        return DeelScraper(company_config, settings)
    if scraper_name == "dp_world":
        return DPWorldScraper(company_config, settings)
    if scraper_name == "docusign":
        return DocusignScraper(company_config, settings)
    if scraper_name == "ebay":
        return EbayScraper(company_config, settings)
    if scraper_name == "ericsson":
        return EricssonScraper(company_config, settings)
    if scraper_name == "expedia":
        return ExpediaScraper(company_config, settings)
    if scraper_name == "everpure":
        return EverpureScraper(company_config, settings)
    if scraper_name == "exl":
        return EXLScraper(company_config, settings)
    if scraper_name == "nutanix":
        return NutanixScraper(company_config, settings)
    if scraper_name == "notion":
        return NotionScraper(company_config, settings)
    if scraper_name == "omnissa":
        return OmnissaScraper(company_config, settings)
    if scraper_name == "outmarket_ai":
        return OutmarketAIScraper(company_config, settings)
    if scraper_name == "publicis_sapient":
        return PublicisSapientScraper(company_config, settings)
    if scraper_name == "optum":
        return OptumScraper(company_config, settings)
    if scraper_name == "siemens":
        return SiemensScraper(company_config, settings)
    if scraper_name == "stripe":
        return StripeScraper(company_config, settings)
    if scraper_name == "synopsys":
        return SynopsysScraper(company_config, settings)
    if scraper_name == "target":
        return TargetScraper(company_config, settings)
    if scraper_name == "tesco":
        return TescoScraper(company_config, settings)
    if scraper_name == "thoughtspot":
        return ThoughtspotScraper(company_config, settings)
    if scraper_name == "twilio":
        return TwilioScraper(company_config, settings)
    if scraper_name == "uipath":
        return UiPathScraper(company_config, settings)
    if scraper_name == "visa":
        return VisaScraper(company_config, settings)

    if scraper_name == "wells_fargo":
        return WellsFargoScraper(company_config, settings)

    logging.getLogger("job_alert_bot").warning(
        "Unsupported scraper: %s", company_config.get("scraper")
    )
    return None
