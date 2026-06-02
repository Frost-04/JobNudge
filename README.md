# job-alert-bot

job-alert-bot monitors company career pages for early-career software roles, compares results with previously seen jobs, and alerts you only when new, relevant postings appear.

## Features
- Company-specific scrapers (Google and Amazon today)
- Playwright-based rendering for dynamic pages
- SQLite storage for deduplication
- YAML configuration for companies, keywords, and settings
- Optional notifications (console by default)
- Health check mode for quick verification

## Architecture
- Config files define companies, keywords, and runtime settings.
- Scrapers fetch raw jobs from company-specific career pages.
- Services filter, deduplicate, and store seen jobs in SQLite.
- Notifications are sent only for newly detected jobs.

## Supported Companies
- Google
- Amazon

Future companies can be added by creating new scraper files.

## Project Structure
```
job-alert-bot/
├── README.md
├── HOW_TO_RUN.md
├── SCRAPER_TUNING.md
├── requirements.txt
├── .env.example
├── .gitignore
├── config/
├── data/
├── src/
└── .github/
```

## Configuration Overview
- config/companies.yaml: companies and their career URLs
- config/keywords.yaml: include/exclude keywords and locations
- config/settings.yaml: timeouts, storage paths, logging, notifications

## Example Alert Output
```
Started job scraper
Google: raw jobs=8, filtered jobs=3, new jobs=1
Amazon: raw jobs=12, filtered jobs=4, new jobs=2
Exported latest jobs to data/latest_jobs.csv

New jobs found: 3

1. Google
Role: Software Engineer, Early Career
Location: Bengaluru, India
Link: https://...

2. Amazon
Role: Software Development Engineer I
Location: Hyderabad, India
Link: https://...
```

## How to Add a New Company-Specific Scraper
1. Create src/scrapers/companyname_scraper.py
2. Subclass BaseScraper and implement async scrape()
3. Return a list of Job objects
4. Register it in src/scrapers/scraper_factory.py
5. Add the company in config/companies.yaml
6. Document tuning notes in SCRAPER_TUNING.md
7. Run python -m src.health_check

## Troubleshooting
- If a scraper returns 0 jobs, check SCRAPER_TUNING.md for manual tuning steps.
- Increase timeouts in config/settings.yaml if pages are slow to load.
- Run with headless: false during debugging.
- Check data/logs/scraper.log for detailed logs.

## Disclaimer
This tool only checks public career pages. It does not bypass captchas, login walls, bot protections, or rate limits. Use reasonable frequencies and respect the target sites' terms of service.
