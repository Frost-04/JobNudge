from __future__ import annotations

import asyncio
import logging
import sys

from dotenv import load_dotenv

from src.scrapers.scraper_factory import get_scraper
from src.services.dedup_service import deduplicate_jobs
from src.services.export_service import export_latest_jobs_to_csv
from src.services.notification_service import notify_new_jobs
from src.services.storage_service import get_new_jobs, init_db, save_jobs
from src.utils.config_loader import load_companies, load_settings
from src.utils.logger import setup_logging


async def run() -> tuple[int, list[dict]]:
    """Run the full scraping pipeline.

    Returns:
        (exit_code, faulty_companies) — exit_code 0 on success, 1 on config/DB failure.
        faulty_companies is a list of dicts: {"company": str, "url": str, "reason": str}
        for companies that returned 0 raw jobs or had scraping errors.
    """
    load_dotenv()

    try:
        settings = load_settings()
    except Exception:
        logging.exception("Failed to load settings.")
        return 1, []

    logger = setup_logging(settings)

    try:
        companies = load_companies()
    except Exception:
        logger.exception("Failed to load configuration files.")
        return 1, []

    try:
        init_db(settings)
    except Exception:
        logger.exception("Failed to initialize database.")
        return 1, []

    logger.info("Started job scraper")

    all_new_jobs = []
    all_jobs = []
    faulty_companies: list[dict] = []
    delay = settings.get("run", {}).get("delay_between_companies_seconds", 0)

    enabled_companies = [company for company in companies if company.get("enabled", True)]
    for index, company in enumerate(enabled_companies):
        name = company.get("name", "Unknown")
        url = company.get("url", "")
        scraper = get_scraper(company, settings)
        if not scraper:
            continue

        try:
            jobs = await scraper.scrape()
            logger.info("%s: raw jobs=%s", name, len(jobs))

            if len(jobs) == 0:
                faulty_companies.append({
                    "company": name,
                    "url": url,
                    "reason": "0 job postings returned (page structure may have changed or no matching listings)",
                })

            deduped = deduplicate_jobs(jobs)
            new_jobs = get_new_jobs(deduped, settings)
            if new_jobs:
                save_jobs(new_jobs, settings)

            all_new_jobs.extend(new_jobs)
            all_jobs.extend(deduped)
        except Exception:
            logger.exception("Scraper failed for %s", name)
            faulty_companies.append({
                "company": name,
                "url": url,
                "reason": "scraping error (network issue, page change, or selector mismatch)",
            })

        if delay and index < len(enabled_companies) - 1:
            await asyncio.sleep(delay)

    try:
        new_jobs_path = settings.get("storage", {}).get("new_jobs_csv_path", "data/new_jobs.csv")
        export_latest_jobs_to_csv(all_new_jobs, new_jobs_path)
        logger.info("Exported %s new jobs to %s", len(all_new_jobs), new_jobs_path)
    except Exception:
        logger.exception("Failed to export new jobs to CSV.")

    notify_new_jobs(all_new_jobs, settings)
    if not all_new_jobs and not settings.get("notifications", {}).get(
        "send_empty_report", False
    ):
        logger.info("No new jobs found.")

    if faulty_companies:
        logger.warning(
            "Faulty companies (%d): %s",
            len(faulty_companies),
            ", ".join(f["company"] for f in faulty_companies),
        )

    return 0, faulty_companies


def main() -> None:
    exit_code, _ = asyncio.run(run())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
