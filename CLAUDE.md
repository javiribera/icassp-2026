# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

Scrapes paper metadata from the ICASSP 2026 proceedings on IEEE Xplore
(`https://ieeexplore.ieee.org/xpl/conhome/11460365/proceeding`) and writes
the results to `output/papers.json` and `output/papers.csv`.

The scraper hits the internal REST API that the IEEE Xplore SPA uses
(`https://ieeexplore.ieee.org/rest/search`), paginates through all records,
and flattens each record for CSV export.

## Commands

```bash
# Install dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run scraper (output goes to ./output/)
python scraper.py
python scraper.py --output data/ --delay 2.0

# Docker
docker build -t icassp-scraper .
docker run --rm -v "$(pwd)/output:/app/output" icassp-scraper
```

## Key Design Decisions

- **No headless browser**: The internal REST API returns JSON directly; `requests` is sufficient.
- **Pagination**: `ROWS_PER_PAGE = 100` is the maximum IEEE Xplore accepts per page. Total pages are computed from `totalRecords` in the first response.
- **Polite crawl**: Default 1.5 s delay between page requests (`--delay` flag to override).
- **Two outputs**: `papers.json` keeps the raw API payload; `papers.csv` is a flat table for quick analysis.
- If the scraper returns 0 records, the most likely cause is a changed API endpoint — inspect XHR traffic on the proceedings page to find the updated URL/parameters.

## Development Branch

Push changes with:

```bash
git push -u origin <branch-name>
```
