# Greenhouse — Scraping Guide

> **⚠️ MANDATORY READING** before creating or debugging any Greenhouse scraper.
> Greenhouse powers career pages for hundreds of companies. The platform has
> **two distinct layouts** (standard vs embed cards) with **three deployment
> patterns** (see Rule 7). Read this first, waste time second.

---

## Platform Fingerprint

You are on Greenhouse if the URL contains `job-boards.greenhouse.io` or if
the page loads the script `boards.greenhouse.io/embed/job_board/js?for=…`.

Greenhouse jobs are rendered in **two different layouts**:

| Layout | URL pattern | Card selector | Detail selector |
|--------|------------|---------------|-----------------|
| **Standard** | `job-boards.greenhouse.io/company` | `tr.job-post` | `div.job__description.body` |
| **Embed → WordPress** | `…/embed/job_board?for=company` | `tr.job-post` | Iframe `grnhse_iframe` → `job_app` |
| **Embed → Standard** | `…/embed/job_board?for=company` | `tr.job-post` | `div.job__description.body` |

Both are **server-rendered HTML** — no SPA, no async card rendering.
`domcontentloaded` is always sufficient for the listing. Detail pages
may need `networkidle` for the embed layout (spa shell + iframe).

---

## ⛔ Rule 1: Two Layouts — Different CSS Classes

The **card structure is identical** (`tr.job-post` table rows), but the
**CSS class names differ** between the two layouts:

### Standard layout (`/company`):
```html
<tr class="job-post">
  <td class="cell">
    <a href="/company/jobs/4612849005">
      <p class="body--medium">Software Engineer</p>
      <p class="body--metadata">Bangalore, India</p>
    </a>
  </td>
</tr>
```

### Embed layout (`/embed/job_board`):
```html
<tr class="job-post">
  <td class="cell">
    <a href="/open-positions/?gh_jid=7971677">
      <p class="body body--medium">Hardware Engineer III</p>
      <p class="body__secondary body--metadata">Gurgaon</p>
    </a>
  </td>
</tr>
```

### ✅ Always inspect the HTML before writing selectors:

| Element | Standard | Embed |
|---------|----------|-------|
| Title | `p.body--medium` | `p.body.body--medium` |
| Location | `p.body--metadata` | `p.body__secondary.body--metadata` |
| Link | `td.cell > a` | `td.cell > a[href*="?gh_jid="]` |
| Detail | `div.job__description.body` | iframe `grnhse_iframe` → body |

---

## ⛔ Rule 2: `domcontentloaded` Always Works for Listing

Unlike Oracle Cloud or Workday, Greenhouse pages are plain server-rendered
HTML tables. No JS framework needed for card rendering (on the standard
layout). No persistent connections that hang `networkidle`.

```python
# Standard layout — always sufficient:
await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)

# Embed layout — needs settle time for the react-select filters:
await page.goto(url, wait_until="domcontentloaded", timeout=60000)
await page.wait_for_timeout(5000)  # Let the SPA shell render
```

**For embed with filters:** See Rule 6.

---

## ⛔ Rule 3: Job IDs Come From URL Path (Standard) or Query Param (Embed)

### Standard layout:
```
URL:  https://job-boards.greenhouse.io/gleanwork/jobs/4612849005
                                              ^^^^^^^^  ^^^^^^^^^^
                                              company   numeric job ID
```

```python
job_id = extract_job_id(url)  # Uses the built-in url_utils helper
# Or directly:
match = re.search(r'/(\d+)(?:/|$|\?)', url)
```

### Embed layout:
```
Type 2 (WordPress wrapper):
URL:  https://www.tower-research.com/open-positions/?gh_jid=7971677
                                                       ^^^^^^^
                                                       query param

Type 3 (standard detail — Capco):
URL:  https://job-boards.greenhouse.io/capco/jobs/7988280
                                                  ^^^^^^^
                                                  URL path
```

```python
# Type 2 (query param):
match = re.search(r'[?&]gh_jid=(\d+)', url)

# Type 3 (URL path — identical to standard layout):
match = re.search(r'/jobs/(\d+)', url)

# Safe fallback (handles both):
match = re.search(r'/jobs/(\d+)', url) or re.search(r'[?&]gh_jid=(\d+)', url)
```

### Fallback (both layouts):
The `a[href]` on each card row always leads to the job detail. Parse the
URL from there.

