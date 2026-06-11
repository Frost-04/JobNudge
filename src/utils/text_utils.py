from __future__ import annotations

import re
from typing import Iterable

_SPACE_RE = re.compile(r"\s+")


def _build_experience_pattern(keywords: list[str]) -> re.Pattern | None:
    """Build a case-insensitive regex that matches any of the given keywords as whole tokens."""
    if not keywords:
        return None
    escaped = sorted((re.escape(k) for k in keywords if k), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)


def extract_experience_snippets(
    text: str | None,
    keywords: list[str],
    words_before: int = 8,
    words_after: int = 3,
) -> str:
    """Extract contextual snippets around experience-related keywords.

    For each occurrence of a keyword in ``text``:
      - Grabs up to *words_before* words preceding the keyword and up to
        *words_after* words following it.
      - Stops at a newline on either side (but does **not** stop at a full
        stop, because abbreviations like "e.g." or "i.e." could be present).
      - Returns a triple-double-quoted, comma-separated string of all
        unique snippets.
    """
    if not text or not keywords:
        return ""

    pattern = _build_experience_pattern(keywords)
    if pattern is None:
        return ""

    # Normalise line endings.
    clean = text.replace("\r\n", "\n").replace("\r", "\n")

    snippets: list[str] = []
    seen: set[str] = set()

    for match in pattern.finditer(clean):
        kw_start = match.start()
        kw_end = match.end()
        kw_text = match.group(0)

        # ---- before context (stop at newline) -------------------------------
        before_text = clean[:kw_start]
        last_nl = before_text.rfind("\n")
        before_segment = before_text[last_nl + 1 :] if last_nl >= 0 else before_text
        before_words = before_segment.split()
        before_words = before_words[-words_before:] if len(before_words) > words_before else before_words

        # ---- after context (stop at newline) --------------------------------
        after_text = clean[kw_end:]
        first_nl = after_text.find("\n")
        after_segment = after_text[:first_nl] if first_nl >= 0 else after_text
        after_words = after_segment.split()[:words_after]

        # ---- assemble snippet ------------------------------------------------
        snippet_parts = before_words + [kw_text] + after_words
        snippet = " ".join(snippet_parts).strip()
        if snippet and snippet not in seen:
            seen.add(snippet)
            snippets.append(snippet)

    if not snippets:
        return ""

    return "; ".join(snippets)


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
