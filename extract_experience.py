"""Temporary script: extract experience snippets from seen_jobs.db (or new_jobs.csv)
and write them to data/experience_extracts.csv."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Any

from src.utils.config_loader import load_keywords, load_settings
from src.utils.text_utils import extract_experience_snippets

OUTPUT_PATH = Path("data/experience_extracts.csv")
DB_PATH = Path("data/seen_jobs.db")
CSV_PATH = Path("data/new_jobs.csv")


def _from_db() -> list[dict[str, Any]]:
    """Read all rows from seen_jobs."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "SELECT company, url, description FROM seen_jobs ORDER BY company, title"
        )
        return [
            {"company": row[0], "url": row[1], "description": row[2] or ""}
            for row in cursor.fetchall()
        ]


def _from_csv() -> list[dict[str, Any]]:
    """Read from new_jobs.csv if DB is unavailable."""
    rows: list[dict[str, Any]] = []
    with CSV_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(
                {
                    "company": row.get("company", ""),
                    "url": row.get("url", ""),
                    "description": row.get("description", ""),
                }
            )
    return rows


def main() -> None:
    # 1. Load experience keywords
    keywords_config = load_keywords()
    if not isinstance(keywords_config, dict):
        keywords_config = {}
    keywords = keywords_config.get("experience_keywords", []) or []
    print(f"Loaded {len(keywords)} experience keywords.")

    # 2. Read jobs from DB, fall back to CSV
    if DB_PATH.exists():
        jobs = _from_db()
        source = f"seen_jobs.db ({len(jobs)} rows)"
    elif CSV_PATH.exists():
        jobs = _from_csv()
        source = f"new_jobs.csv ({len(jobs)} rows)"
    else:
        print("Neither seen_jobs.db nor new_jobs.csv found. Nothing to process.")
        return

    print(f"Reading from: {source}")

    # 3. Extract snippets
    results: list[dict[str, str]] = []
    for job in jobs:
        snippets = extract_experience_snippets(job["description"], keywords)
        results.append(
            {
                "company": job["company"],
                "url": job["url"],
                "extracted_experience_parts": snippets,
            }
        )

    # 4. Write output
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["company", "url", "extracted_experience_parts"]
        )
        writer.writeheader()
        writer.writerows(results)

    non_empty = sum(1 for r in results if r["extracted_experience_parts"])
    print(f"Wrote {len(results)} rows → {OUTPUT_PATH}")
    print(f"  {non_empty} jobs had experience snippets extracted")


if __name__ == "__main__":
    main()
