# Workday — Scraping Guide

> **⚠️ MANDATORY READING** before creating or debugging any Workday scraper.
> Workday job boards share consistent `data-automation-id` selectors across all
> companies, but the SPA rendering and cookie popups vary.  Read this first,
> waste time second.

---

## Platform Fingerprint

If the URL contains `wd5.myworkdayjobs.com` or `wd1.myworkdayjobs.com`, you are on
Workday.  The JS framework is a Workday-proprietary SPA.  All key elements have
stable `data-automation-id` attributes — this is the single best thing about
scraping Workday.

The initial HTML payload may be a skeleton — cards render asynchronously after
`networkidle` or a settle delay.

---

## ⛔ Rule 1: `networkidle` Works (Unlike Oracle Cloud), But Is Slow

Workday pages do NOT have the persistent analytics/chat connections that kill
`networkidle` on Oracle Cloud.  `networkidle` works reliably.  However:

| Approach | Result |
|---|---|
| `networkidle` | Works, but ~30-45s for 20-job pages |
| `domcontentloaded` + 3s settle | Works, faster (~15-20s), **recommended** |
| `commit` + wait | Risky — JS may not have populated the DOM yet |

### ✅ Recommended:

```python
await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
await asyncio.sleep(3)  # Let SPA render finish
```

For pages with **cookie popups** or **slow CDN assets**, add a `wait_for_selector`
on the results container before the settle:

```python
await page.goto(source_url, wait_until="networkidle", timeout=120000)
await page.wait_for_selector('section[data-automation-id="jobResults"]', timeout=45000)
```

---

## ⛔ Rule 2: `data-automation-id` Selectors Are Universal

Every Workday job board uses the **exact same** `data-automation-id` attributes.
Once you learn these selectors, you can scrape ANY Workday company:

### Card selector:

```python
CARD_SELECTOR = 'section[data-automation-id="jobResults"] ul[role="list"] > li'
TITLE_SELECTOR = 'a[data-automation-id="jobTitle"]'
LOCATION_SELECTOR = '[data-automation-id="locations"] dd'
POSTED_SELECTOR = '[data-automation-id="postedOn"] dd'
JOB_ID_SELECTOR = '[data-automation-id="subtitle"] li'

# Detail page
DETAIL_CONTENT_SELECTOR = '[data-automation-id="jobPostingDescription"]'
```

### Detecting the right card selector variations:

Some companies use slightly different UL markup:

```python
# Best: matches ul with role="list" and aria-label starting with "Page"
CARD_SELECTOR = 'section[data-automation-id="jobResults"] ul[role="list"] > li'

# Fallback: any li inside the results section
CARD_SELECTOR = 'section[data-automation-id="jobResults"] li'
```

---

## ⛔ Rule 3: Job IDs Are R‑Prefixed or JR‑Prefixed

Workday job IDs follow one of two patterns:

| Pattern | Example | Regex |
|---------|---------|-------|
| R‑prefixed | `R263019` | `text.upper().startswith("R") and any(ch.isdigit())` |
| JR‑prefixed | `JR0284344` | `text.upper().startswith("JR")` |

The ID lives in `[data-automation-id="subtitle"] li`:

```python
subtitle_items = card.select('[data-automation-id="subtitle"] li')
for item in subtitle_items:
    text = clean_text(item.get_text())
    if text.upper().startswith("R") and any(ch.isdigit() for ch in text):
        job_id = text
        break
```

Fallback: extract from URL path if the subtitle approach fails.

---

## ⛔ Rule 4: Detail Pages Are Reliable But Slow

Workday detail pages **always work** — no captcha, no interstitial, no blocking.
The content is in `[data-automation-id="jobPostingDescription"]`.

### Detail page pattern:

```python
await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)
await detail_page.wait_for_selector(
    '[data-automation-id="jobPostingDescription"]',
    timeout=15000,
)
soup = await self._get_soup(detail_page)
desc = soup.select_one('[data-automation-id="jobPostingDescription"]')
```

### ⚠️ Performance concern:

Detail page enrichment is the **slowest part** of Workday scraping.  Each detail
page takes 10-15s.  For 20 jobs, that's 3-5 minutes of detail enrichment alone.

