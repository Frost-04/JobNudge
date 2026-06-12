# AshbyHQ — Scraping Guide

> **⚠️ MANDATORY READING** before creating or debugging any AshbyHQ scraper.
> AshbyHQ is an ATS (Applicant Tracking System) that powers job boards for many
> companies.  It has a **public board** at `jobs.ashbyhq.com/{company}` and
> supports **two listing patterns** (native board vs custom listing → AshbyHQ
> detail).  Read this first, waste time second.

---

## Platform Fingerprint

If the job listing is at `jobs.ashbyhq.com/{company}` or if detail pages
open at `jobs.ashbyhq.com/{company}/{uuid}`, you are on AshbyHQ.

AshbyHQ is a **React SPA** — the initial HTML is a skeleton; cards render
after client-side hydration.  There is **no Cloudflare or anti-bot blocking**
on the AshbyHQ domain — it is always accessible headlessly.

Two deployment patterns exist:

| Pattern | Listing | Detail | Example |
|---------|---------|--------|---------|
| **Native board** | `jobs.ashbyhq.com/{company}` | Same domain `/{company}/{uuid}` | OpenAI |
| **Custom listing + AshbyHQ detail** | company.com → links to AshbyHQ | `jobs.ashbyhq.com/{company}/{uuid}` | Notion |

---

## ⛔ Rule 1: Card Classes Vary by Pattern — Inspect First

### Pattern A: Native AshbyHQ Board (OpenAI)

The listing is a single-page board with all jobs grouped by department:

```html
<h2 class="ashby-department-heading">
  <span class="ashby-department-heading-level">Applied AI</span>
</h2>
<div class="ashby-job-posting-brief-list">
  <a class="_container_j2da7_1" href="/openai/de06790a-...">
    <div class="_jobPosting_12ilq_378 ashby-job-posting-brief">
      <h3 class="_title_12ilq_382 ashby-job-posting-brief-title">Job Title</h3>
      <div class="_details_12ilq_388 ashby-job-posting-brief-details">
        <p>Department • City, Country • Full time • Hybrid</p>
      </div>
    </div>
  </a>
</div>
```

**Key classes (stable across companies):**

| Element | Selector |
|---------|----------|
| Card container | `div.ashby-job-posting-brief-list a[href*="/{company}/"]` |
| Title | `h3.ashby-job-posting-brief-title` |
| Details (location + type) | `div.ashby-job-posting-brief-details` |
| Department heading | `h2.ashby-department-heading` |

### Pattern B: Custom Listing → AshbyHQ Detail (Notion)

The listing is on the company's own domain with custom CSS-module classes.
Detail pages link to AshbyHQ:

```html
<li class="openPositions_jobsListItem__0mSS9">
  <a href="https://jobs.ashbyhq.com/notion/UUID" class="jobPosting_jobLink__VOc2Y">
    <div class="jobPosting_jobTitle__AbyvH">TITLE</div>
    <div class="jobPosting_jobLocation__Q1A3S">LOCATION</div>
  </a>
</li>
```

**Key insight:** For Pattern B, the listing selectors are **company-specific**
(CSS modules), but the **detail page is always AshbyHQ** and uses the same
universal selectors (see Rule 4).

---

## ⛔ Rule 2: `domcontentloaded` + Settle Always Works

AshbyHQ is a React SPA — the initial HTML is a skeleton.  You need a settle
delay for client-side hydration:

```python
# Native board (Pattern A):
await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
await page.wait_for_timeout(5000)  # Let React hydrate

# Custom listing (Pattern B):
await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
await page.wait_for_timeout(4000)  # Let React hydrate

# Detail page:
await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)
await detail_page.wait_for_timeout(4000)  # Let React hydrate
```

`networkidle` is **not needed** — AshbyHQ has no persistent analytics connections.
`domcontentloaded` + 4-5s settle is always sufficient.

---

## ⛔ Rule 3: Job IDs Are UUIDs

All AshbyHQ job IDs are **UUIDs** from the URL path:

```
https://jobs.ashbyhq.com/openai/de06790a-7243-4e33-a6f1-e7bd34009588
                                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                  UUID job ID
```

