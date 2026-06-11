from __future__ import annotations

import re
from typing import Any

from src.models.job import Job
from src.utils.text_utils import extract_experience_snippets


def _build_title_blacklist_pattern(keywords: list[str]) -> re.Pattern[str] | None:
    """Build a case-insensitive whole-word OR regex from a list of title blacklist terms.
    
    Keywords ending with a period (like "sr.") have the trailing period stripped
    so they can match across word boundaries (e.g. "Sr. Developer")."""
    if not keywords:
        return None
    cleaned = []
    for k in keywords:
        k = k.strip()
        if not k:
            continue
        # Strip trailing dot so "sr." can match "sr." as well as "Sr" in "Sr. Developer"
        if k.endswith("."):
            k = k[:-1]
        cleaned.append(k)
    if not cleaned:
        return None
    escaped = sorted((re.escape(k) for k in cleaned if k), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)


def _build_year_reject_pattern(
    year_min: int = 2,
    year_max: int = 20,
) -> re.Pattern[str] | None:
    """Build a regex that matches 'X+ years', 'X years', 'X-year', 'X+year'
    where X is between *year_min* and *year_max* (inclusive)."""
    if year_min > year_max or year_min < 1:
        return None
    # e.g. (?:2|3|4|5|6|7|8|9|1[0-9]|20)
    if year_max <= 9:
        num_pat = "|".join(str(n) for n in range(year_min, year_max + 1))
    else:
        single = "|".join(str(n) for n in range(year_min, min(year_max, 9) + 1))
        teens = f"{max(year_min, 10)}|" if year_min <= 19 else ""
        if year_max >= 10:
            teen_start = max(year_min, 10)
            teen_end = min(year_max, 19)
            if teen_start <= teen_end:
                teens = "|".join(str(n) for n in range(teen_start, teen_end + 1))
            else:
                teens = ""
        if year_max >= 20:
            twenty = "|20" if year_max >= 20 else ""
        else:
            twenty = ""
        parts = []
        if single:
            parts.append(single)
        if teens:
            parts.append(teens)
        if twenty:
            parts.append("20")
        num_pat = "|".join(parts)
    return re.compile(
        r"\b(?:" + num_pat + r")\+?\s*years?\b",
        re.IGNORECASE,
    )


def _matches_any(text: str, keywords: list[str]) -> bool:
    """Check if any keyword appears as a whole-word/phrase match in text (case-insensitive).

    Uses ``\\b`` word-boundary anchors so that ``"intern"`` does NOT accidentally
    match ``"International"``, ``"internal"``, etc.
    """
    if not keywords:
        return False
    text_lower = text.lower()
    for kw in keywords:
        kw_lower = kw.lower().strip()
        if not kw_lower:
            continue
        # Build a word-boundary pattern: \b<escaped_keyword>\b
        # Handles multi-word phrases (e.g. "new grad") and hyphenated terms
        # (e.g. "entry-level") correctly.
        pattern = r"\b" + re.escape(kw_lower) + r"\b"
        if re.search(pattern, text_lower):
            return True
    return False


def _title_contains_blacklisted(title: str, pattern: re.Pattern[str] | None) -> bool:
    """Return True if the title matches any blacklisted term."""
    if pattern is None:
        return False
    return bool(pattern.search(title))


def _has_year_requirement(description: str, pattern: re.Pattern[str] | None) -> bool:
    """Return True if a year-pattern like '2+ years' is found."""
    if pattern is None:
        return False
    return bool(pattern.search(description))


def pre_filter_jobs(
    jobs: list[dict[str, str]],
    keywords_config: dict[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Classify CSV job dicts into auto_accept, auto_reject, and uncertain buckets.

    Rules (first match wins):
      1. auto_accept: keyword match in title OR description
      2. auto_reject: title contains blacklisted word (senior / staff / principal…)
      3. auto_reject: description contains "X+ years" / "X years" where X ∈ [2, 20]
      4. Everything else → uncertain (goes to AI)

    Auto-accept is checked FIRST — a job matching "software engineer i" is
    accepted even if the title also contains "sr." or "staff".

    Returns:
        (auto_accepted, auto_rejected, uncertain) — three lists of job dicts.
    """
    auto_reject_cfg = keywords_config.get("auto_reject", {}) or {}
    auto_accept_cfg = keywords_config.get("auto_accept", {}) or {}

    accept_keywords: list[str] = auto_accept_cfg.get("keywords", []) or []
    title_blacklist: list[str] = auto_reject_cfg.get("title_blacklist", []) or []
    year_min: int = int(auto_reject_cfg.get("year_min", 2))
    year_max: int = int(auto_reject_cfg.get("year_max", 20))

    title_blacklist_pat = _build_title_blacklist_pattern(title_blacklist)
    year_pat = _build_year_reject_pattern(year_min, year_max)

    auto_accepted: list[dict[str, str]] = []
    auto_rejected: list[dict[str, str]] = []
    uncertain: list[dict[str, str]] = []

    for job in jobs:
        job_id = job.get("job_id", "")
        title = job.get("title", "") or ""
        description = job.get("description", "") or ""
        title_lower = title.lower()
        combined_lower = (title + " " + description).lower()

        # 1. Auto-accept (highest priority)
        if _matches_any(combined_lower, accept_keywords):
            auto_accepted.append(job)
            continue

        # 2. Title blacklist
        if _title_contains_blacklisted(title_lower, title_blacklist_pat):
            auto_rejected.append(job)
            continue

        # 3. Year requirement pattern
        if _has_year_requirement(description, year_pat):
            auto_rejected.append(job)
            continue

        # 4. Uncertain → send to AI
        uncertain.append(job)

    return auto_accepted, auto_rejected, uncertain


def extract_experience_from_jobs(
    jobs: list[Job],
    experience_keywords: list[str],
) -> list[Job]:
    """Populate the ``extracted_experience_parts`` field on every job.

    For each job description, scans for occurrences of the given experience-
    related keywords and stores the resulting snippets in the job's
    ``extracted_experience_parts`` field.

    Returns the same list (mutated in place) for chaining convenience.
    """
    for job in jobs:
        job.extracted_experience_parts = extract_experience_snippets(
            job.description,
            experience_keywords,
        )
    return jobs