**Mitigation:** The standard `_should_exclude()` title filter skips Senior/Staff/
Lead/Principal roles, cutting enrichment time significantly.

---

## ⛔ Rule 5: Boilerplate Headings in Descriptions

Workday descriptions often include UI labels as `<h1>` headings like
"Job Details:", "Posting Statement:", "Position of Trust", etc.  These add
no value and should be filtered:

```python
BOILERPLATE_HEADINGS = {
    "job details:",
    "posting statement:",
    "position of trust",
    "additional locations:",
    "work model for this role",
}
```

---

## ⛔ Rule 6: Posted Dates Use Relative Text

Posted dates on Workday cards are **relative text**, not ISO dates:

| Card text | Meaning |
|-----------|---------|
| `Posted Today` | Today |
| `Posted Yesterday` | Yesterday |
| `Posted 7 Days Ago` | 7 days ago |
| `Posted 30+ Days Ago` | > 30 days |

These are preserved as-is in the `posted_date` field.  The AI pipeline handles
the parsing.

---

## Card Selectors That Actually Work

### Most reliable (in order):

```python
CARD_SELECTOR = (
    'section[data-automation-id="jobResults"] ul[role="list"] > li,'
    'section[data-automation-id="jobResults"] li'
)
TITLE_SELECTOR = 'a[data-automation-id="jobTitle"]'
LOCATION_SELECTOR = '[data-automation-id="locations"] dd'
POSTED_SELECTOR = '[data-automation-id="postedOn"] dd'
JOB_ID_SELECTOR = '[data-automation-id="subtitle"] li'
```

### Wait chain:

```python
async def _wait_for_results(self, page):
    selectors = [
        'section[data-automation-id="jobResults"]',
        'a[data-automation-id="jobTitle"]',
    ]
    for selector in selectors:
        try:
            await page.wait_for_selector(selector, timeout=45000)
            return
        except Exception:
            continue
```

### Location from card:

```python
loc_els = card.select('[data-automation-id="locations"] dd')
if loc_els:
    location = clean_text(loc_els[0].get_text())
# Returns: "India, Bangalore, Nova"
```

### Detail description extraction:

The `_extract_description()` method walks `[data-automation-id="jobPostingDescription"]`
children, separating headings (h1-h4) from body content (p, ul, ol, li) and
preserving structure with newlines.

---

## Company Variations

| Company | `networkidle` works? | Extra delay? | Notes |
|---------|---------------------|--------------|-------|
| **Intel** | ✅ Yes | No | Original reference scraper |
| **Red Hat** | ✅ Yes | No | Identical to Intel |
| **Ciena** | ✅ Yes | **3s settle** | Cards load slower than typical Workday |
| **Samsung** | ✅ Yes | No | Pre‑filtered by location via URL params |
| **ThoughtSpot** | ✅ Yes | No | JR‑prefixed job IDs |
| **Analog Devices** | ✅ Yes | **3s settle** | Pre‑filtered URL (India + Engineering). R‑prefixed IDs |

---

## Summary: The Workday Playbook

```
1. WAIT STRATEGY:
   goto(wait_until="networkidle" or "domcontentloaded", timeout=60000-120000)
   → wait_for_selector('section[data-automation-id="jobResults"]')
   → asyncio.sleep(3) if cards don't appear consistently

2. CARD PARSING:
   cards = soup.select('section[data-automation-id="jobResults"] ul[role="list"] > li')
   → title from a[data-automation-id="jobTitle"]
   → location from [data-automation-id="locations"] dd
   → posted date from [data-automation-id="postedOn"] dd
   → job ID from [data-automation-id="subtitle"] li (R/JR‑prefixed)

3. DETAIL ENRICHMENT:
   detail_page.goto(job_url, "domcontentloaded", timeout=60000)
   → wait for [data-automation-id="jobPostingDescription"]
   → extract structured description (skip boilerplate headings)
   → title-based exclusion for Senior/Staff/Lead/Principal

4. KNOWN ISSUES:
   - Detail enrichment is slow (10-15s per job) — exclusion filter helps
   - Cookie popups may need dismissal (rare on Workday)
   - Posted dates are relative text, not ISO
```
