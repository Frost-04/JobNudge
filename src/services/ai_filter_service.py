from __future__ import annotations

import csv
import json
import logging
import os
import re
from pathlib import Path
from typing import Any


def read_jobs_from_csv(csv_path: str) -> list[dict[str, str]]:
    """Read jobs from a CSV file and return as a list of dicts."""
    path = Path(csv_path)
    if not path.exists():
        logging.getLogger("job_alert_bot").warning("CSV file not found: %s", csv_path)
        return []

    with path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        jobs = list(reader)

    logging.getLogger("job_alert_bot").info("Read %d jobs from %s", len(jobs), csv_path)
    return jobs


def build_jobs_json(jobs: list[dict[str, str]]) -> str:
    """Build a compact JSON string containing only the fields Gemini needs."""
    slim_jobs = []
    for job in jobs:
        slim_jobs.append(
            {
                "job_id": job.get("job_id", ""),
                "company": job.get("company", ""),
                "title": job.get("title", ""),
                "url": job.get("url", ""),
                "description": job.get("description", ""),
            }
        )
    return json.dumps(slim_jobs, indent=2, ensure_ascii=False)


def call_gemini(prompt: str, api_key: str, model_name: str) -> str:
    """Send a prompt to Gemini and return the response text."""
    try:
        import google.generativeai as genai
    except ImportError:
        raise ImportError(
            "google-generativeai package is required. Install with: pip install google-generativeai"
        )

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)

    logger = logging.getLogger("job_alert_bot")
    logger.info("Calling Gemini model: %s", model_name)

    response = model.generate_content(prompt)

    if not response.text:
        logger.error("Gemini returned empty response.")
        return "[]"

    logger.info("Gemini response received (%d chars).", len(response.text))
    return response.text


def parse_job_ids(response_text: str) -> list[str]:
    """Extract a JSON array of job_ids from Gemini's response text."""
    logger = logging.getLogger("job_alert_bot")

    # Try to extract JSON array from the response (handles markdown code blocks, extra text, etc.)
    json_match = re.search(r"\[.*?\]", response_text, re.DOTALL)
    if not json_match:
        logger.error("No JSON array found in Gemini response. Raw: %s", response_text[:500])
        return []

    try:
        ids = json.loads(json_match.group(0))
        if not isinstance(ids, list):
            logger.error("Parsed JSON is not a list: %s", type(ids))
            return []
        # Ensure all elements are strings
        ids = [str(item) for item in ids]
        logger.info("Gemini returned %d matching job_ids.", len(ids))
        return ids
    except json.JSONDecodeError:
        logger.exception("Failed to parse JSON from Gemini response: %s", json_match.group(0)[:200])
        return []


def export_jobs_to_send(
    all_jobs: list[dict[str, str]],
    matching_ids: list[str],
    output_path: str,
) -> int:
    """Filter jobs by matching_ids and export to CSV. Returns count of exported jobs."""
    matching_id_set = set(matching_ids)
    matching_jobs = [job for job in all_jobs if job.get("job_id", "") in matching_id_set]

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not matching_jobs:
        # Still write an empty file with headers so the user knows it ran
        fieldnames = list(all_jobs[0].keys()) if all_jobs else [
            "job_id", "company", "title", "location", "url",
            "posted_date", "description", "matched_keywords", "scraped_at",
        ]
    else:
        fieldnames = list(matching_jobs[0].keys())

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for job in matching_jobs:
            writer.writerow(job)

    logger = logging.getLogger("job_alert_bot")
    logger.info(
        "Exported %d AI-filtered jobs to %s (from %d total, %d AI matches).",
        len(matching_jobs),
        output_path,
        len(all_jobs),
        len(matching_ids),
    )
    return len(matching_jobs)


def filter_and_export_with_ai(
    new_jobs_csv_path: str,
    jobs_to_send_csv_path: str,
    prompt_template: str,
    api_key: str,
    settings: dict,
) -> int:
    """
    Full AI filtering pipeline:
    1. Read jobs from new_jobs.csv
    2. Build prompt and call Gemini
    3. Parse matching job_ids from response
    4. Export matching jobs to jobs_to_send.csv

    Returns the number of jobs exported.
    """
    logger = logging.getLogger("job_alert_bot")

    # 1. Read jobs
    jobs = read_jobs_from_csv(new_jobs_csv_path)
    if not jobs:
        logger.info("No new jobs to filter with AI. Skipping.")
        return 0

    # Check API key
    if not api_key:
        logger.error("GEMINI_API_KEY is not set in .env. Skipping AI filtering.")
        return 0

    # 2. Build prompt
    jobs_json = build_jobs_json(jobs)

    # Handle batching if there are too many jobs
    ai_config = settings.get("ai", {})
    max_batch = int(ai_config.get("max_jobs_per_batch", 50))
    model_name = str(ai_config.get("model", "gemini-2.0-flash"))

    if len(jobs) <= max_batch:
        all_matching_ids = _process_single_batch(
            jobs, jobs_json, prompt_template, api_key, model_name
        )
    else:
        logger.info(
            "Batching %d jobs into chunks of %d.", len(jobs), max_batch
        )
        all_matching_ids = []
        for i in range(0, len(jobs), max_batch):
            batch = jobs[i : i + max_batch]
            batch_json = build_jobs_json(batch)
            matching_ids = _process_single_batch(
                batch, batch_json, prompt_template, api_key, model_name
            )
            all_matching_ids.extend(matching_ids)

    # 4. Export
    count = export_jobs_to_send(jobs, all_matching_ids, jobs_to_send_csv_path)

    # Print summary to console
    if count > 0:
        print(f"\n🤖 AI selected {count} jobs for sending. See {jobs_to_send_csv_path}")
    else:
        print("\n🤖 AI did not select any jobs for sending.")

    return count


def _process_single_batch(
    jobs: list[dict[str, str]],
    jobs_json: str,
    prompt_template: str,
    api_key: str,
    model_name: str,
) -> list[str]:
    """Process a single batch: build full prompt, call Gemini, parse result."""
    logger = logging.getLogger("job_alert_bot")

    full_prompt = prompt_template.replace("{jobs_json}", jobs_json)
    logger.info("Sending %d jobs to Gemini for AI filtering.", len(jobs))

    try:
        response_text = call_gemini(full_prompt, api_key, model_name)
        matching_ids = parse_job_ids(response_text)
        return matching_ids
    except Exception:
        logger.exception("AI filtering failed for batch of %d jobs.", len(jobs))
        return []
