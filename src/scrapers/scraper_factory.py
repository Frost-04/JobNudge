from __future__ import annotations

import logging
from typing import Any

from src.scrapers.adobe_scraper import AdobeScraper
from src.scrapers.agoda_scraper import AgodaScraper
from src.scrapers.airbnb_scraper import AirbnbScraper
from src.scrapers.akamai_scraper import AkamaiScraper
from src.scrapers.american_express_scraper import AmericanExpressScraper
from src.scrapers.apple_scraper import AppleScraper
from src.scrapers.arcesium_scraper import ArcesiumScraper
from src.scrapers.arista_scraper import AristaScraper
from src.scrapers.arm_scraper import ArmScraper
from src.scrapers.atlan_scraper import AtlanScraper
from src.scrapers.atlassian_scraper import AtlassianScraper
from src.scrapers.bcg_scraper import BCGScraper
from src.scrapers.chubb_scraper import ChubbScraper
from src.scrapers.blackrock_scraper import BlackRockScraper
from src.scrapers.bloomreach_scraper import BloomreachScraper
from src.scrapers.bloomberg_scraper import BloombergScraper
from src.scrapers.browserstack_scraper import BrowserstackScraper
from src.scrapers.booking_holdings_scraper import BookingHoldingsScraper
from src.scrapers.broadcom_scraper import BroadcomScraper
from src.scrapers.bytedance_scraper import ByteDanceScraper
from src.scrapers.cadence_scraper import CadenceScraper
from src.scrapers.capco_scraper import CapcoScraper
from src.scrapers.cerebras_scraper import CerebrasScraper
from src.scrapers.clevertap_scraper import CleverTapScraper
from src.scrapers.cloudera_scraper import ClouderaScraper
from src.scrapers.cisco_scraper import CiscoScraper
from src.scrapers.citi_scraper import CitiScraper
from src.scrapers.mediatek_scraper import MediatekScraper
from src.scrapers.amd_scraper import AmdScraper
from src.scrapers.amazon_scraper import AmazonScraper
from src.scrapers.analog_devices_scraper import AnalogDevicesScraper
from src.scrapers.anthropic_scraper import AnthropicScraper
from src.scrapers.base_scraper import BaseScraper
from src.scrapers.cohesity_scraper import CohesityScraper
from src.scrapers.ciena_scraper import CienaScraper
from src.scrapers.concert_ai_scraper import ConcertAIScraper
from src.scrapers.confluent_scraper import ConfluentScraper
from src.scrapers.crowdstrike_scraper import CrowdstrikeScraper
from src.scrapers.db_scraper import DBScraper
from src.scrapers.databricks_scraper import DatabricksScraper
from src.scrapers.glean_scraper import GleanScraper
from src.scrapers.goldman_sachs_scraper import GoldmanSachsScraper
from src.scrapers.google_scraper import GoogleScraper
from src.scrapers.harman_scraper import HarmanScraper
from src.scrapers.harness_scraper import HarnessScraper
from src.scrapers.hevodata_scraper import HevoDataScraper
from src.scrapers.honeywell_scraper import HoneywellScraper
from src.scrapers.hpe_scraper import HPEScraper
from src.scrapers.ibm_scraper import IbmScraper
from src.scrapers.indeed_scraper import IndeedScraper
from src.scrapers.infineon_scraper import InfineonScraper
from src.scrapers.inmobi_scraper import InmobiScraper
from src.scrapers.intel_scraper import IntelScraper
from src.scrapers.intuit_scraper import IntuitScraper
from src.scrapers.ixigo_scraper import IxigoScraper
from src.scrapers.jfrog_scraper import JfrogScraper
from src.scrapers.jpmorganchase_scraper import JPMorganChaseScraper
from src.scrapers.kotak_scraper import KotakScraper
from src.scrapers.linkedin_scraper import LinkedInScraper
from src.scrapers.lowes_scraper import LowesScraper
from src.scrapers.makemytrip_scraper import MakemytripScraper
from src.scrapers.marvell_scraper import MarvellScraper
from src.scrapers.meesho_scraper import MeeshoScraper
from src.scrapers.mastercard_scraper import MastercardScraper
from src.scrapers.meta_scraper import MetaScraper
from src.scrapers.mercari_scraper import MercariScraper
from src.scrapers.merck_scraper import MerckScraper
from src.scrapers.microsoft_scraper import MicrosoftScraper
from src.scrapers.mongodb_scraper import MongoDbScraper
from src.scrapers.navi_scraper import NaviScraper
from src.scrapers.netapp_scraper import NetAppScraper
from src.scrapers.netflix_scraper import NetflixScraper
from src.scrapers.nike_scraper import NikeScraper
from src.scrapers.nk_securities_scraper import NKSecuritiesScraper
from src.scrapers.notion_scraper import NotionScraper
from src.scrapers.nutanix_scraper import NutanixScraper
from src.scrapers.nvidia_scraper import NvidiaScraper
from src.scrapers.omnissa_scraper import OmnissaScraper
from src.scrapers.okta_scraper import OktaScraper
from src.scrapers.openai_scraper import OpenAIScraper
from src.scrapers.optum_scraper import OptumScraper
from src.scrapers.outmarket_ai_scraper import OutmarketAIScraper
from src.scrapers.publicis_sapient_scraper import PublicisSapientScraper
from src.scrapers.palo_alto_scraper import PaloAltoScraper
from src.scrapers.payu_scraper import PayuScraper
from src.scrapers.phonepe_scraper import PhonePeScraper
from src.scrapers.postman_scraper import PostmanScraper
from src.scrapers.oracle_scraper import OracleScraper
from src.scrapers.qualcomm_scraper import QualcommScraper
from src.scrapers.deel_scraper import DeelScraper
from src.scrapers.dell_scraper import DellScraper
from src.scrapers.dp_world_scraper import DPWorldScraper
from src.scrapers.dolby_scraper import DolbyScraper
from src.scrapers.docusign_scraper import DocusignScraper
from src.scrapers.ea_scraper import EAScraper
from src.scrapers.ebay_scraper import EbayScraper
from src.scrapers.emergent_scraper import EmergentScraper
from src.scrapers.ericsson_scraper import EricssonScraper
from src.scrapers.everpure_scraper import EverpureScraper
from src.scrapers.expedia_scraper import ExpediaScraper
from src.scrapers.freshworks_scraper import FreshworksScraper
from src.scrapers.gitlab_scraper import GitLabScraper
from src.scrapers.exl_scraper import EXLScraper
from src.scrapers.quince_scraper import QuinceScraper
from src.scrapers.razorpay_scraper import RazorpayScraper
from src.scrapers.redhat_scraper import RedhatScraper
from src.scrapers.ringcentral_scraper import RingCentralScraper
from src.scrapers.roku_scraper import RokuScraper
from src.scrapers.rippling_scraper import RipplingScraper
from src.scrapers.rubrik_scraper import RubrikScraper
from src.scrapers.salesforce_scraper import SalesforceScraper
from src.scrapers.scapia_scraper import ScapiaScraper
from src.scrapers.slice_scraper import SliceScraper
from src.scrapers.sprinklr_scraper import SprinklrScraper
from src.scrapers.samsung_scraper import SamsungScraper
from src.scrapers.sap_scraper import SapScraper
from src.scrapers.sandisk_scraper import SandiskScraper
from src.scrapers.schneider_electric_scraper import SchneiderElectricScraper
from src.scrapers.siemens_scraper import SiemensScraper
from src.scrapers.western_digital_scraper import WesternDigitalScraper
from src.scrapers.servicenow_scraper import ServiceNowScraper
from src.scrapers.stripe_scraper import StripeScraper
from src.scrapers.swiggy_scraper import SwiggyScraper
from src.scrapers.symphonyai_scraper import SymphonyAIScraper
from src.scrapers.synopsys_scraper import SynopsysScraper
from src.scrapers.target_scraper import TargetScraper
from src.scrapers.teradata_scraper import TeradataScraper
from src.scrapers.tower_research_scraper import TowerResearchScraper
from src.scrapers.tesco_scraper import TescoScraper
from src.scrapers.texas_instruments_scraper import TexasInstrumentsScraper
from src.scrapers.thoughtspot_scraper import ThoughtspotScraper
from src.scrapers.twilio_scraper import TwilioScraper
from src.scrapers.uber_scraper import UberScraper
from src.scrapers.uipath_scraper import UiPathScraper
from src.scrapers.visa_scraper import VisaScraper
from src.scrapers.walmart_scraper import WalmartScraper
from src.scrapers.waymo_scraper import WaymoScraper
from src.scrapers.whatfix_scraper import WhatfixScraper
from src.scrapers.wells_fargo_scraper import WellsFargoScraper
from src.scrapers.workday_scraper import WorkdayScraper
from src.scrapers.dynatrace_scraper import DynatraceScraper
from src.scrapers.highlevel_scraper import HighLevelScraper
from src.scrapers.jumpcloud_scraper import JumpCloudScraper
from src.scrapers.acko_scraper import AckoScraper
from src.scrapers.aspen_scraper import AspenScraper
from src.scrapers.zebra_scraper import ZebraScraper
from src.scrapers.zeta_scraper import ZetaScraper
from src.scrapers.zoom_scraper import ZoomScraper
from src.scrapers.zscaler_scraper import ZscalerScraper


