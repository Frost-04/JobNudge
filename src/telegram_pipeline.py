"""
Telegram Pipeline — Full Job Alert Bot with AI + Telegram

Runs the complete pipeline:
  1. Scrapes job listings (main.py)
  2. Filters new jobs through Gemini AI (ai_pipeline.py → jobs_to_send.csv)
  3. Sends formatted Telegram alerts for AI-selected jobs

Usage:
    python -m src.telegram_pipeline

Requires in .env:
    GEMINI_API_KEY=your-gemini-key
    TELEGRAM_BOT_TOKEN=your-telegram-bot-token
    TELEGRAM_CHAT_ID=your-telegram-chat-id
"""

from __future__ import annotations

import asyncio
import csv
import html
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.services.notification_service import post_telegram_message
from src.utils.config_loader import load_settings

ROCKET = "\U0001F680"
ROBOT = "\U0001F916"
WARNING = "\u26A0\uFE0F"


async def run_telegram_pipeline() -> int:
    """Run scraping → AI filter → Telegram notification."""
    load_dotenv()

    # ── Step 1 & 2: Run the AI pipeline (scraping + AI filtering) ─────
    print("\n" + "=" * 60)
    print("  STEPS 1-2: Running scraping + AI filtering pipeline")
    print("=" * 60 + "\n")

    from src.ai_pipeline import run_ai_pipeline

    ai_exit_code, faulty_companies = await run_ai_pipeline()

    if ai_exit_code != 0:
        logging.getLogger("job_alert_bot").error(
            "AI pipeline failed with code %d. Aborting Telegram pipeline.", ai_exit_code
        )
        # Still try to send fault alert if we have Telegram creds
        await _send_faulty_alert_raw(faulty_companies)
        return ai_exit_code

    # ── Step 3: Load settings ──────────────────────────────────────────
    try:
        settings = load_settings()
    except Exception:
        logging.exception("Failed to load settings for Telegram pipeline.")
        return 1

    jobs_to_send_path = settings.get("storage", {}).get(
        "jobs_to_send_csv_path", "data/jobs_to_send.csv"
    )

    # ── Step 4: Send fault alert for broken companies ──────────────────
    await _send_faulty_alert(faulty_companies, settings)

    # ── Step 5: Read jobs_to_send.csv ──────────────────────────────────
    jobs = _read_jobs_to_send(jobs_to_send_path)

    if not jobs:
        print("\n📭 No AI-selected jobs to send. Skipping Telegram notification.")
        return 0

    # ── Step 6: Send Telegram notifications ────────────────────────────
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        print("\n❌ ERROR: Telegram credentials not set in .env.")
        print("   Add: TELEGRAM_BOT_TOKEN=your-bot-token")
        print("   Add: TELEGRAM_CHAT_ID=your-chat-id")
        print(f"\n   {len(jobs)} AI-selected job(s) are waiting in {jobs_to_send_path}")
        return 1

    timeout = settings.get("run", {}).get("request_timeout_seconds", 30)
    max_jobs = int(
        settings.get("notifications", {}).get("max_jobs_per_alert", 20)
    )

    print(f"\n📤 Sending {min(len(jobs), max_jobs)} Telegram alert(s)...")

    success = 0
    for job in jobs[:max_jobs]:
        message = _format_csv_job_for_telegram(job)
        try:
            post_telegram_message(token, chat_id, message, timeout)
            success += 1
            print(f"   ✅ Sent: {job.get('company', '')} — {job.get('title', '')}")
        except Exception:
            logging.getLogger("job_alert_bot").exception(
                "Failed to send Telegram alert for %s — %s",
                job.get("company", ""),
                job.get("title", ""),
            )

    # ── Done ───────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"  ✅ Telegram Pipeline complete! {success}/{len(jobs[:max_jobs])} sent.")
    print("=" * 60 + "\n")
    return 0


async def _send_faulty_alert(faulty_companies: list[dict], settings: dict) -> None:
    """Send a single consolidated Telegram alert for all faulty companies."""
    if not faulty_companies:
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        print(f"\n⚠ {len(faulty_companies)} company(s) had issues but Telegram is not configured.")
        for f in faulty_companies:
            print(f"   ⚠ {f['company']}: {f['reason']}")
        return

    timeout = settings.get("run", {}).get("request_timeout_seconds", 30)

    lines = [
        f"{WARNING} <b>Scraper Alert — {len(faulty_companies)} company(s) had issues</b>",
        "",
    ]

    for f in faulty_companies:
        lines.append(f"<b>{html.escape(f['company'])}</b>")
        lines.append(f"Reason: {html.escape(f['reason'])}")
        if f.get("url"):
            lines.append(f"URL: {html.escape(f['url'])}")
        lines.append("")

    lines.append("<i>Check the page structure or network — selectors may need updating.</i>")

    message = "\n".join(lines)

    try:
        post_telegram_message(token, chat_id, message, timeout)
        print(f"\n⚠ Sent fault alert for {len(faulty_companies)} company(s).")
    except Exception:
        logging.getLogger("job_alert_bot").exception("Failed to send fault alert to Telegram.")
        print(f"\n⚠ {len(faulty_companies)} company(s) had issues (Telegram send failed):")
        for f in faulty_companies:
            print(f"   ⚠ {f['company']}: {f['reason']}")


async def _send_faulty_alert_raw(faulty_companies: list[dict]) -> None:
    """Send fault alert without settings (for early-exit paths). Loads settings internally."""
    if not faulty_companies:
        return
    try:
        settings = load_settings()
    except Exception:
        logging.getLogger("job_alert_bot").exception("Failed to load settings for fault alert.")
        return
    await _send_faulty_alert(faulty_companies, settings)


def _read_jobs_to_send(path: str) -> list[dict[str, str]]:
    """Read jobs_to_send.csv and return non-empty rows."""
    csv_path = Path(path)
    if not csv_path.exists():
        return []

    with csv_path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return []
        return [row for row in reader if any(v.strip() for v in row.values())]


def _format_csv_job_for_telegram(job: dict[str, str]) -> str:
    """Format a single job dict (from CSV) into a Telegram message."""
    company = html.escape(job.get("company", "Unknown").strip())
    title = html.escape(job.get("title", "Unknown").strip())
    url = html.escape(job.get("url", "").strip(), quote=True)
    job_id = html.escape(job.get("job_id", "").strip())
    location = html.escape(job.get("location", "").strip())

    lines = [
        f"{ROCKET} <b>AI-Selected Job</b> {ROBOT}",
        "",
        f"<b>Company:</b> {company}",
        f"<b>Role:</b> {title}",
    ]

    if job_id and job_id != "0":
        lines.append(f"<b>Job ID:</b> {job_id}")

    if location:
        lines.append(f"<b>Location:</b> {location}")

    lines.append("")
    lines.append(f'<a href="{url}">🔗 View Job Listing</a>')

    return "\n".join(lines)


def main() -> None:
    sys.exit(asyncio.run(run_telegram_pipeline()))


if __name__ == "__main__":
    main()
