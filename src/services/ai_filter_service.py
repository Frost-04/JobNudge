from __future__ import annotations

import csv
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import requests

from src.services.filter_service import pre_filter_jobs


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
    """Build a compact JSON string containing only the fields Gemini needs.

    Sends extracted_experience_parts instead of the full description so the AI
    only sees experience-related snippets (up to 8 words before + 3 words after
    each experience keyword match).  Falls back to description if no snippets
    were extracted.
    """
    slim_jobs = []
    for job in jobs:
        exp_parts = job.get("extracted_experience_parts", "")
        description = job.get("description", "")
        # Prefer experience snippets; fall back to full description if empty
        exp_text = exp_parts.strip() if exp_parts.strip() else description
        slim_jobs.append(
            {
                "job_id": job.get("job_id", ""),
                "company": job.get("company", ""),
                "title": job.get("title", ""),
                "url": job.get("url", ""),
                "description": exp_text,
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


def call_deepseek(prompt: str, api_key: str, model_name: str) -> str:
    """Send a prompt to DeepSeek and return the response text.

    Uses the OpenAI-compatible chat completions API.
    """
    logger = logging.getLogger("job_alert_bot")
    logger.info("Calling DeepSeek model: %s", model_name)

    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
    }

    response = requests.post(url, json=payload, headers=headers, timeout=120)
    response.raise_for_status()

    data = response.json()
    content = data["choices"][0]["message"]["content"]

    logger.info("DeepSeek response received (%d chars).", len(content))
    return content


def call_ai(
    prompt: str,
    api_key: str,
    model_name: str,
    provider: str,
) -> str:
    """Dispatch to the correct AI provider (gemini or deepseek)."""
    if provider == "deepseek":
        return call_deepseek(prompt, api_key, model_name)
    return call_gemini(prompt, api_key, model_name)


def parse_job_ids(response_text: str) -> list[str]:
    """Extract a JSON array of job_ids from the AI response text."""
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
        # Clear the file entirely (same behavior as new_jobs.csv when empty)
        path.write_text("", encoding="utf-8")
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
    keywords_config: dict[str, Any] | None = None,
    provider: str = "gemini",
    model_name: str = "gemini-2.5-flash",
) -> int:
    """
    Full AI filtering pipeline with optional pre-filtering:

    1. Read jobs from new_jobs.csv
    2. Pre-filter into auto_accept / auto_reject / uncertain (if keywords_config provided)
    3. Send uncertain jobs to AI (Gemini or DeepSeek)
    4. Export auto_accepted + AI-matched jobs to jobs_to_send.csv

    Returns the number of jobs exported.
    """
    logger = logging.getLogger("job_alert_bot")

    # 1. Read jobs
    jobs = read_jobs_from_csv(new_jobs_csv_path)
    if not jobs:
        logger.info("No new jobs to filter with AI. Skipping.")
        path = Path(jobs_to_send_csv_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return 0

    # ── Pre-filter: classify jobs before AI ──────────────────────────
    auto_accepted: list[dict[str, str]] = []
    auto_rejected: list[dict[str, str]] = []
    uncertain: list[dict[str, str]] = jobs  # default: all go to AI

    if keywords_config:
        auto_accepted, auto_rejected, uncertain = pre_filter_jobs(
            jobs, keywords_config
        )
        logger.info(
            "Pre-filter: %d auto-accepted, %d auto-rejected, %d uncertain → AI",
            len(auto_accepted),
            len(auto_rejected),
            len(uncertain),
        )
        # Print summary to console
        if auto_accepted:
            print(f"\n✅ Auto-accepted {len(auto_accepted)} job(s) — skipping AI")
        if auto_rejected:
            print(f"❌ Auto-rejected {len(auto_rejected)} job(s) — not eligible")
        if uncertain:
            print(f"🤖 {len(uncertain)} job(s) need AI review")
    else:
        logger.info("No keywords config provided — sending all %d jobs to AI.", len(jobs))

    # Check API key
    if not api_key:
        logger.error("AI API key is not set in .env. Skipping AI filtering.")
        path = Path(jobs_to_send_csv_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # If we have auto-accepted jobs, export them even without AI
        if auto_accepted and keywords_config:
            logger.info("Exporting %d auto-accepted jobs (AI skipped).", len(auto_accepted))
            return export_jobs_to_send(auto_accepted, [j.get("job_id", "") for j in auto_accepted], jobs_to_send_csv_path)
        path.write_text("", encoding="utf-8")
        return 0

    # ── AI filtering on uncertain jobs only ──────────────────────────
    ai_matched_ids: list[str] = []

    if uncertain:
        ai_config = settings.get("ai", {})
        max_batch = int(ai_config.get("max_jobs_per_batch", 50))

        if len(uncertain) <= max_batch:
            uncertain_json = build_jobs_json(uncertain)
            ai_matched_ids = _process_single_batch(
                uncertain, uncertain_json, prompt_template, api_key,
                model_name, provider,
            )
        else:
            logger.info("Batching %d uncertain jobs into chunks of %d.", len(uncertain), max_batch)
            ai_matched_ids = []
            for i in range(0, len(uncertain), max_batch):
                batch = uncertain[i : i + max_batch]
                batch_json = build_jobs_json(batch)
                matching_ids = _process_single_batch(
                    batch, batch_json, prompt_template, api_key,
                    model_name, provider,
                )
                ai_matched_ids.extend(matching_ids)
    else:
        logger.info("No uncertain jobs to send to AI.")

    # ── Export: auto_accepted + AI-matched ───────────────────────────
    # Use the original full job list for export (preserves all columns).
    # Build a combined matching-ID set from auto_accept + AI results.
    ai_matched_set = set(ai_matched_ids)
    auto_accept_ids = {j.get("job_id", "") for j in auto_accepted}

    all_matched_ids = list(auto_accept_ids | ai_matched_set)

    count = export_jobs_to_send(jobs, all_matched_ids, jobs_to_send_csv_path)

    if count > 0:
        print(f"\n🤖 Final: {count} job(s) exported to {jobs_to_send_csv_path}")
        if auto_accepted:
            print(f"   ({len(auto_accept_ids)} auto-accepted, {len(ai_matched_set)} AI-selected)")
    else:
        print("\n🤖 No jobs selected for sending.")

    return count


def _process_single_batch(
    jobs: list[dict[str, str]],
    jobs_json: str,
    prompt_template: str,
    api_key: str,
    model_name: str,
    provider: str = "gemini",
) -> list[str]:
    """Process a single batch: build full prompt, call AI, parse result."""
    logger = logging.getLogger("job_alert_bot")

    full_prompt = prompt_template.replace("{jobs_json}", jobs_json)
    logger.info(
        "Sending %d jobs to %s (%s) for AI filtering.",
        len(jobs), provider, model_name,
    )

    try:
        response_text = call_ai(full_prompt, api_key, model_name, provider)
        matching_ids = parse_job_ids(response_text)
        return matching_ids
    except Exception:
        logger.exception("AI filtering failed for batch of %d jobs.", len(jobs))
        return []