def get_scraper(
    company_config: dict[str, Any], settings: dict[str, Any]
) -> BaseScraper | None:
    scraper_name = str(company_config.get("scraper", "")).lower()

    if scraper_name == "adobe":
        return AdobeScraper(company_config, settings)
    if scraper_name == "agoda":
        return AgodaScraper(company_config, settings)
    if scraper_name == "airbnb":
        return AirbnbScraper(company_config, settings)
    if scraper_name == "akamai":
        return AkamaiScraper(company_config, settings)
    if scraper_name == "apple":
        return AppleScraper(company_config, settings)
    if scraper_name == "american_express":
        return AmericanExpressScraper(company_config, settings)
    if scraper_name == "bloomberg":
        return BloombergScraper(company_config, settings)

    if scraper_name == "bloomreach":
        return BloomreachScraper(company_config, settings)

    if scraper_name == "blackrock":
        return BlackRockScraper(company_config, settings)
    if scraper_name == "browserstack":
        return BrowserstackScraper(company_config, settings)
    if scraper_name == "booking_holdings":
        return BookingHoldingsScraper(company_config, settings)
    if scraper_name == "broadcom":
        return BroadcomScraper(company_config, settings)

    if scraper_name == "bytedance":
        return ByteDanceScraper(company_config, settings)

    if scraper_name == "cadence":
        return CadenceScraper(company_config, settings)
    if scraper_name == "capco":
        return CapcoScraper(company_config, settings)
    if scraper_name == "cerebras":
        return CerebrasScraper(company_config, settings)
    if scraper_name == "cisco":
        return CiscoScraper(company_config, settings)
    if scraper_name == "clevertap":
        return CleverTapScraper(company_config, settings)
    if scraper_name == "cloudera":
        return ClouderaScraper(company_config, settings)
    if scraper_name == "mediatek":
        return MediatekScraper(company_config, settings)
    if scraper_name == "arcesium":
        return ArcesiumScraper(company_config, settings)
    if scraper_name == "arista":
        return AristaScraper(company_config, settings)
    if scraper_name == "arm":
        return ArmScraper(company_config, settings)
    if scraper_name == "atlan":
        return AtlanScraper(company_config, settings)

    if scraper_name == "atlassian":
        return AtlassianScraper(company_config, settings)

    if scraper_name == "bcg":
        return BCGScraper(company_config, settings)
    if scraper_name == "chubb":
        return ChubbScraper(company_config, settings)
    if scraper_name == "citi":
        return CitiScraper(company_config, settings)
    if scraper_name == "cohesity":
        return CohesityScraper(company_config, settings)
    if scraper_name == "ciena":
        return CienaScraper(company_config, settings)
    if scraper_name == "concert_ai":
        return ConcertAIScraper(company_config, settings)
    if scraper_name == "confluent":
        return ConfluentScraper(company_config, settings)
    if scraper_name == "crowdstrike":
        return CrowdstrikeScraper(company_config, settings)
    if scraper_name == "db":
        return DBScraper(company_config, settings)

    if scraper_name == "databricks":
        return DatabricksScraper(company_config, settings)
    if scraper_name == "google":
        return GoogleScraper(company_config, settings)
    if scraper_name == "amd":
        return AmdScraper(company_config, settings)
    if scraper_name == "anthropic":
        return AnthropicScraper(company_config, settings)
    if scraper_name == "amazon":
        return AmazonScraper(company_config, settings)
    if scraper_name == "analog_devices":
        return AnalogDevicesScraper(company_config, settings)
    if scraper_name == "microsoft":
        return MicrosoftScraper(company_config, settings)
    if scraper_name == "mongodb":
        return MongoDbScraper(company_config, settings)
    if scraper_name == "nvidia":
        return NvidiaScraper(company_config, settings)
    if scraper_name == "palo_alto":
        return PaloAltoScraper(company_config, settings)
    if scraper_name == "payu":
        return PayuScraper(company_config, settings)
    if scraper_name == "jpmorgan":
        return JPMorganChaseScraper(company_config, settings)
    if scraper_name == "jfrog":
        return JfrogScraper(company_config, settings)
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
    if scraper_name == "marvell":
        return MarvellScraper(company_config, settings)
    if scraper_name == "meta":
        return MetaScraper(company_config, settings)
    if scraper_name == "mercari":
        return MercariScraper(company_config, settings)
    if scraper_name == "merck":
        return MerckScraper(company_config, settings)
    if scraper_name == "navi":
        return NaviScraper(company_config, settings)
    if scraper_name == "netapp":
        return NetAppScraper(company_config, settings)
    if scraper_name == "netflix":
        return NetflixScraper(company_config, settings)
    if scraper_name == "nike":
        return NikeScraper(company_config, settings)
    if scraper_name == "nk_securities":
        return NKSecuritiesScraper(company_config, settings)
    if scraper_name == "salesforce":
        return SalesforceScraper(company_config, settings)
    if scraper_name == "scapia":
        return ScapiaScraper(company_config, settings)

    if scraper_name == "slice":
        return SliceScraper(company_config, settings)

    if scraper_name == "sprinklr":
        return SprinklrScraper(company_config, settings)

    if scraper_name == "samsung":
        return SamsungScraper(company_config, settings)
    if scraper_name == "sap":
        return SapScraper(company_config, settings)
    if scraper_name == "schneider_electric":
        return SchneiderElectricScraper(company_config, settings)
    if scraper_name == "sandisk":
        return SandiskScraper(company_config, settings)
    if scraper_name == "western_digital":
        return WesternDigitalScraper(company_config, settings)
    if scraper_name == "okta":
        return OktaScraper(company_config, settings)
    if scraper_name == "oracle":
        return OracleScraper(company_config, settings)
    if scraper_name == "servicenow":
        return ServiceNowScraper(company_config, settings)
    if scraper_name == "harness":
        return HarnessScraper(company_config, settings)

    if scraper_name == "hevodata":
        return HevoDataScraper(company_config, settings)

    if scraper_name == "harman":
        return HarmanScraper(company_config, settings)

    if scraper_name == "hpe":
        return HPEScraper(company_config, settings)
    if scraper_name == "ibm":
        return IbmScraper(company_config, settings)
    if scraper_name == "indeed":
        return IndeedScraper(company_config, settings)
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
    if scraper_name == "glean":
        return GleanScraper(company_config, settings)
    if scraper_name == "goldman_sachs":
        return GoldmanSachsScraper(company_config, settings)
    if scraper_name == "honeywell":
        return HoneywellScraper(company_config, settings)
    if scraper_name == "quince":
        return QuinceScraper(company_config, settings)
    if scraper_name == "razorpay":
        return RazorpayScraper(company_config, settings)
    if scraper_name == "redhat":
        return RedhatScraper(company_config, settings)

    if scraper_name == "ringcentral":
        return RingCentralScraper(company_config, settings)

    if scraper_name == "roku":
        return RokuScraper(company_config, settings)

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

    if scraper_name == "dolby":
        return DolbyScraper(company_config, settings)

    if scraper_name == "docusign":
        return DocusignScraper(company_config, settings)
    if scraper_name == "ea":
        return EAScraper(company_config, settings)
    if scraper_name == "ebay":
        return EbayScraper(company_config, settings)
    if scraper_name == "emergent":
        return EmergentScraper(company_config, settings)
    if scraper_name == "ericsson":
        return EricssonScraper(company_config, settings)
    if scraper_name == "expedia":
        return ExpediaScraper(company_config, settings)
    if scraper_name == "freshworks":
        return FreshworksScraper(company_config, settings)
    if scraper_name == "gitlab":
        return GitLabScraper(company_config, settings)
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
    if scraper_name == "phonepe":
        return PhonePeScraper(company_config, settings)
    if scraper_name == "postman":
        return PostmanScraper(company_config, settings)
    if scraper_name == "publicis_sapient":
        return PublicisSapientScraper(company_config, settings)
    if scraper_name == "openai":
        return OpenAIScraper(company_config, settings)
    if scraper_name == "optum":
        return OptumScraper(company_config, settings)
    if scraper_name == "siemens":
        return SiemensScraper(company_config, settings)
    if scraper_name == "stripe":
        return StripeScraper(company_config, settings)
    if scraper_name == "swiggy":
        return SwiggyScraper(company_config, settings)
    if scraper_name == "synopsys":
        return SynopsysScraper(company_config, settings)

    if scraper_name == "symphonyai":
        return SymphonyAIScraper(company_config, settings)

    if scraper_name == "target":
        return TargetScraper(company_config, settings)
    if scraper_name == "teradata":
        return TeradataScraper(company_config, settings)
    if scraper_name == "tower_research":
        return TowerResearchScraper(company_config, settings)
    if scraper_name == "tesco":
        return TescoScraper(company_config, settings)
    if scraper_name == "texas_instruments":
        return TexasInstrumentsScraper(company_config, settings)
    if scraper_name == "thoughtspot":
        return ThoughtspotScraper(company_config, settings)
    if scraper_name == "twilio":
        return TwilioScraper(company_config, settings)
    if scraper_name == "uber":
        return UberScraper(company_config, settings)
    if scraper_name == "uipath":
        return UiPathScraper(company_config, settings)
    if scraper_name == "visa":
        return VisaScraper(company_config, settings)
    if scraper_name == "walmart":
        return WalmartScraper(company_config, settings)

    if scraper_name == "waymo":
        return WaymoScraper(company_config, settings)

    if scraper_name == "whatfix":
        return WhatfixScraper(company_config, settings)

    if scraper_name == "wells_fargo":
        return WellsFargoScraper(company_config, settings)

    if scraper_name == "workday":
        return WorkdayScraper(company_config, settings)

    if scraper_name == "dynatrace":
        return DynatraceScraper(company_config, settings)

    if scraper_name == "highlevel":
        return HighLevelScraper(company_config, settings)

    if scraper_name == "jumpcloud":
        return JumpCloudScraper(company_config, settings)

    if scraper_name == "acko":
        return AckoScraper(company_config, settings)

    if scraper_name == "aspen":
        return AspenScraper(company_config, settings)

    if scraper_name == "zebra":
        return ZebraScraper(company_config, settings)

    if scraper_name == "zscaler":
        return ZscalerScraper(company_config, settings)

    if scraper_name == "zeta":
        return ZetaScraper(company_config, settings)

    if scraper_name == "zoom":
        return ZoomScraper(company_config, settings)

    logging.getLogger("job_alert_bot").warning(
        "Unsupported scraper: %s", company_config.get("scraper")
    )
    return None
