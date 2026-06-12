# Oracle Cloud Candidate Experience — Hard-Won Lessons

> **⚠️ MANDATORY READING** before creating or debugging any Oracle Cloud scraper.
> This doc exists because these sites break every "obvious" approach. Read it first,
> waste time second.

---

## Platform Fingerprint

If the URL contains `/hcmUI/CandidateExperience/` or the company's career site redirects
to `*.fa.ocs.oraclecloud.com`, you are on Oracle Cloud. The JS framework is **Knockout.js**
with custom `<posting-locations>`, `<search-result-item-header>`, and `<job-tags>` web
components. The page is an SPA — the initial HTML payload is a skeleton with `<ul>` and
`<!-- ko -->` templates; real data is bound at runtime by Knockout.

---

## ⛔ Rule 1: `networkidle` Is a Trap — Never Use It

`page.goto(url, wait_until="networkidle")` **will hang forever** on Oracle Cloud pages.

**Why:** These pages load analytics beacons, chat widgets (Oracle Digital Assistant), and
long-polling XHRs that never "finish". Playwright's `networkidle` waits for 500ms of zero
network activity, which **never happens**.

### ✅ What works:

```python
# Fastest reliable approach:
await page.goto(url, wait_until="commit", timeout=30000)

# Then give Knockout.js time to hydrate:
await page.wait_for_timeout(8000)

# Verify cards rendered via JS evaluation (not wait_for_selector):
card_count = await page.evaluate(
    "() => document.querySelectorAll('span.job-tile__title').length"
)
```

**Why `commit`:** Returns as soon as the response headers arrive and the browser
commits to the navigation. Much faster than `domcontentloaded` and avoids the
analytics hang that kills `networkidle`.

**Why JS evaluation over `wait_for_selector`:** `wait_for_selector` also waits on
network activity resolution by default. Oracle's ongoing analytics streams cause it
to timeout unpredictably. `page.evaluate` runs immediately against the current DOM.

---

## ⛔ Rule 2: `domcontentloaded` Hangs Intermittently

`wait_until="domcontentloaded"` *sometimes* works but **sometimes hangs** for 30+ seconds
before timing out. The root cause is the same: slow third-party resources (analytics,
Oracle chat, CDN assets) blocking the `DOMContentLoaded` event.

### What we tried:

| Approach | Result |
|---|---|
| `networkidle` followed by `domcontentloaded` on timeout | Hung forever |
| `domcontentloaded` alone | Worked ~30% of the time, hung the other 70% |
| `domcontentloaded` + 15s additional wait | Still hung sometimes; added 15s overhead |
| **`commit` + 8s settle + JS evaluation** | **Works 100% of the time, fastest** |

### ⏱️ Timing

- After `commit`, 8 seconds of `wait_for_timeout` is the sweet spot.
  - 5s: Knockout sometimes hasn't finished rendering all 25 cards.
  - 8s: All cards rendered reliably.
  - 10s: Works but wastes 2s per run.

---

## ⛔ Rule 3: Cards Are Knockout-Bound — BS4 Sees Nothing at First

The initial HTML from the server is a skeleton:

```html
<ul id="panel-list" class="jobs-list__list">
    <!-- ko foreach: { data: requisitions, as: 'job' } -->
    <!-- ko ifnot: job.id === 'CE_TALENT_COMMUNITY_TILE' -->
    <!-- /ko -->
    <!-- /ko -->
</ul>
```

All the `<li data-qa="searchResultItem">` cards, `<span class="job-tile__title">` titles,
and `<posting-locations>` components are **dynamically bound** by Knockout.js **after**
page load. BS4 scraping before the settle wait sees **zero cards**.

### ✅ What works:

1. Wait 8s after `commit`
2. Verify with `page.evaluate` that `querySelectorAll('span.job-tile__title')` returns > 0
3. Then call `page.content()` and pipe to BS4

### Diagnostic checklist when 0 jobs appear:

```python
# Check if JS has rendered cards yet:
js_count = await page.evaluate(
    "() => document.querySelectorAll('span.job-tile__title').length"
)
# Check if BS4 sees them:
soup_count = len(soup.select('span.job-tile__title'))
# If js_count > 0 but soup_count == 0: you called _get_soup() too early
```

---

## ⛔ Rule 4: Direct Detail Page Access Is Blocked

**Every attempt to open a direct job URL** (e.g., `/hcmUI/.../job/144292/?...`) **is
blocked** by Honeywell's Oracle Cloud instance. You get a 200 HTML response, but the
content is a "Connect" / "Are You Still With Us?" / "Work Summary" interstitial —
**not** the job detail page. No `h1.job-details__title`, no `div.job-details__description-content`,
no metadata items.

### What we tried:

| Approach | Result |
|---|---|
| Direct `goto(job_url)` with `domcontentloaded` | Captcha / interstitial page |
| `goto(job_url)` with `commit` + 10s wait | Same — blocked |
| Passing cookies/referrer headers | Same — blocked |
| Using the shared browser context | Same — blocked |

### Conclusion: Card-level descriptions are sufficient.

Honeywell's cards expose `p.job-list-item__description` with a short summary
(200-600 chars). This is the best data available. **Do NOT waste time trying
to crack detail pages — the value isn't there.**

### When detail pages DO work:

