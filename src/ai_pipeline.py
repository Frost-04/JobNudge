"""
AI Pipeline Orchestrator

Runs the full scraping pipeline first (via main.run()), then passes the
resulting new_jobs.csv through Gemini AI filtering to produce jobs_to_send.csv.

Usage:
    python -m src.ai_pipeline

Requires:
    GEMINI_API_KEY in .env
    config/ai_prompt.yaml with your filtering prompt
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

from src.services.ai_filter_service import filter_and_export_with_ai
from src.utils.config_loader import load_ai_prompt, load_settings


async def run_ai_pipeline() -> tuple[int, list[dict]]:
    """Run the full pipeline: scraping → AI filtering → export.

    Returns:
        (exit_code, faulty_companies) — exit_code 0 on success.
        faulty_companies from the scraper phase for upstream alerting.
    """
    load_dotenv()

    # ── Step 1: Run the main scraper pipeline ──────────────────────────
    print("\n" + "=" * 60)
    print("  STEP 1: Running job scraper pipeline (main.py)")
    print("=" * 60 + "\n")

    from src.main import run as run_main_scraper

    scraper_exit_code, faulty_companies = await run_main_scraper()

    if scraper_exit_code != 0:
        logging.getLogger("job_alert_bot").error(
            "Scraper pipeline failed with code %d. Aborting AI pipeline.", scraper_exit_code
        )
        return scraper_exit_code, faulty_companies

    # ── Step 2: Load config for AI filtering ───────────────────────────
    print("\n" + "=" * 60)
    print("  STEP 2: Running AI filtering on new jobs")
    print("=" * 60 + "\n")

    try:
        settings = load_settings()
        prompt_config = load_ai_prompt()
    except Exception:
        logging.exception("Failed to load config for AI pipeline.")
        return 1, faulty_companies

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("❌ ERROR: GEMINI_API_KEY is not set in your .env file.")
        print("   Add a line like: GEMINI_API_KEY=your-key-here")
        return 1, faulty_companies

    prompt_template = prompt_config.get("prompt", "")
    if not prompt_template:
        logging.getLogger("job_alert_bot").error("AI prompt is empty in config/ai_prompt.yaml.")
        return 1, faulty_companies

    new_jobs_path = settings.get("storage", {}).get(
        "new_jobs_csv_path", "data/new_jobs.csv"
    )
    jobs_to_send_path = settings.get("storage", {}).get(
        "jobs_to_send_csv_path", "data/jobs_to_send.csv"
    )

    # ── Step 3: AI filtering and export ────────────────────────────────
    try:
        count = filter_and_export_with_ai(
            new_jobs_csv_path=new_jobs_path,
            jobs_to_send_csv_path=jobs_to_send_path,
            prompt_template=prompt_template,
            api_key=api_key,
            settings=settings,
        )
    except Exception:
        logging.exception("AI filtering pipeline failed.")
        return 1, faulty_companies

    # ── Done ───────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"  ✅ AI Pipeline complete! {count} job(s) exported to {jobs_to_send_path}")
    print("=" * 60 + "\n")
    return 0, faulty_companies


def main() -> None:
    exit_code, _ = asyncio.run(run_ai_pipeline())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
