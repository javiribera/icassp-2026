# ICASSP 2026 Proceedings Scraper

Fetches metadata (title, authors, DOI, abstract, keywords) for all papers in the
[ICASSP 2026 proceedings](https://ieeexplore.ieee.org/xpl/conhome/11460365/proceeding)
on IEEE Xplore (4 589 papers).

## Output

`output/papers.json` — a JSON array, one object per paper:

```json
[
  {
    "title": "...",
    "abstract": "...",
    "authors": [
      {"name": "Alice Smith", "affiliation": "MIT, Cambridge, MA, USA"},
      {"name": "Bob Jones",   "affiliation": "Stanford University, CA, USA"}
    ],
    "doi": "10.1109/ICASSP...",
    "url": "https://ieeexplore.ieee.org/document/..."
  }
]
```

Author affiliations require an extra `/rest/document/{id}/` call per paper
(~4 589 requests). This runs concurrently after the initial scrape and is on
by default. Pass `--no-affiliations` to skip it.

## Two modes

### Mode 1 — REST API (fast, ~1 min)

Calls the internal XHR endpoint used by the IEEE Xplore SPA.

```bash
python scraper.py
```

If you get HTTP 403, pass a session cookie copied from your browser:

1. Open the [proceedings page](https://ieeexplore.ieee.org/xpl/conhome/11460365/proceeding) in Chrome/Firefox.
2. Open DevTools → Network → filter by `rest/search`.
3. Click any request → Headers → copy the full `Cookie:` value.
4. Pass it:

```bash
python scraper.py --cookie "JSESSIONID=abc123; TS01...=..."
```

### Mode 2 — Browser / Playwright (always works, ~10–20 min)

Drives a real headless Chromium, intercepting the same XHR responses.

```bash
pip install playwright
playwright install chromium
python scraper.py --browser
```

## Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # runtime only
pip install -r requirements-dev.txt      # adds ruff + mypy for local dev
# For browser mode only:
# pip install playwright && playwright install chromium
```

## Lint and type-check

```bash
ruff check scraper.py
python -m mypy scraper.py --ignore-missing-imports
```

## Google Cloud Run

Output (`papers.json`) is written to a GCS bucket mounted at `/app/output`.

**One-time setup:**
```bash
gcloud artifacts repositories create icassp \
  --repository-format=docker --location=us-central1
gcloud storage buckets create gs://MY_BUCKET --location=us-central1
```

**Deploy and run:**
```bash
gcloud builds submit --config cloudbuild.yaml \
  --substitutions _GCS_BUCKET=MY_BUCKET
```

Cloud Build runs lint → typecheck → docker build → push → deploy Cloud Run Job → execute. `papers.json` appears in `gs://MY_BUCKET/` when done.

Available substitutions (pass via `--substitutions KEY=VALUE`):

| Key | Default | Description |
|-----|---------|-------------|
| `_REGION` | `us-central1` | GCP region |
| `_AR_REPO` | `icassp` | Artifact Registry repo name |
| `_JOB_NAME` | `icassp-scraper` | Cloud Run Job name |
| `_GCS_BUCKET` | *(required)* | GCS bucket for `papers.json` |

## Docker

```bash
# REST API mode (small image, ~200 MB)
docker build --target api -t icassp-scraper:api .
docker run --rm -v "$(pwd)/output:/app/output" icassp-scraper:api
docker run --rm -v "$(pwd)/output:/app/output" icassp-scraper:api \
  --cookie "JSESSIONID=abc123; ..."

# Browser mode (larger image, ~800 MB — Chromium included)
docker build --target browser -t icassp-scraper:browser .
docker run --rm -v "$(pwd)/output:/app/output" icassp-scraper:browser
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--browser` | off | Use Playwright instead of the REST API |
| `--cookie STRING` | — | Session cookie (REST API mode only) |
| `--delay SECONDS` | 1.5 | Pause between requests |
| `--workers N` | 8 | Concurrent workers for affiliation fetching (REST mode) |
| `--no-affiliations` | off | Skip affiliation fetching (faster) |
| `--output DIR` | `./output` | Output directory |

## Notes

- Respect IEEE Xplore's [Terms of Use](https://ieeexplore.ieee.org/Xplorehelp/overview-of-ieee-xplore/terms-of-use).
  The default 1.5 s delay keeps request rates polite.
- If the scraper returns 0 records, the internal API path may have changed;
  inspect XHR traffic on the proceedings page to find the updated URL/params.
