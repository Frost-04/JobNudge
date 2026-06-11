from __future__ import annotations

import html
import logging
import os
import time

import requests

from src.models.job import Job

ROCKET = "\U0001F680"


def notify_new_jobs(jobs: list[Job], settings: dict) -> None:
    logger = logging.getLogger("job_alert_bot")
    notifications = settings.get("notifications", {})
    channel = str(notifications.get("channel", "console")).lower()
    send_empty = bool(notifications.get("send_empty_report", False))

    if not jobs and not send_empty:
        return

    if channel == "telegram":
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if token and chat_id:
            _send_telegram(jobs, token, chat_id, notifications, settings)
            return

        logger.warning("Telegram credentials missing. Falling back to console.")

    _print_console(jobs)


def _print_console(jobs: list[Job]) -> None:
    if not jobs:
        print("No new jobs found.")
        return

    print(f"New jobs found: {len(jobs)}\n")
    for index, job in enumerate(jobs, start=1):
        print(f"{index}. {job.company}")
        print(f"Role: {job.title}")
        if job.job_id and job.job_id != "0":
            print(f"Job ID: {job.job_id}")
        print(f"Location: {job.location}")
        print(f"Link: {job.url}")
        if job.extracted_experience_parts:
            print(f"Experience snippets: {job.extracted_experience_parts}")
        print("")


def _send_telegram(
    jobs: list[Job],
    token: str,
    chat_id: str,
    notifications: dict,
    settings: dict,
) -> None:
    logger = logging.getLogger("job_alert_bot")
    max_jobs = int(notifications.get("max_jobs_per_alert", 20))
    timeout = settings.get("run", {}).get("request_timeout_seconds", 30)

    if not jobs:
        post_telegram_message(token, chat_id, "No new jobs found.", timeout)
        return

    for job in jobs[:max_jobs]:
        message = format_telegram_job_message(job)
        try:
            post_telegram_message(token, chat_id, message, timeout)
        except Exception:
            logger.exception("Failed to send Telegram alert.")
            break
        # Respect Telegram rate limits — 1 message per second
        time.sleep(1)


def format_telegram_job_message(job: Job) -> str:
    lines = [
        f"{ROCKET} New job found",
        "",
        f"Company: {html.escape(job.company)}",
        f"Role: {html.escape(job.title)}",
    ]

    if job.job_id and job.job_id != "0":
        lines.append(f"Job ID: {html.escape(job.job_id)}")

    lines.extend(
        [
            f"Location: {html.escape(job.location)}",
            f"Link: {html.escape(job.url)}",
        ]
    )

    if job.extracted_experience_parts:
        lines.extend(["", f"Experience snippets: {html.escape(job.extracted_experience_parts)}"])

    return "\n".join(lines)


def post_telegram_message(
    token: str, chat_id: str, message: str, timeout: float | int
) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    response = requests.post(url, data=payload, timeout=timeout)
    response.raise_for_status()
