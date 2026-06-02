from __future__ import annotations

from src.models.job import Job


def deduplicate_jobs(jobs: list[Job]) -> list[Job]:
    seen: set[str] = set()
    unique_jobs: list[Job] = []

    for job in jobs:
        key = job.unique_key()
        if key in seen:
            continue
        seen.add(key)
        unique_jobs.append(job)

    return unique_jobs
