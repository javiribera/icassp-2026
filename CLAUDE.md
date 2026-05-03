# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

Scrapes paper metadata from the ICASSP 2026 proceedings on IEEE Xplore
(`https://ieeexplore.ieee.org/xpl/conhome/11460365/proceeding`, 4 589 papers)
and writes results to `output/papers.json` and `output/papers.csv`.

## Commands

```bash
# Install (REST API mode only)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run — REST API mode (fast, ~1 min)
python scraper.py
python scraper.py --cookie "JSESSIONID=..."   # if 403, pass browser cookie
python scraper.py --delay 2.0 --output data/

# Run — browser mode (always works, ~10–20 min)
pip install playwright && playwright install chromium
python scraper.py --browser

# Docker (REST API mode)
docker build -t icassp-scraper .
docker run --rm -v "$(pwd)/output:/app/output" icassp-scraper
```

## Architecture

`scraper.py` is a single-file script with two strategies:

**REST API mode** (default): calls `https://ieeexplore.ieee.org/rest/search?publication-number=11460365`
— the internal XHR endpoint used by the IEEE Xplore SPA. It reads `totalRecords`
from the first response, computes page count using `ROWS_PER_PAGE = 100`, then
paginates with a configurable delay. No headless browser needed.

**Browser mode** (`--browser`): drives headless Chromium via Playwright and
intercepts the same `/rest/search` XHR responses via `page.on("response", ...)`.
Use this when the REST API returns 403 from data-centre IPs or without a valid
session cookie.

Both modes produce the same two output files via `flatten()` + `save()`.

## Key Details

- `--cookie` accepts the full `Cookie:` header string copied from browser DevTools;
  only used in REST API mode.
- The `flatten()` function normalises the nested API record into a flat dict for CSV.
- `output/` is git-ignored; the Docker image mounts it as a volume.
- Playwright is an optional dependency (not in `requirements.txt`); install it only
  for `--browser` mode.

## Development Branch

Push changes with:

```bash
git push -u origin <branch-name>
```
