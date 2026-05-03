# ICASSP 2026 Proceedings Scraper

Fetches metadata for all 4,589 papers in the
[ICASSP 2026 proceedings](https://ieeexplore.ieee.org/xpl/conhome/11460365/proceeding)
on IEEE Xplore.

## How it works

The proceedings page is a JavaScript SPA. When you click "Load More", no new
HTML page loads — the button fires a `fetch()` call to the server's internal
REST endpoint and appends the JSON results to the DOM. The scraper calls that
same endpoint directly in a loop, bypassing the button entirely.

**The endpoint is undocumented.** It was inferred by inspecting the browser's
Network tab (DevTools → Network → Fetch/XHR → click "Load More"). The URL,
parameters, and response field names have never been verified against a real
response, because the sandbox this code was written in blocks all outbound
traffic. Before trusting the output, inspect the actual JSON response on your
machine and confirm that field names like `totalRecords`, `records`,
`articleTitle`, `authors`, `doi`, `articleNumber` match reality. If they
differ, update `structure()` in `scraper.py` accordingly.

## Scraping phases

The scraper runs in two sequential phases:

| Phase | Requests | What it fetches |
|-------|----------|-----------------|
| Search | 46 calls (100 papers/page) | title, abstract, authors, DOI, article number |
| Affiliation enrichment | ~4,589 calls (1 per paper) | per-author institution strings |

Affiliations are absent from the search results and require a separate
`GET /rest/document/{articleNumber}/` call per paper. Skip this phase with
`--no-affiliations`.

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

## Two modes

Both modes call the same internal endpoint. The difference is **who makes the
HTTP request**:

### Mode 1 — REST API (faster, ~1–10 min)

Python's `requests` library calls the endpoint directly. Works from most
personal or university networks. May return 403 from cloud/data-centre IPs
because IEEE Xplore blocks them by IP reputation.

```bash
python scraper.py
```

If you get HTTP 403, copy your browser session cookie and pass it:

1. Open the proceedings page in Chrome.
2. Open DevTools → Network → filter by `rest/search`.
3. Click "Load More" → select the request → copy the full `Cookie:` header value.
4. Run:

```bash
python scraper.py --cookie "JSESSIONID=abc123; TS01...=..."
```

### Mode 2 — Browser / Playwright (~10–30 min)

Loads the proceedings page once in headless Chromium to establish a real
browser session (cookies, TLS fingerprint), then calls `fetch()` from inside
the browser's JavaScript context for every request. Because the fetch runs
inside the browser, IEEE Xplore sees it as a same-origin first-party request.

Requires installing Playwright and Chromium (~300 MB):

```bash
pip install playwright
playwright install chromium
python scraper.py --browser
```

## Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # runtime deps only
pip install -r requirements-dev.txt      # adds ruff + mypy for development
```

## Lint and type-check

```bash
ruff check scraper.py
python -m mypy scraper.py --ignore-missing-imports
```

## Docker

The Dockerfile has three targets:

| Target | Size | Use |
|--------|------|-----|
| `lint` | ~200 MB | Runs ruff + mypy; fails the build if checks don't pass |
| `api` | ~200 MB | REST API mode; runs as non-root user |
| `browser` | ~800 MB | Browser mode; includes Chromium |

```bash
# REST API mode
docker build --target api -t icassp-scraper:api .
docker run --rm -v "$(pwd)/output:/app/output" icassp-scraper:api
docker run --rm -v "$(pwd)/output:/app/output" icassp-scraper:api \
  --cookie "JSESSIONID=abc123; ..."

# Browser mode
docker build --target browser -t icassp-scraper:browser .
docker run --rm -v "$(pwd)/output:/app/output" icassp-scraper:browser
```

## Google Cloud Run

`cloudbuild.yaml` builds, deploys, and runs the scraper as a Cloud Run Job.
Output is written directly to a GCS bucket mounted at `/app/output`.

**One-time setup:**
```bash
gcloud artifacts repositories create icassp \
  --repository-format=docker --location=us-central1
gcloud storage buckets create gs://MY_BUCKET --location=us-central1
```

**Submit a build:**
```bash
gcloud builds submit --config cloudbuild.yaml \
  --substitutions _GCS_BUCKET=MY_BUCKET
```

Cloud Build steps (in order): lint → typecheck → docker build (`browser`
target) → push to Artifact Registry → deploy Cloud Run Job → execute and wait.
`papers.json` appears in `gs://MY_BUCKET/` when the job completes.

> **Note:** Cloud Run runs on GCP infrastructure. IEEE Xplore may block GCP
> IPs even in browser mode since the requests still originate from a
> data-centre IP range. If the job returns 0 results, run the scraper locally
> instead and upload the output to GCS manually.

Available `--substitutions`:

| Key | Default | Description |
|-----|---------|-------------|
| `_REGION` | `us-central1` | GCP region |
| `_AR_REPO` | `icassp` | Artifact Registry repository name |
| `_JOB_NAME` | `icassp-scraper` | Cloud Run Job name |
| `_GCS_BUCKET` | *(required)* | GCS bucket for `papers.json` |

## All options

| Flag | Default | Description |
|------|---------|-------------|
| `--browser` | off | Use Playwright/Chromium instead of the REST API |
| `--cookie STRING` | — | Full `Cookie:` header value (REST API mode only) |
| `--delay SECONDS` | 1.5 | Pause between requests |
| `--workers N` | 8 | Concurrent workers for affiliation fetching (REST mode only) |
| `--no-affiliations` | off | Skip affiliation fetching; `affiliation` fields will be empty strings |
| `--output DIR` | `./output` | Directory for `papers.json` |

## Caveats

- The internal IEEE Xplore API is undocumented and may change without notice.
  If the scraper returns 0 results, inspect XHR traffic in DevTools to find
  the updated endpoint or parameters.
- Respect IEEE Xplore's [Terms of Use](https://ieeexplore.ieee.org/Xplorehelp/overview-of-ieee-xplore/terms-of-use).
  The default 1.5 s delay keeps request rates polite.