```python
import re

def _extract_job_id(self, url: str) -> str:
    if not url:
        return ""
    match = re.search(
        r"/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
        url,
    )
    return match.group(1) if match else ""
```

No numeric IDs, no R/JR prefixes — always UUIDs.

---

## ⛔ Rule 4: Detail Page Selectors Are Universal

The AshbyHQ detail page uses **hashed CSS classes** (e.g. `_descriptionText_5yu8i_201`),
but the **structure is consistent** across ALL companies:

```html
<div class="_descriptionText_5yu8i_201">
  <h1>About the team</h1>
  <p>...</p>
  <h2>Responsibilities</h2>
  <ul><li>...</li></ul>
  <h2>Qualifications</h2>
  <ul><li>...</li></ul>
</div>
```

### ✅ Universal detail page selector:

```python
DESCRIPTION_SELECTOR = 'div._descriptionText_5yu8i_201'
```

The hashed suffix (`5yu8i_201`) is **stable for the current AshbyHQ version**
but should be treated as a **prefix match** in fallbacks:

```python
# Fallback chain for description:
for cls_pattern in ['_descriptionText_', 'description', 'Description', 'job-description', 'posting-body']:
    container = soup.select_one(f'div[class*="{cls_pattern}"]')
    if container:
        break
```

### Description extraction:

```python
def _extract_description(self, soup) -> str:
    container = soup.select_one('div._descriptionText_5yu8i_201')
    if not container:
        return ""

    for unwanted in container.select("script, style, noscript"):
        unwanted.decompose()

    text = container.get_text(separator="\n")
    return self._clean_multiline_text(text)
```

---

## ⛔ Rule 5: Location Extraction Depends on Pattern

### Pattern A (Native board): `•`-separated details

The `ashby-job-posting-brief-details` div contains a `<p>` with
`•`-separated parts:

```
Department • City, Country • Full time • Hybrid
```

The **first part is always the department**, then location, then
employment type, then salary/equity/remote info.

```python
def _extract_location(self, card: Tag) -> str:
    details = card.select_one('div.ashby-job-posting-brief-details')
    if not details:
        return ""

    full_text = details.get_text(" ").strip()

    # Split by bullet separator
    parts = [p.strip() for p in full_text.split("•")]

    if len(parts) < 2:
        return full_text

    # Skip department (parts[0]), collect location parts
    location_parts = []
    for part in parts[1:]:
        part_lower = part.lower()
        # Stop at employment type or salary
        if any(kw in part_lower for kw in [
            "full time", "part time", "contract", "intern",
            "temporary", "full-time", "part-time", "$"
        ]):
            break
        location_parts.append(part)

    return " • ".join(location_parts) if location_parts else parts[1]
```

### Pattern B (Custom listing): Dedicated location class

The custom listing typically has a dedicated location element:

```python
location_el = card.select_one("div.jobPosting_jobLocation__Q1A3S")
# or fallback:
location_el = card.select_one("[class*='jobLocation'], [class*='location']")
location = location_el.get_text(strip=True) if location_el else ""
```

---

## ⛔ Rule 6: Native Board Shows ALL Jobs — Filter Client-Side

The native board (`jobs.ashbyhq.com/{company}`) shows **every job** for the
company (hundreds).  AshbyHQ has **no URL-based location filtering** — the
`?location=India` query param is not supported on the public board.

You must **parse all cards** and filter in Python:

```python
INDIA_KEYWORDS = [
    "india", "bangalore", "bengaluru", "mumbai", "delhi",
    "hyderabad", "pune", "chennai", "gurgaon", "gurugram",
    "noida", "remote india",
]

def _is_india_location(location: str) -> bool:
    if not location:
        return False
    loc_lower = location.lower()
    for keyword in INDIA_KEYWORDS:
        if keyword in loc_lower:
            return True
    return False

# Phase 1: Parse ALL cards (no max_jobs limit)
all_jobs = []
for card in cards:
    job = self._parse_card(card, source_url)
    if job:
        all_jobs.append(job)

# Phase 2: Filter for India and enrich
india_jobs = [j for j in all_jobs if _is_india_location(j.location)]
for job in india_jobs:
    if not self._should_exclude(job.title):
        detail_data = await self._scrape_detail_page(job.url)
        # ...
```