---

## ⛔ Rule 4: Detail Pages — Standard vs Embed (WordPress) vs Embed (Standard)

### Standard layout detail (& Embed → Standard / Type 3):
The detail page is a **separate URL** on `job-boards.greenhouse.io`.
The full job description is in `div.job__description.body`.

This applies to **both** the pure standard layout AND the Type 3
hybrid (embed listing → standard detail, e.g. Capco).

```python
DETAIL_SELECTOR = 'div.job__description.body'

# Detail page workflow:
await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)
soup = await self._get_soup(detail_page)
desc = soup.select_one('div.job__description.body')
```

Detail pages on standard Greenhouse are **always accessible** — no
captcha, no interstitial, no blocking.

### Embed layout detail:
The detail is shown **inside the same WordPress/SPA page** via a
Greenhouse `job_app` iframe. Two iframes are involved:
1. `grnhse_iframe` on the outer page
2. `job_app` endpoint inside that iframe, containing the full description

```python
# Detail page workflow (embed):
await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)
await detail_page.wait_for_timeout(8000)  # Let iframes load

# Find the greenhouse job_app iframe by URL pattern:
frame = None
for f in detail_page.frames:
    if 'greenhouse' in f.url.lower() and 'job_app' in f.url:
        frame = f
        break

# Extract HTML from the iframe:
detail_html = await frame.content()
soup = BeautifulSoup(detail_html, "html.parser")
```

**Why not use `page.frame(name='grnhse_iframe')`:** With the real Chrome
channel (`channel="chrome"`), the iframe often has an **empty name**
attribute. Always fall back to URL-pattern matching:

```python
frame = detail_page.frame(name='grnhse_iframe')
if not frame:
    for f in detail_page.frames:
        if 'greenhouse' in f.url.lower() and 'job_app' in f.url:
            frame = f
            break
```

---

## ⛔ Rule 5: "New" Badge in Titles (Standard Layout)

Some standard Greenhouse listings add a `<span class="tag-container">`
inside the title `<p>` for "New" jobs:

```html
<p class="body--medium">
  Senior Backend Engineer
  <span class="tag-container">
    <span class="tag tag-new">NEW</span>
  </span>
</p>
```

This corrupts the BS4 `get_text()` output. The `_clean_text()` method
handles this, but **always test** the title extraction on a live page.

```python
# _clean_text handles whitespace and unicode, but NOT the badge removal.
# Add explicit badge removal if needed:
for badge in title_el.select('.tag-container, .tag'):
    badge.decompose()
title = self._clean_text(title_el.get_text())
```

---

## ⛔ Rule 6: Embed Filters — React-Select Interaction

Greenhouse embed pages use `react-select` dropdowns for Department and
Office filters. These are **multi-select** components that **close after
each selection**.

### The multi-select dance:

```python
FILTER_DEPARTMENTS = [
    "Application Reliability Engineering",
    "Core AI and Machine Learning",
    "Core Engineering",
]

async def _apply_filters(self, page):
    for dept in self.FILTER_DEPARTMENTS:
        await self._select_react_option(page, "#department-filter", dept)
        await page.wait_for_timeout(800)  # Let table re-render

    await self._select_react_option(page, "#office-filter", "Gurgaon")
    await page.wait_for_timeout(3000)

async def _select_react_option(self, page, input_id, label):
    # 1. Click the input to open dropdown
    await page.click(input_id, timeout=10000)
    await page.wait_for_timeout(1000)

    # 2. Wait for the listbox portal to appear
    await page.wait_for_selector('[role="listbox"]', timeout=5000)

    # 3. Click the option by text
    option = page.locator(f'[role="option"]:has-text("{label}")').first
    await option.click(timeout=5000)
    await page.wait_for_timeout(400)

    # Dropdown auto-closes after selection
```

### Critical: reopen for each selection

The dropdown **closes immediately** after each click. You MUST call
`page.click(input_id)` again for the next department. Do NOT try to
click multiple options from a single open dropdown — only the first
click will register.

### Verify filter count:

Always verify the filter reduced the job count from "all" to the
expected subset:

```
Before filters: 50 cards
After filters:  5 cards   ← correct
```

---

## ⛔ Rule 7: Direct Embed URL vs WordPress Wrapper

