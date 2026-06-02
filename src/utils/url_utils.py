from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {"gclid", "fbclid", "igshid", "mc_cid", "mc_eid"}
JOB_ID_KEYS = {
    "job_id",
    "jobid",
    "jobId",
    "job",
    "id",
    "reqid",
    "reqId",
    "requisitionid",
    "requisitionId",
    "requisition_id",
}
JOB_ID_KEYS_LOWER = {key.lower() for key in JOB_ID_KEYS}

_JOB_ID_PATH_RE = re.compile(r"/jobs/(?:results/)?([A-Za-z0-9_-]+)")
_JOB_ID_ALT_PATH_RE = re.compile(r"/job/(?:details/)?([A-Za-z0-9_-]+)")


def normalize_url(url: str | None) -> str:
    if not url:
        return ""

    url = url.strip()
    parts = urlsplit(url)
    query_params = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        key_lower = key.lower()
        if key_lower in TRACKING_KEYS or any(key_lower.startswith(prefix) for prefix in TRACKING_PREFIXES):
            continue
        query_params.append((key, value))

    cleaned_query = urlencode(query_params, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, cleaned_query, ""))


def make_absolute_url(base_url: str, maybe_relative_url: str | None) -> str:
    if not maybe_relative_url:
        return ""

    parts = urlsplit(maybe_relative_url)
    if parts.scheme and parts.netloc:
        return normalize_url(maybe_relative_url)

    return normalize_url(urljoin(base_url, maybe_relative_url))


def extract_job_id(url: str | None) -> str:
    if not url:
        return "0"

    parts = urlsplit(url)
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in JOB_ID_KEYS_LOWER:
            cleaned = value.strip()
            if cleaned:
                return cleaned

    path = parts.path or ""
    match = _JOB_ID_PATH_RE.search(path)
    if match:
        return match.group(1)

    match = _JOB_ID_ALT_PATH_RE.search(path)
    if match:
        return match.group(1)

    return "0"
