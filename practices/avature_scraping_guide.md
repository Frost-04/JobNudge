# Avature — Scraping Guide

> **⚠️ MANDATORY READING** before creating or debugging any Avature scraper.
> Avature job boards have consistent class naming (`.article--result`,
> `.article__header__text__title`, `.list-item-location`) across all companies,
> but the detail page structure and metadata labels vary per instance.  Read this
> first, waste time second.

---

## Platform Fingerprint

If the search page URL contains `/careers/Home/` with numeric filter query params
(e.g. `?8171=%5B10590%5D&8171_format=5683`) and the cards are
`article.article--result`, you are on Avature.

The platform is **server-rendered HTML** — no SPA, no Knockout.js, no async card
rendering.  `domcontentloaded` is always sufficient for the listing page.  Detail
pages are also server-rendered and always accessible via direct URL.

---

## ⛔ Rule 1: Server‑Rendered — `domcontentloaded` Always Works

Unlike Oracle Cloud or Workday, Avature pages are plain server-rendered HTML.
No JS framework, no async rendering, no persistent connections.

```python
# Always sufficient:
await page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
await page.wait_for_selector('article.article--result', timeout=45000)
```

No `networkidle` needed.  No settle delay needed.  No `commit` needed.

---

## ⛔ Rule 2: Title Link Class Varies — Check Before Cloning

The title anchor element varies across Avature instances:

| Company | Title selector |
|---------|---------------|
| **Bloomberg** | `h3.article__header__text__title a.link` |
| **Siemens** | `h3.article__header__text__title a.link` |
| **Electronic Arts** | `h3.article__header__text__title a.link_result` |

**Always inspect the card HTML** to confirm which anchor class is used.

---

## ⛔ Rule 3: Job ID Comes From TWO Sources

Avature job IDs have a consistent fallback chain:

### Primary: URL path extraction

```
https://jobs.ea.com/en_US/careers/JobDetail/SRE-III/214248
                                                    ^^^^^^
```

```python
match = re.search(r"/(\d+)(?:\?|$)", url)
```

### Fallback: Card subtitle span

Different companies use different span class names for the job ID:

| Company | ID span | Raw text example |
|---------|---------|-----------------|
| Bloomberg | `span.list-item-jobId` (hidden, inferred from SaveJob link) | `?jobId=19839` |
| Siemens | `span.list-item-jobId` | `Job ID: 494008` |
| Electronic Arts | `span.list-item-id` | `Role ID 214248` |

```python
# EA example:
job_id_el = card.select_one("span.list-item-id")
if job_id_el:
    job_id = re.search(r"(\d+)", clean_text(job_id_el.get_text())).group(1)

# Siemens example:
job_id_el = card.select_one("span.list-item-jobId")
if job_id_el:
    # Text: "Job ID: 494008" → extract numeric part
```

### Extra fallback: SaveJob query param (Bloomberg)

```python
save_el = card.select_one("a.button--secondary[href*='SaveJob']")
if save_el:
    save_href = save_el.get("href", "")
    match = re.search(r"jobId=(\d+)", save_href)
```

---

## ⛔ Rule 4: Detail Page Description Structure Varies

Avature detail pages use `div.article__content__view__field` containers, but
the description location varies:

### Bloomberg / Siemens — Structured field with `.field--rich-text`:

```python
DETAIL_SELECTOR = "div.article__content__view__field.field--rich-text div.article__content__view__field__value"
```

These contain rich HTML — `<div>`, `<strong>`, `<ul>`, `<li>`, `<br>` — and need
the `_extract_div_contents()` recursive parser.

### EA — Multiple flat field values, no `.field--rich-text`:

```python
# All field values on the page:
all_values = soup.select("div.article__content__view__field__value")

# Skip short boilerplate (< 100 chars), keep rich content
for value_el in all_values:
    text = clean_text(value_el.get_text())
    if len(text) < 100:
        continue  # Skip company intros, empty fields
    rich_text = extract_rich_description(value_el)
```

---

## ⛔ Rule 5: Posted Date Lives in Detail Metadata (or Doesn't Exist)

Avature listing cards do NOT show posted dates.  You MUST go to the detail page.

### Metadata extraction from labeled field pairs:

```python
for field in soup.select("div.article__content__view__field"):
    label_el = field.select_one("div.article__content__view__field__label")
    value_el = field.select_one("div.article__content__view__field__value")
    if label_el:
        label = clean_text(label_el.get_text())
        value = clean_text(value_el.get_text()) if value_el else ""
```

| Company | Posted date label | Present? |
|---------|------------------|----------|
| Bloomberg | Not exposed | ❌ No posted date |
| Siemens | `"Posted since"` | ✅ Yes |
| Electronic Arts | Not exposed | ❌ No posted date |

### Always test first:

Open a detail page URL in the browser.  Check for `div.article__content__view__field__label`
elements.  If none contain "Posted" or "Date", the company simply doesn't expose it.

---

## ⛔ Rule 6: Description Boilerplate Filtering

Avature detail pages often include company intros, diversity statements, and
"About Us" blurbs that are NOT the actual job description.  Strategies:

### Strategy A: Length threshold (EA)

```python
if len(text) < 100:
    # Too short to be a job description — skip
    continue
```

### Strategy B: Field class targeting (Bloomberg/Siemens)

```python
# Only extract from the explicitly marked rich-text field
DETAIL_SELECTOR = "div.article__content__view__field.field--rich-text div.article__content__view__field__value"
```

---

## Card Selectors That Actually Work

### Universal Avature selectors:

```python
CARD_SELECTOR = "article.article--result"
LOCATION_SELECTOR = "span.list-item-location"

# Title — varies by company, always check:
TITLE_SELECTOR = "h3.article__header__text__title a.link"  # Bloomberg, Siemens
TITLE_SELECTOR = "h3.article__header__text__title a.link_result"  # EA

# Job ID — varies by company:
JOB_ID_CARD_SELECTOR = "span.list-item-jobId"  # Siemens
JOB_ID_CARD_SELECTOR = "span.list-item-id"     # EA
JOB_ID_SAVE_SELECTOR = "a.button--secondary[href*='SaveJob']"  # Bloomberg
```

### Wait chain:

```python
selectors = [
    "div.results--listed",
    "article.article--result",
]
for selector in selectors:
    try:
        await page.wait_for_selector(selector, timeout=45000)
        return
    except Exception:
        continue
```

### Detail page:

```python
await detail_page.goto(job_url, wait_until="domcontentloaded", timeout=60000)
await detail_page.wait_for_selector(
    "div.article__content",  # or "article.article--details"
    timeout=15000,
)
```

### Rich-text description extraction:

All Avature scrapers share the same `_extract_div_contents()` recursive parser
that handles:
- `<div>` → recurse into children
- `<strong>`, `<b>` → bold label text
- `<br>` → line break (`\n`)
- `<ul>`, `<ol>` → bullet list items prefixed with `- `
- `<a>` → link text + href

---

## Company Variations

| Company | Title link class | Job ID source | Posted date? | Description field class? |
|---------|-----------------|--------------|-------------|--------------------------|
| **Bloomberg** | `a.link` | URL + SaveJob fallback | ❌ No | `.field--rich-text` |
| **Siemens** | `a.link` | `span.list-item-jobId` + URL | ✅ "Posted since" | `.field--rich-text` |
| **Electronic Arts** | `a.link_result` | `span.list-item-id` + URL | ❌ No | Flat fields, no `.field--rich-text` |

---

## Summary: The Avature Playbook

```
1. IDENTIFY SELECTORS:
   Check card HTML for:
   - Title: a.link or a.link_result?
   - Job ID: span.list-item-id, span.list-item-jobId, or SaveJob link?
   - Location: Always span.list-item-location (universal)

2. CARD PARSING:
   goto("domcontentloaded", timeout=60000)
   → wait for article.article--result
   → extract title, URL, location, job ID
   → posted_date initially None (detail page may have it)

3. DETAIL ENRICHMENT:
   detail_page.goto(job_url, "domcontentloaded", timeout=60000)
   → Check for .field--rich-text (Bloomberg/Siemens) or flat fields (EA)
   → Extract description, skip short boilerplate
   → Extract posted date from metadata label pairs (if present)
   → title-based exclusion for Senior/Staff/Lead/Principal

4. KNOWN ISSUES:
   - Title anchor class varies — always inspect first
   - Job ID span name varies — check card HTML
   - Posted date may be absent entirely (Bloomberg, EA)
   - Boilerplate filtering needed — company intros leak into description
```
