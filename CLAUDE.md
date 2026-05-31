# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

Two scripts:
- `scraper.py` — scrapes paper metadata from academic conference proceedings; both IEEE Xplore
  (icassp) and CVF Open Access (cvpr, iccv, wacv) are supported via `--conference` + `--year`.
  Writes `output/papers.json` and `output/papers.csv`.
- `estimate_relevance.py` — scores each paper 0–100 for relevance using the Anthropic API;
  reads `PROMPT_FOR_RELEVANCE.txt` (user-supplied). Writes `papers_with_relevance.json/.csv`.

## Commands

```bash
# Install
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt      # includes ruff + mypy

# Lint and type-check (must pass before committing)
ruff check scraper.py estimate_relevance.py
python -m mypy scraper.py estimate_relevance.py --ignore-missing-imports

# Scrape CVF (static HTML, no auth)
python scraper.py --conference cvpr  --year 2024
python scraper.py --conference cvpr  --year 2024 --limit 20   # smoke-test

# Scrape IEEE (JSON REST API; use --cookie if 403)
python scraper.py --conference icassp --year 2026
python scraper.py --conference icassp --year 2026 --cookie "JSESSIONID=..."

# Resume an interrupted run
python scraper.py --conference cvpr --year 2024   # same command, same --output

# Score relevance (requires ANTHROPIC_API_KEY and PROMPT_FOR_RELEVANCE.txt)
python estimate_relevance.py
python estimate_relevance.py --no-batch --limit 5   # test with 5 papers

# Docker
docker build -t conf-scraper .
docker run --rm -v "$(pwd)/output:/app/output" conf-scraper \
  --conference cvpr --year 2024
```

## Architecture

### scraper.py

Two backends behind shared scaffolding (checkpoint/resume, `enrich_details`, `save`):

**CVF backend** (`cvpr`, `iccv`, `wacv`):
- `cvf_list()` — GET `openaccess.thecvf.com/{CONF}{year}?day=all`; parse `dt.ptitle > a`.
  Fallback: discover `?day=...` links from root page.
- `cvf_fetch_one()` — GET per-paper page; parse `#papertitle`, `#authors`, `#abstract`, PDF link.

**IEEE backend** (`icassp`):
- `ieee_list()` — POST `/rest/search` pagination; maps records to unified paper dicts.
- `ieee_fetch_one()` — GET `/rest/document/{articleNumber}/`; updates abstract + affiliations.

**Shared driver**:
- `enrich_details()` — `ThreadPoolExecutor` over backend's `fetch_one(session, paper)`;
  checkpoints every `CHECKPOINT_INTERVAL=200`.
- `save(papers, output_dir)` — strips `_`-prefixed internal fields; writes `papers.json` +
  `papers.csv` (columns: `title, abstract, url`).
- `save_checkpoint`/`load_checkpoint` — `{"papers":[...], "done":[ids]}`.

**Unified paper dict** (written to papers.json):
```
title, venue, year, abstract, authors:[{name, affiliation}], doi, url, pdf_url
```
`affiliation` is always `""` for CVF. `doi`/`pdf_url` differ by backend.

### estimate_relevance.py

- **Batches API (default)**: submits all unscored papers in one batch; polls until complete;
  stores `batch_id` in checkpoint for resume if interrupted mid-poll.
- **`--no-batch`**: `ThreadPoolExecutor` over `client.messages.create()`; checkpoint every 200.
- Prompt: loads `PROMPT_FOR_RELEVANCE.txt` + paper `title`/`abstract`; asks for 0–100 integer.
- Resume key: paper `title` (unique within a run).
- `UsageSummary` accumulates token counts and logs a token/cost summary on exit (in a `finally`,
  so it prints even on crash). Batch-mode cost applies the Batches API 50% discount.

## Key Details

- `--conference` and `--year` are both required for `scraper.py`.
- `--cookie` applies to IEEE only; ignored for CVF.
- CVPR2026 returns 403 until papers are published (~June 2026). Test with `--year 2024`.
- `--limit N` truncates and disables checkpointing (for smoke-testing).
- `output/` is git-ignored; Docker mounts it as a volume.
- `--ignore-missing-imports` in mypy covers `bs4` and `anthropic` missing stubs.

## Git Workflow

Always push directly to `main`. Never create a pull request.

```bash
git push -u origin main
```
