# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

Scrapes paper metadata from the ICASSP 2026 proceedings on IEEE Xplore
(`https://ieeexplore.ieee.org/xpl/conhome/11460365/proceeding`, 4 589 papers)
and writes results to `output/papers.json`.

## Commands

```bash
# Install
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt      # includes ruff + mypy

# Lint and type-check (must pass before committing)
ruff check scraper.py
python -m mypy scraper.py --ignore-missing-imports

# Run (~20–30 min)
python scraper.py
python scraper.py --cookie "JSESSIONID=..."   # if 403, pass browser cookie
python scraper.py --delay 2.0 --output data/
python scraper.py --limit 20                  # first 20 papers only (testing)

# Resume an interrupted run (checkpoint auto-saved to output/checkpoint.json)
python scraper.py            # just re-run with same --output

# Docker — scrape papers
docker build -t icassp-scraper .
docker run --rm -v "$(pwd)/output:/app/output" icassp-scraper

# Docker — score relevance (requires ANTHROPIC_API_KEY)
docker run --rm \
  -v "$(pwd)/output:/app/output" \
  -v "$(pwd)/PROMPT_FOR_RELEVANCE.txt:/app/PROMPT_FOR_RELEVANCE.txt:ro" \
  -e ANTHROPIC_API_KEY=sk-... \
  --entrypoint python icassp-scraper \
  estimate_relevance.py
```

## Architecture

`scraper.py` calls `https://ieeexplore.ieee.org/rest/search` — the internal
XHR endpoint used by the IEEE Xplore SPA. It reads `totalRecords` from the
first response, computes page count using `ROWS_PER_PAGE = 100`, then
paginates with a configurable delay.

Produces `output/papers.json` via `structure()` + `save()`.
`structure()` converts a raw API record into
`{title, abstract, authors: [{name, affiliation}], doi, url}`.

**Detail enrichment**: `/rest/search` returns truncated abstracts and no author affiliations.
After the search scrape, the script fetches `/rest/document/{articleNumber}/`
for every paper to get the full abstract and per-author affiliation strings.
`fetch_details_api()` uses `ThreadPoolExecutor` with `--workers` (default 8);
retries on 429/503 with exponential backoff.
Skip with `--no-details` (alias: `--no-affiliations`).

**Checkpoint/resume**: `save_checkpoint()` / `load_checkpoint()` write `<output>/checkpoint.json` after the search phase and every `CHECKPOINT_INTERVAL=200` detail completions. On re-run, completed details are skipped. Checkpoint is deleted on success. Disabled when `--limit` is used.

## Key Details

- `--cookie` accepts the full `Cookie:` header string copied from browser DevTools.
- `--limit N` truncates to the first N papers after the search phase; disables checkpointing. Use for smoke-testing.
- `output/` is git-ignored; the Docker image mounts it as a volume.
- Playwright is required only for `schedule.py` (not in `requirements.txt`); install it separately.

## Git Workflow

Always push directly to `main`. Never create a pull request.

```bash
git push -u origin main
```
