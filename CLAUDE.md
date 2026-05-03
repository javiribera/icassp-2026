# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

Scrapes paper metadata from the ICASSP 2026 proceedings on IEEE Xplore
(`https://ieeexplore.ieee.org/xpl/conhome/11460365/proceeding`, 4 589 papers)
and writes results to `output/papers.json` and `output/papers.csv`.

## Commands

```bash
# Install
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt      # includes ruff + mypy

# Lint and type-check (must pass before committing)
ruff check scraper.py
python -m mypy scraper.py --ignore-missing-imports

# Run — REST API mode (fast, ~1 min)
python scraper.py
python scraper.py --cookie "JSESSIONID=..."   # if 403, pass browser cookie
python scraper.py --delay 2.0 --output data/

# Run — browser mode (always works, ~10–20 min)
pip install playwright && playwright install chromium
python scraper.py --browser

# Docker — REST API mode (~200 MB image)
docker build --target api -t icassp-scraper:api .
docker run --rm -v "$(pwd)/output:/app/output" icassp-scraper:api

# Docker — browser mode (~800 MB image, Chromium included)
docker build --target browser -t icassp-scraper:browser .
docker run --rm -v "$(pwd)/output:/app/output" icassp-scraper:browser
```

## Google Cloud Run

`cloudbuild.yaml` runs six steps in order: lint → typecheck (parallel with lint) → docker build (`browser` target) → push (sha + latest tags) → `gcloud run jobs deploy` → `gcloud run jobs execute --wait`.

The Cloud Run Job mounts a GCS bucket at `/app/output` via `--add-volume type=cloud-storage` (requires `--execution-environment=gen2`). `papers.json` is written there and persists after the container exits.

One-time setup: create the Artifact Registry repo and GCS bucket. Then:
```bash
gcloud builds submit --config cloudbuild.yaml --substitutions _GCS_BUCKET=my-bucket
```

## Architecture

`scraper.py` is a single-file script with two strategies:

**REST API mode** (default): calls `https://ieeexplore.ieee.org/rest/search?publication-number=11460365`
— the internal XHR endpoint used by the IEEE Xplore SPA. It reads `totalRecords`
from the first response, computes page count using `ROWS_PER_PAGE = 100`, then
paginates with a configurable delay. No headless browser needed.

**Browser mode** (`--browser`): loads the proceedings page once in headless
Chromium to establish a real browser session (cookies, TLS fingerprint), then
calls `page.evaluate(fetch(...))` from within the browser's JavaScript context
for every paginated API request. Because `fetch()` runs inside the browser,
IEEE Xplore sees it as a same-origin first-party request — IP blocking does not
apply. All calls reuse the single open page; no navigation between pages.
`_JS_FETCH` is the JavaScript template passed to `page.evaluate()`.
Use this when the REST API returns 403.

Both modes produce `output/papers.json` via `structure()` + `save()`.
`structure()` converts a raw API record into
`{title, abstract, authors: [{name, affiliation}], doi, url}`.

**Affiliation enrichment**: `/rest/search` does not include author affiliations.
After the search scrape, the script fetches `/rest/document/{articleNumber}/`
for every paper to get per-author affiliation strings.
- REST mode: `fetch_affiliations_api()` — `ThreadPoolExecutor` with `--workers` (default 8)
- Browser mode: `_browser_fetch_affiliations()` — batched `Promise.all()` in JS context, `AFF_BATCH_SIZE=50` papers per batch
- Skip with `--no-affiliations`

## Key Details

- `--cookie` accepts the full `Cookie:` header string copied from browser DevTools;
  only used in REST API mode.
- The `flatten()` function normalises the nested API record into a flat dict for CSV.
- `output/` is git-ignored; the Docker image mounts it as a volume.
- Playwright is an optional dependency (not in `requirements.txt`); install it only
  for `--browser` mode.

## Git Workflow

Always push directly to `main`. Never create a pull request.

```bash
git push -u origin main
```