Some Oracle Cloud instances (like JPMorgan Chase) **do** allow direct detail
URL access. In those cases, the detail page pattern works:

```python
await detail_page.goto(job_url, wait_until="commit", timeout=30000)
await detail_page.wait_for_timeout(6000)
# Then extract from:
#   h1.job-details__title
#   li.job-meta__item (label/value pairs)
#   div.job-details__description-content
```

**Test early:** Before wiring up full detail enrichment, curl/wget a single
job URL or open it in a browser. If you see a captcha/interstitial, skip
detail enrichment entirely.

---

## Card Selectors That Actually Work

### Most reliable (in order):

```python
CARD_SELECTOR = (
    "li[data-qa='searchResultItem'], "
    "div.job-tile, "
    "div.search-results"
)
LINK_SELECTOR = "a.job-list-item__link[href*='/job/'], a[href*='/job/']"
TITLE_SELECTOR = "span.job-tile__title"
DESCRIPTION_SELECTOR = "p.job-list-item__description"  # or p.job-grid-item__description
```

### Location extraction:

The `<posting-locations>` web component is the most reliable source:

```python
posting_locations = card.select_one("posting-locations")
if posting_locations:
    primary_span = posting_locations.select_one(
        "span[data-bind*='primaryLocation']"
    )
    location = primary_span.get_text() if primary_span else ""

    # Also check aria-label for secondary locations:
    # aria-label="Locations,Bengaluru, Karnataka, India,Pune, Maharashtra, India"
    for el in posting_locations.select("[aria-label]"):
        aria = el.get("aria-label", "")
        if aria.lower().startswith("locations,"):
            # Parse comma-separated cities
```

### Job ID extraction (fallback chain):

```python
# 1. URL path: /hcmUI/.../job/144292/
match = re.search(r"/job/(\d+)", url)

# 2. aria-labelledby attribute on the link
labelled_link = card.select_one("[aria-labelledby]")
job_id = labelled_link.get("aria-labelledby", "")  # e.g., "144292"

# 3. search-result-item-header id attribute
header = card.select_one("search-result-item-header[id]")
job_id = header.get("id", "")  # e.g., "144292"
```

### Posted date:

Not always visible in card-level HTML. The text-based regex approach:

```python
text = card.get_text(" ")
match = re.search(
    r"Posted\s+on\s+(\d{1,2}/\d{1,2}/\d{4})",
    text, flags=re.IGNORECASE
)
```

---

## Anti-Filtering: URL Structure

Oracle Cloud applies filters via query parameters, not AJAX. This is a **good thing**
— you can pre-construct the URL with all filters baked in:

```
?lastSelectedFacet=POSTING_DATES
&selectedCategoriesFacet=300000017425610
&selectedPostingDatesFacet=7
&selectedWorkLocationsFacet=300000017420441%3B300000017419733%3B300000017420327
&sortBy=POSTING_DATES_DESC
```

No Playwright filter interactions needed — the URL is self-contained. If the user
changes the filter URL, just update the config.

---

## Summary: The Oracle Cloud Playbook

```
1. WAIT STRATEGY:
   goto(wait_until="commit", timeout=30000)
   → wait_for_timeout(8000)
   → page.evaluate("document.querySelectorAll('span.job-tile__title').length")
   → if 0: fallback to _fallback_links()

2. CARD PARSING:
   soup = BS4(page.content())
   cards = soup.select("li[data-qa='searchResultItem']")
   → extract title from span.job-tile__title
   → extract location from posting-locations web component
   → extract job ID from URL regex /job/(\d+)
   → extract short description from p.job-list-item__description

3. DETAIL ENRICHMENT:
   Test a single job URL first:
     If blocked (captcha/interstitial): SKIP — use card descriptions
     If accessible: detail_page.goto(job_url, wait_until="commit", ...)
                    → h1.job-details__title
                    → div.job-details__description-content
                    → li.job-meta__item pairs

4. ANTI-HANG:
   NEVER use networkidle
   PREFER commit over domcontentloaded
   USE page.evaluate() to check readiness, NOT wait_for_selector
```

---

## Companies on Oracle Cloud (so far)

| Company | Difficulty | Detail pages work? | Notes |
|---------|------------|-------------------|-------|
| **JPMorgan Chase** | Hard | ✅ Yes | `a.job-grid-item__link`, detail enrichment works |
| **American Express** | Medium | ✅ Yes (detail-first) | Shadow DOM in cards; detail pages for all data |
| **Kotak Mahindra Bank** | Easy | ❌ Not needed | URL-as-filter, no interaction needed |
| **Honeywell** | Hard | ❌ Blocked (captcha) | Card-only; detail URLs redirect to interstitial |

---

## Quick Reference: What Will Make You Waste Time

| Thing you'll be tempted to try | Why it will fail |
|---|---|
| `networkidle` | Hangs forever — analytics/chat never close connections |
| `domcontentloaded` alone | Hangs ~70% of the time on Oracle; use `commit` instead |
| `wait_for_selector` after goto | Selector might exist in skeleton DOM before KO binds; returns stale/empty |
| Direct detail URL scraping | Many instances block with interstitial; test first, code later |
| Increasing timeout values | Doesn't fix the root cause; fix the wait strategy instead |
| `load` event | Even slower than `domcontentloaded`; same analytics problem |