If a company embeds Greenhouse inside their own page (WordPress,
custom SPA), you have **three options**:

| Approach | Listing URL | Detail URL | When to use |
|----------|------------|------------|-------------|
| **Standard-only** | `job-boards.greenhouse.io/company` | `…/company/jobs/{id}` | No filters needed, or client-side filtering is sufficient |
| **Embed → WordPress detail** | `…/embed/job_board?for=company` | `company.com/page/?gh_jid=NNN` | Company has custom wrapper; embed cards use `?gh_jid=` links |
| **Embed → Standard detail** | `…/embed/job_board?for=company` | `…/company/jobs/{id}` | No custom wrapper; embed cards link directly to standard detail URLs |

### ✅ Type 1: Standard-only (simplest)

Use when the standard board has enough location granularity or when
client-side filtering is acceptable:

```python
LISTING_URL = "https://job-boards.greenhouse.io/company"
DETAIL_BASE = "https://job-boards.greenhouse.io"

# Card links are relative: /company/jobs/4612849005
```

### ✅ Type 2: Embed listing + WordPress detail (Tower Research pattern)

```python
EMBED_LISTING_URL = "https://job-boards.greenhouse.io/embed/job_board?for=company"
DETAIL_BASE = "https://www.company.com"

# Card links are relative: /open-positions/?gh_jid=NNN
# Convert to absolute WordPress URLs for detail enrichment:
def _make_detail_url(self, href):
    if href.startswith("/"):
        return f"{self.DETAIL_BASE}{href}"
    return href
```

### ✅ Type 3: Embed listing + Standard detail (Capco pattern)

**This is a hybrid.** The embed board is used for its react-select
filters (Department + Office), but the card links point directly to
**standard Greenhouse detail pages** — no WordPress wrapper, no iframe:

```python
EMBED_LISTING_URL = "https://job-boards.greenhouse.io/embed/job_board?for=capco"
DETAIL_BASE = "https://job-boards.greenhouse.io"

# Card links on embed page use standard URL format:
#   /capco/jobs/7988280
# NOT: /page/?gh_jid=NNN
LINK_SELECTOR = 'td.cell > a[href*="/jobs/"]'
```

**How to detect Type 2 vs Type 3:** Inspect ONE card link on the embed
listing page:
- `?gh_jid=NNN` in the href → Type 2 (WordPress wrapper detail)
- `/company/jobs/NNN` in the href → Type 3 (standard detail)
- **Never assume** — always check the actual card HTML.

---

## ⛔ Rule 8: Description Extraction — Standard vs Embed

### Standard layout description:
The `div.job__description.body` contains the **entire** job description
as rich HTML. Remove scripts/styles, then extract text.

```python
def _extract_description(self, soup):
    container = soup.select_one('div.job__description.body')
    if not container:
        return ""

    for unwanted in container.select('script, style, noscript'):
        unwanted.decompose()

    return self._clean_multiline_text(container.get_text(separator="\n"))
```

### Embed layout description:
Inside the `job_app` iframe, the content is a plain `<body>` with
structured sections:

```html
<h3><strong>Responsibilities</strong></h3>
<ul><li>…</li></ul>
<h3><strong>Qualifications</strong></h3>
<ul><li>…</li></ul>
<h3><strong>Benefits</strong></h3>
<p>…</p>
```

Anchor on the "Responsibilities" `h3`, walk up to the parent `div`,
and extract everything:

```python
def _extract_description(self, soup):
    for unwanted in soup.select('script, style, noscript'):
        unwanted.decompose()

    for h3 in soup.find_all('h3'):
        if 'responsibilities' in h3.get_text(strip=True).lower():
            parent = h3.parent
            for _ in range(4):  # Walk up to enclosing div
                if parent and parent.name == 'div':
                    text = parent.get_text(separator="\n")
                    if len(text) > 300:
                        return self._clean_multiline_text(text)
                parent = parent.parent if parent else None
            break

    # Fallback: entire body
    body = soup.find('body')
    if body:
        return self._clean_multiline_text(body.get_text(separator="\n"))
    return ""
```

---

## Card Selectors That Actually Work

### Standard layout selectors:
```python
CARD_SELECTOR = 'tr.job-post'
TITLE_SELECTOR = 'p.body--medium'
LOCATION_SELECTOR = 'p.body--metadata'
LINK_SELECTOR = 'td.cell > a'
DETAIL_SELECTOR = 'div.job__description.body'
```

