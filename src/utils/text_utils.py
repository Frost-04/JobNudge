from __future__ import annotations

import re
from typing import Iterable

_SPACE_RE = re.compile(r"\s+")


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    return _SPACE_RE.sub(" ", text.strip().lower())


def contains_any(text: str | None, keywords: Iterable[str]) -> bool:
    normalized_text = normalize_text(text)
    for keyword in keywords:
        keyword_normalized = normalize_text(keyword)
        if keyword_normalized and keyword_normalized in normalized_text:
            return True
    return False


def find_matching_keywords(text: str | None, keywords: Iterable[str]) -> list[str]:
    normalized_text = normalize_text(text)
    matched: list[str] = []
    seen: set[str] = set()
    for keyword in keywords:
        keyword_normalized = normalize_text(keyword)
        if keyword_normalized and keyword_normalized in normalized_text:
            if keyword_normalized not in seen:
                matched.append(keyword_normalized)
                seen.add(keyword_normalized)
    return matched


def normalize_location(location: str | None) -> str:
    normalized = normalize_text(location)
    if "bengaluru" in normalized:
        normalized = normalized.replace("bengaluru", "bangalore")
    return normalized
