from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import TextIO

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from src.models.job import Job
from src.scrapers.scraper_factory import get_scraper
from src.services.storage_service import init_db
from src.utils.config_loader import load_companies, load_keywords, load_settings
from src.utils.logger import setup_logging


OUTPUT_FILE = Path("health_check_output.txt")


class _Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BLUE = "\033[34m"


def _use_colors() -> bool:
    return sys.stdout.isatty()


def _color(text: str, code: str) -> str:
    if not _use_colors():
        return text
    return f"{code}{text}{_Colors.RESET}"


def _status(label: str, ok: bool) -> str:
    text = "PASS" if ok else "FAIL"
    color = _Colors.GREEN if ok else _Colors.RED
    return f"{label}: {_color(text, color)}"


def _plain_status(label: str, ok: bool) -> str:
    text = "PASS" if ok else "FAIL"
    return f"{label}: {text}"


def _section(title: str) -> None:
    print(_color(f"\n== {title} ==", _Colors.BLUE))


def _file_section(file: TextIO, title: str) -> None:
    file.write(f"\n== {title} ==\n")


def _step(message: str) -> None:
    print(_color(f"- {message}", _Colors.DIM))


def _file_step(file: TextIO, message: str) -> None:
    file.write(f"- {message}\n")


def _safe_repr(value: object, max_length: int = 5000) -> str:
    """
    Returns a readable repr, truncating very long strings like descriptions.
    """

    if value is None:
        return "None"

    if isinstance(value, str):
        cleaned = value.replace("\r", " ").replace("\n", " ").strip()

        if len(cleaned) > max_length:
            cleaned = cleaned[:max_length].rstrip() + "..."

        return repr(cleaned)

    return repr(value)


def _format_job(job: Job) -> str:
    """
    Pretty formatter for Job dataclass.

    Expected fields:
        job_id
        company
        title
        location
        url
        source_url
        posted_date
        description
        scraped_at
        matched_keywords
    """

    return (
        "Job(\n"
        f"  job_id={_safe_repr(job.job_id)},\n"
        f"  company={_safe_repr(job.company)},\n"
        f"  title={_safe_repr(job.title)},\n"
        f"  location={_safe_repr(job.location)},\n"
        f"  url={_safe_repr(job.url)},\n"
        f"  source_url={_safe_repr(job.source_url)},\n"
        f"  posted_date={_safe_repr(job.posted_date)},\n"
        f"  description={_safe_repr(job.description)},\n"
        f"  scraped_at={_safe_repr(job.scraped_at)},\n"
        f"  matched_keywords={_safe_repr(job.matched_keywords)},\n"
        ")"
    )


async def _check_playwright(settings: dict) -> bool:
    run_settings = settings.get("run", {})
    headless = bool(run_settings.get("headless", True))
    _step(f"Launching Playwright (headless={headless})")

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=headless)
            await browser.close()
        return True
    except Exception:
        return False


async def run() -> int:
    start_ts = time.perf_counter()
    load_dotenv()

    with OUTPUT_FILE.open("w", encoding="utf-8") as output:
        output.write("Health Check Output\n")
        output.write("===================\n")
        output.write(f"Output file: {OUTPUT_FILE.name}\n")

        _section("Config")
        _file_section(output, "Config")

        _step("Loading settings.yaml, companies.yaml, keywords.yaml")
        _file_step(output, "Loading settings.yaml, companies.yaml, keywords.yaml")

        try:
            settings = load_settings()
            companies = load_companies()
            load_keywords()
        except Exception as exc:
            print(_status("CONFIG", False))
            output.write(_plain_status("CONFIG", False) + "\n")
            output.write(f"Error: {exc!r}\n")
            return 1

        logger = setup_logging(settings)

        print(_status("CONFIG", True))
        output.write(_plain_status("CONFIG", True) + "\n")

        _section("Database")
        _file_section(output, "Database")

        _step("Initializing SQLite database")
        _file_step(output, "Initializing SQLite database")

        db_ok = True

        try:
            init_db(settings)
            print(_status("DATABASE", True))
            output.write(_plain_status("DATABASE", True) + "\n")
        except Exception as exc:
            db_ok = False
            print(_status("DATABASE", False))
            output.write(_plain_status("DATABASE", False) + "\n")
            output.write(f"Error: {exc!r}\n")
            logger.exception("Database initialization failed.")

        _section("Playwright")
        _file_section(output, "Playwright")

        playwright_ok = await _check_playwright(settings)

        print(_status("PLAYWRIGHT", playwright_ok))
        output.write(_plain_status("PLAYWRIGHT", playwright_ok) + "\n")

        _section("Scrapers")
        _file_section(output, "Scrapers")

        _step("Starting company scrapers")
        _file_step(output, "Starting company scrapers")

        for company in companies:
            if not company.get("enabled", True):
                continue

            name = company.get("name", "Unknown")

            print(_color(f"\n{name}", _Colors.BOLD))
            output.write(f"\n{name}\n")
            output.write("-" * len(name) + "\n")

            _step("Creating scraper instance")
            _file_step(output, "Creating scraper instance")

            scraper = get_scraper(company, settings)

            if not scraper:
                message = "Unsupported scraper."
                print(_color(message, _Colors.YELLOW))
                output.write(message + "\n")
                continue

            try:
                _step("Running scrape()")
                _file_step(output, "Running scrape()")

                scraper_timeout = settings.get("health_check", {}).get(
                    "scraper_timeout_seconds",
                    120,
                )

                jobs = await asyncio.wait_for(
                    scraper.scrape(),
                    timeout=scraper_timeout,
                )

                print(f"Extracted jobs: {len(jobs)}")
                output.write(f"Extracted jobs: {len(jobs)}\n")

                max_samples = settings.get("health_check", {}).get(
                    "max_sample_jobs_per_company",
                    5,
                )

                if jobs:
                    print("Sample:")
                    output.write("\nSample Jobs:\n")

                    for index, job in enumerate(jobs[:max_samples], start=1):
                        job_id_suffix = (
                            f" | {job.job_id}"
                            if job.job_id and job.job_id != "0"
                            else ""
                        )

                        print(
                            f"- {job.title} | {job.location} | {job.url}{job_id_suffix}"
                        )

                        output.write(f"\n[{index}]\n")
                        output.write(_format_job(job))
                        output.write("\n")

                output.flush()

            except asyncio.TimeoutError:
                print("Extracted jobs: 0")
                output.write("Extracted jobs: 0\n")
                output.write("Error: scraper timed out\n")
                logger.exception("Health check scraper timed out for %s", name)

            except Exception as exc:
                print("Extracted jobs: 0")
                output.write("Extracted jobs: 0\n")
                output.write(f"Error: {exc!r}\n")
                logger.exception("Health check scraper failed for %s", name)

        elapsed = time.perf_counter() - start_ts

        _section("Summary")
        _file_section(output, "Summary")

        print(_color(f"Completed in {elapsed:.1f}s", _Colors.DIM))
        output.write(f"Completed in {elapsed:.1f}s\n")

        output.write(f"\nFinal status: {'PASS' if playwright_ok and db_ok else 'FAIL'}\n")

    print(f"\nDetailed health check output written to: {OUTPUT_FILE.resolve()}")

    return 0 if playwright_ok and db_ok else 1


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
