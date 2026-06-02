from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from src.utils.text_utils import normalize_location, normalize_text
from src.utils.url_utils import normalize_url


@dataclass(slots=True)
class Job:
    company: str
    title: str
    location: str
    url: str
    source_url: str
    job_id: str = "0"
    posted_date: str | None = None
    description: str | None = None
    scraped_at: str = ""
    matched_keywords: list[str] = field(default_factory=list)

    def unique_key(self) -> str:
        base = "|".join(
            [
                normalize_text(self.company),
                normalize_text(self.title),
                normalize_location(self.location),
                normalize_url(self.url),
            ]
        )
        return hashlib.sha256(base.encode("utf-8")).hexdigest()