### Embed layout selectors:
```python
CARD_SELECTOR = 'tr.job-post'
TITLE_SELECTOR = 'p.body.body--medium'
LOCATION_SELECTOR = 'p.body__secondary.body--metadata'

# ⚠️ LINK SELECTOR DEPENDS ON THE EMBED TYPE:
# Type 2 (WordPress wrapper — Tower Research):
LINK_SELECTOR = 'td.cell > a[href*="?gh_jid="]'
# Type 3 (standard detail — Capco):
LINK_SELECTOR = 'td.cell > a[href*="/jobs/"]'

# Safe fallback (works for both):
LINK_SELECTOR = 'td.cell > a[href]'

# Detail: if Type 2, use iframe extraction (see Rule 4);
#         if Type 3, use div.job__description.body directly (see Rule 8).
```

### Fallback chain:
```python
JOB_CARD_SELECTORS = [
    'tr.job-post',
    'div.job-posts',
    'div.job-posts--table',
]
```

### Link fallback (when card selectors fail):
```python
# Standard:
page.locator('a[href*="/jobs/"]')

# Embed:
page.locator('a[href*="?gh_jid="]')
```

---

## Company Variations

| Company | Layout | URL Pattern | Notes |
|---------|--------|------------|-------|
| **Quince** | Standard | `job-boards.greenhouse.io/quince/jobs/` | Original template |
| **InMobi** | Standard | `job-boards.greenhouse.io/inmobi/jobs/` | Identical to Quince |
| **Glean** | Standard | `job-boards.greenhouse.io/gleanwork/jobs/` | Identical; check title for "New" badge |
| **Capco** | Embed → Standard (Type 3) | `embed/job_board?for=capco` → `…/capco/jobs/NNN` | React-select filters for Dept + Office; standard detail (no iframe) |
| **Tower Research** | Embed → WordPress (Type 2) | `tower-research.com/open-positions/` | Direct embed for listing; WordPress for detail; react-select multi-filter |
| **Arcesium** | Custom listing + Standard detail | `arcesium.com/careers` → `greenhouse.io/arcesiumllc/jobs/NNN` | Extract job URLs from custom listing; detail via standard Greenhouse |
| **EverPure** | Custom listing + Standard detail | `everpuredata.com/careers` → `greenhouse.io/purestorage/jobs/NNN` | Same as Arcesium |
| **Cerebras** | Custom listing + Standard detail | `cerebras.ai/open-positions` → `greenhouse.io/cerebrassystems/jobs/NNN` | Title extracted from `div.job__title` after removing `div.job__location` |

---

## Summary: The Greenhouse Playbook

```
1. IDENTIFY THE LAYOUT:
   → URL contains /embed/? → Embed layout
   → URL is job-boards.greenhouse.io/company → Standard layout
   → URL is company.com but links to greenhouse.io → Custom listing + Standard detail

2. STANDARD LAYOUT WORKFLOW:
   goto(wait_until="domcontentloaded")
   → wait_for_selector('tr.job-post')
   → cards = soup.select('tr.job-post')
   → title from p.body--medium; location from p.body--metadata
   → job_id from URL /jobs/(\d+)
   → detail from div.job__description.body

3. EMBED LAYOUT WORKFLOW:
   goto(embed_url, wait_until="domcontentloaded")
   → apply react-select filters (reopen for each selection)
   → cards = soup.select('tr.job-post')
   → title from p.body.body--medium; location from p.body__secondary.body--metadata
   → INSPECT ONE CARD LINK TO DETERMINE TYPE:
       → ?gh_jid= in href → Type 2 (WordPress): job_id from ?gh_jid=(\d+)
                            detail via WordPress → greenhouse job_app iframe → body
       → /jobs/NNN   in href → Type 3 (Standard): job_id from /jobs/(\d+)
                                detail via div.job__description.body directly

4. CUSTOM LISTING + STANDARD DETAIL:
   → parse company's custom page for greenhouse.io links
   → job_id from /jobs/(\d+)
   → detail from div.job__description.body on greenhouse.io

5. KNOWN ISSUES:
   - "New" badge corrupts title text (standard layout)
   - Embed iframes have empty names with real Chrome channel
   - React-select multi-select closes after each click
   - Description extraction differs completely between layouts
```