**Do NOT** apply `max_jobs` during card parsing — India jobs may be deep
in the list (e.g. OpenAI has 734 total jobs, only 8 in India, and they
appear after position ~200).  Scan ALL cards, then take `max_jobs` from
the India matches.

---

## ⛔ Rule 7: Hashed Classes Are Stable Per Version

AshbyHQ uses CSS modules with hashed suffixes (e.g. `_container_j2da7_1`,
`_title_12ilq_382`).  These change between AshbyHQ deployments but are
**stable for months at a time**.

Always prefer the **unhashed semantic classes** whenever available:

| Prefer | Avoid |
|--------|-------|
| `div.ashby-job-posting-brief-list` | `div._jobList_12ilq_xxx` |
| `h3.ashby-job-posting-brief-title` | `h3._title_12ilq_382` |
| `div.ashby-job-posting-brief-details` | `div._details_12ilq_388` |

The `ashby-*` prefixed classes are the stable API.  The `_hash_` classes
are generated and may change.

For the **detail page**, the hashed class `div._descriptionText_5yu8i_201`
is the only reliable selector.  If it breaks after an AshbyHQ update, fall
back to `div[class*="_descriptionText_"]`.

---

## Card Selectors That Actually Work

### Pattern A — Native board:

```python
CARD_SELECTOR = 'div.ashby-job-posting-brief-list a[href*="/{company}/"]'
TITLE_SELECTOR = 'h3.ashby-job-posting-brief-title'
DETAILS_SELECTOR = 'div.ashby-job-posting-brief-details'

JOB_CARD_SELECTORS = [
    CARD_SELECTOR,
    'a[href*="/{company}/"][href$="/"]',
    'h3.ashby-job-posting-brief-title',
]
```

### Pattern B — Custom listing:

```python
CARD_SELECTOR = "a[href*='jobs.ashbyhq.com/{company}/']"
TITLE_SELECTOR = "[class*='jobTitle'], [class*='title']"
LOCATION_SELECTOR = "[class*='jobLocation'], [class*='location']"
```

### Detail page (both patterns):

```python
DESCRIPTION_SELECTOR = 'div._descriptionText_5yu8i_201'

# Fallback chain for wait:
detail_selectors = [
    'div._descriptionText_5yu8i_201',
    'h1',
    'article',
    'div[class*="description"]',
]
```

---

## Company Variations

| Company | Pattern | Notes |
|---------|--------|-------|
| **OpenAI** | Native board | Cloudflare on `openai.com` → scrape AshbyHQ directly; 734 total jobs, ~8 India; client-side filter |
| **Notion** | Custom listing + AshbyHQ detail | Multi-URL support; listing on `notion.com/careers`, detail on `jobs.ashbyhq.com/notion/{uuid}` |

---

## Summary: The AshbyHQ Playbook

```
1. IDENTIFY THE PATTERN:
   → Listing at jobs.ashbyhq.com/{company} → Pattern A (native board)
   → Listing at company.com, detail at jobs.ashbyhq.com → Pattern B (custom)

2. PATTERN A (NATIVE BOARD):
   goto(domcontentloaded) → wait 5s for React hydration
   → cards = soup.select('div.ashby-job-posting-brief-list a')
   → title from h3.ashby-job-posting-brief-title
   → location from div.ashby-job-posting-brief-details (•-split, skip dept + type)
   → job_id = UUID from URL path
   → Parse ALL cards (no max_jobs limit) → filter India client-side
   → detail from div._descriptionText_5yu8i_201

3. PATTERN B (CUSTOM LISTING → ASHBYHQ DETAIL):
   → Parse custom listing selectors (company-specific)
   → job_id = UUID from AshbyHQ URL
   → detail from div._descriptionText_5yu8i_201 (same as Pattern A)

4. KNOWN ISSUES:
   - Native board shows ALL jobs — India filter must be client-side
   - Hashed classes (_descriptionText_5yu8i_201) may change on AshbyHQ updates
   - No URL-based location filtering on native board
   - React hydration needs 4-5s settle; domcontentloaded alone misses cards
```
