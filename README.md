# JobNudge

JobNudge monitors company career pages for early-career software roles, compares results with previously seen jobs, optionally filters them through Gemini AI, and alerts you via console or Telegram only when new, relevant postings appear.

## Features
- **4 company-specific scrapers** — Google, Amazon, Microsoft, JPMorgan Chase
- **Playwright-based rendering** for JavaScript-heavy career pages
- **SQLite persistence** — tracks seen jobs across runs so you only get new ones
- **YAML configuration** — companies, keywords, AI prompts, and runtime settings
- **Gemini AI filtering** — uses Google Gemini to identify truly fresher-eligible roles
- **Telegram notifications** — formatted alerts with job details
- **Three entry points** — health check, full scrape, and AI+Telegram pipeline
- **Fault reporting** — alerts you when a scraper returns 0 jobs or errors out

## Architecture

```
Config (YAML)  →  Scrapers (Playwright + BS4)  →  Dedup (in-memory + SQLite)
                                                      ↓
                                               Export (CSV)
                                                      ↓
                                               AI Filter (Gemini)
                                                      ↓
                                               Notify (Console or Telegram)
```

- **Config** files define companies, keywords, AI prompt, and runtime settings.
- **Scrapers** fetch raw jobs from company career pages using Playwright + BeautifulSoup4.
- **Services** deduplicate, persist, export, AI-filter, and notify.
- **Pipelines** compose the services into runnable workflows.

## Supported Companies

| Company | Scraper Key | Career Page |
|---|---|---|
| Google | `google` | Google Careers search results |
| Amazon | `amazon` | Amazon Jobs search results |
| Microsoft | `microsoft` | Microsoft Careers (React-rendered) |
| JPMorgan Chase | `jpmorgan` | Oracle Cloud Candidate Experience |

Each scraper extracts job ID, title, location, link, posted date, and description from the search results page. Microsoft and JPMorgan scrapers also enrich results by opening each job's detail page.

Future companies can be added by creating new scraper files — see [Adding a Scraper](#how-to-add-a-new-company-specific-scraper).

## Project Structure

```
JobNudge/
├── README.md
├── HOW_TO_RUN.md
├── HOW_IT_WORKS.md
├── changes.md
├── requirements.txt
├── LICENSE
├── config/
│   ├── companies.yaml          ← Which companies to scrape
│   ├── keywords.yaml           ← Include/exclude/location keywords
│   ├── settings.yaml           ← Runtimes, timeouts, storage, AI config
│   └── ai_prompt.yaml          ← Gemini filtering prompt template
├── data/
│   ├── new_jobs.csv            ← Output: truly new jobs this run
│   ├── jobs_to_send.csv        ← Output: AI-filtered jobs ready for Telegram
│   ├── seen_jobs.db            ← SQLite: permanent seen-job tracker
│   └── logs/
│       └── scraper.log         ← Application logs
└── src/
    ├── main.py                 ← Full scrape pipeline
    ├── ai_pipeline.py          ← Scrape + AI filter pipeline
    ├── telegram_pipeline.py    ← Scrape + AI filter + Telegram pipeline
    ├── health_check.py         ← Diagnostic runner
    ├── models/
    │   └── job.py              ← Job dataclass
    ├── scrapers/
    │   ├── base_scraper.py     ← Abstract base with Playwright plumbing
    │   ├── scraper_factory.py  ← Maps config name → scraper class
    │   ├── amazon_scraper.py
    │   ├── google_scraper.py
    │   ├── microsoft_scraper.py
    │   └── jpmorganchase_scraper.py
    ├── services/
    │   ├── dedup_service.py    ← In-memory dedup
    │   ├── storage_service.py  ← SQLite persistence
    │   ├── export_service.py   ← CSV export
    │   ├── ai_filter_service.py← Gemini AI filtering
    │   ├── notification_service.py ← Console + Telegram alerts
    │   └── filter_service.py   ← Keyword filtering (optional)
    └── utils/
        ├── config_loader.py    ← YAML file reader
        ├── logger.py           ← Logging setup
        ├── text_utils.py       ← Text normalization + keyword matching
        └── url_utils.py        ← URL normalization + job ID extraction
```

## Configuration Overview

| File | Purpose |
|---|---|
| `config/companies.yaml` | Companies to scrape, their URLs, scraper type, enabled flag |
| `config/keywords.yaml` | Include/exclude keywords and preferred locations |
| `config/settings.yaml` | Timeouts, storage paths, logging, notifications, AI model |
| `config/ai_prompt.yaml` | Prompt template for Gemini AI filtering (uses `{jobs_json}` placeholder) |

## Entry Points

### Health Check (diagnostic)
```bash
python -m src.health_check
```
Tests config loading, database initialization, Playwright launch, and runs each scraper — prints sample results and writes `health_check_output.txt`. Does NOT persist jobs or send notifications.

### Full Scrape Pipeline
```bash
python -m src.main
```
Scrapes all enabled companies, deduplicates, exports new jobs to `data/new_jobs.csv`, and sends console notifications.

### AI Pipeline
```bash
python -m src.ai_pipeline
```
Runs the full scrape pipeline first, then passes `new_jobs.csv` through Gemini AI to identify fresher-eligible roles. Output: `data/jobs_to_send.csv`.

Requires `GEMINI_API_KEY` in `.env`.

### Telegram Pipeline (full end-to-end)
```bash
python -m src.telegram_pipeline
```
Runs scrape → AI filter → sends formatted Telegram messages for each AI-selected job. Also sends fault alerts for broken scrapers.

Requires `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID` in `.env`.

## Example Alert Output

**Console (main.py):**
```
Started job scraper
JPMorgan Chase: raw jobs=25
Exported 5 new jobs to data/new_jobs.csv

New jobs found: 5

1. JPMorgan Chase
Role: Software Engineer I
Location: Bengaluru, KA, IND
Link: https://jpmc.fa.oraclecloud.com/...
```

**Telegram (telegram_pipeline.py):**
```
🚀 AI-Selected Job 🤖

Company: JPMorgan Chase
Role: Software Engineer I
Location: Bengaluru, KA, IND
Link: https://jpmc.fa.oraclecloud.com/...
```

## How to Add a New Company-Specific Scraper

1. Create `src/scrapers/companyname_scraper.py`
2. Subclass `BaseScraper` and implement `async scrape() → list[Job]`
3. Implement `_parse_card()`, `_extract_job_id()`, and other extraction methods
4. Register it in `src/scrapers/scraper_factory.py`
5. Add the company entry in `config/companies.yaml`
6. Run `python -m src.health_check` to verify

## Troubleshooting

- **Scraper returns 0 jobs** — the page structure may have changed. Inspect the page DOM and update selectors in the scraper.
- **Slow page loads** — increase `page_load_timeout_seconds` in `config/settings.yaml`.
- **Debugging selectors** — set `headless: false` in settings to watch the browser.
- **AI pipeline errors** — ensure `GEMINI_API_KEY` is set in `.env` and `google-generativeai` is installed.
- **Telegram errors** — verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are correct.
- **Logs** — check `data/logs/scraper.log` for detailed error traces.

## Disclaimer

This tool only checks public career pages. It does not bypass captchas, login walls, bot protections, or rate limits. Use reasonable frequencies and respect the target sites' terms of service.
