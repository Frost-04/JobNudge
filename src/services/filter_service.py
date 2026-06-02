from __future__ import annotations

from src.models.job import Job
from src.utils.text_utils import contains_any, find_matching_keywords, normalize_location


def filter_jobs(jobs: list[Job], keywords_config: dict) -> list[Job]:
    include_keywords = keywords_config.get("include", []) or []
    exclude_keywords = keywords_config.get("exclude", []) or []
    locations = keywords_config.get("locations", []) or []
    normalized_locations = [normalize_location(loc) for loc in locations if loc]

    location_matched: list[Job] = []
    location_unmatched: list[Job] = []

    for job in jobs:
        searchable_text = " ".join(
            part for part in [job.title, job.location, job.description or ""] if part
        )

        matched = (
            find_matching_keywords(searchable_text, include_keywords)
            if include_keywords
            else []
        )
        if include_keywords and not matched:
            continue
        if exclude_keywords and contains_any(searchable_text, exclude_keywords):
            continue

        job.matched_keywords = matched

        if normalized_locations:
            location_text = normalize_location(searchable_text)
            if any(location in location_text for location in normalized_locations):
                location_matched.append(job)
            else:
                location_unmatched.append(job)
        else:
            location_matched.append(job)

    return location_matched + location_unmatched
