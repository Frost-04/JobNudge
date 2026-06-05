from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

from src.scrapers.scraper_factory import get_scraper
from src.services.dedup_service import deduplicate_jobs
from src.services.export_service import export_latest_jobs_to_csv
from src.services.filter_service import extract_experience_from_jobs
from src.services.notification_service import notify_new_jobs
from src.services.storage_service import get_new_jobs, init_db, save_jobs
from src.utils.config_loader import load_companies, load_keywords, load_settings
from src.utils.logger import setup_logging


@dataclass
class _ScrapeResult:
    """Result from scraping a single company."""
    company_name: str
    company_url: str
    jobs: list
    fault: Optional[dict] = None


async def _scrape_one(name: str, url: str, scraper, settings: dict, logger) -> _ScrapeResult:
    """Scrape one company and return the result.

    Exceptions are caught internally — never propagated.
    Each scraper is wrapped with asyncio.wait_for to prevent a slow/hung
    scraper from stalling the entire pipeline. The timeout is read from:
      1. Per-company ``timeout_seconds`` in companies.yaml (if set)
      2. Global ``run.scraper_timeout_seconds`` in settings.yaml (default: 180)
    """
    timeout = int(
        scraper.company_config.get("timeout_seconds")
        or settings.get("run", {}).get("scraper_timeout_seconds", 180)
    )

    try:
        jobs = await asyncio.wait_for(scraper.scrape(), timeout=timeout)
        logger.info("%s: raw jobs=%s", name, len(jobs))

        if len(jobs) == 0:
            return _ScrapeResult(
                company_name=name,
                company_url=url,
                jobs=[],
                fault={
                    "company": name,
                    "url": url,
                    "reason": "0 job postings returned (page structure may have changed or no matching listings)",
                },
            )
        return _ScrapeResult(company_name=name, company_url=url, jobs=jobs)
    except asyncio.TimeoutError:
        logger.warning("Scraper timed out for %s (limit: %ss)", name, timeout)
        try:
            await asyncio.wait_for(scraper.close_browser(), timeout=10)
        except Exception:
            pass
        return _ScrapeResult(
            company_name=name,
            company_url=url,
            jobs=[],
            fault={
                "company": name,
                "url": url,
                "reason": f"scraper timed out after {timeout}s (page may be slow or unresponsive)",
            },
        )
    except Exception:
        logger.exception("Scraper failed for %s", name)
        return _ScrapeResult(
            company_name=name,
            company_url=url,
            jobs=[],
            fault={
                "company": name,
                "url": url,
                "reason": "scraping error (network issue, page change, or selector mismatch)",
            },
        )


async def run() -> tuple[int, list[dict]]:
    """Run the full scraping pipeline.

    All enabled companies are scraped in parallel via asyncio.gather().
    Deduplication, DB persistence, CSV export, and notifications happen
    sequentially after all scraping results are collected.

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
        keywords_config = load_keywords()
        experience_keywords = keywords_config.get("experience_keywords", []) or []
    except Exception:
        logger.exception("Failed to load keywords configuration.")
        return 1, []

    try:
        init_db(settings)
    except Exception:
        logger.exception("Failed to initialize database.")
        return 1, []

    logger.info("Started job scraper")

    # ── Phase 1: Scrape all enabled companies in parallel ──────────────
    enabled_companies = [c for c in companies if c.get("enabled", True)]
    tasks = []
    for company in enabled_companies:
        name = company.get("name", "Unknown")
        url = company.get("url", "")
        scraper = get_scraper(company, settings)
        if not scraper:
            logger.warning("Skipping %s (unsupported scraper: %s)", name, company.get("scraper"))
            continue
        tasks.append(_scrape_one(name, url, scraper, settings, logger))

    if not tasks:
        logger.warning("No enabled companies with valid scrapers found.")
        return 0, []

    results: list[_ScrapeResult] = await asyncio.gather(*tasks)

    # ── Phase 2: Process results sequentially (dedup, DB, faulty tracking) ──
    all_new_jobs = []
    all_jobs = []
    faulty_companies: list[dict] = []

    for result in results:
        if result.fault:
            faulty_companies.append(result.fault)

        if not result.jobs:
            continue

        deduped = deduplicate_jobs(result.jobs)

        # Extract experience snippets from job descriptions.
        if experience_keywords:
            extract_experience_from_jobs(deduped, experience_keywords)

        new_jobs = get_new_jobs(deduped, settings)
        if new_jobs:
            save_jobs(new_jobs, settings)

        all_new_jobs.extend(new_jobs)
        all_jobs.extend(deduped)

    # ── Phase 3: Export & notify (sequential, after all results are in) ──
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
