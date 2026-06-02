from __future__ import annotations

import csv
from pathlib import Path

from src.models.job import Job


def export_latest_jobs_to_csv(jobs: list[Job], path: str) -> None:
    csv_path = Path(path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "job_id",
        "company",
        "title",
        "location",
        "url",
        "posted_date",
        "description",
        "matched_keywords",
        "scraped_at",
    ]

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for job in jobs:
            writer.writerow(
                {
                    "job_id": job.job_id,
                    "company": job.company,
                    "title": job.title,
                    "location": job.location,
                    "url": job.url,
                    "posted_date": job.posted_date or "",
                    "description": job.description or "",
                    "matched_keywords": ", ".join(job.matched_keywords),
                    "scraped_at": job.scraped_at,
                }
            )
